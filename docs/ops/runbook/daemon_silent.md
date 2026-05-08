# `daemon_silent`

| field | value |
|-------|-------|
| Severity | `DEGRADED` |
| Source | T2 watchdog — `scripts/daemon_watchdog.py` |
| Tier owner | infra-oncall |
| Parent META | OP-721 (subscription daemon silent-failure) |

## What triggers it

The systemd timer `gerrit-jira-bridge-watchdog.timer` fires
`scripts/daemon_watchdog.py` every 5 minutes. For each entry in
`configs/watchdog.yaml` the script tails the daemon's log file
(`log_path`) and looks for the most recent line whose `event` field
matches the entry's `heartbeat_event` (default: `heartbeat`).

`daemon_silent` is emitted when the most recent heartbeat is **older
than `max_silence_minutes`** (10 min for `gerrit-jira-bridge`). With
the 5-min timer cadence and 10-min silence floor this gives a
worst-case detection latency of ~15 min, which is the AC #1 budget.

The daemon process may still be running — `daemon_silent` only
asserts that the heartbeat write side has stopped. Hung event loops,
deadlocks holding the heartbeat mutex, and journal-buffer flush
failures all manifest as `daemon_silent` while `systemctl status`
still shows `active (running)`.

## Severity rationale

`DEGRADED` rather than `CRITICAL` because:

* the daemon may simply be processing a long backlog and pause its
  heartbeat for a few minutes at a time;
* a fresh `unit_not_loaded` would have already paged at `P0` had the
  daemon been *missing* — `daemon_silent` is the "loaded but quiet"
  branch;
* operator response window: ~30 min. Beyond that, escalate.

The watchdog promotes to `unit_not_loaded` (P0) only when
`systemctl is-enabled` fails — that path is paged immediately.

## Immediate action

1. **Confirm the daemon is actually quiet** — do not trust the alert
   alone. From the bridge host:

       systemctl --user status gerrit-jira-bridge
       journalctl --user -u gerrit-jira-bridge --since '20 min ago' \
           | grep -E '"event":\s*"heartbeat"' | tail -5

   If you see heartbeats within the last 10 min, the watchdog's log
   tail may be stale (e.g. log rotation). Re-run:

       systemctl --user start gerrit-jira-bridge-watchdog.service

   and check `journalctl --user -u gerrit-jira-bridge-watchdog`.

2. **If the daemon is genuinely silent**, capture state before
   restarting (so root-cause is recoverable):

       py-spy dump --pid $(systemctl --user show -p MainPID --value gerrit-jira-bridge)

   then restart:

       systemctl --user restart gerrit-jira-bridge

3. Watch for the next heartbeat (≤60s). Confirm `daemon_silent`
   clears on the next watchdog tick (≤5 min).

## Root-cause investigation

| Symptom in `py-spy` dump | Likely cause | Reference |
|--------------------------|--------------|-----------|
| Stuck in `urllib`/`httpx` socket read | Upstream LLM / Gerrit hang | OP-689 family; `docs/sop/lessons-learned.md` L18 |
| Stuck in `asyncpg.acquire` | Connection pool exhausted | `docs/ops/observability_runbook.md` §pool |
| Stuck in `_emit_heartbeat` itself | Heartbeat mutex deadlock | unusual — open a META ticket |
| Process is gone / pid invalid | Watchdog config drift | check `log_path` in `configs/watchdog.yaml` |

Cross-check the alert payload's `last_heartbeat_age_seconds`
context field — anything > 10 min × 60s = 600s confirms genuine
silence vs. a watchdog tick race.

## Escalation

* Auto-clears on next heartbeat? Close the alert. No human action.
* Recurring within 24h? Open a JIRA ticket against `infra-oncall`,
  link this runbook, and label `meta:op-721`.
* `daemon_silent` plus `notifier_dispatch_failed` (T2) at the same
  time? Treat as `P0` — the alert path itself is also degraded.
  Page the on-call lead via the LINE channel.
