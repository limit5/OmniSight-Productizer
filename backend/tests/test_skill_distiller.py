"""BP.M.2 -- Architect Guild skill distiller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from backend import skill_distiller as sd


@dataclass
class _FakeRun:
    id: str = "task-123"
    kind: str = "architect/blueprint"
    status: str = "completed"
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


@dataclass
class _FakeStep:
    idempotency_key: str
    output: Any = None
    error: str | None = None


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.calls.append((sql, args))
        return {
            "id": args[0],
            "tenant_id": args[1],
            "skill_name": args[2],
            "source_task_id": args[3],
            "markdown_content": args[4],
            "version": args[5],
            "status": args[6],
        }


def _steps(n: int) -> list[_FakeStep]:
    return [
        _FakeStep(
            idempotency_key=f"step-{i}",
            output={"summary": f"completed stage {i}"},
        )
        for i in range(n)
    ]


def test_should_distill_requires_success() -> None:
    run = _FakeRun(status="failed", metadata={"tool_calls": 9, "iterations": 9})
    assert sd.should_distill(run, _steps(9)) is False


def test_should_distill_on_tool_call_threshold() -> None:
    run = _FakeRun(metadata={"tool_calls": 6, "iterations": 1})
    assert sd.should_distill(run, _steps(1)) is True


def test_should_distill_on_iteration_threshold() -> None:
    run = _FakeRun(metadata={"tool_calls": 0, "iterations": 4})
    assert sd.should_distill(run, _steps(1)) is True


def test_step_count_is_iteration_fallback() -> None:
    run = _FakeRun(metadata={"tool_calls": 0})
    stats = sd.trajectory_stats(run, _steps(4))
    assert stats.iterations == 4
    assert sd.should_distill(run, _steps(4)) is True


def test_step_output_tool_calls_are_summed_when_metadata_absent() -> None:
    run = _FakeRun()
    steps = [
        _FakeStep("a", output={"tool_calls": 2}),
        _FakeStep("b", output={"tool_call_count": 4}),
    ]
    stats = sd.trajectory_stats(run, steps)
    assert stats.tool_calls == 6
    assert sd.should_distill(run, steps) is True


def test_build_markdown_contains_review_queue_metadata() -> None:
    run = _FakeRun(
        metadata={
            "tenant_id": "t-acme",
            "task_id": "task-acme",
            "tool_calls": 8,
            "iterations": 2,
        }
    )
    skill_name, markdown = sd.build_markdown(run, _steps(2))
    assert skill_name == "auto-architect-blueprint"
    assert "source: architect_guild" in markdown
    assert "status: draft" in markdown
    assert "tool_call_count: 8" in markdown
    assert "source_task_id: 'task-acme'" in markdown
    assert "## Human Review Notes" in markdown


@pytest.mark.asyncio
async def test_distill_inserts_draft_row() -> None:
    conn = _FakeConn()
    run = _FakeRun(
        metadata={
            "tenant_id": "t-acme",
            "task_id": "task-acme",
            "tool_calls": 6,
            "iterations": 1,
        }
    )
    result = await sd.distill(run, _steps(2), conn=conn)
    assert result.written is True
    assert result.draft is not None
    assert result.draft.id.startswith("ads-")
    assert result.draft.tenant_id == "t-acme"
    assert result.draft.source_task_id == "task-acme"
    assert result.draft.status == "draft"

    sql, args = conn.calls[0]
    assert "INSERT INTO auto_distilled_skills" in sql
    assert args[1] == "t-acme"
    assert args[6] == "draft"


@pytest.mark.asyncio
async def test_distill_skips_below_threshold() -> None:
    conn = _FakeConn()
    run = _FakeRun(metadata={"tool_calls": 5, "iterations": 3})
    result = await sd.distill(run, _steps(1), conn=conn)
    assert result.written is False
    assert "below threshold" in result.skipped_reason
    assert conn.calls == []


@pytest.mark.asyncio
async def test_architect_hook_honours_l1_gate(monkeypatch) -> None:
    monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)
    conn = _FakeConn()
    run = _FakeRun(metadata={"tool_calls": 9, "iterations": 9})
    result = await sd.architect_guild_hook(run, _steps(2), conn=conn)
    assert result.written is False
    assert result.skipped_reason == "disabled"
    assert conn.calls == []

    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l1")
    result = await sd.architect_guild_hook(run, _steps(2), conn=conn)
    assert result.written is True
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_distill_scrubs_secret_before_insert() -> None:
    conn = _FakeConn()
    run = _FakeRun(metadata={"tenant_id": "t-acme", "tool_calls": 6})
    steps = [
        _FakeStep(
            "leaky",
            output={
                "summary": (
                    "token ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
                    "was rotated"
                )
            },
        )
    ]
    result = await sd.distill(run, steps, conn=conn)
    assert result.written is True
    assert result.draft is not None
    assert "[GITHUB_PAT]" in result.draft.markdown_content
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in (
        result.draft.markdown_content
    )
