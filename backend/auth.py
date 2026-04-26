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

# Y2 (#278) 2026-04-25: ``super_admin`` is the platform-operator tier
# above tenant ``admin``. Introduced for the Y2 admin-REST API row 1
# (POST /api/v1/admin/tenants) — only super-admins may create / mutate
# tenants. The role is purely additive: rank is computed from the tuple
# index so existing callers (``role_at_least(have, "admin")``) keep
# working unchanged. Y3 (#279) will own the user-facing super-admin
# bootstrap (POST /admin/super-admins); Y2 only needs the dependency
# in place.
ROLES = ("viewer", "operator", "admin", "super_admin")
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
    # Q.2 (2026-04-24): only meaningful on the Session instance returned
    # by ``create_session`` / ``rotate_session`` — i.e. the exact moment
    # of issuance. Not persisted in the ``sessions`` table (it's a
    # boundary decision, not a row property) and always defaults to
    # False for sessions reconstructed from the DB by ``get_session``.
    is_new_device: bool = False


SESSION_TTL_S = 8 * 60 * 60          # 8 hours
ROTATION_GRACE_S = 30                # old token stays valid 30s after rotation
SESSION_COOKIE = "omnisight_session"
CSRF_COOKIE = "omnisight_csrf"
CSRF_HEADER = "X-CSRF-Token"


def compute_ua_hash(user_agent: str) -> str:
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.encode("utf-8", errors="replace")).hexdigest()[:32]


# Q.2 device-fingerprint lookback (2026-04-24): a (user_id, ua_hash,
# ip_subnet) tuple that has not been observed within this window is
# treated as a "new device" by ``_create_session_impl`` — the returned
# Session carries ``is_new_device=True`` so the Q.2 downstream alert
# pipeline (email + SSE ``security.new_device_login``) can fire.
# 30 days chosen to align with the Q.2 spec ("is the tuple seen in the
# past 30 days"); kept as a module constant so tests can monkeypatch.
FINGERPRINT_LOOKBACK_S = 30 * 24 * 3600.0


# Q.2 new-device-alert gates (2026-04-24):
#
# `NEW_DEVICE_ALERT_USER_WINDOW_S` — per-user anti-spam. After a user
# receives a new-device alert, they won't receive another for this
# many seconds even if the second login is from a different subnet.
# Bounds burst from an attacker probing many IPs / UAs in quick
# succession. Spec: "同 user 每分鐘最多發一則新裝置通知（防 spam）".
#
# `NEW_DEVICE_ALERT_SUBNET_WINDOW_S` — per-(user, subnet) DHCP-tolerance
# dedup. After a user is alerted about logging in from a given /24
# (IPv4) or /64 (IPv6), any further first-sightings from the same
# subnet within this window are silently swallowed — the user has
# effectively already been alerted about that physical network, and
# the UA-hash churn that triggered ``is_new_device=True`` a second
# time (e.g. browser update, Firefox → Chrome on same laptop) is not
# worth a second push. Spec: "同一 IP subnet 24h 內視為同裝置（容忍
# DHCP 抽換）".
#
# Both gates use the shared `backend.rate_limit` token-bucket primitive
# (capacity=1, window=<this constant>). Redis-backed in prod (shared
# across workers), in-memory fallback in dev. Single-shot semantics:
# ``get_limiter().allow()`` consumes one token and is an atomic
# check-and-consume — no peek API exists, so see the helper docstring
# for the gate-ordering rationale (per-user first, per-subnet second).
NEW_DEVICE_ALERT_USER_WINDOW_S = 60.0
NEW_DEVICE_ALERT_SUBNET_WINDOW_S = 24 * 3600.0


# Q.2 opt-out preference key (2026-04-24):
#
# `user_preferences.new_device_alerts` — per-user toggle that lets a
# user silence the new-device-login alert pipeline entirely. The table
# is SP-5.8; this is just a well-known key under it. Written via the
# existing ``PUT /user-preferences/new_device_alerts`` endpoint. Read
# fresh from PG on every alert dispatch — no module-local cache — so a
# user hitting the toggle from /settings/security sees the setting take
# effect on the very next login, matching security-UX expectations.
#
# Value convention: a lowercase-stripped value in
# ``NEW_DEVICE_ALERTS_FALSY_VALUES`` means "alerts OFF". Any other
# value (including the common "1" / "true" / "on", a present-but-empty
# string, or a missing row) means "alerts ON". This is deliberately
# opt-out — a flaky write or UI bug must NOT accidentally disable a
# security alert, and the fail-open philosophy mirrors the rest of
# ``notify_new_device_login`` (SSE / IM dispatch errors also fail open).
NEW_DEVICE_ALERTS_PREF_KEY = "new_device_alerts"
NEW_DEVICE_ALERTS_FALSY_VALUES = frozenset({"0", "false", "off", "no"})


def compute_ip_subnet(ip: str) -> str:
    """Collapse *ip* to the coarser network it belongs to.

    IPv4 → the ``/24`` network prefix as a dotted string (``"1.2.3"``).
    IPv6 → the ``/64`` prefix as a colon-separated hex string.
    Empty / malformed input → ``""``.

    The goal (per Q.2 spec) is to tolerate DHCP lease churn on the same
    physical network: a laptop whose ISP rotates its IP within the same
    /24 should NOT re-trigger the new-device alert. IPv6 uses /64
    because that's the canonical subnet unit under SLAAC — the
    interface-identifier low 64 bits change freely even on one NIC.

    Host-port inputs (``"1.2.3.4:5678"``) are tolerated — we strip the
    port before parsing. Bracketed IPv6 (``"[::1]:8080"``) is tolerated
    the same way. Anything we can't parse as an IP (hostname,
    connection-level empty) returns ``""``; all such sessions collapse
    into the same empty-subnet bucket, which is the conservative
    choice — we'd rather under-alert than spam on unparseable IPs.
    """
    if not ip:
        return ""
    import ipaddress
    s = ip.strip()
    # Strip port if present. IPv6 form uses brackets; IPv4 uses a
    # single colon.
    if s.startswith("["):
        end = s.find("]")
        if end > 0:
            s = s[1:end]
    elif s.count(":") == 1:
        s = s.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(s)
    except ValueError:
        return ""
    if isinstance(addr, ipaddress.IPv4Address):
        # "1.2.3.4" → "1.2.3" (first 3 octets = /24 prefix).
        return ".".join(str(addr).split(".")[:3])
    # IPv6: take the first 4 16-bit groups in exploded form (/64).
    exploded = addr.exploded  # e.g. "2001:0db8:85a3:0000:...:0001"
    groups = exploded.split(":")[:4]
    return ":".join(groups)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DB helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


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


async def _check_password_history_impl(
    conn, user_id: str, plain: str,
) -> bool:
    rows = await conn.fetch(
        "SELECT password_hash FROM password_history "
        "WHERE user_id = $1 ORDER BY id DESC LIMIT $2",
        user_id, PASSWORD_HISTORY_LIMIT,
    )
    for row in rows:
        if verify_password(plain, row["password_hash"]):
            return True
    r = await conn.fetchrow(
        "SELECT password_hash FROM users WHERE id = $1", user_id,
    )
    if r and r["password_hash"] and verify_password(plain, r["password_hash"]):
        return True
    return False


async def check_password_history(
    user_id: str, plain: str, conn=None,
) -> bool:
    """Return True if the password matches any of the last N stored hashes.

    SP-4.4 (2026-04-21): ported to native asyncpg. Polymorphic ``conn``
    so the router call-site (routers/auth.py POST /auth/change-password)
    stays unchanged. The argon2 verify loop runs outside the DB acquire
    only when conn is None — otherwise the caller's conn is held for
    the full loop (acceptable because callers sharing a conn are
    already inside a tx that logically spans the history check).
    """
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _check_password_history_impl(
                owned_conn, user_id, plain,
            )
    return await _check_password_history_impl(conn, user_id, plain)


async def _record_password_history(conn, user_id: str, pw_hash: str) -> None:
    """Append a hash to the user's password history and trim to N.

    Internal helper — always called from inside an outer transaction
    (change_password wraps it) so the INSERT + DELETE-trim land atomic.
    """
    await conn.execute(
        "INSERT INTO password_history (user_id, password_hash) "
        "VALUES ($1, $2)",
        user_id, pw_hash,
    )
    await conn.execute(
        "DELETE FROM password_history WHERE user_id = $1 AND id NOT IN "
        "(SELECT id FROM password_history WHERE user_id = $2 "
        "ORDER BY id DESC LIMIT $3)",
        user_id, user_id, PASSWORD_HISTORY_LIMIT,
    )


async def _change_password_impl(
    conn, user_id: str, new_password: str,
) -> None:
    r = await conn.fetchrow(
        "SELECT password_hash FROM users WHERE id = $1", user_id,
    )
    if r and r["password_hash"]:
        await _record_password_history(conn, user_id, r["password_hash"])
    pw_hash = hash_password(new_password)
    await conn.execute(
        "UPDATE users SET password_hash = $1, must_change_password = 0 "
        "WHERE id = $2",
        pw_hash, user_id,
    )


async def change_password(
    user_id: str, new_password: str, conn=None,
) -> None:
    """Update a user's password and clear must_change_password flag.
    Records the old hash in password_history for reuse prevention.

    SP-4.4 (2026-04-21): tx-wrapped. Under SQLite the three statements
    (SELECT old hash, INSERT into history, DELETE-trim, UPDATE users)
    rode the file-level lock so interleaving was impossible. Under the
    pool, two concurrent change_password calls on the same user could
    interleave — e.g. both read the same old_hash, both INSERT it into
    history (duplicate retention slot), then race the UPDATE. The
    transaction serialises them via the implicit row lock the UPDATE
    takes, keeping the retention window correct.
    """
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            async with owned_conn.transaction():
                await _change_password_impl(owned_conn, user_id, new_password)
        return
    async with conn.transaction():
        await _change_password_impl(conn, user_id, new_password)


async def flag_user_must_change_password(
    user_id: str, conn=None,
) -> bool:
    """Q.2 (#296) 「這不是我」cascade: set ``must_change_password=1`` for
    a single user so the K1 428 gate forces them onto the change-password
    flow on their next authenticated request.

    Separate from ``flag_all_admins_must_change_password`` (which is the
    bootstrap L8 reset path scoped to admins) because:
      * this is per-user, not role-scoped, and
      * the caller is a regular user invoking cascade on their own
        account — we must NOT restrict to role='admin'.

    Returns True iff exactly one row was updated (the target user
    existed and was enabled). A silent no-op on an unknown / disabled
    user mirrors the rest of the auth helpers (they treat a missing
    target as a non-error), and the cascade router already returns a
    404 before this is called for the unknown case.
    """
    sql = "UPDATE users SET must_change_password = 1 WHERE id = $1"
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            status = await owned_conn.execute(sql, user_id)
    else:
        status = await conn.execute(sql, user_id)
    try:
        return int(status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False


async def flag_all_admins_must_change_password(conn=None) -> list[dict]:
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

    SP-4.5 (2026-04-21): ported to native asyncpg. Also collapses the
    old SELECT-then-UPDATE-per-row loop into a single
    ``UPDATE ... RETURNING`` so the operation is atomic against a
    concurrent ``users`` mutation (e.g. a simultaneous ``PATCH /users/
    {id}`` that disables an admin between the SELECT and the UPDATE
    would, under the old code, have left that admin flagged despite
    being disabled at commit time). The filter reads the enabled-at-
    UPDATE-time state, which is the invariant we want.
    """
    sql = (
        "UPDATE users SET must_change_password = 1 "
        "WHERE role = 'admin' AND enabled = 1 "
        "RETURNING id, email"
    )
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            rows = await owned_conn.fetch(sql)
    else:
        rows = await conn.fetch(sql)
    return [{"id": r["id"], "email": r["email"]} for r in rows]


LOCKOUT_BASE_S = 15 * 60
LOCKOUT_MAX_S = 24 * 60 * 60
LOCKOUT_THRESHOLD = 10


def _lockout_duration(failed_count: int) -> float:
    """Exponential backoff: 15 min × 2^(n - threshold), capped at 24 h."""
    exponent = max(0, failed_count - LOCKOUT_THRESHOLD)
    return min(LOCKOUT_BASE_S * (2 ** exponent), LOCKOUT_MAX_S)


async def _record_login_failure(conn, user_id: str) -> int:
    """Increment failed_login_count atomically, set locked_until if over
    threshold. Returns the new count.

    SP-4.4 (2026-04-21): atomic ``col = col + 1 RETURNING`` — the old
    pattern (caller-supplied ``current_count`` + UPDATE to
    ``current_count + 1``) was a lost-update race under the pool. Two
    concurrent failed logins for the same account would both read
    ``failed_login_count = N`` and both write ``N + 1``; under SQLite's
    file-lock this couldn't interleave, under asyncpg it can. Now the
    increment happens inside the UPDATE itself, so the kernel serialises
    on the row lock — no pre-fetch, no lost updates.
    """
    row = await conn.fetchrow(
        "UPDATE users SET failed_login_count = failed_login_count + 1 "
        "WHERE id = $1 "
        "RETURNING failed_login_count",
        user_id,
    )
    if not row:
        return 0
    new_count = int(row["failed_login_count"])
    if new_count >= LOCKOUT_THRESHOLD:
        locked_until = time.time() + _lockout_duration(new_count)
        await conn.execute(
            "UPDATE users SET locked_until = $1 WHERE id = $2",
            locked_until, user_id,
        )
    return new_count


async def _reset_login_failures(conn, user_id: str) -> None:
    await conn.execute(
        "UPDATE users SET failed_login_count = 0, locked_until = NULL "
        "WHERE id = $1",
        user_id,
    )


async def is_account_locked(email: str, conn=None) -> tuple[bool, float]:
    """Check if account is locked. Returns (locked, retry_after_s).

    SP-4.4 (2026-04-21): polymorphic ``conn``. All existing callers pass
    no conn; the read borrows from pool. The lockout check is used
    upstream of ``authenticate_password`` as a fast-path by some routes,
    but the hot login path is now authenticate_password itself so this
    function is read-only (no need to coordinate the un-lock side
    effect; that happens inside authenticate_password when the clock
    has rolled past ``locked_until``).
    """
    sql = "SELECT locked_until FROM users WHERE email = $1"
    normalised = email.lower().strip()
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            r = await owned_conn.fetchrow(sql, normalised)
    else:
        r = await conn.fetchrow(sql, normalised)
    if not r or r["locked_until"] is None:
        return False, 0.0
    remaining = r["locked_until"] - time.time()
    if remaining <= 0:
        return False, 0.0
    return True, remaining


_AUTH_LOGIN_COLS = (
    "id, email, name, role, enabled, password_hash, "
    "must_change_password, failed_login_count, locked_until, tenant_id"
)


async def authenticate_password(email: str, password: str) -> Optional[User]:
    """Verify email + password, returning the User or None.

    SP-4.4 (2026-04-21): ported to native asyncpg. Key structural
    changes versus the SQLite version:

    * Pool connection is NOT held across the argon2 verify (~100 ms
      wall-clock per call — enough to starve a 20-slot pool under a
      brute-force burst). The read acquires briefly, the verify runs
      with no conn held, and any write-path conditionals re-acquire.
    * Each write-path branch (rehash, reset-failures + last_login
      bump, record-failure) runs inside its own short transaction.
      Mixing them into one outer tx would mean holding a conn across
      the verify; the tradeoff is that a crash mid-branch leaves the
      login "mostly succeeded but last_login not updated" — acceptable
      since last_login is UI-only.
    * ``_record_login_failure`` is now lost-update-safe (atomic
      increment) so the three-write-path structure doesn't need extra
      coordination.

    The M1 timing-oracle defence is preserved verbatim — every branch
    runs argon2 verify (dummy or real) before touching control flow.
    """
    from backend.db_pool import get_pool
    pool = get_pool()
    normalised = email.lower().strip()

    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            f"SELECT {_AUTH_LOGIN_COLS} FROM users WHERE email = $1",
            normalised,
        )

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
    now = time.time()
    if locked_until is not None and locked_until > now:
        return None
    if locked_until is not None and locked_until <= now:
        # Lockout has organically expired; clear the flag so the next
        # wrong password doesn't immediately re-enter lockout with a
        # stale count.
        async with pool.acquire() as conn:
            await _reset_login_failures(conn, r["id"])

    if not password_ok:
        async with pool.acquire() as conn:
            await _record_login_failure(conn, r["id"])
        return None

    # Password matched → rehash if needed, reset counters, bump
    # last_login. Tx-wrap these together so a crash partway through
    # doesn't leave the user with must_change_password cleared but
    # failed_login_count unreset or similar inconsistency.
    async with pool.acquire() as conn:
        async with conn.transaction():
            if needs_rehash(r["password_hash"]):
                new_hash = hash_password(password)
                await conn.execute(
                    "UPDATE users SET password_hash = $1 WHERE id = $2",
                    new_hash, r["id"],
                )
                logger.info(
                    "[AUTH] Auto-rehashed password to argon2id for user %s",
                    r["email"],
                )
            await _reset_login_failures(conn, r["id"])
            await conn.execute(
                "UPDATE users SET last_login_at = "
                "to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS') "
                "WHERE id = $1",
                r["id"],
            )
    return User(
        id=r["id"], email=r["email"], name=r["name"],
        role=r["role"], enabled=bool(r["enabled"]),
        must_change_password=bool(r["must_change_password"]),
        tenant_id=r["tenant_id"],
    )


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
    is_new_device = await _record_session_fingerprint(
        conn, user_id, ua_h, compute_ip_subnet(ip), now,
    )
    return Session(token=token, user_id=user_id, csrf_token=csrf,
                   created_at=now, expires_at=expires, ip=ip,
                   user_agent=ua, last_seen_at=now,
                   is_new_device=is_new_device)


async def _record_session_fingerprint(
    conn, user_id: str, ua_hash: str, ip_subnet: str, now: float,
) -> bool:
    """Check whether ``(user_id, ua_hash, ip_subnet)`` was observed in
    the past ``FINGERPRINT_LOOKBACK_S`` and upsert the history row.

    Returns True iff the tuple is "new" — i.e. the history table has
    no row for it, or the existing row's ``last_seen_at`` is stale
    (older than the lookback window). A stale row is treated as new
    so that a device that last logged in 31 days ago re-triggers the
    Q.2 alert flow — matches the spec's 30-day semantics.

    The upsert runs in the same transaction as the parent
    ``INSERT INTO sessions`` (we ride the caller's ``conn``), so from
    the fingerprint table's perspective the session row and its
    history entry land atomically — there's no window where a session
    exists without its fingerprint counterpart.

    Why a dedicated history table rather than scanning ``sessions`` +
    ``session_revocations`` (the Q.2 spec literally reads "查
    sessions + revoked_sessions 歷史"):

    * ``sessions`` rows are garbage-collected by
      ``cleanup_expired_sessions`` on cold boot and by
      ``_get_session_impl`` on expired-token probes. Anything older
      than the session TTL + cold-boot is gone, so a 30-day history
      scan on ``sessions`` would under-report.
    * ``session_revocations`` is keyed by token and carries no ip /
      ua_hash columns (it drives the Q.1 401-banner lookup and has
      no reason to). Adding them to piggy-back on Q.1 would pollute
      a table with a different lifecycle.

    The ``session_fingerprints`` table (alembic 0020) is the canonical
    store going forward: append-on-first-sight, upsert-on-every-hit.
    """
    row = await conn.fetchrow(
        "SELECT last_seen_at FROM session_fingerprints "
        "WHERE user_id = $1 AND ua_hash = $2 AND ip_subnet = $3",
        user_id, ua_hash, ip_subnet,
    )
    last_seen = float(row["last_seen_at"]) if row else 0.0
    is_new_device = (row is None) or (now - last_seen > FINGERPRINT_LOOKBACK_S)
    await conn.execute(
        "INSERT INTO session_fingerprints "
        "(user_id, ua_hash, ip_subnet, first_seen_at, "
        "last_seen_at, session_count) "
        "VALUES ($1, $2, $3, $4, $4, 1) "
        "ON CONFLICT (user_id, ua_hash, ip_subnet) DO UPDATE SET "
        "last_seen_at = EXCLUDED.last_seen_at, "
        "session_count = session_fingerprints.session_count + 1",
        user_id, ua_hash, ip_subnet, now,
    )
    return is_new_device


async def fingerprint_seen_before(
    user_id: str, ua_hash: str, ip_subnet: str,
    lookback_s: float | None = None, conn=None,
) -> bool:
    """Public probe: has ``(user_id, ua_hash, ip_subnet)`` been seen
    in the past ``lookback_s`` (default ``FINGERPRINT_LOOKBACK_S``)?

    Read-only helper split out for downstream Q.2 code (notification
    dispatcher, SSE emitter, tests) that needs the check without
    going through a full ``create_session``. Returns True if a
    matching row exists with ``last_seen_at`` inside the window;
    False otherwise (no row, or row too old).
    """
    window = lookback_s if lookback_s is not None else FINGERPRINT_LOOKBACK_S
    import time as _time
    cutoff = _time.time() - window
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            row = await owned_conn.fetchrow(
                "SELECT 1 FROM session_fingerprints "
                "WHERE user_id = $1 AND ua_hash = $2 AND ip_subnet = $3 "
                "AND last_seen_at >= $4",
                user_id, ua_hash, ip_subnet, cutoff,
            )
    else:
        row = await conn.fetchrow(
            "SELECT 1 FROM session_fingerprints "
            "WHERE user_id = $1 AND ua_hash = $2 AND ip_subnet = $3 "
            "AND last_seen_at >= $4",
            user_id, ua_hash, ip_subnet, cutoff,
        )
    return row is not None


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


async def _new_device_alerts_enabled(user_id: str) -> bool:
    """Read the Q.2 per-user opt-out preference from PG.

    Returns ``True`` (alerts enabled) when the row is missing, the value
    is not in ``NEW_DEVICE_ALERTS_FALSY_VALUES``, or the read itself
    fails. The fail-open branch is intentional: a transient DB hiccup
    (pool exhaustion, PG restart mid-login) must NOT silently suppress
    a security alert — a missed "someone else is logging in" message
    is strictly worse than an extra one. The warning log line makes the
    suppression path observable so ops can spot systemic failures.

    No caching — the preferences table is small, the key is read on the
    already-rate-limited new-device alert path (≤ 1/min per user), and
    a stale cache would mean a user hitting the "disable alerts" toggle
    from /settings/security would NOT see it take effect until the TTL
    expired. Freshness is cheaper than cache invalidation here.
    """
    try:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_preferences "
                "WHERE user_id = $1 AND pref_key = $2",
                user_id, NEW_DEVICE_ALERTS_PREF_KEY,
            )
    except Exception as exc:
        logger.warning(
            "new_device_alerts pref read failed user=%s err=%s "
            "— failing open (alert will fire)",
            user_id, exc,
        )
        return True
    if row is None:
        return True
    value = (row["value"] or "").strip().lower()
    return value not in NEW_DEVICE_ALERTS_FALSY_VALUES


def _new_device_alert_should_fire(
    user_id: str, ip_subnet: str,
) -> tuple[bool, str]:
    """Q.2 rate-limit + DHCP-tolerance gate for ``notify_new_device_login``.

    Returns ``(should_fire, reason)``:

    * ``(True, "")`` — alert is allowed through.
    * ``(False, "rate_limited_user")`` — same user already received a
      new-device alert within ``NEW_DEVICE_ALERT_USER_WINDOW_S``.
    * ``(False, "rate_limited_subnet")`` — same user already received
      a new-device alert for this ``ip_subnet`` within
      ``NEW_DEVICE_ALERT_SUBNET_WINDOW_S``.

    Gate ordering — **per-user first, per-subnet second** — is
    deliberate. The per-user gate's 60-second window is the tighter
    constraint; if we're already inside that window we want to drop
    fast without touching the subnet bucket. Consuming a subnet token
    only on ``should_fire=True`` preserves the invariant "one subnet
    token == one delivered alert"; this avoids the pathological case
    where a rate-limit drop burns a subnet token, so the user never
    actually learns about the new subnet before its 24h window elapses.

    Edge case documented here so future readers don't "fix" it into a
    bug: when the per-user gate blocks a burst of mixed-subnet logins,
    every subnet after the first is silently dropped. This is
    intentional — the spec says "最多發一則新裝置通知" per minute, so
    merging concurrent new-subnet alerts into the first one matches
    user intent (a reasonable human reads the first alert and takes
    action; sending 5 more in the same minute just trains them to
    ignore the channel).

    Empty ``ip_subnet`` (unparseable IP) skips the subnet gate — we
    still run the user gate so we don't spam on a series of
    unparseable IPs, but we don't partition the dedup key by a
    fingerprint that's effectively "everyone's unknown IP" (which
    would collapse every user's unknown-IP alerts into one bucket).
    """
    from backend.rate_limit import get_limiter
    limiter = get_limiter()
    user_ok, _ = limiter.allow(
        f"new_device_alert:user:{user_id}",
        capacity=1,
        window_seconds=NEW_DEVICE_ALERT_USER_WINDOW_S,
    )
    if not user_ok:
        return (False, "rate_limited_user")
    if not ip_subnet:
        return (True, "")
    subnet_ok, _ = limiter.allow(
        f"new_device_alert:subnet:{user_id}:{ip_subnet}",
        capacity=1,
        window_seconds=NEW_DEVICE_ALERT_SUBNET_WINDOW_S,
    )
    if not subnet_ok:
        return (False, "rate_limited_subnet")
    return (True, "")


async def notify_new_device_login(
    user: "User", sess: Session, ip: str, user_agent: str,
) -> None:
    """Q.2 (#296): fire the new-device-login alert pipeline if applicable.

    No-op when ``sess.is_new_device`` is ``False`` — the upstream
    ``_record_session_fingerprint`` decision is the single source of
    truth, so the de-dup story matches the fingerprint table exactly
    (no parallel state to drift). When the flag is set we emit the SSE
    event ``security.new_device_login`` (scope=user) and dispatch a
    ``warning``-level notification through ``backend.notifications``,
    which routes to the configured default IM channel (Slack at L2,
    falling back to silent persist if no channel is wired).

    The notification module's external dispatch already runs as
    ``asyncio.create_task`` so the login route doesn't pay the webhook
    latency. Both branches are wrapped — a failed alert must NEVER
    fail the login itself; a fingerprint write that succeeded but a
    flaky Slack webhook must not lock the user out.

    Rate-limit gates layer on top of the fingerprint primitive via
    ``_new_device_alert_should_fire``:

    * per-user 1/min cap (``NEW_DEVICE_ALERT_USER_WINDOW_S``) — anti-
      spam against bursty attacker probes.
    * per-(user, subnet) 24h dedup (``NEW_DEVICE_ALERT_SUBNET_WINDOW_S``)
      — DHCP tolerance on the same physical network. Complements the
      fingerprint table's (user, ua_hash, subnet) dedup by catching
      the second-order case where the UA hash shifts (e.g. browser
      update) but the subnet did not.

    Both gates use the shared ``backend.rate_limit`` limiter, so they
    coordinate across uvicorn workers when Redis is wired (which it
    is in prod, Phase 1 of A3.3 — see TODO.md A3 for the wire-up).

    Per-user opt-out (``user_preferences.new_device_alerts``) is the
    first gate below the ``is_new_device`` fast path — a user who has
    explicitly disabled this alert receives nothing, regardless of
    what the fingerprint / rate-limit primitives decide. The choke
    point stays centralised: every alert fan-out goes through this
    single function, so the toggle applies everywhere (password login,
    MFA verify, WebAuthn complete — all three call sites).
    """
    if not getattr(sess, "is_new_device", False):
        return
    if not await _new_device_alerts_enabled(user.id):
        logger.info(
            "notify_new_device_login: suppressed user=%s reason=pref_disabled",
            user.id,
        )
        return
    should_fire, reason = _new_device_alert_should_fire(
        user.id, compute_ip_subnet(ip),
    )
    if not should_fire:
        logger.info(
            "notify_new_device_login: suppressed user=%s reason=%s",
            user.id, reason,
        )
        return
    try:
        from backend.events import emit_new_device_login as _emit
        token_hint = _mask_token(sess.token)
        _emit(
            user_id=user.id,
            token_hint=token_hint,
            ip=ip or "",
            user_agent=user_agent or "",
            session_id=session_id_from_token(sess.token),
        )
    except Exception as exc:
        logger.warning("notify_new_device_login: SSE emit failed: %s", exc)
    try:
        from backend.notifications import notify as _notify
        await _notify(
            level="warning",
            title="新裝置登入",
            message=(
                f"偵測到 {user.email} 從新裝置登入（IP {ip or '未知'}，"
                f"User-Agent {user_agent[:120] or '未知'}）。如非本人操作，"
                f"請至安全中心結束該裝置 session 並更換密碼。"
            ),
            source="auth.security",
            action_label="管理裝置",
            action_url="/settings/security",
        )
    except Exception as exc:
        logger.warning("notify_new_device_login: notify dispatch failed: %s", exc)


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
    reason: str | None = None, trigger: str | None = None,
) -> int:
    now = time.time()
    grace = now + ROTATION_GRACE_S
    # Q.1 UI follow-up (2026-04-24): when this rotation was triggered
    # by a security event (password_change / totp_disabled / ...), log
    # the reason per-peer-token into ``session_revocations`` BEFORE the
    # UPDATE shrinks their expires_at. After the grace window the row
    # is evicted by ``_get_session_impl`` → the peer device's next
    # request lands in ``current_user`` with an empty session lookup,
    # and we need a side-channel to explain *why* it's 401-ing. That
    # side-channel is this log.
    if reason:
        if exclude_token:
            rows = await conn.fetch(
                "SELECT token FROM sessions "
                "WHERE user_id = $1 AND token != $2 AND expires_at > $3",
                user_id, exclude_token, now,
            )
        else:
            rows = await conn.fetch(
                "SELECT token FROM sessions "
                "WHERE user_id = $1 AND expires_at > $2",
                user_id, now,
            )
        for r in rows:
            await conn.execute(
                "INSERT INTO session_revocations "
                "(token, user_id, reason, trigger, revoked_at) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (token) DO UPDATE SET "
                "reason = EXCLUDED.reason, "
                "trigger = EXCLUDED.trigger, "
                "revoked_at = EXCLUDED.revoked_at",
                r["token"], user_id, reason, trigger or "", now,
            )
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
    reason: str | None = None, trigger: str | None = None,
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

    Q.1 UI follow-up (2026-04-24): when ``reason`` is set, each
    affected peer token is logged into ``session_revocations`` with
    ``(reason, trigger)``. This lets the peer device's next-request
    401 carry a tailored detail ("please sign in again because your
    password was changed on another device") instead of the generic
    "Authentication required".
    """
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _rotate_user_sessions_impl(
                owned_conn, user_id, exclude_token, reason, trigger,
            )
    return await _rotate_user_sessions_impl(
        conn, user_id, exclude_token, reason, trigger,
    )


# Q.1 UI follow-up (2026-04-24): window during which a revoked-session
# log entry is reported to the peer device. After this window the row
# is still queried but intentionally returns None — by then the user
# has had plenty of time to notice they were logged out, and surfacing
# "your password was changed 9 days ago" would be noise rather than a
# useful hint. The value is generous on purpose; the table is small
# (one row per peer session per rotation) so retention cost is cheap.
SESSION_REVOCATION_REPORT_WINDOW_S = 7 * 24 * 3600.0


async def _get_session_revocation_impl(conn, token: str) -> Optional[dict]:
    r = await conn.fetchrow(
        "SELECT token, user_id, reason, trigger, revoked_at "
        "FROM session_revocations WHERE token = $1",
        token,
    )
    if not r:
        return None
    revoked_at = float(r["revoked_at"] or 0.0)
    if revoked_at <= 0 or (time.time() - revoked_at) > SESSION_REVOCATION_REPORT_WINDOW_S:
        return None
    return {
        "token": r["token"],
        "user_id": r["user_id"],
        "reason": r["reason"] or "",
        "trigger": r["trigger"] or "",
        "revoked_at": revoked_at,
    }


async def get_session_revocation(token: str, conn=None) -> Optional[dict]:
    """Look up the most recent revocation record for a session token.

    Returns ``{token, user_id, reason, trigger, revoked_at}`` if the
    token was rotated by a security event within the report window;
    ``None`` otherwise (including "token was never known", "rotation
    happened but not security-flagged", or "record is older than the
    report window").

    Used by ``current_user`` on the 401 path — when the session
    cookie resolves to no live session, this probe tells us whether
    the device is 401-ing because of a security event (→ tailored
    message) or just because the session aged out naturally (→
    generic "please sign in again").
    """
    if not token:
        return None
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _get_session_revocation_impl(owned_conn, token)
    return await _get_session_revocation_impl(conn, token)


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


# Q.1 UI follow-up (2026-04-24): human-readable 401 message keyed off
# the ``trigger`` recorded in ``session_revocations``. Kept short —
# the UI renders it as a banner; it should read the same whether the
# user sees it inline on /login or popped into a toast.
_SESSION_REVOCATION_MESSAGES: dict[str, str] = {
    "password_change":
        "Your password was changed on another device. Please sign in again.",
    "totp_enrolled":
        "Two-factor authentication was enabled on another device. "
        "Please sign in again.",
    "totp_disabled":
        "Two-factor authentication was disabled on another device. "
        "Please sign in again.",
    "backup_codes_regenerated":
        "Your MFA backup codes were regenerated on another device. "
        "Please sign in again.",
    "webauthn_registered":
        "A new security key was registered on your account. "
        "Please sign in again.",
    "webauthn_removed":
        "A security key was removed from your account. "
        "Please sign in again.",
    "role_change":
        "Your account role was changed by an administrator. "
        "Please sign in again.",
    "account_disabled":
        "Your account was disabled by an administrator. "
        "Contact your administrator for access.",
}


def _session_revocation_message(trigger: str) -> str:
    return _SESSION_REVOCATION_MESSAGES.get(
        trigger or "",
        "Your session was ended for security reasons. Please sign in again.",
    )


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


# Y2 (#278) 2026-04-25: anonymous synthetic user is granted the new
# top role ``super_admin``. ``OMNISIGHT_AUTH_MODE=open`` is the dev
# default — every request gets this user — so without this bump the
# new admin-REST endpoints would 403 in local dev / pytest, breaking
# the pre-Y2 contract that "open mode == do everything". Real users
# in session/strict modes still need an explicit ``role='super_admin'``
# row in the users table; this only governs the dev fallback.
_ANON_ADMIN = User(id="anonymous", email="anonymous@local", name="(anonymous)",
                   role="super_admin", enabled=True, tenant_id="t-default")


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

    # Q.1 UI follow-up (2026-04-24): if the cookie points at a token
    # we revoked for a security event (password change / MFA toggle /
    # admin disable / role_change), surface that reason in the 401
    # detail so the UI can route to a "because your credentials were
    # changed, please sign in again" banner instead of a generic toast.
    detail: dict | str = "Authentication required"
    if cookie:
        try:
            rev = await get_session_revocation(cookie)
        except Exception as exc:
            logger.debug(
                "get_session_revocation probe failed on 401 path "
                "(non-fatal): %s", exc,
            )
            rev = None
        if rev:
            detail = {
                "reason": rev.get("reason") or "user_security_event",
                "trigger": rev.get("trigger") or "",
                "message": _session_revocation_message(
                    rev.get("trigger") or "",
                ),
            }
    raise HTTPException(status_code=401, detail=detail)


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
# Y2 (#278): platform-tier gate for admin REST API (tenant CRUD,
# super-admin self-service). Strict superset of require_admin —
# tenant admins MUST 403 against these endpoints.
require_super_admin = require_role("super_admin")


async def require_tenant(user: User = Depends(current_user)) -> User:
    """Set the request-scoped tenant context from the authenticated user."""
    from backend.db_context import set_tenant_id
    set_tenant_id(user.tenant_id)
    return user


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y5 (#281) row 2 — project-scope authorisation dependency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Allowed project roles in increasing rank order. Mirrors the
# ``project_members.role`` CHECK from alembic 0034
# (``IN ('owner', 'contributor', 'viewer')``); the ordered tuple
# encodes the inclusion order so a ``min_role`` gate is a single
# integer-rank compare, matching the ``ROLES`` / ``_RANK`` pattern
# used by ``require_role`` for platform tiers above.
PROJECT_ROLE_HIERARCHY = ("viewer", "contributor", "owner")
_PROJECT_ROLE_RANK = {r: i for i, r in enumerate(PROJECT_ROLE_HIERARCHY)}

# Tenant-membership role → effective project role default-resolution.
# Per the alembic 0034 docstring: when no explicit ``project_members``
# row exists, an *active* tenant membership with role admin / owner
# acts as ``contributor`` on every project of that tenant. member /
# viewer tenant roles intentionally fall through (key absent) → no
# project access by default; they need an explicit grant via the
# Y4 row 5 POST /tenants/{tid}/projects/{pid}/members surface.
_TENANT_ROLE_DEFAULT_PROJECT_ROLE = {
    "owner": "contributor",
    "admin": "contributor",
}

# SQL constants pulled out as module-level for drift-guard tests
# (consistent with the SQL-constant pattern in tenant_projects.py;
# secret-leak grep + PG ``$N`` placeholder check are easier when the
# query body is a single named symbol).
_FETCH_PROJECT_TENANT_SCOPED_FOR_AUTHZ_SQL = (
    "SELECT id, tenant_id FROM projects WHERE id = $1 AND tenant_id = $2"
)
_FETCH_PROJECT_BY_ID_FOR_AUTHZ_SQL = (
    "SELECT id, tenant_id FROM projects WHERE id = $1"
)
_FETCH_PROJECT_MEMBER_FOR_AUTHZ_SQL = (
    "SELECT role FROM project_members "
    "WHERE user_id = $1 AND project_id = $2"
)
_FETCH_TENANT_MEMBERSHIP_FOR_AUTHZ_SQL = (
    "SELECT role, status FROM user_tenant_memberships "
    "WHERE user_id = $1 AND tenant_id = $2"
)


def project_role_at_least(have: Optional[str], need: str) -> bool:
    """Compare project roles by rank (``viewer < contributor < owner``).

    Returns ``False`` for ``have is None`` (no role resolved) or for
    unknown role tokens. ``super_admin`` is NOT in the project-role
    hierarchy — callers handle that platform-tier bypass before
    consulting this comparator.
    """
    if have is None or have not in _PROJECT_ROLE_RANK:
        return False
    if need not in _PROJECT_ROLE_RANK:
        return False
    return _PROJECT_ROLE_RANK[have] >= _PROJECT_ROLE_RANK[need]


def require_project_member(min_role: str = "viewer"):
    """FastAPI dependency factory enforcing per-project RBAC.

    Resolves the caller's effective role on the URL-path
    ``{project_id}`` and refuses the request if it ranks below
    ``min_role`` in :data:`PROJECT_ROLE_HIERARCHY`.

    Resolution order (cheap → expensive):

    1. Platform ``super_admin`` short-circuit — returns the user with
       ``current_user_role`` pinned to ``"super_admin"``. The
       SQLAlchemy listener (Y5 row 3) treats this as "no per-project
       filter" when paired with the ``X-Admin-Cross-Project: 1``
       header, and emits an audit row for cross-project access.
    2. Direct ``project_members`` row for ``(user_id, project_id)``
       — its role is the effective role.
    3. Tenant-membership fallback (alembic 0034 default-resolution):
       active ``user_tenant_memberships`` with role ∈ {owner, admin}
       maps to ``contributor`` on every project of the tenant;
       member / viewer tenant roles confer no project access.

    On success the request-scoped ContextVars are populated:

    * ``set_tenant_id`` ← project's tenant (defence in depth even if
      the route forgot to depend on ``require_tenant`` first)
    * ``set_project_id`` ← path param
    * ``set_user_role`` ← effective project role

    Status codes:

    * 200 — role resolves and ranks ≥ ``min_role``
    * 403 — authenticated, but no membership / role too low
    * 404 — project does not exist (or, when the path also carries
            ``{tenant_id}``, lives in a different tenant — by
            intent: the caller has no business probing cross-tenant
            existence by status code)

    Why ``tenant_id`` is read from ``request.path_params`` rather
    than the dependency signature
    ────────────────────────────────────────────────────────────
    The TODO row's primary input is ``project_id`` — but the
    common route shape is ``/tenants/{tid}/projects/{pid}/...``.
    Declaring ``tenant_id: str = None`` in the dependency signature
    would make FastAPI bind it from the *query string* on routes
    that nest ``project_id`` directly (e.g. ``/api/v1/projects/
    {project_id}/...``), which is the wrong source. Reading from
    ``request.path_params`` gives us "use the URL tenant when the
    route declares one, otherwise derive from the project row" with
    no false binding.

    Module-global state audit (SOP Step 1)
    ──────────────────────────────────────
    Two role tuples + four SQL strings + the rank dict are all
    module-level immutable; each uvicorn worker derives the same
    values from source. The asyncpg pool is shared via PG. No new
    in-memory cache.

    Read-after-write timing audit (SOP Step 1)
    ──────────────────────────────────────────
    Three single-row reads (project, project_members, optional
    tenant_membership). No writes, no transaction; concurrent
    membership mutations land via PG row locks on their write paths.
    """
    if min_role not in _PROJECT_ROLE_RANK:
        raise ValueError(
            f"unknown project role: {min_role!r}; "
            f"must be one of {PROJECT_ROLE_HIERARCHY}"
        )

    async def _dep(
        project_id: str,
        request: Request,
        user: User = Depends(current_user),
    ) -> User:
        from backend.db_context import (
            set_project_id,
            set_tenant_id,
            set_user_role,
        )
        from backend.db_pool import get_pool

        # Optional ``{tenant_id}`` from the same path. ``path_params``
        # is a plain dict; missing key → None (legitimate for routes
        # that nest ``project_id`` directly).
        path_tenant_id = request.path_params.get("tenant_id")

        async with get_pool().acquire() as conn:
            if path_tenant_id is not None:
                project_row = await conn.fetchrow(
                    _FETCH_PROJECT_TENANT_SCOPED_FOR_AUTHZ_SQL,
                    project_id, path_tenant_id,
                )
            else:
                project_row = await conn.fetchrow(
                    _FETCH_PROJECT_BY_ID_FOR_AUTHZ_SQL,
                    project_id,
                )
        if project_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"project not found: {project_id!r}",
            )
        resolved_tenant_id = project_row["tenant_id"]

        # Platform-tier bypass — ``super_admin`` may read / write any
        # project. The listener (Y5 row 3) is responsible for the
        # cross-project audit trail; this dependency only pins the
        # context vars so the listener can see "this is a super-admin
        # request scoped to (tenant, project)".
        if role_at_least(user.role, "super_admin"):
            set_tenant_id(resolved_tenant_id)
            set_project_id(project_id)
            set_user_role("super_admin")
            return user

        async with get_pool().acquire() as conn:
            pm_row = await conn.fetchrow(
                _FETCH_PROJECT_MEMBER_FOR_AUTHZ_SQL,
                user.id, project_id,
            )

        effective_role: Optional[str] = None
        if pm_row is not None:
            effective_role = pm_row["role"]
        else:
            async with get_pool().acquire() as conn:
                tm_row = await conn.fetchrow(
                    _FETCH_TENANT_MEMBERSHIP_FOR_AUTHZ_SQL,
                    user.id, resolved_tenant_id,
                )
            if tm_row is not None and tm_row["status"] == "active":
                effective_role = _TENANT_ROLE_DEFAULT_PROJECT_ROLE.get(
                    tm_row["role"]
                )
            # else: anonymous / suspended / member / viewer → None
            # → 403 below.

        if not project_role_at_least(effective_role, min_role):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"requires project role {min_role} or higher on "
                    f"project {project_id!r}"
                ),
            )

        set_tenant_id(resolved_tenant_id)
        set_project_id(project_id)
        set_user_role(effective_role)
        return user

    return _dep


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
