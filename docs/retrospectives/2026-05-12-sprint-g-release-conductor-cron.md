---
title: Sprint G release conductor cron retrospective
date: 2026-05-12
ticket: OP-944
labels: [meta:retrospective, sprint-g, release-conductor]
scope: code delivery and design outcomes only
---

# Sprint G Release Conductor Cron Retrospective

**Scope**: code delivery and design outcomes only.

**Out of scope**: real release cycle time, production webhook latency,
notification delivery SLOs, and Sprint H event-driven behavior. Those belong
to later release or Sprint H retrospectives after the conductor has run live.

## 0. Sprint Goal

Sprint G set out to turn release coordination from an operator-held
checklist into a JIRA-graph-backed conductor. The key decision was to avoid a
new standalone service and instead make the existing workflow explicit:

- one `RELEASE-vX.Y.Z` META,
- release children linked by `blockedBy`,
- runner pickup through the existing JIRA gate,
- cron and hotfix triggers that instantiate work only when preconditions are
  met,
- notification and dashboard read models for operator visibility.

The sprint succeeded as a code-delivery and design sprint. It does not yet
prove that release cycle time improved in production.

## 1. Child Outcomes

| Child | Ticket | Outcome |
| --- | --- | --- |
| G1 | OP-937 | Release template engine created the canonical release META plus 13-child graph. |
| G2 | OP-938 | Idempotency guard made duplicate live META creation fail closed. |
| G3 | OP-939 | Daily cron can scan SemVer fixVersions, run acceptance, back off, and instantiate through G1. |
| G4 | OP-940 | Operator labels added explicit skip and force-create paths with audit events. |
| G5 | OP-941 | Hotfix conductor variant added HOTFIX META creation from labelled Gerrit changes. |
| G6 | OP-942 | Release-child transition notifications fan out through Slack/email routing. |
| G7 | OP-943 | Pending releases dashboard/API exposes current release state as a read model. |
| G8 | OP-944 | ADR-0017, this retrospective, and an operator lesson close the design loop. |

All seven implementation children were at `公開済み` in JIRA when OP-944
began on 2026-05-12. OP-944 was the final documentation child.

## 2. What Worked

### JIRA graph as conductor

The strongest Sprint G outcome is the decision to reuse JIRA `Blocks` as the
release state machine. This aligned release automation with the runner's
existing pickup rules and avoided creating another authority that would need
to be reconciled with JIRA.

The design is easy to explain: if a child is blocked, the runner cannot pick
it up; if its blocker reaches Published, the next child becomes eligible.
That makes release progress visible to the same humans and agents already
using the project board.

### Idempotency before scheduling

G2 landed before the cron and hotfix paths became dangerous. That mattered:
without the idempotency guard, a transient cron retry or repeated hotfix scan
could create duplicate METAs. The guard fails closed on lookup outage and
ambiguous state, which is the right bias for release orchestration.

### Operator labels were explicit

The G4 labels solved a real operational need without hiding state in local
files. `release:skip-auto-conductor` and `release:force-create` are visible
where operators already inspect fixVersions. Conflict handling and audit
events keep overrides reviewable.

### Read models stayed read-only

G6 notifications and G7 dashboard work did not seize release authority.
They observe transitions and make state visible, but the state remains in the
release graph and release-state rows. That separation is what lets Sprint H
add event-driven wakeups without making Slack, email, or dashboard state
load-bearing.

## 3. What Was Underestimated

### The conductor touched more surfaces than "cron" implied

The ticket family looked like a cron automation sprint, but the actual
system spans:

- JIRA template creation,
- idempotency and duplicate suppression,
- daily fixVersion scanning,
- operator override labels,
- hotfix scans,
- notification routing,
- dashboard read models,
- runbook and retrospective documentation.

The sprint stayed coherent because each child had a narrow owner. The
planning lesson is that "release conductor" is a product surface, not a
single script.

### Spec-source drift appeared at the end

The ticket description cites `docs/operations/release-conductor-pattern.md`,
but the local worktree did not contain that file during OP-944. The current
operator source is `docs/operations/release-conductor-runbook.md`, plus the
template and script artifacts.

This did not block OP-944 because enough local and JIRA evidence existed,
but it is exactly the kind of drift that a closing ADR should capture. ADRs
and retrospectives should name the current artifact when an older spec path
has moved or never landed.

### Release evidence is not the same as live release evidence

Sprint G can say the conductor pieces landed and are contract-tested. It
cannot say the daily cron improved a real production release yet. That
claim requires at least one release cycle where the META is instantiated,
children run through Published, notifications fire, and the dashboard
matches the live state.

## 4. Child-by-Child Notes

### G1 - OP-937

G1 established the core graph. The release template records the 13 release
stages, their areas, tiers, and acceptance criteria. The runbook makes
`scripts/instantiate_release_meta.py` the only supported creation path.

The important design choice was preserving the graph in JIRA rather than
using a local scheduler file.

### G2 - OP-938

G2 made the engine re-runnable. The guard checks for existing release and
hotfix METAs before creation, permits `--force` only for Archived recovery,
and fails closed on JQL outage or ambiguity.

This was correctly sequenced before cron and hotfix automation.

### G3 - OP-939

G3 delivered the daily cron skeleton and offline contract tests. The cron
enumerates SemVer fixVersions, suppresses existing METAs, runs acceptance,
records audit rows, and alerts after repeated failures.

The cron is intentionally conservative. It makes ready releases appear in
the graph; it does not decide that a release is safe by itself.

### G4 - OP-940

G4 added operator override labels. The skip label prevents automation for a
fixVersion. The force-create label bypasses acceptance but records the
operator override warning.

The label conflict path is important: conflicting labels refuse rather than
guessing which operator intent wins.

### G5 - OP-941

G5 added hotfix META creation from labelled Gerrit changes. This carried the
release-conductor pattern into an urgent path without making the hotfix path
free-form.

The hotfix path is a good example of pattern reuse: new trigger, same
idempotency and audit posture.

### G6 - OP-942

G6 made release transitions visible through notification routing. The
important boundary is best-effort fan-out. Notification failure should alert,
but it should not corrupt or block the underlying release state.

### G7 - OP-943

G7 added pending-release visibility. The dashboard/API is useful because it
turns release state into an operator scan surface. It remains a read model,
which keeps authority in the conductor graph.

### G8 - OP-944

G8 closes Sprint G with three artifacts:

- ADR-0017 for the half-automation decision,
- this retrospective for sprint outcome,
- L-OP-944 for the reusable operator lesson.

That closure is necessary before Sprint H adds event-driven behavior on top.

## 5. Lessons

1. **Create the graph before optimizing the scheduler.**
   Sprint H can reduce polling latency because Sprint G first created a
   stable graph to wake up.

2. **Idempotency belongs before cron.**
   Any scheduled release automation must be safe to retry before it runs on
   a timer.

3. **Operator overrides must be visible on the operator surface.**
   FixVersion labels are better than local env flags because they travel
   with the release object and can be audited.

4. **Read models must stay read-only.**
   Dashboards and notifications are useful only while they cannot outrank
   the conductor graph.

5. **Closing docs should reconcile spec-source drift.**
   A final ADR/retro is the right place to record that the referenced source
   path is missing and name the artifact that actually became canonical.

## 6. Follow-up for Sprint H

Sprint H should preserve these Sprint G invariants:

- JIRA graph stays authoritative.
- event-driven wakeups are hints over the graph.
- webhook idempotency is mandatory.
- missed events degrade to G3 cron behavior.
- dashboards and notifications remain read-only.

The event-driven layer should be judged on latency reduction and
operational clarity, not on replacing the conductor base.

## 7. Retrospective Conclusion

Sprint G succeeded at half-automating release coordination. It created the
release graph, made creation idempotent, added daily cron, gave operators
explicit override controls, mirrored the pattern for hotfixes, and exposed
notification plus dashboard read models.

The sprint's main design outcome is ADR-0017: the JIRA graph is the release
conductor; automation may create, observe, and wake it, but not replace it.
The main operational lesson is L-OP-944: close the poll-driven baseline
before filing event-driven follow-up, and make the baseline's invariants
explicit.
