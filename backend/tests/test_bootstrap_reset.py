"""L8 #1 — ``POST /api/v1/bootstrap/reset`` QA escape-hatch tests.

Validates the admin + dev-mode-only reset transition that wipes the
wizard state for QA reruns:

  * happy path — dev mode + admin → 200, ``bootstrap_state`` rows
    deleted, marker file removed, every enabled admin re-flagged
    ``must_change_password=1``, audit row written, gate cache reset
  * mode guard — non-dev deploy mode → 403, no DB or marker mutation
  * auth guard — non-admin caller → 401/403
  * idempotent — replaying reset on an already-clean install still
    returns 200 (counts go to zero)
  * ``flag_all_admins_must_change_password`` helper — re-flags enabled
    admins, skips disabled ones
  * ``reset_bootstrap_state_table`` helper — returns row count + leaves
    the table itself intact
  * ``clear_marker`` helper — wipes the marker file, no-ops when the
    marker is already absent

The reset route lives under ``/bootstrap/*`` so the global wizard gate
middleware lets it through both before AND after finalize — it is the
only post-finalize wizard endpoint, since the whole point is to undo
finalize.

Task #97 migration (2026-04-21): fixtures ported from SQLite tempfile
to pg_test_pool. The HTTP client fixture replaces the conftest
``client`` fixture with a pool-backed variant — auth.py, bootstrap
helpers, and the reset route's db access all land on the same PG.
Direct ``_au._conn().execute`` accesses replaced with inline
``get_pool().acquire()`` + $N placeholders.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend import auth as _au
from backend import bootstrap as _boot


# ─────────────────────────────────────────────────────────────────
#  Helpers / fixtures
# ─────────────────────────────────────────────────────────────────


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
def _dev_mode(monkeypatch):
    """Pin deploy_mode detection to ``dev`` for the duration of the test.

    Done via the public ``OMNISIGHT_DEPLOY_MODE`` env override (the same
    knob operators flip on QA hosts) so the test exercises the same
    code path production callers will hit, not a monkey-patched probe.
    """
    monkeypatch.setenv("OMNISIGHT_DEPLOY_MODE", "dev")
    yield "dev"


@pytest.fixture()
async def _reset_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """Pool-backed HTTP client for the ``/bootstrap/reset`` endpoint.

    Mirrors the conftest ``client`` fixture but points at the PG test
    DB via ``OMNISIGHT_DATABASE_URL`` so the db._conn() compat wrapper
    used by bootstrap helpers (and audit.log) reads/writes the same
    rows that pg_test_pool's TRUNCATE targets.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users, bootstrap_state, audit_log "
            "RESTART IDENTITY CASCADE"
        )

    from backend import db
    from backend.main import app
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

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, bootstrap_state, audit_log "
                "RESTART IDENTITY CASCADE"
            )


@pytest.fixture()
def _admin_override(monkeypatch):
    """Override the FastAPI admin dependency + isolate the marker file."""
    from backend.main import app

    admin = _make_admin()
    app.dependency_overrides[_au.require_admin] = lambda: admin
    app.dependency_overrides[_au.current_user] = lambda: admin

    tmp = tempfile.mkdtemp(prefix="omnisight_boot_reset_")
    _boot._reset_for_tests(Path(tmp) / "marker.json")
    try:
        yield admin
    finally:
        app.dependency_overrides.pop(_au.require_admin, None)
        app.dependency_overrides.pop(_au.current_user, None)
        _boot._reset_for_tests()


@pytest.fixture()
def _viewer_override():
    """Override require_admin with a dep that 403s — non-admin caller."""
    from fastapi import HTTPException
    from backend.main import app

    viewer = _make_viewer()

    def _forbid():
        raise HTTPException(status_code=403, detail="admin only")

    app.dependency_overrides[_au.require_admin] = _forbid
    app.dependency_overrides[_au.current_user] = lambda: viewer

    tmp = tempfile.mkdtemp(prefix="omnisight_boot_reset_")
    _boot._reset_for_tests(Path(tmp) / "marker.json")
    try:
        yield viewer
    finally:
        app.dependency_overrides.pop(_au.require_admin, None)
        app.dependency_overrides.pop(_au.current_user, None)
        _boot._reset_for_tests()


# ─────────────────────────────────────────────────────────────────
#  Helper-level tests
# ─────────────────────────────────────────────────────────────────


@pytest.fixture()
async def _isolated_db(pg_test_pool, pg_test_dsn, monkeypatch, tmp_path):
    """Fresh PG + isolated marker for helper-level integration tests.

    Used by tests that need direct DB access (not via the HTTP client).
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    marker = tmp_path / ".bootstrap_state.json"

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users, bootstrap_state, audit_log "
            "RESTART IDENTITY CASCADE"
        )

    from backend import db
    if db._db is not None:
        await db.close()
    await db.init()
    _boot._reset_for_tests(marker)
    try:
        yield {"db": db, "marker": str(marker)}
    finally:
        await db.close()
        _boot._reset_for_tests()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, bootstrap_state, audit_log "
                "RESTART IDENTITY CASCADE"
            )


@pytest.mark.asyncio
async def test_reset_bootstrap_state_table_returns_row_count(_isolated_db):
    """DELETE FROM bootstrap_state — count matches what was inserted."""
    await _boot.record_bootstrap_step(_boot.STEP_ADMIN_PASSWORD, actor_user_id="a1")
    await _boot.record_bootstrap_step(_boot.STEP_LLM_PROVIDER, actor_user_id="a1")
    await _boot.record_bootstrap_step(_boot.STEP_CF_TUNNEL, actor_user_id="a1")

    deleted = await _boot.reset_bootstrap_state_table()
    assert deleted == 3

    # Table itself stays — next wizard run must be able to upsert.
    assert await _boot.list_bootstrap_steps() == []
    # Re-running on an empty table reports zero, not an error.
    assert await _boot.reset_bootstrap_state_table() == 0


@pytest.mark.asyncio
async def test_clear_marker_wipes_persisted_marker(_isolated_db):
    """clear_marker() removes the JSON file + survives a missing file."""
    _boot.mark_smoke_passed(True)
    _boot.mark_cf_tunnel(configured=True)
    assert Path(_isolated_db["marker"]).exists()
    assert _boot._read_marker().get("smoke_passed") is True

    _boot.clear_marker()
    assert not Path(_isolated_db["marker"]).exists()
    assert _boot._read_marker() == {}

    # No-op on the second call — a missing marker is the desired state.
    _boot.clear_marker()


@pytest.mark.asyncio
async def test_flag_all_admins_must_change_password_skips_disabled(_isolated_db):
    """Helper re-flags enabled admins; disabled rows stay untouched."""
    a1 = await _au.create_user("ops1@test.local", "Ops One", role="admin",
                                password="initial-pw-strong-12345")
    a2 = await _au.create_user("ops2@test.local", "Ops Two", role="admin",
                                password="initial-pw-strong-67890")
    viewer = await _au.create_user("v@test.local", "Viewer", role="viewer",
                                    password="viewer-pw-strong-12345")

    # Disable a2 — must NOT be re-flagged.
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute("UPDATE users SET enabled=0 WHERE id=$1", a2.id)

    flagged = await _au.flag_all_admins_must_change_password()
    flagged_emails = {row["email"] for row in flagged}
    assert "ops1@test.local" in flagged_emails
    assert "ops2@test.local" not in flagged_emails  # disabled
    assert "v@test.local" not in flagged_emails  # not admin

    # The flag is actually persisted on the enabled admin.
    refreshed = await _au.get_user(a1.id)
    assert refreshed is not None and refreshed.must_change_password is True

    # Disabled admin untouched.
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT must_change_password FROM users WHERE id=$1", a2.id,
        )
    assert r is not None and bool(r["must_change_password"]) is False

    # Viewer untouched.
    v_ref = await _au.get_user(viewer.id)
    assert v_ref is not None and v_ref.must_change_password is False


# ─────────────────────────────────────────────────────────────────
#  Endpoint tests
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_happy_path_in_dev_mode(_reset_http_client, _admin_override, _dev_mode):
    """Dev mode + admin → wizard state wiped, response counts non-zero."""
    admin = _admin_override
    # Pre-load wizard state: every gate green + every step recorded so
    # the reset has something to actually delete.
    for step in _boot.REQUIRED_STEPS:
        await _boot.record_bootstrap_step(step, actor_user_id=admin.id)
    _boot.mark_smoke_passed(True)
    _boot.mark_cf_tunnel(configured=True)
    # Seed one enabled admin so flag_all_admins_must_change_password
    # has a real row to re-flag (the override admin user only exists in
    # the FastAPI dependency, not in the DB).
    seeded = await _au.create_user(
        "seeded@test.local", "Seeded", role="admin",
        password="initial-pw-strong-12345",
    )

    r = await _reset_http_client.post(
        "/api/v1/bootstrap/reset",
        json={"reason": "QA E2E rerun"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset"
    assert body["deploy_mode"] == "dev"
    # Four required steps were recorded, so ≥4 rows must have been
    # deleted (the conftest fixture may also have written rows during
    # the green-status pin — accept ≥4, not == 4).
    assert body["bootstrap_state_rows_deleted"] >= 4
    assert body["admins_reflagged"] >= 1
    assert body["marker_cleared"] is True
    assert body["actor_user_id"] == admin.id

    # Side effects observable in the DB.
    assert await _boot.list_bootstrap_steps() == []
    refreshed = await _au.get_user(seeded.id)
    assert refreshed is not None and refreshed.must_change_password is True


@pytest.mark.asyncio
async def test_reset_idempotent_on_already_clean_install(_reset_http_client, _admin_override, _dev_mode):
    """Replaying reset against a fresh install still 200s — counts → 0."""
    r = await _reset_http_client.post(
        "/api/v1/bootstrap/reset",
        json={},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset"
    assert body["bootstrap_state_rows_deleted"] == 0
    assert body["admins_reflagged"] == 0  # no admin rows seeded
    assert body["marker_cleared"] is True


@pytest.mark.asyncio
async def test_reset_writes_audit_row(_reset_http_client, _admin_override, _dev_mode):
    """``bootstrap.reset`` audit row captures actor + reason + counts."""
    admin = _admin_override
    await _au.create_user(
        "seeded@test.local", "Seeded", role="admin",
        password="initial-pw-strong-12345",
    )

    r = await _reset_http_client.post(
        "/api/v1/bootstrap/reset",
        json={"reason": "playwright suite"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text

    from backend import audit
    rows = await audit.query(entity_kind="bootstrap", limit=50)
    matching = [row for row in rows if row["action"] == "bootstrap.reset"]
    assert matching, f"no bootstrap.reset audit row in {[(r['action'], r.get('actor')) for r in rows]}"
    row = matching[0]
    assert row["actor"] == admin.email
    after = row.get("after") or {}
    assert after.get("reason") == "playwright suite"
    assert after.get("severity") == "warning"
    assert after.get("deploy_mode") == "dev"
    assert "seeded@test.local" in (after.get("admins_reflagged") or [])


@pytest.mark.asyncio
async def test_reset_403_when_not_dev_mode(_reset_http_client, _admin_override, monkeypatch):
    """Pinning OMNISIGHT_DEPLOY_MODE=systemd → 403, no mutation."""
    admin = _admin_override
    monkeypatch.setenv("OMNISIGHT_DEPLOY_MODE", "systemd")

    # Seed state we can verify is NOT touched.
    await _boot.record_bootstrap_step(_boot.STEP_ADMIN_PASSWORD, actor_user_id=admin.id)
    _boot.mark_smoke_passed(True)
    seeded = await _au.create_user(
        "seeded@test.local", "Seeded", role="admin",
        password="initial-pw-strong-12345",
    )

    r = await _reset_http_client.post(
        "/api/v1/bootstrap/reset",
        json={"reason": "should be denied"},
        follow_redirects=False,
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["deploy_mode"] == "systemd"
    assert "dev" in body["detail"].lower()

    # Pre-existing wizard state survives the refused call.
    rows = await _boot.list_bootstrap_steps()
    assert any(row["step"] == _boot.STEP_ADMIN_PASSWORD for row in rows)
    assert _boot._read_marker().get("smoke_passed") is True
    refreshed = await _au.get_user(seeded.id)
    assert refreshed is not None and refreshed.must_change_password is False


@pytest.mark.asyncio
async def test_reset_403_for_non_admin(_reset_http_client, _viewer_override, _dev_mode):
    """Viewer caller → 401/403, even in dev mode (auth gate fires first)."""
    r = await _reset_http_client.post(
        "/api/v1/bootstrap/reset",
        json={},
        follow_redirects=False,
    )
    assert r.status_code in (401, 403)
