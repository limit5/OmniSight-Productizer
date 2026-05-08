# `audit_pool_not_initialised`

| field | value |
|-------|-------|
| Severity | `DEGRADED` (only when **un**-suppressed; see below) |
| Source | T3 forwarder — `scripts/journal_error_forwarder.py` (record originates in `gerrit-jira-bridge` audit logger) |
| Tier owner | bridge-maintainer |
| Parent META | OP-721 |

## What triggers it

The bridge's audit logger writes through an `asyncpg` connection
pool that is initialised lazily on the first request. During the
first ~1s after daemon start, the pool may not yet be ready; the
audit logger raises `audit_pool_not_initialised`, retries on a 1-s
backoff, and the second attempt always succeeds.

This is a known, documented boot-time race
(`docs/sop/lessons/L-OP-19-stream-consumers-need-catchup-plus-idempotency.md` family).

In **default** T3 config (`configs/error_pager.yaml`), this code is
**allow-listed with `suppress: true`** — meaning the journal
forwarder drops the record before any T1 `notify()` call, and the
operator never sees it.

This runbook page exists for the case where the suppression has been
removed (e.g. an operator is investigating boot-time noise and
toggled suppression off) **or** the code appears outside the
expected first-60s-of-boot window — both of which mean the alert
has reached the operator and needs handling.

## Severity rationale

When suppressed (default): no severity, no alert.

When un-suppressed and emitted within 60s of daemon start:
treat as `WARN` (transient; will clear on retry). The forwarder
classifies via the `severity_map` so it actually arrives at
`DEGRADED` (`PRIORITY=3` default mapping). The implication is that
the operator is intentionally inspecting boot noise and will not
page on it.

When emitted **outside** the boot window (≥60s after daemon start)
that means the audit pool actually died — escalate to `CRITICAL` and
treat as a real outage. The forwarder does **not** auto-promote in
this case; the operator must read the timestamp.

## Immediate action

1. **Check elapsed time since daemon start:**

       systemctl --user show -p ActiveEnterTimestamp gerrit-jira-bridge

   * Within 60s? Allow-listed by design; re-enable suppression in
     `configs/error_pager.yaml` and `systemctl --user reload
     omnisight-journal-error-forwarder.service`. No further action.
   * Beyond 60s? Continue.

2. **Confirm the pool is actually broken** — the audit table should
   accept new rows:

       psql -h $PGHOST -U $PGUSER -d $PGDB \
            -c 'INSERT INTO audit_log (event, ts) VALUES ('"'"'probe'"'"', now());'

   If this fails, treat as if [`audit_write_failed`](audit_write_failed.md)
   had fired and follow that page.

3. If the probe succeeds, the bridge has a stale pool reference.
   Bouncing the daemon clears it:

       systemctl --user restart gerrit-jira-bridge

## Root-cause investigation

* **Why was suppression removed?** Check `git log` against
  `configs/error_pager.yaml`. If the change is recent and not
  associated with a deliberate investigation, revert it.
* **Pool exhaustion patterns** — see `docs/ops/observability_runbook.md`
  §pool. If pool size hits its hard cap and the bridge is the only
  consumer, raise the cap. If multiple consumers are competing, the
  bridge needs a scoped pool.
* **Crash patterns** — `journalctl --user -u gerrit-jira-bridge`
  immediately before the alert; look for connection-reset bursts.

## Escalation

* Boot-window emission with suppression on: not actionable. Don't
  page.
* Boot-window emission with suppression off: low-priority — the
  operator who toggled the suppression owns the cleanup.
* **Outside boot window**: escalate to bridge-maintainer immediately;
  audit-trail integrity is in question. Cross-link a JIRA ticket
  under `meta:op-721`.
