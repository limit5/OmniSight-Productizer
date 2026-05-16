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
  - Companion (in `docs/sop/`, pending docs-site migration):
    `jira-label-conventions.md` — the full label namespace registry +
    forbidden-combinations (incl. the `type:meta` mis-routing case study);
    `jira-label-schema.yaml` — its machine-parseable mirror, validated by
    `scripts/file_jira_ticket.py`.

The remaining SOPs in `docs/sop/` (architecture anti-patterns, debug
hypothesis, implement-phase, fix-bug, gerrit-jira-bridge, label conventions,
etc.) will be migrated in follow-up tickets once the framework choice and
theme have been validated by the operator.
