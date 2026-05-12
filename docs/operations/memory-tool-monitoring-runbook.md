# Memory Tool monitoring runbook (OP-908 / F10)

## Purpose

`scripts/memory_tool_monitor.py` runs hourly against:

```text
/var/omnisight/memory/<fleet>/
```

It records per-fleet byte usage, file count, cap percentage, threshold action,
and stale-file candidates for the F15 operator dashboard ingestion path.

## Install

```bash
sudo cp deploy/systemd/memory-tool-monitor.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memory-tool-monitor.timer
```

The timer uses `OnCalendar=hourly` and `Persistent=true`, so a missed hour runs
after the host comes back.

## Configuration

Defaults:

```text
OMNISIGHT_MEMORY_TOOL_ROOT=/var/omnisight/memory
OMNISIGHT_MEMORY_TOOL_FLEETS="claude codex merger"
OMNISIGHT_MEMORY_TOOL_CAP_MB=100
OMNISIGHT_MEMORY_TOOL_METRICS=/var/omnisight/memory/metrics.jsonl
```

For a non-standard audit log, edit the service `--audit-log` argument. The
monitor reads C1 `op=evict` and `MemoryCapExceeded` rows. Those rows include
`fleet`, so cap-scaling proposals are per fleet.

## Thresholds

| Cap usage | Action |
|---|---|
| `< 70%` | `ok` |
| `70% <= pct < 90%` | `warn` log |
| `90% <= pct < 100%` | `page_operator` log |
| `>= 100%` | `evict_required` log; C1 write-path eviction handles deletion |

Monitoring itself is read-only. It never deletes memory files.

## Metrics contract for F15

Each hourly run appends one JSONL row per fleet to:

```text
/var/omnisight/memory/metrics.jsonl
```

Example:

```json
{"type":"memory_tool_monitor","op":"scan","fleet":"codex","bytes":73400320,"file_count":44,"cap_bytes":104857600,"pct_cap":70.0,"action":"warn","stale_files":[],"timestamp":"2026-05-12T00:00:00Z"}
```

F15 should ingest this file directly for the operator dashboard. `stale_files`
contains model-visible paths, unread days, and byte size.

## Cap proposals

Daily cap scaling is proposal-only. If one fleet has more than five eviction
triggers on each of the previous three UTC days, the monitor appends a 10 MB
step proposal to:

```text
docs/audit/memory-cap-proposals.md
```

Duplicate proposals for the same fleet inside 24 hours are skipped and logged
as `CapAutoScaleProposalConflict`. Operators approve by editing deployment
configuration; declining a proposal requires no code change.

## Stale-file detection

Files with `st_atime` older than 30 days are flagged as proactive eviction
candidates. To suppress a known false positive, add either the relative path or
the model-visible `/memories/...` path to an allowlist file and pass:

```text
--stale-allowlist /etc/omnisight/memory-stale-allowlist.txt
```

Inline overrides are also supported through:

```text
OMNISIGHT_MEMORY_STALE_ALLOWLIST="/memories/keep.md,folder/keep-too.md"
```

Use this for the `StaleFileDetectFalsePositive` error catalog case.

## Errors

| Error | Meaning | Recovery |
|---|---|---|
| `MonitorReadFailed` | A fleet directory could not be read. | Check mount, owner, mode, and retry next hourly run. |
| `CapAutoScaleProposalConflict` | A same-fleet proposal already exists within 24 hours. | No action unless the operator wants to approve the existing proposal. |
| `StaleFileDetectFalsePositive` | A file is intentionally unread but should not be flagged. | Add it to the stale-file allowlist. |

## Verification

Manual scan:

```bash
python3 scripts/memory_tool_monitor.py \
  --root /var/omnisight/memory \
  --audit-log /var/omnisight/progress.txt
```

Timer status:

```bash
systemctl status memory-tool-monitor.timer
journalctl -u memory-tool-monitor.service -n 50
tail -n 3 /var/omnisight/memory/metrics.jsonl
```
