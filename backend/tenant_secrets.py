"""I4 — Tenant-scoped secrets management.

CRUD API for storing encrypted credentials per tenant. KS.1.11 writes
new values through the per-tenant DEK envelope while preserving legacy
``secret_store`` Fernet reads during the migration window. Secret types:

  - ``git_credential``   — per-repo tokens (GitHub, GitLab, Gerrit…)
  - ``provider_key``     — LLM / SaaS API keys
  - ``cloudflare_token`` — Cloudflare API tokens
  - ``webhook_secret``   — inbound webhook HMAC secrets
  - ``custom``           — anything else

All reads/writes are scoped to the tenant in ``db_context``.

Phase-3-Runtime-v2 SP-4.6 (2026-04-21): ported from aiosqlite
compat wrapper to native asyncpg pool. ``upsert_secret`` is now
atomic via ``INSERT ... ON CONFLICT DO UPDATE`` — the previous
SELECT-then-INSERT/UPDATE was a classic check-then-act race under
pool concurrency (two upserts on the same ``(tenant, type,
key_name)`` could both read "not exists" and both try to INSERT;
one wins with UNIQUE, the other raised an integrity error and the
caller never got a deterministic "last write wins" that the old
SQLite single-writer behaviour had implicitly provided).

Module-global state: none. ``secret_store._fernet`` is lazily
cached per-worker from env / disk; all workers compute the same
key from the same source, so ciphertext is interoperable across
workers (the small race on first-boot key-file generation lives in
secret_store.py, not here — flagged for follow-up).
"""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from typing import Any

from backend.db_context import require_current_tenant
from backend import secret_store
from backend.security import envelope as tenant_envelope

logger = logging.getLogger(__name__)


SECRET_ENVELOPE_FORMAT_VERSION = 1
SECRET_BINDING_FORMAT_VERSION = 1

_SECRET_COLS = (
    "id, tenant_id, secret_type, key_name, encrypted_value, "
    "metadata, created_at, updated_at, version"
)


def _secret_carrier(ciphertext: str, dek_ref: tenant_envelope.TenantDEKRef) -> str:
    payload = {
        "fmt": SECRET_ENVELOPE_FORMAT_VERSION,
        "ciphertext": ciphertext,
        "dek_ref": dek_ref.to_dict(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _load_secret_carrier(
    encrypted_value: str,
) -> tuple[str, tenant_envelope.TenantDEKRef]:
    payload = json.loads(encrypted_value)
    if not isinstance(payload, dict):
        raise ValueError("secret envelope must be an object")
    if payload.get("fmt") != SECRET_ENVELOPE_FORMAT_VERSION:
        raise ValueError("unknown secret envelope format")
    ciphertext = payload.get("ciphertext")
    if not isinstance(ciphertext, str) or not ciphertext:
        raise ValueError("secret envelope missing ciphertext")
    dek_ref_raw = payload.get("dek_ref")
    if not isinstance(dek_ref_raw, dict):
        raise ValueError("secret envelope missing dek_ref")
    return ciphertext, tenant_envelope.TenantDEKRef.from_dict(dek_ref_raw)


def _binding_payload(
    tid: str, secret_type: str, key_name: str, plaintext: str,
) -> str:
    payload = {
        "fmt": SECRET_BINDING_FORMAT_VERSION,
        "tid": tid,
        "typ": secret_type,
        "key": key_name,
        "tok": plaintext,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _load_binding_payload(
    payload: str, tid: str, secret_type: str, key_name: str,
) -> str:
    envelope = json.loads(payload)
    if not isinstance(envelope, dict):
        raise ValueError("secret binding must be an object")
    if envelope.get("fmt") != SECRET_BINDING_FORMAT_VERSION:
        raise ValueError("unknown secret binding format")
    for field, expected in (
        ("tid", tid),
        ("typ", secret_type),
        ("key", key_name),
    ):
        stored = envelope.get(field)
        if not isinstance(stored, str) or not hmac.compare_digest(stored, expected):
            raise ValueError(f"secret binding mismatch: {field}")
    plaintext = envelope.get("tok")
    if not isinstance(plaintext, str):
        raise ValueError("secret binding missing plaintext")
    return plaintext


def _encrypt_secret_value(
    tid: str, secret_type: str, key_name: str, plaintext: str,
) -> str:
    payload = _binding_payload(tid, secret_type, key_name, plaintext)
    ciphertext, dek_ref = tenant_envelope.encrypt(
        payload,
        tid,
        purpose="tenant-secret",
    )
    return _secret_carrier(ciphertext, dek_ref)


def _decrypt_secret_value(
    encrypted_value: str, tid: str, secret_type: str, key_name: str,
) -> str:
    try:
        ciphertext, dek_ref = _load_secret_carrier(encrypted_value)
    except (TypeError, ValueError, tenant_envelope.EnvelopeEncryptionError):
        if isinstance(encrypted_value, str) and not encrypted_value.lstrip().startswith("{"):
            return secret_store.decrypt(encrypted_value)
        raise
    payload = tenant_envelope.decrypt(ciphertext, dek_ref)
    return _load_binding_payload(payload, tid, secret_type, key_name)


async def _list_secrets_impl(
    conn, tid: str, secret_type: str | None,
) -> list[dict[str, Any]]:
    if secret_type:
        rows = await conn.fetch(
            f"SELECT {_SECRET_COLS} FROM tenant_secrets "
            "WHERE tenant_id = $1 AND secret_type = $2 "
            "ORDER BY secret_type, key_name",
            tid, secret_type,
        )
    else:
        rows = await conn.fetch(
            f"SELECT {_SECRET_COLS} FROM tenant_secrets "
            "WHERE tenant_id = $1 "
            "ORDER BY secret_type, key_name",
            tid,
        )
    results: list[dict[str, Any]] = []
    for r in rows:
        try:
            plain = _decrypt_secret_value(
                r["encrypted_value"],
                r["tenant_id"],
                r["secret_type"],
                r["key_name"],
            )
            fp = secret_store.fingerprint(plain)
        except Exception:
            fp = "****"
        meta: dict = {}
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except Exception:
            pass
        results.append({
            "id": r["id"],
            "tenant_id": r["tenant_id"],
            "secret_type": r["secret_type"],
            "key_name": r["key_name"],
            "fingerprint": fp,
            "metadata": meta,
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            # Q.7 #301 — echoed to the client so PUTs can send If-Match.
            "version": int(r["version"]) if r["version"] is not None else 0,
        })
    return results


async def list_secrets(
    secret_type: str | None = None, conn=None,
) -> list[dict[str, Any]]:
    """List secrets for current tenant. Values are fingerprinted, never plaintext."""
    tid = require_current_tenant()
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _list_secrets_impl(owned_conn, tid, secret_type)
    return await _list_secrets_impl(conn, tid, secret_type)


async def get_secret_value(secret_id: str, conn=None) -> str | None:
    """Retrieve the plaintext value of a secret (for internal use only)."""
    tid = require_current_tenant()
    sql = (
        "SELECT encrypted_value, secret_type, key_name FROM tenant_secrets "
        "WHERE id = $1 AND tenant_id = $2"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            row = await owned_conn.fetchrow(sql, secret_id, tid)
    else:
        row = await conn.fetchrow(sql, secret_id, tid)
    if not row:
        return None
    return _decrypt_secret_value(
        row["encrypted_value"], tid, row["secret_type"], row["key_name"],
    )


async def get_secret_by_name(
    key_name: str, secret_type: str | None = None, conn=None,
) -> str | None:
    """Retrieve plaintext by key_name within current tenant."""
    tid = require_current_tenant()
    if secret_type:
        sql = (
            "SELECT encrypted_value, secret_type, key_name FROM tenant_secrets "
            "WHERE key_name = $1 AND tenant_id = $2 AND secret_type = $3 "
            "LIMIT 1"
        )
        args: tuple = (key_name, tid, secret_type)
    else:
        sql = (
            "SELECT encrypted_value, secret_type, key_name FROM tenant_secrets "
            "WHERE key_name = $1 AND tenant_id = $2 "
            "LIMIT 1"
        )
        args = (key_name, tid)
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            row = await owned_conn.fetchrow(sql, *args)
    else:
        row = await conn.fetchrow(sql, *args)
    if not row:
        return None
    return _decrypt_secret_value(
        row["encrypted_value"], tid, row["secret_type"], row["key_name"],
    )


async def _upsert_secret_impl(
    conn, tid: str, key_name: str, plaintext: str,
    secret_type: str, metadata: dict[str, Any] | None,
) -> str:
    enc = _encrypt_secret_value(tid, secret_type, key_name, plaintext)
    meta_json = json.dumps(metadata or {})
    candidate_id = f"sec-{uuid.uuid4().hex[:12]}"
    # Atomic upsert. On INSERT the candidate_id wins; on CONFLICT the
    # pre-existing row's ``id`` is preserved (DO UPDATE SET never
    # touches id) and ``RETURNING id`` reads back the real id.
    # updated_at is explicitly refreshed on conflict since PG column
    # DEFAULTs only fire on INSERT.
    row = await conn.fetchrow(
        "INSERT INTO tenant_secrets "
        "(id, tenant_id, secret_type, key_name, encrypted_value, metadata) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (tenant_id, secret_type, key_name) DO UPDATE SET "
        "  encrypted_value = EXCLUDED.encrypted_value, "
        "  metadata = EXCLUDED.metadata, "
        "  updated_at = to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS') "
        "RETURNING id, (xmax = 0) AS inserted",
        candidate_id, tid, secret_type, key_name, enc, meta_json,
    )
    actual_id = row["id"]
    if row["inserted"]:
        logger.info(
            "Created secret %s/%s for tenant %s", secret_type, key_name, tid,
        )
    else:
        logger.info(
            "Updated secret %s/%s for tenant %s", secret_type, key_name, tid,
        )
    return actual_id


async def upsert_secret(
    key_name: str,
    plaintext: str,
    secret_type: str = "custom",
    metadata: dict[str, Any] | None = None,
    conn=None,
) -> str:
    """Create or update a secret. Returns the secret id.

    SP-4.6 (2026-04-21): atomic via ``ON CONFLICT DO UPDATE``. Two
    concurrent upserts on the same ``(tenant, secret_type, key_name)``
    serialise on PG's UNIQUE-enforcement locking; the loser finds the
    winner's row via the conflict path and updates it, instead of
    raising an integrity error as the old SELECT-then-INSERT would
    have done under pool concurrency.
    """
    tid = require_current_tenant()
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _upsert_secret_impl(
                owned_conn, tid, key_name, plaintext, secret_type, metadata,
            )
    return await _upsert_secret_impl(
        conn, tid, key_name, plaintext, secret_type, metadata,
    )


async def delete_secret(secret_id: str, conn=None) -> bool:
    """Delete a secret. Returns True if deleted."""
    tid = require_current_tenant()
    sql = "DELETE FROM tenant_secrets WHERE id = $1 AND tenant_id = $2"
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            status = await owned_conn.execute(sql, secret_id, tid)
    else:
        status = await conn.execute(sql, secret_id, tid)
    try:
        return int(status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False
