# Codex Review Prompt — Model B (rc-tag → promote) release-train design + implementation audit

**Target output file**: `/tmp/model-b-rc-promote-codex-audit-2026-05-21.txt`
**Reviewer role**: independent technical reviewer. You did NOT write this design. Find **problems, design flaws, unreasonable assumptions, race conditions, security/provenance gaps, and operational hazards** in BOTH the proposed Model B flow AND how it collides with the existing code. Be concrete and adversarial. Your prior audit (`docs/audit/codex-reviews/retire-main-release-train-codex-audit-2026-05-21.txt`) already covered the general retire-main migration; THIS pass is specifically about whether Model B (rc → promote-by-digest) is sound and implementable.

## What to read first
1. The design under review: `docs/design/2026-05-21-model-b-rc-promote-release-train.md` (the concrete flow + change points + 6 known open wrinkles).
2. The decision context: `docs/adr/ADR-0040-single-trunk-release-train.md`.
3. Existing code the design touches — verify the design's assumptions against reality, cite `path:line`:
   - `.gitlab-ci.yml` (tag trigger, build/sign/sbom/attest/bundle stages, `:latest` push, COSIGN_KEY vs OIDC, sha-short tag)
   - `scripts/sync_staging_to_develop.sh`, `scripts/staging_deploy.sh`
   - `scripts/deploy-prod.sh`, `scripts/check_deploy_ref.sh`, `deploy/prod-deploy-allowlist.txt`
   - `backend/agents/auto_tag_release.py`, `backend/agents/auto_promote_main.py`
   - `scripts/emit_bundle_json.sh`, `scripts/build_image_bundle.py`, `backend/api_versioning.py`, `backend/routers/health.py`, `backend/frontend_compat.py`
   - `docker-compose.prod.yml`, `deploy/staging/docker-compose.yml`
   - `Dockerfile.backend` / `Dockerfile.frontend` / `Dockerfile.bridge` (how bundle.json is baked; how the image references its own tag/digest)
   - `.gerrit/project.config` (Verified gate, submit rules)
   - the release conductor / coordinator: `backend/release_conductor/state_machine.py`, `backend/agents/pipeline_coordinator*.py`

## Tasks — find what is wrong or risky

A. **Promote-by-digest correctness.** Is "re-tag digest D → final `v*` without rebuild" actually achievable with this registry + `docker buildx imagetools` / `crane`? Does cosign sign/verify and the attestation/SBOM survive a re-tag if signing is by-tag vs by-digest? Trace the exact signing/verification key in `.gitlab-ci.yml`. What breaks?

B. **bundle.json / `/api/version` identity wrinkle (open #1).** The baked `/app/bundle.json` is built with the rc git_ref. After promotion, what does a running prod container report? Is digest-as-truth defensible, or does this corrupt provenance, `/readyz`, FE/BE compat, or audit? Propose the least-bad resolution.

C. **Version allocator + concurrency.** Pressure-test the monotonic allocator: two concurrent auto-cuts, a retried promote, a promote that races a new rc, an aborted pipeline mid-tag. Where can a tag get moved/reused/duplicated? Is there any existing allocator in the code, or is this all net-new?

D. **Green-gate sufficiency.** The design admits there is no per-SHA full-test signal today (`.gitlab-ci.yml` has no test stage; Gerrit Verified disabled). Is the staging-gate JSONL (smoke/canary) an acceptable rc gate, or is promoting an rc that only passed smoke to a "prod-usable final tag" unsafe? What is the minimum honest gate?

E. **Staging fidelity.** Does staging actually validate the SAME artifact that gets promoted (digest equality)? Check `sync_staging_to_develop.sh` / `staging_deploy.sh` / staging compose for any place the deployed image could differ from the rc digest (latest fallback, GHCR vs GitLab registry, worker vs api image skew).

F. **Migration / rollback under tag promote.** deploy-prod.sh runs alembic on deploy. With promote-by-digest and no release branch, is rollback to the previous final tag safe? Where must an expand/contract migration gate live so a non-backward-compatible migration BLOCKS the promote (not just warns)?

G. **rc storm / cost / GC.** Estimate the build + registry cost of daily (or per-green-commit) rc builds of 3 signed images, and whether the design's GC hand-wave is adequate. Any pipeline concurrency / queueing hazard.

H. **Anything else** — security (who can push tags / trigger promote; can a non-human bot create a final `v*` tag?), supply-chain, observability, operator foot-guns, and any place the design contradicts the existing code or ADR-0040.

## Output
Structure by task A–H. For each finding: `path:line` + the flaw + why it bites + severity (HIGH/MED/LOW) + a concrete fix or the decision the operator must make. End with: (1) a **GO / GO-WITH-FIXES / NO-GO** verdict on Model B as designed, (2) the **must-fix-before-implementation** list, and (3) any **simpler alternative** if Model B is over-engineered for this codebase. Write the FULL review to `/tmp/model-b-rc-promote-codex-audit-2026-05-21.txt`; do not summarize to stdout.
