# Release-train readiness + operations handoff (2026-05-22)

**Audience**: operators + agents picking up the single-trunk Release Train. This is the SINGLE entry point — the model, the verified mechanism, the runbooks (cut a release / operate staging / signing), the key facts, and the remaining activation items. Companion to ADR-0040 (the decision), ADR-0042 (registry/distribution), and `[[reference_runner_operations_sop]]` (the runner fleet).

> **TL;DR**: every release-train MECHANISM was hand-exercised end-to-end on 2026-05-22 and works — candidate build → staging deploy (systemd-daemon, survives restart) → /readyz + deploy-overlay → staging gate → **promote-by-digest** → **cosign signing**. What stands between here and a hands-off auto-release is a short list of **activation items** (below), not unknowns.

---

## 1. The model (ADR-0040)
- **`develop` is the only long-lived branch.** No `main`. Runner `feature/*` → `refs/for/develop` → Gerrit +2 → submit.
- **A release = a promoted IMAGE TAG + digest** (RT-20 image-tag-only): NO `v*` git tag. Candidate images `sha-<fullsha>` are built from a chosen green develop SHA; promotion **retags the validated digest** to `vX.Y.Z` in the GitLab CR (no rebuild → identical digest).
- **Sole registry = private GitLab CR `sora.services:49160/omnisight/omnisight-productizer`** (ADR-0042). GHCR decommissioned (prod + host run zero GHCR images). On-prem distribution would need a public registry later (ADR-0042 trigger).

## 2. Verified mechanism (what was hand-exercised 2026-05-22)
| Step | Mechanism | Status |
|---|---|---|
| Candidate build | `sha-<fullsha>` backend+frontend → CR (pipeline-API `CANDIDATE_SHA`, or `build_image.sh`) | ✅ built+pushed manually |
| Bundle digest-seal | bundle.json carries the real post-build digests (OP-1607) | ✅ flows through deploy+promote |
| Staging deploy | dedicated `omnisight-staging` stack, systemd-managed | ✅ healthy; **survives full restart (47s) + restart-storm** |
| /readyz + deploy-overlay | RT-08 overlay lock → `/api/version` deployed_tag/digests; gate fail-closed | ✅ overlay populates real values; **gate 503s fail-closed on a bad lock** |
| Staging gate | smoke/canary green evidence over the candidate digests | ✅ `assert_staging_gate_passed` matches digests |
| **Promote-by-digest** (RT-12) | `imagetools create` retag validated digest → `vX.Y.Z`, verify same digest | ✅ **`vX.Y.Z` resolved to the exact candidate digest, no rebuild** |
| **cosign signing** | key-based attestation, verifies against `deploy/cosign/cosign.pub` | ✅ signs + verifies (OP-1610) |

---

## 3. RUNBOOK — cut a release
Prereqs: a green `develop` SHA; `COSIGN_KEY`/`COSIGN_PASSWORD` exported (see §6); GitLab CR login (`docker login sora.services:49160`).
1. **Pick the SHA**: a develop commit with green CI (until RT-04a auto-records green, choose by hand).
2. **Build the candidate** (both images), tag `sha-<fullsha>`, push to CR:
   `docker build -f Dockerfile.backend -t sora.services:49160/omnisight/omnisight-productizer/backend:sha-<sha> .` (+ frontend) → `docker push`. (In CI this is the pipeline-API `candidate-*` jobs, which also cosign-sign + seal the bundle.)
3. **Seal the bundle** with the real digests (CI does this; manual = a bundle.json with `images.{backend,frontend}.digest`).
4. **Deploy to staging** (§4) on `OMNISIGHT_IMAGE_TAG=sha-<sha>` → verify `/readyz`=200 + `/api/version` overlay shows the real digest.
5. **Run the staging gate** (smoke) → green JSONL evidence over the candidate digests (`scripts/staging_gate.py`).
6. **Promote**: `python3 scripts/promote_image_bundle.py --bundle <sealed> --from staging --to vX.Y.Z --actor <you> --approval-refs <JIRA> --registry sora.services:49160/omnisight/omnisight-productizer --staging-evidence <green.json>` → retags the digest to `vX.Y.Z` + cosign-attests + writes the release_train row. (Verify: `vX.Y.Z` resolves to the candidate digest.)
7. **Deploy prod** by the promoted tag/digest (the existing prod deploy SOP `[[reference_prod_deploy_sop]]`).

## 4. RUNBOOK — operate staging
- **Authority compose**: `deploy/staging/docker-compose.yml` (PG-backed, GitLab CR). Env: `deploy/staging/.env` (gitignored, staging-only secrets; `OMNISIGHT_REGISTRY`=49160, `OMNISIGHT_IMAGE_TAG`=the candidate, `OMNISIGHT_ADMIN_PASSWORD`/`DECISION_BEARER`, `OMNISIGHT_LLM_PROVIDER=ollama`).
- **Daemon (boot-survivable)**: `omnisight-staging-compose.service` (user systemd, **enabled**, `WantedBy=default.target`, Linger=yes). `systemctl --user {start,restart,stop,status} omnisight-staging-compose.service`. Project = `omnisight-staging`, ports 8010/8011/55432/18080, isolated PG/volumes — never collides with prod.
- **Fresh DB init (one-time)**: the backend will NOT bootstrap an empty DB (integrity probe `exit 78`). On a brand-new PG volume, run once before first boot:
  `docker run --rm --network omnisight-staging_default -e OMNISIGHT_DATABASE_URL=postgresql+asyncpg://omnisight:<pw>@postgres:5432/omnisight_staging --entrypoint sh <backend-image> -c "cd /app/backend && python -m alembic -c alembic.ini upgrade head"`. (The PG volume persists across reboot, so this is once-per-fresh-volume only.)
- **Deploy-overlay lock** (RT-08): `scripts/write_deploy_overlay_lock.py --bundle <sealed> --tag <tag> ... --out $OMNISIGHT_DEPLOY_OVERLAY_DIR/deploy-overlay.lock` (writer now chmod 644, OP-1609). Compose mounts `$OMNISIGHT_DEPLOY_OVERLAY_DIR:/etc/omnisight:ro`. Set `OMNISIGHT_REQUIRE_DEPLOY_OVERLAY=1` to make `/readyz` enforce it. A lock change needs the backend recreated (`up -d --force-recreate backend-a backend-b`, OP-1609) to be re-read.
- **Verify**: `curl http://localhost:18080/readyz` (caddy ingress) → 200; `curl .../api/version | jq` → real `deployed_tag`/`deployed_digest_*`.
- **Health audit**: `bash scripts/deployment-audit.sh`. The `sora-bridge-sync` row is still annotated "stranded on `main`@rc1" (OP-1608), but that control-plane re-point has landed (§7) — the annotation in `scripts/deployment-audit.sh` is a hardcoded string and refreshing it is a tooling follow-up, out of scope for this docs batch.

## 5. RUNBOOK — teardown / rebuild staging
- Tear down (keep volumes): `docker compose -p omnisight-staging -f deploy/staging/docker-compose.yml down`. Add `-v` to wipe the DB (then re-do the fresh-DB init).
- The PG image MUST be `omnisight-postgres-pgvector:16-alpine` (schema needs the `vector` extension; vanilla `postgres:16-alpine` fails — OP-1605).
- Backends need a writable `/app/data` volume (`staging-data`) for the secret key (read_only rootfs — OP-1605).

## 6. RUNBOOK — cosign signing (key-based, ADR-0040 #5 reconciled)
- **The signing key is self-managed (NOT keyless)**: private `~/.config/omnisight/cosign/cosign.key`, password `~/.config/omnisight/cosign/cosign-password.txt`, public = committed `deploy/cosign/cosign.pub`.
- Sign/promote env: `export COSIGN_KEY=~/.config/omnisight/cosign/cosign.key COSIGN_PASSWORD="$(cat ~/.config/omnisight/cosign/cosign-password.txt)"`.
- The promote attests with `--key` + `--tlog-upload=false` (private; no public Sigstore Rekor leak) + a URI predicate-type (OP-1610).
- Verify an attestation: `cosign verify-attestation --key deploy/cosign/cosign.pub --type https://omnisight.dev/attestation/image-promotion/v1 --insecure-ignore-tlog <image@digest>`.
- ⚠ `build_image.sh` default `OMNISIGHT_COSIGN_KEY` path is stale (`cosign-private-key`) — set `OMNISIGHT_COSIGN_KEY=~/.config/omnisight/cosign/cosign.key` for build-time signing.
- ⚠ **CI signing-variable naming drift (OP-1705 doc-drift batch):** ADR-0023 §2.3/§11 names the GitLab CI signing variables `SORA_COSIGN_KEY` / `SORA_COSIGN_PASSWORD`, but the live `.gitlab-ci.yml` `candidate-sign-image` / `candidate-attest-image` jobs pass `--key "$COSIGN_KEY"` (with the key password supplied via cosign's native `COSIGN_PASSWORD` env var) — there is no `SORA_COSIGN_*` reference or alias anywhere in the pipeline. **For provisioning CI signing creds, the live pipeline names `COSIGN_KEY` / `COSIGN_PASSWORD` are authoritative.** ADR-0023's `SORA_COSIGN_*` is unreconciled; bringing the ADR (or the pipeline) into line is a follow-up — this docs batch must not edit ADRs (decisions) or CI.

## 7. Key facts / locations
- Registry: `sora.services:49160/omnisight/omnisight-productizer/{backend,frontend,installer}` (claude-bot = Maintainer, can push).
- Prod: compose `docker-compose.prod.yml` (project `omnisight-productizer`), PG-HA `omnisight-pg-primary`/`-standby` (pgvector), runs `v0.6.0`.
- Control plane: `/home/user/sora-bridge/` (separate checkout) runs the gerrit-jira-bridge heartbeat + pipeline-coordinator + merger-bot — **never delete it**; it now tracks `develop` (re-pointed off `main`@rc1 per OP-1608; it had been 79 commits stale).

## 8. Remaining activation items (the gap to hands-off auto-release)
| Item | Ticket | What |
|---|---|---|
| Green-evidence producer | RT-04a (in OP-1603 family) | CI records a green row per develop SHA so auto-cut picks a SHA without a human |
| Candidate-build CI auto-trigger | (scope:release-train) | pipeline-API auto-builds a candidate on a green SHA |
| cosign creds in CI / build-sign | OP-1610 follow-ups | `OMNISIGHT_COSIGN_KEY` path fix; CI `--tlog-upload=false`; promote inline-verify needs build-signed candidates |
| ~~Control-plane re-point off main~~ — **landed** | OP-1608 | ✅ sora-bridge re-pointed `main`@rc1 → `develop` (was the 1 honest audit fatal; the `deployment-audit.sh` annotation refresh is a tooling follow-up — see §4) |
| RT-08b/13b/16/17 | filed | overlay/rollback/dry-run/cutover evidence — now reachable on the live staging |

## 9. Gotchas this sprint learned (don't re-discover)
- **Archived ≠ resolved**: runner blocker-check only treats `公開済み`/Published as done; close [OP] tickets at 公開済み, never Archived ([[reference_runner_operations_sop]]).
- **pgvector PG image** + **writable /app/data** + **fresh-DB `alembic upgrade head`** are required for staging to boot (§4/§5).
- **Overlay lock must be 644** (container uid 65532 reads it) + a lock change needs `--force-recreate` (OP-1609).
- **Ungraceful backend crash → ~5% ingress blip** until caddy ejects (graceful rolling-restart avoids it; possible caddy passive-health tuning).
- **cosign ≥2.x rejects bare predicate-type strings** — use a URI (OP-1610).

## 10. See also
- ADR-0040 (single-trunk release train), ADR-0041 (merger conflict triage), ADR-0042 (image distribution).
- `docs/operations/runner-operations-sop.md`, `docs/operations/2026-05-22-staging-dry-run-tabletop-and-gap-inventory.md`, `docs/operations/release-train-hotfix-policy.md`, `docs/operations/release-train-actor-protection.md`.
- Memory: `[[project_audit19_staging_buildout]]`, `[[project_retire_main_blast_radius_audit]]`, `[[reference_prod_deploy_sop]]`.
