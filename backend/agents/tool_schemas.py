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
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.agents import skills_loader

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
            "String replacement in a file using the WP.3 diff-validation "
            "cascade for unique edits. `old_string` must match exactly once "
            "unless `replace_all=true`; fuzzy fallback requires at least "
            "three context lines. Must Read the file first."
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

register_tool(
    ToolSchema(
        name="KnowledgeRetrieval",
        description=(
            "Semantic retrieval over the internal workspace RAG index. Returns "
            "top-K relevant chunks with citations containing source path, line "
            "range, and similarity score."
        ),
        category="search",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language or code search query.",
                },
                "tenant_id": {
                    "type": "string",
                    "description": (
                        "Tenant scope. Defaults to OMNISIGHT_RAG_TENANT_ID "
                        "when omitted."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                    "description": "Maximum number of chunks to return.",
                },
                "source_path": {
                    "type": "string",
                    "description": "Optional repo-relative path filter.",
                },
                "metadata_filter": {
                    "type": "object",
                    "description": "Optional exact metadata filter.",
                },
            },
            "required": ["query"],
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

def _load_hd_skill_schemas(project_root: Path) -> list[ToolSchema]:
    """Load BP.B Guild HD skill schemas from the WP.2 skill loader.

    Module-global state audit: import-time schemas are derived from bundled
    repo files, so every worker computes the same registry from the same
    checkout without sharing mutable process state.
    """
    registry = skills_loader.load_default_scopes(
        project_root,
        home=Path("/__omnisight_no_home_skills_for_tool_schemas__"),
    )
    return [
        ToolSchema(
            name=skill.name,
            description=(
                f"{skill.description} "
                "(placeholder; input_schema fills as phase ships)"
            ),
            category="skill_hd",
            deferred=True,
            input_schema={"type": "object"},
        )
        for skill in registry.list_all()
        if skill.name.startswith("SKILL_HD_")
    ]


for _schema in _load_hd_skill_schemas(Path(__file__).resolve().parents[2]):
    register_tool(
        _schema
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


def _validate_schemas() -> int:
    """AB.10.5 — drift detection on registered tool schemas.

    Validates that every entry has a well-formed JSON Schema for its
    ``input_schema``: must be ``{"type": "object", ...}`` shape that
    Anthropic ``tools=[]`` accepts. Returns count of validation
    errors (0 == clean).

    Catches: typo in property names, missing top-level "type",
    non-object schemas, malformed property dicts.
    """
    errors: list[str] = []
    for schema in _REGISTRY.values():
        s = schema.input_schema
        if not isinstance(s, dict):
            errors.append(f"{schema.name}: input_schema must be a dict, got {type(s).__name__}")
            continue
        if s.get("type") != "object":
            errors.append(
                f"{schema.name}: input_schema.type must be 'object', got {s.get('type')!r}"
            )
        # Properties must be a dict if present
        if "properties" in s and not isinstance(s["properties"], dict):
            errors.append(
                f"{schema.name}: input_schema.properties must be dict, "
                f"got {type(s['properties']).__name__}"
            )
        # Required must be a list of strings if present
        if "required" in s:
            if not isinstance(s["required"], list):
                errors.append(
                    f"{schema.name}: input_schema.required must be list, "
                    f"got {type(s['required']).__name__}"
                )
            elif not all(isinstance(r, str) for r in s["required"]):
                errors.append(
                    f"{schema.name}: input_schema.required entries must be strings"
                )
            else:
                # Required names must exist in properties (where applicable)
                props = s.get("properties", {})
                if isinstance(props, dict):
                    for r in s["required"]:
                        if r not in props:
                            errors.append(
                                f"{schema.name}: required field {r!r} "
                                "not declared in properties"
                            )
        # Each property entry must itself be a dict with "type"
        props = s.get("properties", {})
        if isinstance(props, dict):
            for prop_name, prop_def in props.items():
                if not isinstance(prop_def, dict):
                    errors.append(
                        f"{schema.name}.properties.{prop_name}: must be dict"
                    )
                    continue
                if "type" not in prop_def and "enum" not in prop_def:
                    errors.append(
                        f"{schema.name}.properties.{prop_name}: missing 'type' or 'enum'"
                    )

    if errors:
        print("Schema validation errors:")
        for e in errors:
            print(f"  - {e}")
        print(f"\nTotal: {len(errors)} error(s)")
    else:
        print(f"OK: all {len(_REGISTRY)} registered tool schemas validated cleanly.")
    return len(errors)


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
    parser.add_argument(
        "--validate-schemas",
        action="store_true",
        help="AB.10.5 — validate every registered ToolSchema has a "
        "well-formed JSON Schema input_schema (exit 1 on errors).",
    )
    args = parser.parse_args()

    if args.list:
        for s in list_schemas(include_deferred=True):
            mark = " [deferred]" if s.deferred else ""
            print(f"{s.category:12s}  {s.name}{mark}")

    if args.validate_schemas:
        if _validate_schemas() > 0:
            raise SystemExit(1)

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
