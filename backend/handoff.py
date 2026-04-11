"""Automatic HANDOFF.md generation for agent task transitions.

When an agent finalizes a workspace, this module generates a structured
handoff document that captures:
- What was done (commits, files changed)
- What the agent produced (tool results, answer)
- What remains to be done
- Known issues and context

The handoff is stored in both the workspace directory and the database,
so subsequent agents can load it as context.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_handoff(
    agent_id: str,
    task_id: str,
    task_title: str = "",
    agent_type: str = "",
    sub_type: str = "",
    model_name: str = "",
    answer: str = "",
    tool_results: list[dict] | None = None,
    finalize_result: dict | None = None,
    retry_count: int = 0,
) -> str:
    """Generate HANDOFF.md content from task execution results.

    Returns the markdown content as a string.
    """
    now = datetime.now().isoformat(timespec="seconds")
    tool_results = tool_results or []
    finalize_result = finalize_result or {}

    branch = finalize_result.get("branch", "")
    commit_count = finalize_result.get("commit_count", 0)
    commits = finalize_result.get("commits", "")
    diff_summary = finalize_result.get("diff_summary", "")
    files_changed = finalize_result.get("files_changed", [])

    # Build sections
    lines: list[str] = []

    # Frontmatter
    lines.append("---")
    lines.append(f"task_id: {task_id}")
    lines.append(f"agent_id: {agent_id}")
    lines.append(f"agent_type: {agent_type}")
    if sub_type:
        lines.append(f"role: {sub_type}")
    if model_name:
        lines.append(f"model: {model_name}")
    lines.append(f"status: finalized")
    lines.append(f"timestamp: {now}")
    lines.append("---")
    lines.append("")

    # Title
    lines.append(f"# Task Handoff: {task_title or task_id}")
    lines.append("")

    # Task summary
    lines.append("## Task Summary")
    lines.append(f"- **Task ID**: {task_id}")
    lines.append(f"- **Agent**: {agent_id} ({agent_type}{f'/{sub_type}' if sub_type else ''})")
    if model_name:
        lines.append(f"- **Model**: {model_name}")
    if branch:
        lines.append(f"- **Branch**: `{branch}`")
    lines.append(f"- **Commits**: {commit_count}")
    if retry_count:
        lines.append(f"- **Retries**: {retry_count}")
    lines.append("")

    # Agent's answer / conclusion
    if answer:
        lines.append("## Agent Output")
        # Truncate very long answers
        if len(answer) > 2000:
            lines.append(answer[:2000])
            lines.append("... [truncated]")
        else:
            lines.append(answer)
        lines.append("")

    # Tool execution results
    if tool_results:
        lines.append("## Tool Execution Results")
        for tr in tool_results:
            name = tr.get("tool_name", "unknown")
            success = tr.get("success", True)
            output = tr.get("output", "")
            status = "OK" if success else "FAILED"
            lines.append(f"### [{status}] {name}")
            # Truncate long outputs
            if len(output) > 500:
                output = output[:500] + "..."
            lines.append(f"```\n{output}\n```")
            lines.append("")

    # Files changed
    if files_changed:
        lines.append("## Files Changed")
        lines.append("| File | Status |")
        lines.append("|------|--------|")
        for f in files_changed:
            lines.append(f"| `{f}` | modified |")
        lines.append("")

    # Commit history
    if commits:
        lines.append("## Commit History")
        lines.append(f"```\n{commits}\n```")
        lines.append("")

    # Diff summary
    if diff_summary and diff_summary != "No changes made.":
        lines.append("## Diff Summary")
        lines.append(f"```\n{diff_summary}\n```")
        lines.append("")

    # Known issues (from failed tools or retries)
    failed_tools = [tr for tr in tool_results if not tr.get("success", True)]
    if failed_tools or retry_count:
        lines.append("## Known Issues")
        if retry_count:
            lines.append(f"- Pipeline retried {retry_count} time(s) due to tool errors")
        for tr in failed_tools:
            lines.append(f"- Tool `{tr.get('tool_name')}` failed: {tr.get('output', '')[:200]}")
        lines.append("")

    # Context for next agent
    lines.append("## Handoff Notes")
    lines.append("- Review the files changed above before starting new work on this branch")
    if branch:
        lines.append(f"- Workspace branch: `{branch}`")
    lines.append(f"- Generated at: {now}")
    lines.append("")

    return "\n".join(lines)


async def save_handoff(
    agent_id: str,
    task_id: str,
    content: str,
    workspace_path: Path | None = None,
) -> None:
    """Persist handoff to workspace file and database."""
    # Write to workspace directory
    if workspace_path and workspace_path.is_dir():
        handoff_path = workspace_path / "HANDOFF.md"
        handoff_path.write_text(content, encoding="utf-8")
        logger.info("Handoff written to %s", handoff_path)

    # Persist to database
    from backend import db
    try:
        await db.upsert_handoff(task_id, agent_id, content)
    except Exception as exc:
        logger.warning("Failed to persist handoff to DB: %s", exc)


async def load_handoff_for_task(task_id: str) -> str:
    """Load handoff content from DB for a given task."""
    from backend import db
    try:
        return await db.get_handoff(task_id)
    except Exception:
        return ""
