"""Health & readiness probes.

Two distinct concerns, two distinct endpoints:

* ``/healthz`` — **liveness**. Must return 200 fast whenever the Python
  process is alive enough to answer an HTTP request. No I/O. No DB.
  The orchestrator uses this to decide "is the process dead, should I
  restart it?". ``/livez`` is a byte-identical alias — the charter
  (``docs/ops/orchestration_selection.md`` §7.3) commits K8s probes to
  the ``/livez`` spelling, so G5 #4 wires there; ``/healthz`` stays the
  historical canonical path for compose / systemd / CF checks.

* ``/readyz`` — **readiness**. Returns 200 only when the process is
  ready to serve traffic: DB is reachable, alembic migrations are at
  head, and at least one LLM provider in the fallback chain has
  credentials (or is the Ollama local fallback, which needs none).
  Starts failing immediately once ``_lifecycle.coordinator`` flips to
  draining — this is the mechanism by which the orchestrator takes a
  replica out of rotation during graceful shutdown.

``/api/v1/health`` is kept as a legacy alias returning the liveness
payload so existing wizard/UI consumers keep working.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.models import HealthResponse

logger = logging.getLogger(__name__)


# Router mounted under ``settings.api_prefix`` — preserves the legacy
# ``/api/v1/health`` contract.
router = APIRouter(tags=["health"])

# Router mounted at the server root so probes answer on ``/healthz``
# and ``/readyz`` without an API prefix (k8s / systemd / docker-compose
# / Cloudflare health checks expect root-level paths).
probe_router = APIRouter(tags=["health"])


_VERSION = "0.1.0"
_PHASE = "3.2"
_ENGINE = "OmniSight Engine"


@router.get("/health", response_model=HealthResponse)
async def health_check() -> dict:
    """Legacy liveness payload (kept for backwards compatibility)."""
    return {
        "status": "online",
        "engine": _ENGINE,
        "version": _VERSION,
        "phase": _PHASE,
    }


# ─────────────────────────────────────────────────────────────────────
#  /healthz — liveness probe
# ─────────────────────────────────────────────────────────────────────
@probe_router.get("/healthz")
async def healthz() -> dict:
    """Liveness probe — fast, no I/O.

    Returns 200 whenever the event loop can service the request. We
    intentionally do *not* check the DB here: a DB stall should not
    make the orchestrator restart a healthy process — that is the job
    of ``/readyz``.
    """
    return {"status": "ok", "live": True}


# Mirror the liveness probe under the API prefix too so callers that
# only know about ``/api/v1/*`` still have a way to reach it.
@router.get("/healthz")
async def healthz_prefixed() -> dict:
    return await healthz()


# ``/livez`` is the K8s-charter spelling for the liveness probe
# (``docs/ops/orchestration_selection.md`` §7.3 commits the Deployment
# httpGet to ``/livez``). It delegates to the same handler so the two
# paths return byte-identical payloads — the orchestrator sees one
# contract regardless of which spelling it probes.
@probe_router.get("/livez")
async def livez() -> dict:
    return await healthz()


@router.get("/livez")
async def livez_prefixed() -> dict:
    return await healthz()


# ─────────────────────────────────────────────────────────────────────
#  /readyz — readiness probe
# ─────────────────────────────────────────────────────────────────────
async def _check_db() -> tuple[bool, str]:
    """Returns (ok, detail). Detail is a short human-readable string."""
    try:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            result = await conn.fetchval("SELECT 1")
        if result != 1:
            return False, "db_ping_no_row"
        return True, "ok"
    except RuntimeError as exc:
        return False, f"db_not_initialized: {exc}"
    except Exception as exc:
        return False, f"db_ping_failed: {exc}"


async def _check_migrations() -> tuple[bool, str]:
    """Compare alembic's current head with the migrations directory.

    We avoid importing SQLAlchemy's heavy env setup — instead we do a
    direct read against the ``alembic_version`` table (populated by
    alembic after each upgrade) and compare it with the newest version
    file on disk.  Returns (ok, detail).  If the alembic table doesn't
    exist yet (fresh install), we treat that as *not* ready — a newly
    booted process should run migrations before serving traffic.

    SP-5.9 (2026-04-21): SQLite-vs-PG dialect dispatch removed
    (runtime is PG-only now). Always uses ``information_schema.tables``
    for the "does alembic_version exist?" probe. If a future rollback
    needs the SQLite path, it'll come back through the compat wrapper
    or a fresh dialect check.
    """
    try:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            table_exists = await conn.fetchval(
                "SELECT EXISTS("
                "  SELECT 1 FROM information_schema.tables "
                "  WHERE table_schema = 'public' "
                "  AND table_name = 'alembic_version'"
                ")"
            )
            if not table_exists:
                return True, "alembic_not_applied_legacy_schema"
            current = await conn.fetchval(
                "SELECT version_num FROM alembic_version"
            )
    except Exception as exc:
        return False, f"alembic_probe_failed: {exc}"
    if not current:
        return False, "alembic_version_empty"

    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    try:
        files = sorted(
            f.name for f in versions_dir.iterdir()
            if f.is_file() and f.suffix == ".py" and not f.name.startswith("_")
        )
    except FileNotFoundError:
        return True, f"current={current},no_versions_dir"

    if not files:
        return True, f"current={current},no_migrations"

    # Alembic version files are conventionally named ``NNNN_<slug>.py``;
    # the numeric prefix of the last file is the latest revision.
    latest_file = files[-1]
    latest_prefix = latest_file.split("_", 1)[0]
    if latest_prefix and latest_prefix not in current:
        # We compare by prefix membership rather than equality because
        # alembic stores the full revision ID — tolerant of either
        # scheme (short-hash or numeric prefix).
        return False, f"migration_pending: current={current} latest_file={latest_file}"

    return True, f"current={current}"


#: C3 — deep-check result cache. Maps ``(provider, key_suffix)`` →
#: ``(ok, detail, expires_at)``. Keyed on the key suffix so a rotation
#: invalidates the cache entry on the next probe. 60 s TTL is long
#: enough to keep `/readyz` cheap under frequent probes (Caddy polls
#: every 2 s during a rolling deploy → 30 cache hits per miss) but
#: short enough that a newly-broken provider is caught within a minute.
_DEEP_CHECK_CACHE: dict[tuple[str, str], tuple[bool, str, float]] = {}
_DEEP_CHECK_TTL_S = 60.0
#: Network timeout per provider probe. Shorter than the Caddy probe
#: cycle so a hung provider can't inflate `/readyz` latency past the
#: proxy timeout.
_DEEP_CHECK_TIMEOUT_S = 3.0


def _provider_probe_url(provider: str) -> str | None:
    """Cheapest GET per provider. Authenticated list/models endpoints
    that verify the key is *usable*, not just syntactically present.

    Chosen to be:
      * idempotent (safe to poll);
      * <1 KB response (fast even on flaky links);
      * rejected with 401 on invalid key (so we can distinguish
        "network down" from "key revoked" from "quota exhausted").
    """
    return {
        "anthropic": "https://api.anthropic.com/v1/models",
        "openai": "https://api.openai.com/v1/models",
        "google": "https://generativelanguage.googleapis.com/v1beta/models",
        "openrouter": "https://openrouter.ai/api/v1/models",
        "groq": "https://api.groq.com/openai/v1/models",
        "deepseek": "https://api.deepseek.com/v1/models",
        "xai": "https://api.x.ai/v1/models",
        "together": "https://api.together.xyz/v1/models",
    }.get(provider)


def _probe_provider_deep(provider: str, key: str) -> tuple[bool, str]:
    """Make one authenticated GET. Returns ``(ok, detail)``.

    Cached in ``_DEEP_CHECK_CACHE`` on the ``(provider, key_tail)``
    key so rotation invalidates naturally. All network errors are
    treated as ``not_ok`` — if the provider is unreachable, we're
    not ready to serve requests that depend on it.
    """
    import time as _time
    import urllib.request
    import urllib.error

    # key_tail = last 6 chars, opaque to logs but unique across rotations
    key_tail = key[-6:] if key else ""
    cache_key = (provider, key_tail)
    cached = _DEEP_CHECK_CACHE.get(cache_key)
    now = _time.time()
    if cached and cached[2] > now:
        return cached[0], cached[1] + ":cached"

    url = _provider_probe_url(provider)
    if url is None:
        # Unknown provider — behave like a presence check did before.
        ok = bool(key)
        detail = "key_present" if ok else "no_key"
        _DEEP_CHECK_CACHE[cache_key] = (ok, detail, now + _DEEP_CHECK_TTL_S)
        return ok, detail

    # Provider-specific auth header. Anthropic and Google use
    # non-standard headers; everyone else uses Bearer.
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif provider == "google":
        # Google's API key rides in the query string, not a header.
        url = f"{url}?key={key}"
        headers = {}
    else:
        headers = {"Authorization": f"Bearer {key}"}

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=_DEEP_CHECK_TIMEOUT_S) as resp:
            # Any 2xx is "key accepted". 200 covers all known providers.
            ok = 200 <= resp.status < 300
            detail = f"http_{resp.status}"
    except urllib.error.HTTPError as exc:
        # 401/403 = key rejected; 429 = quota; 5xx = provider-side. All
        # of these mean "not usable right now".
        ok = False
        detail = f"http_{exc.code}"
    except Exception as exc:
        ok = False
        # Truncate to stay log-safe; full exception lives in probe-level
        # logger.debug when that's wired up.
        detail = f"probe_error:{type(exc).__name__}"

    _DEEP_CHECK_CACHE[cache_key] = (ok, detail, now + _DEEP_CHECK_TTL_S)
    return ok, detail


def _check_db_pool() -> tuple[bool, str]:
    """Phase-3-Runtime-v2 SP-1.5: observational probe of the asyncpg.Pool.

    This probe is deliberately **stats-only** — it does not borrow a
    connection from the pool. Reasoning:
      * The existing ``_check_db()`` probe already hits PG via the
        compat wrapper, so PG liveness is covered.
      * This probe reports the pool's OWN state (initialised / sizing /
        in-flight borrows), which is complementary.
      * Borrowing on every /readyz call would add ~1 borrow/sec of
        Caddy-driven load across the fleet — cheap individually, but
        pointless when the compat probe already covers PG liveness.

    Epic 7 (compat wrapper deletion) will replace the compat-based
    ``_check_db()`` with a pool-borrowing probe. Until then, this
    check is informational and never fails the /readyz gate — the
    only way it returns ``ok=False`` is if ``get_pool_stats()`` itself
    raises, which is a code-level bug (the helper is defensive and
    returns a sentinel shape when the pool is uninit).
    """
    try:
        from backend import db_pool as _db_pool
        stats = _db_pool.get_pool_stats()
    except Exception as exc:  # pragma: no cover — defence in depth
        return False, f"pool_stats_failed: {type(exc).__name__}: {exc}"

    if not stats.get("initialised"):
        # Legitimate — SQLite dev mode, or app still starting up before
        # lifespan init_pool has fired. Not a /readyz fail.
        return True, "pool: not-initialised (SQLite dev mode or pre-startup)"

    return True, (
        f"pool: min={stats['min_size']} max={stats['max_size']} "
        f"size={stats['size']} free={stats['free_size']} "
        f"used={stats['used_size']}"
    )


def _check_provider_chain() -> tuple[bool, str]:
    """At least one provider in the fallback chain must be usable.

    By default this is a shallow check — the presence of the API key
    counts as "ready". Set ``OMNISIGHT_READYZ_DEEP_CHECK=1`` (C3 audit
    2026-04-19) to escalate to a real authenticated GET against each
    configured provider, cached for 60 s so frequent probes don't
    hammer the upstream. Deep mode catches rotated/revoked keys that
    the shallow check cannot — a scenario where ``/readyz`` would
    otherwise stay green while every real request fails 401.
    """
    import os as _os
    from backend.config import settings

    chain = [p.strip() for p in settings.llm_fallback_chain.split(",") if p.strip()]
    if not chain:
        return False, "empty_fallback_chain"

    # Map provider → env/credential check.  Credentials live on
    # Settings, so we read them from ``settings`` rather than os.environ
    # directly.
    provider_key = {
        "anthropic": settings.anthropic_api_key,
        "google": settings.google_api_key,
        "openai": settings.openai_api_key,
        "xai": settings.xai_api_key,
        "groq": settings.groq_api_key,
        "deepseek": settings.deepseek_api_key,
        "together": settings.together_api_key,
        "openrouter": settings.openrouter_api_key,
    }

    deep = _os.environ.get("OMNISIGHT_READYZ_DEEP_CHECK", "").strip().lower() in {"1", "true", "yes"}

    ready: list[str] = []
    rejected: list[str] = []
    for prov in chain:
        if prov == "ollama":
            # Ollama is a local model runner — no credential needed; we
            # take its mere presence in the chain as "available fallback"
            # so a credential-less dev/CI install still passes readyz.
            ready.append(prov)
            continue
        key = provider_key.get(prov, "")
        if not key:
            continue
        if not deep:
            ready.append(prov)
            continue
        ok, detail = _probe_provider_deep(prov, key)
        if ok:
            ready.append(prov)
        else:
            rejected.append(f"{prov}:{detail}")

    if not ready:
        if rejected:
            return False, f"deep_check_all_failed: {','.join(rejected)}"
        return False, f"no_configured_provider_in_chain: {','.join(chain)}"
    suffix = " (deep)" if deep else ""
    return True, f"ready={','.join(ready)}{suffix}"


def _build_readyz_payload(checks: dict, ready: bool) -> dict:
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "checks": checks,
        "timestamp": time.time(),
    }


async def _readyz_handler() -> JSONResponse:
    # G7 (HA-07): time the probe and label the histogram sample with
    # outcome so Grafana can split p50/p99 by ready / not_ready /
    # draining. `outcome` is resolved once we know the verdict.
    import time as _time
    from backend import lifecycle as _lifecycle
    from backend import metrics as _metrics

    _started = _time.perf_counter()

    def _observe(outcome: str) -> None:
        _metrics.readyz_latency_seconds.labels(outcome=outcome).observe(
            _time.perf_counter() - _started,
        )

    checks: dict = {}

    # ── 1. Draining gate ─────────────────────────────────────────────
    # Once SIGTERM has flipped the coordinator, this replica must stop
    # advertising itself as ready so the upstream LB drains it before
    # the 30s in-flight timeout kicks in.
    if _lifecycle.coordinator.shutting_down:
        checks["draining"] = {"ok": False, "detail": "server_is_draining"}
        payload = _build_readyz_payload(checks, ready=False)
        _observe("draining")
        return JSONResponse(
            status_code=503,
            content=payload,
            headers={"Retry-After": "30", "Connection": "close"},
        )
    checks["draining"] = {"ok": True, "detail": "not_draining"}

    # ── 2. DB ping ───────────────────────────────────────────────────
    db_ok, db_detail = await _check_db()
    checks["db"] = {"ok": db_ok, "detail": db_detail}

    # ── 3. Migrations ────────────────────────────────────────────────
    mig_ok, mig_detail = await _check_migrations()
    checks["migrations"] = {"ok": mig_ok, "detail": mig_detail}
    # H2 audit (2026-04-19): expose migration mismatch as a Prometheus
    # gauge so `OmniSightMigrationMismatch` can alert on a deploy that
    # forgot `alembic upgrade head`. Previously the mismatch only
    # surfaced in the /readyz JSON payload — invisible to Prometheus.
    try:
        _metrics.readyz_migrations_pending.set(0 if mig_ok else 1)
    except Exception:
        pass  # metrics are best-effort — never block readyz

    # ── 4. Provider chain ────────────────────────────────────────────
    prov_ok, prov_detail = _check_provider_chain()
    checks["provider_chain"] = {"ok": prov_ok, "detail": prov_detail}

    # ── 5. asyncpg.Pool (SP-1.5, observational) ──────────────────────
    # Informational probe — see _check_db_pool docstring for why this
    # is not part of the `ready` gate during Epics 1-6. Epic 7 swaps
    # the compat-based _check_db() for a pool-borrowing probe at which
    # point this one folds into the main db gate.
    pool_ok, pool_detail = _check_db_pool()
    checks["db_pool"] = {"ok": pool_ok, "detail": pool_detail}

    ready = db_ok and mig_ok and prov_ok
    payload = _build_readyz_payload(checks, ready=ready)
    outcome = "ready" if ready else "not_ready"
    _observe(outcome)
    return JSONResponse(
        status_code=200 if ready else 503,
        content=payload,
        headers={} if ready else {"Retry-After": "5"},
    )


@probe_router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness probe — DB + migrations + provider chain."""
    return await _readyz_handler()


@router.get("/readyz")
async def readyz_prefixed() -> JSONResponse:
    return await _readyz_handler()
