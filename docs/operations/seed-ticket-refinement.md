# Seed-ticket refinement (OP-781)

This runbook covers the operator workflow for un-pausing the ~509 placeholder
JIRA tickets that were bulk-paused on 2026-05-08 with the
`runner-needs-refinement` label after the OP-231 21-cycle infinite-revert
loop traced back to a `_(operator: refine before pickup — TODO.md source line
is the seed)_` description.

The helper at `scripts/refine_seed_tickets.py` reduces the manual cleanup cost
from `500 × 15 min ≈ 125 h` to `500 × 30 sec review ≈ 4 h`, plus a one-shot
~$10 LLM spend on Anthropic Haiku.

## Three-stage workflow

```
                                     ┌──── operator review ────┐
                                     ▼                         │
1. propose ─→ data/refine-proposals/OP-XXX.json ─→ 2. accept / reject
                                                                │
                                     ┌──── one approved────────┘
                                     ▼
                              3. apply ─→ JIRA: Task → Story
                                          + label flip
                                          + audit comment
```

### Stage 1: `--propose`

```bash
python3 scripts/refine_seed_tickets.py --propose --limit 50 --dry-run
python3 scripts/refine_seed_tickets.py --propose --limit 50
python3 scripts/refine_seed_tickets.py --propose          # full sweep (≈500)
```

For each ticket carrying `runner-needs-refinement`:

1. Fetch description + parent (Wave) summary + parent description excerpt.
2. Send to Anthropic Haiku (`claude-haiku-4-5-20251001`) with the prompt at
   `scripts/refine_seed_tickets.py::PROMPT_SYSTEM`.
3. Parse JSON response into `Proposal` dataclass.
4. Auto-classify confidence (`high` / `medium` / `low`):
   - `high`: summary or description contains a recognisable keyword
     (e.g. `add aria-label`, `alembic migration`, `extract module`).
   - `low`: spec-alignment one-liners (`與 ISO 26262`), explicit research
     verbs (`investigate`, `research`), or summary + description both very
     short with no anchor.
   - `medium`: everything else — operator should glance at every one.
5. Save `data/refine-proposals/OP-XXX.json` (gitignored — `data/` is in
   `.gitignore`).
6. Append one JSONL line to `data/refine-proposals/audit.log`:
   `{"ts":..., "operator":..., "action":"propose", "key":..., "token_usage":...}`.
7. Optional: pass `--post-comment` to also leave a `[ai-proposal]` comment
   on the ticket so it shows up in the operator's JIRA inbox.

The cost report is printed at the end of the run:

```
Cost report
  Tickets proposed: 50
  Total input tokens : 23,400
  Total output tokens: 18,750
  Total cost (Haiku) : $0.0937
  Avg cost / ticket  : $0.0019
```

The OP-781 spec budgets ~$0.02/ticket × 500 ≈ $10. Real runs vary: tickets
with longer parent context push input tokens up; tickets that fit on one line
land closer to $0.001.

### Stage 2: `--review`

```bash
python3 scripts/refine_seed_tickets.py --review OP-231              # inspect
python3 scripts/refine_seed_tickets.py --review OP-231 --accept     # approve
python3 scripts/refine_seed_tickets.py --review OP-231 --reject     # discard
```

Default mode prints a side-by-side dump of the original placeholder
description and the LLM proposal. `--accept` flips the JSON file's
`status` to `accepted` (gating Stage 3); `--reject` flips it to `rejected`.

For high-confidence tickets you can shell-loop the bulk path:

```bash
for k in $(jq -r '.key' data/refine-proposals/*.json | grep ...); do
  python3 scripts/refine_seed_tickets.py --review "$k" --accept
done
```

### Stage 3: `--apply`

```bash
python3 scripts/refine_seed_tickets.py --apply OP-231 --dry-run   # preview
python3 scripts/refine_seed_tickets.py --apply OP-231             # live
```

For each accepted proposal:

1. Render new description (Goal preserved verbatim from the placeholder body
   so the LLM cannot rewrite operator intent — only AC / Files / Prereqs are
   proposal-driven).
2. PUT description.
3. Change `issuetype` Task → Story (re-eligible for runner pickup, JQL
   excludes Task).
4. Remove `runner-needs-refinement` label.
5. Add `refined-by:ai-assisted` label (audit trail; JQL filter includes
   `labels != "refined-by:ai-assisted"` so re-runs don't re-propose).
6. Post `[ai-refined]` comment with operator + timestamp + token usage.
7. Append `{"action":"apply", ...}` line to `data/refine-proposals/audit.log`.

`--apply` refuses to run on a proposal whose `status` is not `accepted`. Use
`--review --accept` first; pass `--force` only if you intentionally need to
re-apply an already-applied proposal (e.g. after a manual JIRA edit
reverted the change).

## Confidence-tier guidance

| Tier   | Operator action                            |
| ------ | ------------------------------------------ |
| high   | Glance, accept-then-apply in batch         |
| medium | Read both columns; accept the AC verbatim  |
| low    | Refine manually — LLM proposal is a draft  |

Low-confidence proposals on spec-alignment one-liners (`與 ISO 26262`,
`research X`, `evaluate options`) typically need human refinement; the LLM
fabricates plausible-sounding ACs but the underlying Goal is genuinely
ambiguous. Reject those and rewrite the description by hand.

## Audit trail

`data/refine-proposals/audit.log` is a JSONL ledger:

```json
{"ts":"2026-05-08T14:00:00+00:00","operator":"sora@...","action":"propose","key":"OP-231","token_usage":{"input_tokens":234,"output_tokens":167},"cost_usd":0.000857,"confidence":"medium"}
{"ts":"2026-05-08T14:30:00+00:00","operator":"sora@...","action":"apply","key":"OP-231","token_usage":{"input_tokens":234,"output_tokens":167},"cost_usd":0.000857,"confidence":"medium","model":"claude-haiku-4-5-20251001"}
```

Use `jq` for quick aggregates:

```bash
# Total cost so far
jq -s 'map(.cost_usd) | add' data/refine-proposals/audit.log

# Per-action counts
jq -r .action data/refine-proposals/audit.log | sort | uniq -c

# Confidence distribution of applied refinements
jq -r 'select(.action=="apply") | .confidence' data/refine-proposals/audit.log | sort | uniq -c
```

## Failure modes and recovery

- **LLM JSON parse fails** — `Proposal.notes` records `"LLM JSON parse failed: ..."`,
  `confidence` is forced to `low`, `proposed_acceptance_criteria` is empty.
  Operator must re-propose with `--overwrite` (often a model retry succeeds)
  or refine manually.
- **JIRA label PUT fails mid-apply** — JIRA writes are idempotent (each
  uses `_request_idempotent` from `backend/agents/jira_dispatch.py`), so a
  re-run of `--apply --force` is safe.
- **Ticket already refined** — `--apply` reads the proposal status; an
  `applied` proposal is a no-op without `--force`. The runner JQL filter
  also excludes `refined-by:ai-assisted`, so an accidental double-propose
  on an already-refined ticket simply finds nothing.
- **Cost overrun** — abort the batch with Ctrl-C; partial proposals already
  saved are committed. The cost report prints incrementally so you can stop
  early.

## Pairs with

- **OP-720** Phase 1 (loop guard — runner-side defense): kicks in when a
  refined ticket re-loops, but the goal of OP-781 is for refined tickets
  to *not* loop in the first place.
- **OP-737** (ticket creation helper): prevents NEW placeholders from
  reaching JIRA. OP-781 is the historical-cleanup analogue.
