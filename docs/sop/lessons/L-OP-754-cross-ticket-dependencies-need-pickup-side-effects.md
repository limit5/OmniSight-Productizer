---
id: L-OP-754
ticket: OP-754
title: Cross-ticket dependencies need pickup side effects
date: 2026-05-08
tags: [runner, jira, dependencies]
---

# Cross-ticket dependencies need pickup side effects

**Situation**: JIRA Blocks links can correctly describe ticket ordering,
but a runner that only returns `False` from pre-pickup leaves operators
without a visible waiting marker. The same ticket can be silently skipped
on every tick, and cross-runner dependency chains are hard to diagnose.

**Fix**: Treat dependency blocks as a first-class pickup outcome. The
runner propagates `blocked-by:<key>` into a JIRA comment, adds a
`runner-blocked:waiting-<key>` label while waiting, and removes stale
waiting labels when the Blocks gate passes.

**Verification**: `backend/tests/test_file_coordinator.py` covers Blocks
status gating, cycle detection, and the synthetic A-before-B dispatch
flow. `backend/tests/test_file_mutex.py` covers runner waiting-label and
comment side effects.

**Generalisation**: Any pre-pickup gate that intentionally skips work
should leave both a machine-readable marker and an operator-readable
comment, then clean up the marker automatically when the gate opens.
