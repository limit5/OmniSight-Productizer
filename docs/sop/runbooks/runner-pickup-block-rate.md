# Runner pickup block rate runbook (OP-1119)

The `runner_pickup_block_rate` alert (severity `warn`) fires when more
than 20 % of a runner instance's acquire attempts ended in
`outcome="lost"` over the trailing one-hour window — i.e. the
incumbent claim mutex blocked the pickup. Sustained block rate is the
early signal that the cross-runner coordination substrate is being
starved, usually by a stuck lease or a mis-keyed resource.

The alert is emitted by the v2-AlertBridge framework (`warn` →
email + stdout) and dedupes on `owner_instance_id`.

## What triggers it

The producer increments `runner_claim_acquire_total{outcome=...,
owner_instance_id=...}` on every call into `acquire_claim()`:

| `outcome` | Meaning |
| --- | --- |
| `won` | The runner inserted a new `runner_claims` row and proceeded. |
| `lost` | A different runner already held the resource (`ClaimBlocked`). |
| `api-error` | Postgres was unreachable; `ClaimAPIError` was raised. |

The rule computes `lost / (won + lost + api-error)` per
`owner_instance_id` on a 1-hour rate. The threshold (20 %) was chosen
from the pre-substrate baseline (≤ 8 % typical, with bursts to ~ 15 %
during release cuts); anything above 20 % over a full hour means
sustained contention, not a transient burst.

## Operator response

```sh
# 1. Enumerate which resource_keys the affected instance is colliding on.
omnisight runner-rescue dump --instance "$OWNER_INSTANCE_ID"

# 2. The dump lists every active lease this instance has tried to
#    acquire in the last hour. Compare against the active rows owned by
#    OTHER instances on the same resource_key. The block-side is the
#    incumbent — that lease is the one wedging the fleet.

# 3. Decide:
#    (a) Incumbent lease holder is stuck (paired runner_claim_stale alert):
#        follow the runner-claim-stale runbook to release the wedged lease.
#    (b) Incumbent lease is healthy but the resource_key is over-contended
#        (e.g. multiple runners legitimately want the same ticket): there
#        is no operator action — the runner backoff will sort it out on the
#        next tick window. The alert auto-resolves once the rate dips.
#    (c) Incumbent lease is healthy but the contending runner has the
#        wrong owner_instance_id / agent_class assignment: stop the
#        contender via systemd and re-check its config.
```

## Cross-checks before paging deeper

- Is `runner_claim_stale` firing for the same `owner_instance_id`?
  → The two alerts share root cause. Resolve the stale-claim alert
  first; the block-rate alert auto-clears.
- Is `runner_state_drift` firing? → The legacy JIRA `claim:*` labels
  may be out of sync with the substrate; the substrate is still the
  authoritative read source but operators relying on JIRA may be
  acting on stale state. See `runner-state-drift.md`.
- Did a release cut just happen? → Release cuts cause a transient
  ~ 15 % block-rate spike for ≤ 20 min. The `for: 1h` clause should
  absorb this; if the alert fires, the spike outlasted normal.

## What it is NOT

- It is not a single-pickup failure. Individual `ClaimBlocked`s are
  normal and operator-invisible — the alert only fires on sustained,
  fleet-impacting rates.
- It is not the same as `runner-blocked` JIRA labels. Those are a
  legacy observability signal that the substrate replaces; they are
  read-only during the shadow window per ADR-0037 Phase 1.

## Related

- Spec: `docs/sop/runner-substrate-contract.md` §8.
- ADR: `docs/adr/ADR-0037-runner-state-substrate-decoupling.md`
  §validation-contract (the 20 % threshold corresponds to the
  "runner-blocked drops from 45 % → < 10 %" target after cutover; the
  alert is the watch-rail while we approach it).
- AlertBridge contract: `docs/sop/alert-rule-contract.md`.
- Sibling runbook: `runner-claim-stale.md`.
