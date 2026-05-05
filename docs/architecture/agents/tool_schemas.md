# tool_schemas

**Purpose**: Central registry of tool schemas that OmniSight ships to the Anthropic Messages/Batch API as the `tools=[]` payload. Also the source of truth for the auto-generated tool reference doc.

**Key types / public surface**:
- `TOOL_SCHEMA_VERSION` — semver string for ToolSearch lazy-load schema responses.
- `ToolSchema` — frozen Pydantic model (name, description, input_schema, category, deferred, schema_version) with `.to_anthropic()` and `.to_toolsearch()` serializers.
- `register_tool(schema)` — module-load-time registration; rejects duplicate names.
- `to_anthropic_tools(names=None)` — produces the API payload; defaults to all eager (non-deferred) tools.
- `to_toolsearch_schemas(names)` — produces the versioned lazy-load payload for deferred tools only.
- `list_schemas(category, include_deferred)` / `get_schema(name)` — registry lookup.
- `generate_markdown_reference()` + `_main()` CLI (`--list`, `--regen-doc`, `--check-doc`, `--validate-schemas`).

**Key invariants**:
- "Deferred" tools are registered but excluded from the default eager payload — they're only sent when explicitly named, typically after a `ToolSearch` call. This keeps the default `tools=[]` small.
- ToolSearch lazy-load responses carry both a registry-level `schema_version` and per-tool `schema_version`; the Anthropic `tools=[]` payload deliberately omits this metadata.
- `ToolSchema` is frozen; the `_REGISTRY` dict is module-global and populated at import time, so importing this module has side effects (every `register_tool` call runs).
- `docs/agents/tool-reference.md` must stay in sync with the registry; `--check-doc` is presumably wired into CI and exits 1 on drift.
- `SKILL_HD_*` entries are deliberate placeholders with `input_schema={"type": "object"}` — the validator (`_validate_schemas`) tolerates this shape, and they're filled in as each HD phase (HD.1–HD.21) ships.

**Cross-module touchpoints**:
- Consumed by `backend/agents/anthropic_native_client.py` (AB.2) for building API requests.
- Consumed by `ToolSearch` runtime (not in this file) to lazy-surface deferred schemas to the model.
- No imports from other backend modules — pure stdlib + Pydantic, making it safe to import early.
