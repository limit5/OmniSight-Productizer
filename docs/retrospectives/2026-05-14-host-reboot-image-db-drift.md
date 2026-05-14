# Retrospective: Host Reboot Exposed Image-vs-DB Migration Drift

**Date**: 2026-05-14
**Type**: Incident retro (unplanned interruption + deployment pipeline gap)
**Linked tickets**:
- OP-1035 (CI: publish per-develop-commit images to GHCR — only half of the pipeline)
- OP-1046 (Resolve alembic 0203 duplicate + merge 7 heads — drove DB to 0202)
- OP-1095 (runner presync failure at interruption — stash `rescue-OP-1095-presync-fail-2026-05-14T05:10Z`)
- AUDIT-23 / OP-976 (Shipped-but-not-deployed defense — failed to catch this)
- AUDIT-19 family (staging build-out — still operator-bring-up pending)

**Author**: Claude (interactive session) + sora (operator)
**Status**: Demo restored via temporary `/livez` healthcheck override; root-cause fix (image rebuild + auto-redeploy + drift monitor) **still pending**

---

## Situation

Windows host force-installed an update at ~2026-05-14 05:29 CST while runners were mid-execution overnight. WSL2 began graceful shutdown but Docker / docker-compose subprocesses did not terminate within the 30 s `stop-sigterm` window, so systemd SIGKILL'd them and WSL emitted:

```
InitTerminateInstanceInternal:2717: systemctl poweroff did not terminate the instance in 10000 ms, calling reboot(RB_POWER_OFF)
```

Host stayed offline ~3 h 51 min (05:29 → 09:20). On reboot, `omnisight-compose-prod.service` started but failed at 09:22:04 because `backend-a` healthcheck never went healthy. systemd marked the unit `failed`, and the `depends_on: service_healthy` chain stopped frontend / caddy / cloudflared / installer from starting. The public URL was therefore down from 05:29 until manual recovery at 10:50.

Operator was *not* mid-deploy. The expectation was: complete the in-flight Sprint S12 + S12.G + OP-1095 work first, then test rebuild + the full pipeline. The reboot pre-empted that plan and exposed the partially-built pipeline mid-construction.

## What actually broke (root cause chain)

The healthcheck failure was **not** caused by the reboot itself, nor by the half-finished auth middleware (the operator's first hypothesis). The actual chain:

1. **2026-05-07** — prod backend image (`ghcr.io/your-org/omnisight-backend:latest`, sha256:06d0f323…) was built. Its bundled alembic versions topped out at `0200_provider_usage_event.py`.
2. **2026-05-07 → 2026-05-13** — develop branch advanced. `OP-1046` (2026-05-13 11:54) resolved an alembic `0203` duplicate plus 7-heads merge, pushing the live DB schema to `0202`.
3. **2026-05-13 11:23** — `OP-1035` merged: CI now publishes a per-develop-commit image to GHCR. This is the **first half** of the auto-deploy pipeline. It does *not* trigger prod pull / restart, and `docker-compose.prod.yml` uses `pull_policy: missing` — meaning prod won't pick up new `:latest` images unless containers are recreated.
4. **2026-05-14 05:10** — runner stashed `rescue-OP-1095-presync-fail-2026-05-14T05:10Z`. OP-1095's presync logic failed; the runner self-rescued, but didn't get to finish before:
5. **2026-05-14 05:29** — host shutdown began.
6. **2026-05-14 09:20** — host back, docker-compose ran healthcheck on the 7-day-old image. `/readyz` does an always-on migration probe:
   ```json
   "migrations": {
     "ok": false,
     "detail": "migration_pending: current=0202 latest_file=0200_provider_usage_event.py"
   }
   ```
   503 returned. systemd unit went `failed`. Frontend chain never started.

Pre-reboot, the same drift had been silently failing readyz `118` consecutive times in the live container without alerting anyone — visible in `docker inspect ... .State.Health.FailingStreak` but not surfacing anywhere actionable.

## Hypothesis evolution during diagnosis

| Operator hypothesis | Verdict | Notes |
|---|---|---|
| "Auth middleware is half-done and blocks healthcheck" | ⚠️ Partially true | `/health` does return 401 (`www-authenticate: Cookie`), auth_baseline is in `enforce` mode. But the docker healthcheck probes `/readyz`, which is auth-whitelisted (`backend/auth_baseline.py:80`, `backend/main.py:600`). Not the cause of today's outage. Still a separate bug: `/health` *should* be in the whitelist (it's listed in `main.py:600`) but isn't honored. |
| "Was running on dev settings, got switched to prod, now broken" | ⚠️ Direction right, mechanism wrong | `OMNISIGHT_ENV=production` + `OMNISIGHT_READYZ_DEEP_CHECK=1` are set in `.env`. **But** the migration probe runs even with `DEEP_CHECK=` (empty), so disabling deep-check did *not* fix readyz. The real failure mode is image-vs-DB schema drift, not env-mode regression. |
| "Recent multi-env / canary / promotion ADR work is related" | ✅ Yes | OP-764 (D3 multi-env config), OP-960/983 (auto_promote_main), OP-965 (staging gate), OP-966/968 (force-promote), OP-1030 (staging gate port fix), and the in-progress OP-1035 (per-develop image build) are all on the same pipeline being assembled. OP-1035 supplies the build-half; pull / redeploy / drift-monitor halves are still missing. |

## Concrete actions taken (recovery)

All edits are **temporary**; they suppress the drift symptom rather than fixing the drift itself. They must be reverted once the image is rebuilt + DB schemas realign.

| # | File | Change | Reversal |
|---|---|---|---|
| 1 | `.env:70` | `OMNISIGHT_READYZ_DEEP_CHECK=1` → `=` (empty) with TEMP comment | Restore `=1`, remove TEMP block |
| 2 | `docker-compose.prod.yml:237` | backend-a healthcheck `/readyz` → `/livez` | Swap back |
| 3 | `docker-compose.prod.yml:311` | backend-b healthcheck `/readyz` → `/livez` | Swap back |
| 4 | `docker-compose.prod.yml:372` | caddy self-healthcheck `/readyz` → `/livez` | Swap back |
| 5 | `deploy/reverse-proxy/Caddyfile:67` | upstream `health_uri /readyz` → `/livez` (`:8000` block) | Swap back |
| 6 | `deploy/reverse-proxy/Caddyfile:151` | upstream `health_uri /readyz` → `/livez` (`/api/v1/*` route) | Swap back |
| 7 | `deploy/reverse-proxy/Caddyfile:299` | upstream `health_uri /readyz` → `/livez` (catch-all route) | Swap back |
| 8 | `docker stop ai_gateway` | Stopped sister-project nginx that was in restart-loop because `ai_engine` (ollama) container has been Exited for 10 days | `docker start ai_gateway` after `ai_engine` is brought back |

Item 1 stayed in place even though it doesn't fix the issue — kept as a visible breadcrumb for the cleanup checklist.

⚠️ While the override is in effect, **any endpoint that touches a column introduced in alembic 0201 or 0202 will 500**. Demo operations must stay on pre-0200-schema features.

One-shot reversal command (run *only after* image is rebuilt to match DB):

```bash
cd /home/user/work/sora/OmniSight-Productizer
sed -i 's|"http://localhost:8000/livez"|"http://localhost:8000/readyz"|g; \
        s|"http://localhost:8001/livez"|"http://localhost:8001/readyz"|g' docker-compose.prod.yml
sed -i 's|health_uri /livez|health_uri /readyz|g' deploy/reverse-proxy/Caddyfile
# .env line 70: restore OMNISIGHT_READYZ_DEEP_CHECK=1 + remove TEMP block manually
docker compose -f docker-compose.prod.yml --profile tunnel up -d --force-recreate
```

## Verification

Container state at 10:51 CST (after override applied):

| Container | Status |
|---|---|
| omnisight-productizer-backend-a-1 | Up (healthy) |
| omnisight-productizer-backend-b-1 | Up (healthy) |
| omnisight-productizer-caddy-1 | Up (healthy) |
| omnisight-productizer-frontend-1 | Up (healthy) |
| omnisight-productizer-cloudflared-1 | Up |
| omnisight-productizer-omnisight-installer-1 | Up (healthy) |
| omnisight-productizer-docker-socket-proxy-1 | Up (healthy) |
| ai_gateway (sister project) | Stopped (was in crashloop, intentionally halted) |

DB alembic state: `version_num = 0202` (confirmed via `psql` on `omnisight-pg-primary`).
Container alembic versions: top file `0200_provider_usage_event.py` (confirmed via `docker exec` ls).
Image build time: `2026-05-07T12:16:41Z` (7 days behind DB schema).

## Pipeline gaps still missing (the "打通" punch list)

Listed in the order they would have caught this outage:

1. **Image-vs-DB drift detector + alert** — `/readyz` already detects it; needs to feed a real alert channel (not just docker `FailingStreak`). 118 silent failures pre-reboot is the signal that AUDIT-23's `deployment-audit.sh` cron doesn't audit alembic-head-vs-image — only deployment metadata.
2. **Auto-pull + auto-redeploy on new `:latest`** — OP-1035 added the build side; the consumer side does not exist. Options: switch `pull_policy: missing` → `always` + cron `docker compose up -d`, or run a watcher (Watchtower / Diun / custom) that triggers redeploy when GHCR digest changes.
3. **OP-1095 presync-failure root cause** — the interruption stash (`rescue-OP-1095-presync-fail-2026-05-14T05:10Z`) is still on disk; the underlying presync bug is unfixed and will recur on next pickup attempt.
4. **`.env` overlap discipline** — single `.env` file holds prod secrets; no env-specific layering (dev/staging/prod). Already in scope under OP-764 D3 multi-environment config but not finished.
5. **Healthcheck endpoint contract** — `/readyz` returning 503 because migration probe is hard-coded always-on means there is no way to deploy a new image that lags the DB schema (forward-only). For rollback scenarios this is a design hole; for forward-only deploys it's fine. Worth an ADR.
6. **`/health` whitelist consistency** — `main.py:600` lists `/health` as whitelisted but the running container still 401s it. Either the whitelist is overridden by a downstream middleware, or there are two whitelists drifting. Low priority (no consumer relies on `/health` today) but worth tracing.
7. **Sister-project `ai_gateway` orphan** — sister stack's nginx points at an upstream container that's been dead 10 days. Either revive `omnisight-ai-core` properly or remove the nginx + cloudflared tunnel that depends on it.

## S12 / S12.G coverage analysis (post-incident review 2026-05-14)

After demo recovery, a sweep of the Sprint S12 + S12.G planning artifacts (committed yesterday as `f0274e7c`) and corresponding JIRA filing state was performed to answer two operator questions:

1. Does the S12 plan consider what happened today?
2. If today hadn't happened, would S12 / S12.G deliver "prod auto-deployment" as expected?

### JIRA filing state (as of 2026-05-14 ~11:30 CST)

| Layer | Filed | Status |
|---|---|---|
| Sprint S12 (Foundation Rebuild — 11 phases 31.A..31.K, ~306 fixed tickets) | **0 / ~306** | Gerrit +2 pending on planning commit; G.A-v0 hard-blocks any 31.A filing |
| S12.G META + Block (OP-1047, OP-1048) | 2 / 2 | OP-1047 進行中; OP-1048 承認済み |
| S12.G G.A-v0 (kernel; 31.A filing blocker, 3-5d) | 8 / 8 | All 公開済み (OP-1049..1056) |
| S12.G G.A-v1 (plugin framework, 2-3 wk) | 20 / 20 | 公開済み 2, 承認済み 2, **Under Review 1 (OP-1095 — interruption point)**, To Do 14, META 1 |
| S12.G G.B / G.C / G.D-minimal / G.F-limited | 0 / ~25 | Not yet decomposed to JIRA |
| SP-B-X (runner runtime backfill, cross-cutting) | 21 / 21 | Most 公開済み |

Filed total in S12 / S12.G / SP-B-X family: **51 tickets**. Planned total when S12 fully decomposes: **~360+**.

### Coverage matrix: 9 gaps exposed today vs S12 scope

| # | Gap exposed by today's incident | S12 coverage | Where in S12 |
|---|---|---|---|
| ① | 118 silent healthcheck failures (no alert) | ✅ Full | 31.G 9a Alertmanager→Discord webhook bridge + 5 alert rules + dedupe |
| ② | Image build automation | ✅ Full | 31.E Wave 2 (9a / 9b / CIWave2Smoke) + 31.F cosign |
| ③ | Canary deploy + first-cycle confirmation | ✅ Full | 31.H canary-runner + ops abstraction + CanarySmoke; 3bc 9-Doc first-cycle manual policy |
| ④ | Prod cutover orchestration | ✅ Full | 31.J 5-step orchestrator + scope_components + 7a→7b sequential rollback dry-run + pause/resume |
| **⑤** | **"Shipped-but-not-deployed" runtime detector** | 🟡 Weak | AUDIT-29a-2 (OP-1017) anti-pattern #13 doc + AUDIT-29a-3 (OP-1018) CI lint hook — **filing-time + static only, NOT runtime drift detector** |
| **⑥** | **Image-vs-DB alembic head drift** | 🔴 Design blind spot | None — S12 implicitly assumes canary-runner is the single canonical path for image deploy |
| **⑦** | **Auth middleware half-done / `/health` 401** | 🔴 None | No auth-cleanup ticket in any S12 phase or in S12.G |
| **⑧** | **WSL2 graceful shutdown / docker 30s timeout** | 🔴 None | 31.H BackupDRDrill verifies *restore* but not *shutdown contract* |
| **⑨** | **Sister-project `ai_gateway` orphan** | 🔴 None | Out of Productizer scope (`omnisight-ai-core`); cross-project hygiene not contracted |

Bottom line for Q2: **S12 + S12.G ships "auto deploy of routine image flow + canary auto-promote-after-baseline + 24/7 alerting"** but does NOT ship "auto cross-version cutover" — 31.J 6a..6e are deliberately `operator-window` class. Major version transitions (rc2 → GA) stay operator-confirmed by design.

### Detail: the 5 gaps S12 doesn't cover

#### ⑤ Shipped-but-not-deployed runtime detector (weak)

- **What S12 has**: `AUDIT-29a-2` (OP-1017) merged anti-pattern #13 into `architecture-anti-patterns.md`; `AUDIT-29a-3` (OP-1018) is a CI lint hook that rejects `area:devops` tickets lacking an operator-activation step.
- **What's missing**: runtime drift detector that compares the running container's image build-time / SHA against the latest `:latest` digest on GHCR. Today the running image was 7 days stale and no monitor flagged it.
- `scripts/deployment-audit.sh` (AUDIT-23 / OP-976) exists but audits deployment metadata (tickets with `deployed:` AC), not running-image-vs-source-of-truth drift.

#### ⑥ Image-vs-DB alembic head drift (design blind spot — biggest one)

- **S12 implicit assumption**: canary-runner is the single canonical path for image deploy → image carries its alembic migrations → on container start `alembic upgrade head` runs → DB always tracks the running image. Therefore drift between image and DB cannot occur.
- **Today's reality**: image at SHA `06d0f323` (built 2026-05-07, top migration 0200); live DB at `0202`. Drift exists because **DB was advanced through some path other than canary-runner image startup**. Candidates:
  - Cross-branch alembic head merge (OP-1046 on 2026-05-13 resolved 0203 duplicate + merged 7 heads → DB upgrade trail).
  - Manual `alembic upgrade head` from a dev runner workspace pointed at the prod DB.
  - A previous image rollback that left DB ahead.
- **S12 does not propose**: a runtime monitor exporting `image_alembic_head` vs `db_alembic_head` as a Prometheus metric, nor an alert rule when they differ.
- **Available infrastructure already**: `backend/metrics.py:704` defines a `readyz_migrations_pending` gauge — but it only fires inside the `/readyz` handler when deep-check is on; it is *not* a routine scrape signal and never reaches Alertmanager.
- **Candidate mitigations** (to be discussed):
  - Promote `readyz_migrations_pending` to a routine scrape signal (always-on, independent of `/readyz` deep-check toggle).
  - Add a Prometheus rule in 31.G that pages on `alembic_drift > 0` for >5 minutes.
  - Add an S12.G forbidden_combination: `network-production WITH alembic_drift > 0` → governance engine refuses to file any prod-touching ticket while drift is non-zero.
  - File a 31.I-Plus ticket that wires drift into 31.I-2bc healthcheck-validator and 31.I-8b daily health-verify timer.

#### ⑦ Auth middleware half-completion

- **Symptom**: `GET /health → HTTP 401 www-authenticate: Cookie` even though the path is explicitly listed in two whitelists (`backend/main.py:600`, `backend/auth_baseline.py:80`).
- **Most-likely cause**: a third enforcement point in `auth_baseline.py` (`OMNISIGHT_AUTH_BASELINE_MODE=enforce`) doesn't honor the whitelists, OR the whitelists are out of sync with a third enforcement layer added during the auth strict-mode rollout.
- **Not blocking today** (`/health` has no consumer; healthcheck moved to `/livez`), but indicates the auth strict-mode rollout was left half-finished — exactly matching the operator's memory.
- **Out of S12 scope today**: no auth-cleanup ticket exists in the 11 phases or S12.G.

#### ⑧ WSL2 graceful shutdown / docker 30s timeout

- **Mechanism**: `user@1000.service` `TimeoutStopSec` default is 30 s. Docker + `docker-compose down` subprocesses did not exit cleanly within that window → SIGKILL → WSL2 emitted `RB_POWER_OFF`.
- **Risk surface**: any in-flight Postgres COMMIT, fsync, or container shutdown hook is potentially aborted. Today no obvious corruption surfaced (Postgres started clean post-reboot), but this is luck-of-the-draw, not a guarantee.
- **31.H coverage**: BackupDRDrill verifies decrypt + restore + query contract — the *bottom of the incident funnel*. The top of the funnel (making sure shutdown actually completes before SIGKILL) is unaddressed.
- **Candidate fixes** (to be discussed):
  - Bump `TimeoutStopSec` on the systemd unit to ~120 s.
  - Add a pre-shutdown hook that runs `docker compose stop -t 60` explicitly, separately from compose's down command.
  - Add a host-side chaos test that simulates a forced shutdown and verifies post-reboot pg + compose state.

#### ⑨ Sister-project `ai_gateway` orphan

- `ai_gateway` (nginx, sister-stack `omnisight-ai-core_omnisight_net`) has been crashlooping for **10 days** because its upstream `ai_engine:11434` (ollama) container has been `Exited (0)`.
- Belongs to sister project `omnisight-ai-core`, not Productizer. 4th-runner / qwen3.6 27B local-LLM work is paused per [[project_local_llm_investigation]].
- **Why it matters now**: today's reboot would have re-triggered the crashloop indefinitely. Manual `docker stop ai_gateway` is the right short-term action.
- **Cross-project hygiene gap**: when one compose stack's dead container hammers shared docker daemon resources (CPU, log volume) on a co-tenant host, there's no contracted detection. Productizer S12 won't cover this; needs a different home.

## What this retrospective is NOT

- Not a tier-drift retro per §14 (no tier-shaped estimate to compare against — this was an unplanned external event).
- Not a Sprint S12 / S12.G retro — those are still in-flight and this incident does not close them.
- Not a runner-bug retro for OP-1095 — the OP-1095 stash needs its own analysis; this retro only records that the interruption *coincided* with OP-1095's self-rescue.

## Filed-as

- `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md` (this file)
- Companion META JIRA ticket: *to be opened by sora* (label `meta:retrospective` + `meta:incident`)

## Chain (2026-05-14 documents)

This incident triggered a 3-document chain captured the same day. Read in order to follow the depth progression:

1. **This document** — incident event, recovery actions, immediate ⑤–⑨ gap analysis
2. **`docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md`** — tactical patch: S12.G G.A-v2 Runtime Defense Contract spec (50 tickets, 5-dim defense framework, 7 Qs locked)
3. **`docs/architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md`** — strategic reframe: the deeper realization that runner-as-product was the strategic misjudgment; proposed 3-layer architecture (Platform / dev-runner / user-agent); multi-tenant constraint; Q1/Q2 still open

The chain emerged in that order because **each stage exposed a deeper layer of the same problem**. Future readers of this incident retro should know that the document chain — not just this file — is the complete record.
