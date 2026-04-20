"""SP-4.6 — tenant_secrets.py pool-backed tests.

Replaces the orphaned ``tests/test_tenant_secrets.py`` at repo root
(which used the SQLite tempfile fixture pattern and imported a
non-existent ``backend.secrets`` module, so it wasn't being
collected by ``backend/pytest.ini`` anyway).

Coverage:
  * CRUD round-trips (list, get_by_id, get_by_name, upsert, delete)
  * Tenant isolation (two tenants + same key_name → own values)
  * ``upsert_secret`` atomicity under concurrent racers (load-bearing
    regression guard for the SP-4.6 ON CONFLICT fix).
  * Unique-per-tenant constraint (DB-level).
"""

from __future__ import annotations

import asyncio

import pytest


DEFAULT_TENANT = "t-default"
OTHER_TENANT = "t-secrets-other"


@pytest.fixture()
async def _secrets_db(pg_test_pool, monkeypatch):
    # Fresh slate per test; tenant_secrets.tenant_id has an FK to
    # ``tenants`` so seed both tenants up-front.
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE tenant_secrets RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "($1, $2, $3), ($4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            DEFAULT_TENANT, "Default", "starter",
            OTHER_TENANT, "Other", "starter",
        )
    from backend.db_context import set_tenant_id
    set_tenant_id(DEFAULT_TENANT)
    from backend import tenant_secrets
    try:
        yield tenant_secrets
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE tenant_secrets RESTART IDENTITY CASCADE"
            )
        set_tenant_id(None)


# ── CRUD round-trips ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_and_list_roundtrip(_secrets_db):
    sec = _secrets_db
    sid = await sec.upsert_secret("my-key", "super-secret", "provider_key")
    assert sid.startswith("sec-")

    items = await sec.list_secrets()
    assert len(items) == 1
    assert items[0]["key_name"] == "my-key"
    assert items[0]["secret_type"] == "provider_key"
    # Fingerprint is derived from plaintext but is not plaintext.
    assert items[0]["fingerprint"] != "super-secret"


@pytest.mark.asyncio
async def test_get_secret_value_roundtrip(_secrets_db):
    sec = _secrets_db
    sid = await sec.upsert_secret("api-token", "my-api-token-12345", "provider_key")
    val = await sec.get_secret_value(sid)
    assert val == "my-api-token-12345"


@pytest.mark.asyncio
async def test_get_secret_by_name(_secrets_db):
    sec = _secrets_db
    await sec.upsert_secret("github_token", "ghp_abc123", "git_credential")
    val = await sec.get_secret_by_name("github_token", "git_credential")
    assert val == "ghp_abc123"
    # Also works without specifying secret_type.
    val2 = await sec.get_secret_by_name("github_token")
    assert val2 == "ghp_abc123"


@pytest.mark.asyncio
async def test_upsert_updates_existing_preserves_id(_secrets_db):
    sec = _secrets_db
    sid1 = await sec.upsert_secret("rotate-me", "old-value", "provider_key")
    sid2 = await sec.upsert_secret("rotate-me", "new-value", "provider_key")
    assert sid1 == sid2, "upsert on conflict must preserve original id"
    val = await sec.get_secret_value(sid1)
    assert val == "new-value"


@pytest.mark.asyncio
async def test_upsert_updates_metadata(_secrets_db):
    sec = _secrets_db
    await sec.upsert_secret(
        "gh-token", "ghp_xxx", "git_credential",
        metadata={"provider": "github"},
    )
    items = await sec.list_secrets()
    assert items[0]["metadata"]["provider"] == "github"
    # Second upsert overwrites metadata.
    await sec.upsert_secret(
        "gh-token", "ghp_xxx", "git_credential",
        metadata={"provider": "github", "scope": "repo"},
    )
    items = await sec.list_secrets()
    assert items[0]["metadata"] == {"provider": "github", "scope": "repo"}


@pytest.mark.asyncio
async def test_delete_and_delete_nonexistent(_secrets_db):
    sec = _secrets_db
    sid = await sec.upsert_secret("temp", "temp-value", "custom")
    assert await sec.delete_secret(sid) is True
    assert await sec.get_secret_value(sid) is None
    # Deleting again returns False (no row affected).
    assert await sec.delete_secret(sid) is False
    assert await sec.delete_secret("nonexistent-id") is False


@pytest.mark.asyncio
async def test_list_filtered_by_type(_secrets_db):
    sec = _secrets_db
    await sec.upsert_secret("k1", "v1", "provider_key")
    await sec.upsert_secret("k2", "v2", "git_credential")
    await sec.upsert_secret("k3", "v3", "provider_key")
    providers = await sec.list_secrets(secret_type="provider_key")
    git = await sec.list_secrets(secret_type="git_credential")
    assert len(providers) == 2
    assert len(git) == 1


# ── Tenant isolation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_secrets_isolated_between_tenants(_secrets_db):
    sec = _secrets_db
    from backend.db_context import set_tenant_id
    set_tenant_id(DEFAULT_TENANT)
    await sec.upsert_secret("shared-name", "default-value", "provider_key")

    set_tenant_id(OTHER_TENANT)
    await sec.upsert_secret("shared-name", "other-value", "provider_key")

    set_tenant_id(DEFAULT_TENANT)
    assert await sec.get_secret_by_name("shared-name", "provider_key") == "default-value"
    set_tenant_id(OTHER_TENANT)
    assert await sec.get_secret_by_name("shared-name", "provider_key") == "other-value"


@pytest.mark.asyncio
async def test_cannot_delete_other_tenant_secret(_secrets_db):
    sec = _secrets_db
    from backend.db_context import set_tenant_id
    set_tenant_id(DEFAULT_TENANT)
    sid = await sec.upsert_secret("my-secret", "val", "custom")

    set_tenant_id(OTHER_TENANT)
    assert await sec.delete_secret(sid) is False, (
        "tenant must not be able to delete another tenant's secret"
    )

    set_tenant_id(DEFAULT_TENANT)
    assert await sec.get_secret_value(sid) == "val"


@pytest.mark.asyncio
async def test_require_tenant_context(_secrets_db):
    sec = _secrets_db
    from backend.db_context import set_tenant_id
    set_tenant_id(None)
    with pytest.raises(RuntimeError, match="No tenant_id"):
        await sec.list_secrets()


# ── SP-4.6: atomic upsert under concurrency ─────────────────────


@pytest.mark.asyncio
async def test_upsert_secret_concurrent_same_key_atomic(_secrets_db):
    """Load-bearing regression guard for SP-4.6's ON CONFLICT fix.

    The old SELECT-then-INSERT pattern had a check-then-act race:
    two concurrent upserts on the same (tenant, secret_type, key_name)
    could both observe "not exists" and both INSERT → one raises on
    the UNIQUE violation. Under SQLite's file-lock this window was
    effectively zero. Under asyncpg pool it's open wide enough to
    hit reliably under ``asyncio.gather`` with many racers.

    ``INSERT ... ON CONFLICT DO UPDATE`` closes the window: PG's
    UNIQUE enforcement routes the loser to the DO UPDATE branch,
    no exception raised, and both callers observe a single row with
    one of their values (last-writer-wins by commit order).
    """
    sec = _secrets_db
    N = 10
    # Each racer tries to upsert the same key with its own value.
    results = await asyncio.gather(
        *(
            sec.upsert_secret("contention-key", f"value-{i}", "provider_key")
            for i in range(N)
        ),
        return_exceptions=True,
    )
    # No racer should have raised — the old SELECT-then-INSERT pattern
    # would have produced UniqueViolationError on at least one call.
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, (
        f"atomic upsert failed: {len(errors)} racers raised "
        f"(expected 0). Sample: {errors[0]!r}"
    )
    # All racers return the same id (same underlying row).
    distinct_ids = set(results)
    assert len(distinct_ids) == 1, (
        f"expected one merged row, got {len(distinct_ids)} distinct ids "
        f"(possible INSERT-race ghost rows): {distinct_ids}"
    )
    # Exactly one row in the table for this key.
    items = await sec.list_secrets(secret_type="provider_key")
    assert len(items) == 1
    # The final plaintext is one of the racers' values (last writer
    # by PG commit order; we don't assert which specific one).
    final_plain = await sec.get_secret_value(items[0]["id"])
    assert final_plain in {f"value-{i}" for i in range(N)}


@pytest.mark.asyncio
async def test_upsert_secret_unique_per_tenant(_secrets_db):
    """DB-level UNIQUE (tenant_id, secret_type, key_name) — same key
    name in different tenants is allowed; same key name same tenant
    same type collapses to one row."""
    sec = _secrets_db
    from backend.db_context import set_tenant_id

    set_tenant_id(DEFAULT_TENANT)
    await sec.upsert_secret("dup", "a", "provider_key")
    await sec.upsert_secret("dup", "b", "provider_key")  # upserts into same row
    items = await sec.list_secrets()
    assert len(items) == 1

    set_tenant_id(OTHER_TENANT)
    await sec.upsert_secret("dup", "c", "provider_key")  # separate tenant, new row
    items_other = await sec.list_secrets()
    assert len(items_other) == 1
    assert await sec.get_secret_by_name("dup", "provider_key") == "c"
