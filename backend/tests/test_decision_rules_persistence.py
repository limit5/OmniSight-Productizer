"""A1 — decision rules survive restart via SQLite persistence."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
async def tmp_db(monkeypatch):
    """Fresh SQLite file per test so persisted rules don't leak."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rules.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        # Force the resolver to re-read the env var.
        from backend import config as _cfg
        _cfg.settings.database_path = path

        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


@pytest.mark.asyncio
async def test_replace_and_load(tmp_db):
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
async def test_replace_rejects_duplicate_ids(tmp_db):
    from backend import decision_rules as dr
    dr.clear()
    with pytest.raises(ValueError, match="duplicate rule id"):
        dr.replace_rules([
            {"id": "r1", "kind_pattern": "a/*"},
            {"id": "r1", "kind_pattern": "b/*"},
        ])


@pytest.mark.asyncio
async def test_load_skips_malformed_rows(tmp_db):
    """Legacy/partial rows must not poison the engine."""
    from backend import db, decision_rules as dr
    dr.clear()
    # Write a bogus row directly.
    await db._conn().execute(
        "INSERT INTO decision_rules (id, kind_pattern, severity, auto_in_modes, "
        "default_option_id, priority, enabled, note) VALUES (?,?,?,?,?,?,?,?)",
        ("bad", "", None, "[]", None, 100, 1, ""),  # empty kind_pattern fails normalise
    )
    await db._conn().commit()
    loaded = await dr.load_from_db()
    assert loaded == 0
