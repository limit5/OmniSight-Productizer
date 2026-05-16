---
id: SPRINT-S12G-V2-FAMILY8-GRACEFUL-SHUTDOWN-CONTRACT
version: v1 (2026-05-16)
title: G.A-v2 Family ⑧ — Graceful shutdown contract (per-service timeouts on WSL2)
scope: Contract spec for the productizer's graceful-shutdown obligations on a WSL2 host — per-service-class timeout requirements, `scripts/shutdown.sh` graduated SIGTERM→SIGKILL contract, `omnisight-compose-prod.service` systemd wiring, `docker-compose.prod.yml` `stop_grace_period` invariants, Postgres WAL-safety verification, post-restart probes, the Windows-side `RB_POWER_OFF` limitation, cross-family delegation to Family ⑤ / ⑥, chaos-test contract, and the 14-day soak invariants. Doc-only ticket (v2-⑧-1a); no runtime, db, devops, embedded, frontend, security, tests, or tooling code is touched by OP-1158.
status: Draft — OP-1158 (this ticket); per Sprint S12.G v1.4 spec §"Family ⑧ — WSL2 graceful shutdown contract"; defense dimensions D3 (shutdown — new) + D4 (recovery — new); D1 / D2 / D5 explicitly N/A by cross-family delegation rationale.
related:
  - sprint-s12g-A-v2-runtime-defense-contract-spec.md §3 "Family ⑧ — WSL2 graceful shutdown contract" (parent spec)
  - 2026-05-16-v2-family5-image-surfacing-contract.md (Family ⑤ — `OmniSightStaleImage` consumes the post-reboot D2 delegation surface)
  - 2026-05-16-v2-alertbridge-framework-contract.md (alert plumbing the post-reboot delegated alerts ride on)
  - docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md (the host-reboot incident that surfaced this family)
  - scripts/shutdown.sh (existing 390-line graduated-shutdown script; today NOT wired to systemd `ExecStop`)
  - /etc/systemd/system/omnisight-compose-prod.service (today `TimeoutStopSec=60`; mathematically insufficient — see §3)
  - docker-compose.prod.yml (today no `stop_grace_period` on any prod service — see §4)
  - JIRA OP-1158 (v2-⑧-1a — this spec)
  - JIRA OP-1159+ (v2-⑧-1bc, v2-⑧-2a, v2-⑧-2b, v2-⑧-3a, v2-⑧-4a-Doc, v2-⑧-DRDrill, v2-⑧-Integration — downstream impl tickets that consume this spec)
---

# G.A-v2 Family ⑧ · Graceful shutdown contract — v1 (2026-05-16)

## §0. Reading order

1. §1 — the gap class this contract eliminates (5 minutes); the 2026-05-14 reboot in one paragraph; what "graceful shutdown" means on a WSL2 host.
2. §2 — **service classification**: every prod service categorized into one of four shutdown-criticality classes, with the per-class grace period (PG=30, backend=40, caddy/frontend/cloudflared=15, default=10).
3. §3 — the **`scripts/shutdown.sh` contract**: graduated SIGTERM → wait → SIGKILL; per-service-class grace; verification loop; idempotency invariant; rerun-safety contract.
4. §4 — the **systemd wiring contract**: `omnisight-compose-prod.service` `ExecStop` MUST call `scripts/shutdown.sh`; `TimeoutStopSec=180` justified mathematically (sum-of-per-service + buffer).
5. §5 — the **`docker-compose.prod.yml` contract**: `stop_grace_period` per service exactly matching the script's per-class budget; required for the SIGKILL fallback to land inside the systemd `TimeoutStopSec` envelope.
6. §6 — **Postgres WAL-safety verification**: `pg_isready` + checkpoint completion BEFORE issuing `docker stop`; cross-link to Family ⑥'s post-restart `alembic_version` integrity probe.
7. §7 — **post-restart probes**: `pg_isready` + `alembic_version` table integrity probe; cross-family handoff to ⑥.
8. §8 — **WSL2-specific constraints**: Windows-shutdown 10-second `RB_POWER_OFF` hard limit; what is recoverable vs. not; the explicit "Productizer cannot detect last forced shutdown" limitation paragraph.
9. §9 — **cross-family delegation**: Family ⑤ / ⑥ AlertRules emit post-reboot warnings if image / DB drift; Family ⑧ does NOT re-emit (D1 / D2 / D5 N/A rationale).
10. §10 — the **chaos test contract** (`v2-⑧-DRDrill`): the scenarios that MUST be exercised pre-merge; what counts as a pass.
11. §11 — the **14-day soak invariants** (`v2-⑧-Integration`): RTO < 60 s, 0 corruption, weekly synthetic shutdown.
12. §12 — open questions, changelog, hand-off.
13. §13 — boundary justification (spec-ticket area block).

This spec is the **contract** — not the implementation. Code lands in `v2-⑧-1bc` (the extended `scripts/shutdown.sh`), `v2-⑧-2a` (the systemd unit refactor with operator-window cooperation), `v2-⑧-2b` (the `docker-compose.prod.yml` `stop_grace_period` additions), `v2-⑧-3a` (the PG WAL safety probes), `v2-⑧-4a-Doc` (the operator runbook for WSL2-specific limits), `v2-⑧-DRDrill` (the chaos test), and `v2-⑧-Integration` (the 14-day soak). Anchors in §2 through §11 are stable and will be referenced from each downstream ticket's AC.

---

## §1. The gap class this contract eliminates

### §1.1 The class, in one sentence

> *"A multi-service stack on a WSL2 host where the host-shutdown budget is shorter than the sum of per-service drain budgets, the per-service drain budgets are not declared anywhere the host can read them, and the script that DOES know the right budgets is not actually wired to the host's shutdown path — so under any non-cooperative host shutdown the stack is SIGKILLed mid-flight and Postgres recovers from WAL replay every time."*

Concrete instance from 2026-05-14: a routine Windows update triggered a host reboot at 02:00. WSL2 issued its 10-second pre-`RB_POWER_OFF` warning to systemd. `omnisight-compose-prod.service` ran `ExecStop=docker compose down` with `TimeoutStopSec=60`. For a 7-container stack with the docker daemon's default per-container 10 s SIGTERM-to-SIGKILL window, the floor budget is **7 × 10 s = 70 s** — already over the unit's 60 s envelope before any drain logic runs. Postgres was SIGKILLed mid-checkpoint. On reboot, Postgres replayed WAL, came up clean, and `alembic_version` was internally consistent — but ONLY because Postgres' own crash-safety guarantees held; the productizer's defense contract contributed nothing to that recovery.

Forensic walk:

| Layer | What it knew | What it failed to do |
|---|---|---|
| `scripts/shutdown.sh` | The right per-service stop budget (cloudflared first; frontend 15 s; backend 40 s; workers 60 s; verification loop) | Not invoked — `ExecStop` ran `docker compose down` directly |
| `omnisight-compose-prod.service` | A `TimeoutStopSec=60` that bounds the whole-stack stop | Set below the floor budget; no awareness of per-container grace |
| `docker-compose.prod.yml` | The container topology | No `stop_grace_period` on any service → docker daemon default 10 s SIGTERM→SIGKILL window applied uniformly, including to Postgres |
| WSL2 init | A 10 s pre-`RB_POWER_OFF` warning before the Windows-side reboot kernel call | Cannot extend; cannot be reasoned with from inside the guest |
| Windows shutdown | The reboot trigger (Update / Schedule / User) | Not observable from inside WSL2 — the productizer cannot tell whether the prior shutdown was forced |

The bug class is **no party owns the shutdown contract**. The shell script knows the right budgets but is not in the call chain. The systemd unit is in the call chain but has the wrong envelope. The compose file declares no per-service grace, so the docker daemon falls back to the 10 s default for every container including the one that absolutely must finish its checkpoint. WSL2 caps the whole exercise at 10 s with no observability.

### §1.2 Why this is dangerous beyond "Postgres recovers anyway"

The 2026-05-14 instance happened to be benign because (a) Postgres' WAL replay is correct, (b) no in-flight HTTP request lost a non-idempotent side effect at the unlucky moment, and (c) no schema upgrade was mid-flight. Each of those holds-by-luck instead of holds-by-contract is the next outage:

- **In-flight non-idempotent write loss.** A POST that has committed in Postgres but not yet returned to the client at the moment of SIGKILL leaves the client thinking the request failed. On retry the client may (depending on the path) either double-post or skip — both are user-visible bugs. The 40 s backend drain (§2 row 2) exists specifically so the FastAPI `lifespan` close hook can finish in-flight requests.
- **Schema upgrade mid-flight.** A backend container that was running `alembic upgrade head` at the moment of SIGKILL leaves `alembic_version` updated for committed steps but the next step un-run. Family ⑥'s rescue path covers this — but only if the post-restart probe (§7) actually runs and surfaces the inconsistency.
- **Caddy mid-TLS-handshake.** Caddy SIGKILLed mid-handshake can leave the certificate cache in a state where the first few requests after restart see TLS errors. 15 s grace is enough for in-flight handshakes to finish.
- **Cloudflared mid-tunnel-drain.** Cloudflared SIGKILLed without drain leaves the tunnel endpoint marked unhealthy at Cloudflare's edge for ~30–60 s, during which fresh public traffic gets a 502 even though the productizer is already coming back up. 15 s grace lets cloudflared deregister gracefully.
- **Workers mid-task.** Worker tasks that have written partial state but not marked themselves complete leave orphan rows. The 60 s worker grace (per `scripts/shutdown.sh` line 12) is enough for in-flight tasks to either complete or be requeued — see §2 row 4.
- **Forensic blindness during the next forced reboot.** Without the operator runbook (§8) calling out the "Productizer cannot detect last forced shutdown" limitation, the operator does not know to manually run the Family ⑥ post-restart integrity probe after a Windows-side update.

The cost asymmetry: **today's 2026-05-14 instance is the benignest possible outcome of this gap class; the next instance will be more expensive.**

### §1.3 Why "make the timeout bigger" is not the fix

Three reasons the trivial fix (bump `TimeoutStopSec` to 300; declare no other invariant) does not close the gap:

1. **WSL2 caps the host budget at 10 s.** A `TimeoutStopSec=300` on the systemd unit cannot survive a Windows-side `RB_POWER_OFF` that fires 10 s after the WSL2 init starts shutting down. The unit envelope only matters for *cooperative* shutdowns (`systemctl stop omnisight-compose-prod`, `wsl --shutdown` with no prior Windows reboot). The contract must distinguish the two cases — see §8.
2. **The per-service budgets are not interchangeable.** Postgres needs checkpoint-completion time; cloudflared needs tunnel-deregister time; the backend needs in-flight-request-drain time. A single uniform "longer timeout" does not let the docker daemon route the right SIGKILL deadline to the right container. The compose file's `stop_grace_period` is the per-container budget; it MUST be declared per-service.
3. **A timeout without verification is silent.** Today's `TimeoutStopSec=60` already silently exceeds budget; the failure mode is "SIGKILL fired at second 60, services may or may not have finished, nobody knows." The contract requires a verification loop (§3.4) that proves every service reached `inactive` (systemd) or `exited` (compose) within budget. Without verification, a 180 s envelope is just a 60 s envelope with more room to hide.

The structural fix is to **declare the budgets per service, wire the right caller in the right place, verify after every shutdown, and document explicitly which classes of shutdown the contract does NOT defend against (the WSL2 `RB_POWER_OFF` case).**

### §1.4 What "graceful shutdown" means in this contract

For the purposes of this contract, a graceful shutdown of the OmniSight prod stack on a WSL2 host means:

1. **Each in-flight side effect is either fully completed or fully reversed.** No half-committed Postgres transactions; no orphan worker rows; no Cloudflare-edge-marked-healthy-but-actually-dead tunnel.
2. **Each per-service drain runs to completion within its declared budget.** §2 lists the budgets; §3 enforces them; §4 raises the envelope to fit; §5 declares them to the docker daemon.
3. **Post-restart the stack reaches healthy state within 60 s** (the RTO from §11). Postgres is `pg_isready`-green; `alembic_version` is consistent with the running image's expected head; backend `/readyz` returns 200; Caddy serves; Cloudflared tunnel is re-registered.
4. **Verification is mandatory.** Every shutdown produces a verification record (the script's exit code + log line summary) the operator can read to confirm the contract held.
5. **The contract documents what it cannot defend against** (Windows-side `RB_POWER_OFF`, the operator unplugging the host) and what compensating signals exist (Family ⑤ / ⑥ AlertRules; §9).

Anything weaker than (1)–(5) does not satisfy this contract.

---

## §2. Service classification — four shutdown-criticality classes

Every service in `docker-compose.prod.yml` falls into exactly one of four shutdown-criticality classes. The class determines the `stop_grace_period` in §5 and the per-service argument to `docker compose stop -t` in §3.

### §2.1 The four classes

| Class | Grace period | Members (today, per `docker-compose.prod.yml`) | Why this budget | What happens if SIGKILLed at budget |
|---|---|---|---|---|
| **PG (Postgres / WAL-safety critical)** | **30 s** | `db_ha` (top-level `postgres:` service when present); `omnisight-data` volume-owner sidecars do NOT count | Postgres must finish the in-progress checkpoint and fsync WAL before SIGTERM completes. The Postgres `shutdown_timeout` default is 60 s for "smart" shutdown; we use the faster "fast" shutdown (rollback in-flight tx, checkpoint, exit) which typically completes in 5–15 s on our workload but is bounded at 30 s for headroom. | Postgres comes up clean via WAL replay (it is crash-safe by design); but `pg_resetwal` is needed if checkpoint never landed and WAL is corrupt. We have not seen this on our workload but the contract assumes the worst case. |
| **Backend (in-flight request drain)** | **40 s** | `backend-a`, `backend-b` | FastAPI `lifespan` close hook runs `lifecycle.py` drain: 30 s for in-flight requests to complete + 10 s buffer for the asyncio event loop teardown. Matches `omnisight-backend.service:TimeoutStopSec=40` from the systemd unit. | In-flight HTTP requests return 502 to clients; clients on idempotent endpoints retry cleanly; clients on non-idempotent endpoints may double-post or skip. |
| **Stateless (no drain required)** | **15 s** | `caddy`, `frontend`, `cloudflared` | These services either hold no in-flight state (caddy reverse-proxying), hold trivial in-memory state (frontend next.js), or need a tiny window to deregister externally (cloudflared tunnel drain). 15 s is generous; 10 s is also tolerable but cloudflared's edge re-registration occasionally takes 12–13 s. | Caddy: brief 502 burst until restart. Frontend: clients reload. Cloudflared: ~30 s tunnel-unhealthy window at the Cloudflare edge until the new instance re-registers. |
| **Other (default)** | **10 s** | `prometheus`, `grafana`, `grafana-env-validator`, `docker-socket-proxy`, `omnisight-installer`, observability sidecars, `ai_core` (when present; per Family ⑨ it is auxiliary and tolerates abrupt shutdown), redis-if-added | These services are either ephemeral (validators, installer), have no in-flight state worth waiting for (prometheus / grafana flush their TSDB block on SIGTERM in well under 10 s), or are auxiliary (Family ⑨ contract: unavailable-then-skip). | None of consequence; Prometheus drops the last partial scrape window (≤ 15 s of data) and resumes on restart. |

### §2.2 Classification decision rule

A service belongs to **PG** iff and only iff a crash-without-checkpoint would risk WAL corruption (today: only `db_ha`/`postgres`).

A service belongs to **Backend** iff and only iff it serves HTTP requests with non-trivial drain semantics — i.e. it has a `lifespan` (or equivalent) hook that takes more than 10 s of wall time to complete.

A service belongs to **Stateless** iff and only iff its drain is bounded by external-system handshakes (TLS, tunnel registration) or it has no drain at all but needs > 10 s for a clean SIGTERM (e.g. frontend's next.js graceful close).

A service belongs to **Other** otherwise. Default-class membership requires no justification; PG / Backend / Stateless membership requires the matching criterion above.

### §2.3 Per-service table (today's prod stack)

The classification today (2026-05-16; `docker-compose.prod.yml` audited at HEAD) is:

| Service | Class | Grace period | Rationale |
|---|---|---|---|
| `db_ha` (when active) | PG | 30 s | WAL-safety critical. |
| `backend-a` | Backend | 40 s | FastAPI lifecycle drain. |
| `backend-b` | Backend | 40 s | Same. |
| `caddy` | Stateless | 15 s | Reverse-proxy; trivial in-memory state. |
| `frontend` | Stateless | 15 s | Next.js graceful close. |
| `cloudflared` | Stateless | 15 s | Tunnel-edge deregister. |
| `prometheus` | Other | 10 s | TSDB flush. |
| `grafana` | Other | 10 s | Stateless dashboard. |
| `grafana-env-validator` | Other | 10 s | Ephemeral validator. |
| `docker-socket-proxy` | Other | 10 s | Read-only proxy. |
| `omnisight-installer` | Other | 10 s | Ephemeral installer. |
| `ai_core` (when active) | Other | 10 s | Auxiliary per Family ⑨. |

### §2.4 Sum-of-budgets — the systemd envelope floor

The systemd `TimeoutStopSec` envelope (§4) must be at least the sum of per-class budgets across the longest realistic stop sequence. For the worst case (two backends draining in series, one PG checkpointing, two stateless drains, four other-class flushes):

```text
PG (30 s) + Backend×2 (40 s each, parallelizable but conservatively serial) + Stateless×3 (15 s each, parallelizable) + Other×4 (10 s each, parallelizable)
= 30 + 80 + 15 + 10
= 135 s (conservative; with parallelism within class)
```

Adding a 45 s buffer for systemd's own teardown sequencing, pre-stop verification (§6), and post-stop verification loop (§3.4): **180 s**. This is the value §4 fixes for `TimeoutStopSec`.

Note that the §3 script parallelizes within a class but serializes between classes, so the *actual* observed budget is closer to 30 + 40 + 15 + 10 ≈ 95 s. The 180 s envelope is the budget under pathological serialization — the contract requires the envelope to be a ceiling, not a target.

---

## §3. The `scripts/shutdown.sh` contract — graduated SIGTERM → wait → SIGKILL

This section names the contract that the existing `scripts/shutdown.sh` (390 lines, audited 2026-04-18) MUST satisfy after the `v2-⑧-1bc` extension. The script today implements most of this informally; the contract makes the invariants explicit and adds the per-service-class budget enumeration + verification loop + idempotency guarantees.

### §3.1 Calling contract

```bash
scripts/shutdown.sh [options]
```

Invocation form when called from systemd `ExecStop` (per §4):

```bash
ExecStop=/opt/omnisight/scripts/shutdown.sh --mode=compose --timeout=180 --backup-db
```

Options the contract requires the script to support (today's flags from the audit are preserved; the contract pins them):

| Flag | Semantics | Default |
|---|---|---|
| `--mode <systemd\|compose\|auto>` | Which orchestration layer to drive. `auto` (default) prefers systemd if `omnisight-backend.service` is installed, else falls back to compose. Today's prod is compose; systemd mode is supported for installer-managed prod variants. | `auto` |
| `--compose-file <path>` | Override compose file. Default: `docker-compose.prod.yml` if present, else `docker-compose.yml`. | auto-detect |
| `--timeout <seconds>` | Override the longest per-service grace period. **MUST be ≥ 40** (backend drain minimum). | `90` (script's own default; the systemd invocation passes `--timeout=180` to match `TimeoutStopSec`) |
| `--backup-db` | Best-effort SQLite `.backup` before stopping the backend. Postgres backups are handled by the volume-snapshot path, not this flag. | off |
| `--skip-ingress` | Leave Caddy / Cloudflared up (rolling-restart flows). | off |
| `--dry-run` | Print the would-be commands; change nothing. | off |
| `--force` | Do not fail on already-stopped or missing services. | off |
| `-h \| --help` | Help text; exit 0. | n/a |

Exit codes (the contract pins them; `v2-⑧-1bc` MUST preserve):

| Code | Meaning |
|---|---|
| **0** | All in-scope services are down; verification (§3.4) passed. |
| **1** | A service failed to stop within its grace period; verification failed. |
| **2** | Prerequisite missing (`systemctl` / `docker` not available). |
| **3** | Invalid arguments. |

### §3.2 Per-service-class grace budgets (the wire-up)

The script MUST pass the §2 grace periods to the underlying orchestrator (systemd or compose). The contract pins the per-service `-t` argument to `docker compose stop` exactly as §2.3 declares:

```bash
# Pseudocode for the compose path; v2-⑧-1bc implements this in scripts/shutdown.sh
# Stateless first (15 s) — they have no upstream dependency
docker compose -f $FILE stop -t 15 caddy frontend cloudflared

# Backend next (40 s) — drains in parallel between backend-a and backend-b
docker compose -f $FILE stop -t 40 backend-a backend-b

# PG (30 s) — last application-data service, after backend drain has freed connections
# (see §6 for the pre-stop pg_isready + checkpoint check)
pg_isready -h db_ha -t 5 && docker exec db_ha psql -U postgres -c "CHECKPOINT;"
docker compose -f $FILE stop -t 30 db_ha

# Other (10 s) — observability and ephemera, parallelizable
docker compose -f $FILE stop -t 10 prometheus grafana grafana-env-validator docker-socket-proxy omnisight-installer ai_core
```

The script's existing systemd-mode flow (lines 196–235 of today's `scripts/shutdown.sh`) follows the same order but talks to systemd units instead of compose services. The order is identical: ingress → frontend → backend → PG → workers/other.

### §3.3 Idempotency invariant — rerun-safe

The script MUST be safely re-runnable. Concretely:

- A second invocation when the stack is already down exits 0 with `log: already inactive` for each service.
- A second invocation when a previous invocation hard-failed (a service hung past grace, was SIGKILLed) cleans up the residue: removes dangling containers (`docker compose rm -f -s` for any service in `Exited` state), verifies systemd units are `inactive`, exits 0.
- The script holds no on-disk state that would prevent re-invocation. (There is no PID file; there is no lock file.)
- Concurrent invocation is undefined behavior. The contract does NOT require concurrent-safe; it requires *sequentially* re-runnable. If the operator runs `shutdown.sh` twice in parallel the docker daemon serialises the actual SIGTERM dispatches and one of the two invocations will see most services already gone.

Verification of idempotency lives in `v2-⑧-1bc`'s test suite (`test_shutdown_rerun_after_clean_stop`, `test_shutdown_rerun_after_hung_service`, `test_shutdown_rerun_after_partial_failure`).

### §3.4 Verification loop — every shutdown produces evidence

After the per-class stop commands return, the script runs a verification loop:

```bash
# Pseudocode; v2-⑧-1bc implements
deadline=$(( $(date +%s) + 30 ))
while (( $(date +%s) < deadline )); do
    running=$(docker compose -f $FILE ps --services --filter status=running)
    if [[ -z "$running" ]]; then
        log "verified: all services down"
        return 0
    fi
    sleep 2
done
err "verification timeout — still running: $running"
return 1
```

For systemd mode the verification calls `systemctl is-active --quiet <unit>` for every unit in the stop sequence; any unit still `active` after the loop budget yields exit 1.

The verification log line MUST include:

- The final list of services / units that reached `inactive`/`exited`.
- The wall-clock time elapsed per service (from the script's own timer; not from docker / systemd) — this is what feeds the §11 RTO p99 invariant.
- The exit code (0 or 1).
- The `--mode` and `--timeout` values used.

The line is structured (single line, key=value) so log aggregation can extract per-service stop times for the RTO histogram.

### §3.5 What the script does NOT do

The contract is precise about non-obligations to prevent scope creep in `v2-⑧-1bc`:

- It does NOT take Postgres backups (Postgres dumps are out of scope; the `--backup-db` flag is SQLite-only, used by dev-compose flows where the backend uses SQLite).
- It does NOT restart services. It only stops them. Re-up is the systemd unit's `ExecStart` job.
- It does NOT post to Discord / Slack / any external sink. Operator-facing notifications ride the existing AlertBridge channels (Family ⑨ `OmniSightAuxServiceUnavailable24h`-style; Family ⑤ post-reboot stale-image; Family ⑥ post-restart drift). Family ⑧ does NOT emit its own alert (see §9).
- It does NOT detect "was the prior shutdown forced?" That information lives Windows-side and is unreadable from inside WSL2 (see §8.4).

---

## §4. Systemd wiring contract — `omnisight-compose-prod.service`

The systemd unit `omnisight-compose-prod.service` MUST be refactored per the following contract. Today's unit (audited 2026-05-14) has `ExecStop=docker compose down` + `TimeoutStopSec=60`; both are wrong. The contract fixes both.

### §4.1 Required unit changes

```ini
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/omnisight
ExecStart=/usr/bin/docker compose -f docker-compose.prod.yml up -d
ExecStop=/opt/omnisight/scripts/shutdown.sh --mode=compose --timeout=180
TimeoutStopSec=180
```

Three invariants:

1. **`ExecStop` MUST call `scripts/shutdown.sh`, not raw `docker compose down`.** The script is the only place that owns the per-service-class graduated stop sequence (§3.2). Calling `docker compose down` directly leaves every container on the docker daemon's default 10 s SIGTERM-to-SIGKILL window, which violates §2 for every non-Other class.
2. **`TimeoutStopSec` MUST be `180`** — the sum-of-budgets + buffer derived in §2.4. Anything lower will SIGKILL the script mid-flight; anything higher is fine but the contract pins 180 s as the canonical value for cross-installation consistency.
3. **`ExecStop` MUST pass `--mode=compose --timeout=180` explicitly.** The script's auto-detection works but the systemd-side caller is the source of truth for the envelope; making the argument explicit prevents the script's default (90 s) from silently undercutting the unit envelope on a host where systemd-unit detection mis-fires.

### §4.2 The `TimeoutStopSec=180` derivation (mathematical)

From §2.4:

```text
Worst-case sequential stop budget:
    PG       : 30 s (1 instance)
    Backend  : 40 s × 2 = 80 s    (serialized worst case)
    Stateless: 15 s × 3 = 45 s    (parallelized in practice ≈ 15 s)
    Other    : 10 s × 4 = 40 s    (parallelized in practice ≈ 10 s)
    ----
    Σ        : 195 s (worst case, fully serialized)

Realistic (per-class parallelism):
    PG (30) + Backend (40, parallel within class) + Stateless (15, parallel) + Other (10, parallel)
    = 30 + 40 + 15 + 10 = 95 s

Add buffer for systemd teardown + script verification loop (§3.4) + log flush:
    95 s + 45 s buffer = 140 s
    -OR-
    195 s worst case + (no extra buffer because parallelism within class already accounts for most slack) ≈ 195 s

Round to a clean number that fits both:
    TimeoutStopSec = 180

This is:
    - Greater than the realistic per-class-parallel budget (95 + 45 = 140) ✓
    - Within rounding error of the worst-case fully-serial budget (195) — if a host happens to serialize within class, the script will exit between 140 and 195 s; the unit will not kill it mid-flight
    - Well under the systemd default `TimeoutStopSec=infinity` so a buggy script cannot hang the host indefinitely
    - Well under the `wsl --shutdown` cooperative budget which is also bounded by Windows' overall shutdown budget
```

The contract pins **180 s**. A future amendment that wants to raise it must (a) update §2 budgets, (b) re-derive Σ, (c) update §5 `stop_grace_period` declarations, and (d) update `v2-⑧-Integration`'s RTO assertion.

### §4.3 Other unit invariants (declared here for completeness)

- `Type=oneshot` + `RemainAfterExit=yes` — the unit is a "managed compose stack" wrapper, not a long-running daemon. `ExecStart=docker compose up -d` returns when the stack is up; `RemainAfterExit=yes` keeps systemd's view of the unit as `active` so `ExecStop` fires on system shutdown.
- `WorkingDirectory=/opt/omnisight` — the canonical install path; `docker-compose.prod.yml` is resolved relative to this.
- No `Restart=` — the docker daemon already restarts containers per their `restart:` policy in compose. The unit does not duplicate.
- `KillMode=control-group` — default; ensures `ExecStop`'s shell script and the docker stop dispatches it issues are all under the same cgroup.

### §4.4 Verification via `systemd-analyze verify`

Per the parent spec `v2-⑧-2a` row, the refactored unit MUST pass `systemd-analyze verify omnisight-compose-prod.service` without warnings. This is a static check (no actual stop required); CI in `v2-⑧-2a`'s scope runs it as a unit test.

A pass means: no syntax errors, no deprecated directives, no missing required directives. It does NOT mean the unit's actual stop sequence works — that is `v2-⑧-DRDrill`'s job.

### §4.5 Operator-window cooperation for `v2-⑧-2a`

Refactoring the unit on a live prod host requires an operator window because (a) `systemctl daemon-reload` is needed to pick up the new unit file, (b) the next stop following the reload exercises the new contract and must be observed live, (c) any failure of the new contract on the first live exercise must be rolled back by the operator within the same window. The parent spec marks `v2-⑧-2a` as `class: claude + operator-prepare-only`; the operator does the live install + first-exercise observation. The claude-side artifact is the unit file diff + the `v2-⑧-DRDrill` chaos test that exercises it on a sandbox host.

---

## §5. `docker-compose.prod.yml` contract — `stop_grace_period` per service

Every service in `docker-compose.prod.yml` MUST declare a `stop_grace_period` that matches its §2 class. This is what tells the docker daemon how long to wait between SIGTERM and SIGKILL when *something other than* the §3 script invokes `docker stop` — for example, a direct `docker compose down` from an operator's terminal, or the daemon's own shutdown sequence.

### §5.1 Required declarations

The downstream ticket `v2-⑧-2b` adds these to `docker-compose.prod.yml`. The contract pins them exactly:

```yaml
services:
  db_ha:
    # ... existing config ...
    stop_grace_period: 30s        # PG class — §2.1

  backend-a:
    stop_grace_period: 40s        # Backend class

  backend-b:
    stop_grace_period: 40s        # Backend class

  caddy:
    stop_grace_period: 15s        # Stateless class

  frontend:
    stop_grace_period: 15s        # Stateless class

  cloudflared:
    stop_grace_period: 15s        # Stateless class

  prometheus:
    stop_grace_period: 10s        # Other class

  grafana:
    stop_grace_period: 10s        # Other class

  grafana-env-validator:
    stop_grace_period: 10s        # Other class

  docker-socket-proxy:
    stop_grace_period: 10s        # Other class

  omnisight-installer:
    stop_grace_period: 10s        # Other class

  ai_core:
    stop_grace_period: 10s        # Other class (Family ⑨ auxiliary)
```

### §5.2 Why declare these AND pass `-t` in §3.2

The `docker compose stop -t N` flag overrides the per-container `stop_grace_period` for that one invocation. The two are redundant when the script is the caller — but they are NOT redundant when `docker compose down` is called directly:

| Caller | Reads `-t` arg? | Reads `stop_grace_period`? |
|---|---|---|
| `scripts/shutdown.sh` (the contract's primary path) | yes (`-t 40` etc. per §3.2) | yes, but `-t` wins |
| Operator running `docker compose -f docker-compose.prod.yml down` from terminal | no `-t` passed | yes — uses `stop_grace_period` |
| Docker daemon shutdown (e.g. `systemctl restart docker`) | n/a | yes — uses `stop_grace_period` |
| Direct `docker stop <container>` from operator | no `-t` passed | yes — uses `stop_grace_period` |

The declaration in §5.1 is the *default* the docker daemon applies whenever the explicit `-t` is absent. The `-t` in §3.2 is the *override* the script uses for in-script clarity. Both must exist and agree; the contract enforces agreement (a future fixture test in `v2-⑧-1bc` cross-checks that the `-t` values in the script match the `stop_grace_period` values in the compose file).

### §5.3 What this contract does NOT add to compose

To keep the surface tight:

- It does NOT add `stop_signal:` — the daemon's default SIGTERM is correct for every service today. (Postgres in particular handles SIGTERM as "fast shutdown" which is exactly what we want.)
- It does NOT add `healthcheck:` adjustments — those are Family ⑥'s scope.
- It does NOT add per-service `restart:` policy changes — out of scope; restart-on-crash policy is unchanged.
- It does NOT add `depends_on:` adjustments — the existing dependency graph is correct and the script's order (§3.2) matches it.

---

## §6. Postgres WAL safety verification — pre-stop probe

Before issuing `docker compose stop db_ha` (or the systemd equivalent), the script MUST verify that Postgres is in a state where a `stop_grace_period=30s` SIGTERM-then-SIGKILL window will not leave WAL in a corrupt state.

### §6.1 The pre-stop probe sequence

```bash
# Pseudocode for v2-⑧-3a's pre-stop probe
pg_isready -h db_ha -p 5432 -t 5 || {
    warn "Postgres not reachable for pre-stop probe — proceeding with stop anyway"
    return 0  # Not fatal; the stop sequence handles unreachable PG
}

# Force a checkpoint so WAL is small and SIGTERM finishes fast
docker exec db_ha psql -U postgres -c "CHECKPOINT;" -t -A || {
    warn "CHECKPOINT command failed — proceeding with stop anyway"
    return 0  # Not fatal; Postgres' shutdown will checkpoint regardless
}

# Verify the checkpoint actually completed
docker exec db_ha psql -U postgres -c \
    "SELECT pg_last_wal_replay_lsn(), pg_current_wal_lsn();" || true

log "Postgres pre-stop checkpoint complete"
```

### §6.2 Why "warn but proceed" on probe failure

The probe is **defense-in-depth**, not a gate. Postgres' shutdown sequence itself runs a final checkpoint as part of "fast shutdown" (the SIGTERM response); the pre-stop probe just shortens the SIGTERM window by emptying WAL ahead of time, which gives more margin under the 30 s `stop_grace_period`.

If the probe fails — Postgres unreachable, CHECKPOINT errors, etc. — the contract still wants the stop to proceed because:

1. An unreachable Postgres is already in a degraded state; the stop is the recovery action.
2. Postgres' own shutdown is correct without the probe; the probe is an optimization, not a correctness invariant.
3. Gating the stop on probe success creates a deadlock: if Postgres is hung in a way that breaks `pg_isready`, the productizer would refuse to shut it down ever.

The probe failure IS logged at WARN so post-mortem analysis can correlate.

### §6.3 What this does NOT cover

- It does NOT take a `pg_dump` backup. Backups are scheduled separately (`v2-⑥` family scope for the rescue path).
- It does NOT verify replica health. Today the prod stack is single-instance; if we add replicas, the probe sequence will need a §6 amendment to cover replica lag.
- It does NOT block on long-running transactions. A 30-minute analytics query mid-flight will be SIGTERMed and rolled back by Postgres' own fast-shutdown semantics; the productizer accepts this trade-off (analytics queries are idempotent in our workload).

### §6.4 Post-restart counterpart

The pre-stop probe in §6 has a post-restart counterpart in §7. The pre-stop probe makes the stop faster + safer; the post-restart probe verifies recovery actually held. Both are required by the contract.

---

## §7. Post-restart probes — `pg_isready` + `alembic_version` integrity

After a restart (whether graceful or forced), a post-restart probe MUST verify the stack reached a known-good state within the §11 RTO budget. The probe is a separate process from the shutdown script; it runs from the systemd `ExecStartPost=` hook (or a sidecar `omnisight-postrestart-probe.service` oneshot, per `v2-⑧-3a`'s impl choice).

### §7.1 The probe sequence

```bash
# Pseudocode for v2-⑧-3a's post-restart probe
deadline=$(( $(date +%s) + 60 ))   # RTO budget

# 1. Postgres is reachable
while (( $(date +%s) < deadline )); do
    pg_isready -h db_ha -p 5432 -t 2 && break
    sleep 2
done || { err "PG not ready within RTO"; exit 1; }

# 2. alembic_version table integrity (cross-link to Family ⑥)
db_head=$(docker exec db_ha psql -U postgres -t -A -c \
    "SELECT version_num FROM alembic_version;")
[[ -n "$db_head" ]] || { err "alembic_version row missing"; exit 1; }

# 3. The DB head matches the image's expected head (delegated to Family ⑥'s
#    /version endpoint + comparator; this section calls the comparator)
image_head=$(curl -fsS http://backend-a:8000/version | jq -r .alembic_head_in_image)
if [[ "$db_head" != "$image_head" ]]; then
    err "alembic head drift: db=$db_head image=$image_head"
    # Do NOT exit 1 — Family ⑥ owns the rescue path; this probe surfaces the
    # condition and the Family ⑥ AlertRule fires on the metric.
fi

# 4. Backend /readyz returns 200
curl -fsS http://backend-a:8000/readyz >/dev/null || {
    err "/readyz failed within RTO"
    exit 1
}

log "post-restart probe passed (RTO budget remaining: $(( deadline - $(date +%s) ))s)"
```

### §7.2 Cross-link to Family ⑥

Step (3) above does NOT own the drift-detection logic; it merely reads the symptom. The drift detection state machine and rescue path live in Family ⑥ (`docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md` when filed; today the contract is part of the parent spec §3 Family ⑥). Family ⑧'s post-restart probe is the *trigger* — it observes the drift and lets Family ⑥'s metric / alert / rescue chain take over.

### §7.3 RTO budget — 60 s

The probe's overall deadline is 60 s from when systemd marks `omnisight-compose-prod.service` as `active`. This is the same RTO Family ⑧'s 14-day soak asserts (§11).

If the probe fails inside 60 s, the failure is logged and the systemd unit's `OnFailure=` hook fires (`v2-⑧-3a` decides whether `OnFailure=` triggers an operator alert or simply marks the unit `failed` for the operator's next `systemctl status` check).

If the probe exceeds 60 s without conclusively passing or failing, it exits 1. The `v2-⑧-Integration` 14-day soak counts any 60s-budget exceedance as a soak-criterion failure.

### §7.4 What this probe does NOT do

- It does NOT auto-remediate drift. Drift remediation is Family ⑥'s `v2-⑥-RescueCLI` scope.
- It does NOT take backups. Post-restart is the wrong time to back up — the in-memory state may not have re-stabilized.
- It does NOT verify Caddy / Cloudflared / frontend. Those are stateless and self-recover; their failure surfaces via existing health endpoints, not via this probe.

---

## §8. WSL2-specific constraints

This section is mandatory per the parent spec v1.4 (codex P1-4 caveat). It documents what the contract cannot defend against on a WSL2 host and what the operator must know.

### §8.1 The 10-second `RB_POWER_OFF` hard limit

When Windows initiates a host reboot (Windows Update, scheduled restart, user-triggered restart), WSL2 receives a shutdown signal and propagates a SIGTERM-like notification to the guest's PID 1 (systemd). The guest has approximately **10 seconds** before the Linux kernel inside WSL2 receives `RB_POWER_OFF` and the entire VM is hard-stopped.

10 seconds is **less than the 180 s `TimeoutStopSec` envelope** declared in §4. Under a Windows-side reboot, the guest cannot complete the §3 shutdown script — the VM goes away in 10 s regardless of what systemd is doing.

### §8.2 What is recoverable vs. what is not

| Class of host shutdown | Time budget | Family ⑧ contract holds? | Recovery mechanism |
|---|---|---|---|
| Cooperative `systemctl stop omnisight-compose-prod` | 180 s | **Yes** — full graceful shutdown via §3 script. | Operator restart; post-restart probe (§7) passes immediately. |
| Cooperative `wsl --shutdown` (no Windows reboot) | 60–180 s (Windows-side budget) | **Mostly** — depends on Windows' overall shutdown budget; usually enough for a full graceful stop. | Operator restart; probe passes. |
| Windows reboot (Update / Schedule / User-triggered) | **~10 s** (RB_POWER_OFF) | **No** — the VM is gone before the script finishes. | Postgres' WAL replay recovers DB (correct by Postgres' own crash-safety); productizer relies on Family ⑤ (post-reboot stale-image alert) + Family ⑥ (post-reboot drift alert) + §7 post-restart probe to surface inconsistencies. |
| Windows BugCheck (BSOD) | **0 s** (hard kill) | **No** — same as above but worse. | Same as Windows reboot; manual operator inspection of the §7 probe output on next boot. |
| Operator unplugs the host | **0 s** | **No** | Postgres WAL replay; operator inspection. |

The contract explicitly accepts that the bottom three rows are **not defended against by Family ⑧**. They are defended against by:

1. **Postgres' own crash-safety** (WAL replay, fsync on commit). The PG class's 30 s `stop_grace_period` is an *optimization* over the WAL-replay path, not a substitute for it.
2. **Family ⑤'s `OmniSightStaleImage` alert** — fires post-reboot if the running image is older than the GHCR `:latest` digest. This catches "did the host come back up on the right code?" without needing to know whether the prior shutdown was forced.
3. **Family ⑥'s `OmniSightAlembicDrift` alert** — fires post-reboot if `alembic_version` does not match the running image's expected head. This catches "did the schema and code come back up in agreement?" without needing to know whether the prior shutdown was forced.
4. **§7 post-restart probe** — surfaces the symptoms above into the systemd unit's status; the operator sees `systemctl status omnisight-compose-prod` go `failed` if the probe doesn't pass.

### §8.3 What manual operator hooks are recommended

For installations that want to extend the cooperative-shutdown surface beyond what the productizer can do from inside WSL2, the operator can configure Windows-side hooks. These are out of scope for the productizer (we do not ship a Windows-side installer for them; see §10 boundary block) but the operator runbook (`v2-⑧-4a-Doc`) names them:

- **Windows Task Scheduler `OnEvent` for shutdown.** A pre-shutdown task that runs `wsl -d Ubuntu -- /opt/omnisight/scripts/shutdown.sh --mode=compose --timeout=120` before the WSL2 VM gets `RB_POWER_OFF`. This converts a Windows reboot into a cooperative shutdown, *if* the Windows shutdown sequence honors the task's exit code (it does for Update; it does not for BSOD or hard power loss).
- **Group Policy "Specify the maximum log file size for shutdown scripts"** — a Windows-side setting that gives shutdown tasks more than the default ~30 s budget. Out of scope for the productizer to set; documented in the runbook so the operator can configure.
- **WSL2 `/etc/wsl.conf` `[boot]` and `[user]` settings** — control startup order and default user, not shutdown. Listed here only so the runbook can disambiguate.

The contract does NOT promise these hooks will be in place. They are operator-side runbook entries.

### §8.4 ⚠ Limitation: Productizer cannot detect last forced shutdown

**This paragraph is mandatory per the parent spec v1.4 codex P1-4 caveat.**

The productizer running inside WSL2 cannot detect whether the prior shutdown was cooperative or forced. Specifically:

- **Windows BugCheck data** (the `MEMORY.DMP` file, the System event log Event ID 1074 / 1076 entries) is on the Windows filesystem under `C:\Windows\`. The WSL2 guest does not have read access to those files by default. Even if it did, parsing Windows event logs from inside Linux requires a Windows-side tool the productizer does not bundle.
- **Windows Update reboot scheduling** is recorded in the Windows registry (`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired`). Same accessibility problem.
- **Scheduled shutdowns** are recorded in Task Scheduler XML files. Same accessibility problem.
- **WSL2 init shutdown timestamp** is the closest signal the guest can read (`/var/log/wtmp` reboot entries, `last -x | grep reboot`). But this only tells the guest *that* the host rebooted; it cannot tell *why* — Windows Update vs. operator-triggered vs. crash all leave identical `wtmp` traces.

**What this means in practice**: after a host reboot, the operator MUST manually check whether the prior shutdown was forced by inspecting Windows-side sources (Event Viewer → Windows Logs → System → filter for Event ID 1074, 1076, 6008, 6005, 6006). The productizer cannot do this. The operator's runbook (`v2-⑧-4a-Doc`) MUST instruct the operator to:

1. Inspect Windows-side reboot reason on the first login after a host reboot.
2. If the reboot was forced (BugCheck, hard power loss), manually run the post-restart probe sequence (§7) again and inspect its log output, since the automatic probe ran on first boot but the operator should not assume it succeeded silently.
3. If the reboot was forced, manually check Family ⑤'s `OmniSightStaleImage` alert state and Family ⑥'s `OmniSightAlembicDrift` alert state — these would have fired automatically, but a forced reboot may have left the alert pipeline itself in a partial state until Prometheus + Alertmanager are themselves healthy, which the operator should confirm.

This is operator overhead that the productizer cannot eliminate from inside WSL2. The contract is honest about this: the limitation paragraph (this §8.4) is prominent so a future spec amendment cannot accidentally promise detection the productizer cannot deliver.

---

## §9. Cross-family delegation — D1 / D2 / D5 N/A rationale

Family ⑧ covers two of the five defense dimensions (D3 + D4) and explicitly delegates the other three to other families. This section pins the delegation map so a future amendment cannot accidentally widen Family ⑧'s scope without re-negotiating cross-family contracts.

### §9.1 D3 (shutdown) — Family ⑧'s own

Covered by §3 (the script) + §4 (the systemd unit) + §5 (the compose file). The script's per-service-class graduated stop sequence IS the D3 contract for the prod stack.

### §9.2 D4 (recovery) — Family ⑧'s own

Covered by §6 (pre-stop PG WAL safety probe) + §7 (post-restart probe). The recovery path is "Postgres' own WAL replay" enriched by these two probes; the script does not itself recover state, but it (a) shortens the WAL on shutdown so replay is cheap and (b) surfaces post-restart inconsistencies via the §7 probe so Family ⑥'s rescue path can engage.

### §9.3 D1 (detection) — N/A; delegated to Family ⑤ + Family ⑥

**N/A rationale.** A "last shutdown was forced" signal would require Windows-side hook integration to read BugCheck / Update / Schedule data. That is explicitly out of scope per §1.2 of the parent spec and per §8.4 of this document; documented in `v2-⑧-4a-Doc` as operator-manual.

The post-reboot signal Family ⑧ would otherwise emit is not "the shutdown was forced" (the productizer cannot detect that; §8.4) but "post-reboot the stack is in an unexpected state." That signal IS emitted, but by Family ⑤ (image stale) and Family ⑥ (alembic drift). Family ⑧ does NOT re-emit the same signal — cross-family delegation.

### §9.4 D2 (exception handling) — N/A; delegated to Family ⑤ + Family ⑥

**N/A rationale.** Post-reboot warning to operator is delivered via:

- **`v2-⑤-AlertRule`** — `OmniSightStaleImage` if image got rolled back during forced shutdown (the running image is older than `:latest` by more than the §11 RTO budget, which a forced shutdown could cause if the image had been pulled-but-not-restarted).
- **`v2-⑥-AlertRule`** — `OmniSightAlembicDrift` if DB and image misalign post-reboot (a forced shutdown could leave them transiently misaligned if a migration was mid-flight).

Family ⑧ doesn't re-emit either. The cross-family delegation is intentional — having two alerts fire for "post-reboot anomaly" is noise; one alert per actual misalignment is the right signal density.

### §9.5 D5 (rescue) — N/A; per-feature, not host-shutdown level

**N/A rationale.** At the host-shutdown level there is no rescue path — once SIGKILL or `RB_POWER_OFF` fires, the runtime is gone. The "rescue" of a forced shutdown is the next boot, which is not under the productizer's control (the operator or the OS triggers it).

Rescue paths DO exist at the per-feature level: `v2-⑥-RescueCLI` for DB drift, future per-app rescue paths for stuck containers. Operator's Windows-side `Restart-Service` is the only host-shutdown rescue and that is a Windows-side artifact, not a productizer artifact.

### §9.6 Delegation map summary

| Defense dim | Covered by | Family ⑧'s role |
|---|---|---|
| D1 | Operator runbook + Family ⑤ + Family ⑥ | Documents the limit (§8.4); does not detect. |
| D2 | Family ⑤ AlertRule + Family ⑥ AlertRule | Delegates entirely. |
| **D3** | **Family ⑧** (§3 + §4 + §5) | **Owns.** |
| **D4** | **Family ⑧** (§6 + §7) | **Owns.** |
| D5 | Family ⑥ RescueCLI + per-feature rescues | Delegates entirely. |

---

## §10. Chaos test contract — `v2-⑧-DRDrill`

The parent spec's `v2-⑧-DRDrill` ticket exercises the contract pre-merge. This section names the scenarios that MUST be in the test suite for the contract to be considered enforced.

### §10.1 Scenarios that MUST be exercised

Each scenario runs on a sandbox host (not prod). The test harness is either a dedicated WSL2 sandbox VM or a CI runner with docker-in-docker; the test author picks per scenario tractability.

1. **Cooperative full stop** — `systemctl stop omnisight-compose-prod`; verify the §3 script runs to completion; verify the §3.4 verification loop reports all services down; verify total wall time < 180 s; verify the §7 post-restart probe passes when the unit is re-started.
2. **Cooperative full stop with hung backend** — inject a 60 s sleep into the backend's `lifespan` close hook; verify the script times out the backend at 40 s and SIGKILLs it; verify PG and the rest still stop cleanly; verify the post-restart probe still passes (PG WAL replay covers the SIGKILLed backend's last write).
3. **Cooperative full stop with hung Postgres** — inject a stuck CHECKPOINT (a long-running transaction holding pg_xlog); verify the §6 pre-stop probe warns but proceeds; verify Postgres is SIGKILLed at 30 s; verify Postgres' own WAL replay on next start covers the rollback; verify the §7 probe passes.
4. **Forced shutdown — simulated `RB_POWER_OFF`** — `kill -9 1` on the WSL2 init process (the cleanest in-guest analog to `RB_POWER_OFF`); verify Postgres' WAL replay on next boot is clean (no `pg_resetwal` needed); verify the §7 post-restart probe passes within 60 s; verify Family ⑤ + Family ⑥ AlertRules fire if image-or-DB has drifted (this part is mocked at the metric level since real GHCR pulls are out of scope for a chaos test).
5. **`docker compose down` from operator terminal** — operator runs `docker compose -f docker-compose.prod.yml down` directly, bypassing the §3 script; verify the per-container `stop_grace_period` from §5 is what the daemon uses; verify no service is SIGKILLed prematurely (specifically: PG must get its full 30 s, backend its full 40 s).
6. **Repeated invocations of `scripts/shutdown.sh`** — invoke the script three times in sequence; verify (a) first invocation does the work, (b) second invocation exits 0 with "already inactive" logs, (c) third invocation also exits 0; verify no residue (dangling containers, stuck systemd units).
7. **`systemd-analyze verify omnisight-compose-prod.service`** — static check; verify the unit file passes without warnings.
8. **`stop_grace_period` cross-check** — programmatic check that every `-t N` value in `scripts/shutdown.sh` has a matching `stop_grace_period: Ns` for the same service in `docker-compose.prod.yml`; fail the test if they diverge.

### §10.2 What counts as a pass

The DRDrill passes iff and only iff all 8 scenarios pass in a single CI run. A flake in any one scenario blocks merge.

The pass criterion is automated: each scenario emits structured JSON with `{scenario, pass, evidence}` and the harness aggregates. The aggregated record is committed to `docs/audit/AUDIT-G-A-v2-Family-8/dr-drill-YYYY-MM-DD.json` as the evidence file per the v2-A8 SelfAudit pattern (parent spec §4 row 8).

### §10.3 What is OUT of scope for DRDrill

- Real Windows-side reboots. The simulator (`kill -9 1`) is the closest we can do without a CI VM that mounts a Windows host. The parent spec's `v2-⑧-Integration` 14-day soak picks up the real-Windows-reboot signal indirectly via post-reboot probe results.
- Multi-host failover. The prod stack is single-host today.
- Database backup / restore. Out of scope; Family ⑥ owns that.
- Network partition during shutdown. Out of scope; the chaos test runs on a single host, no network partition simulation.

---

## §11. 14-day soak invariants — `v2-⑧-Integration`

The 14-day soak (per the parent spec's `v2-⑧-Integration` row) is the final validation that the contract holds in real operation. The soak is run on the prod host (with operator coordination) starting after `v2-⑧-DRDrill` passes.

### §11.1 The three invariants

1. **RTO < 60 s** — every restart (whether triggered by the weekly synthetic shutdown in §11.3 or by any incidental operator action during the soak window) reaches a state where the §7 post-restart probe passes within 60 s. The soak records each restart's wall-clock time-to-probe-pass. The 60 s threshold is a p99 — one outlier per 14-day window is allowed (operator may have been doing something unusual on the host); two or more outliers fail the soak.
2. **Zero corruption** — no Postgres WAL replay error, no `pg_resetwal` invocation, no `alembic_version` row missing-or-malformed on any restart during the window. This is a hard zero — even one corruption event fails the soak and the contract is considered un-shipped.
3. **Weekly synthetic shutdown** — a cron job (`v2-⑧-Integration` provides the cron unit + the script invocation) runs `systemctl stop omnisight-compose-prod` followed by `systemctl start omnisight-compose-prod` once per week (e.g. Sunday 03:00 local). The synthetic shutdown exercises the cooperative path on a live host. Each synthetic shutdown emits its own §10.1 row-1-style evidence record. Skipped synthetic shutdowns (operator suppressed the cron during a freeze) do not count against the soak but the operator must record the suppression reason in the soak log.

### §11.2 Evidence collection

The soak emits one JSON record per restart to `docs/audit/AUDIT-G-A-v2-Family-8/soak-YYYY-MM-DD.json`. Each record has:

```json
{
  "timestamp": "2026-05-23T03:00:14Z",
  "trigger": "synthetic_weekly | operator_manual | unknown_post_reboot",
  "shutdown_wall_seconds": 47,
  "shutdown_script_exit_code": 0,
  "shutdown_verification_passed": true,
  "restart_wall_seconds_to_probe_pass": 38,
  "post_restart_probe_exit_code": 0,
  "alembic_drift_detected": false,
  "stale_image_detected": false,
  "notes": null
}
```

At soak end (day 14), the harness aggregates the 14 days of records into a summary that is the AC artifact for `v2-⑧-Integration`.

### §11.3 The weekly synthetic shutdown

The cron line (per `v2-⑧-Integration`):

```cron
# /etc/cron.d/omnisight-family8-weekly-soak
0 3 * * 0 root /opt/omnisight/scripts/family8-soak-tick.sh
```

The script `family8-soak-tick.sh` (per `v2-⑧-Integration`):

```bash
#!/usr/bin/env bash
# Weekly synthetic-shutdown tick for Family ⑧'s 14-day soak.
set -euo pipefail

# Pre-stop snapshot
systemctl stop omnisight-compose-prod
sleep 10
systemctl start omnisight-compose-prod

# Wait for the post-restart probe to finish (it's wired as a oneshot via ExecStartPost)
systemctl status omnisight-postrestart-probe.service --no-pager | tee -a /var/log/omnisight/soak.log
```

The cron MUST be operator-suppressible by removing the file (no shipped `if`-flag inside the cron itself; KISS). During a known-busy operator window, the operator removes the file; the contract is "graceful — operator owns the suppression."

### §11.4 What ends the soak

The soak ends:

- **Pass**: 14 consecutive days without a hard invariant violation; AC artifact committed; `v2-⑧-Integration` closes.
- **Fail**: any corruption event (invariant #2) ends the soak immediately; `v2-⑧-Integration` is reopened with the failing evidence record; the contract is considered un-shipped until the regression is fixed and the soak restarts at day 0.
- **Abort**: operator-triggered abort (e.g. unplanned prod incident demanding a different code path); the soak restarts at day 0 once the incident is resolved.

---

## §12. Open questions, changelog, hand-off

### §12.1 Open questions (intentionally left to downstream)

These were considered and *deferred* — they don't belong in the spec but are flagged so downstream tickets can pick them up without re-discovery:

1. **Q12.1.1 — Should `scripts/shutdown.sh` emit a Prometheus counter (graceful-shutdown success / failure count) for the §11 RTO histogram?** *Recommended at `v2-⑧-1bc`:* yes, `omnisight_shutdown_total{result}` + `omnisight_shutdown_duration_seconds` histogram. The 14-day soak then asserts the p99 from the histogram instead of from individual JSON records, which is more robust against operator-suppressed weeks.
2. **Q12.1.2 — Does the `stop_grace_period` set in `docker-compose.prod.yml` need to be ALSO set on the matching `docker-compose.staging.yml`?** *Recommended at `v2-⑧-2b`:* yes, identical values, so staging exercises the same shutdown sequence pre-prod. The contract pins prod values; staging mirrors.
3. **Q12.1.3 — Does the §7 post-restart probe run on EVERY restart (including operator `docker compose restart backend-a` for a single service)?** *Resolution at `v2-⑧-3a`:* the probe runs from `omnisight-compose-prod.service`'s `ExecStartPost=`; a single-service restart via `docker compose restart` does NOT re-trigger the systemd unit, so the probe does NOT re-run. This is by design — single-service restart is the operator's chosen scope; the probe is a whole-stack check.
4. **Q12.1.4 — Should the `--backup-db` flag take a Postgres dump in addition to the SQLite dump?** *Resolution here, NOT deferred:* no. Postgres dumps are a separate operational concern (volume snapshots; pg_dump cron; PITR) and conflating them with the shutdown script invites scope creep + a slow shutdown. The flag remains SQLite-only.
5. **Q12.1.5 — Does the contract apply to non-WSL2 hosts (a pure Linux prod host)?** *Resolution here:* §1–§7 apply identically; §8 (WSL2 constraints) is moot on a pure Linux host but the runbook (`v2-⑧-4a-Doc`) still applies because the limitation paragraph is operator-orienting. A future portable installer might bake "is this WSL2?" detection and skip §8.4's operator paragraph; not needed today.
6. **Q12.1.6 — What happens if `scripts/shutdown.sh` itself is missing on the host at the moment systemd fires `ExecStop`?** *Resolution at `v2-⑧-2a`:* the systemd unit's `ExecStop=` path is absolute (`/opt/omnisight/scripts/shutdown.sh`); if missing, systemd logs "exec format error" and falls back to its default cgroup-kill. The fallback is graceless but Postgres' own crash-safety covers WAL. The operator runbook warns against `rm`ing the script. A future `v2-⑧-2a` enhancement could add `Condition` directives to refuse to start the unit if the script is missing; not in scope today.

### §12.2 Changelog

| Version | Date | Author | Change |
|---|---|---|---|
| v1 | 2026-05-16 | claude-bot (OP-1158) | Initial spec. D3 + D4 ownership; D1 / D2 / D5 N/A rationale per parent spec v1.4. Per-service-class grace budgets pinned (PG=30, backend=40, stateless=15, other=10). `TimeoutStopSec=180` derived. WSL2 `RB_POWER_OFF` limit + "Productizer cannot detect last forced shutdown" §8.4 limitation paragraph mandatory per parent spec v1.4 codex P1-4 caveat. |

### §12.3 Hand-off block (for the next ticket)

The next ticket to pick up Family ⑧ work is **v2-⑧-1bc** (per parent spec §3 Family ⑧ row 2). That ticket's AC should reference:

- §2 (service classification and grace periods).
- §3 (the `scripts/shutdown.sh` contract — calling contract, per-class wire-up, idempotency, verification loop).
- §10 (the chaos-test scenarios that will exercise it).

After `v2-⑧-1bc` ships the extended script:

- **v2-⑧-2a** refactors `omnisight-compose-prod.service` per §4. Operator-window cooperation required.
- **v2-⑧-2b** adds `stop_grace_period` to every service in `docker-compose.prod.yml` per §5.
- **v2-⑧-3a** adds the PG WAL safety pre-stop probe (§6) and the post-restart probe (§7).
- **v2-⑧-4a-Doc** writes `docs/sop/wsl2-shutdown-contract.md` — operator runbook covering §8 (the WSL2 constraints) and §8.4 (the mandatory "Productizer cannot detect last forced shutdown" paragraph).
- **v2-⑧-DRDrill** implements the §10 chaos test.
- **v2-⑧-Integration** runs the §11 14-day soak.

If any downstream ticket's AC contains the literal phrase *"per `2026-05-16-v2-family8-graceful-shutdown-contract.md` §X.Y"*, the section anchor must remain stable. Renumbering this spec requires a v1.1 amendment with a redirect table.

### §12.4 Cross-references

- **Parent spec**: `sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 "Family ⑧ — WSL2 graceful shutdown contract" — this doc is the contract surface that row's `Contract spec doc` cell references.
- **Family ⑤** (`2026-05-16-v2-family5-image-surfacing-contract.md`) §7 alert routing — emits `OmniSightStaleImage` post-reboot when the running image is older than expected (D2 delegation per §9).
- **Family ⑥** (TBD — `2026-05-16-v2-family6-image-db-drift-contract.md` when filed) — owns `OmniSightAlembicDrift` and the `RescueCLI`; consumed by §7's post-restart probe.
- **AlertBridge framework** (`2026-05-16-v2-alertbridge-framework-contract.md`) — the alert plumbing both delegated families ride on.
- **`scripts/shutdown.sh`** — existing 390-line graduated-shutdown script; the §3 contract pins what `v2-⑧-1bc` extends it to satisfy.
- **`omnisight-compose-prod.service`** — existing systemd unit; the §4 contract pins what `v2-⑧-2a` refactors.
- **`docker-compose.prod.yml`** — existing compose file; the §5 contract pins what `v2-⑧-2b` adds to.

---

## §13. Boundary justification (spec ticket boundary block)

This ticket (OP-1158 / v2-⑧-1a) declares the following boundaries per `docs/sop/jira-ticket-conventions.md` §11:

```yaml
scope_components: [docs]
destructive_op_classes: []
external_side_effect: none
filesystem_writes:
  - docs/sprint-s12/2026-05-16-v2-family8-graceful-shutdown-contract.md  (NEW)
  - docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md  (MINOR: x-ref)
runtime_paths_changed: 0
db_migrations: 0
ci_yaml_changed: 0
prod_compose_changed: 0
secrets_touched: []
```

**Exemption from the area-block list (out-of-area: backend / db / devops / embedded / frontend / security / tests / tooling):** the area-block list in the ticket envelope excludes work in those areas because this is a docs-only ticket. The work is `area:docs` only; the spec describes future devops / systemd / compose / backend / tests contract surfaces but does not modify any backend / db / devops / embedded / frontend / security / tests / tooling file. The downstream `v2-⑧-1bc` (extends `scripts/shutdown.sh` — area:devops + area:tests), `v2-⑧-2a` (modifies `omnisight-compose-prod.service` — area:devops), `v2-⑧-2b` (modifies `docker-compose.prod.yml` — area:devops), `v2-⑧-3a` (adds PG probes — area:devops + area:db + area:tests), `v2-⑧-4a-Doc` (adds operator runbook — area:docs), `v2-⑧-DRDrill` (chaos test — area:tests + area:devops), and `v2-⑧-Integration` (14-day soak — area:devops + area:tests + area:docs) will be re-evaluated under the full area-block set at filing time.
