---
id: SPRINT-S12G-V2-FAMILY10-RUNNER-DEFENSE-CONTRACT
version: v1 (2026-05-16)
title: G.A-v2 Family ⑩ — Runner Defense Contract + state-separation spec (claim ownership + label taxonomy)
scope: Contract spec for Family ⑩ runner defense — formalises the state-separation contract between the human-facing JIRA ledger (A-class durable labels) and the Postgres-backed runner-coordination substrate (B-class volatile runtime state); catalogues every existing `runner-*` and `claim:*` label, classifies each A-class vs B-class, and defines the strangler-pattern migration sequence the rest of Family ⑩ (16 downstream impl tickets + ADR-0037) consumes. Doc-only ticket (v2-⑩-1a / OP-1156); no runtime, db, devops, embedded, frontend, security, tests, or tooling code is touched here. Defense dimensions D1+D2+D3+D4+D5 (all five — the most comprehensive family in G.A-v2).
status: Draft — OP-1156 (this ticket); architectural decision LOCKED 2026-05-14 (parent spec v1.2 Family ⑩ addition, codex Package 2 self-audit §0+§4)
related:
  - sprint-s12g-A-v2-runtime-defense-contract-spec.md §3 "Family ⑩ — Runner Defense Contract" (parent spec)
  - 2026-05-16-v2-alertbridge-framework-contract.md (shared alert plumbing — §11 alert routing of this doc cites it)
  - 2026-05-16-v2-family9-aux-service-contract.md (sibling spec; same docs-only filing shape)
  - backend/agents/runner_coordination.py (518 LOC; shipped OP-1106 / v2-⑩-1bc — shadow-only at land time; §10 catalogues partial coverage)
  - backend/agents/runner_stoploss.py (216 LOC; shipped OP-1140 — per-ticket §11-revert circuit breaker; §10 catalogues coverage)
  - backend/agents/runner_artifacts.py (74 LOC; shipped OP-1111 — `RUNNER_RUNTIME_ARTIFACTS` constant; addresses Mode 1)
  - backend/alembic/versions/0236_runner_claims.py (shipped OP-1106 — `runner_claims` table; canonical schema)
  - backend/alembic/versions/0237_runner_audit_events.py (shipped OP-1118 — operator-override audit trail)
  - docs/sop/runner-pickup-mutex.md (OP-977 fencing-token pattern doc — this spec extends it; does NOT replace its rationale)
  - docs/audit/codex-reviews/runner-self-audit-and-redesign-2026-05-14.txt (codex Package 2 self-audit — the lived-experience evidence behind this family)
  - docs/adr/ADR-0035-runner-fsm-and-error-handling.md (FSM definitions A-class state lives in)
  - JIRA OP-1156 (this spec) ; OP-1157+ (v2-⑩-ADR / 1bc / 2-Shadow / 2bc / 2d / 2-Cutover / 3 / 4a / 4bc / 5a / 5bc / 5d / 5e / RescueCLI / AlertRule / Integration — 16 downstream children that consume this doc's §-anchors)
  - JIRA SP-B-X-META (unblocks when v2-⑩-AlertRule fires at least 1 of {C9 bridge-health, C10 instance-drift, C11 human-authority-yield, C12 state-authority-precedence}; §11 of this doc is the binding criterion)
---

# G.A-v2 Family ⑩ · Runner Defense Contract — v1 (2026-05-16)

## §0. Pre-flight survey (what already exists on `develop`)

This spec is filed AFTER the first three Family ⑩ implementation tickets have
already shipped to `develop`. Per the
[[feedback_filing_existing_impl_check]] discipline (OP-1145 incident
2026-05-16: filed v2-AlertBridge-1bc without checking that OP-1103 had
already shipped `bridge.py`; codex faithfully overwrote 324 lines), this
doc starts by enumerating concrete on-disk state so downstream impl
tickets do NOT re-derive or accidentally overwrite landed code.

**Survey performed 2026-05-16 against `refs/remotes/origin/develop`:**

| Artefact | Lines | Landed by | Status vs. spec |
|---|---:|---|---|
| `backend/agents/runner_coordination.py` | 518 | OP-1106 (v2-⑩-1bc) | ✅ All 4 contracted functions (`acquire_claim`, `release_claim`, `record_phase`, `find_active_holders`) PLUS 3 extensions (`expire_stale_active_claims`, `force_release_claim`, `record_audit_event`) live; shadow-only at land time per its module docstring |
| `backend/alembic/versions/0236_runner_claims.py` | 118 | OP-1106 (v2-⑩-1bc) | ✅ `runner_claims` table created with all 13 contracted columns + 1 partial unique index (`uq_runner_claims_resource_active`) + 2 supporting indices (`idx_runner_claims_ticket`, `idx_runner_claims_heartbeat_active`); Postgres + SQLite DDL both present |
| `backend/alembic/versions/0237_runner_audit_events.py` | 91 | OP-1118 (v2-⑩-RescueCLI) | ✅ `runner_audit_events` append-only audit trail (id / ts / action / operator_fingerprint / target_lease_id / target_ticket_key / details JSONB); used by `record_audit_event()` above |
| `backend/agents/runner_stoploss.py` | 216 | OP-1140 | ✅ Per-ticket §11-revert circuit breaker — orthogonal to the coordination substrate; lives entirely in JIRA-label-space (B-class `runner-stoploss:revert-<ts>` + `runner-stoploss:circuit-tripped-<ts>`); §2 of this doc classifies its labels |
| `backend/agents/runner_artifacts.py` | 74 | OP-1111 (v2-⑩-3) | ✅ Closes Mode 1 (runner-owned artefacts make successful work look unsafe — OP-1070 root cause); centralised `RUNNER_RUNTIME_ARTIFACTS` consumed by every dirty-check / stash / Change-Id call site |
| `backend/agents/runner_progress.py` | 458 | (earlier — pre-Family ⑩) | ⚠ Predates this contract; its labels (`runner-progress:*`) need §2 classification — they are B-class (runtime checkpoint) and will migrate to the substrate at v2-⑩-2-Cutover |
| `backend/agents/jira_dispatch.py` `claim_ticket_atomic()` (OP-977) | n/a | (referenced) | ⚠ STILL the live mutex on `develop`; substrate is shadow-only; the cutover sequence in §5 of this doc is the migration plan |
| `docs/sop/runner-pickup-mutex.md` (OP-977 fencing-token pattern doc) | n/a | (referenced) | ✅ Still authoritative for the **label** mutex; this spec extends the rationale to the substrate without superseding the doc until §5 cutover lands |

**What is therefore NEW in this spec (not duplicating prior work):**

- §1: explicit state-separation contract (JIRA ledger = A-class; Postgres = B-class) — this concept is named in the parent spec but never written down at section-anchor granularity until here.
- §2: complete A-class / B-class taxonomy table covering 23+ existing `runner-*` and `claim:*` label families (plus the 11 other label families touched by the runner — `capability:*`, `prefer:*`, `class:*`, `tier:*`, `area:*`, `parent:*`, `scope:*`, `ticket-id:*`, `mutex:*`, `runner-stoploss:*`, `runner-blocked:waiting-*`) — codex's audit referenced "labels are split" but never enumerated.
- §5: strangler-pattern migration **sequencing** — which downstream ticket flips which sub-contract on, in what order, with which rollback flag.
- §6: validation contract **targets + how to measure** — codex's "40% → 0%" / "45% → <10%" numbers are referenced but the measurement mechanism is defined here for the first time.
- §8: bridge-health **graded** contract (v2-⑩-4a stake) — capability-scoped, not global; per-capability hard/soft gate distinction.
- §9: typed **capability registry** structure (v2-⑩-5a stake) — provider × model × tools × max-tier × cost-mode × health × known-failure-classes — replacing the flat `capability:enable=*` label namespace.
- §10: comprehensive partial-coverage map so downstream tickets file `closes-gap-x` not `creates-feature-y`.
- §11: binding SP-B-X-META unblocking criterion (1-of-4 alerts).
- §12: ADR-0037 cross-link with the architectural decision tree.

Downstream tickets MUST cite the §-anchors of this doc when their AC references contract surface — see §13.4 for the hand-off block listing every downstream ticket that consumes which anchors.

---

## §1. State-separation contract

### §1.1 The two stores, in one paragraph

> *"JIRA is the **ledger** — durable, human-readable, audit-friendly ticket metadata. Postgres `runner_claims` is the **runtime state machine** — volatile, machine-only, mutex-and-heartbeat. Anything that needs a human to read it lives on JIRA; anything that participates in a fencing-token-mediated mutex lives in Postgres. The migration from today's mixed-substrate world (every state bit lives in JIRA labels) to the contract is **strangler-pattern**: shadow-write 1 wk → enforce-from-table → drop label-based writes (keep read-only compat 1 sprint for dashboards)."*

### §1.2 Why the two-store split is non-negotiable

The 2026-05-14 codex Package 2 self-audit (referenced top of this file)
established three concrete failure modes (named in §7 of this doc) that
share one root cause: **JIRA's label data model does not support
compare-and-swap**. Atlassian's `update.labels.add` is a set-union — two
runners writing the *same* label both succeed and both read it back, so
each thinks it won the mutex. OP-977 (AUDIT-24) patched this by extending
the label payload to `claim:{instance_id}:{epoch_us:016d}-{uuid8}` and
applying *lowest-token-wins* on readback (`docs/sop/runner-pickup-mutex.md`),
which is correct but is a workaround for a substrate that fundamentally
isn't a database.

Three categories of behaviour that the label substrate cannot serve
correctly, even with OP-977's fencing tokens:

1. **Heartbeat.** A heartbeat is a per-second update; PUT-ting a JIRA
   label every second per active runner would exceed Atlassian Cloud's
   per-project write rate limits within minutes. The label substrate
   cannot represent liveness.
2. **Phase transitions.** OP-1070-style debugging needs to answer "where
   was the runner when it died?" — pickup / capability-resolved /
   in-progress / pre-push / pushed / under-review. Encoding this in
   labels is either glyph-noise (each phase a label) or lossy (one label
   with the latest phase). A first-class column is the only sane shape.
3. **Idempotent release.** The `finally`-block release pattern depends
   on releasing being a no-op when the lease is already released
   (because a finally may fire on a path that already released earlier).
   Label removal in JIRA is a destructive PUT, not idempotent at the
   protocol level; race scenarios produce ghost claims.

The substrate fixes all three at once: heartbeat is an UPDATE on a row
(cheap, no rate limit), phase is a column (typed, queryable), release is
an UPDATE setting `state='released'` (idempotent because the WHERE clause
restricts to `state='active'`). This is not optional — it is the only
shape that makes the contract correct.

### §1.3 What stays on JIRA, what moves to Postgres

| Concern | Store | Rationale |
|---|---|---|
| Ticket title, description, AC, priority, fixVersion, sprint | **JIRA** | Human-authored; human-edited; audit lives in JIRA changelog API |
| Issuetype, status (To Do / In Progress / Under Review / Done), assignee | **JIRA** | JIRA workflow engine owns transitions; Postgres has no business expressing them |
| A-class labels (see §2) — durable scope metadata: `tier:*`, `class:*`, `prefer:*`, `area:*`, `parent:*`, `scope:*`, `ticket-id:*`, `capability:enable=*`, `capability:disable=*` (legacy until §9 cuts over) | **JIRA** | Human-set at filing time; consulted by capability_matrix / scoping decisions; survives runner restart by design |
| ADR cross-references (`ADR-NNNN` labels) | **JIRA** | Documentation cross-link; not state |
| B-class labels (see §2) — volatile runtime state: `claim:*`, `runner-pickup-mutex`, `runner-blocked:*`, `runner-pushed-to-gerrit` (comment), `runner-mutex-lost`, `runner-claim-stale`, `runner-state-authority-precedence`, all `runner-progress:*`, `runner-cwd-sentinel` (referenced from worktree) | **Postgres `runner_claims` + columns therein** | Volatile; rewritten on every pickup attempt; no human authorship; humans never edit these |
| `runner-stoploss:*` labels (per-ticket §11-revert circuit breaker, OP-1140) | **JIRA (special case)** | Discussed in §2.3: this is the lone B-class label family that REMAINS on JIRA — its store is intentionally the ticket because the breaker survives across runner restarts AND humans (operators) read & clear it from the JIRA UI. The substrate has no role here; §5 cutover does NOT move this. |
| Per-file mutex (`mutex:<path>` labels for parallel-runner file-overlap gate, OP-731/800/687) | **JIRA today; Postgres after a separate Family ⑩ amendment** | Out of scope for v1 of this contract; called out in §13.1 changelog/open-questions to revisit. The current label-based file mutex is correct and small; the runner-claim mutex is the bigger problem. |
| Heartbeat | **Postgres** | Sub-second cadence; impossible in JIRA |
| Phase | **Postgres** | Typed enum |
| Lease ID + fencing token | **Postgres** | First-class; replaces the `claim:{instance}:{epoch_us}-{uuid8}` label encoding |
| Audit trail of operator overrides | **Postgres `runner_audit_events`** | Append-only; queryable; per-incident review |
| Operator-facing summary of a stuck claim | **JIRA comment posted by the rescue CLI** | Humans read JIRA, not psql |

### §1.4 The two-store invariant (binding on every downstream ticket)

> *"For every concern, exactly **one** of {JIRA labels, Postgres `runner_claims`} is the **source of truth**. The other may carry a stale read-only mirror during transition periods (§5 strangler shadow) but MUST be marked `(mirror — see <anchor>)` in its module docstring so future readers do not mistake it for the source."*

Any downstream impl ticket that adds a new concern MUST pick a side and
document why; tickets that straddle (write to both as if both authoritative)
are §11-revertable per the parent spec's `defense_contract` field.

---

## §2. A-class vs B-class label taxonomy

This section is the **binding** classification of every label family the
runner writes, reads, or filters on. Downstream tickets MUST use this
table when deciding whether a new label is A-class (stays on JIRA) or
B-class (moves to Postgres). New label families introduced in downstream
tickets MUST add a row to this table via spec amendment (one-line PR).

### §2.1 Classification rules (how a label gets classified)

A label is **A-class** if ALL of the following are true:

1. Set by a **human** at filing time (or by `scripts/file_jira_ticket.py`,
   which is a human-driven tool), and not subsequently mutated by the
   runner.
2. Read by `capability_matrix.resolve_for_areas()` or
   `pre_pickup_ok()` or any scoping / routing decision.
3. Has lifetime ≥ the ticket itself (i.e., never auto-pruned).
4. Carries semantic meaning a human reader would need to interpret the
   ticket — removing it would confuse a reviewer.

A label is **B-class** if ANY of the following are true:

1. Set by the runner during a pickup attempt and removed (or expected to
   be removed) at the end of the same pickup.
2. Encodes mutex / lease / fencing-token state.
3. Encodes a phase / heartbeat / liveness signal.
4. Lifetime is bounded by a single runner invocation (or by an operator
   override that clears it).
5. Would be more correctly expressed as a row in `runner_claims` (or a
   column thereon).

If a label satisfies both lists, classify by which list contains MORE
criteria — ties are A-class by default (keep on JIRA; safer fallback).

### §2.2 A-class labels (stay on JIRA — durable ledger)

| Label family | Example | Set by | Read by | Purpose | Survives runner cutover? |
|---|---|---|---|---|---|
| `tier:*` | `tier:S`, `tier:M`, `tier:L`, `tier:X` | Human filing | capability_matrix, runner JQL | Pickup eligibility + downstream review gating | ✅ Yes — never moves |
| `class:*` | `class:subscription-claude`, `class:subscription-codex`, `class:operator-window`, `class:operator-rehearsal`, `class:api-anthropic` | Human filing | capability_matrix, runner JQL | Routing (which agent class picks up) | ✅ Yes |
| `prefer:*` | `prefer:claude`, `prefer:codex` | Human filing | runner JQL ORDER-BY tiebreak | Tie-break when multiple agents could pick up | ✅ Yes |
| `area:*` | `area:backend`, `area:docs`, `area:devops`, `area:db`, `area:embedded`, `area:frontend`, `area:security`, `area:tests`, `area:tooling` | Human filing | capability_matrix (resolves enabled capabilities per area), runner JQL | Pickup eligibility + capability resolution + boundary block | ✅ Yes |
| `parent:*` | `parent:META-ATLAS`, `parent:OP-1098` | Human filing | UI filtering; META roll-up rendering | Ticket family membership | ✅ Yes |
| `scope:*` | `scope:sprint-atlas`, `scope:s12-g-a-v2` | Human filing | Sprint roll-up; release-notes generator | Sprint / sub-phase membership | ✅ Yes |
| `ticket-id:*` | `ticket-id:atlas-X-1a` | Human filing | Cross-ref to spec doc | Spec-doc back-link | ✅ Yes |
| `capability:enable=*` | `capability:enable=code_edit`, `capability:enable=gerrit_push`, `capability:enable=jira_update` | Human filing | tool_dispatcher CapabilityNotPermitted gate | Capability whitelist (OP-855 matrix) | ⚠ A-class TODAY; v2-⑩-5a/5bc/5d/5e migrate to typed `capability_profile` table (§9) — labels remain readable for 1 sprint compatibility window |
| `capability:disable=*` | `capability:disable=run_tests` | Human filing | tool_dispatcher | Per-ticket capability subtraction | ⚠ Same as `enable=*` — see §9 |
| `runner-needs-refinement` | (literal) | Operator | runner JQL exclude | Manual hold; ticket needs human re-scope before runner can attempt | ✅ Yes — set & cleared by humans only |
| `runner-stoploss:circuit-tripped-<ts>` | `runner-stoploss:circuit-tripped-20260515T143020Z` | runner_stoploss.py (OP-1140) | `pre_pickup_stoploss_ok()` | Per-ticket §11-revert circuit breaker | ✅ STAYS on JIRA per §1.3 special-case — operator-readable + survives runner restart |
| `runner-stoploss:revert-<ts>` | `runner-stoploss:revert-20260515T140000Z` | runner_stoploss.py (OP-1140) | Counter for trip threshold | Per-revert tally | ✅ Same as above |
| ADR cross-ref labels | `ADR-0023`, `ADR-0033`, `ADR-0034`, `ADR-0035`, `ADR-0037` (this family) | Human filing or spec author | UI search | Spec-doc back-link | ✅ Yes |

### §2.3 B-class labels (move to Postgres — volatile runtime state)

| Label family | Example | Today's writer | Today's reader | Target store after cutover | Cutover ticket |
|---|---|---|---|---|---|
| `claim:*` bare (OP-838 legacy) | `claim:claude-1` | none (SUPERSEDED) | none | n/a — legacy treated as stale per OP-977 | already done OP-977 |
| `claim:{instance}:{epoch_us}-{uuid}` (OP-977 fencing token) | `claim:claude-1:0001715000000000000-ab12cd34` | `claim_ticket_atomic()` | `find_mutex_holders()` | `runner_claims.fencing_token` column (same encoded value; same lowest-wins lexicographic order) | v2-⑩-2bc (replace JQL read), v2-⑩-2d (replace acquire), v2-⑩-2-Cutover (drop write) |
| `runner-pickup-mutex` | (literal — older same-ticket marker) | (legacy) | (legacy) | `runner_claims` row, `state='active'` | v2-⑩-2-Cutover |
| `runner-blocked:waiting-<reason>` | `runner-blocked:waiting-blocker-OP-1042` | runner waits-on resolver | runner JQL | `runner_claims.phase` value `blocked-waiting`; `runner_claims.external_refs` carries the reason JSON | v2-⑩-2d + v2-⑩-2-Cutover |
| `runner-capability-blocked` | (literal) | capability_matrix safe-default leak detector | runner JQL exclude | `runner_claims.phase` value `capability-blocked`; row's external_refs carries detected capability gap | v2-⑩-2d |
| `runner-claim-stale` | (literal — marker for pre-AUDIT-24 stale bare claims) | OP-977 sweeper | (operator review) | n/a (the sweeper itself goes away once OP-977 labels are not written; see v2-⑩-2-Cutover Code AC) | v2-⑩-2-Cutover |
| `runner-mutex-lost` | (literal — log marker when a runner discovers it lost the lowest-token race) | `claim_ticket_atomic()` | (operator review) | `runner_claims.release_reason='mutex-lost'`; lease snapshot in audit-event row | v2-⑩-2d |
| `runner-mutex-blocked` | (literal) | OP-977 pre-pickup | (operator review) | `runner_claims` row by another owner; `find_active_holders()` is the read | v2-⑩-2bc |
| `runner-mutex-override:file-overlap` | (literal) | operator | `pre_pickup_ok()` file-overlap gate | STAYS in JIRA — operator-driven, human-readable override; future Family ⑩ amendment may consider but NOT in this contract | (none — out of scope) |
| `runner-mutex-api-error` | (literal — JIRA write-error marker) | OP-977 acquire fallback | (operator review) | n/a — disappears with the JIRA writes themselves | v2-⑩-2-Cutover |
| `runner-pushed-to-gerrit` | (comment marker, not label) | jira_dispatch `post_runner_pushed_comment()` | (operator review, dedupe by `runner_comment_dedupe.py`) | STAYS as JIRA comment — it's a human-facing ledger entry (operator wants to see "patchset on gerrit at URL X" in the ticket), not runtime state | (none — already correctly A-class qua comment) |
| `runner-push-fail`, `runner-push-fail:transient`, `runner-push-fail:unknown` | (literal — gerrit push failure markers) | runner finalize | runner JQL | `runner_claims.release_reason='push-fail'` with subtype in external_refs; companion JIRA comment STAYS for human visibility | v2-⑩-2d |
| `runner-no-commits-from-<inst>`, `runner-no-commits-from-cli` | (literal — terminal "CLI exited without writing commits" markers) | runner finalize | runner JQL exclude (avoids retry) | `runner_claims.release_reason='no-commits'`; companion JIRA comment STAYS for human visibility | v2-⑩-2d |
| `runner-gerrit-setup-fail` | (literal) | runner pre-push | (operator review) | `runner_claims.release_reason='gerrit-setup-fail'`; companion comment STAYS | v2-⑩-2d |
| `runner-state-authority-precedence` | (literal — bot vs human concurrent edit marker) | jira_dispatch concurrent-edit handler | (operator review) | `runner_audit_events` row with action `state-authority-precedence`; companion comment STAYS | v2-⑩-2d |
| `runner-cwd-sentinel` (the file, NOT a label — sentinel in worktree) | `.runner-cwd-sentinel` | runner pre-pickup | runner pre-pickup re-check | `runner_claims.external_refs.worktree_path` (so the claim itself attests workspace ownership); file remains as belt-and-suspenders | v2-⑩-2d + OP-1111-extension (gitignore + cleanup) |
| `runner-progress:*` | (per-phase progress markers from runner_progress.py) | runner phases | runner finalize | `runner_claims.phase` (typed enum) — progress.txt itself stays as a per-pickup file but **NOT** committed (per OP-1111 gitignore) | v2-⑩-2-Cutover |
| `mutex:<file-path>` (file-overlap mutex, OP-731/800/687) | `mutex:backend/agents/jira_dispatch.py` | runner pre-pickup file_mutex_check | runner pre-pickup | STAYS on JIRA for v1 (out of scope — see §1.3); future amendment may revisit | (none — out of scope) |

**Total label families covered: 23** (10 A-class + 12 B-class + 1 special-case
mutex:file-overlap noted out of scope).

### §2.4 The `runner-blocked` overshadow problem

Today, multiple distinct conditions all collapse onto the SAME
`runner-blocked:*` label space, making it impossible to triage:

- `runner-blocked:waiting-blocker-<KEY>` — blocked by a Blocks-inward ticket
- `runner-blocked:waiting-bridge-stale` — bridge heartbeat too old (today's hard global gate; v2-⑩-4a fixes)
- `runner-blocked:waiting-capability` — safe-default leak (already partially handled by `runner-capability-blocked` but overlap is real)
- `runner-blocked:waiting-file-mutex` — file-overlap mutex held by another runner
- `runner-blocked:waiting-claim` — same-ticket mutex held (already handled by `runner-mutex-blocked` but again overlap)

Every one of these has a distinct downstream meaning; collapsing them to
the same label is the root of the **45% `runner-blocked`** number in the
codex Package 2 audit. After this contract, each becomes a distinct
`phase` value on the `runner_claims` row, and the label namespace either
goes away (substrate is authoritative) or becomes a 1-bit "is currently
blocked yes/no" tag whose detail lives in the row's `external_refs`.
The validation contract in §6 measures the resulting drop.

---

## §3. `runner_claims` table schema

This is the binding schema. The on-disk DDL is already shipped at
`backend/alembic/versions/0236_runner_claims.py` (OP-1106). This section
documents the **contract**, NOT the DDL — the DDL must conform to this
contract on every future migration.

### §3.1 Columns (binding)

| Column | Type | Constraint | Purpose |
|---|---|---|---|
| `lease_id` | TEXT | PRIMARY KEY | Client-generated UUID4 per acquire attempt; immutable; the handle the caller holds for release / heartbeat |
| `ticket_key` | TEXT | NOT NULL | JIRA key (e.g., `OP-1156`); FK in spirit only (we don't enforce against an external system) |
| `resource_key` | TEXT | NOT NULL | The mutex resource — `mutex:ticket:<KEY>` for same-ticket pickup mutex, `mutex:file:<path>` for file-overlap (future use), `mutex:bridge` for bridge-singleton (future use). At-most-one active row per resource_key enforced by partial unique index |
| `owner_agent_class` | TEXT | NOT NULL | `claude` / `codex` / `operator` / `merger` |
| `owner_instance_id` | TEXT | NOT NULL | Per-runner identity (e.g., `claude-1`, `codex-2`); honours the OP-783 invariant "1 bot account ↔ 1 instance_id" |
| `fencing_token` | TEXT | NOT NULL UNIQUE | `claim:{instance}:{epoch_us:016d}-{uuid8}` — same encoding as OP-977 labels for migration compatibility; UNIQUE means a re-issued token is a programmer error |
| `state` | TEXT | NOT NULL CHECK IN ('active', 'released') DEFAULT 'active' | Closed set; partial unique index on `(resource_key) WHERE state='active'` is the at-most-one-active-owner enforcement |
| `phase` | TEXT | NOT NULL DEFAULT 'pickup' | Free-form for forward-compat; canonical values listed in §3.3 |
| `heartbeat_at` | TIMESTAMPTZ (PG) / TEXT ISO (SQLite) | NOT NULL DEFAULT NOW() | Updated by `record_phase()`; freshness signal for `expire_stale_active_claims()` TTL sweep (default 5 min) |
| `acquired_at` | TIMESTAMPTZ / TEXT ISO | NOT NULL DEFAULT NOW() | Immutable; for audit |
| `released_at` | TIMESTAMPTZ / TEXT ISO | NULLABLE | Set on transition to `state='released'` |
| `release_reason` | TEXT | NULLABLE | Free-form (e.g., `success`, `push-fail`, `no-commits`, `ttl-expired`, `operator-override:<reason>`, `mutex-lost`, `capability-blocked`); for filter+aggregate in the validation contract |
| `external_refs` | JSONB (PG) / TEXT JSON (SQLite) | NOT NULL DEFAULT `{}` | Forward-compat bag: Gerrit Change ID, worktree path, capability-resolved set, blocker keys, file-mutex paths |

### §3.2 Indices

| Index | Definition | Purpose |
|---|---|---|
| (PK) | `lease_id` | Single-row lookup for release / heartbeat |
| `uq_runner_claims_resource_active` | `(resource_key) WHERE state='active'` partial UNIQUE | The at-most-one-active-owner enforcement; INSERT race → IntegrityError → wrapped to `ClaimBlocked` |
| `idx_runner_claims_ticket` | `(ticket_key, state)` | "what's happening on this ticket right now?" query for the rescue CLI and dashboards |
| `idx_runner_claims_heartbeat_active` | `(heartbeat_at) WHERE state='active'` partial | TTL sweep performance — bounds the scan in `expire_stale_active_claims()` |

### §3.3 Canonical `phase` values (forward-compat namespace)

| Phase | Set by | Meaning |
|---|---|---|
| `pickup` | `acquire_claim()` default | Initial acquire, no work started |
| `capability-resolved` | runner post-matrix | Capability matrix resolved; tool dispatcher armed |
| `in-progress` | runner during work | Mid-pickup; default heartbeat phase |
| `pre-push` | runner pre-finalize | Work complete; pushing to Gerrit |
| `pushed` | runner post-push | Gerrit Change-Id registered; transition-to-Under-Review pending |
| `under-review` | runner post-transition | Substrate-side mirror of the JIRA workflow state for the duration of the lease |
| `blocked-waiting` | runner pre-pickup re-check | Detected a blocker (capability / bridge / file-mutex); lease NOT released; waiting + heartbeating |
| `capability-blocked` | runner | Capability gap detected post-acquire; lease being released with reason `capability-blocked` |

New phases require an amendment to this list. The column type is `TEXT`
not an enum to allow forward-compat without migration; values are
opaque to the DB but must conform to this list on every read.

### §3.4 `release_reason` canonical values

| Reason | Set by | Counted in validation contract? |
|---|---|---|
| `success` | runner finalize | Yes (denominator) |
| `push-fail` | runner finalize (transient or unknown subtype in `external_refs`) | Yes |
| `no-commits` | runner finalize | Yes |
| `gerrit-setup-fail` | runner pre-push | Yes |
| `capability-blocked` | runner | Yes |
| `mutex-lost` | acquire race fallback | Yes |
| `ttl-expired` | `expire_stale_active_claims()` | Yes — high count means runners are dying |
| `operator-override:<reason>` | `force_release_claim()` (rescue CLI) | Yes — high count means humans are firefighting; SP-B-X-META C11 alert |
| `revert-§11` | runner self-revert path | Yes |

---

## §4. Function API contract

The 4-function public API named in the parent spec is the binding
contract. The shipped module
(`backend/agents/runner_coordination.py`, OP-1106) adds 3 additional
functions that downstream tickets consume; this section is the contract
for all 7.

### §4.1 `acquire_claim` (binding)

```python
def acquire_claim(
    *,
    ticket_key: str,
    resource_key: str,
    owner_agent_class: str,
    owner_instance_id: str,
    phase: str = "pickup",
    external_refs: Optional[dict] = None,
) -> ClaimLease:
    """Atomically claim ``resource_key`` for ``ticket_key``.

    Returns the resulting :class:`ClaimLease`. Raises
    :class:`ClaimBlocked` (with ``existing_lease`` populated when
    readable) if a different active lease holds ``resource_key``.

    Idempotency: re-acquiring an existing active claim by the same
    (ticket_key, resource_key, owner_agent_class, owner_instance_id)
    returns the existing lease unchanged.
    """
```

Contract obligations:

1. The pair `(resource_key, state='active')` is unique — partial unique
   index `uq_runner_claims_resource_active` enforces. INSERT race →
   `IntegrityError` → caught → `ClaimBlocked` raised. Caller MUST treat
   `ClaimBlocked` as "another owner holds this; back off".
2. Same-owner re-acquire is a no-op (returns the existing row). This is
   the OP-977 same-bot fast-fail behaviour preserved through migration.
3. Fencing token is server-side minted via `_make_fencing_token()`; the
   caller MUST NOT supply one (no override path).
4. `external_refs` is opaque to the DB; SHOULD carry `worktree_path`,
   `capability_resolved` set, and `blockers_inward` when known at acquire
   time.

### §4.2 `release_claim` (binding)

```python
def release_claim(
    *,
    lease_id: str,
    fencing_token: str,
    release_reason: str,
) -> None:
    """Release an active claim.

    Idempotency: releasing an already-released lease (or one that was
    never acquired) is a no-op.

    Raises :class:`FencingTokenMismatch` if the active row exists but
    its fencing_token does not match — protects against a stale lease
    holder overwriting a fresh lease.
    """
```

Contract obligations:

1. Idempotent — `finally` block release is safe even on a path that
   already released earlier.
2. Fencing-token mismatch is a hard error — a stale runner that wakes
   up post-TTL-sweep MUST NOT silently release a freshly-acquired lease.
3. `release_reason` is required (no default) — operator review depends
   on it being filled.

### §4.3 `record_phase` (binding)

```python
def record_phase(
    *,
    lease_id: str,
    fencing_token: str,
    phase: str,
) -> None:
    """Update ``phase`` and bump ``heartbeat_at`` on an active claim.

    Raises :class:`ClaimNotFound` if no active claim exists.
    Raises :class:`FencingTokenMismatch` if the fencing token doesn't
    match the active row.
    """
```

Contract obligations:

1. Heartbeat (`heartbeat_at = NOW()`) is a side-effect of EVERY call.
2. `phase` MUST be a value from §3.3's canonical list (callers that
   want a new phase amend §3.3 first).
3. Runner SHOULD call `record_phase()` at least once per minute on
   long-running pickups so TTL sweep at 5 min doesn't fire spuriously.

### §4.4 `find_active_holders` (binding)

```python
def find_active_holders(
    *,
    resource_keys: Optional[Sequence[str]] = None,
    exclude_ticket: Optional[str] = None,
) -> list[ClaimLease]:
    """Return active claim rows, optionally filtered."""
```

Contract obligations:

1. Replaces `jira_dispatch.find_mutex_holders()` (JQL → table). Returned
   shape is the same `ClaimLease` snapshot as `acquire_claim` returns —
   callers can pivot from label-parsing to row-attribute access in one
   refactor.
2. Returned list ordered by `acquired_at ASC` so the oldest active
   holder appears first (matches the lowest-token-wins ordering of the
   OP-977 label substrate — preserves "earliest wins" semantics through
   migration).

### §4.5 `expire_stale_active_claims` (extension; not in original 4-API but binding)

```python
def expire_stale_active_claims(*, max_age_seconds: int = 300) -> int
```

Contract obligations:

1. TTL = 300 s (5 min) default; matches `OMNISIGHT_RUNNER_STALE_CLAIM_MAX_AGE_S`
   conceptually but uses heartbeat freshness, NOT acquired-age.
2. Idempotent on repeat invocations within the same cadence.
3. Returns the count of expired rows so the caller (cron / systemd /
   pre-pickup helper) can emit a metric.

### §4.6 `force_release_claim` + `record_audit_event` (rescue-CLI binding)

```python
def force_release_claim(
    *,
    lease_id: str,
    release_reason: str,
    operator_fingerprint: str,
) -> Optional[ClaimLease]

def record_audit_event(
    *,
    action: str,
    operator_fingerprint: Optional[str] = None,
    target_lease_id: Optional[str] = None,
    target_ticket_key: Optional[str] = None,
    details: Optional[dict] = None,
) -> None
```

Contract obligations:

1. `force_release_claim` bypasses the fencing-token check — operator
   override per ADR-0034 separation of duties.
2. Every call to `force_release_claim` MUST be paired with a
   `record_audit_event(action='rescue.release', ...)` in the same logical
   transaction (the rescue CLI handles this; direct callers MUST follow
   the pattern).
3. `operator_fingerprint` is required for any write action; absent
   fingerprint → `ValueError` from `force_release_claim`.

### §4.7 Exception surface (binding)

| Exception | Raised by | Caller MUST |
|---|---|---|
| `CoordinationError` | base — not raised directly | catch as last-resort |
| `ClaimBlocked` | `acquire_claim` | back off, retry-after, or §11-revert; NEVER overwrite |
| `ClaimNotFound` | `record_phase`, indirectly via `force_release_claim` returning `None` | treat as "lease already released by TTL or rescue" |
| `FencingTokenMismatch` | `release_claim`, `record_phase` | log + audit-event + ABORT — never retry; this means a stale runner woke up |

---

## §5. Strangler-pattern migration plan

This section is the **binding sequence** of downstream tickets. Each row
flips one sub-contract on; the order is non-negotiable because each row
depends on the previous row's invariant.

### §5.1 Sequence

```
v2-⑩-1a   (THIS spec — OP-1156)               docs only
    ↓
v2-⑩-ADR  (ADR-0037 — substrate decoupling)  docs only; OP-1157
    ↓
v2-⑩-1bc  (substrate module + table)         ✅ ALREADY SHIPPED on develop (OP-1106) — re-pinned here for traceability
    ↓
v2-⑩-2-Shadow  (dual-write both substrates for 1 wk; daily comparison report)
    ↓
v2-⑩-2bc  (cut READ path: replace find_mutex_holders JQL → find_active_holders table)
    ↓
v2-⑩-2d   (cut WRITE path: replace claim acquisition in 3 runner entry points + finally-block release on every terminal path)
    ↓
v2-⑩-2-Cutover  (drop label writes; keep label reads for 1 sprint compat for dashboards; 40-ticket sample verification at end)
    ↓
parallel branches:
    ├──→ v2-⑩-3       (RUNNER_RUNTIME_ARTIFACTS centralisation — ✅ ALREADY SHIPPED OP-1111)
    ├──→ v2-⑩-4a / 4bc (bridge-health graded — §8)
    ├──→ v2-⑩-5a / 5bc / 5d / 5e (capability registry — §9)
    └──→ v2-⑩-RescueCLI (✅ ALREADY SHIPPED OP-1118; operator override; uses §4.6 functions)
    ↓
v2-⑩-AlertRule  (Prometheus alerts via AlertBridge — unblocks SP-B-X-META, §11)
    ↓
v2-⑩-Integration  (4-week soak; assert §6 validation contract met)
```

### §5.2 Per-ticket rollback flag

Each cutover ticket MUST ship with an env var that reverts to the
prior-substrate behaviour, so a production incident during cutover is a
one-flag rollback rather than a code revert:

| Ticket | Rollback flag | Effect when set |
|---|---|---|
| v2-⑩-2-Shadow | `OMNISIGHT_RUNNER_COORD_SHADOW_WRITE=0` | Disable substrate writes; pure label substrate |
| v2-⑩-2bc | `OMNISIGHT_RUNNER_COORD_TABLE_READ=0` | Read mutex from JQL (pre-cutover behaviour) |
| v2-⑩-2d | `OMNISIGHT_RUNNER_COORD_TABLE_ACQUIRE=0` | Acquire mutex via OP-977 label path (pre-cutover behaviour) |
| v2-⑩-2-Cutover | `OMNISIGHT_RUNNER_COORD_LABEL_WRITE=1` | Re-enable label writes (compat-only) |
| v2-⑩-4bc | `OMNISIGHT_RUNNER_BRIDGE_GATE_GLOBAL=1` | Re-instate the today's global hard bridge gate |
| v2-⑩-5bc | `OMNISIGHT_CAPABILITY_REGISTRY_LABELS_ONLY=1` | Read capability whitelist from labels only (pre-typed-registry) |

The pattern echoes `OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY=1` (OP-977's
rollback for the AUDIT-24 fix). All cutover rollback flags MUST default
to ON-the-new-behaviour; the env var being set is the off-switch.

### §5.3 Per-step verification gate

Each ticket completes only when:

1. Its smoke / unit tests pass.
2. The corresponding line of the §6 validation contract is met (or, for
   pre-Cutover steps, is moving in the right direction over a 1-week
   window).
3. The rollback flag has been exercised at least once in a CI run
   (regression test that flipping the flag restores the prior behaviour).

### §5.4 Cutover gating per JIRA write

A label write at any point during cutover MUST be guarded by `if not
COORD_TABLE_WRITE_ENABLED:` so that the strangler can be inspected in
production. Every guard site MUST include a `# OP-XXXX cutover gate`
comment so a grep finds them at the v2-⑩-2-Cutover ticket.

---

## §6. Validation contract

This section is the **binding measurement target**. The v2-⑩-Integration
ticket ships only when all targets are met on a continuous 1-week window
over the LAST 40 runner-handled tickets after enforce-mode kicks in.

### §6.1 Target metrics

| # | Metric | Today | Target | How to measure |
|---|---|---:|---:|---|
| 1 | Fraction of terminal tickets carrying any `claim:*` label | ~40% | 0% | JQL `status in (Done, "Won't Do", Closed) AND labels in ("claim:*")` over last-40 sample → expect 0 rows |
| 2 | Fraction of pickups ending in any `runner-blocked:*` label | ~45% | <10% | Combine the 3 distinct `phase` values (`blocked-waiting`, `capability-blocked`, `mutex-lost` reasons) in `release_reason` aggregation; divide by total terminal rows |
| 3 | Tickets with >1 active row per `resource_key` simultaneously | unknown (label substrate makes this hard to measure) | 0 | `SELECT resource_key, COUNT(*) FROM runner_claims WHERE state='active' GROUP BY resource_key HAVING COUNT(*)>1` → expect empty |
| 4 | Duplicate pickup attempts on a single ticket | unknown | 0 | `SELECT ticket_key, COUNT(*) FROM runner_claims WHERE state='active' GROUP BY ticket_key HAVING COUNT(*)>1` over a 1-hour rolling window → expect 0 |
| 5 | OP-1070-style runtime-artefact reverts | (recurring) | 0 | Regression test fixture replays OP-1070: pre-create `.runner-cwd-sentinel` + `progress.txt` in worktree; assert runner finalize does NOT §11-revert |
| 6 | TTL sweep collection rate | n/a | < 5% of leases ever expire via TTL | Higher rate means runners are dying without releasing → investigate |
| 7 | Operator-override rate | (no audit today) | < 1% of leases require `force_release_claim` | Higher rate means runtime gates are too tight → tune |

### §6.2 The 40-ticket sample protocol

1. After v2-⑩-2-Cutover ships AND a 1-week observation window has
   elapsed, query the last 40 runner-handled tickets (any status,
   ordered by `acquired_at DESC` from `runner_claims`).
2. For each, compute the 7 metrics above.
3. Pass criteria are the §6.1 targets met on the **whole sample** (not
   averages — any single ticket failing #3 or #4 is a hard fail).
4. If pass: open the v2-⑩-Integration ticket's "Exercised AC" + close
   SP-B-X-META per §11.
5. If fail: write a structured incident under
   `docs/audit/AUDIT-G-A-v2-Family-10/<incident>.md` documenting which
   metrics missed by how much; v2-⑩-Integration stays open until a
   re-run hits all targets.

### §6.3 The OP-1070 regression replay (binding fixture)

A first-class integration test fixture (lives at
`backend/tests/test_runner_op1070_regression.py` when v2-⑩-2d ships)
MUST encode:

1. Pre-create `.runner-cwd-sentinel` and `progress.txt` in the runner
   worktree.
2. Invoke the full pickup pipeline against a synthetic ticket.
3. Assert that runner finalize completes WITHOUT §11-revert — i.e., the
   runtime artefacts are detected, classified as runner-owned, and
   excluded from dirty-check via the centralised
   `RUNNER_RUNTIME_ARTIFACTS` set.
4. Assert that the resulting `runner_claims` row has
   `release_reason='success'`, NOT `release_reason='revert-§11'`.

If this test fails, v2-⑩-Integration is NOT shippable. The test is the
gate.

---

## §7. The 5 lived failure modes (from codex Package 2 self-audit)

These five modes are the lived-experience evidence behind this family.
Each mode gets a sub-section here describing (a) the symptom, (b) the
root cause, (c) which part of THIS contract addresses it, and (d) the
verification line in §6.

### §7.1 Mode 1 — "Runner-owned artefacts make my successful work look unsafe" (OP-1070)

**Symptom**: codex completed AC at 02:07:39, was §11-reverted at 02:07:51
because `.runner-cwd-sentinel` had disappeared; later completed at
02:46:29, §11-reverted at 02:46:42 because `progress.txt` was the first
uncommitted path detected by the dirty-check.

**Root cause**: dirty-check / stash / Change-Id call sites each had their
own private notion of "what's a runner-owned file"; the sentinel and
progress.txt were owned by different sites and treated as "user dirty"
by the others.

**Addressed by**: centralised `RUNNER_RUNTIME_ARTIFACTS` constant in
`backend/agents/runner_artifacts.py` (v2-⑩-3, ✅ shipped OP-1111). All
dirty-check / stash / Change-Id sites now consult the one constant.
Additionally, the substrate moves the sentinel's *meaning* into the
`runner_claims.external_refs.worktree_path` column (the row itself
attests workspace ownership) so even if the file is missing the lease
is the source of truth.

**Verification**: §6 metric #5 (OP-1070 regression replay).

### §7.2 Mode 2 — "Claim and status state are split across labels, assignee, comments, and worktree side effects"

**Symptom**: OP-1070 — assignee diverged from pickup; multiple codex
worktrees attempted the same ticket; reconciliation impossible because
"who owns this?" had 4 partial answers in 4 substrates.

**Root cause**: There was no single answer to "who currently owns this
ticket's runtime state?". JIRA assignee said one bot; the `claim:*` label
said another; the open worktree on disk said a third; the under-review
comment was from a fourth pickup.

**Addressed by**: §1 state-separation contract. `runner_claims` is THE
answer to "who owns this lease right now?" — the partial unique index
on `(resource_key) WHERE state='active'` makes it impossible for two
rows to coexist for the same resource. JIRA assignee / labels / comments
are downgraded to mirrors and audit trail.

**Verification**: §6 metric #3 and #4 (zero double-owner rows; zero
duplicate pickups).

### §7.3 Mode 3 — "Health gates meant to protect me block every pickup because they depend on the thing they are checking" (OP-1067/1077)

**Symptom**: bridge heartbeat coupled to event-loop progress; quiet
Gerrit periods staled the heartbeat; the runner's pre-pickup hard global
bridge gate refused EVERY pickup → fleet-wide pickup DoS.

**Root cause**: the bridge gate is GLOBAL (any stale bridge blocks any
pickup) but the runner needs the bridge only for capability-scoped work
— a code-only docs ticket has no Gerrit dependency, yet it was blocked.

**Addressed by**: §8 bridge-health graded contract (v2-⑩-4a + v2-⑩-4bc).
Bridge gate becomes per-capability: stale bridge blocks ONLY tickets
whose capability set includes `gerrit_push`, allowing docs / code-only
pickups to proceed with a "review pending bridge" lease annotation.

**Verification**: §6 metric #2 (`runner-blocked:*` rate drops from 45%
to <10% — the bridge gate stops over-firing).

### §7.4 Mode 4 — "Capability decisions are flat labels in a 9-area whitelist; new dimensions require new label families" (codex Package 2 §4 second half + OP-1124 / OP-1126 / OP-1148 lived experience)

**Symptom**: `capability:enable=*` labels are a flat opaque list; adding
a new dimension (cost-mode, known-failure-classes, max-tier) requires a
new label family AND a new safe-default fallback path in
`capability_matrix.py`. The safe-default leak (OP-1124) demonstrated
that even a fully-populated matrix can collapse to `[mcp_search,
memory_recall]` because the matrix lookup runs in a place where the
label set is partial — flat labels make it impossible to validate at
filing time.

**Root cause**: capability is multi-dimensional but the label namespace
is mono-dimensional. Every new dimension is a new label family
(memory-style: `capability:enable=X`, `capability:disable=Y`,
`capability:cost-mode=Z`...) and the matrix has to merge them at runtime.

**Addressed by**: §9 typed capability registry (v2-⑩-5a + v2-⑩-5bc +
v2-⑩-5d + v2-⑩-5e). Capability becomes a typed row in
`capability_profile` with provider × model × tools × max-tier ×
cost-mode × health × known-failure-classes columns. Validators run at
filing time AND at runtime against the same typed shape.

**Verification**: §6 metric #2 contribution from `capability-blocked`
(safe-default leak fixed when the matrix has a typed source of truth).

### §7.5 Mode 5 — "Operator-side rescue is a manual scavenger hunt — strip labels, run psql, restart worktree" (lived experience pre-OP-1118)

**Symptom**: stuck claim labels (`claim:codex-1:001722..-ab12`) on a
ticket whose codex worktree had died; the only operator recourse was a
manual `git stash` + label strip + worktree restart, with no audit.

**Root cause**: no first-class rescue surface. The label substrate
*itself* had no concept of "operator override"; operators wrote raw JQL
to find stuck claims and `update.labels.remove` to clear them, with no
audit trail.

**Addressed by**: `force_release_claim` + `record_audit_event` (v2-⑩-RescueCLI,
✅ shipped OP-1118) per §4.6 + the `runner_audit_events` table per
`backend/alembic/versions/0237_runner_audit_events.py`. Every rescue
release writes one audit row with the operator fingerprint and the
pre-release lease snapshot — incident review can answer "who released
this lease and why" in one SQL query.

**Verification**: §6 metric #7 (operator-override rate stays <1%; if it
spikes, root-cause the underlying gate that is causing rescues).

---

## §8. Bridge-health graded contract (v2-⑩-4a stake)

The Codex Package 2 self-audit identified the bridge gate (`pre_pickup_ok`'s
hard-global bridge-staleness refusal) as the root cause of OP-1067/1077
(quiet-Gerrit-period pickup DoS). v2-⑩-4a re-grades the gate; v2-⑩-4bc
implements; this section is the contract.

### §8.1 The graded gate

Bridge staleness blocks pickup ONLY for tickets whose resolved capability
set requires the bridge. Specifically:

| Capability resolved at pickup time | Bridge-stale gate verdict | Lease annotation if proceeding |
|---|---|---|
| `gerrit_push` enabled | **HARD BLOCK** — refuse pickup until bridge is fresh; emit `runner-blocked:waiting-bridge-stale` (during transition) → `runner_claims.phase='blocked-waiting'` + `external_refs.blocker='bridge-stale'` after substrate cutover | n/a (no lease taken) |
| `gerrit_push` NOT in resolved set (e.g., docs-only ticket, lint-only ticket) | **PROCEED** — bridge irrelevant to this pickup | `runner_claims.external_refs.bridge_state='stale-at-acquire'` (annotation for operator review; NOT a block) |
| `gerrit_push` enabled BUT capability is `closes-as:duplicate` or similar non-push terminal | **PROCEED** with `external_refs.bridge_state='stale-but-no-push-attempted'` | n/a |

### §8.2 Definition of "stale"

| Threshold | Effect |
|---|---|
| heartbeat ≤ 60 s old | Fresh; no annotation |
| heartbeat 60-300 s old | Warning; emit `omnisight_bridge_staleness_seconds` gauge; alert at warn-level after 5 min |
| heartbeat > 300 s old | Stale; per §8.1 verdict |

The bridge-heartbeat mechanism itself is owned by Sprint H — this
contract only describes how the runner CONSUMES the freshness signal.
The current `OMNISIGHT_BRIDGE_HEARTBEAT_AT` reading mechanism continues;
v2-⑩-4bc only refactors `pre_pickup_ok` consumption.

### §8.3 Lease state for "review pending bridge"

When a pickup proceeds under §8.1 case 2 (gerrit_push NOT required) but
the bridge is stale, the lease carries
`external_refs.bridge_state='stale-at-acquire'`. The rescue CLI's `dump`
subcommand surfaces this; AlertBridge §11 ships a `runner_pickup_under_stale_bridge`
counter so operators can see the rate.

### §8.4 OP-1067 / OP-1077 regression test

A first-class regression test (lives at
`backend/tests/test_pre_pickup_bridge_gate.py` when v2-⑩-4bc ships)
MUST exercise:

1. Stub bridge as stale (heartbeat 600 s old).
2. Stub ticket capability resolved to `[code_edit, run_tests]` (no
   `gerrit_push`).
3. Assert `pre_pickup_ok()` returns `(True, "bridge-stale-but-no-push")`
   — pickup proceeds.
4. Assert `runner_claims.external_refs.bridge_state == 'stale-at-acquire'`.

If this test fails, v2-⑩-4bc is NOT shippable.

---

## §9. Capability registry vs flat labels (v2-⑩-5a stake)

The OP-1124 / OP-1126 / OP-1148 incident series (memory entries
[[feedback_capability_safe_default_leak]],
[[feedback_runner_recognized_areas]],
[[project_codex_capability_envelope]]) established that the flat
`capability:enable=*` label namespace is the wrong substrate. v2-⑩-5a
specifies the replacement; v2-⑩-5bc / 5d / 5e implement.

### §9.1 The typed shape

`capability_profile` is a typed row with the following dimensions:

| Dimension | Type | Example | Source of authority |
|---|---|---|---|
| `provider` | enum | `anthropic`, `openai`, `google`, `groq`, `deepseek`, `openrouter`, `ollama` | env (provider keys) |
| `model` | text | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`, `gpt-5-codex`, ... | per-provider model registry |
| `tools` | text[] (set) | `{code_edit, run_tests, gerrit_push, jira_update, mcp_search, memory_recall, run_lint}` | runtime capability gate |
| `max_tier` | enum | `S`, `M`, `L`, `X` | per-class policy |
| `cost_mode` | enum | `subscription`, `api-anthropic`, `api-openai`, `operator-window` | per-class policy |
| `health` | enum | `ok`, `degraded`, `down` | live probe per provider |
| `known_failure_classes` | text[] (set) | `{rate-limit, context-overflow, capability-mismatch, mcp-timeout}` | per-model deconfliction matrix |

### §9.2 Pickup eligibility under the typed registry

Eligibility becomes a typed query against `capability_profile`:

```sql
SELECT * FROM capability_profile
WHERE provider IN (current_class.providers)
  AND model IN (current_class.models)
  AND tools @> required_tools_for_ticket(ticket_key)
  AND max_tier >= ticket.tier
  AND cost_mode = current_class.cost_mode
  AND health = 'ok'
  AND NOT (known_failure_classes && failed_classes_for_ticket(ticket_key));
```

The pickup grabs the FIRST row (ORDER BY `prefer` then `acquired_at`)
that satisfies the predicate. Today's flat-label matrix code becomes a
backward-compat adapter that reads the typed profile and synthesises the
old shape for callers that haven't migrated yet.

### §9.3 Filing-time validator

A filing-time validator (v2-⑩-5a Code AC) MUST reject tickets whose
required-tools-for-ticket set has no matching `capability_profile` row.
This closes the OP-1124 safe-default-leak class at filing time — the
matrix cannot silently collapse to `[mcp_search, memory_recall]` because
the filing tool fails first.

### §9.4 Pre-pickup quota / circuit-breaker enforcement (v2-⑩-5d)

Today's quota check runs AFTER partial execution (post-tool-dispatch).
v2-⑩-5d moves it PRE pickup so a quota-exhausted provider does not
acquire a lease and then fail mid-flight with a cost-bearing call. The
mechanism: extend `capability_profile.health` to `quota-exhausted` when
`provider_quota_tracker.py` reports remaining budget below threshold;
the §9.2 query then naturally excludes the row.

### §9.5 Model deconfliction (v2-⑩-5e)

The decision matrix (failure class × fallback model): if a model failed
with `context-overflow`, fallback to a larger-context model; if with
`rate-limit`, fallback to a different provider; if with `capability-mismatch`,
do NOT fallback (escalate to operator). The matrix is data, not code,
stored as `capability_failure_classes` JSON config the deconfliction
module reads.

---

## §10. Existing partial coverage (what's already on `develop`)

This section is the **inverse** of the pre-flight survey in §0 — for each
piece of contract surface above, where it stands today on `develop`. Read
left-to-right to see "what does the downstream impl ticket actually
need to write?".

| Contract surface (anchor) | On-develop state | What downstream ticket adds |
|---|---|---|
| §1 state-separation principle | partly true in code; the table exists, but JIRA labels are still authoritative | v2-⑩-2-Shadow + 2bc + 2d + 2-Cutover flip authority |
| §2 A-class / B-class taxonomy | not enforced; no validator | v2-⑩-2-Cutover adds a lint that fails CI if a new `claim:*` label literal appears in `backend/**/*.py` outside the migration shim |
| §3 schema (`runner_claims`) | ✅ FULLY shipped (alembic 0236 / OP-1106) | nothing — references the table |
| §3 schema (`runner_audit_events`) | ✅ FULLY shipped (alembic 0237 / OP-1118) | nothing |
| §4.1 `acquire_claim` | ✅ FULLY shipped | nothing |
| §4.2 `release_claim` | ✅ FULLY shipped | nothing |
| §4.3 `record_phase` | ✅ FULLY shipped | v2-⑩-2d adds caller invocations at phase boundaries in the 3 runner entry points |
| §4.4 `find_active_holders` | ✅ FULLY shipped | v2-⑩-2bc adds the caller in `jira_dispatch.pre_pickup_ok` replacing `find_mutex_holders` |
| §4.5 `expire_stale_active_claims` | ✅ FULLY shipped | v2-⑩-2d wires the cron/systemd timer caller |
| §4.6 `force_release_claim` / `record_audit_event` | ✅ FULLY shipped | nothing on the lib side; CLI `omnisight-runner-rescue` itself shipped OP-1118 |
| §4.7 exception surface | ✅ FULLY shipped | nothing |
| §5 strangler-pattern sequence | substrate exists; cutover not started | EVERY downstream ticket in §5.1 |
| §5.2 rollback flags | not yet defined | each cutover ticket adds its flag |
| §6 validation contract metrics | no measurement today | v2-⑩-Integration runs the 40-ticket sample protocol |
| §6.3 OP-1070 regression replay | not yet written | v2-⑩-2d ships the test |
| §7 5 failure modes | Mode 1 partially closed (OP-1111); Mode 5 fully closed (OP-1118); Modes 2/3/4 open | per row above |
| §8 bridge-health graded | NOT yet — gate is global today | v2-⑩-4a (spec) + v2-⑩-4bc (impl) |
| §9 capability registry | NOT yet — flat labels today | v2-⑩-5a (spec) + v2-⑩-5bc (table + adapter) + v2-⑩-5d (quota pre-pickup) + v2-⑩-5e (deconfliction) |
| §11 SP-B-X-META unblocker | alert rules not yet defined | v2-⑩-AlertRule ships the 4 rules; one firing closes SP-B-X-META |
| §12 ADR-0037 | not yet written | v2-⑩-ADR (OP-1157 — next ticket) |

**Bottom line — what THIS spec (OP-1156) ships**:

- 0 backend code changes.
- 0 db changes.
- 0 devops / embedded / frontend / security / tests / tooling changes.
- 1 new spec doc (this file).
- 1 minor x-ref edit in the parent spec.
- The rest is the binding contract that 16 downstream tickets implement
  in sequence per §5.1.

---

## §11. SP-B-X-META unblocking criterion (binding)

SP-B-X-META (the long-running meta-ticket for Sprint B "runner reliability")
has been open for ~30 days awaiting a "30-day stability" criterion. The
30-day criterion has historically been interpreted as "no operator
intervention required for 30 days", but per Sprint B retrospective the
correct criterion is **"at least 1 alert from {C9 bridge-health, C10
instance-drift, C11 human-authority-yield, C12 state-authority-precedence}
fires in production"** — i.e., observability has moved from absent to
actionable.

### §11.1 The 4 alert rules (binding for v2-⑩-AlertRule)

v2-⑩-AlertRule ships exactly these 4 Prometheus rules via the AlertBridge
framework (`docs/sprint-s12/2026-05-16-v2-alertbridge-framework-contract.md`):

| Alert ID | Name | Expression (sketch) | Severity | Fires when |
|---|---|---|---|---|
| **C9** | `runner_bridge_stale` | `omnisight_bridge_staleness_seconds > 300` for 5 min | warn | Bridge heartbeat older than 5 min — the OP-1067/1077 signal |
| **C10** | `runner_instance_drift` | `count by (instance_id) (runner_claims_active{state="active"}) > 1` for 1 min | crit | The OP-783 invariant violated — two leases share an instance_id |
| **C11** | `runner_human_authority_yield` | `rate(runner_audit_events_total{action="rescue.release"}[24h]) > 0` | warn | Any operator-override happened in 24h — humans firefighting |
| **C12** | `runner_state_authority_precedence` | `rate(runner_state_authority_precedence_total[1h]) > 0` | warn | Bot vs human concurrent edit — the JIRA "labels concurrently changed" signal |

### §11.2 The closure criterion

SP-B-X-META closes when:

1. AlertBridge has shipped (`v2-AlertBridge-1bc`, ✅ shipped OP-1103).
2. v2-⑩-AlertRule has shipped (the 4 rules above are loaded into
   Prometheus).
3. At LEAST 1 of the 4 rules has fired in production at least once
   (operator can verify in Grafana / alert log).
4. The fired alert was acted upon (operator response within SLO; closed
   feedback loop).

Condition 3 is the unblocker. Condition 4 is required for full closure
but not for unblocking — unblocking means the META can move forward to
"closing review" status; the closing review needs all 4 conditions.

### §11.3 What happens if no alert ever fires in 30 days

If the system runs 30 days post-deploy with NONE of the 4 alerts firing,
that is itself a signal — possibly the thresholds are too loose, possibly
the runner is genuinely steady, possibly the substrate has eliminated
the failure modes. In that case the META is closed with a
`closed-as:no-alerts-needed` resolution and the alert rules remain armed
as a regression watch.

---

## §12. ADR-0037 cross-link

The architectural decision behind this contract is recorded in
**ADR-0037 — Runner state substrate decoupling** (filed under v2-⑩-ADR,
the sibling spec ticket OP-1157 that THIS ticket OP-1156 blocks).
ADR-0037 is the **formalised** version of codex Package 2 §0 + §4 (the
self-audit's architectural recommendation) and the LOCKED decision per
parent spec v1.2 Family ⑩.

### §12.1 What ADR-0037 says (one paragraph summary)

> *"JIRA labels and Postgres `runner_claims` are not redundant stores of
> the same data; they are stores of **different kinds of data**. JIRA
> labels are durable, human-authored, audit-friendly ledger entries.
> `runner_claims` is a volatile, machine-only, mutex-and-heartbeat
> runtime state machine. The strangler-pattern migration is the only
> safe path: shadow-write 1 week, enforce-from-table, drop label writes
> (keep read-only label compat 1 sprint for dashboard consumers). The
> two stores are never made consistent with each other; they are made
> consistent with their DIFFERENT roles."*

### §12.2 What this spec (OP-1156) contributes to ADR-0037

This spec is the **section-level material** ADR-0037 references:

- ADR-0037 §"State separation" → cites §1 here.
- ADR-0037 §"Label taxonomy" → cites §2 here.
- ADR-0037 §"Schema rationale" → cites §3 here.
- ADR-0037 §"API surface" → cites §4 here.
- ADR-0037 §"Migration" → cites §5 here.
- ADR-0037 §"Validation" → cites §6 here.
- ADR-0037 §"Failure modes" → cites §7 here (5 modes named in codex
  Package 2 are now formalised here).
- ADR-0037 §"Sub-contracts" → cites §8 (bridge graded) and §9
  (capability registry) here.
- ADR-0037 §"Already-shipped state" → cites §10 here.
- ADR-0037 §"Alerting" → cites §11 here.

The two documents are **co-authored**: ADR-0037 is the high-level
decision record (operator-facing, reviewer-facing, accepts / rejects /
deprecates), and this spec is the **engineering contract** (implementer-facing,
test-fixture-facing, validation-target-facing). Neither stands alone.

### §12.3 Why ADR-0037 is filed SEPARATELY from this spec

Separation-of-concerns: ADRs are short, accept/reject decisions with
context; specs are long, exhaustive engineering surfaces. Conflating the
two (one giant doc) makes both harder to consume — the operator who
needs to review the LOCK never wants the 700-line spec; the engineer who
needs to implement v2-⑩-2bc never wants to re-read the ADR
justification. The two-doc shape (this spec + the ADR) is the same shape
used for the Family ⑥ ADR-0036 + Family ⑥ spec pair.

---

## §13. Open questions, changelog, hand-off block

### §13.1 Open questions (for v1.1 amendment cycle, if needed)

1. **Q10.1.1 — Should the per-file mutex (`mutex:<path>` labels for
   parallel-runner file-overlap, OP-731/800/687) also migrate to the
   substrate?** *Recommended deferral:* yes, but in a separate Family ⑩
   amendment (v2-⑩-2-Cutover ships first; file-mutex cutover follows
   independently). The current label-based file-mutex works; this
   contract focuses on the claim-mutex which is the bigger problem.
2. **Q10.1.2 — Should the rescue CLI also be able to operate on the
   bridge-staleness signal (force-fresh)?** *Recommended:* no. The
   bridge has its own observability + heal path (Sprint H); the rescue
   CLI is for `runner_claims` rescue only. Conflating the two surfaces
   makes the rescue CLI's blast radius too broad.
3. **Q10.1.3 — Does the typed `capability_profile` table (§9) replace
   the existing `routing_policy.py` matrix, or live alongside?**
   *Recommended deferral to v2-⑩-5a:* live alongside for 1 sprint then
   replace. The routing_policy.py implements complex tiebreak logic
   (`prefer:*` ordering, `class:*` filtering) that doesn't immediately
   port to a typed table; the migration is data-shape first, logic
   later.
4. **Q10.1.4 — Does the substrate need a multi-region story (Postgres
   replication / geo-distributed runners)?** *Recommended:* no. The
   "1 bot account ↔ 1 instance_id" invariant (OP-783) implies single-region
   per bot account anyway; multi-region runners would be a different
   project tier (out of S12.G scope).
5. **Q10.1.5 — Should `release_reason` be a closed enum (typed) instead
   of free-form TEXT?** *Recommended deferral:* yes, in v1.1 once we
   have ~3 months of real values to seed the enum. Today's free-form
   TEXT is a forward-compat hedge while the canonical-values list in
   §3.4 settles.

### §13.2 Changelog

| Version | Date | Author | Change |
|---|---|---|---|
| v1 | 2026-05-16 | claude-bot (OP-1156) | Initial spec. Architectural decision LOCKED per parent spec v1.2 Family ⑩ (2026-05-14, codex Package 2 self-audit §0+§4). Pre-flight survey quoted: substrate + table + rescue CLI + RUNNER_RUNTIME_ARTIFACTS already shipped on develop (OP-1106 / 0236 / 0237 / OP-1118 / OP-1111 / OP-1140). All 5 codex-named failure modes named and addressed. 23-label-family A-class/B-class taxonomy table. SP-B-X-META 1-of-4-alert unblocker binding. ADR-0037 cross-link defined. |

### §13.3 Hand-off block (for the next ticket — v2-⑩-ADR / OP-1157)

The next ticket to pick up Family ⑩ work is **v2-⑩-ADR (ADR-0037)** per
parent spec §3 Family ⑩ row 2. That ticket's AC SHOULD reference:

- §1 of this doc (state-separation contract) → ADR §"State separation"
- §2 of this doc (A-class / B-class taxonomy) → ADR §"Label taxonomy"
- §3 of this doc (`runner_claims` schema) → ADR §"Schema rationale"
- §4 of this doc (function API) → ADR §"API surface"
- §5 of this doc (strangler sequence + rollback flags) → ADR §"Migration"
- §6 of this doc (validation contract) → ADR §"Validation"
- §7 of this doc (5 failure modes) → ADR §"Failure modes"
- §10 of this doc (partial coverage) → ADR §"Already-shipped state"
- §12 of this doc (this section) → ADR §"This spec / ADR pair"

After v2-⑩-ADR ships:

- **v2-⑩-1bc** is already ✅ shipped (OP-1106); the ticket is closed as
  `closes-as:already-shipped` referencing OP-1106's Change-Id; its
  AC-verification comment cites §3 + §4 of this doc as the spec the
  shipped code already satisfies.
- **v2-⑩-2-Shadow** is the next implementation ticket; consumes §5.1
  row 4 + §5.2 `OMNISIGHT_RUNNER_COORD_SHADOW_WRITE` rollback flag.
- **v2-⑩-2bc** then **v2-⑩-2d** then **v2-⑩-2-Cutover** flip the
  three sub-contracts on per §5.1.
- **v2-⑩-3** is ✅ shipped (OP-1111); closed as `closes-as:already-shipped`.
- **v2-⑩-4a** spec follows; consumes §8 of this doc.
- **v2-⑩-5a** spec follows; consumes §9 of this doc.
- **v2-⑩-RescueCLI** is ✅ shipped (OP-1118); closed as
  `closes-as:already-shipped`.
- **v2-⑩-AlertRule** ships the 4 rules per §11.1; one firing closes
  SP-B-X-META per §11.2.
- **v2-⑩-Integration** runs the 40-ticket sample protocol per §6.2.

### §13.4 Per-anchor downstream-ticket consumer map

Reviewers and downstream implementers should use this map to answer
"which §-anchors does my ticket cite?". Every Family ⑩ downstream MUST
appear in this table; the v2-⑩-ADR ticket's first commit amends this
table when new tickets are filed.

| Downstream ticket | Anchors consumed |
|---|---|
| v2-⑩-ADR | §1, §2, §3, §4, §5, §6, §7, §10, §12 |
| v2-⑩-1bc (already shipped OP-1106) | §3, §4 |
| v2-⑩-2-Shadow | §5.1 row 4, §5.2 row 1 |
| v2-⑩-2bc | §4.4, §5.1 row 5, §5.2 row 2, §6 |
| v2-⑩-2d | §4.1, §4.2, §4.3, §5.1 row 6, §5.2 row 3, §6.3 |
| v2-⑩-2-Cutover | §2 lint, §5.1 row 7, §5.2 row 4, §6.1 metric #1 |
| v2-⑩-3 (already shipped OP-1111) | §7.1, §10 |
| v2-⑩-4a | §8 |
| v2-⑩-4bc | §8.1, §8.2, §8.3, §8.4 |
| v2-⑩-5a | §9 |
| v2-⑩-5bc | §9.1, §9.2 |
| v2-⑩-5d | §9.4 |
| v2-⑩-5e | §9.5 |
| v2-⑩-RescueCLI (already shipped OP-1118) | §4.6, §7.5 |
| v2-⑩-AlertRule | §11.1, §11.2 |
| v2-⑩-Integration | §6.1, §6.2, §6.3 |
| (cross-link) SP-B-X-META | §11 |

---

## §14. Boundary justification (spec ticket boundary block)

This ticket (OP-1156 / v2-⑩-1a) declares the following boundaries per
`docs/sop/jira-ticket-conventions.md` §11:

```yaml
scope_components: [docs]
destructive_op_classes: []
external_side_effect: none
filesystem_writes:
  - docs/sprint-s12/2026-05-16-v2-family10-runner-defense-contract.md  (NEW)
  - docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md  (MINOR: x-ref)
runtime_paths_changed: 0
db_migrations: 0
ci_yaml_changed: 0
prod_compose_changed: 0
secrets_touched: []
```

**Exemption from the area-block list** (out-of-area: backend / db /
devops / embedded / frontend / security / tests / tooling): this is a
docs-only ticket. The work is `area:docs` only; the spec describes
future backend / db contract surfaces but does NOT modify any backend /
db / devops / embedded / frontend / security / tests / tooling file —
all such modifications are deferred to the named downstream tickets in
§5.1 and §13.4, each of which will be re-evaluated under the full
area-block set at filing time (backend + db + tests at minimum for
v2-⑩-2bc / 2d / 2-Cutover / 5bc; deploy for v2-⑩-AlertRule's Prometheus
rules; tooling for v2-⑩-RescueCLI extensions).

Per [[feedback_filing_existing_impl_check]]: §0 of this spec performs
the mandatory pre-flight survey and explicitly notes which downstream
tickets are `closes-as:already-shipped` so future filing-time validators
do not re-derive the substrate. The `runner_coordination.py` module +
`runner_claims` table + rescue CLI + `RUNNER_RUNTIME_ARTIFACTS`
centralisation are all on `develop` as of 2026-05-16; this spec
formalises the **contract** that the shipped code already satisfies and
that the remaining downstream tickets MUST satisfy.
