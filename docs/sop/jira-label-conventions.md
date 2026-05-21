# JIRA Label Conventions — OP project

**Status**: Living reference. Read it before you (a) hand-write `labels:` on a
ticket, (b) add a new namespace to `scripts/file_jira_ticket.py` /
`scripts/file_audit_29_tickets.py`, or (c) add a label to the canonical
registry in `scripts/jira-label-migrate.py`.

**Origin**: OP-1013 (label cleanup, 公開済み) shipped
`scripts/jira-label-migrate.py` with the canonical registry living *in code*
(`CANONICAL_PREFIXES` / `CANONICAL_PREFIX_VALUES` / `CANONICAL_BARE`). This
doc — **OP-1041** (AUDIT-29 housekeeping) — lifts that registry into a
documented, machine-parseable form so a filing-time lint hook can validate it.

**Companion file**: [`jira-label-schema.yaml`](jira-label-schema.yaml) is the
machine-readable version of *exactly this document* — same namespaces, same
values, same forbidden-combinations. The lint hook
(OP-LABEL-LINT-HOOK / OP-1042) loads the YAML;
`scripts/jira-label-migrate.py:is_canonical_label()` is the in-code mirror
(kept in sync by `tests/test_jira_label_schema.py` once OP-1042 lands). If the
YAML and this doc ever disagree, **the YAML wins** and this doc is the bug.

**Related**:

* [`jira-ticket-conventions.md`](jira-ticket-conventions.md) — §2 (required
  fields + labels), §3 (tier scale), §4 (Fix Version vs origin-label axes),
  §5 (Component + Area prompt injection), §11 (discovered-dependency
  protocol), §16 (scheduler / runner pickup JQL), §17 (META Component). The
  *ticket structure* SOP; this doc is the *label vocabulary* it references.
* `scripts/jira-label-migrate.py` — the OP-1013 migration tool + the in-code
  registry this doc supersedes as the human-facing source.
* `backend/agents/capability_matrix.py` — consumes `capability:enable=` /
  `capability:disable=` labels (the operator escape hatch on the OP-855
  matrix).
* `docs/sop/runner-pickup-mutex.md` — the `claim:{instance}:{token}` fencing
  label (AUDIT-24 / OP-977).
* `docs/sop/architecture-anti-patterns.md` — the `type:meta` mis-routing
  failure (case study below) is in the same family as #13 (dead inventory)
  and the shipped-but-not-deployed pattern.

---

## §1 How a label is shaped

Every OP label is one of two forms:

1. **Prefixed** — `<namespace>:<value>`. The namespace selects the
   *semantics*; the value is either free-form or drawn from a fixed enum.
2. **Bare** — a single token with no colon, drawn from a small allowlist
   (`migrated-from-todo`, `rc-blocker`, …). Plus a couple of legitimate
   marker labels that *do* contain a colon but are not severity/portfolio
   labels (`priority:meta`).

A label is **canonical** iff: it is an allow-listed bare label, **or** it is
`<namespace>:<value>` where `<namespace>` is a known namespace below and (if
the namespace is enumerated) `<value>` is one of its allowed values, **and**
it is not on the retired/legacy list (§7). Retired and legacy labels stay
syntactically `prefix:value` but are *not* canonical — the migration tool
rewrites or drops them.

Labels group into six families: **routing** (required on every ticket),
**capability** (operator escape hatch), **taxonomy** (why this exists / where
it sits), **state** (transient runner bookkeeping), **coord-override**
(execution-semantics changes — operator-gated), and **origin/marker** bare
labels.

---

## §2 Routing namespaces — required on every ticket

These three are mandatory (`jira-ticket-conventions.md` §2). Together they
decide *which runner picks the ticket up* and *what prompt context it sees*.

| Namespace | Form | Value | Multi? | Side-effects |
|---|---|---|---|---|
| `class` | `class:<agent_class>` | free (`class:api-anthropic`, `class:subscription-codex`, `class:subscription-claude`, …) | no | Runner JQL pickup filter (`jira-ticket-conventions.md` §16) — a runner only picks up tickets whose `class:` matches its configured agent class. **A sub-task split cannot change it** (§6) — class assignment is intentional. |
| `tier` | `tier:<S\|M\|L\|X>` | enum `{S, M, L, X}` | no | Authority gate (ADR-0005): `tier:X` must be split before pickup *or* human-assigned first; `tier:L` requires +1 human pre-merge. Drift detector (`jira-ticket-conventions.md` §14) computes `actual / target_upper` per tier. Hour bands: S = 1–3 h, M = 4–12 h, L = 1–3 d, X > 3 d. |
| `area` | `area:<domain>` | enum `{backend, frontend, devops, tests, db, docs, security, embedded, tooling, ci, gerrit}` | **yes** | Prompt injection (`jira-ticket-conventions.md` §5): the runner injects `Areas: …` plus a *negation list* (the full `area:*` taxonomy minus the declared areas) into the agent prompt — the agent is told to stay strictly inside its declared areas, and to halt + surrender the ticket (§11) if the work needs an out-of-area domain. |

`class` and `tier` are single-valued — a second one is a filing error.
`area` is multi-valued and *should* list every domain the work touches; an
under-declared `area` set produces a too-narrow prompt and a mid-ticket
discovered-dependency surrender.

---

## §3 Capability namespace — operator escape hatch on the OP-855 matrix

| Namespace | Form | Value | Multi? | Side-effects |
|---|---|---|---|---|
| `capability` | `capability:enable=<cap>` / `capability:disable=<cap>` | `<cap>` ∈ `{code_edit, run_tests, run_lint, gerrit_push, jira_update, mcp_search, memory_recall, run_migration, deploy_action, run_outcomes_grader}` | yes | `backend/agents/capability_matrix.apply_label_overrides()` mutates the resolved capability set at pickup time — `enable=` adds a capability the `(type × area × tier)` matrix would deny, `disable=` removes one it grants. **Coord-override**: this changes *what the runner is allowed to do*; per `jira-ticket-conventions.md` §9/§11 the AI may *propose* it but a human confirms before it takes effect. |

The capability vocabulary is owned by `config/capability_matrix.yaml`
(`capabilities:` block) and `backend/agents/capability_matrix.CAPABILITIES`;
this doc mirrors it for validation only. Adding a capability requires
updating both of those plus this list.

---

## §4 Taxonomy namespaces — why this exists / where it sits

Orthogonal to the JIRA *Fix Version* field (which answers "what does this
ship with?", `jira-ticket-conventions.md` §4). Taxonomy labels answer "why
does this exist?" and "which workstream owns it?". Multiple per ticket are
normal.

| Namespace | Form | Value | Multi? | Notes |
|---|---|---|---|---|
| `sprint` | `sprint:<id>` | free (`sprint:G`, `sprint:H`, …) | no | Sprint/cadence bucket. |
| `phase` | `phase:<id>` | free (`phase:audit-29f`, …) | no | Sub-phase of a multi-phase workstream. |
| `scope` | `scope:<id>` | free (`scope:pre-rc2`, `scope:label-hygiene`, `scope:adr`, `scope:audit`, …) | yes | Release-cut / workstream scope tag. |
| `meta` | `meta:<kind>` | free (`meta:audit-29`, `meta:retrospective`, `meta:wave-retrospective`, `meta:governance`) | yes | Sub-type marker — **only on `Component=META`** (`jira-ticket-conventions.md` §17). See the `type:meta` case-study (§8) — `meta:*` ≠ `type:meta`. |
| `adr` | `adr:<NNNN>` | 4-digit ADR id (`adr:0021`) | yes | Ticket implements/amends that ADR. |
| `epic` | `epic:<slug>` | free | no | Epic-link slug companion. |
| `audit` | `audit:<YYYY-MM-DD>` | ISO date | yes | Output of a deep-audit cycle (`jira-ticket-conventions.md` §4). |
| `incident` | `incident:<YYYY-MM-DD>-<slug>` | ISO date + slug | yes | Incident-response origin. |
| `tech-debt` | `tech-debt:<period>` | free (`tech-debt:q2-2026`) | yes | Quarterly burn-down batch. |
| `compliance` | `compliance:<framework-clause>` | free (`compliance:soc2-cc6.1`) | yes | Compliance-framework-driven. |
| `drift` | `drift:<over-run\|over-tier>` | enum `{over-run, over-tier}` | yes | **Auto-applied** by the drift detector (`jira-ticket-conventions.md` §14) — do not hand-add. |
| `wave` | `wave:<state>` | enum `{complete-pending-retro}` | no | **Auto-applied** by `jira_epic_lifecycle.py` on Epics (`jira-ticket-conventions.md` §10a) — do not hand-add. |
| `mutex` | `mutex:<resource-id>` | free | yes | Resource-conflict prevention (`jira-ticket-conventions.md` §8 type-c). **Coord-override** — changes other tickets' pickup eligibility; operator-gated (§9/§11). |
| `agent` | `agent:<auto>` | enum `{auto}` | no | Set by `scripts/file_jira_ticket.py` — marks runner-pickable tickets. |
| `type` | `type:<kind>` | free (`type:story`, `type:bug`, `type:cleanup`, `type:feature`, `type:meta`) | no | Issuetype hint set by `scripts/file_jira_ticket.py`. **`type:meta` has side-effects** — see §8. |
| `release` | `release:<ver>` | SemVer/rc string (`release:v0.5.0-rc2`) | no | **Transitional** — prefer the JIRA *fixVersion* field. The migration tool promotes `release:vX` → fixVersion and drops the label. |
| `label` | `label:<kind>` | free (`label:cleanup`) | yes | LABEL-CLEANUP-family follow-up marker. |
| `priority` | `priority:meta` (the **only** surviving `priority:*` *label*) | literal `meta` | no | Portfolio-META marker — *not* a severity. All `priority:<severity>` labels were promoted to the JIRA *Priority* field by OP-1013; all `priority:*-track` portfolio buckets were retired (§7). |

---

## §5 State namespaces — transient runner bookkeeping

Written and cleaned up by the runner; not hand-authored.

| Namespace | Form | Value | Owner | Notes |
|---|---|---|---|---|
| `claim` | `claim:{instance}:{token}` where `token = f"{epoch_us:016d}-{uuid8}"` (e.g. `claim:default:0001715518859123456-3af9c1d0`) | structured | `backend/agents/jira_dispatch.claim_ticket_atomic` (AUDIT-24 / OP-977) | Fencing-token mutex — lowest token wins. The legacy bare `claim:{instance}` (OP-838, SUPERSEDED) is treated as expired and swept on the next claim. See `docs/sop/runner-pickup-mutex.md`. A stale `claim:*` left after a revert is a bug (the OP-988 incident, §8). |
| `runner-blocked` | `runner-blocked:waiting-<KEY>` (e.g. `runner-blocked:waiting-OP-927`) | structured | `backend/agents/jira_dispatch` (`DEPENDENCY_WAITING_LABEL_PREFIX`) | Marks a ticket parked on an unmet dependency. The rc1 `runner-blocked:waiting-OP-92*` chain (OP-920..929) was retired by OP-1013 (§7). |
| `runner` | `runner:<flag>` | free (`runner:no-commits-expected`, `runner:atomic-claim`, …) | operator / runner | Behavioral flags. `runner:no-commits-expected` (`jira-ticket-conventions.md` §-runner-no-commits) tells the post-CLI check that rc=0 with *zero* commits is success, not a "runner produced nothing" failure — **coord-override-adjacent**: it changes the success contract, so operator-set. |

---

## §6 Bare / marker labels

| Label | Family | Use |
|---|---|---|
| `migrated-from-todo` | origin | Imported from the legacy `TODO.md` dump (OP-1014 triage backlog). |
| `migrated-from-todo-bulk` | origin | Bulk variant of the above. |
| `rc-blocker` | marker | Must land before the current rc cut. |
| `good-first-ticket` | marker | Low-context starter ticket. |
| `needs-operator-input` | marker | Parked pending an operator decision. |
| `priority:meta` | marker | Portfolio-META marker (listed in §4 too — it is the lone `priority:*` *label* that survives, and is **not** a severity). |

A bare token not on this list is a filing error: either it should be
prefixed (`area:foo`, not `foo`), or it's a per-ticket one-off the migration
tool's `--retire-oneoffs` pass will sweep.

---

## §7 Retired & legacy labels — rewrite or drop

`scripts/jira-label-migrate.py` enforces these; `is_canonical_label()`
returns `False` for every entry below; the lint hook rejects new tickets that
use them. Three actions: **rewrite** (legacy form → canonical label),
**promote** (label → first-class JIRA field), **retire** (drop, no
replacement).

| Pattern | Action | Becomes |
|---|---|---|
| `tier-s` / `tier-m` / `tier-l` / `tier-x` (and all-caps variants) | rewrite | `tier:S` / `tier:M` / `tier:L` / `tier:X` |
| `agent-class:<x>` | rewrite (prefix rename) | `class:<x>` |
| `complexity:trivial\|low\|small` | rewrite | `tier:S` |
| `complexity:medium\|moderate` | rewrite | `tier:M` |
| `complexity:high\|large` | rewrite | `tier:L` |
| `complexity:very-high\|xl\|extreme` | rewrite | `tier:X` |
| `priority:critical\|high\|medium\|normal\|low\|lowest\|trivial` | promote | JIRA *Priority* field (`Highest` / `High` / `Medium` / `Medium` / `Low` / `Lowest` / `Lowest`); label dropped |
| `release:v0.5.0-rc1` / `release:v0.5.0-rc2` / `release:v0.5.0` | promote | JIRA *fixVersion*; label dropped (the `release:` *namespace* itself stays valid-but-transitional — see §4) |
| `parent:OP-N` | promote | a `Blocks` issuelink, parent → child direction (Atlassian semantics: `inwardIssue` = blocker, `outwardIssue` = blocked — the OP-874 inversion trap, see `jira-ticket-conventions.md` §19); label dropped |
| `blocked-by:OP-N` / `blockedby:OP-N` | promote | a `Blocks` issuelink, `OP-N` → this; label dropped |
| `blocks:OP-N` | promote | a `Blocks` issuelink, this → `OP-N`; label dropped |
| `priority:hd-track`, `priority:mp-track`, `priority:rpg-track`, `priority:cl-track`, `priority:l4-track`, `priority:l5-track`, `priority:bp-track`, `priority:he-track`, `priority:fx-track`, `priority:fx2-track`, `priority:wp-track`, and any future `priority:<x>-track` | retire | — (portfolio buckets folded into the JIRA *Priority* field + Component) |
| `refined-by:claude-direct` | retire | — |
| `runner-blocked:waiting-OP-92[0-9]` (the rc1 chain) | retire | — (those dependencies resolved with rc1) |
| any one-off label used by ≤ `--oneoff-threshold` tickets and not on the canonical allowlist (only with `--retire-oneoffs`) | retire | — |

---

## §8 Forbidden combinations

The lint hook checks *combinations*, not just individual label legality.

### 8.1 `type:meta` on an implementation ticket — the AUDIT-29 case study

**Rule**: a ticket may carry `type:meta` **only if it actually has children**
(it is the `outwardIssue` of at least one `Blocks` issuelink). A single-phase
*implementation* ticket — however large — must use a non-meta issuetype hint
(`type:feature`, `type:story`, …). `type:meta` + (description references
implementation paths under `scripts/*` / `backend/*` / `deploy/*` /
`frontend/*` / `db/*`) + (no children) ⇒ **reject at filing time**.

**Why it bites** — observed twice on **OP-988** (AUDIT-29a sub-META), filed
`2026-05-13` with `type:meta` + `priority:meta` + `class:subscription-codex`:

1. `type:meta` (and the `priority:meta` companion) routes the ticket through
   the runner's **META roll-up path** — the path for tickets whose job is to
   *aggregate children*, not to do implementation work.
2. On that path the OP-855 capability-matrix lookup resolves to the YAML's
   `read_only_default` safe-default: **`[mcp_search, memory_recall]`** only —
   no `code_edit`, no `gerrit_push`, no `jira_update`. For a true roll-up
   that's *correct* — a roll-up should never push code.
3. But OP-988 had real implementation AC. The runner picked it up, tried to
   `gerrit_push`, got `CapabilityNotPermitted`, and **reverted the ticket to
   To Do** — twice — each time leaving a stale `claim:default:*` label behind
   (§5).

This is the same failure *family* as the OP-986 `ストーリー`-vs-`Story`
locale bug and the OP-874 `blockedBy`-direction inversion: a label/field with
*implicit downstream behavior* that is easy to set wrong. It belongs next to
`docs/sop/architecture-anti-patterns.md` #13 ("dead inventory") in spirit —
a structural mis-label that no amount of "be careful" prevents; only a
filing-time gate does.

**The fix** (reference: the AUDIT-29 batch decompose, Gerrit #500,
`2026-05-13`):

* **Decompose first** — break the implementation work into runner-pickable
  children; the children carry the routing labels and **no** `type:meta` /
  `priority:meta`; the parent keeps `type:meta` *because it now genuinely
  rolls up* (children `Blocks` it).
* **…or retype** — if it shouldn't be decomposed, drop `type:meta` /
  `priority:meta` and give it a normal issuetype hint + routing labels.
* Once OP-1042 (OP-LABEL-LINT-HOOK) loads this schema, the rule is
  machine-enforced in `scripts/file_jira_ticket.py` — you can't file the bad
  combo.

### 8.2 Other forbidden combinations

| # | Combination | Action | Rationale |
|---|---|---|---|
| 2 | label matches `^priority:[a-z0-9]+-track$` | reject | Portfolio `priority:*-track` buckets were retired by OP-1013 — use the JIRA *Priority* field (+ Component for the portfolio bucket). |
| 3 | label is `tier-s` / `tier-m` / `tier-l` / `tier-x` (or caps) | reject (rewrite on migrate) | Hyphen-form tiers predate the colon convention. |
| 4 | label starts with `agent-class:` | reject (rewrite on migrate) | Folded into `class:` by OP-855. |
| 5 | label starts with `complexity:` | reject (rewrite on migrate) | Folded into the `tier:` scale. |
| 6 | label is `parent:OP-N` / `blocked-by:OP-N` / `blockedby:OP-N` / `blocks:OP-N` | reject (promote to issuelink on migrate) | Dependency direction belongs in a `Blocks` issuelink, not a label — and getting the label direction "right" doesn't help because the *real* state lives in the link (`jira-ticket-conventions.md` §19). |
| 7 | label starts with `release:` **and** the ticket already has a JIRA *fixVersion* | warn | The `release:` label is transitional; if a fixVersion is set, the label is redundant — prefer the field. |
| 8 | two `class:*` labels, or two `tier:*` labels | reject | `class` and `tier` are single-valued (§2). |
| 9 | `meta:*` label present **and** `Component != META` | reject | `meta:*` sub-types only attach to META-component tickets (`jira-ticket-conventions.md` §17). (`meta:*` is not the same as `type:meta` — see §8.1.) |
| 10 | a coord-override label (`capability:enable=` / `capability:disable=` / `mutex:*` / `runner:no-commits-expected`) added by an AI agent without an operator-approval comment | reject | These change execution semantics / other tickets' pickup logic — AI may *propose*, human confirms (`jira-ticket-conventions.md` §9, §11). |

---

## §9 For tooling authors

* `scripts/file_jira_ticket.py` (OP-1042 scope) loads
  [`jira-label-schema.yaml`](jira-label-schema.yaml), validates the
  candidate `labels:` list against §1–§7, and runs the §8 combination
  checks before POSTing the ticket.
* `scripts/file_audit_29_tickets.py` should read the same YAML rather than
  hard-coding label strings (OP-1042 scope).
* `scripts/jira-label-migrate.py:is_canonical_label(label)` remains the
  in-code single-label check (CI / `--validate`); it must stay in sync with
  the YAML — `tests/test_jira_label_schema.py` (OP-1042) asserts no drift.
* CLI spot-check: `scripts/jira-label-migrate.py --validate <label>...`
  (exit 1 on any non-canonical label).
