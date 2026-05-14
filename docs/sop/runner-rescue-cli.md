# Runner Rescue CLI — Operator Runbook

`omnisight-runner-rescue` is the operator-side tool for inspecting and
overriding the `runner_claims` coordination table (alembic 0236, shipped
by OP-1106). Every invocation appends one row to `runner_audit_events`
(alembic 0237) so post-incident review can answer "who released this
lease and why".

Filed under: Sprint Atlas, Family ⑩ (Runner Defense Contract), ticket
OP-1118 (v2-Ⅹ-RescueCLI). Companion to OP-1106 (substrate), OP-1107
(shadow-write), OP-1108 (table reads), OP-1109 (release-on-every-path +
TTL sweeper), OP-1110 (cutover).

## When to use this CLI

| Symptom | Subcommand |
|---|---|
| "A ticket is stuck — I want to see who's holding which mutex / claim." | `dump` |
| "Runner died mid-pickup. Lease is still active. Need to unblock the resource." | `release <lease_id>` |
| "Host-wide incident (reboot, kill -9 storm). Many leases are orphaned. Want to sweep all stale ones." | `reset` |

The CLI is **not** the right place for routine release — the runner
itself releases on every terminal path (OP-1109) and the TTL sweeper
(also OP-1109) cleans up after a 5-min heartbeat silence. Use this CLI
only when those mechanisms have failed or you want to short-circuit the
TTL wait.

## Operator fingerprint gate (ADR-0033)

Write subcommands (`release`, `reset`) require `--operator <fingerprint>`
on every invocation. The fingerprint is recorded in the audit log and is
your identity for L2 actions.

```bash
# Optional allow-list — restrict who can run write subcommands.
# Comma-separated. Empty / unset → accept any non-empty fingerprint
# (dev / single-operator deployments).
export OMNISIGHT_L2_OPERATORS="alice,bob,oncall-rotation"
```

`dump` accepts `--operator` but does not require it; read-only inspect
is open. Setting it anyway is good practice so the audit trail attributes
the dump to a human.

## Subcommands

### `dump` — inspect active claims (read-only)

```bash
omnisight-runner-rescue dump [--operator alice] [--json]
```

Prints one row per active `runner_claims` entry. Use `--json` for machine
parsing. Always audit-logged (`action=rescue.dump`).

Output columns (table mode):

- `TICKET` — JIRA key the claim is bound to
- `AGENT_CLASS` / `INSTANCE` — owning runner identity
- `RESOURCE` — the resource_key (typically `ticket:<key>` for per-ticket
  ownership; `mutex:<resource>` for cross-ticket mutex once OP-1110's
  follow-up lands)
- `PHASE` — last `record_phase()` value (e.g., `pickup`, `working`,
  `submitting`)
- `HEARTBEAT_AT` — UTC timestamp of last `record_phase()` call.
  Drifted heartbeats are sweep candidates.
- `LEASE_ID` — UUID; needed for `release`.

### `release` — force-clear one stuck lease

```bash
omnisight-runner-rescue release <lease_id> \
    --reason "<free-form audit text>" \
    --operator <fingerprint>
```

Force-releases a specific lease bypassing the fencing-token check (the
point — the original holder is dead and you don't have its token).

Exit codes:

- `0` — released; audit row written with the lease snapshot pre-release
- `2` — operator fingerprint not in allow-list
- `3` — no active lease for the given `lease_id` (already released or
  never existed); audit logs the attempt anyway
- `1` — DB / runtime error

### `reset` — bulk-release stale claims

```bash
omnisight-runner-rescue reset \
    --operator <fingerprint> \
    [--max-age-seconds 600] \
    [--dry-run]
```

Releases every active claim whose `heartbeat_at` is older than
`--max-age-seconds` (default 600 = 10 min). `--dry-run` previews the
impact without writing — recommended before a real reset on a busy host.

Equivalent to running `runner_coordination.expire_stale_active_claims()`
manually. The TTL sweeper (OP-1109) does this automatically every cycle
at 300 s; this subcommand is for wider-window manual rescues after host
incidents where dozens of leases are orphaned.

## Common scenarios

### Scenario: a single ticket is wedged

1. `omnisight-runner-rescue dump | grep OP-1234` — confirm there's an
   active lease for the ticket
2. Capture the `LEASE_ID` from the output
3. `omnisight-runner-rescue release <lease_id> --reason "..." --operator <me>`
4. Verify with another `dump` — the lease should be gone
5. Runner picks the ticket up on its next tick

### Scenario: host rebooted, many leases orphaned

1. `omnisight-runner-rescue reset --dry-run --operator <me>` — see how
   many would be released and on which tickets
2. If the count looks right: drop `--dry-run` and run for real
3. Spot-check `dump` after — should be (close to) empty

### Scenario: investigate "is the table even populated?"

1. `omnisight-runner-rescue dump --json | jq length` — quick count
2. If zero, check:
   - Coordination DB path: `echo $OMNISIGHT_DATABASE_PATH`
   - Alembic head: `alembic current` (should show 0236+)
   - Runner logs: search for `runner_coordination.acquire_claim shadow
     failed` or `force_release_claim` — any errors there explain the
     gap

## Audit log queries

The `runner_audit_events` table stores one row per rescue invocation.
Sample queries for incident review:

```sql
-- All rescue activity in the last 24h, newest first
SELECT ts, action, operator_fingerprint, target_ticket_key, details
FROM runner_audit_events
WHERE ts > NOW() - INTERVAL '24 hours'
ORDER BY ts DESC;

-- Find every override for a specific ticket
SELECT ts, action, operator_fingerprint, details
FROM runner_audit_events
WHERE target_ticket_key = 'OP-1234'
ORDER BY ts;

-- Top operators by write-volume (last 7d)
SELECT operator_fingerprint, COUNT(*) AS rescue_count
FROM runner_audit_events
WHERE ts > NOW() - INTERVAL '7 days'
  AND action IN ('rescue.release', 'rescue.reset')
GROUP BY operator_fingerprint
ORDER BY rescue_count DESC;
```

## Limitations

- **No undo**. Releasing a lease via this CLI is final — the row is
  flipped to `state=released` with `release_reason='operator-override:...'`
  and the resource is immediately re-claimable. If you release the wrong
  lease, the original runner may still try to do its work; the table will
  block the *write-back* (no active lease) but it cannot recall a
  push-in-flight.
- **Cross-ticket mutex labels not yet table-resident**. Per the carry-
  forward noted on OP-1108/OP-1110, OP-1107 shadow-wrote only
  `ticket:<key>` rows. `mutex:<resource>` cross-ticket mutex is still
  read via JQL fallback. `dump` will not show mutex-label rows until the
  follow-up extends shadow-write to include them.
- **Audit log is append-only**. There is no `omnisight-runner-rescue
  redact` subcommand. Sensitive details (passwords, PII) should not be
  put into `--reason`.

## See also

- `docs/sop/runner-pickup-mutex.md` — OP-977 fencing-token claim
  protocol (the pre-OP-1110 mechanism the table now supersedes for
  writes; reads keep label fallback for one more sprint).
- `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md`
  §3 Family ⑩ — full design + ticket family + rationale.
- ADR-0033 — Governance Engine + 3-Level Operator Authority (L2
  fingerprint requirement).
- ADR-0034 — Override Review Lifecycle + Separation of Duties.
