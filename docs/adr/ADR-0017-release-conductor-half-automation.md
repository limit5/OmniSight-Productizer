---
id: ADR-0017
title: Release conductor half-automation
status: Proposed
date: 2026-05-12
---

# ADR 0017 - Release conductor half-automation

**Status**: Proposed (2026-05-12, Sprint G / OP-944)

**Decider**: sora (operator) + AI fleet

**Related**:
- OP-937 / G1 - release template engine.
- OP-938 / G2 - idempotency guard for release META creation.
- OP-939 / G3 - daily release-conductor cron skeleton.
- OP-940 / G4 - operator override labels.
- OP-941 / G5 - hotfix conductor variant.
- OP-942 / G6 - notification fan-out.
- OP-943 / G7 - pending releases dashboard.
- OP-944 / G8 - this ADR, Sprint G retrospective, and lesson.
- [ADR-0018 - Event-driven release pipeline](ADR-0018-event-driven-release-pipeline.md) - Sprint H event-driven successor.
- [`docs/operations/release-conductor-runbook.md`](../operations/release-conductor-runbook.md) - canonical operator runbook for the G1-G7 system.

## Context

Before Sprint G, production release work was coordinated by the operator
reading JIRA, running scripts by hand, and remembering which release step
unblocked the next one. That worked while release cadence was low, but it
made the release process hard to audit and hard for runner agents to join.

The project already had mature primitives:

- JIRA tickets with `Blocks` links.
- Gerrit review and submit gates.
- runner pickup discipline through the JIRA workflow.
- release, deploy, smoke, and notification scripts.
- lessons from L-OP-870 and L-OP-922 about sibling order and dependency
  direction.

Sprint G chose not to replace those primitives with a new conductor
service. Instead, it made the JIRA graph the conductor and added automation
around the edges. The release is represented as one `RELEASE-vX.Y.Z` META
plus a serial chain of release children. The runner already knows how to
respect `blockedBy`; the release conductor makes that graph easy to create,
safe to re-run, visible to operators, and connected to cron, hotfix, and
notification paths.

The originally referenced spec path,
`docs/operations/release-conductor-pattern.md`, is not present in this
worktree as of 2026-05-12. The current local source of truth is
`docs/operations/release-conductor-runbook.md`, with implementation
contracts in `config/release_template.yaml`,
`scripts/instantiate_release_meta.py`, `scripts/release_conductor_cron.sh`,
and the release-conductor backend modules delivered by later siblings.

## Decision

Adopt **half-automation** for the release conductor:

1. **JIRA graph remains the source of truth.**
   A release is one META plus children linked by `blockedBy`. The runner
   advances only when the JIRA graph says the next child is eligible.

2. **Automation creates and observes the graph, but does not outrank it.**
   Scripts may instantiate METAs, scan fixVersions, trigger hotfix METAs,
   notify operators, and expose pending-release state. They do not invent
   an independent release state machine that can disagree with JIRA.

3. **Cron is a pickup accelerator, not an autonomous release authority.**
   The daily cron checks for SemVer fixVersions, verifies milestone
   acceptance, confirms no existing release META, and then calls the G1
   template engine. Operator labels may skip or force creation, but those
   overrides are recorded in audit output.

4. **Hotfix flow mirrors release flow.**
   Hotfix automation creates `HOTFIX-vX.Y.Z+N` METAs from labelled Gerrit
   changes, using the same idempotency and audit principles as the release
   path.

5. **Notification and dashboard surfaces are read models.**
   G6 notification fan-out and G7 pending-release state make transitions
   visible. They must not become second sources of truth.

6. **Sprint H may become event-driven only after Sprint G is stable.**
   ADR-0018 builds on this decision by replacing polling latency with
   webhook and internal-event wakeups. That is a scheduler improvement on
   top of the JIRA graph, not a replacement for the graph.

## Design

### G1 - Template engine

G1 created the canonical `RELEASE-vX.Y.Z` template:

- exactly one META ticket per version,
- 13 children `R1` through `R13`,
- a strict default chain `R2` blocked by `R1`, `R3` blocked by `R2`, and
  so on,
- one `Relates` link from each child back to the META,
- a generated operator comment with the resolved `R-id -> OP-key` map.

The template deliberately keeps the release sequence visible in JIRA. That
means operators can debug a release by looking at the same object the runner
uses for pickup.

### G2 - Idempotency guard

G2 extended G1 so repeated runs cannot create duplicate release or hotfix
METAs. The guard checks JIRA for existing version labels before creating
anything and fails closed on ambiguous or failed lookup state.

The recovery rule is intentionally narrow: `--force` is allowed only when
the previous META is Archived. For any live status, the script refuses with
`MetaAlreadyExists`.

### G3 - Daily cron

G3 added the daily release-conductor cron:

- enumerate recent SemVer fixVersions,
- skip when a matching release META already exists,
- run the OP-868 milestone acceptance check,
- invoke the G1 template engine only when acceptance is green,
- back off and notify after repeated acceptance failures.

This gives the release system a scheduled trigger while preserving the
operator's ability to inspect and correct inputs.

### G4 - Operator override labels

G4 added two explicit operator controls:

| Label | Effect |
| --- | --- |
| `release:skip-auto-conductor` | cron ignores the fixVersion |
| `release:force-create` | cron creates the META despite a failed acceptance check, while recording an override warning |

The labels are mutually exclusive. Conflict or malformed labels fail closed
and alert the operator. The decision is that override intent must be visible
in JIRA and audit logs rather than hidden in shell state.

### G5 - Hotfix conductor variant

G5 applied the same conductor pattern to hotfixes. A merged Gerrit change
labelled for a hotfix target can instantiate a `HOTFIX-vX.Y.Z+N` META. The
hotfix path is intentionally smaller than the full release path, but it
shares the same properties: SemVer-like target validation, idempotent META
creation, and audit output.

### G6 - Notification fan-out

G6 connected release-child transitions to Slack and email notification
routes. Notifications are best-effort read models over release state. Bridge
failure is logged but does not block a state transition.

This keeps the conductor's safety property: release progress is determined
by JIRA and review gates, not by the availability of a notification channel.

### G7 - Pending releases dashboard

G7 exposed pending releases through a read-only API and dashboard tab. The
dashboard aggregates current release-state rows, state-machine status, and
human-readable next action.

The dashboard is an operator visibility layer. It does not allow release
mutation and does not bypass the JIRA graph.

### G8 - Close the Sprint G design loop

G8 records the design decision, retrospective, and reusable lesson before
Sprint H event-driven work begins. This is part of the architecture: the
system should not move from poll-driven half-automation to event-driven
automation without writing down what the poll-driven layer guarantees.

## Invariants

1. **No duplicate live release META per version.**
   If JIRA lookup is unavailable or ambiguous, creation fails closed.

2. **JIRA `Blocks` direction is load-bearing.**
   The template engine must use the existing helper that wires
   inward=blocker and outward=blocked. This carries forward L-OP-874.

3. **`Relates` links are provenance, not gates.**
   They connect children to the META but do not affect runner pickup.

4. **Operator overrides are explicit and audited.**
   Override labels live on the fixVersion surface and must result in audit
   events. Hidden shell-only overrides are not part of the contract.

5. **Read models do not mutate release authority.**
   Notification and dashboard paths observe release state. They do not mark
   children done, close METAs, or decide release readiness.

6. **Event-driven follow-up must degrade to the cron path.**
   ADR-0018 may reduce latency, but a missed webhook must not stall release
   progress. Polling remains the fallback consistency path.

## Alternatives Evaluated

### A. Fully manual release checklist - rejected

Manual release execution has the lowest implementation cost, but it keeps
release safety in the operator's memory. It also gives runner agents no
machine-readable state to respect. This was rejected because release
sequence, blockers, and audit evidence need to be visible in JIRA.

### B. Build a standalone conductor service - rejected for Sprint G

A standalone service could own its own database and scheduler, but Sprint G
already had a working dependency graph in JIRA and a runner that respects
it. Creating another authority would introduce reconciliation problems
before the project had proven the simpler graph-driven path.

### C. Event-driven conductor first - deferred to Sprint H

Event-driven scheduling removes poll latency, but it needs webhook auth,
idempotency, replay, and fallback contracts. Sprint G needed the base graph,
idempotency, cron, hotfix, notification, and visibility surfaces first.
ADR-0018 can now specify the event-driven layer against a stable base.

### D. Cron-only conductor without JIRA graph - rejected

Cron could execute scripts in sequence, but that would make release state a
set of shell outcomes instead of a reviewable workflow. JIRA must remain the
human-readable and runner-readable conductor.

## Consequences

### Positive

- Release state is visible in JIRA and reviewable by humans.
- Re-running creation commands is safe when a release already exists.
- Operators have explicit skip and force-create controls.
- Hotfixes use the same family of patterns as full releases.
- Notification and dashboard consumers get a stable read surface.
- Sprint H has a clear base contract for event-driven scheduling.

### Negative

- The conductor is still only half-automated. Poll cadence can add latency
  between stages until the event-driven layer lands.
- JIRA availability remains part of the release critical path.
- The system now has several read surfaces, so documentation must keep clear
  which surfaces are authoritative and which are derived.
- The missing `release-conductor-pattern.md` path shows that spec-source
  references can drift unless ADRs and runbooks name the current artifact.

## Verification

Sprint G verification spans code and documentation already landed by its
children:

- G1 and G2 are pinned by `backend/tests/test_release_template_engine.py`.
- G3 and G4 are pinned by `tests/test_release_conductor_cron.py`.
- G5 is pinned by hotfix template and handler tests.
- G6 is pinned by release notification tests and the routing config.
- G7 is pinned by release state API tests.
- G8 is this ADR, the Sprint G retrospective, and the OP-944 lesson.

OP-944 itself is documentation-only. It does not change backend, db,
devops, embedded, frontend, security, tests, or tooling code.

## Follow-up

ADR-0018 is the direct follow-up. It may add webhook and internal-event
subscriptions, but it must preserve the Sprint G invariants above:

- JIRA graph remains authoritative.
- event delivery is a hint, not the source of truth.
- duplicate and out-of-order events are idempotent.
- cron remains the fallback for missed events.
