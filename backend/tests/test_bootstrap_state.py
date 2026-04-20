"""L1 — ``bootstrap_state`` table + ``bootstrap_finalized`` persistence.

Covers the third L1 checkbox: per-step audit rows and the
``bootstrap_finalized=true`` app-setting anchor that lets
:func:`is_bootstrap_finalized` stay sticky-green across process restarts.

The fixture spins up a fresh sqlite per test and isolates the bootstrap
marker file so ``_read_marker`` / ``_write_marker`` never touch the
shipping ``data/.bootstrap_state.json``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
async def _bootstrap_db(pg_test_pool, pg_test_dsn, monkeypatch):
    """pg_test_pool-backed + isolated bootstrap marker path per test.

    SP-5.5 migration (2026-04-21): fresh sqlite tempfile → PG pool.
    bootstrap.py's 6 DB-touching functions are now pool-native; the
    other bootstrap helpers that still read from ``db._conn()``
    (``list_bootstrap_steps`` back-compat paths, etc.) fall through
    the compat wrapper to the same PG via ``OMNISIGHT_DATABASE_URL``.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    # Clear llm_provider + CF env so _llm_provider_is_configured() /
    # _cf_tunnel_is_configured() don't auto-backfill their respective
    # bootstrap steps via environment defaults. Tests in this module
    # assume a genuinely fresh install from the DB's perspective.
    from backend.config import settings as _settings
    monkeypatch.setattr(_settings, "llm_provider", "")
    monkeypatch.delenv("OMNISIGHT_CLOUDFLARE_TUNNEL_ID", raising=False)
    monkeypatch.delenv("OMNISIGHT_CF_TUNNEL_SKIP", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        marker = os.path.join(tmp, ".bootstrap_state.json")
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, bootstrap_state "
                "RESTART IDENTITY CASCADE"
            )
        from backend import db
        if db._db is not None:
            await db.close()
        await db.init()
        from backend import bootstrap
        bootstrap._reset_for_tests(Path(marker))
        try:
            yield db, bootstrap
        finally:
            await db.close()
            bootstrap._reset_for_tests()
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE users, bootstrap_state "
                    "RESTART IDENTITY CASCADE"
                )


# ── schema ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_state_table_exists_with_expected_columns(_bootstrap_db):
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'bootstrap_state'"
        )
        pk_rows = await conn.fetch(
            "SELECT a.attname AS column_name "
            "FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attnum = ANY(c.conkey) "
            "AND a.attrelid = c.conrelid "
            "WHERE c.contype = 'p' "
            "AND c.conrelid = 'public.bootstrap_state'::regclass"
        )
    cols = {r["column_name"]: r["is_nullable"] for r in rows}
    assert set(cols.keys()) == {"step", "completed_at", "actor_user_id", "metadata"}
    # PRIMARY KEY(step)
    assert {r["column_name"] for r in pk_rows} == {"step"}
    # completed_at NOT NULL
    assert cols["completed_at"] == "NO"
    # metadata NOT NULL
    assert cols["metadata"] == "NO"
    # actor_user_id NULLABLE
    assert cols["actor_user_id"] == "YES"


# ── record_bootstrap_step ──────────────────────────────────────


@pytest.mark.asyncio
async def test_record_and_get_bootstrap_step_roundtrip(_bootstrap_db):
    _, bootstrap = _bootstrap_db
    await bootstrap.record_bootstrap_step(
        bootstrap.STEP_ADMIN_PASSWORD,
        actor_user_id="u-admin",
        metadata={"source": "wizard"},
    )
    row = await bootstrap.get_bootstrap_step(bootstrap.STEP_ADMIN_PASSWORD)
    assert row is not None
    assert row["step"] == bootstrap.STEP_ADMIN_PASSWORD
    assert row["actor_user_id"] == "u-admin"
    assert row["metadata"] == {"source": "wizard"}
    assert row["completed_at"]  # populated by datetime('now')


@pytest.mark.asyncio
async def test_record_bootstrap_step_is_idempotent_upsert(_bootstrap_db):
    _, bootstrap = _bootstrap_db
    await bootstrap.record_bootstrap_step(
        bootstrap.STEP_LLM_PROVIDER,
        actor_user_id="u1",
        metadata={"provider": "anthropic"},
    )
    await bootstrap.record_bootstrap_step(
        bootstrap.STEP_LLM_PROVIDER,
        actor_user_id="u2",
        metadata={"provider": "ollama"},
    )
    steps = await bootstrap.list_bootstrap_steps()
    llm_rows = [s for s in steps if s["step"] == bootstrap.STEP_LLM_PROVIDER]
    assert len(llm_rows) == 1, "upsert must not stack duplicate rows per step"
    assert llm_rows[0]["actor_user_id"] == "u2"
    assert llm_rows[0]["metadata"] == {"provider": "ollama"}


@pytest.mark.asyncio
async def test_record_bootstrap_step_rejects_empty_name(_bootstrap_db):
    _, bootstrap = _bootstrap_db
    with pytest.raises(ValueError):
        await bootstrap.record_bootstrap_step("")


@pytest.mark.asyncio
async def test_record_bootstrap_step_handles_non_serialisable_metadata(_bootstrap_db):
    _, bootstrap = _bootstrap_db

    class _NotJSON:
        pass

    # Should not raise — serialiser falls back to '{}' with a warning
    await bootstrap.record_bootstrap_step(
        bootstrap.STEP_CF_TUNNEL,
        metadata={"garbage": _NotJSON()},
    )
    row = await bootstrap.get_bootstrap_step(bootstrap.STEP_CF_TUNNEL)
    assert row is not None
    # Either {} (true non-serialisable) or serialised via default=str
    assert isinstance(row["metadata"], dict)


@pytest.mark.asyncio
async def test_get_bootstrap_step_missing_returns_none(_bootstrap_db):
    _, bootstrap = _bootstrap_db
    assert await bootstrap.get_bootstrap_step("nonexistent") is None


@pytest.mark.asyncio
async def test_list_bootstrap_steps_ordering(_bootstrap_db):
    _, bootstrap = _bootstrap_db
    for step in (
        bootstrap.STEP_ADMIN_PASSWORD,
        bootstrap.STEP_LLM_PROVIDER,
        bootstrap.STEP_CF_TUNNEL,
        bootstrap.STEP_SMOKE,
    ):
        await bootstrap.record_bootstrap_step(step, actor_user_id="u1")
    steps = await bootstrap.list_bootstrap_steps()
    assert len(steps) == 4
    # completed_at is second-granularity so we can't rely on strict
    # timestamp order; just assert all required steps landed.
    assert {s["step"] for s in steps} >= set(bootstrap.REQUIRED_STEPS)


# ── missing_required_steps ─────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_required_steps_fresh_install(_bootstrap_db):
    _, bootstrap = _bootstrap_db
    missing = await bootstrap.missing_required_steps()
    assert set(missing) == set(bootstrap.REQUIRED_STEPS)


@pytest.mark.asyncio
async def test_missing_required_steps_partial_progress(_bootstrap_db):
    _, bootstrap = _bootstrap_db
    await bootstrap.record_bootstrap_step(bootstrap.STEP_ADMIN_PASSWORD)
    await bootstrap.record_bootstrap_step(bootstrap.STEP_LLM_PROVIDER)
    missing = await bootstrap.missing_required_steps()
    assert bootstrap.STEP_ADMIN_PASSWORD not in missing
    assert bootstrap.STEP_LLM_PROVIDER not in missing
    assert bootstrap.STEP_CF_TUNNEL in missing
    assert bootstrap.STEP_SMOKE in missing


@pytest.mark.asyncio
async def test_missing_required_steps_all_recorded(_bootstrap_db):
    _, bootstrap = _bootstrap_db
    for step in bootstrap.REQUIRED_STEPS:
        await bootstrap.record_bootstrap_step(step)
    assert await bootstrap.missing_required_steps() == []


@pytest.mark.asyncio
async def test_missing_required_steps_autobackfills_cf_tunnel(
    _bootstrap_db, monkeypatch,
):
    """CF tunnel configured via compose env (no wizard provision)
    → the step should auto-backfill with the corresponding source
    marker rather than remain "missing"."""
    _, bootstrap = _bootstrap_db
    monkeypatch.setenv("OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN", "eyJhIjoi.compose.token")

    assert bootstrap._cf_tunnel_is_configured() is True

    missing = await bootstrap.missing_required_steps()
    assert bootstrap.STEP_CF_TUNNEL not in missing

    row = await bootstrap.get_bootstrap_step(bootstrap.STEP_CF_TUNNEL)
    assert row is not None
    assert row["metadata"].get("source") == "auto_backfill_cf_tunnel"

    # Idempotent on re-poll.
    missing2 = await bootstrap.missing_required_steps()
    assert bootstrap.STEP_CF_TUNNEL not in missing2


@pytest.mark.asyncio
async def test_missing_required_steps_autobackfills_llm_provider(
    _bootstrap_db, monkeypatch,
):
    """LLM provider configured via ``OMNISIGHT_ANTHROPIC_API_KEY`` +
    provider selection in settings (the Path B baseline) → auto-
    backfill STEP_LLM_PROVIDER so finalize can proceed even though
    the wizard's provision handler was never called."""
    _, bootstrap = _bootstrap_db
    from backend import config as _cfg

    _cfg.settings.llm_provider = "anthropic"
    monkeypatch.setattr(_cfg.settings, "anthropic_api_key", "sk-ant-api03-fake")

    assert bootstrap._llm_provider_is_configured() is True

    missing = await bootstrap.missing_required_steps()
    assert bootstrap.STEP_LLM_PROVIDER not in missing

    row = await bootstrap.get_bootstrap_step(bootstrap.STEP_LLM_PROVIDER)
    assert row is not None
    assert row["metadata"].get("source") == "auto_backfill_llm_env"


@pytest.mark.asyncio
async def test_missing_required_steps_autobackfills_admin_password_rotated(
    _bootstrap_db,
):
    """Admin rotated via a non-wizard path (K6 bootstrap admin via
    ``OMNISIGHT_ADMIN_PASSWORD`` env, or a CLI reset) →
    auto-backfill STEP_ADMIN_PASSWORD. Evidence is a users-table
    row with ``must_change_password=0`` so a genuinely fresh
    install (no users at all) does NOT trigger the backfill."""
    _, bootstrap = _bootstrap_db
    # Insert a rotated admin row directly (bypasses the wizard handler
    # that would have written STEP_ADMIN_PASSWORD itself).
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, enabled, "
            "must_change_password, password_hash, created_at, tenant_id) "
            "VALUES ('u-1', 'a@b', 'A', 'admin', 1, 0, 'hash', "
            "'2024-01-01 00:00:00', 't-default')"
        )

    assert await bootstrap._admin_rotated_evidence() is True

    missing = await bootstrap.missing_required_steps()
    assert bootstrap.STEP_ADMIN_PASSWORD not in missing

    row = await bootstrap.get_bootstrap_step(bootstrap.STEP_ADMIN_PASSWORD)
    assert row is not None
    assert row["metadata"].get("source") == "auto_backfill_admin_rotated"


@pytest.mark.asyncio
async def test_missing_required_steps_admin_no_backfill_without_evidence(
    _bootstrap_db,
):
    """Guard rail: on a users-table that contains ONLY must_change_
    password=1 admins (fresh install with a default admin seeded but
    not yet rotated), the admin-rotated-evidence probe must return
    False, and the backfill must NOT fire. Otherwise we'd silently
    sign off on an un-rotated default admin."""
    _, bootstrap = _bootstrap_db
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, enabled, "
            "must_change_password, password_hash, created_at, tenant_id) "
            "VALUES ('u-0', 'default@admin', 'Default', 'admin', 1, 1, "
            "'hash', '2024-01-01 00:00:00', 't-default')"
        )

    assert await bootstrap._admin_password_is_default() is True
    assert await bootstrap._admin_rotated_evidence() is False

    missing = await bootstrap.missing_required_steps()
    assert bootstrap.STEP_ADMIN_PASSWORD in missing

    # No row written.
    assert await bootstrap.get_bootstrap_step(bootstrap.STEP_ADMIN_PASSWORD) is None


@pytest.mark.asyncio
async def test_missing_required_steps_autobackfills_smoke_marker(_bootstrap_db):
    """If the smoke-passed marker is set but the step row somehow
    didn't land (rare — maybe record_bootstrap_step raised right
    after mark_smoke_passed wrote the marker), auto-backfill brings
    the two views in sync."""
    _, bootstrap = _bootstrap_db
    bootstrap.mark_smoke_passed(True)
    assert bootstrap._smoke_has_passed() is True

    missing = await bootstrap.missing_required_steps()
    assert bootstrap.STEP_SMOKE not in missing

    row = await bootstrap.get_bootstrap_step(bootstrap.STEP_SMOKE)
    assert row is not None
    assert row["metadata"].get("source") == "auto_backfill_smoke_marker"


# ── mark_bootstrap_finalized ───────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_requires_all_gates_green(_bootstrap_db, monkeypatch):
    _, bootstrap = _bootstrap_db

    async def _red():
        return bootstrap.BootstrapStatus(True, False, False, False)

    monkeypatch.setattr(bootstrap, "get_bootstrap_status", _red)
    for step in bootstrap.REQUIRED_STEPS:
        await bootstrap.record_bootstrap_step(step)
    with pytest.raises(RuntimeError, match="bootstrap not green"):
        await bootstrap.mark_bootstrap_finalized()
    assert bootstrap.is_bootstrap_finalized_flag() is False


@pytest.mark.asyncio
async def test_finalize_requires_all_required_steps_recorded(_bootstrap_db, monkeypatch):
    _, bootstrap = _bootstrap_db

    async def _green():
        return bootstrap.BootstrapStatus(False, True, True, True)

    monkeypatch.setattr(bootstrap, "get_bootstrap_status", _green)
    # Only one required step recorded — finalize must refuse.
    await bootstrap.record_bootstrap_step(bootstrap.STEP_ADMIN_PASSWORD)
    with pytest.raises(RuntimeError, match="missing required steps"):
        await bootstrap.mark_bootstrap_finalized()
    assert bootstrap.is_bootstrap_finalized_flag() is False


@pytest.mark.asyncio
async def test_finalize_happy_path_writes_flag_and_row(_bootstrap_db, monkeypatch):
    _, bootstrap = _bootstrap_db

    async def _green():
        return bootstrap.BootstrapStatus(False, True, True, True)

    monkeypatch.setattr(bootstrap, "get_bootstrap_status", _green)
    for step in bootstrap.REQUIRED_STEPS:
        await bootstrap.record_bootstrap_step(step, actor_user_id="u-admin")

    status = await bootstrap.mark_bootstrap_finalized(
        actor_user_id="u-admin",
        metadata={"reason": "wizard complete"},
    )
    assert status.all_green is True
    # Persisted flag written
    assert bootstrap.is_bootstrap_finalized_flag() is True
    # finalized row landed with the actor + metadata
    fin = await bootstrap.get_bootstrap_step(bootstrap.STEP_FINALIZED)
    assert fin is not None
    assert fin["actor_user_id"] == "u-admin"
    assert fin["metadata"] == {"reason": "wizard complete"}
    # Gate cache flipped sticky-green
    assert await bootstrap.is_bootstrap_finalized() is True


# ── is_bootstrap_finalized picks up persisted flag ──────────────


@pytest.mark.asyncio
async def test_is_bootstrap_finalized_honours_persisted_flag(_bootstrap_db, monkeypatch):
    """Simulates a process restart: flag was set by a prior run, current
    live status is still red (e.g. smoke marker got wiped) but the gate
    must stay green because the wizard already finalized."""
    _, bootstrap = _bootstrap_db

    async def _red():
        return bootstrap.BootstrapStatus(True, False, False, False)

    monkeypatch.setattr(bootstrap, "get_bootstrap_status", _red)
    bootstrap._gate_cache_reset()
    # Before the flag is set: red.
    assert await bootstrap.is_bootstrap_finalized() is False
    # Flip just the persisted flag (no row writes, no live gates).
    data = bootstrap._read_marker()
    data["bootstrap_finalized"] = True
    bootstrap._write_marker(data)
    bootstrap._gate_cache_reset()
    # Now even though the live gates are all red the middleware
    # must treat the app as finalized.
    assert await bootstrap.is_bootstrap_finalized() is True
