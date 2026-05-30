# Medical-Tier Customer Onboarding Runbook

**Audience:** OmniSight operators and the camviewpro regulatory bench.
**Scope:** generic medical-tier camviewpro contribution tickets. This runbook is
customer-neutral and describes the flow already implemented by the contribution
runner, GitHub PR target, and contribution PR tracker.

## Non-Bypass Principle

OmniSight surfaces the medical-tier checklist and tracks state back to JIRA.
It does not bypass camviewpro review gates. Camviewpro CODEOWNERS and the
regulatory bench own the GitHub PR merge decision; OmniSight only opens the PR,
labels regulated-lane work, records tracker state, and waits for explicit
regulatory clearance before closing the JIRA ticket.

## End-To-End Cycle

### 1. File the Medical-Tier Customer Ticket

Create a runner-pickable JIRA Story for the camviewpro work. The Acceptance
Criteria should describe the camviewpro change exactly as the agent should
implement it, including any medical-tier behavior, files, and verification the
camviewpro repo expects.

Required labels:

| Label | Meaning | Who sets it |
|---|---|---|
| `target:camviewpro` | Routes the ticket to the camviewpro contribution flow. | Operator |
| `camviewpro-project:<KEY>` | Selects the configured external product source. If omitted, the runner falls back to the JIRA project key. | Operator |
| `camviewpro-base:<branch>` | Selects the camviewpro base branch. Use `camviewpro-base:main` unless the ticket explicitly needs another base. | Operator |
| `agent:auto` | Makes the ticket eligible for autonomous runner pickup. | Operator |
| `class:subscription-codex` or `class:subscription-claude` | Chooses which runner class implements the contribution. | Operator |
| `tier:M` or `tier:S` | Declares execution tier. Medical-tier contribution work is normally M or S. | Operator |
| `area:*` | Declares every OmniSight area the ticket description asks the runner to touch. For this runbook itself, that is `area:docs`; for camviewpro implementation tickets, use the area labels required by the ticket body. | Operator |
| `capability:enable=gerrit_push` | Allows the runner to push its OmniSight-side completion change. | Operator |
| `camviewpro-pr:<n>` | Records the opened GitHub PR number for tracker polling. | Runner |
| `regulated-lane` | Marks a medical-tier contribution PR or ticket as regulated-lane work. | Runner |
| `regulatory-cleared-required` | Hold marker added after a regulated-lane PR merges but before bench clearance is recorded. | Runner |
| `regulatory-cleared` | Explicit clearance signal after the regulatory bench has signed off. | Operator after regulatory bench acceptance |

Implementation pointers: runner pickup and label routing live in
`auto-runner-jira.py`; the camviewpro route is `_run_camviewpro_contribution`.

### 2. Runner Contribution Flow

After the ticket is pickable, the runner performs the contribution without
operator intervention:

1. Picks the JIRA ticket and routes `target:camviewpro` work to
   `auto-runner-jira.py` `_run_camviewpro_contribution`.
2. Resolves `camviewpro-project:<KEY>` and `camviewpro-base:<branch>`, defaulting
   the base branch to `main` when the base label is absent.
3. Creates a temporary workspace and calls
   `backend/agents/contribution_runner.py` `contribute_to_product`.
4. Clones the configured camviewpro repository at the selected base branch.
5. Pre-sets the fresh clone git identity to `OmniSight Runner
   <runner@omnisight.local>` so the agent can commit normally.
6. Runs the selected agent in the camviewpro worktree.
7. Accepts either uncommitted agent output or already-created agent commits.
8. Pushes a feature branch and calls
   `backend/agents/github_pr_target.py` `open_contribution_pr`.
9. Opens or updates a ready-for-review GitHub PR, records `camviewpro-pr:<n>` on
   the JIRA ticket, and transitions the JIRA ticket to Under Review.

If the contribution changes no files and creates no commits, the runner comments
`[runner-camviewpro-no-changes]` and does not open a PR. If PR creation returns
no result, it comments `[runner-camviewpro-no-pr]`.

### 3. Medical-Flag Detection

`backend/agents/github_pr_target.py` detects medical-tier work from changed
camviewpro paths. A PR is flagged when any changed file starts with one of:

| Path prefix | Meaning |
|---|---|
| `apps/medical/` | Medical application surface. |
| `libs/core-medical-grade/` | Medical-grade core library surface. |
| `docs/regulatory/` | Regulatory documentation surface. |

When one of those prefixes matches, `open_contribution_pr` sets
`flagged_medical=True`, adds the `regulated-lane` label to the GitHub PR, and
prepends the camviewpro AUDIT.md regulated-lane checklist to the PR body.

The PR body checklist asks the operator and regulatory bench to fill:

- DHF entry path/id
- RTM trace
- SOUP impact
- ISO 14971 Risk update
- IEC 62304 Class B activity log
- HIPAA impact, when PHI-adjacent

The checklist is intentionally placeholder-based. Replace each placeholder with
the concrete DHF path, RTM id, SOUP assessment, risk record, activity log, or
HIPAA impact result used by camviewpro's bench process.

### 4. Operator And Regulatory Bench Review

Review the GitHub PR in camviewpro. The PR is ready for review, not draft, and
camviewpro CODEOWNERS remain authoritative.

The operator and regulatory bench should:

1. Confirm the `regulated-lane` PR label is present for medical-tier changes.
2. Fill every checklist item with concrete paths, ids, or "not applicable" notes
   accepted by camviewpro's bench process.
3. Let camviewpro CI run and resolve any CI or review findings in the PR.
4. Wait for the regulatory bench acceptance required by camviewpro.
5. Have the authorized regulator merge the PR in GitHub.

OmniSight does not make the merge decision and does not replace CODEOWNERS,
CI, or bench sign-off.

### 5. Tracker Hold After Merge

`backend/agents/contribution_pr_tracker.py` tracks tickets carrying
`camviewpro-pr:<n>`. After a tracker poll sees the GitHub PR is merged:

1. If the PR does not carry `regulated-lane`, the tracker transitions the JIRA
   ticket to Done/Published.
2. If the PR carries `regulated-lane` and the JIRA ticket does not yet carry
   `regulatory-cleared`, the tracker does not transition to Done.
3. Instead, it adds `regulatory-cleared-required` to the JIRA ticket and records
   a `hold_regulated` event in the regulated-lane audit trail.

The hold is the expected state between GitHub merge and final regulatory
paperwork completion.

### 6. Close The Regulatory Hold

When the regulatory bench has signed off and the required records are filed,
the operator closes the hold by adding the JIRA label `regulatory-cleared`.
Only do this after the relevant DHF and RTM entries are filed, Risk records are
updated, and IEC 62304 Class B activity is logged.

On the next tracker poll, the tracker sees the pair:

- GitHub PR merged and carrying `regulated-lane`
- JIRA ticket carrying `regulatory-cleared`

It then transitions the ticket to Done/Published, removes
`regulatory-cleared-required`, and records a `to_done_cleared` event in the
regulated-lane audit trail.

### 7. Audit Trail

Every regulated-lane terminal tracker event is written by
`backend/agents/regulated_lane_audit.py` as JSONL. The default path is:

```bash
audit/regulated_lane_events.jsonl
```

Override the path with:

```bash
OMNISIGHT_REGULATED_LANE_AUDIT_PATH=/path/to/regulated_lane_events.jsonl
```

Operator review CLI:

```bash
python3 scripts/list_regulated_lane_audit.py [--limit N] [--ticket KEY]
```

Use `--ticket OP-1234` to inspect one ticket, or omit it to list the most recent
events across regulated-lane contribution tickets.

## Troubleshooting

- PR not opening: inspect the runner log for `[runner-camviewpro-no-changes]`,
  `[runner-camviewpro-no-pr]`, or a GitHub PR write error.

  ```bash
  tail -100 /tmp/runner-codex-1.log
  tail -100 /tmp/runner-claude-1.log
  ```

- Ticket stuck Under Review after the PR merged: check whether the GitHub PR has
  `regulated-lane` and whether the JIRA ticket has
  `regulatory-cleared-required`. If the hold is present, wait for bench
  sign-off, then add `regulatory-cleared`.

```bash
backend/.venv/bin/python - <<'PY'
from backend.agents import jira_dispatch

client = jira_dispatch.make_client("subscription-codex")
print(jira_dispatch.fetch_ticket_labels(client, "OP-1234"))
PY
```

- Audit log missing: check whether the audit path is overridden and whether the
  file exists. The audit writer is best-effort and logs write failures without
  blocking tracker flow.

  ```bash
  env | grep OMNISIGHT_REGULATED_LANE_AUDIT_PATH
  python3 scripts/list_regulated_lane_audit.py --limit 20
  ```

## Implementation Map

| Behavior | Code path |
|---|---|
| camviewpro ticket route and JIRA label write-back | `auto-runner-jira.py` `_run_camviewpro_contribution` |
| clone, base checkout, runner git identity, agent commit acceptance | `backend/agents/contribution_runner.py` `contribute_to_product` |
| GitHub PR creation/update, medical path detection, PR body checklist, `regulated-lane` PR label | `backend/agents/github_pr_target.py` `open_contribution_pr` |
| JIRA PR-state tracker, regulated hold, cleared transition | `backend/agents/contribution_pr_tracker.py` |
| JSONL regulated-lane audit writer | `backend/agents/regulated_lane_audit.py` |
| operator audit review CLI | `scripts/list_regulated_lane_audit.py` |
