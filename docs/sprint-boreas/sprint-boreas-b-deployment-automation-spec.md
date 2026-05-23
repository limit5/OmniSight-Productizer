# Sprint Boreas-B — Deployment Automation Spec (scoped to the 2 remaining gaps)

**Status**: DRAFT for codex review (2026-05-23). OP-1133 META.

## Why this spec is small
Boreas-B's original 6-item scope (OP-1133) is **~4/6 already delivered** by the parallel
release-train effort (ADR-0040) + the SLO monitor, not as Boreas-B children. This spec
therefore covers ONLY the 2 genuine remaining Layer-1 gaps. Delivered items, for the record:

| Boreas-B scope item | Delivered by | Evidence |
|---|---|---|
| 2. Image build→publish→consume + provenance/SBOM | release-train candidate pipeline | cosign sign + SBOM + attest → GitLab CR; v0.5.0 cut pipeline #102 |
| 3. Promotion dev→staging→prod | release-train | candidate→staging→gate→promote-by-digest→prod (v0.5.0 cut); **progressive prod canary still HOLD'd** (OP-931/932/933) — depends on gap #5 below |
| 4. Auto-rollback on health degradation | SLO monitor (OP-883) | F1–F4 + OP-1640/1641 (migration-safe), activated 2026-05-23 |
| 6. Operator override / freeze + manual cut | partial | release:force-promote (ADR-0019/OP-966) + manual cut window + deploy-overlay lock |

**Remaining gaps = items #1 (environment isolation) and #5 (multi-tenant routing).** Both
need operator architecture decisions (the META flagged this as operator-window-top).

---

## Gap A — Environment isolation (Boreas-B scope #1, host-reboot "issue #1")

### Current state
- **dev + prod overlap on the same host** ([[project_prod_environment]]) — the standing risk
  behind the 2026-05-14 host-reboot retro (image/DB drift):
  `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md`.
- **staging IS isolated** on the same host: separate compose project `omnisight-staging`,
  separate DB `omnisight_staging`, separate ports (55432/18080), isolated volumes
  (`deploy/staging/docker-compose.yml`). This is a proven same-host isolation pattern.
- The danger zone is **dev**: operator edits + runner worktrees + the prod stack share the host
  + (historically) data paths; a careless restart / volume / migration can cross into prod.

### Decisions for the operator (codex to pressure-test)
1. **Isolation boundary**: (a) same-host isolated dev stack (reuse the staging pattern: own
   compose project + DB + ports + volumes), or (b) a separate dev HOST (the local-LLM box?), or
   (c) per-developer ephemeral dev envs. Tradeoff: (a) is cheap + proven (staging already does
   it) but shares kernel/docker/disk with prod; (b) is the real fix but needs another machine.
2. **What "dev" even is here**: the operator's main workdir + the 4 runners currently operate
   against… what? Define the dev data plane (its own PG? a seeded snapshot? read-only against
   prod?) so dev work cannot mutate prod rows.
3. **Runner/coordinator targeting**: how do runners + the pipeline coordinator know they're in
   dev vs prod (env contract, like staging's `verify-env-contract.sh`)? Fail-closed if a dev
   process points at a prod DB URL (staging already rejects prod URLs/live keys).
4. **Boot order / drift guard**: the reboot retro's lesson — env contract + drift gate at boot
   so a wrong-env or image/DB-ahead state fails closed (the /readyz drift gate already does this
   per-stack).

### Proposed ticket family (gap A) — est. 4–6
- A1: dev-env contract + isolated compose project (own PG/ports/volumes), mirroring staging.
- A2: env-contract guard for runners + coordinator (refuse prod DB URL from a dev process).
- A3: dev data seeding (anonymized prod snapshot — note: the OP-972 staging-pg-snapshot tool is
  currently broken/NOTINSTALLED; fix or replace as part of this).
- A4: boot-order + drift-gate verification for the dev stack.
- A5 (if separate-host chosen): provision + cut-over plan.

---

## Gap B — Multi-tenant routing (Boreas-B scope #5; prerequisite for safe canary)

### Current state
- **Tenancy DATA model already exists**: `tenants` table, `tenant_id` columns across many
  tables (git_accounts, sessions, llm_credentials, …), `backend/routers/admin_tenants.py`,
  envelope `tid` in KS.1. So the app is tenant-AWARE at the data layer.
- **Missing = tenant→VERSION routing**: every tenant currently hits the single deployed image
  pair behind Caddy round-robin (backend-a/backend-b, same version). There is no mechanism to
  land a *subset* of tenants on a *new* version — which is exactly what progressive prod canary
  (HOLD'd OP-931/932/933) needs.

### Decisions for the operator (codex to pressure-test)
1. **Routing layer**: where does tenant→version selection happen? (a) Caddy/ingress routes by a
   tenant key (header/cookie/subdomain) to a version-pinned backend pool; (b) a single backend
   pool that self-selects behavior by tenant cohort (feature-flag style, no separate image); (c)
   per-tenant image pinning via the deploy-overlay. Tradeoff: (a) gives true image-level canary
   (matches the release-train digest model) but needs N backend pools; (b) avoids extra pools
   but isn't a real image canary.
2. **Cohort model**: how is a canary cohort defined + advanced? (% of tenants, an explicit
   allowlist, internal-tenants-first?) How does it tie into the release-train promote (a
   `vX.Y.Z` digest serving the canary cohort while prod stays on the previous final)?
3. **Tenant identity at the edge**: how does Caddy/the ingress know the tenant before the
   backend? (subdomain per tenant? a signed cookie? an API key→tenant map?) — this constrains
   option 1a.
4. **Rollback authority**: when the SLO monitor (OP-883) sees a canary-cohort breach, does it
   roll back just the cohort (the existing canary-vs-full decision in slo_monitor) or the whole
   env? The monitor already has a canary branch (currently unreachable because canary is HOLD'd).
5. **Single-host reality**: with dev/prod on one host (Gap A), is a real multi-pool canary even
   worth it yet, or does Gap A come first? (Likely: Gap A is the prerequisite.)

### Proposed ticket family (gap B) — est. 6–10
- B1: tenant identity resolution at the edge (Caddy/ingress) — design + impl.
- B2: version-pinned backend pools (or the chosen routing mechanism from decision 1).
- B3: canary cohort selection + advancement, wired to the release-train promote.
- B4: un-HOLD + repair the progressive-canary orchestrator (OP-931/932/933) on the new routing.
- B5: SLO monitor canary-scoped rollback (the existing canary branch becomes reachable).
- B6: per-tenant version observability (which tenant on which digest — extends /api/version).

---

## Sequencing recommendation
**Gap A (env isolation) BEFORE Gap B (multi-tenant routing).** Real canary on a host where dev
can clobber prod is premature; and the reboot retro makes env isolation the higher-urgency
floor. Gap B is larger + more architectural (a tenancy/routing model that touches the whole
product) and arguably edges into Layer-2 territory — confirm it's still Layer-1-Boreas-B scope
or split it out.

## Open questions for codex
1. Is the 4/6-delivered mapping correct (anything I credited to the release-train that isn't
   actually there)?
2. Gap A: same-host-isolated vs separate-host — which is the minimal-correct floor given the
   reboot retro, and what's the smallest dev-env-contract that prevents dev→prod mutation?
3. Gap B: is true image-level multi-tenant canary (1a, N pools) over-engineering for a
   single-host, ~1-tenant deployment today? Should B be deferred/de-scoped to a feature-flag
   cohort (1b) until there are real multi-tenant + multi-host needs?
4. Sequencing + whether Gap B belongs in Boreas-B (Layer 1) at all vs a later phase.

---

## codex review round 1 (2026-05-23) — folded in

**Verdict: file Gap A only as Boreas-B Layer-1 completion; DEFER Gap B out of Boreas-B.**

### 4/6-delivered mapping — corrections (for an audit-clean OP-1133)
- #2 build/sign/SBOM/attest — **verified** (.gitlab-ci.yml candidate jobs).
- #3 promotion — shipped path is **candidate→staging(digest-verified)→promote-by-digest→
  `vX.Y.Z`→prod-by-final-TAG**. **"prod pull-by-digest" is NOT shipped** (docker-compose.prod.yml
  consumes `:${OMNISIGHT_IMAGE_TAG}`; `deploy-prod.sh --digest` writes `OMNISIGHT_IMAGE_DIGEST`
  with no compose consumer — RT-07b). Don't claim digest-end-to-end-to-prod.
- #4 auto-rollback — **hook exists** (SLO monitor full→`scripts/deploy.sh --rollback`,
  production_release.py:286), but the final-release rollback IDENTITY semantics are unverified
  (deploy-prod.sh only documents a manual previous-final redeploy). Qualify.
- #6 operator override — **partial controls shipped** (force-promote ADR-0019 still *Proposed*;
  SLO suppress flag real; deploy-overlay lock real). Not a complete freeze+manual-cut system.

### Gap A — final shape (the provable floor)
Same-host isolated dev is the minimal-correct Layer-1 floor (separate host deferred). The
host-reboot retro root cause = stale image/DB drift + dev tooling against prod DB — so the
provable prevention is **DB privilege separation**, not just env-string checks:
- **A1** dev isolated compose project + dev PG + **dev `omnisight_dev` role with NO grants on
  the prod DB** + own ports/volumes (mirror `deploy/staging/`).
- **A2** dev env-contract guard (reject prod-looking DATABASE_URL) for **backend + runners +
  coordinator + any migration entrypoint** (coordinator unit currently has NO DB contract;
  pipeline-coordinator.service:28).
- **A3** dev data seeding — **OPTIONAL / non-blocking** for Layer-1 (don't gate isolation on it;
  the OP-972 snapshot tool is broken anyway).
- **A4** boot-order + `/readyz` drift verification for the dev stack — **required**.
- **A5** separate-host provisioning — **deferred** unless operator wants stronger isolation.

### Gap B — DEFERRED out of Boreas-B
Current tenancy is data/app-layer (middleware resolves tenant from session/header — main.py:762;
Caddy only round-robins backend-a/b — Caddyfile:145; existing canary is weighted-upstream, not
tenant-pinned — canary_rollout.py:136). **True image-level multi-tenant canary (N version-pinned
pools) is over-engineering for a single-host, ~1-tenant deployment.** Do NOT file it now. If a
near-term canary cohort is ever needed, file a small APP-LEVEL feature-flag-cohort design ticket
(explicitly not image-level). Re-evaluate when there are real multi-tenant + multi-host needs
(likely a later/Layer-2 phase).

### Final scope to FILE (Boreas-B completion)
- **Gap A: A1–A4** (A3 optional, A5 deferred) — 4–5 child tickets under OP-1133.
- **2 optional audit-clean tickets** (to make OP-1133's "4/6 delivered" claim clean):
  - RT-07b: prod compose pull-by-digest (consume `OMNISIGHT_IMAGE_DIGEST`).
  - SLO final-release rollback-identity verification (ties to OP-1641 follow-up).
- **Gap B: DEFER** (out of Boreas-B). Optionally a 1-line design-note ticket so it's tracked.
