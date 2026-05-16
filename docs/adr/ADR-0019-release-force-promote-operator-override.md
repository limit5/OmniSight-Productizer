---
id: ADR-0019
title: release:force-promote operator emergency-override contract for R3 + downstream gates
status: Proposed
date: 2026-05-12
---

# ADR-0019 — `release:force-promote` operator emergency-override contract

- **Status**: Proposed (2026-05-12, AUDIT-18a / OP-966)
- **Deciders**: operator (`nanakusa sora`) + AI fleet; implementation gate: AUDIT-18b/18c
- **Tickets**: OP-966 (AUDIT-18a — this ADR), OP-925 (R3 cascade that motivated it), OP-960/ADR-0016 (D5 sibling), OP-944/ADR-0017 (conductor lifecycle), OP-954/ADR-0018 (L3 consumer), OP-961/AUDIT-13a (`release:force-create` pattern mirrored), OP-964/AUDIT-16 (`release_audit` operator-identity columns reused)
- **Blocks**: AUDIT-18b, AUDIT-18c (both MUST reference *this* ADR — see "Numbering note")

## Context

A release is a `RELEASE-vX.Y.Z` META plus 13 child tickets wired by `blockedBy`;
the JIRA graph *is* the state machine and the runner JQL is its executor
(ADR-0017). Step R3 advances `develop → main`
through Gerrit review after the OP-868 milestone-acceptance check passes
(`scripts/release_milestone_checker.py` → `milestone_ready`), and
`backend/agents/auto_promote_main.py` (ADR-0016) builds the `refs/for/main`
change off that event.

The first live R3 attempt for RELEASE-v0.5.0-rc1 (OP-925, 2026-05-12) cascaded
into three bugs (AUDIT-11/12/13). Even with those fixed, R3 still has hard
external dependencies — a green CI/milestone signal, the Gerrit ACL, the
acceptance check's own correctness. When any of them is red or merely flaky,
the **entire RELEASE chain stalls** and the only escape today is editing JIRA
tickets by hand or hand-patching the checker — both unauditable.

AUDIT-17 (staging-gate infrastructure) will make the gate *better*; it does not
remove the need for an operator escape hatch when even a good gate is wrong or
its infra is down. AUDIT-18 is the **orthogonal** "operator emergency override"
path so RELEASE chains never have to wait on a perfect CI signal. Both ship;
neither replaces the other.

`release:force-create` / `release:skip-auto-conductor` (OP-961) already give the
operator control over **cron's META instantiation**. There is no equivalent once
the META exists and the chain is running. `release:force-promote` fills that gap
— a different lifecycle stage, hence a different label.

| Label | Lifecycle stage | Behavior |
|---|---|---|
| `release:force-create` (OP-961) | cron META instantiation | create `RELEASE-vX.Y.Z` META even when acceptance is not green |
| `release:skip-auto-conductor` (OP-961) | cron META instantiation | skip auto-creating the META entirely |
| **`release:force-promote` (this ADR)** | R3 + downstream promotion gates | treat blocked CI gates as green; emit a synthetic `milestone_ready`-equivalent |

## Decision

`release:force-promote` is a **JIRA fixVersion label** (set on the version
object — same surface as `release:force-create`, not a per-ticket label). When
present, *every* gate-checking code path that participates in R3 and downstream
promotion MUST:

1. **Read** the fixVersion label set **at gate-evaluation time** — never a value
   cached at META-creation time. Operators add the label *mid-chain*, after a
   gate goes red.
2. **Emit** the NEW event type `milestone_force_promoted` — distinct from both
   `milestone_ready` and `milestone_blocked` — in place of what would otherwise
   be `milestone_blocked`. Consumers treat it as a green-equivalent for control
   flow but as a flagged/abnormal outcome for audit and display (log level
   `WARN`, mirroring `release_milestone_checker.py`'s existing level rule). When
   gates are *actually* green the override is a **no-op**: the normal
   `milestone_ready` is emitted, with no warning block — so the warning block
   stays meaningful (it appears only when something was actually bypassed).
3. **Prepend** an `OPERATOR FORCE-PROMOTE WARNING: …` block to every downstream
   artifact it generates — the META description, the `release_audit` row
   `detail`, and the Gerrit auto-promote change description / commit message. The
   block names: the label, the operator identity + hostname, the timestamp, the
   gate(s) that were red, and a pointer to this ADR + the force-promote runbook
   section. It is **non-removable** — it rides into the `develop → main` commit
   that ships to prod, which is the point: that commit permanently records it
   arrived on an override.
4. **Audit** the activation to `release_audit` with `outcome=force_promoted`,
   plus the operator hostname/identity columns (already present from AUDIT-16 —
   reused, no schema change), the bypassed gate names, and the resolved
   fixVersion.

### Code paths bound by this contract

- `scripts/release_milestone_checker.py` — the R3 / OP-868 acceptance check.
  Currently emits `milestone_ready` | `milestone_blocked`; gains
  `milestone_force_promoted`.
- `backend/agents/auto_promote_main.py` — D5 `develop → main` (ADR-0016).
  Currently triggers git writes **only** on `event == "milestone_ready"`; MUST
  additionally accept `milestone_force_promoted` and propagate the warning block
  into `PROMOTE` change description + `release_audit` detail.
- the Sprint H L3 event-driven conductor (ADR-0018) — `milestone_force_promoted`
  is a **recognized** state in its milestone-event handling, not an unknown that
  dead-letters; its idempotency guard already tolerates duplicate events (re-run
  R3 ⇒ another `milestone_force_promoted` ⇒ safe).
- `scripts/release_conductor_cron.sh` — must accept `release:force-promote` as a
  **valid** fixVersion label (not a `LabelInvalid`), even though cron itself
  takes no action on it (cron acts at instantiation, not at gate time).

### Frozen wire contract (immutable — changing any of these needs a superseding ADR)

| Surface | Frozen value | Primary consumers |
|---|---|---|
| fixVersion label string | `release:force-promote` | checker, `auto_promote_main`, L3 conductor, conductor-cron label validator |
| event type | `milestone_force_promoted` | L3 conductor state machine, any milestone-event subscriber, `auto_promote_main` |
| `release_audit.outcome` value | `force_promoted` | `release_audit` queries, release dashboards |
| artifact warning prefix | `OPERATOR FORCE-PROMOTE WARNING:` | log scrapers, Gerrit change-description renderer |

The label is lowercase, colon-namespaced, hyphenated. The underscored variant
`release:force_promote` is **invalid** — the conductor cron's `LabelInvalid`
check rejects it, mirroring the existing `release:force_create` rejection test.

### Interaction with the existing labels

`release:force-promote` is **independent** of `release:force-create` and
`release:skip-auto-conductor` — they act at different lifecycle stages. All
combinations are legal; each label is honored at its own stage. Implementations
MUST NOT add a "mutually exclusive with force-create" check (that check exists
only between `force-create` and `skip-auto-conductor`, which *do* collide).

## Alternatives considered

### Alt-1 — Per-call CLI flag (`--force-promote` on the checker / D5 script)
**Rejected.** A flag is invisible after the fact: it never appears on the JIRA
graph that *is* the state machine, so a later auditor reading the release META
has no record an override happened. The fixVersion label is durable, JQL-
queryable, and already read by every consumer that touches the version object.
(Same single-source-of-truth reasoning as the OP-925 post-mortem.)

### Alt-2 — A separate JIRA workflow state ("Force-Promoted")
**Rejected.** Adds workflow-schema churn (every release child would need the new
transition) and mis-models the concept — the override is a property of the
*fixVersion*, not of any one ticket. Labels are the right granularity.

### Alt-3 — Time-based bypass (auto-treat gates as green after red for > N hours)
**Rejected.** Silent automatic bypass is precisely the failure mode this
contract guards against. An override must be a deliberate, attributable operator
act. A stuck gate should page a human, not self-clear.

### Alt-4 — Hand-patch the milestone checker's pass/fail logic per incident
**Rejected** (today's implicit status quo). Unauditable, error-prone, easy to
forget to revert.

### Alt-5 — Overload `release:force-create` to cover the gate path too
**Rejected.** An operator who wanted only "let cron make the META early" would
also silently disable the R3 gate. Distinct concerns ⇒ distinct labels — the
same separation the table above encodes.

### Alt-6 — No override; rely entirely on AUDIT-17 staging gates
**Rejected as the *only* answer.** AUDIT-17 improves the gate; it cannot cover
"the gate (or its infra) is itself wrong." Orthogonal problems; both ship.

## Consequences

### Audit obligations
- Each activation produces **one** `release_audit` row (`outcome=force_promoted`)
  per gate evaluation that consumed the label — R3 re-runs each emit a row, so
  the trail shows how hard the override was leaned on.
- The `OPERATOR FORCE-PROMOTE WARNING:` block propagates into the Gerrit
  auto-promote change description and therefore into the prod-shipped
  `develop → main` commit. Together with the `release_audit` rows and the Gerrit
  receive log this is the "did main move, and on whose override" forensic record.
- No `release_audit` schema change — the operator hostname/identity columns from
  AUDIT-16 are reused. If a needed column turns out missing, that is an
  AUDIT-18b/c discovery, not an amendment to this ADR.

### Runbook updates needed (deferred to AUDIT-18b/18c, intentionally)
`docs/operations/release-conductor-runbook.md` and the D5 / release-cut runbook
gain a **"Force-promote emergency procedure"** section: when it is appropriate,
who may set the label, the requirement to file an incident ticket alongside, and
how to confirm it took effect (a `milestone_force_promoted` event +
`outcome=force_promoted` audit row + the warning block visible in the Gerrit
change). These edits land **with the implementation** so design intent, code,
and operator surface ship in one reviewable chain (per L-OP-954).

### Retry / re-entrancy semantics
- **Level-triggered, not edge-triggered**: while the label is present every
  subsequent gate evaluation re-honors it; removing it restores normal gating on
  the next evaluation (which blocks again if gates are still red).
- A force-promoted R3 does **not** "stick" to downstream steps — R4/R5/… each
  evaluate their own gates and read the same fixVersion label only if they opt
  in. AUDIT-18b/18c enumerate which downstream gates are force-promote-eligible.
- Idempotent: re-running the checker with the label set is safe; consumers must
  tolerate duplicate `milestone_force_promoted` events (the L3 idempotency guard
  already does).

## Numbering note

OP-966 pre-assigned this design "ADR-0018" and reserved "ADR-0017" for AUDIT-17.
By authoring time both numbers were already taken on the integration branch:
**ADR-0017** = "Release conductor half-automation" (OP-944), **ADR-0018** =
"Event-driven release pipeline" (OP-954). Per the L-OP-870 pre-check
(`ls develop:docs/adr/ADR-001[78]*` before commit) this ADR takes the next free
number — **ADR-0019**. No ADR number is reserved for AUDIT-17; that design takes
whatever is free when written. Implementation tickets **AUDIT-18b / AUDIT-18c
reference ADR-0019**, not ADR-0018.

## References

- OP-925 — R3 cascade incident, 2026-05-12 (the motivating failure).
- ADR-0016 / OP-960 — D5 `develop → main` via Gerrit review (`auto_promote_main`
  is a contract consumer here).
- ADR-0017 / OP-944 — Release conductor half-automation (the lifecycle this
  override plugs into).
- ADR-0018 / OP-954 — Event-driven release pipeline (the L3 conductor that must
  recognize `milestone_force_promoted`).
- OP-961 / AUDIT-13a — `release:force-create` / `release:skip-auto-conductor`
  (the existing operator-label pattern mirrored here).
- OP-964 / AUDIT-16 — `release_audit` operator hostname/identity columns reused.
- L-OP-954 — finalize event-driven ADRs only after load evidence (why runbook
  edits are deferred to the implementation tickets).
- L-OP-870 — sibling-discipline / ADR-number pre-check.
- `scripts/release_milestone_checker.py`, `backend/agents/auto_promote_main.py`,
  `scripts/release_conductor_cron.sh` — the in-scope code paths.
