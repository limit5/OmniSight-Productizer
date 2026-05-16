# Runner coordination substrate — state-separation contract

**Status**: Spec draft 2026-05-14 (Sprint Atlas, OP-1104 / `v2-Ⅹ-1a`)
**Sprint**: Atlas (S12.G G.A-v2 Family ⑩ — Runner Defense Contract)
**Spec parent**: `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑩
**Empirical source**: `docs/audit/codex-reviews/runner-self-audit-and-redesign-2026-05-14.txt` (codex Package 2, in-runner self-audit)
**Codifies**: separation of A-class durable ticket metadata (JIRA) from B-class volatile runtime state (Postgres), `runner_claims` table shape, and the 4-function coordination API surface
**Consumed by**: `v2-Ⅹ-ADR` (ADR-0037, JIRA-as-ledger / Postgres-as-lock), `v2-Ⅹ-1bc` (`backend/agents/runner_coordination.py` + alembic), `v2-Ⅹ-2-Shadow` (dual-write observation), `v2-Ⅹ-2bc` (read replacement), `v2-Ⅹ-2d` (write replacement + finally-block release in 3 runner scripts), `v2-Ⅹ-RescueCLI` (`omnisight runner-rescue`), and the rest of Family ⑩
**Supersedes (read-only compat 1 sprint)**: bare/fenced `claim:*` mutex from OP-838 + OP-977 (`docs/sop/runner-pickup-mutex.md`)

## 1. Why this exists

Codex Package 2 self-audit (above) confirmed from **inside** the runner
that ~50% of recent pickup failures are coordination-state failures
masquerading as task failures. Reproducing the headline evidence
(§0.1 of Package 2; §1 lived-experience modes 1–3):

| Failure mode | Codex evidence | Today's anti-pattern |
|---|---|---|
| Runner-owned artifacts make successful work look unsafe | OP-1070: completed AC at 02:07:39, reverted at 02:07:51 because `.runner-cwd-sentinel` had disappeared; later completed at 02:46:29, reverted at 02:46:42 because `progress.txt` was first uncommitted path | Runtime state lives inside the worktree the runner is auditing for cleanliness |
| Claim + status state split across labels / assignee / comments / worktree side effects | OP-1070: assignee diverged from pickup mid-flight, multiple codex worktrees attempted the same ticket | JIRA labels used simultaneously as distributed lock, progress record, and audit surface |
| Health gates depend on the thing they're checking | OP-1067 → OP-1077: bridge heartbeat coupled to event-loop progress; quiet Gerrit → stale heartbeat → fleet-wide pickup DoS | Protection mechanism becomes a single-point denial of service |

Operator's earlier empirical numbers on the same 40-ticket sample
(22.4 labels/ticket; 103 unique labels over 40 tickets; **45%**
carrying `runner-blocked`; **40%** carrying residual `claim:*`; **12%**
`runner-loop-paused-pending-review`) match the codex narrative one-to-one.

> **Single first move (Package 2 §0 + §4, operator-locked v1.2 review,
> 2026-05-14)**: move claim ownership, runner-owned artifacts, pickup
> progress, and terminal cleanup out of JIRA labels/comments into a small
> Postgres-backed coordination record. Keep JIRA as the human-facing
> ticket ledger.

This spec is the contract for that move. Implementation lands under
`v2-Ⅹ-1bc`; this doc defines the schema and surface those tickets
implement against.

## 2. The state-separation contract

A label is one of two classes. The class determines **where it lives**,
**who writes it**, and **whether the runner reads it on the hot path**.

| | A-class — durable ticket metadata | B-class — volatile runtime state |
|---|---|---|
| **Stays in** | JIRA (labels + fields + issuelinks) | Postgres (`runner_claims` table) |
| **Written by** | Filing tools (`scripts/file_jira_ticket.py`), operators, agents proposing taxonomy | Runner code paths (`acquire_claim`, `release_claim`, `record_phase`); never hand-authored |
| **Lifetime** | The ticket's entire lifetime (or until intentionally retired) | A single pickup attempt; releases on terminal exit |
| **Concurrency semantics** | Eventually-consistent set-union (Atlassian `labels.add`) — fine for taxonomy | Linearizable read-modify-write with a fencing token — required for a mutex |
| **Hot-path read** | Pickup JQL filters + prompt boundary calculation | Mutex acquisition / release / staleness sweep |
| **Audit footprint** | Visible in JIRA history forever; survives runner restarts | Visible in `runner_claims` + the JIRA comment summary written at lease-terminal time |
| **Survives JIRA outage** | No (but pickup JQL already requires JIRA) | Yes — Postgres write durable independent of JIRA reachability |

**Cardinality** — every label seen in the OP project belongs to **exactly one** class. There is no third bucket. Labels currently written by runner code that look like state (e.g. `runner-blocked:waiting-OP-927`) are B-class even though they pre-date the substrate; they move under the strangler migration in §6.

## 3. A-class label registry (durable; stays in JIRA)

Authoritative source: `docs/sop/jira-label-schema.yaml`. Every namespace
in `namespaces.*` whose `group` is **not** `state`, plus every entry in
`bare_labels`, is A-class. Restated here so that this doc is a complete
substrate contract on its own.

### 3.1 Routing (required on every ticket; `group: routing`)
- `class:*` — agent class (e.g. `class:subscription-codex`, `class:subscription-claude`, `class:api-anthropic`); JQL pickup filter
- `tier:S | tier:M | tier:L | tier:X` — work-budget gate
- `area:*` — multi-valued domain whitelist (9-value enum: `backend / db / devops / docs / embedded / frontend / security / tests / tooling`); drives prompt boundary

### 3.2 Capability override (`group: capability`)
- `capability:enable=<cap>` — adds a capability the OP-855 matrix denies
- `capability:disable=<cap>` — removes a capability the matrix grants
- `<cap>` is one of the OP-855 enum: `code_edit`, `run_tests`, `run_lint`, `gerrit_push`, `jira_update`, `mcp_search`, `memory_recall`, `run_migration`, `deploy_action`, `run_outcomes_grader`

### 3.3 Taxonomy (`group: taxonomy`)
`sprint:*`, `phase:*`, `scope:*`, `meta:*` (META-component only), `adr:NNNN`, `epic:<slug>`, `audit:YYYY-MM-DD`, `incident:YYYY-MM-DD-<slug>`, `tech-debt:*`, `compliance:*`, `drift:over-run | drift:over-tier` (auto-applied by `scripts/jira-drift-detector`), `wave:complete-pending-retro` (auto-applied by `scripts/jira_epic_lifecycle.py`), `mutex:<resource-id>` (operator-gated coord-override), `agent:auto` (set by filing script), `type:*`, `release:v*` (transitional; prefer fixVersion), `label:*`, `priority:meta` (the lone surviving `priority:*`).

### 3.4 Bare markers (`bare_labels`)
`migrated-from-todo`, `migrated-from-todo-bulk`, `rc-blocker`, `good-first-ticket`, `needs-operator-input`, `priority:meta` (colon-bearing exception).

### 3.5 Operator-set runner control (A-class even though they share the `runner-` prefix)
These look like state labels but are durable operator decisions; runner code **reads** them, never writes them.

| Label | Owner / writer | Effect |
|---|---|---|
| `runner:no-commits-expected` | Operator | Changes the post-CLI success contract (ops-only ticket). `coord_override_adjacent: true` in schema. |
| `runner-skip-ci` (`SKIP_CI_LABEL`) | Operator | CI-recovery agent treats this ticket as opt-out. |
| `runner-force-submit` (`FORCE_SUBMIT_LABEL`) | Operator | Allows `scripts/force_submit.py` to bypass the normal gate. |
| `runner-needs-refinement` (`NEEDS_REFINEMENT_LABEL`) | Operator / refinement bot | Excludes from pickup JQL until refined. |
| `runner-batch-merge-candidate` (hashtag) | Operator | AI reviewer eligibility marker. |
| `runner-glance-required` (hashtag) | Operator | AI reviewer eligibility marker. |
| `runner-trust-tier=*` (hashtag) | Operator | Trust scoring pin. |
| `runner-mutex-override:file-overlap` (`FILE_OVERLAP_OVERRIDE_LABEL`) | Operator | One-shot override for the file-collision mutex. |

These remain in JIRA labels indefinitely. Family ⑩'s capability registry
(`v2-Ⅹ-5a / v2-Ⅹ-5bc`) will eventually fold the `runner-trust-tier=*`
and friends into typed config rows, but until then they are A-class.

## 4. B-class label registry (volatile; moves to Postgres)

Every label below was written by **runner code** as a side-effect of a
pickup attempt. They encode coordination state, not durable ticket
metadata. They migrate to columns of `runner_claims` per §5; reading
them off JIRA labels stops once `v2-Ⅹ-2-Cutover` lands.

| Today's label | Authoring code path | Migrates to `runner_claims` column |
|---|---|---|
| `claim:{instance}:{epoch_us:016d}-{uuid8}` (fenced; OP-977) | `claim_ticket_atomic` in `backend/agents/jira_dispatch.py` | `owner_instance_id` + `fencing_token` + `acquired_at` + `state='active'` |
| `claim:{instance}` (legacy bare; OP-838 pre-AUDIT-24) | Legacy `_claim_ticket_atomic_legacy` (rollback flag) | Same; treated as expired at read time |
| `runner-blocked:waiting-OP-{N}` (`DEPENDENCY_WAITING_LABEL_PREFIX`) | `pre_pickup_ok()` dependency check | `state='blocked'` + `release_reason='dep-waiting-OP-{N}'` |
| `runner-skipped:file-collision` (`FILE_COLLISION_SKIP_LABEL`) | File-mutex pre-pickup | `state='blocked'` + `release_reason='file-collision'` |
| `runner-loop-paused-pending-review` (`LOOP_PAUSED_LABEL`) | CI-recovery agent on repeated failure | `state='paused'` + `release_reason='loop-paused'` + `phase='ci-recovery'` |

### 4.1 The `[runner-*]` comment tags

The runner emits many bracket-prefixed comment tags as audit trail —
e.g. `[runner-presync-fail]`, `[runner-mutex-lost]`,
`[runner-workspace-tampered]`, `[runner-capability-blocked]`,
`[runner-push-fail:<category>]`, `[runner-toctou:<phase>:<action>]`,
`[runner-yielded-to-human-authority]`, `[runner-pushed-to-gerrit]`,
`[runner-h12-self-heal]`, `[runner-discovered-dependency]`,
`[runner-ops-only-*]`, `[runner-transition-skip]`, etc.

**Disposition**: comment tags **remain in JIRA**. They are append-only
human-readable audit, not coordination state. The substrate **does**
write a single summarizing comment at lease release (per Package 2
§2 closing paragraph: "JIRA should receive a summarized comment after
state converges, not be the lock implementation"); individual phase
tags continue to be posted for human readability.

### 4.2 Runtime worktree artifacts (also B-class — see complement spec)

`progress.txt`, `progress.txt.tmp`, `.runner-cwd-sentinel` are written
inside the worktree today (`docs/sop/runner-runtime-artifacts.md`).
They are B-class **by category** (runner-owned bookkeeping) but they
do not live as JIRA labels and are out of this contract's scope.
Family ⑩'s `v2-Ⅹ-3` consolidates their filter; the long-term solution
(externalize them out of the worktree entirely) is tracked as a
follow-up after the substrate lands.

## 5. `runner_claims` table schema

This is the contract that `v2-Ⅹ-1bc` (alembic migration + ORM model
+ shadow-only writer) implements. The runner's mutex semantics
(fencing token, lowest-token-wins, staleness sweep) carry over from
`docs/sop/runner-pickup-mutex.md` §2 — the substrate is the same
mechanism on a different storage primitive.

```yaml
table: runner_claims
primary_key: lease_id            # opaque, runner-mintable, audit-friendly
columns:
  ticket_key:        text       not null    # e.g. "OP-1104"
  resource_key:      text       not null    # mutex resource (ticket-key for ticket lock; file-overlap key for §4 file-mutex; "claim-default" etc.)
  lease_id:          text       not null primary key
  owner_agent_class: text       not null    # class:* of the holder, e.g. "subscription-codex"
  owner_instance_id: text       not null    # the bot's instance id ("default" today)
  fencing_token:     text       not null    # "{epoch_us:016d}-{uuid8}" — same lexicographic == chronological property as today's label
  state:             text       not null    # enum: active | blocked | paused | released | force-released | expired
  phase:             text       null        # FSM phase: pickup | prepare | cli | push | post-push | terminal-success | terminal-fail | ci-recovery
  heartbeat_at:      timestamptz not null   # bumped at every phase write; staleness sweep uses this
  acquired_at:       timestamptz not null
  released_at:       timestamptz null       # null while state ∈ {active, blocked, paused}
  release_reason:    text       null        # free-form; canonical values: success | pre-sync-fail | capability-fail | no-commits | dirty-worktree | push-fail | mutex-lost | dep-waiting-OP-{N} | file-collision | loop-paused | runner-stopped | rescue-force
  external_refs:     jsonb      not null default '{}'  # {gerrit_change_url, gerrit_change_id, jira_comment_ids, idem_key, ...} populated by record_phase
indexes:
  - (resource_key, state) WHERE state IN ('active','blocked','paused')   # hot path: find_active_holders
  - (ticket_key, acquired_at DESC)                                       # human queries + RescueCLI dump
  - (owner_instance_id, state) WHERE state = 'active'                    # OP-783 1:1 invariant guard
  - (heartbeat_at) WHERE state = 'active'                                # staleness sweep
constraints:
  - "exactly one row per resource_key WHERE state = 'active'"            # enforced via partial unique index
  - "released_at IS NULL  ≡  state IN ('active','blocked','paused')"     # CHECK constraint
  - "fencing_token matches '^[0-9]{16}-[0-9a-f]{8}$'"                    # schema-level format guard
```

**Invariants (lifted from OP-977 §3.2 and Package 2 §4)**:
1. **OP-783 1:1**: a given `(owner_instance_id, bot account)` pair never has > 1 row with `state='active'` across resources at the same instant. The partial unique index on `(owner_instance_id, state)` is the runtime enforcer.
2. **Lowest-token-wins**: when `acquire_claim` finds an existing `state='active'` row for a `resource_key`, the requester compares its own fencing token to the incumbent's. A lower token wins (caller is the earlier claimer); the loser exits cleanly without a write, identical to today's `_lowest_uuid_claim_winner`.
3. **Stale sweep**: a row with `state='active'` and `heartbeat_at < now() - OMNISIGHT_RUNNER_STALE_CLAIM_MAX_AGE_S` (default 7200s = 2× CLI timeout) is swept to `state='expired'` on the next `acquire_claim` against its `resource_key`. Same semantics as today's `BackwardCompatStaleClaim` + `OrphanClaimLabel` from `docs/sop/runner-pickup-mutex.md` §4.
4. **Terminal release is idempotent**: `release_claim(lease)` on an already-released lease is a no-op (audit-logged, never raises). Same best-effort semantics as today's `release_ticket_claim`.

## 6. 4-function API surface

These are the only entry points the runner uses against the substrate.
Anything else (raw SQL, bare label writes for new state, ad-hoc helper
modules) is an architectural regression. `v2-Ⅹ-1bc` implements all four
in shadow-only mode (writes the table but no consumer reads it).
`v2-Ⅹ-2bc` and `v2-Ⅹ-2d` switch the consumers.

```python
# backend/agents/runner_coordination.py — interface contract (v2-Ⅹ-1bc implements)

def acquire_claim(
    client: DispatchClient,
    ticket_key: str,
    resources: list[str],
    agent_class: str,
    instance_id: str,
) -> ClaimLease | ClaimBlocked:
    """
    Mint a fencing token, INSERT one row per resource with state='active',
    enforce the partial unique index, run lowest-token-wins against any
    incumbent, sweep stale rows on the path.

    Returns ClaimLease(lease_id, token, resources, ...) on win;
    ClaimBlocked(reason, incumbent_lease_id, incumbent_owner) on lose.

    Replaces: claim_ticket_atomic() label-mutex path in
    backend/agents/jira_dispatch.py (read-side compat retained 1 sprint
    via shadow read of claim:* labels by v2-Ⅹ-2-Shadow).
    """

def release_claim(
    lease: ClaimLease,
    reason: str,
    external_refs: dict | None = None,
) -> None:
    """
    UPDATE released_at, state='released', release_reason=reason,
    merge external_refs. Idempotent; best-effort if DB unreachable
    (logged WARN, not raised — same posture as today's
    release_ticket_claim).

    Wired into a finally block around every terminal path in
    auto-runner-jira.py, auto-runner-multi.py, auto-runner-codex.py
    (v2-Ⅹ-2d):
      1. pre-sync fail        → reason='pre-sync-fail'
      2. capability denial    → reason='capability-fail'
      3. no-commit            → reason='no-commits'
      4. dirty worktree       → reason='dirty-worktree'
      5. push hard-fail       → reason='push-fail'
      6. clean success        → reason='success'
    """

def record_phase(
    lease: ClaimLease,
    phase: str,
    artifact_refs: dict | None = None,
) -> None:
    """
    UPDATE heartbeat_at = now(), phase = <new>, merge artifact_refs
    into external_refs. Called at every FSM phase boundary; doubles
    as the heartbeat against the staleness sweep.

    Replaces ad-hoc writes to progress.txt + comment-based phase
    tagging as the *coordination* signal. Comment tags continue to
    be posted for human readability (§4.1); the substrate row is
    the authoritative phase record.
    """

def find_active_holders(
    resources: list[str],
    exclude_ticket: str | None = None,
) -> list[ClaimLease]:
    """
    SELECT * FROM runner_claims WHERE state='active'
    AND resource_key = ANY(resources)
    AND (exclude_ticket IS NULL OR ticket_key <> exclude_ticket).

    Replaces find_mutex_holders() JQL search in jira_dispatch.py.
    Used by pre_pickup_ok() to decide mutex eligibility.
    """
```

### 6.1 Out-of-API operations
- **DB schema mutation** — owned by alembic migrations under `v2-Ⅹ-1bc`; never inline.
- **Force-release** — `omnisight runner-rescue release <lease_id>` (`v2-Ⅹ-RescueCLI`) writes `state='force-released'` with `release_reason='rescue-force'`, requires L2 fingerprint per ADR-0033, audit-logged.
- **Mass dump for forensics** — `omnisight runner-rescue dump --ticket <key>` (`v2-Ⅹ-RescueCLI`) read-only.

## 7. Migration rules — strangler order

Per Package 2 §8 sequencing + spec §3 Family ⑩ table.

1. **`v2-Ⅹ-1bc` — substrate exists** (shadow-only): `runner_claims` table + module + tests. Runner code does **not yet** read it. Risk: zero — pure add.
2. **`v2-Ⅹ-2-Shadow` — dual-write** (1 week observation): every `claim:*` label write **also** writes to `runner_claims`; daily comparison report ensures the two states agree. No behavior change.
3. **`v2-Ⅹ-2bc` — read replacement**: `find_mutex_holders()` and `pre_pickup_ok()` mutex check switch to `find_active_holders()`. JIRA labels still written (dashboards) but not authoritative.
4. **`v2-Ⅹ-2d` — write replacement**: `acquire_claim()` before `transition_to_in_progress()`; `release_claim()` in the finally block of all six terminal paths (above) across `auto-runner-jira.py` / `auto-runner-multi.py` / `auto-runner-codex.py`.
5. **`v2-Ⅹ-2-Cutover` — drop label writes**: read-only label compatibility kept for 1 sprint for dashboard consumers, then dropped.
6. **`v2-Ⅹ-3` — runtime artifacts**: `RUNNER_RUNTIME_ARTIFACTS` constant centralized (closes the structural debt that OP-1074/1075/1076 patched).

**Validation contract** (1 week after `v2-Ⅹ-2-Cutover` on last 40 runner-handled tickets, per spec §3 Family ⑩):
- `claim:*` labels on terminal tickets drop from **40% → 0%**
- `runner-blocked` drops from **45% → < 10%**
- 0 ticket has > 1 `state='active'` row per `resource_key`
- 0 duplicate pickup attempts on a single ticket
- Regression test: OP-1070-pattern replay (progress.txt + sentinel present) does not revert at finalization

## 8. Defense contract (D1–D5) for the substrate itself

Dog-fooded per G.A-v2 Q7 (required immediately on v2-* family).

```yaml
defense_contract:
  error_detection:
    signal: prometheus_metric
    channel: runner_claim_acquire_total{outcome="lost|won|api-error"}, runner_claim_stale_total, runner_claim_active{resource_key, owner_instance_id}
    detection_latency_p99: 30s
    observability_test: backend/tests/test_runner_coordination_metrics.py    # v2-Ⅹ-1bc implements
    alert_rule_id: RunnerClaimStale, RunnerPickupBlockRate, RunnerStateDrift  # v2-Ⅹ-AlertRule wires

  exception_handling:
    enumerated_states: [active, blocked, paused, released, force-released, expired, api-error]
    remediation_hint_contract: "every ClaimBlocked carries (incumbent_lease_id, incumbent_owner_instance_id, incumbent_acquired_at) + a 1-line human hint; operator can pass lease_id straight to `omnisight runner-rescue dump <lease_id>`"
    user_facing: false               # operator-facing only; never reaches an end user
    fail_loudly: true                # ClaimAPIError raised (not swallowed) when DB unreachable so the runner can choose its fallback posture

  shutdown_contract:
    drain_seconds_p99: 5             # release_claim() is one UPDATE; well inside any TimeoutStopSec
    cleanup_sequence:
      - "for each active lease owned by this instance: release_claim(reason='runner-stopped')"
      - "flush metrics"
      - "exit 0"
    forced_termination_safe: true    # SIGKILL leaves rows; staleness sweep reaps them at next acquire (same recovery as today's OrphanClaimLabel)
    state_persistence: "runner_claims rows survive process death; lease ownership is reconstructable from the table alone"

  recovery_path:
    trigger_condition: "Postgres unreachable for substrate writes"
    steps:
      - "acquire_claim raises ClaimAPIError; caller (auto-runner-jira.py) converts to clean rc=0 and skips pickup this tick (same posture as today's RunnerMutexAPIError)"
      - "release_claim logs WARN and swallows (best-effort; release is non-load-bearing for correctness — the staleness sweep is the backstop)"
      - "next runner tick retries the acquire path"
    idempotent: true
    rto_seconds_p99: 60              # one tick interval
    evidence_file: backend/tests/test_runner_coordination_db_unavailable.py   # v2-Ⅹ-1bc implements

  rescue_path:
    trigger_condition: "stuck active lease that the runner cannot release on its own (e.g. process killed before reaching its finally block AND staleness threshold not yet reached AND blocking new pickup)"
    operator_actions:
      - "omnisight runner-rescue dump --ticket <key>                # inspect current rows"
      - "omnisight runner-rescue release <lease_id> --reason <text> # force state='force-released'"
      - "omnisight runner-rescue reset <ticket_key>                 # mass-release all leases for a ticket"
    authority_required: L2           # per ADR-0033; cross-runner state mutation
    audit_trail: "every rescue write inserts a runner_audit_events row (event_type, actor_fingerprint, ticket_ref, before_state, after_state, evidence_path, timestamp); persists across rescue process restart per spec §3.0.6 subcontract 7"
```

## 9. Open questions / operator sign-off needed

These remain pending operator decision before `v2-Ⅹ-ADR` (ADR-0037) can
close. None block this spec.

1. **Operator-set runner labels** (§3.5): confirm the 8 listed are A-class
   forever, not "A-class until v2-Ⅹ-5a registry replaces them". Default
   assumption: all 8 stay JIRA labels indefinitely; the capability
   registry only formalizes their **schema**, not their **location**.
2. **`runner-loop-paused-pending-review`** is currently runner-written
   on detected loops but operator-cleared after review. §4 categorizes
   it B-class (writer-side). Verify the read-side (humans glancing
   at JIRA) doesn't need it to stay a label after cutover; if it does,
   keep a read-only label mirror until the dashboard rebuild.
3. **`mutex:*` labels (operator-gated)** are A-class taxonomy today
   (§3.3). The substrate uses `resource_key` for the same purpose at
   runtime. Sign-off: keep `mutex:*` as the durable declaration
   (operator authors), let the substrate read it once at acquire-time
   and translate into `resource_key`s — i.e. no migration needed for
   `mutex:*`.
4. **Audit-event sink** (rescue-path D5 `audit_trail`): point at the
   existing `audit_log` table from OP-693 (not the `release_audit`
   table from alembic 0207 — that's release-event scoped). Confirm
   before `v2-Ⅹ-RescueCLI` is filed.

## 10. References

- Codex Package 2 self-audit: `docs/audit/codex-reviews/runner-self-audit-and-redesign-2026-05-14.txt` (§0 bottom line, §1 lived failure modes, §2 labels-as-state critique, §4 redesign, §8 sequencing)
- Sprint Atlas spec: `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑩
- Existing label schema: `docs/sop/jira-label-schema.yaml` + `docs/sop/jira-label-conventions.md`
- Existing mutex mechanism (being superseded): `docs/sop/runner-pickup-mutex.md` (OP-977 / AUDIT-24)
- Runtime artifacts complement: `docs/sop/runner-runtime-artifacts.md` (OP-1076)
- State-authority precedence: `docs/sop/runner-state-authority-precedence.md` (OP-1070 / SP-B-X-012)
- ADR antecedents: ADR-0033 (L1/L2/L3 authority gates), ADR-0035 (runner FSM + error handling)
- Filing tools (consumers of A-class taxonomy): `scripts/file_jira_ticket.py`, `scripts/jira-label-migrate.py`
