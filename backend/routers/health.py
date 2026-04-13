from fastapi import APIRouter

from backend.models import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    return {
        "status": "online",
        "engine": "OmniSight Engine",
        "version": "0.1.0",
        "phase": "3.2",
    }
