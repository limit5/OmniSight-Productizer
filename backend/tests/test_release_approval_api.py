"""OP-949 H4 — Operator approval web API contract tests.

Covers the AC test plan from the ticket description:

1. ``GET /approvals`` lists every release in ``pending_approval``
   (``staging`` rows) plus every release in ``canary_5`` (still
   abortable per AC #6).
2. ``POST /approvals/approve`` happy path — ``staging → canary_5`` +
   audit event row persisted (AC #4).
3. ``POST /approvals/abort`` happy path — ``staging → failed`` and
   ``canary_5 → rolled_back`` (AC #6).
4. ``ApprovalAuthRefused`` — non-operator roles get 403; API-key
   bearer-token users (the bot analogue of the Gerrit
   ``non-ai-reviewer`` group) get 403 even when role is admin.
5. ``RaceConditionApproval`` — two operators race; the second sees
   409 (either via stale ``row_version`` or via "already in canary_5").
6. ``BackendDispatchFailed`` — H2 event router insert raises; the
   endpoint surfaces 500 + the operator-facing retry hint.
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
from backend.api import release_approval
from backend.release_conductor import event_router, state_machine
from backend.release_conductor.event_handlers import slo_handlers


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
def release_engine():
    """In-memory sqlite engine with the 0232 + 0233 schemas applied.

    H4 reads ``release_state`` and writes ``release_events`` so both
    migrations need to land in the test DB.
    """
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy.pool import StaticPool

    engine = sa.create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mig232 = _load_module(MIGRATION_0232, "_alembic_test_0232_for_h4")
    mig233 = _load_module(MIGRATION_0233, "_alembic_test_0233_for_h4")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            mig233.upgrade()
            mig232.upgrade()
        conn.commit()

    event_router.set_engine_for_tests(engine)
    state_machine.set_engine_for_tests(engine)
    slo_handlers.reset_for_tests()
    try:
        yield engine
    finally:
        event_router.set_engine_for_tests(None)
        state_machine.set_engine_for_tests(None)
        slo_handlers.reset_for_tests()
        engine.dispose()


def _walk_to(release_id: str, version: str, target_state: str) -> None:
    """Helper: create + transition a release to ``target_state``.

    Walks the happy path: pending → building → staging → canary_5.
    """
    state_machine.create(release_id=release_id, version=version)
    walks = [
        (state_machine.STATE_PENDING, state_machine.STATE_BUILDING),
        (state_machine.STATE_BUILDING, state_machine.STATE_STAGING),
        (state_machine.STATE_STAGING, state_machine.STATE_CANARY_5),
    ]
    for src, dst in walks:
        if src == target_state:
            return
        state_machine.transition(
            release_id=release_id,
            from_state=src,
            to_state=dst,
            reason=f"walk-to-{dst}",
        )
        if dst == target_state:
            return


def _operator_user() -> auth.User:
    return auth.User(
        id="user-42",
        email="op@example.test",
        name="Operator 42",
        role="operator",
    )


def _viewer_user() -> auth.User:
    return auth.User(
        id="user-9",
        email="viewer@example.test",
        name="Viewer 9",
        role="viewer",
    )


def _apikey_admin_user() -> auth.User:
    return auth.User(
        id="apikey:bot-1",
        email="bot@example.test",
        name="Bot 1",
        role="admin",
    )


@pytest.fixture()
def operator_client(release_engine):
    """TestClient where current_user is a non-bot operator."""
    app = FastAPI()
    app.include_router(release_approval.router, prefix="/api/v1")
    app.dependency_overrides[auth.current_user] = _operator_user
    return TestClient(app)


@pytest.fixture()
def viewer_client(release_engine):
    """TestClient where current_user is a viewer (insufficient role)."""
    app = FastAPI()
    app.include_router(release_approval.router, prefix="/api/v1")
    app.dependency_overrides[auth.current_user] = _viewer_user
    return TestClient(app)


@pytest.fixture()
def bot_client(release_engine):
    """TestClient where current_user is an API-key bot (must be refused)."""
    app = FastAPI()
    app.include_router(release_approval.router, prefix="/api/v1")
    app.dependency_overrides[auth.current_user] = _apikey_admin_user
    return TestClient(app)


# ─── Case 1 — list endpoint surfaces staging + canary_5 ──────────────
def test_list_approvals_returns_staging_and_canary_5(operator_client) -> None:
    """AC #2 — ``pending_approval`` (staging) rows show up; AC #6 —
    ``canary_5`` rows do too (still abortable)."""
    _walk_to("OP-A001", "v0.1.0", state_machine.STATE_STAGING)
    _walk_to("OP-A002", "v0.2.0", state_machine.STATE_CANARY_5)
    # Building-state row must NOT appear in the queue (not yet awaiting
    # approval, not yet abortable).
    _walk_to("OP-A003", "v0.3.0", state_machine.STATE_BUILDING)

    resp = operator_client.get("/api/v1/release-conductor/approvals")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    release_ids = {row["release_id"] for row in body["releases"]}
    assert release_ids == {"OP-A001", "OP-A002"}

    by_id = {row["release_id"]: row for row in body["releases"]}
    assert by_id["OP-A001"]["sub_state"] == "pending_approval"
    assert by_id["OP-A001"]["canary_percent"] == 0
    assert by_id["OP-A002"]["sub_state"] == "approved_canary"
    assert by_id["OP-A002"]["canary_percent"] == 5

    # Each row must carry a row_version for the optimistic-locking
    # handshake the POST endpoints enforce.
    for row in body["releases"]:
        assert isinstance(row["row_version"], int)
        assert "slo_snapshot" in row
        assert "halted" in row["slo_snapshot"]


def test_list_approvals_surfaces_slo_halt(operator_client) -> None:
    """AC #3 — SLO snapshot must reflect the in-process halt registry."""
    _walk_to("OP-SLO-1", "v9.0.0", state_machine.STATE_STAGING)
    slo_handlers.on_slo_breach(
        {
            "release_id": "OP-SLO-1",
            "breach_id": "br-1",
            "error_rate": 0.07,
            "p95": 1200,
            "breach_window_start": "2026-05-12T00:00:00Z",
        }
    )
    resp = operator_client.get("/api/v1/release-conductor/approvals")
    assert resp.status_code == 200
    row = {r["release_id"]: r for r in resp.json()["releases"]}["OP-SLO-1"]
    assert row["slo_snapshot"]["halted"] is True
    assert row["slo_snapshot"]["breach"]["error_rate"] == 0.07


# ─── Case 2 — approve happy path ─────────────────────────────────────
def test_approve_happy_path_advances_state_and_records_event(
    operator_client,
) -> None:
    """AC #4 — operator approve → ``staging → canary_5`` + audit row."""
    _walk_to("OP-B001", "v1.0.0", state_machine.STATE_STAGING)
    row = state_machine.get_by_release_id(release_id="OP-B001")
    rv = row["row_version"]

    resp = operator_client.post(
        "/api/v1/release-conductor/approvals/approve",
        json={"release_id": "OP-B001", "row_version": rv},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["release_id"] == "OP-B001"
    assert body["state"] == state_machine.STATE_CANARY_5
    assert body["row_version"] == rv + 1
    assert body["audit_event_row_id"] > 0

    # Audit row is queryable through the H2 event router.
    audit = event_router.get_event(row_id=body["audit_event_row_id"])
    assert audit["source"] == release_approval.EVENT_SOURCE_OPERATOR
    assert audit["event_type"] == release_approval.EVENT_TYPE_APPROVED
    assert audit["payload"]["release_id"] == "OP-B001"
    assert audit["payload"]["action"] == "approve"
    assert audit["payload"]["actor"] == "op@example.test"


# ─── Case 3 — abort happy path (both edges) ──────────────────────────
def test_abort_from_staging_marks_failed(operator_client) -> None:
    """AC #6 — abort from staging transitions to ``failed``."""
    _walk_to("OP-C001", "v2.0.0", state_machine.STATE_STAGING)
    row = state_machine.get_by_release_id(release_id="OP-C001")
    rv = row["row_version"]
    resp = operator_client.post(
        "/api/v1/release-conductor/approvals/abort",
        json={"release_id": "OP-C001", "row_version": rv},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == state_machine.STATE_FAILED


def test_abort_from_canary_5_rolls_back(operator_client) -> None:
    """AC #6 — abort from canary_5 transitions to ``rolled_back``."""
    _walk_to("OP-C002", "v2.1.0", state_machine.STATE_CANARY_5)
    row = state_machine.get_by_release_id(release_id="OP-C002")
    rv = row["row_version"]
    resp = operator_client.post(
        "/api/v1/release-conductor/approvals/abort",
        json={"release_id": "OP-C002", "row_version": rv},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == state_machine.STATE_ROLLED_BACK

    # And the audit event records ``action=abort`` rather than approve.
    audit = event_router.get_event(row_id=body["audit_event_row_id"])
    assert audit["event_type"] == release_approval.EVENT_TYPE_ABORTED
    assert audit["payload"]["action"] == "abort"


def test_abort_from_unsupported_state_409(operator_client) -> None:
    """Abort outside the allowed source states surfaces 409."""
    _walk_to("OP-C003", "v2.2.0", state_machine.STATE_BUILDING)
    row = state_machine.get_by_release_id(release_id="OP-C003")
    resp = operator_client.post(
        "/api/v1/release-conductor/approvals/abort",
        json={"release_id": "OP-C003", "row_version": row["row_version"]},
    )
    assert resp.status_code == 409
    assert "RaceConditionApproval" in resp.json()["detail"]


# ─── Case 4 — auth refused ───────────────────────────────────────────
def test_auth_refused_for_viewer_role(viewer_client) -> None:
    """AC #5 — viewer role cannot approve (operator+ required)."""
    _walk_to("OP-D001", "v3.0.0", state_machine.STATE_STAGING)
    # List endpoint also gated.
    resp = viewer_client.get("/api/v1/release-conductor/approvals")
    assert resp.status_code == 403
    assert "ApprovalAuthRefused" in resp.json()["detail"]
    # Approve endpoint gated.
    resp = viewer_client.post(
        "/api/v1/release-conductor/approvals/approve",
        json={"release_id": "OP-D001", "row_version": 0},
    )
    assert resp.status_code == 403
    assert "ApprovalAuthRefused" in resp.json()["detail"]


def test_auth_refused_for_apikey_bot(bot_client) -> None:
    """AC #5 — API-key bearer bots are refused even with role=admin
    (the L3 analogue of the Gerrit ``non-ai-reviewer`` group check)."""
    _walk_to("OP-D002", "v3.1.0", state_machine.STATE_STAGING)
    row = state_machine.get_by_release_id(release_id="OP-D002")
    resp = bot_client.post(
        "/api/v1/release-conductor/approvals/approve",
        json={"release_id": "OP-D002", "row_version": row["row_version"]},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "ApprovalAuthRefused" in detail
    assert "non-ai-reviewer" in detail


# ─── Case 5 — race condition ─────────────────────────────────────────
def test_race_condition_stale_row_version_409(operator_client) -> None:
    """Two operators race; the second sees 409 ``RaceConditionApproval``.

    Operator A clicks first — POSTs row_version=N → succeeds (transition
    bumps to N+1). Operator B's UI is still showing row_version=N and
    POSTs the same — the endpoint refuses with 409 because the row has
    moved on.
    """
    _walk_to("OP-E001", "v4.0.0", state_machine.STATE_STAGING)
    row = state_machine.get_by_release_id(release_id="OP-E001")
    rv = row["row_version"]

    # Operator A wins.
    win = operator_client.post(
        "/api/v1/release-conductor/approvals/approve",
        json={"release_id": "OP-E001", "row_version": rv},
    )
    assert win.status_code == 200

    # Operator B's stale-view request — same row_version, but the row
    # has advanced to canary_5 + row_version=N+1 already.
    lose = operator_client.post(
        "/api/v1/release-conductor/approvals/approve",
        json={"release_id": "OP-E001", "row_version": rv},
    )
    assert lose.status_code == 409
    assert "RaceConditionApproval" in lose.json()["detail"]


def test_race_condition_row_version_mismatch_409(operator_client) -> None:
    """Stale row_version against an unmoved row also surfaces 409.

    Mirrors the case where the operator's session was open for a long
    time and the conductor advanced state via SSE without re-fetch.
    """
    _walk_to("OP-E002", "v4.1.0", state_machine.STATE_STAGING)
    row = state_machine.get_by_release_id(release_id="OP-E002")
    resp = operator_client.post(
        "/api/v1/release-conductor/approvals/approve",
        json={"release_id": "OP-E002", "row_version": row["row_version"] + 99},
    )
    assert resp.status_code == 409
    assert "RaceConditionApproval" in resp.json()["detail"]


# ─── Case 6 — backend dispatch failed ────────────────────────────────
def test_backend_dispatch_failed_returns_500(operator_client) -> None:
    """AC error catalog — H2 event insert fails → 500 + retry hint.

    State transition has already committed (the failure is on the
    audit-only persist_event call); the endpoint surfaces 500 with the
    operator-facing retry hint so the UI can offer a Retry button.
    """
    _walk_to("OP-F001", "v5.0.0", state_machine.STATE_STAGING)
    row = state_machine.get_by_release_id(release_id="OP-F001")
    rv = row["row_version"]

    with patch.object(
        release_approval.event_router,
        "persist_event",
        side_effect=event_router.EventInsertFailed("simulated DB down"),
    ):
        resp = operator_client.post(
            "/api/v1/release-conductor/approvals/approve",
            json={"release_id": "OP-F001", "row_version": rv},
        )
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "BackendDispatchFailed" in detail
    assert "retry" in detail.lower()

    # State transition was applied even though the audit row failed;
    # the UI must reflect via re-fetch on retry.
    after = state_machine.get_by_release_id(release_id="OP-F001")
    assert after["state"] == state_machine.STATE_CANARY_5


# ─── Misc — release not found ────────────────────────────────────────
def test_approve_unknown_release_404(operator_client) -> None:
    resp = operator_client.post(
        "/api/v1/release-conductor/approvals/approve",
        json={"release_id": "OP-NOPE", "row_version": 0},
    )
    assert resp.status_code == 404


def test_approve_only_valid_from_staging(operator_client) -> None:
    """Approve must refuse if the row is not in pending_approval.

    Mirrors the race where SSE pushed the row to canary_5 between the
    UI's last refresh and the click; the operator's intent ("approve")
    is no longer meaningful and we return 409 rather than silently
    accepting + double-transitioning.
    """
    _walk_to("OP-G001", "v6.0.0", state_machine.STATE_CANARY_5)
    row = state_machine.get_by_release_id(release_id="OP-G001")
    resp = operator_client.post(
        "/api/v1/release-conductor/approvals/approve",
        json={"release_id": "OP-G001", "row_version": row["row_version"]},
    )
    assert resp.status_code == 409
    assert "RaceConditionApproval" in resp.json()["detail"]
