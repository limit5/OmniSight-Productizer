# Deploy-line remediation — ticket-split draft (2026-05-25)

Remediates the 35 findings in `docs/audit/2026-05-25-deploy-pipeline-deep-audit.md`. Pipeline:
draft → codex audit → blind-test (risky) → file → execute. scope:`deploy-remediation`. One roll-up META.
**De-risk order: security stop-bleed (A) → enforcement (B) → promote/prod (C) → dev-isolation (D) →
cross-cutting (E) → low/doc/decisions (F).** Owner tag: 🤖=agent:auto · 👤=tier:X operator/me.

Coverage map at bottom (every finding #1–#35 → a ticket).

## codex round-1 (NEEDS-REVISION) — folded in
- **+B4 (#13)** added: staging `.env`/compose `OMNISIGHT_IMAGE_TAG` pinning (was missing from coverage).
- **B1 split**: B1a backend digest-compare (🤖) + **B1b deploy-side running-digest injection (👤 tier:X)** — the
  bundle is NOT an honest source (it carries the #23 zeros); the real digest must be injected at container start
  (host `docker inspect`). The /readyz mismatch-503 enforcement ships **default-OFF** until B1b verified (don't brick prod).
- **C2**: use per-image refs `OMNISIGHT_{BACKEND,FRONTEND}_IMAGE_REF=…@sha256:…` (one digest can't pin the pair).
- **D1**: `REVOKE CONNECT … FROM PUBLIC` + `GRANT CONNECT` back to the prod app/operator roles + verify
  `has_database_privilege` (revoking only the dev role is insufficient — PUBLIC has CONNECT by default).
- **A2**: also update/remove the 4 GHCR contract tests (`test_docker_publish_workflow.py`,
  `test_compose_prod_image_first.py`, `test_pull_30s_effect.py`, `test_build_images_pipeline.py`); REMOVE
  `docker-publish.yml` (don't re-point); **A2 `Blocks: A1`** (disable-on-GitHub before repo-retire).
- **B3**: DEPRECATE/test-only-mark `orchestrator/canary.py` + switch the runbook (don't delete; tests/docs/ADR ref it).
- **F3**: EXCLUDE `deploy/blue-green` — it's a LIVE canary dep (`canary_rollout.py:22-25`); only retire the truly-dead targets.
- **D4 split**: compose-side (remove `OMNISIGHT_DATABASE_PATH`/require URL) = 🤖; applying prod `.env` = 👤 tier:X. SQLite stays legit for dev/tests.
- **Owner reclass → 👤 tier:X (or agent-patch + operator-apply/verify)**: B1b, C2 (prod refs, verify on staging first),
  D4-apply, D1-apply. **Security-sensitive (🤖 but human-+2 must scrutinize)**: D2, D3 (auth guard), E3 (config), A2 (CI).
- VERDICT was NEEDS-REVISION → revisions above applied → now SAFE-TO-FILE.

**B4 `[GATE][devops] Pin staging OMNISIGHT_IMAGE_TAG (no `latest`) + compose fail-closed on unset`** 👤 tier:X
- Covers #13. Set staging `.env` to a pinned `sha-<sha>`/`vX.Y.Z` (not `latest`); make compose reject `latest`
  as well as unset (not just `${VAR:?}`). agent patches the compose guard; operator sets the staging `.env`.

## META `[META] Deploy-line remediation — 2026-05-25 deep audit (35 findings)`
- type:meta · tier:X · scope:deploy-remediation. Links all children + the audit doc + codex review.

---
## Group A — Security stop-bleed (P0)
**A1 `[GATE][devops] Disable GHCR GitHub Actions on the mirror repo (stop the re-armed builds)`** 👤 tier:X
- Covers #2 (GitHub-side). Immediate stop-bleed: my OP-1687 mirror restore re-triggered `build-images.yml`/
  `docker-publish.yml`/`Release` (failing) on every develop push. Disable those 3 workflows on
  `limit5/OmniSight-Productizer` via GitHub API (PUT .../actions/workflows/{id}/disable) using the OP-1687 PAT.
- Accept: the 3 workflows `state=disabled`; next mirror push triggers none. MUST NOT: disable the DR/backup workflows.

**A2 `[OP][devops] Retire GHCR + ^v-tag release automation from develop (ADR-0042/RT-20 conformance)`** 🤖 agent:auto
- Covers #2 (repo-side) + #14. Remove/neutralize `.github/workflows/build-images.yml` + `docker-publish.yml`
  (GHCR builders); remove the `.tag_rules` job family + the `^v` `workflow:` admit in `.gitlab-ci.yml`; retire
  `auto_tag_release.py` + `deploy/systemd/auto-tag-release.service` (ADR-0040 migration step 1).
- Files: `.github/workflows/{build-images,docker-publish}.yml`, `.gitlab-ci.yml` (.tag_rules + workflow rules),
  `backend/agents/auto_tag_release.py`, `deploy/systemd/auto-tag-release.service`. **MUST NOT**: touch the live
  `candidate-*` jobs or the fast-gate; do not delete DR/backup workflows; keep `docker-publish.yml` only if it's
  re-pointed off ghcr (else remove). Spec: ADR-0040 RT-20 + migration §1, ADR-0042.

**A3 `[OP][ci] CI cosign: add --tlog-upload=false to all sign/attest (stop Rekor leak)`** 🤖 agent:auto
- Covers #1. Add `--tlog-upload=false` to the 4 `cosign sign|attest` calls (`.gitlab-ci.yml:404,466,497,550`).
- Accept: a candidate build signs WITHOUT uploading to public Rekor (verify locally that the flag is present on
  all 4). MUST NOT: change the key/predicate-type, disable signing, touch promote-side signing (already correct).

---
## Group B — Gating/enforcement correctness (P0/P1)
**B1 `[OP][backend] /readyz overlay gate: cross-check lock digest vs the running image`** 🤖 agent:auto
- Covers #3. `_check_deploy_overlay` (health.py:481-490) must compare the lock's `deployed_digest_*` against the
  ACTUAL running image digest (the container's own image RepoDigest / a startup-captured digest), not just shape.
  Fail-closed (503) on mismatch. Files: `backend/routers/health.py`, `backend/api_versioning.py` + tests.
  **MUST NOT**: weaken the existing absent/incomplete fail-closed; no change to the lock writer. Design: how does
  the backend learn its own running digest? (env injected at deploy, or read from the bundle) — pick the minimal honest source.

**B2 `[GATE][devops] Smoke gate: install timer + operator credential + prove it can green`** 👤 tier:X
- Covers #4 + #11. Install `staging-gate-smoke.{service,timer}` as user units (the .service is orphaned); provide
  `OMNISIGHT_API_TOKEN` to the smoke unit (the 401 cause); run once and confirm a green smoke record vs develop-tip.
  Accept: smoke timer enabled+firing, a green smoke-status record on the current revision. (Depends on the DAG-executor
  smoke being runnable — confirm or scope-down the smoke suite.)

**B3 `[OP][backend] Canary SLO: kill the stub path, populate p95, single source of truth`** 🤖 agent:auto
- Covers #9 + #10 + #24. Make the documented canary path use the REAL `RollingDeploySloMonitor` (not `_StubMonitor`);
  populate `p95_latency_ms` in `SloSnapshot` (canary_rollout.py:73-77) so the latency SLO actually gates; deprecate/
  remove the dead `orchestrator/canary.py` stub path OR clearly mark it test-only; bind the canary status to the
  candidate digest (not just /healthz 2xx). Files: `backend/canary_rollout.py`, `backend/orchestrator/canary.py`,
  `backend/agents/staging_gate.py`, `docs/operations/canary-runbook.md`. **MUST NOT**: break the live canary timer
  (currently green); keep changes additive to the real path.

---
## Group C — promote/prod correctness (P0/P1)
**C1 `[OP][devops] promote_image_bundle.py: fix DEFAULT_REGISTRY (:49160, lowercase)`** 🤖 agent:auto
- Covers #5. `DEFAULT_REGISTRY` → `sora.services:49160/omnisight/omnisight-productizer`. Files: `scripts/promote_image_bundle.py:51`. Tiny. MUST NOT: change the promote logic.

**C2 `[OP][devops] deploy-prod.sh --digest: make compose honor OMNISIGHT_IMAGE_DIGEST`** 🤖 agent:auto
- Covers #6. Either make `docker-compose.prod.yml` image refs support `@${OMNISIGHT_IMAGE_DIGEST}` when set, or fix
  `deploy-prod.sh` to pin by digest correctly. Files: `scripts/deploy-prod.sh`, `docker-compose.prod.yml`. MUST NOT:
  break the tag path (which works).

**C3 `[HOLD][devops] Orchestrator path B: decide retire-vs-wire (supersedes/updates OP-1690)`** 👤 tier:X
- Covers #8. Decision ticket: either wire path B fully (mount router, real Slack gate, fix smoke URL, fix
  ship() tag-env) OR formally deprecate it in favour of the manual rolling path + update the runbook. Update OP-1690.

---
## Group D — dev isolation (P0/P1)
**D1 `[GATE][db] Implement + apply A1 REVOKE CONNECT on prod DB`** 👤 tier:X
- Covers #7. Author the SQL/migration that `REVOKE CONNECT ON DATABASE omnisight FROM <dev role>` (+ ensure dev role
  exists/scoped); apply to the prod PG-HA primary. agent writes the script; operator/me applies (prod DB). Accept:
  a dev-role connection to the prod DB is refused. MUST NOT: revoke the prod app role's own access.

**D2 `[OP][backend] A2 guard: cover the 3 sa.create_engine prod-DB paths`** 🤖 agent:auto
- Covers #16. Add `env_contract.enforce_env_db_contract` (or route through the guarded resolver) in
  `deploy_audit.py:137`, `release_conductor/state_machine.py:177`, `orchestrator/prod_deploy.py:235`. + tests.

**D3 `[OP][backend] A2 guard: tighten CI_MODE/PYTEST bypass for dev↔staging`** 🤖 agent:auto
- Covers #17. The in-test early-return must still enforce dev↔staging cross-contamination (not only refuse for prod).
  Files: `backend/env_contract.py:179-181` + tests. MUST NOT: break legitimate pytest runs.

**D4 `[OP][devops] prod compose: remove the SQLite-fallback footgun`** 🤖 agent:auto
- Covers #18. Make `OMNISIGHT_DATABASE_URL` required in prod (fail-closed if unset) and/or drop the co-set
  `OMNISIGHT_DATABASE_PATH` so prod can never silently fall back to SQLite. Files: `docker-compose.prod.yml`, prod `.env`.

**D5 `[OP][devops] dev stack: reconcile topology with docs (add services or fix the claims)`** 🤖 agent:auto
- Covers #19 + #32 + #27. Either add frontend/caddy/backend-b to `deploy/dev/docker-compose.yml` to match the
  header/`.env`, or correct the header + remove the unused `.env` ports; add `deploy/dev/.env.example`; bind dev
  backend to 127.0.0.1 (not 0.0.0.0) given AUTH_MODE=open. Decision: lean "fix claims + bind localhost" (minimal).

**D6 `[GATE][devops] dev backend unhealthy — investigate + fix`** 👤 tier:X
- Covers #12. Root-cause `omnisight-dev-backend-a-1` unhealthy + restore. operator/investigate.

---
## Group E — cross-cutting + hygiene (P1/P2)
**E1 `[OP][devops] deployment-audit.sh: fix alembic baseline + retire main-vestige checks`** 🤖 agent:auto
- Covers #21 + #30. Compare prod alembic head against the DEPLOYED release's head (not the local checkout); drop/
  reclassify the `auto-promote-main` check (dead under ADR-0040). Files: `scripts/deployment-audit.sh`.

**E2 `[GATE][devops] Prod compose context: clean develop-tracking checkout`** 👤 tier:X
- Covers #22 + #15. The prod compose runs from `/home/user/work/sora/OmniSight-Productizer` (37 behind + 22 dirty);
  the gate/bridge units run from `/home/user/sora-bridge` (2nd checkout). Move prod-deploy + gate units to a clean
  develop-tracking checkout (or sync + clean the existing ones). operator. MUST NOT: disrupt running prod/staging.

**E3 `[OP][devops] Gate/promote tooling: tolerate extra env vars (pydantic extra_forbidden)`** 🤖 agent:auto
- Covers #26. The Settings model trips `extra_forbidden` on operator-shell env pollution → gate audit-write fail-open.
  Make the release-tooling Settings ignore unknown env (or the gate use a scoped config). Files: `backend/config.py` /
  the gate's settings load. MUST NOT: loosen prod runtime validation — scope to the gate/promote tooling path.

**E4 `[OP][backend] /api/version: stop emitting zero-placeholder *_image_digest`** 🤖 agent:auto
- Covers #23. Either populate `backend_image_digest`/`frontend_image_digest` with the real digest or remove the
  placeholder fields. Files: `backend/api_versioning.py`. MUST NOT: change `deployed_digest_*` (correct).

**E5 `[OP][devops] check_deploy_ref.sh: remove dead v*-tag layers`** 🤖 agent:auto
- Covers #25. Remove Layers 1+2 (signed `v*` git-tag gates) that RT-20 makes impossible; keep the digest path.

---
## Group F — Low / doc / decisions (P2/P3)
**F1 `[OP][docs] Doc-drift batch: runbook/handoff/ADR-conformance updates`** 🤖 agent:auto · `--force`
- Covers #31. Update: prod-deploy runbook (prod tag v0.6.0, not rc5-hotfix4); handoff §7 (OP-1608 resolved); cosign
  var-naming note (SORA_COSIGN_* vs COSIGN_*). Files: the two operations docs. MUST NOT: edit ADRs (decisions).

**F2 `[OP][ci] fast-gate: require tests for devops/CI-affecting changes`** 🤖 agent:auto
- Covers #28. `ci_test_impact.py` should not give deploy-script/CI changes `lint-only`/`skip`. Files: `scripts/ci_test_impact.py`.

**F3 `[OP][devops] Remove/mark dead deploy targets (k8s/helm/cloud) + their ghcr refs`** 🤖 agent:auto
- Covers #35. Delete or clearly-mark-aspirational `deploy/{k8s,helm,render,railway,digitalocean,blue-green}`; purge
  their `ghcr.io` refs. MUST NOT: touch the live compose/staging/prod paths.

**F4 `[OP][backend] Overlay lock: auto re-read on change (no manual --force-recreate)`** 🤖 agent:auto
- Covers #33. Watch/re-read the overlay lock so a rewrite is picked up without manual recreate. Files: `backend/api_versioning.py`.

**F5 `[HOLD][devops] Roadmap/decisions: testing-env, hands-off auto-promote, staging dedicated PG`** 👤 tier:X
- Covers #20 + #29 + #34. Decision/roadmap ticket (not a code fix now): whether to add an integration "testing" env
  (#20); whether to re-enable hands-off auto-promote (#29, RT-04a feeder); staging dedicated PG host (#34 = OP-927).
  Link OP-927.

---
## Coverage map (all 35)
A1#2 · A2#2,#14 · A3#1 · B1#3 · B2#4,#11 · B3#9,#10,#24 · C1#5 · C2#6 · C3#8 · D1#7 · D2#16 · D3#17 · D4#18 ·
D5#19,#27,#32 · D6#12 · E1#21,#30 · E2#22,#15 · E3#26 · E4#23 · E5#25 · F1#31 · F2#28 · F3#35 · F4#33 · F5#20,#29,#34.
**24 children** (15 🤖 agent:auto · 9 👤 tier:X/decision). Deps: A1→A2 (disable before repo-retire); de-risk order A→F.

## Open questions for codex
1. B1 (overlay digest cross-check): what's the honest source of the backend's own running-image digest at runtime?
2. A2: remove `docker-publish.yml` entirely, or re-point off ghcr? Any non-GHCR consumer of it?
3. D5: add the missing dev services vs fix-the-docs — which is the right "reconcile"?
4. Any finding I mis-grouped, or a fix with goal-drift risk that needs operator-ownership not agent:auto?
5. C3/OP-1690: retire path B vs wire it — which does the audit evidence favour?
