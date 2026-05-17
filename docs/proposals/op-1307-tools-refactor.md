# OP-1307 tools module refactor proposal

## Context

`backend/agents/tools.py` is the shared tool surface for agent file, git, bash,
Gerrit review, issue-tracking, and report-generation operations. The module is
intentionally workspace-aware: `_safe_path()` gates file access through the
active workspace context, while `_git()` and `run_bash()` route command
execution through the same root.

Two small refactors would reduce coupling without changing the public tool names
or LangChain `@tool` call surface:

1. Add a private CODEOWNERS guard helper shared by `write_file()`,
   `create_file()`, and `patch_file()`. These three functions repeat the same
   best-effort active-agent lookup, router import, `check_file_permission()`
   call, and block/warn handling before they perform path-specific work. A
   helper such as `_check_codeowners_for_write(path)` could return the existing
   block string or `None`, preserving the current best-effort failure behavior
   while keeping each file tool focused on create/write/patch semantics.
2. Split the command-tool registry into private grouped tuples near each tool
   family, then build the exported `FILE_TOOLS`, `GIT_TOOLS`, `BASH_TOOLS`, and
   `REVIEW_TOOLS` from those groups at the bottom of the file. Today the module
   already has clear section markers, but the registry is separated from the
   family definitions and must be manually kept in sync. Keeping each group's
   registration close to its section would make future additions easier to
   audit without changing exported list names.

## Non-goals

- No behavior change to workspace sandboxing, path resolution, PEP read guards,
  CODEOWNERS enforcement, bash dangerous-pattern blocking, git push restrictions,
  Gerrit review behavior, task wrappers, or report generation.
- No public tool rename and no signature changes for existing `@tool` functions.
- No change to `backend/tests/test_tools.py` in this proposal patch set.
- No database, devops, embedded, frontend, security, tooling, or test-domain
  changes.

## Follow-up implementation ticket draft

Summary: Refactor `backend/agents/tools.py` write guards and tool registration

Areas: backend, tests

Tier: S

Description:

Implement the two OP-1307 proposal refactors in `backend/agents/tools.py`:

- Add a private helper for the repeated CODEOWNERS write guard used by
  `write_file()`, `create_file()`, and `patch_file()`.
- Keep helper behavior best-effort: permission denials still return the same
  `[BLOCKED]` text, warning-only results still log, and unexpected guard errors
  still do not break file operations.
- Move tool-family registration closer to each tool section while preserving the
  exported `FILE_TOOLS`, `GIT_TOOLS`, `BASH_TOOLS`, and `REVIEW_TOOLS` names and
  members.
- Add focused tests under `backend/tests/test_tools.py` only if the refactor
  needs coverage for preserved guard behavior or registry membership.

Acceptance criteria:

1. Existing imports of all public tool functions and `*_TOOLS` registry lists
   remain valid.
2. `write_file()`, `create_file()`, and `patch_file()` preserve current
   CODEOWNERS block/warn/best-effort behavior.
3. `FILE_TOOLS`, `GIT_TOOLS`, `BASH_TOOLS`, and `REVIEW_TOOLS` contain the same
   tool objects as before the refactor.
4. `backend/.venv/bin/pytest backend/tests/test_tools.py` passes.

Filed follow-up: pending JIRA creation from OP-1307; this pickup is restricted
to `jira_dispatch.add_comment()` / `transition_back_to_todo()` for programmatic
JIRA writes.
