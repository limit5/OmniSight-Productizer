# Cross-task awareness operator runbook

**Owner:** Sprint F operator  
**Ticket:** OP-913 / F15  
**Surface:** `components/omnisight/admin/CrossTaskAwarenessPanel.tsx`

This runbook covers the dashboard that joins Sprint F awareness signals:
F10 Memory Tool usage, F9 Cognee KG drift, F11 agent drift trends, F14
project-state SLOs, and the F7 project-state injection feature flag.

## 0. Pre-flight

- [ ] D12 feature flag row exists for `OMNISIGHT_PROJECT_STATE_INJECT`.
- [ ] F10 memory monitor writes `/var/omnisight/memory/metrics.jsonl`.
- [ ] F9 Cognee drift detector writes the latest drift report.
- [ ] F11 agent drift report has a current monthly report or explicitly
      reports insufficient baseline data.
- [ ] F14 `/api/v1/project-state/metrics?limit=50` returns traces and
      cache stats for an admin session.

## 1. Onboarding New Fleet

1. Add the fleet name to `OMNISIGHT_MEMORY_TOOL_FLEETS` for the F10
   monitor.
2. Create the fleet memory directory under `/var/omnisight/memory/<fleet>/`
   with the same ownership as the existing fleet directories.
3. Run one manual monitor scan:

   ```bash
   python3 scripts/memory_tool_monitor.py \
     --root /var/omnisight/memory \
     --metrics /var/omnisight/memory/metrics.jsonl
   ```

4. Refresh the dashboard and confirm the fleet appears in the Memory
   Tool tile with `action=ok`, file count, cap bytes, and timestamp.
5. If the fleet starts at `warn` or worse, reduce retained scratch files
   before enabling unattended runner pickup for that fleet.

## 2. Responding To Drift Alerts

1. Open the Cognee KG tile and note the `kind`, `class_name`, `key`, and
   age for each alert.
2. For `stale`, confirm the backing artefact was intentionally deleted.
   If not, run the F9 detector manually and compare the latest report.
3. For `missing`, inspect the ingestion logs for parser skips or typed
   Cognee failures, then re-run the full rebuild after fixing the root
   cause.
4. For `schema`, stop promotion until the ontology governance digest has
   reviewed the proposed class. Do not add labels directly from the KG.
5. Suppress only confirmed false positives in
   `config/cognee_drift_ignore.yaml` with a rationale and date.

## 3. Reading Agent Drift Trend

The Agent drift tile compares the current rolling 30-day window with the
prior 30-day window.

- `success_delta` below `-10%` after baseline establishment is paging
  severity.
- `time_delta` above `+50%` means the agent class is getting slower and
  should be inspected before assigning larger tickets.
- `lessons_delta` below `-30%` means pickups are using fewer recorded
  lessons and may be losing cross-task context.
- During the first two months, treat alerts as baseline calibration
  unless they coincide with project-state SLO breaches or a canary
  rollback.

## 4. Manual Feature Flag Flip

The dashboard's F7 toggle calls the D12 admin API:

```text
PATCH /api/v1/feature-flags/OMNISIGHT_PROJECT_STATE_INJECT
{"state":"enabled"|"disabled"}
```

Use the dashboard first because it preserves the operator confirmation
state and surfaces `ToggleAuthRefused` immediately. If the dashboard is
unavailable, use the existing `/admin/feature-flags` page or an
authenticated curl with the same PATCH body. After a manual flip, refresh
the Cross-task awareness panel and verify the flag badge shows the new
state.

## 5. Emergency Rollback Procedure

1. Disable `OMNISIGHT_PROJECT_STATE_INJECT` through the F15 dashboard
   toggle.
2. If the toggle reports `ToggleAuthRefused`, sign in again and retry.
3. If D12 is unavailable, follow the Sprint F canary runbook's emergency
   D12 rollback path and record the incident ticket.
4. Restart runner processes only if the feature flag cache does not
   invalidate within the documented 30-second window.
5. Confirm F14 project-state SLO returns to `ok` and the last 10 queries
   no longer show budget exceedances.

## 6. Dashboard Error Catalog

| Error | Dashboard behavior | Operator action |
| --- | --- | --- |
| `DashboardSSEDisconnect` | Shows stale-data banner and keeps the last API-rendered snapshot. | Wait for auto-reconnect or press Refresh. Investigate `/events` only if it persists across reloads. |
| `ToggleAuthRefused` | Redirects to login because the operator session lost admin auth. | Sign in, return to the dashboard, and repeat the confirmed toggle. |

## 7. UI State Machines

### SSE reconnect

```text
live -> disconnected -> stale banner
stale banner -> dashboard SSE event or heartbeat -> refetch
refetch success -> live
refetch failure -> stale banner + fetch error
```

### Toggle confirmation

```text
idle -> Toggle click -> confirm
confirm -> Confirm enabled/disabled -> saving
saving -> PATCH success -> idle with new state
saving -> ToggleAuthRefused -> login redirect
saving -> PATCH failure -> confirm with row error
```

## 8. Recovery / Rollback

The UI is rendered from API snapshots and keeps no durable state. There is
nothing to recover in the browser beyond reloading the page. Operational
rollback is the F7 flag disable path in §5.

## 9. Dry-run Checklist

- [ ] Dashboard loads memory usage, Cognee status, agent drift, SLO, and
      last project-state queries from an API-rendered snapshot.
- [ ] SSE disconnect banner appears when the stream drops and clears on
      the next successful refresh.
- [ ] Feature flag toggle requires confirmation and updates the displayed
      D12 state.
- [ ] Cognee drift pagination is usable with more than five drift rows.
- [ ] Emergency rollback path has been walked by an operator in a dry-run.
