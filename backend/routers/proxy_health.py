"""KS.3.5/KS.3.7 -- BYOG proxy heartbeat and audit metadata endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.proxy_health import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ProxyAuditMetadata,
    ProxyHeartbeat,
    get_proxy_health,
    record_heartbeat,
    record_audit_metadata,
)


router = APIRouter(prefix="/byog/proxies", tags=["byog-proxy-health"])


class ProxyHeartbeatRequest(BaseModel):
    proxy_id: str | None = Field(default=None, min_length=1)
    tenant_id: str = ""
    status: str = "ok"
    service: str = "omnisight-proxy"
    provider_count: int = Field(default=0, ge=0)
    heartbeat_interval_seconds: int = Field(
        default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        gt=0,
    )


class ProxyAuditMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proxy_id: str | None = Field(default=None, min_length=1)
    tenant_id: str = ""
    provider: str = ""
    method: str = ""
    path: str = ""
    status_code: int = Field(default=0, ge=0)
    model: str = ""
    token_count: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    recorded_at: str = ""


@router.post("/{proxy_id}/heartbeat")
async def proxy_heartbeat(proxy_id: str, body: ProxyHeartbeatRequest) -> dict:
    """Record one BYOG proxy heartbeat.

    The mTLS / signed-nonce layer owns caller authentication for Tier 3
    deployments. This handler only records liveness and never accepts or
    stores LLM payload data.
    """
    body_proxy_id = (body.proxy_id or proxy_id).strip()
    if body_proxy_id != proxy_id:
        raise HTTPException(
            status_code=400,
            detail="body proxy_id must match path proxy_id",
        )
    heartbeat = record_heartbeat(
        ProxyHeartbeat(
            proxy_id=proxy_id,
            tenant_id=body.tenant_id,
            status=body.status,
            service=body.service,
            provider_count=body.provider_count,
            heartbeat_interval_seconds=body.heartbeat_interval_seconds,
        )
    )
    return {
        "status": "ok",
        "proxy_id": heartbeat.proxy_id,
        "stale_threshold_seconds": 60,
    }


@router.post("/{proxy_id}/audit")
async def proxy_audit_metadata(
    proxy_id: str,
    body: ProxyAuditMetadataRequest,
) -> dict:
    """Record BYOG proxy audit metadata only.

    Full prompt and response bodies remain customer-owned inside
    ``omnisight-proxy``. This SaaS endpoint rejects undeclared fields so
    prompt/response payloads cannot be persisted here by accident.
    """
    body_proxy_id = (body.proxy_id or proxy_id).strip()
    if body_proxy_id != proxy_id:
        raise HTTPException(
            status_code=400,
            detail="body proxy_id must match path proxy_id",
        )
    metadata = record_audit_metadata(
        ProxyAuditMetadata(
            proxy_id=proxy_id,
            tenant_id=body.tenant_id,
            provider=body.provider,
            method=body.method,
            path=body.path,
            status_code=body.status_code,
            model=body.model,
            token_count=body.token_count,
            prompt_tokens=body.prompt_tokens,
            completion_tokens=body.completion_tokens,
            total_tokens=body.total_tokens,
            recorded_at=body.recorded_at,
        )
    )
    return {
        "status": "ok",
        "proxy_id": metadata.proxy_id,
        "token_count": metadata.token_count,
    }


@router.get("/{proxy_id}/health")
async def proxy_health(proxy_id: str) -> dict:
    health = get_proxy_health(proxy_id)
    payload = {
        "proxy_id": health.proxy_id,
        "connected": health.connected,
        "stale": health.stale,
        "stale_threshold_seconds": health.stale_threshold_seconds,
        "last_heartbeat_at": health.last_heartbeat_at,
        "last_heartbeat_age_seconds": health.last_heartbeat_age_seconds,
    }
    if health.heartbeat is not None:
        payload["heartbeat"] = {
            "tenant_id": health.heartbeat.tenant_id,
            "status": health.heartbeat.status,
            "service": health.heartbeat.service,
            "provider_count": health.heartbeat.provider_count,
            "heartbeat_interval_seconds": (
                health.heartbeat.heartbeat_interval_seconds
            ),
        }
    return payload
