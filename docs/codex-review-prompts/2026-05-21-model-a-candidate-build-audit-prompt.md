# Codex Review Prompt — Model A (candidate-build → promote-by-retag) deep audit

**Target output file**: `/tmp/model-a-candidate-build-codex-audit-2026-05-21.txt`
**Reviewer role**: independent, adversarial technical reviewer. You did NOT write this design. You previously audited the retire-main migration (`docs/audit/codex-reviews/retire-main-release-train-codex-audit-2026-05-21.txt`) and ruled Model B NO-GO (`docs/audit/codex-reviews/model-b-rc-promote-codex-audit-2026-05-21.txt`). THIS pass audits **Model A**. The operator specifically wants to know: in IMPLEMENTATION and in REAL DAY-TO-DAY OPERATION, what about Model A is **unreasonable, hard to maintain, or confusing/chaotic** — not just whether it "works on paper".

## Read first
1. The design under review: `docs/design/2026-05-21-model-a-candidate-build-release-train.md`.
2. `docs/adr/ADR-0040-single-trunk-release-train.md` + your two prior audits in `docs/audit/codex-reviews/`.
3. Existing code (verify the design against reality, cite `path:line`):
   - `.gitlab-ci.yml` (only triggers on `^v` tags — can it build a non-tag develop SHA cleanly?), `Dockerfile.{backend,frontend,bridge}`
   - `scripts/sync_staging_to_develop.sh`, `scripts/staging_deploy.sh`, `scripts/deploy-prod.sh`, `scripts/check_deploy_ref.sh`, `deploy/prod-deploy-allowlist.txt`
   - `scripts/promote_image_bundle.py`, `scripts/build_image_bundle.py`, `scripts/emit_bundle_json.sh`, `scripts/verify_image_signature.sh`, `scripts/enforce_image_retention.py`, `scripts/check_migration_compat.py`
   - `backend/agents/auto_tag_release.py`, `backend/agents/auto_promote_main.py`, `backend/agents/pipeline_coordinator*.py`, `backend/release_conductor/state_machine.py`
   - `backend/api_versioning.py`, `backend/routers/health.py`, `backend/frontend_compat.py`
   - `docker-compose.prod.yml`, `deploy/staging/docker-compose.yml`, `.gerrit/project.config`

## Tasks — focus on unreasonable / hard-to-maintain / chaotic, not just correctness

A. **Candidate-build trigger reality (open #1, #4).** Can GitLab CI build a *specific develop SHA that is neither a branch tip nor a release tag* WITHOUT (a) building every commit or (b) reintroducing a versioned tag? Evaluate the three options (branch pipeline / disposable `cand/<sha>` build-tag / pipeline-API trigger) against the actual `.gitlab-ci.yml` workflow rules. Does `cand/<sha>` just recreate Model B's rc-tag (and the CI-rebuilds-on-tag hazard)? Be concrete about what actually triggers and what gets built.

B. **Two-namespace operator confusion (open #2, #6).** Model A runs `sha-<fullsha>` candidate images AND `vX.Y.Z` final images, with version assigned at promote and `/api/version`/baked bundle reporting the sha. Walk a real incident at 3am: operator sees prod serving digest D, `/api/version` says a sha (not `v0.6.0`), compose pins a digest. Is this MORE or LESS confusing than Model B? Where do operators get lost? What tooling/overlay is mandatory to make it survivable?

C. **Version-assigned-at-promote consequences (open #3).** The JIRA release META / `release_conductor` state machine is version-keyed. If the version doesn't exist until promote, how do changelog, release notes, JIRA META, blockers, and "what are we shipping" work BEFORE promote? Trace `backend/release_conductor/state_machine.py` + the release-as-state-machine assumption. Does Model A break the release-planning workflow?

D. **Promote-by-retag mechanics + provenance.** Same digest-retag concern as Model B: does `scripts/promote_image_bundle.py` actually work for GitLab CR + all 3 images + sign/verify by digest? Does re-tagging without rebuild keep cosign/SBOM/attestation valid? What is the maintenance burden of the promote job over time?

E. **Maintenance burden over a year.** GC of `sha-*` candidate digests, the allocator state, digest-pinned compose files (who updates them, how do they drift), the new `release_train.py` service vs the shadow coordinator. Where does this rot? What breaks silently after 6 months of unattended operation?

F. **Migration / rollback + green gate.** Same shared gaps as B (no per-SHA test gate; alembic runs on deploy; expand/contract not gated). Does Model A's "version at promote" make rollback clearer or murkier (rolling back to a previous `vX.Y.Z` digest vs a sha)?

G. **A-vs-B honest verdict.** Given everything: is Model A GENUINELY simpler and less chaotic to build and operate than Model B, or does A-ondemand converge to B with extra steps? Which has the smaller long-term maintenance surface for THIS codebase + a small operator team + AI runners?

## Output
Structure by task A–G with `path:line` evidence. For each issue: the problem + why it bites in practice + severity + concrete fix or decision needed. End with: (1) **GO / GO-WITH-FIXES / NO-GO** on Model A, (2) **must-fix-before-implementation** list, (3) a direct **A-vs-B recommendation** (which to build, and why) for this codebase. Write the FULL review to `/tmp/model-a-candidate-build-codex-audit-2026-05-21.txt`; do not summarize to stdout.
