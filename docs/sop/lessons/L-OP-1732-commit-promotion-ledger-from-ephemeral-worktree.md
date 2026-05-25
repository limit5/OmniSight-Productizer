---
id: L-OP-1732
ticket: OP-1732
title: An audit artifact written by a tool run from an ephemeral /tmp worktree never reaches the branch — persist it to a stable path and make committing it an explicit SOP step + helper, not a hope
date: 2026-05-26
tags: [release-train, promote, audit, ledger, worktree, devops, docs, sop]
---

# An audit artifact written by a tool run from an ephemeral /tmp worktree never reaches the branch — persist it to a stable path and make committing it an explicit SOP step + helper, not a hope

**Situation**: `scripts/promote_image_bundle.py` records the human-readable
release LEDGER under `REPO_ROOT` — the bundle audit row in
`audit/image_promotion_audit.jsonl` (`DEFAULT_AUDIT_LOG`) and one
`audit/promotion-predicates/*.json` predicate per image
(`sign_promotion_attestation.py` `DEFAULT_PREDICATE_DIR`). The audit write is a
deliberate hard gate (a failed write aborts the promote). But the release SOP
(`docs/operations/2026-05-22-release-train-readiness-handoff.md` §3) mandates
running promote from an **ephemeral** `/tmp/rel-<sha>` develop-tip worktree, so
`REPO_ROOT` is that throwaway checkout: the ledger lands in
`/tmp/rel-<sha>/audit/` and is destroyed on worktree cleanup, never reaching
`develop`. The v0.6.2 promote (2026-05-26) hit exactly this — the row + 2
predicates had to be hand-rescued to `/home/user/backups/promote-records/v0.6.2/`
and were still uncommitted; prior promotes show the same drift. The hard
audit-write gate gave false confidence: it proves the row was *written*, not that
it was *persisted somewhere committable*. (The cryptographic record — the cosign
attestation in the registry — was never at risk; this gap is only the
git-tracked ledger.)

**Fix**: Make landing the ledger an explicit, idempotent, reviewed step rather
than relying on the worktree surviving.

- `scripts/commit_promotion_ledger.py` (OP-1732): given a stable `--from-dir`
  (the persistent promote target, or a rescued backup like
  `/home/user/backups/promote-records/vX.Y.Z/`), it merges the audit rows into
  the canonical `audit/image_promotion_audit.jsonl` and copies the
  `*.promotion.json` predicates into `audit/promotion-predicates/`, then
  `git add`s the touched files. It is idempotent: a byte-identical audit row
  (compared as canonicalised JSON, `sort_keys=True`) is not re-appended and a
  byte-identical predicate is skipped; a predicate name collision with
  **different** content is a hard error, never a silent overwrite. It **never**
  commits (without `--commit`-style intent) and **never** pushes — the ledger
  still lands on `develop` through Gerrit (AI +1 / human +2), and it does not
  touch the registry, signing, or attestations.
- The runbook §3 now carries (a) a caveat at the promote step that the ledger is
  written into the doomed `/tmp` checkout, (b) guidance to point
  `--audit-log`/`--predicate-out-dir` at a persistent location so the ledger is
  off the throwaway worktree from the start, and (c) a MANDATORY post-promote
  step 7 that runs the helper from the canonical checkout and pushes for review.
- First application: the rescued v0.6.2 record was merged via the helper and
  staged (`audit/image_promotion_audit.jsonl` + the 2 predicates).

**Verification**: `tests/test_commit_promotion_ledger.py` pins the behaviour —
append-and-copy from a `--from-dir`; idempotent re-run is a no-op
(`test_idempotent_rerun_is_a_noop`); an already-present row is not duplicated;
a predicate collision with different content raises `LedgerError`
(`test_predicate_collision_with_different_content_is_error`) while identical
content is skipped; `--dry-run` writes/stages nothing; staging calls `git add`
and never `git commit`/`push` (`test_stage_calls_git_add_never_commit_or_push`);
and a CLI smoke runs `--dry-run` against the real v0.6.2 backup when present. The
v0.6.2 row merged byte-for-byte equal to the rescued backup
(`audit-rows.jsonl`), confirming the Exercised AC. The existing
`tests/test_promote_image_bundle.py` (14 passed, 1 skipped) confirms promote's
signing/attestation and the audit-write hard gate are unchanged.

**Generalisation**: **A "we wrote the artifact" gate is not a "the artifact is
durable" gate.** When a tool writes a record relative to its own working
directory and the SOP runs that tool from an ephemeral/throwaway location, the
record's *write* succeeds while its *persistence* silently fails — the gate
proves the wrong invariant. Two complementary fixes, and prefer doing both over
"magic" auto-commit: (1) let the tool target a persistent, committable path
(don't hard-code output under a dir the SOP throws away), and (2) make moving the
artifact into the reviewed branch an *explicit, idempotent, re-runnable* helper +
a numbered SOP step, never an implicit hope that the worktree survives. Keep the
human/review gate (here Gerrit +1/+2) — durability is a separate concern from
authorisation, so a persistence helper must not also become an auto-push that
bypasses review. (Adjacent: L-LEGACY-002 "git add TODO.md before merge commit"
and L-LEGACY-006 "git mv requires explicit git add" — both are the same family:
a file change that *looks* done locally but is never actually captured into the
commit/branch that ships.)
