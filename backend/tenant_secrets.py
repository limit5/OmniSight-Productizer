"""I4 — Tenant-scoped secrets management.

CRUD API for storing encrypted credentials per tenant. Wraps
``secret_store.encrypt/decrypt`` and persists ciphertext in the
``tenant_secrets`` SQLite table.  Secret types:

  - ``git_credential``   — per-repo tokens (GitHub, GitLab, Gerrit…)
  - ``provider_key``     — LLM / SaaS API keys
  - ``cloudflare_token`` — Cloudflare API tokens
  - ``webhook_secret``   — inbound webhook HMAC secrets
  - ``custom``           — anything else

All reads/writes are scoped to the tenant in ``db_context``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import aiosqlite

from backend.db_context import require_current_tenant
from backend.secret_store import decrypt, encrypt, fingerprint

logger = logging.getLogger(__name__)


def _conn() -> aiosqlite.Connection:
    from backend.db import _conn as _db_conn
    return _db_conn()


async def list_secrets(
    secret_type: str | None = None,
) -> list[dict[str, Any]]:
    """List secrets for current tenant. Values are fingerprinted, never plaintext."""
    tid = require_current_tenant()
    sql = (
        "SELECT id, tenant_id, secret_type, key_name, encrypted_value, "
        "metadata, created_at, updated_at "
        "FROM tenant_secrets WHERE tenant_id = ?"
    )
    params: list[Any] = [tid]
    if secret_type:
        sql += " AND secret_type = ?"
        params.append(secret_type)
    sql += " ORDER BY secret_type, key_name"

    async with _conn().execute(sql, params) as cur:
        rows = await cur.fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        try:
            plain = decrypt(r["encrypted_value"])
            fp = fingerprint(plain)
        except Exception:
            fp = "****"
        meta = {}
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
        })
    return results


async def get_secret_value(secret_id: str) -> str | None:
    """Retrieve the plaintext value of a secret (for internal use only)."""
    tid = require_current_tenant()
    sql = (
        "SELECT encrypted_value FROM tenant_secrets "
        "WHERE id = ? AND tenant_id = ?"
    )
    async with _conn().execute(sql, (secret_id, tid)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return decrypt(row["encrypted_value"])


async def get_secret_by_name(
    key_name: str, secret_type: str | None = None,
) -> str | None:
    """Retrieve plaintext by key_name within current tenant."""
    tid = require_current_tenant()
    sql = (
        "SELECT encrypted_value FROM tenant_secrets "
        "WHERE key_name = ? AND tenant_id = ?"
    )
    params: list[Any] = [key_name, tid]
    if secret_type:
        sql += " AND secret_type = ?"
        params.append(secret_type)
    sql += " LIMIT 1"
    async with _conn().execute(sql, params) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return decrypt(row["encrypted_value"])


async def upsert_secret(
    key_name: str,
    plaintext: str,
    secret_type: str = "custom",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Create or update a secret. Returns the secret id."""
    tid = require_current_tenant()
    enc = encrypt(plaintext)
    meta_json = json.dumps(metadata or {})

    async with _conn().execute(
        "SELECT id FROM tenant_secrets WHERE key_name = ? AND tenant_id = ? AND secret_type = ?",
        (key_name, tid, secret_type),
    ) as cur:
        existing = await cur.fetchone()

    if existing:
        sid = existing["id"]
        await _conn().execute(
            "UPDATE tenant_secrets SET encrypted_value = ?, metadata = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (enc, meta_json, sid),
        )
        await _conn().commit()
        logger.info("Updated secret %s/%s for tenant %s", secret_type, key_name, tid)
        return sid

    sid = f"sec-{uuid.uuid4().hex[:12]}"
    await _conn().execute(
        "INSERT INTO tenant_secrets (id, tenant_id, secret_type, key_name, "
        "encrypted_value, metadata) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, tid, secret_type, key_name, enc, meta_json),
    )
    await _conn().commit()
    logger.info("Created secret %s/%s for tenant %s", secret_type, key_name, tid)
    return sid


async def delete_secret(secret_id: str) -> bool:
    """Delete a secret. Returns True if deleted."""
    tid = require_current_tenant()
    cur = await _conn().execute(
        "DELETE FROM tenant_secrets WHERE id = ? AND tenant_id = ?",
        (secret_id, tid),
    )
    await _conn().commit()
    return cur.rowcount > 0
