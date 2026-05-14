---
id: ADR-0037
title: Runner State Substrate Decoupling — JIRA labels = ledger only, Postgres = lock + state
status: Proposed
date: 2026-05-14
relates_to:
  - ADR-0035 (Runner FSM + Error Handling Contract)
  - ADR-0034 (Override Review Lifecycle + Separation of Duties)
  - Sprint S12.G G.A-v2 Family ⑩ — Runner Defense Contract
  - docs/audit/codex-reviews/runner-self-audit-and-redesign-2026-05-14.txt
  - docs/sop/runner-pickup-mutex.md (AUDIT-24 / OP-977)
ticket: OP-1105 (v2-⑩-ADR)
---

# ADR-0037 — Runner State Substrate Decoupling

## Status

Proposed (2026-05-14). Filed under Sprint S12.G G.A-v2 Family ⑩ (Runner Defense Contract). Locks in the architectural decision summarised in
`docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑩ before any v2-⑩-1bc / v2-⑩-2bc implementation lands.

## Context

### Lived-experience evidence

The codex Package 2 runner self-audit (2026-05-14,
`docs/audit/codex-reviews/runner-self-audit-and-redesign-2026-05-14.txt`)
captured first-person evidence that the dominant runner failure class is
not code generation but **coordination-state leakage**:

- OP-1070 (2026-05-14): codex completed AC at 02:07:39 then reverted at
  02:07:51 because `.runner-cwd-sentinel` had disappeared; later
  completed at 02:46:29, reverted at 02:46:42 because the first
  uncommitted path was `progress.txt`.
- OP-1070: assignee diverged to `live=None` at 02:55 / 03:14 mid-pickup;
  multiple codex worktrees raced the same ticket; `[runner-presync-fail]`
  comments interleaved with fresh pickups.
- OP-1067 / OP-1077: bridge-health heartbeat coupled to
  `_maintenance_ticks()` inside a Gerrit `stream-events` loop made the
  heartbeat stale during quiet Gerrit periods → fleet-wide pickup DoS.

Operator-side empirical numbers from the same window:

| Metric | Value |
|---|---|
| Labels per ticket (avg) | 22.4 |
| Unique labels (40-ticket sample) | 103 |
| Terminal tickets carrying `runner-blocked` | 45 % |
| Terminal tickets carrying `claim:*` residue | 40 % |
| Terminal tickets carrying `runner-loop-paused-pending-review` | 12 % |
| SP-B-X-001..021 tickets that are reactive FIX rather than feature | 50 % |

### Why JIRA labels became the lock

JIRA labels are the lock by accident, not by design. They are the only
durable, multi-agent-readable surface that the runner has ever had:
Postgres lived behind backend service boundaries, the worktree is
agent-local, and Gerrit reflects only post-push state. The runner
gradually accumulated nine distinct label / comment categories that
together encode a distributed lock + state machine + audit log + retry
log:

- Identity-on-pickup: `claim:<instance>:<token>` (AUDIT-24 / OP-977
  fencing-token mutex), historical `claim:<instance>` (pre-AUDIT-24).
- Mutex-held-by-other dependency wait: `runner-blocked:waiting-<KEY>`
  (`DEPENDENCY_WAITING_LABEL_PREFIX` in `backend/agents/jira_dispatch.py`).
- Transient-failure markers as comments: `[runner-presync-fail]`,
  `[runner-live-state-fail]`, `[runner-provider-dispatch]`,
  `[runner-pushed-to-gerrit]`, `[runner-pre-review-self-fix-warning]`,
  `[runner-mutex-blocked]`, `[runner-workspace-tampered]`,
  `[runner-capability-blocked]`, `[runner-dirty-worktree]`.
- Long-lived cooldowns: `runner-loop-paused-pending-review`
  (`ci_recovery.LOOP_PAUSED_LABEL`).

This namespace fails as a coordination substrate for five independent
reasons:

1. **Not transactional with status / assignee transitions.** A label add
   and a status transition are two REST calls; a process death between
   them leaves the system in a state no later observer can
   reconstruct.
2. **Not automatically cleaned on early exit.** §11 self-revert paths,
   pre-sync failure, and capability-blocked exits did not strip their
   own `claim:*` labels — documented in
   `feedback_stale_claim_labels.md` and the operator's 40 % `claim:*`
   residue measurement.
3. **Human-editable.** Operators can and do edit labels for triage; the
   runner cannot distinguish "lock released" from "lock invalidated by
   operator".
4. **Eventually consistent under JIRA Cloud read-after-write.** JQL
   indexes update with second-scale lag; `find_mutex_holders()` can
   miss a label that was added five seconds ago by another agent.
5. **Flat namespace.** Durable ticket metadata (`area:*`, `tier:*`,
   `class:*`) and volatile runtime state (`claim:*`, `runner-blocked:*`)
   share the same surface, so triage tools and dashboards cannot tell
   audit history from current state.

### Where this leaves prior fixes

ADR-0035 and the SP-B-X tickets (OP-1057..1077) addressed each of the
nine symptoms in isolation:

- **OP-977 / AUDIT-24** moved `claim:*` from idempotent bare labels to
  fencing-token `claim:{instance}:{epoch_us}-{uuid}` with lowest-token-wins
  re-read — this fixed the cross-agent race but left the label *as the
  lock surface*.
- **OP-1059 / SP-B-X-006** added an orphan reaper.
- **OP-1061 / SP-B-X-008** wired `release_ticket_claim` into post-CLI
  exit paths that previously skipped it.
- **OP-1069 / SP-B-X-011** added human-authority race detection.
- **OP-1070 / SP-B-X-012** added state-authority precedence.
- **OP-1074 / OP-1075 / OP-1076** patched the
  `.runner-cwd-sentinel` / `progress.txt` artifact contract in three
  separate hot-fixes (`RUNNER_RUNTIME_ARTIFACTS` in
  `backend/agents/runner_progress.py`, referenced from
  `jira_dispatch.py:1051`).
- **OP-1077 / SP-B-X-019** moved bridge-health heartbeat into a
  background thread.

Each fix is correct for its local symptom. None addresses the root
cause: JIRA labels are used as a distributed lock + progress record +
audit log simultaneously, with no atomicity between those roles.

## Decision

### D1 — Substrate split

The runner's coordination state SHALL split along two axes:

| Surface | Authoritative for | Examples |
|---|---|---|
| **Postgres** `runner_claims` table | Lock, lease, fencing token, heartbeat, phase, terminal cleanup state, structured external refs | claim ownership, mutex hold, pickup progress phase, terminal release reason |
| **JIRA labels + comments** | Human-facing ticket ledger only | `area:*`, `tier:*`, `class:*`, `capability:enable=*`, audit-trail comments, post-terminal summary |

JIRA remains the human triage surface. Postgres becomes the source of
truth for everything the runner reads to make pickup / hold / release
decisions. Both surfaces stay readable; only the **authority** moves.

### D2 — Coordination substrate API

A new module `backend/agents/runner_coordination.py` SHALL expose:

```python
acquire_claim(client, ticket_key: str, resources: list[str],
              agent_class: str, instance_id: str) -> ClaimLease | ClaimBlocked
heartbeat_claim(lease: ClaimLease) -> None
release_claim(lease: ClaimLease, reason: str) -> None
record_phase(lease: ClaimLease, phase: str,
             artifact_refs: dict | None = None) -> None
find_active_holders(resources: list[str],
                    exclude_ticket: str | None = None) -> list[ClaimLease]
mark_terminal(lease: ClaimLease, terminal_state: str,
              external_refs: dict) -> None
```

backed by the schema specified verbatim in the codex audit §4:

```
runner_claims(
  ticket_key         text         not null,
  resource_key       text         not null,
  lease_id           text         primary key,
  owner_agent_class  text         not null,
  owner_instance_id  text         not null,
  fencing_token      text         not null,
  state              text         not null,   -- pending | active | released
  phase              text,                    -- restoring_session | working | submitting | …
  heartbeat_at       timestamptz  not null,
  acquired_at        timestamptz  not null,
  released_at        timestamptz,
  release_reason     text,
  external_refs      jsonb                    -- { jira_key, gerrit_change_id, change_url, … }
);
```

The implementation ticket is v2-⑩-1bc (codex-routed); the schema kernel
dependency is G.A-v1-19. ADR-0037 is normative for the *split*; the
exact column types remain editable in the implementation ticket but the
**function signatures and the lease-token semantics are binding**.

### D3 — Strangler-pattern migration plan

Migration proceeds in four phases. Each phase has a measurable exit
gate; no phase advances until its gate is met on a 40-ticket sample.

#### Phase 0 — Inventory (this ADR)

- Categorise every existing `runner-*` and `claim:*` label per the
  migration table below (§D4).
- Land `RUNNER_RUNTIME_ARTIFACTS` as the single source of truth for
  dirty-check exclusions (v2-⑩-3).

Exit gate: ADR-0037 adopted + Family ⑩ tickets filed.

#### Phase 1 — Shadow

Implementation ticket: **v2-⑩-2-Shadow**. Every `acquire_claim` /
`release_claim` / `record_phase` write SHALL dual-write: both the
existing JIRA label / comment AND a Postgres row. Reads remain
label-driven. No behavior change.

Observation period: 1 week minimum. Daily comparison report between the
two surfaces (`runner_state_drift` Prometheus rule, v2-⑩-AlertRule).

Exit gate:
- 0 drifts in the 40-ticket sample for ≥ 3 consecutive days, OR
- Every observed drift is documented as a known JIRA-side staleness
  (e.g. operator hand-edit) and is replayed correctly from Postgres.

#### Phase 2 — Enforce

Implementation tickets: **v2-⑩-2bc** (mutex query swap) + **v2-⑩-2d**
(claim acquisition at runner entry points).

- `find_mutex_holders()` SHALL query `runner_claims` instead of JQL.
- Each runner entry point (`auto-runner-jira.py`,
  `auto-runner-multi.py`, `auto-runner-codex.py`) SHALL call
  `acquire_claim()` **before** `transition_to_in_progress()`, and SHALL
  `release_claim()` inside a `finally:` covering all six terminal
  paths: pre-sync fail, capability fail, no-commit, dirty worktree,
  push hard-fail, success.
- Label writes continue (dual-write retained).

Exit gate (last 40 runner-handled tickets, 1 week after enforce):
- `claim:*` labels on terminal tickets: 40 % → **0 %**
- `runner-blocked` on terminal tickets: 45 % → **< 10 %**
- 0 ticket has > 1 active claim row per mutex resource
- 0 duplicate pickup attempts on a single ticket
- OP-1070-style regression replay: runner finalization does NOT revert
  when `progress.txt` / `.runner-cwd-sentinel` are present.

#### Phase 3 — Drop

Implementation ticket: **v2-⑩-2-Cutover**.

- Stop writing `claim:*` and `runner-blocked:waiting-*` labels.
- Keep **read-only label compatibility for one sprint** (≈ 2 weeks)
  so existing dashboards, JQL filters, and operator scripts continue to
  function while their owners migrate.
- After the compat window expires, delete the dead label code paths and
  any tests asserting on them.

Exit gate: cutover ticket merged + read-only compat window elapsed + 0
dashboard regressions reported.

### D4 — Per-label migration rules

The following table is the **complete** disposition list. Any
`runner-*` or `claim:*` artifact not in this table MUST be re-classified
and re-filed before being written by any new code path.

| Label / comment | Source today | Class | Disposition | Implementation ticket |
|---|---|---|---|---|
| `claim:<instance>:<token>` (fencing) | `jira_dispatch._fenced_claim_label`, `claim_ticket_atomic` | **B-volatile** | **Move to Postgres** — `runner_claims.lease_id` + `fencing_token`; label dropped at Phase 3 | v2-⑩-2bc + v2-⑩-2-Cutover |
| `claim:<instance>` (legacy bare, pre-AUDIT-24) | `_our_claim_label`, `_claim_ticket_atomic_legacy` | **B-volatile** | Already deprecated by OP-977. Drop at Phase 3 along with the legacy code path | v2-⑩-2-Cutover |
| `runner-blocked:waiting-<KEY>` | `DEPENDENCY_WAITING_LABEL_PREFIX` (`jira_dispatch.py:1982`) | **B-volatile** | **Move to Postgres** — `runner_claims.state='blocked'` + `external_refs.waiting_on=[KEY,...]`; label dropped at Phase 3 | v2-⑩-2bc |
| `runner-loop-paused-pending-review` | `ci_recovery.LOOP_PAUSED_LABEL` | **B-volatile, cross-process** | **Move to Postgres** as a long-lived lease with `state='paused'` + `release_reason='operator-review'`; resume requires explicit operator unpause via RescueCLI | v2-⑩-RescueCLI |
| `[runner-presync-fail]` (comment) | `auto-runner-multi.py:414` | **Audit history** | **Keep in JIRA as comment**. Underlying state moves to `runner_claims.release_reason='presync_fail'`; comment becomes a human-readable mirror of the structured state, not the state itself | v2-⑩-2d |
| `[runner-live-state-fail]` (comment) | `auto-runner-multi.py:431` | **Audit history** | Same as `[runner-presync-fail]`: structured state in Postgres; comment retained for human audit | v2-⑩-2d |
| `[runner-mutex-blocked]` (comment) | runner pre-pickup path | **Audit history** | Structured state in Postgres (`state='blocked'` + winner lease ref); comment retained | v2-⑩-2bc |
| `[runner-workspace-tampered]` (comment) | runner pre-sync path | **Audit history** | Structured state in Postgres (`release_reason='workspace_tampered'`); comment retained. Note: per `feedback_claude_cli_workspace_tampered.md` this signal is currently unreliable on claude-2 worktree | v2-⑩-2d |
| `[runner-capability-blocked]` (comment) | OP-855 capability matrix path | **Audit history** | Structured state in Postgres (`release_reason='capability_blocked'` + denied tool list); comment retained | v2-⑩-2d |
| `[runner-dirty-worktree]` (comment) | runner pre-commit path | **Audit history** | Structured state in Postgres (`release_reason='dirty_worktree'` + dirty path list); comment retained | v2-⑩-2d |
| `[runner-provider-dispatch]` (comment) | `auto-runner-multi.py:469` | **Audit history** | Stays as JIRA comment. Routing decision is not a lock, so no Postgres mirror needed | (no change) |
| `[runner-pushed-to-gerrit]` (comment) | `jira_dispatch._post_gerrit_url_comment` (line 1512–1523) | **Audit history** | Stays as JIRA comment (consumed by `gerrit_jira_bridge.py:351`). Underlying state also lives in Postgres `external_refs.gerrit_change_url` so the bridge can be re-derived if the comment is lost | (no change to label/comment; v2-⑩-2d adds Postgres mirror) |
| `[runner-pre-review-self-fix-warning]` (comment) | OP-1057 / SP-B-X-001 path | **Audit history** | Stays as JIRA comment. Self-fix outcome is not coordination state | (no change) |

**Decision rule for future labels**: any new `runner-*` or `claim:*`
artifact MUST declare its class (A-durable / B-volatile) at filing time
and route through this ADR. The G.A-v2 filing-time validator
(`v2-A2 forbidden_combinations`) SHALL refuse a ticket that introduces a
B-volatile label without a Postgres mirror.

### D5 — Authority precedence under split

When the two surfaces disagree:

1. **Pre-Phase-2 (shadow)**: JIRA wins; Postgres is observation only.
2. **Phase-2 onwards (enforce)**: Postgres wins for *coordination*
   decisions (pickup eligibility, mutex hold, release). JIRA wins for
   *triage* decisions (operator-edited `area:*`, `tier:*`, ticket
   summary).
3. **Operator override**: Operator-driven label edits SHALL invalidate
   any in-flight lease via a coordination event, not via surprise at
   pre-submit. This is the `RescueCLI` path (v2-⑩-RescueCLI), audit-trailed
   per ADR-0034.

### D6 — RescueCLI as the only out-of-band release path

`omnisight runner-rescue {dump,release,reset}` (v2-⑩-RescueCLI) is the
**only** sanctioned way to force-release a stuck claim outside the
runner's `finally` path. Ad-hoc operator label stripping (the current
`feedback_stale_claim_labels.md` workaround) becomes obsolete in Phase
3. RescueCLI writes to `audit_log` per ADR-0034 and requires an L2
fingerprint.

## Consequences

### Positive

- **Atomicity**: claim acquisition + state transition + assignee
  ownership become one Postgres transaction. The dual-write window in
  Phase 1 is bounded; once cutover lands, the runner has a single lock
  surface.
- **Self-cleaning**: Postgres lease + heartbeat + `finally`-block
  release retire the 40 % stale-`claim:*` residue and the 45 %
  `runner-blocked` carry-over without operator intervention.
- **Triage hygiene**: JIRA labels shrink toward durable metadata
  (`area:*`, `tier:*`, `class:*`, `capability:enable=*`). The 22.4
  labels-per-ticket average drops materially. Dashboards built on JIRA
  labels remain valid because A-durable labels are unaffected.
- **OP-1070 regression class is closed at the source**: runtime
  artifacts no longer participate in pickup eligibility decisions
  because eligibility is computed from `runner_claims`, not from
  worktree cleanliness × label state.
- **SP-B-X-META 30-day stability gate becomes meetable**: the
  `runner_claim_stale`, `runner_pickup_block_rate`, and
  `runner_state_drift` alerts (v2-⑩-AlertRule) provide the
  observability that SP-B-X-META closure criteria require.

### Negative / Tradeoffs

- **Dual-write window**: For at least 1 week (Phase 1) and a 1-sprint
  read-compat tail (Phase 3), code writes both surfaces. This is a
  controlled cost — explicit in the migration plan, alerted on via
  `runner_state_drift` — but it does temporarily double the
  coordination write path.
- **New on-call surface**: `runner_claims` becomes a tier-S Postgres
  table. Stuck rows now require RescueCLI rather than JIRA hand-edit.
  Operators must learn the new tool; the runbook lives at
  `docs/sop/runner-pickup-mutex.md` (to be amended by v2-⑩-2d).
- **Backwards-compat read window only**: dashboards and JQL filters
  that read `claim:*` / `runner-blocked:*` for *current state* break
  at Phase 3 cutover unless re-pointed at Postgres before then. This
  is a known cost. Dashboards reading these labels for *historical
  audit* continue to work (comments are retained).
- **Cross-worktree visibility cost**: today every agent can read every
  ticket's labels via the JIRA REST API; Postgres requires the runner
  to hold DB creds. Per
  `reference_backend_credentials_model.md`, creds live in the
  `git_accounts` table and the runner already has Postgres access, so
  no new auth surface is added — but third-party agents that wanted to
  observe coordination state via JIRA alone now need an alternate
  read path (the v2-⑩-AlertRule + future read API).

### Risks

- **Cutover regression**: any consumer of `claim:*` / `runner-blocked:*`
  that we miss in the Phase-3 inventory breaks silently. Mitigation: the
  read-compat window + `runner_state_drift` alert + the explicit
  per-label disposition table in §D4 (which is grep-checkable against
  the codebase).
- **Schema drift between ADR and implementation**: the column list in
  §D2 is illustrative; the v2-⑩-1bc alembic migration is the
  authoritative artifact. Mitigation: v2-⑩-1bc is tier-S and codex-routed
  with explicit unit/integration tests; any deviation from this ADR's
  function signatures forces an ADR-0037 amendment.
- **Operator workflow drag**: operators currently dump stuck claims
  with `jira label remove`. RescueCLI requires L2 fingerprint per
  ADR-0034. Mitigation: RescueCLI ships with `dump` (read-only,
  fingerprint-free) before `release` / `reset` are wired so operators
  can adopt the diagnostic half first.

## Rejected alternatives

### R1 — Keep labels, add a per-label classifier

Continue using JIRA labels as the substrate but introduce a typed
`label_class` field on each label (A-durable / B-volatile / audit) and
let the runner only act on A-durable labels. **Rejected** because it
preserves every existing failure mode (non-atomicity, eventual
consistency, operator-editability) and only renames the problem. The
40 % `claim:*` residue is a *cleanup* failure, not a *classification*
failure.

### R2 — Use the worktree as the coordination substrate

Store lease + phase in a `.runner-coord/` directory inside each
worktree. **Rejected** because it inverts the original mistake: it
makes the worktree responsible for cross-worktree coordination state
again. Multiple agents working in different worktrees cannot read each
other's filesystem; cross-instance mutex requires a shared store.

### R3 — Use Redis or another lightweight KV

Add Redis or sqlite-WAL specifically for runner coordination.
**Rejected** because Postgres is already a deployment dependency,
already has audit / backup / replication paths, and is the system of
record for adjacent runner state (e.g. `audit_log`, `jira_operations`,
`git_accounts`). Introducing a second store doubles the operational
surface for no observable gain.

### R4 — Skip Shadow, go straight to Enforce

Land the Postgres substrate and immediately flip `find_mutex_holders` to
read from it. **Rejected** because the prior incident class (OP-977,
OP-1074, OP-1077) demonstrates that runner coordination changes deployed
without a parallel observation window introduce fleet-wide pickup DoS.
The 1-week shadow + `runner_state_drift` alert is non-negotiable.

### R5 — Drop labels entirely at Phase 3 (no read-compat window)

Cutover deletes label writes AND label reads simultaneously.
**Rejected** because operator dashboards, the Gerrit→JIRA bridge
(`gerrit_jira_bridge.py:351` reads `[runner-pushed-to-gerrit]`), and
ad-hoc JQL queries used in triage all depend on the existing surface.
A 1-sprint read-only compat window lets those consumers migrate before
their writes vanish.

## Relationship to other ADRs

### ADR-0035 — Runner FSM + Error Handling Contract (companion)

ADR-0035 codified the runner's top-level FSM, invariants I1–I6, the
Gerrit-first / JIRA-second external-mutation ordering (§C4), and the
idempotency primitives (§7). It is the **static** contract.

ADR-0037 is the **substrate** contract. It supersedes the implicit
label-based coordination substrate that ADR-0035's invariants assumed
but never named. Specifically:

- ADR-0035 I2 ("JIRA assignee = bot identity throughout `working`")
  remains binding. ADR-0037 narrows the *mechanism*: assignee mirrors
  the active Postgres lease, not the other way around.
- ADR-0035 I5 ("All side-effecting operations are idempotency-token
  guarded") remains binding. ADR-0037 makes the token surface
  Postgres-backed (`runner_claims.lease_id` + `fencing_token`)
  rather than `(ticket_key, operation, args_hash)` only.
- ADR-0035 §C4 Gerrit-first ordering is unchanged. ADR-0037 adds:
  Postgres `mark_terminal` runs after Gerrit push success and before
  JIRA transition, so a process death between Gerrit and JIRA leaves
  a recoverable `runner_claims` row rather than a `claim:*` label
  that nothing cleans up.

ADR-0035 will be amended in a follow-up edit to add a "Supersession
note: label-based coordination" pointer to ADR-0037. That amendment is
the Deploy AC for this ticket.

### ADR-0034 — Override Review Lifecycle + Separation of Duties

RescueCLI inherits ADR-0034's override audit-trail requirement.
Every `omnisight runner-rescue release` and `runner-rescue reset`
invocation writes an `override_record` to `audit_log` with L2
fingerprint, affected lease IDs, and operator justification. The
post-override reviewer requirement (Q5 in ADR-0034 §1) applies: the
operator who triggers a force-release cannot also approve the
follow-up review.

### ADR-0033 — Governance Engine + Operator Authority Hierarchy

ADR-0037 is an L3 (runner) contract. Lease state is read-write by the
runner; force-release is L2-gated via RescueCLI; structural changes to
`runner_claims` schema are L1-gated and require an ADR-0037 amendment.

### ADR-0023 — Foundation Rebuild

ADR-0023 is the implementation contract for ADR-0001 + ADR-0002. ADR-0037
does not amend ADR-0023, but Phase 2 enforce / Phase 3 cutover assume
that the per-pickup ephemeral-clone story (ADR-0023 §3.5) and the
ADR-0023 §11 phase-gate-evidence pattern are already in place; if
ADR-0023 §3.5 lands first, Phase 2's `finally`-block coverage of
pre-sync / dirty-worktree exits gets simpler.

## Migration validation

Per the codex audit §4 closing paragraph and the Family ⑩ validation
contract:

- Track the last 40 runner-handled tickets for 1 week after Phase 2
  enforce lands.
- Replay OP-1070-pattern (progress / sentinel runtime files present at
  finalization) as an explicit regression test in v2-⑩-Integration.
- On any post-cutover regression that traces to a label disposition
  this ADR got wrong, file the incident at
  `docs/audit/AUDIT-G-A-v2-Family-10/` and amend §D4 in this ADR.

## Open follow-ups (out of scope for this ADR)

The following are surfaced by the codex audit §6 but intentionally
deferred:

- Per-provider quota / circuit-breaker state (v2-⑩-5d) — depends on
  the substrate but adds new columns/tables; tracked separately.
- Capability registry typed policy (v2-⑩-5a / 5bc) — replaces flat
  `capability:enable=*` labels; same migration template as §D4 but a
  larger surface, so filed independently.
- Cross-provider model-deconfliction policy (v2-⑩-5e) — strategy, not
  substrate.
- Bridge-health graded contract (v2-⑩-4a / 4bc) — uses the substrate
  but the policy decision (graded vs hard gate) is the ticket's own
  contribution.

These tickets share Family ⑩'s critical path but are not gated by
ADR-0037 adoption beyond the shared `runner_claims` schema kernel.
