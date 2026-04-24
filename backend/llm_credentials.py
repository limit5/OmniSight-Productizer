"""Phase 5b-3 (#llm-credentials) — llm_credentials CRUD service.

Pool-backed CRUD for the ``llm_credentials`` table introduced in
row 5b-1 (alembic 0029). Mirrors :mod:`backend.git_accounts`'s
service layer — deliberately copy-paste-with-domain-rename per the
Phase 5b preamble (``可沿用 Phase 5-1 的 git_accounts 結構複製貼上
改 domain 名``) — with these domain-specific simplifications:

* Single ``encrypted_value`` column (LLM credential is one API key)
  replaces the three ``encrypted_{token,ssh_key,webhook_secret}``
  columns on ``git_accounts``.
* No ``url_patterns`` / ``ssh_host`` / ``ssh_port`` / ``project``
  (not meaningful for LLM providers — routing is per-(tenant,
  provider), not per-URL).
* ``metadata`` JSONB carries ``base_url`` (ollama / self-hosted
  OpenAI-compatible gateways), ``org_id`` (OpenAI org scoping),
  ``notes`` and future OAuth ``scopes``.
* Ollama is keyless — ``encrypted_value`` stays empty but a row
  still exists so the resolver can thread ``base_url`` through
  ``metadata`` without reaching back into ``settings``.

Every mutator is tenant-scoped through ``db_context.require_current_tenant``
(pinned by the FastAPI router via the authenticated user's
``tenant_id``) and writes an ``audit_log`` row so operators can
trace who rotated / deleted a credential and when. API responses
use :func:`backend.secret_store.fingerprint` (``…abc4``) — plaintext
never leaves the server.

Module-global audit (SOP Step 1, qualified answer #2 — PG coordination)
───────────────────────────────────────────────────────────────────────
No module-globals. Same story as row 5-4: ``secret_store._fernet``
is lazily cached per worker from the same key source (env var ∨
``.secret_key`` file, first-boot race closed by SP-B.3's flock)
so ciphertext produced by one worker decrypts on any other; races
on ``is_default`` / version are serialised by PG via (a) the partial
unique index ``uq_llm_credentials_default_per_provider`` (b)
row-level locking on UPDATE / DELETE.

Read-after-write audit (SOP Step 1)
───────────────────────────────────
Three parallel-handler hazard points — same as row 5-4:

1. Two concurrent POSTs with ``is_default=TRUE`` for the same
   ``(tenant, provider)`` — partial unique index blocks the loser
   with ``UniqueViolationError``; surfaced as HTTP 409.
2. Two concurrent PATCHes flipping ``is_default`` — same invariant,
   same index. Implemented as a two-step UPDATE inside a transaction
   so the index never observes two TRUEs.
3. DELETE of the default credential — done inside a transaction
   with optional auto-elect-new-default so the tenant's provider
   is never left defaultless after the call returns.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import asyncpg

from backend.db_context import require_current_tenant
from backend.secret_store import decrypt, encrypt, fingerprint

logger = logging.getLogger(__name__)


_VALID_PROVIDERS = {
    "anthropic", "google", "openai", "xai", "groq",
    "deepseek", "together", "openrouter", "ollama",
}
_VALID_AUTH_TYPES = {"pat", "oauth"}


_LLM_CREDENTIALS_COLS = (
    "id, tenant_id, provider, label, encrypted_value, metadata, "
    "auth_type, is_default, enabled, last_used_at, created_at, "
    "updated_at, version"
)


class LLMCredentialConflict(Exception):
    """Raised when a mutation would violate the partial unique index
    ``uq_llm_credentials_default_per_provider`` — at most one row per
    ``(tenant_id, provider)`` may have ``is_default=TRUE``. Surfaced
    by the router as HTTP 409."""


class LLMCredentialNotFound(Exception):
    """Raised when the target row doesn't exist in this tenant's scope."""


def _new_id() -> str:
    return f"lc-{uuid.uuid4().hex[:12]}"


def _safe_decrypt(ciphertext: str, row_id: str = "?") -> str:
    if not ciphertext:
        return ""
    try:
        return decrypt(ciphertext)
    except Exception as exc:
        logger.warning(
            "llm_credentials row %s: decrypt failed (%s) — treating as empty",
            row_id, type(exc).__name__,
        )
        return ""


def _parse_metadata(raw: Any) -> dict[str, Any]:
    """Normalise the ``metadata`` column into a dict.

    JSONB on PG returns a dict/list directly; SQLite stores TEXT-of-JSON.
    A decode failure yields an empty dict rather than crashing the
    response (the row is still usable by the resolver chain).
    """
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def _row_to_public_dict(row: Any) -> dict[str, Any]:
    """Shape an ``llm_credentials`` row for API response.

    **Does not include the plaintext API key.** Only the fingerprint
    (``…abc4``) is exposed so a stolen response body cannot be
    replayed as a credential. The ``encrypted_value`` ciphertext is
    also NOT echoed — the fingerprint derives from the plaintext
    but that plaintext never leaves the server side.
    """
    row_id = row["id"]
    value_plain = _safe_decrypt(row["encrypted_value"] or "", row_id)
    metadata = _parse_metadata(row["metadata"])

    return {
        "id": row_id,
        "tenant_id": row["tenant_id"],
        "provider": row["provider"],
        "label": row["label"] or "",
        "value_fingerprint":
            fingerprint(value_plain) if value_plain else "",
        "auth_type": row["auth_type"] or "pat",
        "is_default": bool(row["is_default"]),
        "enabled": bool(row["enabled"]),
        "metadata": metadata,
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": int(row["version"] or 0),
    }


def _validate_provider(provider: str) -> None:
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"Unknown provider {provider!r}; expected one of "
            f"{sorted(_VALID_PROVIDERS)}"
        )


def _validate_auth_type(auth_type: str) -> None:
    if auth_type not in _VALID_AUTH_TYPES:
        raise ValueError(
            f"Unknown auth_type {auth_type!r}; expected one of "
            f"{sorted(_VALID_AUTH_TYPES)}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CRUD — implementations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def list_credentials(
    *,
    provider: str | None = None,
    enabled_only: bool = False,
    conn=None,
) -> list[dict[str, Any]]:
    """List credentials scoped to the current tenant.

    ``enabled_only=True`` filters out soft-disabled rows; default is
    ``False`` so the admin UI can see + re-enable them.
    """
    tid = require_current_tenant()
    where = ["tenant_id = $1"]
    params: list[Any] = [tid]
    if provider:
        _validate_provider(provider)
        where.append(f"provider = ${len(params) + 1}")
        params.append(provider)
    if enabled_only:
        where.append("enabled = TRUE")
    sql = (
        f"SELECT {_LLM_CREDENTIALS_COLS} FROM llm_credentials "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY provider, is_default DESC, "
        "last_used_at DESC NULLS LAST, id"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            rows = await owned.fetch(sql, *params)
    else:
        rows = await conn.fetch(sql, *params)
    return [_row_to_public_dict(r) for r in rows]


async def get_credential(
    credential_id: str, *, conn=None,
) -> dict[str, Any] | None:
    """Return the public-dict form of one credential, or ``None`` if
    not found in this tenant's scope."""
    tid = require_current_tenant()
    sql = (
        f"SELECT {_LLM_CREDENTIALS_COLS} FROM llm_credentials "
        "WHERE tenant_id = $1 AND id = $2"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, tid, credential_id)
    else:
        row = await conn.fetchrow(sql, tid, credential_id)
    if row is None:
        return None
    return _row_to_public_dict(row)


async def get_plaintext_value(
    credential_id: str, *, conn=None,
) -> str | None:
    """Internal: fetch the plaintext API key for this tenant's row.

    Used by the ``POST /{id}/test`` live-probe handler. Never
    returned to API responses.
    """
    tid = require_current_tenant()
    sql = (
        "SELECT encrypted_value FROM llm_credentials "
        "WHERE tenant_id = $1 AND id = $2"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, tid, credential_id)
    else:
        row = await conn.fetchrow(sql, tid, credential_id)
    if row is None:
        return None
    ct = row["encrypted_value"] or ""
    if not ct:
        return ""
    return _safe_decrypt(ct, credential_id)


async def create_credential(
    *,
    provider: str,
    label: str = "",
    value: str = "",
    auth_type: str = "pat",
    is_default: bool = False,
    enabled: bool = True,
    metadata: dict[str, Any] | None = None,
    conn=None,
) -> dict[str, Any]:
    """Create a new ``llm_credentials`` row for the current tenant.

    ``value`` is the plaintext API key — Fernet-encrypted server-side
    before it hits the DB; an empty string is allowed (useful for
    keyless providers like ``ollama`` where ``metadata.base_url``
    is the operative field).

    Raises :class:`LLMCredentialConflict` if the new row would violate
    the one-default-per-(tenant, provider) invariant.
    """
    _validate_provider(provider)
    _validate_auth_type(auth_type)
    tid = require_current_tenant()

    now = time.time()
    meta_json = json.dumps(metadata or {})
    row_id = _new_id()
    enc_value = encrypt(value) if value else ""

    insert_sql = (
        "INSERT INTO llm_credentials ("
        "id, tenant_id, provider, label, encrypted_value, metadata, "
        "auth_type, is_default, enabled, created_at, updated_at, version"
        ") VALUES ("
        "$1, $2, $3, $4, $5, $6, "
        "$7, $8, $9, $10, $11, 0"
        ") RETURNING " + _LLM_CREDENTIALS_COLS
    )
    params = [
        row_id, tid, provider, label, enc_value, meta_json,
        auth_type, bool(is_default), bool(enabled), now, now,
    ]

    async def _run(c) -> dict[str, Any]:
        try:
            row = await c.fetchrow(insert_sql, *params)
        except asyncpg.UniqueViolationError as exc:
            raise LLMCredentialConflict(
                f"A credential for provider={provider!r} is already "
                f"marked default in this tenant; clear that first or "
                f"create this credential with is_default=False. ({exc})"
            ) from exc
        return _row_to_public_dict(row)

    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            out = await _run(owned)
    else:
        out = await _run(conn)

    try:
        from backend import audit as _audit
        after = {k: v for k, v in out.items() if k not in {"tenant_id"}}
        await _audit.log(
            action="llm_credential.create",
            entity_kind="llm_credential",
            entity_id=out["id"],
            before=None,
            after=after,
        )
    except Exception as exc:  # pragma: no cover — audit best-effort
        logger.warning(
            "llm_credentials.create_credential: audit.log raised %s — proceeding",
            type(exc).__name__,
        )
    return out


async def update_credential(
    credential_id: str,
    *,
    updates: dict[str, Any],
    conn=None,
) -> dict[str, Any]:
    """Update selected fields on an existing credential.

    Recognised keys
    ───────────────
    ``label``, ``auth_type``, ``is_default``, ``enabled``, ``metadata``
    — direct column writes (``is_default`` handled specially, see
    below). ``value`` — re-encrypt before write; empty string clears
    it; ``None`` leaves it alone (partial-update / rotation semantics
    match row 5-4).

    Unknown keys raise ``ValueError`` so typos don't silently no-op.

    ``is_default=True`` flip
    ────────────────────────
    Inside a transaction, first clear any existing default for this
    (tenant, provider), then set this row's flag. The partial unique
    index sees at most one default at any COMMIT point.

    Raises
    ──────
    * :class:`LLMCredentialNotFound` — id not in this tenant.
    * :class:`LLMCredentialConflict` — partial-unique race.
    * :class:`ValueError` — unknown key / bad auth_type.
    """
    tid = require_current_tenant()
    if not updates:
        existing = await get_credential(credential_id, conn=conn)
        if existing is None:
            raise LLMCredentialNotFound(credential_id)
        return existing

    allowed_direct = {"label", "auth_type", "enabled"}
    allowed_json = {"metadata"}
    allowed_enc = {"value"}
    allowed_flag = {"is_default"}
    allowed = allowed_direct | allowed_json | allowed_enc | allowed_flag

    unknown = [k for k in updates if k not in allowed]
    if unknown:
        raise ValueError(f"Unknown update fields: {unknown}")

    set_clauses: list[str] = []
    params: list[Any] = []
    idx = 1

    want_set_default = updates.get("is_default") is True
    want_clear_default = updates.get("is_default") is False

    for k, v in updates.items():
        if k == "is_default":
            continue
        if k in allowed_direct:
            if k == "auth_type":
                _validate_auth_type(v)
            if k == "enabled":
                v = bool(v)
            set_clauses.append(f"{k} = ${idx}")
            params.append(v)
            idx += 1
        elif k in allowed_json:
            v = dict(v) if v is not None else {}
            set_clauses.append(f"metadata = ${idx}")
            params.append(json.dumps(v))
            idx += 1
        elif k in allowed_enc:
            # ``value`` → ``encrypted_value`` — rotate the key.
            if v is None:
                continue  # no-op on this column
            if v == "":
                set_clauses.append(f"encrypted_value = ${idx}")
                params.append("")
            else:
                set_clauses.append(f"encrypted_value = ${idx}")
                params.append(encrypt(v))
            idx += 1

    now = time.time()
    set_clauses.append(f"updated_at = ${idx}")
    params.append(now)
    idx += 1
    set_clauses.append("version = version + 1")

    pk_sql = f"tenant_id = ${idx} AND id = ${idx + 1}"
    params_pk = [tid, credential_id]

    async def _run(c) -> tuple[dict[str, Any], dict[str, Any]]:
        before = await c.fetchrow(
            f"SELECT {_LLM_CREDENTIALS_COLS} FROM llm_credentials "
            "WHERE tenant_id = $1 AND id = $2",
            tid, credential_id,
        )
        if before is None:
            raise LLMCredentialNotFound(credential_id)

        async with c.transaction():
            if want_set_default:
                await c.execute(
                    "UPDATE llm_credentials SET is_default = FALSE, "
                    "updated_at = $1, version = version + 1 "
                    "WHERE tenant_id = $2 AND provider = $3 "
                    "AND is_default = TRUE AND id <> $4",
                    now, tid, before["provider"], credential_id,
                )
                set_clauses.insert(0, "is_default = TRUE")
            elif want_clear_default:
                set_clauses.insert(0, "is_default = FALSE")

            sql = (
                f"UPDATE llm_credentials SET {', '.join(set_clauses)} "
                f"WHERE {pk_sql} "
                f"RETURNING {_LLM_CREDENTIALS_COLS}"
            )
            try:
                row = await c.fetchrow(sql, *params, *params_pk)
            except asyncpg.UniqueViolationError as exc:
                raise LLMCredentialConflict(
                    "Default-flag conflict: another concurrent "
                    f"mutation flipped the default for provider "
                    f"{before['provider']!r}. Retry."
                ) from exc
            if row is None:
                raise LLMCredentialNotFound(credential_id)
        return _row_to_public_dict(row), _row_to_public_dict(before)

    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            after_pub, before_pub = await _run(owned)
    else:
        after_pub, before_pub = await _run(conn)

    try:
        from backend import audit as _audit
        diff_keys = {
            k for k in after_pub
            if k != "updated_at" and after_pub.get(k) != before_pub.get(k)
        }
        await _audit.log(
            action="llm_credential.update",
            entity_kind="llm_credential",
            entity_id=credential_id,
            before={k: before_pub[k] for k in diff_keys if k in before_pub},
            after={k: after_pub[k] for k in diff_keys if k in after_pub},
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "llm_credentials.update_credential: audit.log raised %s",
            type(exc).__name__,
        )
    return after_pub


async def delete_credential(
    credential_id: str,
    *,
    auto_elect_new_default: bool = True,
    conn=None,
) -> dict[str, Any]:
    """Delete a credential. When deleting the provider's current
    default, either refuse (``auto_elect_new_default=False``) or
    promote the next LRU-ranked enabled row to the default spot
    (``auto_elect_new_default=True``, the default).

    If ``auto_elect_new_default=False`` AND there is at least one
    other enabled row of the same provider, raises
    :class:`LLMCredentialConflict` so the UI can ask the operator to
    flip the default first.

    If ``auto_elect_new_default=False`` AND the deleted row is the
    only enabled row of its provider, the delete proceeds — no
    replacement is possible, the provider just becomes defaultless.

    Returns the deleted row's public dict + ``{"promoted_id":
    <new-default-id-or-None>}``.

    Raises :class:`LLMCredentialNotFound` if the id doesn't match a
    row in this tenant's scope.
    """
    tid = require_current_tenant()

    async def _run(c) -> tuple[dict[str, Any], str | None]:
        async with c.transaction():
            before_row = await c.fetchrow(
                f"SELECT {_LLM_CREDENTIALS_COLS} FROM llm_credentials "
                "WHERE tenant_id = $1 AND id = $2",
                tid, credential_id,
            )
            if before_row is None:
                raise LLMCredentialNotFound(credential_id)
            before_pub = _row_to_public_dict(before_row)
            provider = before_row["provider"]
            was_default = bool(before_row["is_default"])

            promoted_id: str | None = None
            if was_default and not auto_elect_new_default:
                other = await c.fetchrow(
                    "SELECT id FROM llm_credentials "
                    "WHERE tenant_id = $1 AND provider = $2 "
                    "AND id <> $3 AND enabled = TRUE LIMIT 1",
                    tid, provider, credential_id,
                )
                if other is not None:
                    raise LLMCredentialConflict(
                        f"Refusing to delete the default {provider!r} "
                        f"credential without a replacement. Mark another "
                        f"{provider!r} credential default first, or call "
                        f"again with auto_elect_new_default=True."
                    )

            status = await c.execute(
                "DELETE FROM llm_credentials "
                "WHERE tenant_id = $1 AND id = $2",
                tid, credential_id,
            )
            try:
                deleted_count = int(status.rsplit(" ", 1)[-1])
            except (ValueError, AttributeError):
                deleted_count = 0
            if deleted_count == 0:
                raise LLMCredentialNotFound(credential_id)

            if was_default and auto_elect_new_default:
                candidate = await c.fetchrow(
                    "SELECT id FROM llm_credentials "
                    "WHERE tenant_id = $1 AND provider = $2 "
                    "AND enabled = TRUE "
                    "ORDER BY last_used_at DESC NULLS LAST, id LIMIT 1",
                    tid, provider,
                )
                if candidate is not None:
                    promoted_id = candidate["id"]
                    await c.execute(
                        "UPDATE llm_credentials SET is_default = TRUE, "
                        "updated_at = $1, version = version + 1 "
                        "WHERE tenant_id = $2 AND id = $3",
                        time.time(), tid, promoted_id,
                    )
        return before_pub, promoted_id

    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            before_pub, promoted_id = await _run(owned)
    else:
        before_pub, promoted_id = await _run(conn)

    try:
        from backend import audit as _audit
        await _audit.log(
            action="llm_credential.delete",
            entity_kind="llm_credential",
            entity_id=credential_id,
            before=before_pub,
            after={"promoted_id": promoted_id} if promoted_id else None,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "llm_credentials.delete_credential: audit.log raised %s",
            type(exc).__name__,
        )
    out = dict(before_pub)
    out["promoted_id"] = promoted_id
    return out
