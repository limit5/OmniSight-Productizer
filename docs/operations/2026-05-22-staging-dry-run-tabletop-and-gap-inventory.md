# Staging stand-up + first release-train dry-run — tabletop walkthrough & gap inventory (2026-05-22)

**Purpose**: before standing up staging for real, this is a deep audit + paper dry-run of the whole release-train path, recording the problems each step would hit, the fix for each, and the verified env-var corrections — then a sized gap inventory to decide the next step. Sources: 3 parallel code/infra audits (env-contract, runbooks/scripts, dry-run machinery), cross-validated against live `docker ps` + registry probes.

## Verdict (TL;DR)
The staging infra (AUDIT-19) is **more drifted than "just env vars"**. Three layers of gap, smallest→biggest:
1. **Config drift** — quick, mechanical (dead registry port, wrong PG superuser, missing contract secrets). ~1–2 h.
2. **Runbook staleness** — several docs reference the retired `main`/GHCR/D2 world. ~½ day.
3. **Two unimplemented CODE connectors** — these HARD-BLOCK a *real* promote and are the real work: (a) candidate `bundle.json` never gets its real post-build digests, (b) the RT-08 deploy-overlay lock is read but never written. RT-08/RT-09 shipped the scaffolding, not these connectors.

**A MINIMAL dry-run (deploy candidate → migrate → /readyz → smoke gate, all observational) is reachable with only the config fixes.** A FULL dry-run (real promote-by-digest + enforced overlay/compat gates) needs the 2 code connectors first.

Also surfaced: **the staging stack actually RUNNING today is a 3rd, stale variant** (`ghcr.io/your-org/omnisight-backend:latest`, pre-RT-08) from `/home/user/sora-bridge/...`, **red on migration drift** (DB at `m_audit_29_final`, image only carries `0200`). It is neither repo compose.

---

## Deep audit — what's alive vs stale

### Registry (cross-validated, 3 sources)
- `sora.services:**49154**` (staging compose + `staging_deploy.sh` default) → **DEAD** (HTTP 000; old Phase-2 GitLab HTTP port).
- `sora.services:**49160**` → **LIVE** GitLab CR (HTTP 401, auth-gated); prod pulls `:49160/omnisight/omnisight-productizer/backend:v0.5.0-rc5-hotfix4`.
- `:5050` (prod compose comment) and GHCR `:latest` (running staging) → two more wrong assumptions. **4 registry assumptions in flight.**
- Path case: live is lowercase `omnisight/omnisight-productizer`; staging template has `OmniSight-Productizer`.

### Env-contract vs current backend (the release-train drift)
Hard-required to boot+green (from `backend/config.py` `validate_startup_config`, strict under `ENV!=debug`): `OMNISIGHT_DATABASE_URL`, `AUTH_MODE=strict`, **`OMNISIGHT_ADMIN_PASSWORD`**, **`OMNISIGHT_DECISION_BEARER`**, a provider key (or ollama), `DOCKER_RUNTIME=runsc`, webhook secrets for *enabled* integrations.
- **MISSING from staging contract**: `OMNISIGHT_ADMIN_PASSWORD` + `OMNISIGHT_DECISION_BEARER` (staging relies on an out-of-band `.env` to boot — not in `infra/staging/.env.template`).
- **RT-08 / RT-05c gates are INERT everywhere (prod too)**: neither `OMNISIGHT_REQUIRE_DEPLOY_OVERLAY` nor `OMNISIGHT_REQUIRE_FRONTEND_COMPAT` is set in any compose; the code docstrings claiming "the staging/prod compose sets it" are **aspirational, false**. So /readyz overlay+compat are observational, never blocking.
- `release_train` (RT-10a) + feature flags (RT-15) are **DB-backed, no required env** — good.
- Healthz port drift: template `18080` vs live `19000`.

### Runbooks / scripts liveness
| File | Verdict | Key stale item |
|---|---|---|
| `infra/staging/snapshot-restore.sh` | **STALE (broken, exit 5 daily)** | `STAGING_PG_SUPERUSER=omnisight` → role doesn't exist; must be **`omnisight_staging`** (line ~102). One-line fix unblocks the snapshot. |
| `scripts/staging_deploy.sh` | PARTLY-STALE | core trigger still gates on `main`/`master` change-merged (lines 162–168) — dead under ADR-0040; registry `49154`. |
| `scripts/sync_staging_to_develop.sh` | **ALIVE** (the train-correct path: develop-tip + `--image-tag` + `--bundle`) | minor URL defaults. |
| `scripts/staging_gate.py` | **ALIVE** | exit 2 = "suite red" (normal verdict, no live stack), not a bug. |
| `docs/operations/staging-environment.md` | **DEAD** | whole premise = `main_promoted` + GHCR + `auto_deploy_staging.py`. |
| `staging-runbook.md`, `staging-environment-runbook.md`, `staging-migration-5a-to-5c-runbook.md` | PARTLY-STALE | `main` gate, `git checkout main`, `pg-primary` (actual: `omnisight-pg-primary`), `-U omnisight` (actual `omnisight_staging`), D2/GHCR, `staging.sora.services` DNS. |
| `docs/sop/cross-host-portability-staging.md` | **ALIVE** (the corrective authority the others violate) | port-based, fail-closed. |

---

## Tabletop dry-run — 6 steps, what breaks, recorded solution

| # | Step | Verdict | What the dry-run hits | Recorded solution |
|---|---|---|---|---|
| 1 | **Candidate build** (RT-09) | PARTIAL | ADR-0040 hole #1 RESOLVED — `.gitlab-ci.yml:5` builds `sha-<fullsha>` from a develop SHA via pipeline-API. BUT `candidate-prepare-bundle` bakes `bundle.json` with **placeholder zero digests** (`:249-251`) and **no later job re-resolves the real backend+frontend digests**. No green-evidence producer (RT-04a) → pick SHA by hand. | **CODE**: add a CI step to re-emit `bundle.json` (or a `candidate-bundle.lock`) with post-build digests. Manually select green SHA until RT-04a lands. |
| 2 | **Migration** | **WORKS** | fresh staging PG → drift gate exits 0 → lifespan `alembic upgrade head` → `0247_release_train` applied. OP-1446/1447 both FIXED. | needs `/app/MANIFEST.json` baked (it is). none. |
| 3 | **/readyz** | PARTIAL | gate = db & migrations & provider & overlay & compat. overlay+compat are **observational** (REQUIRE_* unset) → **/readyz PASSES** on a fresh candidate, but the RT-08/RT-05c gates that are the whole point are silently inert. | **CONFIG+CODE**: to actually enforce, set `OMNISIGHT_REQUIRE_DEPLOY_OVERLAY=1`+`OMNISIGHT_REQUIRE_FRONTEND_COMPAT=1` AND implement the overlay-lock writer (step 5). For a minimal dry-run, leave observational. |
| 4 | **Smoke/canary gate** (OP-965) | **WORKS** (red until stack up) | probes `/healthz`+`/readyz`+`/api/version` (needs non-empty bundle/digest). exit 2 today = no live stack. evidence will stamp the **placeholder zero digest** from bundle.json. | none for the probe; the zero-digest is fixed by step 1. |
| 5 | **Promote-by-digest** (RT-12) | **GAP (hard blocker)** | mechanics correct (retag digest→`vX.Y.Z`, audit hard-gate, dry-run mode exists). BUT `assert_staging_gate_passed` requires canary-evidence digest == promote digest; both read the **placeholder zeros** → `imagetools create` hits a nonexistent digest, or gate mismatch raises. **A real promote cannot complete.** | **CODE (same as step 1)**: real-digest bundle artifact. Until then only `--skip-staging-gate` *dry* promotes work. |
| 6 | **Rollback** (RT-13b) | PARTIAL | RT-13a resolver (`release_train_status.py --to vX.Y.Z`) resolves the audited digest pair read-only — works. BUT **no RT-13b commit** wires it to a deploy; `staging_deploy.sh` blue-green gates standby on `/health` (liveness) **not `/readyz`** → can switch to alive-but-not-ready. | **CODE (small)**: wrapper piping resolver digests → deploy; switch standby gate to `/readyz`. |

**Top blockers to a *full* dry-run**: (1) no real-digest bundle artifact [steps 1+5], (2) deploy-overlay lock writer unimplemented [steps 3+5], (3) REQUIRE_* flags unwired [step 3], (4) no green-evidence producer [step 1, soft], (5) blue-green gates `/health` not `/readyz` [step 6].

---

## Env-var verification — correct / wrong→fix
| Var / setting | Current (staging) | Correct | Action |
|---|---|---|---|
| `OMNISIGHT_REGISTRY` | `sora.services:49154/omnisight/OmniSight-Productizer` | `sora.services:49160/omnisight/omnisight-productizer` | **fix** (compose ×4 lines + `.env.example:165` which says `:5050`) |
| `STAGING_PG_SUPERUSER` (snapshot) | `omnisight` | `omnisight_staging` | **fix** (unblocks daily snapshot exit-5) |
| `OMNISIGHT_ADMIN_PASSWORD` | absent from contract | required | **add** to `.env.template` (+ contract assert) |
| `OMNISIGHT_DECISION_BEARER` | absent from contract | required | **add** |
| `OMNISIGHT_REQUIRE_DEPLOY_OVERLAY` / `_FRONTEND_COMPAT` | unset (inert) | `1` once writers exist | **defer** (needs overlay-lock writer first) |
| `OMNISIGHT_STAGING_HEALTHZ_PORT` | template `18080` | live `19000` | reconcile |
| prod PG container name (docs) | `pg-primary` | `omnisight-pg-primary` | **fix docs** |

---

## Gap inventory — how big

| Bucket | Size | Items |
|---|---|---|
| **A. Config drift** | **Small (~1–2 h)** | registry port/path, PG superuser, add admin/bearer to contract, healthz port. Mechanical, unambiguous. |
| **B. Runbook staleness** | **Medium (~½ day)** | rewrite/supersede `staging-environment.md`; de-`main`/de-GHCR/de-D2 the other 3 runbooks + 2 scripts; fix container name + `git checkout develop`. |
| **C. Compose divergence** | **Decision** | 3 staging composes (`deploy/staging` PG/registry, root `docker-compose.staging.yml` SQLite orphan, live `sora-bridge/...`). Pick ONE authoritative; delete orphans. The live stack uses none of the repo files. |
| **D. CODE connectors (the real work)** | **Bigger (the gating chunk)** | (1) candidate bundle digest-resolution [hard-blocks promote]; (2) deploy-overlay lock writer [RT-08 identity]; (3) RT-04a green-evidence producer [soft]; (4) RT-13b rollback wrapper + `/readyz` standby gate. |

**Reachability now**: with **only bucket A**, a *minimal* dry-run runs (deploy → migrate → /readyz green → smoke gate), proving the deploy/runtime path. The *full* dry-run (real promote + enforced gates) is gated on **bucket D #1+#2**.

## Recommended next step (for decision)
1. **Stand up a minimal staging on bucket-A fixes** to prove deploy/migrate/readyz/smoke (low risk, no prod data, isolated ports/PG) — validates the bulk of the path cheaply.
2. **In parallel, file bucket-D #1 (bundle digest-resolution) + #2 (overlay-lock writer)** as the real connectors — these are what make a *promotable* candidate; they're small, well-scoped code, and were simply never wired.
3. Reconcile the compose divergence (C) + sweep the runbooks (B) as cleanup.
4. Defer the anonymized prod-snapshot (Phase B fidelity) — not needed for the dry-run + carries prod-data risk.
