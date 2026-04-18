"""L1 #2 — Bootstrap wizard gate middleware tests.

Covers the global redirect middleware added in
:mod:`backend.main` (`_bootstrap_gate`):

  * exempt paths never redirect (wizard, auth/login, healthz, static,
    docs, root)
  * non-exempt paths redirect to ``/bootstrap`` with 307 while any
    bootstrap gate is still red
  * once :func:`backend.bootstrap.is_bootstrap_finalized` returns True
    the middleware steps aside and the app serves requests normally
  * the in-process cache invalidates correctly (TTL + sticky-on-green)
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend import bootstrap
from backend.main import _bootstrap_path_is_exempt
from backend.rate_limit import reset_limiters


@pytest.fixture(autouse=True)
def _clean():
    """Reset limiter + bootstrap gate cache between tests."""
    reset_limiters()
    bootstrap._gate_cache_reset()
    yield
    bootstrap._gate_cache_reset()
    reset_limiters()


# ── exempt-path matcher (unit) ──────────────────────────────────


@pytest.mark.parametrize(
    ("path", "rel", "expected"),
    [
        # wizard itself — raw and api-prefixed
        ("/bootstrap", "/bootstrap", True),
        ("/bootstrap/", "/bootstrap/", True),
        ("/bootstrap/status", "/bootstrap/status", True),
        ("/api/v1/bootstrap/finalize", "/bootstrap/finalize", True),
        # auth + health probes
        ("/api/v1/auth/login", "/auth/login", True),
        ("/api/v1/auth/logout", "/auth/logout", True),
        ("/api/v1/auth/change-password", "/auth/change-password", True),
        ("/healthz", "/healthz", True),
        ("/api/v1/health", "/health", True),
        # static / framework resources
        ("/_next/static/chunks/main.js", "/_next/static/chunks/main.js", True),
        ("/static/logo.png", "/static/logo.png", True),
        ("/assets/app.css", "/assets/app.css", True),
        ("/favicon.ico", "/favicon.ico", True),
        ("/robots.txt", "/robots.txt", True),
        # common asset suffixes
        ("/icon.svg", "/icon.svg", True),
        ("/main.js.map", "/main.js.map", True),
        ("/fonts/Inter.woff2", "/fonts/Inter.woff2", True),
        # docs / root
        ("/", "/", True),
        ("/docs", "/docs", True),
        ("/openapi.json", "/openapi.json", True),
        ("/redoc", "/redoc", True),
        # non-exempt API routes
        ("/api/v1/agents", "/agents", False),
        ("/api/v1/tasks", "/tasks", False),
        ("/api/v1/chat", "/chat", False),
        ("/arbitrary/page", "/arbitrary/page", False),
        # near-miss on "/bootstrap" shouldn't over-match
        ("/bootstrapped", "/bootstrapped", False),
    ],
)
def test_exempt_path_matcher(path, rel, expected):
    assert _bootstrap_path_is_exempt(path, rel) is expected


# ── is_bootstrap_finalized cache behaviour ──────────────────────


@pytest.mark.asyncio
async def test_is_bootstrap_finalized_sticks_once_green(monkeypatch):
    """Once the gate flips green in a process it shouldn't flap."""
    calls = {"n": 0}

    async def _fake_status():
        calls["n"] += 1
        return bootstrap.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(bootstrap, "get_bootstrap_status", _fake_status)
    assert await bootstrap.is_bootstrap_finalized() is True
    # second + third calls must not re-probe the status
    assert await bootstrap.is_bootstrap_finalized() is True
    assert await bootstrap.is_bootstrap_finalized() is True
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_is_bootstrap_finalized_rechecks_while_red(monkeypatch):
    """While still red the middleware must keep re-checking within TTL."""
    # Very small TTL so the test is fast but deterministic.
    monkeypatch.setattr(bootstrap, "_GATE_CACHE_TTL", 0.01)

    calls = {"n": 0}

    async def _fake_status():
        calls["n"] += 1
        return bootstrap.BootstrapStatus(
            admin_password_default=True,
            llm_provider_configured=False,
            cf_tunnel_configured=False,
            smoke_passed=False,
        )

    monkeypatch.setattr(bootstrap, "get_bootstrap_status", _fake_status)
    assert await bootstrap.is_bootstrap_finalized() is False
    # Within TTL → cached (no second probe)
    assert await bootstrap.is_bootstrap_finalized() is False
    assert calls["n"] == 1
    # After TTL → re-probe
    await asyncio.sleep(0.02)
    assert await bootstrap.is_bootstrap_finalized() is False
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_is_bootstrap_finalized_fails_open_on_probe_error(monkeypatch):
    """Broken DB must never lock operators out — fail-open to True."""

    async def _boom():
        raise RuntimeError("db exploded")

    monkeypatch.setattr(bootstrap, "get_bootstrap_status", _boom)
    assert await bootstrap.is_bootstrap_finalized() is True


# ── end-to-end middleware (using the `client` fixture) ──────────


@pytest.fixture()
def _force_red(monkeypatch):
    """Pin bootstrap status to not-finalized for the duration of a test."""

    async def _red():
        return bootstrap.BootstrapStatus(
            admin_password_default=True,
            llm_provider_configured=False,
            cf_tunnel_configured=False,
            smoke_passed=False,
        )

    monkeypatch.setattr(bootstrap, "get_bootstrap_status", _red)
    bootstrap._gate_cache_reset()
    yield


@pytest.fixture()
def _force_green(monkeypatch):
    async def _green():
        return bootstrap.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(bootstrap, "get_bootstrap_status", _green)
    bootstrap._gate_cache_reset()
    yield


@pytest.mark.asyncio
async def test_gate_redirects_non_exempt_when_bootstrap_red(client, _force_red):
    # API paths get 503 JSON (not 307 redirect) so the frontend can
    # handle the response programmatically without OOM'ing on redirect
    # loops. See commit af18eb0.
    r = await client.get("/api/v1/agents", follow_redirects=False)
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "bootstrap_required"
    assert body["redirect"] == "/bootstrap"

    # Browser page navigations still get 307 redirect.
    r2 = await client.get("/some-page", follow_redirects=False)
    assert r2.status_code == 307
    assert r2.headers["location"] == "/bootstrap"


@pytest.mark.asyncio
async def test_gate_lets_bootstrap_paths_through(client, _force_red):
    """Wizard paths must never self-redirect (they'd infinite-loop)."""
    # Wizard API path — route may 404 (no router yet) but MUST NOT 307.
    r = await client.get("/api/v1/bootstrap/status", follow_redirects=False)
    assert r.status_code != 307


@pytest.mark.asyncio
async def test_gate_lets_auth_login_through(client, _force_red):
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "x@y.z", "password": "nope"},
        follow_redirects=False,
    )
    assert r.status_code != 307


@pytest.mark.asyncio
async def test_gate_lets_healthz_through(client, _force_red):
    """Liveness probe must always succeed so k8s doesn't kill the pod."""
    r = await client.get("/api/v1/health", follow_redirects=False)
    assert r.status_code == 200
    # Also the /healthz alias — may 404 (no route) but MUST NOT 307.
    r2 = await client.get("/healthz", follow_redirects=False)
    assert r2.status_code != 307


@pytest.mark.asyncio
async def test_gate_lets_static_resources_through(client, _force_red):
    for path in (
        "/_next/static/chunks/main.js",
        "/favicon.ico",
        "/static/logo.png",
        "/assets/app.css",
    ):
        r = await client.get(path, follow_redirects=False)
        assert r.status_code != 307, f"{path} was redirected"


@pytest.mark.asyncio
async def test_gate_lets_docs_through(client, _force_red):
    for path in ("/", "/docs", "/openapi.json"):
        r = await client.get(path, follow_redirects=False)
        assert r.status_code != 307, f"{path} was redirected"


@pytest.mark.asyncio
async def test_gate_steps_aside_once_green(client, _force_green):
    """When every gate is green non-exempt paths reach their handler."""
    r = await client.get("/api/v1/agents", follow_redirects=False)
    assert r.status_code != 307


@pytest.mark.asyncio
async def test_gate_returns_503_for_api_post_when_red(client, _force_red):
    """API POST gets 503 JSON (not 307 redirect) to prevent OOM."""
    r = await client.post(
        "/api/v1/agents",
        json={"name": "test"},
        follow_redirects=False,
    )
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "bootstrap_required"


@pytest.mark.asyncio
async def test_gate_preserves_method_on_browser_redirect(client, _force_red):
    """Browser page navigations still get 307 (not 302) to preserve method."""
    r = await client.post(
        "/some-form-page",
        content="data",
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"] == "/bootstrap"
