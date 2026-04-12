"""Tests for artifact generation pipeline."""

import asyncio

import pytest

from backend.models import Artifact, ArtifactType


class TestArtifactDB:

    @pytest.mark.asyncio
    async def test_insert_and_list(self):
        import uuid
        from backend import db
        await db.init()
        try:
            art_id = f"art-test-{uuid.uuid4().hex[:6]}"
            await db.insert_artifact({
                "id": art_id, "task_id": "t-test", "agent_id": "a1",
                "name": "test.md", "type": "markdown", "file_path": "/tmp/test.md",
                "size": 100, "created_at": "2026-01-01T00:00:00",
            })
            arts = await db.list_artifacts(task_id="t-test")
            assert any(a["id"] == art_id for a in arts)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_delete(self):
        import uuid
        from backend import db
        await db.init()
        try:
            art_id = f"art-del-{uuid.uuid4().hex[:6]}"
            await db.insert_artifact({
                "id": art_id, "task_id": "t-test", "agent_id": "a1",
                "name": "del.md", "type": "markdown", "file_path": "/tmp/del.md",
                "size": 50, "created_at": "2026-01-01T00:00:00",
            })
            ok = await db.delete_artifact(art_id)
            assert ok
            gone = await db.get_artifact(art_id)
            assert gone is None
        finally:
            await db.close()


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


class TestReportGenerator:

    @pytest.mark.asyncio
    async def test_list_templates(self):
        from backend.report_generator import list_templates
        templates = list_templates()
        assert "compliance_report" in templates
        assert "test_summary" in templates

    @pytest.mark.asyncio
    async def test_generate_compliance_report(self):
        from backend.report_generator import generate_report
        from backend import db
        await db.init()
        try:
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
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_generate_test_summary(self):
        from backend.report_generator import generate_report
        from backend import db
        await db.init()
        try:
            result = await generate_report(
                "test_summary",
                {"title": "Validation Results", "total_tests": 50, "passed": 48, "failed": 2},
                task_id="test-task",
            )
            assert "error" not in result
            assert result["size"] > 0
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_unknown_template_returns_error(self):
        from backend.report_generator import generate_report
        from backend import db
        await db.init()
        try:
            result = await generate_report("nonexistent_template", {})
            assert "error" in result
            assert "not found" in result["error"].lower()
        finally:
            await db.close()


class TestArtifactTool:

    @pytest.mark.asyncio
    async def test_generate_artifact_report_tool(self):
        from backend.agents.tools import generate_artifact_report
        from backend import db
        await db.init()
        try:
            result = await generate_artifact_report.ainvoke({
                "template": "compliance_report",
                "title": "FCC Report",
                "context_json": '{"hardware_spec": {"sensor": "IMX335"}}',
            })
            assert "[OK]" in result
            assert "Report generated" in result
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_tool_with_invalid_template(self):
        from backend.agents.tools import generate_artifact_report
        from backend import db
        await db.init()
        try:
            result = await generate_artifact_report.ainvoke({
                "template": "nonexistent",
                "title": "Test",
            })
            assert "[ERROR]" in result
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_tool_with_task_id(self):
        from backend.agents.tools import generate_artifact_report
        from backend import db
        await db.init()
        try:
            result = await generate_artifact_report.ainvoke({
                "template": "compliance_report",
                "title": "Task Report",
                "task_id": "task-42",
            })
            assert "[OK]" in result
            # Verify artifact has task_id
            arts = await db.list_artifacts(task_id="task-42")
            assert len(arts) >= 1
            assert arts[0]["task_id"] == "task-42"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_tool_with_invalid_json(self):
        from backend.agents.tools import generate_artifact_report
        from backend import db
        await db.init()
        try:
            result = await generate_artifact_report.ainvoke({
                "template": "compliance_report",
                "title": "Test",
                "context_json": "not valid json",
            })
            # Should still work (empty context fallback)
            assert "[OK]" in result
        finally:
            await db.close()
