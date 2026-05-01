# tools

**Purpose**: Defines the full catalogue of system tools (file I/O, git, bash, simulation, deploy, MCP, image-gen, L2/L3 memory, etc.) that agents can invoke in either rule-based fallback or LLM tool-calling mode. Each tool is workspace-aware and sandboxed so multiple agents can operate concurrently on isolated checkouts.

**Key types / public surface**:
- `set_active_workspace(path, agent_id)` / `get_active_workspace()` — contextvar-based per-invocation workspace + agent override.
- `@tool` decorated coroutines: `read_file`, `write_file`, `create_file`, `patch_file`, `run_bash`, `git_*`, `gerrit_*`, `run_simulation`, `deploy_to_evk`, `image_generate`, `summarize_state`, `search_past_solutions`, etc.
- `TOOL_MAP` — name→tool dict used by the executor for dynamic dispatch.
- `AGENT_TOOLS` — per-agent-type tool whitelist (`firmware`, `software`, `reviewer`, `devops`, …).
- `_DANGEROUS_PATTERNS` / `_SAFE_PUSH_PATTERN` / `_safe_path()` — the safety layer.

**Key invariants**:
- All paths are resolved through `_safe_path`, which uses `Path.relative_to` (not `startswith`) to prevent prefix-confusion escapes (`/work` vs `/workspace`).
- `write_file` is a deprecation trap: it refuses to overwrite an existing file whose new body exceeds `OMNISIGHT_PATCH_MAX_INLINE_LINES` (default 50); use `patch_file` instead. `create_file` is uncapped but rejects existing paths.
- `git push` is hard-restricted to `agent/*` branches; PR creation likewise. With Gerrit enabled, push auto-rewrites to `refs/for/{target}`. AI reviewers can only score ±1 — `+2`/Submit are reserved for humans.
- DB-touching tools assume worker (not request) context: each acquires its own pool conn via `db_pool.get_pool().acquire()` rather than a request-scoped one.
- `run_bash` prefers Docker-container exec when an agent has one, falling back to host; direct `simulate.sh` calls are redirected to `run_simulation` for structured output.

**Cross-module touchpoints**:
- Imports from `backend.codeowners`, `backend.container`, `backend.git_auth`, `backend.gerrit`, `backend.git_platform`, `backend.sdk_provisioner`, `backend.intelligence`, `backend.report_generator`, `backend.workspace`, `backend.db`/`db_pool`, `backend.events`, `backend.llm_credential_resolver`, plus router-level `_agents`/`_tasks` registries.
- Consumed by the agent
