"""Phase 5-4 (#multi-account-forge) — git_accounts CRUD REST endpoints.

Surface
───────
* ``GET    /git-accounts``              — list (masked, tenant-scoped)
* ``POST   /git-accounts``              — create
* ``GET    /git-accounts/{id}``         — detail (masked)
* ``PATCH  /git-accounts/{id}``         — partial update / rotate token
* ``DELETE /git-accounts/{id}``         — delete (auto-elect new default)
* ``POST   /git-accounts/{id}/test``    — live probe the token
* ``POST   /git-accounts/resolve``      — debug-pick account for ``url``

All mutations write to :mod:`backend.audit` (the service layer in
``backend.git_accounts`` takes care of that — this router stays
thin). All reads return token / ssh_key / webhook_secret only as
:func:`backend.secret_store.fingerprint` strings (``…abc4``) —
plaintext secrets never leave the server.

Tenant RLS
──────────
The ``require_admin`` dependency resolves the authenticated user;
we forward their ``tenant_id`` to :func:`db_context.set_tenant_id`
so every :mod:`backend.git_accounts` call path filters through
``tenant_where_pg``. A caller with admin on tenant A cannot list /
update / delete any row in tenant B (verified by
``test_git_accounts_crud.py::test_tenant_isolation``).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import git_accounts as _ga
from backend import git_credentials as _gc
from backend.db_context import set_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/git-accounts", tags=["git-accounts"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schemas
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class GitAccountCreate(BaseModel):
    platform: str = Field(..., pattern=r"^(github|gitlab|gerrit|jira)$")
    instance_url: str = Field("", max_length=2048)
    label: str = Field("", max_length=256)
    username: str = Field("", max_length=256)
    token: str = Field("", max_length=4096)
    ssh_key: str = Field("", max_length=32768)
    ssh_host: str = Field("", max_length=256)
    ssh_port: int = Field(0, ge=0, le=65535)
    project: str = Field("", max_length=256)
    webhook_secret: str = Field("", max_length=1024)
    url_patterns: list[str] = Field(default_factory=list)
    auth_type: str = Field("pat", pattern=r"^(pat|oauth|ssh)$")
    is_default: bool = False
    enabled: bool = True
    metadata: dict = Field(default_factory=dict)


class GitAccountUpdate(BaseModel):
    # All fields optional — partial PATCH. ``None`` means "don't
    # touch"; explicit empty string on a secret clears it.
    label: str | None = None
    username: str | None = None
    instance_url: str | None = None
    ssh_host: str | None = None
    ssh_port: int | None = None
    project: str | None = None
    url_patterns: list[str] | None = None
    auth_type: str | None = Field(None, pattern=r"^(pat|oauth|ssh)$")
    is_default: bool | None = None
    enabled: bool | None = None
    metadata: dict | None = None
    # Secret rotations — encrypted server-side before hitting DB.
    token: str | None = None
    ssh_key: str | None = None
    webhook_secret: str | None = None


def _ensure_tenant(user: _au.User) -> None:
    """Pin the request-scoped tenant context from the authenticated user."""
    set_tenant_id(user.tenant_id or "t-default")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Read endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("")
async def list_git_accounts(
    platform: str | None = Query(
        None, pattern=r"^(github|gitlab|gerrit|jira)$"
    ),
    enabled_only: bool = Query(False),
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    items = await _ga.list_accounts(
        platform=platform, enabled_only=enabled_only,
    )
    return {"items": items, "count": len(items)}


@router.get("/{account_id}")
async def get_git_account(
    account_id: str,
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    row = await _ga.get_account(account_id)
    if row is None:
        raise HTTPException(404, "git_account not found")
    return row


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Write endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("", status_code=201)
async def create_git_account(
    body: GitAccountCreate,
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    try:
        out = await _ga.create_account(
            platform=body.platform,
            instance_url=body.instance_url,
            label=body.label,
            username=body.username,
            token=body.token,
            ssh_key=body.ssh_key,
            ssh_host=body.ssh_host,
            ssh_port=body.ssh_port,
            project=body.project,
            webhook_secret=body.webhook_secret,
            url_patterns=body.url_patterns,
            auth_type=body.auth_type,
            is_default=body.is_default,
            enabled=body.enabled,
            metadata=body.metadata,
        )
    except _ga.GitAccountConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return out


@router.patch("/{account_id}")
async def update_git_account(
    account_id: str,
    body: GitAccountUpdate,
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    updates = {
        k: v for k, v in body.model_dump(exclude_unset=True).items()
    }
    try:
        out = await _ga.update_account(account_id, updates=updates)
    except _ga.GitAccountNotFound:
        raise HTTPException(404, "git_account not found")
    except _ga.GitAccountConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return out


@router.delete("/{account_id}")
async def delete_git_account(
    account_id: str,
    auto_elect_new_default: bool = Query(True),
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    try:
        out = await _ga.delete_account(
            account_id,
            auto_elect_new_default=auto_elect_new_default,
        )
    except _ga.GitAccountNotFound:
        raise HTTPException(404, "git_account not found")
    except _ga.GitAccountConflict as exc:
        raise HTTPException(409, str(exc))
    return {"status": "deleted", **out}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Live probe
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _probe_token_for(
    platform: str, token: str, instance_url: str, ssh_host: str,
    ssh_port: int,
) -> dict:
    """Dispatch to the correct platform probe.

    Reuses the Bootstrap Step-3.5 probes from
    :mod:`backend.routers.integration` so the probe logic doesn't
    drift across two endpoints. Gerrit probes via SSH; the rest
    via HTTP REST.
    """
    # Local import to avoid a hard-coupling at router import time
    # (and to keep the dependency cycle routers→integration
    # one-way).
    from backend.routers import integration as _int

    if platform == "github":
        return await _int._probe_github_token(token)
    if platform == "gitlab":
        return await _int._probe_gitlab_token(token, instance_url)
    if platform == "gerrit":
        return await _int._probe_gerrit_ssh(ssh_host, ssh_port, instance_url)
    if platform == "jira":
        return await _probe_jira_token(token, instance_url)
    return {"status": "error", "message": f"Unknown platform {platform!r}"}


async def _probe_jira_token(token: str, instance_url: str) -> dict:
    """Minimal JIRA probe — ``GET /rest/api/3/myself`` with Bearer.

    JIRA Cloud's PAT-ish credential is a Basic-auth user:token combo
    in practice, but Atlassian tokens from the operator's profile
    also work with ``Authorization: Bearer <token>`` against the
    REST v3. We try Bearer first because that's what the operator
    typed into the UI; a 401 surfaces the "try basic auth" hint.
    """
    if not token:
        return {"status": "error", "message": "Token is required"}
    base = (instance_url or "").strip().rstrip("/")
    if not base:
        return {
            "status": "error",
            "message": "instance_url is required for JIRA probe",
        }
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/json",
        f"{base}/rest/api/3/myself",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    raw = stdout.decode(errors="replace")
    try:
        import json as _json
        data = _json.loads(raw)
    except Exception:
        return {
            "status": "error",
            "message": "Invalid response from JIRA API",
        }
    if isinstance(data, dict) and ("accountId" in data or "emailAddress" in data):
        return {
            "status": "ok",
            "accountId": data.get("accountId", ""),
            "email": data.get("emailAddress", ""),
            "displayName": data.get("displayName", ""),
        }
    message = "JIRA returned an unexpected response"
    if isinstance(data, dict):
        message = data.get("errorMessages", [message])[0] if data.get(
            "errorMessages"
        ) else data.get("message", message)
    return {"status": "error", "message": message}


@router.post("/{account_id}/test")
async def test_git_account(
    account_id: str,
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    row = await _ga.get_account(account_id)
    if row is None:
        raise HTTPException(404, "git_account not found")
    token = await _ga.get_plaintext_token(account_id)
    try:
        probe = await asyncio.wait_for(
            _probe_token_for(
                row["platform"],
                token or "",
                row["instance_url"],
                row["ssh_host"],
                int(row["ssh_port"] or 0),
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "message": "Connection timed out (15s)",
            "account_id": account_id,
        }
    probe.pop("_body_offset", None)
    return {
        "account_id": account_id,
        "platform": row["platform"],
        **probe,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Resolve (debug)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/resolve")
async def resolve_account_for_url(
    url: str = Query(..., min_length=1, max_length=2048),
    user: _au.User = Depends(_au.require_admin),
):
    """Return the ``git_accounts`` row that :func:`pick_account_for_url`
    would pick for *url*, plus a short ``matched_via`` tag describing
    WHICH resolver step produced the match so operators can debug
    URL-pattern behaviour.

    Does NOT touch ``last_used_at`` — debug introspection must not
    move the LRU bookkeeping.
    """
    _ensure_tenant(user)
    picked = await _gc.pick_account_for_url(url, touch=False)
    if picked is None:
        return {
            "url": url,
            "matched": False,
            "matched_via": None,
            "account": None,
        }
    # We ran pick with touch=False; to keep the response small +
    # unleaky, strip the plaintext secret fields that the 5-2 shim
    # exposes for sync callers.
    redacted = {
        k: v for k, v in picked.items()
        if k not in {"token", "ssh_key", "webhook_secret"}
    }
    return {
        "url": url,
        "matched": True,
        # Tag the resolver step that matched: pattern / host /
        # default. We recompute cheaply from the picked row rather
        # than threading the step out of pick_account_for_url.
        "matched_via": _classify_match(picked, url),
        "account": redacted,
    }


def _classify_match(picked: dict, url: str) -> str:
    """Best-effort tag of WHICH pick step matched *url*.

    Matches against:
    1. url_patterns (fnmatch) → ``"url_pattern"``
    2. exact host against instance_url / ssh_host → ``"exact_host"``
    3. is_default → ``"platform_default"``
    4. otherwise → ``"fallback"``
    """
    from backend.git_credentials import (
        _extract_host, _matches_pattern, _normalize_url_for_pattern_match,
    )
    stripped = _normalize_url_for_pattern_match(url)
    patterns = picked.get("url_patterns") or []
    for pat in patterns:
        if _matches_pattern(stripped, pat):
            return "url_pattern"
    needle = _extract_host(url)
    entry_host = _extract_host(
        picked.get("instance_url", "") or picked.get("url", "")
    )
    ssh_host = (picked.get("ssh_host") or "").lower()
    if needle and (needle == entry_host or needle == ssh_host):
        return "exact_host"
    if picked.get("is_default"):
        return "platform_default"
    return "fallback"
