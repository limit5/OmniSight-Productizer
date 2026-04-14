"""Phase 56-DAG-C S3 — prompt_registry.bootstrap_from_disk."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from backend import prompt_registry as pr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as cfg
        cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


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
