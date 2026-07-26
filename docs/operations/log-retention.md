# Log retention — bounding the append-mode sinks

**Ticket:** OP-2734 · **Scope label:** `scope:failed-units-2026-07-25`

## Why

The host filesystem hit 100% on 2026-07-20 and killed `pipeline-coordinator` with ENOSPC. These
log sinks were **not** the cause — 2-3 GB against a 1007 GB filesystem — but every one of them was
unbounded, which is its own defect, and the coordinator's decision-log had reached **1.4 GB**.

## `copytruncate` is mandatory, not a preference

Every rotated file here is a systemd `StandardOutput=append:` target, and the writing daemon holds
the file descriptor open for the life of the unit. A rename-based rotation would leave the daemon
writing into an unlinked inode: the log would look rotated while silently never growing again —
the same looks-fine-isn't failure this sweep exists to remove. `copytruncate` copies then truncates
in place, so the descriptor stays valid.

One consequence worth knowing: immediately after a rotation the live file is **sparse** — its
apparent size jumps back to the daemon's write offset while its actual block usage is near zero.
Judge these files with `du`, not `ls -l`.

## What is rotated, and what is pruned

| sink | policy |
|---|---|
| `logs/coordinator/systemd.log`, `logs/bridge/systemd.log`, `logs/release-milestone/staging-sync.log`, `logs/backup/prod-backup.log`, `logs/release-milestone/canary-status.jsonl` | logrotate, 20 MB, keep 5, compressed |
| `~/.local/state/omnisight-alerts/run.log` | logrotate, 5 MB, keep 3, compressed |
| `~/.config/omnisight/coordinator/decision-log/*.jsonl` | age prune, keep `DECISION_LOG_KEEP_DAYS` (14) |

The decision-log is a directory of dated day-files rather than one growing file, so it is pruned by
age instead of rotated. 14 days is deliberately well clear of what recovery needs: ADR-0021 §9 L6
replays only the **last 24 hours**. For scale, a single coordinator cold start writes ~8.7 MB of
`coordinator_source_event` records in one burst.

## Result of the first run — 2026-07-26

decision-log **1.4 GB → 241 MB** (52 files pruned), 1092 MB reclaimed overall, and the coordinator
kept writing throughout with an unchanged PID — the copytruncate correctness check.

## Install

```sh
install -m 700 -D scripts/omnisight-log-retention.sh ~/.local/bin/omnisight-log-retention.sh
install -m 644 -D deploy/logrotate/omnisight-logs.conf ~/.config/omnisight/logrotate.conf
install -m 644 deploy/systemd/omnisight-log-retention.service ~/.config/systemd/user/
install -m 644 deploy/systemd/omnisight-log-retention.timer   ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now omnisight-log-retention.timer
```

Daily at 05:15, failure routed to the JIRA alert channel (OP-2728). Dry-run with
`logrotate -d --state ~/.local/state/omnisight-logrotate.status ~/.config/omnisight/logrotate.conf`.

## Not covered here

Docker reclamation. ~85 GB is reclaimable at low risk — dominated by **34 stale GitLab CI runner
cache volumes totalling 69.5 GB, all created 2026-05/06** — but it is a separately approved
operation because it must protect the deployed digests and the documented rollback digests. Scoped
on OP-2734; nothing is executed by this timer.
