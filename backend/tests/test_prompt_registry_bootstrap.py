"""Phase 56-DAG-C S3 — prompt_registry.bootstrap_from_disk.

Task #105 migration (2026-04-21): ``fresh_db`` fixture ported from
SQLite tempfile to pg_test_pool. register_active / register_canary
are now pool-native and require the asyncpg pool to be initialised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import prompt_registry as pr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def fresh_db(pg_test_pool, pg_test_dsn, monkeypatch):
    # get_active / get_by_id still use db._conn() (compat wrapper).
    # Point OMNISIGHT_DATABASE_URL at the same PG the pool uses so
    # reads via compat hit the same data writes via pool leave behind.
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE prompt_versions RESTART IDENTITY CASCADE"
        )
    from backend import db
    if db._db is not None:
        await db.close()
    await db.init()
    try:
        yield db
    finally:
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE prompt_versions RESTART IDENTITY CASCADE"
            )


def _make_prompt_dir(tmp_path: Path, files: dict[str, str]) -> list[Path]:
    d = tmp_path / "backend" / "agents" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, body in files.items():
        p = d / name
        p.write_text(body, encoding="utf-8")
        paths.append(p)
    return sorted(paths)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_bootstrap_registers_fresh_prompt(fresh_db, tmp_path, monkeypatch):
    paths = _make_prompt_dir(tmp_path, {"orchestrator.md": "# v1 body"})
    monkeypatch.setattr(pr, "PROMPTS_ROOT", paths[0].parent)
    monkeypatch.setattr(pr, "_PROJECT_ROOT", tmp_path)

    out = await pr.bootstrap_from_disk(paths=paths)
    assert out == [("backend/agents/prompts/orchestrator.md", "registered")]

    active = await pr.get_active("backend/agents/prompts/orchestrator.md")
    assert active is not None
    assert active.body == "# v1 body"
    assert active.version == 1


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent(fresh_db, tmp_path, monkeypatch):
    paths = _make_prompt_dir(tmp_path, {"orchestrator.md": "# same body"})
    monkeypatch.setattr(pr, "PROMPTS_ROOT", paths[0].parent)
    monkeypatch.setattr(pr, "_PROJECT_ROOT", tmp_path)

    first = await pr.bootstrap_from_disk(paths=paths)
    assert first[0][1] == "registered"
    second = await pr.bootstrap_from_disk(paths=paths)
    assert second[0][1] == "unchanged"


@pytest.mark.asyncio
async def test_bootstrap_registers_new_version_when_body_changes(
    fresh_db, tmp_path, monkeypatch,
):
    paths = _make_prompt_dir(tmp_path, {"orchestrator.md": "# v1"})
    monkeypatch.setattr(pr, "PROMPTS_ROOT", paths[0].parent)
    monkeypatch.setattr(pr, "_PROJECT_ROOT", tmp_path)

    await pr.bootstrap_from_disk(paths=paths)
    # Edit the file — next bootstrap should re-register.
    paths[0].write_text("# v2 body edited", encoding="utf-8")
    out = await pr.bootstrap_from_disk(paths=paths)
    assert out[0][1] == "registered"

    active = await pr.get_active("backend/agents/prompts/orchestrator.md")
    assert active.version == 2
    assert active.body == "# v2 body edited"


@pytest.mark.asyncio
async def test_bootstrap_skips_paths_outside_root(fresh_db, tmp_path, monkeypatch):
    # Create a prompt-looking file OUTSIDE the prompts root.
    good_dir = (tmp_path / "backend" / "agents" / "prompts")
    good_dir.mkdir(parents=True)
    good = good_dir / "real.md"
    good.write_text("# real", encoding="utf-8")
    outside = tmp_path / "imposter.md"
    outside.write_text("# sneaky", encoding="utf-8")

    monkeypatch.setattr(pr, "PROMPTS_ROOT", good_dir)
    monkeypatch.setattr(pr, "_PROJECT_ROOT", tmp_path)

    out = await pr.bootstrap_from_disk(paths=[good, outside])
    registered = [p for p, a in out if a == "registered"]
    assert len(registered) == 1
    assert registered[0].endswith("real.md")


@pytest.mark.asyncio
async def test_bootstrap_skips_claude_md(fresh_db, tmp_path, monkeypatch):
    """CLAUDE.md is L1-immutable; even if someone drops it into the
    prompts dir, bootstrap must refuse."""
    good_dir = (tmp_path / "backend" / "agents" / "prompts")
    good_dir.mkdir(parents=True)
    claude = good_dir / "CLAUDE.md"
    claude.write_text("# rules", encoding="utf-8")

    monkeypatch.setattr(pr, "PROMPTS_ROOT", good_dir)
    monkeypatch.setattr(pr, "_PROJECT_ROOT", tmp_path)

    out = await pr.bootstrap_from_disk(paths=[claude])
    # Skipped entirely — not even an "unchanged" entry.
    assert out == []


@pytest.mark.asyncio
async def test_bootstrap_survives_unreadable_file(fresh_db, tmp_path, monkeypatch):
    good_dir = (tmp_path / "backend" / "agents" / "prompts")
    good_dir.mkdir(parents=True)
    good = good_dir / "ok.md"
    good.write_text("# ok", encoding="utf-8")
    ghost = good_dir / "ghost.md"  # never actually created

    monkeypatch.setattr(pr, "PROMPTS_ROOT", good_dir)
    monkeypatch.setattr(pr, "_PROJECT_ROOT", tmp_path)

    out = await pr.bootstrap_from_disk(paths=[good, ghost])
    # ok.md registered; ghost.md silently skipped.
    assert ("backend/agents/prompts/ok.md", "registered") in out
    assert not any(p.endswith("ghost.md") for p, _ in out)


@pytest.mark.asyncio
async def test_bootstrap_registers_the_shipped_orchestrator_prompt(fresh_db):
    """End-to-end smoke: the REAL orchestrator.md that ships in the
    repo must round-trip through bootstrap without errors."""
    out = await pr.bootstrap_from_disk()  # no paths arg → scans real root
    # The orchestrator prompt is expected to be present.
    paths = [p for p, _ in out]
    assert any(p.endswith("orchestrator.md") for p in paths), paths


# ── Task #105: concurrent-race regression guard ──────────────────


@pytest.mark.asyncio
async def test_concurrent_bootstrap_same_path_no_unique_violation(
    fresh_db, tmp_path, monkeypatch,
):
    """Load-bearing regression guard for task #105.

    Smoke-surfaced bug: two uvicorn workers running bootstrap_from_disk
    concurrently on the same empty DB both saw the table empty, both
    computed ``next_v=1``, both INSERTed, one hit UNIQUE(path, version),
    the failing tx poisoned the shared compat connection, and every
    subsequent caller (including /readyz's db_ping) got
    ``current transaction is aborted``.

    The advisory lock in _register_active_impl makes this impossible —
    this test proves it at asyncio scale; the multi-worker subprocess
    harness in task #82 would catch it at OS-process scale too.

    Without the lock, this test fails with a UNIQUE violation (and
    with the prior compat pattern, it ALSO corrupted the conn and
    broke unrelated callers). Confirms both fixes hold.
    """
    import asyncio
    paths = _make_prompt_dir(tmp_path, {"orchestrator.md": "# same body"})
    monkeypatch.setattr(pr, "PROMPTS_ROOT", paths[0].parent)
    monkeypatch.setattr(pr, "_PROJECT_ROOT", tmp_path)

    # 6 concurrent bootstrap calls on the same empty DB.
    results = await asyncio.gather(
        *(pr.bootstrap_from_disk(paths=paths) for _ in range(6)),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, (
        f"concurrent bootstrap raised: {errors[0]!r}. "
        f"Advisory lock should serialise same-path writes."
    )
    # Exactly one row in prompt_versions, version=1, role='active'.
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT version, role FROM prompt_versions "
            "WHERE path = 'backend/agents/prompts/orchestrator.md' "
            "ORDER BY version",
        )
    assert len(rows) == 1, (
        f"concurrent idempotent registration must collapse to 1 row, "
        f"got {len(rows)}: {[dict(r) for r in rows]}"
    )
    assert rows[0]["version"] == 1
    assert rows[0]["role"] == "active"
