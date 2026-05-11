# Anthropic Memory Tool runbook (OP-851 / C1)

**Status**: Standalone (no Managed Agents runtime). Spike-verified per
ticket OP-851 AC #2; spec source:
`docs/audit/2026-05-11-sprint-abc-master-plan.md` §3.2.

This runbook covers bring-up, daily operations, the tier filter, the
fallback path to B10 BM25 retrieval, and the kill-switch the operator
can use if Anthropic re-couples the tool to Managed Agents in a
future release.

---

## 1. What this is

The Anthropic Memory Tool (`memory_20260120`) is a client-side tool:
Anthropic emits `tool_use` blocks with one of six commands (`view`,
`create`, `str_replace`, `insert`, `delete`, `rename`); the OmniSight
runner implements storage on a per-fleet shared filesystem.

| Layer | Location | Source |
|---|---|---|
| API spec | `tools=[{"type": "memory_20260120", "name": "memory"}]` | `MEMORY_TOOL_SPEC` |
| Beta header | `anthropic-beta: managed-agents-2026-04-01` | `AnthropicClient(beta_headers=[...])` |
| Handler | `backend/agents/memory_tool_handler.py` | `MemoryToolHandler` |
| Storage | `/var/omnisight/memory/<fleet_id>/` | `OMNISIGHT_MEMORY_TOOL_ROOT` |
| Config | `config/memory_tool.yaml` | — |
| Audit log | `<worktree>/.runner/progress*.txt` (JSONL rows) | B9 progress.txt |

The Memory Tool is **separate** from the Cognee KG (C3) and B10 BM25
retrieval — Cognee is a structured retrieval index, Memory Tool is a
filesystem scratchpad. C2 (failure-class-indexed memory) layers on top
of the Memory Tool's `lesson:*` prefix.

## 2. Bring-up

1. Create the fleet root directory:
   ```bash
   sudo mkdir -p /var/omnisight/memory/<fleet_id>
   sudo chown -R omnisight:omnisight /var/omnisight/memory
   ```
2. (Optional) Override the default 100 MB cap:
   ```bash
   export OMNISIGHT_MEMORY_TOOL_CAP_MB=200
   ```
3. (Optional) Override the storage root (testing / multi-tenant):
   ```bash
   export OMNISIGHT_MEMORY_TOOL_ROOT=/srv/omnisight-mem
   ```
4. Start the SDK launcher as usual; it auto-binds the handler in
   `main_async()` via `bind_memory_tool(...)` and seeds the
   `docs/sop/lessons/L-*.md` archive on first start.

Verify the seed worked:

```python
from backend.agents.memory_tool_handler import (
    build_memory_tool_handler, list_seeded_lessons,
)
handler = build_memory_tool_handler(fleet_id="default")
print(list_seeded_lessons(handler))  # ["lesson:L-OP-...md", ...]
```

## 3. Tier filter (AC #6)

Each memory entry may carry a `tier:` label (S/M/L/X). Two ways to set
the label:

1. **YAML front-matter** (preferred — survives file moves):
   ```markdown
   ---
   tier: M
   ---
   # Body
   ```
2. **Filename prefix** (fallback — `tier-<X>-...`):
   ```
   tier-L-incident-postmortem.md
   ```

Recall rules:

| Tier | Auto-recall? | Operator action required |
|---|---|---|
| `S` | ✅ Yes | — |
| `M` | ✅ Yes | — |
| `L` | 🚧 Only when `OMNISIGHT_MEMORY_TIER_L_OPTIN=1` | Set the env var in the runner systemd unit |
| `X` | ❌ Never auto-recalled | Operator must export the entry out-of-band |

Entries without a label default to **S** (lessons-learned baseline —
safe to share across the fleet).

When the runner asks for a tier-restricted entry it doesn't have
permission to read, the handler emits one `tier_refuse` audit row and
returns a `tier_violation_unauthorized_recall` error tool_result so
the model can self-correct.

## 4. Eviction policy (AC #4)

Cap defaults to **100 MB per fleet** (overridable via
`OMNISIGHT_MEMORY_TOOL_CAP_MB`).

Algorithm: oldest-first by `st_mtime`. Triggered on every `create` /
`str_replace` / `insert` whose net byte delta would push total usage
past the cap. Each evicted file emits one `op=evict` audit row.

Edge case: a **single write** larger than the cap returns
`memory_storage_full` immediately (no partial write, no eviction
attempted) — operators should bump the cap rather than splitting the
file.

## 5. Audit log (AC #5)

Every operation appends one JSONL row to the per-ticket
`progress.txt`. Schema:

```json
{
  "type": "memory_tool",
  "tool": "memory",
  "op": "read|write|delete|list|rename|evict|tier_refuse|error:<cmd>",
  "key": "/memories/<rel-path>",
  "timestamp": "2026-05-11T12:00:00+00:00",
  "ticket_key": "OP-851"
}
```

This row shape multiplexes with B3 ToM scratchpad rows (`type =
tom_scratchpad`) so B9 can read both streams from the same file.

Quick filter — only memory rows for one ticket:

```bash
grep '"type": "memory_tool"' <worktree>/.runner/progress*.txt | jq -c
```

## 6. Standalone spike (AC #2) and kill-switch

The Memory Tool is **standalone** — the `managed-agents-2026-04-01`
header is a feature flag, not a runtime gate (verified against the
Anthropic public docs at `platform.claude.com/docs/.../memory-tool`).

If a future Anthropic release re-couples the two, set the kill-switch
to force the runner to fall back to B10 BM25 retrieval:

```bash
export OMNISIGHT_MEMORY_TOOL_REQUIRES_MA=1
```

The `verify_standalone()` check raises `MemoryToolNotStandalone` when
the kill-switch is set; `build_memory_tool_handler(...)` then returns
`None`, and the launcher prints
`[C1] Memory Tool spike failed — falling back to B10 BM25 only`. The
model can still call `Read` / `Grep` etc. — only the memory_20260120
tool surface is removed.

## 7. Fallback to B10 BM25

When `MemoryToolUnavailable` surfaces (filesystem error, kill-switch,
or unhandled exception in the handler), the launcher's lesson
retrieval path (`_build_lesson_system_prompt`) is still wired via
Cognee → BM25 fallback. The model loses the live read/write surface
but keeps the read-only prior-lessons context.

## 8. Backup + corruption recovery

* Memory storage is durable filesystem — daily snapshot is the
  recovery boundary.
* Schedule a cron job (pattern from OP-798 / OP-837):
  ```cron
  15 3 * * * /usr/local/bin/snapshot-omnisight-memory /var/omnisight/memory /backup/omnisight-memory
  ```
* On `memory_corrupted` (file unreadable / non-utf-8), restore the
  affected file from the latest snapshot. The handler emits one
  `error:view` audit row with `error: memory_corrupted` so the
  operator can `grep` for incidents.

## 9. Operator checklist

* [ ] Storage root exists with correct ownership.
* [ ] Cap is set in line with disk headroom (default 100 MB).
* [ ] Tier-L opt-in is enabled **only** for fleets that need it.
* [ ] Daily snapshot cron is scheduled.
* [ ] `progress.txt` rotation is wired (B9 — log volume scales with
  tool-call rate).
* [ ] Spike kill-switch is reachable in the runbook so an on-call
  engineer can flip it during a vendor incident.
