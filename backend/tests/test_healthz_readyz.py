"""G1 #2 — ``/healthz`` (liveness) + ``/readyz`` (readiness) contracts.

Covers ``backend/routers/health.py``:

  * ``/healthz`` — liveness
      - mounted at the server root (no ``/api/v1`` prefix)
      - mirrored under ``/api/v1/healthz`` for API-prefix-only callers
      - returns 200 without touching the database
      - stays 200 while the shutdown coordinator is draining (so the
        orchestrator can still tell "process alive")
  * ``/readyz`` — readiness
      - mounted at the server root (no prefix) + mirrored under the
        API prefix
      - structured JSON response with per-check booleans
      - 503 with Retry-After while draining
      - 503 when the DB cannot be pinged
      - 503 when every provider in ``llm_fallback_chain`` is missing
        credentials
      - 200 when DB + migrations + provider chain are all healthy
      - ollama-in-chain counts as "configured" even without credentials
"""

from __future__ import annotations

import pytest

from backend import lifecycle
from backend.routers import health as health_mod


# ──────────────────────────────────────────────────────────────
#  Fixture — reset the shutdown coordinator between tests.
# ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_lifecycle():
    lifecycle.coordinator.reset_for_tests()
    yield
    lifecycle.coordinator.reset_for_tests()


# ──────────────────────────────────────────────────────────────
#  Unit — liveness
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_returns_fast_payload():
    """Pure liveness: no I/O. Must return ``status=ok`` and ``live=True``."""
    result = await health_mod.healthz()
    assert result == {"status": "ok", "live": True}


@pytest.mark.asyncio
async def test_healthz_prefixed_delegates_to_same_handler():
    a = await health_mod.healthz()
    b = await health_mod.healthz_prefixed()
    assert a == b


@pytest.mark.asyncio
async def test_livez_alias_matches_healthz_payload():
    """``/livez`` is the K8s-charter spelling of liveness (G5 #4). It
    must delegate to the same handler as ``/healthz`` so the two spellings
    stay byte-identical — no drift between compose-era callers and K8s
    probes."""
    a = await health_mod.healthz()
    b = await health_mod.livez()
    c = await health_mod.livez_prefixed()
    assert a == b == c


# ──────────────────────────────────────────────────────────────
#  Unit — individual readyz checks
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_db_fails_when_not_initialized(monkeypatch):
    """Before db.init() runs, _conn() raises RuntimeError. That must
    surface as a structured ``db_not_initialized`` detail, not a 500."""
    from backend import db as _db

    monkeypatch.setattr(_db, "_db", None)
    ok, detail = await health_mod._check_db()
    assert ok is False
    assert "db_not_initialized" in detail


@pytest.mark.asyncio
async def test_check_db_passes_after_init(client):
    """The client fixture calls db.init() already, so the ping must
    succeed for the remainder of the test."""
    ok, detail = await health_mod._check_db()
    assert ok is True
    assert detail == "ok"


@pytest.mark.asyncio
async def test_check_migrations_legacy_schema_is_allowed(client):
    """Without alembic having run, db.init() still builds the raw
    schema. The readyz migration check tolerates this path so ``main``
    deploys aren't blocked by a missing alembic run."""
    ok, detail = await health_mod._check_migrations()
    assert ok is True
    assert "legacy" in detail or "current=" in detail


def test_check_provider_chain_requires_credential_or_ollama(monkeypatch):
    """At least one chain entry must have credentials — except ollama,
    which is treated as always-available (local fallback)."""
    from backend.config import settings

    # Empty chain → explicitly fail
    monkeypatch.setattr(settings, "llm_fallback_chain", "")
    ok, detail = health_mod._check_provider_chain()
    assert ok is False
    assert "empty" in detail

    # All paid providers with no keys → fail
    monkeypatch.setattr(settings, "llm_fallback_chain", "anthropic,openai")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    ok, detail = health_mod._check_provider_chain()
    assert ok is False
    assert "no_configured_provider" in detail

    # One provider has a key → pass
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    ok, detail = health_mod._check_provider_chain()
    assert ok is True
    assert "anthropic" in detail

    # Ollama alone → pass even without credentials
    monkeypatch.setattr(settings, "llm_fallback_chain", "ollama")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    ok, detail = health_mod._check_provider_chain()
    assert ok is True
    assert "ollama" in detail


# ──────────────────────────────────────────────────────────────
#  Integration — ASGI probes
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_root_200(client, monkeypatch):
    """/healthz at the server root must answer 200 with the liveness
    payload — no API prefix, no auth, no DB."""
    r = await client.get("/healthz", follow_redirects=False)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["live"] is True


@pytest.mark.asyncio
async def test_healthz_prefixed_200(client):
    """/api/v1/healthz mirrors the root-level probe."""
    r = await client.get("/api/v1/healthz", follow_redirects=False)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_livez_root_200(client):
    """``/livez`` (root) must answer 200 with the same liveness payload
    as ``/healthz`` — K8s probes target the ``/livez`` spelling per the
    G5 #4 charter commitment."""
    r = await client.get("/livez", follow_redirects=False)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["live"] is True


@pytest.mark.asyncio
async def test_livez_prefixed_200(client):
    """``/api/v1/livez`` mirrors the root-level K8s-probe path."""
    r = await client.get("/api/v1/livez", follow_redirects=False)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["live"] is True


@pytest.mark.asyncio
async def test_livez_stays_200_while_draining(client):
    """Liveness must keep answering 200 during graceful shutdown at the
    K8s-probe spelling too — otherwise K8s would restart draining pods
    instead of letting them finish in-flight work."""
    lifecycle.coordinator.begin_draining()
    r = await client.get("/livez", follow_redirects=False)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_healthz_stays_200_while_draining(client):
    """Liveness must keep answering 200 during graceful shutdown — the
    orchestrator needs to distinguish "draining" from "dead"."""
    lifecycle.coordinator.begin_draining()
    r = await client.get("/healthz", follow_redirects=False)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_legacy_health_endpoint_preserved(client):
    """The pre-existing ``/api/v1/health`` endpoint must keep working
    for the wizard/UI clients that already depend on it."""
    r = await client.get("/api/v1/health", follow_redirects=False)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "online"


@pytest.mark.asyncio
async def test_readyz_200_when_everything_healthy(client, monkeypatch):
    """Happy-path: DB up, legacy schema migrations, one provider with
    a key → 200 with ``ready=True`` and per-check ``ok=True``."""
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "anthropic,ollama")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")

    r = await client.get("/readyz", follow_redirects=False)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["ready"] is True
    assert body["checks"]["db"]["ok"] is True
    assert body["checks"]["migrations"]["ok"] is True
    assert body["checks"]["provider_chain"]["ok"] is True
    assert body["checks"]["draining"]["ok"] is True


@pytest.mark.asyncio
async def test_readyz_prefixed_mirrors_root(client, monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "ollama")

    r = await client.get("/api/v1/readyz", follow_redirects=False)
    assert r.status_code == 200
    assert r.json()["ready"] is True


@pytest.mark.asyncio
async def test_readyz_503_with_retry_after_while_draining(client, monkeypatch):
    """G1 bullet #2: readyz must fail closed the instant SIGTERM flips
    the drain flag, so the LB takes this replica out of rotation before
    the 30s in-flight budget expires."""
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "ollama")

    lifecycle.coordinator.begin_draining()
    r = await client.get("/readyz", follow_redirects=False)
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "30"
    body = r.json()
    assert body["ready"] is False
    assert body["checks"]["draining"]["ok"] is False


@pytest.mark.asyncio
async def test_readyz_503_when_provider_chain_empty(client, monkeypatch):
    """No credentials, no ollama → readyz must refuse to advertise the
    replica as ready. DB is still fine so that check stays green."""
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "anthropic,openai")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")

    r = await client.get("/readyz", follow_redirects=False)
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["checks"]["provider_chain"]["ok"] is False
    # DB should still be reachable under the client fixture.
    assert body["checks"]["db"]["ok"] is True


@pytest.mark.asyncio
async def test_readyz_503_when_db_unavailable(client, monkeypatch):
    """Force a DB ping failure by monkeypatching the connection.
    Everything else healthy → 503 with db.ok=False."""
    from backend.config import settings
    from backend import db as _db

    monkeypatch.setattr(settings, "llm_fallback_chain", "ollama")

    def _fake_conn():
        raise RuntimeError("Database not initialized — call db.init() first")

    monkeypatch.setattr(_db, "_conn", _fake_conn)

    r = await client.get("/readyz", follow_redirects=False)
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["checks"]["db"]["ok"] is False
    assert "db_not_initialized" in body["checks"]["db"]["detail"]


@pytest.mark.asyncio
async def test_readyz_carries_retry_after_when_not_ready(client, monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    r = await client.get("/readyz", follow_redirects=False)
    assert r.status_code == 503
    # Smaller hint than the draining branch — upstream can reprobe quickly
    # once the provider credential is actually configured.
    assert r.headers.get("Retry-After") == "5"


@pytest.mark.asyncio
async def test_readyz_is_not_rate_limited(client, monkeypatch):
    """/readyz must stay reachable under rate-limit pressure because
    it's how the LB decides to drain a replica."""
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_fallback_chain", "ollama")

    for _ in range(20):
        r = await client.get("/readyz", follow_redirects=False)
        assert r.status_code == 200
