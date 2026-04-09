from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    return {
        "status": "online",
        "engine": "OmniSight Engine",
        "version": "0.1.0",
        "phase": "3.2",
    }
