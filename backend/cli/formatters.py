"""Pure-function output formatters for ``omnisight`` CLI.

Kept side-effect free so the tests can assert exact byte output
without spinning up Click's ``CliRunner`` every time.
"""

from __future__ import annotations

import json
from typing import Any


def format_status(payload: dict[str, Any]) -> str:
    """Render the /status JSON into a compact KPI table."""
    rows = [
        ("Tasks completed", f"{payload.get('tasks_completed', 0)}/{payload.get('tasks_total', 0)}"),
        ("Agents running", str(payload.get("agents_running", 0))),
        ("Workspaces active", str(payload.get("workspaces_active", 0))),
        ("Containers active", str(payload.get("containers_active", 0))),
        ("CPU", str(payload.get("cpu_summary", "N/A"))),
        ("Memory", str(payload.get("memory_summary", "N/A"))),
        ("WSL", str(payload.get("wsl_status", "N/A"))),
        ("USB", str(payload.get("usb_status", "N/A"))),
    ]
    width = max(len(k) for k, _ in rows)
    lines = ["OmniSight system status", "=" * 40]
    for k, v in rows:
        lines.append(f"  {k.ljust(width)} : {v}")
    return "\n".join(lines)


def format_workspace_list(rows: list[dict[str, Any]]) -> str:
    """Render /workspaces into an aligned text table."""
    if not rows:
        return "No active workspaces."
    cols = ("agent_id", "task_id", "branch", "status", "commit_count")
    widths = {c: len(c) for c in cols}
    rendered: list[tuple[str, ...]] = []
    for r in rows:
        cells = tuple(str(r.get(c, "")) for c in cols)
        rendered.append(cells)
        for c, v in zip(cols, cells):
            widths[c] = max(widths[c], len(v))
    header = "  ".join(c.upper().ljust(widths[c]) for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    body = [
        "  ".join(v.ljust(widths[c]) for c, v in zip(cols, cells))
        for cells in rendered
    ]
    return "\n".join([header, sep, *body, f"\n{len(rows)} workspace(s)"])


def format_skills_list(rows: list[dict[str, Any]]) -> str:
    """Render effective WP.2 skills into an aligned text table."""
    if not rows:
        return "No effective skills found."
    cols = ("name", "scope", "provider_rank", "source_path", "description")
    widths = {c: len(c) for c in cols}
    rendered: list[tuple[str, ...]] = []
    for r in rows:
        cells = tuple(str(r.get(c, "") or "") for c in cols)
        rendered.append(cells)
        for c, v in zip(cols, cells):
            widths[c] = max(widths[c], len(v))
    header = "  ".join(c.upper().ljust(widths[c]) for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    body = [
        "  ".join(v.ljust(widths[c]) for c, v in zip(cols, cells))
        for cells in rendered
    ]
    return "\n".join([header, sep, *body, f"\n{len(rows)} skill(s)"])


def format_skill_resolve(row: dict[str, Any]) -> str:
    """Render one effective WP.2 skill source resolution."""
    lines = [
        f"Skill: {row.get('name', '?')}",
        f"Description: {row.get('description', '')}",
        f"Scope: {row.get('scope', '')}",
        f"Provider rank: {row.get('provider_rank', '')}",
        f"Source: {row.get('source_path', '')}",
    ]
    keywords = row.get("keywords") or []
    if keywords:
        lines.append(f"Keywords: {', '.join(str(k) for k in keywords)}")
    return "\n".join(lines)


def format_inspect(agent: dict[str, Any], workspace: dict[str, Any] | None) -> str:
    """Render agent + optional workspace info as markdown."""
    status = agent.get("status", "?")
    if isinstance(status, dict):
        status = status.get("value") or status.get("name") or "?"
    progress = agent.get("progress") or {}
    cur = progress.get("current", 0) if isinstance(progress, dict) else 0
    tot = progress.get("total", 0) if isinstance(progress, dict) else 0
    lines = [
        f"### 🔎 Agent `{agent.get('id', '?')}`",
        f"- name: **{agent.get('name', '?')}**",
        f"- type: `{agent.get('type', '?')}`"
        + (f" / `{agent['sub_type']}`" if agent.get("sub_type") else ""),
        f"- status: **{status}**",
        f"- progress: {cur}/{tot}",
    ]
    if agent.get("ai_model"):
        lines.append(f"- model: `{agent['ai_model']}`")
    tc = (agent.get("thought_chain") or "").strip()
    if tc:
        snippet = tc if len(tc) <= 1200 else tc[:1200] + " …"
        lines.append("")
        lines.append("**Thought chain / ReAct log**:")
        lines.append("```")
        lines.append(snippet)
        lines.append("```")
    if workspace:
        lines.append("")
        lines.append(
            f"**Workspace**: branch `{workspace.get('branch', '?')}` · "
            f"status `{workspace.get('status', '?')}` · "
            f"{workspace.get('commit_count', 0)} commit(s)"
        )
        if workspace.get("path"):
            lines.append(f"- path: `{workspace['path']}`")
    else:
        lines.append("")
        lines.append("_No active workspace for this agent._")
    return "\n".join(lines)


def format_inject_result(payload: dict[str, Any]) -> str:
    hint = payload.get("hint") or {}
    if not isinstance(hint, dict):
        hint = {}
    text = str(hint.get("text") or "")
    aid = str(hint.get("agent_id") or "?")
    author = str(hint.get("author") or "cli")
    note = f"({len(text)} chars, author={author})" if text else ""
    return f"✅ Hint injected into `{aid}` {note}".rstrip()


def format_run_event(event: str, data: dict[str, Any]) -> str:
    """Render one SSE frame from /invoke/stream as a single line."""
    if event in ("", "message"):
        event = "message"
    core = data.get("message") or data.get("detail")
    if core:
        return f"[{event}] {core}"
    try:
        blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    except Exception:
        blob = str(data)
    if len(blob) > 300:
        blob = blob[:300] + " …"
    return f"[{event}] {blob}"


def format_json(payload: Any) -> str:
    """Stable JSON output for ``--json`` flag on every command."""
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str)
