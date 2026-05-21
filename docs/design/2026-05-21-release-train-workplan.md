# Release-train migration — work plan **v2** (revised Model A, post codex audit)

**Status**: v2 — incorporates codex work-plan audit 2026-05-21 (`docs/audit/codex-reviews/release-train-workplan-codex-audit-2026-05-21.txt`, verdict on v1 = NO-GO-as-written → GO-WITH-FIXES). Target = revised Model A (`docs/design/2026-05-21-model-a-candidate-build-release-train.md`) under ADR-0040. Builds on 4 prior codex audits in `docs/audit/codex-reviews/*2026-05-21*`.

## What v1 got wrong (fixed here)
1. **Critical-path ordering bug** — v1 promoted (P1.4) BEFORE the staging gate (P1.5), inverting Model A's `candidate → staging → promote` invariant. **Fixed**: staging gate + migration gate now BLOCK promote.
2. **Freeze too late** — v1 retired old release automation in Phase 2; ADR-0040 says freeze it FIRST. **Fixed**: P0.0 freeze (disable, not delete).
3. **4-AC was prose, not machine-checkable** — would repeat AUDIT-23 (shipped-but-not-deployed). **Fixed**: every AC must be an executable verify block + host evidence; `deployment-audit.sh` extended to cover release-train checks (P0.0b).
4. **Runner labels not real** — `area:ci`/`area:gerrit` are NOT in the runner whitelist; several 🤖 items are inherently 👤. **Fixed**: runner-vocab prep (P0.1a) + honest 👤/🤖 split (code child 🤖, activation child 👤).
5. **Dependency cycle** — P0.6 depended on P1.3 while in Phase 0. **Fixed**: split P0.6a (Phase 0) + P1.7 (Phase 1).
6. **Dropped items** — FE feature flags + `backend/routers/webhooks.py` (a landmine) were missing. **Fixed**: added (P1.8, P0.0 freeze covers webhooks).
7. **Audit best-effort** — `release_audit`/staging audit writes are non-fatal (DSN-silently-missing history). **Fixed**: hard-gate audit for promotion + prod deploy.

## Standing rules
- **4-AC per item must be machine-executable** (Code · Deploy · Integration · Exercised + Go-Live), with concrete verify commands + host evidence (`systemctl`/`docker inspect`/`curl …/api/version | jq`/registry digest/`alembic current`). "Merged to develop" is NOT evidence (`docs/sop/architecture-anti-patterns.md` cure).
- **Every sibling group has an integration ticket at filing time**, blockedBy all siblings (no "add it later"). Parent META can't close until integration closes.
- 🤖 = code child runner-pickable; 👤 = operator/ops-only (`runner:no-commits-expected`) for protected GitLab/Gerrit settings, prod env locks, prod rollback, systemd unit removal.
- **Migration operating mode**: develop feature work keeps flowing; only release-cut/tag/promote automation is frozen during the window; manual prod deploy stays on the last approved final tag until P2.5 dry-run passes.

---

## Corrected sequence (codex 15-step)

### Phase 0.0 — freeze + guards (FIRST, before any behaviour change)
- **P0.0a — Freeze old release writers** 👤: disable (not delete) `auto-promote-main`, `auto-promote-develop`, old `auto_tag_release`, release-cut guard timers, AND `backend/routers/webhooks.py` main force-push + `gh -r main`/GitLab `ref=main` CI triggers. Document rollback boundary. *Exercised: no timer/webhook can push `refs/for/main` or trigger release CI on main.*
- **P0.0b — Release-train audit harness** 🤖: extend `scripts/deployment-audit.sh` with check kinds `gitlab-sha-status`, `registry-digest`, `cosign-verify`, `deploy-ref-rejected`, `http-json-field`, `release-train-row`, `disabled-unit`. *Exercised: a not-activated release-train item shows RED.* (Without this the 4-AC guard is unenforceable.)

### Phase 0.1 — filing prep + green signal
- **P0.1a — Runner vocabulary/filing prep** 👤+🤖: add (or decide against) `area:ci` + `area:gerrit` to `RECOGNISED_AREAS` (`auto-runner-jira.py:143`), `jira-label-conventions`, `config/capability_matrix.yaml`, validator + prompt-builder tests. Define per-item filing metadata (issuetype/tier/type/areas/capabilities/mutex/blockedBy). Add integration-ticket skeletons.
- **P0.1b — Fast exact-SHA green signal** 👤+🤖 (decomposed): split per codex B1 — (1) **fast per-change submit gate** on develop (lint + targeted pytest + FE type/build + migration static check) keyed by SHA; (2) **candidate-SHA certification** (full pytest + FE test/build + build/sign/sbom/attest + staging + release-to-release migration) keyed by full SHA. Candidate selection uses "last certified SHA" — does NOT block develop merges on the full 60-180min suite. Children: CI job · green-evidence store/query · Gerrit-Verified-enable/branch-protection (👤) · bad-SHA negative test · integration verifier.

### Phase 0.2 — shared infra (parallelizable)
- **P0.2/P1.5 — staging fidelity (ATOMIC GROUP + integration ticket)** 🤖+👤: `deploy/staging/docker-compose.yml` off GHCR/`latest` → GitLab CR; fail-closed unset tag; `pull_policy: always`/digest-pinned; deploy candidate BY DIGEST; migrations BEFORE ingress switch; **FE/BE compat = hard staging gate** (today `frontend_compat` is telemetry only); staging-gate JSONL records digest triplet + observed `/api/version`. *Exercised: missing ref rejected; pulled digests == candidate bundle; skew BLOCKS promote.*
- **P0.3 — cosign trust-mode consistency (ATOMIC)** 🤖: pick key-based (current); fix audit JSON (`signed_with` is FALSE), `verify_image_signature.sh` (drop GH-keyless fallback), docs. *Exercised: tampered/unsigned image FAILS the same verifier the promote+deploy gates call.*
- **P0.5 — prod-deploy lockdown (ATOMIC: deploy-prod.sh + check_deploy_ref.sh + allowlist + prod compose)** 🤖+👤: final `vX.Y.Z` tag/digest ONLY; reject branch/`release/*`/`hotfix/*`/`-rc`; drop `BRANCH=main` default + `MASTER_HEAD` metadata; remove `--insecure-skip-verify` w/o audited break-glass; **prod compose `${OMNISIGHT_IMAGE_TAG:?required}`** (no `latest` default). *Exercised: main/branch/rc/unset all REJECTED.*
- **P0.7 — build/deploy identity overlay** 🤖: `/api/version`+`/readyz` expose `build_git_sha`/`build_git_ref` + `deployed_tag`/`deployed_digest_{b,f,br}`/`promotion_audit_id`. **Define overlay load semantics** (read-before-start from guaranteed env lock, OR per-request with cache-invalidation + error state). *Exercised: prod restart exposes new triplet; missing/mismatched overlay fails the gate.*

### Phase 1 — Model A core (after P0 green+staging+lockdown+overlay LIVE)
- **P1.1 — CANDIDATE_SHA candidate pipeline** 🤖: API/manual `CANDIDATE_SHA=<40hex>` → fetch develop + `merge-base --is-ancestor` assert + `checkout --detach` → build `sha-<fullsha>` (3 images) → sign/attest BY DIGEST → no `:latest` → `resource_group` guard. NOT a `cand/<sha>` git tag.
- **P1.2/P1.3 — early version reserve + `release_train` lock** 🤖+👤: reserve `vX.Y.Z` (JIRA fixVersion + RELEASE META) for planning, NO image/git tag until promote. New `release_train` table (separate from `release_state` — codex F1): `candidate_sha, green_evidence, source_digests{b,f,br}, reserved_version, promotion_state, actor, row_version, final_tag_digest_equality`; CAS; migration+indexes+uniqueness; **audit insert is a hard gate**. *Exercised: 2 concurrent promotes → exactly one wins.*
- **P0.4 — release-to-release migration rollback gate** 🤖 (after release_train/provenance exist): previous final digest's migration head vs candidate; BLOCK rollback-unsafe promote unless break-glass.
- **P1.4 — promote job** 🤖 (blocked by P1.5 + P0.4 evidence): rewrite `promote_image_bundle.py` for GitLab CR + all 3 images + retag digest→`vX.Y.Z` (no rebuild) + verify each final tag resolves to expected digest + per-image attestation + 1 bundle audit row (hard-gate) + idempotent failed state. **Decide final git-tag semantics** (codex E4): final git tag either excluded from CI build trigger OR no final git tag (identity = image tag/digest + release_train row); add a test that final-tag creation does NOT rebuild.
- **P1.6 — operator status + rollback tool** 👤+🤖 (BLOCKS P2.5 + first prod go-live): `release_train_status.py --env prod` joins runtime `/api/version` + compose lock + registry resolution + release_audit → `vX.Y.Z == git_sha == {digests}`. Rollback `--to vX.Y.Z` resolves digest triplet from immutable audit.
- **P1.7 — GitLab CR retention + release_train protection** 🤖 (split from P0.6; deps P1.3): final digests forever; source SHA digests referenced by finals retained; SBOM/attest/audit mirrored for finals; unpromoted `sha-*` deleted after N days; never delete a release_train/audit-referenced digest.
- **P1.8 — FE runtime feature flags** 🤖+👤 (ADR-0040 must-fix, dropped in v1): frontend-safe `GET /api/feature-flags/effective` (public booleans only, server-evaluated, fail-closed) + public allowlist + FE provider/hook/SSR bootstrap + **fix tier-enum drift** (BE `debug/dogfood/early_access/staged/ga` vs FE `preview/release/runtime`). Required before any trunk release carrying incomplete UI.

### Phase 2 — cutover (ONLY after P2.5 dry-run)
- **P2.5 — end-to-end dry-run (integration ticket, blockedBy all siblings)** 👤: `sha → candidate → staging-by-digest → migration gate → promote vX.Y.Z → prod-by-digest → status → rollback`; durable evidence JSON.
- **P2-cleanup (after dry-run)** 👤+🤖: DELETE old automation (freeze was P0.0a); Gerrit cutover (remove main/release-cut SRs, enable develop Verified); ADR-0040 Proposed→Accepted + supersede flips + cross-refs + ADR-0023 reconcile; `main` vestigial-pointer-or-delete decision.

## Force/break-glass policy (codex F3)
No unattended force-promote. Emergency override = human command + exact SHA + immutable audit reason + explicit record of bypassed gates. MUST NOT move final tags or bypass digest equality.

## Rollback checkpoints (codex D4)
- After P0.5, before P1.4: rollback = revert deploy-prod/check_deploy_ref/allowlist as one reviewed change (only if no new final digest promoted yet).
- After first promotion: no ref movement; deploy previous approved digest via release_train_status rollback.
- Every checkpoint: tested command + release_audit row.

## ⚠️ ADR sequencing (codex E3)
Accept ADR-0040 (or a "migration contract" doc) BEFORE filing code tickets, so runners/reviewers don't see old ADRs calling main authoritative. P2-cleanup only does obsolete-cross-ref tidy.

## Top-5 before filing ANY ticket
1. Fix ordering: P1.5 staging gate + P0.4 migration gate BLOCK P1.4 promote.
2. P0.0 freeze old writers/webhooks now (not Phase 2) + rollback policy.
3. Runner vocab: add `area:ci`/`area:gerrit` or mark those items 👤; honest filing metadata per item.
4. Machine-executable ACs + integration tickets + extend `deployment-audit.sh` (P0.0b).
5. Restore dropped blast-radius: `webhooks.py`, FE flags, final-git-tag/no-rebuild semantics, release_audit hard-gating.
