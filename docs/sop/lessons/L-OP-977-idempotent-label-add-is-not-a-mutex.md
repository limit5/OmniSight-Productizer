---
id: L-OP-977
ticket: OP-977
title: An idempotent set-add is not a mutex — a "fix" that doesn't change the failing semantics isn't a fix
date: 2026-05-12
tags: [ci, jira, runner, reliability, post-mortem]
---

# An idempotent set-add is not a mutex

**Situation**: OP-838 (2026-05-11) was filed and merged to "prevent duplicate Gerrit pushes for same ticket" after the OP-836 #356 / OP-837 #358 abandon incident. Its fix added a `claim:{instance_id}` label written via JIRA's `update.labels.add` and read back: GET-assignee → PUT assignee+label → GET-readback. It worked for the *cross-bot* shape (the assignee field is single-valued, last-writer-wins, so only one bot survives the readback) — which happened to be the shape of the incident that triggered it. On 2026-05-12, OP-974 hit the shape OP-838 *couldn't* serialise: two `claude` runners sharing one `instance_id` both PUT `claim:default`, both read it back, both believed they'd claimed; the loser's `NoCommitsOnBranchError` recovery transitioned the ticket back to `To Do`, reverting the winner's already-Under-Review work. Operator rescue at 16:45.

**Root cause of the *non-fix***: Atlassian's `update.labels.add` is a **set-union (idempotent)**, not a compare-and-swap. The OP-838 design assumed an atomicity ("only one writer's add lands") that the primitive does not provide. The fix added a step and a label but did not change the operation whose semantics were the actual problem. It passed review and a unit test because the test exercised the cross-bot interleaving (where the *assignee* field — a genuinely single-valued primitive — does the serialising), not the same-instance one.

**Fix (OP-977 / AUDIT-24)**: replace the bare label with a **fencing token** — `claim:{instance_id}:{epoch_us:016d}-{uuid8}`, unique per attempt — and decide the winner by `min(...)` over *all* `claim:{instance_id}:*` labels in the post-PUT readback. The winner is now a function of a **total order over a set every racer observes identically**, not of "did my idempotent write succeed". The loser leaves its label for GC so the winner re-evaluating `min(...)` never flips. Stale tokens (crashed runners) are swept once aged past `2× CLI timeout`; pre-AUDIT-24 bare labels are treated as expired. Rollback flag `OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY=1`. Race-simulation suite `backend/tests/test_atomic_claim_race.py` reproduces the OP-974 schedule (two threads, same `instance_id`, same bot, fully interleaved GET/PUT/GET) and asserts exactly one wins and the lowest token wins for every token assignment. Pattern doc: `docs/sop/runner-pickup-mutex.md`.

**Verification**: `test_atomic_claim_race.py` 7-case suite + 25-iteration randomised stress test green; `test_jira_dispatch_mutex.py` (31 cases) green; `claim_ticket_atomic`'s public signature unchanged so `auto-runner-jira.py` keeps working.

**Generalisation**: When you "fix" a concurrency bug, name the primitive that will do the serialising and check its actual semantics — `labels.add` (set-union), `assignee=` (last-writer-wins), `If-Match`/ETag (true CAS) are all different. A mutex needs *either* a CAS-like primitive *or* a deterministic tie-break over a value all racers read consistently; "everyone writes the same marker then everyone reads it" is neither. And: a regression test for a race must exercise the *failing* interleaving — if the test would pass against the broken code, it isn't testing the fix. This is the canonical instance of the `architecture-anti-patterns.md` "fix shipped but didn't fix the root cause" symptom and of `feedback_blind_spot_audit.md`'s anti-workaround rule.
