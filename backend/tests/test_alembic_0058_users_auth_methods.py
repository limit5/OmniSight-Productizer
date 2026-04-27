"""AS.0.3 — alembic 0058 ``users.auth_methods`` migration contract.

Locks the load-bearing properties of the AS.0.3 column-add:

1.  **Structural** — revision ``0058`` chains onto ``0056`` (the
    AS.2.2 ``oauth_tokens`` migration reserves 0057 in the AS
    roadmap migration table; the chain skips it deliberately);
    the PG branch emits ``::jsonb`` casts and ``IF NOT EXISTS``;
    the SQLite branch is guarded by ``PRAGMA table_info``.

2.  **Functional (SQLite)** — pre-seed three users on the pre-0058
    schema (one with password, one without, one operator-edited
    auth_methods), run ``upgrade()``, then assert:
      * the ``auth_methods`` column exists,
      * the password user is backfilled to ``["password"]``,
      * the no-password (invite-pending) user stays at ``[]``,
      * the operator-edited row is NOT clobbered,
      * a brand-new INSERT that omits ``auth_methods`` falls
        through to the column DEFAULT ``'[]'`` (safe minimum —
        no NOT NULL violation, no silent ``"password"`` grant).

3.  **Idempotency** — running ``upgrade()`` twice is a no-op:
    the SQLite branch's ``PRAGMA table_info`` guard prevents
    duplicate-column errors and the backfill UPDATE only matches
    rows at the ``'[]'`` default so the second pass touches zero
    rows.

4.  **PG dialect branch** — capture SQL via a stub bind, verify
    the ALTER + UPDATE pair fires with ``jsonb`` literals and the
    ``["password"]`` legacy backfill JSON shape.

5.  **No OAuth seed for legacy rows** — the backfill JSON contains
    ``"password"`` ONLY; ``oauth_*`` tags are reserved for AS.1
    OAuth client to write via ``link_oauth_after_verification``.

The schema half of the AS.0.3 takeover-prevention contract; the
helper-module half is locked by ``test_account_linking_helper.py``.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0058 = (
    BACKEND_ROOT / "alembic" / "versions" / "0058_users_auth_methods.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0058():
    return _load_module(MIGRATION_0058, "_alembic_test_0058")


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0058.read_text()

    def test_revision_id_is_0058(self, source: str) -> None:
        assert 'revision = "0058"' in source

    def test_down_revision_is_0056(self, source: str) -> None:
        # 0057 is reserved for AS.2.2 ``oauth_tokens`` per the AS
        # roadmap migration table; the chain jumps over it the same
        # way 0056 jumped over 0055.
        assert 'down_revision = "0056"' in source

    def test_pg_branch_uses_jsonb(self, m0058) -> None:
        assert "::jsonb" in m0058._PG_ADD_COLUMN
        assert "jsonb NOT NULL DEFAULT" in m0058._PG_ADD_COLUMN

    def test_pg_branch_idempotent(self, m0058) -> None:
        assert "ADD COLUMN IF NOT EXISTS auth_methods" in m0058._PG_ADD_COLUMN

    def test_sqlite_branch_guards_via_pragma(self, source: str) -> None:
        assert "PRAGMA table_info(users)" in source

    def test_legacy_password_user_seed_is_password_only(self, m0058) -> None:
        payload = json.loads(m0058._LEGACY_PASSWORD_USER_AUTH_METHODS_JSON)
        # AS.0.3 contract: existing prod users with a password get
        # exactly ``["password"]`` — NO oauth tags get auto-seeded
        # even when oidc_subject is non-empty (the existing OIDC
        # route is a 501 stub, no prod federation has happened).
        assert payload == ["password"]

    def test_pg_backfill_filters_on_empty_default_and_password(
        self, m0058,
    ) -> None:
        # Both filters are mandatory: WHERE auth_methods = '[]' so
        # operator hand-edits aren't clobbered, AND password_hash <> ''
        # so invited-but-not-completed users don't get a phantom
        # "password" method they can't yet use.
        sql = m0058._PG_BACKFILL
        assert "WHERE auth_methods = '[]'::jsonb" in sql
        assert "password_hash <> ''" in sql

    def test_sqlite_backfill_filters_on_empty_default_and_password(
        self, m0058,
    ) -> None:
        sql = m0058._SQLITE_BACKFILL
        assert "WHERE auth_methods = '[]'" in sql
        assert "password_hash <> ''" in sql


# ─── Group 2: functional SQLite upgrade ───────────────────────────────────


def _bootstrap_pre_0058_users_schema(conn: sqlite3.Connection) -> None:
    """Recreate the pre-0058 ``users`` table shape.

    Mirrors alembic 0005 + the legacy ``backend/db.py`` _SCHEMA so
    the migration's ALTER TABLE has a realistic source schema to
    bite into.  We deliberately leave ``auth_methods`` OFF — the
    migration is responsible for adding it.
    """
    conn.executescript(
        """
        CREATE TABLE users (
            id              TEXT PRIMARY KEY,
            email           TEXT NOT NULL UNIQUE,
            name            TEXT NOT NULL DEFAULT '',
            role            TEXT NOT NULL DEFAULT 'viewer',
            password_hash   TEXT NOT NULL DEFAULT '',
            oidc_provider   TEXT NOT NULL DEFAULT '',
            oidc_subject    TEXT NOT NULL DEFAULT '',
            enabled         INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            last_login_at   TEXT,
            failed_login_count INTEGER NOT NULL DEFAULT 0,
            locked_until    REAL,
            tenant_id       TEXT NOT NULL DEFAULT 't-default'
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
def upgraded_db(monkeypatch, m0058) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0058_users_schema(conn)
    # Three users to exercise backfill + edge cases:
    #   u-pwd     — has password_hash → backfilled to ["password"]
    #   u-invite  — empty password_hash (invited, not yet completed)
    #             → stays at []
    #   u-admin   — has password_hash, would also backfill
    conn.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u-pwd', 'pwd@example.com', '$argon2id$x$y')"
    )
    conn.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u-invite', 'invite@example.com', '')"
    )
    conn.execute(
        "INSERT INTO users (id, email, password_hash) "
        "VALUES ('u-admin', 'admin@example.com', '$argon2id$a$b')"
    )
    _bind(monkeypatch, conn)
    m0058.upgrade()
    return conn


class TestSqliteUpgradeAddsColumn:
    def test_auth_methods_column_exists(self, upgraded_db) -> None:
        cols = {
            row[1]
            for row in upgraded_db.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }
        assert "auth_methods" in cols

    def test_password_users_backfilled_to_password_only(
        self, upgraded_db,
    ) -> None:
        for uid in ("u-pwd", "u-admin"):
            row = upgraded_db.execute(
                "SELECT auth_methods FROM users WHERE id = ?", (uid,),
            ).fetchone()
            assert row is not None, f"user {uid} missing"
            assert json.loads(row[0]) == ["password"], (
                f"user {uid} backfill mismatch: {row[0]}"
            )

    def test_invite_user_without_password_stays_empty(
        self, upgraded_db,
    ) -> None:
        row = upgraded_db.execute(
            "SELECT auth_methods FROM users WHERE id = 'u-invite'"
        ).fetchone()
        # The invited-but-not-completed user has no password yet —
        # they shouldn't get a phantom "password" method.  The
        # change-password helper appends "password" when they
        # actually set one (see _change_password_impl).
        assert json.loads(row[0]) == []

    def test_post_migration_insert_without_auth_methods_uses_default(
        self, upgraded_db,
    ) -> None:
        upgraded_db.execute(
            "INSERT INTO users (id, email, password_hash) VALUES "
            "('u-late', 'late@example.com', 'h')"
        )
        row = upgraded_db.execute(
            "SELECT auth_methods FROM users WHERE id = 'u-late'"
        ).fetchone()
        # Brand-new INSERT bypassing the AS-aware code path falls
        # through to the column DEFAULT — empty JSON array.  This
        # is the safe minimum: NOT NULL satisfied, no silent
        # ``"password"`` grant the operator never asked for.
        assert json.loads(row[0]) == []

    def test_post_migration_insert_with_explicit_value_kept(
        self, upgraded_db,
    ) -> None:
        upgraded_db.execute(
            "INSERT INTO users (id, email, password_hash, auth_methods) "
            "VALUES ('u-new', 'new@example.com', 'h', "
            '\'["password","oauth_google"]\')'
        )
        row = upgraded_db.execute(
            "SELECT auth_methods FROM users WHERE id = 'u-new'"
        ).fetchone()
        assert json.loads(row[0]) == ["password", "oauth_google"]


class TestOperatorEditsPreserved:
    def test_backfill_does_not_clobber_pre_existing_value(
        self, monkeypatch, m0058,
    ) -> None:
        """If an operator manually pre-staged ``auth_methods`` (e.g.
        via a hand-rolled ALTER + UPDATE) the migration must NOT
        overwrite it.  Only rows still equal to the column DEFAULT
        ``'[]'`` get backfilled — and even then ONLY when the row
        carries a non-empty password_hash."""
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0058_users_schema(conn)
        # Pre-stage the column the way an out-of-band operator might —
        # column already exists with a non-default value on one row.
        conn.execute(
            "ALTER TABLE users ADD COLUMN auth_methods TEXT NOT NULL "
            "DEFAULT '[]'"
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, auth_methods) "
            "VALUES ('u-operator', 'op@example.com', 'h', "
            '\'["password","oauth_github"]\')'
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, auth_methods) "
            "VALUES ('u-pristine', 'pristine@example.com', 'h', '[]')"
        )
        _bind(monkeypatch, conn)

        m0058.upgrade()

        operator_row = conn.execute(
            "SELECT auth_methods FROM users WHERE id = 'u-operator'"
        ).fetchone()
        assert json.loads(operator_row[0]) == ["password", "oauth_github"], (
            "operator-staged value was clobbered by AS.0.3 backfill"
        )
        pristine_row = conn.execute(
            "SELECT auth_methods FROM users WHERE id = 'u-pristine'"
        ).fetchone()
        assert json.loads(pristine_row[0]) == ["password"]


# ─── Group 3: idempotency ─────────────────────────────────────────────────


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(
        self, monkeypatch, m0058,
    ) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0058_users_schema(conn)
        conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES "
            "('u-default', 'default@example.com', 'h')"
        )
        _bind(monkeypatch, conn)
        m0058.upgrade()
        first = conn.execute(
            "SELECT auth_methods FROM users WHERE id = 'u-default'"
        ).fetchone()[0]
        m0058.upgrade()
        second = conn.execute(
            "SELECT auth_methods FROM users WHERE id = 'u-default'"
        ).fetchone()[0]
        assert json.loads(first) == json.loads(second) == ["password"]


# ─── Group 4: PG dialect branch executes ──────────────────────────────────


class TestPgBranchExecutes:
    def test_pg_branch_emits_jsonb_alter_and_backfill(
        self, monkeypatch, m0058,
    ) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0058.upgrade()

        # ALTER + UPDATE = exactly two statements on PG.
        assert len(captured) == 2
        joined = "\n".join(captured)
        assert "ALTER TABLE users" in joined
        assert "ADD COLUMN IF NOT EXISTS auth_methods jsonb" in joined
        assert "::jsonb" in joined
        assert "UPDATE users" in joined
        # The backfill writes ["password"] and ONLY that — no
        # oauth_* tag is auto-seeded even when oidc_subject is set.
        assert '["password"]' in joined
        # Belt-and-braces: verify no oauth_* string leaked into the
        # backfill — a future refactor that "helpfully" auto-binds
        # oidc_subject must trip this regression.
        assert "oauth_" not in joined

    def test_pg_downgrade_drops_column(self, monkeypatch, m0058) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0058.downgrade()
        assert len(captured) == 1
        assert "DROP COLUMN IF EXISTS auth_methods" in captured[0]
