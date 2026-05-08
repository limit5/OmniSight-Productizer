"""OP-771 -- progressive canary rollout contracts."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from backend import auth
from backend import canary_rollout as cr


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


class _Monitor:
    def __init__(self, *snapshots: cr.SloSnapshot) -> None:
        self.snapshots = list(snapshots)

    def snapshot(self) -> cr.SloSnapshot:
        if not self.snapshots:
            return cr.SloSnapshot(error_rate=0.0, source="test")
        return self.snapshots.pop(0)


def _controller(
    tmp_path: Path,
    *,
    monitor: _Monitor | None = None,
    clock: _Clock | None = None,
) -> tuple[cr.CanaryController, _Clock, Path, Path]:
    clock = clock or _Clock()
    snippet = tmp_path / "canary-weighted.caddy"
    state = tmp_path / "canary_state.json"
    controller = cr.CanaryController(
        writer=cr.CaddyWeightWriter(snippet, state_path=state),
        monitor=monitor or _Monitor(),
        clock=clock,
    )
    return controller, clock, snippet, state


def test_start_writes_5_percent_caddy_weights(tmp_path: Path) -> None:
    controller, _, snippet, state_path = _controller(tmp_path)

    state = controller.start(
        rollout_id="op-771",
        stable_color="blue",
        canary_color="green",
    )

    assert state.stage.canary_percent == 5
    text = snippet.read_text(encoding="utf-8")
    assert "lb_policy weighted_round_robin 95 5" in text
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["stage"]["canary_percent"] == 5


def test_per_tenant_hashing_is_stable_and_monotonic() -> None:
    tenants = [f"tenant-{i}" for i in range(1000)]

    first = {
        tenant
        for tenant in tenants
        if cr.stable_canary_assignment(tenant, 5)
    }
    second = {
        tenant
        for tenant in tenants
        if cr.stable_canary_assignment(tenant, 5)
    }
    wider = {
        tenant
        for tenant in tenants
        if cr.stable_canary_assignment(tenant, 25)
    }

    assert first == second
    assert first.issubset(wider)
    assert 30 <= len(first) <= 70
    assert 220 <= len(wider) <= 280


def test_slo_pass_advances_5_to_25_then_100(tmp_path: Path) -> None:
    controller, clock, snippet, _ = _controller(tmp_path)
    state = controller.start(
        rollout_id="op-771",
        stable_color="blue",
        canary_color="green",
    )

    clock.advance(10 * 60)
    state, decision = controller.evaluate(state)
    assert decision.action == "advance"
    assert state.stage.canary_percent == 25
    assert "lb_policy weighted_round_robin 75 25" in snippet.read_text(
        encoding="utf-8"
    )

    clock.advance(15 * 60)
    state, decision = controller.evaluate(state)
    assert decision.action == "advance"
    assert state.stage.canary_percent == 100
    assert "OMNISIGHT_UPSTREAM_B" in snippet.read_text(encoding="utf-8")


def test_synthetic_50_percent_error_at_5_percent_auto_aborts_before_25(
    tmp_path: Path,
) -> None:
    controller, clock, snippet, _ = _controller(
        tmp_path,
        monitor=_Monitor(cr.SloSnapshot(error_rate=0.50, source="synthetic")),
    )
    state = controller.start(
        rollout_id="op-771",
        stable_color="blue",
        canary_color="green",
    )

    clock.advance(10 * 60)
    state, decision = controller.evaluate(state)

    assert decision.action == "abort"
    assert decision.stage == "5"
    assert state.status == "aborted"
    assert state.stage.canary_percent == 5
    text = snippet.read_text(encoding="utf-8")
    assert "OMNISIGHT_UPSTREAM_A" in text
    assert "OMNISIGHT_UPSTREAM_B" not in text


def test_manual_pause_advance_abort_controls_rewrite_state(
    tmp_path: Path,
) -> None:
    controller, _, _, state_path = _controller(tmp_path)
    state = controller.start(
        rollout_id="op-771",
        stable_color="blue",
        canary_color="green",
    )

    state = controller.manual_control(state, "pause", reason="operator_pause")
    assert state.status == "paused"

    state = controller.manual_control(state, "advance", reason="operator_advance")
    assert state.status == "running"
    assert state.stage.canary_percent == 25

    state = controller.manual_control(state, "abort", reason="operator_abort")
    assert state.status == "aborted"
    assert json.loads(state_path.read_text(encoding="utf-8"))["reason"] == (
        "operator_abort"
    )


def test_router_exposes_dashboard_control_with_auth_dependencies() -> None:
    from backend.routers import canary_rollout

    assert "Depends(auth.require_admin)" in inspect.getsource(
        canary_rollout.start_canary
    )
    assert "Depends(auth.require_admin)" in inspect.getsource(
        canary_rollout.evaluate_canary
    )
    assert "Depends(auth.require_admin)" in inspect.getsource(
        canary_rollout.control_canary
    )
    assert "Depends(auth.require_viewer)" in inspect.getsource(
        canary_rollout.tenant_assignment
    )


@pytest.mark.asyncio
async def test_router_control_and_assignment_use_controller_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.routers import canary_rollout

    state_path = tmp_path / "canary_state.json"
    snippet = tmp_path / "canary-weighted.caddy"
    monkeypatch.setattr(cr, "DEFAULT_STATE_PATH", state_path)
    monkeypatch.setattr(cr, "DEFAULT_SNIPPET_PATH", snippet)

    actor = auth.User(id="u1", email="admin@example.com", name="Admin", role="admin")
    await canary_rollout.start_canary(
        canary_rollout.StartCanaryRequest(
            rollout_id="op-771",
            stable_color="blue",
            canary_color="green",
        ),
        _actor=actor,
    )

    paused = await canary_rollout.control_canary(
        canary_rollout.ManualControlRequest(
            command="pause",
            reason="dashboard_pause",
        ),
        _actor=actor,
    )
    assert paused["status"] == "paused"

    assignment = await canary_rollout.tenant_assignment("tenant-a", _actor=actor)
    assert assignment["rollout_id"] == "op-771"
    assert assignment["target_color"] in {"blue", "green"}
