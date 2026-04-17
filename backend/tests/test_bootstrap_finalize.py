"""L1 — ``POST /api/v1/bootstrap/finalize`` endpoint tests.

Validates the admin-only finalize transition:

  * happy path — every gate green + all required steps recorded →
    200, persisted ``bootstrap_finalized=true``, ``finalized`` row landed
  * guard — gates red → 409 with ``status`` + ``missing_steps`` payload
  * guard — required step missing → 409 naming the absent step
  * auth — non-admin / unauthenticated → 401/403
  * status probe — ``GET /bootstrap/status`` returns the four-gate dict

The finalize route lives under ``/bootstrap/*`` so the global wizard
gate middleware in :mod:`backend.main` lets it through during install.
"""

from __future__ import annotations

import pytest

from backend import auth as _au
from backend import bootstrap as _boot


def _make_admin(user_id: str = "admin-u1", email: str = "admin@test.local") -> _au.User:
    return _au.User(
        id=user_id, email=email, name="admin",
        role="admin", tenant_id="t-default",
    )


def _make_viewer() -> _au.User:
    return _au.User(
        id="viewer-u1", email="v@test.local", name="viewer",
        role="viewer", tenant_id="t-default",
    )


@pytest.fixture()
def _admin_override(monkeypatch):
    """Override the FastAPI admin dependency with a fixed admin user.

    Relies on the shared async `client` fixture (see conftest.py) to
    spin up a fresh sqlite + AsyncClient. The shared fixture already
    pins `get_bootstrap_status` to green; individual tests that want a
    red status re-monkeypatch it and reset the gate cache.
    """
    from backend.main import app

    admin = _make_admin()
    app.dependency_overrides[_au.require_admin] = lambda: admin
    app.dependency_overrides[_au.current_user] = lambda: admin
    # Isolate the marker file so it doesn't touch data/.bootstrap_state.json.
    import tempfile
    from pathlib import Path
    tmp = tempfile.mkdtemp(prefix="omnisight_boot_fin_")
    _boot._reset_for_tests(Path(tmp) / "marker.json")
    try:
        yield admin
    finally:
        app.dependency_overrides.pop(_au.require_admin, None)
        app.dependency_overrides.pop(_au.current_user, None)
        _boot._reset_for_tests()


@pytest.fixture()
def _viewer_override():
    """Override require_admin with a dep that raises 403 (non-admin)."""
    from fastapi import HTTPException
    from backend.main import app

    viewer = _make_viewer()

    def _forbid():
        raise HTTPException(status_code=403, detail="admin only")

    app.dependency_overrides[_au.require_admin] = _forbid
    app.dependency_overrides[_au.current_user] = lambda: viewer
    import tempfile
    from pathlib import Path
    tmp = tempfile.mkdtemp(prefix="omnisight_boot_fin_")
    _boot._reset_for_tests(Path(tmp) / "marker.json")
    try:
        yield viewer
    finally:
        app.dependency_overrides.pop(_au.require_admin, None)
        app.dependency_overrides.pop(_au.current_user, None)
        _boot._reset_for_tests()


# ── happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_happy_path_200_and_flag(client, _admin_override):
    admin = _admin_override
    for step in _boot.REQUIRED_STEPS:
        await _boot.record_bootstrap_step(step, actor_user_id=admin.id)

    r = await client.post(
        "/api/v1/bootstrap/finalize",
        json={"reason": "done"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["finalized"] is True
    assert body["actor_user_id"] == admin.id
    assert body["status"] == {
        "admin_password_default": False,
        "llm_provider_configured": True,
        "cf_tunnel_configured": True,
        "smoke_passed": True,
    }
    assert _boot.is_bootstrap_finalized_flag() is True
    fin = await _boot.get_bootstrap_step(_boot.STEP_FINALIZED)
    assert fin is not None
    assert fin["actor_user_id"] == admin.id
    assert fin["metadata"].get("reason") == "done"


@pytest.mark.asyncio
async def test_finalize_works_without_body(client, _admin_override):
    admin = _admin_override
    for step in _boot.REQUIRED_STEPS:
        await _boot.record_bootstrap_step(step, actor_user_id=admin.id)
    r = await client.post(
        "/api/v1/bootstrap/finalize", follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    assert r.json()["finalized"] is True


# ── guard: gates red ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_409_when_gates_red(client, _admin_override, monkeypatch):
    admin = _admin_override

    async def _red():
        return _boot.BootstrapStatus(
            admin_password_default=True,
            llm_provider_configured=False,
            cf_tunnel_configured=False,
            smoke_passed=False,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _red)
    _boot._gate_cache_reset()

    for step in _boot.REQUIRED_STEPS:
        await _boot.record_bootstrap_step(step, actor_user_id=admin.id)
    # is_bootstrap_finalized would now read red live → the gate middleware
    # would 307. Force the gate cache to treat the app as finalized so
    # the route reaches the handler and we observe the handler's own
    # 409 logic (what we actually care about here).
    _boot._gate_cache["finalized"] = True
    _boot._gate_cache["ts"] = 1e18

    r = await client.post(
        "/api/v1/bootstrap/finalize",
        json={"reason": "try"},
        follow_redirects=False,
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert "status" in body
    assert body["status"]["admin_password_default"] is True
    assert _boot.is_bootstrap_finalized_flag() is False


# ── guard: missing step rows ────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_409_when_required_step_missing(client, _admin_override):
    admin = _admin_override
    # Gates are green (from shared `client` fixture) but only one step
    # was recorded.
    await _boot.record_bootstrap_step(
        _boot.STEP_ADMIN_PASSWORD, actor_user_id=admin.id,
    )
    r = await client.post(
        "/api/v1/bootstrap/finalize", follow_redirects=False,
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert "missing_steps" in body
    missing = set(body["missing_steps"])
    assert _boot.STEP_LLM_PROVIDER in missing
    assert _boot.STEP_CF_TUNNEL in missing
    assert _boot.STEP_SMOKE in missing
    assert _boot.STEP_ADMIN_PASSWORD not in missing
    assert _boot.is_bootstrap_finalized_flag() is False


# ── guard: non-admin ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_forbidden_for_non_admin(client, _viewer_override):
    r = await client.post(
        "/api/v1/bootstrap/finalize",
        json={},
        follow_redirects=False,
    )
    assert r.status_code in (401, 403)


# ── status probe ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_status_endpoint_public(client, monkeypatch):
    """GET /bootstrap/status is callable without admin auth — the wizard
    UI needs to read it before the operator has set a password.

    Re-uses the shared `client` fixture which pins status to green; we
    override it to red for this probe so the response exercises the
    full shape (status + all_green + missing_steps).
    """
    import tempfile
    from pathlib import Path
    _boot._reset_for_tests(Path(tempfile.mkdtemp()) / "marker.json")

    async def _red():
        return _boot.BootstrapStatus(
            admin_password_default=True,
            llm_provider_configured=False,
            cf_tunnel_configured=False,
            smoke_passed=False,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _red)
    _boot._gate_cache_reset()
    # Same middleware-bypass trick as the 409 test — the wizard gate
    # would otherwise 307 because live status is red.
    _boot._gate_cache["finalized"] = True
    _boot._gate_cache["ts"] = 1e18

    r = await client.get(
        "/api/v1/bootstrap/status", follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"]["admin_password_default"] is True
    assert body["all_green"] is False
    assert body["finalized"] is False
    assert set(body["missing_steps"]) == set(_boot.REQUIRED_STEPS)
    _boot._reset_for_tests()
