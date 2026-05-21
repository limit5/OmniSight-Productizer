---
id: ADR-0040
title: Single-trunk Release Train — retire `main`, tag-driven releases
status: Proposed
date: 2026-05-21
---

# ADR-0040 — Single-trunk Release Train (retire `main`, tag-driven releases)

**Status**: Proposed (2026-05-21) — awaiting operator acceptance. Implementation/migration tracked separately (see §Migration).

**Supersedes**: ADR-0001 (Five-branch Git Flow), ADR-0020 (Release cut to `main` as a single merge change), ADR-0039 (Release-cut submit-requirement hashtag/applicableIf), ADR-0016 (D5 develop→main promotion — already historical). Obsoletes the `develop→main` promotion portions of ADR-0010 (Deployment Automation Gates) and ADR-0011 (Sprint D plan).

**Amends**: ADR-0017 (Release conductor), ADR-0018 (Event-driven release pipeline), ADR-0019 (`release:force-promote` override), ADR-0021 (Release Pipeline Coordinator).

**Reconciles**: ADR-0023 (Foundation Rebuild, Draft) — its premise that "the five-branch design **stays** … Phase 31.K marks ADR-0001 Accepted+Implemented" is **reversed** by this ADR.

**Relates**: ADR-0038 (Image pipeline on GitLab — kept; this ADR depends on its tag-driven build), ADR-0002 (GitLab primary / GitHub mirror), ADR-0003 (Gerrit review).

---

## Context

ADR-0001 adopted full GitFlow (5 long-lived branch types) on 2026-05-04 to handle multi-AI parallelism. In practice (2026-05): `develop` and `main` diverged continuously; `main` rotted **175 commits** behind develop; the operator repeatedly force-aligned `main` and hard-cut releases. The develop→main release-cut machinery (ADR-0016 → ADR-0020) wedged on 2nd+ cuts (AUDIT-26), and its `auto_promote_main` consumer was never activated (AUDIT-26d). Meanwhile the GitLab CI image pipeline (ADR-0038) is **tag-driven and branch-agnostic** (`workflow: $CI_COMMIT_TAG =~ /^v.*/`) and already builds prod images successfully (pipeline #32 = v0.5.0-rc5-hotfix4). Prod has de-facto been running tagged **develop** commits; `main` had no real consumer.

GitFlow's two-long-lived-branch model is designed for versioned, multi-release software, not a continuously-delivered web app with fast AI-driven velocity. The drift, churn, and wedging are inherent to that mismatch.

## Decision

Adopt a **single-trunk Release Train**:

1. **`develop` is the only long-lived trunk.** Runner per-ticket `feature/*` → `refs/for/develop` → Gerrit +2 submit. Ingress unchanged.
2. **`main` is retired.** No develop→main merge, no FF-from-release, no promotion to main.
3. **A release = a promoted IMAGE tag + digest recorded in `release_train`** (NOT a `v*` git tag — see Decision RT-20 below; a git tag would trigger the existing CI `^v` build rule and rebuild a different digest, breaking "validated digest == shipped digest"). Candidate images are built from a chosen develop SHA via pipeline-API (`CANDIDATE_SHA`); promotion retags the validated digest to `vX.Y.Z` in GitLab CR.
4. **No `release/X.Y` branches.** Hotfixes are also tags (`v0.6.1`) cut from develop. **Every `v*` tag MUST be a prod-usable build** — there is no stabilisation branch to repair on.
5. **Promotion is by image digest**, not rebuild: the tagged image rides staging → canary (prod backend-a) → prod (backend-b).
6. **Auto-cut produces a candidate to STAGING; prod promotion stays gated.** An auto-scheduled tag must be cut from a *green* develop commit (see Consequences — a develop-green signal is a prerequisite, currently missing).
7. **Frontend feature flags will be added** so incomplete FE features can dark-ship through a train (today only the backend `feature_flags` SDK has consumers).

## Consequences

**Positive**: kills main-rot, the force-align loop, the cut-wedging (AUDIT-26), and the coordinator's develop→main promote rehearsal. Version anchor becomes the tag = one CI pipeline = pinned backend+frontend+bridge. Fewer moving parts.

**Costs / new disciplines required**:
- **No fix isolation.** Without a stabilisation branch, a hotfix cannot ship "only the fix" if develop has moved ahead with unvetted commits. Mitigated only by (a) frequent trains, (b) green-cut, (c) feature flags. **Frequent cadence is mandatory, not optional.**
- **"Every tag prod-usable" needs a develop-green gate.** `.gitlab-ci.yml` has no test/lint stage today (tests run per-change at Gerrit submit). A "develop tip is green now" signal must be built before auto-cut is safe.
- **FE feature-flag wiring is net-new work** (API to expose flag state + FE consumption + fail-closed parity).

**Migration blast radius** (must change atomically with retiring main — see `memory: project_retire_main_blast_radius_audit` + codex audit `/tmp/retire-main-release-train-codex-audit-2026-05-21.txt`): 5 live `main`-hardcoded landmines (`scripts/deploy-prod.sh`, `backend/agents/auto_tag_release.py`, `scripts/staging_deploy.sh`, `backend/routers/webhooks.py`, `.gerrit/project.config` Human-Plus-2 carve-out). Plus deletion of dormant promote machinery, retirement of the release-branch family, ~6 ADR cross-ref updates, and dedup of the duplicate `NNNN-*.md` / `ADR-NNNN-*.md` ADR files.

## ⛔ BLOCKING design holes surfaced by the codex audit (2026-05-21)

These MUST be resolved before this ADR moves Proposed → Accepted. Full audit: `/tmp/retire-main-release-train-codex-audit-2026-05-21.txt`.

1. **Candidate-artifact circular dependency (the deepest hole).** `scripts/sync_staging_to_develop.sh` stages an image tagged with the raw develop SHA, but the GitLab pipeline only builds on `v*` tags (publishes `${tag}`, `sha-${short}`, `latest`) — it never builds a raw develop-SHA image. So "validate on staging BEFORE cutting the tag" is impossible: the candidate image doesn't exist until a tag exists. **Must pick a candidate-artifact model first**: (a) build immutable images for every accepted develop commit (branch pipeline → `sha-${short}`), or (b) auto-create an `rc`/pre-release tag, stage THAT, then promote to final `v*`, or (c) accept that the final tag exists pre-validation and gate DEPLOY (not the tag). Each has different operator semantics.
2. **"Every tag prod-usable" has no test gate.** `.gitlab-ci.yml` has no test/lint stage; Gerrit `Verified` is disabled (`applicableIf=is:false`). A `v*` tag can build+sign+attest without ever proving the suite passed at that SHA — and a bad tag is a *permanent* release object. Auto-cut needs a real per-SHA green signal (pytest + FE build/typecheck + migration check + smoke/canary) before it may tag.
3. **Hotfix isolation gap (stop-the-line policy needed).** With no stabilisation branch, `v0.6.1` from develop ships *every* later develop commit too. Need an explicit written policy: revert/disable unready develop changes before tagging, OR a defined tag-from-last-release-tag exception.
4. **Rollback ≠ tag rollback once migrations ran.** `deploy-prod.sh` runs alembic upgrade on deploy; previous-tag redeploy does NOT undo schema. Need an expand/contract migration gate + explicit "when previous-tag rollback is valid vs needs DB intervention" doc.
5. **Supply-chain signing inconsistency.** Pipeline signs with `COSIGN_KEY` but audit JSON + docs claim `cosign-keyless-gitlab-oidc`. Either implement OIDC keyless or correct the provenance claims — otherwise "immutable tag = trustworthy artifact" is undermined.
6. **FE feature-flag is net-new work, not a flip.** Existing `backend/routers/feature_flags.py` is an operator/admin registry (leaks owner/rollout/tenant). Need: a frontend-safe `GET /api/feature-flags/effective` (public booleans only, server-evaluated, fail-closed), an FE provider/hook + SSR bootstrap, and **fix the tier-enum drift** (backend `debug/dogfood/early_access/staged/ga` vs FE `debug/dogfood/preview/release/runtime`).
7. **Registry/`latest` "green-but-not-deployed" risk.** Staging compose still defaults GHCR `:latest`; prod compose defaults `OMNISIGHT_IMAGE_TAG=latest`. Unset tag → deploys newest regardless of approval. Make staging+prod **fail closed** on unset tag; share one registry/tag/digest contract across CI/staging/prod/health. Also: expose `git_ref`/`git_sha` in `/api/version`; make `frontend_compat` an actual gate or stop documenting it as one.

## Migration (codex-recommended order — do NOT big-bang)

1. **Freeze old release automation** (disable auto-promote-main, auto-promote-develop, auto-tag-release, release-cut guards) before any behaviour change.
2. **Land this ADR + runbook contract** (supersede ADR-0001/0020 before code).
3. **Convert deploy validation atomically**: `deploy-prod.sh` + `scripts/check_deploy_ref.sh` + `deploy/prod-deploy-allowlist.txt` + runbook — tag/digest-only, refuse branch refs.
4. **Resolve the candidate-artifact model + staging/build** before enabling auto-cut (hole #1).
5. **Add FE runtime flags** before the first trunk release carrying incomplete UI.
6. **Replace Gerrit main/release rules with develop/tag gates**; decide whether `Verified` becomes active on develop.
7. **Update conductor/coordinator** to key release identity on immutable tag/SHA/digest, not main promotion.
8. **Dry-run one `v*` tag end-to-end** before retiring `main`.

**Atomic change groups**: {deploy-prod.sh + check_deploy_ref.sh + allowlist + runbook} · {staging-sync + GitLab publication + staging compose registry/tag defaults} · {Gerrit project.config + auto-promote/release-cut systemd disablement} · {FE effective-flags endpoint + lib/api.ts client + provider/hook + enum alignment} · {ADR supersession + runbook + rollback policy}.

**Rollback**: pre-cut → re-enable old units/allowlist only if no v* tag was cut under the new model. Post-cut → never delete/move immutable tags; roll back by deploying the previous approved `v*` tag/digest (subject to migration compat); mark a bad cut in audit metadata rather than retagging.

## Minimum acceptance checklist (from codex)
- No service/timer/script can push `refs/for/main` or update `release/v*`.
- Prod deploy refuses `main`/`release/*`/`hotfix/*`/any non-`v*` ref.
- Staging + prod pull the same registry namespace + immutable tag/digest scheme; unset tag fails closed.
- An exact develop SHA can be proven green before a final prod tag — OR final tags are explicitly post-build candidates until gates pass.
- FE flags: frontend-safe runtime API + client provider + fail-closed + tier-enum alignment.
- Supply-chain audit truthfully reflects actual signing mode + final image digests.
- ADRs + operator docs make the trunk/tag model normative; old main/release-cut ADRs marked superseded.

## Decisions LOCKED 2026-05-21
- **Candidate-artifact model = revised Model A** (`docs/design/2026-05-21-model-a-candidate-build-release-train.md`): build `sha-<fullsha>` from a chosen green develop SHA via pipeline-API; validate on staging by digest; promote = retag the SAME digest to the reserved `vX.Y.Z`. Resolves BLOCKING hole #1.
- **RT-20 (final release identity) = IMAGE-TAG-ONLY.** No `v*` git tag is created (avoids the CI-rebuild hazard with zero fragility — no need to special-case the CI workflow rule). Release identity = promoted GitLab CR image tag `vX.Y.Z` (→ validated digest) + `release_train`/`release_audit` row. Git/human provenance = the audit row (git_sha + digests); optionally a NON-`v*`-matching ref like `released/vX.Y.Z` created post-promote (CI never fires on it). Rationale: prod already runs pinned images not git refs; main is retired; the audit row is the authoritative record; avoids fragile CI tag-exclusion logic.
- **RT-21 (bridge) = release-train tracks BACKEND + FRONTEND digests only (a PAIR, not a triplet).** Verified this session: prod compose deploys backend-a/b + frontend (no bridge container); the gerrit-jira-bridge/coordinator run as host daemons from the `sora-bridge` develop checkout (control-plane, separate concern). The `bridge` image build is decoupled from the release-train promote/deploy gate. (If bridge is ever containerised into prod, add it then.)
- **RT-08-pre (overlay load semantics) = READ-BEFORE-START from an env lock.** The deploy/promote step writes a digest+identity lock; the container reads it once at startup; `/api/version` serves it. Rationale: deploys always rolling-restart anyway, so "needs restart to update" is not a limitation; deterministic; no per-request IO or cache-invalidation failure modes; fail-closed (missing lock at startup → readiness fails → deploy gate catches it).

## Open items (still operator decisions)
- Auto-cut cadence + trigger owner (operator vs pipeline-coordinator "列車長").
- `main`: vestigial "last-deployed" pointer vs delete outright (RT-17).
- Hotfix stop-the-line policy wording (RT-18 — owner to draft).
