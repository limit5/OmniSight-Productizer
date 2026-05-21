# Model A — candidate-build → validate → promote-by-retag release train (concrete flow)

**Status**: ✅ **CHOSEN DIRECTION** — codex audit 2026-05-21 (`docs/audit/codex-reviews/model-a-candidate-build-codex-audit-2026-05-21.txt`) verdict = **GO-WITH-FIXES**; direct recommendation = **"Build Model A, not Model B, but revise it first."** Beats Model B on the deciding factor: **operational identity** (B leaves prod images that say `v0.6.0-rc1` while operators say `v0.6.0`; A's sha-built candidate is honest build provenance + final semver as deployment metadata). NOT implementation-ready until the must-fixes below are done.

## ✅ Codex verdict: GO-WITH-FIXES — build A over B, with ONE critical revision

**THE revision (codex C1/C2/G1 — my draft got this wrong):** my draft said "version assigned only at promote / candidate has no version." That **breaks the existing version-keyed planning** — JIRA `fixVersion`, `RELEASE-vX.Y.Z` META, release notes, changelog ALL need the version BEFORE shipping (`release_conductor/state_machine.py` refuses empty version; `release_notes_from_milestone.py`/`auto_changelog.py` query exact fixVersion). **Corrected model: RESERVE the semver `vX.Y.Z` early (for planning), but do NOT use it as a build/git tag until promote.** The candidate is still built as `sha-<fullsha>`; promotion retags the validated digest to the reserved `vX.Y.Z`. This keeps the existing planning workflow intact AND gets A's benefit (honest sha provenance + digest-promote, no rebuild).

**Candidate-build trigger (codex A1/A2/A3/G2):** MUST be an **API/manual pipeline with `CANDIDATE_SHA=<40hex>`** that fetches origin/develop, asserts the SHA is an ancestor, `git checkout --detach $CANDIDATE_SHA`, builds `sha-<fullsha>` images, signs by digest, never pushes `:latest`. **Do NOT use a `cand/<sha>` git tag** — codex: that converges right back to Model B's rc-tag/rebuild hazard with a different name. (Current CI literally cannot build a non-tag SHA at all today — `workflow: $CI_COMMIT_TAG =~ /^v/` else `never`.)

**Mandatory operator tooling (codex B1/B3/F5):** A is LESS confusing than B *only after* a `release_train_status.py --env prod` tool + `/api/version` overlay that answer, from one source: **`prod vX.Y.Z == git_sha S == backend/frontend/bridge digests D*`**. Without it, A is MORE confusing at 3am (runtime reports sha, ops think in versions). Treat as mandatory, not nice-to-have. Rollback command takes `--to vX.Y.Z` and resolves the digest triplet from immutable release_audit.

**Must-fix before implementation (codex 12):**
1. API/manual `CANDIDATE_SHA` pipeline (NOT `cand/<sha>` tags).
2. CI candidate workflow: checkout+verify exact full SHA → build `sha-<fullsha>` → sign/attest by digest → no `:latest`.
3. Reserve `vX.Y.Z` before planning, create final image/git tag only at promote.
4. `release_train` state/lock: candidate_sha, green evidence, per-image source digests, reserved version, promotion state, actor, final-tag digest-equality.
5. Staging+prod same GitLab CR namespace; fail closed on unset ref; digest lock files; verify pulled-digest equality.
6. `/api/version`+`/readyz` deployment overlay (build git_sha/ref + deployed tag/digest triplet + promotion audit id).
7. Rewrite `promote_image_bundle.py` (or new promote job): GitLab CR, all 3 images, target-tag digest verify, per-image attestations, idempotent.
8. Cosign trust mode consistent (signer == verifier == audit JSON == docs).
9. Exact-SHA green gate + promote-time expand/contract migration gate.
10. GitLab CR retention for unpromoted `sha-*` (protect any digest referenced by release_audit).
11. Disable/delete old main/release-cut automation + Gerrit SRs in the SAME migration window.
12. Operator `vX.Y.Z ↔ git_sha ↔ digest triplet` status/rollback tool.

Note: ~7 of these (5,6,8,9,10,11 + green gate) are SHARED infra prereqs — needed for ANY model, not A-specific.

---


## Core idea

`develop` is the trunk. A release candidate has **no version number** — it is simply an **immutable image set built from a chosen green develop commit**, identified by `sha-<fullsha>` (commit digest). Staging validates that exact artifact. Only **after** it passes is a **final version `vX.Y.Z` assigned by re-tagging the SAME image digests** (no rebuild). There is no `rc` tag and no up-front version allocation — version is assigned at promote time. This removes Model B's rc/final identity split + rc allocator races.

## Lifecycle (happy path)

```
develop ●──●──●──●(tip)
              │  ① scheduler/列車長 picks a GREEN develop SHA  (green = per-SHA CI test gate passed)
              ▼
   ② candidate build of THAT exact SHA → immutable images  backend/frontend/bridge :sha-<fullsha>
        (sign BY DIGEST → sbom → attest → bundle)   digests {Db, Df, Dbr}
              │
              ▼
   ③ staging deploys EXACTLY :sha-<fullsha> (by digest) → gates: smoke + canary + FE/BE compat + migration expand/contract
        record digest equality (deployed digest == built digest)
              │
        ┌── fail ──┐                ┌── pass ──┐
        ▼          │                ▼
   discard;        │     ④ promote: allocate next final version vX.Y.Z
   fix on develop; │        re-tag the SAME digests {Db,Df,Dbr} → :vX.Y.Z  (NO rebuild; re-sign/attest BY DIGEST)
   pick newer SHA ─┘        record release_audit: vX.Y.Z == {digests} == git_sha
                                          │
                                          ▼
   ⑤ prod deploy vX.Y.Z BY DIGEST (rolling backend-a → /readyz → backend-b)
```

Key property: **the digest validated on staging is byte-identical to the digest shipped to prod** — version assignment is pure metadata (a tag re-point), not a rebuild.

## Candidate-build trigger — the design choice to pressure-test

Today CI only runs on `$CI_COMMIT_TAG =~ /^v.*/`. To build a chosen develop SHA (not the tip-as-tag), three options:

| Option | Mechanism | Trade-off |
|---|---|---|
| **A-every** | branch pipeline on every develop push → build `sha-<fullsha>` | simplest logic; **highest** CI + registry cost; most GC |
| **A-ondemand (CHOSEN)** | scheduler triggers a build of the chosen SHA via the **GitLab pipeline API** (`POST /pipeline` with `CANDIDATE_SHA=<40hex>` variable; job does `checkout --detach $CANDIDATE_SHA`). ⚠ **`cand/<sha>` git-tag trigger is REJECTED** (codex: converges back to Model B's rc-tag/CI-rebuild hazard). | cost ≈ Model B; the build trigger is NOT a release identity (no version), just a way to materialise `sha-<fullsha>` |
| **A-hybrid** | build only on `develop` commits that the scheduler marks "release-intent" | middle ground |

Recommended: **A-ondemand**. The candidate build is throwaway; the ONLY durable release identity is the final `:vX.Y.Z` image tag created at promote.

## Concrete change points (file-level)

| File | Change |
|---|---|
| `.gitlab-ci.yml` | add a candidate-build path that builds `sha-<fullsha>` for a chosen develop SHA via **pipeline-API trigger with `CANDIDATE_SHA`** (NOT a `cand/*` git tag — rejected). **Sign/attest BY DIGEST** (codex E1). STOP pushing mutable `:latest`. Add a **promote job** that re-tags validated digests → `:vX.Y.Z` (no rebuild) + re-signs by digest + verifies all three final tags resolve to the expected digests. |
| `scripts/sync_staging_to_develop.sh` | deploy the candidate by **digest** (`backend@sha256:Db` …) pulled from GitLab CR; fail closed if any of the 3 images/digests is absent or mismatched. |
| `scripts/deploy-prod.sh` + `check_deploy_ref.sh` + `prod-deploy-allowlist.txt` | accept only **final `vX.Y.Z` image tag or exact digest**; reject branch refs + candidate refs; drop `MASTER_HEAD` metadata; remove `--insecure-skip-verify` without break-glass audit. |
| `backend/agents/auto_tag_release.py` | REPLACE with a tag-only promote (no `--main-branch`, no `release/v*`, no `auto_promote_main` import); operate by digest. |
| NEW `backend/agents/release_train.py` (or extend coordinator with tested acting-mode) | scheduler: green-SHA selection → candidate build trigger → wait for staging gate (digest-matched) → **final version allocator** (monotonic `vX.Y.Z`; no rc number; refuse to reuse/move) → promote driver. |
| `scripts/promote_image_bundle.py` | REWIRE to GitLab CR namespace; retag + re-sign + verify **all three** images; emit per-image attestation + one bundle audit row (currently GHCR-namespaced, signs only `promotions[0]`). |
| `scripts/build_image_bundle.py` / `emit_bundle_json.sh` / `backend/api_versioning.py` | expose `git_sha` + a deployment overlay (`deployed_tag`/`deployed_digest`) in `/api/version`. Baked bundle = build provenance (the sha); final version is overlay metadata (NO re-seal — would change digest). |
| `docker-compose.prod.yml` + `deploy/staging/docker-compose.yml` | single GitLab CR namespace; **fail closed on unset tag**; digest-pinned refs; `pull_policy: always` (or digest refs) so staging never validates a stale local image. |
| `.gerrit/project.config` | enable a real **develop green gate** (Verified active on develop = the per-SHA CI signal); remove main/release-cut SRs. |
| registry | GC: keep final `vX.Y.Z` digests indefinitely; delete unpromoted `sha-*` candidate digests after N days; protect any digest referenced by release_audit. |

## Version + cadence
- Final tag scheme: `vX.Y.Z` (no `-rc`). Hotfix = `vX.Y.(Z+1)` from a newer develop SHA (no branch). Pre-release suffix only if external tooling needs it — pick ONE grammar across CI/deploy/retention.
- Allocator assigns the final version only at promote (simpler than B — nothing to allocate at build time). Must be monotonic + atomic + never move an existing tag.
- Cadence: scheduler picks green SHAs on a cadence (e.g., daily) or on operator demand.

## The 7 SHARED infra prereqs (same as Model B — not A-specific)
per-SHA CI test gate · staging fidelity (no GHCR/latest, fix pull_policy, fail-closed, digest-verify) · cosign trust-mode consistency · expand/contract migration gate before promote · prod-deploy lockdown (tag/digest-only, no break-glass-without-audit) · Gerrit main/release-cut removal + develop Verified · GitLab CR retention + pipeline concurrency.

## OPEN wrinkles (for codex to pressure-test)
1. **Candidate-build trigger mechanism** — is "build a specific develop SHA that is not a branch tip and not a release tag" cleanly doable in GitLab CI without (a) building every commit or (b) reintroducing a versioned tag? Does the `cand/<sha>` build-tag idea just recreate Model B's rc-tag-with-a-different-name (and its CI-rebuild-on-tag hazard)?
2. **Promote = retag, but `/api/version`/bundle still report the sha** — does "version is metadata overlay, digest is truth" actually work end-to-end through health, audit, FE/BE compat, and operator tooling, or does it confuse operators who expect `/api/version` to say `v0.6.0`?
3. **Final version allocation timing** — assigning the version only at promote: how does an operator/changelog refer to "the release" before it has a number? Does the JIRA release META (which is version-keyed) break?
4. **Cost of A-every vs A-ondemand** — if A-ondemand uses a build trigger, is it meaningfully simpler/cheaper than Model B, or does it converge to B minus the version-at-rc?
5. **"Green" still undefined** — same as B: no per-SHA full-test signal exists today.
6. **Maintenance/operator clarity over a year** — two image-tag namespaces (`sha-*` candidates + `vX.Y.Z` finals) + digest-pinned compose: is this more or less confusing than B's rc/final tags in day-to-day ops, incident response, and rollback?
