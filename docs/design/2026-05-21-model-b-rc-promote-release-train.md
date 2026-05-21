# Model B — rc-tag → promote release train (concrete flow)

**Status**: ⛔ **NO-GO as drafted** per codex audit 2026-05-21 (`docs/audit/codex-reviews/model-b-rc-promote-codex-audit-2026-05-21.txt`). The promote-by-digest idea is viable but 10 must-fixes are required first. Implements the candidate-artifact model for ADR-0040 (single-trunk release train, retire `main`); resolves the original circular-dependency hole but introduced new identity/race issues codex caught.

## ⛔ Codex verdict: NO-GO until these are fixed (2026-05-21)

**THE core contradiction I got wrong (codex A1):** the existing GitLab pipeline triggers on the **git tag** (`$CI_COMMIT_TAG =~ /^v.*/`). If "promote" creates a final `v0.6.0` **git tag**, CI **rebuilds** → a *different* digest → defeats the whole "validated digest == shipped digest" point. **Resolution: the final release identity must be a registry IMAGE retag of digest D, NOT a new git tag.** Either (a) final = image tag only (then ADR-0040's "release = a v* git tag" wording must change to "release = promoted image digest"), or (b) if a final git tag is still wanted for provenance, the CI workflow MUST exclude final (non-rc) tags from the build job and route them to a promote-only path. Pick one before any code.

**Must-fix list (codex):**
1. Decide final release identity (git tag vs image tag vs both); final git tags must NOT trigger rebuild.
2. Real `release_train` allocator/lock (DB or atomic remote ref) with immutable rc tag + version + git SHA + per-image digests. `auto_tag_release.py`/`auto_promote_main.py` not reusable as-is.
3. Exact-SHA CI green signal (backend pytest + FE build/typecheck/lint/tests + migration-compat + build/sign/sbom/attest) — none exists today (no test stage; Gerrit Verified disabled).
4. Rewire staging+prod to the SAME GitLab registry namespace; **fail closed on unset tag**; drop `:latest` from rc builds + compose defaults; fix `pull_policy: missing` (staging can validate a stale/wrong image).
5. Staging gate must prove **digest equality + bundle identity**, not just develop-SHA smoke/canary.
6. Resolve cosign trust mode — pipeline signs by-key (`COSIGN_KEY`) but audit JSON/verify script claim keyless-OIDC. Make signer == verifier == audit.
7. Promote-time **expand/contract migration gate** that BLOCKS final promotion for rollback-unsafe migrations (deploy runs alembic; previous-tag redeploy ≠ schema rollback).
8. Lock down prod deploy: final-semver-tag-or-digest ONLY; reject branch refs + `-rc` tags; remove `--insecure-skip-verify` break-glass without audit (`check_deploy_ref.sh`).
9. `/api/version` expose `git_ref`/`git_sha` + a deployment overlay (`promoted_tag`/`deployed_digest`/`source_rc_tag`) — a promoted image reports `rc1` forever otherwise; do NOT re-seal the baked bundle (would change digest).
10. Define GitLab CR retention + pipeline `resource_group`/concurrency for rc storms.

**Useful existing pieces codex found** (I didn't know these existed): `scripts/promote_image_bundle.py` (digest retag via `buildx imagetools` — but GHCR-namespaced, signs only `promotions[0]`, no all-3 verify), `scripts/verify_image_signature.sh`, `scripts/enforce_image_retention.py` (GHCR + `-rc.N` regex), `scripts/check_migration_compat.py`. Some "net-new" work is partially built but mis-targeted.

**Codex's simpler alternative:** build immutable `sha-<fullsha>` images for develop commits (Model A), validate that exact artifact, then create the final tag only after green — avoids the rc/final identity split + allocator races entirely. Trade-off = branch-pipeline build cost.

---


## Core idea

`develop` is the trunk. A release candidate is an **`-rc<N>` tag** built by the existing tag-driven GitLab pipeline; staging validates **that exact built artifact**; on pass it is **promoted by re-tagging the SAME image digest** to the final `v<X.Y.Z>` tag. The final tag therefore *always* points at a staging-validated digest → "every final `v*` tag is prod-usable" holds. rc tags are cheap and disposable.

## Lifecycle (happy path)

```
develop ●──●──●──●(tip, GREEN)
                 │  ① auto-cut: resolve green develop SHA → allocate next version
                 │     → push tag  v0.6.0-rc1  @ that SHA
                 ▼
   ② GitLab pipeline (UNCHANGED trigger: $CI_COMMIT_TAG =~ /^v.*/)
        build ×3 (backend/frontend/bridge) → sign → sbom → attest → bundle
        publish  :v0.6.0-rc1  +  :sha-<short>   (digest D)
                 │
                 ▼
   ③ staging-sync deploys EXACTLY :v0.6.0-rc1 (digest D)
        run gates: smoke + canary + FE/BE compat + migration expand/contract check
                 │
        ┌──── fail ────┐                 ┌──── pass ────┐
        ▼              │                 ▼
   abandon rc;         │      ④ promote/bless: re-tag digest D → :v0.6.0
   fix on develop;     │           (NO rebuild; same digest; re-sign/attest BY DIGEST)
   cut v0.6.0-rc2 ─────┘           record in release_audit: v0.6.0 == D == git_sha
                                          │
                                          ▼
                       ⑤ prod deploy v0.6.0 BY DIGEST (rolling backend-a → /readyz → backend-b)
```

## Concrete change points (file-level)

| File | Change |
|---|---|
| `.gitlab-ci.yml` | (a) keep tag trigger as-is (rc matches `^v`). (b) **sign/attest BY DIGEST not tag** so promotion doesn't invalidate provenance (codex E1). (c) STOP pushing mutable `:latest` on rc builds (codex E3). (d) add a **promote job** (manual/API-triggered) that re-tags digest `D` → final `v*` via `docker buildx imagetools create` / `crane tag` (no rebuild) + re-signs by digest. |
| `scripts/sync_staging_to_develop.sh` | deploy `OMNISIGHT_IMAGE_TAG=v0.6.0-rc1` (the exact rc tag), NOT the raw develop SHA. Fail closed if the rc image is not present in the registry. |
| `scripts/staging_deploy.sh` | re-key from `change-merged on main/master` to "rc tag published" (or retire in favour of sync_staging). |
| `scripts/deploy-prod.sh` + `scripts/check_deploy_ref.sh` + `deploy/prod-deploy-allowlist.txt` | tag/digest-only; allow only **final** `v*` (reject `-rc`, reject branch refs main/release/hotfix); deploy by digest; drop `MASTER_HEAD` metadata. |
| `backend/agents/auto_tag_release.py` | REPLACE: no `--main-branch`, no `release/v*` branch update, no `auto_promote_main` import. New behaviour = rc-cut + (separate) promote. Operate only on develop commits. |
| NEW `backend/agents/release_train.py` (or extend coordinator) | the auto-cut scheduler + **monotonic version allocator** (refuse to move/reuse tags; concurrency-safe) + green-gate check + promote driver. |
| `scripts/emit_bundle_json.sh` / `backend/api_versioning.py` | record `git_ref`+`git_sha` in bundle and expose in `/api/version`; handle the **rc→final identity wrinkle** (see Open #1). |
| `docker-compose.prod.yml` + `deploy/staging/docker-compose.yml` | single registry namespace; **fail closed if `OMNISIGHT_IMAGE_TAG` unset** (no `:latest` default); staging off GHCR/latest. |
| `.gerrit/project.config` | add a real **develop green gate** (Verified active on develop) so auto-cut has a per-SHA green signal; remove main/release-cut SRs. |
| registry | GC policy for old `-rc` tags (they proliferate). |

## Version + cadence
- Tag scheme: `v<MAJOR>.<MINOR>.<PATCH>-rc<N>` → promote to `v<MAJOR>.<MINOR>.<PATCH>`. Hotfix = `v<X.Y.(Z+1)>-rc1` cut from develop (no branch).
- Allocator: monotonic, persisted, atomic (two concurrent auto-cuts must not collide); NEVER move an existing tag.
- Cadence: time-boxed auto-cut (e.g., daily) OR coordinator-decided; only cuts from a GREEN develop SHA.

## Green gate before an rc is cut (codex B2/F1)
Minimum before `auto-cut` pushes an rc tag: exact develop SHA resolved; all release-ticket changes merged on develop; no open blockers; **fresh CI test/lint/build pass for that exact SHA** (currently MISSING — `.gitlab-ci.yml` has no test stage, Gerrit Verified disabled); migration expand/contract classified backward-compatible.

## Failure / rollback
- rc fails staging → abandon (cut next rc from fixed develop). Final tag never created → no bad final tag.
- post-promote prod issue → redeploy previous final `v*` digest (valid only if migrations were expand/contract; else DB intervention). Mark bad final tag in `release_audit`; never delete/move an immutable tag.

## Known OPEN wrinkles (for codex to pressure-test)
1. **bundle.json/`/api/version` identity after promote.** The image baked `/app/bundle.json` at build time with `git_ref=refs/tags/v0.6.0-rc1`. After re-tagging digest D to `v0.6.0`, the running container's `/api/version` still reports `rc1`. Is that acceptable (digest is the truth, rc1==v0.6.0), or must promote re-seal an external bundle? (interacts with codex E2/E4).
2. **Signature/attestation portability.** If cosign signs by tag, re-tagging breaks verification; must sign by digest. Confirm the registry + cosign + admission/verify path all key on digest.
3. **Promote atomicity.** Re-tag digest D → `v0.6.0` must be atomic and idempotent; what if promote runs twice, or two rc's race to the same final version?
4. **"Green" definition.** No per-SHA full-test signal exists today. Is staging-gate (smoke/canary JSONL) sufficient as the rc gate, or is a real CI test stage mandatory before promote?
5. **rc storm / cost.** Daily (or per-green-commit) rc builds × 3 images + sign + sbom + attest = CI + registry load. GC + cadence tuning needed.
6. **Frontend.** rc must carry the FE bundle id (today FE build-commit env is empty); FE feature flags (separate work) must gate incomplete UI in an rc that promotes.
