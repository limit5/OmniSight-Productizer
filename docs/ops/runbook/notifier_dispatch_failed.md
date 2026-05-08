# `notifier_dispatch_failed`

| field | value |
|-------|-------|
| Severity | `ERROR` (file sink only — does **not** itself reach the operator notifier) |
| Source | T2 watchdog — `scripts/daemon_watchdog.py::Pipeline.emit` |
| Tier owner | infra-oncall |
| Parent META | OP-721 |

## What triggers it

Inside the watchdog's `emit()` path, after building the alert record
and writing it to the file sink (`alerts_path`), the watchdog calls
`notifier_module.notify(severity, code=..., **fields)` if a
notifier module is configured (default
`backend.agents.operator_notifier`).

If that call raises **any** exception, the watchdog catches the
exception and emits `notifier_dispatch_failed` (severity `ERROR`)
into the alerts file with `code=<original_code>` and `err=<message>`
captured in the context. The original alert is **also** preserved in
the file sink — `notifier_dispatch_failed` is purely diagnostic and
does **not** silence the underlying signal.

This is a deliberate "fail-soft" design: if T1 is down, the watchdog
must still record what it saw so the operator can recover after T1
is fixed.

## Severity rationale

`ERROR` rather than `DEGRADED` or `CRITICAL` because:

* the signal is about the **alert path itself**, not the daemon
  being watched;
* the corresponding "real" alert (`daemon_silent`, `unit_not_loaded`,
  …) is already preserved in the file sink and will be picked up
  whenever T1 recovers;
* paging on this code without context would create a feedback storm
  if T1 itself flaps.

`notifier_dispatch_failed` is intended to be observed by **T3**
(`scripts/journal_error_forwarder.py`) — T3 tails the journal,
matches the `ERROR` priority, and routes it through its own T1 path.
If T1 is also down for T3, T3 will likewise fail-soft into its file
sink. That sink is the lowest layer; an operator who has lost T1
must read it manually.

## Immediate action

1. **Find out why T1 is broken.** Almost always a wiring / env-var
   problem rather than the notifier code itself:

       systemctl --user status gerrit-jira-bridge
       journalctl --user -u gerrit-jira-bridge --since '5 min ago' \
           | grep -E 'OMNISIGHT_NOTIFIER|operator_notifier'

   Common causes:
   * `OMNISIGHT_NOTIFIER_JIRA_TICKET` unset and notify() called
     without an explicit ticket → JIRA channel raises.
   * SMTP credentials rotated → email channel raises 535.
   * Slack webhook URL stale → `urlopen` 404.
   * `backend.agents.operator_notifier` not importable
     (PYTHONPATH / venv issue).

2. **Re-run T1 canary** to confirm the channel matrix once the
   wiring is fixed:

       python -c 'from backend.agents.operator_notifier import canary_self_test; print(canary_self_test())'

   Every wired channel must report `"ok"`.

3. **Replay the file sink** if the original alert needs to be
   re-dispatched:

       jq -c 'select(.code != "notifier_dispatch_failed")' \
         /home/user/work/sora/logs/bridge/watchdog-alerts.jsonl

   Pipe to a one-shot dispatcher script if needed (or open a JIRA
   ticket manually citing the line).

## Root-cause investigation

The `err` context field carries the exception class + message
captured from the failing `notify()` call. Common patterns:

| `err` excerpt | Likely fix |
|---------------|------------|
| `RuntimeError: JIRA channel: no ticket on payload and no ... configured` | Set `OMNISIGHT_NOTIFIER_JIRA_TICKET` or pass `ticket=` from the watchdog config |
| `smtplib.SMTPAuthenticationError ...` | Rotate SMTP password; restart unit so it picks up the new env |
| `urllib.error.HTTPError: HTTP Error 404` | Slack webhook URL is dead; regenerate in Slack admin |
| `ModuleNotFoundError: No module named 'backend'` | PYTHONPATH / unit `WorkingDirectory` regression |

## Escalation

* Single occurrence? File a low-priority follow-up; the file sink
  has the original alert.
* Recurring (≥3 in 1h)? T1 itself is unhealthy — escalate to
  on-call lead. Treat as if T1 is fully down: rely on the file sink
  + manual JIRA comments until T1 is restored.
* If `notifier_dispatch_failed` is the **only** signal in the file
  sink (no underlying alert) and T2 keeps emitting it, the bug is
  in the watchdog itself; downgrade T2 by stopping the timer until
  fixed (`systemctl --user stop gerrit-jira-bridge-watchdog.timer`).
