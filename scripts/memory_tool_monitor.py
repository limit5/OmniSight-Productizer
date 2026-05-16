#!/usr/bin/env python3
"""OP-908 -- Memory Tool eviction monitoring and cap proposal helper."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_ROOT = Path("/var/omnisight/memory")
DEFAULT_FLEETS = ("claude", "codex", "merger")
DEFAULT_CAP_MB = 100
DEFAULT_METRICS = DEFAULT_ROOT / "metrics.jsonl"
DEFAULT_PROPOSALS = Path("docs/audit/memory-cap-proposals.md")
STALE_DAYS = 30
WARN_PCT = 70.0
PAGE_PCT = 90.0
EVICT_PCT = 100.0
CAP_STEP_MB = 10
PROPOSAL_CONFLICT_HOURS = 24

log = logging.getLogger("memory_tool_monitor")


@dataclass(frozen=True)
class StaleFile:
    path: str
    unread_days: int
    bytes: int


@dataclass(frozen=True)
class FleetMetric:
    type: str
    op: str
    fleet: str
    path: str
    bytes: int
    file_count: int
    cap_bytes: int
    pct_cap: float
    action: str
    stale_files: list[StaleFile] = field(default_factory=list)
    timestamp: str = ""
    error: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


def _cap_bytes(cap_mb: int) -> int:
    return cap_mb * 1024 * 1024


def load_allowlist(paths: Sequence[Path], inline: str) -> set[str]:
    allowed = {item.strip() for item in inline.split(",") if item.strip()}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if item and not item.startswith("#"):
                allowed.add(item)
    return allowed


def classify_action(pct_cap: float) -> str:
    if pct_cap >= EVICT_PCT:
        return "evict_required"
    if pct_cap >= PAGE_PCT:
        return "page_operator"
    if pct_cap >= WARN_PCT:
        return "warn"
    return "ok"


def scan_fleet(
    *,
    root: Path,
    fleet: str,
    cap_mb: int,
    now: datetime,
    stale_allowlist: set[str],
) -> FleetMetric:
    fleet_dir = root / fleet
    cap = _cap_bytes(cap_mb)
    total = 0
    count = 0
    stale: list[StaleFile] = []
    cutoff = now - timedelta(days=STALE_DAYS)
    try:
        files = [p for p in fleet_dir.rglob("*") if p.is_file()]
        for path in files:
            stat = path.stat()
            total += stat.st_size
            count += 1
            rel = path.relative_to(fleet_dir).as_posix()
            model_path = f"/memories/{rel}"
            if rel in stale_allowlist or model_path in stale_allowlist:
                continue
            atime = datetime.fromtimestamp(stat.st_atime, tz=timezone.utc)
            if atime < cutoff:
                stale.append(StaleFile(
                    path=model_path,
                    unread_days=(now - atime).days,
                    bytes=stat.st_size,
                ))
    except OSError as exc:
        log.error("MonitorReadFailed fleet=%s path=%s error=%s", fleet, fleet_dir, exc)
        return FleetMetric(
            type="memory_tool_monitor",
            op="scan",
            fleet=fleet,
            path=str(fleet_dir),
            bytes=0,
            file_count=0,
            cap_bytes=cap,
            pct_cap=0.0,
            action="MonitorReadFailed",
            timestamp=_iso(now),
            error="MonitorReadFailed",
        )
    pct = (total / cap * 100.0) if cap else 0.0
    action = classify_action(pct)
    if action == "warn":
        log.warning("memory fleet %s at %.1f%% cap", fleet, pct)
    elif action == "page_operator":
        log.error("memory fleet %s at %.1f%% cap; page operator", fleet, pct)
    elif action == "evict_required":
        log.error("memory fleet %s at %.1f%% cap; C1 eviction required", fleet, pct)
    return FleetMetric(
        type="memory_tool_monitor",
        op="scan",
        fleet=fleet,
        path=str(fleet_dir),
        bytes=total,
        file_count=count,
        cap_bytes=cap,
        pct_cap=round(pct, 3),
        action=action,
        stale_files=stale,
        timestamp=_iso(now),
    )


def append_metrics(path: Path, metrics: Iterable[FleetMetric]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for metric in metrics:
            payload = asdict(metric)
            payload["stale_files"] = [asdict(item) for item in metric.stale_files]
            fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _row_timestamp(row: dict) -> datetime | None:
    raw = row.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def eviction_counts_by_day(paths: Sequence[Path], now: datetime) -> dict[str, dict[str, int]]:
    start = (now - timedelta(days=3)).date()
    counts: dict[str, dict[str, int]] = {}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("op") not in {"evict", "MemoryCapExceeded"}:
                continue
            fleet = row.get("fleet")
            if not isinstance(fleet, str) or not fleet:
                continue
            ts = _row_timestamp(row)
            if ts is None or ts.date() < start:
                continue
            day = ts.date().isoformat()
            counts.setdefault(fleet, {}).setdefault(day, 0)
            counts[fleet][day] += 1
    return counts


def needs_cap_proposal(day_counts: dict[str, int], now: datetime) -> bool:
    days = [(now.date() - timedelta(days=offset)).isoformat() for offset in (1, 2, 3)]
    return all(day_counts.get(day, 0) > 5 for day in days)


def proposal_conflicts(path: Path, fleet: str, now: datetime) -> bool:
    if not path.exists():
        return False
    cutoff = now - timedelta(hours=PROPOSAL_CONFLICT_HOURS)
    for line in path.read_text(encoding="utf-8").splitlines():
        if f"fleet={fleet}" not in line:
            continue
        marker = "created_at="
        if marker not in line:
            continue
        raw = line.split(marker, 1)[1].split()[0]
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created >= cutoff:
            log.warning("CapAutoScaleProposalConflict fleet=%s created_at=%s", fleet, raw)
            return True
    return False


def append_cap_proposals(
    *,
    path: Path,
    counts: dict[str, dict[str, int]],
    cap_mb: int,
    now: datetime,
) -> list[str]:
    proposed: list[str] = []
    for fleet, day_counts in sorted(counts.items()):
        if not needs_cap_proposal(day_counts, now):
            continue
        if proposal_conflicts(path, fleet, now):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# Memory Tool cap proposals\n\n", encoding="utf-8")
        line = (
            f"- created_at={_iso(now)} fleet={fleet} current_cap_mb={cap_mb} "
            f"proposed_cap_mb={cap_mb + CAP_STEP_MB} reason=eviction_count_gt_5_per_day_for_3_days "
            f"operator_status=pending\n"
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        proposed.append(fleet)
    return proposed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("OMNISIGHT_MEMORY_TOOL_ROOT", DEFAULT_ROOT)),
    )
    parser.add_argument(
        "--fleets",
        default=os.environ.get("OMNISIGHT_MEMORY_TOOL_FLEETS", " ".join(DEFAULT_FLEETS)),
    )
    parser.add_argument(
        "--cap-mb",
        type=int,
        default=int(os.environ.get("OMNISIGHT_MEMORY_TOOL_CAP_MB", DEFAULT_CAP_MB)),
    )
    parser.add_argument(
        "--metrics",
        type=Path,
        default=Path(os.environ.get("OMNISIGHT_MEMORY_TOOL_METRICS", DEFAULT_METRICS)),
    )
    parser.add_argument("--proposal-file", type=Path, default=DEFAULT_PROPOSALS)
    parser.add_argument("--audit-log", action="append", type=Path, default=[])
    parser.add_argument("--stale-allowlist", action="append", type=Path, default=[])
    parser.add_argument(
        "--stale-allowlist-entry",
        default=os.environ.get("OMNISIGHT_MEMORY_STALE_ALLOWLIST", ""),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    now = _utcnow()
    fleets = tuple(item for item in args.fleets.split() if item)
    allowlist = load_allowlist(args.stale_allowlist, args.stale_allowlist_entry)
    metrics = [
        scan_fleet(
            root=args.root,
            fleet=fleet,
            cap_mb=args.cap_mb,
            now=now,
            stale_allowlist=allowlist,
        )
        for fleet in fleets
    ]
    append_metrics(args.metrics, metrics)
    counts = eviction_counts_by_day(args.audit_log, now)
    proposed = append_cap_proposals(
        path=args.proposal_file,
        counts=counts,
        cap_mb=args.cap_mb,
        now=now,
    )
    for fleet in proposed:
        log.warning("cap proposal created for fleet=%s", fleet)
    return 1 if any(metric.error for metric in metrics) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
