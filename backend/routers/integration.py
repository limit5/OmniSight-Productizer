"""System integration settings — view, update, and test external connections."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets as _secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import json

from backend import auth as _au
from backend.config import (
    LEGACY_CREDENTIAL_FIELDS,
    LEGACY_LLM_CREDENTIAL_FIELDS,
    is_legacy_credential_field,
    is_legacy_llm_credential_field,
    settings,
)
from backend.db_context import set_tenant_id
from backend.shared_state import SharedKV

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Runtime settings cross-worker sync (2026-04-22)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Operator found that saving Google API key → SAVE & APPLY → Google
# went green, then saving OpenAI API key → OpenAI green but Google
# dark + field cleared. Root cause:
#
#   * Prod runs 2 replicas × ``OMNISIGHT_WORKERS=2`` = 4 separate
#     Python processes, each with its own ``settings`` singleton
#     in memory.
#   * ``PUT /runtime/settings`` calls ``setattr(settings, key, value)``
#     which only mutates the CURRENT worker's in-memory state.
#   * Caddy round-robins subsequent ``/providers`` + ``/settings``
#     polls across all 4 workers. Operator's save-Google request
#     landed on worker-1; save-OpenAI landed on worker-3 (which
#     had empty ``settings.google_api_key`` from its own startup
#     env load). The next ``/providers`` poll round-robined back
#     to worker-2/3/4 → saw OpenAI=set, Google=empty → UI rendered
#     Google grey + empty input.
#
# Proper fix is Phase 5b (DB-persisted, encrypted, per-tenant). This
# file provides a **bridging fix** that unblocks the current UX:
# route writes through Redis-backed ``SharedKV`` (all 4 workers see
# the same hash) and overlay Redis state onto the local ``settings``
# object before every read. Redis is already required infra (we use
# it for the rate limiter + SSE fan-out), so this adds zero new
# deps. Redis persistence itself (RDB / AOF) means values now
# survive backend restarts too — gain we didn't expect when
# planning Phase 5b as "DB-only".
#
# Trade-offs vs Phase 5b:
#   * Redis is shared global state — not per-tenant. Phase 5b adds
#     tenant scoping.
#   * Values are Fernet-unencrypted in Redis (plaintext inside
#     Redis hash). Phase 5b encrypts via ``secret_store``. Given
#     Redis already stores other secrets (rate-limit buckets with
#     token refill tokens are fine; SharedTokenUsage is fine; audit
#     hash chain is in PG) the blast radius matches our existing
#     Redis trust model. Operator deploys protect Redis with
#     docker-network isolation + AUTH. Still — this is a known
#     gap until 5b fully lands.
#   * No CRUD endpoints (just mutate via ``PUT /runtime/settings``);
#     Phase 5b adds a proper ``POST /llm-credentials`` API with
#     live test-key + rotation affordances.

_runtime_settings_kv = SharedKV("runtime_settings")

# Fields whose cross-worker coherence is operator-visible. Any
# field the SYSTEM INTEGRATIONS modal (or a wizard / rotate flow)
# mutates at runtime needs to be here — otherwise ``PUT /settings``
# lands on one worker, ``GET /settings`` round-robins to another,
# and the modal shows the token field blank after save (the exact
# 2026-04-22 GitHub/GitLab "消失了" symptom).
#
# Split by storage semantics:
#   * ``_SHARED_KV_STR_FIELDS``  — stored verbatim in Redis, string type.
#   * ``_SHARED_KV_TYPED_FIELDS`` — bool / int fields; stored as
#     ``str(value)`` in Redis, coerced back to the native type on
#     overlay via ``_coerce_kv_value``. Added 2026-04-22 after
#     finalize_gerrit_integration exposed the gap: without typed
#     mirroring, ``gerrit_enabled`` (bool) and ``gerrit_ssh_port``
#     (int) stayed process-local — so wizard finalize on worker-A
#     left the other 3 workers treating Gerrit as disabled, which
#     caused intermittent 403s on inbound Gerrit webhooks and
#     half-the-time-missing replication pushes.
#
# IMPORTANT: never add a bool to ``_SHARED_KV_STR_FIELDS`` —
# ``setattr(settings, "gerrit_enabled", "False")`` writes a
# non-empty string which ``if settings.gerrit_enabled:`` evaluates
# truthy, the worst-case silent-bug corner. Put bools in
# ``_SHARED_KV_TYPED_FIELDS`` with ``bool`` as the type.
_SHARED_KV_STR_FIELDS: frozenset[str] = frozenset({
    # ── LLM (added 2026-04-22, commit 8d626489) ──
    "llm_provider", "llm_model", "llm_fallback_chain",
    "anthropic_api_key", "google_api_key", "openai_api_key",
    "xai_api_key", "groq_api_key", "deepseek_api_key",
    "together_api_key", "openrouter_api_key",
    "ollama_base_url",
    # ── Git forges (added 2026-04-22, same-day follow-up) ──
    # Root cause of operator's "GitHub/GitLab token 消失了"
    # report: save on worker-A, next getSettings round-robins
    # to worker-B, returns ``{github_token: ""}``, input goes
    # blank even though worker-A has the real token.
    "github_token", "gitlab_token", "gitlab_url",
    "git_ssh_key_path",
    # ── Gerrit ──
    "gerrit_url", "gerrit_ssh_host", "gerrit_project",
    "gerrit_replication_targets", "gerrit_webhook_secret",
    # ── JIRA / Slack / PagerDuty ──
    "notification_jira_url", "notification_jira_token",
    "notification_jira_project",
    # Y-prep.3 (#289) — JIRA inbound automation routing knobs. Same
    # cross-worker-coherence reason as the rest of this set: a wizard
    # edit on worker-A must show up in jira_event_router decisions on
    # workers B/C/D without restart, otherwise the artifact-packaging
    # pipeline silently runs the OLD whitelist on 3 of 4 workers and
    # operators see "fired sometimes" intermittency.
    "jira_intake_label", "jira_done_statuses",
    "notification_slack_webhook", "notification_slack_mention",
    "notification_pagerduty_key",
    # ── Inbound webhook HMAC secrets ──
    "github_webhook_secret", "gitlab_webhook_secret",
    "jira_webhook_secret",
    # ── CI/CD ──
    "ci_jenkins_url", "ci_jenkins_user", "ci_jenkins_api_token",
})

_SHARED_KV_TYPED_FIELDS: dict[str, type] = {
    # ── Gerrit bool/int (added 2026-04-22) ──
    # Without these, finalize_gerrit_integration / wizard /
    # flat PUT-settings only mutated the receiving worker and
    # the other 3 kept ``gerrit_enabled=False`` from ``.env``.
    "gerrit_enabled": bool,
    "gerrit_ssh_port": int,
    # ── CI enable toggles ──
    # Same multi-worker shape as Gerrit — toggling Jenkins / GitHub
    # Actions / GitLab-CI in the modal should converge without
    # needing an ``.env`` rewrite + restart.
    "ci_github_actions_enabled": bool,
    "ci_jenkins_enabled": bool,
    "ci_gitlab_enabled": bool,
    # ── Docker sandbox toggle ──
    "docker_enabled": bool,
}

# Backwards-compatible union membership test. Existing call sites
# use ``if key in _SHARED_KV_FIELDS`` to decide "should I mirror"
# — keep that idiom working against the merged field set.
_SHARED_KV_FIELDS: frozenset[str] = (
    _SHARED_KV_STR_FIELDS | frozenset(_SHARED_KV_TYPED_FIELDS.keys())
)


def _coerce_kv_value(key: str, raw: str):
    """Coerce a Redis-stored string back to the native type declared
    in ``_SHARED_KV_TYPED_FIELDS``. Returns ``_SENTINEL_SKIP`` if the
    value can't be coerced — caller should leave the local setting
    untouched rather than write a corrupted value.

    Bool semantics match what operator-input dicts + Python's
    ``str(True)``/``str(False)`` produce; anything else (including
    an empty string or garbled Redis value) resolves to ``False``
    conservatively rather than raising.
    """
    typ = _SHARED_KV_TYPED_FIELDS.get(key)
    if typ is None:
        return raw  # plain string field
    if typ is bool:
        return str(raw).strip().lower() in ("true", "1", "yes", "on")
    if typ is int:
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return _SENTINEL_SKIP
    return raw


_SENTINEL_SKIP = object()


def _apply_runtime_setting(key: str, value) -> None:
    """Write a runtime setting to THIS worker's in-memory ``settings``
    AND mirror to Redis-backed ``SharedKV`` so peer workers see it
    on their next overlay.

    Use this helper instead of a raw ``setattr(settings, key, value)``
    anywhere a wizard / rotate / finalize flow mutates an integration
    config outside the generic ``PUT /settings`` path. Keeps the
    mirroring policy and type-aware serialisation centralised.
    Typed fields (bool / int) are stored as ``str(value)`` in Redis
    and coerced back on overlay — see ``_coerce_kv_value``.

    Phase 5-10 (#multi-account-forge): when the field is a deprecated
    credential (listed in ``config.LEGACY_CREDENTIAL_FIELDS``), emit
    an ``audit.log`` row via the fire-and-forget sync wrapper so the
    rotate / wizard / finalize entry points carry the same telemetry
    as ``PUT /runtime/settings``. ``log_sync`` never raises — silently
    noops when there's no running loop (unit-test edge case).
    """
    if hasattr(settings, key):
        setattr(settings, key, value)
    if key in _SHARED_KV_FIELDS:
        try:
            _runtime_settings_kv.set(key, str(value))
        except Exception as exc:
            logger.warning(
                "SharedKV mirror failed for %s: %s (local setattr "
                "still in effect, cross-worker coherence degraded)",
                key, exc,
            )
    if is_legacy_credential_field(key):
        replacement_hint = LEGACY_CREDENTIAL_FIELDS[key]
        logger.warning(
            "Phase-5-10 deprecated-write: settings.%s — "
            "authoritative source is %s",
            key, replacement_hint,
        )
        try:
            from backend import audit as _audit
            _audit.log_sync(
                action="settings.legacy_credential_write",
                entity_kind="settings_legacy_field",
                entity_id=key,
                before=None,
                after={
                    "field": key,
                    "replacement": replacement_hint,
                    "note": "legacy scalar write via wizard/rotate; "
                            "authoritative source is git_accounts (Phase 5)",
                },
                actor="system",
            )
        except Exception as exc:
            logger.warning(
                "audit-warn for deprecated settings write failed: %s "
                "(write itself took effect; audit row missing)",
                exc,
            )


def _overlay_runtime_settings() -> None:
    """Read Redis-backed runtime settings into the local ``settings``
    singleton so subsequent ``getattr(settings, ...)`` reads reflect
    mutations made by any worker.

    Cheap: single ``hgetall`` round-trip to Redis, falls back to
    local dict on Redis failure. Called at the top of the GET /
    PUT handlers and from ``list_providers`` hot path. Typed fields
    (bool / int) are coerced from the Redis string back to their
    native type before setattr.
    """
    try:
        overlay = _runtime_settings_kv.get_all()
        if not overlay:
            return
        for key, raw in overlay.items():
            if key not in _SHARED_KV_FIELDS:
                continue
            if not hasattr(settings, key):
                continue
            coerced = _coerce_kv_value(key, raw)
            if coerced is _SENTINEL_SKIP:
                logger.debug(
                    "overlay skipped: %s = %r (coercion failed)",
                    key, raw,
                )
                continue
            setattr(settings, key, coerced)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("runtime settings overlay skipped: %s", exc)

# Phase-3 P6 (2026-04-20): prefix renamed /system → /runtime — see
# backend/routers/system.py for the full rationale. This router shares
# the same prefix to keep the URL namespace coherent.
router = APIRouter(prefix="/runtime", tags=["integration"])


def _mask(value: str) -> str:
    """Mask sensitive values for API response."""
    if not value or len(value) < 8:
        return "***" if value else ""
    return value[:3] + "*" * min(len(value) - 6, 20) + value[-3:]


def _get_masked_credentials() -> list[dict]:
    """Get credential registry with tokens masked for API response."""
    try:
        from backend.git_credentials import get_credential_registry
        registry = get_credential_registry()
        return [
            {
                "id": r.get("id", ""),
                "url": r.get("url", ""),
                "platform": r.get("platform", "unknown"),
                "token": _mask(r.get("token", "")),
                "ssh_key": r.get("ssh_key", ""),
                "ssh_host": r.get("ssh_host", ""),
                "ssh_port": r.get("ssh_port", 0),
                "project": r.get("project", ""),
                "has_secret": bool(r.get("webhook_secret", "")),
            }
            for r in registry
        ]
    except Exception:
        return []


async def _get_tenant_secrets_summary(user) -> dict:
    """Fetch tenant-scoped secrets grouped by type for the settings view."""
    try:
        tid = getattr(user, "tenant_id", "t-default")
        set_tenant_id(tid)
        from backend import tenant_secrets as sec
        items = await sec.list_secrets()
        grouped: dict[str, list] = {}
        for s in items:
            grouped.setdefault(s["secret_type"], []).append({
                "id": s["id"],
                "key_name": s["key_name"],
                "fingerprint": s["fingerprint"],
                "metadata": s["metadata"],
                "updated_at": s["updated_at"],
            })
        return {"tenant_id": tid, "secrets": grouped}
    except Exception:
        return {"tenant_id": getattr(user, "tenant_id", "t-default"), "secrets": {}}


@router.get("/settings")
async def get_settings(_user=Depends(_au.require_operator)):
    """Return all integration settings grouped by category. Tokens are masked."""
    # Cross-worker sync: any ``PUT /settings`` that landed on a
    # different worker has mirrored its writes to the Redis-backed
    # ``SharedKV``. Overlay those here so the response reflects the
    # full picture, not just this worker's local ``setattr`` history.
    _overlay_runtime_settings()
    tenant_secrets = await _get_tenant_secrets_summary(_user)
    return {
        "llm": {
            "provider": settings.llm_provider,
            "model": settings.get_model_name(),
            "temperature": settings.llm_temperature,
            "fallback_chain": settings.llm_fallback_chain,
            "anthropic_api_key": _mask(settings.anthropic_api_key),
            "google_api_key": _mask(settings.google_api_key),
            "openai_api_key": _mask(settings.openai_api_key),
            "xai_api_key": _mask(settings.xai_api_key),
            "groq_api_key": _mask(settings.groq_api_key),
            "deepseek_api_key": _mask(settings.deepseek_api_key),
            "together_api_key": _mask(settings.together_api_key),
            "openrouter_api_key": _mask(settings.openrouter_api_key),
            "ollama_base_url": settings.ollama_base_url,
        },
        "git": {
            "ssh_key_path": settings.git_ssh_key_path,
            "github_token": _mask(settings.github_token),
            "gitlab_token": _mask(settings.gitlab_token),
            "gitlab_url": settings.gitlab_url,
            "credentials": _get_masked_credentials(),
        },
        "gerrit": {
            "enabled": settings.gerrit_enabled,
            "url": settings.gerrit_url,
            "ssh_host": settings.gerrit_ssh_host,
            "ssh_port": settings.gerrit_ssh_port,
            "project": settings.gerrit_project,
            "replication_targets": settings.gerrit_replication_targets,
        },
        "jira": {
            "url": settings.notification_jira_url,
            "token": _mask(settings.notification_jira_token),
            "project": settings.notification_jira_project,
            # Y-prep.3 (#289) — surfaced unmasked: these are routing knobs,
            # not credentials. Empty string = router uses built-in default
            # (``omnisight-intake`` / ``Done,Closed``); the UI labels the
            # placeholder with the default so an operator immediately knows
            # what's running without having to read source.
            "intake_label": settings.jira_intake_label,
            "done_statuses": settings.jira_done_statuses,
        },
        "slack": {
            "webhook": _mask(settings.notification_slack_webhook),
            "mention": settings.notification_slack_mention,
        },
        "pagerduty": {
            "key": _mask(settings.notification_pagerduty_key),
        },
        "webhooks": {
            "github_secret": "configured" if settings.github_webhook_secret else "",
            "gitlab_secret": "configured" if settings.gitlab_webhook_secret else "",
            "gerrit_secret": "configured" if settings.gerrit_webhook_secret else "",
            "jira_secret": "configured" if settings.jira_webhook_secret else "",
        },
        "ci": {
            "github_actions_enabled": settings.ci_github_actions_enabled,
            "jenkins_enabled": settings.ci_jenkins_enabled,
            "jenkins_url": settings.ci_jenkins_url,
            "jenkins_user": settings.ci_jenkins_user,
            # B14 Part D row 235 — per-field status indicator for the CI/CD
            # tab needs a "is the Jenkins API token wired up" signal without
            # leaking the plaintext secret. Same contract as the
            # `webhooks.*_secret` keys: "configured" or "" — never the
            # actual token. Jenkins URL + user stay plaintext (URL and
            # username are not secrets).
            "jenkins_api_token": "configured" if settings.ci_jenkins_api_token else "",
            "gitlab_ci_enabled": settings.ci_gitlab_enabled,
        },
        "docker": {
            "enabled": settings.docker_enabled,
            "memory_limit": settings.docker_memory_limit,
            "cpu_limit": settings.docker_cpu_limit,
        },
        "tenant_secrets": tenant_secrets,
    }


class SettingsUpdate(BaseModel):
    """Flat key-value update — keys match config.py field names."""
    updates: dict[str, str | int | float | bool]


# Whitelist of fields safe to update at runtime.
#
# Phase 5b-6 (2026-04-24) removed the 8 ``{provider}_api_key`` fields
# + ``ollama_base_url`` from this set. Those fields are now owned by
# the ``llm_credentials`` table (Phase 5b-1 through 5b-5) and writes
# must go through ``POST /api/v1/llm-credentials`` so the value is
# Fernet-encrypted + per-tenant scoped + persisted across backend
# restarts. The PUT handler below surfaces a 200 response with
# ``rejected[<field>]="deprecated: use POST /api/v1/llm-credentials"``
# + audit trail when a legacy UI keeps writing them.
_UPDATABLE_FIELDS = frozenset({
    "llm_provider", "llm_model", "llm_temperature", "llm_fallback_chain",
    "github_token", "gitlab_token", "gitlab_url", "git_ssh_key_path",
    "gerrit_enabled", "gerrit_url", "gerrit_ssh_host", "gerrit_ssh_port",
    "gerrit_project", "gerrit_replication_targets",
    "notification_jira_url", "notification_jira_token", "notification_jira_project",
    "jira_intake_label", "jira_done_statuses",
    "notification_slack_webhook", "notification_slack_mention",
    "notification_pagerduty_key",
    "github_webhook_secret", "gitlab_webhook_secret", "jira_webhook_secret",
    "ci_github_actions_enabled", "ci_jenkins_enabled", "ci_jenkins_url",
    "ci_jenkins_user", "ci_jenkins_api_token", "ci_gitlab_enabled",
    "docker_enabled", "docker_memory_limit", "docker_cpu_limit",
})


@router.put("/settings")
async def update_settings(body: SettingsUpdate, _user=Depends(_au.require_admin)):
    """Update integration settings at runtime.

    Values land on this worker's in-memory ``settings`` singleton AND
    (for the subset listed in ``_SHARED_KV_FIELDS``) are mirrored into
    Redis-backed ``SharedKV`` so the other 3 workers overlay them on
    their next request. This fixes the 2026-04-22 operator report
    where saving Google then OpenAI caused Google's green light to
    disappear — each save was landing on a different worker and the
    UI was round-robin reading from workers that hadn't seen the
    prior save. Full DB-persistent per-tenant solution lands in
    Phase 5b.
    """
    # Pull peer-worker writes in before we apply ours — means our
    # response's ``applied``/``rejected`` classification reflects the
    # merged view, not this worker's stale startup copy.
    _overlay_runtime_settings()

    applied = {}
    rejected = {}
    # Phase 5-10 (#multi-account-forge): collect legacy credential
    # writes for the audit trail + response banner. The set of fields
    # considered legacy lives in ``backend.config.LEGACY_CREDENTIAL_FIELDS``
    # (registry-of-truth); the write still applies (read-OK, write-warn
    # contract), but we log WHO kept using the superseded UI so operators
    # can track migration progress over time.
    deprecated_writes: dict[str, str] = {}
    # Phase 5b-6 (#llm-credentials): legacy LLM credential writes are
    # stronger than the Phase 5-10 warn-and-write pattern — the whole
    # point of 5b-1..5b-5 was to move keys out of process memory, so
    # we REJECT the write and point the caller at the new endpoint.
    # Still emit an audit row + response block so operators notice.
    deprecated_llm_rejects: dict[str, str] = {}
    actor_id = getattr(_user, "id", None) or getattr(_user, "email", None) or "admin"
    for key, value in body.updates.items():
        if key not in _UPDATABLE_FIELDS:
            # Phase 5b-6: legacy LLM credential writes deserve a more
            # actionable rejection reason than plain "not updatable" —
            # tell the caller where the field moved to.
            if is_legacy_llm_credential_field(key):
                replacement_hint = LEGACY_LLM_CREDENTIAL_FIELDS[key]
                rejected[key] = (
                    "deprecated: use POST /api/v1/llm-credentials "
                    f"(→ {replacement_hint})"
                )
                deprecated_llm_rejects[key] = replacement_hint
            else:
                rejected[key] = "not updatable"
            continue
        if not hasattr(settings, key):
            rejected[key] = "unknown field"
            continue
        setattr(settings, key, value)
        applied[key] = True
        if is_legacy_credential_field(key):
            deprecated_writes[key] = LEGACY_CREDENTIAL_FIELDS[key]
        # Mirror the subset whose coherence is operator-visible into
        # ``SharedKV`` so the other workers pick it up on their next
        # ``_overlay_runtime_settings()`` call. Silent failure on
        # Redis hiccup is acceptable — local ``setattr`` still took
        # effect and Redis is best-effort cross-worker sync, not
        # authoritative storage.
        if key in _SHARED_KV_FIELDS:
            try:
                _runtime_settings_kv.set(key, str(value))
            except Exception as exc:
                logger.warning(
                    "SharedKV mirror failed for %s: %s (local setattr "
                    "still in effect, cross-worker coherence degraded)",
                    key, exc,
                )

    # Phase 5-10: audit the legacy writes (best-effort — the write has
    # already taken effect, so an audit-pool hiccup must not fail the
    # HTTP mutation). One row per field keeps grep-by-field simple.
    if deprecated_writes:
        try:
            from backend import audit as _audit
            for key, replacement_hint in deprecated_writes.items():
                logger.warning(
                    "Phase-5-10 deprecated-write: settings.%s — "
                    "authoritative source is %s",
                    key, replacement_hint,
                )
                await _audit.log(
                    action="settings.legacy_credential_write",
                    entity_kind="settings_legacy_field",
                    entity_id=key,
                    before=None,
                    after={
                        "field": key,
                        "replacement": replacement_hint,
                        "note": "legacy scalar write; authoritative source is git_accounts (Phase 5)",
                    },
                    actor=str(actor_id),
                )
        except Exception as exc:
            logger.warning(
                "audit-warn for deprecated settings write failed: %s "
                "(write itself took effect; audit row missing)",
                exc,
            )

    # Phase 5b-6 (#llm-credentials): audit + warn-log for rejected
    # LLM credential writes. Plaintext value is explicitly NOT echoed
    # into the audit row (``after`` carries field metadata only) so a
    # snapshot of the audit log cannot be replayed to recover the
    # attempted key. Best-effort: a failed audit write must not poison
    # the HTTP response.
    if deprecated_llm_rejects:
        try:
            from backend import audit as _audit
            for key, replacement_hint in deprecated_llm_rejects.items():
                logger.warning(
                    "Phase-5b-6 deprecated-write rejected: settings.%s — "
                    "authoritative source is %s (use POST "
                    "/api/v1/llm-credentials)",
                    key, replacement_hint,
                )
                await _audit.log(
                    action="settings.legacy_llm_credential_write",
                    entity_kind="settings_legacy_llm_field",
                    entity_id=key,
                    before=None,
                    after={
                        "field": key,
                        "replacement": replacement_hint,
                        "note": "rejected — authoritative source is "
                                "llm_credentials (Phase 5b); caller should "
                                "POST /api/v1/llm-credentials instead",
                    },
                    actor=str(actor_id),
                )
        except Exception as exc:
            logger.warning(
                "audit-warn for deprecated LLM settings write failed: %s "
                "(write was already rejected; audit row missing)",
                exc,
            )

    # Clear LLM cache if provider/model/key changed
    llm_related = {"llm_", "anthropic_", "google_", "openai_", "xai_", "groq_", "deepseek_", "together_", "openrouter_", "ollama_"}
    llm_keys = [k for k in applied if any(k.startswith(p) for p in llm_related)]
    if llm_keys:
        try:
            from backend.agents.llm import _cache
            _cache.clear()
        except Exception:
            pass
        # Emit SSE event so Orchestrator panel can sync
        try:
            from backend.events import emit_invoke
            emit_invoke("provider_switch", f"{settings.llm_provider}/{settings.get_model_name()}")
        except Exception:
            pass

    # Q.3-SUB-5 (#297): cross-device push for the *non-LLM* subset
    # (Gerrit / JIRA / GitHub / GitLab / Slack / PagerDuty / webhooks
    # / CI / Docker). The LLM subset already owns the
    # ``invoke('provider_switch')`` emit above — splitting the two
    # keeps a pure-LLM save from double-firing. Previously the
    # SYSTEM INTEGRATIONS modal on a second device only refreshed on
    # modal re-open (``integration-settings.tsx:2680-2687``); this
    # push lets a passively-open modal repaint without the manual
    # close/reopen dance.  Best-effort — a flaky Redis / bus must not
    # fail the HTTP mutation (SharedKV still mirrored, cross-device
    # sync degrades to the old modal-open refetch).
    non_llm_applied = [k for k in applied if k not in llm_keys]
    if non_llm_applied:
        try:
            from backend.events import emit_integration_settings_updated
            emit_integration_settings_updated(non_llm_applied, scope="user")
        except Exception as exc:
            logger.debug(
                "emit_integration_settings_updated failed for %s: %s",
                non_llm_applied, exc,
            )

    logger.info("Settings updated: %s", list(applied.keys()))
    response: dict[str, object] = {
        "status": "updated",
        "applied": list(applied.keys()),
        "rejected": rejected,
        "note": "Changes persist via Redis across workers + restarts; "
                "full DB persistence lands in Phase 5b.",
    }
    # Phase 5-10 (#multi-account-forge): surface a deprecation banner
    # when the operator wrote a legacy credential field. The frontend
    # INTEGRATION SETTINGS modal reads this block and renders an inline
    # "move to Git Accounts" link; empty dict when no legacy writes.
    if deprecated_writes:
        response["deprecations"] = {
            "fields": deprecated_writes,
            "migrate_to": "git_accounts",
            "doc": "/docs/phase-5-multi-account/02-migration-runbook.md",
        }
    # Phase 5b-6 (#llm-credentials): surface a separate deprecation
    # banner for rejected LLM credential writes. The UI can use this
    # to redirect the user to the new ``POST /api/v1/llm-credentials``
    # endpoint. Separate key from Phase 5-10's ``deprecations`` block
    # so the UI can render different messaging (Phase 5b is REJECTED
    # not warned — the write did not take effect).
    if deprecated_llm_rejects:
        response["llm_deprecations"] = {
            "fields": deprecated_llm_rejects,
            "migrate_to": "llm_credentials",
            "endpoint": "/api/v1/llm-credentials",
            "doc": "/docs/ops/llm_credentials.md",
        }
    return response


@router.post("/test/{integration}")
async def test_integration(integration: str, _user=Depends(_au.require_admin)):
    """Test connectivity for an external integration."""
    tester = _TESTERS.get(integration)
    if not tester:
        raise HTTPException(400, f"Unknown integration: {integration}. Valid: {sorted(_TESTERS.keys())}")
    try:
        return await asyncio.wait_for(tester(), timeout=15)
    except asyncio.TimeoutError:
        return {"status": "error", "message": "Connection timed out (15s)"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ─── B14 Part A row 3: Git-forge token probe (Bootstrap Step 3.5) ──────
#
# Validates a *candidate* Git forge token supplied in the request body —
# does NOT mutate ``settings.github_token`` / ``settings.gitlab_token``.
# The Bootstrap wizard needs this because the operator is entering a
# brand-new token they haven't saved yet: reusing ``/system/test/github``
# would force a save-before-validate round-trip and leave a bad token
# persisted if validation fails.
#
# The existing ``/system/test/{integration}`` endpoint still exercises
# the currently-configured credential and is what Settings → Integration
# uses after the token has been written.

class GitForgeTokenTest(BaseModel):
    provider: str  # "github" | "gitlab" | "gerrit"
    token: str = ""
    url: str = ""  # optional — for GitLab self-hosted instances / Gerrit REST URL
    ssh_host: str = ""  # Gerrit only — `[user@]host` for the SSH probe
    ssh_port: int = 29418  # Gerrit only — SSH port (Gerrit default 29418)


# ─── B14 Part B row 217: masked read / PUT of the multi-instance token map ──
#
# Row 216 already lets the SAVE & APPLY flow serialise the instance list into
# ``settings.github_token_map`` / ``settings.gitlab_token_map`` via the generic
# ``PUT /system/settings`` endpoint — but the matching readback round-trips the
# raw JSON (token-bearing), which is unsafe to surface to the UI. This endpoint
# is the dedicated masked view: GET returns host-keyed entries with tokens
# reduced to the same ``_mask()`` shape used elsewhere; PUT accepts a full host
# → token list per-platform and writes the JSON form back to settings plus
# invalidates the credential cache so subsequent operations see the new map.


def _parse_token_map(raw: str) -> dict[str, str]:
    """Tolerant parse of a settings JSON map → {host: token}. Non-dict and
    invalid JSON both collapse to an empty map so callers never need to
    distinguish "unset" from "malformed"."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str) and k and v:
            out[k] = v
    return out


def _masked_instance_list(raw: str, platform: str) -> list[dict]:
    """Build the UI-friendly masked view of a {host: token} map. Stable
    ordering makes the endpoint round-trip predictable in tests."""
    entries = _parse_token_map(raw)
    return [
        {"platform": platform, "host": host, "token_masked": _mask(token)}
        for host, token in sorted(entries.items())
    ]


class TokenMapInstance(BaseModel):
    host: str
    token: str = ""  # blank on a PUT means "keep existing token for this host"


class TokenMapUpdate(BaseModel):
    github: list[TokenMapInstance] = []
    gitlab: list[TokenMapInstance] = []


@router.get("/settings/git/token-map")
async def get_git_token_map(_user=Depends(_au.require_operator)):
    """Return the configured per-host token maps with tokens masked.

    Shape::

        {
          "github": [{"platform": "github", "host": "...", "token_masked": "..."}],
          "gitlab": [...],
        }

    Empty platforms surface as empty lists — never ``null`` — so the UI
    can render "no additional instances configured" without branching on
    presence.
    """
    return {
        "github": _masked_instance_list(settings.github_token_map, "github"),
        "gitlab": _masked_instance_list(settings.gitlab_token_map, "gitlab"),
    }


@router.put("/settings/git/token-map")
async def update_git_token_map(
    body: TokenMapUpdate, _user=Depends(_au.require_admin),
):
    """Replace the per-host token maps.

    A blank ``token`` for a given host preserves the existing secret so the
    UI can round-trip the masked list without re-prompting every token.
    Removing a host just means omitting it from the PUT body — this
    endpoint is a replace, not a patch.

    Duplicate hosts in the payload are merged last-write-wins (the final
    entry in the list). Empty host strings are ignored.
    """

    def _merge(
        new: list[TokenMapInstance], existing_raw: str,
    ) -> tuple[str, int, int]:
        existing = _parse_token_map(existing_raw)
        merged: dict[str, str] = {}
        preserved = 0
        for inst in new:
            host = (inst.host or "").strip()
            if not host:
                continue
            token = inst.token
            if not token:
                # Blank token → keep whatever was already stored. If the
                # caller never supplied a token for a brand-new host the
                # entry is dropped rather than written as an empty string
                # (an empty token would silently break every credential
                # lookup for that host).
                prior = existing.get(host, "")
                if not prior:
                    continue
                token = prior
                preserved += 1
            merged[host] = token
        serialised = json.dumps(merged) if merged else ""
        return serialised, len(merged), preserved

    gh_json, gh_count, gh_preserved = _merge(body.github, settings.github_token_map)
    gl_json, gl_count, gl_preserved = _merge(body.gitlab, settings.gitlab_token_map)

    settings.github_token_map = gh_json
    settings.gitlab_token_map = gl_json

    # Bust the credential registry cache so the new map is observed by
    # `find_credential_for_url()` and friends without a process restart.
    try:
        from backend.git_credentials import clear_credential_cache
        clear_credential_cache()
    except Exception:  # pragma: no cover — defensive
        pass

    logger.info(
        "Token map updated: github=%d (kept %d) gitlab=%d (kept %d)",
        gh_count, gh_preserved, gl_count, gl_preserved,
    )
    return {
        "status": "updated",
        "github": _masked_instance_list(gh_json, "github"),
        "gitlab": _masked_instance_list(gl_json, "gitlab"),
        "note": "Changes are runtime-only and will reset on restart.",
    }


async def _probe_gerrit_ssh(ssh_host: str, ssh_port: int, url: str = "") -> dict:
    """Run ``ssh -p {port} {host} gerrit version`` against a *candidate*
    Gerrit SSH endpoint and return the parsed version. Never reads from
    or mutates ``settings``.

    B14 Part A row 5 — Bootstrap Step 3.5 Gerrit tab. Mirrors the
    GitHub / GitLab probes in spirit (non-mutating, timeout-bounded,
    structured ``{status, version|message}`` result) but uses SSH
    because Gerrit's canonical API over SSH (``gerrit version``) is the
    only probe that exercises the same transport the merger agent and
    the replication path will later use — a token-only HTTP probe would
    not catch SSH key / host-key mismatches.

    The host field may contain ``user@host`` (standard ssh syntax); the
    SSH key is pulled from the operator's running environment via the
    ssh client's default search path. ``StrictHostKeyChecking=accept-new``
    lets first-time probes succeed on a fresh host without a manual
    ``ssh-keyscan`` dance while still protecting against later host-key
    swaps (once the key is recorded).
    """
    host = (ssh_host or "").strip()
    if not host:
        return {"status": "error", "message": "SSH host is required"}
    try:
        port = int(ssh_port) if ssh_port is not None else 29418
    except (TypeError, ValueError):
        return {"status": "error", "message": "SSH port must be an integer"}
    if port < 1 or port > 65535:
        return {"status": "error", "message": "SSH port must be between 1 and 65535"}
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-p", str(port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        host,
        "gerrit", "version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        # Gerrit prints `gerrit version 3.9.2` on stdout.
        raw = stdout.decode(errors="replace").strip()
        m = re.search(r"gerrit version\s+(\S+)", raw, re.IGNORECASE)
        version = m.group(1) if m else raw or "unknown"
        result = {
            "status": "ok",
            "version": version,
            "ssh_host": host,
            "ssh_port": port,
        }
        if url:
            result["url"] = url.strip().rstrip("/")
        return result
    err = (stderr or stdout).decode(errors="replace").strip()
    return {"status": "error", "message": err[:300] or "SSH probe failed"}


async def _probe_gitlab_token(token: str, url: str) -> dict:
    """Call GitLab's ``GET /api/v4/version`` with the supplied token and
    return the instance ``version`` + ``revision``. Never reads from
    ``settings``. ``url`` is optional — falls back to ``gitlab.com``.

    B14 Part A row 4 — Bootstrap Step 3.5 GitLab tab. The probe is
    intentionally distinct from ``_test_gitlab`` (which exercises
    ``settings.gitlab_token`` + ``settings.gitlab_url``) so a candidate
    token can be validated before being written."""
    if not token:
        return {"status": "error", "message": "Token is required"}
    base = (url or "").strip().rstrip("/") or "https://gitlab.com"
    if not (base.startswith("http://") or base.startswith("https://")):
        return {
            "status": "error",
            "message": "URL must start with http:// or https://",
        }
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s",
        "-H", f"PRIVATE-TOKEN: {token}",
        "-H", "User-Agent: OmniSight-Bootstrap",
        f"{base}/api/v4/version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    raw = stdout.decode(errors="replace")
    try:
        data = json.loads(raw)
    except Exception:
        return {
            "status": "error",
            "message": "Invalid response from GitLab API",
        }
    if isinstance(data, dict) and "version" in data:
        result = {
            "status": "ok",
            "version": data["version"],
            "url": base,
        }
        if data.get("revision"):
            result["revision"] = data["revision"]
        return result
    message = "GitLab returned an unexpected response"
    if isinstance(data, dict):
        message = data.get("message") or data.get("error") or message
    return {"status": "error", "message": message}


async def _probe_github_token(token: str) -> dict:
    """Call GitHub's ``GET /user`` with the supplied token and return
    the resolved login + display name. Never reads from ``settings``."""
    if not token:
        return {"status": "error", "message": "Token is required"}
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-D", "-",
        "-H", f"Authorization: token {token}",
        "-H", "User-Agent: OmniSight-Bootstrap",
        "https://api.github.com/user",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    raw = stdout.decode(errors="replace")
    # Split headers from body on the blank line (curl -D - prepends them).
    scopes = ""
    body_start = 0
    if "\r\n\r\n" in raw:
        head, _, rest = raw.partition("\r\n\r\n")
        # Follow any 100-continue / 3xx continuations if curl left extra
        # header blocks — take the last one as the response headers.
        while "\r\n\r\n" in rest and rest.lstrip().startswith("HTTP/"):
            head, _, rest = rest.partition("\r\n\r\n")
        for line in head.splitlines():
            if line.lower().startswith("x-oauth-scopes:"):
                scopes = line.split(":", 1)[1].strip()
                break
        body = rest
        body_start = raw.find(body)
    else:
        body = raw
    try:
        data = json.loads(body)
    except Exception:
        return {
            "status": "error",
            "message": "Invalid response from GitHub API",
        }
    if "login" in data:
        return {
            "status": "ok",
            "user": data["login"],
            "name": data.get("name") or data["login"],
            "scopes": scopes,
            "_body_offset": body_start,  # unused; retained for debugging
        }
    return {
        "status": "error",
        "message": data.get("message", "GitHub returned an unexpected response"),
    }


@router.post("/git-forge/test-token")
async def test_git_forge_token(
    body: GitForgeTokenTest, _user=Depends(_au.require_admin)
):
    """Validate a candidate Git forge credential WITHOUT persisting it.

    Used by the Bootstrap Step 3.5 Git Forge setup to let the operator
    sanity-check their credential before they commit it to settings.
    ``github`` / ``gitlab`` run a token probe against the respective
    REST APIs; ``gerrit`` runs an SSH probe (``gerrit version``) since
    Gerrit's first-class transport is SSH, not HTTP.
    """
    provider = (body.provider or "").strip().lower()
    if provider not in {"github", "gitlab", "gerrit"}:
        raise HTTPException(400, f"Unknown provider: {body.provider}")
    try:
        if provider == "gitlab":
            result = await asyncio.wait_for(
                _probe_gitlab_token(body.token, body.url), timeout=15,
            )
        elif provider == "gerrit":
            result = await asyncio.wait_for(
                _probe_gerrit_ssh(body.ssh_host, body.ssh_port, body.url),
                timeout=15,
            )
        else:
            result = await asyncio.wait_for(
                _probe_github_token(body.token), timeout=15,
            )
    except asyncio.TimeoutError:
        return {"status": "error", "message": "Connection timed out (15s)"}
    except Exception as exc:  # pragma: no cover — network-level failure
        return {"status": "error", "message": str(exc)}
    # Strip internal debug key before returning.
    result.pop("_body_offset", None)
    return result


async def _resolve_ssh_public_key() -> dict:
    """Read the OmniSight SSH public key for Gerrit ``Settings → SSH Keys``.

    B14 Part C row 223 — Step 2 of the Gerrit Setup Wizard. The operator
    needs the exact ``ssh-ed25519 AAAA… comment`` line that Gerrit's
    account-level "Add New SSH Key" form accepts. We also surface the
    fingerprint (from ``ssh-keygen -lf``) so the operator can cross-check
    it against what Gerrit shows after pasting.

    Never writes. Never exposes the private key — only the ``.pub``
    sibling. Derives the ``.pub`` path from ``settings.git_ssh_key_path``
    (which points at the private key by default, e.g.
    ``~/.ssh/id_ed25519``); if the setting is already the ``.pub`` file
    it is used as-is. Returning a structured ``{status, public_key,
    fingerprint, key_path, key_type, comment}`` dict keeps the shape
    symmetric with the other probes (``_probe_*``) so the wizard's Step
    2 code path mirrors Step 1.
    """
    raw_path = (settings.git_ssh_key_path or "").strip()
    if not raw_path:
        return {
            "status": "error",
            "message": "git_ssh_key_path is not configured",
        }
    base = Path(raw_path).expanduser()
    pub_path = base if str(base).endswith(".pub") else Path(str(base) + ".pub")
    if not pub_path.exists():
        return {
            "status": "error",
            "message": f"SSH public key not found: {pub_path}",
            "key_path": str(pub_path),
        }
    if not os.access(str(pub_path), os.R_OK):
        return {
            "status": "error",
            "message": f"SSH public key not readable: {pub_path}",
            "key_path": str(pub_path),
        }
    try:
        public_key = pub_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return {
            "status": "error",
            "message": f"Failed to read public key: {exc}",
            "key_path": str(pub_path),
        }
    if not public_key:
        return {
            "status": "error",
            "message": f"SSH public key is empty: {pub_path}",
            "key_path": str(pub_path),
        }
    # `<type> <base64> [comment]` — comment is optional per OpenSSH format.
    parts = public_key.split(None, 2)
    key_type = parts[0] if parts else ""
    comment = parts[2] if len(parts) >= 3 else ""
    # Best-effort fingerprint. Failure here is non-fatal — the public
    # key itself is the load-bearing payload; the fingerprint is
    # operator-facing cross-check only.
    fingerprint = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh-keygen", "-lf", str(pub_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            # `256 SHA256:xxxx user@host (ED25519)`
            fp_parts = stdout.decode(errors="replace").strip().split(None, 3)
            if len(fp_parts) >= 2 and fp_parts[1].startswith("SHA256:"):
                fingerprint = fp_parts[1]
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        fingerprint = ""
    return {
        "status": "ok",
        "public_key": public_key,
        "fingerprint": fingerprint,
        "key_path": str(pub_path),
        "key_type": key_type,
        "comment": comment,
    }


@router.get("/git-forge/ssh-pubkey")
async def get_git_forge_ssh_pubkey(_user=Depends(_au.require_operator)):
    """Return the OmniSight SSH public key for Gerrit ``Settings → SSH Keys``.

    Read-only — never mutates settings, never exposes the private key.
    Drives Step 2 of the Gerrit Setup Wizard (display-pubkey + paste-into-
    Gerrit flow) and is safe to surface to any operator-role session
    since the public half of an SSH keypair is non-secret by design.
    """
    return await _resolve_ssh_public_key()


# ─── B14 Part C row 224: Gerrit merger-agent-bot group probe ──────────────
#
# Step 3 of the Gerrit Setup Wizard walks the operator through creating the
# O7 submit-rule groups — specifically the `merger-agent-bot` group whose
# single member signs the AI half of the dual-+2 gate (see CLAUDE.md Safety
# Rules + docs/ops/gerrit_dual_two_rule.md §1). The probe here is non-
# mutating: it runs `ssh -p {port} {host} gerrit ls-members merger-agent-bot`
# against the operator's Gerrit and returns the member list. An empty or
# missing group is a *configuration* error, not a transport error, so we
# surface it as `status: "error"` with a message the UI can render verbatim.
# No `create-group` / `set-members` calls are made here — those require
# admin privileges and must stay manual per the runbook.

class GerritBotVerify(BaseModel):
    ssh_host: str = ""
    ssh_port: int = 29418
    group: str = "merger-agent-bot"


async def _probe_gerrit_ls_members(
    ssh_host: str, ssh_port: int, group: str
) -> dict:
    """Run ``ssh -p {port} {host} gerrit ls-members {group}`` and parse the
    table Gerrit prints. Shape mirrors ``_probe_gerrit_ssh`` — the caller
    should funnel us through ``asyncio.wait_for(..., timeout=15)``.

    Gerrit's ``ls-members`` output is a header row plus one row per member::

        id    username    full name    email
        1000001    merger-agent-bot    Merger Agent    merger@svc...

    We only need the member count + a short preview of usernames for the
    UI. Failure modes we distinguish:

      - SSH transport failure  → ``status=error`` with raw stderr (first 300 chars)
      - Group not found        → Gerrit exits nonzero with ``fatal: No such group``
      - Group exists, no members → ``status=error`` (configuration gap)
      - Group exists, members   → ``status=ok`` with ``members`` + ``member_count``
    """
    host = (ssh_host or "").strip()
    if not host:
        return {"status": "error", "message": "SSH host is required"}
    grp = (group or "").strip() or "merger-agent-bot"
    try:
        port = int(ssh_port) if ssh_port is not None else 29418
    except (TypeError, ValueError):
        return {"status": "error", "message": "SSH port must be an integer"}
    if port < 1 or port > 65535:
        return {"status": "error", "message": "SSH port must be between 1 and 65535"}
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-p", str(port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        host,
        "gerrit", "ls-members", grp,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = (stderr or stdout).decode(errors="replace").strip()
        return {
            "status": "error",
            "group": grp,
            "message": err[:300] or "gerrit ls-members failed",
        }
    raw = stdout.decode(errors="replace").strip()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    # Drop the header row if present (first column is literally "id" or
    # "_account_id"). Be defensive — some Gerrit builds omit the header
    # when called over SSH with a TTY-less session.
    members: list[dict] = []
    for ln in lines:
        parts = ln.split("\t") if "\t" in ln else ln.split(None, 3)
        first = (parts[0] or "").strip().lower() if parts else ""
        if first in {"id", "_account_id", "account_id"}:
            continue
        if not parts or not parts[0].strip():
            continue
        username = parts[1].strip() if len(parts) >= 2 else ""
        full_name = parts[2].strip() if len(parts) >= 3 else ""
        email = parts[3].strip() if len(parts) >= 4 else ""
        members.append({
            "username": username,
            "full_name": full_name,
            "email": email,
        })
    if not members:
        return {
            "status": "error",
            "group": grp,
            "member_count": 0,
            "members": [],
            "message": (
                f"Group '{grp}' has no members. Add the service account with "
                f"`gerrit set-members {grp} --add <bot-account>`."
            ),
        }
    return {
        "status": "ok",
        "group": grp,
        "member_count": len(members),
        "members": members,
        "ssh_host": host,
        "ssh_port": port,
    }


@router.post("/git-forge/gerrit/verify-bot")
async def verify_gerrit_merger_bot(
    body: GerritBotVerify, _user=Depends(_au.require_admin)
):
    """Verify the ``merger-agent-bot`` Gerrit group exists and has members.

    B14 Part C row 224 — Step 3 of the Gerrit Setup Wizard. Shares the SSH
    transport with Step 1's ``_probe_gerrit_ssh`` but calls ``gerrit
    ls-members`` instead of ``gerrit version`` so the probe only succeeds
    when the O7 dual-+2 group is properly seated. Never mutates Gerrit —
    group creation + member-add stay manual (they require admin rights
    per the runbook in docs/ops/gerrit_dual_two_rule.md §1).
    """
    try:
        result = await asyncio.wait_for(
            _probe_gerrit_ls_members(body.ssh_host, body.ssh_port, body.group),
            timeout=15,
        )
    except asyncio.TimeoutError:
        return {"status": "error", "message": "Connection timed out (15s)"}
    except Exception as exc:  # pragma: no cover — network-level failure
        return {"status": "error", "message": str(exc)}
    return result


# ─── B14 Part C row 225: Gerrit submit-rule (dual-+2) probe ──────────────
#
# Step 4 of the Gerrit Setup Wizard verifies that the target project's
# ``project.config`` on ``refs/meta/config`` carries the O7 dual-+2
# policy (see CLAUDE.md Safety Rules + docs/ops/gerrit_dual_two_rule.md §2
# for the authoritative rule). Gerrit exposes no SSH command that dumps
# arbitrary files, so the probe uses ``git fetch`` + ``git show`` over
# the same SSH transport used by Steps 1/3 — this keeps a single set of
# credentials load-bearing and avoids a second auth surface (HTTP
# password) just for Step 4.
#
# What counts as "dual-+2 rule" for this probe?
#
#   (A) ``label-Code-Review`` is granted to the ``ai-reviewer-bots``
#       group (so AI reviewers — Merger / lint-bot / security-bot —
#       can cast +2 votes at all).
#   (B) ``label-Code-Review`` is granted to the ``non-ai-reviewer``
#       group (so humans can cast the hard-gate +2).
#   (C) ``submit`` is gated to the ``non-ai-reviewer`` group (so no
#       bot can bypass the human hard gate — this is the load-bearing
#       fence CLAUDE.md Safety Rules guards).
#
# Any one of these missing is flagged — the wizard surfaces the
# missing item(s) verbatim so the operator can diff against the
# canonical ``.gerrit/project.config.example`` shipped in the repo.
# The probe never mutates Gerrit; it never writes back to ``settings``.

class GerritSubmitRuleVerify(BaseModel):
    ssh_host: str = ""
    ssh_port: int = 29418
    project: str = ""


# Validates Gerrit project paths: letters, digits, `_`, `-`, `.`, `/`.
# Rejects leading `/`, `..` components, and anything a shell/URL could
# surprise us with. `git fetch` is invoked via `create_subprocess_exec`
# (no shell), so this is belt + braces — catching obvious typos early.
_GERRIT_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-./]{0,199}$")


def _validate_gerrit_project(project: str) -> str | None:
    """Return an error message if the project name is rejected, else None."""
    proj = (project or "").strip()
    if not proj:
        return "Project is required"
    if not _GERRIT_PROJECT_RE.match(proj):
        return (
            "Project must be letters/digits/_/-/./ and start with a word "
            "character"
        )
    if ".." in proj.split("/") or proj.startswith("/") or proj.endswith("/"):
        return "Project path looks malformed"
    return None


# The three ACL fragments we look for in `project.config`. Match the
# Gerrit access-section grammar loosely: we accept any range prefix
# (e.g. `-1..+1`, `-2..+2`) so a tenant who has tightened the label
# range still passes, and we accept either the canonical
# `[access "refs/heads/*"]` scope or an inherited All-Projects scope.
# The group name is the load-bearing identity.
_DUAL_TWO_CHECKS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "ai_reviewers_can_vote",
        re.compile(
            r"label-Code-Review\s*=\s*-?\d+\.\.\+?\d+\s+group\s+ai-reviewer-bots\b",
            re.IGNORECASE,
        ),
        "AI reviewer bots are missing `label-Code-Review` grant "
        "(group `ai-reviewer-bots`).",
    ),
    (
        "humans_can_vote",
        re.compile(
            r"label-Code-Review\s*=\s*-?\d+\.\.\+?\d+\s+group\s+non-ai-reviewer\b",
            re.IGNORECASE,
        ),
        "Human reviewers are missing `label-Code-Review` grant "
        "(group `non-ai-reviewer`).",
    ),
    (
        "submit_gated_to_humans",
        re.compile(
            r"^\s*submit\s*=\s*group\s+non-ai-reviewer\b", re.IGNORECASE | re.MULTILINE
        ),
        "`submit` is not gated to `non-ai-reviewer` — any group with "
        "submit permission would bypass the human hard gate.",
    ),
]


async def _fetch_gerrit_project_config(
    ssh_host: str, ssh_port: int, project: str
) -> tuple[int, str, str]:
    """Run ``git fetch`` + ``git show`` over the Gerrit SSH transport to
    read ``project.config`` off ``refs/meta/config``. Returns the tuple
    ``(returncode, stdout, stderr)`` so the caller can route Gerrit
    error output (``fatal: …``) verbatim into the probe result.

    The temp-repo lives under ``tempfile.TemporaryDirectory`` and is
    torn down on exit regardless of outcome. ``GIT_TERMINAL_PROMPT=0``
    keeps git from blocking on a broken auth path (vs. blocking on an
    invisible prompt); ``BatchMode=yes`` + ``ConnectTimeout=5`` on
    ``GIT_SSH_COMMAND`` mirror ``_probe_gerrit_ssh`` so the transport
    behaviour matches Step 1.
    """
    import tempfile

    ref = "refs/meta/config"
    url = f"ssh://{ssh_host}:{int(ssh_port)}/{project}"
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": (
            "ssh -o StrictHostKeyChecking=accept-new "
            "-o ConnectTimeout=5 -o BatchMode=yes"
        ),
    }
    with tempfile.TemporaryDirectory(prefix="gerrit-submit-rule-") as tmp:
        # `git init` + fetch instead of `git clone` because cloning
        # refs/meta/config directly (not an advertised branch) requires
        # a follow-up `git fetch` anyway — collapsing the two calls.
        init = await asyncio.create_subprocess_exec(
            "git", "init", "-q", tmp,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        await init.communicate()
        if init.returncode != 0:  # pragma: no cover — git missing from image
            return (init.returncode or 1, "", "git init failed")

        fetch = await asyncio.create_subprocess_exec(
            "git", "-C", tmp, "fetch", "--depth=1", url, ref,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, fetch_err = await fetch.communicate()
        if fetch.returncode != 0:
            return (
                fetch.returncode or 1,
                "",
                fetch_err.decode(errors="replace").strip(),
            )

        show = await asyncio.create_subprocess_exec(
            "git", "-C", tmp, "show", "FETCH_HEAD:project.config",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        show_out, show_err = await show.communicate()
        return (
            show.returncode or 0,
            show_out.decode(errors="replace"),
            show_err.decode(errors="replace").strip(),
        )


async def _probe_gerrit_submit_rule(
    ssh_host: str, ssh_port: int, project: str
) -> dict:
    """Fetch ``refs/meta/config:project.config`` from ``project`` and
    verify it declares the dual-+2 ACL triple
    (``ai-reviewer-bots`` can vote, ``non-ai-reviewer`` can vote,
    ``submit`` gated to ``non-ai-reviewer``).

    Shape mirrors the other ``_probe_gerrit_*`` helpers::

        { status: "ok"|"error", project, checks: [...], missing: [...],
          ssh_host, ssh_port }

    ``checks`` is the authoritative per-check breakdown the UI renders
    inline; ``missing`` is a convenience list of the failing check IDs
    so the wizard can show a single red bullet list.
    """
    host = (ssh_host or "").strip()
    if not host:
        return {"status": "error", "message": "SSH host is required"}
    proj_err = _validate_gerrit_project(project)
    if proj_err:
        return {"status": "error", "message": proj_err}
    try:
        port = int(ssh_port) if ssh_port is not None else 29418
    except (TypeError, ValueError):
        return {"status": "error", "message": "SSH port must be an integer"}
    if port < 1 or port > 65535:
        return {
            "status": "error",
            "message": "SSH port must be between 1 and 65535",
        }

    proj = project.strip()
    rc, stdout, stderr = await _fetch_gerrit_project_config(host, port, proj)
    if rc != 0:
        err = stderr or stdout or "git fetch failed"
        return {
            "status": "error",
            "project": proj,
            "ssh_host": host,
            "ssh_port": port,
            "message": err[:300],
        }
    config = stdout or ""
    # Strip comment-only lines so a commented-out rule in a sample file
    # can't trick the probe into a false-positive match.
    scrubbed = "\n".join(
        line for line in config.splitlines() if not line.lstrip().startswith(("#", ";"))
    )
    checks: list[dict] = []
    missing: list[str] = []
    for check_id, pattern, detail in _DUAL_TWO_CHECKS:
        ok = bool(pattern.search(scrubbed))
        checks.append({"id": check_id, "ok": ok, "detail": "" if ok else detail})
        if not ok:
            missing.append(check_id)

    if missing:
        friendly = "; ".join(c["detail"] for c in checks if not c["ok"])
        return {
            "status": "error",
            "project": proj,
            "ssh_host": host,
            "ssh_port": port,
            "checks": checks,
            "missing": missing,
            "message": (
                f"project.config is missing {len(missing)} dual-+2 rule"
                f"{'s' if len(missing) != 1 else ''}: {friendly}"
            ),
        }
    return {
        "status": "ok",
        "project": proj,
        "ssh_host": host,
        "ssh_port": port,
        "checks": checks,
        "missing": [],
    }


@router.post("/git-forge/gerrit/verify-submit-rule")
async def verify_gerrit_submit_rule(
    body: GerritSubmitRuleVerify, _user=Depends(_au.require_admin)
):
    """Verify the target Gerrit project carries the O7 dual-+2 submit rule.

    B14 Part C row 225 — Step 4 of the Gerrit Setup Wizard. Non-mutating:
    reads ``refs/meta/config:project.config`` over the Gerrit SSH
    transport and pattern-matches the three ACL lines that encode the
    dual-+2 gate. Installation of the rule stays manual (it requires
    ``Push`` on ``refs/meta/config`` which is an admin-only ref) per
    docs/ops/gerrit_dual_two_rule.md §2.
    """
    try:
        result = await asyncio.wait_for(
            _probe_gerrit_submit_rule(body.ssh_host, body.ssh_port, body.project),
            timeout=30,
        )
    except asyncio.TimeoutError:
        return {"status": "error", "message": "Connection timed out (30s)"}
    except Exception as exc:  # pragma: no cover — network-level failure
        return {"status": "error", "message": str(exc)}
    return result


# ─── B14 Part C row 226: Gerrit webhook setup (Step 5) ──────────────────
#
# Step 5 of the Gerrit Setup Wizard wires the inbound webhook surface so
# Gerrit can deliver `patchset-created` / `comment-added` / `change-merged`
# events back to OmniSight. Two pieces have to land in Gerrit's
# `webhooks.config` (under `refs/meta/config` — same admin path as Step 4):
#
#   url    = <OmniSight base URL>/api/v1/webhooks/gerrit
#   secret = <HMAC-SHA256 shared secret> (settings.gerrit_webhook_secret)
#
# The probe never mutates Gerrit. Instead it surfaces the URL the operator
# must paste plus the *current* secret status (configured / not). For the
# common case where the operator hasn't picked a secret yet (fresh
# install), the wizard offers a one-click "Generate Secret" that writes a
# 32-byte URL-safe token into `settings.gerrit_webhook_secret` and returns
# the *plain* value exactly once — the operator pastes it into Gerrit, then
# the value is masked on subsequent reads (no re-reveal endpoint by design;
# rotate to invalidate-and-re-issue is the supported recovery path).
#
# The webhook URL is derived from the inbound `Request` so it follows the
# same scheme/host the operator is talking to (cloudflared tunnel, direct
# LAN IP, localhost, …). `X-Forwarded-Proto` / `X-Forwarded-Host` are
# honoured first because cloudflared / nginx terminates HTTPS upstream and
# would otherwise leave the URL stuck on `http://internal-host:8000`.

_WEBHOOK_PATH = "/api/v1/webhooks/gerrit"
_JIRA_WEBHOOK_PATH = "/api/v1/webhooks/jira"


def _mask_secret(value: str) -> str:
    """Mask a webhook secret for display. ``_mask`` lives at module scope
    but is tuned for tokens (3 + tail). Webhook secrets are URL-safe
    base64; show the first 4 + last 4 so the operator can cross-check
    against what they pasted into Gerrit without leaking the rotation
    surface."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def _derive_webhook_url(request: Request, path: str = _WEBHOOK_PATH) -> str:
    """Build the externally-facing URL an external system will POST to.

    Honours ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` first because
    cloudflared (default deploy) terminates HTTPS upstream — without
    these headers we'd hand the operator ``http://127.0.0.1:8000/...``,
    which the external system cannot reach. Falls back to
    ``Request.base_url`` which Starlette derives from the actual
    HTTP/1.1 ``Host`` header.

    ``path`` defaults to the Gerrit webhook path (original caller);
    pass ``_JIRA_WEBHOOK_PATH`` for the JIRA rotate endpoint.
    """
    fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    fwd_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if fwd_proto and fwd_host:
        return f"{fwd_proto}://{fwd_host}{path}"
    base = str(request.base_url or "").rstrip("/")
    if base:
        return f"{base}{path}"
    return path


@router.get("/git-forge/gerrit/webhook-info")
async def get_gerrit_webhook_info(
    request: Request, _user=Depends(_au.require_admin)
):
    """Return the inbound webhook URL + secret status the operator must
    paste into Gerrit's ``webhooks.config`` (Step 5 of the Setup Wizard).

    Never returns the plain secret — only ``secret_configured`` plus a
    ``secret_masked`` preview so the operator can confirm what's wired
    without re-revealing it. Use ``POST .../webhook-secret/generate`` to
    rotate (which returns the new plain value exactly once).
    """
    secret = settings.gerrit_webhook_secret or ""
    return {
        "status": "ok",
        "webhook_url": _derive_webhook_url(request),
        "secret_configured": bool(secret),
        "secret_masked": _mask_secret(secret),
        "signature_header": "X-Gerrit-Signature",
        "signature_algorithm": "hmac-sha256",
        "event_types": ["patchset-created", "comment-added", "change-merged"],
    }


class GerritFinalizeBody(BaseModel):
    """Wizard finalize payload — Steps 1-5 collected on the client side, this
    is the single atomic write into ``settings.gerrit_*`` that flips the
    integration ON. Fields mirror the wizard inputs:

    * ``url`` — REST URL the operator entered in Step 1 (optional; some
      shops only run SSH-only Gerrit and never enable the REST plugin).
    * ``ssh_host`` / ``ssh_port`` — Step 1 SSH endpoint that already
      passed the ``gerrit version`` probe.
    * ``project`` — the project path validated by Step 4's submit-rule
      probe (e.g. ``project/omnisight-core``).
    * ``replication_targets`` — optional CSV of remote names; empty is
      fine for most installs (single-Gerrit deploys).
    """
    url: str = ""
    ssh_host: str
    ssh_port: int = 29418
    project: str = ""
    replication_targets: str = ""


@router.post("/git-forge/gerrit/finalize")
async def finalize_gerrit_integration(
    body: GerritFinalizeBody, _user=Depends(_au.require_admin)
):
    """Persist the wizard's collected Gerrit settings and flip
    ``gerrit_enabled`` on — the closing act of the Setup Wizard.

    Steps 1-5 only mutate ``gerrit_webhook_secret`` (Step 5 generate).
    Without this finalize step the operator would have to re-enter every
    Step 1 value into the Settings form by hand to actually turn the
    integration on, defeating the wizard. This endpoint is the single
    atomic write that promotes wizard inputs into ``settings.gerrit_*``
    and reports success so the UI can render the「Gerrit 整合已啟用」
    confirmation banner.

    Validation is intentionally narrow:
      * ``ssh_host`` must be non-empty (Step 1 cannot have passed
        otherwise; we double-check here so a hand-rolled curl can't
        write garbage).
      * ``ssh_port`` must be in [1, 65535].
      * ``url`` and ``project`` are normalised (trim) but not
        round-tripped to Gerrit again — Step 1 + Step 4 already proved
        they work.

    The webhook secret is *not* echoed back even masked — the Step 5
    generate response was the one-and-only reveal.
    """
    ssh_host = (body.ssh_host or "").strip()
    if not ssh_host:
        raise HTTPException(400, "ssh_host is required (Step 1 must pass first)")
    if not (1 <= body.ssh_port <= 65535):
        raise HTTPException(400, f"ssh_port {body.ssh_port} out of range 1..65535")

    # 2026-04-22: route every finalize write through
    # ``_apply_runtime_setting`` so all six fields mirror into
    # Redis-backed SharedKV + peer workers converge on the wizard's
    # state. Previously this used raw ``setattr(settings, ...)``
    # which only mutated THIS worker. After the same-day typed-KV
    # follow-up landed ``gerrit_enabled`` (bool) and
    # ``gerrit_ssh_port`` (int) are also mirrored — coerced back
    # to their native types on overlay via ``_coerce_kv_value`` —
    # so wizard finalize alone is now sufficient to enable Gerrit
    # across all workers without an ``.env`` rewrite + restart.
    _apply_runtime_setting("gerrit_enabled", True)
    _apply_runtime_setting("gerrit_url", (body.url or "").strip())
    _apply_runtime_setting("gerrit_ssh_host", ssh_host)
    _apply_runtime_setting("gerrit_ssh_port", body.ssh_port)
    _apply_runtime_setting("gerrit_project", (body.project or "").strip())
    _apply_runtime_setting(
        "gerrit_replication_targets",
        (body.replication_targets or "").strip(),
    )

    logger.info(
        "Gerrit integration finalized by user=%s host=%s port=%d project=%s url=%s",
        getattr(_user, "username", "?"),
        ssh_host, body.ssh_port,
        settings.gerrit_project or "(unset)",
        settings.gerrit_url or "(unset)",
    )
    return {
        "status": "ok",
        "enabled": True,
        "message": "Gerrit 整合已啟用",
        "config": {
            "url": settings.gerrit_url,
            "ssh_host": settings.gerrit_ssh_host,
            "ssh_port": settings.gerrit_ssh_port,
            "project": settings.gerrit_project,
            "replication_targets": settings.gerrit_replication_targets,
            "webhook_secret_configured": bool(settings.gerrit_webhook_secret),
        },
        "note": (
            "All six fields (incl. gerrit_enabled + ssh_port) persist via "
            "Redis across workers. Values survive backend restarts too "
            "since Redis is the source of truth via overlay; the ``.env`` "
            "values only matter on a cold Redis. Phase 5b will move "
            "credentials into a DB-persistent per-tenant table."
        ),
    }


@router.post("/git-forge/gerrit/webhook-secret/generate")
async def generate_gerrit_webhook_secret(
    request: Request, _user=Depends(_au.require_admin)
):
    """Mint + persist a fresh ``gerrit_webhook_secret`` and return it once.

    32 bytes of ``secrets.token_urlsafe`` → ~43-char URL-safe string with
    ~256 bits of entropy, well above the 128-bit floor recommended for
    HMAC-SHA256 keys. The plain value is returned **only** in this
    response — the operator must capture it before closing the wizard;
    subsequent ``webhook-info`` calls will surface only the masked
    preview. Rotating here invalidates whatever secret Gerrit currently
    holds, so the operator must re-paste the new value into Gerrit's
    ``webhooks.config`` for events to keep verifying.
    """
    new_secret = _secrets.token_urlsafe(32)
    # Route through the SharedKV mirror so peer workers pick up the
    # rotated secret via Redis — otherwise the Gerrit webhook HMAC
    # verifier running on another worker would keep comparing against
    # the old secret and reject legitimate events.
    _apply_runtime_setting("gerrit_webhook_secret", new_secret)
    logger.info(
        "gerrit_webhook_secret rotated by user=%s len=%d",
        getattr(_user, "username", "?"),
        len(new_secret),
    )
    return {
        "status": "ok",
        "secret": new_secret,
        "secret_masked": _mask_secret(new_secret),
        "webhook_url": _derive_webhook_url(request),
        "signature_header": "X-Gerrit-Signature",
        "signature_algorithm": "hmac-sha256",
        "note": (
            "Save this value now — it will not be shown again. Paste it "
            "into Gerrit `refs/meta/config:webhooks.config` under the "
            "matching `[remote ...]` block as `secret = <value>`."
        ),
    }


@router.post("/git-forge/jira/webhook-secret/generate")
async def generate_jira_webhook_secret(
    request: Request, _user=Depends(_au.require_admin)
):
    """Mint + persist a fresh ``jira_webhook_secret`` and return it once.

    Mirrors the Gerrit rotate endpoint (Y-prep.2 #288). 32 bytes of
    ``secrets.token_urlsafe`` → ~43-char URL-safe string with ~256 bits
    of entropy. The plain value is returned **only** in this response —
    the operator must capture it and paste it into JIRA's webhook
    ``Authorization`` header configuration before closing the modal;
    subsequent reads (``GET /settings``) surface only the
    ``configured``/``""`` status via the webhooks block.

    Cross-worker coherence: ``_apply_runtime_setting`` writes through
    the Redis-backed SharedKV mirror (``jira_webhook_secret`` is in
    ``_SHARED_KV_STR_FIELDS``), so peer workers pick up the rotated
    secret on their next overlay — the inbound
    ``POST /api/v1/webhooks/jira`` verifier on any worker compares
    against the new value, not the one in this worker's .env-loaded
    ``settings`` from boot.

    JIRA webhooks use a shared token in the ``Authorization: Bearer
    <token>`` header (not HMAC body signing like Gerrit), so the
    ``signature_header`` / ``signature_algorithm`` fields in the
    response reflect that — structural parity with the Gerrit response
    shape, accurate semantics for the JIRA transport.
    """
    new_secret = _secrets.token_urlsafe(32)
    _apply_runtime_setting("jira_webhook_secret", new_secret)
    logger.info(
        "jira_webhook_secret rotated by user=%s len=%d",
        getattr(_user, "username", "?"),
        len(new_secret),
    )
    return {
        "status": "ok",
        "secret": new_secret,
        "secret_masked": _mask_secret(new_secret),
        "webhook_url": _derive_webhook_url(request, _JIRA_WEBHOOK_PATH),
        "signature_header": "Authorization",
        "signature_algorithm": "bearer-token",
        "note": (
            "Save this value now — it will not be shown again. In JIRA's "
            "webhook settings, set the request ``Authorization`` header to "
            "``Bearer <value>`` so the inbound verifier accepts events."
        ),
    }


# ── Test functions ──

async def _test_ssh() -> dict:
    key_path = Path(settings.git_ssh_key_path).expanduser()
    if not key_path.exists():
        return {"status": "error", "message": f"SSH key not found: {key_path}"}
    if not os.access(str(key_path), os.R_OK):
        return {"status": "error", "message": f"SSH key not readable: {key_path}"}
    return {"status": "ok", "path": str(key_path)}


async def _test_gerrit() -> dict:
    """Probe Gerrit SSH connectivity using the resolved default account.

    Phase 5-7 (#multi-account-forge): the host / port come from the
    ``pick_default("gerrit")`` row so the probe button tests what the
    resolver would actually use, not stale ``settings.gerrit_*``
    scalars. Falls back to the legacy shim's ``default-gerrit``
    virtual row when ``git_accounts`` is empty so single-instance
    deployments need no operator action.
    """
    if not settings.gerrit_enabled:
        return {"status": "not_configured", "message": "Gerrit is disabled"}
    from backend.git_credentials import pick_default
    account = await pick_default("gerrit", touch=False)
    ssh_host = (account or {}).get("ssh_host") or ""
    ssh_port = int((account or {}).get("ssh_port") or 0) or 29418
    if not ssh_host:
        return {"status": "not_configured", "message": "Gerrit SSH host not set"}
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-p", str(ssh_port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        f"{ssh_host}",
        "gerrit", "version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        return {"status": "ok", "version": stdout.decode().strip()}
    return {"status": "error", "message": (stderr or stdout).decode().strip()[:200]}


async def _test_github() -> dict:
    # B14 Part E row 240: surface the OAuth scopes alongside the login so
    # the operator can confirm the token has the privileges OmniSight needs
    # (`repo`, `workflow`, …) without round-tripping to GitHub's UI. Scopes
    # ride on the `X-OAuth-Scopes` response header, so we include `-D -` to
    # capture headers and split on the blank line. Header parsing mirrors
    # `_probe_github_token` so the two paths stay consistent.
    #
    # Phase 5-6 (#multi-account-forge): the token is resolved via
    # ``pick_default("github")`` so operator-configured ``git_accounts``
    # rows are honoured; resolver's legacy shim falls back to
    # ``settings.github_token`` when the table is empty.
    from backend.git_credentials import pick_default
    account = await pick_default("github")
    token = (account or {}).get("token") or ""
    if not token:
        return {"status": "not_configured", "message": "GitHub token not set"}
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-D", "-",
        "-H", f"Authorization: token {token}",
        "https://api.github.com/user",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    raw = stdout.decode(errors="replace")
    scopes = ""
    body = raw
    if "\r\n\r\n" in raw:
        head, _, rest = raw.partition("\r\n\r\n")
        while "\r\n\r\n" in rest and rest.lstrip().startswith("HTTP/"):
            head, _, rest = rest.partition("\r\n\r\n")
        for line in head.splitlines():
            if line.lower().startswith("x-oauth-scopes:"):
                scopes = line.split(":", 1)[1].strip()
                break
        body = rest
    try:
        data = json.loads(body)
        if "login" in data:
            return {"status": "ok", "user": data["login"], "scopes": scopes}
        return {"status": "error", "message": data.get("message", "Unknown error")}
    except Exception:
        return {"status": "error", "message": "Invalid response from GitHub API"}


async def _test_gitlab() -> dict:
    # B14 Part E row 240 spec: `GET /api/v4/version` → display version. The
    # version endpoint is the canonical "is this GitLab reachable + is my
    # token valid" probe (it requires authentication on self-managed
    # instances), and it returns the instance version which is more useful
    # for diagnostics than the bare username.
    #
    # Phase 5-6 (#multi-account-forge): the token and self-hosted base
    # URL are resolved via ``pick_default("gitlab")``. The registry row's
    # ``instance_url`` takes precedence over the legacy
    # ``settings.gitlab_url`` scalar so multi-instance deployments pick
    # the right base automatically; the legacy shim supplies both when
    # ``git_accounts`` is empty.
    from backend.git_credentials import pick_default
    account = await pick_default("gitlab")
    token = (account or {}).get("token") or ""
    if not token:
        return {"status": "not_configured", "message": "GitLab token not set"}
    base = (
        ((account or {}).get("instance_url") or settings.gitlab_url or "https://gitlab.com")
        .rstrip("/")
    )
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-H", f"PRIVATE-TOKEN: {token}",
        f"{base}/api/v4/version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and "version" in data:
            result = {"status": "ok", "version": data["version"], "url": base}
            if data.get("revision"):
                result["revision"] = data["revision"]
            return result
        message = "Unknown error"
        if isinstance(data, dict):
            message = data.get("message") or data.get("error") or message
        return {"status": "error", "message": message}
    except Exception:
        return {"status": "error", "message": "Invalid response from GitLab API"}


async def _test_jira() -> dict:
    # B14 Part E row 240 spec: `GET /rest/api/2/serverInfo` → display
    # version. serverInfo carries `version` + `serverTitle` + `buildNumber`
    # which is exactly what an operator wants to see when diagnosing a
    # Jira link, and it works on Cloud and Server alike.
    if not settings.notification_jira_url or not settings.notification_jira_token:
        return {"status": "not_configured", "message": "Jira URL or token not set"}
    base = settings.notification_jira_url.rstrip("/")
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s",
        "-H", f"Authorization: Bearer {settings.notification_jira_token}",
        f"{base}/rest/api/2/serverInfo",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and "version" in data:
            result = {"status": "ok", "version": data["version"]}
            if data.get("serverTitle"):
                result["server_title"] = data["serverTitle"]
            return result
        message = "Unknown error"
        if isinstance(data, dict):
            message = data.get("message") or str(data)[:100]
        return {"status": "error", "message": message}
    except Exception:
        return {"status": "error", "message": "Invalid response from Jira"}


async def _test_slack() -> dict:
    if not settings.notification_slack_webhook:
        return {"status": "not_configured", "message": "Slack webhook not set"}
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", settings.notification_slack_webhook,
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"text": "[TEST] OmniSight integration test — connection OK"}),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    response = stdout.decode().strip()
    if response == "ok":
        return {"status": "ok", "message": "Test message sent to Slack (a real message was posted to the channel)"}
    return {"status": "error", "message": f"Slack returned: {response[:100]}"}


_TESTERS = {
    "ssh": _test_ssh,
    "gerrit": _test_gerrit,
    "github": _test_github,
    "gitlab": _test_gitlab,
    "jira": _test_jira,
    "slack": _test_slack,
}


# ── Vendor SDK CRUD ──

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PLATFORMS_DIR = _PROJECT_ROOT / "configs" / "platforms"


class VendorSDKCreate(BaseModel):
    platform: str  # Profile name (filename without .yaml)
    label: str
    vendor_id: str
    soc_model: str = ""
    sdk_version: str = ""
    toolchain: str = "aarch64-linux-gnu-gcc"
    cross_prefix: str = "aarch64-linux-gnu-"
    kernel_arch: str = "arm64"
    arch_flags: str = "-march=armv8-a"
    qemu: str = "qemu-aarch64-static"
    sysroot_path: str = ""
    cmake_toolchain_file: str = ""
    # SDK source for auto-provisioning (Phase 45)
    sdk_git_url: str = ""          # Git URL to clone SDK from
    sdk_git_branch: str = "main"   # Branch to clone
    sdk_install_script: str = ""   # Post-clone setup script
    npu_enabled: bool = False
    deploy_method: str = "ssh"
    deploy_target_ip: str = ""


@router.post("/vendor/sdks")
async def create_vendor_sdk(body: VendorSDKCreate, _user=Depends(_au.require_admin)):
    """Create a new vendor SDK platform profile."""
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', body.platform):
        raise HTTPException(400, "Platform name must be alphanumeric/dash/underscore")
    profile_path = _PLATFORMS_DIR / f"{body.platform}.yaml"
    if profile_path.exists():
        raise HTTPException(409, f"Platform profile already exists: {body.platform}")

    import yaml
    data = {
        "platform": body.platform,
        "label": body.label,
        "vendor_id": body.vendor_id,
        "soc_model": body.soc_model,
        "sdk_version": body.sdk_version,
        "toolchain": body.toolchain,
        "cross_prefix": body.cross_prefix,
        "kernel_arch": body.kernel_arch,
        "arch_flags": body.arch_flags,
        "qemu": body.qemu,
        "sysroot_path": body.sysroot_path,
        "cmake_toolchain_file": body.cmake_toolchain_file,
        "sdk_git_url": body.sdk_git_url,
        "sdk_git_branch": body.sdk_git_branch,
        "sdk_install_script": body.sdk_install_script,
        "npu_enabled": body.npu_enabled,
        "deploy_method": body.deploy_method,
        "deploy_target_ip": body.deploy_target_ip,
        "docker_packages": [
            f"gcc-{body.cross_prefix.rstrip('-')}",
            f"g++-{body.cross_prefix.rstrip('-')}",
            f"binutils-{body.cross_prefix.rstrip('-')}",
        ],
    }
    profile_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
    logger.info("Created vendor SDK profile: %s", body.platform)
    return {"status": "created", "platform": body.platform, "path": str(profile_path)}


@router.delete("/vendor/sdks/{platform}")
async def delete_vendor_sdk(platform: str, _user=Depends(_au.require_admin)):
    """Delete a vendor SDK platform profile."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', platform):
        raise HTTPException(400, "Invalid platform name (alphanumeric, hyphens, underscores only)")
    profile_path = _PLATFORMS_DIR / f"{platform}.yaml"
    if not profile_path.exists():
        raise HTTPException(404, f"Platform profile not found: {platform}")
    # Prevent deleting built-in profiles
    builtin = {"aarch64", "armv7", "riscv64"}
    if platform in builtin:
        raise HTTPException(403, f"Cannot delete built-in platform: {platform}")
    profile_path.unlink()
    logger.info("Deleted vendor SDK profile: %s", platform)
    return {"status": "deleted", "platform": platform}


@router.post("/vendor/sdks/{platform}/install")
async def install_vendor_sdk(platform: str, _user=Depends(_au.require_admin)):
    """Clone and provision the vendor SDK for a platform.

    Reads sdk_git_url from the platform YAML, clones the repo,
    scans for toolchain/sysroot, and updates the platform profile.
    """
    if not re.match(r'^[a-zA-Z0-9_-]+$', platform):
        raise HTTPException(400, "Invalid platform name")
    from backend.sdk_provisioner import provision_sdk
    result = await provision_sdk(platform)
    if result["status"] == "error":
        raise HTTPException(400, result["details"])
    return result


@router.get("/vendor/sdks/{platform}/validate")
async def validate_vendor_sdk(platform: str):
    """Validate that SDK paths in a platform profile exist on disk."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', platform):
        raise HTTPException(400, "Invalid platform name")
    from backend.sdk_provisioner import validate_sdk_paths
    return validate_sdk_paths(platform)
