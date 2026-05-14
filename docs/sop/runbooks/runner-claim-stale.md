# Runner claim stale runbook (OP-1119)

The `runner_claim_stale` alert (severity `page`) fires when an active
row in the `runner_claims` table has a `heartbeat_at` older than 5
minutes. The owning runner instance is wedged or crashed mid-phase; any
peer trying to pick up the same `resource_key` is blocked by the
mutex until the lease releases.

The alert is emitted by the v2-AlertBridge framework (`page` →
email + stdout) and dedupes on `owner_instance_id`.

## What triggers it

The producer (`backend/agents/runner_coordination.py`) scrapes
`runner_claim_heartbeat_age_seconds` per active lease. The runner
updates `heartbeat_at` on each phase transition via `record_phase()`;
the gauge is `now() - heartbeat_at`. Healthy runners advance phase
several times per minute, so a five-minute gap is well outside
normal jitter.

Common causes:

- The runner CLI is wedged inside a long-running tool call (network
  hang, runaway model output) past the per-CLI timeout.
- The runner process was `kill -9`'d after acquiring a lease but before
  reaching the `finally` block that calls `release_claim()`.
- The host rebooted with an active lease still in the table; the
  orphan reaper (OP-1059) eventually catches this but only on its
  10-minute cycle.
- Postgres write failures in `record_phase()` (the runner keeps
  working but heartbeats fall behind).

## Operator response

Take 1 — confirm the wedged instance and decide release vs reset:

```sh
# 1. Identify which runner is the offender (from the alert labels).
omnisight runner-rescue dump --instance "$OWNER_INSTANCE_ID"

# 2. If the runner is genuinely stuck (no PID alive, or PID alive but
#    blocked on a known external hang), release every active lease it
#    owns. release_claim flips state='force-released' and frees the
#    resource_key for the next claimer.
omnisight runner-rescue release "$LEASE_ID" --reason "wedged-by-oncall: <short>"

# 3. If the runner host itself is unreachable, mass-release everything
#    for the affected ticket so a peer can pick it up:
omnisight runner-rescue reset "$TICKET_KEY" --reason "host-unreachable"
```

Take 2 — if release does not unblock pickup within the next two
runner ticks (~ 2 min), the substrate is stuck for a different
reason. Escalate:

- Check Postgres health (`runner_claims` may be locked by a long-
  running `BEGIN IMMEDIATE`).
- Check the runner host's process state; a defunct runner with the
  lease may need a hard `systemctl restart`.
- Page the runner subsystem owner if the substrate itself appears
  unhealthy (see [bridge-health runbook](./bridge-health-degraded.md)).

## What it is NOT

- It is not the orphan reaper. The reaper (OP-1059) eventually sweeps
  this state on its 10-minute timer; the alert exists because 10
  minutes is too slow when the wedged runner is starving the rest of
  the fleet (a 3-runner fleet loses ~33 % of pickup capacity per
  stuck instance).
- It does not implicate the ticket the runner was working on. The
  ticket can be re-picked-up safely by another runner once the
  lease releases.

## Related

- Spec: `docs/sop/runner-substrate-contract.md` §8 (defense contract,
  alert_rule_id row).
- ADR: `docs/adr/ADR-0037-runner-state-substrate-decoupling.md`
  (substrate rationale).
- AlertBridge contract: `docs/sop/alert-rule-contract.md`.
- Sibling runbook: `runner-instance-collision.md` (the orthogonal
  failure where two runners claim the same instance id).
