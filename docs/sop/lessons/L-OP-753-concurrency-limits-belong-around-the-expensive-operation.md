---
id: L-OP-753
ticket: OP-753
title: Concurrency limits belong around the expensive operation
date: 2026-05-08
tags: [runner, gerrit, reliability]
---

# Concurrency limits belong around the expensive operation

**Situation**: Auto-rebase sweeps need enough parallelism to drain a burst
of open patchsets, but an unbounded fan-out of rebase attempts can overload
the runner host when many changes need the same hot-file rebase at once.

**Fix**: OP-753 keeps sweep fan-out small and places a semaphore directly
around each rebase attempt. The default allows two active rebase operations,
with `OMNISIGHT_REBASE_CONCURRENCY` as the operator override.

**Verification**: `backend/tests/test_auto_rebase.py::test_synthetic_eight_rebases_never_exceed_two_active_attempts`
proves eight submitted rebase requests never exceed two active attempts,
and `test_rebase_concurrency_env_override_allows_three_active_attempts`
proves the environment override changes the token count.

**Generalisation**: Queue fan-out and expensive-operation concurrency are
different controls. Keep enough workers to avoid serial queue latency, but
put the hard token bucket around the specific I/O-heavy operation that can
overload the host.
