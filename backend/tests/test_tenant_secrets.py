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
import json
import os
import subprocess
import sys

import pytest


DEFAULT_TENANT = "t-default"
OTHER_TENANT = "t-secrets-other"


def test_secret_value_envelope_helpers_survive_hard_restart(monkeypatch):
    """KS.1.11 customer-secret compat without requiring a live PG pool.

    ``tenant_secrets`` persists the returned string in the existing
    ``encrypted_value`` column; decrypting that value in a fresh
    interpreter simulates a random hard restart during the dual-write
    window.
    """
    monkeypatch.setenv(
        "OMNISIGHT_SECRET_KEY",
        "ks-1-11-tenant-hard-restart-secret",
    )
    from backend import secret_store
    from backend import tenant_secrets as sec
    secret_store._reset_for_tests()

    stored = sec._encrypt_secret_value(
        DEFAULT_TENANT,
        "provider_key",
        "openai",
        "sk-proj-tenant-hard-restart",
    )
    outer = json.loads(stored)
    assert outer["fmt"] == sec.SECRET_ENVELOPE_FORMAT_VERSION
    assert outer["dek_ref"]["tenant_id"] == DEFAULT_TENANT
    assert outer["dek_ref"]["encryption_context"]["purpose"] == "tenant-secret"
    assert "sk-proj-tenant-hard-restart" not in stored

    code = """
import os
from backend import tenant_secrets as sec

print(sec._decrypt_secret_value(
    os.environ["KS111_TENANT_STORED"],
    "t-default",
    "provider_key",
    "openai",
))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        check=True,
        env={
            **dict(os.environ),
            "KS111_TENANT_STORED": stored,
        },
    )
    assert proc.stdout.strip() == "sk-proj-tenant-hard-restart"


def test_secret_value_legacy_fernet_helper_fallback(monkeypatch):
    """Existing Fernet-only ``tenant_secrets.encrypted_value`` cells
    remain readable during the KS.1.11 compatibility window."""
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-11-tenant-legacy-secret")
    from backend import secret_store
    from backend import tenant_secrets as sec
    secret_store._reset_for_tests()
    legacy = secret_store.encrypt("sk-legacy-tenant")
    assert sec._decrypt_secret_value(
        legacy,
        DEFAULT_TENANT,
        "provider_key",
        "legacy",
    ) == "sk-legacy-tenant"


def test_secret_value_envelope_disabled_writes_single_fernet(monkeypatch):
    """KS.1.12: knob-off writes the old single-Fernet tenant secret
    format during the migration rollback window."""
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-12-tenant-rollback-secret")
    from backend import secret_store
    from backend import tenant_secrets as sec
    from backend.security import envelope as tenant_envelope
    secret_store._reset_for_tests()
    monkeypatch.setenv(tenant_envelope.ENVELOPE_ENABLED_ENV, "false")

    stored = sec._encrypt_secret_value(
        DEFAULT_TENANT,
        "provider_key",
        "openai",
        "sk-proj-tenant-rollback",
    )

    assert not stored.lstrip().startswith("{")
    assert sec._decrypt_secret_value(
        stored,
        DEFAULT_TENANT,
        "provider_key",
        "openai",
    ) == "sk-proj-tenant-rollback"


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
async def test_new_secret_writes_use_ks_envelope_carrier(_secrets_db, pg_test_pool):
    """KS.1.11 compat regression: customer/tenant secrets written via
    the existing CRUD API now go through the per-tenant DEK envelope,
    while keeping the original ``tenant_secrets.encrypted_value`` column.
    """
    sec = _secrets_db
    sid = await sec.upsert_secret("openai", "sk-proj-new-path", "provider_key")
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT encrypted_value FROM tenant_secrets WHERE id = $1",
            sid,
        )
    stored = row["encrypted_value"]
    outer = json.loads(stored)
    assert outer["fmt"] == sec.SECRET_ENVELOPE_FORMAT_VERSION
    assert outer["ciphertext"].startswith("{")
    assert outer["dek_ref"]["tenant_id"] == DEFAULT_TENANT
    assert outer["dek_ref"]["encryption_context"]["purpose"] == "tenant-secret"
    assert "sk-proj-new-path" not in stored
    assert await sec.get_secret_value(sid) == "sk-proj-new-path"


@pytest.mark.asyncio
async def test_legacy_fernet_secret_reads_during_compat_window(
    _secrets_db, pg_test_pool,
):
    """Existing Fernet-only ``tenant_secrets`` rows stay readable while
    KS.1.11 writes use the new envelope carrier."""
    from backend import secret_store
    sec = _secrets_db
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_secrets "
            "(id, tenant_id, secret_type, key_name, encrypted_value) "
            "VALUES ($1, $2, $3, $4, $5)",
            "sec-legacy-fernet", DEFAULT_TENANT, "provider_key", "legacy",
            secret_store.encrypt("sk-legacy-fernet"),
        )
    assert await sec.get_secret_value("sec-legacy-fernet") == "sk-legacy-fernet"
    assert await sec.get_secret_by_name("legacy", "provider_key") == "sk-legacy-fernet"


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


# ── Coverage gap-fill (task #83, 2026-04-21) ─────────────────────
#
# The module-level baseline was 84%; the seven uncovered branches
# were the decrypt/JSON-parse fallbacks inside ``_list_secrets_impl``,
# the ``conn is not None`` arms of the polymorphic public helpers,
# the not-found path of ``get_secret_by_name``, and the
# status-string parse fallback in ``delete_secret``. Each fill is
# a targeted test rather than a generic increase.


@pytest.mark.asyncio
async def test_list_secrets_fingerprints_unreadable_ciphertext_as_masked(
    _secrets_db, pg_test_pool,
):
    """Covers the ``except Exception: fp = "****"`` arm in
    ``_list_secrets_impl``. Hand-seed a row whose ``encrypted_value``
    is NOT valid Fernet output so ``decrypt`` raises; the list call
    must still return the row with a masked fingerprint instead of
    propagating the exception."""
    sec = _secrets_db
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_secrets "
            "(id, tenant_id, secret_type, key_name, encrypted_value) "
            "VALUES ($1, $2, $3, $4, $5)",
            "sec-corrupt", DEFAULT_TENANT, "custom", "broken",
            "this-is-not-valid-fernet-ciphertext",
        )
    items = await sec.list_secrets()
    broken = [r for r in items if r["key_name"] == "broken"]
    assert broken and broken[0]["fingerprint"] == "****"


@pytest.mark.asyncio
async def test_list_secrets_swallows_corrupt_metadata_json(
    _secrets_db, pg_test_pool,
):
    """Covers the ``except Exception: pass`` arm on the metadata JSON
    decode. A legacy row with malformed ``metadata`` must surface as
    an empty dict, not blow up the list call."""
    from backend.secret_store import encrypt
    sec = _secrets_db
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_secrets "
            "(id, tenant_id, secret_type, key_name, "
            "encrypted_value, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            "sec-badmeta", DEFAULT_TENANT, "custom", "badmeta",
            encrypt("val"), "::: not valid json :::",
        )
    items = await sec.list_secrets()
    badmeta = [r for r in items if r["key_name"] == "badmeta"]
    assert badmeta and badmeta[0]["metadata"] == {}


@pytest.mark.asyncio
async def test_public_helpers_accept_explicit_conn(_secrets_db, pg_test_pool):
    """Covers the ``conn is not None`` arms of ``list_secrets``,
    ``get_secret_value``, ``get_secret_by_name``, ``upsert_secret``,
    and ``delete_secret``. These are the polymorphic-conn pattern
    branches that production call sites use to share a caller-owned
    pool connection (e.g. inside a multi-statement transaction)."""
    sec = _secrets_db
    async with pg_test_pool.acquire() as conn:
        sid = await sec.upsert_secret(
            "explicit-conn", "v", "custom", conn=conn,
        )
        val = await sec.get_secret_value(sid, conn=conn)
        assert val == "v"
        by_name = await sec.get_secret_by_name(
            "explicit-conn", "custom", conn=conn,
        )
        assert by_name == "v"
        items = await sec.list_secrets(conn=conn)
        assert any(i["id"] == sid for i in items)
        assert await sec.delete_secret(sid, conn=conn) is True


@pytest.mark.asyncio
async def test_get_secret_by_name_returns_none_when_missing(_secrets_db):
    """Covers the ``if not row: return None`` exit. The no-secret_type
    branch is the one the current happy-path test doesn't exercise —
    grep confirms line 149 was uncovered before this test landed."""
    sec = _secrets_db
    assert await sec.get_secret_by_name("does-not-exist") is None
    assert await sec.get_secret_by_name(
        "does-not-exist", "custom",
    ) is None


# ── Encryption round-trip across edge-case payloads ───────────────
#
# Phase-3 Step C.1 (2026-04-21): absorbed from the deleted
# ``tests/test_tenant_secrets.py::TestEncryptionRoundTrip``. The
# long-string + special-char payloads specifically guard against
# Fernet padding / base64-safe encoding regressions that the
# CRUD-level tests above wouldn't necessarily surface.


@pytest.mark.asyncio
async def test_encryption_round_trip_edge_case_payloads(_secrets_db):
    sec = _secrets_db
    edge_cases = [
        "simple-key",
        "ghp_1234567890abcdef1234567890abcdef12345678",
        "sk-ant-api03-xxxx",
        "a" * 500,
        "special!@#$%^&*()_+{}|:<>?",
    ]
    for i, val in enumerate(edge_cases):
        sid = await sec.upsert_secret(
            f"edge-{i}", val, "custom",
        )
        recovered = await sec.get_secret_value(sid)
        assert recovered == val, f"round-trip failed for payload #{i}"
