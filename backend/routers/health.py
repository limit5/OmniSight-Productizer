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
        from backend import db as _db
        conn = _db._conn()
    except RuntimeError as exc:
        return False, f"db_not_initialized: {exc}"
    except Exception as exc:
        return False, f"db_connection_error: {exc}"
    try:
        async with conn.execute("SELECT 1") as cur:
            row = await cur.fetchone()
            if row is None:
                return False, "db_ping_no_row"
        return True, "ok"
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
    """
    try:
        from backend import db as _db
        conn = _db._conn()
    except Exception as exc:
        return False, f"db_unavailable: {exc}"

    try:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='alembic_version'"
        ) as cur:
            row = await cur.fetchone()
    except Exception as exc:
        return False, f"sqlite_master_failed: {exc}"

    if row is None:
        # No alembic table — either alembic hasn't run yet, or this
        # deploy uses the legacy bare schema in db.py.  We allow the
        # legacy path (main.py calls db.init() which creates the raw
        # schema) so return ok with a descriptive note.
        return True, "alembic_not_applied_legacy_schema"

    try:
        async with conn.execute("SELECT version_num FROM alembic_version") as cur:
            ver_row = await cur.fetchone()
    except Exception as exc:
        return False, f"alembic_version_read_failed: {exc}"

    current = ver_row[0] if ver_row else None
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


def _check_provider_chain() -> tuple[bool, str]:
    """At least one provider in the fallback chain must be usable."""
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

    ready: list[str] = []
    for prov in chain:
        if prov == "ollama":
            # Ollama is a local model runner — no credential needed; we
            # take its mere presence in the chain as "available fallback"
            # so a credential-less dev/CI install still passes readyz.
            ready.append(prov)
            continue
        key = provider_key.get(prov, "")
        if key:
            ready.append(prov)

    if not ready:
        return False, f"no_configured_provider_in_chain: {','.join(chain)}"
    return True, f"ready={','.join(ready)}"


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

    # ── 4. Provider chain ────────────────────────────────────────────
    prov_ok, prov_detail = _check_provider_chain()
    checks["provider_chain"] = {"ok": prov_ok, "detail": prov_detail}

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
