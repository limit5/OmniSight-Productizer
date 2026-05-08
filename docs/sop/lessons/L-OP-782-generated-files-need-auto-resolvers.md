---
id: L-OP-782
ticket: OP-782
title: Generated files need auto-resolvers after per-file decomposition
date: 2026-05-08
tags: [meta, generated-files, rebase]
---

# Generated files need auto-resolvers after per-file decomposition

**Situation**: OP-745 moved lessons into per-file entries, which removed the
direct N-writers-on-one-file conflict. The generated aggregate
`docs/sop/lessons-learned.md` still changed whenever any lesson landed, so
in-flight patch sets hit the same conflict class one layer later.

**Fix**: Register generated outputs in `.gerrit/auto-resolve.yaml` and let R3
auto-rebase plus the submit queue run the matching generator when every
conflicting file is registered. The resolver must leave no conflict markers and
must not modify unregistered files.

**Verification**: OP-782 adds `backend/tests/test_auto_resolve.py` coverage for
the registry, R3 auto-resolution, resolver failure fallback, and a synthetic
lessons-index rebase. It also extends
`backend/tests/test_submit_queue_worker.py` so the submit queue exercises the
same resolver path.

**Generalisation**: Per-file decomposition only removes the first shared write
target. If a tracked generated file aggregates those per-file sources, add an
auto-resolver or stop tracking the generated output before parallel patch sets
start rewriting it independently.
