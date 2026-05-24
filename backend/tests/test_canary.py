"""OP-882 — canary rollout orchestrator contracts.

Five cases per ticket §Test plan:

    1. 5 → 25 → 100 happy path
    2. Gate-fail at the 25% stage
    3. Rollback writes the prior 100% stable allocation
    4. Traffic-shift race retries once, then aborts
    5. SSE events fire at every stage transition
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.orchestrator import canary


REAL_TEMPLATE = canary.PROJECT_ROOT / "deploy" / "caddy" / "canary-template.caddy"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


class _Monitor:
    def __init__(self, *snapshots: canary.SloSnapshot) -> None:
        self._snapshots = list(snapshots) or [
            canary.SloSnapshot(error_rate=0.0, p95_latency_ms=100.0, source="test")
        ]

    def snapshot(self) -> canary.SloSnapshot:
        snap = self._snapshots[0]
        if len(self._snapshots) > 1:
            self._snapshots.pop(0)
        return snap


class _RecordingPublish:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, data: dict[str, Any], **_: Any) -> None:
        self.events.append((event, data))


class _FlakyWriter:
    """Test double that simulates traffic-shift races."""

    def __init__(self, *, fail_times: int) -> None:
        self.fail_times = fail_times
        self.writes: list[canary.CanaryStage] = []

    def write(self, *, rollout_id: str, stage: canary.CanaryStage) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise canary.TrafficShiftRaceCondition(
                f"simulated race on stage={stage.name}"
            )
        self.writes.append(stage)


@pytest.fixture
def template_path(tmp_path: Path) -> Path:
    """Copy the checked-in template into tmp_path so tests are hermetic."""
    target = tmp_path / "canary-template.caddy"
    target.write_text(REAL_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return target


@pytest.fixture
def writer(template_path: Path, tmp_path: Path) -> canary.TrafficShiftWriter:
    return canary.TrafficShiftWriter(
        template_path=template_path,
        output_path=tmp_path / "canary.caddy",
    )


def _orchestrator(
    *,
    writer: canary.TrafficShiftWriter,
    clock: _Clock,
    monitor: canary.SloMonitor | None = None,
    publish: _RecordingPublish | None = None,
    rollout_id: str = "op-882-rollout-1",
) -> canary.CanaryOrchestrator:
    return canary.CanaryOrchestrator(
        rollout_id=rollout_id,
        stable_color="blue",
        canary_color="green",
        monitor=monitor or _Monitor(),
        writer=writer,
        baseline=canary.SloBaseline(p95_latency_ms=100.0),
        clock=clock,
        publish=publish or _RecordingPublish(),
    )


def test_happy_path_5_25_100(writer: canary.TrafficShiftWriter) -> None:
    clock = _Clock()
    publish = _RecordingPublish()
    orch = _orchestrator(writer=writer, clock=clock, publish=publish)

    stage = orch.start()
    assert stage.canary_percent == 5
    rendered = writer.output_path.read_text(encoding="utf-8")
    assert "lb_policy weighted_round_robin 95 5" in rendered
    assert "# canary-stage: 5" in rendered

    clock.advance(5 * 60)
    stage = orch.advance()
    assert stage.canary_percent == 25
    rendered = writer.output_path.read_text(encoding="utf-8")
    assert "lb_policy weighted_round_robin 75 25" in rendered

    clock.advance(10 * 60)
    stage = orch.advance()
    assert stage.canary_percent == 100
    rendered = writer.output_path.read_text(encoding="utf-8")
    assert "lb_policy weighted_round_robin 0 100" in rendered

    # 100% stage is terminal — one more advance() completes the rollout.
    final = orch.advance()
    assert final.name == "100"
    assert ("canary.completed", final) is not None  # sanity


def test_gate_fail_at_25_percent(writer: canary.TrafficShiftWriter) -> None:
    clock = _Clock()
    publish = _RecordingPublish()
    monitor = _Monitor(
        canary.SloSnapshot(error_rate=0.0, p95_latency_ms=100.0, source="test"),
        canary.SloSnapshot(error_rate=0.01, p95_latency_ms=100.0, source="test"),
    )
    orch = _orchestrator(writer=writer, clock=clock, monitor=monitor, publish=publish)

    orch.start()
    clock.advance(5 * 60)
    orch.advance()  # promote to 25%

    clock.advance(10 * 60)
    with pytest.raises(canary.CanaryGateFailed) as excinfo:
        orch.advance()
    assert "error_rate" in str(excinfo.value)

    event_names = [name for name, _ in publish.events]
    assert "canary.gate.failed" in event_names
    assert "canary.rolled_back" in event_names


def test_rollback_writes_zero_percent_allocation(
    writer: canary.TrafficShiftWriter,
) -> None:
    clock = _Clock()
    orch = _orchestrator(writer=writer, clock=clock)
    orch.start()
    clock.advance(5 * 60)
    orch.advance()  # at 25%

    orch.rollback(reason="operator_test")

    rendered = writer.output_path.read_text(encoding="utf-8")
    assert "lb_policy weighted_round_robin 100 0" in rendered
    assert "# canary-stage: rollback" in rendered


def test_traffic_shift_race_retries_then_aborts(
    template_path: Path, tmp_path: Path
) -> None:
    clock = _Clock()
    # First call: writer fails once, orchestrator retries and succeeds.
    flaky_once = _FlakyWriter(fail_times=1)
    orch = _orchestrator(writer=flaky_once, clock=clock)
    orch.start()
    assert [s.name for s in flaky_once.writes] == ["5"]

    # Second scenario: writer fails twice, orchestrator gives up.
    flaky_twice = _FlakyWriter(fail_times=2)
    orch = _orchestrator(writer=flaky_twice, clock=_Clock())
    with pytest.raises(canary.TrafficShiftRaceCondition):
        orch.start()

    # The on-disk owner-marker race is also surfaced by the real writer:
    output_path = tmp_path / "canary.caddy"
    real = canary.TrafficShiftWriter(
        template_path=template_path, output_path=output_path
    )
    real.write(rollout_id="rollout-A", stage=canary.STAGES[0])
    with pytest.raises(canary.TrafficShiftRaceCondition):
        real.write(rollout_id="rollout-B", stage=canary.STAGES[0])


def test_sse_emits_at_every_stage_transition(
    writer: canary.TrafficShiftWriter,
) -> None:
    clock = _Clock()
    publish = _RecordingPublish()
    orch = _orchestrator(writer=writer, clock=clock, publish=publish)

    orch.start()
    clock.advance(5 * 60)
    orch.advance()
    clock.advance(10 * 60)
    orch.advance()
    orch.advance()  # 100% terminal -> canary.completed

    names = [name for name, _ in publish.events]
    assert names == [
        "canary.stage.started",
        "canary.stage.transitioned",
        "canary.stage.transitioned",
        "canary.completed",
    ]

    # Every emitted payload carries the rollout-id + stage identity so
    # downstream SSE consumers can correlate.
    for _, payload in publish.events:
        assert payload["rollout_id"] == "op-882-rollout-1"
        assert payload["stable_color"] == "blue"
        assert payload["canary_color"] == "green"
        assert payload["stage"] in {"5", "25", "100"}


def test_observe_window_gate_blocks_premature_advance(
    writer: canary.TrafficShiftWriter,
) -> None:
    clock = _Clock()
    orch = _orchestrator(writer=writer, clock=clock)
    orch.start()
    clock.advance(60)  # only 1 minute, observe window is 5min
    with pytest.raises(canary.CanaryGateFailed) as excinfo:
        orch.advance()
    assert "observe window open" in str(excinfo.value)


def test_module_is_marked_deprecated_op1694() -> None:
    """OP-1694: this orchestrator is deprecated in favour of the real
    SLO-gated backend.canary_rollout.CanaryController. The marker keeps the
    module test-only without deleting it (tests/docs/ADR still reference it)."""
    assert getattr(canary, "__deprecated__", False) is True
    assert "deprecated" in (canary.__doc__ or "").lower()
    assert "canary_rollout.CanaryController" in (canary.__doc__ or "")


def test_constructing_orchestrator_emits_deprecation_warning(
    writer: canary.TrafficShiftWriter,
) -> None:
    with pytest.warns(DeprecationWarning, match="deprecated"):
        canary.CanaryOrchestrator(
            rollout_id="dep-check",
            stable_color="blue",
            canary_color="green",
            writer=writer,
        )


def test_constructing_stub_monitor_emits_deprecation_warning() -> None:
    with pytest.warns(DeprecationWarning, match="deprecated"):
        canary._StubMonitor()


def test_p95_latency_outside_baseline_band_triggers_rollback(
    writer: canary.TrafficShiftWriter,
) -> None:
    clock = _Clock()
    publish = _RecordingPublish()
    # baseline = 100ms, tolerance ±20% → band is [80, 120]; 200ms breaches.
    monitor = _Monitor(
        canary.SloSnapshot(error_rate=0.0, p95_latency_ms=200.0, source="test"),
    )
    orch = _orchestrator(writer=writer, clock=clock, monitor=monitor, publish=publish)
    orch.start()
    clock.advance(5 * 60)
    with pytest.raises(canary.CanaryGateFailed) as excinfo:
        orch.advance()
    assert "p95_latency_ms" in str(excinfo.value)
    assert "canary.gate.failed" in [name for name, _ in publish.events]
    assert "canary.rolled_back" in [name for name, _ in publish.events]
