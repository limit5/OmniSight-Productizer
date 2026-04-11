"""Tests for backend/handoff.py — handoff generation and structure."""

from backend.handoff import generate_handoff


class TestGenerateHandoff:

    def test_basic_handoff(self):
        content = generate_handoff(
            agent_id="firmware-alpha",
            task_id="task-001",
            task_title="Build driver",
        )
        assert "task-001" in content
        assert "firmware-alpha" in content
        assert "Build driver" in content

    def test_handoff_with_full_context(self):
        content = generate_handoff(
            agent_id="firmware-alpha",
            task_id="task-001",
            task_title="Build IMX335 driver",
            agent_type="firmware",
            sub_type="bsp",
            model_name="claude-sonnet-4",
            answer="Driver compiled successfully.",
            tool_results=[
                {"tool_name": "read_file", "output": "config loaded", "success": True},
                {"tool_name": "run_bash", "output": "[ERROR] compile failed", "success": False},
            ],
            finalize_result={
                "branch": "agent/firmware-alpha/task-001",
                "commit_count": 2,
                "commits": "abc1234 Add driver",
                "diff_summary": "1 file changed, 50 insertions(+)",
                "files_changed": ["drivers/imx335.c"],
            },
            retry_count=1,
        )
        assert "firmware/bsp" in content
        assert "claude-sonnet-4" in content
        assert "agent/firmware-alpha/task-001" in content
        assert "imx335.c" in content
        assert "Known Issues" in content
        assert "retried" in content.lower()

    def test_handoff_frontmatter(self):
        content = generate_handoff(
            agent_id="validator-gamma",
            task_id="task-002",
            agent_type="validator",
            sub_type="sdet",
            model_name="gpt-4o",
        )
        assert content.startswith("---")
        assert "task_id: task-002" in content
        assert "role: sdet" in content
        assert "model: gpt-4o" in content

    def test_handoff_failed_tools_in_known_issues(self):
        content = generate_handoff(
            agent_id="sw-beta",
            task_id="task-003",
            tool_results=[
                {"tool_name": "run_bash", "output": "segfault", "success": False},
            ],
        )
        assert "Known Issues" in content
        assert "run_bash" in content
        assert "segfault" in content

    def test_handoff_no_changes(self):
        content = generate_handoff(
            agent_id="reporter-delta",
            task_id="task-004",
            finalize_result={
                "branch": "agent/reporter-delta/task-004",
                "commit_count": 0,
                "commits": "",
                "diff_summary": "No changes made.",
                "files_changed": [],
            },
        )
        assert "**Commits**: 0" in content
        assert "Files Changed" not in content  # no files section

    def test_handoff_long_answer_truncated(self):
        long_answer = "x" * 5000
        content = generate_handoff(
            agent_id="fw-alpha",
            task_id="task-005",
            answer=long_answer,
        )
        assert "[truncated]" in content
