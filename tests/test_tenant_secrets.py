"""I4 — Tenant-scoped secrets tests.

Covers: tenant_secrets table, CRUD operations, tenant isolation,
encryption/decryption round-trip, and API key tenant scoping.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OMNISIGHT_AUTH_MODE", "session")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    from backend.config import settings
    settings.database_path = db_path
    from backend import db
    db._DB_PATH = tmp_path / "test.db"
    db._db = None
    _run(db.init())
    from backend.secret_store import _reset_for_tests
    _reset_for_tests()
    yield db
    _run(db.close())


DEFAULT_TENANT = "t-default"


class TestTenantSecretsTable:
    def test_table_exists(self, _setup_db):
        db = _setup_db
        async def check():
            async with db._conn().execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tenant_secrets'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
        _run(check())

    def test_table_columns(self, _setup_db):
        db = _setup_db
        async def check():
            async with db._conn().execute("PRAGMA table_info(tenant_secrets)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            expected = {"id", "tenant_id", "secret_type", "key_name",
                        "encrypted_value", "metadata", "created_at", "updated_at"}
            assert expected.issubset(cols)
        _run(check())

    def test_unique_constraint(self, _setup_db):
        db = _setup_db
        async def check():
            from backend.secret_store import encrypt
            enc = encrypt("test-value")
            await db._conn().execute(
                "INSERT INTO tenant_secrets (id, tenant_id, secret_type, key_name, encrypted_value) "
                "VALUES (?, ?, ?, ?, ?)",
                ("s1", DEFAULT_TENANT, "provider_key", "openai", enc),
            )
            await db._conn().commit()
            with pytest.raises(Exception):
                await db._conn().execute(
                    "INSERT INTO tenant_secrets (id, tenant_id, secret_type, key_name, encrypted_value) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("s2", DEFAULT_TENANT, "provider_key", "openai", enc),
                )
        _run(check())


class TestSecretsAPI:
    def test_upsert_and_list(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(DEFAULT_TENANT)

        async def check():
            sid = await sec.upsert_secret("my-key", "super-secret", "provider_key")
            assert sid.startswith("sec-")

            items = await sec.list_secrets()
            assert len(items) == 1
            assert items[0]["key_name"] == "my-key"
            assert items[0]["secret_type"] == "provider_key"
            assert items[0]["fingerprint"] != "super-secret"
            assert "cret" in items[0]["fingerprint"]
        _run(check())

    def test_get_secret_value(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(DEFAULT_TENANT)

        async def check():
            sid = await sec.upsert_secret("api-token", "my-api-token-12345", "provider_key")
            val = await sec.get_secret_value(sid)
            assert val == "my-api-token-12345"
        _run(check())

    def test_get_by_name(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(DEFAULT_TENANT)

        async def check():
            await sec.upsert_secret("github_token", "ghp_abc123def456", "git_credential")
            val = await sec.get_secret_by_name("github_token", "git_credential")
            assert val == "ghp_abc123def456"
        _run(check())

    def test_upsert_updates_existing(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(DEFAULT_TENANT)

        async def check():
            sid1 = await sec.upsert_secret("rotate-me", "old-value", "provider_key")
            sid2 = await sec.upsert_secret("rotate-me", "new-value", "provider_key")
            assert sid1 == sid2
            val = await sec.get_secret_value(sid1)
            assert val == "new-value"
        _run(check())

    def test_delete(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(DEFAULT_TENANT)

        async def check():
            sid = await sec.upsert_secret("temp", "temp-value", "custom")
            deleted = await sec.delete_secret(sid)
            assert deleted is True
            val = await sec.get_secret_value(sid)
            assert val is None
        _run(check())

    def test_delete_nonexistent(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(DEFAULT_TENANT)

        async def check():
            deleted = await sec.delete_secret("nonexistent-id")
            assert deleted is False
        _run(check())

    def test_list_by_type(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(DEFAULT_TENANT)

        async def check():
            await sec.upsert_secret("key1", "val1", "provider_key")
            await sec.upsert_secret("key2", "val2", "git_credential")
            await sec.upsert_secret("key3", "val3", "provider_key")

            providers = await sec.list_secrets(secret_type="provider_key")
            assert len(providers) == 2
            git = await sec.list_secrets(secret_type="git_credential")
            assert len(git) == 1
        _run(check())

    def test_metadata(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(DEFAULT_TENANT)

        async def check():
            meta = {"provider": "github", "scope": "repo"}
            await sec.upsert_secret("gh-token", "ghp_xxx", "git_credential", metadata=meta)
            items = await sec.list_secrets()
            assert items[0]["metadata"]["provider"] == "github"
        _run(check())


class TestTenantIsolation:
    def test_secrets_isolated_between_tenants(self, _setup_db):
        db = _setup_db
        from backend.db_context import set_tenant_id
        from backend import secrets as sec

        async def check():
            await db._conn().execute(
                "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, ?)",
                ("t-acme", "Acme Corp", "professional"),
            )
            await db._conn().commit()

            set_tenant_id(DEFAULT_TENANT)
            await sec.upsert_secret("shared-name", "default-value", "provider_key")

            set_tenant_id("t-acme")
            await sec.upsert_secret("shared-name", "acme-value", "provider_key")

            set_tenant_id(DEFAULT_TENANT)
            val = await sec.get_secret_by_name("shared-name", "provider_key")
            assert val == "default-value"

            set_tenant_id("t-acme")
            val = await sec.get_secret_by_name("shared-name", "provider_key")
            assert val == "acme-value"
        _run(check())

    def test_cannot_delete_other_tenant_secret(self, _setup_db):
        db = _setup_db
        from backend.db_context import set_tenant_id
        from backend import secrets as sec

        async def check():
            await db._conn().execute(
                "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, ?)",
                ("t-other", "Other Corp", "starter"),
            )
            await db._conn().commit()

            set_tenant_id(DEFAULT_TENANT)
            sid = await sec.upsert_secret("my-secret", "val", "custom")

            set_tenant_id("t-other")
            deleted = await sec.delete_secret(sid)
            assert deleted is False

            set_tenant_id(DEFAULT_TENANT)
            val = await sec.get_secret_value(sid)
            assert val == "val"
        _run(check())

    def test_list_only_own_tenant_secrets(self, _setup_db):
        db = _setup_db
        from backend.db_context import set_tenant_id
        from backend import secrets as sec

        async def check():
            await db._conn().execute(
                "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, ?)",
                ("t-iso", "Isolated Corp", "enterprise"),
            )
            await db._conn().commit()

            set_tenant_id(DEFAULT_TENANT)
            await sec.upsert_secret("default-only", "v1", "provider_key")
            await sec.upsert_secret("default-git", "v2", "git_credential")

            set_tenant_id("t-iso")
            await sec.upsert_secret("iso-only", "v3", "cloudflare_token")

            items = await sec.list_secrets()
            assert len(items) == 1
            assert items[0]["key_name"] == "iso-only"

            set_tenant_id(DEFAULT_TENANT)
            items = await sec.list_secrets()
            assert len(items) == 2
        _run(check())

    def test_require_tenant_context(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(None)

        async def check():
            with pytest.raises(RuntimeError, match="No tenant_id"):
                await sec.list_secrets()
        _run(check())


class TestApiKeysTenantId:
    def test_api_keys_has_tenant_id(self, _setup_db):
        db = _setup_db
        async def check():
            async with db._conn().execute("PRAGMA table_info(api_keys)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            assert "tenant_id" in cols
        _run(check())

    def test_api_keys_default_tenant(self, _setup_db):
        db = _setup_db
        async def check():
            import hashlib
            key_hash = hashlib.sha256(b"test-key").hexdigest()
            await db._conn().execute(
                "INSERT INTO api_keys (id, name, key_hash, key_prefix, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                ("ak-1", "test", key_hash, "test", "admin"),
            )
            await db._conn().commit()
            async with db._conn().execute(
                "SELECT tenant_id FROM api_keys WHERE id = ?", ("ak-1",)
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == DEFAULT_TENANT
        _run(check())


class TestEncryptionRoundTrip:
    def test_encrypt_decrypt(self, _setup_db):
        from backend.db_context import set_tenant_id
        from backend import secrets as sec
        set_tenant_id(DEFAULT_TENANT)

        async def check():
            test_values = [
                "simple-key",
                "ghp_1234567890abcdef1234567890abcdef12345678",
                "sk-ant-api03-xxxx",
                "a" * 500,
                "special!@#$%^&*()_+{}|:<>?",
            ]
            for val in test_values:
                sid = await sec.upsert_secret(f"test-{hash(val)}", val, "custom")
                recovered = await sec.get_secret_value(sid)
                assert recovered == val, f"Round-trip failed for: {val[:20]}..."
        _run(check())
