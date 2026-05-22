---
id: ADR-0042
title: Image distribution strategy + GHCR decommission (with on-prem trigger)
status: Proposed
date: 2026-05-22
---

# ADR-0042 — Image distribution strategy & GHCR decommission

**Status**: Proposed (2026-05-22). Confirms the registry/distribution model before decommissioning GHCR.

**Relates**: ADR-0011 (multi-tenant SaaS), ADR-0038 (image pipeline on GitLab), ADR-0040 (release train, image-tag-only RT-20), the 2026-05-22 staging/registry audit.

## Context
The GHCR→GitLab CR migration is ~90% done (prod backend + frontend pull from the private GitLab CR `sora.services:49160`). Remaining GHCR pieces — the `omnisight-installer` sidecar, the `mobile-build` toolchain image, the GitHub-Actions publish workflows, and stale code refs — are **all internal** (pulled by our own host/runners with credentials). The audit confirmed the **only** capability GHCR has that the private GitLab CR does not is **public/anonymous pull** (`49160` returns 401; it sits on a single LAN IP).

The canonical product model (ADR-0011 + `docs/architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md`) is **multi-tenant SaaS-hosted**: tenants use our hosted instance with strict per-tenant isolation; **they never pull OmniSight images.** Operator confirmed 2026-05-22: SaaS-hosted is the model, **with a possible future on-prem/enterprise tier** (large customers running OmniSight on their own infrastructure).

## Decision
1. **The private GitLab CR (`sora.services:49160`, auth-gated) is the sole image registry** for the SaaS-hosted model. It is sufficient because every image is pulled internally (our host/runners hold credentials).
2. **GHCR is decommissioned.** Its only unique capability — public/anonymous distribution — is not used by the SaaS model. The remaining pieces migrate or retire (all CR-ready, none has a fundamental mismatch):
   - `omnisight-installer` → build/push to GitLab CR + update the prod compose ref (it also has a local `build:` path).
   - `mobile-build` → push to GitLab CR; flip `MOBILE_BUILD_IMAGE`.
   - GitHub-Actions publish workflows (`build-images.yml`, `docker-publish.yml`) → disable (GitLab CI is the active publish path; confirm they are not running, then remove).
   - Stale refs (`slo_monitor.py`, `production_release.py`) → repoint strings to GitLab CR.
3. **The build pipeline stays registry-agnostic** (image-tag-only per RT-20; the Dockerfiles + candidate build take the registry as a parameter), so the publish *target* is a config value, not a baked assumption.

## 🔑 Mitigation policy — the on-prem trigger (so the future bridge is not silently burned)
**TRIGGER**: if/when an **on-prem / enterprise self-deploy tier** is introduced — i.e. external customers must pull *customer-facing* OmniSight images onto their own infrastructure — then **a public or licensed distribution channel MUST be (re-)established at that point.** The private GitLab CR (LAN IP + shared creds) **MUST NOT** be exposed as the customer distribution channel.

At trigger-time, the channel options are: a public registry (GHCR / Docker Hub / a public GitLab project) **or** a licensed pull-through proxy with per-customer credentials. Because the build pipeline is registry-agnostic (Decision §3), (re-)establishing a publish target is a fresh registry + a publish job — **a cheap, additive change, not a migration.** Decommissioning GHCR now therefore does NOT burn this bridge.

**What this record prevents**: a future engineer silently assuming "private CR is our only registry forever" and being unable to ship an on-prem tier without rediscovering the public-distribution gap. The gap is documented, gated on an explicit trigger.

**Caveat to surface at trigger-time**: an on-prem tier is far more than "a public registry" — it pulls in licensing/entitlement, secret-baking, per-customer update distribution, and tenant-isolation-on-foreign-infra. Those are a deliberate product workstream; this ADR only fixes the *registry/distribution* dimension.

## Consequences
- **Positive**: one registry to operate; no dead GHCR publish path; the audit's "4 registry assumptions in flight" collapse to one. Cleaner secret + retention story.
- **Costs**: the installer + mobile-build need a one-time push to GitLab CR; GHA workflows + stale refs need cleanup (all small, all itemized).
- **Risk retained (mitigated above)**: loss of a public-distribution channel — acceptable because the SaaS model doesn't use one, and the trigger policy makes re-establishment cheap + explicit.

## Rollout (the phased GHCR decommission, gated on this ADR's acceptance)
1. Confirm the GHA publish workflows are inert (not just present); disable/remove them.
2. Build + push `omnisight-installer` (and `mobile-build` if still used) to GitLab CR; update refs.
3. Repoint stale code refs (`slo_monitor`, `production_release`) to GitLab CR.
4. Retire the GHCR-pulling staging/orphan composes (covered by the staging stand-up work).
5. Record "GHCR decommissioned; on-prem trigger active" in the registry/ops runbook.
