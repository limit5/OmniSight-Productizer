# Capability registry — typed-policy schema (replaces `capability:enable=*` labels)

**Status**: Spec draft 2026-05-15 (Sprint Atlas, OP-1114 / `v2-Ⅹ-5a`)
**Sprint**: Atlas (S12.G G.A-v2 Family ⑩ — Runner Defense Contract)
**Spec parent**: `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑩ row 11
**Companion specs**: `docs/sop/runner-substrate-contract.md` (OP-1104 / `v2-Ⅹ-1a` — JIRA-as-ledger / Postgres-as-lock); `docs/sop/runner-pickup-mutex.md` (OP-977 / AUDIT-24 — fencing-token mutex semantics carried over)
**Defense dimensions covered**: D1 (error detection — registry rows are observable) + D2 (exception handling — pickup gate raises a typed `CapabilityRegistryDenied` instead of swallowed default)
**Consumed by**: `v2-Ⅹ-5bc` (`capability_profile` table + alembic + dual-source adapter + tests), `v2-Ⅹ-5d` (pre-pickup quota enforcement via `provider_orchestrator.py`), `v2-Ⅹ-5e` (failover deconfliction matrix)
**Supersedes (read-only compat 1 sprint)**: flat `capability:enable=<cap>` / `capability:disable=<cap>` JIRA labels (`backend/agents/capability_matrix.py::apply_label_overrides`, OP-855); the OP-855 `(ticket_type × area × tier)` YAML matrix (`config/capability_matrix.yaml`) remains the **base** lookup but is fronted by the typed registry for per-(provider, model) overlays

## 1. Why this exists

The runner currently expresses three orthogonal kinds of policy through
**one** mechanism — flat JIRA labels of the form
`capability:enable=<cap>` / `capability:disable=<cap>` — and one
mostly-orthogonal YAML matrix:

| Concern | Today | Today's failure mode |
|---|---|---|
| What capabilities does **this ticket type / area / tier** unlock? | `config/capability_matrix.yaml` (OP-855) | One axis; doesn't know the agent picking it up |
| What capabilities does **this provider × model** support / refuse? | implicit; encoded in adapter Python (`provider_adapters/*.py`) | Not declarable; not queryable; not testable |
| What capabilities does **this specific pickup** add or remove for a one-off operator override? | `capability:enable=<cap>` label on the JIRA ticket | Untyped string; no schema; survives the pickup that needed it; collides with taxonomy |

Three concrete pain points have surfaced from these:

1. **Untyped escape hatch.** `capability:enable=gerrit_push` is a free
   string. There is no schema declaring "this label is permitted only on
   these (ticket_type, area, tier) cells", so a typo
   (`capability:enable=gerit_push`) is silently dropped on the
   `_assert_known_capability` floor with a runtime `CapabilityMatrixError`
   — caught only when the runner actually tries to push.
   `scripts/file_audit_29_decompose.py` slings 25-line `extra_caps=[…]`
   blocks of these labels by hand, with no model awareness.
2. **Provider-blind matrix.** The OP-855 matrix maps `(type × area ×
   tier) → caps`, with **no** `provider × model` axis. A `gemini-subscription`
   pickup on a backend / S ticket gets the same nominal capability set
   as an `anthropic-subscription` pickup, even though Gemini's adapter
   today is a structural placeholder (`HealthStatus(reachable=False)`)
   and cannot in fact `gerrit_push`. The runner only finds out at
   dispatch time, after `acquire_claim()` has been minted.
3. **No typed cost / health / failure-class signal.** Quota
   (`provider_quota_tracker`), circuit-breaker state
   (`provider_orchestrator.CircuitBreaker`), failure-class taxonomy
   (`backend/agents/failure_class.py`), and the trust-tier hashtag
   (`runner-trust-tier=*` on Gerrit changes) are five separate stores
   with five separate update paths. None of them is consulted **before**
   the pickup gate. The pre-pickup question — "should *this* model
   pick up *this* ticket *now*?" — is answered piecemeal across five
   modules, after the lock is taken.

This spec is the contract for collapsing the three concerns into a
single typed-policy data structure, keyed on `(provider, model)` and
fronted as an overlay on the OP-855 base matrix. `v2-Ⅹ-5bc` lands the
`capability_profile` Postgres table + the dual-source adapter that
reads both new rows AND old `capability:enable=*` labels for one sprint;
`v2-Ⅹ-5d` and `v2-Ⅹ-5e` consume the typed health / cost / failure-class
fields for pre-pickup enforcement and failover deconfliction.

This spec does **not**:
- Implement the table or migration — that's `v2-Ⅹ-5bc`.
- Wire pre-pickup quota checks — that's `v2-Ⅹ-5d`.
- Decide failover deconfliction policy — that's `v2-Ⅹ-5e`.
- Touch the OP-855 base matrix YAML — the matrix remains the source of
  truth for `(type × area × tier)` defaults; the registry is an
  **overlay**, applied last (§4).

## 2. Scope axes — the seven dimensions

Per ticket description and Family ⑩ row 11, a typed policy entry is
keyed on these seven axes. Each is enumerated; nothing is a free string.

| Axis | Type | Source of truth | Example values |
|---|---|---|---|
| **provider** | enum | `backend.agents.provider_orchestrator.SUBSCRIPTION_VENDOR_REGISTRY` keys (5 today: `anthropic`, `google`, `grok`, `openai`, `xai`) | `anthropic` |
| **model** | string (provider-scoped) | provider adapter declares its supported models in `models:` block (§3.3) | `claude-opus-4-7`, `claude-sonnet-4-6`, `gpt-5-codex`, `gemini-2.5-pro` |
| **tools** | set\<capability\> | OP-855 `CAPABILITIES` enum (`backend.agents.capability_matrix.CAPABILITIES`, 10 values today: `code_edit`, `run_tests`, `run_lint`, `gerrit_push`, `jira_update`, `mcp_search`, `memory_recall`, `run_migration`, `deploy_action`, `run_outcomes_grader`) | `{code_edit, gerrit_push, jira_update}` |
| **max_tier** | enum | ADR-0005 tier ladder (`S < M < L < X`); `X` means "operator-only, never pick up" | `M` (this entry never picks up an L-tier ticket) |
| **cost_mode** | enum | `subscription` (flat-rate CLI; quota enforced by `provider_quota_tracker`); `metered` (per-token billing; quota enforced by upstream provider); `local` (no upstream quota — e.g. ollama / local-LLM 4th-runner experiment) | `subscription` |
| **health** | enum + last-checked timestamp | `provider_adapter.health_check()` projection: `healthy` (reachable + subscription_active) / `degraded` (reachable but circuit-breaker half-open) / `down` (not reachable) / `unknown` (never probed) | `healthy` |
| **known_failure_classes** | set\<FailureClass\> | `backend.agents.failure_class.FailureClass` enum (11 values today). The set names failure classes this entry has demonstrated repeatedly (≥ N within rolling window). Used by `v2-Ⅹ-5e` failover deconfliction: a fallback model that shares a known failure class doesn't get to pick up the same ticket. | `{LINT_FAILURE, RUNNER_TIMEOUT}` |

### 2.1 Why these seven and not more

Codex's Package 2 self-audit named exactly these seven as the policy
shape required to fix Modes 1-3 + the pre-pickup-quota finding (§3
Family ⑩ row 13: "quota currently checked POST partial execution; must
be enforced PRE pickup so cost-bearing failure doesn't happen
mid-flight"). Anything beyond these seven is **not** a registry concern:

- **Routing weight / priority** is not a policy field; it's a
  scheduler-side concern — out of scope, owned by the scheduler.
- **Per-tenant overrides** are out of scope for v0; the Runner-as-Product
  pivot (`docs/architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md`)
  reframes per-tenant policy as a Layer-3 concern that consumes this
  registry, not a column on it.
- **Per-area boundary lists** stay in `auto-runner-jira.py::RECOGNISED_AREAS`
  + the `area:*` label whitelist; the registry only grants / denies
  capabilities, not paths.

## 3. The `capability_profile` schema

This is the contract that `v2-Ⅹ-5bc` (alembic migration + ORM model +
dual-source adapter + tests) implements. The shape below is the
authoritative declaration; the table DDL + ORM model in `v2-Ⅹ-5bc` MUST
match it.

```yaml
table: capability_profile
primary_key: profile_id           # opaque ULID, not (provider, model) — see §3.1
columns:
  profile_id:             text       not null primary key      # ULID, monotonic, sortable; survives provider+model rename
  provider:               text       not null                  # enum from SUBSCRIPTION_VENDOR_REGISTRY
  model:                  text       not null                  # provider-scoped string; e.g. "claude-opus-4-7"
  tools:                  jsonb      not null                  # array<capability>; subset of CAPABILITIES; sorted at write time
  max_tier:               text       not null                  # enum: S | M | L | X
  cost_mode:              text       not null                  # enum: subscription | metered | local
  health_state:           text       not null                  # enum: healthy | degraded | down | unknown
  health_checked_at:      timestamptz null                     # null while health_state='unknown'
  known_failure_classes:  jsonb      not null default '[]'     # array<FailureClass>; sorted at write time
  active:                 boolean    not null default true     # soft-delete; deactivated rows kept for audit
  created_at:             timestamptz not null default now()
  updated_at:             timestamptz not null                 # bumped on every UPDATE; trigger-enforced
  created_by:             text       not null                  # actor: 'system' (auto-bootstrap from adapter), 'operator:<fingerprint>' (RescueCLI write), 'auto-redeploy:<lease_id>' (failure-class learner future)
  notes:                  text       null                      # free-form operator annotation; never read by runner code
indexes:
  - (provider, model) WHERE active = true                      # unique partial — only one active row per (provider, model)
  - (provider, model, updated_at DESC)                         # historical lookup including soft-deleted rows
  - (health_state) WHERE active = true                         # health-dashboard query
constraints:
  - "EXACTLY ONE row per (provider, model) WHERE active = true"           # unique partial index
  - "tools ⊆ CAPABILITIES"                                                 # CHECK via array containment + a per-write Python validator
  - "max_tier ∈ {S, M, L, X}"
  - "cost_mode ∈ {subscription, metered, local}"
  - "health_state ∈ {healthy, degraded, down, unknown}"
  - "known_failure_classes ⊆ FailureClass enum"
  - "(health_state = 'unknown') ≡ (health_checked_at IS NULL)"             # CHECK constraint
```

### 3.1 Why `profile_id` (ULID), not `(provider, model)`

Provider rebrands and model versioning happen
(`claude-3-opus-20240229` → `claude-opus-4-7`; `gpt-4` → `gpt-5-codex`).
A natural-key primary key would force every UPDATE to either shadow-write
the old row or destroy historical audit. ULID + soft-delete `active`
column lets us:
1. Rotate `claude-opus-4-7` → `claude-opus-4-8` by inserting a new row
   and flipping `active` on the old one.
2. Keep the old row's `known_failure_classes` history for the failure-class
   learner (future work, not in 5a/bc/d/e scope).
3. Reference a profile by `profile_id` from the future
   `runner_dispatch_log` table (post-`v2-Ⅹ-5e`) without breaking when the
   model name shifts.

### 3.2 Why JSONB for `tools` and `known_failure_classes`, not separate join tables

Both are bounded sets (≤ 10 capabilities; ≤ 11 failure classes). Either
shape works on Postgres; JSONB wins on:
- **Read-locality**: every pickup reads both fields together in §4's
  `resolve_overlay`. A join would add two extra round trips.
- **Schema-velocity**: the OP-855 capability vocabulary or
  `FailureClass` enum can grow in their respective Python modules
  without an alembic migration on this table — only the per-write
  Python validator changes.

The trade-off: SQL-only filters (`WHERE 'gerrit_push' = ANY(tools)`)
need a JSONB containment query (`WHERE tools @> '["gerrit_push"]'`).
That's acceptable — the only consumer of that filter is the dashboard,
not the hot pickup path.

### 3.3 Adapter-side `models:` declaration

Each provider adapter exports a `MODELS: tuple[str, ...]` constant
naming the models the adapter knows how to dispatch against. `v2-Ⅹ-5bc`
bootstraps the registry by inserting one row per `(provider, model)`
in the cross-product of `SUBSCRIPTION_VENDOR_REGISTRY` ×
`AdapterModule.MODELS`, with sane defaults inferred from the adapter's
current shape (`tools`, `cost_mode`, `health` all derived — see §5
round-trip evidence).

The constant is the **only** new piece of code each adapter needs
during the strangler period; everything else is computed by the bootstrap
script. Adapters that don't yet declare `MODELS` get a single-row
`(provider, '<unknown>')` entry that the runner refuses pickups against
until an operator fills it in via RescueCLI.

## 4. Resolution semantics — overlay on the OP-855 matrix

The registry is an **overlay**, not a replacement. The OP-855 matrix
remains the source of truth for "what can a backend / S ticket do at
all"; the registry narrows that set further by the `(provider, model)`
of the pickup-attempting agent.

```
resolve(ticket_type, area, tier, labels, *, provider, model) -> frozenset[str]
    base    = capability_matrix.resolve(ticket_type, area, tier, labels=[], strict=False)
    overlay = registry.lookup_active(provider, model)         # may raise CapabilityRegistryMissingProfile
    typed   = base ∩ overlay.tools                            # registry can only narrow, never widen
    typed_with_label_overrides = apply_label_overrides(typed, labels)   # legacy path; deprecated end of next sprint
    enforce_max_tier(overlay.max_tier, tier)                  # raises CapabilityRegistryDenied if tier > overlay.max_tier
    enforce_health(overlay.health_state)                      # raises CapabilityRegistryDenied if 'down'; warns if 'degraded'
    return typed_with_label_overrides
```

Five locked rules:

1. **Registry can only narrow.** The intersection step (`base ∩
   overlay.tools`) makes it impossible for a registry row to grant a
   capability the OP-855 matrix denied. This protects the matrix's
   tier-S-no-gerrit-push invariant from a misconfigured registry row.
2. **Label overrides still applied last (sprint of compat).**
   `apply_label_overrides()` from `capability_matrix.py` runs **after**
   the intersection. This is the strangler bridge: while operators
   still file tickets with `capability:enable=*` labels, those labels
   keep working. After the cutover sprint, the label overrides path
   becomes a deprecation warning (still applies, but logs WARN) for one
   more sprint, then is removed.
3. **`max_tier` is a hard pickup gate.** A registry entry with
   `max_tier=M` raises `CapabilityRegistryDenied` for an `L`-tier
   ticket *before* `acquire_claim()` is called. No partial pickup, no
   `transition_to_in_progress()`. The runner emits a
   `[runner-capability-blocked:max-tier]` comment tag and skips this
   tick.
4. **`health=down` is a hard pickup gate.** Same posture as `max_tier`.
   The runner can't dispatch through a `down` adapter; pre-pickup denial
   is cheaper than a mid-pickup `DispatchResult(success=False)`.
   `health=degraded` proceeds with a WARN log; `health=unknown` proceeds
   but enqueues a health probe.
5. **Missing profile → safe-default-then-warn.** If the registry has
   no active row for `(provider, model)`, `resolve` falls back to the
   OP-855 matrix's `read_only_default` and emits a
   `[runner-capability-blocked:missing-profile]` tag. This matches the
   existing `CapabilityMatrixMissingEntry` posture — never crash on
   pickup, always log loud.

### 4.1 The `cost_mode` and `known_failure_classes` fields are read by sibling tickets, not by §4

`v2-Ⅹ-5d` reads `cost_mode` to decide whether `provider_quota_tracker`
must be consulted pre-pickup (`metered` and `subscription` yes; `local`
no). `v2-Ⅹ-5e` reads `known_failure_classes` to decide whether a
fallback model's profile shares enough failure DNA with the failed
primary that handing the ticket to the fallback is futile. Both are
out of this spec's scope but the registry shape MUST carry the fields
so 5d / 5e have something to read.

## 5. Round-trip — the 5 current adapters into the new schema

This section is the Exercised AC. For each of the 5 subscription
adapters in `backend/agents/provider_adapters/` (commit
`d1de899b`, develop, 2026-05-15), it derives the registry row that
`v2-Ⅹ-5bc`'s bootstrap script would write, from the adapter's
in-tree shape. No source change is made to any adapter as part of
this round-trip; the round-trip is an inspection exercise that proves
the schema can express each adapter's current behavior without loss.

### 5.1 `anthropic-subscription` (claude CLI)

Source: `backend/agents/provider_adapters/anthropic_subscription.py:43`
(`PROVIDER_ID = "anthropic-subscription"`); concrete dispatch via
`["claude", "-p", "--output-format", "json", ...]`. CLI-installed +
subscription-active gate (`_subscription_active` returns True iff
`loggedIn==True && authMethod=="claude.ai" && subscriptionType ∉ {"", "none", "unknown"}`).

```yaml
profile_id:            01HZA000000000000000ANTHROPIC          # illustrative
provider:              anthropic
model:                 claude-opus-4-7                         # the model id from the system prompt; bootstrap reads adapter.MODELS
tools:                 [code_edit, run_tests, run_lint, gerrit_push, jira_update, mcp_search, memory_recall, run_migration, deploy_action, run_outcomes_grader]
max_tier:              L                                       # subscription cap; X is operator-only
cost_mode:             subscription                            # flat-rate CLI; quota enforced by provider_quota_tracker
health_state:          healthy                                 # default at bootstrap; flipped to 'down' if health_check() returns reachable=False
known_failure_classes: []                                      # empty at bootstrap; populated by v2-Ⅹ-5e learner (out of scope here)
active:                true
created_by:            system
notes:                 "Bootstrap from anthropic_subscription.py PROVIDER_ID + adapter.MODELS"
```

Round-trip verification:
- `tools` superset of {`gerrit_push`, `jira_update`}: the adapter's
  `dispatch()` returns successful `DispatchResult` with `tokens_used` —
  i.e. the CLI is wired for code_edit / push paths. ✓
- `cost_mode = subscription`: confirmed by
  `_record_usage(tokens_used)` writing into `provider_quota_tracker`. ✓
- `health_state = healthy` start state: `_health_detail` returns
  "claude CLI installed and subscription active" iff both gates pass.
  Flipping to `down` on a probe failure is the runner-side
  responsibility. ✓

### 5.2 `openai-subscription` (codex CLI)

Source: `backend/agents/provider_adapters/openai_subscription.py:43`
(`PROVIDER_ID = "openai-subscription"`); concrete dispatch via
`["codex", "exec", "--cd", os.getcwd(), "--yolo", "--json", "-"]`.
Same `_record_usage` + `_subscription_active` machinery as anthropic.

```yaml
provider:              openai
model:                 gpt-5-codex                             # codex CLI model id; bootstrap reads adapter.MODELS
tools:                 [code_edit, run_tests, run_lint, gerrit_push, jira_update, mcp_search, memory_recall, run_migration, deploy_action]
max_tier:              L                                       # symmetric with anthropic; X reserved for operator-window tickets
cost_mode:             subscription
health_state:          healthy
known_failure_classes: []
active:                true
created_by:            system
notes:                 "Bootstrap from openai_subscription.py; same dispatch shape as anthropic adapter"
```

Round-trip verification:
- `tools` excludes `run_outcomes_grader`: the codex CLI doesn't carry
  the `outcomes_grader` capability today (no equivalent of claude's
  reasoning-mode grader). Operator can flip this on later by RescueCLI
  if that changes. ✓ (asymmetry preserved — the registry expresses it
  natively where the OP-855 matrix could not)
- `cost_mode = subscription`: confirmed by `_record_usage` write. ✓

### 5.3 `gemini-subscription` (placeholder shell)

Source: `backend/agents/provider_adapters/gemini_subscription.py:34`
(`PROVIDER_ID = "gemini-subscription"`); `dispatch()` returns
`DispatchResult(success=False, error={"kind":"not_ready", ...})`;
`health_check()` returns `reachable=False`,
`detail="Gemini subscription runtime is not wired yet"`.

```yaml
provider:              google
model:                 <unknown>                               # adapter declares no MODELS yet (§3.3 placeholder rule)
tools:                 []                                      # no dispatch capability today
max_tier:              S                                       # placeholder; widened by RescueCLI when adapter is wired
cost_mode:             subscription                            # adapter declares subscription posture; revisit at wire-up
health_state:          down                                    # bootstrap-time projection of health_check()
health_checked_at:     <bootstrap-time>
known_failure_classes: []
active:                true                                    # row exists so dashboards see Gemini; resolve() refuses pickup via tools=[]
created_by:            system
notes:                 "Bootstrap from gemini_subscription.py placeholder; awaiting v0.6.0 / future MP wire-up"
```

Round-trip verification:
- `health_state = down` with `tools = []`: the §4 resolve flow narrows
  the OP-855 base to `∅` for any pickup attempt against this profile,
  AND raises `CapabilityRegistryDenied` on the `health=down` gate. This
  is the **desired** behavior for a placeholder — no silent degrade,
  no silent partial pickup. ✓
- The adapter never enters the failure-class taxonomy because it never
  reaches dispatch. ✓

### 5.4 `xai-subscription` (placeholder shell)

Source: `backend/agents/provider_adapters/xai_subscription.py:34`
(`PROVIDER_ID = "xai-subscription"`); identical placeholder shape to
gemini's adapter (returns `kind: not_ready`; health unreachable).

```yaml
provider:              xai
model:                 <unknown>
tools:                 []
max_tier:              S
cost_mode:             subscription
health_state:          down
health_checked_at:     <bootstrap-time>
known_failure_classes: []
active:                true
created_by:            system
notes:                 "Bootstrap from xai_subscription.py placeholder"
```

Round-trip verification: identical to §5.3 (placeholder symmetry). ✓

### 5.5 `grok-subscription` (coming-provider entry, not registered)

Source: `backend/agents/provider_adapters/grok_subscription.py:29`
(`PROVIDER_ID = "grok-subscription"`); adapter calls
`register_coming_provider(PROVIDER_ID, coming_version="v0.6.0")`
**instead of** `register_adapter(...)`. Its `dispatch()` /
`health_check()` raise `NotImplementedError(NOT_IMPLEMENTED_MESSAGE)`.

```yaml
provider:              grok
model:                 <unknown>
tools:                 []
max_tier:              S
cost_mode:             subscription
health_state:          unknown                                 # never probed; coming_version='v0.6.0'
health_checked_at:     null                                    # constraint: health_state='unknown' ≡ health_checked_at IS NULL
known_failure_classes: []
active:                true                                    # row exists so dashboards see "Grok coming v0.6.0"
created_by:            system
notes:                 "Bootstrap from grok_subscription.py register_coming_provider; awaiting MP.W14 / v0.6.0"
```

Round-trip verification:
- `health_state = unknown` (not `down`): the adapter is structurally
  flagged as a future provider, not a broken current provider. The
  resolve flow refuses pickup via `tools = []`, but health probes are
  not scheduled until adapter promotion. ✓
- The CHECK constraint `(health_state='unknown') ≡ (health_checked_at IS NULL)`
  is satisfied. ✓

### 5.6 Round-trip summary

| Adapter | `tools` size | `max_tier` | `cost_mode` | `health_state` | Row writable to schema |
|---|---|---|---|---|---|
| anthropic-subscription | 10 | L | subscription | healthy | ✓ |
| openai-subscription | 9 | L | subscription | healthy | ✓ |
| gemini-subscription | 0 | S | subscription | down | ✓ |
| xai-subscription | 0 | S | subscription | down | ✓ |
| grok-subscription | 0 | S | subscription | unknown | ✓ |

All 5 round-trip cleanly. The schema expresses the asymmetry between
anthropic and openai (the `run_outcomes_grader` cell), the placeholder
posture of gemini / xai (`health=down`, `tools=[]`), and the
coming-provider posture of grok (`health=unknown`, `tools=[]`) without
loss.

## 6. Migration plan — `capability:enable=*` labels → typed-policy entries

Strangler-pattern, three sprints. Same posture as the
`runner-substrate-contract.md` §6 migration: never break the read path,
always dual-write before flipping the read.

### 6.1 Phase 0 — `v2-Ⅹ-5bc` lands (this sprint, blocking on this spec)

- Alembic migration creates `capability_profile` table per §3.
- Bootstrap script (`scripts/bootstrap_capability_profiles.py`, new)
  walks `SUBSCRIPTION_VENDOR_REGISTRY`, imports each adapter module,
  reads its `MODELS:` constant + computes the §5 round-trip default
  row, INSERTs one `created_by='system'` row per `(provider, model)`
  pair.
- Dual-source adapter (`backend.agents.capability_registry.resolve`)
  reads BOTH the new `capability_profile` table AND the old
  `capability:enable=<cap>` labels; `apply_label_overrides()` in
  `capability_matrix.py` is unchanged. Tests assert that for every
  current ticket carrying a `capability:enable=*` label, the resolved
  capability set is unchanged.
- No call site flips yet. Pure shadow + dual-source — same risk
  posture as `v2-Ⅹ-1bc`.

### 6.2 Phase 1 — Operator UI / RescueCLI (this sprint or next)

- `omnisight runner-rescue capability {dump,set,deactivate}` extends
  the existing `runner-rescue` CLI surface from `v2-Ⅹ-RescueCLI`. L2
  fingerprint per ADR-0033 required for `set` / `deactivate`.
- `dump` is read-only and L1 — anyone can inspect.
- `set` writes a new row with `created_by='operator:<fingerprint>'` and
  flips the previous active row's `active=false`. The audit trail is
  identical to `v2-Ⅹ-RescueCLI` for runner_claims (`runner_audit_events`
  row per write).

### 6.3 Phase 2 — Read consumer flip (`v2-Ⅹ-5d` + 5e land)

- `v2-Ⅹ-5d` adds the §4 `enforce_max_tier` + `enforce_health` gates
  before `acquire_claim()` in `auto-runner-jira.py` /
  `auto-runner-multi.py` / `auto-runner-codex.py`. Pre-pickup quota
  enforcement consults `cost_mode`.
- `v2-Ⅹ-5e` adds the failover deconfliction matrix using
  `known_failure_classes`.
- After this phase: registry rows are authoritative for `(provider, model)`
  capabilities. `capability:enable=*` labels still apply (they go
  through `apply_label_overrides()` last, per §4 rule 2), but they are
  no longer the **primary** declaration site for new policy. Filing
  scripts (`scripts/file_audit_29_decompose.py`, etc.) update to write
  registry rows instead of labels going forward; existing labels on
  in-flight tickets keep working.

### 6.4 Phase 3 — Label-override deprecation (one sprint after Phase 2)

- `capability_matrix.apply_label_overrides()` logs a structured WARN
  on every label match (`capability_label_override_used` event with
  `(ticket_key, capability, source='label-override')` payload), giving
  operators a count of remaining usages.
- A single-line sweep of open OP project tickets via
  `scripts/jira-label-migrate.py` (existing, OP-695 era) rewrites any
  remaining `capability:enable=*` labels into registry rows for
  `(provider=*, model=*)` if the ticket carries a `class:*` label
  identifying the agent. Labels that don't carry a class are surfaced
  to operator-window for manual disposition.
- Once warn-count hits zero for two weeks, `apply_label_overrides()` is
  deleted. The `capability:enable=*` / `capability:disable=*` labels
  enter the `deprecated_labels` set in
  `docs/sop/jira-label-schema.yaml`; `scripts/file_jira_ticket.py`
  refuses to write them.

### 6.5 Migration invariants

The migration MUST preserve these invariants at every phase boundary:

1. **No silent capability widening.** A label that today grants
   `gerrit_push` MUST continue to grant `gerrit_push` until the label
   itself is removed in Phase 3. Tests in `v2-Ⅹ-5bc` assert this
   ticket-by-ticket.
2. **No silent capability narrowing during dual-source.** While Phase 0-2
   are active, the resolved capability set for a pickup is the **union**
   of (registry-derived ∩ matrix-base) and (label-derived). Only Phase 3
   drops the label half. This protects against a registry-bootstrap
   miss accidentally locking a ticket out mid-sprint.
3. **No registry write outside the API.** Direct SQL INSERTs on
   `capability_profile` are an architectural regression. Bootstrap
   script + RescueCLI + future failure-class learner are the only
   writers.
4. **Audit trail is durable.** Every registry row carries `created_by`;
   every change to `active` writes a `runner_audit_events` row with
   the prior `profile_id` for forensic recovery. Soft-delete only;
   never DELETE FROM `capability_profile` outside an alembic schema
   migration.

## 7. Backwards-compat adapter design

The `apply_label_overrides()` function in
`backend/agents/capability_matrix.py` is the migration's compat seam.
This section is the interface contract `v2-Ⅹ-5bc` implements; the
function signature does not change in 5bc, only the call site adds the
registry overlay.

```python
# backend/agents/capability_registry.py — new module (v2-Ⅹ-5bc implements)

@dataclass(frozen=True)
class CapabilityProfile:
    profile_id: str
    provider: str
    model: str
    tools: frozenset[str]
    max_tier: str                           # 'S' | 'M' | 'L' | 'X'
    cost_mode: str                          # 'subscription' | 'metered' | 'local'
    health_state: str                       # 'healthy' | 'degraded' | 'down' | 'unknown'
    health_checked_at: datetime | None
    known_failure_classes: frozenset[str]
    active: bool
    notes: str | None

class CapabilityRegistryDenied(PermissionError):
    """Pickup denied by the registry overlay (max_tier / health / missing-profile)."""
    def __init__(self, reason: str, *, provider: str, model: str, detail: str) -> None:
        ...

class CapabilityRegistryMissingProfile(LookupError):
    """No active profile exists for (provider, model). Caller falls back to read_only_default."""

def lookup_active(provider: str, model: str) -> CapabilityProfile:
    """Return the single active profile for (provider, model), or raise
    CapabilityRegistryMissingProfile. Pure DB read; no side effects.
    """

def resolve(
    *,
    ticket_type: str,
    area: str,                              # single area; multi-area callers use resolve_for_areas
    tier: str,
    labels: Iterable[str],
    provider: str,
    model: str,
    matrix: CapabilityMatrix | None = None, # injected for testability; defaults to load_capability_matrix()
) -> frozenset[str]:
    """Layered resolve per §4. Order:
       1. matrix base (§4 rule 1: registry can only narrow)
       2. ∩ profile.tools
       3. apply_label_overrides() (§4 rule 2; deprecation path)
       4. enforce_max_tier (§4 rule 3; raises CapabilityRegistryDenied)
       5. enforce_health (§4 rule 4; raises on 'down', warns on 'degraded')
       6. on missing profile: log + fall back to matrix.read_only_default
          + apply_label_overrides on that (§4 rule 5)
    """

def resolve_for_areas(
    *,
    ticket_type: str,
    areas: Iterable[str],
    tier: str,
    labels: Iterable[str],
    provider: str,
    model: str,
    matrix: CapabilityMatrix | None = None,
) -> frozenset[str]:
    """Multi-area variant. Reuses CapabilityMatrix.resolve_for_areas() for
    the base, then applies the same overlay. Union semantics on the matrix
    side (multi-area widens), intersection semantics on the registry side
    (registry still narrows).
    """
```

### 7.1 Why a new module, not a method on `CapabilityMatrix`

Three reasons:

1. **Concern separation.** `capability_matrix.py` knows the
   `(type × area × tier)` axis and operator labels. It deliberately
   does NOT know about provider/model. Co-locating the registry overlay
   on the matrix would back-couple that knowledge.
2. **Test isolation.** The matrix has 30+ existing unit tests in
   `backend/tests/test_capability_matrix.py`. Layering registry calls
   into the matrix would force every existing test to mock the
   registry. A new module lets the registry tests live alone and the
   matrix tests stay pure.
3. **Strangler clarity.** When the deprecation completes (§6.4) and
   `apply_label_overrides()` is deleted, the matrix module shrinks to
   pure (type × area × tier) lookup. The registry module owns the
   provider × model axis indefinitely.

### 7.2 Call-site migration in `auto-runner-jira.py`

Today: `capability_matrix.resolve_for_areas(ticket_type, areas, tier,
labels=labels)` — single call.

After `v2-Ⅹ-5bc`:
`capability_registry.resolve_for_areas(ticket_type=…, areas=…, tier=…,
labels=…, provider=runner.provider, model=runner.model)` — same return
type (`frozenset[str]`), same semantics for callers, with the overlay
applied internally. The runner injects its own `(provider, model)` from
the same `class:*` parsing path it already uses for assignee.

A `CapabilityRegistryDenied` from the new resolver is treated identically
to the existing `CapabilityNotPermitted`: comment-tag, transition back
to TODO with reason, exit cleanly.

## 8. Defense contract (D1+D2) for the registry itself

Per spec §4 Q7 / G.A-v2 self-audit, every v2 ticket carries a
`defense_contract` block. The registry only covers D1+D2 because the
substrate (D3-D5 — shutdown, recovery, rescue) belongs to the
`capability_profile` table's host (the runner-coordination Postgres,
already covered by `runner-substrate-contract.md` §8).

```yaml
defense_contract:
  error_detection:
    signal: prometheus_metric
    channel: capability_registry_resolve_total{outcome="ok|denied|missing|degraded"}, capability_registry_active_profiles{provider, health_state}
    detection_latency_p99: 30s
    observability_test: backend/tests/test_capability_registry_metrics.py    # v2-Ⅹ-5bc implements
    alert_rule_id: CapabilityRegistryAllProfilesDown, CapabilityRegistryMissingProfileRate, CapabilityRegistryDeniedRate
                   # CapabilityRegistryAllProfilesDown: every (provider, model) row in down state for > 10 min
                   # CapabilityRegistryMissingProfileRate: > 5% of pickups falling back to read_only_default in 1 h window
                   # CapabilityRegistryDeniedRate: > 20% of pickups CapabilityRegistryDenied in 1 h window — signals a
                   #   misconfigured registry row blocking real work

  exception_handling:
    enumerated_states: [ok, denied:max-tier, denied:health-down, denied:missing-profile, missing-profile-fell-back, db-unreachable]
    remediation_hint_contract: >
      every CapabilityRegistryDenied carries (provider, model, profile_id, reason);
      operator can pass profile_id straight to `omnisight runner-rescue capability dump <profile_id>`.
      CapabilityRegistryMissingProfile carries (provider, model) so operator knows what
      `omnisight runner-rescue capability set` invocation will create the missing row.
    user_facing: false               # operator-facing only
    fail_loudly: true                # CapabilityRegistryDBError raised (not swallowed) when the
                                     # capability_profile table is unreachable; runner falls back
                                     # to OP-855 matrix-only resolve with a loud WARN log
                                     # (caller treats unreachable registry as "every profile is in unknown state")
```

D3-D5 are inherited from `runner-substrate-contract.md` §8 because the
table lives in the same Postgres instance and shares
`acquire_claim` / `release_claim` shutdown / drain / rescue paths.

## 9. Open questions / operator sign-off needed

These remain pending operator decision before `v2-Ⅹ-5bc` lands. None
block this spec.

1. **`max_tier=X` semantics.** §3 declares `X` as "operator-only, never
   pick up". But the OP-855 matrix already excludes tier:X via the
   pickup JQL (per `feedback_human_only_tickets_tier_x.md`). Confirm:
   `max_tier=X` on a registry row is a redundant belt-and-braces declaration,
   not a new gate. Default assumption: redundant, kept for completeness
   so an `X`-capable provider (e.g. a future supervised mode) can be
   declared in the same shape.
2. **Per-model bootstrap when adapter declares multiple models.**
   `claude-opus-4-7` AND `claude-sonnet-4-6` both ship through the same
   `anthropic-subscription` adapter. Should the bootstrap insert one
   row per model, or one row per (provider, model) pair where model
   defaults to the adapter's "primary" model? Default assumption: one
   row per model declared in `adapter.MODELS`; the runner injects the
   actual model id at pickup time from `class:*`-derived metadata.
3. **`known_failure_classes` write path.** §3 declares the field as
   the input to `v2-Ⅹ-5e` deconfliction. Who writes it? Three options:
   (a) `v2-Ⅹ-5bc` ships it empty; operator hand-curates via RescueCLI;
   (b) a future failure-class learner reads `runner_incidents` rolling
   window and writes it auto; (c) every `release_claim(reason='*-fail')`
   appends the corresponding FailureClass to the row's set, with a
   decay sweep removing entries that haven't recurred in N days. Default
   assumption for this spec: (a) at 5bc; (c) is the long-term shape;
   (b) is out of v2 scope. Operator confirm at `v2-Ⅹ-5e`.
4. **Cost-mode `local` semantics.** §2 defines `local` as "no upstream
   quota — e.g. ollama / local-LLM 4th-runner experiment". Confirm
   that `v2-Ⅹ-5d` quota enforcement skips `local` profiles entirely
   (no `provider_quota_tracker` consultation), or whether `local`
   should still be subject to a cost-side observability budget (e.g. GPU
   minutes) tracked separately. Default assumption: skip entirely;
   GPU-side observability is covered by the per-host monitoring stack,
   not by the runner's quota tracker.
5. **Profile ID format.** §3.1 picks ULID. Codex Package 2 picked
   UUID-v7 for `runner_claims.lease_id`. They serve the same monotonic
   sortable-by-time purpose. Confirm: align on ULID across both tables,
   or accept the inconsistency? Default assumption: align on ULID for
   `capability_profile.profile_id`; the cost of changing
   `runner_claims.lease_id` (already migrated) is higher than the
   benefit. Document the divergence in `runner-substrate-contract.md`
   §5 if accepted.

## 10. References

- Sprint Atlas spec: `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑩ row 11 (this ticket), rows 12-14 (consumers)
- Substrate contract (companion): `docs/sop/runner-substrate-contract.md` (OP-1104 / `v2-Ⅹ-1a`)
- Existing matrix mechanism (overlaid by this spec): `backend/agents/capability_matrix.py` + `config/capability_matrix.yaml` (OP-855)
- Existing label schema (capability namespace deprecated by Phase 3): `docs/sop/jira-label-schema.yaml` `namespaces.capability` block
- Provider adapter base + registry: `backend/agents/provider_orchestrator.py` (`SUBSCRIPTION_VENDOR_REGISTRY`, `ProviderAdapter`, `register_adapter`, `register_coming_provider`)
- Provider adapters round-tripped in §5: `backend/agents/provider_adapters/{anthropic,openai,gemini,xai,grok}_subscription.py`
- Failure-class taxonomy (read by `known_failure_classes`): `backend/agents/failure_class.py` (OP-854 C2)
- Quota tracker (read by `cost_mode`): `backend/agents/provider_quota_tracker.py` (OP-15)
- Trust-tier hashtag (related but distinct policy axis, not absorbed by this spec — see §3.5 of `runner-substrate-contract.md`): `backend/agents/trust_scoring.py` (OP-735 / OP-756)
- ADR antecedents: ADR-0033 (L1/L2/L3 authority gates — RescueCLI L2 requirement), ADR-0034 (Override Review Lifecycle — registry writes audit-trailed), ADR-0035 (runner FSM + error handling)
