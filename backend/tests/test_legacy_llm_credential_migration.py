"""Phase 5b-5 (#llm-credentials) — legacy ``.env`` → ``llm_credentials``
auto-migration tests.

Three layers, mirroring the layout of
:mod:`backend.tests.test_legacy_credential_migration`:

1. Pure-unit ``_plan_rows`` tests (no PG, no pool) — verify the
   per-provider Settings-read contract, the ollama ``base_url``
   default-versus-custom distinction, and deterministic id shape.
2. Pure-unit ``migrate_legacy_llm_credentials_once`` tests with a
   stub pool — verify kill-switch, empty-Settings no-op, and the
   no-pool fallback (SQLite dev).
3. PG live contract tests via ``pg_test_pool`` — verify end-to-end
   ``llm_credentials`` row writes with Fernet-encrypted values, that
   re-running is a true no-op (idempotency), that the audit chain
   records per-row ``llm_credential_auto_migrate`` entries with
   ``actor='system/migration'``, and that two concurrent calls
   collapse to one row per deterministic id (worker-race safety).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


# Mixed sync (``_plan_rows`` unit tests) + async tests — apply
# ``@pytest.mark.asyncio`` per-test rather than module-wide so the
# sync tests don't trip the pytest-asyncio mark warning.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — Settings monkeypatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"


def _patched_settings(**overrides: Any):
    """Patch ``backend.legacy_llm_credential_migration.settings`` with
    all legacy LLM fields blanked out (api_keys) + ollama default
    base_url (so keyless branch is a no-op by default), then apply
    caller overrides.
    """
    p = patch("backend.legacy_llm_credential_migration.settings")
    mock = p.start()
    mock.anthropic_api_key = ""
    mock.google_api_key = ""
    mock.openai_api_key = ""
    mock.xai_api_key = ""
    mock.groq_api_key = ""
    mock.deepseek_api_key = ""
    mock.together_api_key = ""
    mock.openrouter_api_key = ""
    mock.ollama_base_url = _DEFAULT_OLLAMA_BASE_URL
    for k, v in overrides.items():
        setattr(mock, k, v)
    return p, mock


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. _plan_rows — pure-unit precedence contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_plan_rows_empty_settings_returns_empty_list():
    p, _ = _patched_settings()
    try:
        from backend.legacy_llm_credential_migration import _plan_rows
        assert _plan_rows() == []
    finally:
        p.stop()


def test_plan_rows_anthropic_key_becomes_default_row():
    p, _ = _patched_settings(anthropic_api_key="sk-ant-aaaa")
    try:
        from backend.legacy_llm_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == "lc-legacy-anthropic"
        assert r["provider"] == "anthropic"
        assert r["label"] == "Legacy .env migration"
        assert r["value"] == "sk-ant-aaaa"
        assert r["is_default"] is True
        assert r["enabled"] is True
        assert r["source"] == "settings.anthropic_api_key"
        assert r["metadata"] == {}
    finally:
        p.stop()


def test_plan_rows_whitespace_only_key_is_dropped():
    p, _ = _patched_settings(openai_api_key="   \n  ")
    try:
        from backend.legacy_llm_credential_migration import _plan_rows
        assert _plan_rows() == []
    finally:
        p.stop()


def test_plan_rows_all_eight_keyed_providers_at_once():
    """Every keyed provider gets migrated when its key is non-empty.
    Verifies the resolver's 9-provider list is covered (8 keyed +
    ollama handled separately)."""
    p, _ = _patched_settings(
        anthropic_api_key="sk-ant-a",
        google_api_key="g-goog-b",
        openai_api_key="sk-c",
        xai_api_key="xai-d",
        groq_api_key="gsk_e",
        deepseek_api_key="sk-f",
        together_api_key="tog-g",
        openrouter_api_key="sk-or-h",
    )
    try:
        from backend.legacy_llm_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 8
        providers = sorted(r["provider"] for r in rows)
        assert providers == [
            "anthropic", "deepseek", "google", "groq",
            "openai", "openrouter", "together", "xai",
        ]
        assert all(r["is_default"] is True for r in rows)
        assert all(r["label"] == "Legacy .env migration" for r in rows)
        # Deterministic ids — grep-friendly.
        ids = sorted(r["id"] for r in rows)
        assert ids == [
            "lc-legacy-anthropic",
            "lc-legacy-deepseek",
            "lc-legacy-google",
            "lc-legacy-groq",
            "lc-legacy-openai",
            "lc-legacy-openrouter",
            "lc-legacy-together",
            "lc-legacy-xai",
        ]
    finally:
        p.stop()


def test_plan_rows_ollama_default_base_url_does_not_create_row():
    """The keyless ollama branch must not emit a row when base_url is
    the module default — the resolver's fallback already synthesises
    the same value from Settings so a row carries no signal."""
    p, _ = _patched_settings(ollama_base_url=_DEFAULT_OLLAMA_BASE_URL)
    try:
        from backend.legacy_llm_credential_migration import _plan_rows
        assert _plan_rows() == []
    finally:
        p.stop()


def test_plan_rows_ollama_custom_base_url_creates_keyless_row():
    p, _ = _patched_settings(
        ollama_base_url="http://ai_engine:11434",
    )
    try:
        from backend.legacy_llm_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == "lc-legacy-ollama"
        assert r["provider"] == "ollama"
        assert r["value"] == ""  # keyless
        assert r["metadata"] == {"base_url": "http://ai_engine:11434"}
        assert r["is_default"] is True
        assert r["source"] == "settings.ollama_base_url"
    finally:
        p.stop()


def test_plan_rows_ollama_empty_base_url_skipped():
    p, _ = _patched_settings(ollama_base_url="")
    try:
        from backend.legacy_llm_credential_migration import _plan_rows
        assert _plan_rows() == []
    finally:
        p.stop()


def test_plan_rows_mixed_keys_plus_custom_ollama():
    p, _ = _patched_settings(
        anthropic_api_key="sk-ant-x",
        openrouter_api_key="sk-or-y",
        ollama_base_url="http://ai_engine:11434",
    )
    try:
        from backend.legacy_llm_credential_migration import _plan_rows
        rows = _plan_rows()
        providers = sorted(r["provider"] for r in rows)
        assert providers == ["anthropic", "ollama", "openrouter"]
        ollama_row = [r for r in rows if r["provider"] == "ollama"][0]
        assert ollama_row["metadata"]["base_url"] == "http://ai_engine:11434"
        assert ollama_row["value"] == ""
    finally:
        p.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. migrate_legacy_llm_credentials_once — kill-switch / no-pool / no-op
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_migrate_kill_switch_short_circuits(monkeypatch):
    """``OMNISIGHT_LLM_CREDENTIAL_MIGRATE=skip`` must bypass even when
    legacy creds are present — operator escape hatch."""
    monkeypatch.setenv("OMNISIGHT_LLM_CREDENTIAL_MIGRATE", "skip")
    p, _ = _patched_settings(anthropic_api_key="sk-ant-should-not-run")
    try:
        from backend.legacy_llm_credential_migration import (
            migrate_legacy_llm_credentials_once,
        )
        out = await migrate_legacy_llm_credentials_once()
        assert out["migrated"] == 0
        assert out["skipped_reason"] == (
            "env:OMNISIGHT_LLM_CREDENTIAL_MIGRATE=skip"
        )
        assert out["sources"] == []
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_migrate_no_pool_returns_skipped(monkeypatch):
    """SQLite dev mode: pool not initialised → migration is a no-op."""
    monkeypatch.delenv("OMNISIGHT_LLM_CREDENTIAL_MIGRATE", raising=False)

    def _no_pool():
        raise RuntimeError("pool not init")

    import backend.db_pool
    monkeypatch.setattr(backend.db_pool, "get_pool", _no_pool)

    p, _ = _patched_settings(anthropic_api_key="sk-ant-xx")
    try:
        from backend.legacy_llm_credential_migration import (
            migrate_legacy_llm_credentials_once,
        )
        out = await migrate_legacy_llm_credentials_once()
        assert out["migrated"] == 0
        assert out["skipped_reason"] == "no_pool"
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_migrate_no_legacy_credentials_skipped(monkeypatch):
    """Empty Settings → migration plan empty → reason
    ``no_legacy_credentials``. Pool is never acquired (verified by
    stubbing the pool to raise on ``acquire``)."""
    monkeypatch.delenv("OMNISIGHT_LLM_CREDENTIAL_MIGRATE", raising=False)

    class _StubPool:
        def acquire(self_inner):  # noqa: D401 — fake pool
            raise AssertionError(
                "Pool acquire should not be called when there's nothing "
                "to migrate"
            )

    import backend.db_pool
    monkeypatch.setattr(backend.db_pool, "get_pool", lambda: _StubPool())

    p, _ = _patched_settings()
    try:
        from backend.legacy_llm_credential_migration import (
            migrate_legacy_llm_credentials_once,
        )
        out = await migrate_legacy_llm_credentials_once()
        assert out["migrated"] == 0
        assert out["skipped_reason"] == "no_legacy_credentials"
    finally:
        p.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Live PG contract tests via pg_test_pool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def _live_db(pg_test_pool, monkeypatch):
    """Fresh ``llm_credentials`` + ``audit_log`` slate with the
    ``t-default`` tenant seeded. Mirrors the shape of
    :func:`test_llm_credentials_crud._lc_db`. Kill-switch is unset
    so the migration runs unless a test explicitly opts back in.
    """
    monkeypatch.delenv("OMNISIGHT_LLM_CREDENTIAL_MIGRATE", raising=False)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "('t-default', 'Default', 'starter') "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "TRUNCATE llm_credentials, audit_log RESTART IDENTITY CASCADE"
        )
    yield pg_test_pool
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE llm_credentials, audit_log RESTART IDENTITY CASCADE"
        )


@pytest.mark.asyncio
async def test_pg_migrate_writes_rows_with_encrypted_value(_live_db):
    """End-to-end: two keyed providers + one custom ollama base_url
    produce three ``llm_credentials`` rows with ciphertext (keyed
    rows) and JSON metadata (ollama row)."""
    p, _ = _patched_settings(
        anthropic_api_key="sk-ant-e2e-aaaa",
        openai_api_key="sk-e2e-bbbb",
        ollama_base_url="http://ai_engine:11434",
    )
    try:
        from backend.legacy_llm_credential_migration import (
            migrate_legacy_llm_credentials_once,
        )
        out = await migrate_legacy_llm_credentials_once()
        assert out["migrated"] == 3
        assert out["skipped_reason"] is None
        assert sorted(out["sources"]) == [
            "settings.anthropic_api_key",
            "settings.ollama_base_url",
            "settings.openai_api_key",
        ]
    finally:
        p.stop()

    async with _live_db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, provider, label, encrypted_value, metadata, "
            "is_default, enabled, auth_type, tenant_id "
            "FROM llm_credentials ORDER BY provider, id"
        )
    assert len(rows) == 3
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {
        "lc-legacy-anthropic",
        "lc-legacy-openai",
        "lc-legacy-ollama",
    }

    ant = by_id["lc-legacy-anthropic"]
    assert ant["provider"] == "anthropic"
    assert ant["label"] == "Legacy .env migration"
    assert ant["tenant_id"] == "t-default"
    assert ant["is_default"] is True
    assert ant["enabled"] is True
    assert ant["auth_type"] == "pat"
    # Value is encrypted at rest — plaintext must not appear.
    assert ant["encrypted_value"] != ""
    assert "sk-ant-e2e-aaaa" not in (ant["encrypted_value"] or "")
    # Decrypt round-trips back to the plaintext.
    from backend.secret_store import decrypt
    assert decrypt(ant["encrypted_value"]) == "sk-ant-e2e-aaaa"

    oa = by_id["lc-legacy-openai"]
    assert decrypt(oa["encrypted_value"]) == "sk-e2e-bbbb"

    ol = by_id["lc-legacy-ollama"]
    # Ollama is keyless — encrypted_value empty; base_url in metadata.
    assert ol["encrypted_value"] == ""
    import json as _json
    meta_raw = ol["metadata"]
    meta = (
        _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
    )
    assert meta == {"base_url": "http://ai_engine:11434"}


@pytest.mark.asyncio
async def test_pg_migrate_audit_log_emitted_per_row(_live_db):
    """Each insert writes one ``llm_credential_auto_migrate`` audit row
    with ``actor='system/migration'`` and NO plaintext key."""
    p, _ = _patched_settings(
        anthropic_api_key="sk-ant-audit-aaaa",
        google_api_key="g-goog-audit-bbbb",
    )
    try:
        from backend.legacy_llm_credential_migration import (
            migrate_legacy_llm_credentials_once,
        )
        out = await migrate_legacy_llm_credentials_once()
        assert out["migrated"] == 2
    finally:
        p.stop()

    async with _live_db.acquire() as conn:
        audit_rows = await conn.fetch(
            "SELECT action, entity_kind, entity_id, actor, "
            "before_json, after_json "
            "FROM audit_log WHERE action = 'llm_credential_auto_migrate' "
            "ORDER BY entity_id"
        )
    assert len(audit_rows) == 2
    assert {r["actor"] for r in audit_rows} == {"system/migration"}
    assert {r["entity_kind"] for r in audit_rows} == {"llm_credential"}
    assert {r["entity_id"] for r in audit_rows} == {
        "lc-legacy-anthropic",
        "lc-legacy-google",
    }

    # Plaintext key must NEVER appear in the audit payload.
    for r in audit_rows:
        before_blob = r["before_json"] or ""
        after_blob = r["after_json"] or ""
        # Serialise both for a substring grep.
        blob = f"{before_blob}|{after_blob}"
        assert "sk-ant-audit-aaaa" not in blob
        assert "g-goog-audit-bbbb" not in blob


@pytest.mark.asyncio
async def test_pg_migrate_idempotent_second_run_no_op(_live_db):
    """Re-running after a row was already inserted is a no-op — skip
    reason is ``llm_credentials_non_empty`` so operators can grep the
    boot log to tell the two skipping cases apart."""
    p, _ = _patched_settings(anthropic_api_key="sk-ant-idem-aaaa")
    try:
        from backend.legacy_llm_credential_migration import (
            migrate_legacy_llm_credentials_once,
        )
        first = await migrate_legacy_llm_credentials_once()
        assert first["migrated"] == 1
        second = await migrate_legacy_llm_credentials_once()
        assert second["migrated"] == 0
        assert second["skipped_reason"] == "llm_credentials_non_empty"
        assert second["candidates"] == 1
    finally:
        p.stop()

    async with _live_db.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM llm_credentials")
    assert n == 1


@pytest.mark.asyncio
async def test_pg_migrate_concurrent_workers_only_one_winner(_live_db):
    """Two simultaneous calls (worker race simulation) must collapse to
    a single inserted row per deterministic id — relies on the
    table-non-empty guard (first line of defence) and the
    ``ON CONFLICT (id) DO NOTHING`` fallback (second line)."""
    import asyncio
    p, _ = _patched_settings(anthropic_api_key="sk-ant-race-aaaa")
    try:
        from backend.legacy_llm_credential_migration import (
            migrate_legacy_llm_credentials_once,
        )
        results = await asyncio.gather(
            migrate_legacy_llm_credentials_once(),
            migrate_legacy_llm_credentials_once(),
        )
    finally:
        p.stop()

    total = sum(r["migrated"] for r in results)
    assert total == 1
    async with _live_db.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM llm_credentials")
    assert n == 1


@pytest.mark.asyncio
async def test_pg_migrate_on_conflict_do_nothing_blocks_duplicate(
    _live_db, monkeypatch,
):
    """Second line of defence: with the ``_table_has_any_row`` guard
    bypassed, re-running with the same Settings must still not
    duplicate because the deterministic id collides and ON CONFLICT
    silently drops it."""
    p, _ = _patched_settings(anthropic_api_key="sk-ant-seed-aaaa")
    try:
        from backend import legacy_llm_credential_migration as llm_mig
        first = await llm_mig.migrate_legacy_llm_credentials_once()
        assert first["migrated"] == 1
    finally:
        p.stop()
    async with _live_db.acquire() as conn:
        await conn.execute("TRUNCATE audit_log RESTART IDENTITY CASCADE")

    monkeypatch.setattr(
        "backend.legacy_llm_credential_migration._table_has_any_row",
        lambda conn: _async_false(),
    )
    p, _ = _patched_settings(anthropic_api_key="sk-ant-seed-aaaa")
    try:
        from backend import legacy_llm_credential_migration as llm_mig
        second = await llm_mig.migrate_legacy_llm_credentials_once()
        assert second["migrated"] == 0
        assert second["candidates"] == 1
    finally:
        p.stop()

    async with _live_db.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM llm_credentials")
        audit_count = await conn.fetchval(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action = 'llm_credential_auto_migrate'"
        )
    assert n == 1
    assert audit_count == 0  # loser silently skipped audit emit


@pytest.mark.asyncio
async def test_pg_migrate_resolver_picks_up_migrated_row(_live_db):
    """Contract with :mod:`backend.llm_credential_resolver`: after
    migration the DB-first async resolver must surface the migrated
    row's plaintext key (decrypted) rather than falling through to
    the legacy Settings path."""
    # Patch Settings so only the .env key is visible, migrate it, then
    # BLANK the Settings attr and verify the async resolver still
    # returns the plaintext via the DB-first branch.
    p, _ = _patched_settings(anthropic_api_key="sk-ant-resolver-aaaa")
    try:
        from backend.legacy_llm_credential_migration import (
            migrate_legacy_llm_credentials_once,
        )
        out = await migrate_legacy_llm_credentials_once()
        assert out["migrated"] == 1
    finally:
        p.stop()

    # Now the row lives in llm_credentials. Blank Settings and confirm
    # the resolver reads through to the DB.
    from backend.db_context import set_tenant_id
    from backend import llm_credential_resolver as resolver

    set_tenant_id("t-default")
    try:
        with patch.object(resolver, "settings") as mock_settings:
            mock_settings.anthropic_api_key = ""
            mock_settings.ollama_base_url = ""
            cred = await resolver.get_llm_credential("anthropic")
            assert cred.source == "db"
            assert cred.api_key == "sk-ant-resolver-aaaa"
            assert cred.id == "lc-legacy-anthropic"
            assert cred.tenant_id == "t-default"
    finally:
        set_tenant_id(None)


async def _async_false() -> bool:
    """Helper: async coroutine that resolves to False — lets tests
    bypass the ``_table_has_any_row`` guard and exercise the ON
    CONFLICT second-line-of-defence."""
    return False
