# runner_handlers

**Purpose**: Implements the six filesystem/shell tool handlers (Read, Write, Edit, Bash, Grep, Glob) that back the agentic loop in `auto-runner-sdk.py`. Each handler enforces sandboxing and resource limits before delegating to the OS.

**Key types / public surface**:
- `BASE_DIR` — resolved project root, overridable via `OMNISIGHT_RUNNER_BASE_DIR`. All path ops are constrained to it.
- `read_handler` / `write_handler` / `edit_handler` / `bash_handler` / `grep_handler` / `glob_handler` — the six tool callables, each taking a payload dict and returning a string.
- `bind_to_dispatcher(dispatcher)` — registers all six handlers on an existing `ToolDispatcher`.
- `make_runner_dispatcher()` — convenience: returns a fresh dispatcher with handlers wired.

**Key invariants**:
- Path safety uses `realpath` resolution, so symlinks escaping `BASE_DIR` are rejected — not just lexical checks. Non-existent paths still resolve (Write needs that).
- `Edit` refuses non-unique `old_string` matches unless `replace_all=True`; identical old/new is also rejected.
- `Bash` ignores `run_in_background` (raises `NotImplementedError`) because the runner has no monitor channel for orphaned processes. `timeout` is interpreted as **milliseconds** per the schema, then floored to ≥1 second.
- `Bash` runs commands with `shell=False` after `shlex.split`, and `_validate_bash_command` rejects shell metacharacters (`| & ; ( ) < > $ \` \n \r`). Pipelines / redirection / command substitution must be split into multiple Bash calls or done through a different tool — the LLM-controlled `command` payload is no longer a shell-injection vector (audit FX.1.4 / B4).
- Output caps: Bash stdout 30KB / stderr 10KB (tail-truncated), Grep 50KB tail, Glob 1000 matches. Grep treats exit code 1 as "no match", not error.

**Cross-module touchpoints**:
- Imports `ToolDispatcher` from `backend.agents.tool_dispatcher` for registration.
- Consumed by `auto-runner-sdk.py` via `AnthropicClient.run_with_tools` (per the module docstring) — not visible in the imports here.
