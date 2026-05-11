<!-- OP-907 — sample drift report generated as DoD evidence. -->
<!-- Real reports are written daily by cognee-drift-detect.timer -->
<!-- under docs/audit/cognee-drift-YYYY-MM-DD.md. -->

# Cognee ontology drift — 2026-05-11-sample

Generated: 2026-05-11T04:00:00+00:00

## Counts

- Stale entities: 2
- Missing entities: 1
- Schema mismatches: 1
- Total drift items: 4

## Stale entities (in KG, absent from reality)

Suggested action: Re-ingest the source corpus — KG holds an entity whose backing artefact is gone.

| Class | Key |
|---|---|
| PythonModule | backend.agents.deleted_module |
| JiraTicket | OP-9999 |

## Missing entities (in reality, absent from KG)

Suggested action: Investigate ECL pipeline — backing artefact exists but never landed in KG.

| Class | Key |
|---|---|
| PythonModule | backend.agents.new_module |

## Schema mismatches (class in KG, not in approved YAML)

Suggested action: Update `config/cognee_entity_classes.yaml` or rebuild KG with the approved schema.

| Class | Key |
|---|---|
| UnapprovedClass | UnapprovedClass |

## Allowlist

False-positives can be suppressed in `config/cognee_drift_ignore.yaml`. Add one entry per false-positive with `kind`, `class`, `key`, and a rationale comment. The detector reads the file on every run.
