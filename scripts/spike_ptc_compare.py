#!/usr/bin/env python3
"""Programmatic Tool Calling feasibility spike harness (OP-861).

Offline comparison for one read-only runner workflow:

  * fetch the ticket body
  * fetch the parent META ticket
  * fetch related Gerrit changes

The current runner performs those as separate model-visible tool turns.
The PTC path models the same reads inside ``code_execution_20260120`` and
returns one compact JSON summary. No live Anthropic call is made by default;
this is a deterministic spike harness for cost/latency shape, not a vendor
benchmark.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


PTC_TOOL_VERSION = "code_execution_20260120"
PTC_MODEL = "claude-sonnet-4-6"
PTC_UNAVAILABLE_REASON = "PTC feature unavailable or disabled"

TOKEN_CHARS = 4
MODEL_TURN_LATENCY_S = 1.8
TOOL_LATENCY_S = 0.35
CODE_EXECUTION_OVERHEAD_S = 1.2


Classification = Literal[
    "idempotent_read",
    "bounded_write",
    "external_side_effect",
    "local_side_effect",
    "unsafe_for_ptc",
]


@dataclass(frozen=True)
class RunnerTool:
    name: str
    classification: Classification
    ptc_safe: bool
    reason: str


@dataclass(frozen=True)
class WorkflowMetrics:
    mode: str
    model_turns: int
    tool_invocations: int
    input_tokens: int
    output_tokens: int
    wall_s: float
    note: str


@dataclass(frozen=True)
class Comparison:
    workflow: str
    ptc_available: bool
    baseline: WorkflowMetrics
    ptc: WorkflowMetrics
    delta: dict[str, float]


TOOL_CATALOG: tuple[RunnerTool, ...] = (
    RunnerTool("Read", "idempotent_read", True, "filesystem read"),
    RunnerTool("Grep", "idempotent_read", True, "filesystem search"),
    RunnerTool("Glob", "idempotent_read", True, "filesystem glob"),
    RunnerTool("git_status", "idempotent_read", True, "git state read"),
    RunnerTool("git_log", "idempotent_read", True, "git history read"),
    RunnerTool("git_diff", "idempotent_read", True, "diff read"),
    RunnerTool("git_diff_staged", "idempotent_read", True, "diff read"),
    RunnerTool("git_branch", "idempotent_read", True, "branch read"),
    RunnerTool("jira_fetch", "idempotent_read", True, "ticket read"),
    RunnerTool("gerrit_query", "idempotent_read", True, "change read"),
    RunnerTool("gh_pr_view", "idempotent_read", True, "GitHub PR read"),
    RunnerTool("gh_issue_view", "idempotent_read", True, "GitHub issue read"),
    RunnerTool(
        "mcp_list", "idempotent_read", False,
        "MCP connector tools cannot be called programmatically",
    ),
    RunnerTool(
        "mcp_search", "idempotent_read", False,
        "MCP connector tools cannot be called programmatically",
    ),
    RunnerTool(
        "WebSearch", "idempotent_read", False,
        "network results are external and injection-prone",
    ),
    RunnerTool("Skill", "idempotent_read", True, "loads checked-in skill text"),
    RunnerTool("Agent", "unsafe_for_ptc", False, "spawns nested model/tool loop"),
    RunnerTool("Write", "local_side_effect", False, "mutates worktree"),
    RunnerTool("Edit", "local_side_effect", False, "mutates worktree"),
    RunnerTool("Bash", "unsafe_for_ptc", False, "arbitrary command surface"),
    RunnerTool("git_add", "local_side_effect", False, "mutates index"),
    RunnerTool(
        "git_commit", "local_side_effect", False, "mutates repository history",
    ),
    RunnerTool("git_push", "external_side_effect", False, "remote side effect"),
    RunnerTool("gerrit_push", "external_side_effect", False, "remote side effect"),
    RunnerTool("gerrit_post_comment", "external_side_effect", False, "review side effect"),
    RunnerTool("gerrit_submit_review", "external_side_effect", False, "review side effect"),
    RunnerTool("jira_update", "external_side_effect", False, "ticket mutation"),
    RunnerTool("jira_comment", "external_side_effect", False, "ticket mutation"),
    RunnerTool("jira_transition", "external_side_effect", False, "workflow mutation"),
    RunnerTool(
        "run_tests", "local_side_effect", False,
        "process execution; safe only in host runner",
    ),
    RunnerTool(
        "run_simulation", "unsafe_for_ptc", False,
        "long-running target/toolchain workflow",
    ),
    RunnerTool("image_generate", "external_side_effect", False, "remote generation call"),
)


SAMPLE_DATA = {
    "ticket": {
        "key": "OP-861",
        "summary": "Programmatic Tool Calling feasibility spike",
        "parent": "OP-843",
        "acceptance_count": 5,
    },
    "parent": {
        "key": "OP-843",
        "summary": "Vendor feature disambiguation discipline",
        "status": "Done",
    },
    "gerrit": [
        {
            "change": "Iabc001",
            "subject": "[OP-843] Outcomes spike",
            "status": "MERGED",
        },
        {
            "change": "Iabc002",
            "subject": "[OP-847] Outcomes final attempt",
            "status": "MERGED",
        },
    ],
}


PTC_PROCEDURE = """
async def _claude_code():
    ticket = await jira_fetch("OP-861")
    parent_key = ticket["parent"]
    parent = await jira_fetch(parent_key)
    changes = await gerrit_query(f'topic:{ticket["key"]} OR topic:{parent_key}')
    merged = [c for c in changes if c.get("status") == "MERGED"]
    print(json.dumps({
        "ticket": ticket["key"],
        "parent": parent["key"],
        "acceptance_count": ticket["acceptance_count"],
        "merged_related_changes": len(merged),
    }, sort_keys=True))
""".strip()


def estimate_tokens(text: str) -> int:
    return (len(text) + TOKEN_CHARS - 1) // TOKEN_CHARS


def compact_summary(data: dict[str, Any]) -> dict[str, Any]:
    merged = [c for c in data["gerrit"] if c["status"] == "MERGED"]
    return {
        "ticket": data["ticket"]["key"],
        "parent": data["parent"]["key"],
        "acceptance_count": data["ticket"]["acceptance_count"],
        "merged_related_changes": len(merged),
    }


def build_ptc_request() -> dict[str, Any]:
    """Return the documented Messages API shape for the sample workflow."""
    return {
        "model": PTC_MODEL,
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": (
                "Fetch ticket OP-861, its parent META, and related Gerrit "
                "changes. Return only a compact JSON summary."
            ),
        }],
        "tools": [
            {"type": PTC_TOOL_VERSION, "name": "code_execution"},
            _read_tool("jira_fetch"),
            _read_tool("gerrit_query"),
        ],
    }


def _read_tool(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Read-only {name} helper. Returns JSON.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "allowed_callers": [PTC_TOOL_VERSION],
    }


def classify_catalog() -> dict[str, Any]:
    total = len(TOOL_CATALOG)
    safe = sum(1 for tool in TOOL_CATALOG if tool.ptc_safe)
    by_class: dict[str, int] = {}
    for tool in TOOL_CATALOG:
        by_class[tool.classification] = by_class.get(tool.classification, 0) + 1
    return {
        "total": total,
        "ptc_safe": safe,
        "ptc_safe_fraction": round(safe / total, 3),
        "by_classification": by_class,
        "tools": [asdict(tool) for tool in TOOL_CATALOG],
    }


def compare_workflow(*, ptc_available: bool = True) -> Comparison:
    raw_ticket = json.dumps(SAMPLE_DATA["ticket"], sort_keys=True)
    raw_parent = json.dumps(SAMPLE_DATA["parent"], sort_keys=True)
    raw_changes = json.dumps(SAMPLE_DATA["gerrit"], sort_keys=True)
    raw_all = raw_ticket + raw_parent + raw_changes
    summary = json.dumps(compact_summary(SAMPLE_DATA), sort_keys=True)

    baseline = WorkflowMetrics(
        mode="traditional",
        model_turns=4,
        tool_invocations=3,
        input_tokens=estimate_tokens(raw_all) + 900,
        output_tokens=estimate_tokens(summary) + 240,
        wall_s=round(4 * MODEL_TURN_LATENCY_S + 3 * TOOL_LATENCY_S, 2),
        note="three read tool results enter model-visible context",
    )

    if not ptc_available:
        ptc = WorkflowMetrics(
            mode="ptc-fallback",
            model_turns=baseline.model_turns,
            tool_invocations=baseline.tool_invocations,
            input_tokens=baseline.input_tokens,
            output_tokens=baseline.output_tokens,
            wall_s=baseline.wall_s,
            note=PTC_UNAVAILABLE_REASON,
        )
    else:
        ptc = WorkflowMetrics(
            mode="ptc",
            model_turns=2,
            tool_invocations=3,
            input_tokens=(
                estimate_tokens(PTC_PROCEDURE) + estimate_tokens(summary) + 620
            ),
            output_tokens=estimate_tokens(summary) + 90,
            wall_s=round(
                2 * MODEL_TURN_LATENCY_S
                + 3 * TOOL_LATENCY_S
                + CODE_EXECUTION_OVERHEAD_S,
                2,
            ),
            note=(
                "intermediate reads stay inside code execution; "
                "compact JSON enters context"
            ),
        )

    delta = {
        "input_tokens_saved_pct": _pct_saved(
            baseline.input_tokens, ptc.input_tokens,
        ),
        "output_tokens_saved_pct": _pct_saved(
            baseline.output_tokens, ptc.output_tokens,
        ),
        "wall_s_saved_pct": _pct_saved(baseline.wall_s, ptc.wall_s),
        "model_turns_saved": baseline.model_turns - ptc.model_turns,
    }
    return Comparison(
        workflow="fetch ticket + parent META + related Gerrit changes",
        ptc_available=ptc_available,
        baseline=baseline,
        ptc=ptc,
        delta=delta,
    )


def _pct_saved(before: float, after: float) -> float:
    if before == 0:
        return 0.0
    return round((before - after) / before * 100, 1)


def render_markdown(comparison: Comparison) -> str:
    catalog = classify_catalog()
    safe = catalog["ptc_safe"]
    total = catalog["total"]
    lines = [
        "# OP-861 PTC Offline Comparison",
        "",
        f"Generated: {time.strftime('%Y-%m-%d')}",
        "",
        "## Workflow",
        "",
        comparison.workflow,
        "",
        "## PTC Procedure",
        "",
        "```python",
        PTC_PROCEDURE,
        "```",
        "",
        "## Metrics",
        "",
        "| mode | model turns | tool calls | input tokens | output tokens | wall s | note |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in (comparison.baseline, comparison.ptc):
        lines.append(
            f"| {row.mode} | {row.model_turns} | {row.tool_invocations} | "
            f"{row.input_tokens} | {row.output_tokens} | {row.wall_s:.2f} | {row.note} |"
        )
    lines.extend([
        "",
        "## Delta",
        "",
        json.dumps(comparison.delta, indent=2, sort_keys=True),
        "",
        "## Tool Catalog Summary",
        "",
        f"{safe}/{total} cataloged runner tools are PTC-safe read surfaces.",
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spike_ptc_compare")
    parser.add_argument("--json", help="Optional JSON output path")
    parser.add_argument("--output", help="Optional markdown output path")
    parser.add_argument(
        "--ptc-unavailable",
        action="store_true",
        help="Model fallback path when PTC is unavailable/refused.",
    )
    args = parser.parse_args(argv)

    comparison = compare_workflow(ptc_available=not args.ptc_unavailable)
    payload = {
        "comparison": asdict(comparison),
        "catalog": classify_catalog(),
        "ptc_request": build_ptc_request(),
    }
    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    md = render_markdown(comparison)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
    else:
        print(md, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
