# OmniSight Tool Reference

> **Auto-generated from `backend/agents/tool_schemas.py`. Do NOT edit by hand.**
>
> Run `python -m backend.agents.tool_schemas --regen-doc` to refresh.

**Totals**: 60 tools  ·  10 eager  ·  50 deferred (lazy-load via ToolSearch)  ·  28 HD skills (placeholder).

**Conventions**:

- *eager* tools are always included in the default Anthropic `tools=[]` payload
- *deferred* tools must be explicitly requested via `ToolSearch` or by name
- HD skills are `category: skill_hd`, deferred, and become `input_schema`-rich
  as their owning HD phase (HD.1 - HD.21) ships

ADR: [`docs/operations/anthropic-api-migration-and-batch-mode.md`](../operations/anthropic-api-migration-and-batch-mode.md) §2.

---

## Agent

### `Agent`

Spawn a sub-agent for complex multi-step tasks. Use specialized subagent_type when applicable (Explore for codebase research, Plan for design, claude-code-guide for Claude Code questions). Use `isolation: 'worktree'` for risky changes.

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "description": {
      "type": "string",
      "description": "Short (3-5 word) task description."
    },
    "prompt": {
      "type": "string",
      "description": "Self-contained brief for the sub-agent."
    },
    "subagent_type": {
      "type": "string"
    },
    "model": {
      "type": "string",
      "enum": [
        "sonnet",
        "opus",
        "haiku"
      ]
    },
    "run_in_background": {
      "type": "boolean"
    },
    "isolation": {
      "type": "string",
      "enum": [
        "worktree"
      ]
    }
  },
  "required": [
    "description",
    "prompt"
  ]
}
```

### `AskUserQuestion`  *(deferred)*

Ask the user a question via UI.

*Input schema TBD (placeholder).*

## Filesystem

### `Edit`

Exact string replacement in a file. `old_string` must match exactly once unless `replace_all=true`. Preserves indentation. Must Read the file first.

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": "string"
    },
    "old_string": {
      "type": "string"
    },
    "new_string": {
      "type": "string",
      "description": "Must differ from old_string."
    },
    "replace_all": {
      "type": "boolean",
      "default": false
    }
  },
  "required": [
    "file_path",
    "old_string",
    "new_string"
  ]
}
```

### `Read`

Read a file from the local filesystem. Supports text, image, PDF, and Jupyter notebook formats. Use absolute paths. Use `pages` for PDFs > 10 pages, `offset`+`limit` for large text files.

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": "string",
      "description": "Absolute path to the file to read."
    },
    "offset": {
      "type": "integer",
      "minimum": 0,
      "description": "Line number to start reading from (1-indexed)."
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "description": "Max number of lines to read."
    },
    "pages": {
      "type": "string",
      "description": "Page range for PDF files (e.g., '1-5')."
    }
  },
  "required": [
    "file_path"
  ]
}
```

### `Write`

Write content to a file. Overwrites if file exists. Must Read existing files first before overwriting. Prefer Edit for modifying existing files (smaller diff). Never create docs unless explicitly requested.

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": "string",
      "description": "Absolute path."
    },
    "content": {
      "type": "string",
      "description": "File content."
    }
  },
  "required": [
    "file_path",
    "content"
  ]
}
```

## MCP

### `ListMcpResourcesTool`  *(deferred)*

List MCP server resources.

*Input schema TBD (placeholder).*

### `ReadMcpResourceTool`  *(deferred)*

Read an MCP resource.

*Input schema TBD (placeholder).*

## Meta / Tool Discovery

### `ToolSearch`

Fetch JSON schemas for deferred tools so they can be called. Use 'select:Tool1,Tool2' for direct selection or keyword query for fuzzy match.

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string"
    },
    "max_results": {
      "type": "integer",
      "default": 5,
      "minimum": 1
    }
  },
  "required": [
    "query"
  ]
}
```

## Notebook

### `NotebookEdit`  *(deferred)*

Edit a Jupyter notebook cell.

*Input schema TBD (placeholder).*

## Plan Mode

### `EnterPlanMode`  *(deferred)*

Enter Claude Code plan mode.

*Input schema TBD (placeholder).*

### `ExitPlanMode`  *(deferred)*

Exit plan mode with the proposed plan.

*Input schema TBD (placeholder).*

## Scheduler

### `CronCreate`  *(deferred)*

Create a recurring scheduled job.

*Input schema TBD (placeholder).*

### `CronDelete`  *(deferred)*

Delete a scheduled job.

*Input schema TBD (placeholder).*

### `CronList`  *(deferred)*

List scheduled jobs.

*Input schema TBD (placeholder).*

### `ScheduleWakeup`  *(deferred)*

Schedule next iteration in dynamic /loop mode. delaySeconds clamped to [60, 3600].

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "delaySeconds": {
      "type": "number",
      "minimum": 60,
      "maximum": 3600
    },
    "prompt": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    }
  },
  "required": [
    "delaySeconds",
    "prompt",
    "reason"
  ]
}
```

## Search

### `Glob`

Filename pattern matcher. Returns paths matching the glob (e.g., 'src/**/*.tsx').

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "pattern": {
      "type": "string"
    },
    "path": {
      "type": "string"
    }
  },
  "required": [
    "pattern"
  ]
}
```

### `Grep`

Ripgrep-based content search. Supports regex patterns, paths, globs, type filters, case-insensitive (-i), line numbers (-n), and three output modes.

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "pattern": {
      "type": "string",
      "description": "Regex pattern."
    },
    "path": {
      "type": "string",
      "description": "File or directory."
    },
    "glob": {
      "type": "string",
      "description": "Glob filter."
    },
    "type": {
      "type": "string",
      "description": "File type (e.g., 'py', 'ts')."
    },
    "-i": {
      "type": "boolean",
      "description": "Case-insensitive."
    },
    "-n": {
      "type": "boolean",
      "description": "Show line numbers."
    },
    "output_mode": {
      "type": "string",
      "enum": [
        "content",
        "files_with_matches",
        "count"
      ],
      "default": "files_with_matches"
    }
  },
  "required": [
    "pattern"
  ]
}
```

## Shell

### `Bash`

Execute a shell command. Default 30s timeout (max 600s). Quote paths with spaces. Avoid using cat/head/tail/sed/awk/echo — use Read/Edit/Write tools instead. Use `run_in_background` for long tasks; check via Monitor.

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "command": {
      "type": "string"
    },
    "description": {
      "type": "string",
      "description": "Active-voice description of what command does."
    },
    "timeout": {
      "type": "integer",
      "minimum": 1000,
      "maximum": 600000,
      "description": "Timeout in milliseconds."
    },
    "run_in_background": {
      "type": "boolean",
      "default": false
    }
  },
  "required": [
    "command"
  ]
}
```

## Skill

### `Skill`

Execute a registered skill (markdown-based capability). Skills come from .claude/skills/ + .omnisight/skills/ + bundled (WP.2 loader, three-scope precedence).

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "skill": {
      "type": "string",
      "description": "Skill name (no leading slash)."
    },
    "args": {
      "type": "string"
    }
  },
  "required": [
    "skill"
  ]
}
```

## HD Skills (Placeholder)

### `SKILL_HD_AI_COMPANION`  *(deferred)*

[HD.21] Unified chat surface skill router. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_AUTHENTICITY_VERIFY`  *(deferred)*

[HD.21] Chip authenticity challenge / verification. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_BLOB_COMPAT`  *(deferred)*

[HD.20] (BSP-version, blob-version) compatibility matrix. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_BRINGUP_CHECKLIST`  *(deferred)*

[HD.19] SoC-specific bring-up checklist generator. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_BRINGUP_LIVE_PARSE`  *(deferred)*

[HD.19] Live boot console → AI parse blockers. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_CERT_RETEST_PLAN`  *(deferred)*

[HD.10] EMC / safety retest plan generator. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_CUSTOMER_OVERLAY`  *(deferred)*

[HD.17] Per-customer overlay manifest resolver. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_CVE_AUTO_BACKPORT`  *(deferred)*

[HD.18] Vendor patch → customer-overlay backport proposal. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_CVE_IMPACT`  *(deferred)*

[HD.18] CVE feed → SBOM impact analysis. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_DEVKIT_FORK`  *(deferred)*

[HD.19] DevKit reference → customer fork starting point. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_DIFF_REFERENCE`  *(deferred)*

[HD.4] Reference vs customer design diff. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_FW_SYNC_PATCH`  *(deferred)*

[HD.7] HW change → FW patch list. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_HIL_RUN`  *(deferred)*

[HD.8] Hardware-in-the-loop session execution. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_ISP_TUNING_DIFF`  *(deferred)*

[HD.20] ISP tuning binary before/after compare. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_LICENSE_AUDIT`  *(deferred)*

[HD.21] Ship-time license conflict check. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_LIFECYCLE_AUDIT`  *(deferred)*

[HD.18] Annual reproducibility audit. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_NDA_GATE`  *(deferred)*

[HD.16] NDA boundary enforcement check. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_OTA_PACKAGE_GEN`  *(deferred)*

[HD.21] OTA bundle generation (SWUpdate / RAUC / A-B). (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_PARSE`  *(deferred)*

[HD.1] Parse an EDA file (KiCad / Altium / OrCAD / etc) into HDIR. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_PCB_SI_ANALYZE`  *(deferred)*

[HD.2] PCB signal integrity analysis. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_PLATFORM_RESOLVE`  *(deferred)*

[HD.16] SoC mark → platform spec lookup. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_PORT_ADVISOR`  *(deferred)*

[HD.19] Cross-SoC port required-changes + effort estimate. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_PRODUCTION_BUNDLE`  *(deferred)*

[HD.21] EMS production access bundle generator. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_RAG_QUERY`  *(deferred)*

[HD.9] Datasheet RAG retrieval. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_SBOM_GENERATE`  *(deferred)*

[HD.21] SBOM CycloneDX + SPDX generation. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_SENSOR_SWAP_FEASIBILITY`  *(deferred)*

[HD.5] Sensor substitution feasibility. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_VENDOR_REBASE`  *(deferred)*

[HD.16] Patch rebase conflict auto-attempt. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

### `SKILL_HD_VENDOR_SYNC`  *(deferred)*

[HD.16] Vendor SDK upstream sync pipeline. (placeholder; input_schema fills as phase ships)

*Input schema TBD (placeholder).*

## Task Management

### `Monitor`  *(deferred)*

Stream events from a background process.

*Input schema TBD (placeholder).*

### `PushNotification`  *(deferred)*

Send a push notification.

*Input schema TBD (placeholder).*

### `RemoteTrigger`  *(deferred)*

Trigger a remote OmniSight workflow.

*Input schema TBD (placeholder).*

### `TaskCreate`  *(deferred)*

Create a background task.

*Input schema TBD (placeholder).*

### `TaskGet`  *(deferred)*

Get a task by ID.

*Input schema TBD (placeholder).*

### `TaskList`  *(deferred)*

List tasks (optionally filtered).

*Input schema TBD (placeholder).*

### `TaskOutput`  *(deferred)*

Stream stdout/stderr from a task.

*Input schema TBD (placeholder).*

### `TaskStop`  *(deferred)*

Stop a running task.

*Input schema TBD (placeholder).*

### `TaskUpdate`  *(deferred)*

Update task status (in_progress / completed).

*Input schema TBD (placeholder).*

## Web

### `WebFetch`

Fetch URL content and process with LLM-driven prompt. HTTPS-upgraded automatically. 15-min cache. For GitHub URLs, prefer `gh` via Bash.

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "format": "uri"
    },
    "prompt": {
      "type": "string"
    }
  },
  "required": [
    "url",
    "prompt"
  ]
}
```

### `WebSearch`  *(deferred)*

Web search with optional domain allow/block filters.

**Input schema**:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string"
    },
    "allowed_domains": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "blocked_domains": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": [
    "query"
  ]
}
```

## Git Worktree

### `EnterWorktree`  *(deferred)*

Enter an isolated git worktree.

*Input schema TBD (placeholder).*

### `ExitWorktree`  *(deferred)*

Exit current worktree.

*Input schema TBD (placeholder).*
