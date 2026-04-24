"""Phase 5b-5 (#llm-credentials) — legacy ``.env`` ``{provider}_api_key`` →
``llm_credentials`` auto-migration.

Lifespan startup hook that detects legacy LLM provider API keys living
in ``Settings`` (``anthropic_api_key`` / ``google_api_key`` /
``openai_api_key`` / ``xai_api_key`` / ``groq_api_key`` /
``deepseek_api_key`` / ``together_api_key`` / ``openrouter_api_key``
+ ``ollama_base_url``) and one-shot migrates each one into an
``llm_credentials`` row so the post-Phase-5b resolver
(:func:`backend.llm_credential_resolver.get_llm_credential`) reads the
canonical table instead of falling through to the deprecated Settings
shim. Runs at most once per database (idempotent guard); operator can
disable entirely via ``OMNISIGHT_LLM_CREDENTIAL_MIGRATE=skip``.

Mirrors :mod:`backend.legacy_credential_migration` (Phase 5-5,
multi-account forge) — same deterministic-id + ``ON CONFLICT DO
NOTHING`` pattern, same lifespan best-effort semantics — but scoped
to LLM providers instead of git forges. Reuses :mod:`backend.secret_store`
for Fernet encryption (same key, same first-boot flock) so ciphertext
produced here is byte-compatible with what row 5b-3 CRUD writes.

Idempotency contract
─────────────────────
A non-empty ``llm_credentials`` table is the cue that migration already
happened (or that the operator already created rows via Phase-5b-3
CRUD). Re-running the hook in that state is a no-op — we never
resurrect rows the operator deliberately deleted, and we never
overwrite credentials that may have been rotated since the legacy
``.env`` snapshot. Operators who want to force a re-migration can
either ``DELETE FROM llm_credentials`` first or create replacements
via the Phase-5b-4 UI.

Multi-worker safety (SOP Step 1, qualified answer #2 — PG coordination)
───────────────────────────────────────────────────────────────────────
Production runs ``uvicorn --workers N`` so the lifespan hook fires on
every worker at boot. Without coordination, all N workers would race
on a single empty-table observation, each conclude "empty → migrate",
and emit duplicate inserts.

Two layers of defence (identical to Phase 5-5):

1. **Deterministic primary keys.** Each migrated row's id is a stable
   function of its source (``lc-legacy-{provider}``), not a
   ``uuid.uuid4()``. Worker A and worker B compute the SAME id for
   the SAME source — so even if they both observe the empty table,
   the second INSERT collides on the PK rather than producing a
   duplicate.

2. **``INSERT ... ON CONFLICT (id) DO NOTHING``.** PG resolves the
   collision at row-write time without raising; the loser silently
   skips. We use ``RETURNING id`` to detect winners; only the winner
   emits the audit row + the per-row "[LLM-CRED-MIGRATE]" log line, so
   N workers don't N× the audit chain or the warning volume.

No new module-globals introduced.

Read-after-write audit (SOP Step 1, 2026-04-21 rule)
────────────────────────────────────────────────────
The hook executes inside the lifespan ``startup`` block before uvicorn
opens the listening socket — no request handler can observe the
partial-write state. The resolver's sync path reads ``settings.*_api_key``
directly, so writes to ``llm_credentials`` are invisible to it until
the next process reload; this is intentional (see the row-5b-2
docstring on why sync must not await DB reads). Future row 5b-6
deprecation will remove the scalar writes + rely on ``load_into_settings``
to mirror DB state back into ``Settings`` on lifespan init — that's
the moment sync reads pick up DB writes, not this row.

Not migrated (and why)
───────────────────────
* ``llm_model`` / ``llm_provider`` / ``llm_fallback_chain`` — routing
  knobs, not credentials. Stay as Settings scalars.
* ``ollama_model`` — a model-name knob on the keyless ollama provider.
  Same reason as above.
* ``token_fallback_provider`` / ``token_fallback_model`` — budget
  downgrade routing, not credentials.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)


_AUDIT_ACTION = "llm_credential_auto_migrate"
_DEFAULT_TENANT = "t-default"

_KILL_SWITCH_ENV = "OMNISIGHT_LLM_CREDENTIAL_MIGRATE"
_KILL_SWITCH_SKIP = "skip"

# Canonical legacy Settings attribute per provider. Matches
# :data:`backend.llm_credential_resolver._PROVIDER_KEY_ATTR`.
# Ollama is keyless — present in the dict so a row can still be
# created for the ``base_url`` (threaded through ``metadata``);
# ``api_key_attr=None`` disables the key-write branch.
_PROVIDER_LEGACY_FIELDS: dict[str, dict[str, Any]] = {
    "anthropic":  {"api_key_attr": "anthropic_api_key"},
    "google":     {"api_key_attr": "google_api_key"},
    "openai":     {"api_key_attr": "openai_api_key"},
    "xai":        {"api_key_attr": "xai_api_key"},
    "groq":       {"api_key_attr": "groq_api_key"},
    "deepseek":   {"api_key_attr": "deepseek_api_key"},
    "together":   {"api_key_attr": "together_api_key"},
    "openrouter": {"api_key_attr": "openrouter_api_key"},
    # Ollama is keyless; migrate a row only when base_url differs from
    # the module default (otherwise the row would carry no signal).
    "ollama":     {"api_key_attr": None},
}

# Ollama default base_url from backend.config — migrating a row that
# only carries this default would be noise (the resolver's keyless
# branch already synthesises the same value from Settings). We
# deliberately compare string-equal rather than using a sentinel so
# an operator who has explicitly set OMNISIGHT_OLLAMA_BASE_URL to
# the same value as the default still does not get a spurious row.
_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Plan-then-write
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _plan_rows() -> list[dict[str, Any]]:
    """Return the list of ``llm_credentials`` rows to insert from the
    current ``Settings`` snapshot.

    Each dict has the in-memory shape ``create_credential`` would
    consume, plus a deterministic ``id`` and a one-line ``source`` tag
    for the audit / log line. Empty input → empty list (caller no-ops).

    Rules
    ─────
    * One row per keyed provider whose ``{provider}_api_key`` is
      non-empty after strip. Always ``is_default=TRUE`` — the legacy
      ``.env`` shape allows one key per provider so there is no
      ambiguity about which becomes the default.
    * One row for ollama iff ``ollama_base_url`` is non-empty AND
      differs from the module default ``http://localhost:11434``.
      Without a custom base_url there is no configured fact worth
      persisting (the resolver already supplies the default).
    """
    rows: list[dict[str, Any]] = []

    for provider, info in _PROVIDER_LEGACY_FIELDS.items():
        attr = info.get("api_key_attr")
        if attr is None:
            # Keyless branch — ollama only today. Preserve custom
            # base_url iff it deviates from the default.
            if provider != "ollama":
                continue
            base_url = (getattr(settings, "ollama_base_url", "") or "").strip()
            if not base_url or base_url == _OLLAMA_DEFAULT_BASE_URL:
                continue
            rows.append({
                "id": f"lc-legacy-{provider}",
                "provider": provider,
                "label": "Legacy .env migration",
                "value": "",
                "metadata": {"base_url": base_url},
                "is_default": True,
                "enabled": True,
                "source": f"settings.{provider}_base_url",
            })
            continue

        raw = getattr(settings, attr, "")
        value = (raw or "").strip()
        if not value:
            continue
        rows.append({
            "id": f"lc-legacy-{provider}",
            "provider": provider,
            "label": "Legacy .env migration",
            "value": value,
            "metadata": {},
            "is_default": True,
            "enabled": True,
            "source": f"settings.{attr}",
        })

    return rows


async def _table_has_any_row(conn) -> bool:
    """True iff ``llm_credentials`` has at least one row (any tenant,
    any provider). ``LIMIT 1`` is cheaper than ``COUNT(*)``.
    """
    row = await conn.fetchrow("SELECT 1 FROM llm_credentials LIMIT 1")
    return row is not None


async def _insert_one(conn, row: dict[str, Any]) -> bool:
    """Insert a single planned row via ``ON CONFLICT (id) DO NOTHING``.

    Returns True iff this call inserted (i.e. won the race against any
    concurrent worker that may have proposed the same deterministic
    id). Returns False on conflict — silently — so audit + log emission
    stays one-per-row across the whole worker pool.
    """
    from backend.secret_store import encrypt

    enc_value = encrypt(row["value"]) if row.get("value") else ""
    meta_json = json.dumps(row.get("metadata") or {})
    now = time.time()
    inserted = await conn.fetchrow(
        "INSERT INTO llm_credentials ("
        " id, tenant_id, provider, label, encrypted_value, metadata,"
        " auth_type, is_default, enabled, created_at, updated_at, version"
        ") VALUES ("
        " $1, $2, $3, $4, $5, $6,"
        " 'pat', $7, $8, $9, $10, 0"
        ") ON CONFLICT (id) DO NOTHING "
        "RETURNING id",
        row["id"], _DEFAULT_TENANT, row["provider"],
        row.get("label", ""), enc_value, meta_json,
        bool(row.get("is_default", False)),
        bool(row.get("enabled", True)),
        now, now,
    )
    return inserted is not None


async def migrate_legacy_llm_credentials_once() -> dict[str, Any]:
    """Lifespan hook entry point.

    Returns a structured summary the caller can log:

    ``{
        "migrated": int,                 # rows actually inserted
        "candidates": int,               # rows planned (incl. losers)
        "skipped_reason": str | None,    # set iff migration was no-op
        "sources": list[str],            # source tags of inserted rows
    }``

    Never raises on pool / DB errors — those are logged at warning and
    surface as ``skipped_reason="error: ..."`` so a transient PG hiccup
    on boot doesn't crash the lifespan. (The Phase-5b resolver falls
    back to legacy Settings if migration didn't run, so the rest of
    the app keeps working.)
    """
    # ── Kill switch (operator escape hatch) ──
    kill = (os.environ.get(_KILL_SWITCH_ENV) or "").strip().lower()
    if kill == _KILL_SWITCH_SKIP:
        logger.info(
            "[LLM-CRED-MIGRATE] %s=skip — bypassing legacy → "
            "llm_credentials migration entirely.",
            _KILL_SWITCH_ENV,
        )
        return {
            "migrated": 0,
            "candidates": 0,
            "skipped_reason": f"env:{_KILL_SWITCH_ENV}=skip",
            "sources": [],
        }

    # ── Pool gate (SQLite dev mode has no pool) ──
    try:
        from backend.db_pool import get_pool
        pool = get_pool()
    except RuntimeError:
        logger.info(
            "[LLM-CRED-MIGRATE] db_pool not initialised (SQLite dev "
            "mode); skipping legacy LLM credential migration."
        )
        return {
            "migrated": 0,
            "candidates": 0,
            "skipped_reason": "no_pool",
            "sources": [],
        }

    # ── Plan rows from current Settings snapshot ──
    planned = _plan_rows()
    if not planned:
        logger.info(
            "[LLM-CRED-MIGRATE] no legacy LLM credentials present in "
            "Settings; nothing to migrate."
        )
        return {
            "migrated": 0,
            "candidates": 0,
            "skipped_reason": "no_legacy_credentials",
            "sources": [],
        }

    # ── Idempotency: if any row already exists, defer to operator. ──
    try:
        async with pool.acquire() as conn:
            if await _table_has_any_row(conn):
                logger.info(
                    "[LLM-CRED-MIGRATE] llm_credentials already has "
                    "rows; skipping legacy migration "
                    "(operator-managed table)."
                )
                return {
                    "migrated": 0,
                    "candidates": len(planned),
                    "skipped_reason": "llm_credentials_non_empty",
                    "sources": [],
                }
    except Exception as exc:
        logger.warning(
            "[LLM-CRED-MIGRATE] idempotency check failed (%s); "
            "skipping migration this boot.",
            type(exc).__name__,
        )
        return {
            "migrated": 0,
            "candidates": len(planned),
            "skipped_reason": f"error:{type(exc).__name__}",
            "sources": [],
        }

    # ── Set tenant context for audit chain ──
    from backend.db_context import current_tenant_id, set_tenant_id
    prev_tid = current_tenant_id()
    set_tenant_id(_DEFAULT_TENANT)

    inserted_sources: list[str] = []
    try:
        for row in planned:
            try:
                won = False
                async with pool.acquire() as conn:
                    won = await _insert_one(conn, row)
            except Exception as exc:
                logger.warning(
                    "[LLM-CRED-MIGRATE] insert failed for source=%s "
                    "id=%s (%s); continuing with remaining rows.",
                    row.get("source"), row.get("id"),
                    type(exc).__name__,
                )
                continue

            if not won:
                logger.debug(
                    "[LLM-CRED-MIGRATE] id=%s already inserted by "
                    "sibling worker; skipping audit emit.",
                    row.get("id"),
                )
                continue

            inserted_sources.append(row.get("source", row["id"]))
            # Fire audit row. Don't pass the plaintext key — only
            # metadata (id / provider / source) so a leaked audit
            # snapshot cannot be replayed as a credential.
            try:
                from backend import audit as _audit
                await _audit.log(
                    action=_AUDIT_ACTION,
                    entity_kind="llm_credential",
                    entity_id=row["id"],
                    before=None,
                    after={
                        "provider": row["provider"],
                        "label": row.get("label", ""),
                        "is_default": bool(row.get("is_default", False)),
                        "has_key": bool(row.get("value")),
                        "metadata_keys": sorted(
                            (row.get("metadata") or {}).keys()
                        ),
                        "source": row.get("source", ""),
                    },
                    actor="system/migration",
                )
            except Exception as exc:  # pragma: no cover — audit best-effort
                logger.warning(
                    "[LLM-CRED-MIGRATE] audit.log raised %s for id=%s "
                    "— row already inserted, proceeding.",
                    type(exc).__name__, row.get("id"),
                )
            logger.warning(
                "[LLM-CRED-MIGRATE] migrated source=%s → "
                "llm_credentials row %s (provider=%s, default=%s). "
                "Leave the .env key in place until operator decides "
                "to clean it up — reads still fall through to it if "
                "llm_credentials is later cleared.",
                row.get("source"), row["id"], row["provider"],
                row.get("is_default", False),
            )
    finally:
        set_tenant_id(prev_tid)

    return {
        "migrated": len(inserted_sources),
        "candidates": len(planned),
        "skipped_reason": None,
        "sources": inserted_sources,
    }
