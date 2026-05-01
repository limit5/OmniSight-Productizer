# graph

**Purpose**: Defines and compiles the LangGraph topology that wires the OmniSight multi-agent pipeline — orchestrator → specialist → tool executor → error check → summarizer — and exposes a single async entry point (`run_graph`) for executing it on a user command.

**Key types / public surface**:
- `build_graph()` — constructs and compiles the `StateGraph` with all nodes and edges.
- `agent_graph` — singleton compiled graph used by `run_graph`.
- `run_graph(...)` — async entry point that builds initial `GraphState`, invokes the graph with a timeout, and returns the final state.
- `GRAPH_TIMEOUT` — hard 300s cap on a single graph execution.
- `_route_after_orchestrator` / `_check_tool_calls` — conditional-edge routers (private but they encode the routing contract).

**Key invariants**:
- `_VALID_SPECIALISTS` is the authoritative set of routable specialists; any `state.routed_to` outside it silently falls back to `"general"`.
- The error-check stage feeds back to specialists for self-healing retries; its `"summarizer"` branch actually routes to `context_gate` first, not directly to the summarizer.
- All paths converge on `context_gate → summarizer → END` — including conversational mode, which skips tool execution entirely.
- On `asyncio.TimeoutError`, `run_graph` returns a synthetic `GraphState` with `last_error` set rather than raising; callers must check this.
- Empty `soc_vendor`/`sdk_version` intentionally keep the SDK hard-lock gate permissive (Phase 67-E note).

**Cross-module touchpoints**:
- Imports graph primitives from `backend.llm_adapter` (`StateGraph`, `END`, `HumanMessage`) and all node implementations from `backend.agents.nodes`.
- State schema comes from `backend.agents.state.GraphState`.
- Presumably invoked by higher-level task/runner code (not visible in this module).
