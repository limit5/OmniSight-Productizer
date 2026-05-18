---
id: ADR-0038
title: Image pipeline migration — GitHub Container Registry → GitLab Container Registry
status: Accepted
date: 2026-05-18
ticket: OP-1474 (META OP-1468)
relates_to:
  - ADR-0002 (GitLab self-hosted primary, GitHub one-way mirror, Gerrit review layer)
  - docs/operations/release-image-pipeline.md (OP-763, GHCR-era image pipeline)
  - docs/operations/prod-deploy-runbook.md (OP-881)
  - docs/operations/release-cut-runbook.md (OP-880)
---

# ADR-0038 — Image pipeline migration to GitLab Container Registry

## Status

Accepted (2026-05-18). Records the Phase 5 cutover decision under
META OP-1468; the Phase-6 SOP/ADR refresh is filed as OP-1474.

## Context

ADR-0002 (2026-05-04) declared GitLab the source of truth for repo,
issues, CI/CD, and the container registry, with GitHub retained as a
one-way public mirror. At the time the migration was scoped per surface
and the container-registry move was deferred until cosign signing had
stabilised (per `docs/sprint-s12/phase-31e-ticket-spec.md` §7 Q2 lock
and the phase-31f sign-job decoupling invariants).

Three forces drove the actual image-pipeline cutover now:

1. **Trust boundary alignment with ADR-0002.** Holding the canonical
   signed image digest in GHCR while everything else lived on GitLab
   meant the signature trust root and the registry-of-record were on
   different platforms — verifiable, but cognitively expensive and an
   ongoing tax on every audit conversation.
2. **Network egress posture.** Prod and staging deploy hosts already
   reach `registry.gitlab.com` for `git fetch`; reaching `ghcr.io` for
   `docker pull` required maintaining a second outbound allowlist and a
   second set of pull credentials.
3. **Phase 31.F sign-job invariant is now stable.** The cosign sign job
   reads `$CANONICAL_REGISTRY_DIGEST` (default `gitlab-cr`) and the
   `CANONICAL_REGISTRY` CI variable is switchable for rollback. The
   signing trust root is now provably independent of registry choice,
   which removes the last blocker on cutover.

## Decision

GitLab Container Registry (`registry.gitlab.com/omnisight/...`) is the
canonical push and pull target for every OmniSight runtime image
(backend, frontend, hardware bridge) effective 2026-05-18.

| Surface                          | Pre-cutover (frozen)               | Post-cutover (canonical)             |
| -------------------------------- | ---------------------------------- | ------------------------------------ |
| Push target (CI)                 | `ghcr.io/omnisight/...`            | `registry.gitlab.com/omnisight/...`  |
| Pull target (prod-deploy)        | `ghcr.io/omnisight/...`            | `registry.gitlab.com/omnisight/...`  |
| Pull target (staging-deploy)     | `ghcr.io/omnisight/...`            | `registry.gitlab.com/omnisight/...`  |
| cosign-signed digest             | GHCR digest                        | GitLab CR digest                     |
| GHCR namespace                   | active                             | **read-only mirror, frozen**         |
| `origin` git remote              | `github.com/limit5/...`            | `gitlab.com/omnisight/...` (sora SSH)|

Specifically:

- The canonical signed reference (per phase-31f §3.10 invariant) is the
  **GitLab CR digest**. Existing GHCR signatures remain valid for
  historical verification but no new pushes land there.
- The `origin` remote on deploy and operator hosts points at GitLab over
  the sora SSH identity (`~/.ssh/id_ed25519_sora`); GitHub remains a
  read-only one-way mirror for OSS visibility per ADR-0002.
- The deploy-token used by the prod-deploy orchestrator is provisioned
  with `read_registry` + `write_registry` scope and stored under
  `OMNISIGHT_GITLAB_CR_USER` / `OMNISIGHT_GITLAB_CR_TOKEN` on the
  operator host (see `docs/operations/prod-deploy-runbook.md` §1a).

## Consequences

**Positive:**

- Single trust boundary: signing trust root, source-of-truth repo, CI
  runner, and registry all live on GitLab. Audit conversations no
  longer have to bridge two platforms.
- Egress allowlist simplifies — `registry.gitlab.com` covers both
  `git fetch` and `docker pull`.
- Pull-rate limits on GHCR for unauthenticated workflows stop biting
  the staging-rebuild path.
- Operator deploy procedure converges with the ADR-0002 model that
  was already canonical for every other surface.

**Negative:**

- GHCR-era signatures must be verified against `ghcr.io` digests
  (historical), while new signatures verify against GitLab CR digests.
  `scripts/verify_image_signature.sh` already auto-detects keyless vs.
  key-based and the registry host, but operators inspecting old audit
  rows must remember the registry is part of the canonical signed ref.
- Rollback to GHCR is possible (CI variable `CANONICAL_REGISTRY=ghcr`
  per phase-31f §3.10) but requires re-publishing the deploy token
  bundle to operator hosts. Rollback is therefore best treated as a
  scheduled operation, not a 2 a.m. break-glass.
- One generation of operator hosts may still have `ghcr.io` baked into
  shell history / aliases / scratch scripts. The Phase 6 SOP refresh
  (this ticket) is the documentation half of the fix; the host bootstrap
  refresh is tracked separately under META OP-1468 P6.

## Rollback path

Rollback is the inverse of cutover and is owned by the same operator
window that did the cutover. The mechanical steps:

1. Re-enable the GHCR-push job in `.gitlab-ci.yml` and flip the
   `CANONICAL_REGISTRY` CI variable from `gitlab-cr` to `ghcr`.
2. Re-publish the GHCR pull-token bundle to operator hosts.
3. Push a no-op tag to validate that the next sign job uses the GHCR
   digest as the canonical signed reference (per phase-31f Q3).
4. Revert this ADR to `status: Superseded` and link the forward ADR.

The sign job does **not** re-sign already-signed images — existing
GitLab CR signatures stay valid for historical verification regardless
of the active canonical registry. This is the phase-31f §3.10
signing-trust-root-independence invariant in action.

## References

- META OP-1468 — image pipeline cutover (Phases 1–6)
- OP-1474 — this ticket (Phase 6: SOP + ADR + memory references)
- ADR-0002 — GitLab primary / GitHub mirror / Gerrit review layer
- `docs/sprint-s12/phase-31f-ticket-spec.md` §3.10 — signing-trust-root
  independence invariant that gates cutover safety
- `docs/operations/release-image-pipeline.md` — GHCR-era pipeline
  (frozen; kept for historical verification)
- `docs/operations/prod-deploy-runbook.md` §1a — Required env keys
  checklist updated for this cutover
- `docs/operations/release-cut-runbook.md` §1–§2 — GitLab-over-sora-SSH
  push step that replaced the GitHub push step
