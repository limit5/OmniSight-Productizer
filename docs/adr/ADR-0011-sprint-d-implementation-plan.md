---
id: ADR-0011
title: Sprint D Implementation Plan — Deployment Automation Pipeline
status: Proposed
date: 2026-05-08
---

# ADR-0011: Sprint D Implementation Plan — Deployment Automation Pipeline

**Status**: Proposed (Sprint D — META OP-761)
**Date**: 2026-05-08
**Decider**: sora (operator) + AI fleet
**Extends**: [ADR-0010 — Deployment Automation Gates](ADR-0010-deployment-automation.md)

> **Relationship to ADR-0010**: ADR-0010 (D1, accepted) defines the
> *milestone gate* technical decision (`fixVersion` + structured events).
> This ADR-0011 documents the *broader sprint plan* — the 18 children
> that build the full pipeline around that gate, the 5 alternatives
> considered, and the phased rollout. They are complementary; 0010 is
> the gate definition, 0011 is the pipeline built on top of it.

---

## Context

OmniSight is a live multi-tenant product. Production at `sora.services` runs `backend-a` + `backend-b` (active-active behind Caddy LB), Postgres primary, frontend behind CDN, plus the gerrit-jira-bridge daemon. The product cannot accept downtime windows; every deploy must be zero-downtime.

The current deploy process is manual:
1. Operator picks a commit on `develop` deemed "ready"
2. Cherry-pick or merge to `main`
3. Test on main / a personal staging
4. Apply ad-hoc hotfixes if needed
5. Tag (`vX.Y.Z`) and push to release branch
6. Manually `docker compose pull && up -d` on prod, watching for breakage
7. Rollback by hand if something fails

**Pain points**:
- Each deploy takes ~30-90 min of focused operator time
- Rollback requires recall of "which image was previous good"
- No baseline metric comparison — operator eyeballs Caddy logs
- Schema migrations sometimes break old code mid-rolling-restart
- No record of "who deployed what when" for compliance
- Hotfix path is ad-hoc; same person who broke is fixing under pressure

The fleet now has the supporting infrastructure to automate this: AI Reviewer (OP-713), CI parallel-gate (OP-739 META), conflict observability (OP-746), reliability hardening (OP-747 META). With those landing, the deployment chain becomes the next bottleneck for shipping safely.

## Decision

Implement **milestone-driven semi-automated continuous delivery**: define a release as a JIRA `fixVersion`; when all milestone tickets are 公開済み + acceptance gates green, automatically promote `develop → main → staging → tag`. Production deploy retains a manual operator approval gate (the "ship it" click); after approval, deploy proceeds with automatic SLO monitoring and rollback.

### Pipeline architecture

```
[develop] ──D1: milestone acceptance check──→ [milestone_ready event]
                                                       │
[main]  ←──D5: auto-promote (FF-only)─────────────────┘
   │
   ├──D2: image build + sign──→ [registry: backend:vX.Y.Z, frontend:vX.Y.Z]
   │
   └──D6: auto-deploy staging──→ [staging.sora.services]
                                          │
                                          ├──D7: smoke + metric baseline (auto-abort on regression)
                                          ↓
                                  [staging_passed event]
                                          │
                                          ├──D8: tag + release branch
                                          ↓
                              ★ D9: operator approval gate ★
                                          │
                                          ├──D10: canary 5% → 25% → 100% (or D9 blue-green if canary off)
                                          │       D11: SLO monitor at every stage
                                          ↓
                                  [prod release vX.Y.Z live]
                                          │
                                          ├──D11: 1h post-deploy SLO watch
                                          │       (auto-rollback on breach)
                                          ↓
                                  [release stable]
                                          │
                                          ├──D16: auto release notes (PS for review)
                                          └──D17: dashboard / D18: audit log
```

### D16 release notes automation

`backend.agents.release_notes_generator` consumes the D8
`release_tagged` event from the release milestone log and drafts
`docs/releases/vX.Y.Z.md` within the 60-second service poll. The draft is
assembled from JIRA tickets with `fixVersion=vX.Y.Z`, grouped into the
release-note template sections, plus links to matching
`docs/sop/lessons/L-OP-XXX-*.md` per-file lessons.

The generator commits the markdown file and pushes `HEAD:refs/for/develop`
through the existing Gerrit dispatch helper. It emits
`release_notes_drafted` with the Gerrit change URL, but never merges the
patchset; operator polish and human +2 remain the publishing boundary.

### Where automation stops, where humans stay

| Decision | Automated | Human gate | Why |
|---|---|---|---|
| Promote develop → main | ✓ | — | Acceptance gates objective; FF-only check prevents bad merges |
| Build + sign images | ✓ | — | Mechanical |
| Deploy to staging | ✓ | — | Staging is for testing; failure is recoverable |
| Smoke + baseline | ✓ | — | Regression is binary; auto-abort safer than human "looks fine" |
| Tag + release branch | ✓ | — | Mechanical |
| **Deploy to prod** | — | **★ operator click** | Business timing concerns (peak hours, partner comms) outside our domain |
| Canary stage advance | ✓ | manual override | SLO check per stage objective; operator can intervene if needed |
| Rollback on SLO breach | ✓ | — | Fast (< 5 min) is the entire safety net |
| Hotfix workflow | ½ | accelerated approval | Lower acceptance bar; still operator-gated |
| Post-deploy compliance | ✓ | review | Audit log auto-written; operator reviews quarterly |

The single human gate at production deploy is **deliberate** — Google SRE, Netflix, AWS all preserve a similar approval step in their CD pipelines. Reasons:
- Deploy timing affects customer-facing experience (peak vs off-peak)
- Coordination with marketing / support / partners requires human judgement
- "Big red button" responsibility cannot be fully delegated to automation
- Audit / compliance frameworks (SOC2, ISO 27001) require demonstrable human control over production changes

### D8 tag + release branch contract

OP-769 adds `backend.agents.auto_tag_release`, a daemon-style consumer for
the staging gate log. On each `staging_passed` event it resolves the
SemVer `fixVersion`, creates an annotated immutable `vX.Y.Z` tag on
`main` HEAD, pushes the tag to Gerrit and GitLab, cherry-picks that commit
onto `release/vX.Y`, pushes the release branch to both remotes, and emits
`release_tagged` for D9.

Existing tags are never moved. If a requested `vX.Y.Z` already points at
any commit other than the current `main` HEAD, the automation fails closed
so the operator can cut a new patch version instead of rewriting release
evidence.

### Zero-downtime requirements (orthogonal to automation)

The pipeline ENABLES zero-downtime; it does not GUARANTEE it. Each release must independently meet:

1. **Backwards-compatible schema migrations** — old code (still running on backend-b during rollout) must work with new schema (already applied). D4 enforces in CI.
2. **Backwards-compatible API** — frontend cached on user's browser (old version) must work with new backend. D13 formalises versioning.
3. **Feature flags default-off** — new behaviours land disabled; D12 service for flip after deploy.
4. **No "rename and remove" in single release** — split into deprecate-then-remove across ≥1 release boundary.

### Milestone definition

Each release is a JIRA `fixVersion`. Acceptance criteria (D1):

```yaml
# Conceptual; D1 implements as Python check
acceptance:
  all_tickets_published: |
    SELECT COUNT(*) WHERE fixVersion = $V AND status != "公開済み"
    must equal 0
  no_open_blockers: |
    SELECT COUNT(*) WHERE affects_version = $V AND priority = "Highest" AND status != "公開済み"
    must equal 0
  ci_canary_green_24h: |
    canary suite on develop tip green for last 24h continuously
  smoke_recent: |
    smoke suite on develop tip green within last 4h
  migration_compat_verified: |
    every alembic migration in this milestone has rollback test passing
```

Operator-defined release window can override scheduling (D17 dashboard exposes).

## Consequences

### Positive

- **Operator time per release**: 30-90 min → ~5 min (review + click)
- **Deploy frequency**: 1-2/week → potentially 1/day (limited by milestone cadence, not pipeline)
- **MTTR for failed deploy**: 15-30 min manual rollback → < 5 min auto-rollback
- **Audit / compliance**: full trail of every production change (D18)
- **Schema migration safety**: regression caught in CI (D4) instead of prod
- **Hotfix MTTR**: 1-2h ad-hoc → < 30 min via D14 fast-path
- **Knowledge transfer**: runbook (D18) + dashboard (D17) reduce bus-factor risk

### Negative

- **Initial complexity** — 18 tickets, ~50-70 hours of work, plus the existing prerequisites
- **Staging environment cost** — additional infrastructure to maintain
- **Migration burden** — existing `.env`, env vars, manual scripts must migrate to D3 / D12
- **Operator UX learning curve** — old `docker compose up` muscle memory replaced by approval clicks + dashboard
- **Vendor lock-in risk** — choice of registry / vault / CI tooling creates coupling that's costly to switch later

### Neutral / observed

- **Sprint D depends on multiple predecessors**: must wait for OP-739 CI gate + OP-747 H Wave 1 + OP-746 observability to ship before starting (~1-2 weeks)
- **The sprint is bounded** — once D1-D18 ship, the system reaches a stable architecture; further enhancements (e.g. multi-region, Kubernetes migration) are explicit follow-ups, not implicit
- **The "human gate" is intentionally NOT auto-deferrable**. Even if operator is asleep, prod doesn't deploy without consent. Fail-safe over fail-fast.

## Alternatives Considered

### Alt 1: **Full Continuous Deployment (Netflix/CodeDeploy model)**

Every commit to `main` deploys directly to prod, gated only by automated tests. No human approval.

**Rejected because**: OmniSight is a multi-tenant SaaS with paying customers. Continuous deployment requires institutional confidence in test coverage that we don't have yet. CI suite has known coverage gaps; baseline measurements are still being collected (OP-746). Also, business-timing concerns (peak hours, partner notifications) require human discretion that isn't easily codified.

**Reconsider when**: CI coverage > 90% + 6 months of clean SLO + organisational maturity to accept blameless incidents.

### Alt 2: **Pure Manual (status quo)**

Continue current operator-driven manual deploys.

**Rejected because**: Operator time is the scarcest resource and current pace already burns 1-2 hours/week on manual orchestration. Doesn't scale as feature pace increases. No audit trail. No reliable rollback. Knowledge concentrated in single person (bus factor 1).

### Alt 3: **GitOps (ArgoCD / Flux)**

Declarative state in Git; operators don't deploy, they edit yaml files; controller reconciles cluster state.

**Rejected for now because**: requires Kubernetes (we run docker-compose). Migration to K8s is a separate large investment. Could revisit when/if scale demands it.

**Could revisit when**: scaling to multi-region or compliance requires immutable infra-as-code.

### Alt 4: **Spinnaker-style multi-stage pipeline**

Heavyweight industrial CD platform. Comprehensive but operationally complex.

**Rejected because**: Spinnaker requires significant ops investment to maintain. For OmniSight's scale, the Sprint D bespoke pipeline is sufficient and more aligned with codebase conventions.

### Alt 5: **Trunk-based development with feature flags only**

No release branches; everything deploys from main; new features behind flags toggled on/off post-deploy.

**Partially adopted**: D12 introduces feature flags as a primary control mechanism. But pure trunk-based requires more mature CI than we have; release branches still useful for hotfix isolation.

## Implementation plan

Sprint D children OP-762 through OP-779, organised in 5 phases:

| Phase | Tickets | Focus | Estimated |
|---|---|---|---|
| **Foundations** | D1 / D2 / D3 / D4 | Milestone definition, image pipeline, secrets, migration safety | ~14-21h |
| **Pre-prod pipeline** | D5 / D6 / D7 / D8 | develop→main→staging→tag automation | ~13-20h |
| **Production deploy** | D9 / D10 / D11 / D12 / D13 | Approval gate + canary + SLO + flags + API versioning | ~20-30h |
| **Hotfix + recovery** | D14 / D15 / D16 | Hotfix path, DR, release notes | ~9-13h |
| **Operator UX** | D17 / D18 | Dashboard + runbook + audit | ~7-10h |

**Total**: ~50-70 hours of work. With 2 active runners + parallel children: **2-3 weeks** sprint duration.

## Sprint kickoff conditions (must-have before Sprint D begins)

- [ ] OP-739 META (CI parallel-gate) — all C1/C2/C3 公開済み
- [ ] OP-747 META Wave 1 — H1/H4/H8/H9 公開済み (and deployed)
- [ ] OP-746 公開済み + dashboard live (D7 reuses metric infrastructure)
- [ ] OP-721 T1 (notification bridge) 公開済み (D9/D11 use it)

## Sprint completion criteria

- [ ] All 18 children 公開済み
- [ ] End-to-end synthetic v0.99-test milestone successfully promoted dev→staging→tag→approved→canary→prod-mirror→stable
- [ ] Rollback drill: simulate SLO breach in staging → auto-rollback succeeds in <5 min
- [ ] First real release using pipeline (e.g. v0.5.0) ships zero-downtime
- [ ] ADR-0010 merged
- [ ] Operator runbook completed + dry-run signed off

## References

- META ticket: OP-761
- Predecessor METAs: OP-720 / OP-721 / OP-730 / OP-739 / OP-747
- Industry references: Google SRE Book Ch. 8 (Release Engineering), Netflix Tech Blog "Spinnaker", AWS "Blue/Green Deployments" whitepaper
- Internal references: ADR-0007 (multi-provider) for env var conventions, ADR-0002 (GitLab primary) for replication target

## Revision history

- 2026-05-08: Initial draft (sora + claude conversation, Sprint D planning)
