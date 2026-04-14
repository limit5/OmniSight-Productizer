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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from backend import auth

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


def _cookie_secure() -> bool:
    return (os.environ.get("OMNISIGHT_COOKIE_SECURE") or "").strip().lower() == "true"


# ── Login / logout / whoami ─────────────────────────────────────


class LoginRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


@router.post("/auth/login")
async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    user = await auth.authenticate_password(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid email or password")
    sess = await auth.create_session(
        user.id,
        ip=(request.client.host if request.client else "") or "",
        user_agent=request.headers.get("user-agent", ""),
    )
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
