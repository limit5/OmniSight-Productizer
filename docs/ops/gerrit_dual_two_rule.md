# O7 (#270) — Gerrit Dual-+2 Submit-Rule Runbook

## TL;DR

A Gerrit change in OmniSight **only merges** when BOTH of these are true:

1. At least one `Code-Review: +2` from a **human** in the
   `non-ai-reviewer` group.  *This is the hard gate — no combination
   of AI reviewers can replace it.*
2. At least one `Code-Review: +2` from the `merger-agent-bot` group
   (the O6 Merger Agent has signed off on the merge-conflict block).

Any `Code-Review: -1` or `-2` blocks submission outright.

AI reviewers (Merger, lint-bot, security-bot, future AIs) **can add
additional +2 votes** — this is encouraged because it gives humans
more context — but those AI +2s can never substitute for the human
hard gate.

Policy location: `CLAUDE.md` → Safety Rules → "AI reviewer max score".
Enforcement: `.gerrit/rules.pl` (Prolog) plus the mirror evaluator at
`backend/submit_rule.py` (SSOT for arbiter + CI).

---

## 1. Group design

| Group               | Members                                    | Permissions                                  |
|---------------------|--------------------------------------------|----------------------------------------------|
| `non-ai-reviewer`   | Human engineers ONLY.  Bot accounts are forbidden by policy (verify via `gerrit ls-members`). | `label Code-Review -2..+2`, `submit`.         |
| `ai-reviewer-bots`  | Every AI reviewer account (merger-agent-bot, lint-bot, security-bot, future AIs). | `label Code-Review -2..+2`, `push to refs/for/*`. **NO submit.** |
| `merger-agent-bot`  | The single bot account that runs the O6 Merger Agent.  Must also be in `ai-reviewer-bots`.   | Inherits from `ai-reviewer-bots`.            |

### Why group-based, not account-based?

Adding a new AI reviewer in the future (e.g. a perf-bot, a
doc-bot, …) should require zero changes to `rules.pl`.  Admins add
the new account to `ai-reviewer-bots`, and the rule Just Works.

### Creating the groups

```sh
# As a Gerrit admin:
ssh -p 29418 gerrit-host gerrit create-group non-ai-reviewer \
    --visible-to-all --description "Humans ONLY. Bots forbidden."

ssh -p 29418 gerrit-host gerrit create-group ai-reviewer-bots \
    --visible-to-all --description "Umbrella for every AI reviewer (Merger, lint-bot, security-bot, ...)"

ssh -p 29418 gerrit-host gerrit create-group merger-agent-bot \
    --include ai-reviewer-bots \
    --visible-to-all --description "O6 Merger Agent service account."

# Add the merger service account:
ssh -p 29418 gerrit-host gerrit set-members merger-agent-bot --add merger-agent-bot@svc.omnisight.internal
ssh -p 29418 gerrit-host gerrit set-members ai-reviewer-bots --add merger-agent-bot@svc.omnisight.internal
```

**Verification:**

```sh
ssh -p 29418 gerrit-host gerrit ls-members non-ai-reviewer
# → must print human accounts only; fail the check if any bot appears.
```

---

## 2. Installing the submit-rule

The OmniSight policy lives in two files:

* [`.gerrit/project.config.example`](../../.gerrit/project.config.example)
* [`.gerrit/rules.pl`](../../.gerrit/rules.pl)

Install them on `refs/meta/config`:

```sh
git clone ssh://gerrit-host:29418/omnisight-productizer gerrit-meta
cd gerrit-meta
git fetch origin refs/meta/config:refs/remotes/origin/meta/config
git checkout meta/config

cp ../.gerrit/project.config.example project.config
cp ../.gerrit/rules.pl rules.pl

git add project.config rules.pl
git commit -m "O7: dual-+2 submit rule (human hard gate + merger bot)"
git push origin HEAD:refs/meta/config
```

Verify the rule loaded:

```sh
ssh -p 29418 gerrit-host gerrit rules --test-submit-rule omnisight-productizer <change-id>
```

---

## 3. Test matrix

The O7 specification mandates the following cases.  The matrix is
enforced by `backend/tests/test_submit_rule_matrix.py` against the
SSOT evaluator, and by the Gerrit Prolog rule at submit time.

| Votes                                                        | Expected |
|--------------------------------------------------------------|----------|
| Merger +2 only (no human)                                    | reject   |
| Human +2 only (no merger)                                    | reject   |
| Merger +2 + human +2                                         | **allow**|
| Merger +2 + human -1                                         | reject   |
| `N × AI +2` (merger + lint-bot + security-bot + …) + 0 human | reject   |
| `N × AI +2` + human +2                                       | **allow**|
| Merger +2 + human +2 + another AI +2                         | **allow**|
| Merger +2 + human +2 + any reviewer -1                       | reject   |

The "N × AI" row is the load-bearing test: any combination of AI +2s,
no matter the count or account, cannot satisfy rule (1).

---

## 4. Operational flows

### 4.1 Gerrit merge-conflict → Merger → human

```
Gerrit detects merge-conflict
        │
        ▼  (webhook POST /orchestrator/merge-conflict)
Merge Arbiter.on_merge_conflict_webhook
        │
        ▼
O6 Merger Agent.resolve_conflict
        │
        ├──► +2 voted ──► SSE change.awaiting_human_plus_two → Slack/email
        │                      │
        │                      ▼
        │                 Human reviews; casts +2 or -1
        │                      │
        │                      ▼  (webhook POST /orchestrator/human-vote)
        │                 Arbiter.on_human_vote_recorded
        │                      │
        │                      ├──► +2 → submit-rule allow → gerrit submit
        │                      └──► -1 → revoke merger +2, WIP
        │
        └──► abstain / refuse ──► JIRA ticket to catc_owner + SSE change.merger_abstain
```

### 4.2 Human disagreement rollback

When a human casts `Code-Review: -1` or `-2`:

1. `merge_arbiter.on_human_vote_recorded` notices the negative vote.
2. It calls `GerritVoteRevoker.revoke(score=0)` with comment
   "human disagrees, merger withdraws".
3. Change transitions to work-in-progress (Gerrit surface) and the
   O6 failure counter is reset so the next patchset can re-trigger
   the merger cleanly.
4. SSE `orchestration.change.work_in_progress` fires.

### 4.3 Merger abstain / refuse

Any non-+2 outcome from the merger:

1. Arbiter opens a JIRA ticket under `task.jira_ticket` and assigns
   the original CATC owner.
2. Ticket description carries the merger's reason code + rationale +
   change-id + file path.
3. Change stays work-in-progress; no Merger +2 is written.  The
   submit-rule will keep blocking until a human reviewer manually
   resolves the conflict and both +2s arrive.

### 4.4 Merger audit

Every vote / abstain / refusal is audited via
`backend.audit.log(action="merger.*", actor="merger-agent-bot")`.
Operators can reconstruct the full history via:

```sh
gh api repos/OWNER/REPO/audit  # GitHub-side mirror
# OR
psql -c "SELECT * FROM audit_log WHERE entity_id = 'I1234abcd';"
```

---

## 5. Emergency procedures

### 5.1 Pause the merger

Set `OMNISIGHT_MERGER_MIN_CONFIDENCE=1.01` (un-achievable) in the
Merger service env.  Every conflict will hit
`abstained_low_confidence`, triggering the JIRA-ticket fallback.

### 5.2 Rollback the submit-rule

The dual-+2 rule is hard-enforced at Gerrit level.  If it blocks a
time-critical hotfix, the emergency rollback is:

```sh
# Revert rules.pl on refs/meta/config (admin-only):
git checkout meta/config
git revert <O7 rule commit>
git push origin HEAD:refs/meta/config
```

All emergency rollbacks MUST:

* Include a rollback ticket in `docs/ops/upgrade_rollback_ledger.md`.
* Be re-applied within 24 h (tracked in the same ledger).
* Be accompanied by a post-mortem if the rollback lasted > 4 h.

### 5.3 Authorisation gap detected

If a change merges without a human +2 (e.g. due to a rules.pl bug),
treat as a P0 incident:

1. Lock the repo: `gerrit set-project --state read-only <project>`.
2. Page security-oncall.
3. Review the audit trail for every merge in the incident window.

---

## 6. GitHub-native fallback

Customers without Gerrit use `.github/workflows/merge-arbiter.yml`:

* Branch protection requires the `merge-arbiter/dual-plus-two` status.
* Workflow evaluates reviews using the same SSOT evaluator in
  `backend/submit_rule.py`.
* Merger Agent runs as a GitHub App with login `merger-agent-bot[bot]`.
* Human hard gate = membership in the `non-ai-reviewer` team.

The mapping between Gerrit `Code-Review: +2 / -2` and GitHub review
states is:

| Gerrit         | GitHub              |
|----------------|---------------------|
| `Code-Review: +2` | `APPROVED`        |
| `Code-Review: -2` | `CHANGES_REQUESTED` |
| `Code-Review: -1` | `CHANGES_REQUESTED` |
| `Code-Review: 0`  | `COMMENTED` / dismissed |

---

## 7. Related

* [`CLAUDE.md`](../../CLAUDE.md) — L1 Safety Rules (the policy source).
* [`.gerrit/rules.pl`](../../.gerrit/rules.pl) — Prolog source of truth.
* [`backend/submit_rule.py`](../../backend/submit_rule.py) — Python SSOT
  mirror, used by orchestrator + GitHub Actions.
* [`backend/merge_arbiter.py`](../../backend/merge_arbiter.py) — the
  Gerrit webhook / human-vote pipeline driver.
* [`backend/merger_agent.py`](../../backend/merger_agent.py) — O6
  Merger Agent (+2 voter on the AI side).
