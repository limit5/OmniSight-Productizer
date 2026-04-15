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
from backend.rate_limit import ip_limiter, email_limiter

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


def _mask_email(email: str) -> str:
    if "@" in email:
        return email[:2] + "***@" + email.split("@")[-1]
    return email[:2] + "***"


@router.post("/auth/login")
async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    _check_login_rate_limit(request)

    client_ip = _client_key(request)
    email_key = req.email.lower().strip()

    ip_ok, ip_wait = ip_limiter().allow(client_ip)
    if not ip_ok:
        raise HTTPException(
            status_code=429,
            detail=f"too many login attempts from this IP; retry in {int(ip_wait) + 1}s",
            headers={"Retry-After": str(int(ip_wait) + 1)},
        )

    email_ok, email_wait = email_limiter().allow(email_key)
    if not email_ok:
        raise HTTPException(
            status_code=429,
            detail=f"too many login attempts for this account; retry in {int(email_wait) + 1}s",
            headers={"Retry-After": str(int(email_wait) + 1)},
        )

    locked, lock_remaining = await auth.is_account_locked(email_key)
    if locked:
        try:
            from backend import audit as _audit
            await _audit.log(
                action="auth.lockout", entity_kind="auth",
                entity_id=_mask_email(req.email),
                before={"ip": client_ip},
                after={"reason": "account_locked", "retry_after_s": int(lock_remaining)},
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=423,
            detail=f"account locked; retry in {int(lock_remaining) + 1}s",
            headers={"Retry-After": str(int(lock_remaining) + 1)},
        )

    user = await auth.authenticate_password(req.email, req.password)
    if not user:
        _record_failed_login(request)
        masked = _mask_email(req.email)
        try:
            from backend import audit as _audit
            await _audit.log(
                action="auth.login.fail", entity_kind="auth", entity_id=masked,
                before={"ip": client_ip},
                after={"reason": "bad_credentials"},
            )
        except Exception as exc:
            logger.debug("auth.login.fail audit emit failed: %s", exc)

        new_locked, _ = await auth.is_account_locked(email_key)
        if new_locked:
            try:
                from backend import audit as _audit
                await _audit.log(
                    action="auth.lockout", entity_kind="auth",
                    entity_id=masked,
                    before={"ip": client_ip},
                    after={"reason": "threshold_reached"},
                )
            except Exception:
                pass

        raise HTTPException(status_code=401, detail="invalid email or password")

    sess = await auth.create_session(
        user.id,
        ip=client_ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    try:
        from backend import audit as _audit
        await _audit.log(
            action="login_ok", entity_kind="auth", entity_id=user.id,
            before={"ip": client_ip},
            after={"role": user.role, "email": user.email},
            actor=user.email, session_id=sess.token,
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
async def whoami(request: Request,
                 user: auth.User = Depends(auth.current_user)) -> dict:
    sess = getattr(getattr(request, "state", None), "session", None)
    sid = auth.session_id_from_token(sess.token) if sess and sess.token else None
    return {
        "user": user.to_dict(),
        "auth_mode": auth.auth_mode(),
        "session_id": sid,
    }


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=12)


@router.post("/auth/change-password")
async def change_password(req: ChangePasswordRequest, request: Request,
                          user: auth.User = Depends(auth.current_user)) -> dict:
    """Change the current user's password. Clears must_change_password flag."""
    verified = await auth.authenticate_password(user.email, req.current_password)
    if not verified:
        raise HTTPException(status_code=401, detail="current password is incorrect")
    await auth.change_password(user.id, req.new_password)
    try:
        from backend import audit as _audit
        await _audit.log(
            action="password_changed", entity_kind="auth", entity_id=user.id,
            before={"must_change_password": user.must_change_password},
            after={"must_change_password": False},
            actor=user.email,
        )
    except Exception as exc:
        logger.debug("password_changed audit emit failed: %s", exc)
    return {"status": "password_changed", "must_change_password": False}


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


# ── Session management ─────────────────────────────────────────


@router.get("/auth/sessions")
async def list_sessions(request: Request,
                        user: auth.User = Depends(auth.current_user)) -> dict:
    sessions = await auth.list_sessions(user.id)
    current_token = request.cookies.get(auth.SESSION_COOKIE) or ""
    items = []
    for s in sessions:
        items.append({
            "token_hint": s["token_hint"],
            "created_at": s["created_at"],
            "expires_at": s["expires_at"],
            "last_seen_at": s["last_seen_at"],
            "ip": s["ip"],
            "user_agent": s["user_agent"],
            "is_current": s["token"] == current_token,
        })
    return {"items": items, "count": len(items)}


@router.delete("/auth/sessions/{token_hint}")
async def revoke_session(token_hint: str, request: Request,
                         user: auth.User = Depends(auth.current_user)) -> dict:
    target_user_id = user.id
    is_admin = auth.role_at_least(user.role, "admin")
    sessions = await auth.list_sessions(user.id)
    target_token = None
    for s in sessions:
        if s["token_hint"] == token_hint:
            target_token = s["token"]
            target_user_id = s["user_id"]
            break
    if not target_token and is_admin:
        from backend import db
        async with db._conn().execute(
            "SELECT token, user_id FROM sessions"
        ) as cur:
            async for row in cur:
                if auth._mask_token(row["token"]) == token_hint:
                    target_token = row["token"]
                    target_user_id = row["user_id"]
                    break
    if not target_token:
        raise HTTPException(status_code=404, detail="session not found")
    if target_user_id != user.id and not is_admin:
        raise HTTPException(status_code=403, detail="cannot revoke another user's session")
    await auth.revoke_session(target_token)
    try:
        from backend import audit as _audit
        await _audit.write_audit(
            request, action="session_revoke", entity_kind="session",
            entity_id=token_hint, actor=user.email,
        )
    except Exception:
        pass
    return {"status": "revoked", "token_hint": token_hint}


@router.delete("/auth/sessions")
async def revoke_all_other_sessions(request: Request,
                                    user: auth.User = Depends(auth.current_user)) -> dict:
    current_token = request.cookies.get(auth.SESSION_COOKIE) or ""
    count = await auth.revoke_other_sessions(user.id, current_token)
    try:
        from backend import audit as _audit
        await _audit.write_audit(
            request, action="sessions_revoke_all_others", entity_kind="session",
            entity_id=user.id, after={"revoked_count": count},
            actor=user.email,
        )
    except Exception:
        pass
    return {"status": "revoked", "revoked_count": count}
