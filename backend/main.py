"""OmniSight Engine — FastAPI entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.routers import agents, artifacts, chat, events, health, host as _host_router, integration, invoke, providers, simulations, system, tasks, tools, webhooks, workflow as wf_router, workspaces
from backend import db
from backend import lifecycle as _lifecycle

async def _startup_cleanup(log):
    """Reset stuck states left over from a previous crash."""
    # 1. Reset agents stuck in "running" for over 1 hour.
    # created_at is stored as TEXT (ISO-8601-like "YYYY-MM-DD HH:MM:SS")
    # in both SQLite and PG. Phase-3 cutover: rather than teaching the
    # pg_compat shim to rewrite SQLite's 2-arg ``datetime('now','-1 hour')``
    # + 1-arg ``datetime(col)`` forms (which are harder to disambiguate
    # from column identifiers in a regex), we compute the cutoff string
    # in Python and do a plain text comparison — deterministic for our
    # insertion format and dialect-neutral.
    from datetime import datetime as _dt, timedelta as _td
    _cutoff = (_dt.utcnow() - _td(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    n = await db.execute_raw(
        "UPDATE agents SET status='idle', thought_chain='[RECOVERY] Reset on startup' "
        "WHERE status='running' AND created_at < ?",
        (_cutoff,),
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
    # 5. R8 #314 row 2875: scan + remove orphan worktrees left over
    # from a prior crashed/restarted process. ``_workspaces`` is empty
    # at this point in lifespan (in-process dict, fresh per worker)
    # so any subdir under ``.agent_workspaces/`` is by definition
    # orphan; the helper ``git worktree remove --force`` + ``shutil.
    # rmtree`` fallback per orphan, emits ``workspace.orphan_cleanup``
    # SSE + audit row per design §7 row 4. Best-effort — boot must
    # not block on it (workspace recovery is observability, not a
    # gate).
    try:
        from backend.workspace import cleanup_orphan_worktrees
        orphans = await cleanup_orphan_worktrees()
        if orphans:
            log.warning(
                "Startup cleanup: removed %d orphan worktree(s) "
                "(paths=%s)",
                len(orphans),
                ", ".join(o["path"] for o in orphans),
            )
    except Exception as exc:
        log.warning("Startup cleanup: orphan worktree scan failed: %s", exc)


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
        # Phase-3 Step C.2 (2026-04-21): the PG compat wrapper that
        # used to coexist with the pool has been retired. On the PG
        # path ``db.init()`` above is a no-op; the pool is the sole
        # entry point. Gated on ``_resolve_pg_dsn()`` so SQLite dev /
        # test flows (no DSN set) skip pool init and fall back to
        # ``db._db`` for the handful of callers that still reach
        # for aiosqlite directly.
        from backend import db_pool as _db_pool
        from backend.db import _resolve_pg_dsn as _r_pg
        _pg_dsn = _r_pg()
        if _pg_dsn:
            await _db_pool.init_pool(_pg_dsn)
            _log.info("db_pool: initialised against PG DSN")
        else:
            _log.info(
                "db_pool: skipped — no Postgres DSN in "
                "OMNISIGHT_DATABASE_URL/DATABASE_URL (SQLite dev mode)"
            )
        await _startup_cleanup(_log)
        # SP-3.1 (2026-04-20): agents.seed_defaults_if_empty was ported
        # to require a pool-backed asyncpg connection. In SQLite dev mode
        # the pool is absent, so we skip seeding with a clear warning —
        # the in-memory _agents dict starts empty; operator creates
        # agents via the UI instead. Epic 7 removes this branch when
        # the compat wrapper is deleted.
        if _pg_dsn:
            async with _db_pool.get_pool().acquire() as _seed_conn:
                await agents.seed_defaults_if_empty(_seed_conn)
            # SP-3.2: tasks.seed_defaults_if_empty now requires a pool
            # conn too. Share the acquire block with agents to keep
            # startup pool usage bounded.
            async with _db_pool.get_pool().acquire() as _seed_conn:
                await tasks.seed_defaults_if_empty(_seed_conn)
        else:
            _log.warning(
                "[STARTUP] agents/tasks seed_defaults_if_empty skipped — "
                "SQLite dev mode lacks the pool-backed conn that SP-3.1/3.2 "
                "requires. Default agents/tasks will NOT be pre-populated. "
                "Set OMNISIGHT_DATABASE_URL to enable."
            )
        # SP-3.5: token_usage load also requires a pool-backed conn.
        if _pg_dsn:
            async with _db_pool.get_pool().acquire() as _tok_conn:
                await system.load_token_usage_from_db(_tok_conn)
        else:
            _log.warning(
                "[STARTUP] system.load_token_usage_from_db skipped — "
                "SQLite dev mode (SP-3.5)."
            )
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
        # Phase 5-5 (#multi-account-forge): one-shot move of legacy
        # ``Settings.{github,gitlab}_token{,_map}`` / ``gerrit_instances``
        # / ``notification_jira_*`` into the canonical ``git_accounts``
        # table. Idempotent (skipped if the table already has any row);
        # operator escape hatch is ``OMNISIGHT_CREDENTIAL_MIGRATE=skip``.
        # PG-gated like the K6 hook above — SQLite dev mode lacks the
        # pool that the migration writes through.
        if _pg_dsn:
            try:
                from backend import (
                    legacy_credential_migration as _cred_migrate,
                )
                summary = await _cred_migrate.migrate_legacy_credentials_once()
                if summary["migrated"]:
                    _log.warning(
                        "[CRED-MIGRATE] migrated %d legacy credential(s) "
                        "into git_accounts (sources=%s). Plan to remove "
                        "the corresponding env knobs once UI parity ships.",
                        summary["migrated"], summary["sources"],
                    )
                elif summary["skipped_reason"]:
                    _log.info(
                        "[CRED-MIGRATE] no migration this boot "
                        "(reason=%s, candidates=%d).",
                        summary["skipped_reason"],
                        summary.get("candidates", 0),
                    )
            except Exception as exc:
                # Migration is best-effort — never block startup on it.
                _log.warning(
                    "[CRED-MIGRATE] hook raised %s; legacy shim still "
                    "covers credential reads.",
                    type(exc).__name__,
                )
        # Phase 5b-5 (#llm-credentials): one-shot move of legacy
        # ``Settings.{anthropic,google,openai,xai,groq,deepseek,together,
        # openrouter}_api_key`` + non-default ``ollama_base_url`` into
        # the canonical ``llm_credentials`` table. Idempotent (skipped
        # if the table already has any row); operator escape hatch is
        # ``OMNISIGHT_LLM_CREDENTIAL_MIGRATE=skip``. PG-gated like the
        # Phase-5-5 forge-credential hook above — SQLite dev mode lacks
        # the pool that the migration writes through.
        if _pg_dsn:
            try:
                from backend import (
                    legacy_llm_credential_migration as _llm_migrate,
                )
                llm_summary = (
                    await _llm_migrate
                    .migrate_legacy_llm_credentials_once()
                )
                if llm_summary["migrated"]:
                    _log.warning(
                        "[LLM-CRED-MIGRATE] migrated %d legacy LLM "
                        "credential(s) into llm_credentials "
                        "(sources=%s). .env keys are left in place "
                        "for operator review.",
                        llm_summary["migrated"],
                        llm_summary["sources"],
                    )
                elif llm_summary["skipped_reason"]:
                    _log.info(
                        "[LLM-CRED-MIGRATE] no migration this boot "
                        "(reason=%s, candidates=%d).",
                        llm_summary["skipped_reason"],
                        llm_summary.get("candidates", 0),
                    )
            except Exception as exc:
                # Migration is best-effort — never block startup on it.
                _log.warning(
                    "[LLM-CRED-MIGRATE] hook raised %s; legacy "
                    "Settings read path still covers LLM credential "
                    "reads.",
                    type(exc).__name__,
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
        # H4a row 2582: prime the AIMD budget controller from the
        # last-known-good DB row so a restart keeps the previous
        # process's calibration instead of dropping to the static
        # INIT_BUDGET=6. Best-effort — failure silently keeps the
        # cold-start default. PG-gated like the other pool-backed
        # startup hooks (SQLite dev mode has no pool).
        if _pg_dsn:
            try:
                from backend import adaptive_budget as _ab
                loaded = await _ab.prime_from_db()
                if loaded is not None:
                    _log.info(
                        "[adaptive_budget] primed from DB, budget=%d "
                        "(replaces INIT_BUDGET=%d)",
                        loaded, _ab.INIT_BUDGET,
                    )
            except Exception as exc:
                _log.debug(
                    "[adaptive_budget] prime_from_db failed "
                    "(non-fatal): %s",
                    exc,
                )
    except Exception as exc:
        _log.error("Startup failed: %s", exc, exc_info=True)
        raise
    # I10: start cross-worker pub/sub listener for multi-worker SSE
    import asyncio
    from backend import shared_state as _ss
    pubsub_task = asyncio.create_task(_ss.start_pubsub_listener())
    # G1: install SIGTERM/SIGINT handler so the process can drain in
    # under 30 s. Idempotent — safe when lifespan runs twice (tests).
    try:
        _lifecycle.coordinator.install_signal_handlers(asyncio.get_running_loop())
    except Exception as exc:
        _log.debug("[lifecycle] install_signal_handlers failed: %s", exc)
    # Start watchdog for stuck agent detection
    watchdog_task = asyncio.create_task(invoke.run_watchdog())
    # Phase 47D: DecisionEngine timeout sweep (30 s cadence)
    from backend import decision_engine as _de
    sweep_task = asyncio.create_task(_de.run_sweep_loop())
    # Phase 52: Webhook DLQ retry worker
    from backend import notifications as _notif
    dlq_task = asyncio.create_task(_notif.run_dlq_loop())
    # R9 row 2940 (#315): P3 → L1 log + email digest periodic flusher.
    # Singleton-guarded; when SMTP is unconfigured the loop still runs
    # (it falls back to a structured log line per flush) so the sweep
    # is always cleanly drained on shutdown.
    digest_task = asyncio.create_task(_notif.run_email_digest_loop())
    # Phase 63-D: Daily IQ benchmark loop (opt-in L3, gated by env).
    from backend import iq_nightly as _iq
    iq_task = asyncio.create_task(_iq.run_nightly_loop())
    # Phase 65 S4: Fine-tune nightly loop (opt-in L4, gated by env).
    from backend import finetune_nightly as _ft
    ft_task = asyncio.create_task(_ft.run_nightly_loop())
    # Phase 63-E: Memory decay loop (opt-in L3, gated by env).
    from backend import memory_decay as _md
    md_task = asyncio.create_task(_md.run_decay_loop())
    # Z.2 (#291): LLM provider balance refresh loop — 10 min cadence,
    # exponential backoff to 1 h on failure. Writes each supported
    # provider's BalanceInfo to SharedKV("provider_balance") so the
    # upcoming GET /runtime/providers/{provider}/balance endpoint can
    # serve cached values without per-request vendor calls.
    from backend import llm_balance_refresher as _lbr
    balance_task = asyncio.create_task(_lbr.run_refresh_loop())
    # I6: DRF per-tenant sandbox capacity grace deadline sweep
    from backend import sandbox_capacity as _sc
    drf_task = asyncio.create_task(_sc.run_sweep_loop())
    # M2: per-tenant disk quota sweep (5 min) — emits SSE soft warnings
    # and triggers LRU cleanup when over soft threshold.
    from backend import tenant_quota as _tq
    quota_task = asyncio.create_task(_tq.run_quota_sweep_loop())
    # Q.6 #300 checkbox 3: dedicated user_drafts 24h retention sweep.
    # Opportunistic GC on PUT only bounds active typers; idle workers
    # need this explicit loop to prevent stale drafts from lingering.
    from backend import user_drafts_gc as _ud_gc
    drafts_gc_task = asyncio.create_task(_ud_gc.run_gc_loop())
    # M4: cgroup per-container sampler → per-tenant Prometheus gauges +
    # billing accumulator. Lifespan-scoped so it starts with the app
    # and stops cleanly at shutdown.
    from backend import host_metrics as _hm
    host_metrics_task = asyncio.create_task(_hm.run_sampling_loop())
    # H1: whole-host ring buffer (60 × 5s snapshots = 5 min history).
    # Feeds the AIMD capacity planner + the GET /host/metrics endpoint.
    host_ringbuf_task = asyncio.create_task(_hm.run_host_sampling_loop())
    # G7 (HA-07): flip the backend_instance_up gauge to 1 once the
    # app is fully booted. Shutdown flips it back to 0 so the
    # reverse-proxy drops this replica from rotation before the
    # in-flight drain window.
    from backend import ha_observability as _hao
    _hao.mark_instance_up()
    yield
    try:
        _hao.mark_instance_down()
    except Exception as exc:
        _log.debug("[lifecycle] mark_instance_down failed: %s", exc)
    # G1: graceful drain — flip gate, flush SSE, wait in-flight (30 s),
    # close DB. Background tasks are cancelled AFTER the drain so any
    # in-flight request that depends on them still has a chance to
    # finish within the timeout.
    try:
        result = await _lifecycle.graceful_shutdown(close_db=False)
        _log.info("[lifecycle] graceful_shutdown result: %s", result)
    except Exception as exc:
        _log.warning("[lifecycle] graceful_shutdown raised: %s", exc)
    for t in (pubsub_task, watchdog_task, sweep_task, dlq_task, digest_task, iq_task, ft_task, md_task, balance_task, drf_task, quota_task, drafts_gc_task, host_metrics_task, host_ringbuf_task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    await _ss.close()
    # Close the asyncpg pool before the SQLite dev handle.
    # Order matters: any background task still writing via the pool must
    # finish first (_lifecycle.graceful_shutdown above has already drained
    # in-flight requests and background tasks were cancelled), then the
    # pool releases its underlying TCP connections, then ``db.close()``
    # tears down the aiosqlite WAL (no-op on the PG path since
    # ``db.init()`` is a no-op there post-Step-C.2).
    try:
        from backend import db_pool as _db_pool
        await _db_pool.close_pool()
    except Exception as exc:  # pragma: no cover — defence in depth
        _log.warning("[lifecycle] db_pool.close_pool raised: %s", exc)
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
    # M2 audit (2026-04-19): explicit method + header allowlist. With
    # `allow_credentials=True` a `*` value silently fails spec compliance
    # on some browsers AND lets an attacker pair stolen origins with
    # forged X-Forwarded-For / X-Tenant-Id if the reverse proxy ever
    # gets misconfigured. Listing only what legitimate UI + fetch calls
    # need closes that gap without breaking the app. Extend if a new
    # header ever becomes necessary — do NOT re-introduce `*`.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Accept",
        "Accept-Language",
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-CSRF-Token",
        "X-Requested-With",
    ],
    expose_headers=["X-CSRF-Token"],
)

# S2-9 (#354): secure-by-default auth baseline. After this middleware
# loads, every non-allowlisted path requires a valid session.
# Defaults to MODE=log (advisory — just logs would-be-blocks). Flip
# to enforce via `OMNISIGHT_AUTH_BASELINE_MODE=enforce` once the
# allowlist has been validated against production traffic. The
# allowlist lives in backend/auth_baseline.py next to the middleware
# so one code review covers the whole policy.
#
# Registration order matters: added BEFORE ha_observability so the
# 5xx-rate counter doesn't see spurious "no-session 401s" from
# allowlist tuning. This middleware sits between CORSMiddleware
# (above) and ha_observability + routers (below).
from backend import auth_baseline as _auth_baseline
_auth_baseline.install(app)

# G7 (HA-07): register HTTP middleware that feeds the rolling 5xx
# rate metric. Registered here so it wraps ALL other middleware +
# routers — every response, including error responses produced by
# Starlette's default handlers, gets recorded.
from backend import ha_observability as _ha_observability
_ha_observability.register_middleware(app)


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
    "/auth/whoami", "/health", "/healthz", "/livez", "/readyz",
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  I9 — Per-IP / per-user / per-tenant rate limiting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_RATE_LIMIT_EXEMPT = {"/health", "/healthz", "/livez", "/readyz", "/auth/login", "/auth/logout"}


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L1 #2 — Bootstrap wizard gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Until `backend.bootstrap.is_bootstrap_finalized()` flips to True,
# every non-exempt request gets 307-redirected to the wizard at
# ``/bootstrap``. Exempt paths:
#   * ``/bootstrap/*``  — wizard UI + wizard API (with or without api_prefix)
#   * ``/auth/login``   — operator must log in to drive the wizard
#   * ``/healthz``      — k8s/probe liveness (backend ``/health`` too)
#   * static resources  — ``/_next/*``, ``/static/*``, ``/assets/*``,
#                         ``/favicon.ico`` and friends
#   * doc routes        — ``/``, ``/docs``, ``/openapi.json``, ``/redoc``
#
# The gate is registered LAST on purpose — Starlette wraps middleware
# in reverse registration order, so `@app.middleware` declared last
# becomes the outermost layer. That lets it short-circuit before the
# rate-limit / api-key / tenant / password-change gates do any work
# during a fresh install (when they'd otherwise 401/429 on an unconfigured
# system).
_BOOTSTRAP_EXEMPT_REL = {
    "/auth/login", "/auth/logout", "/auth/change-password",
    "/healthz", "/health", "/livez", "/readyz",
}
# ``/cloudflare/*`` is exempt for L4 Step 3 — the wizard's Cloudflare
# tunnel embed (B12 wizard) calls these endpoints before login. The
# router itself still enforces operator RBAC once bootstrap has
# finalized; this exemption only waives the redirect during install.
_BOOTSTRAP_EXEMPT_REL_PREFIXES = (
    "/cloudflare/",
)
_BOOTSTRAP_EXEMPT_RAW = {
    "/", "/healthz", "/livez", "/readyz", "/docs", "/openapi.json", "/redoc",
    "/favicon.ico", "/robots.txt",
}
_BOOTSTRAP_EXEMPT_RAW_PREFIXES = (
    "/_next/", "/static/", "/assets/", "/public/",
)
_BOOTSTRAP_STATIC_SUFFIXES = (
    ".css", ".js", ".map", ".ico", ".png", ".jpg", ".jpeg",
    ".gif", ".svg", ".webp", ".woff", ".woff2", ".ttf", ".eot",
)


def _bootstrap_path_is_exempt(path: str, rel: str) -> bool:
    """Return True if *path* bypasses the bootstrap wizard gate."""
    if path == "/bootstrap" or path.startswith("/bootstrap/"):
        return True
    if rel == "/bootstrap" or rel.startswith("/bootstrap/"):
        return True
    if rel in _BOOTSTRAP_EXEMPT_REL or path in _BOOTSTRAP_EXEMPT_RAW:
        return True
    if any(rel.startswith(p) for p in _BOOTSTRAP_EXEMPT_REL_PREFIXES):
        return True
    if any(path.startswith(p) for p in _BOOTSTRAP_EXEMPT_RAW_PREFIXES):
        return True
    if path.endswith(_BOOTSTRAP_STATIC_SUFFIXES):
        return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  G1 — Graceful shutdown gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Rejects new traffic with 503 once SIGTERM has flipped the drain flag,
# and tracks in-flight request count so the lifespan shutdown can wait
# for outstanding work to finish.  Registered AFTER the bootstrap gate
# (so it becomes the outermost layer) — we want to count even requests
# that the bootstrap gate would otherwise redirect, AND we want 503s
# to short-circuit before any other middleware does real work.
_GRACEFUL_SHUTDOWN_EXEMPT_RAW = {"/healthz", "/health", "/livez", "/readyz"}


@app.middleware("http")
async def _graceful_shutdown_gate(request, call_next):
    """G1 — refuse new traffic while draining + count in-flight."""
    from starlette.responses import JSONResponse as StarletteJSON

    path = request.url.path
    rel = path.removeprefix(settings.api_prefix)
    # Liveness probes must keep working while we drain so the
    # orchestrator can still tell the process is alive (just not
    # ready).  Readiness endpoints should start failing — that is
    # G1 bullet #2, handled by the /readyz router itself.
    exempt = (
        path in _GRACEFUL_SHUTDOWN_EXEMPT_RAW
        or rel in _GRACEFUL_SHUTDOWN_EXEMPT_RAW
    )
    if _lifecycle.coordinator.shutting_down and not exempt:
        return StarletteJSON(
            status_code=503,
            content={"detail": "Server is shutting down"},
            headers={"Retry-After": "30", "Connection": "close"},
        )

    _lifecycle.coordinator.request_started()
    try:
        return await call_next(request)
    finally:
        _lifecycle.coordinator.request_finished()


@app.middleware("http")
async def _bootstrap_gate(request, call_next):
    """L1 #2 — redirect to ``/bootstrap`` until the wizard is finalized.

    API paths (``/api/v1/*``) get a JSON 503 instead of 307 redirect.
    The 307 redirect is only for browser page navigations. Without this
    distinction, the frontend's 20+ polling hooks each follow the 307,
    receive HTML instead of JSON, create parse-error objects, and
    eventually OOM the Node.js process (~3 minutes at default heap).
    """
    from starlette.responses import RedirectResponse, JSONResponse

    path = request.url.path
    rel = path.removeprefix(settings.api_prefix)
    if _bootstrap_path_is_exempt(path, rel):
        return await call_next(request)

    from backend import bootstrap as _boot
    if await _boot.is_bootstrap_finalized():
        return await call_next(request)

    # API calls: return JSON error so the frontend can handle it
    # programmatically (show banner, stop polling) instead of following
    # a redirect to an HTML page and OOM'ing on parse errors.
    if path.startswith(settings.api_prefix + "/") or path.startswith("/api/"):
        return JSONResponse(
            status_code=503,
            content={
                "error": "bootstrap_required",
                "detail": "系統尚未完成初始設定，請先完成 Bootstrap wizard。",
                "redirect": "/bootstrap",
            },
        )

    # Browser page navigations: 307 redirect to the wizard.
    # Use 307 to preserve method + body (so a fetch/POST doesn't get
    # silently downgraded to GET on redirect — the client decides
    # whether to follow).
    return RedirectResponse(url="/bootstrap", status_code=307)


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
# G1 #2 — `/healthz` (liveness) and `/readyz` (readiness) are mounted
# at the server root so systemd / docker-compose / k8s / CF health
# checks don't need to know about the API prefix.
app.include_router(health.probe_router)
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
from backend.routers import orchestration_observability as _orch_obs_router  # O9 (#272)
app.include_router(_orch_obs_router.router, prefix=settings.api_prefix)
from backend.routers import web_observability as _web_obs_router  # W10 (#284)
app.include_router(_web_obs_router.router, prefix=settings.api_prefix)
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
app.include_router(_host_router.router, prefix=settings.api_prefix)
from backend.routers import tenant_egress as _tenant_egress_router  # M6
app.include_router(_tenant_egress_router.router, prefix=settings.api_prefix)
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
from backend.routers import mobile_compliance as _mobile_compliance_router  # P6/MOBILE-STORE-GATES
app.include_router(_mobile_compliance_router.router, prefix=settings.api_prefix)
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
from backend.routers import drafts as _drafts_router  # Q.6 (#300) per-user composer drafts
app.include_router(_drafts_router.router, prefix=settings.api_prefix)
from backend.routers import api_keys as _api_keys_router  # K6/BEARER-PER-KEY
app.include_router(_api_keys_router.router, prefix=settings.api_prefix)
from backend.routers import storage as _storage_router  # M2/DISK-QUOTA-LRU
app.include_router(_storage_router.router, prefix=settings.api_prefix)
from backend.routers import hmi as _hmi_router  # C26/L4-CORE-26 HMI
app.include_router(_hmi_router.router, prefix=settings.api_prefix)
from backend.routers import orchestrator as _orchestrator_router  # O4/ORCHESTRATOR-GATEWAY
app.include_router(_orchestrator_router.router, prefix=settings.api_prefix)
from backend.routers import pep as _pep_router  # R0 (#306) PEP Gateway
app.include_router(_pep_router.router, prefix=settings.api_prefix)
from backend.routers import chatops as _chatops_router  # R1 (#307) ChatOps Interactive
app.include_router(_chatops_router.router, prefix=settings.api_prefix)
from backend.routers import entropy as _entropy_router  # R2 (#308) Semantic Entropy Monitor
app.include_router(_entropy_router.router, prefix=settings.api_prefix)
from backend.routers import scratchpad as _scratchpad_router  # R3 (#309) Scratchpad Offload + Auto-Continuation
app.include_router(_scratchpad_router.router, prefix=settings.api_prefix)
from backend.routers import bootstrap as _bootstrap_router  # L1 Bootstrap wizard REST
app.include_router(_bootstrap_router.router, prefix=settings.api_prefix)
from backend.routers import dashboard as _dashboard_router  # Phase 4-1 aggregator
app.include_router(_dashboard_router.router, prefix=settings.api_prefix)
from backend.routers import git_accounts as _git_accounts_router  # Phase 5-4 multi-account forge CRUD
app.include_router(_git_accounts_router.router, prefix=settings.api_prefix)
from backend.routers import llm_credentials as _llm_credentials_router  # Phase 5b-3 LLM credentials CRUD
app.include_router(_llm_credentials_router.router, prefix=settings.api_prefix)
from backend.routers import llm_balance as _llm_balance_router  # Z.2 (#291) provider balance endpoint
app.include_router(_llm_balance_router.router, prefix=settings.api_prefix)
from backend.routers import admin_tenants as _admin_tenants_router  # Y2 (#278) tenant CRUD admin REST
app.include_router(_admin_tenants_router.router, prefix=settings.api_prefix)
from backend.routers import tenant_invites as _tenant_invites_router  # Y3 (#279) row 1 — invite issuance
app.include_router(_tenant_invites_router.router, prefix=settings.api_prefix)

# O5 (#268) — register JIRA / GitHub / GitLab IntentSource factories.
# Done as a one-shot side-effect here so unit tests that don't import
# main can still exercise the registry directly (they just register
# fakes).
try:
    from backend import intent_sources_bootstrap as _intent_sources_bootstrap
    _intent_sources_bootstrap.register_defaults()
except Exception as _exc:  # pragma: no cover
    import logging as _lg
    _lg.getLogger(__name__).warning(
        "intent_sources bootstrap failed: %s", _exc,
    )


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "online",
        "docs": "/docs",
        "api": settings.api_prefix,
    }
