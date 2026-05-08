# `unit_not_loaded`

| field | value |
|-------|-------|
| Severity | `P0` |
| Source | T2 watchdog — `scripts/daemon_watchdog.py` |
| Tier owner | infra-oncall lead |
| Parent META | OP-721 (closes the OP-689 "daemon never installed" failure mode) |

## What triggers it

The watchdog runs `systemctl [--user] is-enabled <unit>` for every
entry in `configs/watchdog.yaml`. If the command exits non-zero —
meaning the unit file is missing, masked, or disabled — the watchdog
fires `unit_not_loaded` at `P0` *without* checking the heartbeat log
(no point — there is no daemon to heartbeat).

This is exactly the OP-689 incident: the bridge unit was never
installed for two days and we noticed by accident. T2 closes that
gap.

## Severity rationale

`P0` because:

* the daemon is **not running at all**;
* the failure is unambiguous (no transient retry can fix a missing
  unit file);
* every minute the daemon is missing is data loss for whatever
  pipeline it serves (Gerrit→JIRA bridging in this case);
* re-page interval is 5 min until acked
  (`OMNISIGHT_NOTIFIER_P0_REPAGE_SECONDS`).

## Immediate action

1. **Determine which deploy step was skipped.** From the host:

       ls /etc/systemd/system/gerrit-jira-bridge.service \
          ~/.config/systemd/user/gerrit-jira-bridge.service 2>/dev/null

   Both missing → the install step never ran. One present and
   `is-enabled` reports `disabled` → someone disabled it manually.

2. **Reinstall the unit.** Re-run the deploy step that owns the unit
   file. For the gerrit-jira-bridge user-scope unit:

       cp deploy/systemd/gerrit-jira-bridge.service \
          ~/.config/systemd/user/gerrit-jira-bridge.service
       systemctl --user daemon-reload
       systemctl --user enable --now gerrit-jira-bridge.service

3. **Verify.** Within 60s, the next heartbeat must land:

       journalctl --user -u gerrit-jira-bridge --since '90s ago' \
           | grep '"event":"heartbeat"'

   The next watchdog tick (≤5 min) must clear the alert.

4. **Acknowledge** in the operator dashboard so the P0 re-page loop
   stops.

## Root-cause investigation

* **Did this happen because of a deploy?** Cross-check
  `docs/ops/upgrade_rollback_ledger.md` — the most recent entry
  preceding the alert is the prime suspect.
* **Did `systemctl is-enabled` succeed but the file got moved?**
  Check the unit's `FragmentPath` against the deploy fixture under
  `deploy/systemd/`. Drift here is also detected by T5
  ([`bridge_drift`](bridge_drift.md)) on the next daily scan, but T2
  catches it first.
* **Was the unit deliberately disabled?** Check shell history /
  audit log for `systemctl ... disable gerrit-jira-bridge`. If
  intentional, the watchdog config in `configs/watchdog.yaml` should
  drop the entry — operating with a stale watchdog config defeats
  AC #3 (no false positive).

## Escalation

* This alert is itself the escalation. No retry loop, no triage —
  the on-call lead's job is to **get the daemon running again** and
  then file a postmortem.
* Postmortem template:
  `docs/retrospectives/YYYY-MM-DD-<unit>-not-loaded.md`. Link from a
  META ticket labelled `meta:op-721`.
