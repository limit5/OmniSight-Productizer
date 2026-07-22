---
id: L-OP-2725
ticket: OP-2725
title: Run the module's own test files locally — a red test outside the fast gate hides new breakage
date: 2026-07-23
tags: [runner, testing, ci]
---

# Run the module's own test files locally — a red test outside the fast gate hides new breakage

**Situation**: β-3c (Gerrit #2190) spliced `_fetch_learned_items_block(ticket_key)`
into `auto-runner-jira.py::_build_prompt`, but the in-scope name there is `key`.
The `NameError` fired on EVERY runner pickup (before the in-function feature-flag
check, so flag-off did not protect), crash-looping both claude runners at rc=1
~40 s/cycle. The existing
`backend/tests/test_auto_runner_prompt_builder.py::test_recognised_area_builds_prompt_without_raising`
exercised the exact line and was RED on develop — but the CI fast gate does not
run the auto-runner test files, and the β-3c local run (89 green) had not
selected them either, so "all local tests green + fast-gate green" coexisted
with a fleet-down bug.

**Fix**: One-word call-site fix `ticket_key` → `key` (Gerrit #2199). No new
test was needed — the pre-existing test IS the regression test and turned green.

**Verification**: `pytest backend/tests/test_auto_runner_prompt_builder.py
backend/tests/test_beta3c_runner_plane.py` → 38 passed on the fix; AST assert
shows no `ticket_key` Name remains in `_build_prompt`; runners resume clean
cycles after #2199 merged. (`test_auto_runner_ops_only.py` carries 4
pre-existing failures — identical with and without the fix.)

**Generalisation**: Before pushing a change to a file, run that file's OWN test
neighbourhood locally (for `auto-runner-jira.py`: every
`backend/tests/test_auto_runner_*.py`), not just the new feature's test files —
"N tests green" means nothing if the module's guard tests were never selected.
When a neighbourhood test is already red on the base branch, diff the failure
set WITH vs WITHOUT your change instead of skipping the file: identical
failures are pre-existing; any delta is yours. Splice-style edits into a large
untyped script deserve an execution-path test (or at minimum `py_compile` +
an AST name-binding check) — a call site can reference an unbound name and
still import cleanly.
