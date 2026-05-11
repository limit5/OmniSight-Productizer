# Cognee ontology drift detection runbook

**Status**: Active as of 2026-05-11 (OP-907 — F9).
**Owner**: Backend / KG operator.
**Scope**: Daily read-only audit of the Cognee KG vs. ground-truth
artefacts; allowlist management; escalation handling.

## Why this exists

The Cognee KG is *derived* data: git, JIRA and the curated entity-class
YAML are the source of truth. Over time the KG can drift in three
distinct ways:

1. **Stale entity** — a `PythonModule` (or `JiraTicket`) entity stays in
   the KG after its backing artefact was deleted, because the
   nightly rebuild's idempotent `add()` does not implicitly retract.
2. **Missing entity** — a backing artefact exists but never made it into
   the KG (ingestion miss; e.g. the `cognee_initial_ingest.py`
   walker skipped it due to a parse error, or the rebuild was
   short-circuited by a typed failure mid-run).
3. **Schema mismatch** — the KG holds a node label that is not in
   `config/cognee_entity_classes.yaml`. Per the ontology governance
   rule (`docs/operations/cognee-ontology-governance.md`), classes
   may only grow through the weekly digest, so any silently added
   label is a contract violation.

Catching these early matters because Cognee queries are class-aware:
once the schema diverges from the YAML, downstream callers
(`build_repo_map_via_cognee`, `retrieve_lessons_via_cognee`,
`backend.agents.scheduler` retrieval helpers) may either miss real
hits (missing entity) or return ghosts (stale entity).

## How it runs

| Surface | Path |
|---|---|
| Script | `scripts/cognee_drift_detect.py` |
| systemd service | `deploy/systemd/cognee-drift-detect.service` |
| systemd timer | `deploy/systemd/cognee-drift-detect.timer` (04:00 UTC daily) |
| Approved schema | `config/cognee_entity_classes.yaml` |
| Per-entity allowlist | `config/cognee_drift_ignore.yaml` |
| State file | `var/cognee_drift_state.json` |
| Audit reports | `docs/audit/cognee-drift-YYYY-MM-DD.md` |
| Logs | `/var/log/omnisight/cognee-drift-detect.log` |

### Daily flow

```
04:00 UTC   →  cognee-drift-detect.timer fires
            →  cognee-drift-detect.service runs the script
            →  script queries Neo4j for (labels, key) of every node
            →  script collects reality (YAML, git ls-files, JIRA JQL)
            →  diff → stale / missing / schema-mismatch sets
            →  write docs/audit/cognee-drift-<today>.md
            →  reconcile state file (first_seen per drift item)
            →  items >7d old → escalate via OP-721 notifier
```

The 04:00 UTC slot is one hour after the 03:00 UTC
`cognee-nightly-rebuild.timer`. The hour-of-slack is intentional —
the rebuild can run long on a cold cache, and the detector reading
mid-rebuild would produce spurious "stale" hits as the writer
retracted and re-added nodes.

## Operator playbook

### A. The daily report shows new drift

1. Open the latest `docs/audit/cognee-drift-YYYY-MM-DD.md`.
2. For each row, follow the kind-specific action:
   * **Stale** — locate the source artefact (`backend/agents/X.py`,
     ticket `OP-XYZ`). If it was deleted on purpose, leave alone —
     the next full rebuild will retract it. If the rebuild is not
     retracting (the report shows the same entry tomorrow), run
     `python -m scripts.cognee_full_rebuild --repo-root /opt/omnisight`
     manually; if that still doesn't clear it, escalate to OP-852
     adapter author.
   * **Missing** — inspect why `cognee_initial_ingest.py` skipped the
     artefact. Common causes: AST parse error (look for
     `python_ast_skip` in the rebuild log), ECL pipeline raised
     `CogneeIndexCorruption` mid-run, ingestion adapter typed-error.
     Fix the root cause and re-run the rebuild.
   * **Schema mismatch** — a new class label appeared. Either
     a) update `config/cognee_entity_classes.yaml` if the class is
     correct (after the weekly digest review), or b) find the
     pipeline path that wrote the label and constrain it back to
     the approved set.
3. Drift items that you've intentionally triaged should be moved to
   `config/cognee_drift_ignore.yaml` with a rationale comment.

### B. The 7-day escalation paged

The OP-721 notifier delivered a `cognee_drift_unresolved_7d` event.
Acknowledge in JIRA, then follow §A for each unresolved row. The
escalation stops re-firing once the drift either disappears from the
report or moves into the allowlist.

If the notifier itself is wedged (`escalation_bridge_down` log line),
the detector falls back to email using the same SMTP env vars as
`backend.agents.operator_notifier`. Reach for the runbook of OP-722
to fix the bridge; the daily reports keep landing on disk regardless.

### C. False-positive — add to allowlist

Edit `config/cognee_drift_ignore.yaml`, append:

```yaml
- kind: stale            # or missing | schema
  class: PythonModule
  key: backend.agents.experiment_only
  rationale: |
    Spike retained in KG pending OP-NNNN. Acknowledged 2026-05-11.
```

Commit through the standard Gerrit flow (OP-855 / `non-ai-reviewer`
+2 required). The file is read on every run, so the next 04:00 UTC
detect picks it up — no service restart needed.

### D. Detector exit codes

| Exit | Meaning | Operator action |
|---|---|---|
| 0 | Run completed (drift may exist — read the report) | Inspect report; archive if no drift. |
| 1 | Unexpected fatal error | Read stack trace in the .log file; file a runner ticket. |
| 2 | `DriftDetectKGUnreachable` — Neo4j refused | Verify Neo4j health (`scripts/cognee_healthcheck.py`); cron retries tomorrow. |

### E. Skipping a day

If the operator needs to suppress a single run (e.g. Neo4j is being
re-provisioned), the systemd timer can be stopped:

```
sudo systemctl stop cognee-drift-detect.timer
# work on Neo4j ...
sudo systemctl start cognee-drift-detect.timer
```

`Persistent=true` on the timer means a host that was off at 04:00
will run the audit on next boot — there's no need to manually
"backfill" missed days.

### F. Manual invocation (debugging)

```
# Default (writes report under docs/audit/ + checks JIRA + escalates):
python -m scripts.cognee_drift_detect --repo-root /opt/omnisight

# Offline (no JIRA + no escalation), report to /tmp:
python -m scripts.cognee_drift_detect \
    --repo-root /opt/omnisight \
    --out-dir /tmp/drift \
    --no-jira --no-escalation
```

The `--no-escalation` flag is the right tool when reproducing a
historic state for forensics: drift items are still written into the
state file's `first_seen` map but the notifier is never invoked.

## Error catalog

* **`DriftDetectKGUnreachable`** — Neo4j refused the snapshot query.
  Behaviour: exit 2, no report written, cron retries tomorrow.
* **`DriftAuditWriteFailed`** — could not write
  `docs/audit/cognee-drift-*.md`. Behaviour: report falls through to
  stdout (visible in journald + the .log file) and the rest of the
  pipeline continues so escalation still works.
* **`EscalationBridgeDown`** — `operator_notifier.notify` raised.
  Behaviour: degrade to a direct SMTP email; if that also fails, log
  `email_fallback_failed` and continue (the audit report on disk is
  still the canonical record).

## Tuning

| Env var / flag | Meaning | Default |
|---|---|---|
| `--jira-window-days` | "recently merged" lookback for JIRA reality | 30 |
| `--no-jira` | skip JIRA dimension entirely | false |
| `--no-escalation` | never call the OP-721 notifier | false |
| `OMNISIGHT_COGNEE_NEO4J_*` | Neo4j connection — same as the rest of the Cognee stack | see `backend.agents.cognee_integration.CogneeConfig` |
| `OMNISIGHT_NOTIFIER_*` | OP-721 channels — see `operator_notifier.py` | see `backend.agents.operator_notifier` |

`ESCALATION_AGE_DAYS = 7` is a constant in the script — change it
only if you have a documented operator agreement to do so (the spec
hard-codes 7 days per AC #5).

## Related docs

* `docs/operations/cognee-runbook.md` — Cognee stack operations
* `docs/operations/cognee-ontology-governance.md` — ontology growth
  governance (the schema part of drift)
* `config/cognee_entity_classes.yaml` — approved class list
* OP-852, OP-899, OP-903 — F2 / F4 / F5 ingestion lineage
* OP-721 (META) — operator notification bridge
