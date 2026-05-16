---
id: L-OP-1040
ticket: OP-1040
title: A pre-review mergeability self-fix is a new lifecycle point — map the existing rebase loci before adding it, cap it, and escalate on exhaustion
date: 2026-05-13
tags: [runner, gerrit, rebase, self-fix, audit, architecture]
---

# A pre-review mergeability self-fix is a new lifecycle point — map the existing rebase loci before adding it, cap it, and escalate on exhaustion

**Situation**: A runner pickup that finishes work, pushes a brand-new Gerrit
change, and *then* pings the operator for +1/+2 can ping on a change that was
born unmergeable — `develop` moved between branch-cut and push, so Gerrit reports
`mergeable=false` the moment the patchset lands. The operator sees a review
request, opens the change, finds the "Cannot merge" banner, and has to bounce it
back. Before AUDIT-29g (OP-1038/1039) nothing closed that window: the runner's
own push path had no mergeability check, and every *other* rebase mechanism in
the codebase operates at a different point in a change's life and deliberately
does **not** touch this case:

- `backend/agents/auto_rebase.py` (`AutoRebaseSweeper`, OP-733) — fires on a
  `change-merged` event on `develop` and sweeps **other** open bot-owned PSes,
  calling Gerrit's REST rebase endpoint (`POST /a/changes/{id}/revisions/current/rebase`).
  PSes that **conflict are left alone** for the OP-720 stale-PS scanner; it never
  force-pushes a conflict resolution. Lifecycle: *someone else's* change, *after a
  merge*.
- `backend/agents/auto_rebase.local_rebase_with_resolvers` + `auto_resolve_config.py`
  (OP-782) — a local `git rebase target_sha`; on conflict it runs a *registered
  deterministic resolver* (lockfiles, generated indices) for **only** the files
  that have one, `git add`s them, `git rebase --continue`, pushes. Any
  *unregistered* file in the conflict → it raises; nothing is resolved.
  Lifecycle: post-merge, generated-file-only, fully deterministic.
- `backend/agents/submit_queue_worker.py` (OP-751) — at *submit* time (post-+2),
  serialises rebase→CI→merge for Submit-Ready PSes; a rebase conflict makes it
  vote `-1` with `reject_reason="rebase_conflict"` and leave the PS. Lifecycle:
  just before merge.
- `backend/agents/ci_recovery.py` (OP-741) — a *classifier*: a "stale develop"
  CI failure routes to the `invoke_r3_auto_rebase` hook (which delegates to the
  bridge's OP-733 sweeper). It does no rebasing itself.

So the cross-codebase audit answer is: **no other agent does an unconditional
rebase-and-force-push of a pre-review runner change** — the four existing loci
are post-merge sweeps, generated-file resolvers, submit-gate rebases, and a
failure classifier, each scoped to a distinct lifecycle point and ownership. The
pre-review self-fix is additive at a genuinely new point, not a re-implementation
of any of them; merging it created no duplicate logic.

**Fix**: AUDIT-29g built one small stateless module wired into the *existing*
push path rather than a new runner code path:

- `backend/agents/pre_review_self_fix.py` (`self_fix_mergeability`, OP-1038):
  after the runner push, `GET /a/changes/{n}/revisions/current/mergeable`; while
  it is `false`, `git fetch <gerrit-ssh-url> develop` → `git rebase FETCH_HEAD`
  → `git push --force --no-thin HEAD:refs/for/<target>`, capped at
  `DEFAULT_MAX_ATTEMPTS = 3`. Three outcomes are distinguished so the loop never
  spins: `mergeable=true` → done (`rebased`/`force_pushed` flags say whether a
  fix was needed); a *rebase that fails* (a real file conflict) → `git rebase
  --abort` and return non-mergeable immediately (retrying a clean rebase cannot
  fix a content conflict, so it does **not** burn the remaining attempts);
  `mergeable=false` after the 3rd successful rebase → `cap_exhausted=True`.
  Missing HTTP credentials → `skipped=True` (still a non-mergeable terminal
  result, so the caller does not forward an unverified patchset).
- `backend/agents/jira_dispatch.py` (OP-1038): `push_to_gerrit_for_review` calls
  `self_fix_mergeability` after parsing the Change URL; `mergeable=false` →
  `GerritPushResult(success=False, ...)` so the runner does not advance the
  ticket. OP-1039 added `file_pre_review_self_fix_exhaustion_ticket`: on
  `cap_exhausted`, file a `class:operator` / `needs-coordinator` /
  `pre-review-self-fix-exhausted` Story carrying the source ticket, Gerrit URL,
  Change-Id, attempt count, runner detail, and a 30k-char-bounded
  `git diff FETCH_HEAD...HEAD` context, with `@coordinator` + operator-fallback
  mention in the body — so an exhausted loop escalates instead of wedging the
  pickup.

**Verification**: `backend/tests/test_pre_review_self_fix.py` pins the unit
behaviour (rebase+force-push until mergeable; 3-attempt cap on persistent
`mergeable=false`); `backend/tests/test_jira_dispatch.py::test_push_to_gerrit_for_review_files_exhaustion_ticket`
pins the escalation-ticket fields. OP-1040 adds
`backend/tests/test_pre_review_self_fix_integration.py`, which uses **real git**:
it builds a bare "gerrit" remote + a runner worktree, makes `develop` diverge
with a conflicting commit, and drives `self_fix_mergeability` with the real
`subprocess.run` and a stub `urlopen` — the first test injects a true file
conflict and asserts the rebase is aborted (`detail` contains "git rebase
failed", working tree clean, `mergeable is False`); the second makes the rebase a
clean no-op but holds `mergeable=false`, asserts the real `git fetch`/`rebase`/
`push --force` cycle runs exactly 3× (`cap_exhausted`, `attempts == 3`), then
feeds that `SelfFixResult` into `jira_dispatch.file_pre_review_self_fix_exhaustion_ticket`
with a fake `_request` and asserts the escalation Story's summary, labels, and
`@coordinator`/Change-Id/attempt-count/diff body. The new file lives in
`backend/tests/` so the `backend-tests` shard in `.github/workflows/ci.yml` runs
it on every backend commit (Integration AC); the "≥7 consecutive CI cycles
without flake" Exercised AC is a post-merge observation, not code-verifiable in
this ticket.

**Generalisation**: **Before adding an auto-repair/self-fix loop for an external
system's state, enumerate every existing place that touches that state at an
adjacent lifecycle point — a new loop should be *additive at a distinct point*,
never a quiet re-implementation of one that already exists.** Write the audit
down (which loci, what each does on conflict, why none covers the new window) so
a future reviewer can confirm the non-overlap by reading rather than re-deriving
it. And the loop itself needs three properties or it becomes a liability: (1) a
hard attempt cap; (2) a three-way classification of each iteration —
*fixed* / *still broken, a retry might help* / *broken in a way a retry cannot
help* — where only the middle case consumes an attempt (here: a clean-rebase
retry can fix "develop moved, no file overlap" but cannot fix a content conflict,
so a content conflict short-circuits instead of burning the cap); and (3) an
escalation on exhaustion (a labelled operator ticket with enough context to act)
rather than a silent give-up or an infinite spin. (Adjacent: L-OP-746 "measure
before optimising conflict pressure", L-OP-736 "runner git operations need
layered cleanup for stuck rebase", L-OP-749 "idempotency and circuit breakers for
external mutations" — the cap here is the circuit breaker; L-OP-756 "binary gates
need graceful degradation tiers" — `skipped` on missing creds is the degradation
tier.)
