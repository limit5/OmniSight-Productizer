# Bridge-health degraded runbook (SP-B-X-009 / OP-1067)

The Gerrit/JIRA bridge daemon (`backend.agents.gerrit_jira_bridge`)
owns the `change-merged → Published` transition. When it goes silent,
every ticket the fleet picks up that day will wedge at Approved
waiting for a transition the bridge will never make.

C9 (OP-1067) elevates the bridge's liveness to a first-class runner
signal: the daemon touches a heartbeat file every 30 s, and every
runner instance checks the mtime before claiming a ticket. A stale
heartbeat surrenders the tick immediately and emits a
`Severity.CRITICAL` `bridge_down` notification through
`operator_notifier`.

## What triggered this page

* `operator_notifier` alert with code `bridge_down`.
* On-call channels (Slack / LINE / email) carry a payload like:

  ```
  [CRITICAL] bridge_down: bridge heartbeat stale at
      /var/run/omnisight-bridge/heartbeat; runners blocked
  Context:
    heartbeat_path: /var/run/omnisight-bridge/heartbeat
    age_sec: 9999.0
  ```

* Runner logs across hosts begin emitting
  `[runner] bridge_down: heartbeat stale at <path> ...; skipping pickup`
  every tick — no claims are attempted while the gate is tripped.
* `OMNISIGHT_FLEET_HEALTH_CANARY_KEY` (when set) carries a matching
  `[runner-bridge-down]` JIRA comment for the incident retrospective.

## Triage in one minute

1. **Confirm the bridge unit is healthy.**

   ```sh
   systemctl --user status gerrit-jira-bridge.service
   journalctl --user -u gerrit-jira-bridge.service -n 200 --no-pager
   ```

   Expected steady-state: an `INFO heartbeat ...` line every
   `BridgeConfig.heartbeat_seconds` (default 60 s), and the file at
   `OMNISIGHT_BRIDGE_HEARTBEAT_PATH` (default
   `/var/run/omnisight-bridge/heartbeat`) refreshed every 30 s.

2. **Check the heartbeat file directly.**

   ```sh
   stat -c '%y %n' "${OMNISIGHT_BRIDGE_HEARTBEAT_PATH:-/var/run/omnisight-bridge/heartbeat}"
   ```

   If `stat` reports a recent mtime but the gate is still firing,
   suspect clock skew on the runner host or a stale `chrony`/`ntp`
   peering.

3. **Inspect Gerrit `stream-events` upstream.** The bridge maintenance
   tick fires on each event consumed from `gerrit stream-events`; the
   stream going silent (Gerrit SSH connectivity, key revocation, idle
   timeout) is the most common root cause.

   ```sh
   ssh -i ~/.config/omnisight/gerrit-claude-bot-ed25519 \
       -p 29418 claude-bot@sora.services gerrit stream-events <<< ''
   ```

   Any non-zero exit + `Permission denied`/`publickey` indicates
   credential drift. See `reference_gerrit_self_hosted.md` for the
   credential layout.

## Recovery paths (ordered by blast radius)

### A. Bridge daemon crashed / wedged

```sh
systemctl --user restart gerrit-jira-bridge.service
sleep 5
stat -c '%y' "${OMNISIGHT_BRIDGE_HEARTBEAT_PATH:-/var/run/omnisight-bridge/heartbeat}"
```

The bridge re-emits a heartbeat in `stream_forever()` *before* the
catchup loop, so the file should refresh within seconds of the
restart. Catchup itself runs idempotently and re-walks any
In-Progress / Under-Review / Approved tickets to Published as needed.

### B. Heartbeat path unwritable (post-OP-831 user-systemd path drift)

If the path defaults to `/var/run/omnisight-bridge/heartbeat` but the
bridge runs as a user-level service that cannot `mkdir` there, set
the override and restart:

```sh
mkdir -p ~/.local/state/omnisight-bridge
echo 'OMNISIGHT_BRIDGE_HEARTBEAT_PATH=%h/.local/state/omnisight-bridge/heartbeat' \
  >> ~/.config/omnisight/gerrit-jira-bridge.env
systemctl --user restart gerrit-jira-bridge.service
```

Pair the override with the matching env var on every runner host so
the gate reads the same path the bridge writes:

```sh
echo 'OMNISIGHT_BRIDGE_HEARTBEAT_PATH=%h/.local/state/omnisight-bridge/heartbeat' \
  >> ~/.config/omnisight/runner.env
```

### C. Gerrit upstream auth failure

Rotate the SSH key per `reference_gerrit_self_hosted.md`, then
restart the bridge. The bridge surfaces auth failures as a
`gerrit_auth_failed` ALERT and exits 2 — operator must restart.

## Non-recovery: do NOT auto-recover

Per the SP-B-X-009 non-goals, the gate **does not** auto-restart the
bridge or auto-stand up a replacement. The gate's job is to fail
closed and page on-call; recovery is operator-only so we never paper
over an upstream Gerrit-side outage that needs human investigation.

## After the bridge is back

1. Confirm the heartbeat is fresh:

   ```sh
   stat -c '%y' "${OMNISIGHT_BRIDGE_HEARTBEAT_PATH:-/var/run/omnisight-bridge/heartbeat}"
   ```

2. Watch one runner tick — the `[runner] bridge_down` log lines stop
   and pickups resume on the next polling cycle (no restart needed;
   the gate is evaluated each tick).

3. If `OMNISIGHT_FLEET_HEALTH_CANARY_KEY` was set, post a short
   resolution comment on that ticket with the root cause and the
   incident timeline, then `acknowledge(<notification_id>)` the
   `operator_notifier` page so the CRITICAL re-page loop stops.

## Tuning the gate

| Env var | Default | Effect |
| --- | --- | --- |
| `OMNISIGHT_BRIDGE_HEARTBEAT_PATH` | `/var/run/omnisight-bridge/heartbeat` | Where the bridge writes and the runner reads. Must agree on both sides. |
| `OMNISIGHT_BRIDGE_STALE_AFTER_SEC` | `300` | Runner-side stale threshold in seconds. Tighten only after observing a steady-state mtime cadence the daemon can honour. |
| `OMNISIGHT_FLEET_HEALTH_CANARY_KEY` | unset | When set to a JIRA key, the gate posts a `[runner-bridge-down]` comment there on each tripped pickup. Leave unset on production runners to avoid ticket-spam; set on a fleet-health canary ticket for incident retros. |

## Why no JIRA comment by default

The gate fires on **every** runner tick while the bridge is down. If
N runner hosts are deployed, leaving the JIRA write on would post N×
comments per minute on whichever ticket the gate landed on — which
is itself a JIRA-side incident. `operator_notifier` already handles
deduplication and rate-limiting on the on-call channels; JIRA is
opt-in via the canary key for the same reason.
