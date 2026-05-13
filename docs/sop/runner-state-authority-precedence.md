# Runner state-authority precedence

**Status**: Adopted (2026-05-14)
**Spec**: Sprint SP-B-X §3.14 SP-B-X-012 / OP-1070
**Implementation**: `backend/agents/state_authority_resolver.py`
**Bridge integration**: `backend/agents/gerrit_jira_bridge.py` (force-walk path; pre-existing)
**Runner integration**: `auto-runner-jira.py` TOCTOU reread sites (follow-up; see §5)

## Why this document exists

Before this rule was written down, three subsystems each had an implicit
view of "what's the truth when sources disagree":

- The JIRA-Gerrit bridge daemon force-walks JIRA when Gerrit reports a
  merged change (per OP-743).
- The runner's TOCTOU reread (SP-B-X-004) re-fetches JIRA labels at phase
  boundaries and aborts if assignee/status changed.
- The runner-progress recovery probe (SP-B-X-002a) surfaces leftover
  stashes from prior pickups as `[progress-recovered]` comments.

All three were internally consistent but had no shared codification.
Any contributor editing one had no way to verify the other two still
agreed. The OP-1048 incident on 2026-05-13 exercised exactly this gap.

## The three rules

### Rule 1 — Gerrit merged > JIRA workflow

If Gerrit reports a change as `MERGED` for a ticket the runner believes
is still `To Do` or `進行中`, the truth is on Gerrit. The bridge daemon
force-walks JIRA to match.

**Why Gerrit wins**: Gerrit is the irreversible event horizon — once a
patch lands on `refs/heads/develop`, the code is in production
regardless of what JIRA says. JIRA workflow is reversible bookkeeping.

**Concrete consequences**:
- The bridge's force-walk continues to be the actor; this module just
  emits an audit-log marker so operators can trace each resolution.
- The marker is `[state-authority-resolved]` instead of the legacy
  `[bridge]` (when the change is a precedence resolution rather than a
  vanilla merge event).

### Rule 2 — JIRA workflow > runner-internal state

If a runner's `progress.txt` says `working`-complete but JIRA reports
`Under Review` (because another runner picked up the same ticket between
this runner's phases — race detected by SP-B-X-004 TOCTOU reread or by
SP-B-X-011 human-authority yield), JIRA wins and the runner aborts.

**Why JIRA wins**: multiple runners can write to their own
`progress.txt` files concurrently. JIRA is the single shared truth for
workflow state.

**Concrete consequences**:
- The runner FSM treats `abort_runner_pickup` as a clean exit (rc=0),
  not a failure. The work is dropped silently. A subsequent fresh
  pickup re-reads the canonical JIRA state.
- This rule does **not** force a state change; it tells the *runner*
  what to do (abort).

### Rule 3 — runner-internal state > cached snapshots

The on-disk `progress.txt` is authoritative over the in-memory pickup
snapshot. If they diverge, the runner re-reads `progress.txt`.

**Why disk wins**: the snapshot is captured once at pickup time; the
progress.txt is updated at every phase boundary. The disk file is the
durable, monotonic source.

**Concrete consequences**:
- Used by SP-B-X-002a's `find_recovered_snapshot()` to decide whether
  to post `[progress-recovered]`.
- The runner refreshes its in-memory snapshot before the next phase
  boundary.

## What this module is NOT

- **Not** a force-walk actor. It returns decisions; callers act.
- **Not** a state-machine implementation. The bridge daemon
  (`gerrit_jira_bridge.py`) and the runner FSM (`auto-runner-jira.py`)
  each have their own state machines; this module is shared advisory
  logic.
- **Not** a cache. Each `resolve()` call processes its inputs fresh.
  Callers wanting caching layer it on top.

## Audit-log integration

Every resolution that is NOT `noop` may write one row to the
`release_audit` table (per alembic 0207 schema) with:

- `outcome = "noop"` (the resolution itself is observability, not an
  irreversible release event — using the existing `noop` enum value)
- `fix_version = NULL`
- `develop_sha`, `main_sha` from caller-supplied or `OMNISIGHT_*_SHA` env
- `detail` JSON blob containing:
  - `kind: "state-authority-resolved"`
  - `ticket_key`, `rule_id`, `action`
  - `idem_key` (SP-B-X-001 deterministic token, for replay detection)
  - rule-specific fields (e.g., `gerrit_change_url`,
    `jira_status_name`, `snapshot_phase`, `progress_phase`)

The audit write is **non-fatal**: a DB outage logs a WARN but does not
affect the decision returned to the caller. The decision is the
authoritative output; the audit row is observability.

## §5 — Runner / bridge wiring (follow-up)

The resolver module is the contract. Two wirings remain (deferred to a
follow-up so each surgery is small):

1. **Runner FSM**: `auto-runner-jira.py` calls `resolve()` at every
   TOCTOU reread (SP-B-X-004 boundary) and acts on the result. The
   wiring belongs in the existing TOCTOU helper to keep the FSM tidy.
2. **Bridge daemon**: `gerrit_jira_bridge.py` force-walk path swaps
   `[bridge]` → `[state-authority-resolved]` marker when the walk is a
   precedence resolution. Vanilla merge → bridge events keep `[bridge]`.

Until §5 lands, the resolver is a documented + tested contract waiting
for consumers.

## References

- ADR-0035 §3 (External system ordering binding rule)
- SP-B-X spec §3.14 SP-B-X-012 (this ticket)
- OP-743 (bridge force-walk antecedent)
- SP-B-X-002a / OP-1060 (`progress.txt` writer; runner-internal source)
- SP-B-X-004 / OP-1062 (TOCTOU reread; consumer of Rule 2)
- SP-B-X-011 / OP-1069 (human-authority yield; consumer of Rule 2)
- alembic 0207 (`release_audit` schema)
