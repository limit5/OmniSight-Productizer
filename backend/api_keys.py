"""K6 — Per-key bearer tokens with scopes and audit trail.

Replaces the single OMNISIGHT_DECISION_BEARER env var with an api_keys
table. Each key is identified by a prefix (first 8 chars) for log
readability and validated via SHA-256 hash comparison.

Key format: ``omni_<40-char-urlsafe-random>`` (total ~46 chars).
Stored as ``sha256(<full_key>)``; only the 8-char prefix is kept in
cleartext for log display.

Phase-3-Runtime-v2 SP-5.7a (2026-04-21): ported from the legacy
compat DB wrapper to native asyncpg pool. 9 public functions move to
``get_pool().acquire() + $N placeholders``. Rowcount-based returns
(``revoke_key`` / ``enable_key`` / ``delete_key`` / ``update_scopes``)
swap to ``UPDATE ... RETURNING id`` so we can tell match vs miss
(asyncpg's Pool.execute doesn't expose ``rowcount`` the way the
compat wrapper did).

Module-global audit (SOP Step 1): only state is ``KEY_PREFIX_LEN``
constant + ``ApiKey`` dataclass — identical across workers, answer (1).
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
from backend.db_pool import get_pool

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
        endpoint_scope = _endpoint_to_oauth_scope(endpoint)
        for scope in self.scopes:
            if endpoint.startswith(scope):
                return True
            if endpoint_scope and _oauth_scope_allows(scope, endpoint_scope):
                return True
        return False


def _endpoint_to_oauth_scope(endpoint: str) -> str:
    """Map project-local paths to OAuth-style API key scopes.

    Module-global audit: this helper is pure and derives the same scope
    string in every worker; no cache or mutable process state is used.
    """
    path = endpoint or ""
    if path == "/.well-known/agent.json":
        return "a2a:discover:agent-card"
    if path.startswith("/a2a/invoke/"):
        agent_name = path.removeprefix("/a2a/invoke/").split("/", 1)[0].split("?", 1)[0]
        if agent_name:
            return f"a2a:invoke:{agent_name}"
    return ""


def _oauth_scope_allows(grant: str, required: str) -> bool:
    if not grant or not required:
        return False
    if grant == required:
        return True
    if grant.endswith(":*"):
        return required.startswith(grant[:-1])
    return False


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_LIST_COLS = (
    "id, name, key_prefix, scopes, created_by, last_used_ip, "
    "last_used_at, enabled, created_at"
)


def _row_to_key(r, *, override_ip: str | None = None,
                override_last_used: float | None = None) -> ApiKey:
    return ApiKey(
        id=r["id"], name=r["name"], key_prefix=r["key_prefix"],
        scopes=json.loads(r["scopes"] or '["*"]'),
        created_by=r["created_by"],
        last_used_ip=override_ip if override_ip is not None else r["last_used_ip"],
        last_used_at=(
            override_last_used if override_last_used is not None
            else r["last_used_at"]
        ),
        enabled=bool(r["enabled"]),
        created_at=r["created_at"] or "",
    )


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

    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys "
            "(id, name, key_hash, key_prefix, scopes, created_by, enabled) "
            "VALUES ($1, $2, $3, $4, $5, $6, 1)",
            key_id, name, hashed, prefix, scope_json, created_by,
        )
    key = ApiKey(id=key_id, name=name, key_prefix=prefix, scopes=scope_list,
                 created_by=created_by, enabled=True)
    logger.info("[API-KEY] Created key %s (%s) by %s", key_id, name, created_by)
    return key, raw


async def rotate_key(key_id: str) -> tuple[ApiKey | None, str]:
    """Generate a new secret for an existing key. Returns (ApiKey, new_raw)
    or (None, '') if key not found."""
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, name, scopes, created_by, enabled, created_at "
            "FROM api_keys WHERE id = $1",
            key_id,
        )
        if not r:
            return None, ""
        raw = "omni_" + secrets.token_urlsafe(30)
        hashed = _hash_key(raw)
        prefix = raw[:KEY_PREFIX_LEN]
        await conn.execute(
            "UPDATE api_keys SET key_hash = $1, key_prefix = $2 WHERE id = $3",
            hashed, prefix, key_id,
        )
    scopes = json.loads(r["scopes"] or '["*"]')
    key = ApiKey(id=r["id"], name=r["name"], key_prefix=prefix, scopes=scopes,
                 created_by=r["created_by"], enabled=bool(r["enabled"]),
                 created_at=r["created_at"] or "")
    logger.info("[API-KEY] Rotated key %s (%s)", key_id, r["name"])
    return key, raw


async def revoke_key(key_id: str) -> bool:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE api_keys SET enabled = 0 WHERE id = $1 RETURNING id",
            key_id,
        )
    revoked = row is not None
    if revoked:
        logger.info("[API-KEY] Revoked key %s", key_id)
    return revoked


async def enable_key(key_id: str) -> bool:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE api_keys SET enabled = 1 WHERE id = $1 RETURNING id",
            key_id,
        )
    return row is not None


async def delete_key(key_id: str) -> bool:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM api_keys WHERE id = $1 RETURNING id",
            key_id,
        )
    deleted = row is not None
    if deleted:
        logger.info("[API-KEY] Deleted key %s", key_id)
    return deleted


async def list_keys() -> list[ApiKey]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_LIST_COLS} FROM api_keys ORDER BY created_at DESC"
        )
    return [_row_to_key(r) for r in rows]


async def get_key(key_id: str) -> ApiKey | None:
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            f"SELECT {_LIST_COLS} FROM api_keys WHERE id = $1",
            key_id,
        )
    return _row_to_key(r) if r else None


async def validate_bearer(raw_token: str, ip: str = "") -> ApiKey | None:
    """Validate a raw bearer token against all enabled keys.
    Updates last_used_ip and last_used_at on match. Returns None if
    no matching enabled key found."""
    hashed = _hash_key(raw_token)
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, name, key_prefix, scopes, created_by, enabled, created_at "
            "FROM api_keys WHERE key_hash = $1 AND enabled = 1",
            hashed,
        )
        if not r:
            return None
        now = time.time()
        await conn.execute(
            "UPDATE api_keys SET last_used_ip = $1, last_used_at = $2 "
            "WHERE id = $3",
            ip or None, now, r["id"],
        )
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

    Task #106 (2026-04-21): deterministic id
    ``ak-legacy-<sha256(legacy_secret)[:12]>`` + ``INSERT ... ON
    CONFLICT (id) DO NOTHING`` collapses concurrent migrations across
    uvicorn workers to a single row. Pre-fix each worker generated
    its own uuid → N rows per secret. Smoke-verified post-fix.

    SP-5.7a: ported off compat (with #106's ON-CONFLICT semantic
    preserved). The RETURNING tells us whether we inserted (row) or
    hit the conflict (None) — on conflict we skip the log line so
    only one worker's startup surfaces the "migrated" warning.
    """
    legacy = (os.environ.get("OMNISIGHT_DECISION_BEARER") or "").strip()
    if not legacy:
        return None
    hashed = _hash_key(legacy)
    key_id = f"ak-legacy-{hashed[:12]}"
    prefix = legacy[:KEY_PREFIX_LEN] if len(legacy) >= KEY_PREFIX_LEN else legacy
    async with get_pool().acquire() as conn:
        inserted = await conn.fetchrow(
            "INSERT INTO api_keys "
            "(id, name, key_hash, key_prefix, scopes, created_by, enabled) "
            "VALUES ($1, 'legacy-bearer', $2, $3, '[\"*\"]', "
            "'system/migration', 1) "
            "ON CONFLICT (id) DO NOTHING "
            "RETURNING id",
            key_id, hashed, prefix,
        )
    if inserted is None:
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
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE api_keys SET scopes = $1 WHERE id = $2 RETURNING id",
            json.dumps(scopes), key_id,
        )
    return row is not None
