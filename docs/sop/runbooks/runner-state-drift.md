# Runner state drift runbook (OP-1119)

The `runner_state_drift` alert (severity `warn`) fires when the daily
shadow comparison job (Phase 1 of ADR-0037) finds at least one
disagreement between the authoritative `runner_claims` table and the
legacy JIRA `claim:*` labels. Drift during the shadow window is
non-fatal — the substrate is the read authority — but each event must
be reconciled before the v2-Ⅹ-2bc read-replacement cutover flips,
because the closure gate requires 0 drift across the 40-ticket sample
for ≥ 3 consecutive days.

The alert is emitted by the v2-AlertBridge framework (`warn` →
email + stdout) and dedupes on `kind`.

## What triggers it

The shadow comparison job (filed under `v2-Ⅹ-2-Shadow`) runs daily,
walks the in-progress JIRA ticket set, and compares against the
`runner_claims` table. It emits `runner_state_drift_count{kind=...}`
as a gauge with two possible kinds:

| `kind` | Meaning |
| --- | --- |
| `table_only` | An active `runner_claims` row exists with no matching `claim:*` JIRA label. The substrate believes the resource is held; JIRA-watching operators do not. |
| `jira_only` | A `claim:*` JIRA label exists with no matching active `runner_claims` row. JIRA-watching operators believe the resource is held; the substrate does not. |

The threshold is strict zero — any drift count above zero is a
disagreement that must be reconciled.

## Operator response

```sh
# 1. Pull the latest comparison report. The job writes a JSONL file
#    under docs/audit/AUDIT-G-A-v2-Family-10/ named YYYY-MM-DD-drift.jsonl,
#    one line per disagreement.
ls -t docs/audit/AUDIT-G-A-v2-Family-10/*-drift.jsonl | head -1

# 2. For each drifted row, decide which side is authoritative.
#    The substrate (runner_claims) is canonical post-ADR-0037; JIRA
#    labels are a read mirror. So the reconciliation always brings JIRA
#    into agreement with the substrate, not the other way round.
```

For `kind=table_only` (substrate has row, JIRA has no label):

```sh
# A runner acquired the lease but the JIRA dual-write fell behind
# (network blip, JIRA rate-limit). Re-emit the `claim:*` label.
omnisight runner-rescue dump --ticket "$TICKET_KEY"
# → confirm the substrate's owner_instance_id + fencing_token
# Then have the runner's instance write the matching label back
# via the standard dual-write path; or, if the runner is no longer
# active, force-release the substrate row to bring both sides in sync.
omnisight runner-rescue release "$LEASE_ID" --reason "shadow-drift-reconcile"
```

For `kind=jira_only` (JIRA has label, substrate has no row):

```sh
# A pre-substrate claim label survived migration, or a JIRA-only
# operator (or older runner CLI) wrote a claim:* label without going
# through acquire_claim(). The fix is to strip the orphan label so
# the next dual-write reflects the substrate truth.
#
# The orphan-reaper runbook covers the same surface for stale claim
# labels; for shadow-window drift specifically, the cleanest path is:
python3 -c "from backend.agents.jira_dispatch import release_ticket_claim; release_ticket_claim('$TICKET_KEY', instance='$INSTANCE')"
```

After reconciling, re-run the comparison job on demand to confirm
the next sweep is clean:

```sh
python3 -m backend.agents.runner_coordination_compare --report-only
```

## Cross-checks before reconciling

- Is `runner_claim_stale` firing for the same instance? → The
  drift is downstream of a wedged runner. Fix the stale claim
  (runner-claim-stale.md) first; the next comparison sweep will
  clear the drift automatically.
- Are multiple drift rows from the same epoch? → Likely a JIRA
  outage during a dual-write window. Wait for the next sweep before
  manual reconciliation; the runner may self-heal.

## What it is NOT

- It is not a bug in the substrate. Drift is expected during the
  Phase 1 shadow window — that is the whole point of running both
  surfaces in parallel for a week. The alert is the
  reconciliation-progress tracker, not a failure indicator.
- It is not the cutover gate by itself. The closure criterion is a
  zero-drift count sustained across ≥ 3 consecutive days on the
  40-ticket sample (per ADR-0037 §exit-gate). One day of drift = 0
  is not yet "cutover ready".

## Related

- Spec: `docs/sop/runner-substrate-contract.md` §8.
- ADR: `docs/adr/ADR-0037-runner-state-substrate-decoupling.md`
  §migration-plan + §exit-gate.
- AlertBridge contract: `docs/sop/alert-rule-contract.md`.
- Sibling runbooks: `runner-claim-stale.md`,
  `runner-pickup-block-rate.md`, `orphan-reaper.md`.
