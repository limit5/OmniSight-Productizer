"""AS.2.2 — alembic 0057 ``oauth_tokens`` migration contract.

Locks the load-bearing properties of the AS.2.2 table-add:

1.  **Structural** — revision ``0057`` chains onto ``0058`` (the
    AS roadmap migration table reserved 0057 ahead of time but
    AS.0.3 / 0058 was authored to skip the slot so it could land
    first; this row appends after rather than retro-inserting to
    keep the existing 0058 contract test stable); the PG branch
    uses ``DOUBLE PRECISION`` and ``IF NOT EXISTS``; the SQLite
    branch uses ``REAL`` and is also ``IF NOT EXISTS`` guarded
    for idempotency.

2.  **Functional (SQLite)** — pre-seed the dependency tables
    (``users`` + ``tenants``), run ``upgrade()``, then assert:
      * the ``oauth_tokens`` table exists,
      * every column listed in the AS.2.2 TODO row is present
        (``user_id`` / ``provider`` / ``access_token_enc`` /
        ``refresh_token_enc`` / ``expires_at`` / ``scope`` /
        ``key_version``) plus the bookkeeping triplet
        (``created_at`` / ``updated_at`` / ``version``),
      * the composite ``(user_id, provider)`` PK enforces
        one-binding-per-pair (second insert for the same pair
        raises),
      * the provider CHECK rejects an unknown provider string,
      * the FK ``user_id → users.id`` cascades on user delete,
      * the secondary index ``idx_oauth_tokens_provider_expires``
        was created and is shaped for the AS.2.4 refresh-hook
        scan,
      * column DEFAULTs for the ciphertext / scope columns are
        the empty string (the vault never emits an empty
        ciphertext, so empty is unambiguous "not set yet"),
      * ``key_version`` defaults to ``1`` matching
        :data:`backend.security.token_vault.KEY_VERSION_CURRENT`.

3.  **Idempotency** — running ``upgrade()`` twice is a no-op:
    ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS``
    short-circuit on the second pass.

4.  **PG dialect branch** — capture SQL via a stub bind, verify
    the CREATE TABLE uses ``DOUBLE PRECISION`` (not ``REAL``) and
    ``NULLS LAST`` on the index expression (the asyncpg+PG read
    path the AS.2.4 refresh-hook will use).

5.  **Round-trip with the AS.2.1 token vault** — encrypt a
    plaintext via :func:`backend.security.token_vault.encrypt_for_user`,
    persist the resulting ``ciphertext`` + ``key_version`` into
    the freshly-migrated table, read back, and decrypt — the
    schema must accept the vault's output shape verbatim.

6.  **Cross-module drift guard** — the provider CHECK string in
    the migration source MUST byte-equal the sorted list emitted
    by :data:`backend.security.token_vault.SUPPORTED_PROVIDERS`
    and :data:`backend.account_linking._AS1_OAUTH_PROVIDERS`.
    Adding a new provider must touch all three in the same PR.

The schema half of the AS.2.2 contract; the vault helper half
is locked by ``test_token_vault.py``.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0057 = (
    BACKEND_ROOT / "alembic" / "versions" / "0057_oauth_tokens.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0057():
    return _load_module(MIGRATION_0057, "_alembic_test_0057")


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0057.read_text()

    def test_revision_id_is_0057(self, source: str) -> None:
        assert 'revision = "0057"' in source

    def test_down_revision_is_0058(self, source: str) -> None:
        # 0057 chains AFTER 0058 (not before) — AS.0.3 / 0058 was
        # authored to skip the reserved 0057 slot so it could land
        # first; appending here keeps that contract test stable
        # and lets dev DBs already at 0058 pick up the new table
        # via a normal upgrade-head pass.
        assert 'down_revision = "0058"' in source

    def test_pg_branch_uses_double_precision(self, m0057) -> None:
        # epoch-seconds columns use DOUBLE PRECISION on PG to round-
        # trip ``time.time()`` losslessly; same convention as
        # git_accounts / llm_credentials / sessions.
        assert "DOUBLE PRECISION" in m0057._PG_CREATE_TABLE
        # Sanity: no naked ``REAL`` on the PG path (REAL is the SQLite
        # affinity; on PG it would silently be float4).
        assert " REAL" not in m0057._PG_CREATE_TABLE

    def test_pg_branch_idempotent(self, m0057) -> None:
        assert "CREATE TABLE IF NOT EXISTS oauth_tokens" in m0057._PG_CREATE_TABLE

    def test_pg_index_uses_nulls_last(self, m0057) -> None:
        # AS.2.4 refresh hook range-scans on (provider, expires_at);
        # NULL-expiry rows must sort past the range so the hook can
        # stop early.
        assert "NULLS LAST" in m0057._PG_INDEX_EXPIRES

    def test_sqlite_branch_uses_real(self, m0057) -> None:
        # SQLite has no DOUBLE PRECISION; REAL is the parallel
        # affinity and round-trips ``time.time()`` cleanly.
        assert " REAL" in m0057._SQLITE_CREATE_TABLE
        assert "DOUBLE PRECISION" not in m0057._SQLITE_CREATE_TABLE

    def test_sqlite_branch_idempotent(self, m0057) -> None:
        assert "CREATE TABLE IF NOT EXISTS oauth_tokens" in m0057._SQLITE_CREATE_TABLE
        assert (
            "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_provider_expires"
            in m0057._SQLITE_INDEX_EXPIRES
        )

    def test_provider_check_clause_matches_vault_whitelist(self, m0057) -> None:
        # AS.0.4 §5.2 cross-module drift guard: the provider CHECK
        # in the schema MUST byte-equal the vault's SUPPORTED_PROVIDERS
        # frozenset (and account_linking._AS1_OAUTH_PROVIDERS).  The
        # CHECK clause string is sorted alphabetically so we can
        # assert the exact substring.
        from backend.security import token_vault
        from backend import account_linking

        sorted_providers = sorted(token_vault.SUPPORTED_PROVIDERS)
        expected_clause = ",".join(f"'{p}'" for p in sorted_providers)
        assert expected_clause == m0057._PROVIDERS_SQL, (
            f"alembic 0057 provider list {m0057._PROVIDERS_SQL!r} drifted "
            f"from token_vault.SUPPORTED_PROVIDERS {sorted_providers!r}"
        )
        # Belt+braces: byte-equality across the three modules.
        assert (
            token_vault.SUPPORTED_PROVIDERS
            == account_linking._AS1_OAUTH_PROVIDERS
        )

    def test_composite_pk_declared(self, m0057) -> None:
        # One binding per (user_id, provider) — enforced at the
        # database layer rather than via a separate UNIQUE index.
        assert "PRIMARY KEY (user_id, provider)" in m0057._PG_CREATE_TABLE
        assert "PRIMARY KEY (user_id, provider)" in m0057._SQLITE_CREATE_TABLE

    def test_user_id_fk_cascade_declared(self, m0057) -> None:
        # GDPR / DSAR delete-user paths must not leave stranded
        # ciphertext rows.
        assert "REFERENCES users(id) ON DELETE CASCADE" in m0057._PG_CREATE_TABLE
        assert "REFERENCES users(id) ON DELETE CASCADE" in m0057._SQLITE_CREATE_TABLE

    def test_key_version_default_is_one(self, m0057) -> None:
        # Must match token_vault.KEY_VERSION_CURRENT so a vault-
        # encrypted row inserted with no explicit ``key_version``
        # round-trips cleanly through ``decrypt_for_user``.
        from backend.security import token_vault

        assert "key_version        INTEGER NOT NULL DEFAULT 1" in m0057._PG_CREATE_TABLE
        assert "key_version        INTEGER NOT NULL DEFAULT 1" in m0057._SQLITE_CREATE_TABLE
        assert token_vault.KEY_VERSION_CURRENT == 1

    def test_required_columns_present_in_pg(self, m0057) -> None:
        # The seven columns the AS.2.2 TODO row enumerates plus the
        # bookkeeping triplet (created_at / updated_at / version).
        required = (
            "user_id",
            "provider",
            "access_token_enc",
            "refresh_token_enc",
            "expires_at",
            "scope",
            "key_version",
            "created_at",
            "updated_at",
            "version",
        )
        for col in required:
            assert col in m0057._PG_CREATE_TABLE, f"PG branch missing {col}"
            assert col in m0057._SQLITE_CREATE_TABLE, f"SQLite branch missing {col}"


# ─── Group 2: functional SQLite upgrade ───────────────────────────────────


def _bootstrap_pre_0057_users_schema(conn: sqlite3.Connection) -> None:
    """Recreate the minimum-viable ``users`` table the FK targets.

    We only need ``users.id`` for the FK to resolve; the full
    schema isn't relevant to the migration's behavior.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE users (
            id     TEXT PRIMARY KEY,
            email  TEXT NOT NULL DEFAULT ''
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
def upgraded_db(monkeypatch, m0057) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0057_users_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email) VALUES ('u-alice', 'alice@example.com')"
    )
    conn.execute(
        "INSERT INTO users (id, email) VALUES ('u-bob', 'bob@example.com')"
    )
    _bind(monkeypatch, conn)
    m0057.upgrade()
    return conn


class TestSqliteUpgradeCreatesTable:
    def test_oauth_tokens_table_exists(self, upgraded_db) -> None:
        row = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='oauth_tokens'"
        ).fetchone()
        assert row is not None

    def test_all_required_columns_present(self, upgraded_db) -> None:
        cols = {
            row[1]
            for row in upgraded_db.execute(
                "PRAGMA table_info(oauth_tokens)"
            ).fetchall()
        }
        required = {
            "user_id",
            "provider",
            "access_token_enc",
            "refresh_token_enc",
            "expires_at",
            "scope",
            "key_version",
            "created_at",
            "updated_at",
            "version",
        }
        missing = required - cols
        assert not missing, f"oauth_tokens missing columns: {missing}"

    def test_provider_expires_index_exists(self, upgraded_db) -> None:
        row = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_oauth_tokens_provider_expires'"
        ).fetchone()
        assert row is not None

    def test_composite_pk_rejects_duplicate_pair(self, upgraded_db) -> None:
        upgraded_db.execute(
            "INSERT INTO oauth_tokens "
            "(user_id, provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("u-alice", "google", 1.0, 1.0),
        )
        # Second insert for the same (user_id, provider) pair must
        # raise — the composite PK enforces "one binding per pair".
        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO oauth_tokens "
                "(user_id, provider, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("u-alice", "google", 2.0, 2.0),
            )

    def test_composite_pk_allows_different_providers_per_user(
        self, upgraded_db,
    ) -> None:
        upgraded_db.execute(
            "INSERT INTO oauth_tokens "
            "(user_id, provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("u-alice", "google", 1.0, 1.0),
        )
        # Same user, different provider — must succeed.
        upgraded_db.execute(
            "INSERT INTO oauth_tokens "
            "(user_id, provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("u-alice", "github", 1.0, 1.0),
        )

    def test_provider_check_rejects_unknown_provider(
        self, upgraded_db,
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO oauth_tokens "
                "(user_id, provider, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("u-alice", "facebook", 1.0, 1.0),
            )

    def test_provider_check_accepts_each_supported_provider(
        self, upgraded_db,
    ) -> None:
        from backend.security import token_vault

        # Two distinct users so the composite PK doesn't reject the
        # second insert when we cycle through providers.
        upgraded_db.execute(
            "INSERT INTO users (id, email) VALUES ('u-multi', 'm@x.com')"
        )
        for provider in sorted(token_vault.SUPPORTED_PROVIDERS):
            upgraded_db.execute(
                "INSERT INTO oauth_tokens "
                "(user_id, provider, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("u-multi", provider, 1.0, 1.0),
            )

    def test_user_delete_cascades_to_oauth_tokens(self, upgraded_db) -> None:
        upgraded_db.execute(
            "INSERT INTO oauth_tokens "
            "(user_id, provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("u-alice", "google", 1.0, 1.0),
        )
        upgraded_db.execute(
            "INSERT INTO oauth_tokens "
            "(user_id, provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("u-bob", "github", 1.0, 1.0),
        )
        upgraded_db.execute("DELETE FROM users WHERE id = 'u-alice'")
        rows = upgraded_db.execute(
            "SELECT user_id FROM oauth_tokens ORDER BY user_id"
        ).fetchall()
        # Alice's row was cascaded; Bob's survived.
        assert rows == [("u-bob",)]

    def test_default_ciphertext_columns_are_empty_string(
        self, upgraded_db,
    ) -> None:
        upgraded_db.execute(
            "INSERT INTO oauth_tokens "
            "(user_id, provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("u-alice", "google", 1.0, 1.0),
        )
        row = upgraded_db.execute(
            "SELECT access_token_enc, refresh_token_enc, scope, key_version, version "
            "FROM oauth_tokens WHERE user_id='u-alice'"
        ).fetchone()
        assert row == ("", "", "", 1, 0)


# ─── Group 3: idempotency ─────────────────────────────────────────────────


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(
        self, monkeypatch, m0057,
    ) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0057_users_schema(conn)
        _bind(monkeypatch, conn)
        m0057.upgrade()
        m0057.upgrade()
        # A second upgrade should not have raised; sanity-check the
        # table still exists with one PK index + one secondary index.
        idx = sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='oauth_tokens'"
            ).fetchall()
        )
        assert "idx_oauth_tokens_provider_expires" in idx


# ─── Group 4: PG dialect branch executes ──────────────────────────────────


class TestPgBranchExecutes:
    def test_pg_branch_emits_create_table_and_index(
        self, monkeypatch, m0057,
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
        m0057.upgrade()

        # CREATE TABLE + CREATE INDEX = exactly two statements on PG.
        assert len(captured) == 2
        joined = "\n".join(captured)
        assert "CREATE TABLE IF NOT EXISTS oauth_tokens" in joined
        assert "DOUBLE PRECISION" in joined
        assert "REFERENCES users(id) ON DELETE CASCADE" in joined
        assert "PRIMARY KEY (user_id, provider)" in joined
        assert (
            "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_provider_expires"
            in joined
        )
        assert "NULLS LAST" in joined

    def test_pg_downgrade_drops_index_then_table(
        self, monkeypatch, m0057,
    ) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        # ``op.execute`` is the alembic public API used in the
        # downgrade; route it to our bind capture too.
        def _exec(sql):
            captured.append(str(sql))

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        monkeypatch.setattr(alembic_op, "execute", _exec)
        m0057.downgrade()
        joined = "\n".join(captured)
        # Index drop precedes table drop (defensive — PG would let
        # DROP TABLE cascade-drop the index, but explicit ordering
        # documents intent).
        assert "DROP INDEX IF EXISTS idx_oauth_tokens_provider_expires" in joined
        assert "DROP TABLE IF EXISTS oauth_tokens" in joined
        i_idx = joined.find("DROP INDEX IF EXISTS")
        i_tab = joined.find("DROP TABLE IF EXISTS")
        assert i_idx < i_tab, "index drop must precede table drop"


# ─── Group 5: round-trip with the AS.2.1 token vault ─────────────────────


class TestVaultRoundTrip:
    """The vault is the only approved entry-point for this table's
    ciphertext columns; the schema must accept the vault's output
    shape verbatim (TEXT for ciphertext, INTEGER for key_version).
    A schema drift here would leave the AS.6.1 OAuth router unable
    to persist token-exchange responses."""

    def test_vault_ciphertext_round_trips_through_table(
        self, upgraded_db, m0057,
    ) -> None:
        # ``token_vault.encrypt_for_user`` requires ``backend.secret_store``
        # which lazily generates a Fernet key on first use; this is a
        # pure-Python op with no DB / network dependency.
        from backend.security import token_vault

        plaintext = "ya29.a0AfH6SMC-fakeaccesstokenforacontracttest"
        enc = token_vault.encrypt_for_user(
            "u-alice", "google", plaintext,
        )
        upgraded_db.execute(
            "INSERT INTO oauth_tokens "
            "(user_id, provider, access_token_enc, key_version, "
            " scope, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("u-alice", "google", enc.ciphertext, enc.key_version,
             "openid email", 1.0, 1.0),
        )
        row = upgraded_db.execute(
            "SELECT access_token_enc, key_version FROM oauth_tokens "
            "WHERE user_id=? AND provider=?",
            ("u-alice", "google"),
        ).fetchone()
        # Reconstruct the EncryptedToken dataclass and decrypt.
        roundtripped = token_vault.EncryptedToken(
            ciphertext=row[0], key_version=row[1],
        )
        recovered = token_vault.decrypt_for_user(
            "u-alice", "google", roundtripped,
        )
        assert recovered == plaintext

    def test_default_key_version_matches_vault_constant(
        self, upgraded_db,
    ) -> None:
        from backend.security import token_vault

        # An INSERT that omits ``key_version`` must default to the
        # vault's KEY_VERSION_CURRENT — otherwise a freshly written
        # row whose ``key_version`` falls through to the column
        # default would be rejected by ``decrypt_for_user`` with
        # UnknownKeyVersionError.
        upgraded_db.execute(
            "INSERT INTO oauth_tokens "
            "(user_id, provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("u-alice", "google", 1.0, 1.0),
        )
        row = upgraded_db.execute(
            "SELECT key_version FROM oauth_tokens "
            "WHERE user_id='u-alice' AND provider='google'"
        ).fetchone()
        assert row[0] == token_vault.KEY_VERSION_CURRENT


# ─── Group 6: migrator drift guard ───────────────────────────────────────


class TestMigratorListsTable:
    """The Phase-3 G4 lesson: any new table MUST be added to the
    migrator's ``TABLES_IN_ORDER`` or the next prod cutover loses
    its data silently.  This test belt+braces the broader
    ``test_migrator_schema_coverage`` drift gate by pinning the
    expected entry name."""

    def test_oauth_tokens_in_tables_in_order(self) -> None:
        import importlib.util as _u
        repo_root = Path(__file__).resolve().parents[2]
        spec = _u.spec_from_file_location(
            "migrate_sqlite_to_pg", repo_root / "scripts" / "migrate_sqlite_to_pg.py"
        )
        mig = _u.module_from_spec(spec)
        sys.modules["migrate_sqlite_to_pg"] = mig
        spec.loader.exec_module(mig)
        assert "oauth_tokens" in mig.TABLES_IN_ORDER

    def test_oauth_tokens_not_in_identity_id_set(self) -> None:
        # Composite TEXT PK — listing it as IDENTITY would crash
        # the sequence-reset logic at cutover time.
        import importlib.util as _u
        repo_root = Path(__file__).resolve().parents[2]
        spec = _u.spec_from_file_location(
            "migrate_sqlite_to_pg", repo_root / "scripts" / "migrate_sqlite_to_pg.py"
        )
        mig = _u.module_from_spec(spec)
        sys.modules["migrate_sqlite_to_pg"] = mig
        spec.loader.exec_module(mig)
        assert "oauth_tokens" not in mig.TABLES_WITH_IDENTITY_ID

    def test_oauth_tokens_replays_after_users(self) -> None:
        # FK target ``users`` must be earlier in the replay order
        # so PG's enforced FK constraints don't trip during cutover.
        import importlib.util as _u
        repo_root = Path(__file__).resolve().parents[2]
        spec = _u.spec_from_file_location(
            "migrate_sqlite_to_pg", repo_root / "scripts" / "migrate_sqlite_to_pg.py"
        )
        mig = _u.module_from_spec(spec)
        sys.modules["migrate_sqlite_to_pg"] = mig
        spec.loader.exec_module(mig)
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("users") < order.index("oauth_tokens")
