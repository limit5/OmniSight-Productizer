"""K3 tests — cookie flags + CSP + security response headers."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
async def _k3_client(monkeypatch, tmp_path):
    db_path = tmp_path / "k3.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "true")
    monkeypatch.setenv("OMNISIGHT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("OMNISIGHT_ADMIN_PASSWORD", "test-strong-password-123")

    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()

    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    await db.init()
    from backend import auth
    await auth.ensure_default_admin()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        await db.close()


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
