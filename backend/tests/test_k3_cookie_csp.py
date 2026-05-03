"""K3 tests — cookie flags + CSP + security response headers.

Task #97 migration (2026-04-21): fixture ported from SQLite tempfile
to pg_test_pool. The HTTP client fixture sets
``OMNISIGHT_DATABASE_URL`` so routes still on the ``db._conn()``
compat wrapper (auth/login, auth/logout, health) read/write the
same PG that pg_test_pool's TRUNCATE targets.
"""

from __future__ import annotations

import base64
import hashlib
import re

import pytest


@pytest.fixture()
async def _k3_client(pg_test_pool, pg_test_dsn, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "true")
    monkeypatch.setenv("OMNISIGHT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("OMNISIGHT_ADMIN_PASSWORD", "test-strong-password-123")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import db
    from backend.main import app
    from backend import bootstrap as _boot
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )
    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    if db._db is not None:
        await db.close()
    await db.init()
    from backend import auth
    await auth.ensure_default_admin()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


def _parse_set_cookie(resp, cookie_name: str) -> dict:
    """Extract flags from a Set-Cookie header by name."""
    for h in resp.headers.multi_items():
        if h[0].lower() != "set-cookie":
            continue
        val = h[1]
        if val.startswith(f"{cookie_name}="):
            parts = [p.strip().lower() for p in val.split(";")]
            flags = {}
            for part in parts[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    flags[k] = v
                else:
                    flags[part] = True
            return flags
    return {}


# ── Cookie flag tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_cookie_httponly_secure_samesite(_k3_client):
    c = _k3_client
    resp = await c.post("/api/v1/auth/login", json={
        "email": "admin@test.local",
        "password": "test-strong-password-123",
    })
    assert resp.status_code == 200

    flags = _parse_set_cookie(resp, "omnisight_session")
    assert flags.get("httponly") is True, "session cookie must be HttpOnly"
    assert flags.get("secure") is True, "session cookie must be Secure"
    assert flags.get("samesite") == "lax", "session cookie must be SameSite=Lax"


@pytest.mark.asyncio
async def test_csrf_cookie_not_httponly_but_secure(_k3_client):
    c = _k3_client
    resp = await c.post("/api/v1/auth/login", json={
        "email": "admin@test.local",
        "password": "test-strong-password-123",
    })
    assert resp.status_code == 200

    flags = _parse_set_cookie(resp, "omnisight_csrf")
    assert "httponly" not in flags, "CSRF cookie must NOT be HttpOnly (frontend reads it)"
    assert flags.get("secure") is True, "CSRF cookie must be Secure"
    assert flags.get("samesite") == "lax", "CSRF cookie must be SameSite=Lax"


# ── Security response header tests ────────────────────────────


@pytest.mark.asyncio
async def test_security_headers_present(_k3_client):
    c = _k3_client
    resp = await c.get("/api/v1/health")
    h = resp.headers

    assert h.get("x-frame-options") == "DENY"
    assert h.get("x-content-type-options") == "nosniff"
    assert "max-age=" in (h.get("strict-transport-security") or "")
    assert h.get("referrer-policy") == "strict-origin"
    assert "camera=()" in (h.get("permissions-policy") or "")


@pytest.mark.asyncio
async def test_csp_no_unsafe_eval(_k3_client):
    c = _k3_client
    resp = await c.get("/api/v1/health")
    csp = resp.headers.get("content-security-policy") or ""

    assert "'unsafe-eval'" not in csp, "CSP must never allow unsafe-eval"
    assert "script-src" in csp
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp


@pytest.mark.asyncio
async def test_csp_script_src_no_unsafe_inline(_k3_client):
    """K3: script-src must not contain 'unsafe-inline' — nonce-based enforced."""
    c = _k3_client
    resp = await c.get("/api/v1/health")
    csp = resp.headers.get("content-security-policy") or ""

    script_src = ""
    for directive in csp.split(";"):
        d = directive.strip()
        if d.startswith("script-src"):
            script_src = d
            break

    assert script_src, "CSP must contain a script-src directive"
    assert "'unsafe-inline'" not in script_src, \
        "script-src must not contain 'unsafe-inline' (use nonce-based)"


@pytest.mark.asyncio
async def test_csp_script_src_contains_unique_nonce(_k3_client):
    c = _k3_client
    first = await c.get("/api/v1/health")
    second = await c.get("/api/v1/health")

    def _nonce(resp) -> str:
        csp = resp.headers.get("content-security-policy") or ""
        match = re.search(r"'nonce-([^']+)'", csp)
        assert match, f"CSP must contain a nonce source: {csp}"
        return match.group(1)

    first_nonce = _nonce(first)
    second_nonce = _nonce(second)

    assert len(base64.b64decode(first_nonce)) == 18
    assert first_nonce != second_nonce


@pytest.mark.asyncio
async def test_csp_hash_sources_auto_generated():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from starlette.responses import HTMLResponse

    from backend.main import _security_headers

    inline_script = "console.log('SC.6.1')"
    expected_hash = base64.b64encode(
        hashlib.sha256(inline_script.encode("utf-8")).digest()
    ).decode("ascii")

    probe = FastAPI()
    probe.middleware("http")(_security_headers)

    @probe.get("/")
    async def _probe():
        return HTMLResponse(f"<script>{inline_script}</script>")

    transport = ASGITransport(app=probe)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/")

    csp = resp.headers.get("content-security-policy") or ""
    assert f"'sha256-{expected_hash}'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]


@pytest.mark.asyncio
async def test_logout_clears_cookies(_k3_client):
    c = _k3_client
    login_resp = await c.post("/api/v1/auth/login", json={
        "email": "admin@test.local",
        "password": "test-strong-password-123",
    })
    assert login_resp.status_code == 200
    cookies = dict(login_resp.cookies)
    csrf = login_resp.json()["csrf_token"]

    logout_resp = await c.post(
        "/api/v1/auth/logout",
        cookies=cookies,
        headers={"X-CSRF-Token": csrf},
    )
    assert logout_resp.status_code == 200

    for h_name, h_val in logout_resp.headers.multi_items():
        if h_name.lower() == "set-cookie":
            if "omnisight_session=" in h_val or "omnisight_csrf=" in h_val:
                assert 'max-age=0' in h_val.lower() or '=""' in h_val or "expires=" in h_val.lower(), \
                    f"Logout must expire cookie: {h_val}"
