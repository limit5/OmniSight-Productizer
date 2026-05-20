# 2026-05-13 Deployment Audit Baseline

Ticket: OP-1016 / AUDIT-29a-1

## Scope

This baseline closes the four fatal `expected=yes` red rows from the AUDIT-29
deployment audit harness. The command remains:

```bash
scripts/deployment-audit.sh
```

The daily systemd timer is the host equivalent of cron expression
`0 7 * * *` and runs:

```text
deployment-audit.timer -> deployment-audit.service
```

The service appends structured JSONL rows to the existing release-conductor
audit surface:

```text
/home/user/work/sora/logs/release-conductor/audit.jsonl
```

## Closed Fatal Rows

| Row | 2026-05-12 fatal condition | 2026-05-20 closure |
|---|---|---|
| `release-milestone-checker.timer` | Timer was installed later but the service result was red. | Unit now runs without the staging `/audit/verify` preflight dependency and the latest service result is `success`. |
| `auto-promote-develop.timer` | Timer/service was not live in the baseline run. | Missing log directory was provisioned and the service result is now `success`; the timer remains enabled. |
| `OMNISIGHT_DATABASE_URL@auto-promote-develop.service` | The audit required a live PID and failed for the oneshot service. | The audit now verifies systemd `Environment` / `EnvironmentFile` configuration for oneshot services; `OMNISIGHT_DATABASE_URL` is configured via the host env file. |
| `alembic-head auto` | The audit ran Alembic from repo root and could not read the backend config. | The audit now runs Alembic from `backend/` and resolves the systemd-visible Alembic executable; prod DB reports `current=0245 == repo head`. |

## Verification

Latest host run:

```text
deployment-audit (OP-976 / AUDIT-23) — host=X870E-NOVA-WIFI user=user bus=--user date=2026-05-20T06:32:43Z
summary: 6 green · 4 red · 0 warn · 0 red-with-expected=yes (fatal)
RESULT: PASS — all expected-live artefacts confirmed (gated/warn rows are informational).
```

Daily timer state:

```text
deployment-audit.timer
OnCalendar=*-*-* 07:00:00
NextElapseUSecRealtime=Thu 2026-05-21 07:00:00 CST
```

Latest JSONL audit row:

```json
{"event":"deployment_audit","fatal_red":0,"result":"PASS","source":"scripts/deployment-audit.sh","timestamp":"2026-05-20T06:32:44.452539Z"}
```

## Remaining Non-Fatal Reds

The following rows remain red but are not fatal because the manifest marks them
`n-a` or `gated`:

- `auto-promote-main.service` (`expected=n-a`)
- `staging@http://localhost:8010/healthz` (`expected=gated`)
- `staging-gate-canary.timer` (`expected=gated`)
- `staging-gate-smoke.timer` (`expected=gated`)

## Soak Status

The ">=3 consecutive days of clean daily-audit cron output" AC cannot be fully
verified on 2026-05-20 because the corrected daily timer has only one clean
OP-1016 JSONL row so far. The timer is registered for the next daily run, so
the remaining evidence is time-gated rather than code-gated.
