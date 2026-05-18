"""OP-1504 — WP.10 BP Fleet UI Lanes tests.

Pins the classifier semantics (which status → which lane, ambient
precedence) and the router contract (4 lane keys always present, detail
endpoint, revoke transitions Active→History via status=error).

Both layers are tested:

* The pure classifier (:mod:`backend.bp_fleet_lanes`) gets unit tests
  with hand-built ``Agent`` fixtures — no FastAPI machinery needed.
* The router (:mod:`backend.routers.bp_fleet_lanes`) gets HTTP-level
  tests via ``TestClient``; the in-memory mirror is patched directly
  to avoid pulling in the full agents-router import chain (character
  cards, talent tree, party store).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import bp_fleet_lanes as lanes
from backend.models import Agent, AgentProgress, AgentStatus, AgentType
from backend.routers import bp_fleet_lanes as fleet_router


# ─────────────────────────── fixtures ───────────────────────────


def _agent(
    agent_id: str,
    *,
    status: AgentStatus = AgentStatus.idle,
    sub_type: str = "",
    current: int = 0,
    total: int = 0,
    name: str | None = None,
    ai_model: str | None = None,
) -> Agent:
    return Agent(
        id=agent_id,
        name=name or f"Agent {agent_id}",
        type=AgentType.software,
        sub_type=sub_type,
        status=status,
        progress=AgentProgress(current=current, total=total),
        ai_model=ai_model,
    )


@pytest.fixture
def fleet_fixture() -> list[Agent]:
    """A handful of agents covering each lane."""
    return [
        _agent("a-run", status=AgentStatus.running),
        _agent("a-boot", status=AgentStatus.booting),
        _agent("a-mat", status=AgentStatus.materializing),
        _agent("a-wait", status=AgentStatus.awaiting_confirmation),
        _agent("s-idle", status=AgentStatus.idle),
        _agent("s-warn", status=AgentStatus.warning),
        _agent("amb-watch", status=AgentStatus.running, sub_type="watchdog"),
        _agent("amb-done", status=AgentStatus.success, sub_type="ambient"),
        _agent("h-ok", status=AgentStatus.success),
        _agent("h-err", status=AgentStatus.error),
    ]


@pytest.fixture
def client(monkeypatch, fleet_fixture) -> TestClient:
    """TestClient mounting only the bp_fleet_lanes router.

    ``_current_agents`` is patched so the router reads the fixture list
    instead of the live in-memory mirror owned by the agents router.
    """
    monkeypatch.setattr(
        fleet_router, "_current_agents", lambda: list(fleet_fixture)
    )
    app = FastAPI()
    app.include_router(fleet_router.router)
    return TestClient(app)


# ─────────────────────────── classifier ─────────────────────────


def test_lane_keys_are_stable() -> None:
    assert lanes.LANE_KEYS == ("active", "scheduled", "ambient", "history")


def test_classify_returns_all_lane_keys_even_when_empty() -> None:
    binned = lanes.classify([])
    assert set(binned.keys()) == set(lanes.LANE_KEYS)
    assert all(v == [] for v in binned.values())


def test_active_lane_captures_running_booting_materializing_and_awaiting(
    fleet_fixture: list[Agent],
) -> None:
    binned = lanes.classify(fleet_fixture)
    ids = {a.id for a in binned["active"]}
    assert ids == {"a-run", "a-boot", "a-mat", "a-wait"}


def test_scheduled_lane_captures_idle_and_warning(
    fleet_fixture: list[Agent],
) -> None:
    binned = lanes.classify(fleet_fixture)
    ids = {a.id for a in binned["scheduled"]}
    assert ids == {"s-idle", "s-warn"}


def test_ambient_subtype_dominates_run_state(
    fleet_fixture: list[Agent],
) -> None:
    """Ambient classification beats running/success.

    ``amb-watch`` is status=running but sub_type=watchdog → Ambient.
    ``amb-done`` is status=success but sub_type=ambient → Ambient.
    Neither shows up under Active or History.
    """
    binned = lanes.classify(fleet_fixture)
    ambient_ids = {a.id for a in binned["ambient"]}
    assert ambient_ids == {"amb-watch", "amb-done"}
    assert "amb-watch" not in {a.id for a in binned["active"]}
    assert "amb-done" not in {a.id for a in binned["history"]}


def test_history_lane_captures_success_and_error(
    fleet_fixture: list[Agent],
) -> None:
    binned = lanes.classify(fleet_fixture)
    ids = {a.id for a in binned["history"]}
    assert ids == {"h-ok", "h-err"}


def test_lane_counts_match_classify_lengths(
    fleet_fixture: list[Agent],
) -> None:
    counts = lanes.lane_counts(fleet_fixture)
    binned = lanes.classify(fleet_fixture)
    assert counts == {k: len(binned[k]) for k in lanes.LANE_KEYS}


def test_detail_payload_marks_revocable_for_active_and_ambient() -> None:
    active = _agent("act", status=AgentStatus.running)
    ambient = _agent("amb", status=AgentStatus.running, sub_type="ambient")
    scheduled = _agent("sch", status=AgentStatus.idle)
    history = _agent("his", status=AgentStatus.success)

    assert lanes.detail_payload(active)["revocable"] is True
    assert lanes.detail_payload(ambient)["revocable"] is True
    assert lanes.detail_payload(scheduled)["revocable"] is False
    assert lanes.detail_payload(history)["revocable"] is False


# ─────────────────────────── router ─────────────────────────────


def test_get_lanes_returns_all_four_lanes_plus_counts(client: TestClient) -> None:
    resp = client.get("/bp/fleet/lanes")
    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload["lanes"].keys()) == set(lanes.LANE_KEYS)
    # counts mirror the lane list lengths
    for key in lanes.LANE_KEYS:
        assert payload["counts"][key] == len(payload["lanes"][key])


def test_get_lanes_compact_row_shape(client: TestClient) -> None:
    resp = client.get("/bp/fleet/lanes")
    rows = resp.json()["lanes"]["active"]
    assert rows  # the fixture has active agents
    row = rows[0]
    expected_keys = {"id", "name", "type", "sub_type", "status", "ai_model", "progress"}
    assert expected_keys <= set(row.keys())
    assert {"current", "total"} <= set(row["progress"].keys())


def test_get_lane_counts_endpoint(client: TestClient) -> None:
    resp = client.get("/bp/fleet/lanes/counts")
    assert resp.status_code == 200
    counts = resp.json()
    assert set(counts.keys()) == set(lanes.LANE_KEYS)
    # 4 active, 2 scheduled, 2 ambient, 2 history from fleet_fixture
    assert counts == {"active": 4, "scheduled": 2, "ambient": 2, "history": 2}


def test_get_agent_detail_returns_full_envelope(client: TestClient) -> None:
    resp = client.get("/bp/fleet/agents/a-run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "a-run"
    assert body["lane"] == "active"
    assert body["revocable"] is True
    assert "workspace" in body and "sub_tasks" in body


def test_get_agent_detail_404_for_unknown(client: TestClient) -> None:
    resp = client.get("/bp/fleet/agents/does-not-exist")
    assert resp.status_code == 404


def test_revoke_moves_active_agent_to_history(client, fleet_fixture, monkeypatch) -> None:
    """Revoke flips status to error → lane should now be history."""
    # The router mutates a shared dict; build one that the router uses.
    mirror: dict[str, Agent] = {a.id: a for a in fleet_fixture}

    # Stub both seams: classifier listing AND the mirror used by revoke.
    monkeypatch.setattr(
        fleet_router, "_current_agents", lambda: list(mirror.values())
    )
    # Inject mirror into the agents-router seam that revoke reads.
    import backend.routers.agents as agents_router
    monkeypatch.setattr(agents_router, "_agents", mirror)

    resp = client.post("/bp/fleet/agents/a-run/revoke")
    assert resp.status_code == 200
    body = resp.json()
    assert body["prev_lane"] == "active"
    assert body["new_lane"] == "history"
    assert body["status"] == "error"
    # The mirror itself reflects the new status.
    assert mirror["a-run"].status == AgentStatus.error


def test_revoke_rejects_scheduled_lane(client, fleet_fixture, monkeypatch) -> None:
    mirror: dict[str, Agent] = {a.id: a for a in fleet_fixture}
    import backend.routers.agents as agents_router
    monkeypatch.setattr(agents_router, "_agents", mirror)
    monkeypatch.setattr(
        fleet_router, "_current_agents", lambda: list(mirror.values())
    )

    resp = client.post("/bp/fleet/agents/s-idle/revoke")
    assert resp.status_code == 409


def test_revoke_404_for_unknown(client, monkeypatch) -> None:
    import backend.routers.agents as agents_router
    monkeypatch.setattr(agents_router, "_agents", {})
    monkeypatch.setattr(fleet_router, "_current_agents", list)
    resp = client.post("/bp/fleet/agents/nope/revoke")
    assert resp.status_code == 404
