---
id: L-OP-2572
ticket: OP-2572
title: Never tag an infra hotfix commit with an OPEN runner ticket's key — the gerrit-jira bridge force-closes it
date: 2026-07-11
tags: [jira, gerrit, runner, ci, process]
---

# Never tag an infra hotfix commit with an OPEN runner ticket's key — the gerrit-jira bridge force-closes it

**Situation**: A quick infra hotfix commit carried `[OP-2571]` in its subject
line while OP-2571 was still an in-flight, un-merged runner ticket. When that
hotfix merged, the gerrit→jira reconciliation bridge did what it is built to
do: it matched the merged change to the ticket key in the subject and drove
OP-2571 to a done/closed resolution. Every manual reopen was immediately
re-closed on the next reconciliation pass — the bridge fought the rescue twice
before the cause was understood. The ticket's real work had not shipped; only
an unrelated hotfix that happened to name it had.

**Fix**: A commit subject `[OP-XXXX]` tag is an instruction to the bridge to
resolve that ticket on merge — treat it as reserved for the change that
actually completes the ticket. For an infra hotfix that merely relates to an
open ticket, reference it in the commit BODY prose only ("context: unblocks
OP-2571's push path"), never as a `[OP-XXXX]` subject tag; or open a separate
tracking ticket for the hotfix and tag that. Here the genuine work was re-filed
clean as OP-2572 rather than fighting the bridge to keep OP-2571 open.

**Verification**: Once the `[OP-2571]` tag was off the hotfix path, OP-2571
stayed in its intended state through subsequent reconciliation passes (the
bridge had no merged change subject-tagged to it), and OP-2572 progressed on
its own change. The reconciliation behaviour itself is correct and was left
unchanged — the defect was the input (a subject tag on the wrong ticket), not
the automation.

**Generalisation**: Any commit-message→ticket bridge that closes tickets on
merge makes the subject-line ticket tag a state-mutating command, not a
comment. So the subject `[OP-XXXX]` must name only the ticket the change is
meant to resolve; mentioning an unrelated or still-open ticket belongs in body
prose or on its own tracking ticket. Racing a reconciliation loop by hand
(reopen → auto-reclose) never wins — remove the tag that is driving the
closure, and if the work is genuinely separate, re-file it as a fresh ticket
rather than salvaging the mistakenly-closed one.
