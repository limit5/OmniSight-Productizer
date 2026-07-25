---
id: L-OP-2728
ticket: OP-2728
title: An alerting artefact is proven only by a deliberately induced failure
date: 2026-07-25
tags: [devops, observability, ci]
---

# An alerting artefact is proven only by a deliberately induced failure

**Situation**: A sweep of failed systemd units on 2026-07-25 found three long-running silent
failures — `pipeline-coordinator` down 5 days after a host ENOSPC, `omnisight-prod-backup` down
3 nights, `staging-sync` failed 10,093 consecutive times and `staging-pg-snapshot` 75/75 without
ever once succeeding. None of them notified anyone. Investigating why produced **four distinct
alerting mechanisms that all appeared configured and had never delivered a single alert**:
(1) `deployment-audit-alert.service` and `staging-gate-alert.service` only `printf` a JSON line
to the journal; (2) `alertmanager` is defined in `docker-compose.prod.yml` but sits behind
`profiles: ["observability"]` and is not running; (3) two Prometheus rule files (`alerts.yml`,
`frontend-freshness-alerts.yml`) sit in the mounted `obs-rules` directory but are never loaded,
because `rule_files` in `prometheus.yml` is an **explicit single-entry list, not a glob** —
`/api/v1/rules` reports exactly one loaded group; (4) `pipeline-coordinator.service` and its
watchdog had **no `OnFailure=` at all**, and the watchdog's `bash -lc` runs under `set -euo
pipefail`, so when its `systemctl restart` returns non-zero under start-limit it exits in ~21 ms
— it dies precisely when it is needed. Every one of these passes a config review by inspection.

**Fix**: Built one channel with a proven delivery path (`scripts/omnisight-alert-notify.py`,
systemd `OnFailure=` → JIRA, one open issue per source, throttled comments) and wired it to the
units whose silence mattered. Its acceptance was not "the unit exists" but an **induced failure**:
`systemd-run --user --unit=alert-selftest-1 --property=OnFailure=omnisight-alert@alert-selftest-1.service
/bin/false`, followed by a second induced failure to prove idempotency and a third with the
throttle disabled to prove the comment path.

**Verification**: The induced-failure run created exactly one issue; the second fired inside the
throttle window and created and commented nothing; the third added a comment to the *same* issue.
The issue count for the source's label never left 1. The host-disk floor was proven the same way,
by forcing `DISK_WARN_PCT=70` against a 75% disk so the alert had to fire, then reverting to the
real 85% threshold and confirming it goes quiet. Test artefacts were closed afterwards and the
throttle stamps cleared.

**Generalisation**: Treat "an alert is configured" as an unverified claim until a deliberately
induced failure has been observed arriving at the destination. Two corollaries that caught real
bugs here: (a) never assume a config directory is glob-loaded — check what the process actually
loaded (`/api/v1/rules`, `systemctl show -p OnFailure`), because a file in the right directory
that nothing reads looks identical to a working one; (b) the alert handler itself must fail
closed and loudly — it must never raise, never return non-zero, and never be the only thing
watching itself. A monitored gate that reports OK while not doing its job is worse than a red
one, because it actively consumes the attention that would otherwise have found the problem.
