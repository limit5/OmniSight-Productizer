# Runner runtime artifacts — filter design decision

**Status**: Adopted (2026-05-14, SP-B-X-018 / OP-1076)
**Constant**: `backend.agents.runner_progress.RUNNER_RUNTIME_ARTIFACTS`

## What

The runner pipeline writes a small number of files to the worktree
purely for its own bookkeeping / safety, not as part of any ticket's
AC. These files must be invisible to every dirty-check that decides
"did the CLI leave uncommitted work?":

| File | Writer | Purpose |
|---|---|---|
| `progress.txt` | `runner_progress.record_phase` (SP-B-X-002a / OP-1060) | Crash-recovery durability per FSM phase |
| `progress.txt.tmp` | `runner_progress.write_progress_atomic` | Atomic-rename temp during progress write |
| `.runner-cwd-sentinel` | `runner_workspace_safety.write_workspace_sentinel` (OP-842 / OP-836) | Tamper-detection marker |

After SP-B-X-018 / OP-1076 these live in ONE canonical constant
(`RUNNER_RUNTIME_ARTIFACTS`) imported by every dirty-check site.

## Where the filter applies

Two dirty-check sites consume the constant:

1. **`runner_progress._worktree_dirty()`** — gating `take_phase_snapshot`'s
   `git stash push --include-untracked`. Without this filter, the
   sentinel/progress files get swept into the stash, then disappear
   from working tree, then trip the sentinel-check → `workspace-tampered`
   revert (SP-B-X-016 / OP-1074 fixed this).
2. **`jira_dispatch.ensure_change_ids()`** — gating the pre-rebase
   "CLI exited without committing" wedge (OP-827 regression
   prevention). Without this filter, `progress.txt` (written by the
   FSM between phases) shows up dirty post-AC and reverts the push
   (SP-B-X-017 / OP-1075 fixed this).

## Why a filter — and not `.gitignore`?

The architecturally-pure answer is to add these files to repo-level
`.gitignore` so `git status --porcelain` itself never reports them.
We **deliberately chose the filter** instead. Reasons:

1. **Consistency with OP-842 precedent.** The original sentinel filter
   was added per OP-842's "smaller, layered fix" docstring, with an
   explicit note that "OP-836 documented the intent to add it to
   `.git/info/exclude` but the implementation was never written; this
   filter is the smaller, layered fix." We extend the same convention
   rather than create two coexisting mechanisms.

2. **Per-callsite intent.** A filter at the callsite makes the
   exclusion's WHY visible to whoever reads the dirty-check code. A
   `.gitignore` entry is invisible at the call site and lacks the
   "this is runner bookkeeping" framing.

3. **Per-callsite control.** If a future dirty-check legitimately
   wants to SEE these files (e.g., a "did the runner crash mid-write"
   audit tool), the filter lets that callsite opt out without
   modifying `.gitignore` first. With `.gitignore`, every future
   callsite must remember to `git status --ignored`.

4. **Minimal repository surface.** `.gitignore` additions would land
   on `develop` permanently. The filter is purely runtime code, easier
   to refactor or roll back.

5. **Test coverage.** With a filter, regression tests can assert
   "both callsites use the canonical constant" (see
   `tests/test_runner_runtime_artifacts_consolidation.py`). With
   `.gitignore` we'd need to test "this entry is in `.gitignore`",
   which catches less.

## What changes if a new runtime artifact is added

When a future runner-runtime file is introduced (e.g. a checksum
file, a per-pickup log, a metrics dump):

1. Add it to `RUNNER_RUNTIME_ARTIFACTS` in `runner_progress.py`.
2. **Do NOT** add per-callsite local filters; they MUST import the
   canonical constant.
3. Update this doc's table.
4. Add a test case to
   `tests/test_runner_runtime_artifacts_consolidation.py` covering
   the new file at both callsites.

The canonical-constant pattern catches the gap that bit SP-B-X-016/017
— OP-1060 added `progress.txt` writes but only updated ONE of the two
dirty-checks. Now there is only one set of files to maintain.

## When to reconsider — `.gitignore` migration triggers

We reserve the right to re-evaluate if any of these hold:

- More than 3 dirty-check sites accumulate (the filter starts to feel
  like duplication-by-pattern even with one constant).
- A new runtime artifact is added that also needs to be invisible to
  external tools (e.g. CI's `git status` check). External tools can't
  import our constant; `.gitignore` is the only universal channel.
- The OP-836 work to add these to `.git/info/exclude` is finally
  picked up (originally intended).

If any trigger fires, file a follow-up ticket to migrate; this
document's §"Why a filter" section should be revisited as part of
that ticket's scoping.

## References

- OP-836 / OP-842: original sentinel filter precedent
- OP-1060 / SP-B-X-002a: progress.txt writer that broke dirty-checks
- OP-1074 / SP-B-X-016: hot-fix #1 (sentinel in `_worktree_dirty`)
- OP-1075 / SP-B-X-017: hot-fix #2 (progress.txt in `ensure_change_ids`)
- OP-1076 / SP-B-X-018: this consolidation
