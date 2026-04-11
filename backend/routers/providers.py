"""LLM provider configuration endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.agents.llm import get_llm, list_providers
from backend.config import settings

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("")
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

    llm_ready = llm is not None
    return {
        "status": "switched",
        "provider": settings.llm_provider,
        "model": settings.get_model_name(),
        "llm_active": llm_ready,
        "note": None if llm_ready else "No API key set — agents will use rule-based fallback.",
    }


async def _do_switch_provider(provider: str, model: str = "") -> None:
    """Internal helper to switch provider (called by auto-downgrade)."""
    from backend.agents.llm import _cache
    settings.llm_provider = provider
    settings.llm_model = model
    _cache.clear()  # Force re-init with new provider


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
