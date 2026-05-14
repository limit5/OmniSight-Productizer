"""OP-1111 / v2-Ⅹ-3 — Centralized RUNNER_RUNTIME_ARTIFACTS constant.

Backwards-compat: safe (additive module; new canonical home for an
existing constant that lived in :mod:`backend.agents.runner_progress`
since SP-B-X-018 / OP-1076).

Per spec ``docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-
spec.md`` §3 Family ⑩ §4 ticket row 8 (OP-1111). The constant names
every file that the runner itself writes inside a worktree as part of
its bookkeeping — files that:

* MUST be filtered out of dirty-check / stash / change-id paths so the
  runner's own writes do not trigger workspace-tampered or
  dirty-worktree reverts (the SP-B-X-016/017 / OP-1070 failure class).
* SHOULD be ignored by git so they never get accidentally staged or
  committed (the OP-1111 reversal of the original SP-B-X-018
  "filter-only" decision — defense in depth: filter at the Python
  layer + ignore at the git layer).

Single source of truth
----------------------

Every dirty-check site MUST import :data:`RUNNER_RUNTIME_ARTIFACTS`
from this module. The previous home (``runner_progress``) re-exports
for backwards compat, so legacy callers that imported
``runner_progress.RUNNER_RUNTIME_ARTIFACTS`` keep working. New code
imports from here.

The ``.gitignore`` entries that mirror this constant are pinned by
``tests/test_runner_runtime_artifacts_consolidation.py`` —
``test_gitignore_covers_every_runtime_artifact`` fails loudly if a new
entry is added here without a matching ``.gitignore`` line.
"""
from __future__ import annotations

#: Filename of the per-ticket phase-progress writer (SP-B-X-002a /
#: OP-1060). ``runner_progress.record_phase`` writes this at every FSM
#: boundary; a naive dirty-check would report dirty on every phase
#: transition.
PROGRESS_FILENAME: str = "progress.txt"

#: Atomic-write tempfile suffix used by ``runner_progress`` for the
#: write-then-rename dance on ``progress.txt``.
_PROGRESS_TMP_SUFFIX: str = ".tmp"

#: Workspace-tamper sentinel (OP-842 / OP-836).
#: ``runner_workspace_safety.write_workspace_sentinel`` drops this
#: pre-CLI as an intentionally-untracked tamper-detection marker.
_WORKSPACE_SENTINEL: str = ".runner-cwd-sentinel"


#: Canonical set of runner-runtime artifact filenames. Every
#: dirty-check / stash / change-id site filters against this set.
#:
#: To add a new artifact, ADD IT HERE — every dirty-check site picks it
#: up automatically. Then update ``.gitignore`` to mirror; the
#: ``test_gitignore_covers_every_runtime_artifact`` regression test
#: catches misses.
RUNNER_RUNTIME_ARTIFACTS: frozenset[str] = frozenset({
    PROGRESS_FILENAME,
    PROGRESS_FILENAME + _PROGRESS_TMP_SUFFIX,
    _WORKSPACE_SENTINEL,
})


#: Sentinel comment block that marks the auto-mirrored region in
#: ``.gitignore``. The regression test scopes its check to the lines
#: between START and END.
GITIGNORE_BEGIN_SENTINEL: str = (
    "# === OP-1111 RUNNER_RUNTIME_ARTIFACTS auto-mirror START ==="
)
GITIGNORE_END_SENTINEL: str = (
    "# === OP-1111 RUNNER_RUNTIME_ARTIFACTS auto-mirror END ==="
)
