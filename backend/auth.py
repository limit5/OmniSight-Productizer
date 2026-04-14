"""Phase 54 — Sessions / RBAC / role gating.

Three modes (chosen by env `OMNISIGHT_AUTH_MODE`):

  open     (default)  no auth required; every request is treated as
                       admin. Preserves the pre-Phase-54 single-user
                       dev flow. Bearer token (legacy
                       OMNISIGHT_DECISION_BEARER) still honoured.
  session              cookie-based session required for mutators;
                       reads remain open. Bearer token still works
                       as a service-to-service backdoor.
  strict               cookie + CSRF required for everything except
                       /auth/login + /health.

Role hierarchy: viewer < operator < admin.
  viewer    read-only access to dashboards, audit (filtered to self
            actor)
  operator  approve/reject decisions; switch profile up to AUTONOMOUS;
            cannot switch to GHOST or change MODE=turbo
  admin     everything, including user management, MODE=turbo, GHOST,
            audit unfiltered

Session token = cryptographic random; CSRF token = independent random
shared via cookie + must be echoed in `X-CSRF-Token` header for
state-changing methods.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Roles + helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ROLES = ("viewer", "operator", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}


def role_at_least(have: str, need: str) -> bool:
    if have not in _RANK or need not in _RANK:
        return False
    return _RANK[have] >= _RANK[need]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mode selection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def auth_mode() -> str:
    m = (os.environ.get("OMNISIGHT_AUTH_MODE") or "open").strip().lower()
    if m not in {"open", "session", "strict"}:
        return "open"
    return m


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Password hashing — stdlib only so deps stay light. PBKDF2-SHA256
#  with 320k iterations matches NIST-recommended floor for 2026.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_PBKDF_ITERS = 320_000


def hash_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _PBKDF_ITERS)
    return f"pbkdf2_sha256${_PBKDF_ITERS}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    if not stored.startswith("pbkdf2_sha256$"):
        return False
    try:
        _, iters_s, salt_hex, digest_hex = stored.split("$", 3)
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        want = bytes.fromhex(digest_hex)
        got = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters)
        return secrets.compare_digest(got, want)
    except Exception:
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class User:
    id: str
    email: str
    name: str
    role: str
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name,
                "role": self.role, "enabled": self.enabled}


@dataclass
class Session:
    token: str
    user_id: str
    csrf_token: str
    created_at: float
    expires_at: float


SESSION_TTL_S = 8 * 60 * 60          # 8 hours
SESSION_COOKIE = "omnisight_session"
CSRF_COOKIE = "omnisight_csrf"
CSRF_HEADER = "X-CSRF-Token"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DB helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _conn():
    from backend import db
    return db._conn()


async def get_user(user_id: str) -> Optional[User]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, email, name, role, enabled FROM users WHERE id=?",
        (user_id,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None
    return User(id=r["id"], email=r["email"], name=r["name"],
                role=r["role"], enabled=bool(r["enabled"]))


async def get_user_by_email(email: str) -> Optional[User]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, email, name, role, enabled FROM users WHERE email=?",
        (email.lower().strip(),),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None
    return User(id=r["id"], email=r["email"], name=r["name"],
                role=r["role"], enabled=bool(r["enabled"]))


async def create_user(email: str, name: str, role: str = "viewer",
                      password: str | None = None,
                      oidc_provider: str = "", oidc_subject: str = "") -> User:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    conn = await _conn()
    uid = f"u-{uuid.uuid4().hex[:10]}"
    pw_hash = hash_password(password) if password else ""
    await conn.execute(
        "INSERT INTO users (id, email, name, role, password_hash, "
        "oidc_provider, oidc_subject, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (uid, email.lower().strip(), name, role, pw_hash,
         oidc_provider, oidc_subject),
    )
    await conn.commit()
    return User(id=uid, email=email.lower().strip(), name=name, role=role)


async def authenticate_password(email: str, password: str) -> Optional[User]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, email, name, role, enabled, password_hash "
        "FROM users WHERE email=?", (email.lower().strip(),),
    ) as cur:
        r = await cur.fetchone()
    if not r or not r["enabled"]:
        return None
    if not verify_password(password, r["password_hash"]):
        return None
    await conn.execute(
        "UPDATE users SET last_login_at=datetime('now') WHERE id=?",
        (r["id"],),
    )
    await conn.commit()
    return User(id=r["id"], email=r["email"], name=r["name"],
                role=r["role"], enabled=bool(r["enabled"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Session management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def create_session(user_id: str, ip: str = "", user_agent: str = "") -> Session:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = time.time()
    expires = now + SESSION_TTL_S
    conn = await _conn()
    await conn.execute(
        "INSERT INTO sessions (token, user_id, csrf_token, created_at, "
        "expires_at, last_seen_at, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, user_id, csrf, now, expires, now, ip, (user_agent or "")[:240]),
    )
    await conn.commit()
    return Session(token=token, user_id=user_id, csrf_token=csrf,
                   created_at=now, expires_at=expires)


async def get_session(token: str) -> Optional[Session]:
    if not token:
        return None
    conn = await _conn()
    async with conn.execute(
        "SELECT token, user_id, csrf_token, created_at, expires_at "
        "FROM sessions WHERE token=?", (token,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None
    if r["expires_at"] < time.time():
        await delete_session(token)
        return None
    # Touch last_seen
    await conn.execute(
        "UPDATE sessions SET last_seen_at=? WHERE token=?",
        (time.time(), token),
    )
    await conn.commit()
    return Session(
        token=r["token"], user_id=r["user_id"], csrf_token=r["csrf_token"],
        created_at=r["created_at"], expires_at=r["expires_at"],
    )


async def delete_session(token: str) -> None:
    if not token:
        return
    conn = await _conn()
    await conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    await conn.commit()


async def cleanup_expired_sessions() -> int:
    conn = await _conn()
    cur = await conn.execute(
        "DELETE FROM sessions WHERE expires_at < ?", (time.time(),),
    )
    await conn.commit()
    return cur.rowcount or 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bootstrap default admin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def ensure_default_admin() -> Optional[User]:
    """Create a default admin user if the users table is empty.
    Email + password come from env (or sensible dev defaults)."""
    conn = await _conn()
    async with conn.execute("SELECT COUNT(*) AS n FROM users") as cur:
        r = await cur.fetchone()
    if r and (r["n"] or 0) > 0:
        return None
    email = (os.environ.get("OMNISIGHT_ADMIN_EMAIL") or "admin@omnisight.local").strip()
    password = (os.environ.get("OMNISIGHT_ADMIN_PASSWORD") or "omnisight-admin").strip()
    user = await create_user(
        email=email, name="OmniSight Admin", role="admin", password=password,
    )
    logger.warning(
        "[AUTH] Created default admin: %s (set OMNISIGHT_ADMIN_PASSWORD to change)",
        email,
    )
    return user


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FastAPI dependencies
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _bearer_matches(req: Request) -> bool:
    expected = (os.environ.get("OMNISIGHT_DECISION_BEARER") or "").strip()
    if not expected:
        return False
    h = req.headers.get("authorization") or ""
    if h.startswith("Bearer "):
        h = h[len("Bearer "):]
    return bool(h) and h == expected


_ANON_ADMIN = User(id="anonymous", email="anonymous@local", name="(anonymous)",
                   role="admin", enabled=True)


async def current_user(request: Request) -> User:
    """Return the user attached to this request. Behaviour depends on
    OMNISIGHT_AUTH_MODE.

    open:    always returns the synthetic anonymous-admin
             (preserves pre-Phase-54 dev behaviour)
    session: cookie-backed session required, falls back to
             anonymous-admin only on idempotent (GET) requests
    strict:  cookie required for every request including reads
    """
    mode = auth_mode()
    if mode == "open":
        return _ANON_ADMIN

    # Bearer token still works as a service-to-service shortcut in
    # session/strict modes — useful for CLI / CI clients that don't
    # want to maintain cookie jars.
    if _bearer_matches(request):
        return _ANON_ADMIN

    cookie = request.cookies.get(SESSION_COOKIE) or ""
    sess = await get_session(cookie) if cookie else None
    if sess:
        u = await get_user(sess.user_id)
        if u and u.enabled:
            return u

    if mode == "session" and request.method in {"GET", "HEAD", "OPTIONS"}:
        return _ANON_ADMIN  # graceful read

    raise HTTPException(status_code=401, detail="Authentication required")


def csrf_check(request: Request, session: Session | None) -> None:
    """For state-changing methods in session/strict modes, the request
    must echo the CSRF token via X-CSRF-Token header. Bearer-token
    callers are exempt (they already proved out-of-band knowledge)."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if _bearer_matches(request):
        return
    mode = auth_mode()
    if mode == "open":
        return
    if not session:
        return  # no session → current_user will already have raised
    header = request.headers.get(CSRF_HEADER) or ""
    if not secrets.compare_digest(header, session.csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def require_role(min_role: str):
    """FastAPI dependency factory. `Depends(require_role("operator"))`.

    Combines current_user + role check + CSRF check."""
    if min_role not in ROLES:
        raise ValueError(f"unknown role: {min_role}")

    async def _dep(request: Request, user: User = Depends(current_user)) -> User:
        # Re-fetch session once for CSRF (current_user already used it
        # to identify the user; Session object isn't returned to keep
        # current_user's signature compact).
        cookie = request.cookies.get(SESSION_COOKIE) or ""
        sess = await get_session(cookie) if cookie else None
        csrf_check(request, sess)
        if not role_at_least(user.role, min_role):
            raise HTTPException(
                status_code=403,
                detail=f"Requires role={min_role} or higher (you are {user.role})",
            )
        return user
    return _dep


# Convenience dependencies — the most common shapes
async def require_viewer(user: User = Depends(current_user)) -> User:
    return user


require_operator = require_role("operator")
require_admin = require_role("admin")
