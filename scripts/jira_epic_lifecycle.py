"""Epic lifecycle automation — Wave retrospective + auto-Archive empty Epics.

Per ``docs/sop/jira-ticket-conventions.md`` §10a. Daily cron triggered.
Implements the 4-layer Epic lifecycle:

L1: Detect Epics whose all child Stories are Published; tag with
    label ``wave:complete-pending-retro``.

L2: After 24h grace, auto-create META ``meta:wave-retrospective`` ticket
    linked-by Epic. Operator (or original wave assignees) fill structured
    retro fields.

L3: When wave-retrospective ticket transitions Published, transition the
    Epic from TODO+`wave:complete-pending-retro` to Approved.

L4: When release pipeline reports `fix_version` shipped, transition all
    Approved Epics matching that fix_version to Published.

Empty-Epic handling (§10a invariant):
- An Epic with 0 children violates the Epic existence invariant
- 7-day observation window (in case operator is decomposing)
- After window: auto-Archive with comment, no Wave retrospective created

Cron suggested: 03:30 UTC daily (after drift_detector at 03:00).
Stateful via ``metrics/epic_lifecycle_state.json`` so multi-day windows
survive cron downtime.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = REPO_ROOT / "metrics" / "epic_lifecycle_state.json"

GRACE_PERIOD_HOURS = 24            # L1 → L2 grace before auto-create retro
EMPTY_EPIC_WINDOW_DAYS = 7         # §10a invariant observation window


@dataclass(frozen=True)
class EpicSnapshot:
    """Minimal Epic view for lifecycle decisions."""

    key: str
    summary: str
    status: str                    # "TODO" / "Approved" / "Published" / etc.
    labels: tuple[str, ...]
    fix_version: str | None
    child_keys: tuple[str, ...]    # all child Story keys
    child_statuses: dict[str, str] # key → status
    last_child_publish_at: datetime | None
    epic_created_at: datetime


# ── Layer 1: detect Wave-complete Epics ───────────────────────────


def detect_wave_complete(epics: list[EpicSnapshot]) -> list[EpicSnapshot]:
    """Return Epics where every child Story is Published / Archived
    AND Epic isn't already tagged wave:complete-pending-retro."""
    raise NotImplementedError(
        "skeleton — filter all-children-done + not yet tagged"
    )


def tag_wave_complete(epic_key: str) -> None:
    """Add wave:complete-pending-retro label + comment with timestamp.

    Idempotent — safe to call repeatedly.
    """
    raise NotImplementedError("skeleton — PUT label + add comment")


# ── Layer 2: auto-create wave retrospective after grace ─────────


def detect_retro_due(epics: list[EpicSnapshot], now: datetime) -> list[EpicSnapshot]:
    """Return Epics tagged wave:complete-pending-retro for ≥ GRACE_PERIOD_HOURS
    AND no caused-by META wave-retrospective ticket exists yet."""
    raise NotImplementedError(
        "skeleton — filter by label-applied-at + caused-by absence"
    )


def create_wave_retrospective(epic: EpicSnapshot) -> str:
    """Create META meta:wave-retrospective ticket per §10a template.

    Returns new ticket key. Pre-fills wave_id / items_completed /
    items_cancelled / items_spilled / fix_version / Epic-link.
    Required structured fields are blank for operator + agent fill.
    """
    raise NotImplementedError(
        "skeleton — POST $BASE/issue with Component=META, "
        "labels=meta:wave-retrospective + tier:S + area:docs, "
        "linked: caused-by epic.key"
    )


# ── Layer 3: retro published → Epic Approved ─────────────────────


def detect_retro_published(retros: list[dict]) -> list[tuple[str, str]]:
    """Return (retro_key, epic_key) pairs where retro just transitioned
    to Published and the linked Epic is still in TODO."""
    raise NotImplementedError(
        "skeleton — JQL META retros transitioned-to-Published since last run"
    )


def transition_epic_to_approved(epic_key: str, retro_key: str) -> None:
    """Transition Epic from TODO → Approved with comment citing retro_key."""
    raise NotImplementedError(
        "skeleton — POST /issue/<epic>/transitions + comment"
    )


# ── Layer 4: release shipped → Epic Published ────────────────────


def detect_release_shipped(fix_version: str) -> list[str]:
    """Return Epic keys with given fix_version in Approved status,
    needing transition to Published.

    Trigger: release pipeline calls this with shipped fix_version arg.
    """
    raise NotImplementedError(
        "skeleton — JQL fix_version + status=Approved + issuetype=Epic"
    )


def transition_epic_to_published(epic_key: str, fix_version: str) -> None:
    """Transition Epic Approved → Published with release-shipped comment."""
    raise NotImplementedError("skeleton — POST /issue/<epic>/transitions")


# ── Empty-Epic handling (§10a invariant) ─────────────────────────


def detect_empty_epics(epics: list[EpicSnapshot], now: datetime) -> list[EpicSnapshot]:
    """Return Epics with 0 children for ≥ EMPTY_EPIC_WINDOW_DAYS.

    Filtered to exclude already-Archived Epics.
    """
    raise NotImplementedError(
        "skeleton — filter children empty + age >= window + status != Archived"
    )


def archive_empty_epic(epic: EpicSnapshot) -> None:
    """Auto-Archive empty Epic with comment explaining §10a invariant.

    Does NOT create a Wave retrospective (no work to retro about).
    """
    raise NotImplementedError(
        "skeleton — comment + transition to Archived"
    )


# ── State / orchestration ────────────────────────────────────────


def fetch_all_epics() -> list[EpicSnapshot]:
    """JQL: all Epics in OP project, populate child statuses + dates."""
    raise NotImplementedError("skeleton — paginated JQL fetch")


def load_state() -> dict:
    """Load last-run state (per-Epic label-applied timestamps)."""
    raise NotImplementedError("skeleton — JSON load with empty default")


def save_state(state: dict) -> None:
    """Persist updated state atomically."""
    raise NotImplementedError("skeleton — JSON write")


# ── CLI ──────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--release-shipped",
        help="ISO version (e.g. v0.4.0) — triggers L4 transition for that fix_version",
    )
    args = parser.parse_args(argv)

    try:
        now = datetime.utcnow()
        epics = fetch_all_epics()

        # L1
        l1_targets = detect_wave_complete(epics)
        for e in l1_targets:
            print(f"L1: {e.key} all children Published → tagging wave:complete-pending-retro")
            if not args.dry_run:
                tag_wave_complete(e.key)

        # L2
        l2_targets = detect_retro_due(epics, now)
        for e in l2_targets:
            if args.dry_run:
                print(f"L2: {e.key} retro due → would create META wave-retrospective")
            else:
                retro_key = create_wave_retrospective(e)
                print(f"L2: {e.key} retro due → created {retro_key}")

        # L3 (poll-driven; needs separate retro-fetch)
        # Skipped in skeleton — implement once L1/L2 land.

        # L4 (operator-triggered via --release-shipped)
        if args.release_shipped:
            l4_targets = detect_release_shipped(args.release_shipped)
            for epic_key in l4_targets:
                if args.dry_run:
                    print(f"L4: {epic_key} fix_version={args.release_shipped} → would Publish")
                else:
                    transition_epic_to_published(epic_key, args.release_shipped)
                    print(f"L4: {epic_key} → Published")

        # Empty-Epic invariant
        empty = detect_empty_epics(epics, now)
        for e in empty:
            if args.dry_run:
                print(f"EMPTY: {e.key} 0 children for ≥ {EMPTY_EPIC_WINDOW_DAYS}d → would Archive")
            else:
                archive_empty_epic(e)
                print(f"EMPTY: {e.key} → Archived")

        return 0
    except NotImplementedError:
        print("ERROR: scripts/jira_epic_lifecycle.py is a skeleton", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
