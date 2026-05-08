---
id: L-OP-752
ticket: OP-752
title: Freeze migration scopes before pickup
date: 2026-05-08
tags: [runner, jira, migration]
---

# Freeze migration scopes before pickup

**Situation**: META migrations can change a shared document or ticket
format while normal runner tickets are still eligible. Without a pickup
gate, an in-flight ticket can write the old format after the migration
patch set has already introduced the new one, creating avoidable Gerrit
conflicts and review ambiguity.

**Fix**: Let the migration ticket hold `migration:in-flight` plus
`migration:scope=<glob>` labels. The runner checks active migration
scopes before pickup, pauses overlapping tickets with a JIRA comment,
and only permits bypass when the target ticket has an audited
`migration:override` label.

**Verification**: `backend/tests/test_jira_dispatch.py` covers active
migration scope lookup, overlap blocking, release after the migration is
no longer active, and override auditing. `backend/tests/test_gerrit_jira_bridge.py`
covers removing the in-flight label when the migration ticket is
published.

**Generalisation**: Structural META changes need an explicit pickup gate
before they land. Prefer temporary labels with path scopes over broad
runner shutdowns so unrelated tickets keep flowing.
