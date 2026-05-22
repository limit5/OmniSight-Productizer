# OP-1608 — sora-bridge control-plane re-point off `main`@rc1 → `develop` — PLAN (for audit)

**Status**: DRAFT for codex audit (2026-05-22). Do not execute until audited + risk-assessed + dry-run passes.

## Problem
`/home/user/sora-bridge` (the control-plane checkout, separate from the dev work tree) is in **detached HEAD at `2be0600` = `[release-cut v0.5.0-rc1] Merge develop into main`** — **1 ahead / 79 behind `develop`**, 0 uncommitted. `scripts/sync_sora_bridge.sh` already targets `develop` (`BRIDGE_BRANCH=develop`) but updates via `git pull --ff-only`; a detached-rc1 HEAD cannot fast-forward to develop (the rc1 merge is not an ancestor of develop) → it has failed `pull_not_fast_forward` 260+ times. **Consequence**: every sora-bridge service runs rc1-era code, 79 commits stale.

## Why it matters (what's blocked)
- The **gerrit-jira-bridge heartbeat** (runner lifeline) + **pipeline-coordinator** + **merger-bot** + **auto-promote-main** all run from this stale checkout.
- The AUDIT-29f coordinator **acting-flip is blocked**: OP-1556 (idle-tick must NOT consult Tier-2 LLM) is merged on develop but NOT in the running coordinator → the live decision-log still shows `tier2_degraded_budget_cap` every tick (the pre-OP-1556 behavior, masked by the budget=0 safety guard). Acting mode is unsafe until the coordinator runs the OP-1556 code.
- It is the deployment-audit's 1 remaining honest fatal (`sora-bridge-sync`).

## Goal
sora-bridge checkout on `develop` (tracking `origin/develop`), all services running current develop code, `sync_sora_bridge.sh` self-healing (FF works going forward), **the gerrit-jira-bridge heartbeat never goes stale long enough for runners to meaningfully abstain**, and **no unintended JIRA mutation or release-cut** on the service restarts.

## Blast radius — ALL sora-bridge services jump rc1 → develop (79 commits)
| Service | On re-point + restart |
|---|---|
| `gerrit-jira-bridge` | runs develop bridge code; **brief heartbeat gap on restart** = the key risk |
| `pipeline-coordinator` (+ watchdog) | re-runs `startup()` with develop code (incl OP-1555 cold-start shadow-gate + OP-1556 idle-noop + OP-1557) — still `acting=False` (shadow) |
| merger-bot daemon | runs develop merger code (the improvements) |
| `auto-promote-main` | develop code + ADR-0040 main-retirement; cursor at log EOF (no replay) |
| timers (sora-bridge-sync, gitlab-cr-monitor, deployment-audit) | run develop versions |

## The change (one-time un-stick; the sync self-heals after)
1. `cd /home/user/sora-bridge && git fetch origin develop`
2. `git checkout -B develop origin/develop` — switch off detached-rc1 onto the `develop` branch at origin tip. Clean (0 uncommitted). The rc1 commit (`2be0600`) is preserved (remote `main` + reflog).
3. Restart the services to load develop code, **bridge first + fastest** to minimize the heartbeat gap; then coordinator, merger, auto-promote.
4. `sync_sora_bridge.sh` (every 5 min) now FFs cleanly going forward → self-heals.

## Risks + mitigations
- **R1 — heartbeat gap on bridge restart → runners abstain.** Bridge writes heartbeat each cycle; a restart leaves a gap until the new process writes its first one. Runner stale-threshold default 60s (`heartbeat_stale_after_seconds`). Mitigate: fast `systemctl --user restart gerrit-jira-bridge`; verify heartbeat freshness recovers within a few seconds (<< 60s) so runners never see `bridge_down`; runners only PAUSE pickups, never break. Measure the actual gap in the dry-run.
- **R2 — coordinator boot mutates live JIRA → rescue-races runners.** Mitigate: OP-1555 cold-start shadow-gate (acting=False → ShadowColdStartGateway = record-only, 0 mutator calls). VERIFY zero gateway mutations on boot. The 10-stale-ticket cold-start signal (incl real release tickets OP-925/926/927) is RECORDED, not executed.
- **R3 — auto-promote-main on develop code → unintended release-cut.** Mitigate: cursor is at log EOF (no replay of the 261 historical events); develop + ADR-0040. VERIFY the cursor is intact + no `milestone_ready` replay before/after restart. Consider NOT restarting auto-promote-main in the same step (it's not blocking the coordinator).
- **R4 — config drift rc1→develop.** The develop bridge/coordinator may require new env the rc1 setup lacks. Mitigate: diff required env between rc1 and develop for `run_gerrit_jira_bridge.py` + `pipeline_coordinator`; confirm the existing drop-ins (`heartbeat-path.conf`, coordinator `env.conf` with budget=0 + PATH) still apply. ⚠ Per the activation memory: when OP-1556 is live, REMOVE `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=0` (restore a real budget) — but NOT in this step (that belongs to the acting-flip, not the re-point; keep budget=0 through the re-point so a still-shadow coordinator can't burn cost).
- **R5 — the 79-commit jump changes behavior.** This is the INTENDED state (current code; the merger/bridge improvements are wanted). Mitigate: verify each service comes up healthy + behaves; watch the decision-log + bridge logs for new errors.
- **R6 — rollback.** If anything breaks: `cd /home/user/sora-bridge && git reset --hard 2be0600 && <restart services>` → back to rc1. The rc1 sha is preserved.

## Dry-run (before any real change)
1. `SYNC_DRY_RUN=1 bash scripts/sync_sora_bridge.sh` → confirm it WANTS to FF to develop + logs the divergence (no pull/restart).
2. **Heartbeat-gap rehearsal**: measure how long the bridge takes from `restart` to first heartbeat write (the gap), confirm << 60s. Watch a runner log for `bridge_down`.
3. Confirm the develop coordinator `startup()` is shadow-gated (OP-1555) — read the develop code path, NOT trust the rc1 behavior.
4. Verify the auto-promote-main cursor is at EOF (no pending replay).

## Acceptance
- sora-bridge on `develop` @ origin/develop tip (`git rev-parse HEAD == origin/develop`); `sync_sora-bridge.service` Result=success on next tick.
- Bridge heartbeat fresh throughout (no sustained `bridge_down`; ≤ a few-second restart blip).
- Coordinator running develop code: decision-log shows OP-1556 **idle-noop** (NOT `tier2_degraded_budget_cap` every tick), still `acting=False`.
- `scripts/deployment-audit.sh` → `sora-bridge-sync` row green; 0 fatal.
- No unintended JIRA mutation, no release-cut.
- Acting-flip remains a SEPARATE later step (review the 10-ticket shadow signal + remove budget=0 first).

---

## v2 — codex-audit revisions + preflight results (2026-05-22)
Codex audit: `docs/audit/codex-reviews/op-1608-sora-bridge-repoint-codex-audit-2026-05-22.txt` (2 BLOCKER, 4 SHOULD-FIX, 2 NICE). Both BLOCKERs preflighted → already mitigated in the live state.

### BLOCKER resolutions (preflighted)
- **B1 (bridge needs DB DSN before heartbeat → restart-loop wedge):** ✅ MITIGATED — the LIVE bridge unit's effective env ALREADY has `OMNISIGHT_DATABASE_URL=postgresql://omnisight:…@127.0.0.1:5432/omnisight` (in `Environment=` + confirmed in the running process). The develop bridge's `BridgeFatalError "requires a Postgres DSN"` (gerrit_jira_bridge.py:2423) is satisfied. **GATE: re-confirm `systemctl --user show gerrit-jira-bridge -p Environment | grep DATABASE_URL` is non-empty immediately before the restart.**
- **B2 (don't restart auto-promote — oneshot wrapper ignores the cursor, scans whole log, can push refs/for/main + leave detached):** ✅ HANDLED — `auto-promote-develop` + `auto-promote-main` are BOTH `inactive`+`disabled` (frozen by RT-01). **The re-point restarts ONLY `gerrit-jira-bridge` + `pipeline-coordinator`. Do NOT touch auto-promote.**

### Revised change steps
0. **Preflight (gates):** bridge env has DATABASE_URL (B1); auto-promote stopped+disabled (B2); `git -C ~/sora-bridge status --short` clean; `SYNC_DRY_RUN=1 bash scripts/sync_sora_bridge.sh` confirms it wants develop.
1. `cd /home/user/sora-bridge && git fetch origin develop` → verify `git rev-parse origin/develop` == dev tip; `git branch -vv`.
2. **Pause the coordinator watchdog** (`systemctl --user stop pipeline-coordinator-watchdog.service`) so a slow first develop boot isn't killed at the 90s heartbeat threshold (SHOULD-FIX 1: coordinator writes its heartbeat only in run_once(), AFTER startup() runs deployment-audit + JIRA/Gerrit probes).
3. `git checkout -B develop origin/develop` (off detached-rc1; clean; rc1 `2be0600` preserved via remote main + reflog).
4. `systemctl --user restart gerrit-jira-bridge.service` (fast) → **immediately verify heartbeat freshens within seconds** (threshold is 300s — NICE 1, gerrit_jira_bridge.py:192 — so a few-second blip is harmless; runners won't `bridge_down`).
5. `systemctl --user restart pipeline-coordinator.service` → verify it writes its heartbeat + decision-log within 90s; **grep the decision-log/logs for `cold-start SHADOW` + ZERO JIRA write/mutator calls** (SHOULD-FIX 2 — acting=False + ShadowColdStartGateway record-only, but VERIFY not assume); confirm idle ticks now show OP-1556 **noop** (not `tier2_degraded_budget_cap`).
6. Re-start the watchdog (`systemctl --user start pipeline-coordinator-watchdog.service`).
7. Keep `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=0` (still shadow — the budget-restore belongs to the acting-flip, NOT this re-point).

### Self-heal limitation (SHOULD-FIX 3 — accept + follow-up)
`sync_sora_bridge.sh` only bounces `gerrit-jira-bridge` on a FF (it declares 5 bridge files). So future develop updates to coordinator/merger/release scripts update the CHECKOUT but DON'T reload those services. The one-time re-point manually restarts the coordinator; **going forward, coordinator/merger need a manual restart on relevant develop changes** (or extend the sync — follow-up ticket).

### Robust rollback (SHOULD-FIX 4)
`cd /home/user/sora-bridge && git merge --abort 2>/dev/null || true; git status --short; git checkout -B develop 2be0600 || git reset --hard 2be0600; systemctl --user restart gerrit-jira-bridge pipeline-coordinator` → back to rc1. (Auto-promote stays stopped, so the detached-merge risk it poses doesn't apply here.)

### Dry-run gate (before step 3 onward)
1. `SYNC_DRY_RUN=1 bash scripts/sync_sora_bridge.sh` → wants develop, no pull/restart.
2. Re-confirm B1 (bridge DATABASE_URL present) + B2 (auto-promote stopped).
3. Decide: the bridge-restart heartbeat blip is the only runner-facing moment — threshold 300s makes it safe; verify the bridge restarts in << that.

### Final risk verdict
All foreseeable risks assessed. 2 BLOCKERs preflighted-clear; the residual is a few-second bridge-heartbeat blip on restart (safe vs the 300s threshold) + a verify-the-shadow-logs step. Acting-flip stays a SEPARATE later step (review the 10-ticket shadow signal + restore budget first).
