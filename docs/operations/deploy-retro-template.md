# Deployment Quarterly Retrospective Template

> Copy this template into the quarterly retrospective location selected
> by the operator, then fill every bracketed field before review.

## Metadata

| Field | Value |
| --- | --- |
| Quarter | `[YYYY-QN]` |
| Retrospective owner | `[name / account]` |
| Review date | `[YYYY-MM-DD]` |
| Release window covered | `[YYYY-MM-DD..YYYY-MM-DD]` |
| Related META ticket | `[OP-____]` |
| Deploy runbook revision | `[git commit or Gerrit Change-Id]` |

## Audit Evidence

Attach the output from:

```bash
python3 scripts/deploy_audit_query.py --since=90d --format=csv \
  > /tmp/deploy-audit-[YYYY-QN].csv
```

| Metric | Value | Evidence |
| --- | --- | --- |
| Production deploys | `[count]` | `[audit row ids / CSV path]` |
| Rollbacks | `[count]` | `[audit row ids / CSV path]` |
| SLO breaches | `[count]` | `[audit row ids / CSV path]` |
| Operator actions | `[count]` | `[audit row ids / CSV path]` |
| Median deploy duration | `[seconds]` | `[query or dashboard link]` |
| Longest deploy duration | `[seconds]` | `[audit row id]` |

## Approval Review

| Question | Answer |
| --- | --- |
| Did every production deploy have a non-empty operator reason? | `[yes/no + evidence]` |
| Did every Tier L/X change have human approval before production? | `[yes/no + evidence]` |
| Did any AI reviewer exceed +1 authority? | `[yes/no + evidence]` |
| Were emergency approvals recorded on the relevant incident ticket? | `[yes/no + evidence]` |

## Runbook Review

| Check | Result | Follow-up |
| --- | --- | --- |
| Milestone checklist was sufficient | `[yes/no]` | `[OP-____ or none]` |
| Cut checklist was sufficient | `[yes/no]` | `[OP-____ or none]` |
| Deploy checklist was sufficient | `[yes/no]` | `[OP-____ or none]` |
| Monitor checklist was sufficient | `[yes/no]` | `[OP-____ or none]` |
| Rollback checklist was sufficient | `[yes/no]` | `[OP-____ or none]` |
| Any `RunbookOutOfDate` event occurred | `[yes/no]` | `[OP-____ or none]` |
| Any `AuditQueryTimeout` event occurred | `[yes/no]` | `[OP-____ or none]` |

## Incidents and Rollbacks

For each rollback, SLO breach, or deploy failure, add one row.

| Date | Release / tag | Event | User impact | Detection source | Recovery path | Follow-up |
| --- | --- | --- | --- | --- | --- | --- |
| `[YYYY-MM-DD]` | `[tag]` | `[deploy failed / rollback / SLO breach]` | `[none / summary]` | `[dashboard / alert / operator]` | `[runbook section]` | `[OP-____]` |

## What Worked

Fill with specific mechanisms, not general praise.

- `[Example: milestone checker caught an open blocker before branch cut.]`
- `[Example: canary SLO gate halted at 5 percent before broad impact.]`

## What Did Not Work

Fill with concrete gaps and evidence.

- `[Example: audit query timed out for a 180-day export; created OP-____ to tune page size guidance.]`
- `[Example: rollback target was not visible in the release ticket; created OP-____.]`

## Decisions

Record only decisions made during the retrospective.

| Decision | Owner | Due date | Tracking ticket |
| --- | --- | --- | --- |
| `[decision]` | `[owner]` | `[YYYY-MM-DD]` | `[OP-____]` |

## Action Items

| Action | Owner | Due date | Verification |
| --- | --- | --- | --- |
| `[action]` | `[owner]` | `[YYYY-MM-DD]` | `[test, doc line, or Change-Id]` |

## Sign-Off

| Reviewer | Role | Date | Disposition |
| --- | --- | --- | --- |
| `[name]` | Human operator | `[YYYY-MM-DD]` | `[approved / changes requested]` |
| `[name]` | Release operator | `[YYYY-MM-DD]` | `[approved / changes requested]` |

Do not close the retrospective until every bracketed field is either
filled with evidence or explicitly marked `N/A` with a reason.
