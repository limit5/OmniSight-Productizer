#!/usr/bin/env python3
"""OP-746 -- daily conflict-rate observability report.

Emits two artefacts to the operator's log dir:

  /var/log/omnisight/conflict-report-<YYYY-MM-DD>.json   (machine)
  /var/log/omnisight/conflict-report-<YYYY-MM-DD>.txt    (human)

The JSON form feeds the operator dashboard tile + alert threshold
checks; the human form is what an oncall reads first thing in the
morning.

Inputs
------
1. ``conflict_observations`` table (Postgres / SQLite)
   - source of truth for every conflict-equivalent event
   - one row per (Verified -1 vote | sibling-merged rebase conflict |
     manual rebase)

2. Bridge structured-log file (newline-delimited JSON)
   - parsed for ``event=ps_merged_metrics`` records
   - feeds median PS lifetime, median rebase count, total merged
   - default path: ``/home/user/work/sora/logs/bridge/systemd.log``
     (the same path the OP-715 / OP-717 systemd unit writes to)

Alerting
--------
The same script writes one alert envelope per threshold breach into
the JSON output under the ``alerts`` key. The systemd timer companion
(``deploy/systemd/conflict-report.service``) pipes those into
``backend.agents.operator_notifier`` so OP-722 fans them out by
severity. The thresholds (per OP-746 spec):

  hourly_rate     > 30 % of PSes over last 4 hourly buckets  → DEGRADED
  same_file_24h   ≥ 5 events / 24h on a single file          → CRITICAL
  median_lifetime > 4h                                       → WARN

CLI
---
  conflict_report.py                       run for "yesterday" UTC
  conflict_report.py --date 2026-05-09     run for a specific UTC day
  conflict_report.py --window-hours 24     change the window length
  conflict_report.py --bridge-log PATH     point at a non-default log
  conflict_report.py --output-dir DIR      override the default
                                            /var/log/omnisight
  conflict_report.py --skip-alerts         do not call the notifier
                                            (used by tests)
  conflict_report.py --dry-run             print the JSON to stdout
                                            instead of writing files
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents.conflict_observations import (  # noqa: E402
    ConflictObservation,
    fetch_recent_observations_sync,
    file_event_counts,
    hourly_buckets,
    summarise,
)


DEFAULT_BRIDGE_LOG = "/home/user/work/sora/logs/bridge/systemd.log"
DEFAULT_OUTPUT_DIR = "/var/log/omnisight"

# Spec thresholds -- centralised so the AC tests can pin them.
ALERT_HOURLY_RATE_THRESHOLD = 0.30
ALERT_HOURLY_BUCKET_COUNT = 4
ALERT_SAME_FILE_THRESHOLD = 5
ALERT_MEDIAN_LIFETIME_MIN = 4 * 60  # 4 hours expressed in minutes


@dataclass(frozen=True)
class PsMergedMetrics:
    """One ``ps_merged_metrics`` record reconstituted from a log line."""

    ts: datetime
    change_id: str
    ticket: str | None
    lifetime_min: float | None
    patchset_count: int
    rebase_count: int
    rework_count: int
    final_diff_size: int
    verified_minus_one_count: int


@dataclass(frozen=True)
class AlertEnvelope:
    severity: str
    code: str
    message: str
    context: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "context": dict(self.context),
        }


# ── Bridge log parsing ───────────────────────────────────────────────


def parse_bridge_log(
    path: Path,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[PsMergedMetrics]:
    """Return ``ps_merged_metrics`` records inside ``[window_start, window_end]``.

    Lines that don't parse as JSON or that describe other events are
    silently skipped — the bridge log mixes many event types.
    """
    if not path.exists():
        return []
    out: list[PsMergedMetrics] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or "ps_merged_metrics" not in raw:
                continue
            # Bridge log lines may have a systemd/python prefix
            # ("YYYY-MM-DD HH:MM:SS LEVEL name: {json}"). Find the first
            # ``{`` and parse from there to be robust to the prefix.
            brace = raw.find("{")
            if brace < 0:
                continue
            try:
                payload = json.loads(raw[brace:])
            except json.JSONDecodeError:
                continue
            if payload.get("event") != "ps_merged_metrics":
                continue
            ts = _parse_iso(payload.get("timestamp"))
            if ts is None:
                continue
            if ts < window_start or ts > window_end:
                continue
            out.append(
                PsMergedMetrics(
                    ts=ts,
                    change_id=str(payload.get("change_id") or ""),
                    ticket=payload.get("ticket") or None,
                    lifetime_min=_coerce_float(payload.get("lifetime_min")),
                    patchset_count=_coerce_int_default(payload.get("patchset_count"), 0),
                    rebase_count=_coerce_int_default(payload.get("rebase_count"), 0),
                    rework_count=_coerce_int_default(payload.get("rework_count"), 0),
                    final_diff_size=_coerce_int_default(payload.get("final_diff_size"), 0),
                    verified_minus_one_count=_coerce_int_default(
                        payload.get("verified_minus_one_count"), 0,
                    ),
                )
            )
    return out


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int_default(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Stats ────────────────────────────────────────────────────────────


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    n = len(sorted_values)
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_values[mid])
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2.0


# ── Report assembly ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ReportInputs:
    window_start: datetime
    window_end: datetime
    observations: list[ConflictObservation]
    ps_merged: list[PsMergedMetrics]


def build_report(inputs: ReportInputs) -> dict[str, Any]:
    """Compose the JSON report payload from raw inputs.

    Pure function: takes already-loaded inputs and returns the dict
    that gets written to disk + fed to alerting. The CLI entry point
    handles I/O.
    """
    summary = summarise(
        inputs.observations,
        window_start=inputs.window_start,
        window_end=inputs.window_end,
    )
    lifetime_values = [
        m.lifetime_min for m in inputs.ps_merged if m.lifetime_min is not None
    ]
    rebase_values = [m.rebase_count for m in inputs.ps_merged]
    median_lifetime = median(lifetime_values)
    median_rebase = median([float(v) for v in rebase_values])

    total_pushed = len(inputs.ps_merged)  # we only see merged events;
    # "pushed" tracking would require patchset-created log lines too.
    total_merged = len(inputs.ps_merged)

    pses_with_conflict_change_ids: set[str] = set()
    for obs in inputs.observations:
        if obs.ps_change_id:
            pses_with_conflict_change_ids.add(obs.ps_change_id)

    pct_pses_hit = (
        (len(pses_with_conflict_change_ids) / total_merged) * 100
        if total_merged
        else 0.0
    )

    buckets = hourly_buckets(
        inputs.observations,
        window_end=inputs.window_end,
        bucket_count=ALERT_HOURLY_BUCKET_COUNT,
    )

    alerts = compute_alerts(
        observations=inputs.observations,
        ps_merged=inputs.ps_merged,
        window_end=inputs.window_end,
        median_lifetime_min=median_lifetime,
        hourly_buckets_=buckets,
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_start": inputs.window_start.isoformat(),
        "window_end": inputs.window_end.isoformat(),
        "totals": {
            "ps_pushed": total_pushed,
            "ps_merged": total_merged,
            "conflict_events": summary.total_events,
            "pses_with_conflict": len(pses_with_conflict_change_ids),
            "pct_pses_hit_at_least_one": round(pct_pses_hit, 1),
        },
        "medians": {
            "ps_lifetime_min": (
                round(median_lifetime, 2) if median_lifetime is not None else None
            ),
            "rebase_count_per_ps": (
                round(median_rebase, 2) if median_rebase is not None else None
            ),
        },
        "by_cause": dict(summary.by_cause),
        "hotspots": [
            {"file": h.file, "events": h.events} for h in summary.hotspots
        ],
        "hourly_buckets_4h": list(buckets),
        "alerts": [a.to_payload() for a in alerts],
    }


# ── Alerts ───────────────────────────────────────────────────────────


def compute_alerts(
    *,
    observations: list[ConflictObservation],
    ps_merged: list[PsMergedMetrics],
    window_end: datetime,
    median_lifetime_min: float | None,
    hourly_buckets_: list[int],
) -> list[AlertEnvelope]:
    """Return the alert envelopes implied by the spec's three thresholds.

    Caller decides whether to dispatch — tests pass the same envelopes
    through ``compute_alerts`` and assert on the list shape.
    """
    alerts: list[AlertEnvelope] = []

    # Hourly rate over last 4 buckets → DEGRADED
    # Approximate the denominator with merged-PS counts in the same
    # 4h window (best-effort; sufficient for "is conflict rate
    # spiking" alerting).
    last_4h_end = window_end
    last_4h_start = window_end - timedelta(hours=ALERT_HOURLY_BUCKET_COUNT)
    pses_in_window = [m for m in ps_merged if last_4h_start <= m.ts <= last_4h_end]
    conflicts_in_window = sum(hourly_buckets_)
    if pses_in_window:
        rate = conflicts_in_window / float(len(pses_in_window))
        if rate > ALERT_HOURLY_RATE_THRESHOLD:
            alerts.append(
                AlertEnvelope(
                    severity="DEGRADED",
                    code="conflict_rate_high_4h",
                    message=(
                        f"Hourly conflict rate {round(rate * 100, 1)}% over the "
                        f"last {ALERT_HOURLY_BUCKET_COUNT} hours "
                        f"(threshold {round(ALERT_HOURLY_RATE_THRESHOLD * 100, 0)}%)"
                    ),
                    context={
                        "rate": round(rate, 4),
                        "conflicts": conflicts_in_window,
                        "pses": len(pses_in_window),
                        "buckets": list(hourly_buckets_),
                    },
                )
            )

    # Same-file ≥ 5 in 24h → CRITICAL (one alert per offending file)
    counts = file_event_counts(observations)
    for path, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if count >= ALERT_SAME_FILE_THRESHOLD:
            alerts.append(
                AlertEnvelope(
                    severity="CRITICAL",
                    code="conflict_hotspot_same_file_24h",
                    message=(
                        f"File '{path}' hit {count} conflict events in the "
                        f"reporting window — likely structural issue."
                    ),
                    context={
                        "file": path,
                        "count": count,
                        "threshold": ALERT_SAME_FILE_THRESHOLD,
                    },
                )
            )

    # Median PS lifetime > 4h → WARN
    if (
        median_lifetime_min is not None
        and median_lifetime_min > ALERT_MEDIAN_LIFETIME_MIN
    ):
        alerts.append(
            AlertEnvelope(
                severity="WARN",
                code="ps_lifetime_high",
                message=(
                    f"Median PS lifetime {round(median_lifetime_min / 60.0, 2)}h "
                    f"(threshold {ALERT_MEDIAN_LIFETIME_MIN // 60}h) — review "
                    "queue may be building up."
                ),
                context={
                    "median_lifetime_min": round(median_lifetime_min, 2),
                    "threshold_min": ALERT_MEDIAN_LIFETIME_MIN,
                },
            )
        )

    return alerts


# ── Human-readable ───────────────────────────────────────────────────


def render_text(report: dict[str, Any]) -> str:
    totals = report["totals"]
    medians = report["medians"]
    lines = [
        f"Daily Conflict Report ({report['window_end'][:10]})",
        "==================================",
        f"Window:                      {report['window_start']} → {report['window_end']}",
        f"Total PSes merged:           {totals['ps_merged']}",
        f"Median PS lifetime (min):    {medians['ps_lifetime_min']}",
        f"Median rebase count per PS:  {medians['rebase_count_per_ps']}",
        f"Conflict events:             {totals['conflict_events']} "
        f"({totals['pct_pses_hit_at_least_one']}% of PSes hit at least 1)",
        "",
        "By cause:",
    ]
    for cause, count in sorted((report.get("by_cause") or {}).items()):
        lines.append(f"  {cause:25s} {count}")
    lines.append("")
    lines.append("Top conflict hotspots:")
    if not report["hotspots"]:
        lines.append("  (none)")
    else:
        for h in report["hotspots"]:
            lines.append(f"  {h['file']:35s} {h['events']} events")
    lines.append("")
    lines.append(f"Hourly buckets (last 4h): {report['hourly_buckets_4h']}")
    lines.append("")
    if report["alerts"]:
        lines.append("Alerts:")
        for a in report["alerts"]:
            lines.append(f"  [{a['severity']}] {a['code']}: {a['message']}")
    else:
        lines.append("Alerts: none")
    return "\n".join(lines) + "\n"


# ── Notifier dispatch ────────────────────────────────────────────────


def dispatch_alerts(
    alerts: Iterable[AlertEnvelope],
    *,
    notifier: Any | None = None,
    flush: bool = True,
) -> int:
    """Forward each alert through the OP-722 operator notifier bridge.

    Returns the count dispatched. Callers that don't want a real
    notifier (tests, ``--skip-alerts``) pass ``notifier=None`` and the
    function becomes a no-op.
    """
    if notifier is None:
        return 0
    count = 0
    for a in alerts:
        notifier.notify(a.severity, a.code, a.message, context=a.context)
        count += 1
    if flush:
        try:
            notifier.flush_all()
        except Exception:
            logging.getLogger(__name__).exception(
                "operator_notifier flush_all raised; alerts may be deferred",
            )
    return count


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        help=(
            "UTC date the report covers (YYYY-MM-DD). Defaults to the "
            "previous full UTC day so a 09:00 UTC cron run reports on "
            "the previous calendar day's traffic."
        ),
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=24,
        help="Window length in hours (default 24)",
    )
    parser.add_argument(
        "--bridge-log",
        default=os.environ.get("OMNISIGHT_BRIDGE_LOG", DEFAULT_BRIDGE_LOG),
        help=f"Path to the bridge structured log (default {DEFAULT_BRIDGE_LOG})",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("OMNISIGHT_CONFLICT_REPORT_DIR", DEFAULT_OUTPUT_DIR),
        help=f"Output directory (default {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--skip-alerts",
        action="store_true",
        help="Do not dispatch alerts via OP-722 notifier",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON report to stdout, write nothing to disk",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("OMNISIGHT_LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("omnisight.conflict_report")

    window_end = _resolve_window_end(args.date)
    window_start = window_end - timedelta(hours=args.window_hours)

    log.info(
        "computing report window=%s..%s",
        window_start.isoformat(), window_end.isoformat(),
    )

    observations = fetch_recent_observations_sync(since=window_start)
    observations = [
        o for o in observations if o.ts <= window_end
    ]

    ps_merged = parse_bridge_log(
        Path(args.bridge_log),
        window_start=window_start,
        window_end=window_end,
    )

    report = build_report(
        ReportInputs(
            window_start=window_start,
            window_end=window_end,
            observations=observations,
            ps_merged=ps_merged,
        )
    )

    log.info(
        "report ready: events=%d ps_merged=%d alerts=%d",
        report["totals"]["conflict_events"],
        report["totals"]["ps_merged"],
        len(report["alerts"]),
    )

    if args.dry_run:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        date_str = window_end.date().isoformat()
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"conflict-report-{date_str}.json"
        text_path = out_dir / f"conflict-report-{date_str}.txt"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        text_path.write_text(render_text(report))
        log.info("wrote %s and %s", json_path, text_path)

    if not args.skip_alerts and report["alerts"]:
        try:
            from backend.agents.operator_notifier import build_notifier_from_env
            notifier = build_notifier_from_env()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "could not build operator notifier; skipping alert dispatch: %s",
                exc,
            )
        else:
            envelopes = [
                AlertEnvelope(
                    severity=a["severity"],
                    code=a["code"],
                    message=a["message"],
                    context=a["context"],
                )
                for a in report["alerts"]
            ]
            dispatch_alerts(envelopes, notifier=notifier)

    return 0


def _resolve_window_end(raw: str | None) -> datetime:
    """Default to the end of the previous full UTC day so a 09:00 UTC
    cron run reports on the prior calendar day's traffic."""
    if raw:
        day = datetime.strptime(raw, "%Y-%m-%d").date()
        return datetime(
            day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc,
        )
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    return datetime(
        yesterday.year, yesterday.month, yesterday.day, 23, 59, 59,
        tzinfo=timezone.utc,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
