"""Phase 5-4 (#multi-account-forge) — git_accounts CRUD service.

Pool-backed CRUD for the ``git_accounts`` table introduced in
row 5-1 (alembic 0027). Every mutator is tenant-scoped through
``db_context.current_tenant_id()`` (set by the FastAPI
``require_tenant`` dependency at the router edge) and writes an
``audit_log`` row so operators can trace who rotated / deleted a
credential and when.

Token / SSH key / webhook secret are never stored plaintext — they
round-trip through :mod:`backend.secret_store`'s Fernet primitives.
List / detail responses use :func:`backend.secret_store.fingerprint`
(``…abc4``) so a logs/screenshot leak cannot reveal the raw PAT.

Module-global audit (SOP Step 1, qualified answer #2)
─────────────────────────────────────────────────────
No module-globals. ``secret_store._fernet`` is lazily cached per
worker from the same key source (env var ∨ ``.secret_key`` file,
first-boot race closed by SP-B.3's flock) so ciphertext produced
by one worker decrypts on any other; all mutator writes land on
PG which serialises races via (a) the partial unique index
``uq_git_accounts_default_per_platform`` for the one-default-per-
platform invariant and (b) standard row-level locking for
UPDATE / DELETE.

Read-after-write audit (SOP Step 1)
───────────────────────────────────
The CRUD surface has three points where parallel router handlers
could race on ``git_accounts``:

1. **Two concurrent POSTs with ``is_default=TRUE``** for the same
   ``(tenant, platform)`` — the partial unique index blocks the
   loser with ``UniqueViolationError``; we catch it and surface
   HTTP 409 with a helpful message.
2. **Two concurrent PATCHes flipping ``is_default``** — same
   invariant, same index. Implemented as a two-step inside a
   transaction: clear any existing default (UPDATE), then set
   this row's flag (UPDATE). Both statements inside one tx so
   the partial unique index never observes an intermediate
   "two defaults" state.
3. **DELETE of the default account** — done inside a transaction
   with optional auto-elect-new-default so the tenant's platform
   is never left defaultless after the call returns.

All three guarantee "after the CRUD returns 2xx, a follow-up GET
sees the committed state" because the commit happens inside the
same pool conn the caller owns (no pool-level read-after-write
reordering across connections).

Phase 5-3 vs 5-4: 5-3 owns the resolver (read side). This module
owns the writer side. The two converge on the same ``git_accounts``
row shape; see ``backend/git_credentials.py::_row_to_dict`` for
the reader-side normaliser.
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


_VALID_PLATFORMS = {"github", "gitlab", "gerrit", "jira"}
_VALID_AUTH_TYPES = {"pat", "oauth", "ssh"}


_GIT_ACCOUNTS_COLS = (
    "id, tenant_id, platform, instance_url, label, username, "
    "encrypted_token, encrypted_ssh_key, ssh_host, ssh_port, project, "
    "encrypted_webhook_secret, url_patterns, auth_type, is_default, "
    "enabled, metadata, last_used_at, created_at, updated_at, version"
)


class GitAccountConflict(Exception):
    """Raised when a mutation would violate a uniqueness invariant.

    Today that is the partial unique index
    ``uq_git_accounts_default_per_platform`` — at most one row per
    ``(tenant_id, platform)`` may have ``is_default=TRUE``. Surfaced
    by the router as HTTP 409.
    """


class GitAccountNotFound(Exception):
    """Raised when the target row doesn't exist in this tenant's scope."""


def _new_id() -> str:
    return f"ga-{uuid.uuid4().hex[:12]}"


def _safe_decrypt(ciphertext: str, row_id: str = "?") -> str:
    if not ciphertext:
        return ""
    try:
        return decrypt(ciphertext)
    except Exception as exc:
        logger.warning(
            "git_accounts row %s: decrypt failed (%s) — treating as empty",
            row_id, type(exc).__name__,
        )
        return ""


def _row_to_public_dict(row: Any) -> dict[str, Any]:
    """Shape a ``git_accounts`` row for API response.

    **Does not include plaintext token / ssh_key / webhook_secret.**
    Only the fingerprint (``…abc4``) is exposed so a stolen response
    body cannot be replayed as a credential. The ``encrypted_*``
    ciphertext is also NOT echoed — the fingerprint is derived from
    the plaintext but that plaintext never leaves the server side.
    """
    # url_patterns + metadata: JSONB on PG → list/dict; TEXT on SQLite → str.
    patterns_raw = row["url_patterns"]
    if isinstance(patterns_raw, str):
        try:
            url_patterns = json.loads(patterns_raw) or []
        except (json.JSONDecodeError, TypeError):
            url_patterns = []
    elif isinstance(patterns_raw, list):
        url_patterns = patterns_raw
    else:
        url_patterns = []

    meta_raw = row["metadata"]
    if isinstance(meta_raw, str):
        try:
            metadata = json.loads(meta_raw) or {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    elif isinstance(meta_raw, dict):
        metadata = meta_raw
    else:
        metadata = {}

    row_id = row["id"]
    token_plain = _safe_decrypt(row["encrypted_token"] or "", row_id)
    ssh_plain = _safe_decrypt(row["encrypted_ssh_key"] or "", row_id)
    whs_plain = _safe_decrypt(row["encrypted_webhook_secret"] or "", row_id)

    return {
        "id": row_id,
        "tenant_id": row["tenant_id"],
        "platform": row["platform"],
        "instance_url": row["instance_url"] or "",
        "label": row["label"] or "",
        "username": row["username"] or "",
        "token_fingerprint": fingerprint(token_plain) if token_plain else "",
        "ssh_key_fingerprint": fingerprint(ssh_plain) if ssh_plain else "",
        "webhook_secret_fingerprint":
            fingerprint(whs_plain) if whs_plain else "",
        "ssh_host": row["ssh_host"] or "",
        "ssh_port": int(row["ssh_port"] or 0),
        "project": row["project"] or "",
        "url_patterns": list(url_patterns),
        "auth_type": row["auth_type"] or "pat",
        "is_default": bool(row["is_default"]),
        "enabled": bool(row["enabled"]),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": int(row["version"] or 0),
    }


def _validate_platform(platform: str) -> None:
    if platform not in _VALID_PLATFORMS:
        raise ValueError(
            f"Unknown platform {platform!r}; expected one of "
            f"{sorted(_VALID_PLATFORMS)}"
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


async def list_accounts(
    *,
    platform: str | None = None,
    enabled_only: bool = False,
    conn=None,
) -> list[dict[str, Any]]:
    """List accounts scoped to the current tenant.

    ``enabled_only=True`` filters out soft-disabled rows; the default
    is ``False`` so the admin UI can see + re-enable them.
    """
    tid = require_current_tenant()
    where = ["tenant_id = $1"]
    params: list[Any] = [tid]
    if platform:
        _validate_platform(platform)
        where.append(f"platform = ${len(params) + 1}")
        params.append(platform)
    if enabled_only:
        where.append("enabled = TRUE")
    sql = (
        f"SELECT {_GIT_ACCOUNTS_COLS} FROM git_accounts "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY platform, is_default DESC, "
        "last_used_at DESC NULLS LAST, id"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            rows = await owned.fetch(sql, *params)
    else:
        rows = await conn.fetch(sql, *params)
    return [_row_to_public_dict(r) for r in rows]


async def get_account(account_id: str, *, conn=None) -> dict[str, Any] | None:
    """Return the public-dict form of one account, or ``None`` if
    not found in this tenant's scope.
    """
    tid = require_current_tenant()
    sql = (
        f"SELECT {_GIT_ACCOUNTS_COLS} FROM git_accounts "
        "WHERE tenant_id = $1 AND id = $2"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, tid, account_id)
    else:
        row = await conn.fetchrow(sql, tid, account_id)
    if row is None:
        return None
    return _row_to_public_dict(row)


async def get_plaintext_token(account_id: str, *, conn=None) -> str | None:
    """Internal: fetch the plaintext token for this tenant's row.
    Used by the ``POST /{id}/test`` live-probe handler. Never returned
    to API responses."""
    tid = require_current_tenant()
    sql = (
        "SELECT encrypted_token FROM git_accounts "
        "WHERE tenant_id = $1 AND id = $2"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            row = await owned.fetchrow(sql, tid, account_id)
    else:
        row = await conn.fetchrow(sql, tid, account_id)
    if row is None:
        return None
    ct = row["encrypted_token"] or ""
    if not ct:
        return ""
    return _safe_decrypt(ct, account_id)


async def create_account(
    *,
    platform: str,
    instance_url: str = "",
    label: str = "",
    username: str = "",
    token: str = "",
    ssh_key: str = "",
    ssh_host: str = "",
    ssh_port: int = 0,
    project: str = "",
    webhook_secret: str = "",
    url_patterns: list[str] | None = None,
    auth_type: str = "pat",
    is_default: bool = False,
    enabled: bool = True,
    metadata: dict[str, Any] | None = None,
    conn=None,
) -> dict[str, Any]:
    """Create a new ``git_accounts`` row for the current tenant.

    Raises :class:`GitAccountConflict` if the new row would violate
    the one-default-per-(tenant,platform) invariant.
    """
    _validate_platform(platform)
    _validate_auth_type(auth_type)
    tid = require_current_tenant()

    now = time.time()
    patterns_json = json.dumps(list(url_patterns) if url_patterns else [])
    meta_json = json.dumps(metadata or {})
    row_id = _new_id()

    enc_token = encrypt(token) if token else ""
    enc_ssh = encrypt(ssh_key) if ssh_key else ""
    enc_whs = encrypt(webhook_secret) if webhook_secret else ""

    insert_sql = (
        "INSERT INTO git_accounts ("
        "id, tenant_id, platform, instance_url, label, username, "
        "encrypted_token, encrypted_ssh_key, ssh_host, ssh_port, project, "
        "encrypted_webhook_secret, url_patterns, auth_type, is_default, "
        "enabled, metadata, created_at, updated_at, version"
        ") VALUES ("
        "$1, $2, $3, $4, $5, $6, "
        "$7, $8, $9, $10, $11, "
        "$12, $13, $14, $15, "
        "$16, $17, $18, $19, 0"
        ") RETURNING " + _GIT_ACCOUNTS_COLS
    )
    params = [
        row_id, tid, platform, instance_url, label, username,
        enc_token, enc_ssh, ssh_host, int(ssh_port), project,
        enc_whs, patterns_json, auth_type, bool(is_default),
        bool(enabled), meta_json, now, now,
    ]

    async def _run(c) -> dict[str, Any]:
        try:
            row = await c.fetchrow(insert_sql, *params)
        except asyncpg.UniqueViolationError as exc:
            # Partial unique index fires if is_default=TRUE collides.
            raise GitAccountConflict(
                f"An account for platform={platform!r} is already "
                f"marked default in this tenant; clear that first or "
                f"create this account with is_default=False. ({exc})"
            ) from exc
        return _row_to_public_dict(row)

    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned:
            out = await _run(owned)
    else:
        out = await _run(conn)

    # Fire-and-forget audit on the same connection path the caller uses.
    try:
        from backend import audit as _audit
        after = {k: v for k, v in out.items() if k not in {"tenant_id"}}
        await _audit.log(
            action="git_account.create",
            entity_kind="git_account",
            entity_id=out["id"],
            before=None,
            after=after,
        )
    except Exception as exc:  # pragma: no cover — audit best-effort
        logger.warning(
            "git_accounts.create_account: audit.log raised %s — proceeding",
            type(exc).__name__,
        )
    return out


async def update_account(
    account_id: str,
    *,
    updates: dict[str, Any],
    conn=None,
) -> dict[str, Any]:
    """Update selected fields on an existing account.

    ``updates`` keys that are recognised
    ─────────────────────────────────────
    ``label``, ``username``, ``instance_url``, ``ssh_host``,
    ``ssh_port``, ``project``, ``url_patterns``, ``auth_type``,
    ``is_default``, ``enabled``, ``metadata`` — direct column writes.
    ``token``, ``ssh_key``, ``webhook_secret`` — re-encrypt before
    write (empty string explicitly clears the secret; ``None`` leaves
    it alone).

    Unknown keys raise ValueError so typos don't silently no-op.

    ``is_default=True`` flip
    ────────────────────────
    Inside a transaction, first clear any existing default for this
    tenant+platform, then set this row's flag. The partial unique
    index sees at most one default at any COMMIT point — there's no
    window where two rows appear default. If the target row doesn't
    exist in this tenant's scope, :class:`GitAccountNotFound` is
    raised and no UPDATE side effects are committed.

    Raises
    ──────
    * :class:`GitAccountNotFound` — id not in this tenant.
    * :class:`GitAccountConflict` — partial-unique race.
    * :class:`ValueError` — unknown update key / bad platform /
      bad auth_type.
    """
    tid = require_current_tenant()
    if not updates:
        # Nothing to do — still return the current row so callers can
        # idempotently sync their UI state.
        existing = await get_account(account_id, conn=conn)
        if existing is None:
            raise GitAccountNotFound(account_id)
        return existing

    allowed_direct = {
        "label", "username", "instance_url", "ssh_host", "ssh_port",
        "project", "auth_type", "enabled",
    }
    allowed_json = {"url_patterns", "metadata"}
    allowed_enc = {"token", "ssh_key", "webhook_secret"}
    allowed_flag = {"is_default"}
    allowed = allowed_direct | allowed_json | allowed_enc | allowed_flag

    unknown = [k for k in updates if k not in allowed]
    if unknown:
        raise ValueError(f"Unknown update fields: {unknown}")

    set_clauses: list[str] = []
    params: list[Any] = []
    idx = 1

    # is_default is handled specially (see below).
    want_set_default = updates.get("is_default") is True
    want_clear_default = updates.get("is_default") is False

    for k, v in updates.items():
        if k == "is_default":
            continue
        if k in allowed_direct:
            if k == "auth_type":
                _validate_auth_type(v)
            if k == "ssh_port":
                v = int(v or 0)
            if k == "enabled":
                v = bool(v)
            set_clauses.append(f"{k} = ${idx}")
            params.append(v)
            idx += 1
        elif k in allowed_json:
            if k == "url_patterns":
                v = list(v) if v is not None else []
                set_clauses.append(f"url_patterns = ${idx}")
                params.append(json.dumps(v))
            else:
                v = dict(v) if v is not None else {}
                set_clauses.append(f"metadata = ${idx}")
                params.append(json.dumps(v))
            idx += 1
        elif k in allowed_enc:
            col = f"encrypted_{k}"
            if v is None:
                # Keyword present but None → no-op on this column.
                continue
            if v == "":
                # Empty string → clear the secret explicitly.
                set_clauses.append(f"{col} = ${idx}")
                params.append("")
            else:
                set_clauses.append(f"{col} = ${idx}")
                params.append(encrypt(v))
            idx += 1

    # Always refresh updated_at + bump version on any change path.
    now = time.time()
    set_clauses.append(f"updated_at = ${idx}")
    params.append(now)
    idx += 1
    set_clauses.append("version = version + 1")

    pk_sql = f"tenant_id = ${idx} AND id = ${idx + 1}"
    params_pk = [tid, account_id]

    async def _run(c) -> dict[str, Any]:
        # Snapshot the "before" state for audit + to return after
        # the transaction commits. Done BEFORE the transaction to
        # keep the snapshot readable even if the UPDATE path raises.
        before = await c.fetchrow(
            f"SELECT {_GIT_ACCOUNTS_COLS} FROM git_accounts "
            "WHERE tenant_id = $1 AND id = $2",
            tid, account_id,
        )
        if before is None:
            raise GitAccountNotFound(account_id)

        async with c.transaction():
            # Handle is_default flip — cannot rely on the partial
            # unique index to auto-swap defaults; we explicitly
            # clear the current default for this platform before
            # setting the target, so the index never observes two
            # TRUEs for the same (tenant, platform).
            if want_set_default:
                await c.execute(
                    "UPDATE git_accounts SET is_default = FALSE, "
                    "updated_at = $1, version = version + 1 "
                    "WHERE tenant_id = $2 AND platform = $3 "
                    "AND is_default = TRUE AND id <> $4",
                    now, tid, before["platform"], account_id,
                )
                set_clauses.insert(0, "is_default = TRUE")
            elif want_clear_default:
                set_clauses.insert(0, "is_default = FALSE")

            sql = (
                f"UPDATE git_accounts SET {', '.join(set_clauses)} "
                f"WHERE {pk_sql} "
                f"RETURNING {_GIT_ACCOUNTS_COLS}"
            )
            try:
                row = await c.fetchrow(sql, *params, *params_pk)
            except asyncpg.UniqueViolationError as exc:
                raise GitAccountConflict(
                    "Default-flag conflict: another concurrent "
                    f"mutation flipped the default for platform "
                    f"{before['platform']!r}. Retry."
                ) from exc
            if row is None:
                # Shouldn't happen — we just read it before the tx,
                # but be defensive against concurrent DELETE.
                raise GitAccountNotFound(account_id)
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
            action="git_account.update",
            entity_kind="git_account",
            entity_id=account_id,
            before={k: before_pub[k] for k in diff_keys if k in before_pub},
            after={k: after_pub[k] for k in diff_keys if k in after_pub},
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "git_accounts.update_account: audit.log raised %s",
            type(exc).__name__,
        )
    return after_pub


async def delete_account(
    account_id: str,
    *,
    auto_elect_new_default: bool = True,
    conn=None,
) -> dict[str, Any]:
    """Delete an account. When deleting the platform's current
    default, either refuse (``auto_elect_new_default=False``) or
    promote the next LRU-ranked enabled row to the default spot
    (``auto_elect_new_default=True``, the default).

    If ``auto_elect_new_default=False`` AND there is at least one
    other enabled row of the same platform, raises
    :class:`GitAccountConflict` with a message describing the
    situation so the UI can ask the operator to flip the default
    first.

    If ``auto_elect_new_default=False`` AND the deleted row is the
    only enabled row of its platform, the delete proceeds — no
    replacement is possible, the platform just becomes defaultless.

    Returns the deleted row's public dict + ``{"promoted_id":
    <new-default-id-or-None>}`` so the caller can tell the UI what
    changed.

    Raises :class:`GitAccountNotFound` if the id doesn't match a
    row in this tenant's scope.
    """
    tid = require_current_tenant()

    async def _run(c) -> tuple[dict[str, Any], str | None]:
        async with c.transaction():
            before_row = await c.fetchrow(
                f"SELECT {_GIT_ACCOUNTS_COLS} FROM git_accounts "
                "WHERE tenant_id = $1 AND id = $2",
                tid, account_id,
            )
            if before_row is None:
                raise GitAccountNotFound(account_id)
            before_pub = _row_to_public_dict(before_row)
            platform = before_row["platform"]
            was_default = bool(before_row["is_default"])

            promoted_id: str | None = None
            if was_default and not auto_elect_new_default:
                other = await c.fetchrow(
                    "SELECT id FROM git_accounts "
                    "WHERE tenant_id = $1 AND platform = $2 "
                    "AND id <> $3 AND enabled = TRUE LIMIT 1",
                    tid, platform, account_id,
                )
                if other is not None:
                    raise GitAccountConflict(
                        f"Refusing to delete the default {platform!r} "
                        f"account without a replacement. Mark another "
                        f"{platform!r} account default first, or call "
                        f"again with auto_elect_new_default=True."
                    )

            # Proceed with delete.
            status = await c.execute(
                "DELETE FROM git_accounts "
                "WHERE tenant_id = $1 AND id = $2",
                tid, account_id,
            )
            try:
                deleted_count = int(status.rsplit(" ", 1)[-1])
            except (ValueError, AttributeError):
                deleted_count = 0
            if deleted_count == 0:
                # Vanished between SELECT and DELETE — treat as not-found.
                raise GitAccountNotFound(account_id)

            # Auto-elect on default-deletion path.
            if was_default and auto_elect_new_default:
                candidate = await c.fetchrow(
                    "SELECT id FROM git_accounts "
                    "WHERE tenant_id = $1 AND platform = $2 "
                    "AND enabled = TRUE "
                    "ORDER BY last_used_at DESC NULLS LAST, id LIMIT 1",
                    tid, platform,
                )
                if candidate is not None:
                    promoted_id = candidate["id"]
                    await c.execute(
                        "UPDATE git_accounts SET is_default = TRUE, "
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
            action="git_account.delete",
            entity_kind="git_account",
            entity_id=account_id,
            before=before_pub,
            after={"promoted_id": promoted_id} if promoted_id else None,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "git_accounts.delete_account: audit.log raised %s",
            type(exc).__name__,
        )
    out = dict(before_pub)
    out["promoted_id"] = promoted_id
    return out
