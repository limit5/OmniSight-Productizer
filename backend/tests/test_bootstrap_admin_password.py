"""L2 — ``POST /api/v1/bootstrap/admin-password`` wizard Step 1 tests.

Covers the first-install password rotation endpoint that the wizard
drives before any admin session exists:

  * happy path — default admin + correct current password →
    200, ``must_change_password`` cleared, ``admin_password_set`` row
    recorded, audit row ``bootstrap.admin_password_set`` written,
    ``admin_password_default`` gate flips green
  * 409 — no admin currently requires a password change (flag already
    cleared / custom password at install time)
  * 401 — wrong current password does NOT touch the target row
  * 422 — weak new password (too short / too guessable) is rejected
    before any DB mutation
  * Helper — :func:`auth.find_admin_requiring_password_change` returns
    the sole flagged admin (or ``None`` once the flag clears)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from backend import auth as _au
from backend import bootstrap as _boot


# ─────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────


@pytest.fixture()
async def _wizard_db(monkeypatch):
    """Fresh sqlite + isolated bootstrap marker for Step 1 integration.

    Uses the shared `client` fixture's DB-per-test pattern directly so
    :func:`auth.ensure_default_admin` lands the default admin row in a
    scoped DB and the wizard marker never touches `data/`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "wizard_step1.db")
        marker = os.path.join(tmp, ".bootstrap_state.json")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", db_path)
        monkeypatch.setenv("OMNISIGHT_ADMIN_EMAIL", "admin@test.local")
        monkeypatch.delenv("OMNISIGHT_ADMIN_PASSWORD", raising=False)

        from backend import config as _cfg
        _cfg.settings.database_path = db_path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        _boot._reset_for_tests(Path(marker))

        user = await _au.ensure_default_admin()
        assert user is not None and user.must_change_password is True
        try:
            yield {"db": db, "admin": user}
        finally:
            await db.close()
            _boot._reset_for_tests()


@pytest.fixture()
async def _wizard_client(_wizard_db, monkeypatch):
    """Async HTTP client with the bootstrap gate bypassed for the route.

    The gate middleware already exempts ``/bootstrap/*`` so the wizard's
    own endpoints can be reached pre-finalize; we still pin the cache so
    no other probe flaps the gate during a test.
    """
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    _boot._gate_cache_reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield {"client": ac, **_wizard_db}
    _boot._gate_cache_reset()


# ─────────────────────────────────────────────────────────────────
#  Helper — find_admin_requiring_password_change
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_admin_requiring_password_change_returns_default_admin(_wizard_db):
    user = await _au.find_admin_requiring_password_change()
    assert user is not None
    assert user.role == "admin"
    assert user.enabled is True
    assert user.must_change_password is True


@pytest.mark.asyncio
async def test_find_admin_requiring_password_change_none_after_rotation(_wizard_db):
    admin = _wizard_db["admin"]
    await _au.change_password(admin.id, "rotated-strong-password-123")
    user = await _au.find_admin_requiring_password_change()
    assert user is None


# ─────────────────────────────────────────────────────────────────
#  Happy path
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_password_endpoint_rotates_and_records_step(_wizard_client):
    client = _wizard_client["client"]
    admin = _wizard_client["admin"]

    r = await client.post(
        "/api/v1/bootstrap/admin-password",
        json={
            "current_password": "omnisight-admin",
            "new_password": "rotated-strong-password-abc-123",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "password_changed"
    assert body["admin_password_default"] is False
    assert body["user_id"] == admin.id

    # Flag cleared on the users row
    refreshed = await _au.get_user(admin.id)
    assert refreshed is not None
    assert refreshed.must_change_password is False

    # New password verifies, old one doesn't
    assert await _au.authenticate_password(
        admin.email, "rotated-strong-password-abc-123",
    ) is not None
    assert await _au.authenticate_password(
        admin.email, "omnisight-admin",
    ) is None

    # bootstrap_state records the step with the admin as actor
    row = await _boot.get_bootstrap_step(_boot.STEP_ADMIN_PASSWORD)
    assert row is not None
    assert row["actor_user_id"] == admin.id
    assert row["metadata"].get("email") == admin.email

    # L1 gate flips green after rotation
    status = await _boot.get_bootstrap_status()
    assert status.admin_password_default is False


@pytest.mark.asyncio
async def test_admin_password_endpoint_writes_audit_row(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/admin-password",
        json={
            "current_password": "omnisight-admin",
            "new_password": "rotated-strong-password-abc-123",
        },
    )
    assert r.status_code == 200, r.text

    from backend import audit
    rows = await audit.query(entity_kind="bootstrap", limit=50)
    actions = [row["action"] for row in rows]
    assert "bootstrap.admin_password_set" in actions


# ─────────────────────────────────────────────────────────────────
#  Error paths
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_password_endpoint_409_when_no_flagged_admin(_wizard_client):
    """Already-rotated install shouldn't let the endpoint be replayed."""
    admin = _wizard_client["admin"]
    await _au.change_password(admin.id, "already-rotated-password-xyz")

    r = await _wizard_client["client"].post(
        "/api/v1/bootstrap/admin-password",
        json={
            "current_password": "already-rotated-password-xyz",
            "new_password": "second-attempt-password-xyz-123",
        },
    )
    assert r.status_code == 409, r.text
    assert r.json()["admin_password_default"] is False


@pytest.mark.asyncio
async def test_admin_password_endpoint_401_on_wrong_current(_wizard_client):
    admin = _wizard_client["admin"]
    r = await _wizard_client["client"].post(
        "/api/v1/bootstrap/admin-password",
        json={
            "current_password": "definitely-not-the-default",
            "new_password": "rotated-strong-password-abc-123",
        },
    )
    assert r.status_code == 401, r.text

    # Flag unchanged — attacker without the default password cannot
    # invalidate it.
    refreshed = await _au.get_user(admin.id)
    assert refreshed is not None
    assert refreshed.must_change_password is True
    # No step row written
    assert await _boot.get_bootstrap_step(_boot.STEP_ADMIN_PASSWORD) is None


@pytest.mark.asyncio
async def test_admin_password_endpoint_422_on_weak_new_password(_wizard_client):
    admin = _wizard_client["admin"]
    # Too short — pydantic rejects at the request layer.
    r = await _wizard_client["client"].post(
        "/api/v1/bootstrap/admin-password",
        json={"current_password": "omnisight-admin", "new_password": "short"},
    )
    assert r.status_code == 422, r.text

    # Long enough but obvious → zxcvbn score < 3 → handler 422.
    r2 = await _wizard_client["client"].post(
        "/api/v1/bootstrap/admin-password",
        json={
            "current_password": "omnisight-admin",
            "new_password": "password1234",
        },
    )
    assert r2.status_code == 422, r2.text

    # Flag untouched either way
    refreshed = await _au.get_user(admin.id)
    assert refreshed is not None
    assert refreshed.must_change_password is True
