# Memory Tool storage runbook (OP-900 / F2)

## Purpose

The Memory Tool stores scratchpad files on the host filesystem under:

```text
/var/omnisight/memory/
```

Canonical project state remains in git and JIRA. This directory is
durable scratch space only; losing it costs each fleet up to its local
memory cap and does not lose canonical data.

## Layout

Provisioning creates one root and three fleet directories:

```text
/var/omnisight/memory/          mode 0750
/var/omnisight/memory/claude/   mode 0750
/var/omnisight/memory/codex/    mode 0750
/var/omnisight/memory/merger/   mode 0750
```

The backend service user owns all four directories. Production hosts
normally use `omnisight:omnisight`; override with
`OMNISIGHT_BACKEND_SERVICE_USER` and `OMNISIGHT_BACKEND_SERVICE_GROUP`
when the service runs under a different account.

## Provisioning

Run provisioning as root on each backend host:

```bash
sudo OMNISIGHT_BACKEND_SERVICE_USER=omnisight \
  OMNISIGHT_BACKEND_SERVICE_GROUP=omnisight \
  scripts/provision_memory_tool_storage.sh
```

The script is idempotent. It recreates missing directories, restores
mode `0750`, and refuses unknown fleet names.

For non-standard roots:

```bash
sudo OMNISIGHT_MEMORY_TOOL_ROOT=/srv/omnisight/memory \
  scripts/provision_memory_tool_storage.sh
```

## Runtime configuration

Set these environment variables on both backend and runner services:

```text
OMNISIGHT_MEMORY_TOOL_ROOT=/var/omnisight/memory
OMNISIGHT_MEMORY_TOOL_CAP_MB=100
```

`deploy/systemd/omnisight-backend.service`,
`deploy/systemd/runner-claude@.service`, and
`deploy/systemd/runner-codex@.service` set those defaults. The backend
systemd unit also grants `ReadWritePaths=/var/omnisight/memory` because
`ProtectSystem=strict` is enabled.

## Cap and eviction

Each fleet has a 100 MB soft cap by default. The handler reads the cap
from `OMNISIGHT_MEMORY_TOOL_CAP_MB`.

On `create`, `str_replace`, or `insert`, the handler checks:

```text
current fleet bytes + incoming byte delta <= cap
```

If the write would exceed the cap, the handler emits a
`MemoryCapExceeded` audit row and evicts oldest-mtime files until the
write fits. Each deleted file emits one `op=evict` JSONL audit row. The
eviction loop preserves the newest three final files: the incoming file
plus the newest two existing files.

If a single inbound write is larger than the cap, or if the handler
cannot get under cap without deleting the newest three files, the
operation returns `memory_storage_full`.

## Error catalog

| Error | Meaning | Operator action |
|---|---|---|
| `memory_dir_not_writable` | Backend cannot create or write the fleet dir at startup. | Re-run provisioning; check owner, mode, and systemd `ReadWritePaths`. |
| `MemoryCapExceeded` | A write crossed the soft cap and eviction started. | Check recent `op=evict` rows and adjust cap if eviction churns. |
| `memory_storage_full` | Eviction could not free enough room while preserving newest three files. | Increase `OMNISIGHT_MEMORY_TOOL_CAP_MB` or manually prune fleet files. |
| `AuditWriteFailed` | Audit JSONL append failed. | Memory operation succeeds; inspect logs and restore progress path writability. |

## Monitoring

Install the hourly monitor:

```bash
sudo cp deploy/systemd/memory-tool-storage-monitor.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memory-tool-storage-monitor.timer
```

The timer runs hourly and appends one JSONL row per fleet to:

```text
/var/omnisight/memory/usage.jsonl
```

Rows have this shape:

```json
{"type":"memory_tool_storage","op":"du","fleet":"codex","path":"/var/omnisight/memory/codex","human":"12M","bytes":12582912,"timestamp":"2026-05-11T00:00:00Z"}
```

F15 can ingest this file as the operator-dashboard dependency stub.

## Verification

Directory ownership and modes:

```bash
stat -c '%U:%G %a %n' /var/omnisight/memory /var/omnisight/memory/{claude,codex,merger}
```

Timer status:

```bash
systemctl status memory-tool-storage-monitor.timer
journalctl -u memory-tool-storage-monitor.service -n 50
tail -n 3 /var/omnisight/memory/usage.jsonl
```

Eviction audit rows:

```bash
grep '"op": "evict"\|"op":"evict"' <worktree>/.runner/progress*.txt
```

## Recovery

If one fleet directory is corrupted, remove only that fleet:

```bash
sudo rm -rf /var/omnisight/memory/<fleet>
sudo scripts/provision_memory_tool_storage.sh
```

The next runner pickup rebuilds seeded lessons from
`docs/sop/lessons/L-*.md`. Manually authored scratchpad files are lost
for that fleet only.
