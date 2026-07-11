---
id: L-OP-2571b
ticket: OP-2571
title: Runner ticket comments do NOT reach the agent's prompt — put mid-flight guidance in the ticket BODY or re-file
date: 2026-07-11
tags: [runner, jira, process]
---

# Runner ticket comments do NOT reach the agent's prompt — put mid-flight guidance in the ticket BODY or re-file

**Situation**: The runner pickup path (R2a) builds the agent prompt from a
fixed field set. `backend/agents/jira_dispatch.py` fetches only
`["summary", "labels", "status", "issuetype", "fixVersions", "created",
"components", "issuelinks", "parent"]` (`jira_dispatch.py:408-409`) — the
ticket body/summary plus its issuelinks — and JIRA `comment` is not in that
list. Consequently any comment added to a ticket mid-flight is invisible to the
agent that subsequently builds it. During the U4 marathon this bit us
concretely: a "please clean up the `.cache/` before push" comment left on
OP-2571 had zero effect on the next build cycle, because the runner never reads
comments into the prompt.

**Fix**: Deliver mid-flight guidance or corrections through channels the prompt
actually consumes: edit the ticket BODY (the description the runner reads), or
re-file the ticket with the corrected scope. Reserve comments for the human
audit trail — they document intent for people, not for the building agent.
(The `.cache/` problem itself was ultimately fixed structurally by ignoring the
directory, not by any comment — see L-OP-2571.)

**Verification**: The prompt field list at `jira_dispatch.py:408-409` contains
no `comment` entry, so by construction comment text cannot enter the agent
prompt; the observed no-op of the OP-2571 cleanup comment on the next build
cycle confirms it end-to-end. A body edit on the same ticket, by contrast, is
picked up because `summary`/description is in the fetched set.

**Generalisation**: When an automated worker composes its instructions from a
specific set of source fields, only those fields steer it — anything a human
adds outside that set (comments, side-channel chat, watchers) is invisible to
the worker no matter how clearly it is written. Know exactly which fields your
runner injects into the agent prompt, and route every actionable
instruction into one of them (here: the ticket body) or into a re-file. Treat
comments as human-facing documentation only, and never assume a mid-flight
comment will change what the next build does.
