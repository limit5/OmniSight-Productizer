# Anthropic API Surface Drift — Operator Response Runbook

**Ticket family:** OP-1066 (SP-B-X-008 / C8) — pinned-tool synthetic canary.
**Audience:** on-call operator who just received an
`anthropic_api_schema_drift` DEGRADED alert.

## What the alert means

`scripts/anthropic-api-canary.py` runs hourly under
`anthropic-api-canary.timer`. Each run sends one minimal
`messages.create()` call against the Anthropic API using the pinned
built-in tool (`text_editor_20250728`), then diffs the response against
the invariants in
`tests/fixtures/anthropic-api-tool-schema-pinned.json`. On any drift it
fires `Severity.DEGRADED` via `operator_notifier.notify` and exits with
code 2.

Drift means the API shape no longer matches what the runner's
dispatcher expects. Common causes (rough order of likelihood):

- Anthropic shipped a non-backwards-compatible tool-block change.
- The pinned `anthropic` SDK was bumped underneath us and now sends a
  different request shape.
- New beta header required to keep the old shape.
- Operational fault (rate-limited / refused / model deprecation) showing
  up as a stop-reason outside the allowed set.

## Triage

### 1 · Confirm it's drift, not a one-off

```bash
systemctl --user status anthropic-api-canary.timer
tail -n 200 ~/work/sora/logs/anthropic-api-canary/canary.log
cd ~/work/sora/OmniSight-claude-worktree
python3 scripts/anthropic-api-canary.py ; echo "exit=$?"
```

- `exit=0` → transient. Acknowledge the alert; do NOT re-pin.
- `exit=2` → reproducible drift; continue.
- `exit=3` → env fault (missing `ANTHROPIC_API_KEY`, SDK not
  installed, fixture missing). Fix env and retry — not API drift.

### 2 · Decide: roll back vs re-pin

Drift is **not auto-healed**. Re-pinning requires an explicit operator
commit. Decision matrix:

| Symptom | Action |
| --- | --- |
| Anthropic SDK was bumped this week | Pin SDK back, file a follow-up. |
| New field appears, runner ignores it cleanly | Re-pin (add new required key), commit with rationale. |
| Existing field renamed/removed | Open BLOCKER; do NOT re-pin until runner is updated. |
| `stop_reason=refusal` or new policy stop | Open ticket against `anthropic_native_client.py` retry policy; re-pin once handled. |
| Transient 529 / 5xx | Do nothing; alert auto-acks after a clean tick. |

### 3 · Re-pinning procedure (if approved)

1. Edit `tests/fixtures/anthropic-api-tool-schema-pinned.json`. Update
   `pinned_at`, bump `schema_version` if non-cosmetic, adjust the
   invariants that drifted.
2. Update `pinned_against` to record SDK version + sample-response date.
3. Validate against a recorded response:
   ```bash
   python3 scripts/anthropic-api-canary.py \
       --dry-run-response /tmp/op1066-drift-payload.json
   echo "exit=$?"   # must be 0
   ```
4. Run the tests: `python3 -m pytest tests/test_anthropic_api_canary.py -v`.
5. Commit subject: `[OP-1066] Re-pin Anthropic API schema after
   <drift summary>`; push for Gerrit review. Reviewer confirms
   `tool_dispatcher.py` + `tool_schemas.py` actually tolerate the new
   shape.

### 4 · Acknowledge the alert

```python
from backend.agents.operator_notifier import acknowledge
acknowledge("<notification_id from alert>")
```

## Known false positives (NOT drift)

- `stop_reason=end_turn` with no `tool_use` block — model chose not to
  call the tool. The diff returns empty in this case (covered by
  `test_end_turn_without_tool_use_is_not_drift`).
- `ANTHROPIC_API_KEY` missing → exit 3, no alert.

## Related

- `scripts/anthropic-api-canary.py`
- `tests/test_anthropic_api_canary.py`
- `backend/agents/operator_notifier.py` (alert sink)
- CLAUDE.md L1 — unit MUST NOT hard-pin `ANTHROPIC_API_KEY`; this is
  enforced by `test_service_does_not_hard_pin_api_key_in_unit_file`.
