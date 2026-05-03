"""SC.10.2 -- DSAR access endpoint.

POST /privacy/access
    Return the current user's account-owned data and write a completed
    ``dsar_requests`` row with a count summary.

This route is intentionally narrow: SC.10.3 owns erasure, SC.10.4 owns
portable JSON exports, and SC.10.5 owns email/SLA background work.
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Depends

from backend import auth

router = APIRouter(prefix="/privacy", tags=["privacy"])

_DSAR_SLA_SECONDS = 30 * 24 * 60 * 60


def _request_id() -> str:
    return f"dsar-access-{uuid.uuid4().hex}"


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
