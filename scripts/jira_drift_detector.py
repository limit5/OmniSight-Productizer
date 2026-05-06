"""Tier drift detector — auto-create META retrospective tickets.

Per ``docs/sop/jira-ticket-conventions.md`` §14. Daily cron triggered.
Computes ``actual_hours / tier_target_upper_bound`` for every ticket
that transitioned to ``Published`` since last run, and auto-creates a
META retrospective ticket when drift exceeds threshold.

Drift thresholds (§14):
- 0.5 ≤ ratio ≤ 1.5  → normal, no action
- ratio > 1.5         → over-run, create retrospective
- ratio < 0.5         → over-tier, create retrospective
- ratio > 3.0         → severe over-run, also flag parent epic

actual_hours computation:
  Sum of (Under Review entry − In Progress entry) deltas across the
  ticket's transition history. Multiple In Progress ↔ Under Review
  cycles count cumulatively. Queue time (TODO sitting around) excluded.

Tier upper bounds:
  tier:S = 3, tier:M = 12, tier:L = 72, tier:X = ∞ (skip — already escalated)

Retrospective ticket schema (§14):
  Component=META, label=meta:retrospective + drift:over-run|over-tier,
  Linked: caused-by <source>, Description with required structured fields
  (situation/divergence/root_cause/contributing/concrete_fix/verification).

Cron suggested: 03:00 UTC daily. Stateful via metrics/drift_detector_state.json
(last-run timestamp) so transitions during an outage are picked up next run.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = REPO_ROOT / "metrics" / "drift_detector_state.json"

TIER_UPPER_BOUNDS_HOURS: dict[str, float] = {
    "tier:S": 3.0,
    "tier:M": 12.0,
    "tier:L": 72.0,
    "tier:X": float("inf"),  # tier:X is already escalated — no drift signal
}

OVER_RUN_THRESHOLD = 1.5
OVER_TIER_THRESHOLD = 0.5
SEVERE_OVER_RUN_THRESHOLD = 3.0


@dataclass(frozen=True)
class DriftSignal:
    """One ticket that drifted; basis for retrospective creation."""

    source_key: str
    source_summary: str
    source_assignee: str  # who lived through it; gets initial retro assignment
    tier_label: str
    tier_target_hours: float
    actual_hours: float
    ratio: float
    drift_kind: str  # "over-run" | "over-tier" | "severe-over-run"


# ── JIRA interactions ──────────────────────────────────────────────


def fetch_recently_published(since: datetime) -> list[dict]:
    """Pull tickets that transitioned to Published since `since`.

    JQL: ``status = "Published" AND status changed to "Published" AFTER <since>``.
    Returns raw JIRA issue payloads (not parsed).
    """
    raise NotImplementedError("skeleton — JQL search w/ since filter")


def compute_in_progress_hours(issue: dict) -> float:
    """Sum (UnderReview - InProgress) deltas across transition history.

    Multiple cycles (changes-requested round trips) count cumulatively.
    Queue time excluded (TODO → InProgress entry is start of work).
    """
    raise NotImplementedError(
        "skeleton — parse issue.changelog.histories, sum deltas"
    )


def create_retrospective_ticket(signal: DriftSignal) -> str:
    """Create META retrospective ticket per §14. Returns new ticket key.

    Workflow rules to apply (caller verifies these are honoured):
    - Assignee = signal.source_assignee
    - Linked: caused-by signal.source_key
    - Labels: meta:retrospective, drift:<kind>, tier:S, area:docs
    - Cannot transition Approved without non-source +1 (workflow validator)
    - Blocks pickup of similar class+area until Published (runner check)
    """
    raise NotImplementedError(
        "skeleton — POST $BASE/issue with Component=META + linked + labels"
    )


# ── Drift logic ────────────────────────────────────────────────────


def evaluate_drift(issue: dict) -> DriftSignal | None:
    """Compute drift for one issue. Returns None if within normal range or tier:X."""
    tier_label = _extract_tier_label(issue)
    if tier_label not in TIER_UPPER_BOUNDS_HOURS:
        return None
    target = TIER_UPPER_BOUNDS_HOURS[tier_label]
    if target == float("inf"):
        return None  # tier:X exempt

    actual = compute_in_progress_hours(issue)
    ratio = actual / target

    if OVER_TIER_THRESHOLD <= ratio <= OVER_RUN_THRESHOLD:
        return None  # within band

    drift_kind = (
        "severe-over-run"
        if ratio > SEVERE_OVER_RUN_THRESHOLD
        else "over-run"
        if ratio > OVER_RUN_THRESHOLD
        else "over-tier"
    )

    return DriftSignal(
        source_key=issue["key"],
        source_summary=issue["fields"]["summary"],
        source_assignee=_extract_assignee_account_id(issue),
        tier_label=tier_label,
        tier_target_hours=target,
        actual_hours=actual,
        ratio=ratio,
        drift_kind=drift_kind,
    )


def _extract_tier_label(issue: dict) -> str | None:
    """Find the tier:X label on issue. Returns None if missing."""
    raise NotImplementedError("skeleton — scan labels for tier: prefix")


def _extract_assignee_account_id(issue: dict) -> str:
    """Get accountId of last In Progress assignee (the agent who lived the drift)."""
    raise NotImplementedError("skeleton — parse changelog for last assignee transition")


# ── State management ──────────────────────────────────────────────


def load_last_run_state() -> datetime:
    """Load last-run timestamp from STATE_PATH; default to 24h ago if missing."""
    raise NotImplementedError("skeleton — JSON load w/ fallback")


def save_run_state(timestamp: datetime) -> None:
    """Persist this run's timestamp for next invocation."""
    raise NotImplementedError("skeleton — JSON write atomic")


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="ISO8601 override for last-run state (testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be retrospectives without creating tickets",
    )
    args = parser.parse_args(argv)

    try:
        since = (
            datetime.fromisoformat(args.since)
            if args.since
            else load_last_run_state()
        )
        issues = fetch_recently_published(since)
        signals = [s for s in (evaluate_drift(i) for i in issues) if s]

        if args.dry_run:
            for s in signals:
                print(f"DRIFT: {s.source_key} ({s.drift_kind}, ratio={s.ratio:.2f})")
            return 0

        for s in signals:
            new_key = create_retrospective_ticket(s)
            print(f"Created {new_key} for drift on {s.source_key}")

        save_run_state(datetime.utcnow())
        return 0
    except NotImplementedError:
        print("ERROR: scripts/jira_drift_detector.py is a skeleton", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
