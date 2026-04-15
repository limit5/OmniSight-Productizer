"""K5 — MFA management + challenge router.

POST   /auth/mfa/totp/enroll         Start TOTP enrollment (returns QR)
POST   /auth/mfa/totp/confirm        Confirm TOTP with first code
POST   /auth/mfa/totp/disable        Remove TOTP
GET    /auth/mfa/status              List enrolled MFA methods
GET    /auth/mfa/backup-codes/status  Remaining backup code count
POST   /auth/mfa/backup-codes/regenerate  Generate new set (returns codes)
POST   /auth/mfa/webauthn/register/begin      Start WebAuthn registration
POST   /auth/mfa/webauthn/register/complete    Finish WebAuthn registration
DELETE /auth/mfa/webauthn/{mfa_id}             Remove a WebAuthn credential
POST   /auth/mfa/challenge            Verify MFA code after password login
POST   /auth/mfa/webauthn/challenge/begin      Start WebAuthn auth challenge
POST   /auth/mfa/webauthn/challenge/complete   Finish WebAuthn auth challenge
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend import auth, mfa

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mfa"])


def _cookie_secure() -> bool:
    return (os.environ.get("OMNISIGHT_COOKIE_SECURE") or "").strip().lower() == "true"


def _client_key(request: Request) -> str:
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    return (request.client.host if request.client else "") or "unknown"


# ── MFA status ──

@router.get("/auth/mfa/status")
async def mfa_status(user: auth.User = Depends(auth.current_user)) -> dict:
    methods = await mfa.get_user_mfa_methods(user.id)
    has_mfa = any(m["verified"] for m in methods)
    require = await mfa.require_mfa_for_user(user.id)
    return {
        "methods": methods,
        "has_mfa": has_mfa,
        "require_mfa": require,
    }


# ── TOTP enrollment ──

@router.post("/auth/mfa/totp/enroll")
async def totp_enroll(user: auth.User = Depends(auth.current_user)) -> dict:
    result = await mfa.totp_begin_enroll(user.id, user.email)
    return result


class TOTPConfirmRequest(BaseModel):
    code: str = Field(min_length=6, max_length=8)


@router.post("/auth/mfa/totp/confirm")
async def totp_confirm(req: TOTPConfirmRequest,
                       user: auth.User = Depends(auth.current_user)) -> dict:
    ok = await mfa.totp_confirm_enroll(user.id, req.code)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid TOTP code")
    codes = await mfa.regenerate_backup_codes(user.id)
    try:
        from backend import audit as _audit
        await _audit.log(
            action="mfa.totp.enrolled", entity_kind="mfa", entity_id=user.id,
            after={"method": "totp"}, actor=user.email,
        )
    except Exception:
        pass
    return {"status": "enrolled", "backup_codes": codes}


@router.post("/auth/mfa/totp/disable")
async def totp_disable(user: auth.User = Depends(auth.current_user)) -> dict:
    ok = await mfa.totp_disable(user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="TOTP not enrolled")
    try:
        from backend import audit as _audit
        await _audit.log(
            action="mfa.totp.disabled", entity_kind="mfa", entity_id=user.id,
            after={"method": "totp"}, actor=user.email,
        )
    except Exception:
        pass
    return {"status": "disabled"}


# ── Backup codes ──

@router.get("/auth/mfa/backup-codes/status")
async def backup_codes_status(user: auth.User = Depends(auth.current_user)) -> dict:
    return await mfa.get_backup_codes_status(user.id)


@router.post("/auth/mfa/backup-codes/regenerate")
async def backup_codes_regenerate(user: auth.User = Depends(auth.current_user)) -> dict:
    codes = await mfa.regenerate_backup_codes(user.id)
    if not codes:
        raise HTTPException(status_code=400, detail="No MFA enrolled — cannot generate backup codes")
    try:
        from backend import audit as _audit
        await _audit.log(
            action="mfa.backup_codes.regenerated", entity_kind="mfa",
            entity_id=user.id, actor=user.email,
        )
    except Exception:
        pass
    return {"codes": codes, "count": len(codes)}


# ── WebAuthn registration ──

class WebAuthnRegisterBeginRequest(BaseModel):
    name: str = ""


@router.post("/auth/mfa/webauthn/register/begin")
async def webauthn_register_begin(
    req: WebAuthnRegisterBeginRequest,
    user: auth.User = Depends(auth.current_user),
) -> dict:
    options = await mfa.webauthn_begin_register(user.id, user.email, user.name)
    return options


class WebAuthnRegisterCompleteRequest(BaseModel):
    credential: dict
    name: str = ""


@router.post("/auth/mfa/webauthn/register/complete")
async def webauthn_register_complete(
    req: WebAuthnRegisterCompleteRequest,
    user: auth.User = Depends(auth.current_user),
) -> dict:
    ok = await mfa.webauthn_complete_register(user.id, req.credential, req.name)
    if not ok:
        raise HTTPException(status_code=400, detail="WebAuthn registration failed")
    try:
        from backend import audit as _audit
        await _audit.log(
            action="mfa.webauthn.registered", entity_kind="mfa",
            entity_id=user.id, after={"name": req.name or "Security Key"},
            actor=user.email,
        )
    except Exception:
        pass
    return {"status": "registered"}


@router.delete("/auth/mfa/webauthn/{mfa_id}")
async def webauthn_remove(mfa_id: str,
                          user: auth.User = Depends(auth.current_user)) -> dict:
    ok = await mfa.webauthn_remove(user.id, mfa_id)
    if not ok:
        raise HTTPException(status_code=404, detail="WebAuthn credential not found")
    try:
        from backend import audit as _audit
        await _audit.log(
            action="mfa.webauthn.removed", entity_kind="mfa",
            entity_id=user.id, after={"mfa_id": mfa_id},
            actor=user.email,
        )
    except Exception:
        pass
    return {"status": "removed"}


# ── MFA challenge (login flow) ──

class MFAChallengeRequest(BaseModel):
    mfa_token: str = Field(min_length=1)
    code: str = Field(min_length=1)


@router.post("/auth/mfa/challenge")
async def mfa_challenge(req: MFAChallengeRequest,
                        request: Request, response: Response) -> dict:
    """Verify TOTP or backup code after password auth returned mfa_required."""
    challenge = mfa.get_mfa_challenge(req.mfa_token)
    if not challenge:
        raise HTTPException(status_code=401, detail="MFA challenge expired or invalid")

    user_id = challenge["user_id"]
    code = req.code.strip()

    is_backup = "-" in code and len(code) == 9
    if is_backup:
        ok = await mfa.verify_backup_code(user_id, code)
    else:
        ok = await mfa.verify_totp(user_id, code)

    if not ok:
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    data = mfa.consume_mfa_challenge(req.mfa_token)
    if not data:
        data = challenge

    sess = await auth.create_session(
        user_id, ip=data.get("ip", ""), user_agent=data.get("user_agent", ""),
    )
    await mfa.mark_session_mfa_verified(sess.token)

    secure = _cookie_secure()
    response.set_cookie(
        key=auth.SESSION_COOKIE, value=sess.token,
        max_age=auth.SESSION_TTL_S, httponly=True, secure=secure, samesite="lax",
    )
    response.set_cookie(
        key=auth.CSRF_COOKIE, value=sess.csrf_token,
        max_age=auth.SESSION_TTL_S, httponly=False, secure=secure, samesite="lax",
    )

    user = await auth.get_user(user_id)
    try:
        from backend import audit as _audit
        await _audit.log(
            action="mfa.challenge.passed", entity_kind="auth",
            entity_id=user_id,
            after={"method": "backup_code" if is_backup else "totp"},
            actor=user.email if user else user_id,
            session_id=sess.token,
        )
    except Exception:
        pass

    return {
        "user": user.to_dict() if user else {},
        "csrf_token": sess.csrf_token,
        "mfa_verified": True,
    }


# ── WebAuthn challenge (login flow) ──

class WebAuthnChallengeBeginRequest(BaseModel):
    mfa_token: str = Field(min_length=1)


@router.post("/auth/mfa/webauthn/challenge/begin")
async def webauthn_challenge_begin(req: WebAuthnChallengeBeginRequest) -> dict:
    challenge = mfa.get_mfa_challenge(req.mfa_token)
    if not challenge:
        raise HTTPException(status_code=401, detail="MFA challenge expired or invalid")
    options = await mfa.webauthn_begin_authenticate(challenge["user_id"])
    return options


class WebAuthnChallengeCompleteRequest(BaseModel):
    mfa_token: str = Field(min_length=1)
    credential: dict


@router.post("/auth/mfa/webauthn/challenge/complete")
async def webauthn_challenge_complete(
    req: WebAuthnChallengeCompleteRequest,
    request: Request, response: Response,
) -> dict:
    challenge = mfa.get_mfa_challenge(req.mfa_token)
    if not challenge:
        raise HTTPException(status_code=401, detail="MFA challenge expired or invalid")

    user_id = challenge["user_id"]
    ok = await mfa.webauthn_complete_authenticate(user_id, req.credential)
    if not ok:
        raise HTTPException(status_code=401, detail="WebAuthn authentication failed")

    data = mfa.consume_mfa_challenge(req.mfa_token)
    if not data:
        data = challenge

    sess = await auth.create_session(
        user_id, ip=data.get("ip", ""), user_agent=data.get("user_agent", ""),
    )
    await mfa.mark_session_mfa_verified(sess.token)

    secure = _cookie_secure()
    response.set_cookie(
        key=auth.SESSION_COOKIE, value=sess.token,
        max_age=auth.SESSION_TTL_S, httponly=True, secure=secure, samesite="lax",
    )
    response.set_cookie(
        key=auth.CSRF_COOKIE, value=sess.csrf_token,
        max_age=auth.SESSION_TTL_S, httponly=False, secure=secure, samesite="lax",
    )

    user = await auth.get_user(user_id)
    try:
        from backend import audit as _audit
        await _audit.log(
            action="mfa.challenge.passed", entity_kind="auth",
            entity_id=user_id, after={"method": "webauthn"},
            actor=user.email if user else user_id,
            session_id=sess.token,
        )
    except Exception:
        pass

    return {
        "user": user.to_dict() if user else {},
        "csrf_token": sess.csrf_token,
        "mfa_verified": True,
    }
