---
id: L-OP-749
ticket: OP-749
title: External mutations need idempotency keys and circuit breakers
date: 2026-05-08
tags: [runner, jira, gerrit, reliability]
---

# External mutations need idempotency keys and circuit breakers

**Situation**: Runner retries can repeat successful side effects when the
failure happens after an external mutation but before the tick completes.
JIRA comments, workflow transitions, assignee edits, and audit inserts
are especially visible because duplicates confuse operators and make the
ticket timeline harder to trust. Separately, polling every 90s while an
external dependency is down creates repeated failures without progress.

**Fix**: Route runner-owned external mutations through stable
idempotency keys and a 24h SQLite response cache, and put service-level
circuit breakers in front of JIRA REST, Gerrit SSH, Gerrit REST, and
backend REST call paths.

**Verification**: `backend/tests/test_idempotency.py` covers key replay,
TTL pruning, and JIRA mutation subkeys. `backend/tests/test_circuit_breaker.py`
covers five JIRA failures opening the breaker, runner skip behavior via
`backend/tests/test_runner_backpressure.py`, and half-open recovery.

**Generalisation**: Any new external mutation helper should take an
optional idempotency key and should execute through the service's circuit
breaker. Retrying a whole runner tick is only safe when individual side
effects are deduplicated and unhealthy dependencies pause the loop.
