"""BS.1.1 — alembic 0051 catalog_tables migration contract.

Locks the load-bearing properties of the ``catalog_entries`` /
``install_jobs`` / ``catalog_subscriptions`` schema introduced for
the BS Bootstrap Vertical-Aware Setup epic
(``docs/design/bs-bootstrap-vertical-aware.md`` §3 + §4 + §7.1).

Test groups
───────────

1.  **Structural** — the migration file must keep its dialect branch,
    revision id, and the load-bearing CHECK / UNIQUE / FK clauses.
    Catches accidental refactors that drop a constraint without
    realising it.

2.  **Functional (SQLite branch)** — bring the migration's SQLite
    branch up against a fresh in-memory DB and exercise the
    constraints that the BS.2 router + BS.1.5 drift guard will rely
    on.  The SQLite branch is what dev runs / what the no-PG CI
    matrix runs, so it must be a faithful subset of the PG branch's
    invariants (every CHECK / UNIQUE / FK is mirrored — only the
    *types* differ: TEXT-of-JSON for JSONB, INTEGER 0/1 for BOOLEAN,
    REAL unix-epoch for TIMESTAMPTZ).

3.  **Symmetry** — ``upgrade()`` then ``downgrade()`` must leave the
    DB without any of the three new tables (``DROP TABLE IF EXISTS``).

Why we do NOT drive ``alembic upgrade head`` here
─────────────────────────────────────────────────

A pre-existing 0016 issue (``CREATE INDEX`` on the not-yet-added
``episodic_memory.last_used_at`` column on SQLite — see the
``test_alembic_0017_sqlite_noop`` docstring) makes a fresh
``alembic upgrade head`` against vanilla SQLite fail mid-chain.  In
production ``backend/db.py::_migrate()`` adds the column before
alembic runs, so this never surfaces; the live PG upgrade is
verified out-of-band by ``test_alembic_pg_live_upgrade`` (gated on
``OMNI_TEST_PG_URL``).  Here we exercise our migration in
isolation against a hand-bootstrapped SQLite DB, which is enough
to lock the invariants without depending on the rest of the chain.

Note on PG dialect-branch coverage
──────────────────────────────────

The PG branch's actual DDL (JSONB / TIMESTAMPTZ / BOOLEAN / GIN
index) is exercised by the live PG upgrade test.  Here we only
assert that the PG branch *executes* (issues at least one
``exec_driver_sql`` call) when ``conn.dialect.name == 'postgresql'``
— we don't try to reproduce a PG semantic check via mocks because
``MagicMock`` doesn't enforce SQL semantics.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    BACKEND_ROOT / "alembic" / "versions" / "0051_catalog_tables.py"
)


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_PATH.read_text()

    def test_revision_id_is_0051(self, source: str) -> None:
        assert 'revision = "0051"' in source

    def test_down_revision_is_0039(self, source: str) -> None:
        # Recorded with explicit reference: when Y migrations 0040-0050
        # land, this revision's down_revision is retargeted to the
        # last Y rev. The CI alembic-graph guard catches a double-head.
        assert 'down_revision = "0039"' in source, (
            "0051's down_revision must be '0039' until Y migrations "
            "0040-0050 land. See the 'Revision chain note' section in "
            "the migration docstring for the chain re-stitch playbook."
        )

    def test_dialect_branch_present(self, source: str) -> None:
        # Dialect branch is the only way the SQLite + PG variants stay
        # honest (JSONB / TIMESTAMPTZ are PG-only). Catches a refactor
        # that collapses the branch into a single PG-flavor block.
        assert 'dialect == "postgresql"' in source
        # The 'else' branch is the SQLite path.
        assert "else:" in source

    def test_three_tables_named(self, source: str) -> None:
        # Each table name appears in CREATE TABLE statements on BOTH
        # branches (so 6 occurrences of "CREATE TABLE IF NOT EXISTS
        # <name>" total). We assert presence of each name at least
        # twice — once per branch.
        for name in ("catalog_entries", "install_jobs", "catalog_subscriptions"):
            occurrences = source.count(f"CREATE TABLE IF NOT EXISTS {name}")
            assert occurrences == 2, (
                f"{name} must appear in both PG and SQLite branches; "
                f"found {occurrences} CREATE TABLE statements"
            )

    def test_partial_unique_present_both_branches(
        self, source: str,
    ) -> None:
        # Load-bearing uniqueness: at most one live row per
        # (id, source, tenant_id) — a hidden=true tombstone is allowed
        # to coexist. The partial UNIQUE is the canonical enforcement.
        assert source.count(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_entries_visible"
        ) == 2, (
            "Partial UNIQUE on (id, source, COALESCE(tenant_id,'')) "
            "WHERE hidden=false/0 must exist in BOTH dialect branches"
        )
        # The COALESCE expression is critical: NULL tenant_id (shipped
        # rows) must collide with the empty-string sentinel so two
        # shipped rows for the same id/source can't coexist.
        assert source.count("COALESCE(tenant_id, '')") >= 2

    def test_source_check_reserves_subscription(
        self, source: str,
    ) -> None:
        # ADR §3.4 forward-compat: the 'subscription' source layer is
        # reserved at schema time so a future feed-imported row type
        # doesn't need a destructive CHECK migration. Asserts the
        # CHECK appears in BOTH dialect branches (the docstring also
        # quotes the enum, so we accept >= 2 rather than exactly 2).
        assert source.count(
            "source IN ('shipped','operator','override','subscription')"
        ) >= 2

    def test_install_method_check_lists_four_methods(
        self, source: str,
    ) -> None:
        # ADR §4 sidecar protocol: install method enum is closed.
        # New methods need a migration + a sidecar method module.
        assert source.count(
            "install_method IN ('noop','docker_pull',\n"
            "                              'shell_script','vendor_installer')"
        ) == 2 or source.count(
            "install_method IN ('noop','docker_pull',"
        ) == 2

    def test_install_jobs_state_check(self, source: str) -> None:
        # ADR §4.2 state machine: queued -> running -> {completed,
        # failed, cancelled}. Any new state must be threaded through
        # the sidecar protocol (R26 — protocol_version bump may be
        # warranted).
        assert source.count(
            "state IN ('queued','running','completed','failed','cancelled')"
        ) == 2

    def test_idempotency_key_is_unique(self, source: str) -> None:
        # ADR §4.4: ``POST /installer/jobs`` uses ON CONFLICT
        # (idempotency_key) DO NOTHING for double-click protection.
        assert source.count("UNIQUE (idempotency_key)") == 2

    def test_tenant_id_xor_source_check(self, source: str) -> None:
        # ADR §3.1: shipped rows MUST have NULL tenant_id (no scope);
        # operator/override/subscription rows MUST have a non-NULL
        # tenant_id (per-tenant scope). This is the load-bearing
        # data-model invariant — losing the CHECK lets a 'shipped'
        # row leak into a tenant scope.
        assert source.count("source = 'shipped'  AND tenant_id IS NULL") == 2

    def test_fk_to_tenants_on_delete_cascade(self, source: str) -> None:
        # When a tenant is hard-deleted every catalog_entries
        # (operator/override/subscription rows), every install_job,
        # and every catalog_subscriptions row tied to that tenant
        # must go too — otherwise residual rows leak into the
        # next-tenant-onto-the-same-id scenario.
        assert source.count(
            "REFERENCES tenants(id) ON DELETE CASCADE"
        ) >= 6  # 3 tables × 2 dialect branches


# ─── Group 2: functional SQLite-branch behaviour ──────────────────────────


def _load_migration_with_fake_alembic(conn: sqlite3.Connection) -> Any:
    """Load 0051 with a stub ``alembic`` module that exposes ``op``
    backed by the given SQLite connection.

    The migration calls ``from alembic import op`` at import time;
    we install a fake module before exec_module so the import
    resolves to our stub. Using ``importlib.util.spec_from_file_location``
    keeps the migration file out of sys.path (alembic itself never
    adds versions/ to sys.path either).
    """

    class _Bind:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._c = c

            class D:
                name = "sqlite"
            self.dialect = D()

        def exec_driver_sql(self, sql: str, *_a: Any, **_k: Any) -> None:
            self._c.executescript(sql)

    class _Op:
        def __init__(self, bind: _Bind) -> None:
            self._bind = bind

        def get_bind(self) -> _Bind:
            return self._bind

        def execute(self, sql: str) -> None:
            self._bind._c.executescript(sql)

    bind = _Bind(conn)
    fake_op = _Op(bind)
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = fake_op  # type: ignore[attr-defined]
    sys.modules["alembic"] = fake_alembic

    spec = importlib.util.spec_from_file_location(
        "_bs11_test_0051", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def upgraded_db():
    """Fresh in-memory SQLite with parent tables (tenants/users) +
    BS.1.1's three new tables created via the migration's SQLite
    branch. PRAGMA foreign_keys=ON so FK CHECKs actually fire."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE tenants (id TEXT PRIMARY KEY);
        CREATE TABLE users (id TEXT PRIMARY KEY);
        INSERT INTO tenants(id) VALUES ('t-default'), ('t-alpha');
        INSERT INTO users(id) VALUES ('u-1'), ('u-2');
        """
    )
    conn.commit()
    mod = _load_migration_with_fake_alembic(conn)
    mod.upgrade()
    conn.commit()
    yield conn, mod
    conn.close()


class TestSqliteUpgradeCreatesTables:
    def test_three_tables_created(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('catalog_entries','install_jobs',"
            "'catalog_subscriptions') ORDER BY name"
        ).fetchall()
        assert [r[0] for r in rows] == sorted(
            ["catalog_entries", "install_jobs", "catalog_subscriptions"]
        )

    def test_partial_unique_index_exists(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='uq_catalog_entries_visible'"
        ).fetchall()
        assert rows, "uq_catalog_entries_visible partial UNIQUE missing"

    def test_state_index_partial(self, upgraded_db) -> None:
        # BS.4 sidecar's poll path SELECTs WHERE state IN ('queued',
        # 'running') FOR UPDATE SKIP LOCKED. Without the partial
        # index the planner full-scans on PG; on SQLite the index
        # exists for parity (the migrator drift guard checks that
        # SQLite + PG schemas align).
        conn, _ = upgraded_db
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_install_jobs_state_queued'"
        ).fetchall()
        assert rows
        assert "WHERE state IN" in rows[0][0]


class TestSqliteCatalogEntriesConstraints:
    """Exercise the load-bearing CHECK / UNIQUE constraints. Each
    test claims one invariant — when a future migration (or an
    accidental edit) loosens the schema, the corresponding test
    flips red and the reviewer has to acknowledge the change."""

    def test_shipped_row_inserts(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        conn.execute(
            "INSERT INTO catalog_entries"
            "(id, source, vendor, family, display_name, version, install_method) "
            "VALUES ('nxp-mcu', 'shipped', 'NXP', 'embedded', 'NXP', '1.0', 'docker_pull')"
        )
        conn.commit()
        rows = conn.execute("SELECT id, source, hidden FROM catalog_entries").fetchall()
        assert rows == [("nxp-mcu", "shipped", 0)]

    def test_duplicate_live_shipped_row_blocked(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        conn.execute(
            "INSERT INTO catalog_entries"
            "(id, source, vendor, family, display_name, version, install_method) "
            "VALUES ('nxp-mcu', 'shipped', 'NXP', 'embedded', 'NXP', '1.0', 'docker_pull')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO catalog_entries"
                "(id, source, vendor, family, display_name, version, install_method) "
                "VALUES ('nxp-mcu', 'shipped', 'NXP', 'embedded', 'NXP', '1.1', 'docker_pull')"
            )
            conn.commit()
        conn.rollback()

    def test_hidden_row_does_not_block_replacement(self, upgraded_db) -> None:
        # Soft-retire the live row by flipping hidden=1; a new live row
        # with the same (id, source, tenant_id) tuple now inserts
        # without conflict. This is the soft-delete pattern BS.2's
        # PATCH /catalog/entries/{id}?hide=true relies on.
        conn, _ = upgraded_db
        conn.execute(
            "INSERT INTO catalog_entries"
            "(id, source, vendor, family, display_name, version, install_method) "
            "VALUES ('nxp-mcu', 'shipped', 'NXP', 'embedded', 'NXP', '1.0', 'docker_pull')"
        )
        conn.execute(
            "UPDATE catalog_entries SET hidden=1 WHERE id='nxp-mcu'"
        )
        conn.execute(
            "INSERT INTO catalog_entries"
            "(id, source, vendor, family, display_name, version, install_method) "
            "VALUES ('nxp-mcu', 'shipped', 'NXP', 'embedded', 'NXP', '2.0', 'docker_pull')"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT version, hidden FROM catalog_entries ORDER BY version"
        ).fetchall()
        assert rows == [("1.0", 1), ("2.0", 0)]

    def test_shipped_with_tenant_id_blocked_by_check(self, upgraded_db) -> None:
        # ADR §3.1: 'shipped' rows are global (NULL tenant_id). A row
        # claiming source='shipped' AND tenant_id='t-alpha' would leak
        # a tenant-scoped row into the global pool — schema CHECK
        # rejects it.
        conn, _ = upgraded_db
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO catalog_entries"
                "(id, source, tenant_id, vendor, family, display_name, version, install_method) "
                "VALUES ('w', 'shipped', 't-alpha', 'V', 'embedded', 'X', '1', 'noop')"
            )
            conn.commit()
        conn.rollback()

    def test_operator_without_tenant_blocked_by_check(self, upgraded_db) -> None:
        # Mirror invariant: 'operator' rows are tenant-scoped (NOT NULL
        # tenant_id). A NULL tenant_id on an 'operator' row would make
        # the row visible cross-tenant, which is the bug ADR §3.1 is
        # explicitly preventing.
        conn, _ = upgraded_db
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO catalog_entries"
                "(id, source, vendor, family, display_name, version, install_method) "
                "VALUES ('c', 'operator', 'O', 'embedded', 'C', '1', 'noop')"
            )
            conn.commit()
        conn.rollback()

    def test_invalid_family_blocked(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO catalog_entries"
                "(id, source, vendor, family, display_name, version, install_method) "
                "VALUES ('x', 'shipped', 'V', 'quantum', 'X', '1', 'noop')"
            )
            conn.commit()
        conn.rollback()

    def test_invalid_install_method_blocked(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO catalog_entries"
                "(id, source, vendor, family, display_name, version, install_method) "
                "VALUES ('x', 'shipped', 'V', 'embedded', 'X', '1', 'rpm_install')"
            )
            conn.commit()
        conn.rollback()

    def test_invalid_source_blocked(self, upgraded_db) -> None:
        # 'subscription' is reserved (CHECK accepts it); a typo like
        # 'subscriptions' (plural) gets caught.
        conn, _ = upgraded_db
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO catalog_entries"
                "(id, source, tenant_id, vendor, family, display_name, version, install_method) "
                "VALUES ('x', 'subscriptions', 't-alpha', 'V', 'embedded', 'X', '1', 'noop')"
            )
            conn.commit()
        conn.rollback()

    def test_subscription_source_reserved(self, upgraded_db) -> None:
        # Forward-compat (R24): 'subscription' is reserved at schema
        # time even though no migration writes a row of that source
        # yet. The CHECK accepts it; the row is per-tenant scoped
        # (see test_operator_without_tenant_blocked_by_check sibling).
        conn, _ = upgraded_db
        conn.execute(
            "INSERT INTO catalog_entries"
            "(id, source, tenant_id, vendor, family, display_name, version, install_method) "
            "VALUES ('s1', 'subscription', 't-alpha', 'V', 'embedded', 'X', '1', 'noop')"
        )
        conn.commit()


class TestSqliteInstallJobsConstraints:
    def test_basic_insert(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        conn.execute(
            "INSERT INTO install_jobs(id, tenant_id, entry_id, idempotency_key) "
            "VALUES ('ij-1', 't-default', 'nxp-mcu', 'idk-1')"
        )
        conn.commit()
        # state defaults to 'queued', protocol_version to 1.
        row = conn.execute(
            "SELECT state, protocol_version, bytes_done FROM install_jobs"
        ).fetchone()
        assert row == ("queued", 1, 0)

    def test_idempotency_key_unique(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        conn.execute(
            "INSERT INTO install_jobs(id, tenant_id, entry_id, idempotency_key) "
            "VALUES ('ij-1', 't-default', 'nxp-mcu', 'idk-1')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            # Different job id, same idempotency_key — must collide.
            conn.execute(
                "INSERT INTO install_jobs(id, tenant_id, entry_id, idempotency_key) "
                "VALUES ('ij-2', 't-default', 'nxp-mcu', 'idk-1')"
            )
            conn.commit()
        conn.rollback()

    def test_invalid_state_blocked(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO install_jobs(id, tenant_id, entry_id, idempotency_key, state) "
                "VALUES ('ij-9', 't-default', 'nxp-mcu', 'idk-9', 'frozen')"
            )
            conn.commit()
        conn.rollback()

    def test_tenant_fk_enforced(self, upgraded_db) -> None:
        # FK to tenants(id) — a job for a non-existent tenant is
        # rejected. Without the FK, deleting a tenant would orphan
        # in-flight install jobs that the sidecar would then try to
        # process for a tenant context that no longer exists.
        conn, _ = upgraded_db
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO install_jobs(id, tenant_id, entry_id, idempotency_key) "
                "VALUES ('ij-9', 't-doesnotexist', 'nxp-mcu', 'idk-9')"
            )
            conn.commit()
        conn.rollback()


class TestSqliteCatalogSubscriptionsConstraints:
    def test_basic_insert(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        conn.execute(
            "INSERT INTO catalog_subscriptions(id, tenant_id, feed_url) "
            "VALUES ('sub-1', 't-alpha', 'https://feed.example/c.json')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT auth_method, refresh_interval_s, enabled "
            "FROM catalog_subscriptions"
        ).fetchone()
        # Defaults: auth_method='none', 24h refresh, enabled=1.
        assert row == ("none", 86400, 1)

    def test_unique_per_tenant_feed_pair(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        conn.execute(
            "INSERT INTO catalog_subscriptions(id, tenant_id, feed_url) "
            "VALUES ('sub-1', 't-alpha', 'https://feed.example/c.json')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            # Same tenant + same feed_url — collide on UNIQUE.
            conn.execute(
                "INSERT INTO catalog_subscriptions(id, tenant_id, feed_url) "
                "VALUES ('sub-2', 't-alpha', 'https://feed.example/c.json')"
            )
            conn.commit()
        conn.rollback()

    def test_invalid_auth_method_blocked(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO catalog_subscriptions"
                "(id, tenant_id, feed_url, auth_method) "
                "VALUES ('sub-3', 't-alpha', 'https://b.example/c.json', 'oauth2')"
            )
            conn.commit()
        conn.rollback()

    def test_different_feed_url_same_tenant_ok(self, upgraded_db) -> None:
        conn, _ = upgraded_db
        conn.execute(
            "INSERT INTO catalog_subscriptions(id, tenant_id, feed_url) "
            "VALUES ('sub-1', 't-alpha', 'https://feed.example/a.json')"
        )
        conn.execute(
            "INSERT INTO catalog_subscriptions(id, tenant_id, feed_url) "
            "VALUES ('sub-2', 't-alpha', 'https://feed.example/b.json')"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT id FROM catalog_subscriptions ORDER BY id"
        ).fetchall()
        assert [r[0] for r in rows] == ["sub-1", "sub-2"]


# ─── Group 3: symmetry — upgrade + downgrade leaves no trace ──────────────


class TestSymmetry:
    def test_downgrade_drops_three_tables(self, upgraded_db) -> None:
        conn, mod = upgraded_db
        mod.downgrade()
        conn.commit()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('catalog_entries','install_jobs',"
            "'catalog_subscriptions')"
        ).fetchall()
        assert rows == [], (
            "downgrade() must drop all three tables — leaving any of "
            "them behind breaks alembic-graph dual-track validation"
        )

    def test_double_upgrade_idempotent(self, upgraded_db) -> None:
        # CREATE TABLE IF NOT EXISTS must let upgrade() run twice
        # without raising. Useful for the alembic dual-track validator
        # that runs upgrade -> downgrade -> upgrade in a single pass.
        conn, mod = upgraded_db
        mod.upgrade()  # already-upgraded -> must be a no-op, not raise
        conn.commit()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='catalog_entries'"
        ).fetchall()
        assert rows


# ─── Group 4: PG branch sanity (mock-based) ───────────────────────────────


class TestPgBranchExecutes:
    """Mock-driven smoke that the PG branch issues DDL when the
    dialect reports postgresql. The actual DDL semantics are
    covered by ``test_alembic_pg_live_upgrade.py`` (gated on
    ``OMNI_TEST_PG_URL``)."""

    def test_pg_branch_issues_at_least_three_create_tables(self) -> None:
        bind = MagicMock()
        bind.dialect.name = "postgresql"

        fake_alembic = types.ModuleType("alembic")

        class _Op:
            def get_bind(self) -> Any:
                return bind

            def execute(self, *_a: Any, **_k: Any) -> None:
                # Used only by downgrade(); upgrade() goes through
                # exec_driver_sql.
                pass

        fake_alembic.op = _Op()  # type: ignore[attr-defined]
        sys.modules["alembic"] = fake_alembic

        spec = importlib.util.spec_from_file_location(
            "_bs11_pg_test_0051", MIGRATION_PATH,
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.upgrade()

        ddl_calls = [
            call.args[0] for call in bind.exec_driver_sql.call_args_list
        ]
        # Three CREATE TABLE plus several CREATE INDEX statements.
        create_table_count = sum(
            1 for sql in ddl_calls if "CREATE TABLE" in sql
        )
        assert create_table_count == 3, (
            f"PG branch should issue exactly 3 CREATE TABLE statements; "
            f"got {create_table_count}"
        )
        # JSONB / TIMESTAMPTZ / BOOLEAN are PG-only types — they MUST
        # show up in the issued DDL (else the branch collapsed into
        # SQLite-flavor and we'd lose proper indexing on PG).
        joined = "\n".join(ddl_calls)
        assert "JSONB" in joined
        assert "TIMESTAMPTZ" in joined
        assert "BOOLEAN" in joined
        # GIN index on metadata is the read-path enabler for
        # ``WHERE metadata @> '{"vendor":"NXP"}'`` admin filter.
        assert "USING GIN (metadata)" in joined
