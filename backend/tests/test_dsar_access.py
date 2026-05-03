"""SC.10.2 -- DSAR access endpoint contract."""
from __future__ import annotations

import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from backend import auth as _au
from backend.routers import privacy as _privacy

pytestmark = pytest.mark.asyncio


def _user(user_id: str, email: str, tenant_id: str = "t-dsar") -> _au.User:
    return _au.User(
        id=user_id,
        email=email,
        name=email.split("@", 1)[0],
        role="viewer",
        tenant_id=tenant_id,
    )


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class _FakeConn:
    def __init__(self):
        self.insert_args = None

    def transaction(self):
        return _FakeTx()

    async def fetchrow(self, sql: str, *args):
        if "FROM users" in sql:
            return {
                "id": "u-dsar-alice",
                "email": "alice-dsar@example.test",
                "name": "Alice DSAR",
                "role": "viewer",
                "enabled": 1,
                "must_change_password": 0,
                "created_at": "2026-05-03 00:00:00",
                "last_login_at": None,
                "tenant_id": "t-dsar",
                "auth_methods": '["password"]',
            }
        return None

    async def fetch(self, sql: str, *args):
        if "FROM user_preferences" in sql:
            return [{
                "pref_key": "locale",
                "value": "en-US",
                "updated_at": 1.0,
                "tenant_id": "t-dsar",
                "project_id": None,
            }]
        if "FROM user_drafts" in sql:
            return [{
                "slot_key": "chat:main",
                "content": "alice draft",
                "updated_at": 1.0,
                "tenant_id": "t-dsar",
            }]
        if "FROM oauth_tokens" in sql:
            return [{
                "provider": "github",
                "expires_at": 2.0,
                "scope": "repo",
                "key_version": 1,
                "created_at": 1.0,
                "updated_at": 1.0,
                "version": 0,
            }]
        return []

    async def execute(self, sql: str, *args):
        assert "INSERT INTO dsar_requests" in sql
        self.insert_args = args
        return "INSERT 0 1"


async def test_access_handler_smoke_uses_pool_and_redacted_shape(monkeypatch):
    conn = _FakeConn()
    from backend import db_pool as _db_pool

    monkeypatch.setattr(_db_pool, "_pool", _FakePool(conn))

    body = await _privacy.create_access_request(
        _user("u-dsar-alice", "alice-dsar@example.test")
    )

    assert body["request"]["status"] == "completed"
    assert body["data"]["profile"]["id"] == "u-dsar-alice"
    assert body["data"]["preferences"][0]["value"] == "en-US"
    assert body["data"]["oauth_connections"][0]["provider"] == "github"
    assert body["result"]["category_counts"]["profile"] == 1
    assert body["result"]["category_counts"]["preferences"] == 1
    assert conn.insert_args is not None
    assert conn.insert_args[2] == "u-dsar-alice"

    encoded = json.dumps(body, sort_keys=True)
    assert "password_hash" not in encoded
    assert "access_token_enc" not in encoded
    assert "refresh_token_enc" not in encoded


@pytest.fixture
async def _privacy_client(pg_test_pool, pg_test_dsn, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    from backend import bootstrap as _boot
    from backend import db as _db
    from backend.main import app

    alice = _user("u-dsar-alice", "alice-dsar@example.test")

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    async def _current_user():
        return alice

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()
    app.dependency_overrides[_au.current_user] = _current_user

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE dsar_requests, oauth_tokens, password_history, "
            "mfa_backup_codes, user_mfa, api_keys, user_drafts, "
            "user_preferences, chat_sessions, chat_messages, sessions, "
            "project_memberships, projects, user_tenant_memberships, users, "
            "tenants RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "('t-dsar', 'DSAR Tenant', 'pro'), "
            "('t-other', 'Other Tenant', 'pro')"
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id, auth_methods) VALUES "
            "($1, $2, 'Alice DSAR', 'viewer', 'hash-secret-alice', 1, "
            "$3, '[\"password\"]'), "
            "('u-dsar-bob', 'bob-dsar@example.test', 'Bob DSAR', 'viewer', "
            "'hash-secret-bob', 1, 't-other', '[\"password\"]')",
            alice.id,
            alice.email,
            alice.tenant_id,
        )
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "(user_id, tenant_id, role, status) VALUES "
            "($1, $2, 'viewer', 'active')",
            alice.id,
            alice.tenant_id,
        )
        await conn.execute(
            "INSERT INTO user_preferences "
            "(user_id, pref_key, value, updated_at, tenant_id) VALUES "
            "($1, 'locale', 'en-US', $2, $3), "
            "('u-dsar-bob', 'locale', 'fr-FR', $2, 't-other')",
            alice.id,
            time.time(),
            alice.tenant_id,
        )
        await conn.execute(
            "INSERT INTO user_drafts "
            "(user_id, slot_key, content, updated_at, tenant_id) VALUES "
            "($1, 'chat:main', 'alice draft', $2, $3), "
            "('u-dsar-bob', 'chat:main', 'bob draft', $2, 't-other')",
            alice.id,
            time.time(),
            alice.tenant_id,
        )
        await conn.execute(
            "INSERT INTO chat_messages "
            "(id, user_id, session_id, role, content, timestamp, tenant_id) "
            "VALUES ('msg-a', $1, 's-a', 'user', 'hello from alice', $2, $3), "
            "('msg-b', 'u-dsar-bob', 's-b', 'user', 'hello from bob', $2, "
            "'t-other')",
            alice.id,
            time.time(),
            alice.tenant_id,
        )
        await conn.execute(
            "INSERT INTO chat_sessions "
            "(session_id, user_id, tenant_id, metadata, created_at, updated_at) "
            "VALUES ('s-a', $1, $2, $3::jsonb, $4, $4)",
            alice.id,
            alice.tenant_id,
            json.dumps({"auto_title": "Alice"}),
            time.time(),
        )
        await conn.execute(
            "INSERT INTO sessions "
            "(token, user_id, csrf_token, created_at, expires_at, "
            "last_seen_at, ip, user_agent, ua_hash, metadata, mfa_verified) "
            "VALUES ('session-token-secret', $1, 'csrf-secret', $2, $3, "
            "$2, '192.0.2.10', 'pytest-agent', 'ua-hash', '{}', 1)",
            alice.id,
            time.time(),
            time.time() + 3600,
        )
        await conn.execute(
            "INSERT INTO user_mfa "
            "(id, user_id, method, secret, credential, name, verified) "
            "VALUES ('mfa-a', $1, 'totp', 'totp-secret', '', 'Phone', 1)",
            alice.id,
        )
        await conn.execute(
            "INSERT INTO mfa_backup_codes (user_id, code_hash, used) "
            "VALUES ($1, 'backup-hash-secret', 0)",
            alice.id,
        )
        await conn.execute(
            "INSERT INTO password_history (user_id, password_hash) "
            "VALUES ($1, 'old-password-secret')",
            alice.id,
        )
        await conn.execute(
            "INSERT INTO projects "
            "(id, tenant_id, product_line, name, slug, created_by) "
            "VALUES ('p-alice', $1, 'default', 'Alice Project', "
            "'alice-project', $2)",
            alice.tenant_id,
            alice.id,
        )
        await conn.execute(
            "INSERT INTO api_keys "
            "(id, name, key_hash, key_prefix, scopes, created_by, created_at) "
            "VALUES ('ak-alice', 'Alice Key', 'api-key-hash-secret', "
            "'ak_live_1234', '[\"read\"]', $1, $2)",
            alice.id,
            time.time(),
        )
        await conn.execute(
            "INSERT INTO oauth_tokens "
            "(user_id, provider, access_token_enc, refresh_token_enc, "
            "expires_at, scope, key_version, created_at, updated_at) "
            "VALUES ($1, 'github', 'access-ciphertext-secret', "
            "'refresh-ciphertext-secret', $2, 'repo', 1, $3, $3)",
            alice.id,
            time.time() + 3600,
            time.time(),
        )

    if _db._db is not None:
        await _db.close()
    await _db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        _boot._gate_cache_reset()
        await _db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE dsar_requests, oauth_tokens, password_history, "
                "mfa_backup_codes, user_mfa, api_keys, user_drafts, "
                "user_preferences, chat_sessions, chat_messages, sessions, "
                "project_memberships, projects, user_tenant_memberships, "
                "users, tenants RESTART IDENTITY CASCADE"
            )


async def test_access_endpoint_returns_only_current_user_data(
    _privacy_client: AsyncClient,
):
    resp = await _privacy_client.post("/api/v1/privacy/access")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["request"]["type"] == "access"
    assert body["request"]["status"] == "completed"
    assert body["request"]["id"].startswith("dsar-access-")
    assert body["request"]["due_at"] - body["request"]["requested_at"] == pytest.approx(
        30 * 24 * 60 * 60,
        rel=1e-6,
    )

    data = body["data"]
    assert data["profile"]["id"] == "u-dsar-alice"
    assert data["preferences"][0]["value"] == "en-US"
    assert data["drafts"][0]["content"] == "alice draft"
    assert data["chat_messages"][0]["content"] == "hello from alice"
    assert data["projects_created"][0]["id"] == "p-alice"
    assert data["api_keys_created"][0]["id"] == "ak-alice"
    assert data["oauth_connections"][0]["provider"] == "github"

    encoded = json.dumps(body, sort_keys=True)
    assert "u-dsar-bob" not in encoded
    assert "bob draft" not in encoded
    assert "hash-secret" not in encoded
    assert "session-token-secret" not in encoded
    assert "csrf-secret" not in encoded
    assert "totp-secret" not in encoded
    assert "backup-hash-secret" not in encoded
    assert "old-password-secret" not in encoded
    assert "api-key-hash-secret" not in encoded
    assert "ciphertext-secret" not in encoded


async def test_access_endpoint_records_completed_dsar_request(
    _privacy_client: AsyncClient,
    pg_test_pool,
):
    resp = await _privacy_client.post("/api/v1/privacy/access")
    assert resp.status_code == 200, resp.text
    request_id = resp.json()["request"]["id"]

    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, tenant_id, user_id, request_type, status, "
            "payload_json, result_json, error "
            "FROM dsar_requests WHERE id = $1",
            request_id,
        )

    assert row is not None
    assert row["tenant_id"] == "t-dsar"
    assert row["user_id"] == "u-dsar-alice"
    assert row["request_type"] == "access"
    assert row["status"] == "completed"
    assert row["error"] == ""
    assert row["payload_json"]["source"] == "privacy_access_endpoint"
    assert row["result_json"]["category_counts"]["profile"] == 1
    assert row["result_json"]["category_counts"]["preferences"] == 1
