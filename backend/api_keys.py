"""K6 — Per-key bearer tokens with scopes and audit trail.

Replaces the single OMNISIGHT_DECISION_BEARER env var with an api_keys
table. Each key is identified by a prefix (first 8 chars) for log
readability and validated via SHA-256 hash comparison.

Key format: ``omni_<40-char-urlsafe-random>`` (total ~46 chars).
Stored as ``sha256(<full_key>)``; only the 8-char prefix is kept in
cleartext for log display.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

KEY_PREFIX_LEN = 8


@dataclass
class ApiKey:
    id: str
    name: str
    key_prefix: str
    scopes: list[str] = field(default_factory=lambda: ["*"])
    created_by: str = ""
    last_used_ip: str | None = None
    last_used_at: float | None = None
    enabled: bool = True
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "scopes": self.scopes,
            "created_by": self.created_by,
            "last_used_ip": self.last_used_ip,
            "last_used_at": self.last_used_at,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }

    def scope_allows(self, endpoint: str) -> bool:
        if "*" in self.scopes:
            return True
        for scope in self.scopes:
            if endpoint.startswith(scope):
                return True
        return False


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _conn():
    from backend import db
    return db._conn()


async def create_key(name: str, scopes: list[str] | None = None,
                     created_by: str = "") -> tuple[ApiKey, str]:
    """Create a new API key. Returns (ApiKey, raw_secret).
    The raw secret is shown exactly once — it is NOT stored."""
    raw = "omni_" + secrets.token_urlsafe(30)
    key_id = f"ak-{uuid.uuid4().hex[:10]}"
    hashed = _hash_key(raw)
    prefix = raw[:KEY_PREFIX_LEN]
    scope_list = scopes or ["*"]
    scope_json = json.dumps(scope_list)

    conn = await _conn()
    await conn.execute(
        "INSERT INTO api_keys (id, name, key_hash, key_prefix, scopes, created_by, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (key_id, name, hashed, prefix, scope_json, created_by),
    )
    await conn.commit()
    key = ApiKey(id=key_id, name=name, key_prefix=prefix, scopes=scope_list,
                 created_by=created_by, enabled=True)
    logger.info("[API-KEY] Created key %s (%s) by %s", key_id, name, created_by)
    return key, raw


async def rotate_key(key_id: str) -> tuple[ApiKey | None, str]:
    """Generate a new secret for an existing key. Returns (ApiKey, new_raw)
    or (None, '') if key not found."""
    conn = await _conn()
    async with conn.execute(
        "SELECT id, name, scopes, created_by, enabled, created_at FROM api_keys WHERE id=?",
        (key_id,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None, ""
    raw = "omni_" + secrets.token_urlsafe(30)
    hashed = _hash_key(raw)
    prefix = raw[:KEY_PREFIX_LEN]
    await conn.execute(
        "UPDATE api_keys SET key_hash=?, key_prefix=? WHERE id=?",
        (hashed, prefix, key_id),
    )
    await conn.commit()
    scopes = json.loads(r["scopes"] or '["*"]')
    key = ApiKey(id=r["id"], name=r["name"], key_prefix=prefix, scopes=scopes,
                 created_by=r["created_by"], enabled=bool(r["enabled"]),
                 created_at=r["created_at"] or "")
    logger.info("[API-KEY] Rotated key %s (%s)", key_id, r["name"])
    return key, raw


async def revoke_key(key_id: str) -> bool:
    conn = await _conn()
    cur = await conn.execute(
        "UPDATE api_keys SET enabled=0 WHERE id=?", (key_id,),
    )
    await conn.commit()
    revoked = (cur.rowcount or 0) > 0
    if revoked:
        logger.info("[API-KEY] Revoked key %s", key_id)
    return revoked


async def enable_key(key_id: str) -> bool:
    conn = await _conn()
    cur = await conn.execute(
        "UPDATE api_keys SET enabled=1 WHERE id=?", (key_id,),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def delete_key(key_id: str) -> bool:
    conn = await _conn()
    cur = await conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    await conn.commit()
    deleted = (cur.rowcount or 0) > 0
    if deleted:
        logger.info("[API-KEY] Deleted key %s", key_id)
    return deleted


async def list_keys() -> list[ApiKey]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, name, key_prefix, scopes, created_by, last_used_ip, "
        "last_used_at, enabled, created_at FROM api_keys ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return [
        ApiKey(
            id=r["id"], name=r["name"], key_prefix=r["key_prefix"],
            scopes=json.loads(r["scopes"] or '["*"]'),
            created_by=r["created_by"],
            last_used_ip=r["last_used_ip"],
            last_used_at=r["last_used_at"],
            enabled=bool(r["enabled"]),
            created_at=r["created_at"] or "",
        )
        for r in rows
    ]


async def get_key(key_id: str) -> ApiKey | None:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, name, key_prefix, scopes, created_by, last_used_ip, "
        "last_used_at, enabled, created_at FROM api_keys WHERE id=?",
        (key_id,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None
    return ApiKey(
        id=r["id"], name=r["name"], key_prefix=r["key_prefix"],
        scopes=json.loads(r["scopes"] or '["*"]'),
        created_by=r["created_by"],
        last_used_ip=r["last_used_ip"],
        last_used_at=r["last_used_at"],
        enabled=bool(r["enabled"]),
        created_at=r["created_at"] or "",
    )


async def validate_bearer(raw_token: str, ip: str = "") -> ApiKey | None:
    """Validate a raw bearer token against all enabled keys.
    Updates last_used_ip and last_used_at on match. Returns None if
    no matching enabled key found."""
    hashed = _hash_key(raw_token)
    conn = await _conn()
    async with conn.execute(
        "SELECT id, name, key_prefix, scopes, created_by, enabled, created_at "
        "FROM api_keys WHERE key_hash=? AND enabled=1",
        (hashed,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None
    now = time.time()
    await conn.execute(
        "UPDATE api_keys SET last_used_ip=?, last_used_at=? WHERE id=?",
        (ip or None, now, r["id"]),
    )
    await conn.commit()
    return ApiKey(
        id=r["id"], name=r["name"], key_prefix=r["key_prefix"],
        scopes=json.loads(r["scopes"] or '["*"]'),
        created_by=r["created_by"],
        last_used_ip=ip or None,
        last_used_at=now,
        enabled=True,
        created_at=r["created_at"] or "",
    )


async def migrate_legacy_bearer() -> ApiKey | None:
    """Detect the old OMNISIGHT_DECISION_BEARER env var and migrate it
    to a hashed api_keys row named 'legacy-bearer'. Warns the operator
    to rotate to a proper per-key setup.

    Task #106 (2026-04-21): the row id is now DETERMINISTIC —
    ``ak-legacy-<sha256(legacy_secret)[:12]>``. Previously the id was
    a fresh ``uuid.uuid4().hex[:6]`` per call, so each uvicorn worker
    running this on startup independently produced a different id and
    created its own row (no UNIQUE collision → N rows for N workers).
    The SP-4.6 close-out smoke with ``OMNISIGHT_WORKERS=2`` produced
    two ``ak-legacy-*`` rows (``66956d`` + ``7fb016``), which was the
    concrete finding that triggered this task.

    The deterministic id + ``INSERT ... ON CONFLICT (id) DO NOTHING``
    collapses concurrent migrations across workers to a single row.
    Same bearer secret → same hash → same id → same row. Rotating
    the bearer env produces a new id automatically (old rows can be
    cleaned up by operators out-of-band).

    Note: ``_conn()`` + ``?`` stays because api_keys.py is still on
    the compat wrapper (SP-5.7 will port the whole module). The
    check-then-insert race is the operational bug — the deterministic
    id + ON CONFLICT fixes it without touching the pool migration.
    """
    legacy = (os.environ.get("OMNISIGHT_DECISION_BEARER") or "").strip()
    if not legacy:
        return None
    hashed = _hash_key(legacy)
    key_id = f"ak-legacy-{hashed[:12]}"
    prefix = legacy[:KEY_PREFIX_LEN] if len(legacy) >= KEY_PREFIX_LEN else legacy
    conn = await _conn()
    # The RETURNING clause tells us whether we actually inserted or
    # hit the conflict — the compat wrapper passes this through from
    # asyncpg on PG (and SQLite ≥ 3.35 supports it too). On conflict
    # no row is returned so fetchone() is None.
    async with conn.execute(
        "INSERT INTO api_keys (id, name, key_hash, key_prefix, scopes, created_by, enabled) "
        "VALUES (?, 'legacy-bearer', ?, ?, '[\"*\"]', 'system/migration', 1) "
        "ON CONFLICT (id) DO NOTHING "
        "RETURNING id",
        (key_id, hashed, prefix),
    ) as cur:
        inserted = await cur.fetchone()
    await conn.commit()
    if inserted is None:
        # Another worker landed this exact row first; do not re-log
        # the "migrated" warning (else every worker emits it).
        return None
    key = ApiKey(id=key_id, name="legacy-bearer", key_prefix=prefix,
                 scopes=["*"], created_by="system/migration", enabled=True)
    logger.warning(
        "[API-KEY] Migrated OMNISIGHT_DECISION_BEARER to api_keys row '%s'. "
        "Please create per-service keys via Admin UI and remove the env var.",
        key_id,
    )
    return key


async def update_scopes(key_id: str, scopes: list[str]) -> bool:
    conn = await _conn()
    cur = await conn.execute(
        "UPDATE api_keys SET scopes=? WHERE id=?",
        (json.dumps(scopes), key_id),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0
