# `transient_5xx_retry_succeeded`

| field | value |
|-------|-------|
| Severity | _suppressed by default_ — when emitted, lands at `WARN` (priority-3 default) |
| Source | T3 forwarder — `scripts/journal_error_forwarder.py` (record originates anywhere the bridge wraps an HTTP retry loop) |
| Tier owner | bridge-maintainer (only when un-suppressed) |
| Parent META | OP-721 |

## What triggers it

The bridge's HTTP retry helper logs
`event="transient_5xx_retry_succeeded"` at level `ERROR` when an
HTTP 5xx was retried with exponential backoff and the **retry**
succeeded (so the operator sees the successful path, not a hard
failure).

This event is logged at `ERROR` purely so the metrics pipeline
sees it — paging on a successful retry is operationally noisy.
T3's `configs/error_pager.yaml` therefore allow-lists the code
with `suppress: true`, dropping the record before any T1
`notify()` call.

This runbook page is here for the case where the suppression has
been removed (intentionally, e.g. while diagnosing a flap) **or**
where the retry loop semantics change such that the code no longer
implies "succeeded" — both of which mean the alert is reaching the
operator and someone needs to know what to do.

## Severity rationale

When suppressed (default): no severity, no alert.

When un-suppressed: `WARN` — the underlying request **did**
succeed, so there is no immediate action. The alert is purely
diagnostic; treating it as `DEGRADED` would burn the operator out
on the first day (this code can fire dozens of times an hour during
a normal upstream blip).

If you genuinely care about retry rate, there is a Prometheus
counter (`omnisight_http_retry_total{result="success"}`) and a
Grafana panel — the operator-notifier path is the wrong tool.

## Immediate action

1. **Verify the alert is reaching you because someone removed
   suppression on purpose.** Check `git log
   configs/error_pager.yaml` for a recent change. If the change
   is associated with an active investigation (e.g. a comment
   citing a JIRA ticket), let it run; do not page.

2. **If suppression was removed by mistake**, revert and reload:

       systemctl --user reload omnisight-journal-error-forwarder.service

   The forwarder hot-reloads its config on the first journal
   record after reload (no restart, no log loss).

3. **If you cannot find the change** that removed suppression,
   re-add the entry yourself:

       # configs/error_pager.yaml — under allow_list:
       - code: transient_5xx_retry_succeeded
         suppress: true
         note: |
           Caller retries 5xx responses with exponential backoff and
           only raises (→ unsuppressed ERROR) if the retry budget
           was exhausted. This code is logged on the SUCCESSFUL
           retry path purely for observability; do not page on it.

## Root-cause investigation

You are almost certainly *not* root-causing
`transient_5xx_retry_succeeded` itself — the underlying upstream
blip is whatever the metrics dashboard shows. Useful things to
look at:

* `omnisight_http_retry_total{result="success"}` rate over the
  past hour (Grafana → bridge → retries).
* Per-host error breakdown: which upstream is flapping?
* Is the flap correlated with an Anthropic / OpenAI / Gerrit
  status incident?

If a real failure mode is hiding behind the retry — e.g. the
retry succeeds but at unacceptable latency cost — the right
response is to add a *new* code (e.g.
`transient_5xx_retry_latency_high`) at `WARN` with a budget, not
to un-suppress this one.

## Escalation

* Don't escalate. This code does not represent a failure. If you
  find yourself wanting to escalate, you probably want a different
  code; talk to bridge-maintainer about adding it.
