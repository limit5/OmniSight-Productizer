# `bridge_drift`

| field | value |
|-------|-------|
| Severity | `DEGRADED` |
| Source | T5 drift scanner — `scripts/drift_scanner.py` |
| Tier owner | bridge-maintainer |
| Parent META | OP-721 |

## What triggers it

The drift scanner inspects the deployed `gerrit-jira-bridge`
checkout on the host (the directory the systemd unit's
`WorkingDirectory=` points at) and compares its **HEAD SHA** against
the SHA the deploy fixture / release ledger says it should be on.
If they diverge, it emits `code="bridge_drift"`.

Cases this catches:

* a manual `git pull` was run on the deploy host without going
  through the rollout SOP;
* a half-finished rollout left the checkout on an intermediate
  commit;
* the deploy host's worktree dirty-state was never cleaned;
* a rebase / force-push upstream invalidated the deployed SHA's
  identity.

## Severity rationale

`DEGRADED` because the deployed code may still be functional — it
is just not the audited code. The same reasoning as
[`image_drift`](image_drift.md): drift is a leading indicator of a
broken release process, not necessarily a production failure.

If the drifted bridge has a *behavioural* bug, you'll see one of:

* [`refused_llm_unavailable`](refused_llm_unavailable.md) at unusual
  rate
* [`gerrit_client_ssh_failed`](gerrit_client_ssh_failed.md)
* [`audit_write_failed`](audit_write_failed.md)
* Or: `daemon_silent` if the drifted code has a hang bug

— and those page at higher severity from inside the daemon. So
`bridge_drift` does the right thing by escalating slowly.

## Severity rationale (detail: why not `CRITICAL`)

Operationally, `DEGRADED` is the right choice because the most
common case is "operator pulled in a small fix and forgot to
update the release ledger" — paging Slack/LINE for that would be
counter-productive. The daily cadence + JIRA + email of `DEGRADED`
gets the right eyeballs in <24h.

## Immediate action

1. **Establish the drift direction:**

       cd /path/to/deployed/gerrit-jira-bridge   # see WorkingDirectory= in the unit
       git rev-parse HEAD
       git log --oneline -5

   Compare against `docs/ops/upgrade_rollback_ledger.md` /
   `git tag --list 'release-bridge-*'` for the expected SHA.

   * **Deploy is ahead** of release ledger: someone deployed
     un-tagged work. Read the diff carefully — if it's a known
     hotfix, update the ledger. If unknown, revert
     (`git checkout <expected_sha>`; `systemctl --user restart
     gerrit-jira-bridge`).
   * **Deploy is behind** ledger: a previous rollout was never
     completed. Resume it: pull, restart, re-verify with the T4
     canary.
   * **Deploy is on a different branch entirely**: open the
     postmortem template — this is a process violation.

2. **Bounce the bridge** after any checkout change:

       systemctl --user restart gerrit-jira-bridge

   Watch for the next heartbeat (≤60s) and the next T4 canary
   (next hour) before declaring resolved.

3. **Re-run the scanner**:

       python scripts/drift_scanner.py --json | jq -e 'all(.[]; .code != "bridge_drift")'

## Root-cause investigation

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Worktree dirty (`git status` shows mods) | Local edit on deploy host | Stash + checkout clean SHA |
| HEAD detached at unknown SHA | Manual rebase / cherry-pick | Restore from ledger |
| HEAD on a non-`develop`/`main` branch | Wrong branch deployed | Restore from ledger; postmortem |
| HEAD on a feature branch | Mid-rollout state | Resume or roll back |

## Escalation

* Single occurrence, fixed in <1h: low-priority follow-up.
* Recurring ≥2 times/week: the rollout SOP is being bypassed.
  Open a META ticket against release-eng to enforce gating.
* Bridge drift correlated with a customer-reported regression:
  promote to `CRITICAL` manually; rollback per
  `docs/ops/blue_green_runbook.md`.
