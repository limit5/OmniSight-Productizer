"""Phase 5b-2 (#llm-credentials) — LLM credential resolver.

Replaces the direct ``getattr(settings, f"{provider}_api_key")`` pattern
in :mod:`backend.agents.llm` with a layered read:

1. **``llm_credentials`` table** (pool-backed, per-tenant) — canonical
   source once row 5b-5 auto-migration populates it. Ciphertext is
   decrypted via :mod:`backend.secret_store`.
2. **Legacy ``Settings`` scalar fields** (``{provider}_api_key`` /
   ``ollama_base_url``) — read-only backward-compat fallback so the
   resolver behaves identically to pre-Phase-5b on an empty
   ``llm_credentials`` table.
3. **Missing** — raise :class:`LLMCredentialMissingError`, a
   :class:`LookupError` subclass so callers can catch "no credential"
   specifically without swallowing unrelated lookup errors.

Two entrypoints for two call shapes:

* :func:`get_llm_credential` — async, DB-first chain. Preferred going
  forward; future async paths (`list_providers` REST handler, CRUD,
  test-key probes) should use this.
* :func:`get_llm_credential_sync` — sync, Settings-only fallback.
  Used by :func:`backend.agents.llm.get_llm` / ``_create_llm`` which
  run on the sync LangChain factory path. The DB is deliberately NOT
  read here: asyncio interop from a sync context in a FastAPI worker
  would either block the event loop (via ``run_until_complete``) or
  cross-thread (via a fresh loop) and neither is safe at the
  granularity that ``get_llm()`` is called at (every agent step).
  Row 5b-5's auto-migration mirrors DB writes back into
  ``settings.{provider}_api_key`` — matching the existing
  ``load_into_settings()`` pattern in :mod:`backend.llm_secrets` —
  so the sync path still observes every write without a round-trip.

Module-global audit (SOP Step 1, 2026-04-21 rule)
-------------------------------------------------
One module-global: ``_LEGACY_WARN_EMITTED`` — one-shot deprecation
log flag, each uvicorn worker emits the same log line once on its
first fallback call. Qualified answer #1: "each worker derives the
same value from the same source" — the flag's presence is identical
in every worker by construction. No shared mutable state that would
require PG / Redis coordination.

Read-after-write audit
----------------------
Zero new write paths. The resolver is a read-only primitive in row
5b-2; writes land in row 5b-3 (CRUD) and row 5b-5 (legacy migration).
No serialisation boundary is being moved.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from backend.config import settings
from backend.db_context import current_tenant_id

logger = logging.getLogger(__name__)


# Canonical provider list — matches ``backend.alembic.versions.0029_llm_credentials``
# CHECK constraint + ``backend.agents.llm.list_providers`` order (9 entries).
# Ollama is the only keyless provider; its attribute is ``None`` so the
# resolver knows not to require an API key.
_PROVIDER_KEY_ATTR: dict[str, Optional[str]] = {
    "anthropic": "anthropic_api_key",
    "google": "google_api_key",
    "openai": "openai_api_key",
    "xai": "xai_api_key",
    "groq": "groq_api_key",
    "deepseek": "deepseek_api_key",
    "together": "together_api_key",
    "openrouter": "openrouter_api_key",
    "ollama": None,
}


_LEGACY_WARN_EMITTED: bool = False


class LLMCredentialMissingError(LookupError):
    """No credential could be resolved for the requested (provider, tenant).

    Distinct from a bare :class:`LookupError` so call sites that surface a
    "configure your Anthropic API key" toast / banner can catch this
    specifically without swallowing unrelated key errors. The message
    always carries the ``provider`` and ``tenant_id`` so log readers
    immediately know the failing scope.
    """


@dataclass(frozen=True)
class LLMCredential:
    """Resolved credential record.

    ``api_key`` is the plaintext secret — empty string for keyless
    providers (today: ``ollama``). ``source`` tags which tier of the
    resolver chain produced the value, so debugging "why is my worker
    still on the legacy key" is just a log-line grep away.
    """

    provider: str
    tenant_id: str
    api_key: str
    source: str  # "db" | "settings" | "keyless"
    metadata: dict = field(default_factory=dict)
    id: Optional[str] = None  # ``llm_credentials.id`` when source == "db"


def _resolve_tenant(tenant_id: Optional[str]) -> str:
    """Explicit kwarg → contextvar → ``t-default`` fallback.

    Matches :func:`backend.git_credentials._resolve_tenant` semantics
    so Phase-5 and Phase-5b tenant-scoping behave identically.
    """
    if tenant_id:
        return tenant_id
    ctx = current_tenant_id()
    if ctx:
        return ctx
    return "t-default"


def _emit_legacy_warn_once(provider: str) -> None:
    """One-shot deprecation warning per worker process.

    The log carries the provider that triggered the fallback so operators
    rolling out row 5b-5's auto-migration can see at a glance which
    providers still have traffic on the legacy read path. The warn is
    intentionally NOT reset on settings reload — we want a single
    breadcrumb per process lifetime, not one per PUT /runtime/settings.
    """
    global _LEGACY_WARN_EMITTED
    if _LEGACY_WARN_EMITTED:
        return
    logger.warning(
        "llm_credential_resolver: reading provider=%s API key from legacy "
        "Settings (OMNISIGHT_%s_API_KEY). Phase 5b-2 shim — migrate to "
        "llm_credentials table via row 5b-3 CRUD or row 5b-5 auto-"
        "migration; see docs/phase-5b-llm-credentials/01-design.md.",
        provider, provider.upper(),
    )
    _LEGACY_WARN_EMITTED = True


def _legacy_settings_credential(
    provider: str,
    tenant_id: str,
) -> Optional[LLMCredential]:
    """Synthesise an :class:`LLMCredential` from legacy Settings fields.

    Returns ``None`` only when the provider is key-based AND no key is
    configured. Keyless providers (``ollama``) always yield a credential
    — the resolver treats "no API key needed" as "configured".

    The Ollama branch threads ``base_url`` through ``metadata`` so the
    adapter has everything it needs without reaching back into
    ``settings.ollama_base_url`` directly (keeping the resolver's
    contract tight).
    """
    key_attr = _PROVIDER_KEY_ATTR.get(provider)

    if key_attr is None:
        # Keyless providers — today just Ollama. Always considered
        # "configured" since there's nothing to configure.
        meta: dict = {}
        if provider == "ollama":
            base_url = (getattr(settings, "ollama_base_url", "") or "").strip()
            if base_url:
                meta["base_url"] = base_url
        return LLMCredential(
            provider=provider,
            tenant_id=tenant_id,
            api_key="",
            source="keyless",
            metadata=meta,
            id=None,
        )

    api_key = (getattr(settings, key_attr, "") or "").strip()
    if not api_key:
        return None

    _emit_legacy_warn_once(provider)
    return LLMCredential(
        provider=provider,
        tenant_id=tenant_id,
        api_key=api_key,
        source="settings",
        metadata={},
        id=None,
    )


def _missing_error(provider: str, tenant_id: str) -> LLMCredentialMissingError:
    key_attr = _PROVIDER_KEY_ATTR.get(provider)
    if key_attr is None:
        # Should never fire — keyless providers always resolve. Defensive
        # so a future provider added to ``_PROVIDER_KEY_ATTR`` with a
        # ``None`` attr gets a clean error instead of a silent miss.
        return LLMCredentialMissingError(
            f"Provider {provider!r} is keyless but resolution still failed "
            f"(tenant={tenant_id!r}); likely an internal bug in the resolver."
        )
    env_name = f"OMNISIGHT_{provider.upper()}_API_KEY"
    return LLMCredentialMissingError(
        f"No LLM credential for provider={provider!r} tenant={tenant_id!r}. "
        f"Either set {env_name} in .env (legacy) or create an "
        f"llm_credentials row via POST /api/v1/llm-credentials (row 5b-3)."
    )


async def _fetch_db_row(provider: str, tenant_id: str) -> Optional[dict]:
    """Pool-backed read from ``llm_credentials``; ``None`` when empty /
    pool not initialised / SQLite dev.

    Returns the first enabled row matching ``(tenant_id, provider)``
    ordered ``is_default DESC, last_used_at DESC NULLS LAST, id`` —
    same ordering contract Phase-5 established for multi-account rows.
    Decrypts ``encrypted_value`` via :mod:`backend.secret_store`;
    a decrypt failure logs and returns ``None`` so a single bad row
    doesn't take down the resolver for the rest of the tenant.
    """
    try:
        from backend.db_pool import get_pool
        pool = get_pool()
    except RuntimeError:
        return None

    sql = (
        "SELECT id, tenant_id, provider, encrypted_value, metadata "
        "FROM llm_credentials "
        "WHERE tenant_id = $1 AND provider = $2 AND enabled = TRUE "
        "ORDER BY is_default DESC, last_used_at DESC NULLS LAST, id "
        "LIMIT 1"
    )
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, tenant_id, provider)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug(
            "llm_credential_resolver._fetch_db_row(%s,%s) failed: %s — "
            "falling back to legacy Settings",
            tenant_id, provider, type(exc).__name__,
        )
        return None
    if row is None:
        return None

    from backend.secret_store import decrypt

    ciphertext = row["encrypted_value"] or ""
    if ciphertext:
        try:
            plaintext = decrypt(ciphertext)
        except Exception as exc:
            logger.warning(
                "llm_credentials row %s: decrypt failed (%s) — treating as "
                "missing so the resolver can fall back to legacy Settings",
                row["id"], type(exc).__name__,
            )
            return None
    else:
        plaintext = ""

    metadata_raw = row["metadata"]
    if isinstance(metadata_raw, str):
        try:
            metadata = json.loads(metadata_raw) or {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    elif isinstance(metadata_raw, dict):
        metadata = metadata_raw
    else:
        metadata = {}

    return {
        "id": row["id"],
        "provider": row["provider"],
        "api_key": plaintext,
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


async def get_llm_credential(
    provider: str,
    tenant_id: Optional[str] = None,
) -> LLMCredential:
    """Resolve the credential for (provider, tenant) — DB-first chain.

    Resolution order:

    1. ``llm_credentials`` table for ``(tenant_id, provider)``.
    2. Legacy ``settings.{provider}_api_key`` (or keyless short-circuit
       for ``ollama``).
    3. :class:`LLMCredentialMissingError`.

    Tenant scope: explicit ``tenant_id`` kwarg overrides the contextvar;
    otherwise uses :func:`backend.db_context.current_tenant_id` with a
    ``t-default`` fallback (matches Phase-5 convention).

    ``provider`` must be one of the 9 canonical ids (see
    :data:`_PROVIDER_KEY_ATTR`). Unknown providers raise
    :class:`LLMCredentialMissingError` immediately rather than fall
    through — a typo is always a caller bug, and we want a clean
    diagnostic not a "no key" false-positive.
    """
    if not provider:
        raise LLMCredentialMissingError("provider argument is required")
    if provider not in _PROVIDER_KEY_ATTR:
        raise LLMCredentialMissingError(
            f"Unknown provider {provider!r} — valid: "
            f"{sorted(_PROVIDER_KEY_ATTR.keys())}"
        )

    tid = _resolve_tenant(tenant_id)

    row = await _fetch_db_row(provider, tid)
    if row is not None:
        return LLMCredential(
            provider=row["provider"],
            tenant_id=tid,
            api_key=row["api_key"],
            source="db",
            metadata=dict(row["metadata"]),
            id=row["id"],
        )

    legacy = _legacy_settings_credential(provider, tid)
    if legacy is not None:
        return legacy

    raise _missing_error(provider, tid)


def get_llm_credential_sync(
    provider: str,
    tenant_id: Optional[str] = None,
) -> LLMCredential:
    """Sync sibling of :func:`get_llm_credential` — Settings-only.

    Rationale: :func:`backend.agents.llm.get_llm` is synchronous and
    called from dozens of sync LangChain call sites (agent step
    functions, LCEL chains, tool wrappers). Making it async would
    cascade an ``await`` wave across the whole agent runtime. Instead
    the sync path reads ``settings.{provider}_api_key`` which — via
    :mod:`backend.llm_secrets`' ``load_into_settings`` +
    row 5b-5's future auto-migration — stays in lock-step with the DB
    table on every write. The DB is authoritative; Settings is the
    sync-readable mirror.

    Same contract as the async variant otherwise: raises
    :class:`LLMCredentialMissingError` on unknown or unconfigured
    providers; returns a ``source="keyless"`` record for ``ollama``.
    """
    if not provider:
        raise LLMCredentialMissingError("provider argument is required")
    if provider not in _PROVIDER_KEY_ATTR:
        raise LLMCredentialMissingError(
            f"Unknown provider {provider!r} — valid: "
            f"{sorted(_PROVIDER_KEY_ATTR.keys())}"
        )

    tid = _resolve_tenant(tenant_id)
    legacy = _legacy_settings_credential(provider, tid)
    if legacy is not None:
        return legacy
    raise _missing_error(provider, tid)


def is_provider_configured(
    provider: str,
    tenant_id: Optional[str] = None,
) -> bool:
    """Sync boolean probe — used by :func:`backend.agents.llm.list_providers`.

    True when either (a) the provider has a non-empty API key via
    Settings, or (b) the provider is keyless (today only ``ollama``).
    False when the provider is unknown OR when it is key-based and no
    key is configured.

    Swallows :class:`LLMCredentialMissingError` — ``list_providers``
    needs a boolean, not an exception, per the REST contract.
    """
    try:
        cred = get_llm_credential_sync(provider, tenant_id)
    except LLMCredentialMissingError:
        return False
    return cred.source == "keyless" or bool(cred.api_key)


def _reset_legacy_warn_for_tests() -> None:
    """Reset the one-shot legacy-warn flag.

    Intended only for unit tests that assert the log fires on a fresh
    process. Production code must not call this — flipping the flag
    defeats the "one line per worker" contract operators rely on to
    spot legacy-read volume during the Phase-5b rollout.
    """
    global _LEGACY_WARN_EMITTED
    _LEGACY_WARN_EMITTED = False
