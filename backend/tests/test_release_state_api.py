"""OP-943 G7 — ``/api/v1/release-state/pending-releases`` contract tests.

Exercises the aggregator endpoint the D17 "Pending releases" tab calls:

1. Lists every open RELEASE-* / HOTFIX-* release with version, current
   state, blocking duration, and the hotfix flag.
2. Terminal releases (``done`` / ``failed``) are excluded by default;
   ``?include_done=true`` brings them back. ``rolled_back`` rows stay
   visible (AC #6 re-entry edge).
3. An approval-pending release surfaces ``approval_pending=true`` plus
   the canary % / SLO snapshot / reason from the pending marker, and
   ``can_approve`` is true for a human caller.
4. A bot principal sees ``can_approve=false`` even on a pending row
   (mirrors the human-only gate on the H4 approve POST).
5. ``blocking_seconds`` is a non-negative integer.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth
from backend.api import release_state as release_state_api
from backend.release_conductor import state_machine


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0233 = BACKEND_ROOT / "alembic" / "versions" / "0233_release_state.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def release_engine():
    """In-memory sqlite engine with the 0233 ``release_state`` schema."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy.pool import StaticPool

    engine = sa.create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mig233 = _load_module(MIGRATION_0233, "_alembic_test_0233_for_op943")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            mig233.upgrade()
        conn.commit()

    state_machine.set_engine_for_tests(engine)
    try:
        yield engine
    finally:
        state_machine.set_engine_for_tests(None)
        engine.dispose()


class _StubUser:
    def __init__(self, id: str = "alice", email: str = "alice@example.com"):
        self.id = id
        self.email = email
        self.name = email
        self.role = "admin"
        self.enabled = True
        self.must_change_password = False
        self.tenant_id = "t-default"


def _make_client(user: _StubUser) -> TestClient:
    app = FastAPI()
    app.include_router(release_state_api.router)
    app.dependency_overrides[auth.current_user] = lambda: user
    return TestClient(app)


def _advance(release_id: str, *states: str) -> None:
    """Walk ``release_id`` through the happy-path states given."""
    chain = [state_machine.STATE_PENDING, *states]
    for frm, to in zip(chain, chain[1:]):
        state_machine.transition(
            release_id=release_id, from_state=frm, to_state=to, reason=f"->{to}"
        )


# ─── Test #1 — listing ────────────────────────────────────────────────────


def test_lists_open_releases_with_state_and_hotfix_flag(release_engine) -> None:
    state_machine.create(release_id="RELEASE-100", version="v1.0.0")
    _advance("RELEASE-100", state_machine.STATE_BUILDING, state_machine.STATE_STAGING)
    state_machine.create(release_id="HOTFIX-7", version="v0.9.4-hotfix1")

    client = _make_client(_StubUser())
    resp = client.get("/release-state/pending-releases")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = {r["release_id"]: r for r in body["releases"]}
    assert set(rows) == {"RELEASE-100", "HOTFIX-7"}
    assert rows["RELEASE-100"]["version"] == "v1.0.0"
    assert rows["RELEASE-100"]["state"] == state_machine.STATE_STAGING
    assert rows["RELEASE-100"]["is_hotfix"] is False
    assert rows["HOTFIX-7"]["is_hotfix"] is True
    assert isinstance(rows["RELEASE-100"]["blocking_seconds"], int)
    assert rows["RELEASE-100"]["blocking_seconds"] >= 0
    assert rows["RELEASE-100"]["approval_pending"] is False
    assert isinstance(body["generated_at"], str)


# ─── Test #2 — terminal filtering ─────────────────────────────────────────


def test_terminal_releases_excluded_unless_requested(release_engine) -> None:
    # done
    state_machine.create(release_id="RELEASE-DONE", version="v2.0.0")
    _advance(
        "RELEASE-DONE",
        state_machine.STATE_BUILDING,
        state_machine.STATE_STAGING,
        state_machine.STATE_CANARY_5,
        state_machine.STATE_CANARY_25,
        state_machine.STATE_CANARY_100,
        state_machine.STATE_DONE,
    )
    # failed
    state_machine.create(release_id="RELEASE-FAIL", version="v2.0.1")
    state_machine.transition(
        release_id="RELEASE-FAIL",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_FAILED,
        reason="g1-failed",
    )
    # rolled_back — NOT terminal for the dashboard (AC #6 re-entry edge)
    state_machine.create(release_id="RELEASE-RB", version="v2.0.2")
    _advance(
        "RELEASE-RB",
        state_machine.STATE_BUILDING,
        state_machine.STATE_STAGING,
        state_machine.STATE_CANARY_5,
    )
    state_machine.transition(
        release_id="RELEASE-RB",
        from_state=state_machine.STATE_CANARY_5,
        to_state=state_machine.STATE_ROLLED_BACK,
        reason="slo-breach",
    )
    # open
    state_machine.create(release_id="RELEASE-OPEN", version="v2.1.0")

    client = _make_client(_StubUser())
    default_ids = {
        r["release_id"] for r in client.get("/release-state/pending-releases").json()["releases"]
    }
    assert default_ids == {"RELEASE-RB", "RELEASE-OPEN"}

    all_ids = {
        r["release_id"]
        for r in client.get("/release-state/pending-releases?include_done=true").json()["releases"]
    }
    assert {"RELEASE-DONE", "RELEASE-FAIL"}.issubset(all_ids)


# ─── Test #3 — approval-pending surfacing + can_approve (human) ───────────


def test_approval_pending_row_surfaces_marker_and_can_approve(release_engine) -> None:
    state_machine.create(release_id="RELEASE-APPROVE", version="v3.0.0")
    _advance("RELEASE-APPROVE", state_machine.STATE_BUILDING, state_machine.STATE_STAGING)
    state_machine.request_approval(
        release_id="RELEASE-APPROVE",
        canary_percent=5,
        slo_snapshot={"error_rate": 0.001, "p95_latency_ms": 110},
        reason="staging_gate",
    )

    client = _make_client(_StubUser(email="alice@example.com"))
    rows = {
        r["release_id"]: r
        for r in client.get("/release-state/pending-releases").json()["releases"]
    }
    row = rows["RELEASE-APPROVE"]
    assert row["approval_pending"] is True
    assert row["canary_percent"] == 5
    assert row["slo_snapshot"]["p95_latency_ms"] == 110
    assert row["approval_reason"] == "staging_gate"
    assert row["approval_requested_at"]  # iso string from the pending marker
    assert row["can_approve"] is True


# ─── Test #4 — bot principal cannot approve ───────────────────────────────


@pytest.mark.parametrize(
    "principal_id,principal_email",
    [
        ("apikey:abc", "apikey:my-bot"),
        ("ci-runner", "ci-runner@example.com"),
        ("alice", "claude-bot@example.com"),
        ("alice", "merger-agent-bot@example.com"),
    ],
)
def test_bot_principal_cannot_approve(
    release_engine, principal_id: str, principal_email: str
) -> None:
    state_machine.create(release_id="RELEASE-BOT", version="v4.0.0")
    state_machine.request_approval(release_id="RELEASE-BOT", canary_percent=5)

    client = _make_client(_StubUser(id=principal_id, email=principal_email))
    body = client.get("/release-state/pending-releases").json()
    row = next(r for r in body["releases"] if r["release_id"] == "RELEASE-BOT")
    # The list itself is readable by anyone, but the 1-click approve is
    # gated to human operators — the dashboard renders the button
    # disabled.
    assert row["approval_pending"] is True
    assert row["can_approve"] is False


# ─── Test #5 — non-pending releases never expose the approval fields ──────


def test_non_pending_release_has_empty_approval_fields(release_engine) -> None:
    state_machine.create(release_id="RELEASE-NOAPP", version="v5.0.0")
    _advance("RELEASE-NOAPP", state_machine.STATE_BUILDING)

    client = _make_client(_StubUser())
    row = next(
        r
        for r in client.get("/release-state/pending-releases").json()["releases"]
        if r["release_id"] == "RELEASE-NOAPP"
    )
    assert row["approval_pending"] is False
    assert row["approval_reason"] is None
    assert row["approval_requested_at"] is None
    assert row["canary_percent"] is None
    assert row["slo_snapshot"] is None
    assert row["can_approve"] is False
