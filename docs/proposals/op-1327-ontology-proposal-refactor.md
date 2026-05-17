# OP-1327 ontology proposal refactor proposal

## Context

`backend/agents/ontology_proposal.py` owns the small two-step gate used when
Cognee ingestion encounters an entity class that is not already present in
`config/cognee_entity_classes.yaml`. The module is intentionally narrow:
`load_ontology()` and `write_ontology()` translate the YAML registry to and
from `EntityClassSpec`, while `OntologyProposalGate.propose()` enforces the
weekly runaway ceiling, invokes the proposer/reviewer callables, and records
accepted or rejected decisions.

Two small refactors would reduce coupling inside the module without changing
the public dataclasses, exceptions, or gate behavior:

1. Extract YAML row and payload conversion into private pure helpers. Today
   `load_ontology()` mixes file existence, YAML parsing, row defaulting, and
   `EntityClassSpec` construction, while `write_ontology()` mixes payload
   assembly with file output. Helpers such as `_spec_from_yaml_row(row)` and
   `_ontology_payload(classes, version, reviewed_date)` would keep the public
   functions focused on I/O and make the registry shape easier to review.
2. Extract the stateful decision bookkeeping from
   `OntologyProposalGate.propose()` into private helpers. The method currently
   handles proposal counting, already-known no-op decisions, reviewer calls,
   accepted-promotion logging, rejected-decision logging, and rejection raising
   in one flow. Helpers such as `_reserve_proposal_slot()`,
   `_known_class_decision(spec)`, and `_record_review_decision(decision)` would
   keep `propose()` focused on the gate sequence while preserving the existing
   pending-promotion and rejected-log side effects.

## Non-goals

- No behavior change to runaway threshold enforcement, increment timing,
  exception types, exception messages, logging names, or digest queue clearing.
- No public API change to `EntityClassSpec`, `ProposalDecision`,
  `OntologyProposalGate`, `load_ontology()`, `write_ontology()`, or exported
  constants.
- No change to the YAML registry format, default `source` / `description` /
  `examples` handling, class-name sorting, or `last_reviewed` format.
- No change to proposer/reviewer callable signatures or production LLM wiring.
- No database, devops, embedded, frontend, security, tooling, or production
  dependency changes.
- No code or test changes in this proposal patch set.

## Follow-up implementation ticket draft

Summary: Refactor ontology proposal YAML helpers and gate decision flow

Areas: backend, tests

Tier: S

Description:

Implement the two OP-1327 proposal refactors in
`backend/agents/ontology_proposal.py`:

- Add private pure helpers for YAML row-to-`EntityClassSpec` conversion and
  ontology payload assembly, then use them from `load_ontology()` and
  `write_ontology()` without changing the public file I/O behavior.
- Add private helpers around `OntologyProposalGate.propose()` for proposal-slot
  reservation, already-known no-op decision construction, and accepted/rejected
  decision recording.
- Preserve the current proposal counter increment timing, runaway exception
  behavior, already-known reviewer skip, pending-promotion append, rejected-log
  append, log messages, and `EntityClassProposalRejected` behavior.
- Add focused tests under `backend/tests/test_ontology_proposal.py` only if the
  helper extraction needs additional coverage for preserved YAML conversion or
  gate side effects.

Acceptance criteria:

1. Existing public imports from `backend.agents.ontology_proposal` remain valid.
2. `load_ontology()` and `write_ontology()` preserve the current YAML registry
   shape, class-name sorting, field defaults, and `last_reviewed` date format.
3. `OntologyProposalGate.propose()` preserves current behavior for runaway,
   already-known, approved, and rejected proposal paths.
4. `backend/.venv/bin/pytest backend/tests/test_ontology_proposal.py` passes.

Filed follow-up: pending operator/JIRA creation. This pickup is restricted to
`jira_dispatch.add_comment()` / `transition_back_to_todo()` for programmatic
JIRA writes, and the repository does not expose a general follow-up ticket
creation helper in that allowed set.
