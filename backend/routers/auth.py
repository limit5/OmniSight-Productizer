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

    # K5: Check if user has MFA enrolled — if so, defer session creation
    from backend import mfa as _mfa
    has_mfa = await _mfa.has_verified_mfa(user.id)
    if has_mfa:
        mfa_token = await _mfa.create_mfa_challenge(
            user.id, ip=client_ip,
            user_agent=request.headers.get("user-agent", ""),
        )
        methods = await _mfa.get_user_mfa_methods(user.id)
        available = [m["method"] for m in methods if m["verified"]]
        try:
            from backend import audit as _audit
            await _audit.log(
                action="login_mfa_required", entity_kind="auth",
                entity_id=user.id,
                before={"ip": client_ip},
                after={"methods": available},
                actor=user.email,
            )
        except Exception:
            pass
        return {
            "mfa_required": True,
            "mfa_token": mfa_token,
            "mfa_methods": list(set(available)),
            "user": {"email": user.email},
        }

    ua_header = request.headers.get("user-agent", "")
    sess = await auth.create_session(
        user.id,
        ip=client_ip,
        user_agent=ua_header,
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
    await auth.notify_new_device_login(user, sess, client_ip, ua_header)
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


@router.get("/auth/tenants")
async def user_tenants(user: auth.User = Depends(auth.current_user)) -> list[dict]:
    """I7: Return tenants accessible to the current user.

    Admin users get all tenants; regular users get only their own.
    """
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        if user.role == "admin":
            rows = await conn.fetch(
                "SELECT id, name, plan, enabled FROM tenants ORDER BY name"
            )
            return [
                {"id": r["id"], "name": r["name"], "plan": r["plan"],
                 "enabled": bool(r["enabled"])}
                for r in rows
            ]
        r = await conn.fetchrow(
            "SELECT id, name, plan, enabled FROM tenants WHERE id = $1",
            user.tenant_id,
        )
    if r:
        return [{"id": r["id"], "name": r["name"], "plan": r["plan"],
                 "enabled": bool(r["enabled"])}]
    return [{"id": user.tenant_id, "name": user.tenant_id,
             "plan": "free", "enabled": True}]


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=12)


@router.post("/auth/change-password")
async def change_password(req: ChangePasswordRequest, request: Request,
                          response: Response,
                          user: auth.User = Depends(auth.current_user)) -> dict:
    """Change the current user's password. Rotates session token."""
    verified = await auth.authenticate_password(user.email, req.current_password)
    if not verified:
        raise HTTPException(status_code=401, detail="current password is incorrect")

    strength_err = auth.validate_password_strength(req.new_password)
    if strength_err:
        raise HTTPException(status_code=422, detail=strength_err)

    if await auth.check_password_history(user.id, req.new_password):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot reuse any of the last {auth.PASSWORD_HISTORY_LIMIT} passwords",
        )

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

    old_token = request.cookies.get(auth.SESSION_COOKIE) or ""
    new_csrf = None
    new_current_token: str | None = None
    if old_token:
        try:
            new_sess, _ = await auth.rotate_session(
                old_token,
                ip=_client_key(request),
                user_agent=request.headers.get("user-agent", ""),
            )
            new_current_token = new_sess.token
            secure = _cookie_secure()
            response.set_cookie(
                key=auth.SESSION_COOKIE, value=new_sess.token,
                max_age=auth.SESSION_TTL_S, httponly=True,
                secure=secure, samesite="lax",
            )
            response.set_cookie(
                key=auth.CSRF_COOKIE, value=new_sess.csrf_token,
                max_age=auth.SESSION_TTL_S, httponly=False,
                secure=secure, samesite="lax",
            )
            new_csrf = new_sess.csrf_token
            try:
                from backend import audit as _audit
                await _audit.log(
                    action="session_rotated", entity_kind="session",
                    entity_id=user.id,
                    before={"reason": "password_change"},
                    after={"grace_s": auth.ROTATION_GRACE_S},
                    actor=user.email, session_id=new_sess.token,
                )
            except Exception:
                pass
        except ValueError:
            pass

    # Q.1 2026-04-22 security red line: kick every OTHER active session
    # belonging to this user. Without this, a compromised laptop still
    # has up to SESSION_TTL_S (8h) of authorised access after the
    # victim changes the password from a safe device. The exclude
    # argument is the NEWLY rotated current-device token — so the
    # device that just changed password stays logged in. Uses the same
    # 30s grace window as the individual rotation path (K4) so in-
    # flight requests from the other devices don't 401-storm. A
    # audit event (reason=user_security_event) separates these from
    # idle / user-initiated / admin-initiated revokes.
    try:
        revoked_count = await auth.rotate_user_sessions(
            user.id, exclude_token=new_current_token,
            reason="user_security_event", trigger="password_change",
        )
        if revoked_count > 0:
            from backend import audit as _audit
            await _audit.log(
                action="session_rotated", entity_kind="session",
                entity_id=user.id,
                before={"reason": "user_security_event",
                        "trigger": "password_change"},
                after={"rotated_count": revoked_count,
                       "grace_s": auth.ROTATION_GRACE_S},
                actor=user.email,
            )
    except Exception as exc:
        logger.warning(
            "peer-session rotation after password_change failed for "
            "user=%s: %s (current device session rotated OK; peer "
            "devices may retain access up to session TTL)",
            user.email, exc,
        )

    result: dict = {"status": "password_changed", "must_change_password": False}
    if new_csrf:
        result["csrf_token"] = new_csrf
    return result


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
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, email, name, role, enabled, "
            "created_at, last_login_at "
            "FROM users ORDER BY created_at DESC"
        )
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
                     request: Request,
                     admin_user: auth.User = Depends(auth.require_admin)) -> dict:
    user = await auth.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    old_role = user.role
    sets: list[str] = []
    params: list = []
    if req.role is not None:
        if req.role not in auth.ROLES:
            return JSONResponse(status_code=422, content={"detail": f"unknown role: {req.role}"})
        params.append(req.role)
        sets.append(f"role = ${len(params)}")
    if req.enabled is not None:
        params.append(1 if req.enabled else 0)
        sets.append(f"enabled = ${len(params)}")
    if req.name is not None:
        params.append(req.name)
        sets.append(f"name = ${len(params)}")
    if not sets:
        return user.to_dict()
    params.append(user_id)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ${len(params)}",
            *params,
        )

    if req.role is not None and req.role != old_role:
        count = await auth.rotate_user_sessions(
            user_id,
            reason="user_security_event", trigger="role_change",
        )
        try:
            from backend import audit as _audit
            await _audit.log(
                action="session_rotated", entity_kind="session",
                entity_id=user_id,
                before={"reason": "user_security_event",
                        "trigger": "role_change",
                        "old_role": old_role},
                after={"new_role": req.role, "rotated_count": count,
                       "grace_s": auth.ROTATION_GRACE_S},
                actor=admin_user.email,
            )
        except Exception:
            pass

    # Q.1 2026-04-22: admin disabling a user must kick every active
    # session. Without this, a disabled account retains access up to
    # SESSION_TTL_S — defeating the point of the disable action.
    # Transitioning to ``enabled=False`` is the security event; turning
    # the account back on does NOT need to rotate (no stale token).
    if req.enabled is False and user.enabled:
        count = await auth.rotate_user_sessions(
            user_id,
            reason="user_security_event", trigger="account_disabled",
        )
        try:
            from backend import audit as _audit
            await _audit.log(
                action="session_rotated", entity_kind="session",
                entity_id=user_id,
                before={"reason": "user_security_event",
                        "trigger": "account_disabled"},
                after={"rotated_count": count,
                       "grace_s": auth.ROTATION_GRACE_S},
                actor=admin_user.email,
            )
        except Exception:
            pass

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


# Q.5 #299 — active-device presence indicator.
#
# The heartbeat producer lives in ``backend/routers/events.py::event_stream``
# (writes via ``session_presence.record_heartbeat`` on SSE connect + every
# 15 s heartbeat tick). This endpoint is the consumer — it answers the
# dashboard badge's "how many of my devices are online right now?" with
# per-device metadata for the hover mini-list.
#
# Window: 60 s (Q.5 spec). ``status`` classifies within that window as
# ``active`` (< ``_PRESENCE_IDLE_THRESHOLD_S``) or ``idle`` (older, but
# still inside the window — the SSE stream is alive, the user is AFK).
# Anything older than the 60 s window is considered offline and excluded.
#
# SOP Step 1 module-global audit: reads the ``session_presence`` SharedKV
# singleton (Redis-backed across workers, in-memory per-worker in dev —
# rubric #2/#3 mixed, documented in shared_state.py) and PG ``sessions``
# table via the pool. No new module-global state introduced.
_PRESENCE_WINDOW_S = 60.0
_PRESENCE_IDLE_THRESHOLD_S = 30.0


def _label_ua(user_agent: str) -> str:
    """Mirror of ``components/omnisight/session-manager-panel.tsx::parseUA``.

    Same lookup order on both sides of the wire so the presence badge's
    device label stays visually identical to the session manager panel.
    """
    ua = user_agent or ""
    if not ua:
        return "Unknown device"
    if "Firefox" in ua:
        browser = "Firefox"
    elif "Edg" in ua:
        browser = "Edge"
    elif "Chrome" in ua:
        browser = "Chrome"
    elif "Safari" in ua:
        browser = "Safari"
    else:
        browser = "Browser"
    if "Windows" in ua:
        os_name = "Windows"
    elif "Mac OS" in ua:
        os_name = "macOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "OS"
    return f"{browser} on {os_name}"


@router.get("/auth/sessions/presence")
async def sessions_presence(
    request: Request,
    user: auth.User = Depends(auth.current_user),
) -> dict:
    """Return the count + brief metadata for the caller's active devices.

    Active = heartbeat recorded by the SSE stream within the last
    ``_PRESENCE_WINDOW_S`` seconds. Devices inside the window but quieter
    than ``_PRESENCE_IDLE_THRESHOLD_S`` are flagged ``status="idle"``;
    fresher ones ``status="active"``.
    """
    from backend.shared_state import session_presence

    now = time.time()
    active = session_presence.active_sessions(
        user.id, window_seconds=_PRESENCE_WINDOW_S, now=now,
    )

    # Resolve UA + token_hint by crosswalking the PG sessions table — the
    # presence hash only keys (user_id, session_id_hash). Sessions
    # revoked mid-window may no longer resolve; keep them in the reply
    # with minimal metadata so the count matches what the SSE stream
    # reported, but mark the device name as unknown.
    sessions = await auth.list_sessions(user.id)
    by_session: dict[str, dict] = {
        auth.session_id_from_token(s["token"]): s for s in sessions
    }

    current_token = request.cookies.get(auth.SESSION_COOKIE) or ""
    current_sid = (
        auth.session_id_from_token(current_token) if current_token else ""
    )

    devices: list[dict] = []
    for session_id, ts in active:
        idle = max(0.0, now - ts)
        status = (
            "active" if idle < _PRESENCE_IDLE_THRESHOLD_S else "idle"
        )
        meta = by_session.get(session_id)
        if meta:
            ua = meta.get("user_agent") or ""
            token_hint = meta.get("token_hint") or ""
        else:
            ua = ""
            token_hint = ""
        devices.append({
            "session_id": session_id,
            "token_hint": token_hint,
            "device_name": _label_ua(ua),
            "ua_hash": auth.compute_ua_hash(ua),
            "last_heartbeat_at": ts,
            "idle_seconds": round(idle, 3),
            "status": status,
            "is_current": bool(current_sid) and session_id == current_sid,
        })

    # Opportunistic GC — safe inside the request path since the hash is
    # small (one field per device). Uses the same 60 s window so nothing
    # we just returned will be pruned.
    try:
        session_presence.prune_expired(
            window_seconds=_PRESENCE_WINDOW_S, now=now,
        )
    except Exception:
        logger.debug("presence: prune_expired swallowed", exc_info=True)

    return {
        "active_count": len(devices),
        "window_seconds": _PRESENCE_WINDOW_S,
        "now": now,
        "devices": devices,
    }


@router.delete("/auth/sessions/{token_hint}")
async def revoke_session(token_hint: str, request: Request,
                         response: Response,
                         cascade: str | None = None,
                         user: auth.User = Depends(auth.current_user)) -> dict:
    """Revoke a single session by opaque token hint.

    Default (no ``cascade`` query param): delete the matching session
    row only. Unchanged legacy behaviour — used by the ``/settings/
    security`` panel and `/auth/sessions` UI.

    Q.2 (#296) 「這不是我」 cascade: ``?cascade=not_me`` escalates the
    operation into a full account-compromise response. When the new-
    device toast renders and the user decides the login wasn't them:

      1. Revoke the flagged session (same as default path).
      2. Rotate every OTHER session belonging to the user via the
         Q.1 path (``auth.rotate_user_sessions`` with reason=
         ``user_security_event``, trigger=``not_me_cascade``) — this
         kicks the calling device too, so on the next request it
         401s and lib/api.ts redirects to ``/login?reason=...``.
      3. Flip ``users.must_change_password = 1`` so the K1 428 gate
         forces a password change before any other API call succeeds
         after re-login.
      4. Clear the caller's own session + CSRF cookies so the browser
         doesn't hold onto a dead token.

    The cascade is idempotent-safe: if the target session was already
    gone (double-click race) we still run steps 2-4, because the
    caller's intent is "kick everyone, force password change" — not
    "just delete this one row". A 404 on the target session returns
    an empty cascade result rather than silently succeeding.
    """
    want_cascade = (cascade or "").strip().lower() == "not_me"

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
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            rows = await conn.fetch("SELECT token, user_id FROM sessions")
        for row in rows:
            if auth._mask_token(row["token"]) == token_hint:
                target_token = row["token"]
                target_user_id = row["user_id"]
                break
    if not target_token:
        raise HTTPException(status_code=404, detail="session not found")
    if target_user_id != user.id and not is_admin:
        raise HTTPException(status_code=403, detail="cannot revoke another user's session")

    # Q.2 (#296) cascade=not_me must not let an admin blast another
    # user's account by revoking one of their sessions — cascade is a
    # self-service security red line. Admins can still revoke a single
    # peer session without cascade (the legacy path).
    if want_cascade and target_user_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="cascade=not_me is self-service only; use /users/{id} "
                   "to disable another user's account",
        )

    await auth.revoke_session(target_token)
    try:
        from backend import audit as _audit
        await _audit.write_audit(
            request, action="session_revoke", entity_kind="session",
            entity_id=token_hint, actor=user.email,
            after={"cascade": "not_me"} if want_cascade else None,
        )
    except Exception:
        pass

    if not want_cascade:
        return {"status": "revoked", "token_hint": token_hint}

    # Cascade: rotate every remaining session for this user (including
    # the caller — no exclude_token) and flip must_change_password.
    rotated_count = 0
    try:
        rotated_count = await auth.rotate_user_sessions(
            user.id, exclude_token=None,
            reason="user_security_event", trigger="not_me_cascade",
        )
    except Exception as exc:
        logger.warning(
            "not_me cascade: rotate_user_sessions failed for user=%s: %s "
            "(target session %s already revoked; peer devices may retain "
            "access up to session TTL). Continuing with must_change_password flip.",
            user.email, exc, token_hint,
        )

    pwflag_ok = False
    try:
        pwflag_ok = await auth.flag_user_must_change_password(user.id)
    except Exception as exc:
        logger.warning(
            "not_me cascade: flag_user_must_change_password failed for "
            "user=%s: %s (sessions rotated OK, but next login may not be "
            "forced to change password)",
            user.email, exc,
        )

    try:
        from backend import audit as _audit
        await _audit.log(
            action="session_rotated", entity_kind="session",
            entity_id=user.id,
            before={"reason": "user_security_event",
                    "trigger": "not_me_cascade",
                    "revoked_token_hint": token_hint},
            after={"rotated_count": rotated_count,
                   "must_change_password": pwflag_ok,
                   "grace_s": auth.ROTATION_GRACE_S},
            actor=user.email,
        )
    except Exception:
        pass

    # Clear the caller's own cookies — their session was just rotated
    # into the grace window and we want the browser to drop the cookie
    # immediately so the next request 401s and the /login redirect
    # kicks in with the ``trigger=not_me_cascade`` banner.
    response.delete_cookie(auth.SESSION_COOKIE)
    response.delete_cookie(auth.CSRF_COOKIE)

    return {
        "status": "revoked",
        "token_hint": token_hint,
        "cascade": "not_me",
        "rotated_count": rotated_count,
        "must_change_password": pwflag_ok,
    }


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
