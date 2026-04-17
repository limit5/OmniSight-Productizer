"""R0 (#306) — PEP router smoke tests.

Exercises each endpoint through a FastAPI TestClient mounted with
just the PEP router (no auth middleware) so the assertions stay
focused on the router's own contract:

  * GET /pep/live        → recent + held + stats + breaker snapshot.
  * GET /pep/policy      → tier whitelists + rule names.
  * GET /pep/status      → breaker snapshot + held count.
  * GET /pep/held        → the HELD queue only.
  * GET /pep/decisions   → paginated recent ring.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import pep_gateway as pep
from backend.routers import pep as pep_router_module


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(pep_router_module.router)
    pep._reset_for_tests()
    return TestClient(app)


def _seed_one_of_each():
    import time
    now = time.time()
    pep._record_recent(pep.PepDecision(
        id="auto-1", ts=now, agent_id="a1", tool="read_file", command="read_file path=x",
        tier="t1", action=pep.PepAction.auto_allow, rule="tier_whitelist",
        impact_scope="local",
    ))
    held = pep.PepDecision(
        id="held-1", ts=now, agent_id="a1", tool="run_bash",
        command="./deploy.sh prod", tier="t3",
        action=pep.PepAction.hold, rule="deploy_prod", impact_scope="prod",
        decision_id="dec-abc",
    )
    pep._held_add(held)
    pep._record_recent(held)
    pep._record_recent(pep.PepDecision(
        id="deny-1", ts=now, agent_id="a2", tool="run_bash", command="rm -rf /",
        tier="t3", action=pep.PepAction.deny, rule="rm_rf_root", impact_scope="destructive",
    ))


def test_live_endpoint_returns_snapshot(client):
    _seed_one_of_each()
    r = client.get("/pep/live")
    assert r.status_code == 200
    data = r.json()
    assert "recent" in data
    assert "held" in data
    assert "stats" in data
    assert "breaker" in data
    assert data["stats"]["total"] == 3
    assert data["stats"]["auto_allowed"] == 1
    assert data["stats"]["held"] == 1
    assert data["stats"]["denied"] == 1
    assert len(data["held"]) == 1
    assert data["held"][0]["id"] == "held-1"


def test_policy_endpoint_lists_tier_whitelists(client):
    r = client.get("/pep/policy")
    assert r.status_code == 200
    data = r.json()
    assert "read_file" in data["tiers"]["t1"]
    assert "run_bash" not in data["tiers"]["t1"]
    assert "run_bash" in data["tiers"]["t3"]
    assert data["destructive_rule_count"] > 5
    assert data["prod_hold_rule_count"] > 3
    assert "rm_rf_root" in data["destructive_rules"]
    assert "deploy_prod" in data["prod_hold_rules"]


def test_status_endpoint_returns_breaker_state(client):
    r = client.get("/pep/status")
    assert r.status_code == 200
    data = r.json()
    assert "breaker" in data
    assert "stats" in data
    assert "held_count" in data
    assert data["breaker"]["open"] is False


def test_held_and_decisions_endpoints(client):
    _seed_one_of_each()
    r = client.get("/pep/held")
    assert r.status_code == 200
    assert r.json()["count"] == 1

    r = client.get("/pep/decisions?limit=2")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2


def test_live_limit_is_clamped(client):
    r = client.get("/pep/live?limit=99999")
    assert r.status_code == 200
    # server silently clamps — just make sure no 422
