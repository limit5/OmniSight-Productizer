# OP-1286 Anthropic native client refactor proposal

## Context

`backend/agents/anthropic_native_client.py` is the Anthropic-specific fast path
for one-shot prompts, batch request params, and the real-time tool loop. The
module is intentionally narrow, but it now carries several concerns in one
place: SDK construction, prompt-cache shaping, stable-vs-beta namespace
selection, request payload assembly, transcript capture, tool execution, and
stale-memory refresh injection.

Two small refactors would reduce coupling without changing the public
`AnthropicClient` API:

1. Add a private request-builder helper shared by `simple()`, `simple_params()`,
   and `run_with_tools()`. These paths all construct the same Anthropic
   `messages.create()` shape: model, max tokens, temperature, messages, optional
   system, optional tools, and optional `mcp_servers`. A helper such as
   `_build_message_kwargs(...)` would keep cache-control and beta-namespace
   eligibility inputs in one place while preserving the existing fallback path
   in `_create_message_with_cache_fallback()`.
2. Move stale-refresh message injection into a private pure-ish helper that
   returns the updated content block and transcript entry instead of mutating
   `messages` and `transcript` directly inside `AnthropicClient`. The current
   `_maybe_inject_stale_refresh()` mixes policy checks, file selection, file
   read, message-content normalization, and transcript append. Splitting the
   content normalization into a helper like `_append_text_block_to_user_message()`
   would make the loop easier to read and easier to test without changing the
   refresh policy from `backend.agents.stale_refresh_strategy`.

## Non-goals

- No behavior change to prompt caching, cache-control fallback, or beta messages
  namespace selection.
- No change to `AnthropicClient.simple()`, `simple_params()`, `run_with_tools()`,
  `TokenUsage`, or `RunResult` public signatures.
- No change to `ToolDispatcher`, tool schemas, MCP registry behavior, or stale
  refresh strategy thresholds.
- No database, security, frontend, devops, embedded, tooling, or production
  dependency changes.

## Follow-up implementation ticket draft

Summary: Refactor Anthropic native client request building and stale-refresh
helpers

Areas: backend, tests

Tier: S

Description:

Implement the two OP-1286 proposal refactors in
`backend/agents/anthropic_native_client.py`:

- Add a private request-builder helper for Anthropic message kwargs and use it
  from `simple()`, `simple_params()`, and `run_with_tools()` without changing
  public method signatures or returned payload shape.
- Extract stale-refresh user-message content normalization from
  `_maybe_inject_stale_refresh()` into a private helper so the refresh method is
  responsible for policy and file selection, not direct content mutation.
- Preserve cache-control placement, cache fallback stripping, stable-vs-beta
  message namespace selection, transcript shape, tool-call logging, and
  `max_iterations` behavior.
- Add focused tests under `backend/tests/test_anthropic_native_client.py` for
  request kwargs preservation and stale-refresh transcript/message preservation.

Acceptance criteria:

1. Existing public imports and `AnthropicClient` method signatures remain
   unchanged.
2. `simple()`, `simple_params()`, and `run_with_tools()` produce the same
   message kwargs for system, tools, raw tools, MCP servers, temperature,
   max-token, and cache-control cases covered by existing tests.
3. Stale-refresh injection still appends one text block to the most recent user
   message and records the same `{"stale_refresh": True}` transcript entry.
4. `backend/.venv/bin/pytest backend/tests/test_anthropic_native_client.py`
   passes.

Filed follow-up: pending JIRA creation from OP-1286; this pickup is restricted
to `jira_dispatch.add_comment()` for programmatic JIRA writes.
