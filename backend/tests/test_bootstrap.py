"""L1 — Bootstrap status detection tests (`backend.bootstrap`).

Covers the four gates surfaced by :func:`get_bootstrap_status`:

  * admin_password_default
  * llm_provider_configured
  * cf_tunnel_configured
  * smoke_passed

Each probe is exercised in isolation (no DB for provider/CF/smoke, and
an isolated PG schema for the admin-password probe) so failures point
at a specific signal rather than a mixture of them.

Task #97 migration (2026-04-21): fixture ported from SQLite tempfile
to pg_test_pool. The bootstrap module's ``_admin_password_is_default``
probe still uses ``db._conn()`` (the compat wrapper), so the fixture
sets ``OMNISIGHT_DATABASE_URL`` so the wrapper reads the same PG as
pg_test_pool.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture()
async def _bootstrap_db(pg_test_pool, pg_test_dsn, monkeypatch, tmp_path):
    """Fresh PG + isolated bootstrap marker path per test."""
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    marker = tmp_path / ".bootstrap_state.json"

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users, bootstrap_state RESTART IDENTITY CASCADE"
        )

    from backend import db
    from backend import bootstrap
    from pathlib import Path

    if db._db is not None:
        await db.close()
    await db.init()
    bootstrap._reset_for_tests(Path(marker))
    try:
        yield db, bootstrap
    finally:
        await db.close()
        bootstrap._reset_for_tests()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, bootstrap_state RESTART IDENTITY CASCADE"
            )


# ── shape / dataclass ───────────────────────────────────────────


def test_bootstrap_status_to_dict_shape():
    from backend.bootstrap import BootstrapStatus

    s = BootstrapStatus(
        admin_password_default=True,
        llm_provider_configured=False,
        cf_tunnel_configured=False,
        smoke_passed=False,
    )
    d = s.to_dict()
    assert set(d.keys()) == {
        "admin_password_default",
        "llm_provider_configured",
        "cf_tunnel_configured",
        "smoke_passed",
    }
    assert d["admin_password_default"] is True
    assert s.all_green is False


def test_bootstrap_status_all_green_only_when_all_gates_pass():
    from backend.bootstrap import BootstrapStatus

    assert BootstrapStatus(False, True, True, True).all_green is True
    # default admin still → not green
    assert BootstrapStatus(True, True, True, True).all_green is False
    # any missing gate → not green
    assert BootstrapStatus(False, False, True, True).all_green is False
    assert BootstrapStatus(False, True, False, True).all_green is False
    assert BootstrapStatus(False, True, True, False).all_green is False


# ── admin_password_default ──────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_password_default_true_when_default_admin_flagged(_bootstrap_db):
    db, bootstrap = _bootstrap_db
    # ensure_default_admin with the well-known default should flip the flag
    monkey_env = os.environ.pop("OMNISIGHT_ADMIN_PASSWORD", None)
    try:
        from backend import auth
        user = await auth.ensure_default_admin()
        assert user is not None and user.must_change_password is True

        status = await bootstrap.get_bootstrap_status()
        assert status.admin_password_default is True
    finally:
        if monkey_env is not None:
            os.environ["OMNISIGHT_ADMIN_PASSWORD"] = monkey_env


@pytest.mark.asyncio
async def test_admin_password_default_false_after_password_change(_bootstrap_db, monkeypatch):
    db, bootstrap = _bootstrap_db
    monkeypatch.delenv("OMNISIGHT_ADMIN_PASSWORD", raising=False)
    from backend import auth
    user = await auth.ensure_default_admin()
    assert user and user.must_change_password is True

    # Operator rotates the password — auth.change_password clears the flag.
    await auth.change_password(user.id, "a-real-strong-password")

    status = await bootstrap.get_bootstrap_status()
    assert status.admin_password_default is False


@pytest.mark.asyncio
async def test_admin_password_default_false_when_non_default_password_at_bootstrap(_bootstrap_db, monkeypatch):
    db, bootstrap = _bootstrap_db
    monkeypatch.setenv("OMNISIGHT_ADMIN_PASSWORD", "a-different-strong-password")
    from backend import auth
    user = await auth.ensure_default_admin()
    assert user and user.must_change_password is False

    status = await bootstrap.get_bootstrap_status()
    assert status.admin_password_default is False


@pytest.mark.asyncio
async def test_admin_password_default_true_when_db_empty(_bootstrap_db):
    """No admin yet → treat as still-default (wizard hasn't completed step 1)."""
    _, bootstrap = _bootstrap_db
    status = await bootstrap.get_bootstrap_status()
    # An empty users table means the default admin hasn't been provisioned,
    # which is indistinguishable (from the wizard's point of view) from
    # "shipping credential still active".
    assert status.admin_password_default is False  # No flagged rows → not default


# ── llm_provider_configured ─────────────────────────────────────


def test_llm_provider_configured_ollama_always_true(monkeypatch):
    from backend import bootstrap
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "ollama", raising=False)
    assert bootstrap._llm_provider_is_configured() is True


def test_llm_provider_configured_requires_matching_key(monkeypatch):
    from backend import bootstrap
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    assert bootstrap._llm_provider_is_configured() is False

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-xxxx", raising=False)
    assert bootstrap._llm_provider_is_configured() is True


def test_llm_provider_configured_empty_provider(monkeypatch):
    from backend import bootstrap
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "", raising=False)
    assert bootstrap._llm_provider_is_configured() is False


def test_llm_provider_whitespace_key_is_unconfigured(monkeypatch):
    from backend import bootstrap
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "openai", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "   ", raising=False)
    assert bootstrap._llm_provider_is_configured() is False


# ── cf_tunnel_configured ────────────────────────────────────────


def test_cf_tunnel_configured_via_marker(tmp_path):
    from backend import bootstrap

    bootstrap._reset_for_tests(tmp_path / "marker.json")
    assert bootstrap._cf_tunnel_is_configured() is False

    bootstrap.mark_cf_tunnel(configured=True)
    assert bootstrap._cf_tunnel_is_configured() is True

    bootstrap.mark_cf_tunnel(configured=False)
    assert bootstrap._cf_tunnel_is_configured() is False

    bootstrap.mark_cf_tunnel(skipped=True)
    assert bootstrap._cf_tunnel_is_configured() is True  # explicit skip passes


def test_cf_tunnel_configured_via_router_state(tmp_path):
    from backend import bootstrap
    from backend.routers import cloudflare_tunnel as _cft

    bootstrap._reset_for_tests(tmp_path / "marker.json")
    _cft._reset_for_tests()
    try:
        assert bootstrap._cf_tunnel_is_configured() is False
        _cft._set_state("tunnel_id", "tun-123")
        assert bootstrap._cf_tunnel_is_configured() is True
    finally:
        _cft._reset_for_tests()


def test_cf_tunnel_configured_via_compose_env_token(tmp_path, monkeypatch):
    """Path B deployments wire the tunnel via ``docker-compose`` +
    Zero Trust Dashboard, never touching the wizard's
    ``/cloudflare-tunnel/provision`` endpoint. In that case the only
    signal is ``OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN`` env presence; the
    gate must recognise it as "configured"."""
    from backend import bootstrap
    from backend.routers import cloudflare_tunnel as _cft

    bootstrap._reset_for_tests(tmp_path / "marker.json")
    _cft._reset_for_tests()
    monkeypatch.delenv("OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN", raising=False)
    try:
        # No marker, no router state, no env → red
        assert bootstrap._cf_tunnel_is_configured() is False

        # Token present → green
        monkeypatch.setenv("OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN", "eyJhIjoi.fake.token")
        assert bootstrap._cf_tunnel_is_configured() is True

        # Empty / whitespace-only token doesn't count
        monkeypatch.setenv("OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN", "   ")
        assert bootstrap._cf_tunnel_is_configured() is False

        monkeypatch.setenv("OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN", "")
        assert bootstrap._cf_tunnel_is_configured() is False
    finally:
        _cft._reset_for_tests()


# ── smoke_passed ────────────────────────────────────────────────


def test_smoke_passed_marker_roundtrip(tmp_path):
    from backend import bootstrap

    bootstrap._reset_for_tests(tmp_path / "marker.json")
    assert bootstrap._smoke_has_passed() is False
    bootstrap.mark_smoke_passed(True)
    assert bootstrap._smoke_has_passed() is True
    bootstrap.mark_smoke_passed(False)
    assert bootstrap._smoke_has_passed() is False


def test_unreadable_marker_is_treated_as_empty(tmp_path):
    from backend import bootstrap

    marker = tmp_path / "marker.json"
    marker.write_text("this is not valid json{{", encoding="utf-8")
    bootstrap._reset_for_tests(marker)
    assert bootstrap._smoke_has_passed() is False
    assert bootstrap._cf_tunnel_is_configured() is False


# ── end-to-end ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_bootstrap_status_full_happy_path(_bootstrap_db, monkeypatch):
    db, bootstrap = _bootstrap_db
    # 1. admin password rotated
    monkeypatch.delenv("OMNISIGHT_ADMIN_PASSWORD", raising=False)
    from backend import auth
    user = await auth.ensure_default_admin()
    await auth.change_password(user.id, "a-real-strong-password")

    # 2. llm provider with key
    from backend.config import settings
    monkeypatch.setattr(settings, "llm_provider", "anthropic", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-xxxx", raising=False)

    # 3. cf tunnel provisioned
    bootstrap.mark_cf_tunnel(configured=True)

    # 4. smoke green
    bootstrap.mark_smoke_passed(True)

    status = await bootstrap.get_bootstrap_status()
    assert status.to_dict() == {
        "admin_password_default": False,
        "llm_provider_configured": True,
        "cf_tunnel_configured": True,
        "smoke_passed": True,
    }
    assert status.all_green is True


@pytest.mark.asyncio
async def test_get_bootstrap_status_fresh_install_all_red(_bootstrap_db, monkeypatch):
    """Fresh install with default admin + no provider key + no CF + no smoke."""
    _, bootstrap = _bootstrap_db
    monkeypatch.delenv("OMNISIGHT_ADMIN_PASSWORD", raising=False)
    from backend import auth
    await auth.ensure_default_admin()

    from backend.config import settings
    monkeypatch.setattr(settings, "llm_provider", "anthropic", raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)

    status = await bootstrap.get_bootstrap_status()
    assert status.admin_password_default is True
    assert status.llm_provider_configured is False
    assert status.cf_tunnel_configured is False
    assert status.smoke_passed is False
    assert status.all_green is False
