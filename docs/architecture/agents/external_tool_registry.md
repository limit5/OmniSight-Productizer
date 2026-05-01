# external_tool_registry

**Purpose**: Wires non-Anthropic tools (MCP servers, subprocess CLIs, Docker sidecars, Python libs) into OmniSight's Anthropic batch dispatcher (AB.4), pairing static code-side metadata with operator-supplied deploy-time bindings and per-task-type tool subsets.

**Key types / public surface**:
- `ExternalToolDefinition` — frozen dataclass declaring tool name, integration type, license tier, sandbox flag, default config; enforces license boundaries in `__post_init__`.
- `ExternalToolRegistry` — operator-facing entry point: `seed_default_bindings()`, `build_handler(tool_name)`, `list_for_task_kind(...)`.
- `HANDLER_CLASSES` — dict mapping the 4 `IntegrationType` literals to `PythonLibHandler` / `SubprocessHandler` / `DockerMCPHandler` / `DockerSidecarHandler`.
- `DEFAULT_TOOL_DEFINITIONS` — 7 pre-declared tools (KiCadMCP, Altium2KiCad, OdbDesign, VisionParse, SKiDL, PyFDT, LDParser).
- `tools_for_task_kind(task_kind)` / `TASK_KIND_DISPATCH` — returns the small tool subset Anthropic should see per task kind; falls back to `generic_dev` with a warning.

**Key invariants**:
- License boundary is structural, not advisory: GPL ⇒ `subprocess`, AGPL ⇒ `docker_sidecar`, and both must set `sandbox_required=True` — otherwise `LicenseBoundaryViolation` at definition construction.
- `DockerMCPHandler` and `DockerSidecarHandler` are **contract stubs** that return `{"status": "deferred"}`; real container/HTTP wiring not implemented yet. KiCad MCP read-only whitelist (R55) blocks methods prefixed `write_/create_/delete_/edit_/modify_`.
- `list_for_task_kind` passes through any name not in the registry as a presumed Claude Code built-in (Read, Bash, etc.) without enabling-checks — typos here are silently forwarded to Anthropic.
- Persistence is in-memory only; PG store and health-check poller deliberately deferred (per docstring) until AB.6/AB.7 land.

**Cross-module touchpoints**:
- Consumed by the AB.4 Anthropic batch dispatcher (referenced in module docstring; not imported here).
- Mirrors the `external_tool_registry` table from alembic migration 0184 — schema source of truth lives in DB layer, not visible in this module.
- Tool subsets reference skill names (`SKILL_HD_*`) presumably defined elsewhere; this module never imports or validates them.
