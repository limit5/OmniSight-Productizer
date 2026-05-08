---
id: L-OP-744
ticket: OP-744
title: Long-running daemons need explicit pool lifecycle management
date: 2026-05-08
tags: [audit, backend, daemon, postgres]
legacy_lesson: 29
---

# Long-running daemons need explicit pool lifecycle management

**Situation**: The Gerrit/JIRA bridge daemon runs outside the FastAPI
lifespan context. Proactive merger work spawned from that daemon calls
`backend.audit.log`, which depends on the process-global `db_pool`; the
bridge never initialised it, so merger vote audit rows were dropped with
`db_pool.get_pool called before init_pool` warnings.

**Fix**: OP-744 gave the bridge daemon its own explicit pool lifecycle:
resolve the Postgres DSN, call `db_pool.init_pool()` before starting the
stream-events loop, and call `db_pool.close_pool()` in shutdown cleanup.

**Verification**: `backend/tests/test_gerrit_jira_bridge.py::test_run_initializes_db_pool_before_stream_and_closes_after`
pins that pool init happens before synthetic audit work reachable from
stream processing and that close runs after the daemon returns.

**Generalisation**: Any long-running process that imports backend code
outside FastAPI must own the lifecycle of shared infrastructure it uses.
Do not assume app lifespan side effects exist in workers, stream
consumers, or one-shot scripts.
