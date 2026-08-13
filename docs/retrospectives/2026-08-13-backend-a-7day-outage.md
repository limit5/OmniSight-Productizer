# Retrospective: backend-a 7-day outage (2026-08-06 → 2026-08-13)

META ticket: OP-2769 (`meta:retrospective`) · remediation: OP-2768 · squatter
lesson: `docs/sop/lessons/L-OP-2768-wsl-shared-netns-port-owner.md`

## Summary

Prod `backend-a` went down 2026-08-06 12:32 and stayed down for **seven days**
while prod served from `backend-b` alone. Nothing alerted, although Prometheus
recorded `up{instance="backend-a:8000"}==0` the entire time. Recovery on
2026-08-13 then had to fight through **five stacked defects**, each of which
had independently failed silent. Total service impact: no external outage
(backend-b held), but zero redundancy for a week, S3 off-site backups withheld
for 3 days, KEK-escrow checks failing, and the frontend + installer containers
were found not running at all.

## Timeline (all times +08)

| when | what | evidence |
|---|---|---|
| 08-06 12:32 | backend-a `up` 1→0, never returns | Prometheus 14d range: single transition |
| 08-06→08-13 | 7 days single-replica; daily 02:4x `docker exec backend-a` failures (kek-escrow) and 11:00 DLP-scan failures | dockerd journal; `omnisight-pgdump-s3-daily.log` |
| 08-11 onward | S3 lane withholds uploads: DLP verdict `unusable` (scanner runs inside backend-a) — **fail-closed, correct**; alert channel comments daily on OP-2765 | lane log; alert journal |
| 08-13 03:29 | WSL force-kill: `systemctl poweroff did not terminate in 10000 ms, calling reboot(RB_POWER_OFF)` | boot -1 journal tail |
| 08-13 04:41 | dockerd restore loses ~28 sandbox/endpoint store entries; backend-a starts into a **netless sandbox** (Networks={}), exit-2 crash-loop ×352 (restart never rebuilds endpoints) | dockerd `Failed to restore endpoint … Key not found in store`; `docker inspect` |
| 08-13 04:38–04:42 | staging-compose dies <1s (user unit raced system dockerd); prod-compose `up -d --no-recreate` adopts the broken container, waits, fails — no OnFailure anywhere | user journals |
| 08-13 10:39 | `--force-recreate` rebuilds backend-a correctly; start blocked: `bind 0.0.0.0:8000: address already in use` | compose output |
| 08-13 ~12:5x | squatter identified **outside Linux**: Docker Desktop (Windows) running Portainer publishing `0.0.0.0:8000`, visible in-distro as an ownerless socket (shared WSL netns) | Windows `netstat -ano` + `tasklist` |
| 08-13 13:1x | Portainer stopped (+`--restart=no`); backend-a healthy; `up==1`; frontend + installer recovered by the compose-unit start; S3 lane: DLP **pass** + upload OK; freshness green | this repo's OP-2768 trail |

## The five layers

0. **Origin (unexplained, evidence destroyed).** Why backend-a stopped on
   08-06 is unknowable: journald held only ~2 boots. Retention was the direct
   casualty → four-piece #4.
1. **Every WSL shutdown is effectively unclean.** 30+ containers cannot stop
   inside WSL's 10s ceiling → `RB_POWER_OFF` → dockerd's libnetwork store
   corrupts. Trigger, not fixable locally; downstream layers must absorb it.
2. **Netless-sandbox boot race.** Containers auto-started while endpoint
   restore/cleanup is still running can come up with zero networks, and
   restart-policy restarts never repair it (victim selection is random —
   backend-b survived because its stale sandbox got cleaned first) →
   four-piece #1 (reconciler).
3. **Repair layers all failed silent.** `up -d --no-recreate` adopts but
   cannot repair; user units cannot `After=` the system docker.service, so
   bring-up raced the daemon; neither compose unit had `OnFailure=` →
   four-piece #2.
4. **Monitoring existed but had no path.** The `up==0` signal sat in
   Prometheus for 7 days (no Alertmanager, no rule, nothing reading it) →
   four-piece #3. The port-8000 squatter then blocked recovery → lesson doc.

## What held

- **Fail-closed DLP** refused to ship unscanned dumps and said exactly why —
  and correctly distinguished `unusable` (scanner absent) from `blocked`.
- **The OP-2728 JIRA alert channel** delivered daily (OP-2765); the gap was
  coverage (nothing watched `up`), not delivery.
- **Prometheus data quality**: the 14-day `up` history pinned the outage start
  to the minute, a week later.
- backend-b + pg-ha carried prod alone without incident.

## What was missing (now shipped as OP-2768)

Reconciler for the netless signature; docker-wait + OnFailure on both compose
units; a replica-up watcher on the proven channel; 60-day journal retention.

## Open items

- Staging `.env` still carries the prod Anthropic key (leg-2 calibration
  residue) — env-contract guard now fails staging-compose loudly at each boot
  until a staging key is minted or the residue is removed (operator decision).
- Host port assignments span Windows + all WSL distros as one namespace; a
  lightweight port registry for the fleet (incl. Docker Desktop) would prevent
  the squatter class.
- Boot -1 (08-10) shows no run of prod-compose at all — why the user manager
  skipped it that boot is still unexplained.
