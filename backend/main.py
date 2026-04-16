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
    # L1-03: sanity-check critical env/config BEFORE opening the DB.
    # Catches deploy-day typos (wrong bearer, missing provider key)
    # at boot instead of the first 401 / first silent provider fail.
    # Strict mode (refuse to boot on hard errors) auto-enables when
    # debug=False; dev stays lenient.
    from backend.config import validate_startup_config, ConfigValidationError
    try:
        validate_startup_config()
    except ConfigValidationError as exc:
        _log.error("config validation failed: %s", exc)
        raise
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
        # K6: migrate legacy OMNISIGHT_DECISION_BEARER env to api_keys table.
        from backend import api_keys as _api_keys
        legacy_key = await _api_keys.migrate_legacy_bearer()
        if legacy_key:
            _log.warning(
                "[K6] Legacy bearer env migrated to api_keys row %s. "
                "Create per-service keys and remove OMNISIGHT_DECISION_BEARER.",
                legacy_key.id,
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
        # Phase 56-DAG-C S3: sync shipped prompt markdown files into
        # prompt_versions so the canary layer has a baseline to compare
        # against on day 1. Best-effort — don't fail startup.
        try:
            from backend import prompt_registry as _pr
            outcomes = await _pr.bootstrap_from_disk()
            reg = [p for p, a in outcomes if a == "registered"]
            if reg:
                _log.info("prompt_registry bootstrap: registered %d", len(reg))
        except Exception as exc:
            _log.debug("prompt_registry bootstrap failed (non-fatal): %s", exc)
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
    # Phase 63-D: Daily IQ benchmark loop (opt-in L3, gated by env).
    from backend import iq_nightly as _iq
    iq_task = asyncio.create_task(_iq.run_nightly_loop())
    # Phase 65 S4: Fine-tune nightly loop (opt-in L4, gated by env).
    from backend import finetune_nightly as _ft
    ft_task = asyncio.create_task(_ft.run_nightly_loop())
    # Phase 63-E: Memory decay loop (opt-in L3, gated by env).
    from backend import memory_decay as _md
    md_task = asyncio.create_task(_md.run_decay_loop())
    # I6: DRF per-tenant sandbox capacity grace deadline sweep
    from backend import sandbox_capacity as _sc
    drf_task = asyncio.create_task(_sc.run_sweep_loop())
    yield
    for t in (watchdog_task, sweep_task, dlq_task, iq_task, ft_task, md_task, drf_task):
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internet-exposure auth S4 — security response headers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Defense-in-depth for the exposed URL. Cloudflare's edge will add
# some of these too; we set them at the origin so a future non-CF
# path (custom domain, on-prem) still gets them.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  K1 — force password change middleware
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_PASSWORD_CHANGE_EXEMPT = {
    "/auth/change-password", "/auth/login", "/auth/logout",
    "/auth/whoami", "/health",
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  I9 — Per-IP / per-user / per-tenant rate limiting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_RATE_LIMIT_EXEMPT = {"/health", "/auth/login", "/auth/logout"}


@app.middleware("http")
async def _rate_limit_gate(request, call_next):
    """I9: Three-dimensional rate limiting (IP, user, tenant).

    Limits are derived from the tenant's plan via quota.py.
    Login endpoints have their own dedicated K2 limiters and are
    exempt here to avoid double-counting.
    """
    from starlette.responses import JSONResponse as StarletteJSON

    rel = request.url.path.removeprefix(settings.api_prefix)
    if rel in _RATE_LIMIT_EXEMPT:
        return await call_next(request)

    from backend.rate_limit import get_limiter
    from backend.quota import quota_for_plan

    limiter = get_limiter()

    # --- resolve client IP ---
    client_ip = (request.headers.get("cf-connecting-ip") or "").strip()
    if not client_ip:
        client_ip = (request.client.host if request.client else "") or "unknown"

    # --- resolve user + tenant (best-effort, no auth required) ---
    user_id: str | None = None
    tenant_id: str | None = None
    plan = "free"

    from backend import auth as _auth
    if _auth.auth_mode() != "open":
        cookie = request.cookies.get(_auth.SESSION_COOKIE) or ""
        if cookie:
            sess = await _auth.get_session(cookie)
            if sess:
                user_obj = await _auth.get_user(sess.user_id)
                if user_obj:
                    user_id = user_obj.id
                    tenant_id = user_obj.tenant_id
    else:
        tenant_id = request.headers.get("x-tenant-id") or "t-default"

    if tenant_id:
        try:
            from backend.db import _conn
            async with _conn().execute(
                "SELECT plan FROM tenants WHERE id = ?", (tenant_id,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    plan = row[0] or "free"
        except Exception:
            pass

    quota = quota_for_plan(plan)

    # --- per-IP check ---
    ip_ok, ip_wait = limiter.allow(
        f"api:ip:{client_ip}", quota.per_ip.capacity, quota.per_ip.window_seconds,
    )
    if not ip_ok:
        retry = int(ip_wait) + 1
        return StarletteJSON(
            status_code=429,
            content={"detail": f"IP rate limit exceeded; retry in {retry}s"},
            headers={"Retry-After": str(retry)},
        )

    # --- per-user check ---
    if user_id:
        user_ok, user_wait = limiter.allow(
            f"api:user:{user_id}", quota.per_user.capacity, quota.per_user.window_seconds,
        )
        if not user_ok:
            retry = int(user_wait) + 1
            return StarletteJSON(
                status_code=429,
                content={"detail": f"User rate limit exceeded; retry in {retry}s"},
                headers={"Retry-After": str(retry)},
            )

    # --- per-tenant check ---
    if tenant_id:
        tenant_ok, tenant_wait = limiter.allow(
            f"api:tenant:{tenant_id}", quota.per_tenant.capacity, quota.per_tenant.window_seconds,
        )
        if not tenant_ok:
            retry = int(tenant_wait) + 1
            return StarletteJSON(
                status_code=429,
                content={"detail": f"Tenant rate limit exceeded; retry in {retry}s"},
                headers={"Retry-After": str(retry)},
            )

    response = await call_next(request)

    # Inject rate-limit headers for observability
    response.headers["X-RateLimit-Plan"] = plan
    if user_id:
        response.headers["X-RateLimit-User"] = user_id
    if tenant_id:
        response.headers["X-RateLimit-Tenant"] = tenant_id

    return response


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  K6 — API key scope enforcement
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.middleware("http")
async def _api_key_scope_gate(request, call_next):
    """If the request carries a per-key bearer token (K6), pre-validate
    the key's scope whitelist BEFORE the request reaches route handlers.
    The api_key attribute is set by auth.current_user during dependency
    injection, but Starlette middleware runs first, so we do an early
    check here and let current_user re-validate later."""
    raw = (request.headers.get("authorization") or "")
    if raw.startswith("Bearer "):
        raw = raw[len("Bearer "):]
    if raw and raw.startswith("omni_"):
        from backend import api_keys as _ak
        ip = request.client.host if request.client else ""
        key = await _ak.validate_bearer(raw, ip=ip)
        if key:
            rel_path = request.url.path.removeprefix(settings.api_prefix)
            if not key.scope_allows(rel_path):
                from starlette.responses import JSONResponse as StarletteJSON
                return StarletteJSON(
                    status_code=403,
                    content={"detail": f"API key scope does not allow access to {rel_path}"},
                )
            request.state.api_key = key
    return await call_next(request)


@app.middleware("http")
async def _tenant_header_gate(request, call_next):
    """I7: Validate X-Tenant-Id header against the authenticated user's tenant.

    If the header is present, it must match the user's own tenant_id
    (or the user must be an admin to switch tenants). Sets the
    request-scoped db_context tenant_id so downstream RLS picks it up.
    """
    header_tid = request.headers.get("x-tenant-id")
    if not header_tid:
        return await call_next(request)

    from backend import auth as _auth
    from backend import db_context

    if _auth.auth_mode() == "open":
        db_context.set_tenant_id(header_tid)
        return await call_next(request)

    cookie = request.cookies.get(_auth.SESSION_COOKIE) or ""
    if not cookie:
        return await call_next(request)
    sess = await _auth.get_session(cookie)
    if not sess:
        return await call_next(request)
    user = await _auth.get_user(sess.user_id)
    if not user:
        return await call_next(request)

    if header_tid != user.tenant_id and user.role != "admin":
        from starlette.responses import JSONResponse as StarletteJSON
        return StarletteJSON(
            status_code=403,
            content={"detail": f"Tenant {header_tid} not accessible"},
        )
    db_context.set_tenant_id(header_tid)
    return await call_next(request)


@app.middleware("http")
async def _must_change_password_gate(request, call_next):
    from starlette.responses import JSONResponse as StarletteJSON
    path = request.url.path
    rel = path.removeprefix(settings.api_prefix)
    if rel in _PASSWORD_CHANGE_EXEMPT or path in ("/", "/docs", "/openapi.json", "/redoc"):
        return await call_next(request)
    from backend import auth as _auth
    if _auth.auth_mode() == "open":
        return await call_next(request)
    cookie = request.cookies.get(_auth.SESSION_COOKIE) or ""
    if not cookie:
        return await call_next(request)
    sess = await _auth.get_session(cookie)
    if not sess:
        return await call_next(request)
    user = await _auth.get_user(sess.user_id)
    if user and user.must_change_password:
        return StarletteJSON(
            status_code=428,
            content={
                "detail": "Password change required before accessing any API. "
                          "POST /api/v1/auth/change-password with "
                          "{current_password, new_password}.",
            },
        )
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    # Tell browsers "only come back over HTTPS for the next 6 months".
    # Safe behind Cloudflare Tunnel (TLS is already terminated at CF's
    # edge). Setting `includeSubDomains` protects api.* and staging.*
    # on the same zone.
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=15552000; includeSubDomains",
    )
    # Refuse to be framed — neutralises most clickjacking vectors.
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Disable MIME sniffing — stops browsers from reinterpreting a
    # JSON response as HTML.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # Strip the Referer on cross-origin so our routes don't leak in
    # other sites' analytics.
    response.headers.setdefault(
        "Referrer-Policy", "strict-origin",
    )
    # Minimal Permissions-Policy — we don't use these APIs, deny them.
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    # CSP. The dashboard is a single Next.js app talking to our own
    # API + optional Cloudflare tunnel subdomains. Kept strict but
    # allow inline styles (Tailwind generates some) and blob: for
    # SVG/image previews. 'unsafe-eval' is intentionally omitted;
    # Next 16 client bundles don't need it for prod builds.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "connect-src 'self' https:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    return response


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
from backend.routers import mfa as _mfa_router  # K5/MFA
app.include_router(_mfa_router.router, prefix=settings.api_prefix)
from backend.routers import observability as _obs_router  # Phase 52
app.include_router(_obs_router.router, prefix=settings.api_prefix)
from backend.routers import skills as _skills_router  # Phase 62
app.include_router(_skills_router.router, prefix=settings.api_prefix)
from backend.routers import dag as _dag_router  # Phase 56-DAG-D
app.include_router(_dag_router.router, prefix=settings.api_prefix)
app.include_router(workspaces.router, prefix=settings.api_prefix)
app.include_router(artifacts.router, prefix=settings.api_prefix)
app.include_router(webhooks.router, prefix=settings.api_prefix)
app.include_router(simulations.router, prefix=settings.api_prefix)
app.include_router(integration.router, prefix=settings.api_prefix)
from backend.routers import secrets as _secrets_router  # I4/TENANT-SECRETS
app.include_router(_secrets_router.router, prefix=settings.api_prefix)
app.include_router(system.router, prefix=settings.api_prefix)
from backend.routers import decisions as _decisions_router  # Phase 47A
app.include_router(_decisions_router.router, prefix=settings.api_prefix)
from backend.routers import memory as _memory_router  # Phase 63-E
app.include_router(_memory_router.router, prefix=settings.api_prefix)
from backend.routers import intent as _intent_router  # Phase 68-C
app.include_router(_intent_router.router, prefix=settings.api_prefix)
from backend.routers import report as _report_router  # B3/REPORT-01
app.include_router(_report_router.router, prefix=settings.api_prefix)
from backend.routers import hil as _hil_router  # C7/HIL-PLUGIN-API
app.include_router(_hil_router.router, prefix=settings.api_prefix)
from backend.routers import compliance as _compliance_router  # C8/COMPLIANCE-HARNESS
app.include_router(_compliance_router.router, prefix=settings.api_prefix)
from backend.routers import safety as _safety_router  # C9/SAFETY-COMPLIANCE
app.include_router(_safety_router.router, prefix=settings.api_prefix)
from backend.routers import radio as _radio_router  # C10/RADIO-COMPLIANCE
app.include_router(_radio_router.router, prefix=settings.api_prefix)
from backend.routers import power as _power_router  # C11/POWER-PROFILING
app.include_router(_power_router.router, prefix=settings.api_prefix)
from backend.routers import realtime as _realtime_router  # C12/REALTIME-DETERMINISM
app.include_router(_realtime_router.router, prefix=settings.api_prefix)
from backend.routers import connectivity as _connectivity_router  # C13/CONNECTIVITY
app.include_router(_connectivity_router.router, prefix=settings.api_prefix)
from backend.routers import sensor_fusion as _sensor_fusion_router  # C14/SENSOR-FUSION
app.include_router(_sensor_fusion_router.router, prefix=settings.api_prefix)
from backend.routers import security_stack as _security_stack_router  # C15/SECURITY-STACK
app.include_router(_security_stack_router.router, prefix=settings.api_prefix)
from backend.routers import ota_framework as _ota_framework_router  # C16/OTA-FRAMEWORK
app.include_router(_ota_framework_router.router, prefix=settings.api_prefix)
from backend.routers import telemetry_backend as _telemetry_backend_router  # C17/TELEMETRY-BACKEND
app.include_router(_telemetry_backend_router.router, prefix=settings.api_prefix)
from backend.routers import payment as _payment_router  # C18/PAYMENT-PCI-COMPLIANCE
app.include_router(_payment_router.router, prefix=settings.api_prefix)
from backend.routers import imaging_pipeline as _imaging_pipeline_router  # C19/IMAGING-PIPELINE
app.include_router(_imaging_pipeline_router.router, prefix=settings.api_prefix)
from backend.routers import print_pipeline as _print_pipeline_router  # C20/PRINT-PIPELINE
app.include_router(_print_pipeline_router.router, prefix=settings.api_prefix)
from backend.routers import enterprise_web_stack as _ews_router  # C21/ENTERPRISE-WEB-STACK
app.include_router(_ews_router.router, prefix=settings.api_prefix)
from backend.routers import barcode_scanner as _barcode_router  # C22/BARCODE-SCANNER-SDK
app.include_router(_barcode_router.router, prefix=settings.api_prefix)
from backend.routers import machine_vision as _machine_vision_router  # C24/MACHINE-VISION
app.include_router(_machine_vision_router.router, prefix=settings.api_prefix)
from backend.routers import motion_control as _motion_control_router  # C25/MOTION-CONTROL
app.include_router(_motion_control_router.router, prefix=settings.api_prefix)
from backend.routers import cloudflare_tunnel as _cf_tunnel_router  # B12/CF-TUNNEL-WIZARD
app.include_router(_cf_tunnel_router.router, prefix=settings.api_prefix)
from backend.routers import uvc_gadget as _uvc_gadget_router  # D1/SKILL-UVC
app.include_router(_uvc_gadget_router.router, prefix=settings.api_prefix)
from backend.routers import preferences as _prefs_router  # J4/USER-PREFS
app.include_router(_prefs_router.router, prefix=settings.api_prefix)
from backend.routers import api_keys as _api_keys_router  # K6/BEARER-PER-KEY
app.include_router(_api_keys_router.router, prefix=settings.api_prefix)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "online",
        "docs": "/docs",
        "api": settings.api_prefix,
    }
