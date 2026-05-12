---
id: L-OP-1014
ticket: OP-1014
title: Bulk import without refinement creates dead inventory
date: 2026-05-13
tags: [jira, process, backlog, anti-pattern]
---

# Bulk import without refinement creates dead inventory

**Situation**: The 2026-05 governance migration dumped `TODO.md` into the OP
project as ~530 open tickets tagged `runner-needs-refinement`,
`migrated-from-todo-bulk` or `migrated-from-todo`. None of them got
`area:` / `tier:` / `class:` labels, a fixVersion, an assignee, or an AC
section. Result: the runner JQL can't pick a single one of them, yet they
account for ~half of every project-wide JIRA query — capacity planning,
sprint review and "how big is the backlog?" all read 1000+ items when the
*actionable* backlog is far smaller. Nobody ever circles back to refine an
unrefined-by-construction ticket, so the dump just sits there.

**Fix**: Two parts.
1. *Don't create the inventory in the first place.* A ticket that lacks the
   runner-pickability invariants (see [[L-OP-737-jira-ticket-creation-2-invariants-for-runner-pickability]])
   and a real AC section is dead on arrival. The 4-AC discipline already
   says a ticket with only a Code AC is shipped-but-not-deployed *by design*
   — the same logic applies one step earlier: a ticket with only a summary
   is un-pickable by design. If you must stage planning notes, keep them in
   a planning doc / Epic, not as hundreds of standalone `タスク` tickets.
2. *When the inventory already exists, triage it with a heuristic, not by
   hand.* `scripts/jira-todo-backlog-triage.py` classifies each legacy-label
   ticket as `keep` (has a live signal — started / assigned / has fixVersion
   / has a non-bot comment / recently updated / too young to call dead),
   `abandon` (old + cold + never started → Won't Do / Archived) or
   `duplicate` (shares a normalised summary with an older sibling → human
   confirms). `triage` is read-only and writes a report + a JSONL decision
   file; `apply` does the bulk Won't Do transition from an operator-reviewed
   decision file and defaults to dry-run.

**Verification**: `scripts/jira-todo-backlog-triage.py triage` run
2026-05-13 against soraapp.atlassian.net produced
`docs/audit/2026-05-13-todo-backlog-triage.md` — 532 open legacy-label
tickets, all created 2026-05-06/07, all still `To Do`, unassigned, no
fixVersion, no comments. By the agreed `older-than-30d` heuristic **0** are
archive-eligible *yet* (the dump is a week old, not stale-aged); the
abandon window opens ~2026-06-05, at which point the script should be
re-run. The honest read: this dump is *new* dead inventory, and the only
way it shrinks is a deliberate operator decision (refine the survivors, or
revert the import) — it will not self-clear.

**Generalisation**: Any "import N items in bulk, refine them later" plan is
a debt that compounds: the unrefined items are immediately invisible to
automation but maximally visible to humans doing planning. Either refine at
import time (expensive but correct) or don't import as tickets at all
(cheap, keeps the backlog honest). If a bulk import already happened, treat
the cleanup as a first-class ticket with a heuristic-driven script — manual
triage of 500 items never finishes. Recorded as anti-pattern #13 in
`docs/sop/architecture-anti-patterns.md`.
