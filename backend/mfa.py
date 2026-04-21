"""K5 — MFA (TOTP + WebAuthn) core logic.

Handles TOTP enrollment/verification with pyotp, backup codes,
and WebAuthn registration/authentication with py_webauthn.

Phase-3-Runtime-v2 SP-5.7b (2026-04-21): ported from compat
``db._conn()`` to native asyncpg pool. 14 DB-touching functions
move to ``get_pool().acquire() + $N placeholders``. ``datetime
('now')`` in UPDATE SETs (last_used, used_at) swapped for
``to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS')``.

Module-global audit (SOP Step 1):
  * ``MFA_ISSUER``, ``TOTP_DRIFT_TOLERANCE``, ``BACKUP_CODE_COUNT``
    — constants, answer (1) identical across workers.

Task #116 / Step B.4 (2026-04-21): the previously-flagged
per-worker dicts ``_webauthn_challenges`` + ``_pending_mfa``
have been migrated to a shared PG table ``mfa_challenges``
(alembic 0018). WebAuthn begin on worker A + complete on worker
B now resolves correctly; same for MFA login challenge tokens.
See ``_challenge_{put,get,pop}`` helpers below.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import secrets
import time
import uuid
from typing import Optional

import pyotp
import qrcode

logger = logging.getLogger(__name__)

MFA_ISSUER = "OmniSight"
TOTP_DRIFT_TOLERANCE = 1  # allow +/- 1 time step (30s each)
BACKUP_CODE_COUNT = 10


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MFA status queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def get_user_mfa_methods(user_id: str) -> list[dict]:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, method, name, verified, created_at, last_used "
            "FROM user_mfa WHERE user_id = $1 ORDER BY created_at",
            user_id,
        )
    return [
        {
            "id": r["id"],
            "method": r["method"],
            "name": r["name"],
            "verified": bool(r["verified"]),
            "created_at": r["created_at"],
            "last_used": r["last_used"],
        }
        for r in rows
    ]


async def has_verified_mfa(user_id: str) -> bool:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM user_mfa "
            "WHERE user_id = $1 AND verified = 1",
            user_id,
        )
    return bool(n and int(n) > 0)


async def require_mfa_for_user(user_id: str) -> bool:
    """Check if this user must have MFA (strict mode forces admin/operator)."""
    require = (os.environ.get("OMNISIGHT_REQUIRE_MFA") or "").strip().lower() == "true"
    if not require:
        return False
    from backend.auth import get_user, role_at_least
    user = await get_user(user_id)
    if not user:
        return False
    return role_at_least(user.role, "operator")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TOTP enrollment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def totp_begin_enroll(user_id: str, user_email: str) -> dict:
    """Generate a TOTP secret and provisioning URI. Returns secret + QR as
    base64 PNG. The method row is created with verified=0; call
    totp_confirm_enroll() to activate.

    SP-5.7b: DELETE-stale-pending + INSERT-new now run in one tx so
    a crash between them can't leave the user with both an abandoned
    stale pending row AND a new fresh one (the UI would offer the
    wrong secret).
    """
    secret = pyotp.random_base32(32)
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=user_email, issuer_name=MFA_ISSUER)

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    mfa_id = f"mfa-{uuid.uuid4().hex[:12]}"
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM user_mfa "
                "WHERE user_id = $1 AND method = 'totp' AND verified = 0",
                user_id,
            )
            await conn.execute(
                "INSERT INTO user_mfa "
                "(id, user_id, method, secret, name, verified) "
                "VALUES ($1, $2, 'totp', $3, 'TOTP Authenticator', 0)",
                mfa_id, user_id, secret,
            )
    return {
        "mfa_id": mfa_id,
        "secret": secret,
        "uri": uri,
        "qr_png_b64": qr_b64,
    }


async def totp_confirm_enroll(user_id: str, code: str) -> bool:
    """Verify the code matches the pending TOTP secret and activate it."""
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, secret FROM user_mfa "
            "WHERE user_id = $1 AND method = 'totp' AND verified = 0",
            user_id,
        )
        if not r:
            return False
        totp = pyotp.TOTP(r["secret"])
        if not totp.verify(code.strip(), valid_window=TOTP_DRIFT_TOLERANCE):
            return False
        await conn.execute(
            "UPDATE user_mfa SET verified = 1 WHERE id = $1", r["id"],
        )
    codes = await _generate_backup_codes(user_id)
    logger.info("[MFA] TOTP enrolled for user %s, %d backup codes generated",
                user_id, len(codes))
    return True


async def totp_disable(user_id: str) -> bool:
    """Delete the user's TOTP method row AND clear their backup codes.

    SP-5.7b: two DELETEs now run in one tx so a crash between them
    can't leave backup codes dangling after the TOTP row is gone
    (which would make the codes unverifiable — UI says 'no MFA' but
    DB still has backup code rows).
    """
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            status = await conn.execute(
                "DELETE FROM user_mfa "
                "WHERE user_id = $1 AND method = 'totp'",
                user_id,
            )
            await conn.execute(
                "DELETE FROM mfa_backup_codes WHERE user_id = $1",
                user_id,
            )
    try:
        return int(status.rsplit(" ", 1)[-1]) > 0
    except (ValueError, AttributeError):
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TOTP verification (login challenge)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def verify_totp(user_id: str, code: str) -> bool:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, secret FROM user_mfa "
            "WHERE user_id = $1 AND method = 'totp' AND verified = 1",
            user_id,
        )
        if not r:
            return False
        totp = pyotp.TOTP(r["secret"])
        if not totp.verify(code.strip(), valid_window=TOTP_DRIFT_TOLERANCE):
            return False
        await conn.execute(
            "UPDATE user_mfa SET "
            "last_used = to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS') "
            "WHERE id = $1",
            r["id"],
        )
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Backup codes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _generate_code() -> str:
    n = secrets.randbelow(100_000_000)
    raw = f"{n:08d}"
    return f"{raw[:4]}-{raw[4:]}"


async def _generate_backup_codes(user_id: str) -> list[str]:
    """Regenerate the 10-code backup set. SP-5.7b: entire
    DELETE-then-INSERT-N loop runs inside one tx so a crash mid-
    loop can't leave the user with a half-regenerated code set
    (would be worse than keeping the old set intact)."""
    codes = [_generate_code() for _ in range(BACKUP_CODE_COUNT)]
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM mfa_backup_codes WHERE user_id = $1",
                user_id,
            )
            for code in codes:
                await conn.execute(
                    "INSERT INTO mfa_backup_codes (user_id, code_hash) "
                    "VALUES ($1, $2)",
                    user_id, _hash_code(code),
                )
    return codes


async def regenerate_backup_codes(user_id: str) -> list[str]:
    has_mfa = await has_verified_mfa(user_id)
    if not has_mfa:
        return []
    return await _generate_backup_codes(user_id)


async def get_backup_codes_status(user_id: str) -> dict:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN used = 0 THEN 1 ELSE 0 END) AS remaining "
            "FROM mfa_backup_codes WHERE user_id = $1",
            user_id,
        )
    return {
        "total": int(r["total"]) if r and r["total"] is not None else 0,
        "remaining": int(r["remaining"]) if r and r["remaining"] is not None else 0,
    }


async def verify_backup_code(user_id: str, code: str) -> bool:
    """Consume a backup code. SP-5.7b: SELECT + UPDATE inside one
    tx with a UPDATE ... WHERE ... AND used = 0 guard so two
    concurrent ``verify_backup_code`` calls with the same code
    can't both succeed — whoever commits second sees 0 rows
    updated and returns False."""
    h = _hash_code(code)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE mfa_backup_codes SET "
                "used = 1, "
                "used_at = to_char(clock_timestamp(), "
                "                   'YYYY-MM-DD HH24:MI:SS') "
                "WHERE user_id = $1 AND code_hash = $2 AND used = 0 "
                "RETURNING id",
                user_id, h,
            )
    if row is None:
        return False
    logger.info("[MFA] Backup code consumed for user %s", user_id)
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared-challenge storage (PG, task #116)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Replaces the pre-B.4 per-worker dicts. Two kinds of challenge:
#
#   kind='webauthn'   — WebAuthn reg/auth challenge bytes, keyed by
#                       user_id (one pending WebAuthn dance per user).
#                       payload is base64-encoded raw challenge bytes.
#   kind='mfa_pending' — MFA login challenge between password-OK and
#                       MFA-code-OK. Keyed by challenge token.
#                       payload is a JSON dict (user_id, ip, ua, ts).
#
# TTL: 300s for both (matches the original dict's cleanup cadence
# for _pending_mfa and is a reasonable ceiling for WebAuthn). Stale
# rows are filtered via ``WHERE created_at > NOW() - interval``
# on every read, not by a background sweep.

_CHALLENGE_TTL_S = 300


async def _challenge_put(kind: str, key: str, payload: bytes | str) -> None:
    """Upsert a challenge. Overwrites any prior entry for this key
    (matches ``dict[key] = value`` semantics)."""
    if isinstance(payload, bytes):
        payload_str = base64.b64encode(payload).decode("ascii")
    else:
        payload_str = payload
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO mfa_challenges (id, kind, payload, created_at) "
            "VALUES ($1, $2, $3, CURRENT_TIMESTAMP) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  kind = EXCLUDED.kind, "
            "  payload = EXCLUDED.payload, "
            "  created_at = EXCLUDED.created_at",
            key, kind, payload_str,
        )


async def _challenge_pop(kind: str, key: str) -> bytes | None:
    """Read-and-delete a challenge under TTL. Returns None if missing
    or stale. Matches ``dict.pop(key, None)`` + TTL check."""
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM mfa_challenges "
            "WHERE id = $1 AND kind = $2 "
            "AND created_at > CURRENT_TIMESTAMP - "
            f"    INTERVAL '{_CHALLENGE_TTL_S} seconds' "
            "RETURNING payload",
            key, kind,
        )
    if row is None:
        return None
    try:
        return base64.b64decode(row["payload"])
    except Exception:
        # Caller passed JSON string (mfa_pending); decode handled there.
        return row["payload"].encode("utf-8") if isinstance(row["payload"], str) else None


async def _challenge_get_json(kind: str, key: str) -> dict | None:
    """Read-without-delete for mfa_pending's get-then-maybe-consume
    flow. Returns the JSON payload dict or None if missing/stale."""
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM mfa_challenges "
            "WHERE id = $1 AND kind = $2 "
            "AND created_at > CURRENT_TIMESTAMP - "
            f"    INTERVAL '{_CHALLENGE_TTL_S} seconds'",
            key, kind,
        )
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WebAuthn
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _rp_id() -> str:
    return (os.environ.get("OMNISIGHT_WEBAUTHN_RP_ID") or "localhost").strip()


def _rp_name() -> str:
    return (os.environ.get("OMNISIGHT_WEBAUTHN_RP_NAME") or MFA_ISSUER).strip()


def _rp_origin() -> str:
    rp_id = _rp_id()
    custom = (os.environ.get("OMNISIGHT_WEBAUTHN_ORIGIN") or "").strip()
    if custom:
        return custom
    if rp_id == "localhost":
        return "http://localhost:3000"
    return f"https://{rp_id}"


async def webauthn_begin_register(user_id: str, user_email: str, user_name: str) -> dict:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    existing_creds = await _get_webauthn_credentials(user_id)
    exclude = [
        PublicKeyCredentialDescriptor(id=base64.urlsafe_b64decode(c["cred_id"] + "=="))
        for c in existing_creds
    ]

    options = generate_registration_options(
        rp_id=_rp_id(),
        rp_name=_rp_name(),
        user_id=user_id.encode("utf-8"),
        user_name=user_email,
        user_display_name=user_name or user_email,
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    await _challenge_put("webauthn", user_id, options.challenge)
    return json.loads(options_to_json(options))


async def webauthn_complete_register(
    user_id: str, credential_json: dict, name: str = "",
) -> bool:
    from webauthn import verify_registration_response

    challenge = await _challenge_pop("webauthn", user_id)
    if not challenge:
        return False

    try:
        from webauthn.helpers import parse_registration_credential_json
        credential = parse_registration_credential_json(json.dumps(credential_json))
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=_rp_id(),
            expected_origin=_rp_origin(),
        )
    except Exception as exc:
        logger.warning("[MFA] WebAuthn registration verify failed: %s", exc)
        return False

    cred_data = {
        "cred_id": base64.urlsafe_b64encode(verification.credential_id).decode().rstrip("="),
        "public_key": base64.b64encode(verification.credential_public_key).decode(),
        "sign_count": verification.sign_count,
        "attestation_object": base64.b64encode(verification.attestation_object).decode() if verification.attestation_object else "",
    }

    mfa_id = f"mfa-{uuid.uuid4().hex[:12]}"
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO user_mfa "
            "(id, user_id, method, credential, name, verified) "
            "VALUES ($1, $2, 'webauthn', $3, $4, 1)",
            mfa_id, user_id, json.dumps(cred_data),
            name or "Security Key",
        )

    if not await _has_backup_codes(user_id):
        await _generate_backup_codes(user_id)

    logger.info("[MFA] WebAuthn credential registered for user %s", user_id)
    return True


async def webauthn_begin_authenticate(user_id: str) -> dict:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    creds = await _get_webauthn_credentials(user_id)
    allow = [
        PublicKeyCredentialDescriptor(id=base64.urlsafe_b64decode(c["cred_id"] + "=="))
        for c in creds
    ]

    options = generate_authentication_options(
        rp_id=_rp_id(),
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    await _challenge_put("webauthn", user_id, options.challenge)
    return json.loads(options_to_json(options))


async def webauthn_complete_authenticate(user_id: str, credential_json: dict) -> bool:
    from webauthn import verify_authentication_response

    challenge = await _challenge_pop("webauthn", user_id)
    if not challenge:
        return False

    creds = await _get_webauthn_credentials(user_id)
    cred_id_b64 = credential_json.get("id", "")

    matched = None
    for c in creds:
        padded = c["cred_id"] + "=" * ((4 - len(c["cred_id"]) % 4) % 4)
        if padded == cred_id_b64 + "=" * ((4 - len(cred_id_b64) % 4) % 4):
            matched = c
            break
        if c["cred_id"] == cred_id_b64:
            matched = c
            break

    if not matched:
        return False

    try:
        from webauthn.helpers import parse_authentication_credential_json
        credential = parse_authentication_credential_json(json.dumps(credential_json))
        verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=_rp_id(),
            expected_origin=_rp_origin(),
            credential_public_key=base64.b64decode(matched["public_key"]),
            credential_current_sign_count=matched["sign_count"],
        )
    except Exception as exc:
        logger.warning("[MFA] WebAuthn authentication failed: %s", exc)
        return False

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE user_mfa SET "
            "last_used = to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS') "
            "WHERE id = $1",
            matched["mfa_id"],
        )
    return True


async def webauthn_remove(user_id: str, mfa_id: str) -> bool:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM user_mfa "
            "WHERE id = $1 AND user_id = $2 AND method = 'webauthn' "
            "RETURNING id",
            mfa_id, user_id,
        )
    return row is not None


async def _get_webauthn_credentials(user_id: str) -> list[dict]:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, credential FROM user_mfa "
            "WHERE user_id = $1 AND method = 'webauthn' AND verified = 1",
            user_id,
        )
    result = []
    for r in rows:
        try:
            cred = json.loads(r["credential"])
            cred["mfa_id"] = r["id"]
            result.append(cred)
        except (json.JSONDecodeError, TypeError):
            pass
    return result


async def _has_backup_codes(user_id: str) -> bool:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM mfa_backup_codes WHERE user_id = $1",
            user_id,
        )
    return bool(n and int(n) > 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MFA challenge (unified entry point for login flow)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Step B.4 (2026-04-21): ``_pending_mfa`` per-worker dict → PG-
# backed via ``mfa_challenges`` with kind='mfa_pending'. All three
# public functions are now ``async def`` because they hit the pool.
# Callers in routers/auth.py + routers/mfa.py + one repo-root test
# updated to ``await``.


async def create_mfa_challenge(user_id: str, ip: str = "", user_agent: str = "") -> str:
    """Create a temporary MFA challenge token after password OK.

    Returns the token string; caller hands it to the client so the
    MFA-code step can consume it via ``consume_mfa_challenge``.
    """
    token = secrets.token_urlsafe(32)
    payload = json.dumps({
        "user_id": user_id,
        "ip": ip,
        "user_agent": user_agent,
        "created_at": time.time(),
    })
    await _challenge_put("mfa_pending", token, payload)
    return token


async def get_mfa_challenge(token: str) -> Optional[dict]:
    """Read-without-consume. Returns None if missing or stale."""
    return await _challenge_get_json("mfa_pending", token)


async def consume_mfa_challenge(token: str) -> Optional[dict]:
    """Read-and-delete under TTL. Returns None if missing or stale."""
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM mfa_challenges "
            "WHERE id = $1 AND kind = 'mfa_pending' "
            "AND created_at > CURRENT_TIMESTAMP - "
            f"    INTERVAL '{_CHALLENGE_TTL_S} seconds' "
            "RETURNING payload",
            token,
        )
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None


async def cleanup_stale_mfa_challenges() -> int:
    """Background-sweep helper: delete rows past the TTL. Not
    strictly needed (every read-path has a TTL guard), but
    callable from a nightly job to keep the table bounded."""
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        status = await conn.execute(
            "DELETE FROM mfa_challenges "
            "WHERE created_at < CURRENT_TIMESTAMP - "
            f"    INTERVAL '{_CHALLENGE_TTL_S} seconds'"
        )
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


async def mark_session_mfa_verified(token: str) -> None:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET mfa_verified = 1 WHERE token = $1",
            token,
        )
