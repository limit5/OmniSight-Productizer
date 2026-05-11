"""OP-949 H4 — Operator approval API contract tests.

Mirrors the 6-case Test plan in the H4 ticket, exercised at the
backend layer:

1. List pending — only releases with an unresolved approval-pending
   sub-state appear in ``GET /release-approvals/pending``.
2. Approve happy path — POST records the decision, dispatches an H2
   event, and the row drops off the pending list on next read.
3. Abort — POST routes to the ``approval_aborted`` log entry.
4. Auth refused — bot principals (``apikey:*`` / ``*-bot``) get 403
   even when the role is admin.
5. Race condition — two operators race; the second receives 409 with
   ``error="already_resolved"`` and the prior verdict in the body.
6. Backend dispatch fail — H2 INSERT failure is surfaced as 502
   with ``error="dispatch_failed"`` while the decision is already
   recorded on the state machine.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth
from backend.api import release_approval as release_approval_api
from backend.release_conductor import event_router, state_machine


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0232 = BACKEND_ROOT / "alembic" / "versions" / "0232_release_events.py"
MIGRATION_0233 = BACKEND_ROOT / "alembic" / "versions" / "0233_release_state.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def approval_engine():
    """In-memory sqlite engine with the 0232 + 0233 schemas applied."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy.pool import StaticPool

    engine = sa.create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mig232 = _load_module(
        MIGRATION_0232, "_alembic_test_0232_for_op949"
    )
    mig233 = _load_module(
        MIGRATION_0233, "_alembic_test_0233_for_op949"
    )
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            mig233.upgrade()
            mig232.upgrade()
        conn.commit()

    state_machine.set_engine_for_tests(engine)
    event_router.set_engine_for_tests(engine)
    try:
        yield engine
    finally:
        state_machine.set_engine_for_tests(None)
        event_router.set_engine_for_tests(None)
        engine.dispose()


class _StubUser:
    """Minimal `auth.User` lookalike for dependency overrides."""

    def __init__(self, id: str = "alice", email: str = "alice@example.com"):
        self.id = id
        self.email = email
        self.name = email
        self.role = "admin"
        self.enabled = True
        self.must_change_password = False
        self.tenant_id = "t-default"


def _make_client(user: _StubUser) -> TestClient:
    """FastAPI TestClient with the approval router + auth bypassed."""
    app = FastAPI()
    app.include_router(release_approval_api.router)

    # The approval router uses Depends(auth.require_admin) which itself
    # depends on auth.current_user; overriding the inner dep is enough.
    app.dependency_overrides[auth.require_admin] = lambda: user
    app.dependency_overrides[auth.current_user] = lambda: user
    return TestClient(app)


# ─── Test #1 — list pending ───────────────────────────────────────────────


def test_list_pending_only_shows_unresolved(approval_engine) -> None:
    state_machine.create(release_id="OP-1001", version="v1.0.0")
    state_machine.transition(
        release_id="OP-1001",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_BUILDING,
        reason="g1",
    )
    state_machine.transition(
        release_id="OP-1001",
        from_state=state_machine.STATE_BUILDING,
        to_state=state_machine.STATE_STAGING,
        reason="green",
    )
    state_machine.request_approval(
        release_id="OP-1001",
        canary_percent=5,
        slo_snapshot={"error_rate": 0.001, "p95_latency_ms": 110},
        reason="staging_gate",
    )

    # Resolved release does NOT appear.
    state_machine.create(release_id="OP-1002", version="v1.0.1")
    state_machine.request_approval(release_id="OP-1002", canary_percent=5)
    state_machine.record_decision(
        release_id="OP-1002", decision="approve", operator="bob@example.com"
    )

    # Release with no approval request does NOT appear.
    state_machine.create(release_id="OP-1003", version="v1.0.2")

    client = _make_client(_StubUser())
    resp = client.get("/release-approvals/pending")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["pending"]) == 1
    row = body["pending"][0]
    assert row["release_id"] == "OP-1001"
    assert row["version"] == "v1.0.0"
    assert row["canary_percent"] == 5
    assert row["slo_snapshot"]["error_rate"] == pytest.approx(0.001)
    assert row["reason"] == "staging_gate"


# ─── Test #2 — approve happy path ─────────────────────────────────────────


def test_approve_records_decision_and_dispatches_event(approval_engine) -> None:
    state_machine.create(release_id="OP-2001", version="v2.0.0")
    state_machine.request_approval(release_id="OP-2001", canary_percent=5)

    client = _make_client(_StubUser(email="alice@example.com"))
    resp = client.post(
        "/release-approvals/OP-2001/approve",
        json={"reason": "looks good"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["release_id"] == "OP-2001"
    assert body["decision"] == "approve"
    assert body["operator"] == "alice@example.com"
    assert isinstance(body["dispatched_event_row_id"], int)
    assert body["dispatched_event_row_id"] > 0
    assert body["row_version"] >= 2  # at least 1 request + 1 decision bump

    # Pending list is now empty.
    pending = client.get("/release-approvals/pending").json()
    assert pending["pending"] == []

    # State machine log has the granted entry.
    status = state_machine.get_approval_status(release_id="OP-2001")
    assert status is not None
    assert status["kind"] == "approval_granted"
    assert status["operator"] == "alice@example.com"

    # H2 event landed with the operator source + matching event_type.
    row = event_router.get_event(row_id=body["dispatched_event_row_id"])
    assert row["source"] == "operator"
    assert row["event_type"] == "operator.approval.granted"
    assert row["payload"]["release_id"] == "OP-2001"


# ─── Test #3 — abort ──────────────────────────────────────────────────────


def test_abort_routes_to_aborted_log_entry(approval_engine) -> None:
    state_machine.create(release_id="OP-2002", version="v2.0.1")
    state_machine.request_approval(release_id="OP-2002", canary_percent=5)

    client = _make_client(_StubUser(email="alice@example.com"))
    resp = client.post(
        "/release-approvals/OP-2002/abort",
        json={"reason": "p95 spike"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "abort"

    status = state_machine.get_approval_status(release_id="OP-2002")
    assert status is not None
    assert status["kind"] == "approval_aborted"

    row = event_router.get_event(row_id=body["dispatched_event_row_id"])
    assert row["event_type"] == "operator.approval.aborted"


# ─── Test #4 — auth refused (bot principals) ──────────────────────────────


@pytest.mark.parametrize(
    "principal_id,principal_email",
    [
        ("apikey:abc", "apikey:my-bot"),
        ("ci-runner", "ci-runner@example.com"),
        ("alice", "merger-agent-bot@example.com"),
        ("alice", "claude-bot@example.com"),
    ],
)
def test_bot_principal_is_refused(
    approval_engine, principal_id: str, principal_email: str
) -> None:
    state_machine.create(release_id="OP-3001", version="v3.0.0")
    state_machine.request_approval(release_id="OP-3001", canary_percent=5)

    bot = _StubUser(id=principal_id, email=principal_email)
    client = _make_client(bot)
    resp = client.post(
        "/release-approvals/OP-3001/approve",
        json={"reason": "should refuse"},
    )
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "auth_refused"

    # The pending-list endpoint also refuses bots.
    resp2 = client.get("/release-approvals/pending")
    assert resp2.status_code == 403


def test_human_principal_is_allowed(approval_engine) -> None:
    state_machine.create(release_id="OP-3002", version="v3.0.1")
    state_machine.request_approval(release_id="OP-3002", canary_percent=5)
    client = _make_client(_StubUser(email="alice@example.com"))
    resp = client.get("/release-approvals/pending")
    assert resp.status_code == 200


# ─── Test #5 — race condition ─────────────────────────────────────────────


def test_race_condition_second_operator_gets_409(approval_engine) -> None:
    state_machine.create(release_id="OP-4001", version="v4.0.0")
    state_machine.request_approval(release_id="OP-4001", canary_percent=5)

    alice = _make_client(_StubUser(email="alice@example.com"))
    bob = _make_client(_StubUser(id="bob", email="bob@example.com"))

    first = alice.post(
        "/release-approvals/OP-4001/approve", json={"reason": "first"}
    )
    assert first.status_code == 200

    second = bob.post(
        "/release-approvals/OP-4001/approve", json={"reason": "racer"}
    )
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["error"] == "already_resolved"
    assert detail["prior"]["operator"] == "alice@example.com"
    assert detail["prior"]["kind"] == "approval_granted"


# ─── Test #6 — backend dispatch fail ──────────────────────────────────────


def test_dispatch_failure_surfaces_as_502(approval_engine) -> None:
    state_machine.create(release_id="OP-5001", version="v5.0.0")
    state_machine.request_approval(release_id="OP-5001", canary_percent=5)

    client = _make_client(_StubUser(email="alice@example.com"))

    with patch.object(
        event_router,
        "persist_event",
        side_effect=event_router.EventInsertFailed("DB unreachable"),
    ):
        resp = client.post(
            "/release-approvals/OP-5001/approve",
            json={"reason": "should record then fail dispatch"},
        )
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "dispatch_failed"

    # The state machine log still has the granted decision — the
    # decision is durable; only the H2 event re-queue is what failed.
    status = state_machine.get_approval_status(release_id="OP-5001")
    assert status is not None
    assert status["kind"] == "approval_granted"


# ─── Bonus contract tests (sub-state primitives) ──────────────────────────


def test_request_approval_is_idempotent(approval_engine) -> None:
    state_machine.create(release_id="OP-6001", version="v6.0.0")
    first = state_machine.request_approval(
        release_id="OP-6001", canary_percent=5, reason="r1"
    )
    second = state_machine.request_approval(
        release_id="OP-6001", canary_percent=5, reason="r1"
    )
    assert second["deduplicated"] is True
    assert second["row_version"] == first["row_version"]

    pending = state_machine.list_pending_approvals()
    assert [r["release_id"] for r in pending] == ["OP-6001"]


def test_record_decision_refuses_without_pending(approval_engine) -> None:
    state_machine.create(release_id="OP-6002", version="v6.0.1")
    with pytest.raises(state_machine.ApprovalAlreadyResolved):
        state_machine.record_decision(
            release_id="OP-6002",
            decision="approve",
            operator="alice@example.com",
        )


def test_record_decision_validates_inputs(approval_engine) -> None:
    state_machine.create(release_id="OP-6003", version="v6.0.2")
    state_machine.request_approval(release_id="OP-6003")
    with pytest.raises(ValueError, match="decision"):
        state_machine.record_decision(
            release_id="OP-6003",
            decision="maybe",
            operator="alice@example.com",
        )
    with pytest.raises(ValueError, match="operator"):
        state_machine.record_decision(
            release_id="OP-6003",
            decision="approve",
            operator="   ",
        )


def test_release_id_validation_at_api_boundary(approval_engine) -> None:
    state_machine.create(release_id="OP-7001", version="v7.0.0")
    state_machine.request_approval(release_id="OP-7001")
    client = _make_client(_StubUser(email="alice@example.com"))

    # Bad shape — leading non-alpha char.
    resp = client.post(
        "/release-approvals/9bad-id/approve", json={"reason": "x"}
    )
    assert resp.status_code == 400

    # Unknown release id passes shape check but 404s downstream.
    resp = client.post(
        "/release-approvals/OP-9999/approve", json={"reason": "x"}
    )
    assert resp.status_code == 404
