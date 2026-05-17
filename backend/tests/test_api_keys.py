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
    def __init__(self, row: dict | None = None) -> None:
        self.row = row
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        if sql.startswith("SELECT id, name, scopes"):
            assert args == ("ak-existing",)
            return self.row
        raise AssertionError(f"unexpected SQL: {sql}")

    async def execute(self, sql: str, *args):
        if sql.startswith("UPDATE api_keys SET key_hash"):
            self.executed.append((sql, args))
            return "UPDATE 1"
        raise AssertionError(f"unexpected SQL: {sql}")


def test_scope_allows_oauth_agent_card_and_a2a_wildcard():
    discover_key = api_keys.ApiKey(
        id="ak-discover",
        name="discover",
        key_prefix="omni_dis",
        scopes=["a2a:discover:agent-card"],
    )
    invoke_key = api_keys.ApiKey(
        id="ak-invoke",
        name="invoke",
        key_prefix="omni_inv",
        scopes=["a2a:invoke:*"],
    )

    assert discover_key.scope_allows("/.well-known/agent.json")
    assert invoke_key.scope_allows("/a2a/invoke/summarizer?trace=1")
    assert not discover_key.scope_allows("/a2a/invoke/summarizer")


def test_oauth_scope_allows_rejects_empty_grant_or_required_scope():
    assert not api_keys._oauth_scope_allows("", "a2a:invoke:writer")
    assert not api_keys._oauth_scope_allows("a2a:invoke:*", "")


@pytest.mark.asyncio
async def test_create_key_rejects_non_positive_ttl():
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        await api_keys.create_key("bad-ttl", ttl_seconds=0)


@pytest.mark.asyncio
async def test_rotate_key_returns_empty_secret_when_key_missing(monkeypatch):
    conn = _Conn(row=None)
    monkeypatch.setattr(api_keys, "get_pool", lambda: _Pool(conn))

    key, raw = await api_keys.rotate_key("ak-existing")

    assert key is None
    assert raw == ""
    assert conn.executed == []


@pytest.mark.asyncio
async def test_rotate_key_updates_secret_and_preserves_key_metadata(monkeypatch):
    conn = _Conn(row={
        "id": "ak-existing",
        "name": "ci-key",
        "scopes": '["a2a:invoke:writer"]',
        "created_by": "ops@example.com",
        "expires_at": 1234.5,
        "enabled": 1,
        "created_at": "2026-05-17T00:00:00Z",
        "tenant_id": None,
    })
    monkeypatch.setattr(api_keys, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(
        api_keys.secrets, "token_urlsafe", lambda _n: "rotated-secret",
    )
    monkeypatch.setattr(
        api_keys, "_pack_hash",
        lambda hashed, key_id, tenant_id="t-default": (
            f"packed:{tenant_id}:{key_id}:{hashed}"
        ),
    )

    key, raw = await api_keys.rotate_key("ak-existing")

    assert raw == "omni_rotated-secret"
    assert key.id == "ak-existing"
    assert key.name == "ci-key"
    assert key.key_prefix == "omni_rot"
    assert key.scopes == ["a2a:invoke:writer"]
    assert key.created_by == "ops@example.com"
    assert key.expires_at == 1234.5
    assert key.enabled
    assert key.created_at == "2026-05-17T00:00:00Z"
    assert len(conn.executed) == 1
    _sql, args = conn.executed[0]
    assert args == (
        f"packed:t-default:ak-existing:{api_keys._hash_key(raw)}",
        api_keys._hash_key(raw),
        "omni_rot",
        "ak-existing",
    )
