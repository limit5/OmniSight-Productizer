# Gerrit/JIRA bridge watchdog diagnosis

**Ticket**: OP-1036 / AUDIT-29e-4
**Date**: 2026-05-13 CST
**Decision**: keep the watchdog. Do not retire it.

## Summary

The `gerrit-jira-bridge-watchdog` is doing useful work and should stay
installed. The intermittent failures were not caused by a bad watchdog
model. They were two concrete runtime issues:

1. The bridge really did stop emitting heartbeat records for longer than
   the configured 10-minute silence window. The watchdog correctly wrote
   `daemon_silent` records to `/home/user/work/sora/logs/bridge/watchdog-alerts.jsonl`.
2. The optional T1 notifier fan-out failed because
   `scripts/daemon_watchdog.py::AlertSink.emit()` called
   `operator_notifier.notify(severity, code=..., **fields)`, while the
   module-level `backend.agents.operator_notifier.notify()` wrapper only
   accepted a pre-built `context` dict. That rejected valid watchdog
   fields such as `unit` and logged `notifier_dispatch_failed`.

The file sink and journald paths remained intact, so the watchdog was
not blind. The fix is to make the module-level notifier wrapper accept
structured alert fields and fold them into the notification context.

## Evidence

Live unit state checked on 2026-05-13 04:00 CST:

| Unit | `is-enabled` | `is-active` |
| --- | --- | --- |
| `gerrit-jira-bridge` | `enabled` | `active` |
| `gerrit-jira-bridge-watchdog.timer` | `enabled` | `active` |
| `gerrit-jira-bridge-watchdog.service` | `disabled` | `inactive` |
| `sora-bridge-sync.timer` | `enabled` | `active` |

The service being `disabled`/`inactive` is expected for a timer-triggered
oneshot service. The timer is the install target.

`systemctl --user list-timers --all 'gerrit-jira-bridge-watchdog.timer' 'sora-bridge-sync.timer'`
showed both timers scheduled:

| Timer | Last run | Next run |
| --- | --- | --- |
| `gerrit-jira-bridge-watchdog.timer` | 2026-05-13 03:57:19 CST | 2026-05-13 04:02:19 CST |
| `sora-bridge-sync.timer` | 2026-05-13 03:59:19 CST | 2026-05-13 04:04:19 CST |

Recent watchdog journal lines showed both the valid silence alerts and
the notifier API mismatch:

```text
2026-05-13T03:26:19+08:00 ... {"code": "daemon_silent", "err": "notify() got an unexpected keyword argument 'unit'", "event": "notifier_dispatch_failed", ...}
2026-05-13T03:26:19+08:00 ... {"code": "daemon_silent", "last_heartbeat": "2026-05-12T19:15:45.219654+00:00", "severity": "DEGRADED", "silence_minutes": 10.57, "threshold_minutes": 10, "unit": "gerrit-jira-bridge"}
2026-05-13T03:57:19+08:00 ... {"event": "all_green", "level": "INFO", "silence_seconds": 103.5, "threshold_minutes": 10, "unit": "gerrit-jira-bridge"}
```

`/home/user/work/sora/logs/bridge/watchdog-alerts.jsonl` also contains
the original `daemon_silent` records, including a longer outage window
from `last_heartbeat=2026-05-12T14:12:27.593079+00:00` through repeated
alerts up to `timestamp=2026-05-12T17:52:14.430918+00:00`.

## Root Cause

The watchdog's primary health assertion is liveness by heartbeat:

- `deploy/systemd/gerrit-jira-bridge-watchdog.timer` fires every 5 minutes.
- `configs/watchdog.yaml` declares a 10-minute silence threshold.
- `scripts/daemon_watchdog.py` emits `daemon_silent` when the latest
  structured heartbeat in the bridge log is older than that threshold.

That part is behaving as designed.

The bug was in the optional fan-out contract between T2 and T1. The
watchdog emits structured fields (`unit`, `log_path`, `last_heartbeat`,
`silence_minutes`, and so on). T1's module-level convenience wrapper did
not accept arbitrary fields, even though the lower-level `Notifier`
already models notification context as a dictionary.

## Remediation

`backend.agents.operator_notifier.notify()` now accepts extra keyword
fields, derives `message` from a structured `message=` field when
present, and folds remaining fields into `context` before delegating to
`Notifier.notify()`.

No watchdog retirement is needed:

- The watchdog still protects the bridge daemon against missing units and
  missing/stale heartbeat records.
- `sora-bridge-sync.timer` is active as the separate freshness guard for
  the stale-code gap documented by AUDIT-23 / OP-798.
- The remaining operational gap is evidence collection: this pickup can
  verify current green state and recent recovery, but cannot honestly
  claim a clean 72-hour green window because the last 72 hours include
  `daemon_silent` alerts.

## Verification

Run locally from the repo root:

```bash
backend/.venv/bin/pytest tests/test_daemon_watchdog.py backend/tests/test_operator_notifier.py
```

Operational checks:

```bash
systemctl --user is-enabled gerrit-jira-bridge gerrit-jira-bridge-watchdog.timer sora-bridge-sync.timer
systemctl --user is-active gerrit-jira-bridge gerrit-jira-bridge-watchdog.timer sora-bridge-sync.timer
systemctl --user start gerrit-jira-bridge-watchdog.service
journalctl --user -u gerrit-jira-bridge-watchdog.service --since '10 min ago' --no-pager
```
