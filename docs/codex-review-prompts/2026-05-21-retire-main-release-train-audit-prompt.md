# Codex Review Prompt — Retire `main` / Single-Trunk Release Train migration (deep blast-radius audit)

**For**: codex independent review cycle.
**Target output file**: `/tmp/retire-main-release-train-codex-audit-2026-05-21.txt`
**Reviewer role**: independent technical reviewer. You did NOT design this plan. Scrutinise it for gaps, hidden couplings, ordering/rollback hazards, and especially **things the prior (Claude) audit MISSED**. Do not merely re-confirm the known items below — your value is in finding what is NOT yet on the list.

---

## 1. The decided plan (locked by operator 2026-05-21)

Migrate from the current 5-branch GitFlow (ADR-0001) to a **single-trunk Release Train**:

1. **`develop` remains the only long-lived trunk.** Runner per-ticket `feature/*` → `refs/for/develop` → Gerrit +2 submit. No change to that ingress.
2. **`main` branch is RETIRED.** No develop→main release-cut merge ever again.
3. **A release = pushing a `v*` git TAG on a develop commit.** GitLab CI (`.gitlab-ci.yml`, `workflow: $CI_COMMIT_TAG =~ /^v.*/`) builds backend/frontend/bridge images from that tagged sha (it is already tag-driven & branch-agnostic).
4. **NO `release/X.Y` branches at all** (operator decision): hotfixes are ALSO just tags (e.g. `v0.6.1`) cut from develop. **Every tag MUST be a usable prod build** — there is no stabilisation branch to fix things on. This raises the bar on "trunk always green" and on hotfix isolation — scrutinise the consequences hard.
5. Promotion: the same image digest rides staging → canary (prod backend-a) → prod (backend-b). `auto-cut` produces a candidate to STAGING; prod promotion stays gated.
6. **Frontend feature flags WILL be added** (today there is a backend feature-flag SDK but no FE consumer) so incomplete FE features can dark-ship through a train.

## 2. What the prior audit already found (do NOT just repeat — go deeper / find gaps)

Verified `main`-hardcoded LIVE landmines (must change atomically with retiring main):
- `scripts/deploy-prod.sh:27` `BRANCH="${OMNISIGHT_DEPLOY_BRANCH:-main}"`
- `backend/agents/auto_tag_release.py:208` `rev-parse main_branch` (default `"main"`); also imports `iter_new_records` from `auto_promote_main` (to be deleted)
- `scripts/staging_deploy.sh:140` keys image-tag extraction on change-merged on `main`/`master`
- `backend/routers/webhooks.py` `_on_change_merged` pushes `main` to mirror remotes; `_trigger_ci_pipelines` fires `gh ... -r main` / GitLab `ref=main`
- `.gerrit/project.config` `Human-Plus-2` SR carve-out (`applicableIf` OR-chain) — risk of weakening the human +2 gate (CLAUDE.md L1) if half-edited

Known dormant (safe-cleanup, already dead): `backend/agents/auto_promote_main.py` (AUDIT-26d unactivated/broken), `release_cut_guard`, `MERGE_ALWAYS` submitType, `release-cut-promote` + `MainFastForwardMergerPlus2` submit-requirements, `auto-promote-develop` units. main has rotted 175 commits behind develop.

ADRs to supersede/modify: ADR-0001, ADR-0020, ADR-0016, ADR-0039 (supersede); ADR-0017/0018/0019/0021 (modify).

Feature-flag scope: `backend/feature_flag_sdk.py` + `backend/agents/feature_flags.py` (DB-backed `feature_flags` table, per-tenant %-rollout, allow-list, fail-closed, operator-flippable). **No `frontend/*` consumer exists.**

## 3. Your tasks — find what we MISSED

Audit the whole codebase + config + deploy + docs and report, with file:line precision:

A. **Additional `main` / release-cut / promote couplings** not in the list above — grep exhaustively (backend, scripts, deploy, .gerrit, .gitlab-ci.yml, frontend, systemd units, alembic, prometheus rules, Caddyfile, compose files, JIRA/conductor code). Include indirect couplings (env vars, hashtags, topics, JQL, label names, audit sinks).

B. **The "pure tag, no release branch" decision's hidden costs** — what breaks or becomes risky when there is NO stabilisation branch? Specifically: how does a hotfix ship ONLY the fix when develop has already moved ahead with unvetted commits? Is "every tag must be prod-usable" actually achievable given the current test/gating maturity? Enumerate the concrete failure modes.

C. **FE feature-flag wiring surface** — what would it take to let the frontend consume the existing `feature_flags` registry? Identify the exact integration points (API endpoint to expose flag state, the FE fetch path in `frontend/lib/api.ts` or similar, SSR vs client concerns, the `X-OmniSight-Frontend-*` header machinery from OP-1483, caching/staleness, fail-closed parity with the backend SDK). Flag any security concern (exposing flag names/cohorts to the client).

D. **Migration ordering & rollback hazards** — what is the safe SEQUENCE of changes so prod/staging never silently breaks mid-migration? What is the rollback if a step goes wrong? Which changes MUST be atomic together?

E. **Supply-chain / signing / bundle implications** — cosign signing, SBOM, attestation, `bundle.json` (note: baked image digests are placeholder-zeros by design; real digests post-build). Does retiring main / pure-tag affect provenance, the `/api/version` contract, the FE/BE bundle compat detector (OP-1483), or the registry retention/GC?

F. **The auto-cut scheduler safety** — given there is no stabilisation branch, what gates MUST exist before an auto-scheduled tag can be cut (develop-green signal — note `.gitlab-ci.yml` has NO test/lint stage; tests run per-change at Gerrit submit). Is there a real "develop tip is green right now" signal anywhere? If not, say so and propose the minimum gate.

G. **Anything else** — coordinator (`pipeline_coordinator*`) role under the new model, the JIRA release META state machine, observability/alerts that assume main, anything that would surprise an operator at 3am.

## 4. Process

- workdir is the repo root. Read real files; cite `path:line`.
- Read at minimum: `docs/adr/ADR-0001-five-branch-gitflow.md`, `ADR-0020-*`, `ADR-0021-*`, `.gerrit/project.config`, `.gitlab-ci.yml`, `scripts/deploy-prod.sh`, `scripts/staging_deploy.sh`, `backend/agents/auto_tag_release.py`, `backend/agents/auto_promote_main.py`, `backend/routers/webhooks.py`, `backend/feature_flag_sdk.py`, `backend/agents/feature_flags.py`, `backend/routers/health.py`, `backend/frontend_compat.py`, and the `deploy/systemd/` release units. Then range wider as your findings dictate.
- Structure the report by task A–G. For each finding: `path:line` + what it is + why it matters under the new model + classification (KEEP/MODIFY/DELETE/SUPERSEDE/NEW-WORK) + risk (HIGH/MED/LOW) + whether the prior audit already had it (so the operator can see the NET-NEW findings).
- End with a **NET-NEW FINDINGS** section: the items the prior Claude audit did not have. This is the most important section.
- Write the FULL review to `/tmp/retire-main-release-train-codex-audit-2026-05-21.txt`. Do NOT summarise to stdout; write the full review to the file.
