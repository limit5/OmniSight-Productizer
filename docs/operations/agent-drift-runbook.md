---
audience: operator
ticket: OP-909
---

# Agent Drift Runbook

## What This Measures

`runner_metrics` records one row per runner pickup. The start hook inserts
agent identity and ticket shape; the completion hook updates the same row
with outcome and elapsed seconds. Writes are fail-open: a telemetry failure
logs `MetricsInsertFailed` and the runner continues.

Tracked fields:

- `agent_class`, `instance_id`, `ticket_key`
- `ticket_type`, `tier`, `area`
- `time_to_complete_seconds`, `outcome`
- `lessons_used_count`, `mcp_calls_count`, `claude_model_used`
- `ts`, `started_at`, `completed_at`

## Monthly Report

Run manually:

```bash
OMNISIGHT_DATABASE_URL=postgresql://... \
  scripts/agent_drift_report.py
```

The script writes:

```text
docs/audit/agent-drift-YYYY-MM.md
```

It compares the last 30 days against the prior 30 days for each
`(agent_class, ticket_type)` bucket:

- average time to complete
- success rate
- average lessons used

If there is less than 30 days of data, it exits with
`MonthlyReportInsufficientData` and does not write a misleading trend.

## Thresholds

| Signal | Threshold | Action |
|---|---:|---|
| Success rate | drop greater than 10% MoM | page operator after baseline |
| Time to complete | increase greater than 50% MoM | warn |
| Lessons used | drop greater than 30% MoM | warn |

During the first two months of metrics, success-rate paging is suppressed
to warning level (`AlertThresholdTunable`) so the baseline can settle.

## Systemd Timer

Install as a user timer on the runner host:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/agent-drift-monthly.service ~/.config/systemd/user/
cp deploy/systemd/agent-drift-monthly.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-drift-monthly.timer
```

Check status:

```bash
systemctl --user status agent-drift-monthly.timer
journalctl --user -u agent-drift-monthly.service -n 100
```

## Recovery

Telemetry is fire-and-forget. If a pickup or completion write fails,
only that row is lost. Fix the database or migration state and let the
next runner pickup repopulate data.

Monthly reports are derived from `runner_metrics`; delete and rerun the
script to regenerate a report after backfills or threshold tuning.
