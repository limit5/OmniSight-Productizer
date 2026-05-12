---
id: L-OP-976
ticket: OP-976
title: Shipped-but-not-deployed — "merged to develop" is not "running on prod"
date: 2026-05-13
tags: [devops, jira, process, deployment, anti-pattern]
---

# Shipped-but-not-deployed — "merged to develop" is not "running on prod"

**Situation**: The 2026-05-12 AUDIT-23 audit
(`docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md`, OP-976) walked
Sprint D/E/F tickets whose deliverable was *infrastructure* — a systemd
unit/timer, a `docker compose` stack, an `EnvironmentFile=` env-wire, a DB
migration needing `alembic upgrade` on prod. Of ~10 non-trivial infra
deliverables only ~2 were confirmed live. `release-milestone-checker.timer`
(OP-762) shipped 2026-05-08 but was not `systemctl --user enable --now`'d until
2026-05-12, so the RELEASE META chain stalled with **zero signal** for ~4 days.
`staging.sora.services` (OP-767 / OP-878) was never `docker compose up -d`, so
the R3 `ci_canary` / `ci_smoke` gates had no producer — the OP-925 R3 cascade.
`~/.config/omnisight/release-audit.env` (OP-964) was never created on prod, so
the D5 audit sink silently fell back to local SQLite ("where is the audit row?"
mystery). Every one of these artefacts carried its own install recipe in its
file header; the recipe was just never executed. The same defect class shows up
again in AUDIT-29 Phase 0: Sprint F's Cognee/Neo4j/Graphiti memory layer is all
公開済み in JIRA with the code paths wired, but the runtime infra was never
stood up and the gating feature-flag module is empty.

**Why it keeps happening**: every gate stops at "merged to `develop`". CI green,
Gerrit +2, the runner's transition to 公開済み — none of them ask "did anyone
run the install recipe in the unit header?" So nobody does. The work *looks*
done because the JIRA state machine and the git history both say it is. It is
anti-pattern #4 ("push without commit") inverted: there *is* a commit and the
merge happened; what is missing is the operator-side activation step.

**Fix**: process, not more code — three tiers (recommend #1 now, #2 next sprint,
#3 eventually):
1. **Mandatory `deployed: <yes | n-a>` AC item** for any ticket whose `Files
   touched` includes `deploy/`, a systemd unit, a compose file, a cron, or a
   migration. `yes` must cite concrete *host* evidence (`systemctl --user
   is-enabled <unit>`, a `docker ps` line, `/proc/<pid>/environ` grep, `alembic
   current`) — "merged to `develop`" is not evidence. `n-a` must say why
   (peer-gated, ships disabled by design, …). Same shape as the 4-AC
   discipline's Deploy AC: a ticket with only a Code AC is
   shipped-but-not-deployed *by construction*.
2. **Scheduled deployment audit**: wrap `scripts/deployment-audit.sh` in
   `deploy/systemd/deployment-audit.{service,timer}` (daily, before
   `auto-promote-develop`), reading a host-specific manifest of *expected-live*
   units / containers / env-vars / migration heads; any red row → structured-log
   line → the T1 alerter (OP-722). Standing regression guard — would have caught
   OP-762 within 24 h instead of ~4 days later via a downstream failure.
3. **A `Deployed` JIRA workflow state** after 公開済み that a ticket only
   reaches once a deploy-verification step passes on prod, with the RELEASE-chain
   JQL keying off `Deployed` not 公開済み. High value, non-trivial — defer until
   #2 has surfaced the real toil.

**Verification**: This lesson + anti-pattern #13 in
`docs/sop/architecture-anti-patterns.md` were merged by OP-1017 (AUDIT-29a-2);
the audit table backing it is `docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md`
(13 tickets reviewed, 10 non-trivial infra deliverables, ~2 confirmed live).
The `deployed:` AC discipline (#1) and the `deployment-audit.{service,timer}`
unit pair (#2) are tracked as standing-guard follow-ups under the AUDIT-23 /
AUDIT-29 deployment-baseline phase.

**Generalisation**: "Merged" is not "deployed" and "the ticket is closed" is not
"the thing is running". Any deliverable with a runtime side needs an explicit,
evidence-bearing activation step in its DoD *and* a cheap recurring check that
the activation actually happened — an install recipe sitting in a unit header is
documentation, not a deployment. Treat the gap between "code in `develop`" and
"artefact live on prod" as a first-class state that something owns, not an
implicit "someone will run it". Recorded as anti-pattern #13 in
`docs/sop/architecture-anti-patterns.md`. See also `L-OP-737`
(runner-pickability invariants at file time) and `L-OP-1014` (bulk import
without refinement — the same "below actionable quality is pure cost" logic one
step earlier in the lifecycle).
