# anthropic_native_client

**Purpose**: Direct `anthropic` SDK wrapper that bypasses LangChain for performance-sensitive Anthropic-specific paths (AB.3 batch submission, AB.4 real-time dispatcher). Provides a stateless client with one-shot, batch-params, and full multi-turn tool-use loop entry points.

**Key types / public surface**:
- `AnthropicClient` — main class; holds SDK handle, default model, and a `ToolDispatcher`.
- `AnthropicClient.simple()` — one-shot prompt → `(text, TokenUsage)`, no tools.
- `AnthropicClient.simple_params()` — builds a params dict for `messages.batches.create()` (AB.3).
- `AnthropicClient.run_with_tools()` — async multi-turn loop executing tool_use blocks until `end_turn` or `max_iterations`.
- `RunResult`, `TokenUsage` — frozen dataclasses returned to callers; `TokenUsage` supports `+` for aggregation.

**Key invariants**:
- Hard cap of 25 iterations on the tool loop; exceeding it returns `stop_reason="max_iterations_exceeded"` rather than raising — callers must check this.
- Prompt caching tags only the **last** system block and **last** tool definition with `cache_control: ephemeral`; assumes ~5 min TTL and stable system+tools prefix to get the 90% discount from turn 2 onward.
- Tool errors are not raised — they come back as `is_error=True` tool_result blocks so the model can self-correct.
- All tool_use blocks in a single assistant turn are resolved and returned in one user message (Anthropic's multi-tool convention).
- `anthropic` SDK is lazily imported inside `__init__` to keep it out of unrelated import graphs and ease monkeypatching.

**Cross-module touchpoints**:
- Imports `ToolDispatcher` / `get_default_dispatcher` from `backend.agents.tool_dispatcher` and `to_anthropic_tools` from `backend.agents.tool_schemas`.
- Docstrings reference AB.3 batch dispatcher and AB.5.6 `RemoteMCPRegistry.to_anthropic_mcp_servers()` as upstream callers; those modules are not imported here directly.
- Positioned alongside (not replacing) `backend.llm_adapter.build_chat_model("anthropic")`, which remains the multi-provider path.
