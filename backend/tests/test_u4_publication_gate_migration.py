"""OP-2567 U4-B — publication-gate trigger (migration 0259) tests.

0249/0250-style importlib module-load harness on in-memory SQLite
``Operations`` (NOT the alembic CLI) — the exact precedent is
``test_alembic_0250_bi0b_retention.py``, which stacks 0249+0250 on one
fixture connection; here 0258 (the ledger tables) and 0259 (the gate
trigger) stack the same way.

SQLite ``PRAGMA foreign_keys`` is OFF by default, so the approval /
eval-run fixture rows use synthetic uuids — deliberately NO
FK-enforcement tests here (A1 lesson: they would silently never fire).

NOTE on downgrade assertions: on this stacked fixture 0258 stays
upgraded, so its append-only triggers legitimately remain after 0259's
downgrade — asserted is ONLY that the gate trigger is gone.

PG assertions ride ``pg_test_pool`` (skipped at fixture setup when
``OMNI_TEST_PG_URL`` is unset — no skip call sites added here).
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest


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

GATE_TRIGGER = "trg_memory_publications_insert_gate"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# A1's test already owns "_alembic_test_0258" — fresh names here.
@pytest.fixture(scope="module")
def m0258():
    return _load_module(MIGRATION_0258, "_alembic_test_0258_for_0259")


@pytest.fixture(scope="module")
def m0259():
    return _load_module(MIGRATION_0259, "_alembic_test_0259")


# ━━ Structural guards ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0259.read_text()

    def test_revision_id_is_0259(self, source: str) -> None:
        assert 'revision = "0259"' in source

    def test_down_revision_is_0258(self, source: str) -> None:
        assert 'down_revision = "0258"' in source

    def test_revision_lexically_before_down_revision(
        self, source: str
    ) -> None:
        # The merged filename-convention test regex-matches the FIRST
        # `revision = "NNNN"` in the file.
        assert source.index('revision = "0259"') < source.index(
            'down_revision = "0258"'
        )

    def test_backwards_compat_marked_safe(self, source: str) -> None:
        assert "Backwards-compat: safe" in source

    def test_new_function_not_0258s(self, source: str) -> None:
        # A NEW plpgsql function — 0258's unconditional-RAISE function
        # is dropped by 0258's own downgrade and has no success path.
        assert "learned_item_publication_gate" in source
        assert "learned_item_ledger_block_mutation" not in source

    def test_pg_function_returns_new(self, source: str) -> None:
        # BEFORE INSERT plpgsql: falling off the end is a runtime error
        # on every insert; RETURN NULL silently drops rows.
        assert "RETURN NEW;" in source
        assert "RETURN NULL" not in source

    def test_pg_condition_inside_function_not_when_clause(
        self, source: str
    ) -> None:
        # PG forbids subqueries in a trigger WHEN clause — the PG
        # CREATE TRIGGER statement must carry no WHEN.
        import re

        pg_trigger = re.search(
            r"CREATE TRIGGER trg_memory_publications_insert_gate.*?"
            r"learned_item_publication_gate\(\)",
            source,
            re.S,
        )
        assert pg_trigger is not None
        assert "WHEN" not in pg_trigger.group(0)


# ━━ SQLite harness (0249/0250 precedent — no alembic CLI) ━━━━━━━━━━━━


@pytest.fixture()
def conn():
    """Empty in-memory SQLite connection with an Operations proxy."""
    import sqlalchemy as sa
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    connection = engine.connect()
    ctx = MigrationContext.configure(connection=connection)
    with Operations.context(ctx):
        yield connection
    connection.close()


@pytest.fixture()
def upgraded(conn, m0258, m0259):
    """Connection with 0258 THEN 0259 applied (stacked-fixture idiom)."""
    m0258.upgrade()
    m0259.upgrade()
    return conn


def _trigger_names(conn) -> set[str]:
    return {
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }


def _insert_eval_run(conn, *, version_id: str, decision: str) -> str:
    rid = uuid.uuid4().hex
    conn.exec_driver_sql(
        "INSERT INTO memory_eval_runs (id, version_id, decision) "
        "VALUES (?, ?, ?)",
        (rid, version_id, decision),
    )
    return rid


def _insert_approval(conn, *, version_id: str, eval_run_id: str) -> str:
    aid = uuid.uuid4().hex
    conn.exec_driver_sql(
        "INSERT INTO memory_approvals "
        "(id, version_id, eval_run_id, live_set_hash, approved_by) "
        "VALUES (?, ?, ?, ?, 'human-reviewer')",
        (aid, version_id, eval_run_id, "0" * 64),
    )
    return aid


def _insert_publication(
    conn,
    *,
    version_id: str,
    state: str,
    approval_id: str | None = None,
) -> str:
    pid = uuid.uuid4().hex
    conn.exec_driver_sql(
        "INSERT INTO memory_publications (id, version_id, approval_id, "
        "state) VALUES (?, ?, ?, ?)",
        (pid, version_id, approval_id, state),
    )
    return pid


def _promote_approval(conn, version_id: str) -> str:
    run = _insert_eval_run(conn, version_id=version_id, decision="promote")
    return _insert_approval(conn, version_id=version_id, eval_run_id=run)


# (a) upgrade/downgrade clean -----------------------------------------


class TestSqliteUpgradeDowngrade:
    def test_upgrade_runs_clean(self, upgraded) -> None:
        assert GATE_TRIGGER in _trigger_names(upgraded)

    def test_upgrade_is_idempotent_on_rerun(
        self, upgraded, m0259
    ) -> None:
        m0259.upgrade()
        assert GATE_TRIGGER in _trigger_names(upgraded)

    def test_downgrade_removes_only_gate_trigger(
        self, upgraded, m0259
    ) -> None:
        m0259.downgrade()
        triggers = _trigger_names(upgraded)
        assert GATE_TRIGGER not in triggers
        # 0258 stays upgraded on this stacked fixture — A1's
        # append-only triggers must survive 0259's downgrade.
        assert "trg_memory_publications_no_update" in triggers
        assert "trg_memory_publications_no_delete" in triggers

    def test_upgrade_after_downgrade_cycles_clean(
        self, upgraded, m0259
    ) -> None:
        m0259.downgrade()
        m0259.upgrade()
        assert GATE_TRIGGER in _trigger_names(upgraded)


# (c)-(g) gate semantics ----------------------------------------------


class TestSqliteGate:
    def test_published_without_approval_raises(self, upgraded) -> None:
        with pytest.raises(Exception, match="PublicationGate"):
            _insert_publication(
                upgraded, version_id="v-1", state="published"
            )

    def test_publishing_without_approval_raises(self, upgraded) -> None:
        with pytest.raises(Exception, match="PublicationGate"):
            _insert_publication(
                upgraded, version_id="v-1", state="publishing"
            )

    def test_published_with_reject_decision_raises(self, upgraded) -> None:
        run = _insert_eval_run(upgraded, version_id="v-1", decision="reject")
        aid = _insert_approval(upgraded, version_id="v-1", eval_run_id=run)
        with pytest.raises(Exception, match="PublicationGate"):
            _insert_publication(
                upgraded, version_id="v-1", state="published", approval_id=aid
            )

    def test_approval_for_other_version_raises(self, upgraded) -> None:
        aid = _promote_approval(upgraded, "v-other")
        with pytest.raises(Exception, match="PublicationGate"):
            _insert_publication(
                upgraded, version_id="v-1", state="published", approval_id=aid
            )

    def test_promote_approval_allows_publishing_and_published(
        self, upgraded
    ) -> None:
        aid = _promote_approval(upgraded, "v-1")
        _insert_publication(
            upgraded, version_id="v-1", state="publishing", approval_id=aid
        )
        _insert_publication(
            upgraded, version_id="v-1", state="published", approval_id=aid
        )
        count = upgraded.exec_driver_sql(
            "SELECT count(*) FROM memory_publications"
        ).scalar()
        assert count == 2

    @pytest.mark.parametrize(
        "state", ("approved", "publish_failed", "revoked", "superseded")
    )
    def test_ungated_states_insert_without_approval(
        self, upgraded, state: str
    ) -> None:
        # Revocation/failure paths must never jam on this trigger.
        _insert_publication(upgraded, version_id="v-1", state=state)
        count = upgraded.exec_driver_sql(
            "SELECT count(*) FROM memory_publications WHERE state=?",
            (state,),
        ).scalar()
        assert count == 1

    def test_superseded_records_successor_in_revoke_reason(
        self, upgraded
    ) -> None:
        # Supersession encoding (frozen): informational pointer only.
        pid = uuid.uuid4().hex
        upgraded.exec_driver_sql(
            "INSERT INTO memory_publications "
            "(id, version_id, state, revoke_reason) "
            "VALUES (?, 'v-1', 'superseded', 'superseded_by:v-2')",
            (pid,),
        )
        row = upgraded.exec_driver_sql(
            "SELECT revoke_reason FROM memory_publications WHERE id=?",
            (pid,),
        ).fetchone()
        assert row[0] == "superseded_by:v-2"


# (h) A1 append-only regression ----------------------------------------


class TestSqliteAppendOnlyRegression:
    def test_update_and_delete_still_raise(self, upgraded) -> None:
        aid = _promote_approval(upgraded, "v-1")
        pid = _insert_publication(
            upgraded, version_id="v-1", state="published", approval_id=aid
        )
        with pytest.raises(Exception, match="append-only"):
            upgraded.exec_driver_sql(
                "UPDATE memory_publications SET state='revoked' WHERE id=?",
                (pid,),
            )
        with pytest.raises(Exception, match="append-only"):
            upgraded.exec_driver_sql(
                "DELETE FROM memory_publications WHERE id=?", (pid,)
            )


# ━━ Postgres (skipped when OMNI_TEST_PG_URL is unset) ━━━━━━━━━━━━━━━━


def _fresh_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars


async def _pg_promote_chain(conn) -> tuple[str, str]:
    """Real FK chain: version → eval_run(promote) → approval."""
    vid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO learned_item_versions "
        "(id, canonical_content_hash, kind, audience, payload, created_by) "
        "VALUES ($1, $2, 'lesson', 'global', '{}', 'test')",
        vid,
        _fresh_hash(),
    )
    rid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO memory_eval_runs (id, version_id, decision) "
        "VALUES ($1, $2, 'promote')",
        rid,
        vid,
    )
    aid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO memory_approvals "
        "(id, version_id, eval_run_id, live_set_hash, approved_by) "
        "VALUES ($1, $2, $3, $4, 'human-reviewer')",
        aid,
        vid,
        rid,
        _fresh_hash(),
    )
    return vid, aid


class TestPostgres:
    async def test_gate_trigger_exists(self, pg_test_pool) -> None:
        async with pg_test_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
            )
        assert GATE_TRIGGER in {r["tgname"] for r in rows}

    async def test_published_without_approval_raises(
        self, pg_test_pool
    ) -> None:
        import asyncpg

        async with pg_test_pool.acquire() as conn:
            vid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO learned_item_versions "
                "(id, canonical_content_hash, kind, audience, payload, "
                " created_by) "
                "VALUES ($1, $2, 'lesson', 'global', '{}', 'test')",
                vid,
                _fresh_hash(),
            )
            with pytest.raises(
                asyncpg.PostgresError, match="PublicationGate"
            ):
                await conn.execute(
                    "INSERT INTO memory_publications (id, version_id, "
                    "state) VALUES ($1, $2, 'published')",
                    str(uuid.uuid4()),
                    vid,
                )

    async def test_promote_approval_allows_publishing_and_published(
        self, pg_test_pool
    ) -> None:
        async with pg_test_pool.acquire() as conn:
            vid, aid = await _pg_promote_chain(conn)
            for state in ("publishing", "published"):
                await conn.execute(
                    "INSERT INTO memory_publications "
                    "(id, version_id, approval_id, state) "
                    "VALUES ($1, $2, $3, $4)",
                    str(uuid.uuid4()),
                    vid,
                    aid,
                    state,
                )
