"""Phase 5b-2 (#llm-credentials) — resolver unit + live-PG contract tests.

Layers
------
1. **Pure unit tests** (no DB, no pool) — exercise the sync variant
   + ``is_provider_configured`` + ``LLMCredentialMissingError``
   semantics using ``monkeypatch`` on ``backend.config.settings``.
2. **Async-no-pool unit tests** — drive :func:`get_llm_credential`
   without an asyncpg pool to confirm the "pool-not-initialised
   silently falls back" contract.
3. **Live PG contract tests** (gated on ``OMNI_TEST_PG_URL``) — run
   the full DB-first chain against a real ``llm_credentials`` table,
   verify decrypt + tenant isolation + fallback ordering.
4. **Integration with** :func:`backend.agents.llm.list_providers` —
   confirm the ``configured`` flag flows through the resolver.

Each test group has an inline comment explaining the contract it
protects; drop the test only alongside a deliberate contract change.
"""

from __future__ import annotations

import asyncio
import logging
import pytest

from backend.config import settings
from backend import llm_credential_resolver as lcr
from backend.llm_credential_resolver import (
    LLMCredentialMissingError,
    get_llm_credential,
    get_llm_credential_sync,
    is_provider_configured,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures: per-test warn-flag reset + empty-settings baseline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(autouse=True)
def _reset_warn_flag():
    """Each test starts with a fresh one-shot warn flag so multiple tests
    can exercise the "emit once" behaviour independently."""
    lcr._reset_legacy_warn_for_tests()
    yield
    lcr._reset_legacy_warn_for_tests()


@pytest.fixture
def empty_settings(monkeypatch):
    """Zero-out every LLM-related Settings field so tests start from a
    known-empty baseline and opt in to specific providers via monkeypatch.

    ``t-default`` tenant is implicit through the resolver's fallback.
    """
    for key_attr in (
        "anthropic_api_key", "google_api_key", "openai_api_key",
        "xai_api_key", "groq_api_key", "deepseek_api_key",
        "together_api_key", "openrouter_api_key",
    ):
        monkeypatch.setattr(settings, key_attr, "", raising=False)
    monkeypatch.setattr(settings, "ollama_base_url", "", raising=False)
    yield settings


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 1: sync resolver + Settings fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_sync_missing_raises_when_no_key(empty_settings):
    """Empty Settings + key-based provider → ``LLMCredentialMissingError``
    with the provider + tenant in the message (so log readers can grep)."""
    with pytest.raises(LLMCredentialMissingError) as excinfo:
        get_llm_credential_sync("anthropic")
    assert "anthropic" in str(excinfo.value)
    assert "t-default" in str(excinfo.value)
    assert "OMNISIGHT_ANTHROPIC_API_KEY" in str(excinfo.value)


def test_sync_reads_legacy_settings(monkeypatch, empty_settings):
    """A configured Settings field surfaces as source=settings."""
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-AAAA")
    cred = get_llm_credential_sync("anthropic")
    assert cred.provider == "anthropic"
    assert cred.api_key == "sk-ant-AAAA"
    assert cred.source == "settings"
    assert cred.tenant_id == "t-default"
    assert cred.id is None


def test_sync_strips_whitespace(monkeypatch, empty_settings):
    """A stray newline / trailing space in .env should not register as a
    configured key — whitespace is stripped, empty is still missing."""
    monkeypatch.setattr(settings, "anthropic_api_key", "   ")
    with pytest.raises(LLMCredentialMissingError):
        get_llm_credential_sync("anthropic")


def test_sync_ollama_keyless_always_resolves(monkeypatch, empty_settings):
    """Keyless provider resolves even when no Settings fields are set."""
    cred = get_llm_credential_sync("ollama")
    assert cred.provider == "ollama"
    assert cred.source == "keyless"
    assert cred.api_key == ""
    # ollama_base_url is empty in the fixture, so metadata omits it.
    assert "base_url" not in cred.metadata


def test_sync_ollama_threads_base_url_into_metadata(monkeypatch, empty_settings):
    """When set, ``ollama_base_url`` flows into ``metadata.base_url`` so
    the adapter has a single credential shape for both keyed + keyless
    providers."""
    monkeypatch.setattr(
        settings, "ollama_base_url", "http://ai_engine:11434",
    )
    cred = get_llm_credential_sync("ollama")
    assert cred.source == "keyless"
    assert cred.metadata["base_url"] == "http://ai_engine:11434"


def test_sync_explicit_tenant_overrides_contextvar(monkeypatch, empty_settings):
    """The ``tenant_id`` kwarg beats ``db_context.current_tenant_id``."""
    monkeypatch.setattr(settings, "google_api_key", "gk-BBBB")
    from backend import db_context
    token = db_context._tenant_var.set("t-ignored")
    try:
        cred = get_llm_credential_sync("google", tenant_id="t-explicit")
        assert cred.tenant_id == "t-explicit"
    finally:
        db_context._tenant_var.reset(token)


def test_sync_unknown_provider_raises(empty_settings):
    """Typo / unsupported provider → clean error, not a silent miss."""
    with pytest.raises(LLMCredentialMissingError) as excinfo:
        get_llm_credential_sync("Anthropic")  # capitalisation typo
    assert "Unknown provider" in str(excinfo.value)


def test_sync_empty_provider_raises():
    with pytest.raises(LLMCredentialMissingError):
        get_llm_credential_sync("")


def test_legacy_warn_emits_once(monkeypatch, empty_settings, caplog):
    """Cross-worker log-volume contract: the one-shot flag fires the warn
    the FIRST time a legacy-Settings fallback happens in a given worker
    and silences subsequent calls so operators don't get log spam."""
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-X")
    with caplog.at_level(logging.WARNING, logger="backend.llm_credential_resolver"):
        get_llm_credential_sync("anthropic")
        get_llm_credential_sync("anthropic")
        get_llm_credential_sync("anthropic")
    warn_lines = [
        r for r in caplog.records
        if r.name == "backend.llm_credential_resolver"
        and "legacy Settings" in r.getMessage()
    ]
    assert len(warn_lines) == 1, (
        f"expected one warn emission, got {len(warn_lines)}: "
        f"{[r.getMessage() for r in warn_lines]}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 1: is_provider_configured — used by list_providers()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_is_configured_true_when_key_set(monkeypatch, empty_settings):
    monkeypatch.setattr(settings, "openai_api_key", "sk-openai-AAAA")
    assert is_provider_configured("openai") is True


def test_is_configured_false_when_key_empty(empty_settings):
    assert is_provider_configured("openai") is False


def test_is_configured_true_for_ollama_even_without_base_url(empty_settings):
    """Keyless providers are always configured — that's the whole point
    of the ``requires_key=False`` branch in list_providers."""
    assert is_provider_configured("ollama") is True


def test_is_configured_false_for_unknown_provider(empty_settings):
    """Unknown provider → False, not a raised error (list_providers
    can't surface exceptions to the REST client)."""
    assert is_provider_configured("bogus") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 2: async resolver, no pool — falls through to Settings
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_async_no_pool_falls_back_to_settings(monkeypatch, empty_settings):
    """Pool not initialised (unit-test default) → resolver silently
    skips the DB lookup and reads legacy Settings. This is the
    empty-``llm_credentials`` deployment case (fresh install before
    row 5b-5 has anything to migrate)."""
    monkeypatch.setattr(settings, "groq_api_key", "gq-CCCC")
    cred = asyncio.run(get_llm_credential("groq"))
    assert cred.source == "settings"
    assert cred.api_key == "gq-CCCC"


def test_async_no_pool_missing_still_raises(empty_settings):
    with pytest.raises(LLMCredentialMissingError):
        asyncio.run(get_llm_credential("openai"))


def test_async_unknown_provider_raises(empty_settings):
    with pytest.raises(LLMCredentialMissingError) as excinfo:
        asyncio.run(get_llm_credential("not-a-provider"))
    assert "Unknown provider" in str(excinfo.value)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 3: Live PG contract — gated on OMNI_TEST_PG_URL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _insert_llm_row(
    pool,
    *,
    row_id: str,
    tenant_id: str,
    provider: str,
    plaintext: str,
    is_default: bool = True,
    enabled: bool = True,
    label: str = "",
    metadata: dict | None = None,
) -> None:
    """Helper: insert a row via direct INSERT (Phase 5b-3 CRUD is not
    yet landed; we go straight to asyncpg for these tests)."""
    from backend.secret_store import encrypt
    import json
    import time

    ciphertext = encrypt(plaintext) if plaintext else ""
    now = time.time()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO llm_credentials "
            "(id, tenant_id, provider, label, encrypted_value, metadata, "
            " auth_type, is_default, enabled, created_at, updated_at, version) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'pat', $7, $8, $9, $9, 0)",
            row_id, tenant_id, provider, label, ciphertext,
            json.dumps(metadata or {}), is_default, enabled, now,
        )


async def _ensure_tenant(pool, tid: str) -> None:
    """Insert a tenants row if missing — FK target for llm_credentials.

    Schema mirrors ``backend.alembic.versions.0011_i2_tenants``:
    ``(id, name, plan, created_at, enabled)``. ``created_at`` has a
    default of ``to_char(now(), ...)`` but asyncpg still prefers an
    explicit column list.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $1) "
            "ON CONFLICT (id) DO NOTHING",
            tid,
        )


async def _clean_llm_credentials(pool) -> None:
    """Clear ``llm_credentials`` so each live-PG test starts fresh.

    ``pg_test_pool`` is the non-transactional PG fixture; without this
    cleanup a test inserting a ``(t-default, anthropic, is_default=TRUE)``
    row leaves the partial-unique index blocking the next test from
    doing the same. `pg_test_conn` would have given us savepoint-
    rollback, but these tests drive the real pool (not a conn) so the
    resolver's ``get_pool()`` can find it — which means cleanup is our
    responsibility.
    """
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE llm_credentials")


@pytest.mark.asyncio
async def test_pg_db_row_wins_over_settings(pg_test_pool, monkeypatch, empty_settings):
    """Canonical contract: when a ``llm_credentials`` row exists for
    ``(tenant, provider)``, the resolver returns it — even if Settings
    has a non-empty key (DB is authoritative)."""
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-from-env")
    await _clean_llm_credentials(pg_test_pool)
    await _ensure_tenant(pg_test_pool, "t-default")
    await _insert_llm_row(
        pg_test_pool,
        row_id="lc-db-test-1",
        tenant_id="t-default",
        provider="anthropic",
        plaintext="sk-from-db",
        label="prod",
    )
    cred = await get_llm_credential("anthropic")
    assert cred.source == "db"
    assert cred.id == "lc-db-test-1"
    assert cred.api_key == "sk-from-db"
    assert cred.provider == "anthropic"
    assert cred.tenant_id == "t-default"


@pytest.mark.asyncio
async def test_pg_empty_table_falls_back_to_settings(
    pg_test_pool, monkeypatch, empty_settings,
):
    """No rows for ``(tenant, provider)`` → Settings fallback fires."""
    monkeypatch.setattr(settings, "together_api_key", "tg-XXXX")
    await _clean_llm_credentials(pg_test_pool)
    cred = await get_llm_credential("together")
    assert cred.source == "settings"
    assert cred.api_key == "tg-XXXX"


@pytest.mark.asyncio
async def test_pg_decrypt_failure_falls_through(
    pg_test_pool, monkeypatch, empty_settings,
):
    """A row with ciphertext that doesn't decrypt under the current
    Fernet key (e.g. key rotation without re-encrypt) must not KO the
    resolver — it logs + falls back to Settings so operators can hot-fix."""
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-env-backup")
    await _clean_llm_credentials(pg_test_pool)
    await _ensure_tenant(pg_test_pool, "t-default")
    # Insert a fake-ciphertext row (not a real Fernet blob).
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO llm_credentials "
            "(id, tenant_id, provider, label, encrypted_value, metadata, "
            " auth_type, is_default, enabled, created_at, updated_at, version) "
            "VALUES ('lc-bad', 't-default', 'anthropic', 'bad', "
            " 'not-a-fernet-blob', '{}'::jsonb, 'pat', TRUE, TRUE, 1, 1, 0)"
        )
    cred = await get_llm_credential("anthropic")
    # DB row was unreadable → fallback to Settings.
    assert cred.source == "settings"
    assert cred.api_key == "sk-env-backup"


@pytest.mark.asyncio
async def test_pg_tenant_isolation(pg_test_pool, monkeypatch, empty_settings):
    """Tenant A's credential must be invisible to tenant B and vice versa
    (the partial unique index + ``WHERE tenant_id = $1`` are load-bearing
    for the multi-tenant story)."""
    await _clean_llm_credentials(pg_test_pool)
    await _ensure_tenant(pg_test_pool, "t-A")
    await _ensure_tenant(pg_test_pool, "t-B")
    await _insert_llm_row(
        pg_test_pool,
        row_id="lc-a",
        tenant_id="t-A",
        provider="openai",
        plaintext="sk-for-A",
    )
    await _insert_llm_row(
        pg_test_pool,
        row_id="lc-b",
        tenant_id="t-B",
        provider="openai",
        plaintext="sk-for-B",
    )
    a = await get_llm_credential("openai", tenant_id="t-A")
    b = await get_llm_credential("openai", tenant_id="t-B")
    assert a.api_key == "sk-for-A"
    assert a.id == "lc-a"
    assert b.api_key == "sk-for-B"
    assert b.id == "lc-b"
    # Explicit C-tenant has nothing — falls through.
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(LLMCredentialMissingError):
        await get_llm_credential("openai", tenant_id="t-C")


@pytest.mark.asyncio
async def test_pg_metadata_round_trips(
    pg_test_pool, monkeypatch, empty_settings,
):
    """Per-account metadata (base_url / org_id / future OAuth scopes)
    must arrive decoded as a dict, not stringified JSON."""
    await _clean_llm_credentials(pg_test_pool)
    await _ensure_tenant(pg_test_pool, "t-default")
    await _insert_llm_row(
        pg_test_pool,
        row_id="lc-meta",
        tenant_id="t-default",
        provider="openai",
        plaintext="sk-scoped",
        metadata={"org_id": "org-abc", "base_url": "https://api.openai.com/v1"},
    )
    cred = await get_llm_credential("openai")
    assert cred.source == "db"
    assert cred.metadata == {
        "org_id": "org-abc",
        "base_url": "https://api.openai.com/v1",
    }


@pytest.mark.asyncio
async def test_pg_disabled_row_skipped(
    pg_test_pool, monkeypatch, empty_settings,
):
    """``enabled=FALSE`` rows must be ignored by the resolver even when
    they're the only row for a provider (soft-disable preserves audit
    history without accidentally flipping the resolver to that key)."""
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-env-fallback")
    await _clean_llm_credentials(pg_test_pool)
    await _ensure_tenant(pg_test_pool, "t-default")
    await _insert_llm_row(
        pg_test_pool,
        row_id="lc-disabled",
        tenant_id="t-default",
        provider="deepseek",
        plaintext="sk-disabled",
        enabled=False,
    )
    cred = await get_llm_credential("deepseek")
    assert cred.source == "settings"
    assert cred.api_key == "sk-env-fallback"


@pytest.mark.asyncio
async def test_pg_default_row_beats_lru(
    pg_test_pool, monkeypatch, empty_settings,
):
    """When multiple rows exist for the same (tenant, provider), the
    ``is_default=TRUE`` row wins regardless of ``last_used_at``."""
    import time
    await _clean_llm_credentials(pg_test_pool)
    await _ensure_tenant(pg_test_pool, "t-default")
    await _insert_llm_row(
        pg_test_pool,
        row_id="lc-default",
        tenant_id="t-default",
        provider="xai",
        plaintext="sk-default-wins",
        is_default=True,
    )
    await _insert_llm_row(
        pg_test_pool,
        row_id="lc-recent",
        tenant_id="t-default",
        provider="xai",
        plaintext="sk-recent-loses",
        is_default=False,
    )
    # Bump the non-default's LRU so ``last_used_at DESC`` would prefer it
    # if we forgot to ``ORDER BY is_default DESC`` first.
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE llm_credentials SET last_used_at = $1 WHERE id = 'lc-recent'",
            time.time() + 10,
        )
    cred = await get_llm_credential("xai")
    assert cred.id == "lc-default"
    assert cred.api_key == "sk-default-wins"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Layer 4: Integration with backend.agents.llm
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_list_providers_configured_flows_through_resolver(
    monkeypatch, empty_settings,
):
    """``list_providers()`` must drive its ``configured`` flag from the
    resolver — NOT from a direct Settings read. Regression guard: a
    future refactor that re-inlines ``bool(settings.*_api_key)`` would
    diverge the REST surface from get_llm and re-introduce the "shows
    configured but get_llm still returns None" bug class."""
    from backend.agents.llm import list_providers
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")
    monkeypatch.setattr(settings, "openai_api_key", "")
    providers = {p["id"]: p for p in list_providers()}
    assert providers["anthropic"]["configured"] is True
    assert providers["openai"]["configured"] is False
    assert providers["ollama"]["configured"] is True  # keyless


def test_create_llm_returns_none_when_missing(monkeypatch, empty_settings):
    """_create_llm translates ``LLMCredentialMissingError`` to None so the
    pre-existing failover cascade in get_llm is untouched."""
    from backend.agents import llm as llm_mod
    # Force build_chat_model to blow up if it's accidentally reached —
    # missing credential should short-circuit before the adapter call.
    def _explode(*args, **kwargs):
        raise AssertionError("build_chat_model called despite missing key")
    monkeypatch.setattr(llm_mod, "build_chat_model", _explode)
    result = llm_mod._create_llm("anthropic", model="claude-opus-4-7")
    assert result is None


def test_create_llm_threads_resolved_key_into_adapter(
    monkeypatch, empty_settings,
):
    """Happy path: resolved credential's ``api_key`` reaches
    build_chat_model verbatim (including when source=settings)."""
    from backend.agents import llm as llm_mod
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-ZZZZ")
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return "fake-chat-model"

    monkeypatch.setattr(llm_mod, "build_chat_model", _capture)
    out = llm_mod._create_llm("openrouter", model="anthropic/claude-haiku-4")
    assert out == "fake-chat-model"
    assert captured["api_key"] == "sk-or-ZZZZ"
    assert captured["provider"] == "openrouter"
    # OpenRouter ships aggregator headers — those must survive the
    # resolver refactor.
    assert "default_headers" in captured


def test_create_llm_ollama_threads_base_url_from_metadata(
    monkeypatch, empty_settings,
):
    """Keyless provider path: base_url comes from the resolver's
    ``metadata.base_url`` (populated from Settings today; from the DB
    row in the future)."""
    from backend.agents import llm as llm_mod
    monkeypatch.setattr(settings, "ollama_base_url", "http://ai_engine:11434")
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return "fake-ollama"

    monkeypatch.setattr(llm_mod, "build_chat_model", _capture)
    out = llm_mod._create_llm("ollama", model="gemma4:e4b")
    assert out == "fake-ollama"
    assert captured["base_url"] == "http://ai_engine:11434"
    # Ollama is keyless — api_key must be None at the adapter boundary.
    assert captured["api_key"] is None
