# OP-1317 runner handlers refactor proposal

## Context

`backend/agents/runner_handlers.py` is the auto-runner SDK tool-handler module
for file reads/writes, edits, shell execution, grep/glob discovery, knowledge
retrieval, and dispatcher registration. The module already has clear section
markers and focused tests in `backend/tests/test_runner_handlers.py`, but the
Bash and Grep sections carry several small policy and formatting details inline
with process execution.

Two small refactors would reduce coupling without changing handler names,
payload contracts, dispatcher registration, or safety behavior:

1. Split Bash execution result formatting into a private helper shared by the
   normal subprocess path and timeout path. `bash_handler()` currently handles
   background rejection, command validation, dedicated-tool hints, timeout
   conversion, subprocess execution, output truncation, and response formatting
   in one function. A helper such as `_format_bash_result(result)` could own
   stdout/stderr truncation and the final `STDOUT` / `STDERR` / `EXIT_CODE`
   shape, keeping `bash_handler()` focused on validation and dispatch.
2. Add a private grep-result formatter paired with `_build_grep_cmd()`.
   `grep_handler()` already delegates command construction, but still owns the
   timeout message, exit-code interpretation, error truncation, and large-output
   truncation. A helper such as `_format_grep_result(cmd, result)` would keep
   `grep_handler()` parallel to `bash_handler()`: resolve path, build command,
   run subprocess, format result.

## Non-goals

- No behavior change to BASE_DIR path containment, PEP read guards, edit
  cascade behavior, Bash denylist/network-egress blocking, dedicated-tool hints,
  grep/rg feature selection, knowledge retrieval, or dispatcher registration.
- No public handler rename and no payload schema or return-shape changes.
- No production code changes in this proposal patch set.
- No test changes in this proposal patch set.
- No database, devops, embedded, frontend, security, tooling, or out-of-scope
  domain changes.

## Follow-up implementation ticket draft

Summary: Refactor runner handler Bash and Grep formatting helpers

Areas: backend, tests

Tier: S

Description:

Implement the two OP-1317 proposal refactors in
`backend/agents/runner_handlers.py`:

- Add a private Bash result-formatting helper and call it from `bash_handler()`
  after `subprocess.run()` succeeds.
- Preserve the existing timeout return string, stdout/stderr truncation limits,
  `STDOUT` / `STDERR` / `EXIT_CODE` response shape, `/bin/bash` execution, and
  `cwd=BASE_DIR` behavior.
- Add a private Grep result-formatting helper and call it from `grep_handler()`
  after `subprocess.run()` succeeds.
- Preserve `rg`/`grep` command construction, no-match handling, error handling,
  and output truncation.
- Add focused tests under `backend/tests/test_runner_handlers.py` only if the
  implementation needs extra coverage beyond the existing Bash and Grep cases.

Acceptance criteria:

1. Existing public imports of all runner handler functions and
   `make_runner_dispatcher()` remain valid.
2. Bash handler tests continue to prove stdout/exit-code formatting, cwd
   anchoring, timeout handling, background rejection, command validation, and
   shell expansion behavior.
3. Grep handler tests continue to prove match discovery, no-match handling,
   count mode, and BASE_DIR escape rejection.
4. `backend/.venv/bin/pytest backend/tests/test_runner_handlers.py` passes.

Filed follow-up: pending operator/JIRA creation. This pickup is restricted to
`jira_dispatch.add_comment()` / `transition_back_to_todo()` for programmatic
JIRA writes.
