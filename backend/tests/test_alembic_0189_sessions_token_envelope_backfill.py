"""FX.11.1 -- alembic 0189 sessions.token envelope backfill contract.

The migration:
  * adds ``sessions.token_lookup_index`` TEXT (nullable) plus a unique
    index ``idx_sessions_token_lookup_index``;
  * iterates every session row and rewrites plaintext ``token`` values
    to the packed ``{"ciphertext", "dek_ref"}`` KS envelope JSON shape;
  * is idempotent -- re-running on rows already in envelope shape is a
    no-op.

These tests exercise the SQLite branch end-to-end (real envelope helper,
real ``LocalFernetKMSAdapter``) plus structural guards on the migration
file. PG-only branches (``ADD COLUMN IF NOT EXISTS``) are checked via
the source guard tests; functional PG behaviour is covered by the
``alembic_pg_live_upgrade`` job in CI.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0189 = (
    BACKEND_ROOT
    / "alembic"
    / "versions"
    / "0189_sessions_token_envelope_backfill.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0189():
    return _load_module(MIGRATION_0189, "_alembic_test_0189")


# ─── Group 1: structural guards on the migration file ────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0189.read_text()

    def test_revision_id_is_0189(self, source: str) -> None:
        assert 'revision = "0189"' in source

    def test_down_revision_is_0188(self, source: str) -> None:
        # 0188 is the merge head that converged the four post-0058 tips.
        assert 'down_revision = "0188"' in source

    def test_pg_branch_uses_add_column_if_not_exists(self, m0189) -> None:
        assert "ADD COLUMN IF NOT EXISTS token_lookup_index" in m0189._PG_ADD_COLUMN

    def test_unique_index_declared(self, m0189) -> None:
        assert "CREATE UNIQUE INDEX IF NOT EXISTS" in m0189._CREATE_LOOKUP_INDEX
        assert "idx_sessions_token_lookup_index" in m0189._CREATE_LOOKUP_INDEX
        assert "ON sessions(token_lookup_index)" in m0189._CREATE_LOOKUP_INDEX

    def test_drop_index_is_if_exists(self, m0189) -> None:
        assert m0189._DROP_LOOKUP_INDEX == (
            "DROP INDEX IF EXISTS idx_sessions_token_lookup_index"
        )

    def test_helpers_exposed(self, m0189) -> None:
        # The helpers are unit-test seams; locking their names guards
        # against silent renames during refactors.
        assert callable(m0189._looks_like_packed_envelope)
        assert callable(m0189._hash_token)
        assert callable(m0189._pack)


# ─── Group 2: helper unit tests ──────────────────────────────────────


class TestPackedEnvelopeDetector:
    def test_plaintext_token_is_not_envelope(self, m0189) -> None:
        assert m0189._looks_like_packed_envelope("not-json-at-all") is False

    def test_random_json_object_is_not_envelope(self, m0189) -> None:
        assert m0189._looks_like_packed_envelope('{"foo": "bar"}') is False

    def test_packed_shape_is_envelope(self, m0189) -> None:
        packed = json.dumps(
            {"ciphertext": "irrelevant", "dek_ref": {"dek_id": "d", "tenant_id": "t"}}
        )
        assert m0189._looks_like_packed_envelope(packed) is True

    def test_missing_dek_ref_is_not_envelope(self, m0189) -> None:
        # ``ciphertext`` alone (the bare envelope half) does not match
        # the packed shape -- the migration writes BOTH halves so the
        # FX.11.2 read path can decrypt without a tenant_deks JOIN.
        assert m0189._looks_like_packed_envelope('{"ciphertext": "x"}') is False

    def test_dek_ref_must_be_object(self, m0189) -> None:
        bogus = json.dumps({"ciphertext": "x", "dek_ref": "not-a-dict"})
        assert m0189._looks_like_packed_envelope(bogus) is False

    def test_hash_token_is_sha256_hex(self, m0189) -> None:
        # Stable across runs; FX.11.2 lookup must compute the exact
        # same hash to find a row by cookie token.
        h = m0189._hash_token("abc")
        assert len(h) == 64
        assert h == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )


# ─── Group 3: functional SQLite end-to-end ───────────────────────────


def _bootstrap_pre_0189_schema(engine) -> None:
    """Create the minimal pre-0189 ``users`` + ``sessions`` shape."""

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE users (
                id        TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 't-default'
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE sessions (
                token        TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                csrf_token   TEXT NOT NULL DEFAULT '',
                created_at   REAL NOT NULL DEFAULT 0,
                expires_at   REAL NOT NULL DEFAULT 0,
                last_seen_at REAL NOT NULL DEFAULT 0
            )
            """
        )


def _seed(engine, *, users, sessions) -> None:
    with engine.begin() as conn:
        for uid, tid in users:
            conn.exec_driver_sql(
                "INSERT INTO users (id, tenant_id) VALUES (?, ?)", (uid, tid)
            )
        for token, uid in sessions:
            conn.exec_driver_sql(
                "INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, uid)
            )


@pytest.fixture()
def upgraded_engine(monkeypatch, m0189, tmp_path):
    """Real SQLAlchemy engine + 0189 upgrade applied.

    Uses a file-backed SQLite (not ``:memory:``) so multiple
    ``engine.begin()`` blocks see each other's writes through SQLAlchemy
    pool reuse on the same path.
    """

    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "fx-11-1-test-secret-key")
    # Reset the secret_store fernet cache so the test KEK is picked up.
    from backend import secret_store

    secret_store._reset_for_tests()

    db_path = tmp_path / "fx111.db"
    engine = create_engine(f"sqlite:///{db_path}")
    _bootstrap_pre_0189_schema(engine)
    _seed(
        engine,
        users=[("u-alice", "t-acme"), ("u-bob", "t-beta")],
        sessions=[
            ("plain-alice-token-aaaa", "u-alice"),
            ("plain-bob-token-bbbb", "u-bob"),
            ("plain-orphan-token-cccc", "u-deleted"),
        ],
    )

    # Bind the alembic ``op.get_bind()`` to a SQLAlchemy connection that
    # exposes both ``exec_driver_sql`` (for the DDL strings) AND
    # ``execute(text(), {...})`` (for the parameterized UPDATE). Use a
    # single connection wrapped by ``begin()`` so the migration's writes
    # are visible to subsequent test reads.
    from alembic import op as alembic_op

    conn = engine.connect()
    txn = conn.begin()
    monkeypatch.setattr(alembic_op, "get_bind", lambda: conn)
    try:
        m0189.upgrade()
        txn.commit()
    except Exception:
        txn.rollback()
        raise
    finally:
        conn.close()

    yield engine
    engine.dispose()


class TestSqliteUpgradeShape:
    def test_token_lookup_index_column_added(self, upgraded_engine) -> None:
        with upgraded_engine.begin() as conn:
            cols = {
                r[1]
                for r in conn.exec_driver_sql(
                    "PRAGMA table_info(sessions)"
                ).fetchall()
            }
        assert "token_lookup_index" in cols

    def test_unique_lookup_index_created(self, upgraded_engine) -> None:
        with upgraded_engine.begin() as conn:
            rows = conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND name='idx_sessions_token_lookup_index'"
            ).fetchall()
        assert len(rows) == 1
        assert "UNIQUE" in (rows[0][1] or "").upper()

    def test_all_session_tokens_now_packed_envelopes(
        self, upgraded_engine, m0189
    ) -> None:
        with upgraded_engine.begin() as conn:
            rows = conn.exec_driver_sql(
                "SELECT token, token_lookup_index FROM sessions ORDER BY user_id"
            ).fetchall()
        assert len(rows) == 3
        for token, lookup in rows:
            assert m0189._looks_like_packed_envelope(token), (
                "every row must end up in the packed envelope shape"
            )
            assert lookup is not None
            assert len(lookup) == 64  # sha256 hex

    def test_token_lookup_index_matches_sha256_of_plaintext(
        self, upgraded_engine, m0189
    ) -> None:
        # Reverse-derive: decrypt one row and confirm the hash on file
        # matches sha256(plaintext). FX.11.2's read path reduces to this
        # exact equality, so locking it here is the contract that lets
        # FX.11.2 ship without its own data-correctness test.
        from backend.security import envelope as ks_envelope

        with upgraded_engine.begin() as conn:
            row = conn.exec_driver_sql(
                "SELECT token, token_lookup_index FROM sessions "
                "WHERE user_id = 'u-alice'"
            ).fetchone()
        packed_json, stored_lookup = row[0], row[1]
        payload = json.loads(packed_json)
        dek_ref = ks_envelope.TenantDEKRef.from_dict(payload["dek_ref"])
        plaintext = ks_envelope.decrypt(payload["ciphertext"], dek_ref)
        assert plaintext == "plain-alice-token-aaaa"
        assert stored_lookup == m0189._hash_token("plain-alice-token-aaaa")

    def test_envelope_carries_correct_tenant_binding(
        self, upgraded_engine
    ) -> None:
        # Each row's envelope must encode the user's tenant -- this is
        # what gates KS.1.x cross-tenant misuse later.
        with upgraded_engine.begin() as conn:
            rows = conn.exec_driver_sql(
                "SELECT user_id, token FROM sessions ORDER BY user_id"
            ).fetchall()
        by_user = {r[0]: json.loads(r[1]) for r in rows}
        assert by_user["u-alice"]["dek_ref"]["tenant_id"] == "t-acme"
        assert by_user["u-bob"]["dek_ref"]["tenant_id"] == "t-beta"
        # Orphan session (user since deleted) falls back to t-default.
        assert by_user["u-deleted"]["dek_ref"]["tenant_id"] == "t-default"


# ─── Group 4: idempotency ────────────────────────────────────────────


class TestIdempotency:
    def test_second_upgrade_is_noop(self, monkeypatch, m0189, tmp_path) -> None:
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "fx-11-1-idem-key")
        from backend import secret_store

        secret_store._reset_for_tests()

        db_path = tmp_path / "fx111-idem.db"
        engine = create_engine(f"sqlite:///{db_path}")
        _bootstrap_pre_0189_schema(engine)
        _seed(
            engine,
            users=[("u-x", "t-x")],
            sessions=[("plain-token-xxxxxxxx", "u-x")],
        )

        from alembic import op as alembic_op

        # First upgrade: encrypts the row.
        conn = engine.connect()
        txn = conn.begin()
        monkeypatch.setattr(alembic_op, "get_bind", lambda: conn)
        m0189.upgrade()
        txn.commit()
        conn.close()

        with engine.begin() as conn:
            after_first = conn.exec_driver_sql(
                "SELECT token, token_lookup_index FROM sessions"
            ).fetchall()

        # Second upgrade: must not touch the already-encrypted row.
        conn = engine.connect()
        txn = conn.begin()
        monkeypatch.setattr(alembic_op, "get_bind", lambda: conn)
        m0189.upgrade()
        txn.commit()
        conn.close()

        with engine.begin() as conn:
            after_second = conn.exec_driver_sql(
                "SELECT token, token_lookup_index FROM sessions"
            ).fetchall()

        assert after_first == after_second, (
            "re-running 0189 must be a no-op once rows are envelope JSON"
        )
        engine.dispose()

    def test_mixed_state_only_encrypts_plaintext_rows(
        self, monkeypatch, m0189, tmp_path
    ) -> None:
        """Simulate an interrupted previous run: one row encrypted, one not."""

        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "fx-11-1-mixed-key")
        from backend import secret_store
        from backend.security import envelope as ks_envelope

        secret_store._reset_for_tests()

        db_path = tmp_path / "fx111-mixed.db"
        engine = create_engine(f"sqlite:///{db_path}")
        _bootstrap_pre_0189_schema(engine)

        # Pre-seed one row already in packed envelope shape (as if a
        # crashed prior run got to it) plus one still-plaintext row.
        # Add the lookup column up-front so the pre-seeded envelope row
        # can carry its hash (mirrors a half-completed prior run).
        ciphertext, dek_ref = ks_envelope.encrypt("preexisting-plain", "t-pre")
        prepacked = m0189._pack(ciphertext, dek_ref)
        prelookup = m0189._hash_token("preexisting-plain")
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE sessions ADD COLUMN token_lookup_index TEXT"
            )
            conn.exec_driver_sql(
                "INSERT OR IGNORE INTO users (id, tenant_id) VALUES (?, ?)",
                ("u-pre", "t-pre"),
            )
            conn.exec_driver_sql(
                "INSERT OR IGNORE INTO users (id, tenant_id) VALUES (?, ?)",
                ("u-fresh", "t-fresh"),
            )
            conn.exec_driver_sql(
                "INSERT INTO sessions (token, user_id, token_lookup_index) "
                "VALUES (?, ?, ?)",
                (prepacked, "u-pre", prelookup),
            )
            conn.exec_driver_sql(
                "INSERT INTO sessions (token, user_id) VALUES (?, ?)",
                ("still-plain-token", "u-fresh"),
            )

        from alembic import op as alembic_op

        conn = engine.connect()
        txn = conn.begin()
        monkeypatch.setattr(alembic_op, "get_bind", lambda: conn)
        m0189.upgrade()
        txn.commit()
        conn.close()

        with engine.begin() as conn:
            rows = {
                r[0]: (r[1], r[2])
                for r in conn.exec_driver_sql(
                    "SELECT user_id, token, token_lookup_index FROM sessions"
                ).fetchall()
            }
        # Pre-existing envelope row is unchanged.
        assert rows["u-pre"] == (prepacked, prelookup)
        # Fresh plaintext row is now packed envelope JSON.
        new_token, new_lookup = rows["u-fresh"]
        assert m0189._looks_like_packed_envelope(new_token)
        assert new_lookup == m0189._hash_token("still-plain-token")
        engine.dispose()


# ─── Group 5: PG branch surface (text-only assertion) ────────────────


class TestPgBranchEmitsAddColumnIfNotExists:
    def test_pg_add_column_form(self, monkeypatch, m0189) -> None:
        # We can't run the real PG ALTER on SQLite; assert the dialect
        # branch picks the IF NOT EXISTS form so a re-run on an already
        # migrated PG DB does not error.
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgConn:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

                class _R:
                    def fetchall(self_inner):
                        return []

                return _R()

            def execute(self, *a, **k):
                return None

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgConn())

        # Stub the envelope import so we never call the real KMS adapter
        # in this dialect-only assertion.
        import backend.security.envelope as ks_envelope

        captured_calls = []

        def _fake_encrypt(plain, tid, **_):
            captured_calls.append((plain, tid))
            return ("env-json", ks_envelope.TenantDEKRef(
                dek_id="d", tenant_id=tid, provider="local-fernet",
                key_id="k", wrapped_dek_b64="d3JhcA==",
                wrap_algorithm="fernet",
            ))

        monkeypatch.setattr(ks_envelope, "encrypt", _fake_encrypt)
        m0189.upgrade()

        joined = "\n".join(captured)
        assert "ADD COLUMN IF NOT EXISTS token_lookup_index" in joined
        assert "CREATE UNIQUE INDEX IF NOT EXISTS" in joined
