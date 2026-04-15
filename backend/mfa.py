"""K5 — MFA (TOTP + WebAuthn) core logic.

Handles TOTP enrollment/verification with pyotp, backup codes,
and WebAuthn registration/authentication with py_webauthn.
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DB helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _conn():
    from backend import db
    return db._conn()


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MFA status queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def get_user_mfa_methods(user_id: str) -> list[dict]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, method, name, verified, created_at, last_used "
        "FROM user_mfa WHERE user_id=? ORDER BY created_at",
        (user_id,),
    ) as cur:
        rows = await cur.fetchall()
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
    conn = await _conn()
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM user_mfa WHERE user_id=? AND verified=1",
        (user_id,),
    ) as cur:
        r = await cur.fetchone()
    return bool(r and r["n"] > 0)


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
    totp_confirm_enroll() to activate."""
    secret = pyotp.random_base32(32)
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=user_email, issuer_name=MFA_ISSUER)

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    mfa_id = f"mfa-{uuid.uuid4().hex[:12]}"
    conn = await _conn()
    await conn.execute(
        "DELETE FROM user_mfa WHERE user_id=? AND method='totp' AND verified=0",
        (user_id,),
    )
    await conn.execute(
        "INSERT INTO user_mfa (id, user_id, method, secret, name, verified) "
        "VALUES (?, ?, 'totp', ?, 'TOTP Authenticator', 0)",
        (mfa_id, user_id, secret),
    )
    await conn.commit()
    return {
        "mfa_id": mfa_id,
        "secret": secret,
        "uri": uri,
        "qr_png_b64": qr_b64,
    }


async def totp_confirm_enroll(user_id: str, code: str) -> bool:
    """Verify the code matches the pending TOTP secret and activate it."""
    conn = await _conn()
    async with conn.execute(
        "SELECT id, secret FROM user_mfa "
        "WHERE user_id=? AND method='totp' AND verified=0",
        (user_id,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return False
    totp = pyotp.TOTP(r["secret"])
    if not totp.verify(code.strip(), valid_window=TOTP_DRIFT_TOLERANCE):
        return False
    await conn.execute(
        "UPDATE user_mfa SET verified=1 WHERE id=?", (r["id"],)
    )
    await conn.commit()
    codes = await _generate_backup_codes(user_id)
    logger.info("[MFA] TOTP enrolled for user %s, %d backup codes generated",
                user_id, len(codes))
    return True


async def totp_disable(user_id: str) -> bool:
    conn = await _conn()
    cur = await conn.execute(
        "DELETE FROM user_mfa WHERE user_id=? AND method='totp'",
        (user_id,),
    )
    await conn.execute(
        "DELETE FROM mfa_backup_codes WHERE user_id=?", (user_id,),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TOTP verification (login challenge)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def verify_totp(user_id: str, code: str) -> bool:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, secret FROM user_mfa "
        "WHERE user_id=? AND method='totp' AND verified=1",
        (user_id,),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return False
    totp = pyotp.TOTP(r["secret"])
    if not totp.verify(code.strip(), valid_window=TOTP_DRIFT_TOLERANCE):
        return False
    await conn.execute(
        "UPDATE user_mfa SET last_used=datetime('now') WHERE id=?",
        (r["id"],),
    )
    await conn.commit()
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Backup codes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _generate_code() -> str:
    n = secrets.randbelow(100_000_000)
    raw = f"{n:08d}"
    return f"{raw[:4]}-{raw[4:]}"


async def _generate_backup_codes(user_id: str) -> list[str]:
    conn = await _conn()
    await conn.execute(
        "DELETE FROM mfa_backup_codes WHERE user_id=?", (user_id,),
    )
    codes: list[str] = []
    for _ in range(BACKUP_CODE_COUNT):
        code = _generate_code()
        codes.append(code)
        await conn.execute(
            "INSERT INTO mfa_backup_codes (user_id, code_hash) VALUES (?, ?)",
            (user_id, _hash_code(code)),
        )
    await conn.commit()
    return codes


async def regenerate_backup_codes(user_id: str) -> list[str]:
    has_mfa = await has_verified_mfa(user_id)
    if not has_mfa:
        return []
    return await _generate_backup_codes(user_id)


async def get_backup_codes_status(user_id: str) -> dict:
    conn = await _conn()
    async with conn.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN used=0 THEN 1 ELSE 0 END) AS remaining "
        "FROM mfa_backup_codes WHERE user_id=?",
        (user_id,),
    ) as cur:
        r = await cur.fetchone()
    return {
        "total": r["total"] if r else 0,
        "remaining": r["remaining"] if r else 0,
    }


async def verify_backup_code(user_id: str, code: str) -> bool:
    h = _hash_code(code)
    conn = await _conn()
    async with conn.execute(
        "SELECT id FROM mfa_backup_codes "
        "WHERE user_id=? AND code_hash=? AND used=0",
        (user_id, h),
    ) as cur:
        r = await cur.fetchone()
    if not r:
        return False
    await conn.execute(
        "UPDATE mfa_backup_codes SET used=1, used_at=datetime('now') WHERE id=?",
        (r["id"],),
    )
    await conn.commit()
    logger.info("[MFA] Backup code consumed for user %s", user_id)
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WebAuthn
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_webauthn_challenges: dict[str, bytes] = {}


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

    _webauthn_challenges[user_id] = options.challenge
    return json.loads(options_to_json(options))


async def webauthn_complete_register(
    user_id: str, credential_json: dict, name: str = "",
) -> bool:
    from webauthn import verify_registration_response

    challenge = _webauthn_challenges.pop(user_id, None)
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
    conn = await _conn()
    await conn.execute(
        "INSERT INTO user_mfa (id, user_id, method, credential, name, verified) "
        "VALUES (?, ?, 'webauthn', ?, ?, 1)",
        (mfa_id, user_id, json.dumps(cred_data), name or "Security Key"),
    )
    await conn.commit()

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

    _webauthn_challenges[user_id] = options.challenge
    return json.loads(options_to_json(options))


async def webauthn_complete_authenticate(user_id: str, credential_json: dict) -> bool:
    from webauthn import verify_authentication_response

    challenge = _webauthn_challenges.pop(user_id, None)
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
        verification = verify_authentication_response(
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

    conn = await _conn()
    await conn.execute(
        "UPDATE user_mfa SET last_used=datetime('now') WHERE id=?",
        (matched["mfa_id"],),
    )
    await conn.commit()
    return True


async def webauthn_remove(user_id: str, mfa_id: str) -> bool:
    conn = await _conn()
    cur = await conn.execute(
        "DELETE FROM user_mfa WHERE id=? AND user_id=? AND method='webauthn'",
        (mfa_id, user_id),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def _get_webauthn_credentials(user_id: str) -> list[dict]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, credential FROM user_mfa "
        "WHERE user_id=? AND method='webauthn' AND verified=1",
        (user_id,),
    ) as cur:
        rows = await cur.fetchall()
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
    conn = await _conn()
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM mfa_backup_codes WHERE user_id=?",
        (user_id,),
    ) as cur:
        r = await cur.fetchone()
    return bool(r and r["n"] > 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MFA challenge (unified entry point for login flow)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_pending_mfa: dict[str, dict] = {}


def create_mfa_challenge(user_id: str, ip: str = "", user_agent: str = "") -> str:
    """Create a temporary MFA challenge token after password OK."""
    token = secrets.token_urlsafe(32)
    _pending_mfa[token] = {
        "user_id": user_id,
        "ip": ip,
        "user_agent": user_agent,
        "created_at": time.time(),
    }
    _cleanup_stale_challenges()
    return token


def get_mfa_challenge(token: str) -> Optional[dict]:
    data = _pending_mfa.get(token)
    if not data:
        return None
    if time.time() - data["created_at"] > 300:
        _pending_mfa.pop(token, None)
        return None
    return data


def consume_mfa_challenge(token: str) -> Optional[dict]:
    data = _pending_mfa.pop(token, None)
    if not data:
        return None
    if time.time() - data["created_at"] > 300:
        return None
    return data


def _cleanup_stale_challenges():
    cutoff = time.time() - 300
    stale = [k for k, v in _pending_mfa.items() if v["created_at"] < cutoff]
    for k in stale:
        _pending_mfa.pop(k, None)


async def mark_session_mfa_verified(token: str) -> None:
    conn = await _conn()
    await conn.execute(
        "UPDATE sessions SET mfa_verified=1 WHERE token=?", (token,),
    )
    await conn.commit()
