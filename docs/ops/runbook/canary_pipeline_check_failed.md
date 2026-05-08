# `canary_pipeline_check_failed`

| field | value |
|-------|-------|
| Severity | `DEGRADED` |
| Source | T4 canary — `scripts/canary_pipeline.py` (constant `CANARY_EVENT_DEGRADED`) |
| Tier owner | bridge-maintainer + gerrit-admin |
| Parent META | OP-721 (closes the OP-708 "synthetic curl passed but real webhooks didn't" gap) |

## What triggers it

The systemd timer `omnisight-canary.timer` runs once an hour. Each
fire-step does a real end-to-end push through the Gerrit pipeline:

1. push a WIP+private change to `refs/for/develop` with
   `hashtag=canary` and a subject without any `[OP-NNN]` key (so
   JIRA dispatch ignores it);
2. poll the bridge's structured log for
   `event="proactive_merger_thread_spawned"` referencing the new
   change number;
3. abandon the change at the end of the run.

If step 2 does not surface the expected event within the poll
budget, the canary exits `rc=2`, writes a structured DEGRADED
JSON record under the event name `canary_pipeline_check_failed`,
and `OnFailure=` chains to `omnisight-canary-alert.service` —
which calls T1 with this code at severity `DEGRADED`.

The canary deliberately uses a real push (not a synthetic curl)
because OP-708 was the case where a synthetic probe passed even
though webhooks were broken. Anything short of "Gerrit hook saw
my push and the bridge reacted" is incomplete coverage.

## Severity rationale

`DEGRADED` rather than `CRITICAL` because:

* the canary itself can flake on transient network blips during the
  push step;
* a single failure does not prove a real outage — needs ≥2 hourly
  failures in a row to confirm;
* the underlying outage paths
  ([`gerrit_client_ssh_failed`](gerrit_client_ssh_failed.md),
  [`daemon_silent`](daemon_silent.md)) page at higher severity from
  inside the bridge itself.

If two consecutive canary runs fail, treat as `CRITICAL` manually
and escalate.

## Immediate action

1. **Re-run the canary by hand** to differentiate transient from
   real:

       systemctl --user start omnisight-canary.service
       journalctl --user -u omnisight-canary.service \
           --since '1 min ago' | tail -20

   If this run succeeds → previous failure was transient; close
   the alert and move on.

2. **If the manual run also fails**, isolate which stage broke:

   * Push step (`gerrit push -o ...`) failed → SSH transport is
     out; cross-link [`gerrit_client_ssh_failed`](gerrit_client_ssh_failed.md).
   * Push succeeded but the change number doesn't appear in the
     bridge log → webhooks plugin is broken. Check Gerrit's
     `refs/meta/config` against the deploy fixture (drift would
     have been caught by T5 [`refs_meta_config_drift`](refs_meta_config_drift.md)
     on the next daily scan, but the canary catches it first).
   * Bridge log empty for the change number after 60s → bridge is
     up but its event loop is hung; cross-link
     [`daemon_silent`](daemon_silent.md).

3. **Clean up orphan canary changes.** If a canary push succeeded
   but the abandon step did not run, you'll have a lingering WIP
   change. List + abandon them:

       ssh -p 29418 ... gerrit query --format=JSON \
           "is:open hashtag:canary owner:claude-bot" \
           | jq -r '.number? // empty' | while read n; do
               ssh -p 29418 ... gerrit review --abandon "$n,1"
           done

## Root-cause investigation

| Symptom | Likely cause | Reference |
|---------|--------------|-----------|
| Push succeeds, no bridge event | Gerrit `webhooks` plugin not delivering | `docs/ops/observability_runbook.md` §webhooks |
| Push fails on auth | SSH key / `claude-bot` account problem | [`gerrit_client_ssh_failed`](gerrit_client_ssh_failed.md) |
| Push fails on network | Tailscale / firewall regression | `docs/ops/multi-wsl-deployment.md` |
| Bridge sees event but no `proactive_merger_thread_spawned` | Bridge classifier bug | filed against bridge-maintainer |

The full operator SOP — including config knobs and the alerter
contract — is in `docs/sop/canary-pipeline.md` (devops-area
companion to this page).

## Escalation

* Single failure → close after confirming the next hourly run is
  clean.
* Two consecutive failures → escalate to gerrit-admin **and**
  bridge-maintainer; pause canary timer
  (`systemctl --user stop omnisight-canary.timer`) only if it is
  itself the cause of noise (rare).
* Sustained failure ≥3h → assume Track C is silently down; force
  a full bridge restart and open a postmortem
  (`docs/retrospectives/YYYY-MM-DD-canary-outage.md`).
