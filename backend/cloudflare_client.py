"""B12 — Cloudflare Tunnel wizard: CF API v4 wrapper.

Thin async client for the Cloudflare API endpoints needed by the
one-click tunnel provisioner:
  - Account listing (from token scope)
  - Zone listing per account
  - Tunnel CRUD (create / list / delete / get-token)
  - DNS record CRUD (CNAME for tunnel ingress)

All methods raise `CloudflareAPIError` subclasses with structured
info so the router can map them to appropriate HTTP status codes.

Token values are NEVER logged — only fingerprints (last-4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

import httpx

logger = logging.getLogger(__name__)

CF_API_BASE = "https://api.cloudflare.com/client/v4"


# ── Error hierarchy ──────────────────────────────────────────────

class CloudflareAPIError(Exception):
    """Base for all CF API errors."""

    def __init__(self, message: str, status: int = 0, cf_errors: list[dict] | None = None):
        super().__init__(message)
        self.status = status
        self.cf_errors = cf_errors or []


class InvalidTokenError(CloudflareAPIError):
    """401 — token is invalid or revoked."""


class MissingScopeError(CloudflareAPIError):
    """403 — token lacks a required permission."""

    def __init__(self, message: str, missing_scopes: list[str] | None = None, **kw):
        super().__init__(message, **kw)
        self.missing_scopes = missing_scopes or []


class ConflictError(CloudflareAPIError):
    """409 — resource already exists (tunnel name, DNS record)."""


class RateLimitError(CloudflareAPIError):
    """429 — CF rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


# ── Data models ──────────────────────────────────────────────────

@dataclass
class CFAccount:
    id: str
    name: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name}


@dataclass
class CFZone:
    id: str
    name: str
    account_id: str
    status: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "account_id": self.account_id, "status": self.status}


class TunnelStatus(str, Enum):
    active = "active"
    inactive = "inactive"
    degraded = "degraded"


@dataclass
class CFTunnel:
    id: str
    name: str
    status: str
    created_at: str
    account_id: str
    connections: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "status": self.status,
            "created_at": self.created_at, "account_id": self.account_id,
            "connections": self.connections,
        }


@dataclass
class CFDNSRecord:
    id: str
    name: str
    type: str
    content: str
    zone_id: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "type": self.type,
                "content": self.content, "zone_id": self.zone_id}


# ── Helpers ──────────────────────────────────────────────────────

def token_fingerprint(token: str) -> str:
    if len(token) <= 8:
        return "****"
    return f"…{token[-4:]}"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _raise_for_cf(resp: httpx.Response) -> None:
    """Map CF error responses to typed exceptions."""
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    errors = body.get("errors", [])
    msg = errors[0].get("message", resp.text) if errors else resp.text

    if resp.status_code == 401:
        raise InvalidTokenError(msg, status=401, cf_errors=errors)
    if resp.status_code == 403:
        raise MissingScopeError(msg, status=403, cf_errors=errors)
    if resp.status_code == 409:
        raise ConflictError(msg, status=409, cf_errors=errors)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise RateLimitError(msg, retry_after=retry, status=429, cf_errors=errors)
    raise CloudflareAPIError(msg, status=resp.status_code, cf_errors=errors)


# ── Client ───────────────────────────────────────────────────────

class CloudflareClient:
    """Async Cloudflare API v4 client (httpx-based)."""

    def __init__(self, token: str, *, timeout: float = 30.0):
        self._token = token
        self._timeout = timeout

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(f"{CF_API_BASE}{path}", headers=_headers(self._token), params=params)
        _raise_for_cf(resp)
        return resp.json()

    async def _post(self, path: str, json_body: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(f"{CF_API_BASE}{path}", headers=_headers(self._token), json=json_body)
        _raise_for_cf(resp)
        return resp.json()

    async def _delete(self, path: str, json_body: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request("DELETE", f"{CF_API_BASE}{path}", headers=_headers(self._token), json=json_body or {})
        _raise_for_cf(resp)
        return resp.json()

    # ── Token verification ──

    async def verify_token(self) -> dict:
        """Call /user/tokens/verify. Returns the token details on success."""
        data = await self._get("/user/tokens/verify")
        return data.get("result", {})

    # ── Accounts ──

    async def list_accounts(self) -> list[CFAccount]:
        data = await self._get("/accounts", params={"per_page": "50"})
        return [CFAccount(id=a["id"], name=a["name"]) for a in data.get("result", [])]

    # ── Zones ──

    async def list_zones(self, account_id: str) -> list[CFZone]:
        data = await self._get("/zones", params={"account.id": account_id, "per_page": "50", "status": "active"})
        return [
            CFZone(id=z["id"], name=z["name"], account_id=z["account"]["id"], status=z["status"])
            for z in data.get("result", [])
        ]

    # ── Tunnels ──

    async def list_tunnels(self, account_id: str, name: str | None = None) -> list[CFTunnel]:
        params: dict[str, str] = {"per_page": "50", "is_deleted": "false"}
        if name:
            params["name"] = name
        data = await self._get(f"/accounts/{account_id}/cfd_tunnel", params=params)
        return [
            CFTunnel(
                id=t["id"], name=t["name"], status=t.get("status", "unknown"),
                created_at=t.get("created_at", ""), account_id=account_id,
                connections=t.get("connections", []),
            )
            for t in data.get("result", [])
        ]

    async def create_tunnel(self, account_id: str, name: str, tunnel_secret: str) -> CFTunnel:
        body = {"name": name, "tunnel_secret": tunnel_secret}
        data = await self._post(f"/accounts/{account_id}/cfd_tunnel", json_body=body)
        r = data["result"]
        return CFTunnel(
            id=r["id"], name=r["name"], status=r.get("status", "inactive"),
            created_at=r.get("created_at", ""), account_id=account_id,
        )

    async def get_tunnel_token(self, account_id: str, tunnel_id: str) -> str:
        data = await self._get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
        return data.get("result", "")

    async def put_tunnel_config(self, account_id: str, tunnel_id: str, config: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.put(
                f"{CF_API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
                headers=_headers(self._token),
                json={"config": config},
            )
        _raise_for_cf(resp)
        return resp.json()

    async def delete_tunnel(self, account_id: str, tunnel_id: str) -> None:
        await self._delete(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}")

    # ── DNS ──

    async def list_dns_records(self, zone_id: str, name: str | None = None, record_type: str = "CNAME") -> list[CFDNSRecord]:
        params: dict[str, str] = {"type": record_type, "per_page": "50"}
        if name:
            params["name"] = name
        data = await self._get(f"/zones/{zone_id}/dns_records", params=params)
        return [
            CFDNSRecord(id=r["id"], name=r["name"], type=r["type"], content=r["content"], zone_id=zone_id)
            for r in data.get("result", [])
        ]

    async def create_dns_cname(self, zone_id: str, name: str, target: str, proxied: bool = True) -> CFDNSRecord:
        body = {"type": "CNAME", "name": name, "content": target, "proxied": proxied, "ttl": 1}
        data = await self._post(f"/zones/{zone_id}/dns_records", json_body=body)
        r = data["result"]
        return CFDNSRecord(id=r["id"], name=r["name"], type=r["type"], content=r["content"], zone_id=zone_id)

    async def delete_dns_record(self, zone_id: str, record_id: str) -> None:
        await self._delete(f"/zones/{zone_id}/dns_records/{record_id}")
