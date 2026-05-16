# G.A-v0 Governance Kernel — Operator Reference

**Status**: Shipped (OP-1048 family A1–A8, 2026-05-13).
**Scope**: Minimum viable kernel that unblocks S12 31.A filing. Full
schema + plugin infrastructure is G.A-v1 (post-rc2).
**Spec**: `docs/sprint-s12/sprint-s12g-governance-engine-spec.md` §5.
**ADRs**: ADR-0033 (kernel surface), ADR-0034 §1 (override audit trail).

## What shipped

| Piece                              | File                                                          | Built in |
| ---------------------------------- | ------------------------------------------------------------- | -------- |
| v0 Pydantic ticket contract        | `governance_engine/schema/v0.py`                              | A1       |
| Forbidden-combinations (10 rules)  | `governance_engine/schema/forbidden_combinations.py`          | A2       |
| Golden 31.A-1a fixture             | `governance_engine/tests/fixtures/31a_1a_apt_base_tools.yaml` | A3       |
| Kernel unit tests                  | `governance_engine/tests/test_v0_kernel.py`                   | A4       |
| L3 runner refusal                  | `backend/agents/jira_dispatch.py::_runner_refuses_pickup`     | A5       |
| Manual override CLI                | `scripts/governance/manual-override.py`                       | A7       |
| Integration proof (this ticket)    | `governance_engine/tests/test_ga_v0_integration.py`           | A8       |

## Flow of a 31.A ticket through the kernel

```
payload (YAML / dict)
   │
   ▼  TicketContract.model_validate(payload)        # A1 schema gate
   │  raises ValidationError on shape / enum / missing-field issues
   ▼  validate_forbidden_combinations(contract)     # A2 cross-field rules
   │  returns [] when clean; list[ForbiddenCombinationError] otherwise
   ▼  jira_dispatch._runner_refuses_pickup(labels)  # A5 first gate
   │  silent refusal on class:operator-window-* / class:operator-rehearsal
   ▼  ticket is L3-eligible
```

Forbidden-combination rule IDs (1–10) are documented in
`governance_engine/schema/forbidden_combinations.py` — each `_rule_NN_*`
function carries its own one-line `rule_name` and `detail`. The same
rule_id is what the override CLI records when an operator bypasses it.

## L3 runner refusal contract (A5)

`_runner_refuses_pickup(labels)` is the FIRST gate in pickup preflight.
A label whose prefix is `class:operator-window-` or whose value is
`class:operator-rehearsal` causes silent refusal: no JIRA comment, only
a structured `runner_refusal_by_class` log event. Co-present
`class:subscription-*` labels do not override the refusal —
operator-window-* wins.

## Manual override CLI (A7)

Lets an L1 operator record a justified override of one of the 10
forbidden-combination rules. The audit trail is the source of truth for
"did this ticket bypass governance, and why?".

### Invocation

```bash
scripts/governance/manual-override.py apply \
    --ticket OP-1234 \
    --rule-id 3 \
    --reason "Operator-window-top batch predates G.A-v0" \
    --l1-fingerprint "$(< ~/.config/omnisight/governance-l1-fingerprint)"
```

The CLI rejects unless `--l1-fingerprint` matches the stored fingerprint
at `~/.config/omnisight/governance-l1-fingerprint` byte-for-byte.
`--rule-id` must be 1–10; `--reason` 1–500 chars; `--ticket` must
match `OP-\d+`.

### Exit codes

| Code | Meaning                                      |
| ---- | -------------------------------------------- |
|    0 | Override recorded                            |
|    1 | Usage error                                  |
|    2 | L1 fingerprint file missing                  |
|    3 | `--l1-fingerprint` does not match expected   |
|    4 | Validation failure (ticket/rule-id/reason)   |

### Audit log

**Location**: `~/.config/omnisight/governance-overrides/overrides.jsonl`
(append-only, mode 0600, one JSON record per line; parent dir 0700).

Each record has exactly these fields:

```json
{
  "ts_utc": "2026-05-13T12:34:56.789012Z",
  "ticket": "OP-1234",
  "rule_id": 3,
  "reason": "<operator-supplied>",
  "l1_fingerprint_hash": "<sha256 hex of the raw fingerprint>",
  "operator_uid": "<os.getlogin / pwd lookup>",
  "hostname": "<socket.gethostname()>"
}
```

The raw fingerprint is never stored — only its sha256.

### Self-test

```bash
scripts/governance/manual-override.py self-test
```

End-to-end against `/tmp/test-overrides.jsonl`: missing fingerprint →
exit 2, mismatch → exit 3, success → exit 0 + mode-0600 record on disk,
validation failure → exit 4. Cleans up its own scratch files.

## Out of scope (deferred)

11 phase-plugin schemas → G.B. JSON Schema export, preflight library,
roster signature verification → G.A-v1. Full review-window override
flow → G.C.

## Health check

```bash
pytest governance_engine/tests/test_v0_kernel.py -v
pytest governance_engine/tests/test_ga_v0_integration.py -v
pytest backend/tests/test_jira_dispatch_authority_refusal.py -v
scripts/governance/manual-override.py self-test
```

All four MUST succeed before filing any 31.A ticket batch.
