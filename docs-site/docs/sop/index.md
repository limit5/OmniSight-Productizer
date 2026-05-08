---
title: SOPs
---

# Standard Operating Procedures

SOPs are the procedural backbone of the project. They define how tickets are
written, how phases are implemented, how bugs are diagnosed, and how reviews
gate merges. CI enforces several of them (drift guards, prereq audit,
scheduler tests), so amendments require a META ticket plus operator approval.

Currently published in this scaffold:

- [JIRA ticket conventions](jira-ticket-conventions.md) — ticket structure,
  dependencies, lifecycle, and runner pickup rules.

The remaining SOPs in `docs/sop/` (architecture anti-patterns, debug
hypothesis, implement-phase, fix-bug, gerrit-jira-bridge, etc.) will be
migrated in follow-up tickets once the framework choice and theme have been
validated by the operator.
