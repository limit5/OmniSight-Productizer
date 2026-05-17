"""Input-validation tests for backend.agents.boss_raid_time_gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.agents.boss_raid_time_gate import (
    BOSS_RAID_COMPLETION_WINDOW,
    boss_raid_completed_in_window,
    boss_raid_time_gate,
)

STARTED_AT = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
CHECKED_AT = STARTED_AT + timedelta(days=1)


@pytest.mark.parametrize(
    "kwargs,exc_type,match",
    [
        ({"raid_id": None}, TypeError, "raid_id must be a string"),
        ({"raid_id": ""}, ValueError, "raid_id is required"),
        ({"raid_id": "   "}, ValueError, "raid_id is required"),
        ({"raid_id": []}, TypeError, "raid_id must be a string"),
        ({"started_at": None}, TypeError, "started_at must be a datetime"),
        (
            {"started_at": datetime(2026, 5, 1, 12, 0)},
            ValueError,
            "started_at must be timezone-aware",
        ),
        ({"started_at": []}, TypeError, "started_at must be a datetime"),
        ({"checked_at": None}, TypeError, "checked_at must be a datetime"),
        (
            {"checked_at": datetime(2026, 5, 2, 12, 0)},
            ValueError,
            "checked_at must be timezone-aware",
        ),
        ({"checked_at": []}, TypeError, "checked_at must be a datetime"),
        (
            {"checked_at": STARTED_AT - timedelta(seconds=1)},
            ValueError,
            "checked_at must be >= started_at",
        ),
        ({"completed": None}, TypeError, "completed must be a bool"),
        ({"completed": 1}, TypeError, "completed must be a bool"),
        ({"completed": []}, TypeError, "completed must be a bool"),
    ],
)
def test_boss_raid_time_gate_rejects_invalid_inputs(
    kwargs: dict[str, object],
    exc_type: type[Exception],
    match: str,
) -> None:
    params = {
        "raid_id": "raid-1",
        "started_at": STARTED_AT,
        "checked_at": CHECKED_AT,
        "completed": False,
    }
    params.update(kwargs)

    with pytest.raises(exc_type, match=match):
        boss_raid_time_gate(**params)


def test_boss_raid_time_gate_accepts_large_valid_values() -> None:
    started_at = datetime(9000, 1, 1, tzinfo=timezone.utc)
    checked_at = started_at + BOSS_RAID_COMPLETION_WINDOW
    raid_id = f" {'r' * 10_000} "

    gate = boss_raid_time_gate(
        raid_id=raid_id,
        started_at=started_at,
        checked_at=checked_at,
        completed=True,
    )

    assert gate.raid_id == "r" * 10_000
    assert gate.started_at == started_at
    assert gate.checked_at == checked_at
    assert gate.is_complete is True


@pytest.mark.parametrize(
    "kwargs,exc_type,match",
    [
        ({"started_at": None}, TypeError, "started_at must be a datetime"),
        (
            {"started_at": datetime(2026, 5, 1, 12, 0)},
            ValueError,
            "started_at must be timezone-aware",
        ),
        ({"started_at": []}, TypeError, "started_at must be a datetime"),
        ({"completed_at": None}, TypeError, "checked_at must be a datetime"),
        (
            {"completed_at": datetime(2026, 5, 2, 12, 0)},
            ValueError,
            "checked_at must be timezone-aware",
        ),
        ({"completed_at": []}, TypeError, "checked_at must be a datetime"),
        (
            {"completed_at": STARTED_AT - timedelta(seconds=1)},
            ValueError,
            "checked_at must be >= started_at",
        ),
    ],
)
def test_boss_raid_completed_in_window_rejects_invalid_inputs(
    kwargs: dict[str, object],
    exc_type: type[Exception],
    match: str,
) -> None:
    params = {
        "started_at": STARTED_AT,
        "completed_at": CHECKED_AT,
    }
    params.update(kwargs)

    with pytest.raises(exc_type, match=match):
        boss_raid_completed_in_window(**params)


def test_boss_raid_completed_in_window_accepts_large_valid_values() -> None:
    started_at = datetime(9000, 1, 1, tzinfo=timezone.utc)
    completed_at = started_at + BOSS_RAID_COMPLETION_WINDOW

    assert boss_raid_completed_in_window(
        started_at=started_at,
        completed_at=completed_at,
    ) is True
