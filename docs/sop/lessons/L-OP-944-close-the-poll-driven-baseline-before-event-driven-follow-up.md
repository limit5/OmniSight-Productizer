---
id: L-OP-944
ticket: OP-944
title: Close the poll-driven baseline before event-driven follow-up
date: 2026-05-12
tags: [release-conductor, sprint-g, adr, retrospective, operations]
---

# Close the poll-driven baseline before event-driven follow-up

**Situation**: Sprint G built a release conductor in layers: release META
template, idempotency guard, daily cron, operator labels, hotfix trigger,
notifications, and a pending-releases dashboard. Sprint H was already
starting to specify event-driven wakeups before the Sprint G closing ADR and
retrospective existed.

That creates a planning risk. Without a written baseline, the event-driven
follow-up can accidentally redefine which component is authoritative. It may
treat webhooks, dashboards, or notifications as the conductor instead of
treating them as accelerators over the JIRA graph.

**Fix**: Before filing or implementing event-driven conductor work, close
the poll-driven baseline with three artifacts:

1. an ADR naming the authority boundary,
2. a retrospective covering every shipped child,
3. a lesson that states which invariants the next sprint must preserve.

For Sprint G, ADR-0017 records that JIRA `Blocks` is the release conductor,
G3 cron is a fallback consistency path, and G6/G7 are read models. ADR-0018
can then build on that baseline instead of replacing it implicitly.

**Verification**: OP-944 adds
`docs/adr/ADR-0017-release-conductor-half-automation.md`,
`docs/retrospectives/2026-05-12-sprint-g-release-conductor-cron.md`, and
this lesson. The retrospective covers all eight Sprint G children, OP-937
through OP-944, and records that the event-driven layer must degrade to the
cron/JIRA graph path.

**Generalisation**: When a sprint creates an operational baseline and the
next sprint optimizes it, close the baseline first. The closing artifacts
should state:

- the source of truth,
- which surfaces are triggers or read models,
- which fallback keeps the system eventually consistent,
- which predecessor tickets were actually shipped,
- which spec-source paths drifted during implementation.

Do this before the follow-up sprint starts changing behavior. Otherwise the
follow-up ADR will smuggle in authority changes that nobody explicitly
reviewed.
