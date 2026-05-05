"""Phase 62 S3 — workflow finish hook + promotion endpoints."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hook: workflow.finish triggers extractor when L1 enabled
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def workflow_db(monkeypatch):
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


@pytest.fixture()
def isolated_pending_dir(monkeypatch):
    """Redirect skills _pending into a temp dir so tests don't pollute
    configs/skills/_pending in the real repo."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "_pending"
        from backend import skills_extractor as ex
        monkeypatch.setattr(ex, "PENDING_DIR", d, raising=False)
        yield d


@pytest.mark.asyncio
async def test_finish_does_not_extract_when_disabled(
    workflow_db, isolated_pending_dir, monkeypatch,
):
    monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)

    from backend import workflow as wf
    run = await wf.start("test/disabled")
    await wf.finish(run.id, status="completed")
    assert not isolated_pending_dir.exists() or not list(isolated_pending_dir.glob("*.md"))


@pytest.mark.asyncio
async def test_finish_does_not_extract_for_failed_runs(
    workflow_db, isolated_pending_dir, monkeypatch,
):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l1")
    from backend import workflow as wf
    run = await wf.start("test/dud")
    # Add 6 fake steps via direct DB insert (bypassing @step decorator).
    conn = await wf._conn()
    for i in range(6):
        await conn.execute(
            "INSERT INTO workflow_steps (run_id, idempotency_key, started_at, "
            "completed_at, output_json, error) VALUES (?,?,?,?,?,?)",
            (run.id, f"k{i}", 0.0, 1.0, None, None),
        )
    await conn.commit()
    await wf.finish(run.id, status="failed")
    assert not isolated_pending_dir.exists() or not list(isolated_pending_dir.glob("*.md"))


@pytest.mark.asyncio
async def test_finish_extracts_when_enabled_and_threshold_met(
    workflow_db, isolated_pending_dir, monkeypatch,
):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l1")
    from backend import workflow as wf, decision_engine as de
    de._reset_for_tests()

    run = await wf.start("test/extracted", metadata={"platform": "rk3588"})
    conn = await wf._conn()
    for i in range(6):
        await conn.execute(
            "INSERT INTO workflow_steps (run_id, idempotency_key, started_at, "
            "completed_at, output_json, error) VALUES (?,?,?,?,?,?)",
            (run.id, f"step-{i}", float(i), float(i + 1), '{"summary": "ok"}', None),
        )
    await conn.commit()

    await wf.finish(run.id, status="completed")

    files = list(isolated_pending_dir.glob("skill-*.md"))
    assert len(files) == 1, f"expected one pending skill, got {files}"
    body = files[0].read_text()
    assert "rk3588" in body
    assert "step_count: 6" in body

    # Decision Engine should have a skill/promote proposal — may be in
    # `pending` or already auto-resolved into history depending on the
    # active mode/profile defaults.
    proposals = (
        [d for d in de.list_pending() if d.kind == "skill/promote"]
        + [d for d in de.list_history(limit=10) if d.kind == "skill/promote"]
    )
    assert len(proposals) >= 1


@pytest.mark.asyncio
async def test_finish_swallows_extractor_errors(
    workflow_db, monkeypatch,
):
    """Even if the extractor blows up, finish() must still mark the
    run as completed."""
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l1")

    from backend import workflow as wf, skills_extractor as ex

    def boom(*a, **kw):
        raise RuntimeError("extractor self-destruct")
    monkeypatch.setattr(ex, "extract", boom)

    run = await wf.start("test/boom")
    await wf.finish(run.id, status="completed")
    fresh = await wf.get_run(run.id)
    assert fresh.status == "completed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Promotion endpoints — path traversal, promote, discard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_list_pending_endpoint(client, isolated_pending_dir):
    isolated_pending_dir.mkdir(parents=True, exist_ok=True)
    (isolated_pending_dir / "skill-test-abc.md").write_text("---\nname: t\n---\n")
    r = await client.get("/api/v1/skills/pending")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["items"][0]["name"] == "skill-test-abc.md"


@pytest.mark.asyncio
async def test_read_pending_returns_body(client, isolated_pending_dir):
    isolated_pending_dir.mkdir(parents=True, exist_ok=True)
    (isolated_pending_dir / "skill-x.md").write_text("# Hello skill")
    r = await client.get("/api/v1/skills/pending/skill-x.md")
    assert r.status_code == 200
    assert r.json()["body"] == "# Hello skill"


@pytest.mark.asyncio
async def test_pending_path_traversal_blocked(client):
    r = await client.get("/api/v1/skills/pending/..%2F..%2Fetc%2Fpasswd")
    # FastAPI normalises url; the encoded slash either 404s (file missing)
    # or 400s (traversal blocked). Either is acceptable — what we MUST
    # NOT see is 200.
    assert r.status_code in (400, 404)


@pytest.mark.asyncio
async def test_effective_skills_endpoint_uses_wp2_loader(
    client,
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "repo"
    skill_file = project / ".omnisight" / "skills" / "flash-fw" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\n"
        "name: flash-fw\n"
        "description: Flash firmware safely.\n"
        "keywords: [firmware, evk]\n"
        "---\n"
        "Body stays server-side.\n",
        encoding="utf-8",
    )

    from backend.routers import skills as _sk_router

    monkeypatch.setattr(_sk_router, "_PROJECT_ROOT", project, raising=False)
    r = await client.get("/api/v1/skills/effective")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"] == [
        {
            "name": "flash-fw",
            "description": "Flash firmware safely.",
            "keywords": ["firmware", "evk"],
            "scope": "project",
            "source_path": str(skill_file),
        }
    ]


@pytest.mark.asyncio
async def test_promote_moves_into_live_tree(
    client, isolated_pending_dir, monkeypatch,
):
    isolated_pending_dir.mkdir(parents=True, exist_ok=True)
    src = isolated_pending_dir / "skill-promo-test.md"
    src.write_text("---\nname: promo\n---\n# body")

    with tempfile.TemporaryDirectory() as live:
        from backend.routers import skills as _sk_router
        live_root = Path(live)
        monkeypatch.setattr(_sk_router, "_SKILLS_LIVE", live_root, raising=False)

        r = await client.post("/api/v1/skills/pending/skill-promo-test.md/promote")
        assert r.status_code == 200
        body = r.json()
        assert body["slug"] == "promo-test"
        moved = live_root / "promo-test" / "SKILL.md"
        assert moved.exists()
        assert "# body" in moved.read_text()
        # Source removed.
        assert not src.exists()


@pytest.mark.asyncio
async def test_discard_removes_pending(client, isolated_pending_dir):
    isolated_pending_dir.mkdir(parents=True, exist_ok=True)
    f = isolated_pending_dir / "skill-trash.md"
    f.write_text("nope")
    r = await client.delete("/api/v1/skills/pending/skill-trash.md")
    assert r.status_code == 200
    assert r.json()["discarded"] == "skill-trash.md"
    assert not f.exists()
