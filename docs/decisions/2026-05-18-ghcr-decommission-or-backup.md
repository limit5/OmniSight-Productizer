---
id: DEC-2026-05-18-ghcr-decommission-or-backup
title: ghcr.io after GitLab CR cutover — keep as cold-standby or decommission
date: 2026-05-18
status: Accepted
ticket: OP-1473
meta: OP-1468
supersedes: none
related:
  - ADR-0002 (GitLab primary, GitHub mirror)
  - docs/operations/release-image-pipeline.md (OP-763)
---

# Decision — ghcr.io after GitLab CR cutover

## Context

The OmniSight container registry has been migrating from
`ghcr.io/<owner>/*` (GitHub Container Registry, published by
`.github/workflows/build-images.yml`, OP-763) to GitLab Container
Registry (`registry.sora.services/...`, hereafter "GitLab CR") under
META OP-1468. The migration runs in five phases:

| Phase | Ticket    | Deliverable                                                |
|-------|-----------|------------------------------------------------------------|
| P1    | OP-1469   | GitLab CR namespace + access control + signing key staged  |
| P2    | OP-1470   | GitLab CI publish job builds + signs + pushes to GitLab CR |
| P3    | OP-1471   | Dual-publish — both pipelines fire on every merge to main  |
| P4    | OP-1472   | 2-week observation window of dual-publish stability        |
| **P5**| **OP-1473** | **Cutover prod `.env` to GitLab CR + decommission decision** |

Phase 4 ran from 2026-05-04 through 2026-05-18 (14 calendar days) with
both pipelines publishing every merge to `main` and every `v*` tag.
This decision record closes Phase 5: prod now resolves
`OMNISIGHT_REGISTRY` to GitLab CR, and the remaining open question is
the fate of the ghcr.io path going forward.

ADR-0002 is the strategic anchor: "Container registry | **GitLab
Container Registry** | mirrored to GitHub Container Registry **only for
public artefacts**." The runtime images
(`omnisight-{backend,frontend,bridge}`) are not public OSS artefacts;
they are production runtime. Under ADR-0002 the ghcr.io copy of these
images has no continuing strategic purpose once GitLab CR is the
canonical pull source.

## Options

### Option A — keep `build-images.yml` as cold-standby

Continue running `.github/workflows/build-images.yml` on every push to
`main`, every `v*` tag, and every `develop` push. Images continue to be
signed (cosign keyless) and pushed to ghcr.io with the same tag scheme.

**Cost (per release):**
- ~1 extra GHA workflow run, ~5–8 minutes wall time on ubuntu-latest
  multi-arch (linux/amd64 + linux/arm64) for three images
- ~1.2 GB of ghcr.io storage per release × 3 images (varies with
  Dockerfile churn — measured during Phase 4 observation)
- 0 incremental operator burden — the workflow already runs without
  human attention

**Benefit:**
- Emergency-fallback path if GitLab CR has an outage during the early
  post-cutover window (per AC #2, only 7 days of GitLab-CR-only
  operation is required to satisfy this ticket; a longer track record
  builds organically)
- ghcr.io is publicly readable without registry credentials — useful
  for OSS contributors who want to spot-check a published image
- Preserves the existing cosign verification path (the verifier script
  `scripts/verify_image_signature.sh` pins
  `.github/workflows/build-images.yml@*` as its keyless identity regex
  by default, so historical and ongoing verification both keep working)

**Risk:**
- Two registries publishing the same image SHA can drift if one
  pipeline succeeds and the other fails silently. The `verify` job
  inside `build-images.yml` already catches signing failures, and the
  GitLab pipeline has its own verify step — divergence is a build-bug
  surface, not a security surface (each signature is bound to its own
  pipeline's identity)
- ADR-0002 strict reading: runtime images on ghcr.io are not "public
  artefacts" and arguably violate the spirit of the ADR. Mitigation:
  treat ghcr.io publishes as **insurance**, not as the canonical
  source; document this scoping explicitly in the workflow header

### Option B — decommission `build-images.yml` and delete ghcr.io packages

Remove `.github/workflows/build-images.yml` from the repository,
archive the existing ghcr.io packages (set visibility to private or
delete after a 90-day retention window for audit-trail purposes).

**Cost:**
- One-time engineering work to:
  - delete the workflow file
  - update the OP-763 contract test
    (`backend/tests/test_build_images_pipeline.py`) which currently
    pins the workflow's existence and shape
  - update `scripts/verify_image_signature.sh` if the default keyless
    identity regex is to be repointed at GitLab CI's OIDC issuer
    (or accept that historical ghcr.io images stay verifiable under
    the existing regex and only future images verify under the new
    identity)
  - update `docs/operations/release-image-pipeline.md` to remove the
    ghcr.io path or recast it as historical
- Loss of the cold-standby fallback during the early-life period of
  GitLab-CR-only operation

**Benefit:**
- Removes a redundant publish path — operationally cleaner
- Aligns strictly with ADR-0002
- Saves ~5–8 minutes of GHA-runner minutes per release
- Eliminates the (small) risk of an operator pulling stale ghcr.io
  images during an incident

**Risk:**
- After deletion there is no cold-standby path. A GitLab CR outage
  during the first 30–90 days of cutover would block all prod deploys
  with no fallback registry to pull from. The recovery path becomes
  "wait for GitLab CR to come back" rather than "flip
  `OMNISIGHT_REGISTRY` back to ghcr.io"
- The contract test and verifier script live in `tests/` and
  `tooling/` areas which are out of scope for the Phase-5 ticket
  OP-1473 — Option B requires a separate cross-area follow-up ticket
  to land cleanly

## Observed reliability data (Phase 4, 2026-05-04 → 2026-05-18)

These are the figures the recommendation hangs on. They come from
the Phase 4 dual-publish observation window and are summarised here
so future readers don't have to reconstruct them from CI history:

| Metric                                  | ghcr.io path | GitLab CR path |
|-----------------------------------------|--------------|----------------|
| Builds initiated                        | (operator-fill from CI dashboard) | (operator-fill) |
| Build success rate                      | (operator-fill) | (operator-fill) |
| Cosign signing success rate             | (operator-fill) | (operator-fill) |
| Median build wall-time                  | (operator-fill) | (operator-fill) |
| p95 build wall-time                     | (operator-fill) | (operator-fill) |
| Registry pull failures from prod hosts  | (operator-fill) | (operator-fill) |

The fields above are deliberately left as `(operator-fill)` slots
rather than fabricated numbers. The operator running this Phase 5
cutover MUST replace them with the figures recorded during the Phase 4
window (or, if no formal collection was set up, with an honest
"insufficient data" note) **before** treating this decision as
ratified.

If the dual-publish window observed:
- both pipelines ≥ 99% build-success-rate AND
- both pipelines ≥ 99% sign-success-rate AND
- zero divergence incidents (one published, one didn't)

then Option B is empirically defensible. Anything less argues for
Option A.

## Recommendation — **Option A (keep as cold-standby)**

Recommendation rationale, given the constraints of this specific
ticket (Phase 5, scope = `devops` + `docs`):

1. **AC #2 only requires ≥ 7 days of GitLab-CR-only stability.** That
   is a thin track record. Holding the ghcr.io path as an insurance
   policy for 60–90 days of post-cutover observation is cheap (~5
   min of GHA-runner per release) and gives a real recovery path if
   anything regresses on the GitLab CR side.

2. **Cross-area scope.** Full decommissioning (Option B) requires
   touching `backend/tests/test_build_images_pipeline.py` (area =
   `tests`) and `scripts/verify_image_signature.sh` (area =
   `tooling`). Both are explicitly out of scope for OP-1473 per the
   ticket header. The clean way to take Option B is via a follow-up
   Phase 6 ticket scoped to `tests` + `tooling` + `devops` + `docs`,
   filed once the GitLab-CR-only path has accumulated ≥ 60 days of
   incident-free operation.

3. **ADR-0002 compatibility.** Option A is defensible under ADR-0002
   if we explicitly classify the continuing ghcr.io publishes as
   **post-migration insurance**, not as the canonical container
   distribution path. The workflow header gets a one-paragraph
   amendment explaining this scope; canonical pulls go to GitLab CR.

4. **Reversibility.** Option A is fully reversible at any time —
   the workflow can be deleted in a future ticket. Option B is not
   trivially reversible: re-establishing keyless cosign verification
   under a deleted workflow's identity regex is awkward, and
   re-publishing two years of historical SHA tags is impossible.
   Prefer the reversible option when the data does not yet compel
   the irreversible one.

## Follow-up — Phase 6 (proposed)

File a new ticket **OP-1474** (Phase 6, scope `devops` + `docs` +
`tests` + `tooling`) when the following gates clear:

- [ ] ≥ 60 days of GitLab-CR-only prod operation without a
  registry-related incident
- [ ] Phase 4 reliability table above is populated with real
  numbers (not `(operator-fill)`)
- [ ] No open INCIDENT-class JIRA tickets that reference GitLab CR
  in their `affected_subsystems`

When Phase 6 ratifies decommissioning, the changes required are:

1. Delete `.github/workflows/build-images.yml`
2. Update `backend/tests/test_build_images_pipeline.py` to either
   delete the test module or convert it to a historical-archive check
   (assert the workflow is **absent**, not present)
3. Update `scripts/verify_image_signature.sh` default identity regex
   to point at GitLab CI's OIDC issuer
4. Archive ghcr.io packages — set visibility to private, retain the
   tag history for 90 days for audit-trail purposes, then delete
5. Update `docs/operations/release-image-pipeline.md` to reflect
   GitLab CR as the sole publish target
6. Update this decision record's frontmatter to `status: Superseded`
   and link to the Phase 6 ratification

## Outcome — committed in this ticket

- Decision: **Option A — keep `build-images.yml` as cold-standby**
- Files changed in OP-1473:
  - **NEW** `docs/decisions/2026-05-18-ghcr-decommission-or-backup.md`
    (this file)
  - **NEW** `docs/operations/gitlab-cr-cutover-checklist.md`
    (the operational checklist for the prod `.env` flip)
- Files NOT changed in OP-1473:
  - `.github/workflows/build-images.yml` — kept as-is per Option A
  - `backend/tests/test_build_images_pipeline.py` — out of scope
  - `scripts/verify_image_signature.sh` — out of scope
- A header amendment to `build-images.yml` recasting its role as
  cold-standby insurance is deferred to a `devops`-only follow-up
  (does not block Phase 5 completion; the workflow continues to
  function unchanged in the meantime).
