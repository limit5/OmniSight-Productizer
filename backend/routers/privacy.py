"""SC.10.2 / SC.10.3 -- DSAR access and erasure endpoints.

POST /privacy/access
    Return the current user's account-owned data and write a completed
    ``dsar_requests`` row with a count summary.
POST /privacy/erasure
    Erase the current user's account-owned data, redact the retained
    ``users`` row, and write a completed ``dsar_requests`` receipt.

These routes are intentionally narrow: SC.10.4 owns portable JSON
exports, and SC.10.5 owns email/SLA background work.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from fastapi import APIRouter, Depends

from backend import auth

router = APIRouter(prefix="/privacy", tags=["privacy"])

_DSAR_SLA_SECONDS = 30 * 24 * 60 * 60


def _request_id() -> str:
    return f"dsar-access-{uuid.uuid4().hex}"


def _erasure_request_id() -> str:
    return f"dsar-erasure-{uuid.uuid4().hex}"


def _row(row) -> dict:
    return dict(row) if row is not None else {}


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


async def _fetch_all_user_data(conn, user: auth.User) -> dict:
    """Return a whitelist-shaped access payload for ``user``.

    Module-global state audit: the route keeps no mutable in-process
    cache; every worker derives the response from PG rows scoped by
    ``user.id`` / ``user.tenant_id``.
    """
    profile = await conn.fetchrow(
        "SELECT id, email, name, role, enabled, must_change_password, "
        "created_at, last_login_at, tenant_id, auth_methods "
        "FROM users WHERE id = $1",
        user.id,
    )
    memberships = await conn.fetch(
        "SELECT user_id, tenant_id, role, status, created_at, last_active_at "
        "FROM user_tenant_memberships WHERE user_id = $1 ORDER BY tenant_id",
        user.id,
    )
    preferences = await conn.fetch(
        "SELECT pref_key, value, updated_at, tenant_id, project_id "
        "FROM user_preferences WHERE user_id = $1 ORDER BY pref_key",
        user.id,
    )
    drafts = await conn.fetch(
        "SELECT slot_key, content, updated_at, tenant_id "
        "FROM user_drafts WHERE user_id = $1 ORDER BY slot_key",
        user.id,
    )
    chat_messages = await conn.fetch(
        "SELECT id, session_id, role, content, timestamp, tenant_id "
        "FROM chat_messages WHERE user_id = $1 ORDER BY timestamp, id",
        user.id,
    )
    chat_sessions = await conn.fetch(
        "SELECT session_id, tenant_id, metadata, created_at, updated_at "
        "FROM chat_sessions WHERE user_id = $1 ORDER BY updated_at DESC",
        user.id,
    )
    sessions = await conn.fetch(
        "SELECT created_at, expires_at, last_seen_at, ip, user_agent, "
        "ua_hash, metadata, mfa_verified, rotated_from "
        "FROM sessions WHERE user_id = $1 ORDER BY created_at DESC",
        user.id,
    )
    mfa_methods = await conn.fetch(
        "SELECT id, method, name, verified, created_at, last_used "
        "FROM user_mfa WHERE user_id = $1 ORDER BY created_at, id",
        user.id,
    )
    mfa_backup_codes = await conn.fetch(
        "SELECT id, used, created_at, used_at "
        "FROM mfa_backup_codes WHERE user_id = $1 ORDER BY created_at, id",
        user.id,
    )
    password_history = await conn.fetch(
        "SELECT id, created_at "
        "FROM password_history WHERE user_id = $1 ORDER BY created_at, id",
        user.id,
    )
    projects_created = await conn.fetch(
        "SELECT id, tenant_id, product_line, name, slug, parent_id, "
        "plan_override, disk_budget_bytes, llm_budget_tokens, created_at, "
        "archived_at FROM projects WHERE created_by = $1 ORDER BY created_at, id",
        user.id,
    )
    api_keys_created = await conn.fetch(
        "SELECT id, name, key_prefix, scopes, created_by, last_used_ip, "
        "last_used_at, enabled, created_at "
        "FROM api_keys WHERE created_by = $1 ORDER BY created_at, id",
        user.id,
    )
    oauth_connections = await conn.fetch(
        "SELECT provider, expires_at, scope, key_version, created_at, "
        "updated_at, version "
        "FROM oauth_tokens WHERE user_id = $1 ORDER BY provider",
        user.id,
    )
    dsar_requests = await conn.fetch(
        "SELECT id, tenant_id, user_id, request_type, status, requested_at, "
        "due_at, completed_at, payload_json, result_json, error, version "
        "FROM dsar_requests WHERE user_id = $1 ORDER BY requested_at DESC, id",
        user.id,
    )

    return {
        "profile": _row(profile),
        "tenant_memberships": _rows(memberships),
        "preferences": _rows(preferences),
        "drafts": _rows(drafts),
        "chat_messages": _rows(chat_messages),
        "chat_sessions": _rows(chat_sessions),
        "sessions": _rows(sessions),
        "mfa_methods": _rows(mfa_methods),
        "mfa_backup_codes": _rows(mfa_backup_codes),
        "password_history": _rows(password_history),
        "projects_created": _rows(projects_created),
        "api_keys_created": _rows(api_keys_created),
        "oauth_connections": _rows(oauth_connections),
        "dsar_requests": _rows(dsar_requests),
    }


def _counts(data: dict) -> dict:
    counts: dict[str, int] = {}
    for key, value in data.items():
        counts[key] = len(value) if isinstance(value, list) else int(bool(value))
    return counts


_EXECUTE_COUNT_RE = re.compile(r"\b(\d+)\s*$")


def _execute_count(status: str) -> int:
    match = _EXECUTE_COUNT_RE.search(status or "")
    return int(match.group(1)) if match else 0


def _redacted_email(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
    return f"redacted+{digest}@privacy.invalid"


def _redacted_name(user_id: str) -> str:
    digest = hashlib.sha256(f"{user_id}:name".encode("utf-8")).hexdigest()[:24]
    return f"redacted:{digest}"


_ERASURE_DELETE_STATEMENTS: tuple[tuple[str, str], ...] = (
    ("tenant_memberships",
        "DELETE FROM user_tenant_memberships WHERE user_id = $1"),
    ("preferences",
        "DELETE FROM user_preferences WHERE user_id = $1"),
    ("drafts",
        "DELETE FROM user_drafts WHERE user_id = $1"),
    ("chat_messages",
        "DELETE FROM chat_messages WHERE user_id = $1"),
    ("chat_sessions",
        "DELETE FROM chat_sessions WHERE user_id = $1"),
    ("sessions",
        "DELETE FROM sessions WHERE user_id = $1"),
    ("mfa_methods",
        "DELETE FROM user_mfa WHERE user_id = $1"),
    ("mfa_backup_codes",
        "DELETE FROM mfa_backup_codes WHERE user_id = $1"),
    ("password_history",
        "DELETE FROM password_history WHERE user_id = $1"),
    ("api_keys_created",
        "DELETE FROM api_keys WHERE created_by = $1"),
    ("oauth_connections",
        "DELETE FROM oauth_tokens WHERE user_id = $1"),
)


async def _erase_user_data(conn, user: auth.User) -> dict:
    """Erase mutable user-owned records and redact the retained user row.

    Module-global state audit: the immutable statement tuple is identical
    in every worker; all mutable erasure state is coordinated by PG in one
    transaction scoped to ``user.id``.
    """
    erased: dict[str, int] = {}
    for category, sql in _ERASURE_DELETE_STATEMENTS:
        erased[category] = _execute_count(await conn.execute(sql, user.id))

    erased["projects_created"] = _execute_count(await conn.execute(
        "UPDATE projects SET created_by = NULL WHERE created_by = $1",
        user.id,
    ))
    erased["profile"] = _execute_count(await conn.execute(
        "UPDATE users SET email = $2, name = $3, password_hash = '', "
        "oidc_provider = '', oidc_subject = '', enabled = 0, "
        "must_change_password = 0, failed_login_count = 0, "
        "locked_until = NULL, last_login_at = NULL, auth_methods = '[]' "
        "WHERE id = $1",
        user.id,
        _redacted_email(user.id),
        _redacted_name(user.id),
    ))
    return erased


@router.post("/access")
async def create_access_request(
    user: auth.User = Depends(auth.current_user),
) -> dict:
    from backend.db_pool import get_pool

    request_id = _request_id()
    requested_at = time.time()
    completed_at = requested_at
    due_at = requested_at + _DSAR_SLA_SECONDS
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            data = await _fetch_all_user_data(conn, user)
            result = {"category_counts": _counts(data)}
            await conn.execute(
                "INSERT INTO dsar_requests "
                "(id, tenant_id, user_id, request_type, status, requested_at, "
                "due_at, completed_at, payload_json, result_json) "
                "VALUES ($1, $2, $3, 'access', 'completed', $4, $5, $6, "
                "$7::jsonb, $8::jsonb)",
                request_id,
                user.tenant_id,
                user.id,
                requested_at,
                due_at,
                completed_at,
                json.dumps({"source": "privacy_access_endpoint"}),
                json.dumps(result),
            )

    return {
        "request": {
            "id": request_id,
            "type": "access",
            "status": "completed",
            "requested_at": requested_at,
            "due_at": due_at,
            "completed_at": completed_at,
        },
        "data": data,
        "result": result,
    }


@router.post("/erasure")
async def create_erasure_request(
    user: auth.User = Depends(auth.current_user),
) -> dict:
    from backend.db_pool import get_pool

    request_id = _erasure_request_id()
    requested_at = time.time()
    completed_at = requested_at
    due_at = requested_at + _DSAR_SLA_SECONDS
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            result = {"erased_counts": await _erase_user_data(conn, user)}
            await conn.execute(
                "INSERT INTO dsar_requests "
                "(id, tenant_id, user_id, request_type, status, requested_at, "
                "due_at, completed_at, payload_json, result_json) "
                "VALUES ($1, $2, $3, 'erasure', 'completed', $4, $5, $6, "
                "$7::jsonb, $8::jsonb)",
                request_id,
                user.tenant_id,
                user.id,
                requested_at,
                due_at,
                completed_at,
                json.dumps({"source": "privacy_erasure_endpoint"}),
                json.dumps(result),
            )

    return {
        "request": {
            "id": request_id,
            "type": "erasure",
            "status": "completed",
            "requested_at": requested_at,
            "due_at": due_at,
            "completed_at": completed_at,
        },
        "result": result,
    }
