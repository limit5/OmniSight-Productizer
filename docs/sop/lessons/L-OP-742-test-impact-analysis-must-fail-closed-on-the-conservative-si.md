---
id: L-OP-742
ticket: OP-742
title: Test-impact analysis must fail closed on the conservative side
date: 2026-05-08
tags: [ci, gerrit, runner]
legacy_lesson: 29
---

# Test-impact analysis must fail closed on the conservative side

**Situation**: OP-742 needed a CI worker that runs only the affected tests for each Gerrit patchset, replacing the 60-180 min full-suite cost with a 5-15 min selective run for typical backend PSes (memory: feedback_test_strategy.md). The naive design — direct file-name map (`foo.py` → `test_foo.py`) plus a reverse-import-graph walk — gives the right answer for most changes, but two failure modes can silently leak un-tested code into the merge queue:

1. **Module with no test file**: editing `backend/ghost.py` when `test_ghost.py` doesn't exist *and* nothing imports `ghost` produces an empty selection. Naive code would emit Verified +1 for "0 tests passed."
2. **High-fan-out leaf**: editing `backend/db.py` or `backend/agents/scheduler.py` triggers behavioural changes in tests that don't statically import them (DB-pool fixtures, scheduler ticks, transaction wrappers). The reverse-import graph is structurally blind to these because the dependency travels through `conftest.py` fixtures, not `import` statements.

A third footgun: dynamic imports (`importlib.import_module(...)`) are invisible to the static parser entirely.

**Fix**: Three layers, applied in order, with the last layer biased toward false positives (running too many tests) rather than false negatives (running too few):

1. Direct map (`Path(stem) → tests/test_<stem>.py`) — file-name convention, cheap and correct for ~70 % of changes.
2. Reverse-import graph (`ast.parse` of every `.py` under `backend/`, transitive walk capped at 6 hops) — handles refactor changes that ripple through the dependency DAG.
3. Conservative fallback in two parts:
   - **High-fan-out path list** (`HIGH_FAN_OUT_PATHS`): touching `backend/db.py`, `backend/auth.py`, `backend/conftest.py`, etc. forces the full suite. The list is maintained explicitly so a careless graph walk can't silently skip safety-critical coverage.
   - **Empty-selection sentinel**: if a backend change produces zero matched tests, `classify_changes` returns `policy="full"` with reason `"no test mapping — falling back to full suite"`. Better to spend 60-180 min than to ship an un-tested change.

**Verification**: `backend/tests/test_ci_test_impact.py::TestImportGraph::test_transitive_importers_walks_chain` pins the reverse-graph contract (editing `backend.lib.helper` pulls `test_helper`, `test_foo`, `test_orchestra`). `TestClassifyChanges::test_high_fan_out_path_forces_full` pins the `backend/db.py` → full-suite invariant. `TestClassifyChanges::test_affected_falls_back_to_full_when_no_tests_match` pins the empty-selection sentinel.

**Generalisation**: For any "select a subset of work to do" optimisation that gates a safety property (CI verification, security scanning, chaos drills), there must be a hard-coded fallback path that runs the full set whenever the selector returns empty *or* whenever a known-fan-out signal is touched. The selector's correctness is a soft optimisation; the fallback is the load-bearing safety guarantee. Never trust a static-analysis selection on the optimistic side without a static fallback list maintained by humans for the cases the analyser provably can't see.
