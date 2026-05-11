"""OP-911 F13 — Sprint F canary orchestrator contracts.

Four cases per ticket §Test plan:

    1. Gate-green proceeds 5 → 25 → 100.
    2. Gate-red rolls back (any signal: SLO breach, runner-success-rate
       drop, or [runner-no-commits-from-cli] revert-loop).
    3. Percent setting persists in D12 (the verify-after-write step
       catches a RolloutPercentMisapplied D12 service bug).
    4. SSE event emission fires for every transition.

Uses a direct spec-loader import like ``tests/test_canary_pipeline.py``
so the script-under-test stays in ``scripts/`` without requiring a
``scripts/__init__.py`` (the ``scripts/`` tree intentionally has no
package marker -- it's a sibling pile of operator tools).
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "sprint_f_canary_orchestrator.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sprint_f_canary_orchestrator_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sprint_f_canary_orchestrator_under_test"] = module
    spec.loader.exec_module(module)
    return module


orch_mod = _load_module()


# --- Test doubles ---------------------------------------------------


class _InMemoryFlagClient:
    """Records every write so tests can assert the persisted % sequence."""

    def __init__(
        self,
        *,
        initial_state: str = "disabled",
        initial_pct: int = 0,
        updated_at: datetime | None = None,
        bug_misapply_to: int | None = None,
        disable_raises: Exception | None = None,
    ) -> None:
        self._state = orch_mod.FlagState(
            rollout_pct=initial_pct,
            state=initial_state,
            updated_at=updated_at,
        )
        self.writes: list[tuple[str, int]] = []
        self._bug_misapply_to = bug_misapply_to
        self._disable_raises = disable_raises

    def get(self, name: str) -> Any:
        return self._state

    def set_rollout_pct(self, name: str, pct: int) -> Any:
        applied_pct = self._bug_misapply_to if self._bug_misapply_to is not None else pct
        self._state = orch_mod.FlagState(
            rollout_pct=applied_pct,
            state="enabled",
            updated_at=self._state.updated_at,
        )
        self.writes.append(("set", applied_pct))
        return self._state

    def disable(self, name: str) -> Any:
        if self._disable_raises is not None:
            raise self._disable_raises
        self._state = orch_mod.FlagState(
            rollout_pct=0, state="disabled",
            updated_at=self._state.updated_at,
        )
        self.writes.append(("disable", 0))
        return self._state


class _StubMetrics:
    """Returns a programmable StageMetrics, optionally a sequence."""

    def __init__(self, *snapshots: Any) -> None:
        default = orch_mod.StageMetrics(
            slo_breach_reason=None,
            runner_success_rate=0.95,
            revert_loop_count=0,
        )
        self._snapshots = list(snapshots) or [default]

    def snapshot(self, since: datetime) -> Any:
        snap = self._snapshots[0]
        if len(self._snapshots) > 1:
            self._snapshots.pop(0)
        return snap


class _RecordingPublish:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, dict(data)))


def _make_orchestrator(
    *,
    flag_client: _InMemoryFlagClient,
    metrics: _StubMetrics,
    publish: _RecordingPublish,
    baseline: float = 0.95,
    clock_now: datetime | None = None,
) -> Any:
    now_holder = {"now": clock_now or datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)}
    def clock() -> datetime:
        return now_holder["now"]
    orch = orch_mod.SprintFCanaryOrchestrator(
        rollout_id="op-911-test",
        flag_client=flag_client,
        metrics=metrics,
        baseline_runner_success_rate=baseline,
        clock=clock,
        publish=publish,
    )
    # Expose the clock holder so tests can advance time.
    orch._test_now_holder = now_holder  # type: ignore[attr-defined]
    return orch


def _advance_clock(orch: Any, hours: float) -> None:
    holder = orch._test_now_holder
    holder["now"] = holder["now"] + timedelta(hours=hours)


# --- Case 1: gate-green proceeds 5 → 25 → 100 -----------------------


def test_gate_green_proceeds_through_all_stages() -> None:
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(updated_at=start_time)
    metrics = _StubMetrics()
    publish = _RecordingPublish()
    orch = _make_orchestrator(
        flag_client=flag_client, metrics=metrics, publish=publish,
        clock_now=start_time,
    )

    stage = orch.start()
    assert stage.name == "5"
    assert flag_client.writes == [("set", 5)]

    # Bump the flag's updated_at to track when each stage actually started
    # (the real D12 PATCH does this via NOW()).
    _advance_clock(orch, 24)
    flag_client._state = orch_mod.FlagState(
        rollout_pct=5, state="enabled",
        updated_at=orch._test_now_holder["now"] - timedelta(hours=24),
    )
    stage = orch.advance()
    assert stage.name == "25"
    assert flag_client.writes[-1] == ("set", 25)

    _advance_clock(orch, 24)
    flag_client._state = orch_mod.FlagState(
        rollout_pct=25, state="enabled",
        updated_at=orch._test_now_holder["now"] - timedelta(hours=24),
    )
    stage = orch.advance()
    assert stage.name == "100"
    assert flag_client.writes[-1] == ("set", 100)

    # Terminal advance at 100% emits `sprint_f.canary.completed` and is
    # idempotent -- no further PATCH.
    pre_count = len(flag_client.writes)
    stage = orch.advance()
    assert stage.name == "100"
    assert len(flag_client.writes) == pre_count


def test_observe_window_blocks_premature_advance() -> None:
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(
        initial_state="enabled", initial_pct=5, updated_at=start_time,
    )
    metrics = _StubMetrics()
    publish = _RecordingPublish()
    orch = _make_orchestrator(
        flag_client=flag_client, metrics=metrics, publish=publish,
        clock_now=start_time,
    )
    _advance_clock(orch, 1)  # only 1h of 24h
    with pytest.raises(orch_mod.CanaryGateFail) as exc:
        orch.advance()
    assert "observe window open" in str(exc.value)


# --- Case 2: gate-red rolls back ------------------------------------


def test_slo_breach_triggers_rollback() -> None:
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(
        initial_state="enabled", initial_pct=5,
        updated_at=start_time - timedelta(hours=24),
    )
    metrics = _StubMetrics(orch_mod.StageMetrics(
        slo_breach_reason="error_rate=0.012>=0.005",
        runner_success_rate=0.95,
        revert_loop_count=0,
    ))
    publish = _RecordingPublish()
    orch = _make_orchestrator(
        flag_client=flag_client, metrics=metrics, publish=publish,
        clock_now=start_time,
    )

    with pytest.raises(orch_mod.CanaryGateFail) as exc:
        orch.advance()
    assert "slo_breach" in str(exc.value)
    # Rollback issued: the last write must be disable.
    assert flag_client.writes[-1] == ("disable", 0)
    event_names = [name for name, _ in publish.events]
    assert "sprint_f.canary.gate.failed" in event_names
    assert "sprint_f.canary.rolled_back" in event_names


def test_runner_success_rate_drop_triggers_rollback() -> None:
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(
        initial_state="enabled", initial_pct=5,
        updated_at=start_time - timedelta(hours=24),
    )
    metrics = _StubMetrics(orch_mod.StageMetrics(
        slo_breach_reason=None,
        runner_success_rate=0.89,  # baseline 0.95 - 6pp -> below floor 0.90
        revert_loop_count=0,
    ))
    publish = _RecordingPublish()
    orch = _make_orchestrator(
        flag_client=flag_client, metrics=metrics, publish=publish,
        baseline=0.95, clock_now=start_time,
    )

    with pytest.raises(orch_mod.CanaryGateFail) as exc:
        orch.advance()
    assert "runner_success_rate" in str(exc.value)
    assert flag_client.writes[-1] == ("disable", 0)


def test_revert_loop_pattern_triggers_rollback() -> None:
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(
        initial_state="enabled", initial_pct=5,
        updated_at=start_time - timedelta(hours=24),
    )
    metrics = _StubMetrics(orch_mod.StageMetrics(
        slo_breach_reason=None,
        runner_success_rate=0.95,
        revert_loop_count=2,
    ))
    publish = _RecordingPublish()
    orch = _make_orchestrator(
        flag_client=flag_client, metrics=metrics, publish=publish,
        clock_now=start_time,
    )

    with pytest.raises(orch_mod.CanaryGateFail) as exc:
        orch.advance()
    assert "runner_no_commits_from_cli" in str(exc.value)
    assert flag_client.writes[-1] == ("disable", 0)


def test_rollback_failed_pages_operator() -> None:
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(
        initial_state="enabled", initial_pct=5,
        updated_at=start_time - timedelta(hours=24),
        disable_raises=orch_mod.CanaryError("D12 host unreachable"),
    )
    metrics = _StubMetrics(orch_mod.StageMetrics(
        slo_breach_reason="hard_failure",
        runner_success_rate=0.95,
        revert_loop_count=0,
    ))
    publish = _RecordingPublish()
    orch = _make_orchestrator(
        flag_client=flag_client, metrics=metrics, publish=publish,
        clock_now=start_time,
    )
    with pytest.raises(orch_mod.RollbackFailed):
        orch.advance()


# --- Case 3: percent setting persists -------------------------------


def test_percent_setting_persists_and_status_re_derives() -> None:
    """The orchestrator's only state is the D12 row; ``status()`` must
    re-derive the current stage from ``flag_client.get(...)`` so a
    Python process restart between stages does not lose state."""
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(updated_at=start_time)
    orch = _make_orchestrator(
        flag_client=flag_client,
        metrics=_StubMetrics(),
        publish=_RecordingPublish(),
        clock_now=start_time,
    )

    orch.start()
    # Re-derive: a fresh orchestrator wrapping the same client must see
    # us at stage 5 without any in-memory cache.
    fresh = _make_orchestrator(
        flag_client=flag_client,
        metrics=_StubMetrics(),
        publish=_RecordingPublish(),
        clock_now=start_time,
    )
    stage, state = fresh.status()
    assert stage is not None
    assert stage.name == "5"
    assert state.rollout_pct == 5
    assert state.state == "enabled"


def test_misapplied_percent_triggers_rollback_and_raises() -> None:
    """D12 returning rollout_pct=17 after PATCH(rollout_pct=5) is a D12
    bug -- the orchestrator must rollback and surface
    RolloutPercentMisapplied so the operator files a follow-up."""
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(
        updated_at=start_time, bug_misapply_to=17,
    )
    orch = _make_orchestrator(
        flag_client=flag_client,
        metrics=_StubMetrics(),
        publish=_RecordingPublish(),
        clock_now=start_time,
    )

    with pytest.raises(orch_mod.RolloutPercentMisapplied):
        orch.start()
    # Rollback must have run after detection.
    assert ("disable", 0) in flag_client.writes


# --- Case 4: SSE event emission -------------------------------------


def test_sse_events_fire_for_every_transition() -> None:
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(updated_at=start_time)
    publish = _RecordingPublish()
    orch = _make_orchestrator(
        flag_client=flag_client,
        metrics=_StubMetrics(),
        publish=publish,
        clock_now=start_time,
    )

    orch.start()
    _advance_clock(orch, 24)
    flag_client._state = orch_mod.FlagState(
        rollout_pct=5, state="enabled",
        updated_at=orch._test_now_holder["now"] - timedelta(hours=24),
    )
    orch.advance()
    _advance_clock(orch, 24)
    flag_client._state = orch_mod.FlagState(
        rollout_pct=25, state="enabled",
        updated_at=orch._test_now_holder["now"] - timedelta(hours=24),
    )
    orch.advance()
    orch.advance()  # terminal -> completed

    names = [name for name, _ in publish.events]
    assert names == [
        "sprint_f.canary.stage.started",
        "sprint_f.canary.stage.transitioned",
        "sprint_f.canary.stage.transitioned",
        "sprint_f.canary.completed",
    ]
    # Every payload carries the rollout-id + flag identity so SSE
    # consumers can correlate.
    for _, payload in publish.events:
        assert payload["rollout_id"] == "op-911-test"
        assert payload["flag_name"] == orch_mod.FLAG_NAME
        assert payload["stage"] in {"5", "25", "100"}
        assert payload["rollout_pct"] in {5, 25, 100}


def test_sse_event_payloads_for_gate_failure_carry_metrics() -> None:
    start_time = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    flag_client = _InMemoryFlagClient(
        initial_state="enabled", initial_pct=5,
        updated_at=start_time - timedelta(hours=24),
    )
    metrics = _StubMetrics(orch_mod.StageMetrics(
        slo_breach_reason="error_rate_over",
        runner_success_rate=0.88,
        revert_loop_count=3,
    ))
    publish = _RecordingPublish()
    orch = _make_orchestrator(
        flag_client=flag_client, metrics=metrics, publish=publish,
        clock_now=start_time,
    )

    with pytest.raises(orch_mod.CanaryGateFail):
        orch.advance()

    gate_fail = next(p for name, p in publish.events if name == "sprint_f.canary.gate.failed")
    assert gate_fail["slo_breach_reason"] == "error_rate_over"
    assert gate_fail["runner_success_rate"] == pytest.approx(0.88)
    assert gate_fail["revert_loop_count"] == 3
    rolled_back = next(p for name, p in publish.events if name == "sprint_f.canary.rolled_back")
    assert rolled_back["rollout_pct"] == 0
    assert "gate_failed" in rolled_back["reason"]
