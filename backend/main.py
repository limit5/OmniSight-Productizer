"""OmniSight Engine — FastAPI entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.routers import agents, artifacts, chat, events, health, invoke, providers, simulations, system, tasks, tools, webhooks, workspaces
from backend import db

@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    _log = logging.getLogger(__name__)
    try:
        await db.init()
        await agents.seed_defaults_if_empty()
        await tasks.seed_defaults_if_empty()
        await system.load_token_usage_from_db()
    except Exception as exc:
        _log.error("Startup failed: %s", exc, exc_info=True)
        raise
    yield
    await db.close()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Multi-Agent orchestration engine for embedded AI camera development.",
    lifespan=lifespan,
)

# CORS — allow Next.js frontend (dev: multiple origins)
_cors_origins = [
    settings.frontend_origin,
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(agents.router, prefix=settings.api_prefix)
app.include_router(tasks.router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)
app.include_router(tools.router, prefix=settings.api_prefix)
app.include_router(providers.router, prefix=settings.api_prefix)
app.include_router(invoke.router, prefix=settings.api_prefix)
app.include_router(events.router, prefix=settings.api_prefix)
app.include_router(workspaces.router, prefix=settings.api_prefix)
app.include_router(artifacts.router, prefix=settings.api_prefix)
app.include_router(webhooks.router, prefix=settings.api_prefix)
app.include_router(simulations.router, prefix=settings.api_prefix)
app.include_router(system.router, prefix=settings.api_prefix)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "online",
        "docs": "/docs",
        "api": settings.api_prefix,
    }
