"""LLM provider configuration endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.agents.llm import get_llm, list_providers
from backend.config import settings
from backend.models import ProvidersListResponse, ProviderHealthResponse

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=ProvidersListResponse)
async def get_providers():
    """List all supported LLM providers and their configuration status."""
    return {
        "active_provider": settings.llm_provider,
        "active_model": settings.get_model_name(),
        "providers": list_providers(),
    }


class SwitchProviderRequest(BaseModel):
    provider: str
    model: str | None = None


@router.post("/switch")
async def switch_provider(body: SwitchProviderRequest):
    """Switch the active LLM provider (runtime only, not persisted).

    To persist, set OMNISIGHT_LLM_PROVIDER in .env.
    """
    valid_ids = {p["id"] for p in list_providers()}
    if body.provider not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {body.provider}. Valid: {sorted(valid_ids)}",
        )

    # Try to initialize — warn if it fails but allow the switch
    llm = get_llm(provider=body.provider, model=body.model)

    # Update runtime settings
    settings.llm_provider = body.provider
    if body.model:
        settings.llm_model = body.model
    else:
        settings.llm_model = ""

    from backend.agents.llm import _cache
    _cache.clear()

    llm_ready = llm is not None
    # Emit SSE event so other UI panels (Orchestrator, Settings) can sync
    from backend.events import emit_invoke
    emit_invoke("provider_switch", f"{settings.llm_provider}/{settings.get_model_name()}")
    return {
        "status": "switched",
        "provider": settings.llm_provider,
        "model": settings.get_model_name(),
        "llm_active": llm_ready,
        "note": None if llm_ready else "No API key set — agents will use rule-based fallback.",
    }


@router.get("/health", response_model=ProviderHealthResponse)
async def get_provider_health():
    """Return health status for each provider in the fallback chain.

    M3: ``circuits`` lists every per-tenant per-key circuit currently
    tracked, scoped to the calling tenant.  The legacy ``health`` shape
    is preserved (driven by the global cooldown dict) so older clients
    keep working.
    """
    import time
    from backend.agents.llm import _provider_failures, PROVIDER_COOLDOWN
    from backend import circuit_breaker
    from backend.db_context import current_tenant_id

    chain = [p.strip() for p in settings.llm_fallback_chain.split(",") if p.strip()]
    all_providers = {p["id"]: p for p in list_providers()}
    now = time.time()
    tenant_id = current_tenant_id() or "t-default"

    health = []
    for pid in chain:
        info = all_providers.get(pid, {})
        last_fail = _provider_failures.get(pid, 0)
        cooldown_remaining = max(0, int(PROVIDER_COOLDOWN - (now - last_fail))) if last_fail else 0
        # Per-tenant per-key circuit overrides the global view when open.
        fp = circuit_breaker.active_fingerprint(pid)
        ct_open = circuit_breaker.is_open(tenant_id, pid, fp)
        ct_remaining = circuit_breaker.cooldown_remaining(tenant_id, pid, fp)
        if ct_open and ct_remaining > cooldown_remaining:
            cooldown_remaining = ct_remaining
        health.append({
            "id": pid,
            "name": info.get("name", pid),
            "configured": info.get("configured", False),
            "is_active": pid == settings.llm_provider,
            "last_failure": last_fail if last_fail else None,
            "cooldown_remaining": cooldown_remaining,
            "status": "active" if pid == settings.llm_provider
                else "cooldown" if cooldown_remaining > 0
                else "available" if info.get("configured")
                else "unconfigured",
        })
    return {
        "chain": chain,
        "health": health,
    }


@router.get("/circuits")
async def get_circuit_breakers(scope: str = "tenant"):
    """M3 — Per-tenant per-provider per-key circuit breaker snapshot.

    Query params:
        scope: ``tenant`` (default) returns only the calling tenant's
               keys.  ``all`` returns every tenant (admin diagnostics).

    Response::

        {
          "tenant_id": "t-foo",
          "scope": "tenant",
          "cooldown_seconds": 300,
          "circuits": [
            {
              "tenant_id": "t-foo",
              "provider": "openai",
              "fingerprint": "\u2026abcd",
              "open": true,
              "cooldown_remaining": 247,
              "failure_count": 3,
              "reason": "401 invalid_api_key",
              ...
            },
            ...
          ]
        }
    """
    from backend import circuit_breaker
    from backend.db_context import current_tenant_id

    tid = current_tenant_id() or "t-default"
    if scope == "all":
        circuits = circuit_breaker.snapshot()
    else:
        circuits = circuit_breaker.snapshot(tenant_id=tid)
    return {
        "tenant_id": tid,
        "scope": scope,
        "cooldown_seconds": circuit_breaker.COOLDOWN_SECONDS,
        "circuits": circuits,
    }


class CircuitResetRequest(BaseModel):
    provider: str | None = None
    fingerprint: str | None = None
    scope: str = "tenant"  # "tenant" | "all"


@router.post("/circuits/reset")
async def reset_circuit_breaker(body: CircuitResetRequest):
    """Operator override: clear the per-tenant per-key circuit state.

    Defaults to the calling tenant.  ``scope="all"`` clears every
    tenant matching the optional ``provider``/``fingerprint`` filters.
    """
    from backend import circuit_breaker
    from backend.db_context import current_tenant_id

    tid = current_tenant_id() or "t-default"
    target_tid = None if body.scope == "all" else tid
    cleared = circuit_breaker.reset(
        tenant_id=target_tid,
        provider=body.provider,
        fingerprint=body.fingerprint,
    )
    return {"status": "reset", "cleared": cleared, "tenant_id": tid, "scope": body.scope}


class FallbackChainRequest(BaseModel):
    chain: list[str]


@router.put("/fallback-chain")
async def update_fallback_chain(body: FallbackChainRequest):
    """Update the LLM fallback chain order (runtime only)."""
    valid_ids = {p["id"] for p in list_providers()}
    invalid = [p for p in body.chain if p not in valid_ids]
    if invalid:
        raise HTTPException(400, f"Unknown provider(s): {invalid}")
    settings.llm_fallback_chain = ",".join(body.chain)
    from backend.agents.llm import _cache
    _cache.clear()
    return {"status": "updated", "chain": body.chain}


@router.get("/test")
async def test_provider():
    """Quick test of the current LLM provider."""
    llm = get_llm()
    if llm is None:
        return {
            "status": "unavailable",
            "provider": settings.llm_provider,
            "model": settings.get_model_name(),
            "message": "No API key configured or provider failed to init. System uses rule-based fallback.",
        }
    try:
        resp = llm.invoke("Reply with exactly: OMNISIGHT_OK")
        return {
            "status": "ok",
            "provider": settings.llm_provider,
            "model": settings.get_model_name(),
            "response": resp.content[:200] if hasattr(resp, "content") else str(resp)[:200],
        }
    except Exception as exc:
        return {
            "status": "error",
            "provider": settings.llm_provider,
            "model": settings.get_model_name(),
            "error": str(exc)[:300],
        }


@router.get("/validate/{model_spec:path}")
async def validate_model(model_spec: str):
    """Validate a model spec and check provider API key availability.

    Examples: /providers/validate/openrouter:qwen/qwen3-235b
              /providers/validate/claude-sonnet-4
    """
    from backend.agents.llm import validate_model_spec
    return validate_model_spec(model_spec)
