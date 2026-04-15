"""K6 — API key per-key bearer token tests.

Covers: key CRUD, scope restriction, revoke effectiveness,
legacy env migration, and audit trail integration.
"""

import asyncio
import os
import sys
import time

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
    os.environ.pop("OMNISIGHT_DECISION_BEARER", None)
    from backend.config import settings
    settings.database_path = db_path
    from backend import db
    db._DB_PATH = tmp_path / "test.db"
    db._db = None
    _run(db.init())
    yield
    _run(db.close())


class TestApiKeyCreate:
    def test_create_key_returns_secret(self):
        from backend import api_keys
        key, secret = _run(api_keys.create_key("test-key", created_by="admin@test.com"))
        assert key.name == "test-key"
        assert key.id.startswith("ak-")
        assert secret.startswith("omni_")
        assert key.enabled is True
        assert key.scopes == ["*"]

    def test_create_key_with_scopes(self):
        from backend import api_keys
        key, _ = _run(api_keys.create_key("scoped-key", scopes=["/profile", "/audit"]))
        assert key.scopes == ["/profile", "/audit"]

    def test_create_key_records_creator(self):
        from backend import api_keys
        key, _ = _run(api_keys.create_key("by-admin", created_by="admin@example.com"))
        assert key.created_by == "admin@example.com"


class TestApiKeyValidation:
    def test_validate_correct_key(self):
        from backend import api_keys
        key, secret = _run(api_keys.create_key("validate-test"))
        validated = _run(api_keys.validate_bearer(secret, ip="127.0.0.1"))
        assert validated is not None
        assert validated.id == key.id
        assert validated.last_used_ip == "127.0.0.1"

    def test_validate_wrong_key_returns_none(self):
        from backend import api_keys
        _run(api_keys.create_key("real-key"))
        validated = _run(api_keys.validate_bearer("omni_wrong_secret_value"))
        assert validated is None

    def test_validate_updates_last_used(self):
        from backend import api_keys
        key, secret = _run(api_keys.create_key("track-usage"))
        before = time.time()
        _run(api_keys.validate_bearer(secret, ip="10.0.0.1"))
        after = time.time()
        refreshed = _run(api_keys.get_key(key.id))
        assert refreshed.last_used_at is not None
        assert before <= refreshed.last_used_at <= after
        assert refreshed.last_used_ip == "10.0.0.1"


class TestApiKeyRevoke:
    def test_revoke_key_prevents_validation(self):
        from backend import api_keys
        key, secret = _run(api_keys.create_key("revoke-me"))
        assert _run(api_keys.validate_bearer(secret)) is not None
        _run(api_keys.revoke_key(key.id))
        assert _run(api_keys.validate_bearer(secret)) is None

    def test_revoke_is_immediate(self):
        from backend import api_keys
        key, secret = _run(api_keys.create_key("instant-revoke"))
        _run(api_keys.revoke_key(key.id))
        result = _run(api_keys.validate_bearer(secret))
        assert result is None

    def test_enable_after_revoke(self):
        from backend import api_keys
        key, secret = _run(api_keys.create_key("re-enable"))
        _run(api_keys.revoke_key(key.id))
        assert _run(api_keys.validate_bearer(secret)) is None
        _run(api_keys.enable_key(key.id))
        assert _run(api_keys.validate_bearer(secret)) is not None


class TestApiKeyRotation:
    def test_rotate_invalidates_old_secret(self):
        from backend import api_keys
        key, old_secret = _run(api_keys.create_key("rotate-me"))
        rotated, new_secret = _run(api_keys.rotate_key(key.id))
        assert rotated is not None
        assert new_secret != old_secret
        assert _run(api_keys.validate_bearer(old_secret)) is None
        assert _run(api_keys.validate_bearer(new_secret)) is not None

    def test_rotate_nonexistent_returns_none(self):
        from backend import api_keys
        key, secret = _run(api_keys.rotate_key("ak-nonexistent"))
        assert key is None
        assert secret == ""


class TestApiKeyScopes:
    def test_wildcard_scope_allows_all(self):
        from backend import api_keys
        key, _ = _run(api_keys.create_key("wildcard"))
        assert key.scope_allows("/profile") is True
        assert key.scope_allows("/audit") is True
        assert key.scope_allows("/anything") is True

    def test_restricted_scope_blocks_unallowed(self):
        from backend import api_keys
        key, _ = _run(api_keys.create_key("limited", scopes=["/profile", "/audit"]))
        assert key.scope_allows("/profile") is True
        assert key.scope_allows("/profile/sub") is True
        assert key.scope_allows("/audit") is True
        assert key.scope_allows("/users") is False
        assert key.scope_allows("/api-keys") is False

    def test_update_scopes(self):
        from backend import api_keys
        key, _ = _run(api_keys.create_key("update-scopes", scopes=["*"]))
        _run(api_keys.update_scopes(key.id, ["/profile"]))
        refreshed = _run(api_keys.get_key(key.id))
        assert refreshed.scopes == ["/profile"]
        assert refreshed.scope_allows("/profile") is True
        assert refreshed.scope_allows("/users") is False


class TestApiKeyList:
    def test_list_returns_all_keys(self):
        from backend import api_keys
        _run(api_keys.create_key("key-1"))
        _run(api_keys.create_key("key-2"))
        _run(api_keys.create_key("key-3"))
        keys = _run(api_keys.list_keys())
        names = [k.name for k in keys]
        assert "key-1" in names
        assert "key-2" in names
        assert "key-3" in names


class TestApiKeyDelete:
    def test_delete_removes_key(self):
        from backend import api_keys
        key, secret = _run(api_keys.create_key("delete-me"))
        _run(api_keys.delete_key(key.id))
        assert _run(api_keys.get_key(key.id)) is None
        assert _run(api_keys.validate_bearer(secret)) is None


class TestLegacyBearerMigration:
    def test_migration_creates_legacy_key(self):
        from backend import api_keys
        os.environ["OMNISIGHT_DECISION_BEARER"] = "my-old-secret-token"
        try:
            migrated = _run(api_keys.migrate_legacy_bearer())
            assert migrated is not None
            assert migrated.name == "legacy-bearer"
            assert migrated.scopes == ["*"]
            validated = _run(api_keys.validate_bearer("my-old-secret-token"))
            assert validated is not None
            assert validated.id == migrated.id
        finally:
            os.environ.pop("OMNISIGHT_DECISION_BEARER", None)

    def test_migration_idempotent(self):
        from backend import api_keys
        os.environ["OMNISIGHT_DECISION_BEARER"] = "my-old-secret-token"
        try:
            first = _run(api_keys.migrate_legacy_bearer())
            assert first is not None
            second = _run(api_keys.migrate_legacy_bearer())
            assert second is None
        finally:
            os.environ.pop("OMNISIGHT_DECISION_BEARER", None)

    def test_no_migration_without_env(self):
        from backend import api_keys
        os.environ.pop("OMNISIGHT_DECISION_BEARER", None)
        result = _run(api_keys.migrate_legacy_bearer())
        assert result is None


class TestAuditSessionId:
    def test_key_session_id_format(self):
        """API key auth should produce session_id=bearer:<key_id> in the session."""
        from backend import api_keys
        key, _ = _run(api_keys.create_key("audit-trace"))
        expected_session_token = f"bearer:{key.id}"
        assert expected_session_token.startswith("bearer:ak-")
