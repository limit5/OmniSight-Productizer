"""AS.0.2 — alembic 0056 ``tenants.auth_features`` migration contract.

Locks the load-bearing properties of the AS.0.2 column-add:

1.  **Structural** — revision ``0056`` chains onto ``0054`` (0055
    is a deliberate gap reserved by the AS migration table); the
    PG branch emits ``::jsonb`` casts and ``IF NOT EXISTS``; the
    SQLite branch is guarded by ``PRAGMA table_info``.

2.  **Functional (SQLite)** — pre-seed two existing tenants on the
    pre-0056 schema, run ``upgrade()``, then assert:
      * the ``auth_features`` column exists,
      * existing rows are explicitly ``oauth_login=false /
        turnstile_required=false / honeypot_active=false``
        (zero behavior change for prod),
      * an INSERT that omits ``auth_features`` falls through to
        the column default ``'{}'`` (safe minimum — no NOT NULL
        violation),
      * operator hand-edits to ``auth_features`` are preserved
        (the backfill only touches rows still at ``'{}'``).

3.  **Idempotency** — running ``upgrade()`` twice is a no-op:
    the SQLite branch's ``PRAGMA table_info`` guard prevents
    duplicate-column errors and the backfill UPDATE only matches
    rows at the ``'{}'`` default so the second pass touches zero
    rows.

4.  **PG dialect branch** — capture SQL via a stub bind, verify
    the ALTER + UPDATE pair fires with ``jsonb`` literals and the
    legacy backfill JSON shape (alphabetically sorted keys for
    deterministic round-trip).

The legacy-tenant 三 false 預設 + 新 tenant 預設全開 split is the
zero-behavior-change contract this AS roadmap row sells; this test
prevents future migration tweaks from quietly flipping it.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0056 = (
    BACKEND_ROOT / "alembic" / "versions" / "0056_tenants_auth_features.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0056():
    return _load_module(MIGRATION_0056, "_alembic_test_0056")


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0056.read_text()

    def test_revision_id_is_0056(self, source: str) -> None:
        assert 'revision = "0056"' in source

    def test_down_revision_is_0054(self, source: str) -> None:
        # 0055 is a deliberate gap — see migration docstring +
        # docs/design/as-auth-security-shared-library.md migration
        # table.
        assert 'down_revision = "0054"' in source

    def test_pg_branch_uses_jsonb(self, m0056) -> None:
        # Inspect the runtime-concatenated SQL strings rather than the
        # raw source so we tolerate string-continuation reflow.
        assert "::jsonb" in m0056._PG_ADD_COLUMN
        assert "jsonb NOT NULL DEFAULT" in m0056._PG_ADD_COLUMN

    def test_pg_branch_idempotent(self, m0056) -> None:
        # Belt-and-braces against operators who ALTER-added the
        # column out of band before the migration ran.
        assert "ADD COLUMN IF NOT EXISTS auth_features" in m0056._PG_ADD_COLUMN

    def test_sqlite_branch_guards_via_pragma(self, source: str) -> None:
        assert "PRAGMA table_info(tenants)" in source

    def test_legacy_default_keys_are_explicit_false(self, m0056) -> None:
        payload = json.loads(m0056._LEGACY_TENANT_AUTH_FEATURES_JSON)
        # Three knobs, all explicit-false. Schema invariant: AS.0.2
        # row's "零行為變動" promise is enforced by *explicit* false,
        # not "absent key → falsy" implicit interpretation.
        assert payload == {
            "honeypot_active": False,
            "oauth_login": False,
            "turnstile_required": False,
        }

    def test_legacy_default_does_not_seed_auth_layer(self, m0056) -> None:
        # ``auth_layer`` is reserved for AS.6 K-rest CF Access SSO;
        # absence on legacy tenants is interpreted as ``password_only``.
        payload = json.loads(m0056._LEGACY_TENANT_AUTH_FEATURES_JSON)
        assert "auth_layer" not in payload


# ─── Group 2: functional SQLite upgrade ───────────────────────────────────


def _bootstrap_pre_0056_tenants_schema(conn: sqlite3.Connection) -> None:
    """Recreate the pre-0056 ``tenants`` table shape.

    Mirrors alembic 0012 + the legacy ``backend/db.py`` _SCHEMA so
    the migration's ALTER TABLE has a realistic source schema to
    bite into.
    """
    conn.executescript(
        """
        CREATE TABLE tenants (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            plan        TEXT NOT NULL DEFAULT 'free',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            enabled     INTEGER NOT NULL DEFAULT 1
        );
        """
    )


class _StubBind:
    """Mimics enough of an alembic context bind for ``conn.exec_driver_sql``."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

        class _Dialect:
            name = "sqlite"

        self.dialect = _Dialect()

    def exec_driver_sql(self, sql: str, *args, **kwargs):
        return self._raw.execute(sql)


def _bind(monkeypatch, conn: sqlite3.Connection) -> None:
    from alembic import op as alembic_op

    bind = _StubBind(conn)
    monkeypatch.setattr(alembic_op, "get_bind", lambda: bind)


@pytest.fixture()
def upgraded_db(monkeypatch, m0056) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0056_tenants_schema(conn)
    # Two existing tenants to exercise backfill: t-default + a custom
    # tenant created via the legacy path.
    conn.execute(
        "INSERT INTO tenants (id, name, plan, enabled) VALUES "
        "('t-default', 'Default Tenant', 'free', 1)"
    )
    conn.execute(
        "INSERT INTO tenants (id, name, plan, enabled) VALUES "
        "('t-acme', 'Acme Corp', 'enterprise', 1)"
    )
    _bind(monkeypatch, conn)
    m0056.upgrade()
    return conn


class TestSqliteUpgradeAddsColumn:
    def test_auth_features_column_exists(self, upgraded_db) -> None:
        cols = {
            row[1]
            for row in upgraded_db.execute(
                "PRAGMA table_info(tenants)"
            ).fetchall()
        }
        assert "auth_features" in cols

    def test_existing_tenants_backfilled_explicit_false(
        self, upgraded_db,
    ) -> None:
        for tid in ("t-default", "t-acme"):
            row = upgraded_db.execute(
                "SELECT auth_features FROM tenants WHERE id = ?", (tid,),
            ).fetchone()
            assert row is not None, f"tenant {tid} missing"
            payload = json.loads(row[0])
            assert payload == {
                "honeypot_active": False,
                "oauth_login": False,
                "turnstile_required": False,
            }, f"tenant {tid} backfill mismatch: {payload}"

    def test_post_migration_insert_without_auth_features_uses_default(
        self, upgraded_db,
    ) -> None:
        upgraded_db.execute(
            "INSERT INTO tenants (id, name, plan, enabled) VALUES "
            "('t-late', 'Late Tenant', 'free', 1)"
        )
        row = upgraded_db.execute(
            "SELECT auth_features FROM tenants WHERE id = 't-late'"
        ).fetchone()
        # Brand-new INSERT bypassing the AS-aware code path falls
        # through to the column DEFAULT — empty JSON object.  This
        # is the safe minimum that prevents a NOT NULL violation
        # without quietly granting AS features the operator never
        # asked for.
        assert json.loads(row[0]) == {}

    def test_post_migration_insert_with_explicit_value_kept(
        self, upgraded_db,
    ) -> None:
        upgraded_db.execute(
            "INSERT INTO tenants (id, name, plan, enabled, auth_features) "
            "VALUES ('t-new', 'New Tenant', 'free', 1, "
            '\'{"honeypot_active": true, "oauth_login": true, '
            '"turnstile_required": true}\')'
        )
        row = upgraded_db.execute(
            "SELECT auth_features FROM tenants WHERE id = 't-new'"
        ).fetchone()
        assert json.loads(row[0]) == {
            "honeypot_active": True,
            "oauth_login": True,
            "turnstile_required": True,
        }


class TestOperatorEditsPreserved:
    def test_backfill_does_not_clobber_pre_existing_value(
        self, monkeypatch, m0056,
    ) -> None:
        """If an operator manually pre-staged ``auth_features`` (e.g.
        via a hand-rolled ALTER + UPDATE) the migration must NOT
        overwrite it.  Only rows still equal to the column DEFAULT
        ``'{}'`` get backfilled."""
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0056_tenants_schema(conn)
        # Pre-stage the column the way an out-of-band operator might —
        # column already exists with a non-default value on one row.
        conn.execute(
            "ALTER TABLE tenants ADD COLUMN auth_features TEXT NOT NULL "
            "DEFAULT '{}'"
        )
        conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled, auth_features) "
            "VALUES ('t-operator-staged', 'Operator', 'free', 1, "
            '\'{"oauth_login": true}\')'
        )
        conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled, auth_features) "
            "VALUES ('t-pristine', 'Pristine', 'free', 1, '{}')"
        )
        _bind(monkeypatch, conn)

        m0056.upgrade()

        operator_row = conn.execute(
            "SELECT auth_features FROM tenants WHERE id = 't-operator-staged'"
        ).fetchone()
        assert json.loads(operator_row[0]) == {"oauth_login": True}, (
            "operator-staged value was clobbered by AS.0.2 backfill"
        )
        pristine_row = conn.execute(
            "SELECT auth_features FROM tenants WHERE id = 't-pristine'"
        ).fetchone()
        assert json.loads(pristine_row[0]) == {
            "honeypot_active": False,
            "oauth_login": False,
            "turnstile_required": False,
        }


# ─── Group 3: idempotency ─────────────────────────────────────────────────


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(
        self, monkeypatch, m0056,
    ) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0056_tenants_schema(conn)
        conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) VALUES "
            "('t-default', 'Default Tenant', 'free', 1)"
        )
        _bind(monkeypatch, conn)
        m0056.upgrade()
        first = conn.execute(
            "SELECT auth_features FROM tenants WHERE id = 't-default'"
        ).fetchone()[0]
        m0056.upgrade()
        second = conn.execute(
            "SELECT auth_features FROM tenants WHERE id = 't-default'"
        ).fetchone()[0]
        assert json.loads(first) == json.loads(second)


# ─── Group 4: PG dialect branch executes ──────────────────────────────────


class TestPgBranchExecutes:
    def test_pg_branch_emits_jsonb_alter_and_backfill(
        self, monkeypatch, m0056,
    ) -> None:
        """The PG branch should emit an ALTER TABLE … jsonb + an
        UPDATE … WHERE auth_features = '{}'::jsonb.  We don't try
        to drive a real PG instance from a unit test — only that
        the dialect branch is taken and the SQL shape is what the
        production migration will execute.
        """
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0056.upgrade()

        # ALTER + UPDATE = exactly two statements on PG.
        assert len(captured) == 2
        joined = "\n".join(captured)
        assert "ALTER TABLE tenants" in joined
        assert "ADD COLUMN IF NOT EXISTS auth_features jsonb" in joined
        assert "::jsonb" in joined
        assert "UPDATE tenants" in joined
        # Backfill writes the explicit-false JSON shape in
        # alphabetically-sorted key order (deterministic round-trip).
        assert '"honeypot_active": false' in joined
        assert '"oauth_login": false' in joined
        assert '"turnstile_required": false' in joined

    def test_pg_downgrade_drops_column(self, monkeypatch, m0056) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0056.downgrade()
        assert len(captured) == 1
        assert "DROP COLUMN IF EXISTS auth_features" in captured[0]
