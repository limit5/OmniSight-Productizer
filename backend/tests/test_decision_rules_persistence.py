"""A1 — decision rules survive restart via PG persistence.

Phase-3 Step C.1 (2026-04-21): ported off the SQLite-file
``tmp_db`` fixture + ``db._conn()`` compat wrapper onto
``pg_test_pool`` + direct pool acquire. The conftest's
``pg_test_conn`` auto-truncates ``decision_rules`` inside the
test transaction so rules don't leak between tests.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_replace_and_load(pg_test_pool):
    from backend import decision_rules as dr
    dr.clear()

    dr.replace_rules([
        {"kind_pattern": "stuck/*", "severity": "risky",
         "auto_in_modes": ["full_auto"], "priority": 10, "note": "keep"},
    ])
    # Give the fire-and-forget persist task a tick to flush.
    import asyncio
    await asyncio.sleep(0.05)

    dr.clear()
    assert dr.list_rules() == []

    loaded = await dr.load_from_db()
    assert loaded == 1
    rules = dr.list_rules()
    assert len(rules) == 1
    assert rules[0]["kind_pattern"] == "stuck/*"
    assert rules[0]["severity"] == "risky"
    assert rules[0]["auto_in_modes"] == ["full_auto"]


@pytest.mark.asyncio
async def test_replace_rejects_duplicate_ids(pg_test_pool):
    from backend import decision_rules as dr
    dr.clear()
    with pytest.raises(ValueError, match="duplicate rule id"):
        dr.replace_rules([
            {"id": "r1", "kind_pattern": "a/*"},
            {"id": "r1", "kind_pattern": "b/*"},
        ])


@pytest.mark.asyncio
async def test_load_skips_malformed_rows(pg_test_pool):
    """Legacy/partial rows must not poison the engine."""
    from backend import decision_rules as dr
    dr.clear()
    # ``pg_test_pool`` doesn't auto-truncate (only ``pg_test_conn``
    # does), so wipe any prior-test rows first — we need the
    # loaded-count assertion to measure just the bogus row.
    async with pg_test_pool.acquire() as conn:
        await conn.execute("DELETE FROM decision_rules")
        # Write a bogus row directly — empty ``kind_pattern`` fails
        # the normaliser in ``load_from_db`` so the row gets dropped.
        await conn.execute(
            "INSERT INTO decision_rules (id, kind_pattern, severity, "
            "auto_in_modes, default_option_id, priority, enabled, note) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            "bad", "", None, "[]", None, 100, 1, "",
        )
    loaded = await dr.load_from_db()
    assert loaded == 0
