"""OP-2567 U4-B — publisher actor-boundary tests (offline).

``insert_publication_event`` runs against the stacked sqlite harness
(0258 + 0259 applied). Choice pinned per the ticket build notes: the
module speaks asyncpg-style (``await conn.execute(sql, *params)`` with
``$N`` placeholders), so the TEST supplies a thin adapter conn that
rewrites ``$N`` → ``?`` positionally (the SQL uses each ``$N`` once in
ascending order, so a plain positional rewrite is exact) — the module
itself stays asyncpg-pure. ``acquire_publish_lock`` is asserted via a
RECORDING fake conn (the real advisory lock is PG-only).
"""
from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path

import pytest

from backend.learned_item_publisher import (
    acquire_publish_lock,
    insert_publication_event,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0258 = (
    BACKEND_ROOT / "alembic" / "versions" / "0258_u4_learned_item_ledger.py"
)
MIGRATION_0259 = (
    BACKEND_ROOT
    / "alembic"
    / "versions"
    / "0259_u4_publication_gate_trigger.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# Fresh sys.modules names — "_alembic_test_0258" is A1's and
# "_alembic_test_0258_for_0259"/"_alembic_test_0259" belong to the
# gate-migration test module.
@pytest.fixture(scope="module")
def m0258():
    return _load_module(MIGRATION_0258, "_alembic_test_0258_for_publisher")


@pytest.fixture(scope="module")
def m0259():
    return _load_module(MIGRATION_0259, "_alembic_test_0259_for_publisher")


@pytest.fixture()
def conn(m0258, m0259):
    """Stacked 0258+0259 in-memory sqlite with an Operations proxy."""
    import sqlalchemy as sa
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    connection = engine.connect()
    ctx = MigrationContext.configure(connection=connection)
    with Operations.context(ctx):
        m0258.upgrade()
        m0259.upgrade()
        yield connection
    connection.close()


class _SqliteAdapterConn:
    """asyncpg-shaped facade over a sync sqlalchemy sqlite connection.

    Rewrites ``$N`` → ``?`` — valid here because the module's SQL uses
    each placeholder exactly once in ascending order (see module
    docstring for the pinned choice)."""

    def __init__(self, connection) -> None:
        self._connection = connection

    async def execute(self, sql: str, *params):
        return self._connection.exec_driver_sql(
            re.sub(r"\$\d+", "?", sql), tuple(params)
        )


class _RecordingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *params):
        self.calls.append((sql, params))


class _ExplodingConn:
    """Sentinel: any DB touch is a test failure."""

    async def execute(self, sql: str, *params):
        raise AssertionError(
            "insert_publication_event touched the DB before validation"
        )


def _promote_approval(conn, version_id: str) -> str:
    rid = uuid.uuid4().hex
    conn.exec_driver_sql(
        "INSERT INTO memory_eval_runs (id, version_id, decision) "
        "VALUES (?, ?, 'promote')",
        (rid, version_id),
    )
    aid = uuid.uuid4().hex
    conn.exec_driver_sql(
        "INSERT INTO memory_approvals "
        "(id, version_id, eval_run_id, live_set_hash, approved_by) "
        "VALUES (?, ?, ?, ?, 'human-reviewer')",
        (aid, version_id, rid, "0" * 64),
    )
    return aid


# ━━ insert_publication_event ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInsertPublicationEvent:
    async def test_happy_path_writes_row(self, conn) -> None:
        aid = _promote_approval(conn, "v-1")
        eid = uuid.uuid4().hex
        await insert_publication_event(
            _SqliteAdapterConn(conn),
            event_id=eid,
            version_id="v-1",
            state="published",
            approval_id=aid,
        )
        row = conn.exec_driver_sql(
            "SELECT version_id, approval_id, state, event_seq "
            "FROM memory_publications WHERE id=?",
            (eid,),
        ).fetchone()
        assert row[0] == "v-1"
        assert row[1] == aid
        assert row[2] == "published"
        # event_seq: plain nullable BIGINT on sqlite — no value
        # assertion (PG identity assigns there; also unasserted).

    async def test_ungated_state_writes_without_approval(self, conn) -> None:
        eid = uuid.uuid4().hex
        await insert_publication_event(
            _SqliteAdapterConn(conn),
            event_id=eid,
            version_id="v-1",
            state="approved",
        )
        count = conn.exec_driver_sql(
            "SELECT count(*) FROM memory_publications WHERE id=?", (eid,)
        ).scalar()
        assert count == 1

    async def test_bad_state_raises_before_any_db_call(self) -> None:
        with pytest.raises(ValueError, match="not one of"):
            await insert_publication_event(
                _ExplodingConn(),
                event_id="e-1",
                version_id="v-1",
                state="promoted",  # not a frozen state
            )

    async def test_gate_trigger_surfaces_through_writer(self, conn) -> None:
        with pytest.raises(Exception, match="PublicationGate"):
            await insert_publication_event(
                _SqliteAdapterConn(conn),
                event_id=uuid.uuid4().hex,
                version_id="v-1",
                state="published",  # no approval_id
            )


# ━━ acquire_publish_lock ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAcquirePublishLock:
    async def test_emits_exact_advisory_lock_sql(self) -> None:
        recorder = _RecordingConn()
        await acquire_publish_lock(recorder, "tenant:t-1")
        assert recorder.calls == [
            (
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                ("tenant:t-1",),
            )
        ]

    async def test_real_lock_on_pg(self, pg_test_pool) -> None:
        async with pg_test_pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                await acquire_publish_lock(conn, "global:-")
            finally:
                await tx.rollback()


# ━━ Dormant-ship guard (A2's rglob pattern, verbatim) ━━━━━━━━━━━━━━━━


class TestDormantShip:
    def test_no_nontest_module_imports_the_new_modules(self) -> None:
        # A2's rglob pattern copied verbatim incl. the alembic/versions
        # exclusion — 0259's function name learned_item_publication_gate
        # contains the substring "learned_item_publication", so omitting
        # that exclusion would self-trip on our own migration.
        own = {"learned_item_publication.py", "learned_item_publisher.py"}
        offenders: list[str] = []
        for py in BACKEND_ROOT.rglob("*.py"):
            rel = py.relative_to(BACKEND_ROOT)
            parts = rel.parts
            if parts[0] in ("tests", "node_modules") or (
                parts[:2] == ("alembic", "versions")
            ):
                continue
            if len(parts) == 1 and parts[0] in own:
                continue
            text = py.read_text(errors="ignore")
            if "learned_item_publisher" in text or (
                "learned_item_publication" in text
            ):
                offenders.append(str(rel))
        assert offenders == [], f"dormant-ship violated by: {offenders}"
