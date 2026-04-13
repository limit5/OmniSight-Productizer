"""OmniSight Engine — FastAPI entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.routers import agents, artifacts, chat, events, health, integration, invoke, providers, simulations, system, tasks, tools, webhooks, workspaces
from backend import db

async def _startup_cleanup(log):
    """Reset stuck states left over from a previous crash."""
    # 1. Reset agents stuck in "running" for over 1 hour
    n = await db.execute_raw(
        "UPDATE agents SET status='idle', thought_chain='[RECOVERY] Reset on startup' "
        "WHERE status='running' AND datetime(created_at) < datetime('now', '-1 hour')"
    )
    if n:
        log.warning("Startup cleanup: reset %d stuck agents to idle", n)
    # 2. Reset simulations stuck in "running"
    n = await db.execute_raw(
        "UPDATE simulations SET status='error' WHERE status='running'"
    )
    if n:
        log.warning("Startup cleanup: marked %d stuck simulations as error", n)
    # 3. Clean orphaned Docker containers
    try:
        from backend.container import cleanup_orphaned_containers
        removed = await cleanup_orphaned_containers()
        if removed:
            log.warning("Startup cleanup: removed %d orphaned containers", removed)
    except Exception:
        pass  # Docker may not be available
    # 4. Clean stale git lock files
    try:
        from backend.workspace import cleanup_stale_locks
        await cleanup_stale_locks()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    _log = logging.getLogger(__name__)
    try:
        await db.init()
        await _startup_cleanup(_log)
        await agents.seed_defaults_if_empty()
        await tasks.seed_defaults_if_empty()
        await system.load_token_usage_from_db()
    except Exception as exc:
        _log.error("Startup failed: %s", exc, exc_info=True)
        raise
    # Start watchdog for stuck agent detection
    import asyncio
    watchdog_task = asyncio.create_task(invoke.run_watchdog())
    yield
    watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
    await db.close()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Multi-Agent orchestration engine for embedded AI camera development.",
    lifespan=lifespan,
)

# CORS — configurable origins (hardcoded localhost only in debug mode)
_cors_origins = [settings.frontend_origin]
if settings.extra_cors_origins:
    _cors_origins.extend(o.strip() for o in settings.extra_cors_origins.split(",") if o.strip())
if settings.debug:
    # Dev convenience: add localhost variants
    for dev_origin in ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3001"]:
        if dev_origin not in _cors_origins:
            _cors_origins.append(dev_origin)
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
app.include_router(integration.router, prefix=settings.api_prefix)
app.include_router(system.router, prefix=settings.api_prefix)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "online",
        "docs": "/docs",
        "api": settings.api_prefix,
    }
