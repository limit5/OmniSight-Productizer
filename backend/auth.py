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
    must_change_password: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name,
                "role": self.role, "enabled": self.enabled,
                "must_change_password": self.must_change_password}


@dataclass
class Session:
    token: str
    user_id: str
    csrf_token: str
    created_at: float
    expires_at: float
    ip: str = ""
    user_agent: str = ""
    last_seen_at: float = 0.0
    metadata: str = "{}"
    mfa_verified: bool = False
    rotated_from: str | None = None


SESSION_TTL_S = 8 * 60 * 60          # 8 hours
ROTATION_GRACE_S = 30                # old token stays valid 30s after rotation
SESSION_COOKIE = "omnisight_session"
CSRF_COOKIE = "omnisight_csrf"
CSRF_HEADER = "X-CSRF-Token"


def compute_ua_hash(user_agent: str) -> str:
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.encode("utf-8", errors="replace")).hexdigest()[:32]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DB helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _conn():
    from backend import db
    return db._conn()


async def get_user(user_id: str) -> Optional[User]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, email, name, role, enabled, must_change_password FROM users WHERE id=?",
        (user_id,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None
    return User(id=r["id"], email=r["email"], name=r["name"],
                role=r["role"], enabled=bool(r["enabled"]),
                must_change_password=bool(r["must_change_password"]))


async def get_user_by_email(email: str) -> Optional[User]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, email, name, role, enabled, must_change_password FROM users WHERE email=?",
        (email.lower().strip(),),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None
    return User(id=r["id"], email=r["email"], name=r["name"],
                role=r["role"], enabled=bool(r["enabled"]),
                must_change_password=bool(r["must_change_password"]))


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


async def change_password(user_id: str, new_password: str) -> None:
    """Update a user's password and clear must_change_password flag."""
    conn = await _conn()
    pw_hash = hash_password(new_password)
    await conn.execute(
        "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
        (pw_hash, user_id),
    )
    await conn.commit()


LOCKOUT_BASE_S = 15 * 60
LOCKOUT_MAX_S = 24 * 60 * 60
LOCKOUT_THRESHOLD = 10


def _lockout_duration(failed_count: int) -> float:
    """Exponential backoff: 15 min × 2^(n - threshold), capped at 24 h."""
    exponent = max(0, failed_count - LOCKOUT_THRESHOLD)
    return min(LOCKOUT_BASE_S * (2 ** exponent), LOCKOUT_MAX_S)


async def _record_login_failure(conn, user_id: str, current_count: int) -> int:
    new_count = current_count + 1
    locked_until = None
    if new_count >= LOCKOUT_THRESHOLD:
        locked_until = time.time() + _lockout_duration(new_count)
    await conn.execute(
        "UPDATE users SET failed_login_count=?, locked_until=? WHERE id=?",
        (new_count, locked_until, user_id),
    )
    await conn.commit()
    return new_count


async def _reset_login_failures(conn, user_id: str) -> None:
    await conn.execute(
        "UPDATE users SET failed_login_count=0, locked_until=NULL WHERE id=?",
        (user_id,),
    )
    await conn.commit()


async def is_account_locked(email: str) -> tuple[bool, float]:
    """Check if account is locked. Returns (locked, retry_after_s)."""
    conn = await _conn()
    async with conn.execute(
        "SELECT locked_until FROM users WHERE email=?",
        (email.lower().strip(),),
    ) as cur:
        r = await cur.fetchone()
    if not r or r["locked_until"] is None:
        return False, 0.0
    remaining = r["locked_until"] - time.time()
    if remaining <= 0:
        return False, 0.0
    return True, remaining


async def authenticate_password(email: str, password: str) -> Optional[User]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, email, name, role, enabled, password_hash, "
        "must_change_password, failed_login_count, locked_until "
        "FROM users WHERE email=?", (email.lower().strip(),),
    ) as cur:
        r = await cur.fetchone()
    if not r or not r["enabled"]:
        return None

    locked_until = r["locked_until"]
    if locked_until is not None and locked_until > time.time():
        return None

    if locked_until is not None and locked_until <= time.time():
        await _reset_login_failures(conn, r["id"])

    if not verify_password(password, r["password_hash"]):
        await _record_login_failure(conn, r["id"], r["failed_login_count"])
        return None

    await _reset_login_failures(conn, r["id"])
    await conn.execute(
        "UPDATE users SET last_login_at=datetime('now') WHERE id=?",
        (r["id"],),
    )
    await conn.commit()
    return User(id=r["id"], email=r["email"], name=r["name"],
                role=r["role"], enabled=bool(r["enabled"]),
                must_change_password=bool(r["must_change_password"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Session management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def create_session(user_id: str, ip: str = "", user_agent: str = "") -> Session:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = time.time()
    expires = now + SESSION_TTL_S
    ua = (user_agent or "")[:240]
    ua_h = compute_ua_hash(ua)
    conn = await _conn()
    await conn.execute(
        "INSERT INTO sessions (token, user_id, csrf_token, created_at, "
        "expires_at, last_seen_at, ip, user_agent, ua_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (token, user_id, csrf, now, expires, now, ip, ua, ua_h),
    )
    await conn.commit()
    return Session(token=token, user_id=user_id, csrf_token=csrf,
                   created_at=now, expires_at=expires, ip=ip,
                   user_agent=ua, last_seen_at=now)


async def get_session(token: str) -> Optional[Session]:
    if not token:
        return None
    conn = await _conn()
    async with conn.execute(
        "SELECT token, user_id, csrf_token, created_at, expires_at, "
        "ip, user_agent, last_seen_at, metadata, mfa_verified, rotated_from "
        "FROM sessions WHERE token=?", (token,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return None
    if r["expires_at"] < time.time():
        await delete_session(token)
        return None
    now = time.time()
    await conn.execute(
        "UPDATE sessions SET last_seen_at=? WHERE token=?",
        (now, token),
    )
    await conn.commit()
    return Session(
        token=r["token"], user_id=r["user_id"], csrf_token=r["csrf_token"],
        created_at=r["created_at"], expires_at=r["expires_at"],
        ip=r["ip"] or "", user_agent=r["user_agent"] or "",
        last_seen_at=now, metadata=r["metadata"] or "{}",
        mfa_verified=bool(r["mfa_verified"]),
        rotated_from=r["rotated_from"],
    )


def get_session_metadata(session: "Session") -> dict:
    """Parse the session metadata JSON string into a dict."""
    import json
    try:
        return json.loads(session.metadata or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


async def update_session_metadata(token: str, updates: dict) -> dict:
    """Merge *updates* into the session's metadata JSON and persist."""
    import json
    if not token:
        return {}
    conn = await _conn()
    async with conn.execute(
        "SELECT metadata FROM sessions WHERE token=?", (token,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return {}
    try:
        meta = json.loads(r["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    meta.update(updates)
    dumped = json.dumps(meta)
    await conn.execute(
        "UPDATE sessions SET metadata=? WHERE token=?", (dumped, token),
    )
    await conn.commit()
    return meta


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


async def rotate_session(old_token: str, ip: str = "",
                         user_agent: str = "") -> tuple[Session, str]:
    """Create a new session for the same user, mark the old token as
    rotated (``rotated_from`` → new token) and let it live for a 30-s
    grace window so in-flight requests finish.

    Returns ``(new_session, old_token)``.
    """
    old = await get_session(old_token)
    if not old:
        raise ValueError("session not found or expired")
    new_sess = await create_session(old.user_id, ip=ip, user_agent=user_agent)
    conn = await _conn()
    grace_expires = time.time() + ROTATION_GRACE_S
    await conn.execute(
        "UPDATE sessions SET rotated_from=?, expires_at=? WHERE token=?",
        (new_sess.token, grace_expires, old_token),
    )
    await conn.commit()
    logger.info("[AUTH] Session rotated for user %s (grace %ds)",
                old.user_id, ROTATION_GRACE_S)
    return new_sess, old_token


async def rotate_user_sessions(user_id: str, exclude_token: str | None = None) -> int:
    """Expire all sessions for *user_id* (except *exclude_token*) with
    a 30-s grace window. Used when a user's role changes."""
    conn = await _conn()
    now = time.time()
    grace = now + ROTATION_GRACE_S
    if exclude_token:
        cur = await conn.execute(
            "UPDATE sessions SET expires_at=? "
            "WHERE user_id=? AND token!=? AND expires_at>?",
            (grace, user_id, exclude_token, now),
        )
    else:
        cur = await conn.execute(
            "UPDATE sessions SET expires_at=? "
            "WHERE user_id=? AND expires_at>?",
            (grace, user_id, now),
        )
    await conn.commit()
    return cur.rowcount or 0


async def check_ua_binding(session: Session, current_ua: str) -> bool:
    """Compare the stored UA hash with the current request's UA.
    Returns True if matched, False if mismatched (caller should log warning)."""
    if not session.user_agent and not current_ua:
        return True
    stored_hash = compute_ua_hash(session.user_agent)
    current_hash = compute_ua_hash((current_ua or "")[:240])
    if not stored_hash or not current_hash:
        return True
    return secrets.compare_digest(stored_hash, current_hash)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bootstrap default admin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def ensure_default_admin() -> Optional[User]:
    """Create a default admin user if the users table is empty.
    Email + password come from env (or sensible dev defaults).
    If the password is the well-known default, flag must_change_password."""
    conn = await _conn()
    async with conn.execute("SELECT COUNT(*) AS n FROM users") as cur:
        r = await cur.fetchone()
    if r and (r["n"] or 0) > 0:
        return None
    email = (os.environ.get("OMNISIGHT_ADMIN_EMAIL") or "admin@omnisight.local").strip()
    password = (os.environ.get("OMNISIGHT_ADMIN_PASSWORD") or "omnisight-admin").strip()
    is_default_pw = password == "omnisight-admin"
    user = await create_user(
        email=email, name="OmniSight Admin", role="admin", password=password,
    )
    if is_default_pw:
        await conn.execute(
            "UPDATE users SET must_change_password=1 WHERE id=?", (user.id,),
        )
        await conn.commit()
        user.must_change_password = True
        logger.warning(
            "[AUTH] Default admin %s created with default password — "
            "must_change_password enforced. All API calls will return "
            "428 until password is changed via POST /auth/change-password.",
            email,
        )
    else:
        logger.warning(
            "[AUTH] Created default admin: %s",
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
        request.state.session = None
        return _ANON_ADMIN

    if _bearer_matches(request):
        bearer_raw = (request.headers.get("authorization") or "")[len("Bearer "):]
        fp = hashlib.sha256(bearer_raw.encode()).hexdigest()[:12]
        request.state.session = Session(
            token=f"bearer:{fp}", user_id="anonymous",
            csrf_token="", created_at=0, expires_at=0,
        )
        return _ANON_ADMIN

    cookie = request.cookies.get(SESSION_COOKIE) or ""
    sess = await get_session(cookie) if cookie else None
    if sess:
        u = await get_user(sess.user_id)
        if u and u.enabled:
            request.state.session = sess
            current_ua = request.headers.get("user-agent", "")
            if not await check_ua_binding(sess, current_ua):
                try:
                    from backend import audit as _audit
                    await _audit.log(
                        action="ua_mismatch_warning",
                        entity_kind="session",
                        entity_id=session_id_from_token(sess.token),
                        before={"stored_ua": sess.user_agent[:80]},
                        after={"current_ua": (current_ua or "")[:80]},
                        actor=u.email,
                        session_id=sess.token,
                    )
                except Exception:
                    pass
                logger.warning(
                    "[AUTH] UA mismatch for user %s session %s",
                    u.email, session_id_from_token(sess.token),
                )
            return u

    if mode == "session" and request.method in {"GET", "HEAD", "OPTIONS"}:
        request.state.session = None
        return _ANON_ADMIN

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Session listing / revocation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return "***"
    return token[:4] + "***" + token[-4:]


def session_id_from_token(token: str) -> str:
    """Derive a stable, non-reversible session_id from the session token.
    Used to tag SSE events so the frontend can filter by originating session."""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:16]


async def list_sessions(user_id: str) -> list[dict]:
    conn = await _conn()
    async with conn.execute(
        "SELECT token, user_id, created_at, expires_at, last_seen_at, "
        "ip, user_agent, metadata, mfa_verified "
        "FROM sessions WHERE user_id=? AND expires_at > ? "
        "ORDER BY last_seen_at DESC",
        (user_id, time.time()),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "token_hint": _mask_token(r["token"]),
            "token": r["token"],
            "user_id": r["user_id"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "last_seen_at": r["last_seen_at"],
            "ip": r["ip"],
            "user_agent": r["user_agent"],
            "metadata": r["metadata"],
            "mfa_verified": bool(r["mfa_verified"]),
        }
        for r in rows
    ]


async def revoke_session(token: str) -> bool:
    conn = await _conn()
    cur = await conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def revoke_other_sessions(user_id: str, keep_token: str) -> int:
    conn = await _conn()
    cur = await conn.execute(
        "DELETE FROM sessions WHERE user_id=? AND token!=?",
        (user_id, keep_token),
    )
    await conn.commit()
    return cur.rowcount or 0
