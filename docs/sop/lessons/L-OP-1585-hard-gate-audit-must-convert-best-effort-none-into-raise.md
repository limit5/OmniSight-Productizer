---
id: L-OP-1585
ticket: OP-1585
title: A hard-gate audit must convert the best-effort logger's None return into a raise
date: 2026-05-22
tags: [audit, release-train, gate, schema, backend]
---

# A hard-gate audit must convert the best-effort logger's None return into a raise

**Situation**: RT-10a's `release_train.promote` requires the audit row
to be a HARD gate — "audit-insert failure aborts before any tag write".
The obvious reuse is `backend/audit.py::log`, but that function is
deliberately *best-effort*: its own docstring is "don't kill the train
because the receipt printer ran out of paper", so on a failed insert
(DSN unreachable, no pool, swallowed exception) it logs a warning and
**returns `None` instead of raising**. Calling it directly inside a
gate would let a promote proceed to the tag write with no audit row —
the exact opposite of the AC.

**Fix**: `promote` treats the audit log as an injected seam
(`AuditLog`) and converts the best-effort contract into a hard one at
the call site: a `None` return *or* a raised exception both become
`AuditWriteError`, the train row is moved to `failed`, and `tag_writer`
(the RT-12 retag) is never invoked. The ordering is explicit —
CAS-acquire → audit gate → tag write — so the gate sits structurally
before any tag side effect.

**Verification**:
- `backend/tests/test_release_train.py::test_audit_insert_returning_none_aborts_before_tag_write`
  and `::test_audit_insert_raising_aborts_before_tag_write` assert the
  tag-writer spy is never called and the row lands in `failed`.
- `::test_two_concurrent_promotes_exactly_one_wins` proves the CAS
  single-winner half of the AC.

**Generalisation**: A best-effort logger and a hard gate are opposite
contracts. When you reuse a "never raises, returns None on failure"
audit/telemetry helper inside a gate, you must explicitly re-interpret
its sentinel return as fatal — silence-on-failure is a feature for the
helper and a bug for the gate. The tell is any gate whose audit/side-
effect call ignores the return value: if the helper can return a
falsy "I didn't actually persist" sentinel, the gate has to branch on
it and abort *before* the protected side effect runs.
