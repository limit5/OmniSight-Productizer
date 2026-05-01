# state

**Purpose**: Defines the shared Pydantic state schema (`GraphState`) that flows through the LangGraph agent topology, plus the small value types (actions, tool calls, tool results) that nodes pass around.

**Key types / public surface**:
- `GraphState` — top-level per-run state object threaded through every graph node.
- `AgentAction` — structured action the API layer should execute (spawn/assign/status/exec/report).
- `ToolCall` — agent-requested tool invocation with name + arguments dict.
- `ToolResult` — tool execution outcome (output text + success flag).

**Key invariants**:
- `messages` uses LangGraph's `add_messages` reducer (append-merge); other list fields use the default REPLACE reducer — notably `error_history` is intentionally REPLACE because `error_check_node` returns the full accumulated list each time.
- `size` is declared **twice** in the class body (both default `"M"`); the second declaration wins. Likely a merge/rebase artefact worth cleaning up.
- `size` defaults to `"M"` deliberately so call sites that bypass the BP.C.1 sizer keep the legacy standard-DAG topology byte-for-byte while the `OMNISIGHT_TOPOLOGY_MODE` flag gates rollout.
- `sandbox_tier` defaults to the most restrictive `"t1"`; unknown values are documented as collapsing to `t1` (collapse logic lives elsewhere, not here).
- State is per-run and never shared across workers — no module-level mutable state, per the SOP audit comments.

**Cross-module touchpoints**:
- Imports `BaseMessage` and `add_messages` from `backend.llm_adapter`.
- Comments reference consumers/producers: `backend.t_shirt_sizer`, `backend.graph_topology`, `backend.agents.graph`, `backend.web.clone_spec_context`, and the conversation/error-check nodes — but this module itself only defines schema.
