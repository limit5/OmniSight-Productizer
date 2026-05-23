# Item-3 SLO-monitor + auto-rollback — DEEP AUDIT (implemented vs current architecture)

**Status**: DRAFT deep-audit (2026-05-23) for codex review. Verifies what OP-772/OP-883
(SLO monitor + auto-rollback on health degradation, Boreas-B item 3) actually built vs
the CURRENT prod architecture (release-train image-tag-only / GitLab CR / OP-1515
OMNISIGHT_REGISTRY abstraction). Triggered by finding item-3 is shipped-but-not-deployed.
Code = shipped `~/sora-bridge` (develop).

## Implemented inventory
- `backend/slo_monitor.py` (421 ln, **OP-772**) — the module the systemd unit runs
  (`python -m backend.slo_monitor`). Prometheus metric source → ConsecutiveBreachDetector
  → ComposeRollbackExecutor (set `OMNISIGHT_IMAGE_TAG=previous_tag`, `docker compose -f
  docker-compose.prod.yml pull/up backend-a backend-b frontend`).
- `backend/orchestrator/slo_monitor.py` (756 ln, **OP-883 D11**) — a SECOND, richer
  monitor: D10-canary-vs-D9-full rollback decision, AC#4 10-min cooldown, **refuses to
  auto-rollback when the measurement plane is dark** (MetricSourceUnavailable), operator
  manual `maybe_rollback`.
- `backend/canary_rollout.py` + `backend/routers/canary_rollout.py` (wired into main.py).
- `deploy/systemd/omnisight-slo-monitor.service` (unit file, in repo).
- `configs/prometheus.yml`, `config/slos.yaml`.

## ✅ Alignments with current architecture (less drift than feared)
| Aspect | Status |
|---|---|
| Compose services `backend-a`/`backend-b`/`frontend` | ✅ match `docker-compose.prod.yml` (:148/:261/:413) |
| Image model `${OMNISIGHT_REGISTRY}/backend:${OMNISIGHT_IMAGE_TAG}` | ✅ rollback sets OMNISIGHT_IMAGE_TAG — consistent with OP-1515/OP-1478 |
| `deploy-prod.sh` writes `OMNISIGHT_PREVIOUS_IMAGE_TAG` + restarts the unit | ✅ (:278-291; warns if unit not installed) |
| `.venv` for the unit's ExecStart | ✅ exists |
| Prometheus in prod compose | ✅ (10 refs) + scrape job `omnisight-backend` |
| Notification path | ✅ `backend.notifications.notify` (warning + P1 critical on rollback) |

## 🔴🟡 Gaps / drift (what needs fixing)

**G1 (BLOCKER) — NOT DEPLOYED.** `omnisight-slo-monitor.service` = `enabled=not-found` /
`inactive` on the host; `omnisight-canary*.service` likewise. NOT in
`scripts/deployment-audit.sh` manifest → the gap is invisible to the daily audit. No
live auto-rollback exists today.

**G2 (BLOCKER) — METRIC CONTRACT MISMATCH → silent always-healthy.** Both monitors query
`http_requests_total{route=...}` + `http_request_duration_seconds_bucket` (slo_monitor.py
`_fetch_route`). But the app exports **domain** metrics only (`decision_total`,
`pipeline_step_seconds`, `provider_*`, … in `backend/metrics.py`, served at
`/api/v1/metrics`) — there is **no instrumentator and no `http_requests_total`
definition anywhere** (only the 2 monitors + staging_validation *query* it). So every
PromQL returns empty → `total_value <= 0` → `_fetch_route` returns
`RouteWindow(error_rate=0, success_rate=1.0)` = **healthy** → no breach ever → **no
rollback ever.** Even deployed, it is a dead safety net (worse: false confidence).

**G3 (SHOULD-FIX) — scrape-target drift.** `configs/prometheus.yml` scrapes
`targets: ["backend:8000"]`, but prod runs `backend-a` + `backend-b` (no `backend`
service). Even after G2, the scrape may hit nothing.

**G4 (SHOULD-FIX) — DUPLICATE implementations; the unit runs the weaker one.** The
systemd unit runs OP-772 `backend/slo_monitor.py` (compose full-rollback only; **no
cooldown, no measurement-plane-dark guard**). The richer OP-883
`backend/orchestrator/slo_monitor.py` (cooldown + **refuse-rollback-when-blind** +
canary/full decision) is NOT wired to the unit. Critically, OP-883's
refuse-when-blind guard is exactly what makes G2 safe-ish (a blind monitor that
refuses to act beats one that reads-healthy). Decide the canonical impl + wire it.

**G5 (SHOULD-FIX) — rollback-target coupling to deploy-prod.sh.** `previous_tag` =
`OMNISIGHT_PREVIOUS_IMAGE_TAG`, written ONLY by `deploy-prod.sh` (:281). The release-train
handoff step 7 says "deploy prod by the existing prod SOP" — if prod is deployed via the
manual SOP / release-train promote rather than `deploy-prod.sh`, no previous_tag is
written → rollback has no target → no-op. Verify the prod deploy path.

**G6 (NICE) — canary branch is dead weight.** OP-883's canary-vs-full decision keys on
`canary_state.json` + the D9/D10 canary orchestrator, but progressive prod canary
(OP-931/932/933) was HOLD'd/Archived → canary_state.json never exists → always full
rollback. The canary code path is unreachable in the current single-env topology.

**G7 (NICE) — SLO threshold spec inconsistency.** OP-772 (error<0.5% / success>99.5% /
p95<500ms) vs OP-883 (error<1% / p95<500ms). `config/slos.yaml` is the source of truth —
reconcile + confirm which thresholds are live.

## Recommended fix scope (ordered; activation BLOCKED until G2 fixed)
1. **G2 (must, first):** make the HTTP-metric contract real — add an ASGI Prometheus
   instrumentator exporting `http_requests_total{route,status}` +
   `http_request_duration_seconds_bucket{route,le}` at `/api/v1/metrics`, OR rewrite the
   monitor's PromQL to existing domain metrics. (Instrumentator is the right fix — these
   are standard RED metrics the SLO spec assumes.)
2. **G4:** pick the OP-883 orchestrator monitor as canonical (it has the
   refuse-when-blind + cooldown safety); point the systemd unit at it; retire/merge the
   OP-772 duplicate. The refuse-when-blind guard is a hard requirement (don't auto-roll
   on a blind read).
3. **G3:** fix the scrape target to backend-a + backend-b (or a shared alias).
4. **G5:** ensure the live prod deploy path writes `OMNISIGHT_PREVIOUS_IMAGE_TAG`
   (deploy-prod.sh, or wire the release-train promote/deploy to set it).
5. **G1 (last):** install + enable the unit, add it (+ watchdog-style liveness) to the
   deployment-audit manifest, then exercise once (force a synthetic breach in staging).
6. G6/G7: drop/guard the dead canary branch; reconcile slos.yaml thresholds.

## ✅ codex verdict: audit-needs-revision — core risk CONFIRMED real (2026-05-23)
`slo-monitor-autorollback-codex-audit-2026-05-23.txt`. Corrections folded in:
- **G2 CONFIRM + sharpened**: silent-dead confirmed for BOTH monitors (slo_monitor.py
  :131/:140/:154; orchestrator :588/:595/:609). app exports its own registry at
  /api/v1/metrics (routers/observability.py:57, metrics.py:962); no `http_requests_total`
  exporter; requirements have `prometheus-client`, NOT an ASGI instrumentator
  (requirements.in:88). **CRITICAL: OP-883's refuse-when-blind only catches Prometheus
  CONNECTION failure, NOT reachable-Prometheus-with-missing-series — an empty series
  still reads healthy.** So the metric-contract fix is non-negotiable; refuse-when-blind
  does NOT save us.
- **G8 (NEW BLOCKER — I missed it): systemd unit path is STALE.** The unit's
  WorkingDirectory + venv ExecStart point to `%h/work/sora/OmniSight-Productizer`
  (the operator's main workdir, ~76 commits behind), NOT `~/sora-bridge` (the live
  develop checkout the coordinator runs from). Installed as-is it would run STALE
  slo_monitor code. Must repoint to ~/sora-bridge + its venv.
- **G4 CONFIRM**: OP-883 canonical (docs/operations/slo-runbook.md:15 calls it the
  always-on monitor; no reason the unit runs OP-772 :17).
- **G5 PARTIAL (Q3 answered)**: current prod SOP IS `scripts/deploy-prod.sh`
  (docs/sop/deploy-prod-runbook.md:3) → writes OMNISIGHT_PREVIOUS_IMAGE_TAG (:281) +
  restarts the unit (:287). Gap only if operators deploy manually after promote.
- **G3 DOWNGRADE to PARTIAL**: backend-a has a legacy `backend` alias
  (docker-compose.prod.yml:195/:215) → `backend:8000` resolves (not a total miss);
  it's **replica-coverage drift** (only backend-a scraped, backend-b missed).
- **G7 CONFIRM**: two config files — `config/slos.yaml` (0.5%) vs
  `config/slo_thresholds.yaml` (1%); intentional per comments but reconcile to one
  source after the canonical pick.
- **G6**: canary code still wired (main.py:1552); the HOLD is JIRA-side (OP-931/932/933
  Archived/HOLD — verified separately, not provable from code alone).

**codex-endorsed fix order (minimal-correct scope):** one canonical monitor + one metric
contract + one shipped-code unit + both replicas scraped + rollback target guaranteed:
1. **G2** real HTTP RED metrics (ASGI instrumentator → `http_requests_total{route,status}`
   + `http_request_duration_seconds_bucket`) OR rewrite monitors to shipped metrics.
2. **G4** OP-883 canonical; wire the unit to `backend.orchestrator.slo_monitor`.
3. **G8** repoint the unit WorkingDirectory + venv to ~/sora-bridge.
4. **G3** scrape both backend-a + backend-b.
5. **G5** confirm prod SOP writes the rollback target; then **G1** install/enable the
   unit + add it to the deployment-audit manifest + exercise once (synthetic breach).
6. G6/G7 cleanup: drop/guard the canary branch; reconcile the two slo config files.

## Open questions for codex
- Q1: is G2 right that nothing exports `http_requests_total` (confirm no instrumentator
  / no caddy/exporter providing it that Prometheus scrapes)? Is the always-healthy
  silent-failure analysis correct (total<=0 → healthy short-circuit, slo_monitor.py
  `_fetch_route`)?
- Q2: which monitor should be canonical — OP-772 (`backend/slo_monitor.py`) or OP-883
  (`backend/orchestrator/slo_monitor.py`)? Any reason the unit runs the OP-772 one?
- Q3: G5 — what is the ACTUAL current prod deploy path (deploy-prod.sh vs manual SOP vs
  release-train promote), and does it write OMNISIGHT_PREVIOUS_IMAGE_TAG?
- Q4: with single-env + canary HOLD'd, is full-rollback-only the right target (drop the
  canary branch), or should canary be un-HOLD'd as part of this?
- Q5: anything else mismatched (slos.yaml thresholds, the /api/v1/metrics path, the
  rollback's compose-file assumption) that this audit missed.
