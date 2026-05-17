from __future__ import annotations

import pytest

from backend import api_keys


class _Acquire:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_Conn":
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _Pool:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _Conn:
    def __init__(self) -> None:
        self.rows_by_lookup: dict[str, dict] = {}
        self.updates: list[tuple[str | None, float, str]] = []

    async def execute(self, sql: str, *args):
        if sql.startswith("INSERT INTO api_keys"):
            (
                key_id,
                name,
                _packed_hash,
                lookup,
                prefix,
                scopes,
                created_by,
                expires_at,
            ) = args
            self.rows_by_lookup[lookup] = {
                "id": key_id,
                "name": name,
                "key_prefix": prefix,
                "scopes": scopes,
                "created_by": created_by,
                "enabled": 1,
                "expires_at": expires_at,
                "created_at": "",
            }
            return "INSERT 0 1"
        if sql.startswith("UPDATE api_keys SET last_used_ip"):
            self.updates.append(args)
            return "UPDATE 1"
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetchrow(self, sql: str, *args):
        if sql.startswith("SELECT id, name, key_prefix"):
            return self.rows_by_lookup.get(args[0])
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_create_key_ttl_sets_expires_at(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(api_keys, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(api_keys.time, "time", lambda: 1000.0)

    key, raw = await api_keys.create_key(
        "short-lived", scopes=["a2a:*"], created_by="ops@example.com",
        ttl_seconds=60,
    )

    row = conn.rows_by_lookup[api_keys._hash_key(raw)]
    assert key.expires_at == 1060.0
    assert key.to_dict()["expires_at"] == 1060.0
    assert row["expires_at"] == 1060.0


@pytest.mark.asyncio
async def test_validate_bearer_rejects_expired_key_without_usage_update(monkeypatch):
    conn = _Conn()
    raw = "omni_expired-test-token"
    conn.rows_by_lookup[api_keys._hash_key(raw)] = {
        "id": "ak-expired",
        "name": "expired",
        "key_prefix": raw[:api_keys.KEY_PREFIX_LEN],
        "scopes": '["*"]',
        "created_by": "ops@example.com",
        "enabled": 1,
        "expires_at": 999.0,
        "created_at": "",
    }
    monkeypatch.setattr(api_keys, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(api_keys.time, "time", lambda: 1000.0)

    assert await api_keys.validate_bearer(raw, ip="203.0.113.10") is None
    assert conn.updates == []


@pytest.mark.asyncio
async def test_validate_bearer_accepts_unexpired_key(monkeypatch):
    conn = _Conn()
    raw = "omni_active-test-token"
    conn.rows_by_lookup[api_keys._hash_key(raw)] = {
        "id": "ak-active",
        "name": "active",
        "key_prefix": raw[:api_keys.KEY_PREFIX_LEN],
        "scopes": '["*"]',
        "created_by": "ops@example.com",
        "enabled": 1,
        "expires_at": 1001.0,
        "created_at": "",
    }
    monkeypatch.setattr(api_keys, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(api_keys.time, "time", lambda: 1000.0)

    key = await api_keys.validate_bearer(raw, ip="203.0.113.10")

    assert key is not None
    assert key.id == "ak-active"
    assert key.expires_at == 1001.0
    assert conn.updates == [("203.0.113.10", 1000.0, "ak-active")]
