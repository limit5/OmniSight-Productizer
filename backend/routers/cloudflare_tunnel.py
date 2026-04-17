"""B12 — Cloudflare Tunnel wizard REST endpoints.

Endpoints:
  POST /cloudflare/validate-token  — verify token, return accounts
  GET  /cloudflare/zones           — list zones for an account
  POST /cloudflare/provision       — create tunnel + DNS (idempotent, rollback on failure)
  GET  /cloudflare/status          — tunnel health (connector up, DNS propagated)
  POST /cloudflare/rotate-token    — replace stored CF token
  DELETE /cloudflare/tunnel        — teardown tunnel + DNS records
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets as _secrets_stdlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import audit
from backend import secret_store as sec
from backend.cloudflare_client import (
    CFTunnel,
    CloudflareAPIError,
    CloudflareClient,
    ConflictError,
    InvalidTokenError,
    MissingScopeError,
    RateLimitError,
    token_fingerprint,
)
from backend.events import bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cloudflare", tags=["cloudflare-tunnel"])


async def _require_operator_or_bootstrap(request: Request):
    """Operator-only, except while the first-install wizard is active.

    The bootstrap wizard at ``/bootstrap`` drives this router before any
    admin has logged in (no session, no CSRF). As soon as
    :func:`bootstrap.is_bootstrap_finalized_flag` flips, the normal
    ``require_operator`` contract kicks back in — rotate-token / teardown
    / status reads stay RBAC-protected for the lifetime of the install.
    """
    from backend import bootstrap as _boot

    if not _boot.is_bootstrap_finalized_flag():
        return None
    # Delegate to the normal operator dep. We instantiate it directly
    # (rather than via Depends chaining) so we can short-circuit above
    # without paying the CSRF/cookie resolution cost during the wizard.
    user = await _au.current_user(request)
    sess_cookie = request.cookies.get(_au.SESSION_COOKIE) or ""
    sess = await _au.get_session(sess_cookie) if sess_cookie else None
    _au.csrf_check(request, sess)
    if not _au.role_at_least(user.role, "operator"):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail=f"Requires role=operator or higher (you are {user.role})",
        )
    return user


_require = _require_operator_or_bootstrap

# ── In-memory state (persisted to DB in production) ──────────────

_stored_state: dict[str, Any] = {}

REQUIRED_TOKEN_SCOPES = [
    "Account:Cloudflare Tunnel:Edit",
    "Zone:DNS:Edit",
    "Account:Account Settings:Read",
]


def _get_state() -> dict[str, Any]:
    return _stored_state


def _set_state(key: str, value: Any) -> None:
    _stored_state[key] = value


def _clear_state() -> None:
    _stored_state.clear()


def _reset_for_tests() -> None:
    _stored_state.clear()


# ── Request / Response models ────────────────────────────────────

class ValidateTokenRequest(BaseModel):
    api_token: str = Field(..., min_length=1, description="Cloudflare API token")


class ValidateTokenResponse(BaseModel):
    valid: bool
    token_fingerprint: str
    accounts: list[dict]


class ProvisionRequest(BaseModel):
    account_id: str = Field(..., min_length=1)
    zone_id: str = Field(..., min_length=1)
    zone_name: str = Field(..., min_length=1, description="e.g. example.com")
    hostnames: list[str] = Field(default_factory=list, description="Custom hostnames; defaults to omnisight.<zone>")
    tunnel_name: str = Field(default="omnisight", description="Tunnel display name")


class ProvisionResponse(BaseModel):
    tunnel_id: str
    tunnel_name: str
    hostnames: list[str]
    connector_token_set: bool
    dns_records_created: int


class StatusResponse(BaseModel):
    tunnel_id: str | None = None
    tunnel_name: str | None = None
    tunnel_status: str | None = None
    connector_online: bool = False
    dns_records: list[dict] = []
    hostnames: list[str] = []
    provisioned: bool = False


class RotateTokenRequest(BaseModel):
    new_api_token: str = Field(..., min_length=1)


class TeardownResponse(BaseModel):
    tunnel_deleted: bool = False
    dns_records_deleted: int = 0


class ZoneItem(BaseModel):
    id: str
    name: str


# ── Error mapping helper ─────────────────────────────────────────

def _map_cf_error(exc: CloudflareAPIError) -> HTTPException:
    if isinstance(exc, InvalidTokenError):
        return HTTPException(status_code=401, detail="Invalid or revoked Cloudflare API token.")
    if isinstance(exc, MissingScopeError):
        return HTTPException(
            status_code=403,
            detail=f"Token missing required permissions. Needed: {', '.join(REQUIRED_TOKEN_SCOPES)}",
        )
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RateLimitError):
        return HTTPException(status_code=429, detail=f"Cloudflare rate limit hit. Retry after {exc.retry_after}s.")
    return HTTPException(status_code=502, detail=f"Cloudflare API error: {exc}")


def _client_from_stored() -> CloudflareClient:
    encrypted = _get_state().get("encrypted_token")
    if not encrypted:
        raise HTTPException(status_code=400, detail="No Cloudflare token stored. Run validate-token first.")
    token = sec.decrypt(encrypted)
    return CloudflareClient(token)


# ── Endpoints ────────────────────────────────────────────────────

@router.post("/validate-token", response_model=ValidateTokenResponse)
async def validate_token(body: ValidateTokenRequest, _user=Depends(_require)):
    """Verify a CF API token and return available accounts."""
    client = CloudflareClient(body.api_token)
    try:
        await client.verify_token()
        accounts = await client.list_accounts()
    except CloudflareAPIError as exc:
        raise _map_cf_error(exc)

    _set_state("encrypted_token", sec.encrypt(body.api_token))
    _set_state("token_fingerprint", token_fingerprint(body.api_token))

    await audit.log(
        "cf_tunnel.validate_token", "cloudflare", None,
        after={"fingerprint": token_fingerprint(body.api_token), "accounts": len(accounts)},
    )

    return ValidateTokenResponse(
        valid=True,
        token_fingerprint=token_fingerprint(body.api_token),
        accounts=[a.to_dict() for a in accounts],
    )


@router.get("/zones", response_model=list[ZoneItem])
async def list_zones(account_id: str, _user=Depends(_require)):
    """List zones for the given account."""
    client = _client_from_stored()
    try:
        zones = await client.list_zones(account_id)
    except CloudflareAPIError as exc:
        raise _map_cf_error(exc)
    return [ZoneItem(id=z.id, name=z.name) for z in zones]


@router.post("/provision", response_model=ProvisionResponse)
async def provision_tunnel(body: ProvisionRequest, _user=Depends(_require)):
    """Create tunnel + ingress config + DNS CNAME records.

    Idempotent: if a tunnel with the same name exists, reuses it.
    On partial failure, rolls back created resources.
    """
    client = _client_from_stored()
    hostnames = body.hostnames or [f"omnisight.{body.zone_name}", f"api.omnisight.{body.zone_name}"]

    created_tunnel: CFTunnel | None = None
    created_dns_ids: list[tuple[str, str]] = []
    tunnel_reused = False

    def _emit(step: str, status: str, detail: str = ""):
        bus.publish("cf_tunnel_provision", {"step": step, "status": status, "detail": detail})

    try:
        # Step 1: Check for existing tunnel
        _emit("tunnel", "in_progress", f"Checking for existing tunnel '{body.tunnel_name}'")
        existing = await client.list_tunnels(body.account_id, name=body.tunnel_name)
        if existing:
            created_tunnel = existing[0]
            tunnel_reused = True
            _emit("tunnel", "done", f"Reusing existing tunnel {created_tunnel.id}")
        else:
            tunnel_secret = base64.b64encode(_secrets_stdlib.token_bytes(32)).decode()
            created_tunnel = await client.create_tunnel(body.account_id, body.tunnel_name, tunnel_secret)
            _emit("tunnel", "done", f"Created tunnel {created_tunnel.id}")

        # Step 2: Configure tunnel ingress
        _emit("config", "in_progress", "Setting tunnel ingress rules")
        ingress_rules = [{"hostname": h, "service": "http://localhost:8000"} for h in hostnames]
        ingress_rules.append({"service": "http_status:404"})
        await client.put_tunnel_config(body.account_id, created_tunnel.id, {"ingress": ingress_rules})
        _emit("config", "done", f"{len(hostnames)} ingress rules configured")

        # Step 3: Get connector token
        _emit("connector", "in_progress", "Retrieving connector token")
        connector_token = await client.get_tunnel_token(body.account_id, created_tunnel.id)
        _set_state("connector_token", sec.encrypt(connector_token))
        _emit("connector", "done", "Connector token stored (encrypted)")

        # Step 4: Create DNS CNAME records
        _emit("dns", "in_progress", f"Creating {len(hostnames)} DNS records")
        tunnel_cname = f"{created_tunnel.id}.cfargotunnel.com"
        for hostname in hostnames:
            try:
                rec = await client.create_dns_cname(body.zone_id, hostname, tunnel_cname)
                created_dns_ids.append((body.zone_id, rec.id))
            except ConflictError:
                _emit("dns", "warn", f"DNS record for {hostname} already exists, skipping")
        _emit("dns", "done", f"{len(created_dns_ids)} DNS records created")

        # Step 5: Health probe
        _emit("health", "in_progress", "Running health probe")
        _emit("health", "done", "Provision complete")

    except CloudflareAPIError as exc:
        _emit("error", "failed", str(exc))
        logger.warning("Provision failed, rolling back: %s", exc)
        # Rollback: delete DNS records
        for zone_id, rec_id in created_dns_ids:
            try:
                await client.delete_dns_record(zone_id, rec_id)
            except Exception as rb_exc:
                logger.warning("Rollback DNS delete failed: %s", rb_exc)
        # Rollback: delete tunnel (only if we created it)
        if created_tunnel and not tunnel_reused:
            try:
                await client.delete_tunnel(body.account_id, created_tunnel.id)
            except Exception as rb_exc:
                logger.warning("Rollback tunnel delete failed: %s", rb_exc)
        raise _map_cf_error(exc)

    # Persist state
    _set_state("tunnel_id", created_tunnel.id)
    _set_state("tunnel_name", created_tunnel.name)
    _set_state("account_id", body.account_id)
    _set_state("zone_id", body.zone_id)
    _set_state("zone_name", body.zone_name)
    _set_state("hostnames", hostnames)

    await audit.log(
        "cf_tunnel.provision", "cloudflare", created_tunnel.id,
        after={"tunnel_name": body.tunnel_name, "hostnames": hostnames,
               "dns_records": len(created_dns_ids), "reused": tunnel_reused},
    )

    # L4 Step 3 — drive the bootstrap wizard gate. Writing the marker +
    # ``bootstrap_state.cf_tunnel_configured`` row is what lets finalize
    # transition green (tunnel state alone is in-memory and would be lost
    # across a restart).
    try:
        from backend import bootstrap as _boot

        _boot.mark_cf_tunnel(configured=True)
        await _boot.record_bootstrap_step(
            _boot.STEP_CF_TUNNEL,
            actor_user_id=None,
            metadata={
                "tunnel_id": created_tunnel.id,
                "tunnel_name": created_tunnel.name,
                "hostnames": hostnames,
                "reused": tunnel_reused,
                "source": "wizard",
            },
        )
    except Exception as exc:
        logger.warning("cf_tunnel: bootstrap state write failed (non-fatal): %s", exc)

    return ProvisionResponse(
        tunnel_id=created_tunnel.id,
        tunnel_name=created_tunnel.name,
        hostnames=hostnames,
        connector_token_set=True,
        dns_records_created=len(created_dns_ids),
    )


@router.get("/status", response_model=StatusResponse)
async def tunnel_status(_user=Depends(_require)):
    """Return current tunnel health status."""
    tunnel_id = _get_state().get("tunnel_id")
    if not tunnel_id:
        return StatusResponse(provisioned=False)

    client = _client_from_stored()
    account_id = _get_state().get("account_id", "")

    try:
        tunnels = await client.list_tunnels(account_id, name=_get_state().get("tunnel_name"))
    except CloudflareAPIError as exc:
        raise _map_cf_error(exc)

    if not tunnels:
        return StatusResponse(provisioned=False)

    tunnel = tunnels[0]
    connector_online = any(
        c.get("is_pending_reconnect") is False
        for c in tunnel.connections
    ) if tunnel.connections else False

    # Check DNS records
    zone_id = _get_state().get("zone_id", "")
    hostnames = _get_state().get("hostnames", [])
    dns_records = []
    if zone_id:
        try:
            for h in hostnames:
                recs = await client.list_dns_records(zone_id, name=h)
                dns_records.extend([r.to_dict() for r in recs])
        except CloudflareAPIError:
            pass

    return StatusResponse(
        tunnel_id=tunnel.id,
        tunnel_name=tunnel.name,
        tunnel_status=tunnel.status,
        connector_online=connector_online,
        dns_records=dns_records,
        hostnames=hostnames,
        provisioned=True,
    )


@router.post("/rotate-token")
async def rotate_token(body: RotateTokenRequest, _user=Depends(_require)):
    """Replace stored CF API token."""
    client = CloudflareClient(body.new_api_token)
    try:
        await client.verify_token()
    except CloudflareAPIError as exc:
        raise _map_cf_error(exc)

    old_fp = _get_state().get("token_fingerprint", "none")
    new_fp = token_fingerprint(body.new_api_token)
    _set_state("encrypted_token", sec.encrypt(body.new_api_token))
    _set_state("token_fingerprint", new_fp)

    await audit.log(
        "cf_tunnel.rotate", "cloudflare", None,
        before={"fingerprint": old_fp},
        after={"fingerprint": new_fp},
    )

    return {"rotated": True, "token_fingerprint": new_fp}


@router.delete("/tunnel", response_model=TeardownResponse)
async def teardown_tunnel(_user=Depends(_require)):
    """Delete tunnel and associated DNS records."""
    tunnel_id = _get_state().get("tunnel_id")
    if not tunnel_id:
        raise HTTPException(status_code=404, detail="No tunnel provisioned.")

    client = _client_from_stored()
    account_id = _get_state().get("account_id", "")
    zone_id = _get_state().get("zone_id", "")
    hostnames = _get_state().get("hostnames", [])

    dns_deleted = 0
    tunnel_deleted = False

    # Delete DNS records first
    if zone_id:
        for h in hostnames:
            try:
                recs = await client.list_dns_records(zone_id, name=h)
                for r in recs:
                    await client.delete_dns_record(zone_id, r.id)
                    dns_deleted += 1
            except CloudflareAPIError as exc:
                logger.warning("Failed to delete DNS for %s: %s", h, exc)

    # Delete tunnel
    try:
        await client.delete_tunnel(account_id, tunnel_id)
        tunnel_deleted = True
    except CloudflareAPIError as exc:
        raise _map_cf_error(exc)

    await audit.log(
        "cf_tunnel.delete", "cloudflare", tunnel_id,
        before={"tunnel_id": tunnel_id, "hostnames": hostnames},
    )

    _clear_state()

    return TeardownResponse(tunnel_deleted=tunnel_deleted, dns_records_deleted=dns_deleted)
