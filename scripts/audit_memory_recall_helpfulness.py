#!/usr/bin/env python3
"""OP-867 (Sprint X / X3) — C1 Memory Tool recall helpfulness audit.

Post-merge follow-up to OP-851 (C1 Anthropic Memory Tool standalone
integration). The C1 ship enabled tier:S / tier:M auto-recall and
tier:L opt-in; this script answers the operator question

    "after a week of production data, are the auto-recalls actually
    helpful, or are they noise that's burning context for nothing?"

How it works
============
1.  **Load** — walks one or more JSONL ``progress.txt`` files emitted
    by ``backend.agents.memory_tool_handler.MemoryToolHandler._audit``.
    The expected row shape is::

        {"type": "memory_tool", "tool": "memory", "op": "read|list|...",
         "key": "/memories/...", "timestamp": "ISO8601",
         "ticket_key": "OP-...", "tier": "S|M|L|X", ...}

2.  **Filter** to *auto-recall* events — rows where ``op`` is ``read``
    or ``list`` (i.e. the model actually pulled memory content back
    into context) AND ``tier`` is ``S`` or ``M`` (the auto-recall
    tiers — tier:L requires explicit opt-in and tier:X is always
    refused, so those don't count as auto-recall noise candidates).
    Rows older than the audit window (default 7 days back from
    ``--now``) are dropped.

3.  **Sample** — randomly pick ``--sample-size`` (default 30) rows
    with a seeded RNG so the operator can reproduce the same draw.
    If the filtered population is smaller than the sample target,
    :class:`InsufficientSamplesForAudit` is raised — the operator
    waits for more data per the OP-867 error catalog.

4.  **Score** — for each sampled event, the operator enters an
    integer 1–5 (1 = harmful / wasted context; 5 = clearly useful).
    Scores can come from:
       * interactive prompt (``--interactive``, default when stdin
         is a TTY)
       * pre-filled JSON file (``--scores path/to/scores.json``)
         for CI / reproducibility — schema ``{"<key>": int, ...}``.

5.  **Aggregate** — emits average, stdev, and a per-tier breakdown.
    Per OP-867 AC #3, if the mean is **< 3.0**, the decision is
    ``TIGHTEN_TIER_M`` (file a follow-up to require label opt-in
    for tier:M, mirroring tier:L). Otherwise ``KEEP_AS_IS``.

Error catalog (per ticket §"Error catalog")
===========================================
* :class:`C1NotYetMerged` — ``backend.agents.memory_tool_handler``
  cannot be imported, or its marker constants are absent. Caller
  should defer the ticket via ``transition_back_to_todo``.
* :class:`InsufficientSamplesForAudit` — fewer than the requested
  sample size of qualifying auto-recall events in the window. The
  operator waits for more data and re-runs the script next week.

Usage
=====

    # interactive scoring against the last 7 days of audit logs:
    python scripts/audit_memory_recall_helpfulness.py \
        --progress data/sdk-launcher/progress.txt \
        --output data/op-867-audit-result.json

    # reproducible / CI mode with pre-filled scores:
    python scripts/audit_memory_recall_helpfulness.py \
        --progress data/sdk-launcher/progress.txt \
        --scores data/op-867-scores.json \
        --output data/op-867-audit-result.json

The script is a deliberately small piece of glue — it is **not**
re-scoring memory content; that is the operator's job. The script
samples, prompts, aggregates, and writes the decision artefact that
feeds ``docs/research/c1-memory-recall-audit-2026-05.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

log = logging.getLogger("audit_memory_recall")

# ── Decision constants (AC #3) ────────────────────────────────────────

DEFAULT_SAMPLE_SIZE = 30
DEFAULT_WINDOW_DAYS = 7
HELPFUL_THRESHOLD = 3.0
SCORE_MIN = 1
SCORE_MAX = 5

# Auto-recall ops — only these contribute memory content to model
# context. Other ops (write/delete/evict/tier_refuse/error:*) are
# operational events, not recall noise candidates.
AUTO_RECALL_OPS: frozenset[str] = frozenset({"read", "list"})
AUTO_RECALL_TIERS: frozenset[str] = frozenset({"S", "M"})

# Verdicts (AC #3).
DECISION_TIGHTEN = "TIGHTEN_TIER_M"
DECISION_KEEP = "KEEP_AS_IS"


# ── Error catalog (per ticket) ────────────────────────────────────────


class AuditError(Exception):
    """Base class for OP-867 error catalog codes."""

    code: str = "audit_error"


class C1NotYetMerged(AuditError):
    """The C1 handler isn't importable — defer ticket per error catalog."""

    code = "c1_not_yet_merged"


class InsufficientSamplesForAudit(AuditError):
    """Fewer than --sample-size qualifying events — wait for more data."""

    code = "insufficient_samples_for_audit"


# ── Data shapes ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuditEvent:
    """One auto-recall candidate row (post-filter)."""

    key: str
    op: str
    tier: str
    timestamp: str
    ticket_key: str | None
    raw: dict[str, Any]


@dataclass
class AuditResult:
    """JSON sidecar emitted by :func:`run_audit`."""

    window_start: str
    window_end: str
    total_events_in_window: int
    sampled_event_count: int
    seed: int
    mean_score: float
    stdev_score: float | None
    per_tier_mean: dict[str, float]
    decision: str
    decision_reason: str
    scores: list[dict[str, Any]] = field(default_factory=list)


# ── C1 readiness check (error catalog: C1NotYetMerged) ───────────────


def verify_c1_merged() -> None:
    """Raise :class:`C1NotYetMerged` if the C1 handler is unavailable.

    The ticket spec marks this as a "wait + retry next week" path,
    so the caller (typically the runner) transitions the ticket back
    to TODO with the error code as the reason.
    """
    try:
        # Imported lazily so the script remains importable in any
        # environment where the audit harness is run on a checkpoint
        # of the repo where C1 is not yet present.
        from backend.agents import memory_tool_handler  # noqa: F401
    except ImportError as exc:
        raise C1NotYetMerged(
            f"backend.agents.memory_tool_handler not importable: {exc}"
        ) from exc
    # Spot-check a marker constant; guards against a half-merged stub.
    if not getattr(memory_tool_handler, "AUDIT_TYPE", None):
        raise C1NotYetMerged(
            "memory_tool_handler.AUDIT_TYPE missing — C1 not fully merged"
        )


# ── Load + filter (AC #1) ────────────────────────────────────────────


def _parse_iso(ts: str) -> datetime | None:
    """Best-effort ISO-8601 parse; trailing Z accepted; None on garbage."""
    if not isinstance(ts, str) or not ts:
        return None
    raw = ts.rstrip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_audit_events(
    paths: Iterable[Path],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[AuditEvent]:
    """Return auto-recall events from ``paths`` inside the window.

    Lines that don't parse, don't carry ``type=memory_tool``, fall
    outside the window, or aren't auto-recall candidates (op + tier
    filter) are silently dropped. The audit window is half-open
    ``[start, end)`` — events at exactly ``start`` are included.
    """
    events: list[AuditEvent] = []
    for path in paths:
        if not path.exists():
            log.warning("audit log missing, skipping: %s", path)
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                log.warning("%s:%d not JSON; skipped", path, lineno)
                continue
            if not isinstance(row, dict):
                continue
            if row.get("type") != "memory_tool":
                continue
            op = row.get("op")
            if op not in AUTO_RECALL_OPS:
                continue
            tier = row.get("tier")
            # ``list`` rows don't carry a tier in C1 (they describe
            # a directory; tier-refuse rows are filtered separately).
            # Treat tierless list rows as recallable-tier-mixed
            # because the directory output already only contains
            # recallable entries (tier_refuse rows are emitted for
            # the hidden ones). For *helpfulness* scoring we want
            # only ``read`` rows where a concrete tier was returned.
            if op == "read" and tier not in AUTO_RECALL_TIERS:
                continue
            ts = _parse_iso(row.get("timestamp", ""))
            if ts is None:
                continue
            if ts < window_start or ts >= window_end:
                continue
            events.append(AuditEvent(
                key=str(row.get("key", "")),
                op=op,
                tier=str(tier or ""),
                timestamp=row.get("timestamp", ""),
                ticket_key=row.get("ticket_key"),
                raw=row,
            ))
    return events


# ── Sample (AC #2) ───────────────────────────────────────────────────


def sample_events(
    events: list[AuditEvent],
    *,
    size: int,
    seed: int,
) -> list[AuditEvent]:
    """Random-sample without replacement; raises if population < size."""
    if len(events) < size:
        raise InsufficientSamplesForAudit(
            f"only {len(events)} qualifying events; need {size} "
            "— wait for more data before re-running."
        )
    rng = random.Random(seed)
    return rng.sample(events, size)


# ── Score (AC #2) ────────────────────────────────────────────────────


def _validate_score(raw: Any) -> int:
    if isinstance(raw, bool):  # bool is an int subclass — reject early
        raise ValueError(f"score must be int 1–5, got bool: {raw!r}")
    if not isinstance(raw, int):
        raise ValueError(f"score must be int 1–5, got {type(raw).__name__}")
    if raw < SCORE_MIN or raw > SCORE_MAX:
        raise ValueError(f"score must be in [{SCORE_MIN},{SCORE_MAX}], got {raw}")
    return raw


def collect_scores_from_file(
    sampled: list[AuditEvent], scores_path: Path
) -> list[int]:
    """Load operator scores from a JSON ``{key: int}`` mapping.

    Every sampled event's ``key`` MUST be present in the file —
    partial scoring is rejected so CI cannot silently drop events.
    """
    data = json.loads(scores_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(
            f"scores file {scores_path} must be a JSON object "
            "mapping key -> int"
        )
    out: list[int] = []
    missing: list[str] = []
    for ev in sampled:
        if ev.key not in data:
            missing.append(ev.key)
            continue
        out.append(_validate_score(data[ev.key]))
    if missing:
        raise ValueError(
            f"scores file is missing {len(missing)} sampled keys; "
            f"first few: {missing[:5]}"
        )
    return out


def collect_scores_interactive(
    sampled: list[AuditEvent],
    *,
    input_fn: Any = input,
    output_fn: Any = print,
) -> list[int]:
    """Prompt the operator for one int 1–5 per sampled event.

    ``input_fn`` / ``output_fn`` are injected so the unit test can
    drive the loop deterministically without binding to stdin/stdout.
    """
    out: list[int] = []
    for idx, ev in enumerate(sampled, 1):
        output_fn(
            f"[{idx}/{len(sampled)}] tier={ev.tier} key={ev.key} "
            f"ticket={ev.ticket_key or '?'} ts={ev.timestamp}"
        )
        while True:
            raw = input_fn("  helpfulness 1–5 (1=harmful 5=clearly useful): ")
            try:
                out.append(_validate_score(int(raw.strip())))
                break
            except (ValueError, AttributeError) as exc:
                output_fn(f"  invalid input: {exc} — please re-enter")
    return out


# ── Aggregate + decide (AC #3) ───────────────────────────────────────


def decide_verdict(mean: float) -> tuple[str, str]:
    """Return ``(decision_code, human_reason)``."""
    if mean < HELPFUL_THRESHOLD:
        return DECISION_TIGHTEN, (
            f"mean helpfulness {mean:.2f} < {HELPFUL_THRESHOLD} — "
            "auto-recall is net-noise; tighten tier:M to require "
            "label opt-in (mirror tier:L)."
        )
    return DECISION_KEEP, (
        f"mean helpfulness {mean:.2f} >= {HELPFUL_THRESHOLD} — "
        "auto-recall is net-positive; keep current tier:S/M default-on."
    )


def aggregate(
    sampled: list[AuditEvent],
    scores: list[int],
    *,
    window_start: datetime,
    window_end: datetime,
    total_in_window: int,
    seed: int,
) -> AuditResult:
    if len(sampled) != len(scores):
        raise ValueError(
            f"sampled/scores length mismatch: "
            f"{len(sampled)} vs {len(scores)}"
        )
    mean = statistics.fmean(scores)
    stdev = statistics.stdev(scores) if len(scores) > 1 else None
    per_tier: dict[str, list[int]] = {}
    for ev, sc in zip(sampled, scores):
        per_tier.setdefault(ev.tier or "?", []).append(sc)
    per_tier_mean = {
        tier: round(statistics.fmean(vals), 3) for tier, vals in per_tier.items()
    }
    decision, reason = decide_verdict(mean)
    return AuditResult(
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        total_events_in_window=total_in_window,
        sampled_event_count=len(sampled),
        seed=seed,
        mean_score=round(mean, 3),
        stdev_score=round(stdev, 3) if stdev is not None else None,
        per_tier_mean=per_tier_mean,
        decision=decision,
        decision_reason=reason,
        scores=[
            {
                "key": ev.key,
                "tier": ev.tier,
                "ticket_key": ev.ticket_key,
                "timestamp": ev.timestamp,
                "score": sc,
            }
            for ev, sc in zip(sampled, scores)
        ],
    )


# ── CLI orchestration ────────────────────────────────────────────────


def run_audit(
    *,
    progress_paths: list[Path],
    now: datetime,
    window_days: int,
    sample_size: int,
    seed: int,
    scores_path: Path | None,
    interactive: bool,
) -> AuditResult:
    """End-to-end audit. Raises the OP-867 error-catalog exceptions."""
    verify_c1_merged()
    window_end = now
    window_start = now - timedelta(days=window_days)
    events = load_audit_events(
        progress_paths,
        window_start=window_start,
        window_end=window_end,
    )
    sampled = sample_events(events, size=sample_size, seed=seed)
    if scores_path is not None:
        scores = collect_scores_from_file(sampled, scores_path)
    elif interactive:
        scores = collect_scores_interactive(sampled)
    else:
        raise ValueError(
            "no scoring source: pass --scores or run interactively"
        )
    return aggregate(
        sampled, scores,
        window_start=window_start,
        window_end=window_end,
        total_in_window=len(events),
        seed=seed,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--progress", type=Path, action="append", required=True,
        help="Path to a JSONL progress.txt; may be passed multiple times.",
    )
    parser.add_argument(
        "--now", type=str, default=None,
        help="ISO-8601 'audit moment'; defaults to UTC now. Use to "
             "reproduce a past audit window in CI.",
    )
    parser.add_argument(
        "--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
        help="Look-back window in days (AC #1; default 7).",
    )
    parser.add_argument(
        "--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
        help="Number of events to sample (AC #2; default 30).",
    )
    parser.add_argument(
        "--seed", type=int, default=0,
        help="RNG seed for reproducible sampling (default 0).",
    )
    parser.add_argument(
        "--scores", type=Path, default=None,
        help="JSON file with operator scores (key -> int 1–5). "
             "When omitted, the script prompts interactively.",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Force interactive scoring (overrides default TTY detect).",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Write the AuditResult JSON to this path.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.now:
        now = _parse_iso(args.now) or datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc)
    interactive = args.interactive or (
        args.scores is None and sys.stdin.isatty()
    )
    try:
        result = run_audit(
            progress_paths=args.progress,
            now=now,
            window_days=args.window_days,
            sample_size=args.sample_size,
            seed=args.seed,
            scores_path=args.scores,
            interactive=interactive,
        )
    except C1NotYetMerged as exc:
        log.error("C1NotYetMerged: %s", exc)
        return 2
    except InsufficientSamplesForAudit as exc:
        log.error("InsufficientSamplesForAudit: %s", exc)
        return 3
    payload = json.dumps(asdict(result), indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
        log.info("wrote %s", args.output)
    print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
