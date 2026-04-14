"""Phase 54 — Auth + user management router.

POST   /auth/login        email + password → sets session cookie
POST   /auth/logout       clears session
GET    /auth/whoami       current user (or anonymous-admin in open mode)
GET    /auth/oidc/{provider}    OIDC redirect stub (Google / GitHub)
GET    /users             list users (admin)
POST   /users             create user (admin)
PATCH  /users/{id}        change role / enable / disable (admin)

Login sets two cookies:
  omnisight_session  HttpOnly, SameSite=Lax, secure when HTTPS
  omnisight_csrf     non-HttpOnly so JS can read + echo via header
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from backend import auth

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


def _cookie_secure() -> bool:
    return (os.environ.get("OMNISIGHT_COOKIE_SECURE") or "").strip().lower() == "true"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Login brute-force defence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Rolling per-IP attempt window. Five failed logins in 15 min → 401
# with `Retry-After` until the oldest attempt ages out. Holds in
# memory only — restart resets the counter, which is fine because a
# restart already clears sessions.
#
# Tunables via env so security ops can tighten under attack:
#   OMNISIGHT_LOGIN_MAX_ATTEMPTS=5
#   OMNISIGHT_LOGIN_WINDOW_S=900       # 15 min
#
# IPv4/IPv6 client.host is the key. Behind Cloudflare Tunnel, the
# real IP arrives as `cf-connecting-ip` header — honour it when present
# so the limit is per-real-client, not per-tunnel-egress.

_LOGIN_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)
# Cap dict size so a parade of unique source IPs can't OOM the box.
_LOGIN_ATTEMPTS_MAX_KEYS = 4096


def _login_max_attempts() -> int:
    raw = (os.environ.get("OMNISIGHT_LOGIN_MAX_ATTEMPTS") or "5").strip()
    try:
        return max(1, min(100, int(raw)))
    except ValueError:
        return 5


def _login_window_s() -> float:
    raw = (os.environ.get("OMNISIGHT_LOGIN_WINDOW_S") or "900").strip()
    try:
        return max(60.0, min(86400.0, float(raw)))
    except ValueError:
        return 900.0


def _client_key(request: Request) -> str:
    """Real-client IP, preferring CF's `cf-connecting-ip` because behind
    a Cloudflare Tunnel the immediate peer is always the tunnel."""
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    return (request.client.host if request.client else "") or "unknown"


def _check_login_rate_limit(request: Request) -> None:
    """Raise 429 if the caller already burned their attempts."""
    key = _client_key(request)
    now = time.time()
    window = _login_window_s()
    cap = _login_max_attempts()

    # Bound dictionary growth — drop the longest-untouched key when full.
    if key not in _LOGIN_ATTEMPTS and len(_LOGIN_ATTEMPTS) >= _LOGIN_ATTEMPTS_MAX_KEYS:
        oldest_key = min(
            _LOGIN_ATTEMPTS,
            key=lambda k: _LOGIN_ATTEMPTS[k][-1] if _LOGIN_ATTEMPTS[k] else 0,
        )
        _LOGIN_ATTEMPTS.pop(oldest_key, None)

    bucket = _LOGIN_ATTEMPTS[key]
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= cap:
        retry_after = int(bucket[0] + window - now) + 1
        raise HTTPException(
            status_code=429,
            detail=f"too many login attempts; retry in {retry_after}s",
            headers={"Retry-After": str(retry_after)},
        )


def _record_failed_login(request: Request) -> None:
    """Append a failure to the per-IP window. Successful logins
    intentionally do NOT add to the window — a long-lived legitimate
    session shouldn't lock out the IP if a roommate later mistypes."""
    _LOGIN_ATTEMPTS[_client_key(request)].append(time.time())


# ── Login / logout / whoami ─────────────────────────────────────


class LoginRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


@router.post("/auth/login")
async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    # Brute-force defence FIRST — never authenticate a request that
    # already exceeded its window, otherwise password-hash CPU is the
    # attack vector even when every password is wrong.
    _check_login_rate_limit(request)

    user = await auth.authenticate_password(req.email, req.password)
    if not user:
        _record_failed_login(request)
        # Audit the failure with the masked email so log search can
        # find brute-force fingerprints by IP without leaking which
        # accounts were probed in cleartext.
        try:
            from backend import audit as _audit
            masked = (req.email[:2] + "***@" + req.email.split("@")[-1]
                      if "@" in req.email else req.email[:2] + "***")
            await _audit.log(
                action="login_failed", entity_kind="auth", entity_id=masked,
                before={"ip": _client_key(request)},
                after={"reason": "bad_credentials"},
            )
        except Exception as exc:
            logger.debug("login_failed audit emit failed: %s", exc)
        raise HTTPException(status_code=401, detail="invalid email or password")
    sess = await auth.create_session(
        user.id,
        ip=_client_key(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    try:
        from backend import audit as _audit
        await _audit.log(
            action="login_ok", entity_kind="auth", entity_id=user.id,
            before={"ip": _client_key(request)},
            after={"role": user.role, "email": user.email},
            actor=user.email,
        )
    except Exception as exc:
        logger.debug("login_ok audit emit failed: %s", exc)
    secure = _cookie_secure()
    response.set_cookie(
        key=auth.SESSION_COOKIE, value=sess.token,
        max_age=auth.SESSION_TTL_S, httponly=True, secure=secure, samesite="lax",
    )
    response.set_cookie(
        key=auth.CSRF_COOKIE, value=sess.csrf_token,
        max_age=auth.SESSION_TTL_S, httponly=False, secure=secure, samesite="lax",
    )
    return {"user": user.to_dict(), "csrf_token": sess.csrf_token}


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> dict:
    cookie = request.cookies.get(auth.SESSION_COOKIE) or ""
    if cookie:
        await auth.delete_session(cookie)
    response.delete_cookie(auth.SESSION_COOKIE)
    response.delete_cookie(auth.CSRF_COOKIE)
    return {"status": "logged_out"}


@router.get("/auth/whoami")
async def whoami(user: auth.User = Depends(auth.current_user)) -> dict:
    return {
        "user": user.to_dict(),
        "auth_mode": auth.auth_mode(),
    }


@router.get("/auth/oidc/{provider}")
async def oidc_redirect(provider: str) -> RedirectResponse:
    """Stub — real OIDC flow lands in v1. Returns 501 unless the env
    has `OMNISIGHT_OIDC_<PROVIDER>_AUTH_URL` configured."""
    key = f"OMNISIGHT_OIDC_{provider.upper()}_AUTH_URL"
    url = os.environ.get(key, "").strip()
    if not url:
        raise HTTPException(
            status_code=501,
            detail=f"OIDC for {provider} not configured (set {key})",
        )
    return RedirectResponse(url=url, status_code=302)


# ── User management (admin only) ────────────────────────────────


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=3)
    name: str = ""
    role: str = "viewer"
    password: str | None = None


class PatchUserRequest(BaseModel):
    role: str | None = None
    enabled: bool | None = None
    name: str | None = None


@router.get("/users")
async def list_users(_: auth.User = Depends(auth.require_admin)) -> dict:
    from backend import db
    async with db._conn().execute(
        "SELECT id, email, name, role, enabled, created_at, last_login_at "
        "FROM users ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    return {"items": [
        {"id": r["id"], "email": r["email"], "name": r["name"],
         "role": r["role"], "enabled": bool(r["enabled"]),
         "created_at": r["created_at"], "last_login_at": r["last_login_at"]}
        for r in rows
    ], "count": len(rows)}


@router.post("/users")
async def create_user(req: CreateUserRequest,
                      _: auth.User = Depends(auth.require_admin)) -> dict:
    if req.role not in auth.ROLES:
        return JSONResponse(status_code=422, content={"detail": f"unknown role: {req.role}"})
    existing = await auth.get_user_by_email(req.email)
    if existing:
        return JSONResponse(status_code=409, content={"detail": "email already exists"})
    user = await auth.create_user(
        email=req.email, name=req.name or req.email.split("@")[0],
        role=req.role, password=req.password,
    )
    return user.to_dict()


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, req: PatchUserRequest,
                     _: auth.User = Depends(auth.require_admin)) -> dict:
    user = await auth.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    sets: list[str] = []
    params: list = []
    if req.role is not None:
        if req.role not in auth.ROLES:
            return JSONResponse(status_code=422, content={"detail": f"unknown role: {req.role}"})
        sets.append("role=?")
        params.append(req.role)
    if req.enabled is not None:
        sets.append("enabled=?")
        params.append(1 if req.enabled else 0)
    if req.name is not None:
        sets.append("name=?")
        params.append(req.name)
    if not sets:
        return user.to_dict()
    params.append(user_id)
    from backend import db
    await db._conn().execute(
        f"UPDATE users SET {', '.join(sets)} WHERE id=?", tuple(params),
    )
    await db._conn().commit()
    updated = await auth.get_user(user_id)
    return updated.to_dict() if updated else {"detail": "vanished"}
