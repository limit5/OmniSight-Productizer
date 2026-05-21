# Release-train migration — filing-ready Story descriptions (NOT yet on JIRA)

**Status**: filing-ready expansion of `docs/design/2026-05-21-release-train-ticket-spec.md` v2 (post 6 codex audits + 3 decisions locked in ADR-0040). Each Story below is ready to POST once the operator confirms + `class` split + filing-tool drift (RT-03) lands. **Hard filing order: RT-00 → RT-03 → RT-01/RT-23 → rest. Do NOT batch ci/gerrit/db tickets before RT-03.**

Common to every Story: `scope:release-train-2026-05`, `agent:auto` on every `[BOT]`, `tier:X`+`runner:no-commits-expected` on every `[OP]`. `verify:` blocks are the Exercised AC; "merged" is NOT done.

---

## RT-00 — `[OP][RT-00] Accept ADR-0040 + supersede flips + cross-refs + ADR-0023 reconcile`
labels: type:docs · tier:X · class:operator · area:docs · runner:no-commits-expected
prereqs: blocks_on=[]
Goal: Flip ADR-0040 Proposed→Accepted and apply the supersede status pointers; operator governance act (no code).
Files: docs/adr/ADR-0040-*.md, ADR-0001/0020/0039/0016/0023 status lines, cross-ref ADRs (0002/03/04/05/07/12).
Spec: ticket-spec RT-00; ADR-0040 §"Decisions LOCKED".
Out-of-scope: no code; webhooks=RT-23; Gerrit SR removal=RT-17.
AC — Code: ADR edits. Deploy: n/a (docs). Integration: runner/reviewer see ADR-0040 as the normative contract. Exercised(verify): `grep -m1 'status: Accepted' docs/adr/ADR-0040-*.md` AND `grep -l 'Superseded by .*ADR-0040' docs/adr/ADR-000{1}-*.md`. Go-Live: ADR-0040 Accepted before any code ticket filed.

## RT-03 — `[BOT][RT-03] Filing-vocab + tool drift fix (add area:ci+area:gerrit; fix db rejection)`
labels: type:feature · tier:M · class:sub-codex · area:tooling,docs,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-00]; mutex=[rt-filing-vocab]
Goal: Make `ci`, `gerrit`, `db` consistently accepted+recognised across runner, filing CLI, schema, conventions, capability matrix — nothing else.
Files: auto-runner-jira.py (RECOGNISED_AREAS ~143), scripts/file_jira_ticket.py (VALID_AREAS ~37, RUNNER_ONLY_AREAS ~62), docs/sop/jira-label-schema.yaml (~53), docs/sop/jira-label-conventions.md, config/capability_matrix.yaml, tests/test_lint_jira_ticket_spec.py.
Spec: ticket-spec RT-03; codex ticket-spec audit C1-C3/F2.
Out-of-scope: do NOT rewrite unrelated label namespaces; no JIRA taxonomy redesign.
AC — Code: add ci/gerrit to whitelist+schema+matrix; remove db from RUNNER_ONLY_AREAS in file_jira_ticket. Deploy: merged to develop (tooling runs from checkout). Integration: `file_jira_ticket.py --area db/ci/gerrit` all accepted. Exercised(verify): `python3 scripts/file_jira_ticket.py --check --area ci && --area gerrit && --area db` exit 0; validator+prompt-builder tests green. Go-Live: a test ticket with `area:ci`+`area:db` files + is runner-recognised.

## RT-01 — `[OP][RT-01] Freeze old release writers (timers + webhook runtime triggers) — disable only`
labels: type:docs(ops) · tier:X · class:operator · area:devops,docs · runner:no-commits-expected
prereqs: blocks_on=[RT-00]
Goal: Runtime-disable old release automation + webhook main triggers; document rollback boundary. Disable ONLY — no deletion, no code.
Files: systemd user units (auto-promote-main/develop, auto-tag-release, release-cut-guard*); webhook trigger config/env; docs/operations rollback-boundary note.
Spec: workplan P0.0a; ADR-0040 migration step 1.
Out-of-scope: NO repo deletion (=RT-17); NO webhooks code edit (=RT-23).
AC — Exercised(verify): `systemctl --user is-enabled auto-promote-main.service` = disabled (×all); no merge webhook triggers release CI (observe one merge). Go-Live: no timer/webhook can push refs/for/main or trigger release CI.

## RT-23 — `[BOT][RT-23] Webhooks CODE cleanup — remove main force-push + gh -r main / GitLab ref=main`
labels: type:feature · tier:M · class:sub-claude · area:backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-01]
Goal: Remove/retarget the main-hardcoded paths in webhooks.py to the release-train contract. Code only.
Files: backend/routers/webhooks.py (_on_change_merged ~1454, _trigger_ci_pipelines ~1648/1703), backend/tests/test_*webhook*.py.
Spec: ADR-0040 landmine #4; codex workplan E2.
Out-of-scope: NO systemd/timer changes (=RT-01).
AC — Exercised(verify): `grep -nE '"main"|ref=main|-r main' backend/routers/webhooks.py` = 0 release-path hits; tests prove merge webhook does not trigger release CI.

## RT-18 — `[OP][RT-18] Hotfix stop-the-line / no-fix-isolation policy`
labels: type:docs · tier:X · class:operator · area:docs · runner:no-commits-expected
prereqs: blocks_on=[RT-00]
Goal: Write the rule for shipping ONLY a fix when develop has moved ahead (revert-unready OR tag-from-last-release exception). Doc only.
Files: docs/operations/release-train-hotfix-policy.md (new).
Spec: ADR-0040 BLOCKING #3 + Open items.
AC — Exercised(verify): doc exists with an explicit decision rule; referenced by RT-16. Go-Live: policy linked from release runbook.

## RT-19 — `[OP][RT-19] Final-tag + promote actor protection`
labels: type:docs(ops) · tier:X · class:operator · area:security,devops · runner:no-commits-expected
prereqs: blocks_on=[RT-00]
Goal: Restrict who can create final `vX.Y.Z` image tags / run promote (release-train service + release-manager only); every tag/promote writes an immutable audit row.
Files: GitLab protected-tag settings, Gerrit tag ACL (.gerrit/project.config createTag), release-manager group; docs.
Spec: codex Model B audit H3.
AC — Exercised(verify): a non-authorised identity creating a final tag is rejected; promote without audit row fails.

## RT-20 — `[OP][RT-20] RECORD: final release identity = image-tag-only (decision locked)`
labels: type:docs · tier:X · class:operator · area:docs · runner:no-commits-expected
prereqs: blocks_on=[RT-00]
Goal: Record the locked decision (image-tag-only, no `v*` git tag) in ADR-0040 + release runbook; small follow-through only.
Spec: ADR-0040 §"Decisions LOCKED" RT-20.
AC — Exercised(verify): ADR-0040 + runbook state image-tag-only; consumed by RT-09/RT-12.

## RT-21 — `[OP][RT-21] Bridge runtime inventory → digest PAIR (backend+frontend)`
labels: type:docs · tier:X · class:operator · area:docs,devops · runner:no-commits-expected
prereqs: blocks_on=[RT-00]
Goal: Record that the release-train tracks backend+frontend only; bridge is a host control-plane daemon, decoupled. Inventory/decision only.
Spec: ADR-0040 §"Decisions LOCKED" RT-21; codex workplan E5.
AC — Exercised(verify): inventory doc lists actual prod-deployed runtime = {backend, frontend}; RT-12/13 consume the pair.

---

## Phase 0.1 — green signal (META RT-04 = Epic·X·class:operator·area:docs)

## RT-04a — `[BOT][RT-04a] Fast develop submit-gate CI job (per-change ONLY; NOT candidate certification)`
labels: type:feature · tier:M · class:sub-codex · area:ci,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-03]; mutex=[rt-greensig]
Goal: A fast per-change gate on develop (lint + targeted pytest + FE type/build + migration static) keyed by SHA. NOT the full candidate certification.
Files: .gitlab-ci.yml (new fast-gate job), tests.
Spec: workplan P0.1b(1).
Out-of-scope: candidate-SHA full certification = RT-04e/RT-09; green store = RT-04b.
AC — Exercised(verify): a deliberately-broken develop change goes red at submit, keyed by its SHA.

## RT-04b — `[BOT][RT-04b] Green-evidence store + query by full SHA`
labels: type:feature · tier:M · class:sub-claude · area:backend,db · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-03,RT-04a]; schema_locks=[green_evidence]
Goal: Persist+query per-full-SHA green status.
Files: backend/agents/<green_evidence>.py (new), alembic migration, backend/tests.
Out-of-scope: CI job = RT-04a.
AC — Exercised(verify): `green_status(<sha>)` returns pass/fail; absent SHA = fail-closed.

## RT-04c — `[OP][RT-04c] Enable Gerrit Verified on develop` — ⚠ DECOUPLED FROM THE TRAIN (2026-05-22)
labels: type:docs(ops) · tier:X · class:operator · area:gerrit,devops · runner:no-commits-expected
prereqs: blocks_on=[RT-03,RT-04a]
**⚠ NOT a release-train dependency. Decoupled from RT-04e on 2026-05-22 — do NOT re-link as a blocker of RT-04e/RT-09/RT-10a.** Reason: this is green-as-GATE (submit-time Verified ENFORCEMENT, blocks every develop merge), whereas the train needs green-as-RECORD (per-SHA, non-blocking — RT-04a fast checks + RT-04b store). Enforcing Verified contradicts RT-04e's "develop merges NOT blocked on full suite / non-blocking develop flow". See ADR-0040 §"Clarification (2026-05-22)".
**⚠ Do NOT flip the Verified SR until a live ci-bot Verified feeder is wired** (CI passes → ci-bot votes Verified+1 on the develop change). `.gitlab-ci.yml` has no Verified voting today → enabling the SR now would LOCK develop (every change needs Verified+1 that nobody grants). If pursued, use a FAST per-change check (not the 60-180min full suite) and treat it as a standalone branch-protection hardening, never a train blocker.
Goal (standalone hardening, deferred): activate Verified submit-requirement on develop (currently applicableIf=is:false) AFTER the feeder exists. Protected-setting activation only.
Files: .gerrit/project.config (Verified SR ~312) + the ci-bot Verified-feeder wiring (prereq).
AC — Exercised(verify): with the feeder live, a develop change without CI-asserted Verified cannot submit, and a passing change DOES get Verified+1 (so develop is not locked).

## RT-04d — `[BOT][RT-04d] Bad full-SHA rejection tests (tests only)`
labels: type:feature(tests) · tier:M · class:sub-codex · area:tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-04a,RT-04b]
Goal: Tests proving a bad/unknown full-SHA is rejected by the green-evidence/candidate gate. Tests only.
AC — Exercised(verify): pytest the negative cases green.

## RT-04e — `[BOT][RT-04e] Green-signal integration verifier (=activation evidence)`
labels: type:feature(tests) · tier:M · class:sub-claude · area:tests,devops · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-04a,RT-04b,RT-04c,RT-04d]
Goal: End-to-end verify of the green pipeline; candidate selection uses last-certified SHA; develop merges NOT blocked on the full suite. No new feature code.
AC — Exercised(verify): integration run shows green-by-SHA + non-blocking develop flow.

---

## Phase 0.2 — shared infra (parallel)

## RT-05 META (Epic·X·operator·docs) → children:
## RT-05a — `[BOT][RT-05a] Staging compose: GitLab CR + fail-closed ref + pull_policy (NO digest-deploy logic)`
labels: type:feature · tier:M · class:sub-codex · area:devops,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-00]; mutex=[staging-compose]
Goal: staging compose off GHCR/latest → GitLab CR; `${OMNISIGHT_IMAGE_TAG:?required}`; pull_policy always/digest.
Files: deploy/staging/docker-compose.yml; compose tests.
Out-of-scope: deploy-by-digest logic = RT-05b; JSONL evidence = RT-05d.
AC — Exercised(verify): `docker compose -f deploy/staging/docker-compose.yml config` FAILS on unset tag; no GHCR/latest refs remain.

## RT-05b — `[BOT][RT-05b] sync_staging deploy-by-digest + post-pull digest equality`
labels: type:feature · tier:M · class:sub-claude · area:devops,backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-05a]; mutex=[staging-sync]
Files: scripts/sync_staging_to_develop.sh, scripts/staging_deploy.sh, tests.
Out-of-scope: compose = RT-05a.
AC — Exercised(verify): pulled digests == candidate bundle; a mismatch is rejected.

## RT-05c — `[BOT][RT-05c] FE/BE compat = hard staging gate`
labels: type:feature · tier:M · class:sub-claude · area:backend,frontend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-05a]
Files: backend/frontend_compat.py, backend/routers/health.py (compat gate path), tests.
Out-of-scope: do NOT rewrite FE/BE version APIs beyond the gate.
AC — Exercised(verify): an injected FE/BE bundle skew BLOCKS the staging gate.

## RT-05d — `[BOT][RT-05d] Staging-gate evidence: JSONL digest pair + observed /api/version (NOT RT-08 prod overlay)`
labels: type:feature · tier:M · class:sub-codex · area:backend,devops,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-05a]; mutex=[api-versioning]  # shared with RT-08
Files: backend/agents/staging_gate.py, backend/api_versioning.py (staging-evidence fields only).
Out-of-scope: prod overlay semantics = RT-08.
AC — Exercised(verify): staging-gate JSONL record includes backend+frontend digests + bundle id + observed /api/version.

## RT-05e — `[BOT][RT-05e] Staging-fidelity integration (=activation evidence)`
labels: type:feature(tests) · tier:M · class:sub-claude · area:tests,devops · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-05a,RT-05b,RT-05c,RT-05d]
AC — Exercised(verify): end-to-end missing-ref-reject + digest-match + skew-block on staging. No feature code.

## RT-06 — `[BOT][RT-06] Cosign KEY-BASED trust consistency + unsigned/tampered negative tests (NO OIDC)`
labels: type:feature · tier:M · class:sub-codex · area:devops,tooling,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-00]
Goal: Make signer==verifier==audit-claim all KEY-BASED (current `COSIGN_KEY`); fix the false `cosign-keyless-gitlab-oidc` audit JSON. Do NOT implement OIDC.
Files: .gitlab-ci.yml (sign/attest/audit-emit ~143/179/259), scripts/verify_image_signature.sh (drop GH-keyless fallback), docs/operations/release-image-pipeline.md.
AC — Exercised(verify): a tampered/unsigned image FAILS the same verifier the promote+deploy gates call; audit JSON says key-based.

## RT-07 META → children:
## RT-07a — `[BOT][RT-07a] deploy-prod/check_deploy_ref final-tag-or-digest-only (NO allowlist/compose)`
labels: type:feature · tier:M · class:sub-claude · area:devops,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-00]; mutex=[prod-deploy-scripts]
Files: scripts/deploy-prod.sh (drop BRANCH=main default+MASTER_HEAD; kill --insecure-skip-verify w/o audit), scripts/check_deploy_ref.sh.
Out-of-scope: allowlist+compose = RT-07b.
AC — Exercised(verify): main/branch/rc/unset ref → REJECTED; final tag/digest passes.

## RT-07b — `[BOT][RT-07b] Prod allowlist + compose fail-closed OMNISIGHT_IMAGE_TAG required`
labels: type:feature · tier:M · class:sub-codex · area:devops,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-07a]; mutex=[prod-deploy-scripts]
Files: deploy/prod-deploy-allowlist.txt, docker-compose.prod.yml (`${OMNISIGHT_IMAGE_TAG:?required}`).
AC — Exercised(verify): unset tag in prod compose fails closed; branch entries removed.

## RT-07c — `[BOT][RT-07c] Prod-deploy rejection test harness (=activation)`
labels: type:feature(tests) · tier:M · class:sub-claude · area:tests,devops · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-07a,RT-07b]
AC — Exercised(verify): all reject cases pass; final-tag/digest passes. Test harness only — no prod deploy.

## RT-08 — `[BOT][RT-08] /api/version+/readyz deployment overlay (read-before-start from env lock)`
labels: type:feature · tier:M · class:sub-claude · area:backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-00]; mutex=[api-versioning]  # shared with RT-05d
Goal: Expose build_git_sha/ref + deployed_tag/digest(pair)/promotion_audit_id, READ ONCE AT STARTUP from a deploy-written env lock; fail-closed if missing (RT-08-pre decision).
Files: backend/api_versioning.py, backend/routers/health.py, tests.
Out-of-scope: staging-evidence fields = RT-05d; prod restart EXERCISE = RT-08b.
AC — Exercised(verify): unit test proves startup reads the lock; missing lock → /readyz fails.

## RT-08b — `[OP][RT-08b] Overlay activation evidence (prod restart exposes triplet)`
labels: type:docs(ops) · tier:X · class:operator · area:devops · runner:no-commits-expected
prereqs: blocks_on=[RT-08]
AC — Exercised(verify): `curl https://prod/api/version | jq '.deployed_tag,.deployed_digest_backend,.deployed_digest_frontend,.build_git_sha'` returns the live values after a prod restart.

---

## Phase 1 — Model A core

## RT-09 — `[BOT][RT-09] API/manual CANDIDATE_SHA=<40hex> pipeline; build sha-<fullsha>; NO cand/* tag; NO final git tag`
labels: type:feature · tier:M · class:sub-codex · area:ci,backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-04e,RT-05e,RT-06,RT-20]
Goal: Pipeline-API candidate build: fetch develop, assert ancestor, checkout --detach $CANDIDATE_SHA, build backend+frontend `sha-<fullsha>`, sign/attest by digest, no `:latest`, resource_group. NO git tag of any kind.
Files: .gitlab-ci.yml (candidate path), tests.
Spec: Model A "Candidate-build trigger (CHOSEN: API)"; RT-20 decision.
Out-of-scope: promote/retag = RT-12; do NOT introduce cand/* or v* tags.
AC — Exercised(verify): triggering with a chosen older green SHA builds exactly `backend:sha-<fullsha>`+`frontend:sha-<fullsha>`; no `:latest`/git-tag created.

## RT-10 META → children:
## RT-10a — `[BOT][RT-10a] release_train table + alembic + CAS + audit hard-gate`
labels: type:feature · tier:M · class:sub-claude · area:db,backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-04e]; schema_locks=[release_train]
Goal: New `release_train` table (candidate_sha, source_digests{backend,frontend}, reserved_version, promotion_state, actor, row_version, final_digest_equality); CAS; audit insert is a HARD gate (fail → promote fails). Separate from release_state.
Files: alembic migration, backend/agents/release_train.py (model+lock), backend/tests.
Out-of-scope: version reservation flow = RT-10b.
AC — Exercised(verify): 2 concurrent promote attempts → exactly one wins; audit-insert failure aborts before any tag write.

## RT-10b — `[BOT][RT-10b] Early version reservation for planning ONLY (fixVersion/META); NO image/git tag`
labels: type:feature · tier:M · class:sub-codex · area:backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-10a]
Files: backend/agents/release_train.py (reservation), backend release_conductor integration, tests.
Out-of-scope: NO image/git tag creation (that's promote=RT-12).
AC — Exercised(verify): release notes/changelog work pre-promote against the reserved fixVersion with no image tag existing.

## RT-10c — `[BOT][RT-10c] Concurrent-promote race test (tests only)`
labels: type:feature(tests) · tier:M · class:sub-claude · area:tests,backend · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-10a]
AC — Exercised(verify): race test asserts CAS single-winner; no CAS impl changes here.

## RT-11 — `[BOT][RT-11] Promote-time release-to-release migration compat gate (NO prod-rollback command)`
labels: type:feature · tier:M · class:sub-claude · area:backend,db,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-10a,RT-08]
Goal: Extend check_migration_compat to compare previous-final migration head vs candidate; BLOCK promote if rollback-unsafe unless audited break-glass.
Files: scripts/check_migration_compat.py (release-to-release mode), backend/agents/release_train.py hook, tests.
Out-of-scope: prod rollback command = RT-13a.
AC — Exercised(verify): a rollback-unsafe migration BLOCKS promote; break-glass requires audit row.

## RT-22 — `[BOT][RT-22] Remove unattended force-promote; audited human break-glass only; NEVER bypass digest equality`
labels: type:feature · tier:M · class:sub-codex · area:backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-10a]
Files: scripts/release_milestone_checker.py (force-promote paths), backend/agents/release_train.py, tests.
AC — Exercised(verify): unattended force path is gone; emergency override needs human cmd + exact SHA + audit reason; negative test proves digest-equality cannot be bypassed.

## RT-12 — `[BOT][RT-12] Promote by retagging validated GitLab CR digests to reserved version; CONSUME RT-20/RT-21`
labels: type:feature · tier:M · class:sub-claude · area:devops,backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-05e,RT-06,RT-10a,RT-11,RT-19,RT-20,RT-21,RT-22]
Goal: Rewrite promote_image_bundle for GitLab CR + backend+frontend (pair, per RT-21) + retag validated digest→`vX.Y.Z` (no rebuild, no git tag per RT-20) + verify each final tag resolves to expected digest + per-image attestation + 1 bundle audit row (hard-gate) + idempotent failed state.
Files: scripts/promote_image_bundle.py, backend/agents/release_train.py, tests.
Out-of-scope: do NOT decide final-tag semantics or bridge shape (consume RT-20/RT-21).
AC — Exercised(verify): promote only after staging(RT-05e)+migration(RT-11) gates; final image tag → exact validated digest; a fresh `v*` git tag is NEVER created and would-not-rebuild (test).

## RT-13a — `[BOT][RT-13a] release_train_status.py status+rollback digest resolver TOOL ONLY`
labels: type:feature · tier:M · class:sub-codex · area:tooling,backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-08,RT-10a]
Files: scripts/release_train_status.py (new), tests.
Out-of-scope: prod exercise = RT-13b.
AC — Exercised(verify): tool joins /api/version overlay + compose lock + registry + release_audit → `vX.Y.Z == git_sha == {backend,frontend digests}`; `--to vX.Y.Z` resolves the digest pair.

## RT-13b — `[OP][RT-13b] Operator status+rollback EXERCISE against staging/prod-like`
labels: type:docs(ops) · tier:X · class:operator · area:devops · runner:no-commits-expected
prereqs: blocks_on=[RT-13a,RT-21]
AC — Exercised(verify): operator resolves prod identity + performs a digest rollback in one command; durable evidence captured.

## RT-14 — `[BOT][RT-14] GitLab CR retention + release_train protection`
labels: type:feature · tier:M · class:sub-codex · area:devops,tooling,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-10a]
Files: scripts/enforce_image_retention.py (GitLab CR + sha-<40hex> classes), tests.
AC — Exercised(verify): an old unpromoted `sha-*` is GC'd; a promoted-source / release_audit-referenced digest is protected (never deleted).

## RT-15 META → children:
## RT-15a — `[BOT][RT-15a] GET /api/feature-flags/effective (public, server-eval, fail-closed) + allowlist`
labels: type:feature · tier:M · class:sub-claude · area:backend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-00]; schema_locks=[ff-effective-contract]
Files: backend/routers/feature_flags.py (new effective endpoint + public allowlist), backend/feature_flag_sdk.py, tests.
Out-of-scope: FE consumption = RT-15b.
AC — Exercised(verify): endpoint returns only public flag booleans for the authed tenant; DB error → false (fail-closed); no owner/rollout/tenant leaked.

## RT-15b — `[BOT][RT-15b] FE feature-flag provider/hook/SSR bootstrap; CONSUME RT-15a contract`
labels: type:feature · tier:M · class:sub-codex · area:frontend,tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-15a]
Files: frontend lib/api.ts (effective-flags client), FE provider/hook, SSR bootstrap; align tier enum (BE early_access/staged/ga ↔ FE).
AC — Exercised(verify): an incomplete-UI flag hides the UI; FE/BE tier enums match (test).

## RT-15c — `[BOT][RT-15c] FE-flag integration`
labels: type:feature(tests) · tier:M · class:sub-claude · area:tests · capability:enable=gerrit_push · agent:auto
prereqs: blocks_on=[RT-15a,RT-15b]
AC — Exercised(verify): dark-ship verified end-to-end. No new API/provider code.

---

## Phase 2 — cutover (after dry-run)

## RT-16 — `[OP][RT-16] End-to-end dry-run (integration gate)`
labels: type:docs(ops) · tier:X · class:operator · area:devops,tests · runner:no-commits-expected
prereqs: blocks_on=[RT-09,RT-12,RT-13b,RT-15c,RT-07c,RT-08b,RT-18]
AC — Exercised(verify): full `sha → candidate → staging-by-digest → migration gate → promote vX.Y.Z → prod-by-digest → status → rollback` runs once; durable evidence JSON archived.

## RT-17 — `[OP][RT-17] Post-dry-run cutover cleanup: delete old automation + Gerrit cutover + retire main per accepted ADR`
labels: type:docs(ops) · tier:X · class:operator · area:devops,gerrit · runner:no-commits-expected
prereqs: blocks_on=[RT-16,RT-03]
AC — Exercised(verify): old automation deleted; main/release-cut SRs removed; `main` retired per the ADR-accepted decision (vestigial pointer or deleted); deployment-audit shows no orphaned old units.
