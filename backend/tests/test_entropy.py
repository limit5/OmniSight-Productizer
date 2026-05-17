"""OP-1234 — Smoke tests for ``backend/routers/entropy.py``.

Pins the typed shape of the two endpoints that surface
SemanticEntropyMonitor snapshots to the Agent Matrix Wall and the
Ops Summary panel. The math/state-machine itself is exercised by
``test_semantic_entropy.py``; here we only check:

  * router prefix + tags + registered routes
  * function return-type annotations are concrete (``dict[str, Any]``,
    not bare ``dict``)
  * empty monitor → ``{"agents": [], "highest": None}``
  * after ``record_output()`` the list endpoint surfaces the new agent
  * unknown agent_id → HTTP 404
"""

from __future__ import annotations

import typing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import semantic_entropy as se
from backend.routers import entropy as entropy_router


@pytest.fixture(autouse=True)
def _reset_monitor():
    se.reset_for_tests()
    yield
    se.reset_for_tests()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(entropy_router.router)
    return TestClient(app)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Router surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_prefix_and_tags():
    assert entropy_router.router.prefix == "/entropy"
    assert "entropy" in entropy_router.router.tags


def test_route_registration_full_set():
    pairs = sorted(
        (sorted(r.methods)[0], r.path)
        for r in entropy_router.router.routes
        if hasattr(r, "methods") and r.methods
    )
    assert pairs == [
        ("GET", "/entropy/agents"),
        ("GET", "/entropy/agents/{agent_id}"),
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Type-hint coverage (OP-1234 scope anchor)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_list_entropy_return_annotation_is_parameterized():
    """``-> dict`` (bare) would regress to ``Any``-valued payloads."""
    hints = typing.get_type_hints(entropy_router.list_entropy)
    assert hints["return"] == dict[str, typing.Any]


def test_get_entropy_signature_is_fully_typed():
    hints = typing.get_type_hints(entropy_router.get_entropy)
    assert hints["agent_id"] is str
    assert hints["return"] == dict[str, typing.Any]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Behavioural smoke — endpoints stay non-regressive
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_list_entropy_empty_monitor(client):
    r = client.get("/entropy/agents")
    assert r.status_code == 200
    body = r.json()
    assert body == {"agents": [], "highest": None}


def test_list_entropy_surfaces_recorded_agent(client):
    # Force a measurement so the agent state has a non-zero score.
    for i in range(3):
        se.record_output("alpha", f"output number {i}", force_check=True)

    r = client.get("/entropy/agents")
    assert r.status_code == 200
    body = r.json()
    agent_ids = [a["agent_id"] for a in body["agents"]]
    assert "alpha" in agent_ids
    assert body["highest"] is not None
    assert body["highest"]["agent_id"] == "alpha"


def test_get_entropy_returns_snapshot_for_known_agent(client):
    se.record_output("beta", "hello world", force_check=True)
    se.record_output("beta", "hello there", force_check=True)

    r = client.get("/entropy/agents/beta")
    assert r.status_code == 200
    snap = r.json()
    assert snap["agent_id"] == "beta"
    assert "entropy_score" in snap
    assert "verdict" in snap


def test_get_entropy_unknown_agent_returns_404(client):
    r = client.get("/entropy/agents/does-not-exist")
    assert r.status_code == 404
    assert "no entropy snapshot" in r.json()["detail"].lower()
