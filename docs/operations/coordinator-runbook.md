# Release Pipeline Coordinator — Operator Runbook

> **Ticket**: OP-1011 (AUDIT-29f-13)
>
> **ADR**: [ADR-0021 — Release Pipeline Coordinator](../adr/ADR-0021-release-pipeline-coordinator.md)
>
> **Audience**: the release operator (`nanakusa sora`) monitoring,
> overriding, pausing, inspecting, or troubleshooting the
> `pipeline_coordinator` daemon.
>
> **Status**: canonical operator artifact for the coordinator daemon.
> Reflects the code as shipped through AUDIT-29f-6 (Tier-2 LLM consult)
> + 29f-7 (cold-start). Where the running code differs from ADR-0021's
> aspirational text, **this runbook documents the code** and flags the
> gap — see [§7 quick reference](#7-quick-reference-card-adr-0021-appendix-b)
> for the implementation-status column.

## 0. Mental model — what is running, and is it acting?

The coordinator is a single long-running **user** systemd daemon
(`backend.agents.pipeline_coordinator`) that, every tick: writes a
heartbeat, drains its event sources (bridge tap + 60 s JIRA poll +
hourly sweep), runs the hybrid decision engine (Tier-1 rules →
Tier-2 LLM consult), and appends one JSON line per decision to the
append-only decision log.

Two facts dominate every operator decision below:

1. **The steady-state action layer is `ShadowActionExecutor` by
   default** (`build_default_coordinator`,
   `backend/agents/pipeline_coordinator.py:1709`). It *records* what a
   rule would do (`executed=false`, `shadow=true`) but **does not mutate
   JIRA**. This is the safe posture for the 29f-14 7-day shadow canary.
   Acting mode is enabled later by swapping the executor — until then,
   a "scary" decision-log line is an *observation*, not an action.
2. **Cold-start recovery DOES act.** The 4-phase boot recovery
   (ADR §3.3 / `pipeline_coordinator.py:475+`) transitions tickets,
   relabels, and clears assignees for real on every daemon boot. So a
   restart is not side-effect-free — see [§5](#5-pause--stop) before
   restarting during an incident.

| Concept | Where it lives |
|---|---|
| Daemon unit | `pipeline-coordinator.service` (user) |
| Watchdog unit | `pipeline-coordinator-watchdog.service` (user) |
| Working dir | `/home/user/sora-bridge` (auto-synced to `develop`) |
| Config dir | `/home/user/.config/omnisight/coordinator/` |
| Heartbeat | `…/coordinator/heartbeat` (rewritten every 60 s) |
| Decision log | `…/coordinator/decision-log/YYYY-MM-DD.jsonl` (0600) |
| Daemon stdout/stderr | `/home/user/work/sora/logs/coordinator/systemd.log` |
| Watchdog logs | journal, `SyslogIdentifier=pipeline-coordinator-watchdog` |

> The deployed units set their environment **inline** via
> `Environment=` directives — there is **no `EnvironmentFile`**. The
> `budget.env` path mentioned in ADR-0021 §5.3 is **not wired** by the
> shipped unit; set budget/model knobs with a systemd drop-in instead
> ([§3.1](#31-changing-an-env-knob)).

## 1. Monitoring

### 1.1 Is it alive?

```bash
# Unit state (both units should be active/running):
systemctl --user status pipeline-coordinator.service
systemctl --user status pipeline-coordinator-watchdog.service

# Heartbeat freshness — should be < 60s old; payload carries the live pid:
stat -c '%Y  %n' /home/user/.config/omnisight/coordinator/heartbeat
cat /home/user/.config/omnisight/coordinator/heartbeat
#   {"event":"coordinator-alive","pid":12345,"ts":"2026-05-20T…Z"}
```

A heartbeat older than the watchdog's stale window (90 s, default) is
the watchdog's restart trigger — see [§6.1](#61-coordinator-keeps-restarting-watchdog-flap).

### 1.2 What is it doing right now?

```bash
# Live daemon log (cold-start phases, tick reasons, drain):
tail -f /home/user/work/sora/logs/coordinator/systemd.log

# Today's decisions, newest last:
tail -f /home/user/.config/omnisight/coordinator/decision-log/$(date -u +%F).jsonl | jq .
```

Key log lines to look for on a healthy boot:

- `cold-start complete — entered steady state at <ts>` — Phase
  Startup-4 reached; the daemon is in its normal loop.
- `entering steady state` / per-tick `decision_tick` records.
- `caught signal N — draining` then `drain complete — exiting cleanly`
  — a clean L5 shutdown (expected on `systemctl stop` / SIGTERM).

### 1.3 Confirm shadow vs acting

```bash
# In shadow mode every action line is executed=false / shadow=true.
# If you ever see executed=true on a *decision_tick* (not a startup_phase),
# acting mode has been enabled — know that before you assume "it can't touch JIRA".
jq -c 'select(.event=="decision_tick") | {ts, mode, reason, dry_run, actions}' \
   /home/user/.config/omnisight/coordinator/decision-log/$(date -u +%F).jsonl
```

## 2. Operator-override labels

These are the labels the **coordinator code actually checks or emits**
(verified against `pipeline_coordinator_rules.py`,
`pipeline_coordinator_modes.py`, and `coordinator_jql()`):

| Label | Direction | Effect (code site) |
|---|---|---|
| `coord-skip` | operator → coord | Coordinator never touches the ticket. Excluded in `coordinator_jql()` (`pipeline_coordinator.py:463`) **and** vetoed by the `operator-keep-out` rule (`pipeline_coordinator_rules.py`, `COORD_SKIP_LABEL`). Operator escape hatch — the coordinator may not remove it from itself (ADR §6.2). |
| `coord-mode:<mode>` | operator → coord | Forces a personality mode for any decision on that ticket. `<mode>` is a slug (`execution`, `investigation`, `rescue`, `triage`, `architecture`) **or** the canonical name (`ExecutionMode`…), case-insensitive (`pipeline_coordinator_modes.py:86`, `_OVERRIDE_SLUGS`). Phase 6 ships Execution + Investigation + Rescue behaviors. |
| `coord-resume-after:<date>` | coord → self | Defer attention until `<date>`. Emitted by the `mark_for_followup` action (ADR §6.1, `pipeline_coordinator_rules.py:194`). **Note:** while the steady-state action layer is shadow, this label is *logged as intended*, not yet applied to JIRA. |
| `needs-coordinator` | operator → coord | Explicit "look at this" — a JQL poll trigger (`pipeline_coordinator.py:465`). |
| `coord-quarantine` | coord → operator | Added when a ticket reverts > 5× in 24 h (`revert-loop-quarantine` rule), paired with an operator @-mention. |
| `needs-operator-action` | coord → operator | Added by `escalate(...)` and by `mention_operator(..., urgency="high")` (`pipeline_coordinator.py:806`). |
| `runner:resume-from-feature-branch` | coord → runner | Cold-start Startup-2(a): an interrupted ticket whose feature branch has commits is left In Progress with this label for the runner to resume (`RESUME_FROM_FEATURE_LABEL`). |
| `coord-keep-open` | operator → **bridge** | Suppresses the **bridge's** Published→Archived auto-archive (`gerrit_jira_bridge.py:72`, `KEEP_OPEN_LABEL`). Checked by the bridge, **not** the coordinator — listed here because it's part of the same operator override vocabulary (ADR-0021 AC). |

## 3. Environment knobs (with defaults)

All knobs are `OMNISIGHT_*` env vars read at process start. Defaults
below are the code defaults — an un-configured deploy uses them.

### Coordinator daemon — `pipeline_coordinator.py`

| Env var | Default | Meaning |
|---|---|---|
| `OMNISIGHT_COORDINATOR_CONFIG_DIR` | `~/.config/omnisight/coordinator` | Base config dir (heartbeat, decision-log, capacity, bridge-events live under it). |
| `OMNISIGHT_COORDINATOR_DECISION_LOG_DIR` | `<config_dir>/decision-log` | Append-only decision log dir (one `YYYY-MM-DD.jsonl` per UTC day). Shared with the bridge so both write one audit trail. |
| `OMNISIGHT_COORDINATOR_BRIDGE_EVENTS_FILE` | `<config_dir>/bridge-events.jsonl` | Bridge event tap the coordinator tails (§4 source). |
| `OMNISIGHT_COORDINATOR_JIRA_AGENT_CLASS` | `subscription-claude` | JIRA client class used for the 60 s poll + mutations. |
| `OMNISIGHT_RUNNER_CAPACITY_PATH` | `<config_dir>/runner_capacity.json` | Capacity summary the coordinator maintains from runner quota telemetry (`pipeline_coordinator_capacity.py:33`). |

### Tier-2 LLM consultation — `pipeline_coordinator_llm_consultation.py`

| Env var | Default | Meaning |
|---|---|---|
| `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD` | `5.0` | Per-UTC-day Tier-2 spend cap. **`<= 0` disables Tier-2 entirely** — the operator kill switch for LLM spend. On cap-hit the daemon degrades to Tier-1-only and alerts (reason `tier2_degraded_budget_cap`). |
| `OMNISIGHT_COORDINATOR_LLM_MODEL` | `claude-opus-4-7` | Model passed to the claude CLI. |
| `OMNISIGHT_COORDINATOR_LLM_TIMEOUT_S` | `90.0` | Per-consult CLI timeout (seconds). |
| `OMNISIGHT_COORDINATOR_CLAUDE_CLI` | `claude` | Path/name of the claude CLI binary. Must be on the daemon's `PATH`. |

### Watchdog — `pipeline_coordinator_watchdog.py` + the `.service` shell loop

| Env var | Default | Meaning |
|---|---|---|
| `OMNISIGHT_COORDINATOR_HEARTBEAT` | `<config_dir>/heartbeat` | Heartbeat path read by the **bundled `.service` shell-loop** watchdog. |
| `OMNISIGHT_COORDINATOR_HEARTBEAT_PATH` | `<config_dir>/heartbeat` | Heartbeat path read by the **Python** watchdog module (`pipeline_coordinator_watchdog.py:40`). Both name the same file; the two readers use different var names — set both if you relocate the heartbeat. |
| `OMNISIGHT_COORDINATOR_WATCHDOG_SERVICE` | `pipeline-coordinator.service` | Unit the watchdog restarts on stale heartbeat. |
| `OMNISIGHT_COORDINATOR_WATCHDOG_POLL_SECONDS` | `30` | Heartbeat poll interval. |
| `OMNISIGHT_COORDINATOR_WATCHDOG_STALE_SECONDS` | `90` | Age beyond which the heartbeat is "stale" → restart. |
| `OMNISIGHT_COORDINATOR_WATCHDOG_REDIE_SECONDS` | `300` | Re-die window: restarted-then-died-again inside this window escalates to operator (Python watchdog only). |

The units also set `PYTHONPATH=/home/user/sora-bridge` and
`PYTHONUNBUFFERED=1`.

**Fixed cadence (code constants, not env knobs):** heartbeat 60 s,
tick 1 s, JIRA poll 60 s, hourly sweep 3600 s, event-dedupe window
300 s (`pipeline_coordinator.py:97-104`). Change these in code +
redeploy, not via env.

### 3.1 Changing an env knob

There is no env file, so use a systemd user drop-in:

```bash
systemctl --user edit pipeline-coordinator.service
# In the editor, add:
#   [Service]
#   Environment=OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=2.0
systemctl --user daemon-reload
systemctl --user restart pipeline-coordinator.service   # see §5 — restart runs cold-start
```

## 4. Decision-log inspection

One JSON line per decision, append-only, one file per UTC day under
`…/coordinator/decision-log/`. The log survives crashes and is what
cold-start L6 replays — never edit it in place.

### 4.1 Record shapes you'll see

| `event` | When | Useful fields |
|---|---|---|
| `decision_tick` | every non-idle tick | `mode`, `reason`, `dry_run`, `actions[]` ({`kind`,`target`,`executed`,`shadow`}), optional `rule_name`, `tier`, `llm_consultation`, `mode_behavior`, `action_results` |
| `startup_phase` | cold-start Phases 1–4 | `phase`, `ticket`, `classification`/`action` (Phase 2), `sweep`/`labels` (Phase 3) |
| `crash_recovery_applied` | L6, on boot after an unclean exit | the resumed/rolled-back action |
| `shutdown_began` / `shutdown_complete` | L5 drain | `decision_id` (paired) |

### 4.2 Common queries

```bash
LOG=/home/user/.config/omnisight/coordinator/decision-log

# Everything the coordinator did today, compact:
jq -c '{ts,event,mode,reason,actions}' "$LOG/$(date -u +%F).jsonl"

# Only Tier-2 LLM consultations (cost + confidence):
jq -c 'select(.llm_consultation) | {ts, reason, llm:.llm_consultation}' "$LOG"/*.jsonl

# Decisions touching a specific ticket:
jq -c --arg t OP-1234 'select((.actions[]?.target==$t) or (.ticket==$t))' "$LOG"/*.jsonl

# Any *real* (non-shadow) action ever taken — should be empty in shadow mode
# except for cold-start startup_phase records:
jq -c 'select(.actions[]?.executed==true)' "$LOG"/*.jsonl

# Budget-degrade events (Tier-2 disabled by daily cap):
grep -h tier2_degraded_budget_cap "$LOG"/*.jsonl | jq -c '{ts,reason}'

# Detect an unclean exit (crash) — shutdown_began with no matching complete:
jq -r 'select(.event=="shutdown_began" or .event=="shutdown_complete") | "\(.event) \(.decision_id)"' \
   "$LOG"/*.jsonl
```

## 5. Pause / stop

> ⚠ **The pause env knobs in ADR-0021 Appendix B are NOT implemented.**
> `OMNISIGHT_COORDINATOR_PAUSED`, `OMNISIGHT_COORDINATOR_DRY_RUN`,
> `OMNISIGHT_COORDINATOR_DISABLE_RESTART`, and the `SIGUSR1`/`SIGUSR2`
> handlers do not exist in the shipped code (only `SIGTERM`/`SIGINT` are
> handled, `pipeline_coordinator.py:1696`). Use the mechanisms below.

### 5.1 Stop one ticket (no daemon change)

Add the `coord-skip` label to that ticket. The coordinator drops it
from its JQL and the `operator-keep-out` rule vetoes any action. This
is the lightest, most reversible control — prefer it.

### 5.2 "Pause everything" — the watchdog will fight you

The daemon has **no in-process pause**. Today it is observe-only
(shadow), so "pausing" usually means "stop boot-time cold-start
side-effects" or "stop LLM spend":

- **Stop Tier-2 LLM spend only:** set
  `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD=0` ([§3.1](#31-changing-an-env-knob))
  and restart. The daemon stays up, degrades to Tier-1-only, no CLI calls.
- **Fully stop the daemon:** the watchdog restarts the coordinator when
  the heartbeat goes stale, so a bare
  `systemctl --user stop pipeline-coordinator.service` is **resurrected
  within ~90 s**. Stop the watchdog *first*:

  ```bash
  systemctl --user stop pipeline-coordinator-watchdog.service
  systemctl --user stop pipeline-coordinator.service
  # …work…
  systemctl --user start pipeline-coordinator.service          # runs cold-start
  systemctl --user start pipeline-coordinator-watchdog.service
  ```

- **Keep it stopped across a reboot / prevent restart:** `mask` it
  (there is no `DISABLE_RESTART` knob):

  ```bash
  systemctl --user mask pipeline-coordinator.service
  # later:
  systemctl --user unmask pipeline-coordinator.service
  ```

### 5.3 Graceful drain

`SIGTERM` is the L5 drain signal (the unit sets `KillSignal=SIGTERM`,
`TimeoutStopSec=90`). `systemctl --user stop` already sends it; the
daemon finishes any in-flight LLM consult (≤60 s), writes
`shutdown_began`/`shutdown_complete`, and exits cleanly. Avoid `kill -9`
— it skips the drain and forces an L6 crash-recovery on next boot.

## 6. Troubleshooting — common failure scenarios + recovery

### 6.1 Coordinator keeps restarting (watchdog flap)

**Symptom:** `systemd.log` shows repeated boots; watchdog journal logs
`coordinator heartbeat stale … restarting`.

**Diagnose:** is the daemon dying during cold-start? `journalctl --user
-u pipeline-coordinator.service -n 100`; look for a Phase Startup-1
infra-verification halt or a Python traceback. A Startup-1 failure
@-mentions the operator and halts startup by design (don't enter the
loop with degraded infra).

**Recover:** fix the failing dependency (bridge / runners / timers),
then `systemctl --user restart pipeline-coordinator.service`. If it is
a code crash and you need quiet, `mask` per [§5.2](#52-pause-everything--the-watchdog-will-fight-you)
while you patch.

### 6.2 No decisions appearing

**Symptom:** heartbeat is fresh but the day's decision log is empty or
only `startup_phase` records.

**Likely cause:** nothing matched the trigger JQL (no `needs-coordinator`
label, no recently-closed tickets, no assignee-less `進行中` tickets) —
this is *normal idle*. Confirm the poller is running via the
`steady state` log line, and hand-feed a trigger by adding
`needs-coordinator` to a test ticket.

### 6.3 Tier-2 LLM consults stopped

**Symptom:** decisions are all Tier-1; `tier2_degraded_budget_cap` in
the log.

**Cause/recover:** daily budget cap hit (resets at the UTC day
boundary), or `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD<=0`, or the claude
CLI isn't reachable (`tier2_llm_invocation_failed`). Check the budget
knob and that `OMNISIGHT_COORDINATOR_CLAUDE_CLI` resolves on the
daemon's `PATH`. Raise the cap via a drop-in if intentional.

### 6.4 Coordinator did something unexpected to a ticket

**First:** confirm it actually happened — in shadow mode the log line
is an *observation* (`executed=false`). Run the
[§1.3](#13-confirm-shadow-vs-acting) check. If `executed=true`, it was
cold-start (a real boot recovery) or acting mode is on.

**Recover:** add `coord-skip` to stop further attention, then reverse
the specific mutation by hand (relabel / re-transition). Cold-start
transitions are idempotent and conservative (it only routes to Under
Review on a *positively confirmed* mergeable change); a wrong call
usually means stale Gerrit/JIRA state — re-run the daemon once state is
correct.

### 6.5 Stale state after a crash

**Symptom:** orphan `claim:default:*` / `runner-blocked:waiting-*`
labels, or a ticket stuck `進行中` with no live runner.

**Recover:** these are exactly what cold-start Phase Startup-2/-3 sweep.
A clean restart re-runs them: stop the watchdog, restart the
coordinator (which re-boots through cold-start), restart the watchdog
([§5.2](#52-pause-everything--the-watchdog-will-fight-you)).

### 6.6 Escalation

When recovery isn't obvious, the daemon @-mentions the operator and
adds `needs-operator-action`. Per CLAUDE.md L1: after 2 identical
failures, stop retrying and escalate to a human rather than looping.

## 7. Quick-reference card (ADR-0021 Appendix B)

Lifted from ADR-0021 Appendix B, with an **implementation-status**
column reconciling the ADR's intent against the shipped code (Integration
AC: the runbook must match what the code actually does).

| Action | ADR-0021 mechanism | Status / real mechanism |
|---|---|---|
| Stop touching one ticket | label `coord-skip` | ✅ Implemented — JQL exclusion + `operator-keep-out` rule. |
| Force a mode for one decision | label `coord-mode:<mode>` | ✅ Implemented — `_OVERRIDE_SLUGS`, slug or canonical name. |
| Defer a ticket | label `coord-resume-after:<date>` | ⚠ Action exists; **applied only in acting mode** (shadow logs intent). |
| Keep a Published ticket open | label `coord-keep-open` | ✅ Implemented in the **bridge** auto-archive, not the coordinator. |
| Stop touching all tickets | `OMNISIGHT_COORDINATOR_PAUSED=1` | ❌ Not implemented. Use `coord-skip` per-ticket, or stop the daemon (watchdog first, [§5.2](#52-pause-everything--the-watchdog-will-fight-you)). |
| Log but don't act | `OMNISIGHT_COORDINATOR_DRY_RUN=1` | ❌ Not implemented as a knob — the daemon is **already** observe-only (shadow) by default. |
| Drain + exit | `kill -SIGUSR1 <pid>` | ⚠ Drain is on **`SIGTERM`** (= `systemctl --user stop`), not SIGUSR1. |
| Run but don't restart on crash | `OMNISIGHT_COORDINATOR_DISABLE_RESTART=1` | ❌ Not implemented. `systemctl --user mask` the unit. |
| Wake from self-pause | `kill -SIGUSR2 <pid>` | ❌ L3 self-pause + SIGUSR2 not implemented in shipped code. |
| Cap / disable LLM spend | (budget cap, ADR §5.3) | ✅ `OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD` (`<=0` disables Tier-2). |
| Revert a logged decision | `git revert` the log entry | ✅ Decision log is an append-only file in the operator's home. |
| Add a Tier-1 rule | author a patch | ✅ Operator-authored rules in `pipeline_coordinator_rules.py`. |

> The ❌/⚠ rows are the known gaps between ADR-0021 Appendix B and the
> shipped daemon. If a future patch implements the pause/SIGUSR knobs,
> update this table and §5 in the same change.

## 8. References

- [ADR-0021 — Release Pipeline Coordinator](../adr/ADR-0021-release-pipeline-coordinator.md) (§3.3 cold-start, §5 decision engine, §6 permissions, §9 resilience, Appendix B/C)
- Units: `deploy/systemd/pipeline-coordinator.service`, `deploy/systemd/pipeline-coordinator-watchdog.service`
- Code: `backend/agents/pipeline_coordinator.py`, `…_rules.py`, `…_modes.py`, `…_capacity.py`, `…_llm_consultation.py`, `…_watchdog.py`
- Bridge auto-archive (`coord-keep-open`): `backend/agents/gerrit_jira_bridge.py`
