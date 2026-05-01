"""AB.1 — Tool Schema Registry for Anthropic API tool calling.

Central registry of tool schemas used when OmniSight dispatches Claude via
Anthropic Messages API or Batch API. Each ToolSchema defines:

  - name: Anthropic tool name (matches `tool_use.name` in API response)
  - description: human-readable, fed to model
  - input_schema: JSON Schema describing tool input
  - category: for documentation grouping
  - deferred: True = lazy-load via ToolSearch (not eager-included in tools=[])

Two consumers:

  1. Anthropic API client (`anthropic_native_client.py`, AB.2) — calls
     `to_anthropic_tools(["Read", "Edit", ...])` to get `tools=[]` payload
  2. Documentation (`docs/agents/tool-reference.md`) — generated from
     the registry via `python -m backend.agents.tool_schemas --regen-doc`

Adding a new tool:

  1. Build a ToolSchema instance
  2. register_tool(schema) at module load time
  3. python -m backend.agents.tool_schemas --regen-doc
  4. Test test_tool_schemas.py passes

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §2
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ToolCategory = Literal[
    "filesystem",
    "shell",
    "search",
    "web",
    "agent",
    "task",
    "skill",
    "scheduler",
    "plan",
    "worktree",
    "notebook",
    "mcp",
    "meta",
    "skill_hd",
]


class ToolSchema(BaseModel):
    """Single tool definition. Serializable to Anthropic tools=[] payload."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., description="Anthropic tool name")
    description: str = Field(..., description="Human-readable, fed to model")
    input_schema: dict[str, Any] = Field(..., description="JSON Schema for tool input")
    category: ToolCategory = Field(..., description="Grouping for documentation")
    deferred: bool = Field(False, description="Lazy-load via ToolSearch")

    def to_anthropic(self) -> dict[str, Any]:
        """Serialize to Anthropic Messages API tools=[] payload entry."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


_REGISTRY: dict[str, ToolSchema] = {}


def register_tool(schema: ToolSchema) -> ToolSchema:
    """Register a tool schema. Raises if name already taken."""
    if schema.name in _REGISTRY:
        raise ValueError(f"Tool {schema.name!r} already registered")
    _REGISTRY[schema.name] = schema
    return schema


def get_schema(name: str) -> ToolSchema:
    """Look up a registered tool schema. Raises KeyError if not found."""
    return _REGISTRY[name]


def list_schemas(
    category: ToolCategory | None = None, include_deferred: bool = False
) -> list[ToolSchema]:
    """Return all matching schemas, sorted by category then name."""
    result = [s for s in _REGISTRY.values() if include_deferred or not s.deferred]
    if category:
        result = [s for s in result if s.category == category]
    return sorted(result, key=lambda s: (s.category, s.name))


def to_anthropic_tools(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Serialize selected (or all non-deferred) tools to Anthropic tools=[] payload.

    If `names` is None, returns all eager (non-deferred) tools. Specify exact
    names to include a deferred tool subset (e.g., for batch tasks that need
    a specific MCP tool).
    """
    if names is None:
        return [s.to_anthropic() for s in list_schemas()]
    return [_REGISTRY[n].to_anthropic() for n in names]


# ─────────────────────────────────────────────────────────────────
#  Eager tools — always included in default tools=[] payload
# ─────────────────────────────────────────────────────────────────

# === Filesystem ===

register_tool(
    ToolSchema(
        name="Read",
        description=(
            "Read a file from the local filesystem. Supports text, image, PDF, "
            "and Jupyter notebook formats. Use absolute paths. Use `pages` for "
            "PDFs > 10 pages, `offset`+`limit` for large text files."
        ),
        category="filesystem",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to read.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Line number to start reading from (1-indexed).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Max number of lines to read.",
                },
                "pages": {
                    "type": "string",
                    "description": "Page range for PDF files (e.g., '1-5').",
                },
            },
            "required": ["file_path"],
        },
    )
)

register_tool(
    ToolSchema(
        name="Write",
        description=(
            "Write content to a file. Overwrites if file exists. Must Read "
            "existing files first before overwriting. Prefer Edit for "
            "modifying existing files (smaller diff). Never create docs unless "
            "explicitly requested."
        ),
        category="filesystem",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path."},
                "content": {"type": "string", "description": "File content."},
            },
            "required": ["file_path", "content"],
        },
    )
)

register_tool(
    ToolSchema(
        name="Edit",
        description=(
            "Exact string replacement in a file. `old_string` must match "
            "exactly once unless `replace_all=true`. Preserves indentation. "
            "Must Read the file first."
        ),
        category="filesystem",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {
                    "type": "string",
                    "description": "Must differ from old_string.",
                },
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    )
)

# === Shell ===

register_tool(
    ToolSchema(
        name="Bash",
        description=(
            "Execute a shell command. Default 30s timeout (max 600s). Quote "
            "paths with spaces. Avoid using cat/head/tail/sed/awk/echo — use "
            "Read/Edit/Write tools instead. Use `run_in_background` for long "
            "tasks; check via Monitor."
        ),
        category="shell",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "description": {
                    "type": "string",
                    "description": "Active-voice description of what command does.",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 600000,
                    "description": "Timeout in milliseconds.",
                },
                "run_in_background": {"type": "boolean", "default": False},
            },
            "required": ["command"],
        },
    )
)

# === Search ===

register_tool(
    ToolSchema(
        name="Grep",
        description=(
            "Ripgrep-based content search. Supports regex patterns, paths, "
            "globs, type filters, case-insensitive (-i), line numbers (-n), "
            "and three output modes."
        ),
        category="search",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern."},
                "path": {"type": "string", "description": "File or directory."},
                "glob": {"type": "string", "description": "Glob filter."},
                "type": {
                    "type": "string",
                    "description": "File type (e.g., 'py', 'ts').",
                },
                "-i": {"type": "boolean", "description": "Case-insensitive."},
                "-n": {"type": "boolean", "description": "Show line numbers."},
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "default": "files_with_matches",
                },
            },
            "required": ["pattern"],
        },
    )
)

register_tool(
    ToolSchema(
        name="Glob",
        description=(
            "Filename pattern matcher. Returns paths matching the glob "
            "(e.g., 'src/**/*.tsx')."
        ),
        category="search",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
    )
)

# === Agent ===

register_tool(
    ToolSchema(
        name="Agent",
        description=(
            "Spawn a sub-agent for complex multi-step tasks. Use specialized "
            "subagent_type when applicable (Explore for codebase research, "
            "Plan for design, claude-code-guide for Claude Code questions). "
            "Use `isolation: 'worktree'` for risky changes."
        ),
        category="agent",
        input_schema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short (3-5 word) task description.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Self-contained brief for the sub-agent.",
                },
                "subagent_type": {"type": "string"},
                "model": {
                    "type": "string",
                    "enum": ["sonnet", "opus", "haiku"],
                },
                "run_in_background": {"type": "boolean"},
                "isolation": {"type": "string", "enum": ["worktree"]},
            },
            "required": ["description", "prompt"],
        },
    )
)

# === Web ===

register_tool(
    ToolSchema(
        name="WebFetch",
        description=(
            "Fetch URL content and process with LLM-driven prompt. "
            "HTTPS-upgraded automatically. 15-min cache. For GitHub URLs, "
            "prefer `gh` via Bash."
        ),
        category="web",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "prompt": {"type": "string"},
            },
            "required": ["url", "prompt"],
        },
    )
)

# === Tool discovery (meta) ===

register_tool(
    ToolSchema(
        name="ToolSearch",
        description=(
            "Fetch JSON schemas for deferred tools so they can be called. "
            "Use 'select:Tool1,Tool2' for direct selection or keyword query "
            "for fuzzy match."
        ),
        category="meta",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5, "minimum": 1},
            },
            "required": ["query"],
        },
    )
)

# === Skill ===

register_tool(
    ToolSchema(
        name="Skill",
        description=(
            "Execute a registered skill (markdown-based capability). "
            "Skills come from .claude/skills/ + .omnisight/skills/ + bundled "
            "(WP.2 loader, three-scope precedence)."
        ),
        category="skill",
        input_schema={
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Skill name (no leading slash).",
                },
                "args": {"type": "string"},
            },
            "required": ["skill"],
        },
    )
)

# ─────────────────────────────────────────────────────────────────
#  Deferred tools — registered but not in default eager payload.
#  Loaded via ToolSearch on demand to keep tools=[] payload small.
# ─────────────────────────────────────────────────────────────────

register_tool(
    ToolSchema(
        name="WebSearch",
        description="Web search with optional domain allow/block filters.",
        category="web",
        deferred=True,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "allowed_domains": {"type": "array", "items": {"type": "string"}},
                "blocked_domains": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        },
    )
)

register_tool(
    ToolSchema(
        name="ScheduleWakeup",
        description=(
            "Schedule next iteration in dynamic /loop mode. delaySeconds "
            "clamped to [60, 3600]."
        ),
        category="scheduler",
        deferred=True,
        input_schema={
            "type": "object",
            "properties": {
                "delaySeconds": {
                    "type": "number",
                    "minimum": 60,
                    "maximum": 3600,
                },
                "prompt": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["delaySeconds", "prompt", "reason"],
        },
    )
)

# Task management family
for _name, _desc in [
    ("TaskCreate", "Create a background task."),
    ("TaskGet", "Get a task by ID."),
    ("TaskList", "List tasks (optionally filtered)."),
    ("TaskOutput", "Stream stdout/stderr from a task."),
    ("TaskStop", "Stop a running task."),
    ("TaskUpdate", "Update task status (in_progress / completed)."),
]:
    register_tool(
        ToolSchema(
            name=_name,
            description=_desc,
            category="task",
            deferred=True,
            input_schema={"type": "object"},
        )
    )

# Cron family
for _name, _desc in [
    ("CronCreate", "Create a recurring scheduled job."),
    ("CronList", "List scheduled jobs."),
    ("CronDelete", "Delete a scheduled job."),
]:
    register_tool(
        ToolSchema(
            name=_name,
            description=_desc,
            category="scheduler",
            deferred=True,
            input_schema={"type": "object"},
        )
    )

# Plan / Worktree / Notebook / IO meta tools
for _name, _desc, _cat in [
    ("EnterPlanMode", "Enter Claude Code plan mode.", "plan"),
    ("ExitPlanMode", "Exit plan mode with the proposed plan.", "plan"),
    ("EnterWorktree", "Enter an isolated git worktree.", "worktree"),
    ("ExitWorktree", "Exit current worktree.", "worktree"),
    ("NotebookEdit", "Edit a Jupyter notebook cell.", "notebook"),
    ("Monitor", "Stream events from a background process.", "task"),
    ("PushNotification", "Send a push notification.", "task"),
    ("RemoteTrigger", "Trigger a remote OmniSight workflow.", "task"),
    ("AskUserQuestion", "Ask the user a question via UI.", "agent"),
    ("ListMcpResourcesTool", "List MCP server resources.", "mcp"),
    ("ReadMcpResourceTool", "Read an MCP resource.", "mcp"),
]:
    register_tool(
        ToolSchema(
            name=_name,
            description=_desc,
            category=_cat,  # type: ignore[arg-type]
            deferred=True,
            input_schema={"type": "object"},
        )
    )

# ─────────────────────────────────────────────────────────────────
#  OmniSight SKILL_HD_* — placeholder schemas for HD priority skills
#  Full input_schema will be filled in as each HD phase ships.
# ─────────────────────────────────────────────────────────────────

_SKILL_HD_REGISTRY = [
    ("SKILL_HD_PARSE", "Parse an EDA file (KiCad / Altium / OrCAD / etc) into HDIR.", "HD.1"),
    ("SKILL_HD_DIFF_REFERENCE", "Reference vs customer design diff.", "HD.4"),
    ("SKILL_HD_SENSOR_SWAP_FEASIBILITY", "Sensor substitution feasibility.", "HD.5"),
    ("SKILL_HD_FW_SYNC_PATCH", "HW change → FW patch list.", "HD.7"),
    ("SKILL_HD_PCB_SI_ANALYZE", "PCB signal integrity analysis.", "HD.2"),
    ("SKILL_HD_HIL_RUN", "Hardware-in-the-loop session execution.", "HD.8"),
    ("SKILL_HD_RAG_QUERY", "Datasheet RAG retrieval.", "HD.9"),
    ("SKILL_HD_CERT_RETEST_PLAN", "EMC / safety retest plan generator.", "HD.10"),
    ("SKILL_HD_PLATFORM_RESOLVE", "SoC mark → platform spec lookup.", "HD.16"),
    ("SKILL_HD_VENDOR_SYNC", "Vendor SDK upstream sync pipeline.", "HD.16"),
    ("SKILL_HD_VENDOR_REBASE", "Patch rebase conflict auto-attempt.", "HD.16"),
    ("SKILL_HD_NDA_GATE", "NDA boundary enforcement check.", "HD.16"),
    ("SKILL_HD_CUSTOMER_OVERLAY", "Per-customer overlay manifest resolver.", "HD.17"),
    ("SKILL_HD_LIFECYCLE_AUDIT", "Annual reproducibility audit.", "HD.18"),
    ("SKILL_HD_CVE_IMPACT", "CVE feed → SBOM impact analysis.", "HD.18"),
    ("SKILL_HD_CVE_AUTO_BACKPORT", "Vendor patch → customer-overlay backport proposal.", "HD.18"),
    ("SKILL_HD_BRINGUP_CHECKLIST", "SoC-specific bring-up checklist generator.", "HD.19"),
    ("SKILL_HD_BRINGUP_LIVE_PARSE", "Live boot console → AI parse blockers.", "HD.19"),
    ("SKILL_HD_PORT_ADVISOR", "Cross-SoC port required-changes + effort estimate.", "HD.19"),
    ("SKILL_HD_DEVKIT_FORK", "DevKit reference → customer fork starting point.", "HD.19"),
    ("SKILL_HD_ISP_TUNING_DIFF", "ISP tuning binary before/after compare.", "HD.20"),
    ("SKILL_HD_BLOB_COMPAT", "(BSP-version, blob-version) compatibility matrix.", "HD.20"),
    ("SKILL_HD_PRODUCTION_BUNDLE", "EMS production access bundle generator.", "HD.21"),
    ("SKILL_HD_OTA_PACKAGE_GEN", "OTA bundle generation (SWUpdate / RAUC / A-B).", "HD.21"),
    ("SKILL_HD_SBOM_GENERATE", "SBOM CycloneDX + SPDX generation.", "HD.21"),
    ("SKILL_HD_LICENSE_AUDIT", "Ship-time license conflict check.", "HD.21"),
    ("SKILL_HD_AUTHENTICITY_VERIFY", "Chip authenticity challenge / verification.", "HD.21"),
    ("SKILL_HD_AI_COMPANION", "Unified chat surface skill router.", "HD.21"),
]

for _name, _desc, _phase in _SKILL_HD_REGISTRY:
    register_tool(
        ToolSchema(
            name=_name,
            description=f"[{_phase}] {_desc} (placeholder; input_schema fills as phase ships)",
            category="skill_hd",
            deferred=True,
            input_schema={"type": "object"},
        )
    )


# ─────────────────────────────────────────────────────────────────
#  Documentation generation
# ─────────────────────────────────────────────────────────────────


def generate_markdown_reference() -> str:
    """Generate `docs/agents/tool-reference.md` content from the registry."""
    eager_count = sum(1 for s in _REGISTRY.values() if not s.deferred)
    deferred_count = sum(1 for s in _REGISTRY.values() if s.deferred)
    skill_hd_count = sum(1 for s in _REGISTRY.values() if s.category == "skill_hd")

    lines = [
        "# OmniSight Tool Reference",
        "",
        "> **Auto-generated from `backend/agents/tool_schemas.py`. Do NOT edit by hand.**",
        ">",
        "> Run `python -m backend.agents.tool_schemas --regen-doc` to refresh.",
        "",
        f"**Totals**: {len(_REGISTRY)} tools  ·  {eager_count} eager  ·  "
        f"{deferred_count} deferred (lazy-load via ToolSearch)  ·  "
        f"{skill_hd_count} HD skills (placeholder).",
        "",
        "**Conventions**:",
        "",
        "- *eager* tools are always included in the default Anthropic `tools=[]` payload",
        "- *deferred* tools must be explicitly requested via `ToolSearch` or by name",
        "- HD skills are `category: skill_hd`, deferred, and become `input_schema`-rich",
        "  as their owning HD phase (HD.1 - HD.21) ships",
        "",
        "ADR: [`docs/operations/anthropic-api-migration-and-batch-mode.md`]"
        "(../operations/anthropic-api-migration-and-batch-mode.md) §2.",
        "",
        "---",
        "",
    ]

    by_category: dict[str, list[ToolSchema]] = {}
    for s in _REGISTRY.values():
        by_category.setdefault(s.category, []).append(s)

    category_titles = {
        "filesystem": "Filesystem",
        "shell": "Shell",
        "search": "Search",
        "agent": "Agent",
        "web": "Web",
        "meta": "Meta / Tool Discovery",
        "skill": "Skill",
        "scheduler": "Scheduler",
        "task": "Task Management",
        "plan": "Plan Mode",
        "worktree": "Git Worktree",
        "notebook": "Notebook",
        "mcp": "MCP",
        "skill_hd": "HD Skills (Placeholder)",
    }

    for cat in sorted(by_category):
        title = category_titles.get(cat, cat.title())
        lines.append(f"## {title}")
        lines.append("")
        for schema in sorted(by_category[cat], key=lambda s: s.name):
            badge = "  *(deferred)*" if schema.deferred else ""
            lines.append(f"### `{schema.name}`{badge}")
            lines.append("")
            lines.append(schema.description)
            lines.append("")
            if schema.input_schema and schema.input_schema != {"type": "object"}:
                lines.append("**Input schema**:")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(schema.input_schema, indent=2))
                lines.append("```")
                lines.append("")
            else:
                lines.append("*Input schema TBD (placeholder).*")
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────────
#  CLI entry point
# ─────────────────────────────────────────────────────────────────


def _main() -> None:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="OmniSight tool schema registry CLI."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all registered tools by category.",
    )
    parser.add_argument(
        "--regen-doc",
        action="store_true",
        help="Regenerate docs/agents/tool-reference.md from registry.",
    )
    parser.add_argument(
        "--check-doc",
        action="store_true",
        help="Verify docs/agents/tool-reference.md matches current registry "
        "(exit 1 on drift).",
    )
    args = parser.parse_args()

    if args.list:
        for s in list_schemas(include_deferred=True):
            mark = " [deferred]" if s.deferred else ""
            print(f"{s.category:12s}  {s.name}{mark}")

    if args.regen_doc or args.check_doc:
        repo_root = Path(__file__).resolve().parents[2]
        doc_path = repo_root / "docs" / "agents" / "tool-reference.md"
        new_content = generate_markdown_reference()

        if args.check_doc:
            if not doc_path.exists():
                print(f"ERROR: {doc_path} does not exist. Run --regen-doc.")
                raise SystemExit(1)
            old_content = doc_path.read_text()
            if old_content != new_content:
                print(f"ERROR: {doc_path} is out of sync with registry.")
                print("Run: python -m backend.agents.tool_schemas --regen-doc")
                raise SystemExit(1)
            print(f"OK: {doc_path} matches registry.")
            return

        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(new_content)
        print(f"Wrote {doc_path}  ({len(new_content):,} bytes)")


if __name__ == "__main__":
    _main()
