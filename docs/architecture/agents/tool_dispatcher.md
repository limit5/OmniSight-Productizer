# tool_dispatcher

**Purpose**: Routes Anthropic `tool_use` blocks to backend Python handlers, executing them and packaging results into the `tool_result` shape Anthropic expects. Acts as the central tool-execution layer for both the live multi-turn loop and the AB.4 batch dispatcher.

**Key types / public surface**:
- `ToolDispatcher` — class holding the handler registry and `execute()` method.
- `ToolResult` — frozen dataclass with `to_anthropic_block()` for serialization.
- `register_handler(tool_name)` — decorator registering into the module-level default dispatcher.
- `get_default_dispatcher()` — accessor for the singleton dispatcher.
- `Handler` type alias — sync or async `(dict) -> Any`.

**Key invariants**:
- `execute()` never raises; all failures (missing handler, handler exception, etc.) become `ToolResult(is_error=True)` with structured JSON content, so the LLM can self-correct.
- Registration validates the tool name against `tool_schemas.get_schema()` — you must add the schema *before* registering a handler, or registration raises `ValueError`.
- Sync handlers are dispatched via `run_in_executor` to keep the event loop unblocked; async handlers are awaited directly.
- Result normalisation: `str` passes through, `None` → `""`, everything else goes through `json.dumps(..., default=str)` with a `str(raw)` fallback. Handlers should return JSON-serializable values to avoid lossy coercion.
- Double-registration of a tool name raises — there is one global default dispatcher and no override path.

**Cross-module touchpoints**:
- Imports `get_schema` from `backend.agents.tool_schemas` for the registration drift guard.
- Called by `backend.agents.anthropic_native_client.run_with_tools()` (per docstring) and the AB.4 batch dispatcher; the actual handler implementations live in other modules that import `register_handler`.
