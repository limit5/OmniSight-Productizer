"""OP-1178 — 0203a_kse must not trip ``idx_api_keys_lookup`` when the
table holds historical pre-Task-#106 duplicate api_keys rows.

Forensic background lives in
``docs/audit/2026-05-17-op-1177-api-keys-0203a-trip.md``. Short version:
pre-2026-04-21 ``migrate_legacy_bearer`` (backend/api_keys.py:288-340)
raced across uvicorn workers and inserted one ``ak-<random-uuid>`` row
per worker per legacy bearer secret. Task #106 made the id deterministic
(``ak-legacy-<sha256(secret)[:12]>``) but did NOT dedupe the historical
stubs. When ``_backfill_api_keys`` walks the table, every row that
encodes the same secret resolves to the same ``key_lookup_index``
value (``sha256(legacy_secret)``) and the second UPDATE trips the
``UNIQUE`` index this same migration just created.

These tests pin the dedupe contract:

* canonical ``ak-legacy-<sha[:12]>`` row survives,
* pre-#106 stub row(s) for the same secret are removed,
* the migration completes and the canonical row ends up in carrier form
  with its lookup populated,
* the dedupe is silent (no rows deleted) on tables without duplicates,
* the dedupe declines to act on duplicate groups that don't contain a
  canonical row — the migration must trip loudly in that case so the
  operator investigates (per the safety guard documented in
  ``_dedupe_legacy_bearer_duplicates``).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0203A = (
    BACKEND_ROOT
    / "alembic"
    / "versions"
    / "0203a_kse_envelope_credential_backfill.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0203a():
    return _load_module(MIGRATION_0203A, "_alembic_test_0203a_kse")


def _sha256_hex(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _bootstrap_pre_0203a_schema(engine) -> None:
    """Pre-migration ``api_keys`` shape (no ``key_lookup_index`` column).

    The migration's ``_ensure_lookup_columns`` adds the column + UNIQUE
    index; the dedupe under test runs after that, so we deliberately omit
    the column here.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE api_keys (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL DEFAULT '',
                tenant_id       TEXT NOT NULL DEFAULT 't-default',
                key_hash        TEXT NOT NULL,
                key_prefix      TEXT NOT NULL DEFAULT '',
                scopes          TEXT NOT NULL DEFAULT '["*"]',
                created_by      TEXT NOT NULL DEFAULT 'system/migration',
                enabled         INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # The migration also touches provisioned_storage; minimal stub so
        # upgrade() doesn't blow up on the unrelated table.
        conn.exec_driver_sql(
            """
            CREATE TABLE provisioned_storage (
                tenant_id   TEXT NOT NULL,
                provider    TEXT NOT NULL,
                bucket_name TEXT NOT NULL,
                PRIMARY KEY (tenant_id, provider)
            )
            """
        )
        # Other tables that upgrade() touches — empty is fine because the
        # backfill helpers just iterate fetchall() and noop on zero rows.
        for ddl in (
            "CREATE TABLE users (id TEXT PRIMARY KEY, tenant_id TEXT)",
            "CREATE TABLE oauth_tokens (user_id TEXT, provider TEXT, "
            "access_token_enc TEXT, refresh_token_enc TEXT, key_version INTEGER, "
            "PRIMARY KEY (user_id, provider))",
            "CREATE TABLE tenant_secrets (id TEXT PRIMARY KEY, tenant_id TEXT, "
            "secret_type TEXT, key_name TEXT, encrypted_value TEXT)",
            "CREATE TABLE git_accounts (id TEXT PRIMARY KEY, tenant_id TEXT, "
            "encrypted_token TEXT, encrypted_ssh_key TEXT, "
            "encrypted_webhook_secret TEXT)",
            "CREATE TABLE llm_credentials (id TEXT PRIMARY KEY, tenant_id TEXT, "
            "encrypted_value TEXT)",
            "CREATE TABLE provisioned_databases (tenant_id TEXT, provider TEXT, "
            "connection_url_enc TEXT, PRIMARY KEY (tenant_id, provider))",
        ):
            conn.exec_driver_sql(ddl)


@pytest.fixture()
def fresh_engine(monkeypatch, tmp_path):
    """File-backed SQLite + the pre-migration ``api_keys`` shape."""
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "op-1178-test-secret-key")
    from backend import secret_store

    secret_store._reset_for_tests()

    db_path = tmp_path / "op1178.db"
    engine = create_engine(f"sqlite:///{db_path}")
    _bootstrap_pre_0203a_schema(engine)
    yield engine
    engine.dispose()


def _run_upgrade(monkeypatch, m0203a, engine) -> None:
    from alembic import op as alembic_op

    conn = engine.connect()
    txn = conn.begin()
    monkeypatch.setattr(alembic_op, "get_bind", lambda: conn)
    try:
        m0203a.upgrade()
        txn.commit()
    except Exception:
        txn.rollback()
        raise
    finally:
        conn.close()


# ─── Helper-level tests ──────────────────────────────────────────────


class TestWouldBeLookup:
    """``_would_be_lookup`` must return exactly what the main loop
    would write to ``key_lookup_index``, across all three branches."""

    def test_returns_populated_lookup_when_present(self, m0203a) -> None:
        assert m0203a._would_be_lookup("anything", "preexisting-sha") == (
            "preexisting-sha"
        )

    def test_returns_bare_key_hash_for_legacy_plaintext(self, m0203a) -> None:
        # Mirrors the loop's ``:lookup`` = old ``key_hash`` write
        # (0203a_kse_envelope_credential_backfill.py:158).
        sha = _sha256_hex("legacy-secret")
        assert m0203a._would_be_lookup(sha, None) == sha

    def test_returns_tok_from_carrier_payload(self, monkeypatch, m0203a) -> None:
        sha = _sha256_hex("legacy-secret")
        # Stub the envelope unpacker so this test doesn't depend on KMS
        # state — the contract under test is just "extract tok".
        binding_json = json.dumps(
            {"fmt": 1, "ctx": {"table": "api_keys", "id": "ak-x"}, "tok": sha},
            sort_keys=True, separators=(",", ":"),
        )
        carrier_json = json.dumps(
            {"fmt": 1, "ciphertext": "x", "dek_ref": {"d": 1}}
        )
        monkeypatch.setattr(m0203a, "_unpack_ks_payload", lambda _v: binding_json)
        assert m0203a._would_be_lookup(carrier_json, None) == sha

    def test_returns_none_when_carrier_unpack_fails(
        self, monkeypatch, m0203a
    ) -> None:
        # If we can't decode the carrier we must NOT guess a lookup; the
        # main loop also bails (``skipped += 1``) on this row.
        carrier_json = json.dumps(
            {"fmt": 1, "ciphertext": "x", "dek_ref": {"d": 1}}
        )

        def _boom(_v):
            raise ValueError("synthetic")

        monkeypatch.setattr(m0203a, "_unpack_ks_payload", _boom)
        assert m0203a._would_be_lookup(carrier_json, None) is None


# ─── Functional dedupe tests ─────────────────────────────────────────


class TestDedupeOP1177Scenario:
    """Exact OP-1177 row shape: canonical + pre-#106 stub for the same
    legacy secret. Migration must dedupe and finish."""

    def _seed_op1177(self, engine) -> tuple[str, str, str]:
        legacy = "decision-bearer-secret-op-1178"
        sha = _sha256_hex(legacy)
        canonical_id = f"ak-legacy-{sha[:12]}"
        stub_id = "ak-deadbeef99"  # pre-#106 uuid-derived shape
        with engine.begin() as conn:
            # Canonical row: pre-migration form (bare sha256 in key_hash,
            # no lookup column yet — it's added by _ensure_lookup_columns).
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
                "VALUES (?, 'legacy-bearer', ?, ?)",
                (canonical_id, sha, legacy[:8]),
            )
            # Stub row: same secret, different id (the pre-#106 race output).
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
                "VALUES (?, 'legacy-bearer', ?, ?)",
                (stub_id, sha, legacy[:8]),
            )
        return canonical_id, stub_id, sha

    def test_migration_completes_without_unique_violation(
        self, monkeypatch, m0203a, fresh_engine
    ) -> None:
        self._seed_op1177(fresh_engine)
        # The bug pre-patch: this call raises IntegrityError on
        # ``idx_api_keys_lookup``. Post-patch it must succeed.
        _run_upgrade(monkeypatch, m0203a, fresh_engine)

    def test_stub_row_deleted_canonical_preserved(
        self, monkeypatch, m0203a, fresh_engine
    ) -> None:
        canonical_id, stub_id, _ = self._seed_op1177(fresh_engine)
        _run_upgrade(monkeypatch, m0203a, fresh_engine)
        with fresh_engine.begin() as conn:
            ids = {
                r[0] for r in conn.exec_driver_sql(
                    "SELECT id FROM api_keys"
                ).fetchall()
            }
        assert canonical_id in ids
        assert stub_id not in ids

    def test_canonical_row_ends_up_in_carrier_form_with_lookup(
        self, monkeypatch, m0203a, fresh_engine
    ) -> None:
        canonical_id, _, sha = self._seed_op1177(fresh_engine)
        _run_upgrade(monkeypatch, m0203a, fresh_engine)
        with fresh_engine.begin() as conn:
            row = conn.exec_driver_sql(
                "SELECT key_hash, key_lookup_index FROM api_keys "
                "WHERE id = ?",
                (canonical_id,),
            ).fetchone()
        assert row is not None
        key_hash, lookup = row
        assert m0203a._looks_like_carrier(key_hash), (
            "canonical row must be repackaged into KS carrier form"
        )
        assert lookup == sha, (
            "canonical row's key_lookup_index must equal sha256(legacy_secret)"
        )


class TestDedupeIsSilentWithoutDuplicates:
    """A table with no duplicates must see zero rows deleted."""

    def test_no_dedupe_on_distinct_secrets(
        self, monkeypatch, m0203a, fresh_engine
    ) -> None:
        sha_a = _sha256_hex("secret-a")
        sha_b = _sha256_hex("secret-b")
        with fresh_engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
                "VALUES (?, 'a', ?, ?)",
                (f"ak-legacy-{sha_a[:12]}", sha_a, "secret-a"),
            )
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
                "VALUES (?, 'b', ?, ?)",
                (f"ak-legacy-{sha_b[:12]}", sha_b, "secret-b"),
            )
        _run_upgrade(monkeypatch, m0203a, fresh_engine)
        with fresh_engine.begin() as conn:
            ids = {
                r[0] for r in conn.exec_driver_sql(
                    "SELECT id FROM api_keys"
                ).fetchall()
            }
        assert ids == {f"ak-legacy-{sha_a[:12]}", f"ak-legacy-{sha_b[:12]}"}


class TestDedupeDeclinesWithoutCanonical:
    """Duplicate group with no canonical ``ak-legacy-<sha[:12]>`` row:
    the dedupe must NOT delete anything — the migration trips loudly so
    an operator can investigate (see the policy comment in
    ``_dedupe_legacy_bearer_duplicates``)."""

    def test_no_canonical_means_migration_still_trips(
        self, monkeypatch, m0203a, fresh_engine
    ) -> None:
        legacy = "unknown-shape-duplicate"
        sha = _sha256_hex(legacy)
        with fresh_engine.begin() as conn:
            # Both ids look like pre-#106 stubs — neither matches
            # ``ak-legacy-<sha[:12]>``. Dedupe declines.
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
                "VALUES (?, 'x', ?, ?)",
                ("ak-aaaaaaaa01", sha, legacy[:8]),
            )
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
                "VALUES (?, 'y', ?, ?)",
                ("ak-bbbbbbbb02", sha, legacy[:8]),
            )
        with pytest.raises(IntegrityError):
            _run_upgrade(monkeypatch, m0203a, fresh_engine)

    def test_no_rows_deleted_when_dedupe_declines(
        self, monkeypatch, m0203a, fresh_engine
    ) -> None:
        legacy = "unknown-shape-duplicate-2"
        sha = _sha256_hex(legacy)
        with fresh_engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
                "VALUES (?, 'x', ?, ?)",
                ("ak-cccccccc01", sha, legacy[:8]),
            )
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
                "VALUES (?, 'y', ?, ?)",
                ("ak-dddddddd02", sha, legacy[:8]),
            )
        # Drive the dedupe helper directly so we can observe its return
        # value and the table state without depending on the (expected)
        # trip from the surrounding upgrade.
        from alembic import op as alembic_op

        conn = fresh_engine.connect()
        txn = conn.begin()
        monkeypatch.setattr(alembic_op, "get_bind", lambda: conn)
        try:
            m0203a._ensure_lookup_columns(conn)
            deleted = m0203a._dedupe_legacy_bearer_duplicates(conn)
            txn.commit()
        finally:
            conn.close()
        assert deleted == 0
        with fresh_engine.begin() as conn2:
            ids = {
                r[0] for r in conn2.exec_driver_sql(
                    "SELECT id FROM api_keys"
                ).fetchall()
            }
        assert ids == {"ak-cccccccc01", "ak-dddddddd02"}


class TestDedupeHandlesMixedPrePostMigrationDuplicates:
    """The OP-1177 scenario also covers the 'one already-migrated,
    other still-plaintext' case — Row B in the audit has its
    ``key_lookup_index`` already populated from a partial earlier run.
    The dedupe must collapse that pair too."""

    def test_canonical_unmigrated_plus_stub_already_migrated(
        self, monkeypatch, m0203a, fresh_engine
    ) -> None:
        legacy = "partial-prior-run-secret"
        sha = _sha256_hex(legacy)
        canonical_id = f"ak-legacy-{sha[:12]}"
        stub_id = "ak-eeeeeeee03"
        # _ensure_lookup_columns has not run yet, but for this scenario
        # we need a row whose lookup is already populated. Add the
        # column up-front (mirroring "a prior partial run already
        # created the column and wrote one row").
        with fresh_engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE api_keys ADD COLUMN key_lookup_index TEXT"
            )
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix) "
                "VALUES (?, 'legacy-bearer', ?, ?)",
                (canonical_id, sha, legacy[:8]),
            )
            conn.exec_driver_sql(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix, "
                "key_lookup_index) VALUES (?, 'legacy-bearer', ?, ?, ?)",
                (stub_id, sha, legacy[:8], sha),
            )
        _run_upgrade(monkeypatch, m0203a, fresh_engine)
        with fresh_engine.begin() as conn:
            rows = conn.exec_driver_sql(
                "SELECT id, key_lookup_index FROM api_keys"
            ).fetchall()
        ids = {r[0] for r in rows}
        assert ids == {canonical_id}
        assert rows[0][1] == sha
