"""Tests for issue tracking: state machine, fact gate, tools, URL parsing."""

import asyncio

import pytest

from backend.models import Task, TaskStatus, TaskComment, TASK_TRANSITIONS


class TestStateMachine:
    """Validate TASK_TRANSITIONS rules."""

    def test_backlog_allowed_transitions(self):
        allowed = TASK_TRANSITIONS["backlog"]
        assert "assigned" in allowed
        assert "in_progress" in allowed
        assert "blocked" in allowed
        assert "completed" not in allowed  # Can't skip straight to completed

    def test_in_progress_to_in_review(self):
        assert "in_review" in TASK_TRANSITIONS["in_progress"]

    def test_in_review_to_completed(self):
        assert "completed" in TASK_TRANSITIONS["in_review"]

    def test_in_review_to_in_progress_revision(self):
        """Review rejected → back to in_progress."""
        assert "in_progress" in TASK_TRANSITIONS["in_review"]

    def test_completed_can_reopen(self):
        assert "backlog" in TASK_TRANSITIONS["completed"]

    def test_blocked_can_unblock(self):
        allowed = TASK_TRANSITIONS["blocked"]
        assert "backlog" in allowed
        assert "in_progress" in allowed

    def test_all_states_have_transitions(self):
        for status in TaskStatus:
            assert status.value in TASK_TRANSITIONS, f"Missing transitions for {status.value}"

    def test_invalid_transition_backlog_to_completed(self):
        """Cannot jump from backlog directly to completed."""
        assert "completed" not in TASK_TRANSITIONS["backlog"]

    def test_invalid_transition_backlog_to_in_review(self):
        """Cannot jump from backlog directly to in_review."""
        assert "in_review" not in TASK_TRANSITIONS["backlog"]

    def test_invalid_transition_completed_to_in_progress(self):
        """Completed can only reopen to backlog, not jump to in_progress."""
        assert "in_progress" not in TASK_TRANSITIONS["completed"]

    def test_bidirectional_block_unblock(self):
        """Any active state can block, and blocked can unblock."""
        for state in ("assigned", "in_progress", "in_review"):
            assert "blocked" in TASK_TRANSITIONS[state], f"{state} cannot be blocked"
        for target in ("backlog", "in_progress"):
            assert target in TASK_TRANSITIONS["blocked"], f"blocked cannot transition to {target}"


class TestTaskModel:

    def test_new_fields_have_defaults(self):
        t = Task(id="t1", title="Test")
        assert t.external_issue_id is None
        assert t.issue_url is None
        assert t.acceptance_criteria is None
        assert t.labels == []

    def test_task_with_issue_fields(self):
        t = Task(
            id="t1", title="Fix bug",
            external_issue_id="OMNI-42",
            issue_url="https://jira.company.com/browse/OMNI-42",
            acceptance_criteria="Unit tests pass, no memory leaks",
            labels=["ai-assigned", "firmware"],
        )
        assert t.external_issue_id == "OMNI-42"
        assert "jira" in t.issue_url
        assert "memory leaks" in t.acceptance_criteria
        assert "ai-assigned" in t.labels

    def test_in_review_status(self):
        t = Task(id="t1", title="Test", status=TaskStatus.in_review)
        assert t.status == TaskStatus.in_review


class TestTaskComment:

    def test_comment_creation(self):
        c = TaskComment(id="c1", task_id="t1", author="agent:fw-alpha", content="Started work")
        assert c.author == "agent:fw-alpha"
        assert c.content == "Started work"


class TestIssueUrlParsing:

    def test_github_issue_url(self):
        from backend.issue_tracker import _parse_github_issue_url
        repo, num = _parse_github_issue_url("https://github.com/org/repo/issues/42")
        assert repo == "org/repo"
        assert num == "42"

    def test_gitlab_issue_url(self):
        from backend.issue_tracker import _parse_gitlab_issue_url
        api, proj, iid = _parse_gitlab_issue_url("https://gitlab.com/group/project/-/issues/10")
        assert api == "https://gitlab.com"
        assert "group" in proj
        assert iid == "10"

    def test_gitlab_self_hosted(self):
        from backend.issue_tracker import _parse_gitlab_issue_url
        api, proj, iid = _parse_gitlab_issue_url("https://gitlab.company.com/team/repo/-/issues/55")
        assert api == "https://gitlab.company.com"
        assert iid == "55"

    def test_jira_issue_url(self):
        from backend.issue_tracker import _parse_jira_issue_url
        base, key = _parse_jira_issue_url("https://jira.company.com/browse/OMNI-123")
        assert base == "https://jira.company.com"
        assert key == "OMNI-123"

    def test_platform_detection(self):
        from backend.issue_tracker import _detect_platform_from_issue_url
        assert _detect_platform_from_issue_url("https://github.com/org/repo/issues/1") == "github"
        assert _detect_platform_from_issue_url("https://gitlab.com/g/p/-/issues/1") == "gitlab"
        assert _detect_platform_from_issue_url("https://jira.co/browse/X-1") == "jira"
        assert _detect_platform_from_issue_url("https://unknown.com/foo") == "unknown"


class TestWrapperTools:

    @pytest.mark.asyncio
    async def test_get_next_task_no_tasks(self):
        from backend.agents.tools import get_next_task
        result = await get_next_task.ainvoke({"label_filter": "nonexistent"})
        assert "[NO TASKS]" in result

    @pytest.mark.asyncio
    async def test_update_task_status_not_found(self):
        from backend.agents.tools import update_task_status
        result = await update_task_status.ainvoke({"task_id": "fake-id", "status": "completed"})
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_add_task_comment_not_found(self):
        from backend.agents.tools import add_task_comment
        result = await add_task_comment.ainvoke({"task_id": "fake-id", "content": "test"})
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_add_task_comment_empty(self):
        from backend.agents.tools import add_task_comment
        result = await add_task_comment.ainvoke({"task_id": "t1", "content": ""})
        assert "[ERROR]" in result
