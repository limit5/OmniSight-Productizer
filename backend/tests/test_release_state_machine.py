"""OP-948 H3 — release state machine contract tests.

Covers the 8 cases named in the ticket test plan:

1. Happy state walk — pending → done end-to-end.
2. Illegal transition refused.
3. Race condition handled — stale ``row_version`` UPDATE refused.
4. History queryable via :func:`state_machine.get_history`.
5. ``transition_log_json`` is well-formed JSON (and parseable).
6. All 9 states reachable from ``pending`` (with the documented edges).
7. ``rolled_back`` is terminal except for the single re-pending edge.
8. Query API returns the latest state + full history.

Also covers the DoD synthetic v0.99-rc walk that the ticket calls out
explicitly: instantiate a fresh release, walk pending → done, assert
the transition log is reconstructable end-to-end.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth
from backend.api import release_state_query
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
def release_state_engine():
    """In-memory sqlite engine with the 0233 schema applied + injected.

    Mirrors the ``audit_engine`` fixture in ``test_deploy_audit_op779``
    so the rest of the suite reads as the canonical pattern for
    table-backed unit tests in this codebase.
    """
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy.pool import StaticPool

    # StaticPool + check_same_thread=False so the same in-memory DB is
    # visible to the FastAPI TestClient (which dispatches into a worker
    # thread) and to the test body. Without these flags the TestClient
    # opens a fresh connection and sees an empty (un-migrated) DB.
    engine = sa.create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    migration = _load_module(MIGRATION_0233, "_alembic_test_0233_for_module")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            migration.upgrade()
        conn.commit()

    state_machine.set_engine_for_tests(engine)
    try:
        yield engine
    finally:
        state_machine.set_engine_for_tests(None)
        engine.dispose()


# ─── Case 1 — happy state walk ───────────────────────────────────────
def test_happy_path_pending_to_done(release_state_engine) -> None:
    """Synthetic v0.99-rc1 release walks pending → done (DoD)."""
    state_machine.create(release_id="OP-9999", version="v0.99.0-rc1")
    walk = [
        (state_machine.STATE_PENDING, state_machine.STATE_BUILDING, "g1-template-created"),
        (state_machine.STATE_BUILDING, state_machine.STATE_STAGING, "build-green"),
        (state_machine.STATE_STAGING, state_machine.STATE_CANARY_5, "staging-smoke-passed"),
        (state_machine.STATE_CANARY_5, state_machine.STATE_CANARY_25, "5pct-stable-15min"),
        (state_machine.STATE_CANARY_25, state_machine.STATE_CANARY_100, "25pct-stable-30min"),
        (state_machine.STATE_CANARY_100, state_machine.STATE_DONE, "100pct-stable-60min"),
    ]
    for from_s, to_s, reason in walk:
        result = state_machine.transition(
            release_id="OP-9999",
            from_state=from_s,
            to_state=to_s,
            reason=reason,
        )
        assert result["state"] == to_s

    row = state_machine.get(version="v0.99.0-rc1")
    assert row["state"] == state_machine.STATE_DONE
    # +1 for the "created" log entry that create() seeds.
    assert len(row["transition_log"]) == len(walk) + 1
    # Optimistic-locking counter monotonically increments by 1 per transition.
    assert row["row_version"] == len(walk)


# ─── Case 2 — illegal transition refused ─────────────────────────────
def test_illegal_transition_refused(release_state_engine) -> None:
    state_machine.create(release_id="OP-1001", version="v1.0.0")
    # pending -> canary_5 is not a valid edge (skip building+staging).
    with pytest.raises(state_machine.IllegalStateTransition, match="not allowed"):
        state_machine.transition(
            release_id="OP-1001",
            from_state=state_machine.STATE_PENDING,
            to_state=state_machine.STATE_CANARY_5,
            reason="should-fail",
        )
    # done is terminal — no edge out, even to failed. Walk pending -> done first.
    for from_s, to_s, reason in (
        (state_machine.STATE_PENDING, state_machine.STATE_BUILDING, "r"),
        (state_machine.STATE_BUILDING, state_machine.STATE_STAGING, "r"),
        (state_machine.STATE_STAGING, state_machine.STATE_CANARY_5, "r"),
        (state_machine.STATE_CANARY_5, state_machine.STATE_CANARY_25, "r"),
        (state_machine.STATE_CANARY_25, state_machine.STATE_CANARY_100, "r"),
        (state_machine.STATE_CANARY_100, state_machine.STATE_DONE, "r"),
    ):
        state_machine.transition(
            release_id="OP-1001",
            from_state=from_s,
            to_state=to_s,
            reason=reason,
        )
    with pytest.raises(state_machine.IllegalStateTransition, match="not allowed"):
        state_machine.transition(
            release_id="OP-1001",
            from_state=state_machine.STATE_DONE,
            to_state=state_machine.STATE_FAILED,
            reason="cannot-fail-from-done",
        )
    # failed is terminal too.
    state_machine.create(release_id="OP-1002", version="v1.0.1")
    state_machine.transition(
        release_id="OP-1002",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_FAILED,
        reason="upstream-broken",
    )
    with pytest.raises(state_machine.IllegalStateTransition, match="not allowed"):
        state_machine.transition(
            release_id="OP-1002",
            from_state=state_machine.STATE_FAILED,
            to_state=state_machine.STATE_PENDING,
            reason="cannot-recover-from-failed",
        )


def test_illegal_transition_when_current_state_differs(release_state_engine) -> None:
    """``from_state`` doesn't match the row state → IllegalStateTransition."""
    state_machine.create(release_id="OP-1003", version="v1.0.2")
    state_machine.transition(
        release_id="OP-1003",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_BUILDING,
        reason="r",
    )
    # Row is now `building`. Caller claims it's still `pending`.
    with pytest.raises(state_machine.IllegalStateTransition, match="is in state"):
        state_machine.transition(
            release_id="OP-1003",
            from_state=state_machine.STATE_PENDING,
            to_state=state_machine.STATE_BUILDING,
            reason="stale-view",
        )


# ─── Case 3 — race condition handled ─────────────────────────────────
def test_race_condition_typical_path_caught_by_state_guard(release_state_engine) -> None:
    """Typical race: second writer sees the state has moved on.

    Two webhook handlers both observe the pre-transition state and
    both call ``transition``. The first one wins; the second one
    discovers via the state-guard SELECT that the row is no longer
    in the expected ``from_state`` and surfaces an
    ``IllegalStateTransition`` (NOT a silent re-do).
    """
    state_machine.create(release_id="OP-2001", version="v2.0.0")
    state_machine.transition(
        release_id="OP-2001",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_BUILDING,
        reason="writer-A",
    )
    with pytest.raises(state_machine.IllegalStateTransition):
        state_machine.transition(
            release_id="OP-2001",
            from_state=state_machine.STATE_PENDING,
            to_state=state_machine.STATE_BUILDING,
            reason="writer-B-stale-view",
        )


def test_race_condition_double_transition_row_version_collision(release_state_engine) -> None:
    """Lost-update race: row_version moves between our SELECT and UPDATE.

    Simulates the cross-transaction interleave that sqlite cannot
    naturally produce (single-writer locking) by using a sqlalchemy
    ``after_execute`` event hook to mutate ``row_version`` *inside* the
    transition's transaction, *after* the SELECT has captured the
    pre-race row_version but *before* the UPDATE WHERE clause runs.

    The UPDATE WHERE ``row_version = :prev_rv`` then matches zero rows,
    which is exactly the lost-update signature, and transition() must
    raise ``RaceConditionDoubleTransition``.
    """
    engine = release_state_engine
    state_machine.create(release_id="OP-2002", version="v2.0.1")
    state_machine.transition(
        release_id="OP-2002",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_BUILDING,
        reason="setup",
    )

    interfered = {"done": False}

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _bump_row_version_before_update(
        conn, cursor, statement, parameters, context, executemany
    ):
        # Right before our UPDATE WHERE row_version=:prev_rv fires,
        # quietly bump the row's row_version on the same in-transaction
        # connection so the WHERE clause matches zero rows. This is the
        # closest single-process analogue of the cross-process race
        # the optimistic-locking column is designed to detect.
        # By the time the hook runs, sqlalchemy has rendered the named
        # binds into the DB-API "?" form, so we identify the transition
        # UPDATE by its (release_state, multi-column SET) shape.
        if (
            "UPDATE release_state SET" in statement
            and "row_version" in statement
            and "last_transition_at" in statement
            and not interfered["done"]
        ):
            interfered["done"] = True
            cursor.execute(
                "UPDATE release_state SET row_version = 99 "
                "WHERE release_id = 'OP-2002'"
            )

    try:
        with pytest.raises(
            state_machine.RaceConditionDoubleTransition, match="race"
        ):
            state_machine.transition(
                release_id="OP-2002",
                from_state=state_machine.STATE_BUILDING,
                to_state=state_machine.STATE_STAGING,
                reason="should-lose-race",
            )
    finally:
        sa.event.remove(
            engine, "before_cursor_execute", _bump_row_version_before_update
        )

    assert interfered["done"], "the race-injection hook must have fired"


# ─── Case 4 — history queryable ──────────────────────────────────────
def test_history_queryable(release_state_engine) -> None:
    state_machine.create(release_id="OP-3001", version="v3.0.0")
    state_machine.transition(
        release_id="OP-3001",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_BUILDING,
        reason="g1",
    )
    state_machine.transition(
        release_id="OP-3001",
        from_state=state_machine.STATE_BUILDING,
        to_state=state_machine.STATE_FAILED,
        reason="build-broke",
    )
    history = state_machine.get_history(version="v3.0.0")
    assert [h["to"] for h in history] == [
        state_machine.STATE_PENDING,
        state_machine.STATE_BUILDING,
        state_machine.STATE_FAILED,
    ]
    assert [h["reason"] for h in history[1:]] == ["g1", "build-broke"]
    # Every entry has from/to/reason/at — log shape is the wire contract.
    for entry in history:
        assert set(entry.keys()) == {"from", "to", "reason", "at"}


# ─── Case 5 — log JSON valid ─────────────────────────────────────────
def test_transition_log_is_valid_jsonl(release_state_engine) -> None:
    """AC #4: column is JSON Lines — each line independently parseable."""
    engine = release_state_engine
    state_machine.create(release_id="OP-4001", version="v4.0.0")
    state_machine.transition(
        release_id="OP-4001",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_BUILDING,
        reason="r1",
    )
    state_machine.transition(
        release_id="OP-4001",
        from_state=state_machine.STATE_BUILDING,
        to_state=state_machine.STATE_STAGING,
        reason="r2",
    )
    with engine.connect() as conn:
        raw = conn.execute(
            sa.text(
                "SELECT transition_log_json FROM release_state "
                "WHERE version = 'v4.0.0'"
            )
        ).scalar_one()
    # JSONL: split on newline; each non-empty line must parse to a dict.
    lines = [ln for ln in raw.split("\n") if ln.strip()]
    assert len(lines) == 3  # genesis + 2 transitions
    parsed = [json.loads(ln) for ln in lines]
    assert all(isinstance(p, dict) for p in parsed)
    assert parsed[0]["from"] is None and parsed[0]["to"] == state_machine.STATE_PENDING
    assert parsed[1]["from"] == state_machine.STATE_PENDING
    assert parsed[1]["to"] == state_machine.STATE_BUILDING
    assert parsed[2]["from"] == state_machine.STATE_BUILDING
    assert parsed[2]["to"] == state_machine.STATE_STAGING
    # The whole blob is NOT a single JSON document (proves we wrote
    # JSONL, not a JSON array).
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


# ─── Case 6 — all 9 states reachable ─────────────────────────────────
def test_all_nine_states_reachable(release_state_engine) -> None:
    """Walk through every state at least once across multiple releases.

    Releases are cheap to create; we use 3 of them to cover the
    happy path (7 states) + failed + rolled_back.
    """
    reached: set[str] = set()

    # Happy-path release
    state_machine.create(release_id="OP-5001", version="v5.0.0")
    reached.add(state_machine.STATE_PENDING)
    for from_s, to_s in (
        (state_machine.STATE_PENDING, state_machine.STATE_BUILDING),
        (state_machine.STATE_BUILDING, state_machine.STATE_STAGING),
        (state_machine.STATE_STAGING, state_machine.STATE_CANARY_5),
        (state_machine.STATE_CANARY_5, state_machine.STATE_CANARY_25),
        (state_machine.STATE_CANARY_25, state_machine.STATE_CANARY_100),
        (state_machine.STATE_CANARY_100, state_machine.STATE_DONE),
    ):
        state_machine.transition(
            release_id="OP-5001",
            from_state=from_s,
            to_state=to_s,
            reason="walk",
        )
        reached.add(to_s)

    # Failed release
    state_machine.create(release_id="OP-5002", version="v5.0.1")
    state_machine.transition(
        release_id="OP-5002",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_FAILED,
        reason="broken",
    )
    reached.add(state_machine.STATE_FAILED)

    # Rolled-back release (must reach canary_5 first to enable rollback)
    state_machine.create(release_id="OP-5003", version="v5.0.2")
    for from_s, to_s in (
        (state_machine.STATE_PENDING, state_machine.STATE_BUILDING),
        (state_machine.STATE_BUILDING, state_machine.STATE_STAGING),
        (state_machine.STATE_STAGING, state_machine.STATE_CANARY_5),
        (state_machine.STATE_CANARY_5, state_machine.STATE_ROLLED_BACK),
    ):
        state_machine.transition(
            release_id="OP-5003",
            from_state=from_s,
            to_state=to_s,
            reason="walk-or-rollback",
        )
    reached.add(state_machine.STATE_ROLLED_BACK)

    assert reached == state_machine.STATES, (
        f"missing states: {state_machine.STATES - reached}"
    )


# ─── Case 7 — rolled_back is terminal except re-pending ──────────────
def test_rolled_back_terminal_except_re_pending(release_state_engine) -> None:
    state_machine.create(release_id="OP-6001", version="v6.0.0")
    # Walk to canary_5 then roll back.
    for from_s, to_s in (
        (state_machine.STATE_PENDING, state_machine.STATE_BUILDING),
        (state_machine.STATE_BUILDING, state_machine.STATE_STAGING),
        (state_machine.STATE_STAGING, state_machine.STATE_CANARY_5),
        (state_machine.STATE_CANARY_5, state_machine.STATE_ROLLED_BACK),
    ):
        state_machine.transition(
            release_id="OP-6001",
            from_state=from_s,
            to_state=to_s,
            reason="r",
        )
    # rolled_back -> building must refuse.
    with pytest.raises(state_machine.IllegalStateTransition, match="not allowed"):
        state_machine.transition(
            release_id="OP-6001",
            from_state=state_machine.STATE_ROLLED_BACK,
            to_state=state_machine.STATE_BUILDING,
            reason="should-fail",
        )
    # rolled_back -> done must refuse.
    with pytest.raises(state_machine.IllegalStateTransition, match="not allowed"):
        state_machine.transition(
            release_id="OP-6001",
            from_state=state_machine.STATE_ROLLED_BACK,
            to_state=state_machine.STATE_DONE,
            reason="should-fail",
        )
    # rolled_back -> pending is the *only* allowed exit.
    result = state_machine.transition(
        release_id="OP-6001",
        from_state=state_machine.STATE_ROLLED_BACK,
        to_state=state_machine.STATE_PENDING,
        reason="operator-re-instantiated",
    )
    assert result["state"] == state_machine.STATE_PENDING


# ─── Case 8 — query API returns latest state + history ───────────────
@pytest.fixture()
def query_api_client(release_state_engine):
    """FastAPI TestClient with the query router mounted + auth bypassed."""
    app = FastAPI()
    app.include_router(release_state_query.router)

    class _StubUser:
        id = "stub-user"
        role = "admin"
        email = "ops@example.test"

    app.dependency_overrides[auth.current_user] = lambda: _StubUser()
    return TestClient(app)


def test_query_api_returns_latest_state(query_api_client) -> None:
    state_machine.create(release_id="OP-7001", version="v7.0.0")
    state_machine.transition(
        release_id="OP-7001",
        from_state=state_machine.STATE_PENDING,
        to_state=state_machine.STATE_BUILDING,
        reason="g1-instantiated",
    )
    state_machine.transition(
        release_id="OP-7001",
        from_state=state_machine.STATE_BUILDING,
        to_state=state_machine.STATE_STAGING,
        reason="build-green",
    )

    resp = query_api_client.get("/release-conductor/state?version=v7.0.0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["release_id"] == "OP-7001"
    assert body["version"] == "v7.0.0"
    assert body["state"] == state_machine.STATE_STAGING
    assert body["row_version"] == 2
    history = body["history"]
    # create() seeds 1 entry, plus 2 transitions = 3 entries total.
    assert len(history) == 3
    assert history[-1]["to"] == state_machine.STATE_STAGING
    assert history[-1]["reason"] == "build-green"


def test_query_api_404_when_missing(query_api_client) -> None:
    resp = query_api_client.get("/release-conductor/state?version=v99.99.99")
    assert resp.status_code == 404


def test_query_api_400_when_version_bad(query_api_client) -> None:
    resp = query_api_client.get("/release-conductor/state?version=not-a-version")
    assert resp.status_code == 400


# ─── Bonus contract tests ────────────────────────────────────────────
def test_create_rejects_duplicate_version(release_state_engine) -> None:
    state_machine.create(release_id="OP-8001", version="v8.0.0")
    # UNIQUE constraint surfaces as IntegrityError per the docstring.
    with pytest.raises(sa.exc.IntegrityError):
        state_machine.create(release_id="OP-8002", version="v8.0.0")


def test_transition_reason_required(release_state_engine) -> None:
    state_machine.create(release_id="OP-8101", version="v8.1.0")
    with pytest.raises(ValueError, match="reason"):
        state_machine.transition(
            release_id="OP-8101",
            from_state=state_machine.STATE_PENDING,
            to_state=state_machine.STATE_BUILDING,
            reason="",
        )
    with pytest.raises(ValueError, match="reason"):
        state_machine.transition(
            release_id="OP-8101",
            from_state=state_machine.STATE_PENDING,
            to_state=state_machine.STATE_BUILDING,
            reason="   ",
        )


def test_transition_release_not_found(release_state_engine) -> None:
    with pytest.raises(state_machine.ReleaseNotFound):
        state_machine.transition(
            release_id="OP-NEVER",
            from_state=state_machine.STATE_PENDING,
            to_state=state_machine.STATE_BUILDING,
            reason="r",
        )


def test_get_by_release_id_round_trips(release_state_engine) -> None:
    state_machine.create(release_id="OP-8201", version="v8.2.0")
    row = state_machine.get_by_release_id(release_id="OP-8201")
    assert row["version"] == "v8.2.0"
    assert row["state"] == state_machine.STATE_PENDING


def test_check_constraint_rejects_unknown_state(release_state_engine) -> None:
    """The DB-side CHECK constraint backs up the Python-side guard.

    A malicious / buggy direct INSERT with a bogus state must be
    refused by the DB engine, not silently accepted.
    """
    engine = release_state_engine
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO release_state "
                    "(release_id, version, state, row_version, "
                    " last_transition_at, transition_log_json, created_at) "
                    "VALUES ('OP-X', 'v9.9.9', 'totally-bogus', 0, "
                    " CURRENT_TIMESTAMP, '[]', CURRENT_TIMESTAMP)"
                )
            )
