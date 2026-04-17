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
async def _bootstrap_db(monkeypatch):
    """Fresh sqlite + isolated bootstrap marker path per test."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "bootstrap_state.db")
        marker = os.path.join(tmp, ".bootstrap_state.json")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", db_path)
        from backend import config as _cfg
        _cfg.settings.database_path = db_path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        from backend import bootstrap
        bootstrap._reset_for_tests(Path(marker))
        try:
            yield db, bootstrap
        finally:
            await db.close()
            bootstrap._reset_for_tests()


# ── schema ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_state_table_exists_with_expected_columns(_bootstrap_db):
    db, _ = _bootstrap_db
    conn = db._conn()
    async with conn.execute("PRAGMA table_info(bootstrap_state)") as cur:
        cols = {row[1]: row for row in await cur.fetchall()}
    assert set(cols.keys()) == {"step", "completed_at", "actor_user_id", "metadata"}
    # step is PRIMARY KEY (pk flag is column 5 of PRAGMA table_info)
    assert cols["step"][5] == 1
    # completed_at NOT NULL
    assert cols["completed_at"][3] == 1
    # metadata NOT NULL
    assert cols["metadata"][3] == 1
    # actor_user_id NULLABLE
    assert cols["actor_user_id"][3] == 0


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
