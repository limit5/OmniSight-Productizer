"""Tests for artifact generation pipeline.

Phase-3-Runtime-v2 SP-3.6b (2026-04-20): migrated from SQLite
``db.init()`` lifecycle to ``pg_test_conn`` / ``pg_test_pool``
fixtures. The db.* direct-call tests (TestArtifactDB) pass the
pg_test_conn fixture explicitly; tests that exercise the report
generator / tool path (which internally acquires from pool) use the
pg_test_pool fixture to ensure the module-global pool is installed.
"""

import pytest
from fastapi import HTTPException

from backend.models import Artifact, ArtifactType


class TestArtifactDB:

    @pytest.mark.asyncio
    async def test_insert_and_list(self, pg_test_conn):
        import uuid
        from backend import db
        art_id = f"art-test-{uuid.uuid4().hex[:6]}"
        await db.insert_artifact(pg_test_conn, {
            "id": art_id, "task_id": "t-test", "agent_id": "a1",
            "name": "test.md", "type": "markdown", "file_path": "/tmp/test.md",
            "size": 100, "created_at": "2026-01-01T00:00:00",
        })
        arts = await db.list_artifacts(pg_test_conn, task_id="t-test")
        assert any(a["id"] == art_id for a in arts)

    @pytest.mark.asyncio
    async def test_delete(self, pg_test_conn):
        import uuid
        from backend import db
        art_id = f"art-del-{uuid.uuid4().hex[:6]}"
        await db.insert_artifact(pg_test_conn, {
            "id": art_id, "task_id": "t-test", "agent_id": "a1",
            "name": "del.md", "type": "markdown", "file_path": "/tmp/del.md",
            "size": 50, "created_at": "2026-01-01T00:00:00",
        })
        ok = await db.delete_artifact(pg_test_conn, art_id)
        assert ok
        gone = await db.get_artifact(pg_test_conn, art_id)
        assert gone is None


class TestArtifactModel:

    def test_defaults(self):
        a = Artifact(id="a1", name="report.md")
        assert a.type == ArtifactType.markdown
        assert a.size == 0
        assert a.task_id is None

    def test_all_types(self):
        for t in ArtifactType:
            a = Artifact(id="a1", name="file", type=t)
            assert a.type == t


class TestArtifactRouterEdgeCases:

    def test_path_validation_allows_nested_tenant_artifact_file(self, tmp_path, monkeypatch):
        from backend.routers import artifacts as art_mod

        tenants_root = tmp_path / "data" / "tenants"
        artifact_file = tenants_root / "t-a" / "artifacts" / "task" / "report.md"
        artifact_file.parent.mkdir(parents=True)
        artifact_file.write_text("ok")

        monkeypatch.setattr(art_mod, "tenants_root", lambda: tenants_root)
        monkeypatch.setattr(art_mod, "_LEGACY_ARTIFACTS_ROOT", tmp_path / ".artifacts")

        assert art_mod._is_valid_artifact_path(artifact_file)

    def test_path_validation_rejects_prefix_sibling_outside_tenants_root(self, tmp_path, monkeypatch):
        from backend.routers import artifacts as art_mod

        tenants_root = tmp_path / "data" / "tenants"
        sibling_file = tmp_path / "data" / "tenants_evil" / "t-a" / "artifacts" / "report.md"
        sibling_file.parent.mkdir(parents=True)
        sibling_file.write_text("no")

        monkeypatch.setattr(art_mod, "tenants_root", lambda: tenants_root)
        monkeypatch.setattr(art_mod, "_LEGACY_ARTIFACTS_ROOT", tmp_path / ".artifacts")

        assert not art_mod._is_valid_artifact_path(sibling_file)

    @pytest.mark.asyncio
    async def test_download_missing_file_inside_artifact_storage_returns_404(self, tmp_path, monkeypatch):
        from backend.routers import artifacts as art_mod

        tenants_root = tmp_path / "data" / "tenants"
        missing_file = tenants_root / "t-a" / "artifacts" / "missing.md"

        async def fake_get_artifact(_conn, artifact_id):
            assert artifact_id == "art-missing-file"
            return {
                "id": artifact_id,
                "name": "missing.md",
                "type": "markdown",
                "file_path": str(missing_file),
            }

        monkeypatch.setattr(art_mod, "tenants_root", lambda: tenants_root)
        monkeypatch.setattr(art_mod, "_LEGACY_ARTIFACTS_ROOT", tmp_path / ".artifacts")
        monkeypatch.setattr(art_mod.db, "get_artifact", fake_get_artifact)

        with pytest.raises(HTTPException) as excinfo:
            await art_mod.download_artifact("art-missing-file", conn=object())

        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Artifact file missing from disk"

    @pytest.mark.asyncio
    async def test_delete_outside_artifact_storage_skips_unlink_but_deletes_metadata(
        self,
        tmp_path,
        monkeypatch,
    ):
        from backend.routers import artifacts as art_mod

        tenants_root = tmp_path / "data" / "tenants"
        outside_file = tmp_path / "outside" / "keep.bin"
        outside_file.parent.mkdir()
        outside_file.write_bytes(b"do not delete")
        deleted_ids = []

        async def fake_get_artifact(_conn, artifact_id):
            assert artifact_id == "art-outside"
            return {
                "id": artifact_id,
                "name": "keep.bin",
                "type": "binary",
                "file_path": str(outside_file),
            }

        async def fake_delete_artifact(_conn, artifact_id):
            deleted_ids.append(artifact_id)
            return True

        monkeypatch.setattr(art_mod, "tenants_root", lambda: tenants_root)
        monkeypatch.setattr(art_mod, "_LEGACY_ARTIFACTS_ROOT", tmp_path / ".artifacts")
        monkeypatch.setattr(art_mod.db, "get_artifact", fake_get_artifact)
        monkeypatch.setattr(art_mod.db, "delete_artifact", fake_delete_artifact)

        await art_mod.delete_artifact("art-outside", conn=object())

        assert outside_file.exists()
        assert deleted_ids == ["art-outside"]


class TestReportGenerator:

    @pytest.mark.asyncio
    async def test_list_templates(self):
        # Pure function, no DB needed.
        from backend.report_generator import list_templates
        templates = list_templates()
        assert "compliance_report" in templates
        assert "test_summary" in templates

    @pytest.mark.asyncio
    async def test_generate_compliance_report(self, pg_test_pool):
        # generate_report writes an artifact via get_pool().acquire()
        # internally; pg_test_pool ensures the module-global pool is
        # installed so that acquire doesn't raise.
        from backend.report_generator import generate_report
        result = await generate_report(
            "compliance_report",
            {"title": "Test Report", "hardware_spec": {"sensor": "IMX335"}},
            task_id="test-task",
            agent_id="reporter-test",
        )
        assert "error" not in result
        assert result["name"].endswith(".md")
        assert result["size"] > 0
        assert result["type"] == "markdown"

    @pytest.mark.asyncio
    async def test_generate_test_summary(self, pg_test_pool):
        from backend.report_generator import generate_report
        result = await generate_report(
            "test_summary",
            {"title": "Validation Results", "total_tests": 50, "passed": 48, "failed": 2},
            task_id="test-task",
        )
        assert "error" not in result
        assert result["size"] > 0

    @pytest.mark.asyncio
    async def test_unknown_template_returns_error(self, pg_test_pool):
        from backend.report_generator import generate_report
        result = await generate_report("nonexistent_template", {})
        assert "error" in result
        assert "not found" in result["error"].lower()


class TestArtifactTool:

    @pytest.mark.asyncio
    async def test_generate_artifact_report_tool(self, pg_test_pool):
        from backend.agents.tools import generate_artifact_report
        result = await generate_artifact_report.ainvoke({
            "template": "compliance_report",
            "title": "FCC Report",
            "context_json": '{"hardware_spec": {"sensor": "IMX335"}}',
        })
        assert "[OK]" in result
        assert "Report generated" in result

    @pytest.mark.asyncio
    async def test_tool_with_invalid_template(self, pg_test_pool):
        from backend.agents.tools import generate_artifact_report
        result = await generate_artifact_report.ainvoke({
            "template": "nonexistent",
            "title": "Test",
        })
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_tool_with_task_id(self, pg_test_pool):
        from backend.agents.tools import generate_artifact_report
        from backend import db
        result = await generate_artifact_report.ainvoke({
            "template": "compliance_report",
            "title": "Task Report",
            "task_id": "task-42",
        })
        assert "[OK]" in result
        # Verify artifact has task_id — acquire a fresh conn (the
        # tool's write is committed via its own pool-scoped acquire).
        async with pg_test_pool.acquire() as conn:
            arts = await db.list_artifacts(conn, task_id="task-42")
        assert len(arts) >= 1
        assert arts[0]["task_id"] == "task-42"
        # Cleanup — pg_test_pool does not auto-rollback committed rows.
        async with pg_test_pool.acquire() as conn:
            for art in arts:
                await db.delete_artifact(conn, art["id"])

    @pytest.mark.asyncio
    async def test_tool_with_invalid_json(self, pg_test_pool):
        from backend.agents.tools import generate_artifact_report
        result = await generate_artifact_report.ainvoke({
            "template": "compliance_report",
            "title": "Test",
            "context_json": "not valid json",
        })
        # Should still work (empty context fallback)
        assert "[OK]" in result
