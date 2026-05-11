# Cognee Ontology Governance

**Status**: Active as of 2026-05-11 (OP-903 — F5 initial ECL ingestion).
**Owner**: Operator + AI fleet collectively.
**Scope**: All entity classes the Cognee KG accepts, plus the
per-week proposal review loop. Architecture decisions (which corpus
to ingest, which backend implementation to use) live in their own
ADRs and are out of scope here.

## Why this exists

The Cognee KG is *derived* data — the source of truth is git + JIRA
+ docs, so it can be rebuilt at any time. But its ontology (the set
of entity classes it recognises) is *not* derived — it's a small
schema we maintain by hand. If the ingestion pipeline could grow
that schema unilaterally every time it saw an unfamiliar shape, the
KG would drift in a single run and queries would lose their meaning.

So the rule is: **the ontology can only grow through a two-step
review** — one LLM proposes, a different LLM (or vendor) reviews,
and the operator gives final approval at the weekly digest.

## Files

| Path | Role |
|---|---|
| `config/cognee_entity_classes.yaml` | The authoritative class list. Hand-edited only — never silently overwritten by the runner. |
| `backend/agents/ontology_proposal.py` | The two-step gate. Exposes `OntologyProposalGate`, `EntityClassProposalRejected`, `OntologyProposalRunaway`. |
| `scripts/cognee_initial_ingest.py` | The ECL bootstrap. Calls the gate when it hits an unknown class. |
| `var/cognee_bootstrap.ckpt.json` | Per-run checkpoint (every 100 entities). Re-runs read it for dedup. |

## Flow

```
unknown entity from extractor
        │
        ▼
┌─────────────────────────────┐
│ Proposer LLM                │  drafts EntityClassSpec
│ (e.g. Claude)               │  (name + source + description)
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ Reviewer LLM                │  accept / reject + rationale
│ (different vendor, e.g. GPT)│
└──────────┬──────────────────┘
           │
           ▼
   ┌───────┴─────────┐
   │                 │
   ▼ approved        ▼ rejected
   │                 │
   │                 └─→ drop entity + append to rejected_log
   │
   ▼
gate.pending_promotions
   │
   ▼ (weekly cron — see below)
operator-approval digest
   │
   ├─→ merge to YAML        (approved by operator)
   └─→ drop from queue      (rejected by operator)
```

## Weekly digest cron

A cron entry runs every Monday 09:00 local and posts the digest into
the operator review channel. The digest is built from
`gate.digest()` (which drains `pending_promotions`) joined against
the rejected_log for transparency.

The operator's decisions are applied by re-loading the YAML with
`load_ontology()`, adding the approved specs, and re-serialising via
`write_ontology()`. The pipeline does NOT auto-merge — keeping the
human in the loop is the whole point of the §4 gate.

### Cron stub

```cron
0 9 * * MON  /usr/bin/env python -m scripts.cognee_weekly_digest \
              --pending-from var/cognee_bootstrap.ckpt.json
```

The digest script itself is a thin wrapper over `gate.digest()` —
out of scope for this ticket (covered by F6, OP-906).

## Runaway guard

If the gate accepts more than **50 proposals in a single week**, it
raises `OntologyProposalRunaway` and halts the pipeline. This almost
always means one of:

- The extractor regressed and is now emitting noise as entities.
- The starter ontology in `cognee_entity_classes.yaml` was reverted
  or truncated, so common classes look "unknown" to the gate.
- A new corpus was added without updating the starter ontology.

Recovery: read the rejected_log + pending_promotions, diagnose the
root cause (NOT just bump the threshold), fix the underlying issue,
then re-run with `--from-scratch` if the checkpoint is poisoned.

## Error catalog (OP-903 §error catalog)

| Error | Cause | Recovery |
|---|---|---|
| `IngestionCheckpointCorrupted` | JSON checkpoint failed to parse | Restart from last good checkpoint, or `--from-scratch` if no recoverable file remains |
| `CogneeQueryDuringIngest` | KG was queried mid-ingest | Caller degrades to B8/B10 baseline path; resume on completion |
| `EntityClassProposalRejected` | Reviewer LLM rejected proposal | Drop the originating entity, log it; no halt |
| `OntologyProposalRunaway` | >50 proposals in a week | Halt; surface to operator for root-cause + reset |

## Manual ontology edits

If the operator wants to add a class directly (e.g. seeding a new
corpus type before any data lands), edit `cognee_entity_classes.yaml`
by hand. The schema is documented inline in the file's top comment.
After editing, bump the `last_reviewed` date so the weekly digest
can show "no change this week" cleanly.

Renames are NOT allowed without an explicit migration step — the KG
nodes carry the class label as a property, and a rename would orphan
every existing node. If a rename is genuinely needed, open a META
retrospective ticket per `docs/sop/jira-ticket-conventions.md` §14
and propose the migration plan there.

## Cross-references

- OP-903 — this ticket (F5 initial bootstrap).
- OP-852 — C3 Cognee adapter (implements the `CogneeBackend` protocol).
- OP-906 — F6 weekly digest cron (consumes `gate.digest()`).
- L-OP-870 — META.issuelinks check; applies during ingestion too.
- arXiv 2604.23090 — Multi-agent ontology generation (informed the dual-LLM design).
