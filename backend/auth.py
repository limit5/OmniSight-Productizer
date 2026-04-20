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
#  Password hashing — Argon2id (primary) with PBKDF2-SHA256 legacy support.
#  New hashes always use Argon2id. Old PBKDF2 hashes are auto-rehashed
#  to Argon2id on successful verification.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import VerifyMismatchError as _Argon2Mismatch

_argon2_ph = _Argon2Hasher()

_PBKDF_ITERS = 320_000

PASSWORD_HISTORY_LIMIT = 5
PASSWORD_MIN_LENGTH = 12
PASSWORD_MIN_ZXCVBN = 3


def hash_password(plain: str) -> str:
    return _argon2_ph.hash(plain)


# M1 audit (2026-04-19): constant-time sentinel for login-path
# timing-oracle defence. Computed once at module import so
# `authenticate_password` can feed it to verify_password on the
# "user not found" branch — the argon2 verify burns roughly the
# same wall-clock the valid-user path would, stopping an adversary
# from enumerating live emails by response-time signal alone. The
# plaintext never resolves to any real user (random token), so
# even if a caller accidentally sends this hash to _verify_pbkdf2
# it can't unlock anything.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def _verify_pbkdf2(plain: str, stored: str) -> bool:
    try:
        _, iters_s, salt_hex, digest_hex = stored.split("$", 3)
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        want = bytes.fromhex(digest_hex)
        got = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters)
        return secrets.compare_digest(got, want)
    except Exception:
        return False


def verify_password(plain: str, stored: str) -> bool:
    if stored.startswith("$argon2"):
        try:
            return _argon2_ph.verify(stored, plain)
        except _Argon2Mismatch:
            return False
        except Exception:
            return False
    if stored.startswith("pbkdf2_sha256$"):
        return _verify_pbkdf2(plain, stored)
    return False


def needs_rehash(stored: str) -> bool:
    if stored.startswith("pbkdf2_sha256$"):
        return True
    if stored.startswith("$argon2"):
        return _argon2_ph.check_needs_rehash(stored)
    return False


def validate_password_strength(plain: str) -> str | None:
    """Return an error message if the password is too weak, else None."""
    if len(plain) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
    from zxcvbn import zxcvbn
    result = zxcvbn(plain)
    if result["score"] < PASSWORD_MIN_ZXCVBN:
        feedback = result.get("feedback", {})
        warning = feedback.get("warning", "")
        suggestions = feedback.get("suggestions", [])
        msg = "Password is too weak"
        if warning:
            msg += f": {warning}"
        if suggestions:
            msg += ". " + " ".join(suggestions)
        return msg
    return None


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
    tenant_id: str = "t-default"

    def to_dict(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name,
                "role": self.role, "enabled": self.enabled,
                "must_change_password": self.must_change_password,
                "tenant_id": self.tenant_id}


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


def _row_to_user(r) -> User:
    """Map a users-table row to the User dataclass.

    Phase-3-Runtime-v2 SP-4.2 (2026-04-20): extracted so the 4 user
    reads share one mapping — previously each inlined the same
    ``User(id=..., email=..., ...)`` construction with slightly
    different formatting. Centralising prevents drift when a new
    column lands.
    """
    return User(
        id=r["id"],
        email=r["email"],
        name=r["name"],
        role=r["role"],
        enabled=bool(r["enabled"]),
        must_change_password=bool(r["must_change_password"]),
        tenant_id=r["tenant_id"],
    )


_USER_COLS = (
    "id, email, name, role, enabled, must_change_password, tenant_id"
)


async def _get_user_impl(conn, user_id: str) -> Optional[User]:
    r = await conn.fetchrow(
        f"SELECT {_USER_COLS} FROM users WHERE id = $1", user_id,
    )
    return _row_to_user(r) if r else None


async def get_user(user_id: str, conn=None) -> Optional[User]:
    """Look up a user by id. Returns None if not found.

    Phase-3-Runtime-v2 SP-4.2 (2026-04-20): polymorphic ``conn`` — the
    10+ existing callers (main.py lifespan, routers/bootstrap.py,
    routers/mfa.py, tests) keep their call-sites unchanged; they
    pass nothing and this function borrows a pool conn for the
    single read. Request handlers that want to share a conn across
    ``get_user`` + later writes can pass their Depends conn.
    """
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _get_user_impl(owned_conn, user_id)
    return await _get_user_impl(conn, user_id)


async def _get_user_by_email_impl(conn, email: str) -> Optional[User]:
    r = await conn.fetchrow(
        f"SELECT {_USER_COLS} FROM users WHERE email = $1",
        email.lower().strip(),
    )
    return _row_to_user(r) if r else None


async def get_user_by_email(email: str, conn=None) -> Optional[User]:
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _get_user_by_email_impl(owned_conn, email)
    return await _get_user_by_email_impl(conn, email)


async def find_admin_requiring_password_change(conn=None) -> Optional[User]:
    """Return the single enabled admin still flagged must_change_password.

    Drives L2 Step 1 of the bootstrap wizard: the operator hasn't logged
    in yet (no session), so the wizard endpoint needs to identify which
    admin row still carries the shipping credential without trusting
    client-supplied identity. If multiple admins share the flag we pick
    the oldest (first created) — practically only one exists during
    bootstrap since ``ensure_default_admin`` only runs on an empty table.

    SP-4.2 (2026-04-20): SQLite's implicit ``rowid`` doesn't exist in
    PG. Replaced ``ORDER BY rowid ASC`` with ``ORDER BY created_at ASC``
    — the schema stores created_at as ``TEXT NOT NULL DEFAULT (datetime('now'))``
    in ``YYYY-MM-DD HH:MM:SS`` format, which is lexicographically
    chronological; oldest admin = smallest timestamp.
    """
    sql = (
        f"SELECT {_USER_COLS} FROM users "
        "WHERE role = 'admin' AND enabled = 1 AND must_change_password = 1 "
        "ORDER BY created_at ASC LIMIT 1"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            r = await owned_conn.fetchrow(sql)
    else:
        r = await conn.fetchrow(sql)
    return _row_to_user(r) if r else None


async def _create_user_impl(
    conn,
    email: str,
    name: str,
    role: str,
    password: str | None,
    oidc_provider: str,
    oidc_subject: str,
    tenant_id: str,
) -> User:
    from backend.db_context import tenant_insert_value
    tid = tenant_id or tenant_insert_value()
    uid = f"u-{uuid.uuid4().hex[:10]}"
    pw_hash = hash_password(password) if password else ""
    norm_email = email.lower().strip()
    await conn.execute(
        "INSERT INTO users (id, email, name, role, password_hash, "
        " oidc_provider, oidc_subject, enabled, tenant_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8)",
        uid, norm_email, name, role, pw_hash,
        oidc_provider, oidc_subject, tid,
    )
    return User(id=uid, email=norm_email, name=name, role=role, tenant_id=tid)


async def create_user(
    email: str,
    name: str,
    role: str = "viewer",
    password: str | None = None,
    oidc_provider: str = "",
    oidc_subject: str = "",
    tenant_id: str = "",
    conn=None,
) -> User:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _create_user_impl(
                owned_conn, email, name, role, password,
                oidc_provider, oidc_subject, tenant_id,
            )
    return await _create_user_impl(
        conn, email, name, role, password,
        oidc_provider, oidc_subject, tenant_id,
    )


async def check_password_history(user_id: str, plain: str) -> bool:
    """Return True if the password matches any of the last N stored hashes."""
    conn = await _conn()
    async with conn.execute(
        "SELECT password_hash FROM password_history "
        "WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, PASSWORD_HISTORY_LIMIT),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        if verify_password(plain, row["password_hash"]):
            return True
    async with conn.execute(
        "SELECT password_hash FROM users WHERE id=?", (user_id,),
    ) as cur:
        r = await cur.fetchone()
    if r and r["password_hash"] and verify_password(plain, r["password_hash"]):
        return True
    return False


async def _record_password_history(conn, user_id: str, pw_hash: str) -> None:
    await conn.execute(
        "INSERT INTO password_history (user_id, password_hash) VALUES (?, ?)",
        (user_id, pw_hash),
    )
    await conn.execute(
        "DELETE FROM password_history WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM password_history WHERE user_id=? ORDER BY id DESC LIMIT ?)",
        (user_id, user_id, PASSWORD_HISTORY_LIMIT),
    )


async def change_password(user_id: str, new_password: str) -> None:
    """Update a user's password and clear must_change_password flag.
    Records the old hash in password_history for reuse prevention."""
    conn = await _conn()
    async with conn.execute(
        "SELECT password_hash FROM users WHERE id=?", (user_id,),
    ) as cur:
        r = await cur.fetchone()
    if r and r["password_hash"]:
        await _record_password_history(conn, user_id, r["password_hash"])
    pw_hash = hash_password(new_password)
    await conn.execute(
        "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
        (pw_hash, user_id),
    )
    await conn.commit()


async def flag_all_admins_must_change_password() -> list[dict]:
    """Re-flag every enabled admin row with ``must_change_password=1``.

    Used by L8 ``POST /bootstrap/reset`` to put the install back into
    its first-boot state for QA. We don't try to restore the original
    ``omnisight-admin`` plaintext (we never stored it) — flipping the
    flag is enough to drive ``find_admin_requiring_password_change()``
    + the K1 428 gate, which is what the L2 wizard step probes.

    Returns the list of ``{id, email}`` dicts that were re-flagged so
    the caller can include them in the audit row. Disabled admins are
    skipped — re-flagging a disabled row would force a password change
    on an account no one can log into.
    """
    conn = await _conn()
    async with conn.execute(
        "SELECT id, email FROM users WHERE role='admin' AND enabled=1",
    ) as cur:
        rows = await cur.fetchall()
    flagged: list[dict] = []
    for r in rows:
        await conn.execute(
            "UPDATE users SET must_change_password=1 WHERE id=?",
            (r["id"],),
        )
        flagged.append({"id": r["id"], "email": r["email"]})
    if flagged:
        await conn.commit()
    return flagged


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

    # M1 audit (2026-04-19): ALWAYS run argon2 verify, even when the
    # user doesn't exist / is disabled / is locked, so the login-
    # response time is constant regardless of outcome. Three previous
    # states leaked via timing:
    #   • non-existent email    ~5 ms   (DB miss, no verify)
    #   • valid-user + locked   ~5 ms   (lockout check, no verify)
    #   • valid-user + wrong pw ~100 ms (argon2 verify)
    # Attacker could enumerate emails + sniff lockout state.
    # Now every path runs verify(dummy or real) before branching, so
    # the wall-clock is argon2-bound uniformly. Keep this structure
    # — do NOT reintroduce early returns above the verify call.
    have_row = bool(r and r["enabled"])
    target_hash = r["password_hash"] if have_row else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, target_hash)

    if not have_row:
        return None

    locked_until = r["locked_until"]
    if locked_until is not None and locked_until > time.time():
        return None
    if locked_until is not None and locked_until <= time.time():
        await _reset_login_failures(conn, r["id"])

    if not password_ok:
        await _record_login_failure(conn, r["id"], r["failed_login_count"])
        return None

    if needs_rehash(r["password_hash"]):
        new_hash = hash_password(password)
        await conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (new_hash, r["id"]),
        )
        logger.info("[AUTH] Auto-rehashed password to argon2id for user %s", r["email"])

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


async def _create_session_impl(
    conn, user_id: str, ip: str, user_agent: str,
) -> Session:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = time.time()
    expires = now + SESSION_TTL_S
    ua = (user_agent or "")[:240]
    ua_h = compute_ua_hash(ua)
    await conn.execute(
        "INSERT INTO sessions (token, user_id, csrf_token, created_at, "
        "expires_at, last_seen_at, ip, user_agent, ua_hash) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        token, user_id, csrf, now, expires, now, ip, ua, ua_h,
    )
    return Session(token=token, user_id=user_id, csrf_token=csrf,
                   created_at=now, expires_at=expires, ip=ip,
                   user_agent=ua, last_seen_at=now)


async def create_session(
    user_id: str, ip: str = "", user_agent: str = "", conn=None,
) -> Session:
    """Issue a new session token for *user_id*.

    Phase-3-Runtime-v2 SP-4.3a (2026-04-20): polymorphic ``conn`` —
    login / refresh routes that already hold a ``Depends(get_conn)``
    can share it; background / worker paths (rotate_session calls
    this internally in SP-4.3b) pass nothing and borrow from pool.
    """
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _create_session_impl(
                owned_conn, user_id, ip, user_agent,
            )
    return await _create_session_impl(conn, user_id, ip, user_agent)


# Minimum age (seconds) a ``last_seen_at`` value must have before
# ``get_session`` bothers writing a fresh one. Every request on a
# dashboard fires ~20 concurrent XHRs, every XHR lands in at least
# two middlewares that call ``get_session`` for the cookie, and each
# caddy replica forwards to one of two backends — so a naive
# "update last_seen on every lookup" pattern issues ~40 UPDATEs per
# page load per user, all against the same row, on the same WAL.
# SQLite's single-writer lock can't keep up and returns ``database
# is locked`` after ``PRAGMA busy_timeout=5000``, which the upstream
# auth middleware then fail-closes on → spurious 401 → operator
# bounced to /login. Throttling to once-per-minute-ish is plenty for
# the "active sessions" UI at ``/auth/sessions`` (its precision is
# human-scale anyway) and it eliminates the write storm.
_SESSION_LAST_SEEN_MIN_AGE_S = 60.0


async def _get_session_impl(conn, token: str) -> Optional[Session]:
    r = await conn.fetchrow(
        "SELECT token, user_id, csrf_token, created_at, expires_at, "
        "ip, user_agent, last_seen_at, metadata, mfa_verified, "
        "rotated_from FROM sessions WHERE token = $1",
        token,
    )
    if not r:
        return None
    if r["expires_at"] < time.time():
        # Expired → evict + treat as not-found. Reuse the conn we
        # already hold; no separate DELETE tx needed.
        await conn.execute(
            "DELETE FROM sessions WHERE token = $1", token,
        )
        return None
    now = time.time()
    stored_last_seen = r["last_seen_at"] or 0.0
    effective_last_seen = stored_last_seen
    # Skip the write when the stored value is recent — see the
    # _SESSION_LAST_SEEN_MIN_AGE_S comment above for why throttling
    # matters (was SQLite WAL contention; on PG it still saves hot-row
    # contention under dashboard fan-out).
    if now - stored_last_seen >= _SESSION_LAST_SEEN_MIN_AGE_S:
        try:
            await conn.execute(
                "UPDATE sessions SET last_seen_at = $1 WHERE token = $2",
                now, token,
            )
            effective_last_seen = now
        except Exception as exc:
            # last_seen_at is UI-only; swallow transient lock errors.
            logger.debug(
                "auth.get_session: last_seen_at update skipped "
                "(non-fatal) token_tail=%s err=%s",
                token[-6:], exc,
            )
    return Session(
        token=r["token"], user_id=r["user_id"], csrf_token=r["csrf_token"],
        created_at=r["created_at"], expires_at=r["expires_at"],
        ip=r["ip"] or "", user_agent=r["user_agent"] or "",
        last_seen_at=effective_last_seen, metadata=r["metadata"] or "{}",
        mfa_verified=bool(r["mfa_verified"]),
        rotated_from=r["rotated_from"],
    )


async def get_session(token: str, conn=None) -> Optional[Session]:
    """Look up a session by token. Hottest function on the auth hot
    path — every authenticated request goes through here.

    SP-4.3a (2026-04-20): polymorphic ``conn``. Existing callers
    (auth.current_user middleware, routers/auth.py, decision_engine,
    auth_baseline) call without conn and this function borrows one
    from the pool for the read + conditional last_seen_at UPDATE.
    """
    if not token:
        return None
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _get_session_impl(owned_conn, token)
    return await _get_session_impl(conn, token)


def get_session_metadata(session: "Session") -> dict:
    """Parse the session metadata JSON string into a dict."""
    import json
    try:
        return json.loads(session.metadata or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


async def _update_session_metadata_impl(
    conn, token: str, updates: dict,
) -> dict:
    import json
    # SELECT FOR UPDATE holds a row-level lock for the duration of
    # the tx — concurrent metadata updates on the same session
    # serialise on the lock instead of racing on the read.
    # Without it, two updates can both see the same baseline,
    # compute different merged dicts, and the later UPDATE clobbers
    # the earlier one.
    r = await conn.fetchrow(
        "SELECT metadata FROM sessions WHERE token = $1 FOR UPDATE",
        token,
    )
    if not r:
        return {}
    try:
        meta = json.loads(r["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    meta.update(updates)
    dumped = json.dumps(meta)
    await conn.execute(
        "UPDATE sessions SET metadata = $1 WHERE token = $2",
        dumped, token,
    )
    return meta


async def update_session_metadata(
    token: str, updates: dict, conn=None,
) -> dict:
    """Merge *updates* into the session's metadata JSON and persist.

    SP-4.3b: SELECT FOR UPDATE + transaction prevents lost updates
    under concurrent writes to the same session's metadata. The
    ``last_write_wins`` semantic of the old code was acceptable under
    SQLite (one writer at a time) but not under pool concurrency.
    """
    if not token:
        return {}
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            async with owned_conn.transaction():
                return await _update_session_metadata_impl(
                    owned_conn, token, updates,
                )
    async with conn.transaction():
        return await _update_session_metadata_impl(conn, token, updates)


async def delete_session(token: str, conn=None) -> None:
    if not token:
        return
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            await owned_conn.execute(
                "DELETE FROM sessions WHERE token = $1", token,
            )
        return
    await conn.execute("DELETE FROM sessions WHERE token = $1", token)


async def cleanup_expired_sessions(conn=None) -> int:
    cutoff = time.time()
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            status = await owned_conn.execute(
                "DELETE FROM sessions WHERE expires_at < $1", cutoff,
            )
    else:
        status = await conn.execute(
            "DELETE FROM sessions WHERE expires_at < $1", cutoff,
        )
    # asyncpg returns "DELETE <n>" status string.
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


async def _rotate_session_impl(
    conn, old_token: str, ip: str, user_agent: str,
) -> tuple[Session, str]:
    # Advisory lock scoped to this token — prevents token-explosion
    # race when two callers concurrently rotate the same old_token
    # (without it, both would read the same row, both create_session,
    # both mark rotated_from → two new sessions for one logical
    # rotation). The lock is tx-scoped: released on COMMIT/ROLLBACK.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"rotate-session-{old_token}",
    )
    # Re-read the old session INSIDE the lock so we see any prior
    # rotation that won the race. get_session's throttled last_seen
    # update is fine to piggy-back on.
    old = await _get_session_impl(conn, old_token)
    if not old:
        raise ValueError("session not found or expired")
    # If the old token already has rotated_from set, a prior rotation
    # won — return that rotation's target instead of creating another.
    if old.rotated_from:
        existing = await _get_session_impl(conn, old.rotated_from)
        if existing is not None:
            return existing, old_token
    new_sess = await _create_session_impl(conn, old.user_id, ip, user_agent)
    grace_expires = time.time() + ROTATION_GRACE_S
    await conn.execute(
        "UPDATE sessions SET rotated_from = $1, expires_at = $2 "
        "WHERE token = $3",
        new_sess.token, grace_expires, old_token,
    )
    logger.info(
        "[AUTH] Session rotated for user %s (grace %ds)",
        old.user_id, ROTATION_GRACE_S,
    )
    return new_sess, old_token


async def rotate_session(
    old_token: str, ip: str = "", user_agent: str = "", conn=None,
) -> tuple[Session, str]:
    """Create a new session for the same user, mark the old token as
    rotated (``rotated_from`` → new token) and let it live for a 30-s
    grace window so in-flight requests finish.

    Returns ``(new_session, old_token)``.

    Phase-3-Runtime-v2 SP-4.3b (2026-04-20): ported to native asyncpg.
    Now serialises concurrent rotations of the same ``old_token`` via
    ``pg_advisory_xact_lock`` (same recipe as SP-4.1 audit.log's
    per-tenant chain lock). Without this, two callers could both
    observe the un-rotated old session and each create a new session
    — the user ends up with two new tokens for one logical rotation,
    and the winner's rotated_from pointer overwrites the other's.
    """
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            async with owned_conn.transaction():
                return await _rotate_session_impl(
                    owned_conn, old_token, ip, user_agent,
                )
    async with conn.transaction():
        return await _rotate_session_impl(conn, old_token, ip, user_agent)


async def _rotate_user_sessions_impl(
    conn, user_id: str, exclude_token: str | None,
) -> int:
    now = time.time()
    grace = now + ROTATION_GRACE_S
    if exclude_token:
        status = await conn.execute(
            "UPDATE sessions SET expires_at = $1 "
            "WHERE user_id = $2 AND token != $3 AND expires_at > $4",
            grace, user_id, exclude_token, now,
        )
    else:
        status = await conn.execute(
            "UPDATE sessions SET expires_at = $1 "
            "WHERE user_id = $2 AND expires_at > $3",
            grace, user_id, now,
        )
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


async def rotate_user_sessions(
    user_id: str, exclude_token: str | None = None, conn=None,
) -> int:
    """Expire all sessions for *user_id* (except *exclude_token*) with
    a 30-s grace window. Used when a user's role changes.

    SP-4.3b: the multi-row UPDATE does NOT advisory-lock — it's a
    bulk operation where ordering across concurrent callers doesn't
    matter (the resulting ``expires_at`` is always ``now + GRACE``,
    idempotent-ish under concurrent calls). A session created mid-
    rotation (between the SELECT inside UPDATE and COMMIT) may or
    may not be caught; admin "logout everything" doesn't promise to
    catch in-flight new sessions — documented.
    """
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _rotate_user_sessions_impl(
                owned_conn, user_id, exclude_token,
            )
    return await _rotate_user_sessions_impl(conn, user_id, exclude_token)


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


async def ensure_default_admin(conn=None) -> Optional[User]:
    """Create a default admin user if the users table is empty.
    Email + password come from env (or sensible dev defaults).
    If the password is the well-known default, flag must_change_password.

    SP-4.2 (2026-04-20): polymorphic conn. The empty-check + create
    + flag update all ride the same acquired conn when called without
    one — so in single-worker boot there's no TOCTOU window. Under
    multi-worker boot (uvicorn --workers N) two workers CAN both see
    the count-zero result; the second's create_user will hit the
    UNIQUE (email) constraint on the users table and raise, which
    main.py's lifespan try/except catches — net result is still
    exactly one admin row.
    """
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _ensure_default_admin_impl(owned_conn)
    return await _ensure_default_admin_impl(conn)


async def _ensure_default_admin_impl(conn) -> Optional[User]:
    n = await conn.fetchval("SELECT COUNT(*) FROM users")
    if n and int(n) > 0:
        return None
    email = (os.environ.get("OMNISIGHT_ADMIN_EMAIL") or "admin@omnisight.local").strip()
    password = (os.environ.get("OMNISIGHT_ADMIN_PASSWORD") or "omnisight-admin").strip()
    is_default_pw = password == "omnisight-admin"
    user = await _create_user_impl(
        conn, email=email, name="OmniSight Admin", role="admin",
        password=password, oidc_provider="", oidc_subject="", tenant_id="",
    )
    if is_default_pw:
        await conn.execute(
            "UPDATE users SET must_change_password = 1 WHERE id = $1",
            user.id,
        )
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


def _extract_bearer(req: Request) -> str:
    """Extract the raw bearer token from the Authorization header."""
    h = req.headers.get("authorization") or ""
    if h.startswith("Bearer "):
        return h[len("Bearer "):]
    return ""


async def _validate_api_key(req: Request) -> "ApiKey | None":  # noqa: F821
    """Check if the request carries a valid per-key bearer token (K6).
    Falls back to legacy OMNISIGHT_DECISION_BEARER env for backwards compat
    during migration window."""
    raw = _extract_bearer(req)
    if not raw:
        return None
    from backend import api_keys
    ip = req.client.host if req.client else ""
    key = await api_keys.validate_bearer(raw, ip=ip)
    if key:
        return key
    expected = (os.environ.get("OMNISIGHT_DECISION_BEARER") or "").strip()
    if expected and secrets.compare_digest(raw, expected):
        logger.warning(
            "[AUTH] Request authenticated via legacy OMNISIGHT_DECISION_BEARER env. "
            "Migrate to per-key API tokens via Admin UI."
        )
        return None  # signal to caller: legacy match
    return None


_ANON_ADMIN = User(id="anonymous", email="anonymous@local", name="(anonymous)",
                   role="admin", enabled=True, tenant_id="t-default")


def _legacy_bearer_matches(req: Request) -> bool:
    """Backwards-compat check for the old single-env bearer."""
    expected = (os.environ.get("OMNISIGHT_DECISION_BEARER") or "").strip()
    if not expected:
        return False
    raw = _extract_bearer(req)
    return bool(raw) and secrets.compare_digest(raw, expected)


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

    raw = _extract_bearer(request)
    if raw:
        key = getattr(getattr(request, "state", None), "api_key", None)
        if not key:
            from backend import api_keys as _ak
            ip = request.client.host if request.client else ""
            key = await _ak.validate_bearer(raw, ip=ip)
        if key:
            request.state.session = Session(
                token=f"bearer:{key.id}", user_id=f"apikey:{key.id}",
                csrf_token="", created_at=0, expires_at=0,
            )
            request.state.api_key = key
            return User(id=f"apikey:{key.id}", email=f"apikey:{key.name}",
                        name=key.name, role="admin", enabled=True)
        if _legacy_bearer_matches(request):
            fp = hashlib.sha256(raw.encode()).hexdigest()[:12]
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
    if _extract_bearer(request):
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


# M4 audit (2026-04-19): per-user LLM-call rate limit (interim).
# Apply as a FastAPI dependency on every endpoint that triggers LLM
# work (/invoke, /invoke/stream, /chat, /chat/stream). Uses the
# existing I9 rate-limiter (Redis-backed in prod, in-memory fallback
# per replica) so no new infra needed. Full per-tenant token-dollar
# budget is a follow-up — this just stops "single user burns the
# global budget" in practice.
async def check_llm_quota(user: "User" = Depends(current_user)) -> "User":
    from backend.config import settings as _settings
    cap = int(getattr(_settings, "llm_calls_per_user_per_hour", 0) or 0)
    if cap <= 0:
        return user  # disabled
    from backend.rate_limit import get_limiter
    allowed, retry_after = get_limiter().allow(
        key=f"llm:user:{user.id}",
        capacity=cap,
        window_seconds=3600.0,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"per-user LLM quota exceeded ({cap}/h); "
                f"retry in {int(retry_after)}s"
            ),
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )
    return user


# Convenience dependencies — the most common shapes
async def require_viewer(user: User = Depends(current_user)) -> User:
    return user


require_operator = require_role("operator")
require_admin = require_role("admin")


async def require_tenant(user: User = Depends(current_user)) -> User:
    """Set the request-scoped tenant context from the authenticated user."""
    from backend.db_context import set_tenant_id
    set_tenant_id(user.tenant_id)
    return user


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


async def _list_sessions_impl(conn, user_id: str) -> list[dict]:
    rows = await conn.fetch(
        "SELECT token, user_id, created_at, expires_at, last_seen_at, "
        "ip, user_agent, metadata, mfa_verified "
        "FROM sessions WHERE user_id = $1 AND expires_at > $2 "
        "ORDER BY last_seen_at DESC",
        user_id, time.time(),
    )
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


async def list_sessions(user_id: str, conn=None) -> list[dict]:
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _list_sessions_impl(owned_conn, user_id)
    return await _list_sessions_impl(conn, user_id)


async def revoke_session(token: str, conn=None) -> bool:
    sql = "DELETE FROM sessions WHERE token = $1"
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            status = await owned_conn.execute(sql, token)
    else:
        status = await conn.execute(sql, token)
    try:
        return int(status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False


async def revoke_other_sessions(
    user_id: str, keep_token: str, conn=None,
) -> int:
    sql = "DELETE FROM sessions WHERE user_id = $1 AND token != $2"
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            status = await owned_conn.execute(sql, user_id, keep_token)
    else:
        status = await conn.execute(sql, user_id, keep_token)
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0
