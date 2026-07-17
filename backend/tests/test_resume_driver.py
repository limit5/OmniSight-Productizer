"""Offline tests for the U6-0 GAP-5c-loop sub-leaf A supervised driver loop (no PostgreSQL)."""
from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Awaitable
from collections.abc import Callable

import pytest

from backend.agents.resume_driver import DriveStats
from backend.agents.resume_driver import drive_resume_jobs

_RAISE = object()


def _driver(sequence: list) -> Callable[[], Awaitable[str]]:
    """A one-shot driver that yields the scripted statuses; a _RAISE sentinel raises RuntimeError."""
    iterator = iter(sequence)

    async def drive_once() -> str:
        value = next(iterator)
        if value is _RAISE:
            raise RuntimeError("scripted driver fault")
        return value

    return drive_once


def _recording_sleep() -> "tuple[list[float], Callable[[float], Awaitable[None]]]":
    """A fake backoff sleep that records its argument and never actually waits."""
    calls: list[float] = []

    async def sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, sleep


async def _drive(driver, **kwargs) -> DriveStats:
    kwargs.setdefault("poll_interval_s", 9.0)
    kwargs.setdefault("error_backoff_s", 3.0)
    kwargs.setdefault("max_consecutive_errors", 3)
    kwargs.setdefault("drain_when_idle", True)
    kwargs.setdefault("should_continue", lambda: True)
    return await drive_resume_jobs(driver, **kwargs)


@pytest.mark.asyncio
async def test_stop_predicate_immediate_returns_zero_ticks() -> None:
    calls, sleep = _recording_sleep()
    stats = await _drive(_driver(["done"]), should_continue=lambda: False, sleep=sleep)
    assert stats.stopped_reason == "stop_predicate"
    assert (stats.ticks, stats.driven, stats.idle, stats.errors) == (0, 0, 0, 0)
    assert calls == []


@pytest.mark.asyncio
async def test_idle_drain_returns_on_first_idle_without_backoff() -> None:
    calls, sleep = _recording_sleep()
    stats = await _drive(_driver(["idle"]), drain_when_idle=True, sleep=sleep)
    assert stats.stopped_reason == "idle_drained"
    assert (stats.ticks, stats.idle) == (1, 1)
    assert calls == []


@pytest.mark.asyncio
async def test_idle_daemon_sleeps_poll_interval_then_stops() -> None:
    calls, sleep = _recording_sleep()
    ticks = {"n": 0}

    def should_continue() -> bool:
        ticks["n"] += 1
        return ticks["n"] <= 2

    stats = await _drive(
        _driver(["idle", "idle", "idle"]),
        drain_when_idle=False,
        should_continue=should_continue,
        poll_interval_s=7.0,
        sleep=sleep,
    )
    assert stats.stopped_reason == "stop_predicate"
    assert stats.idle == 2
    # Two idle drives, but only ONE inter-idle backoff is applied: the second idle's backoff is deferred to the loop
    # top and then skipped because the stop predicate fires first (the finding-2 "no wasted final sleep" guarantee).
    assert calls == [7.0]


@pytest.mark.asyncio
async def test_drives_outcomes_counts_and_invokes_on_outcome_in_order() -> None:
    seen: list[str] = []
    stats = await _drive(
        _driver(["done", "failed", "manual", "queued", "lease_lost", "idle"]),
        drain_when_idle=True,
        on_outcome=seen.append,
    )
    assert stats.stopped_reason == "idle_drained"
    assert stats.driven == 5
    assert dict(stats.outcomes) == {"done": 1, "failed": 1, "manual": 1, "queued": 1, "lease_lost": 1}
    assert seen == ["done", "failed", "manual", "queued", "lease_lost"]
    assert stats.ticks == stats.driven + stats.idle + stats.errors


@pytest.mark.asyncio
async def test_error_then_success_then_error_does_not_trip_breaker() -> None:
    # threshold 2: without the reset the two errors would trip; the success in between must reset consecutive to 0.
    calls, sleep = _recording_sleep()
    stats = await _drive(
        _driver([_RAISE, "done", _RAISE, "idle"]),
        max_consecutive_errors=2,
        drain_when_idle=True,
        error_backoff_s=3.0,
        sleep=sleep,
    )
    assert stats.stopped_reason == "idle_drained"
    assert (stats.errors, stats.driven) == (2, 1)
    assert calls == [3.0, 3.0]


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_nth_error_without_final_sleep() -> None:
    calls, sleep = _recording_sleep()
    stats = await _drive(
        _driver([_RAISE, _RAISE, _RAISE, "done"]),
        max_consecutive_errors=3,
        error_backoff_s=2.0,
        sleep=sleep,
    )
    assert stats.stopped_reason == "circuit_open"
    assert stats.errors == 3
    assert calls == [2.0, 2.0]  # N-1 backoffs; no sleep on the trip


@pytest.mark.asyncio
async def test_max_ticks_caps_drive_starts() -> None:
    stats = await _drive(_driver(["done"] * 10), drain_when_idle=False, max_ticks=3)
    assert stats.stopped_reason == "max_ticks"
    assert (stats.ticks, stats.driven) == (3, 3)


@pytest.mark.asyncio
async def test_no_backoff_sleep_before_max_ticks_on_idle() -> None:
    calls, sleep = _recording_sleep()
    stats = await _drive(
        _driver(["idle", "idle"]),
        drain_when_idle=False,
        max_ticks=1,
        poll_interval_s=9.0,
        sleep=sleep,
    )
    assert stats.stopped_reason == "max_ticks"
    assert stats.ticks == 1
    assert calls == []  # the idle backoff is skipped because the run stops on the next iteration


@pytest.mark.asyncio
async def test_no_backoff_sleep_before_max_ticks_on_error() -> None:
    calls, sleep = _recording_sleep()
    stats = await _drive(
        _driver([_RAISE]),
        drain_when_idle=False,
        max_ticks=1,
        max_consecutive_errors=5,
        error_backoff_s=4.0,
        sleep=sleep,
    )
    assert stats.stopped_reason == "max_ticks"
    assert (stats.ticks, stats.errors) == (1, 1)
    assert calls == []


@pytest.mark.asyncio
async def test_cancelled_error_propagates_and_is_not_counted() -> None:
    async def drive_once() -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _drive(drive_once)


@pytest.mark.asyncio
async def test_should_continue_fault_propagates() -> None:
    def should_continue() -> bool:
        raise RuntimeError("predicate is a caller contract")

    with pytest.raises(RuntimeError, match="caller contract"):
        await _drive(_driver(["done"]), should_continue=should_continue)


@pytest.mark.asyncio
async def test_injected_sleep_fault_propagates() -> None:
    async def sleep(_seconds: float) -> None:
        raise RuntimeError("sleep is a caller contract")

    with pytest.raises(RuntimeError, match="caller contract"):
        await _drive(_driver(["idle", "idle"]), drain_when_idle=False, sleep=sleep)


@pytest.mark.asyncio
async def test_on_outcome_fault_is_isolated() -> None:
    def on_outcome(_status: str) -> None:
        raise RuntimeError("hook boom")

    stats = await _drive(_driver(["done", "idle"]), drain_when_idle=True, on_outcome=on_outcome)
    assert stats.stopped_reason == "idle_drained"
    assert stats.driven == 1


@pytest.mark.asyncio
async def test_ticks_invariant_holds_across_mixed_run() -> None:
    calls, sleep = _recording_sleep()
    stats = await _drive(
        _driver([_RAISE, "done", "idle"]),
        max_consecutive_errors=3,
        drain_when_idle=True,
        sleep=sleep,
    )
    assert stats.ticks == stats.driven + stats.idle + stats.errors
    assert sum(stats.outcomes.values()) == stats.driven


@pytest.mark.asyncio
async def test_zero_max_consecutive_errors_is_rejected_before_any_drive() -> None:
    driven = {"n": 0}

    async def drive_once() -> str:
        driven["n"] += 1
        return "done"

    with pytest.raises(ValueError, match="max_consecutive_errors"):
        await _drive(drive_once, max_consecutive_errors=0)
    assert driven["n"] == 0


@pytest.mark.asyncio
async def test_outcomes_is_a_read_only_snapshot() -> None:
    stats = await _drive(_driver(["done", "idle"]), drain_when_idle=True)
    with pytest.raises(TypeError):
        stats.outcomes["injected"] = 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_busy_daemon_yields_and_is_bounded_by_predicate() -> None:
    # drive_once never suspends and never idles; the loop must still be cancellable/stoppable (guaranteed yield) and
    # is bounded here by the stop predicate rather than idle/max_ticks.
    ticks = {"n": 0}

    def should_continue() -> bool:
        ticks["n"] += 1
        return ticks["n"] <= 3

    stats = await _drive(_driver(["done"] * 5), drain_when_idle=False, should_continue=should_continue)
    assert stats.stopped_reason == "stop_predicate"
    assert stats.driven == 3


def test_module_is_dormant_with_no_production_caller() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "versions" in parts:
            continue
        if path.name == "resume_driver.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "drive_resume_jobs" in text or "resume_driver" in text:
            offenders.append(str(path))
    assert offenders == [], f"resume_driver must have no production caller yet: {offenders}"
