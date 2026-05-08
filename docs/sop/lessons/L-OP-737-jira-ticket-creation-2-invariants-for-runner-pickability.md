---
id: L-OP-737
ticket: OP-737
title: JIRA ticket creation: 2 invariants for runner-pickability
date: 2026-05-08
tags: [ci, jira, runner]
legacy_lesson: 27
---

# JIRA ticket creation: 2 invariants for runner-pickability

**Situation**: 15+ tickets created via JIRA REST API in one session (OP-721/722-728/729-736 family) used `issuetype=Task` and narrow `area:backend` labels. Runner saw 0 of them; codex hit self-halt loops on the converted ones because `area:backend` forbade docs/tests work that the AC required. About 30 minutes of runner ticks were wasted.

**Fix**: Always file with (a) `issuetype=Story` because Task is invisible to runner JQL (`backend/agents/jira_dispatch.py:137`) and (b) `area:` labels covering every domain the AC + Files/Paths sections will touch. The runner prompt enforces "DO NOT introduce changes to <other areas>". New helper: `scripts/file_jira_ticket.py` enforces both at file-time.

**Verification**: Synthetic ticket text with `area:backend` and AC referencing `docs/*` fails `--check` mode of the helper. Real ticket fix: OP-729's `area:backend` to `area:backend,docs,tests` removed the halt.

**Generalisation**: Any agent or human filing runner-pickable tickets must (a) use the issuetype the runner JQL filters for and (b) cover the union of domains the AC will touch in `area:` labels. A helper script that fails at file-time is cheaper than discovering the trap during pickup.
