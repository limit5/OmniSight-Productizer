"""RPG.W21.2 -- boss-raid 14-day continuous engagement gate.

ADR 0008 defines W21 boss raids as time-gated large refactor tasks. This
module keeps the gate backend-local and persistence-free: callers pass the
raid start time plus the check/completion time, and receive deterministic
window state that can be enforced by a later storage or routing boundary.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants and frozen dataclasses only. It
performs no database access, registry mutation, network I/O, or clock reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

BossRaidGateStatus = Literal["open", "complete", "expired"]

BOSS_RAID_COMPLETION_WINDOW_DAYS = 14
BOSS_RAID_COMPLETION_WINDOW = timedelta(days=BOSS_RAID_COMPLETION_WINDOW_DAYS)


@dataclass(frozen=True)
class BossRaidTimeGate:
    """Computed W21.2 time-gate state for one boss raid."""

    raid_id: str
    started_at: datetime
    checked_at: datetime
    deadline_at: datetime
    elapsed: timedelta
    remaining: timedelta
    status: BossRaidGateStatus
    window_days: int = BOSS_RAID_COMPLETION_WINDOW_DAYS

    @property
    def is_open(self) -> bool:
        """Return whether the raid may continue accepting work."""

        return self.status == "open"

    @property
    def is_complete(self) -> bool:
        """Return whether the raid completed inside the allowed window."""

        return self.status == "complete"

    @property
    def is_expired(self) -> bool:
        """Return whether the raid exceeded the allowed completion window."""

        return self.status == "expired"


def boss_raid_time_gate(
    *,
    raid_id: str,
    started_at: datetime,
    checked_at: datetime,
    completed: bool = False,
) -> BossRaidTimeGate:
    """Return W21.2 gate state for a boss raid at ``checked_at``.

    ``completed=True`` means ``checked_at`` is the completion timestamp. A raid
    may complete exactly at the 14-day deadline; any later timestamp expires it.
    """

    clean_raid_id = _required("raid_id", raid_id)
    start = _clean_datetime("started_at", started_at)
    check = _clean_datetime("checked_at", checked_at)
    if check < start:
        raise ValueError("checked_at must be >= started_at")

    deadline = start + BOSS_RAID_COMPLETION_WINDOW
    elapsed = check - start
    remaining = max(deadline - check, timedelta(0))
    status = _gate_status(check, deadline, completed=completed)
    return BossRaidTimeGate(
        raid_id=clean_raid_id,
        started_at=start,
        checked_at=check,
        deadline_at=deadline,
        elapsed=elapsed,
        remaining=remaining,
        status=status,
    )


def boss_raid_completed_in_window(
    *,
    started_at: datetime,
    completed_at: datetime,
) -> bool:
    """Return whether a boss raid completed within the 14-day W21.2 gate."""

    return boss_raid_time_gate(
        raid_id="completion-check",
        started_at=started_at,
        checked_at=completed_at,
        completed=True,
    ).is_complete


def _gate_status(
    checked_at: datetime,
    deadline_at: datetime,
    *,
    completed: bool,
) -> BossRaidGateStatus:
    if checked_at > deadline_at:
        return "expired"
    if completed:
        return "complete"
    return "open"


def _clean_datetime(field: str, value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _required(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} is required")
    return stripped


__all__ = [
    "BOSS_RAID_COMPLETION_WINDOW",
    "BOSS_RAID_COMPLETION_WINDOW_DAYS",
    "BossRaidGateStatus",
    "BossRaidTimeGate",
    "boss_raid_completed_in_window",
    "boss_raid_time_gate",
]
