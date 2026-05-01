# nodes

**Purpose**: Defines the LangGraph node functions for OmniSight's multi-agent topology — the orchestrator/router, specialist agents, tool executor, error/retry gate, conversational path, context compression, and final summarizer. Each node receives `GraphState` and returns a partial state update.

**Key types / public surface**:
- `orchestrator_node` — classifies user input as conversational vs. task and routes to a specialist (LLM-based, with keyword fallback).
- `firmware_node` / `software_node` / `validator_node` / `reporter_node` / `reviewer_node` / `general_node` — specialist nodes built by `_specialist_node_factory`; plan tool calls or answer directly.
- `tool_executor_node` — runs queued `ToolCall`s against `TOOL_MAP`, gated by PEP, scoped to workspace, emitting SSE.
- `error_check_node` + `_should_retry` — separates tool-crash retries from verification `[FAIL]` retries, with loop detection and permission auto-fix.
- `conversation_node`, `context_compression_gate`, `summarizer_node` — Q&A path, L2 history compression, and final-answer synthesis.

**Key invariants**:
- Every node degrades to rule-based behavior when no LLM is configured (`_get_llm()` returns None) — the graph must remain functional offline.
- Error strings concatenated into prompts MUST go through `_sanitize_error_for_prompt` and be wrapped in XML tags; this is an explicit prompt-injection defense (C2/M3 audit, 2026-04-19).
- Two independent retry counters: `retry_count` (tool crashed) and `verification_loop_iteration` (tool succeeded but returned `[FAIL]`); tool errors take priority.
- Permission auto-fix has a hard loop-guard at 2 attempts per category (H8); skill lazy-loading caps at `_MAX_SKILL_LOAD_ITERATIONS` (3); error_history is capped at 50 entries.
- `set_active_workspace` must be reset in a `finally` block — leaking workspace context across runs would cross-contaminate agents.

**Cross-module touchpoints**:
- Imports heavily from `backend.agents.{state,tools,llm}`, `backend.prompt_loader`, `backend.events`, `backend.security`, `backend.rag`, `backend.pep_gateway`, `backend.scratchpad`, `backend.rag_prefetch`, `backend.permission_errors`, `backend.llm_errors`, `backend.budget_strategy`.
- Reads live system state via `backend.routers.invoke` (`_agents`, `_tasks`, `record_agent_error`) and `backend.routers.system` (token-freeze, recent logs).
- Consumed by the LangGraph builder (not shown here) that wires these node functions into the agent topology.
