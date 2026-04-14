"""OmniSight Engine — FastAPI entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.routers import agents, artifacts, chat, events, health, integration, invoke, providers, simulations, system, tasks, tools, webhooks, workflow as wf_router, workspaces
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
    # Phase 52: configure structlog JSON output if requested
    from backend import structlog_setup as _sl
    _sl.configure()
    import logging
    _log = logging.getLogger(__name__)
    try:
        await db.init()
        await _startup_cleanup(_log)
        await agents.seed_defaults_if_empty()
        await tasks.seed_defaults_if_empty()
        await system.load_token_usage_from_db()
        # A1: restore operator-defined decision rules (Phase 50B) from DB
        from backend import decision_rules as _dr
        loaded = await _dr.load_from_db()
        _log.info("Decision rules loaded from DB: %d", loaded)
        # Phase 58: restore active decision profile
        from backend import decision_profiles as _dp
        prof_id = await _dp.load_from_db()
        if prof_id:
            _log.info("Decision profile restored: %s", prof_id)
        # Phase 54: bootstrap a default admin if users table is empty
        # (preserves the single-user dev flow when no SSO configured).
        from backend import auth as _auth
        bootstrapped = await _auth.ensure_default_admin()
        if bootstrapped:
            _log.warning(
                "[AUTH] default admin bootstrapped: %s — change password before sharing!",
                bootstrapped.email,
            )
        # Trim expired sessions on every cold start.
        try:
            removed = await _auth.cleanup_expired_sessions()
            if removed:
                _log.info("[AUTH] purged %d expired sessions", removed)
        except Exception as exc:
            _log.debug("session cleanup failed (non-fatal): %s", exc)
        # Phase 56: surface workflow runs that were still 'running' when
        # the previous process died — operators can /workflow/in-flight
        # to review and decide whether to resume.
        try:
            from backend import workflow as _wf
            in_flight = await _wf.list_in_flight_on_startup()
            if in_flight:
                _log.warning(
                    "[STARTUP] %d workflow run(s) left in-flight by previous "
                    "process: %s",
                    len(in_flight),
                    ", ".join(f"{r.id}({r.kind})" for r in in_flight[:5]),
                )
        except Exception as exc:
            _log.debug("workflow in-flight scan failed (non-fatal): %s", exc)
    except Exception as exc:
        _log.error("Startup failed: %s", exc, exc_info=True)
        raise
    # Start watchdog for stuck agent detection
    import asyncio
    watchdog_task = asyncio.create_task(invoke.run_watchdog())
    # Phase 47D: DecisionEngine timeout sweep (30 s cadence)
    from backend import decision_engine as _de
    sweep_task = asyncio.create_task(_de.run_sweep_loop())
    # Phase 52: Webhook DLQ retry worker
    from backend import notifications as _notif
    dlq_task = asyncio.create_task(_notif.run_dlq_loop())
    yield
    for t in (watchdog_task, sweep_task, dlq_task):
        t.cancel()
        try:
            await t
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
app.include_router(wf_router.router, prefix=settings.api_prefix)
from backend.routers import audit as _audit_router  # Phase 53
app.include_router(_audit_router.router, prefix=settings.api_prefix)
from backend.routers import profile as _profile_router  # Phase 58
app.include_router(_profile_router.router, prefix=settings.api_prefix)
from backend.routers import projects as _projects_router  # Phase 61
app.include_router(_projects_router.router, prefix=settings.api_prefix)
from backend.routers import auth as _auth_router  # Phase 54
app.include_router(_auth_router.router, prefix=settings.api_prefix)
from backend.routers import observability as _obs_router  # Phase 52
app.include_router(_obs_router.router, prefix=settings.api_prefix)
from backend.routers import skills as _skills_router  # Phase 62
app.include_router(_skills_router.router, prefix=settings.api_prefix)
app.include_router(workspaces.router, prefix=settings.api_prefix)
app.include_router(artifacts.router, prefix=settings.api_prefix)
app.include_router(webhooks.router, prefix=settings.api_prefix)
app.include_router(simulations.router, prefix=settings.api_prefix)
app.include_router(integration.router, prefix=settings.api_prefix)
app.include_router(system.router, prefix=settings.api_prefix)
from backend.routers import decisions as _decisions_router  # Phase 47A
app.include_router(_decisions_router.router, prefix=settings.api_prefix)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "online",
        "docs": "/docs",
        "api": settings.api_prefix,
    }
