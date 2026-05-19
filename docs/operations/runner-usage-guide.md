# Runner Usage Guide — operator + AI onboarding

> **Status**: First publish 2026-05-20, OP-1528 (Runner-UX-C)
> **Audience**: every human and AI operator who files, claims, or
> rescues a JIRA ticket destined for the auto-runner.
> **Read this first** before consulting the per-feedback memories or
> any of the deeper SOPs linked at the bottom — this doc is the
> conceptual layer above them.
> **Companion**: [`runner-ticket-filing-checklist.md`](../sop/runner-ticket-filing-checklist.md)
> (Runner-UX-B, OP-1527) — the mechanical filing recipe; don't duplicate.

---

## 1. What is the runner?

The runner is the autonomous loop that turns a JIRA ticket into a
Gerrit change without human typing:

```
JIRA (To Do, agent:auto + class:subscription-*)
   │
   ▼
auto-runner-jira.py  ──┐
                       ├─ ticks every ~90s on each runner host
backend/agents/*  ─────┘
   │
   ▼
fresh git clone (Sprint Boreas-C ephemeral cycle)
   │
   ▼
codex / claude CLI (per agent_class)
   │
   ▼
Gerrit refs/for/develop  →  AI +1 / human +2  →  develop  →  cut → main → deploy
```

Concretely:

* **`auto-runner-jira.py`** is the dispatch loop. One process per host
  per `OMNISIGHT_RUNNER_INSTANCE_ID`. It does JQL fetch, scoring,
  pre-pickup gates, claim acquisition, prompt build, CLI invocation,
  Gerrit push, and forward-transition.
* **`backend/agents/*`** is the durable runtime (capability matrix,
  jira_dispatch, scheduler, runner_stoploss, claim mutex, etc.).
  The CLI script is a thin wrapper — the load-bearing code lives in
  `backend/agents/` and is shared with future user-facing
  Orchestrator surfaces. See
  [`docs/operations/runner-strategy.md`](runner-strategy.md) for the
  three-branch framing (user product mirror / dev tool / internal
  agent runtime).
* **Ephemeral worktree (Sprint Boreas-C, OP-1136/1137/1138)**: every
  pickup runs from a fresh git clone at `$workspace`, not a long-lived
  worktree. The wrapper `scripts/runner-wrapper/run-ephemeral.sh`
  sets `OMNISIGHT_RUNNER_EPHEMERAL=1` and pins both
  `OMNISIGHT_{CODEX,CLAUDE}_WORKTREE` to the workspace path. This
  closed the OP-1126/OP-1124 overnight-drift class of bugs.
* **Bare-mirror cache (OP-1136 / Boreas-C1)**: each cycle clones from a
  local bare mirror, refreshed by `runner-mirror-fetch.service` timer,
  to keep clone time bounded and traffic off Gerrit.

If you want the deepest "why does this exist at all" framing, read
[`docs/operations/runner-strategy.md`](runner-strategy.md) §1 ("The
three branches the runner becomes"). The runner is **pre-paid design
work for the future user-facing Orchestrator** — every Phase reused.

---

## 2. WHO can use the runner — agent classes & subscription envelopes

The runner dispatches by `class:subscription-*` label on the ticket
**and** the `agent_class` env var on the runner process
(`OMNISIGHT_RUNNER_CLASS`, set per host via systemd drop-in). The two
must match or the ticket is invisible to that host.

| `class:subscription-*` | Driver | Worktree env override | Bot account family |
|---|---|---|---|
| `class:subscription-codex` | `codex exec --cd <wt> --yolo` (OpenAI Codex CLI) | `OMNISIGHT_CODEX_WORKTREE` | `codex-bot`, `codex-bot-<INSTANCE_ID>` |
| `class:subscription-claude` | `claude --dangerously-skip-permissions -p <prompt>` (Anthropic Claude Code CLI) | `OMNISIGHT_CLAUDE_WORKTREE` | `claude-bot`, `claude-bot-<INSTANCE_ID>` |
| `class:subscription-api-openai` | SDK path (`api-openai`) — wired but CLI returns rc=99 in `auto-runner-jira.py`; SDK runner runs separately. | — | — |

### What to send to codex vs claude

This is **AC-discipline-driven**, not capability-driven:

* **Codex** follows AC literally and shines on well-scoped, contract-clean
  tickets — extend-existing, mechanical refactors, focused tests, doc
  tickets where structure is spec'd. It does **not** grep the repo to
  detect scope-overlap on its own ([[feedback_filing_existing_impl_check]]).
  Route here when AC enumerates files, symbols to preserve, and
  pre-flight checks. See [[project_codex_capability_envelope]] (4
  stress tests on 2026-05-16) for the full routing rubric.
* **Claude** is the right pick for design-space-open work where there
  is no implementation anchor — pure architectural / spec / contract
  tickets — and for cross-cutting refactors where AC cannot fully
  enumerate the invariants. Also use claude for anything labeled
  `class_override_required: yes` (auth/security).

### Subscription class vs labels you also need

`class:subscription-*` is necessary but not sufficient. The pickup JQL
(`docs/sop/jira-ticket-conventions.md` §16) additionally requires:

* `agent:auto` (operator-set "runner is allowed"); without it the
  ticket is human-only.
* `assignee is EMPTY` (the runner self-assigns at claim time; if you
  manually assign, the ticket vanishes from pickup — that's how
  `[HOLD]` is implemented).
* `status = "To Do"`.
* `area:<one of 9>` — `backend`, `db`, `devops`, `docs`, `embedded`,
  `frontend`, `security`, `tests`, `tooling` ([[feedback_runner_recognized_areas]]).
  Unknown area → silent pre-pickup rc=1 infinite loop. **No JIRA
  signal**; only visible in runner logs.
* Not `tier:X` (X = HUMAN ONLY; runner JQL excludes; see [[feedback_human_only_tickets_tier_x]]).
* `runner-stoploss:circuit-tripped-*` absent (or pickup is refused —
  see §6 below).

---

## 3. WHEN to file a runner ticket vs push manually

The runner's sweet spot:

* **Single-file or tightly-scoped multi-file** scope.
* **AC is clear and self-checkable**: each AC item names a test, a
  file:line range, or a deliverable that can be grep'd.
* **Boundary is clean**: `area:*` actually spans every file the AC
  touches (see [[feedback_ticket_area_must_span_all_4_ac_areas]];
  Code + Deploy + Integration + Exercised AC each have an area).
* **No live state-machine surgery required** mid-PR (no concurrent
  schema migration on the row you're editing, no rebases pending on a
  shared branch, no breaking-change negotiation needed).
* **Scope is not already shipped on develop** ([[feedback_filing_existing_impl_check]];
  [[feedback_runner_already_shipped_loop]]).

Operator must do it manually when:

* Multi-file refactor where the diff has to be reviewed as a whole
  with human judgement (no AC can enumerate the invariants).
* Breaking change to a public contract that needs human review of the
  migration plan.
* Infrastructure / cluster / credential changes (anything visible to
  others or hard to roll back).
* Anything in `tier:X` (HUMAN ONLY) or with `class_override_required:yes`.
* Anything where the right action is "wait" or "negotiate" rather than
  "produce a patch".

The 4-AC discipline ([[feedback_4_ac_discipline]]) is the single
clearest filter: **if you cannot write Code / Deploy / Integration /
Exercised AC items each with a concrete deliverable, the ticket isn't
runner-shaped yet.**

---

## 4. HOW to file a ticket

This guide does **not** repeat filing mechanics. The canonical
checklist is:

* **[`docs/sop/runner-ticket-filing-checklist.md`](../sop/runner-ticket-filing-checklist.md)**
  (Runner-UX-B, OP-1527) — the pre-flight checklist: required labels,
  area↔path table, AC skeleton, pre-flight greps, common typos.
* **[`docs/sop/jira-ticket-creation-checklist.md`](../sop/jira-ticket-creation-checklist.md)**
  — the older, more general checklist; the Runner-UX-B doc supersedes
  it for runner-shaped tickets.
* **[`docs/sop/jira-ticket-conventions.md`](../sop/jira-ticket-conventions.md)**
  — the source of truth for §11 (discovered-dependency surrender),
  §14 (lessons-learned acceptance criteria), §16 (pickup JQL).
* **[`docs/sop/jira-label-conventions.md`](../sop/jira-label-conventions.md)**
  / [`jira-label-schema.yaml`](../sop/jira-label-schema.yaml) — full
  label allowlist.

If you skip those, you will recreate one of the common pitfalls in §8.

---

## 5. HOW to interpret runner comments — taxonomy

Every comment the runner posts is prefixed `[runner-<tag>]` (sometimes
with a `:<subtag>` qualifier) so JIRA's comment stream stays grep-able
and the operator-rescue scripts can pattern-match on prefix. The
source of truth is `auto-runner-jira.py` + `backend/agents/runner_stoploss.py`;
the table below is exhaustively cross-referenced against those files
as of 2026-05-20.

### 5.1 Pre-pickup gates (runner has not yet started CLI work)

| Prefix | Meaning | Operator action |
|---|---|---|
| `[runner-capability-pre-pickup-blocked]` (`auto-runner-jira.py:109`) | Capability matrix refused this `(issuetype, area, tier)` combination before pickup. Pickup loop self-skips. | Check `area:*` + `tier:*` labels; the matrix entry may need extending or the ticket needs different labels. See `docs/sop/capability-matrix-fallback-policy.md`. |
| `[runner-file-mutex]` (`auto-runner-jira.py:313`) | Another runner holds a `mutex:<path>` lease on a file this ticket touches; pickup skipped to avoid two CLIs editing the same file. | Usually transient — will retry next tick once the other runner finishes. If `mutex:<path>` is stale (orphan), use `omnisight-runner-rescue` to release. |
| `[runner-dependency-blocked]` (`auto-runner-jira.py:318`) | A `Blocks`-inward dependency is not yet in a runner-pickable state. | Wait for the upstream ticket, or unblock manually (e.g., resolve the blocker, delete the link if wrong). See [[feedback_runner_pickup_blocker_unenforced]] and [[feedback_dependency_refresh_must_delete_old_links]] — JIRA has no cycle detection; deadlocks are possible. |
| `[runner-dependency-unblocked]` (`auto-runner-jira.py:324`) | Companion to the above — runner posts exactly one when the blocker clears. Informational. | None. |
| `[runner-bridge-down]` (`auto-runner-jira.py:508`) | Bridge daemon heartbeat is silent; pickup short-circuited so we don't ship without telemetry. | Check `/var/run/omnisight-bridge` and the bridge logs. Known reboot hazard: see [[project_bridge_heartbeat_tmpfs_trap]] (tmpfs wipe; Path B env override in `~/.local/state/omnisight-bridge/` is durable). |
| `[runner-capability-matrix-invalid]` (`auto-runner-jira.py:2529`) | The matrix YAML failed to parse at pickup time — runner reverts the ticket to To Do; whole loop refuses to pick anything until fixed. | Operator: fix the matrix YAML (recent edit?), restart runner. |
| `[runner-bad-area-label]` (`auto-runner-jira.py:2546`) | Ticket carries an `area:*` label outside the 9-value whitelist. | Fix the label on the ticket; see [[feedback_runner_recognized_areas]]. Without this comment, the older path was a silent rc=1 loop — now there's at least a JIRA breadcrumb. |
| `[runner-cwd-unsafe]` (`auto-runner-jira.py:2579`) | The worktree path failed the safety check (e.g., main repo writable). Pickup refused. | Operator: check `OMNISIGHT_{CODEX,CLAUDE}_WORKTREE` settings — the ephemeral wrapper should be setting these. |
| `[runner-workspace-tampered]` (`auto-runner-jira.py:2692`) | The Claude CLI deterministically deletes `.runner-cwd-sentinel` on certain tickets in some worktrees → revert loop. | Switch the ticket to codex (toggle `class:subscription-*`) — codex CLI doesn't trip this detector. Real fix is a `.gitignore` for the sentinel. See [[feedback_claude_cli_workspace_tampered]]. |

### 5.2 Claim mutex (pre-CLI)

| Prefix | Meaning | Operator action |
|---|---|---|
| `[runner-mutex-lost]` (`auto-runner-jira.py:2607`) | Another runner won the same-ticket fencing-token race. This runner exits clean; the other proceeds. | None — designed behavior. If you see this on every tick for a ticket no other runner is working, see §7 + [[feedback_stale_claim_labels]] (stale claim leftover from a prior revert). |
| `[runner-mutex-api-error]` (`auto-runner-jira.py:2600`) | Transport / HTTP failure during claim sequence — runner skips pickup, retries next tick. | Usually transient (JIRA blip). Persistent failure → check JIRA status. |

### 5.3 Mid-flight (CLI is running or just exited)

| Prefix | Meaning | Operator action |
|---|---|---|
| `[runner-capability-blocked]` (`auto-runner-jira.py:734`) | CLI tried to invoke a capability not in the matrix-permitted set for this `(issuetype, area, tier)`. Reverts to To Do. | Either: (a) add the missing capability label / matrix entry; (b) trim the ticket's scope so it stays inside permitted capabilities. See [[feedback_capability_safe_default_leak]] (OP-1124). |
| `[runner-presync-fail]` (`auto-runner-jira.py:2424`) | Could not prepare the worktree (fetch / checkout failure pre-CLI). Reverts to To Do. | Operator: check git connectivity, mirror health (`runner-mirror-fetch.service`). |
| `[runner-h12-self-heal]` (`auto-runner-jira.py:2394`) | Runner detected a merged Gerrit change on this branch and self-healed forward. Informational. | None. |
| `[runner-yielded-to-human-authority]` (`auto-runner-jira.py:2039`) | A human committed on the branch while the CLI was running. Runner stops. | Human owns this ticket now; runner won't pick it up again unless reverted to clean state. |
| `[runner-toctou:<phase>:<action>]` (`auto-runner-jira.py:2057`) | Time-of-check-to-time-of-use race: the live ticket state changed between the runner's pre-flight and now (e.g., operator unset assignee, status moved). Runner aborts and reverts. | Usually self-recovers next tick. Operator: **never** unset assignee on a ticket a runner may be working on — see [[feedback_never_unset_assignee_on_running_ticket]] (2026-05-18 TOCTOU storm destroyed 5 tickets' work). |
| `[runner-no-commits-from-cli]` (`auto-runner-jira.py:2946`) | CLI exited cleanly but produced 0 commits between base and HEAD. Reverts to To Do. | Most common revert. If repeated: check whether the work is already shipped (§7), whether AC is ambiguous, or whether the CLI hit a sandbox boundary. |
| `[runner-dirty-worktree]` (`auto-runner-jira.py:2970`) | CLI exited with uncommitted changes left in the worktree. Reverts to To Do. | Indicates the CLI applied edits but did not commit them. Often AC ambiguity. |
| `[runner-detected-shipped]` (`auto-runner-jira.py:1869`, OP-1401) | CLI produced 0 commits **and** a recent bot comment matched "already-shipped" phrasing → forward-archive rather than revert. Sidesteps the revert→stoploss loop. | None — designed behavior. If it fires on a ticket whose impl is **not** actually shipped, check phrase-match heuristics. See [[feedback_runner_already_shipped_loop]]. |
| `[runner-discovered-dependency]` (per `docs/sop/jira-ticket-conventions.md` §11) | CLI itself surrendered the ticket because the work requires touching an area outside its `area:*` boundary. CLI moves ticket back to To Do with a note. | Operator: either re-file with broader area, split the ticket along area lines, or accept the surrender. The runner does **not** double-revert in this case (`auto-runner-jira.py:2858`). |
| `[runner-ops-only-unexpected-commits]` (`auto-runner-jira.py:1675`) | Ticket carries `runner:no-commits-expected` (ops-only / runbook ticket) but CLI **did** commit. Suspicious. | Inspect commits — operator must decide whether to ship them or revert. |
| `[runner-ops-only-permission-refused]` (`auto-runner-jira.py:1693`) | Ops-only forward-transition refused by JIRA. | Likely workflow-permission gap; check ticket type + transition allowlist. |
| `[runner-ops-only-fail]` (`auto-runner-jira.py:1716`) | Other failure on the ops-only forward path. | Read the traceback in the comment body. |
| `[memory-writeback]` (`auto-runner-jira.py:1566`) | OP-906 fan-write to Memory Tool / Cognee / incidents store completed; lesson ID surfaced. | None — informational. |
| `[ac-evidence-strict]` (`auto-runner-jira.py:2831`) | AC-evidence strict mode tripped: revert-to-TODO failed during AC-evidence enforcement. | Read context — usually a JIRA transition error. |

### 5.4 Push & post-push

| Prefix | Meaning | Operator action |
|---|---|---|
| `[runner-gerrit-setup-fail]` (`auto-runner-jira.py:3008`) | Could not prepare the Gerrit push (rebase / remote / auth). Reverts to To Do. | Common when the runner pushed a change earlier that hasn't merged yet — see [[feedback_runner_push_setup_fail_leaves_stuck]] (OP-1400). Check that `git rebase --keep-empty` succeeds in the worktree. |
| `[runner-push-fail:transient]` (`auto-runner-jira.py:1987`) | Push failed; classifier says retryable. Auto-recovers. | None unless it persists. |
| `[runner-push-fail:unknown]` (`auto-runner-jira.py:1997`) | Push failed; classifier can't tell. Manual review needed. | Operator: read the captured `detail` (truncated to 500 chars) in the comment. |
| `[runner-gerrit-push-recovered]` (`auto-runner-jira.py:3039`) | Push retry succeeded after a transient failure. Informational. | None. |
| `[runner-pushed-to-gerrit]` (referenced `auto-runner-jira.py:1230`, posted via `jira_dispatch.post_runner_pushed_comment`) | **Success.** Ticket transitioned to Under Review; awaiting AI +1 + human +2 (or merger-bot +2 for resolve patches, per CLAUDE.md L1). | None — the runner pushed your change; review is now human. |
| `[runner-transition-skip]` (`auto-runner-jira.py:1429`) | Push already succeeded but the JIRA transition to Under Review failed (4xx etc.). Push wins — change is shippable. | Operator: cosmetic; manually move ticket to Under Review when convenient. |
| `[runner-pre-review-self-fix-warning]` (`auto-runner-jira.py:1516`) | Pre-review self-fix step produced a non-fatal warning (e.g., linter applied but with a notice). | Usually safe to ignore; verify the warning text doesn't hide a regression. |

### 5.5 Stoploss circuit (per-ticket revert limit)

| Prefix | Meaning | Operator action |
|---|---|---|
| `[runner-stoploss] Circuit tripped: N §11-reverts in the last W minutes` (`backend/agents/runner_stoploss.py:195`) | Same ticket has §11-reverted ≥`OMNISIGHT_STOPLOSS_THRESHOLD` (default 3) times within `OMNISIGHT_STOPLOSS_WINDOW_MIN` (default 15 min). Runner **refuses further pickups** until cleared. | **Investigate before re-arming.** Tripping repeats unless the root cause (capability gap, area mismatch, boundary trap, already-shipped scope, ...) is fixed. To clear: strip the `runner-stoploss:circuit-tripped-<ts>` label. The "53-ticket stoploss day" on 2026-05-17 was caused by [[feedback_runner_already_shipped_loop]]; the `[runner-detected-shipped]` path (5.3) was added to head off the same loop next time. |

### 5.6 The "Reverting to TODO" comment

The runner posts the `Reverting to TODO. Reason: ...` comment as part
of `jira_dispatch.transition_back_to_todo`. The **Reason** field
embeds the prefix above (e.g., `Reason: [runner-no-commits-from-cli]
CLI exited without committing.`). Always parse the prefix first —
that's the rescuable signal; the prose around it is for humans.

---

## 6. Failure mode triage

Frequency / impact / rescue, ordered by how often you'll meet them in
practice (2026-05 data):

### High frequency, mostly self-healing

| Mode | Frequency | Rescue |
|---|---|---|
| `[runner-mutex-lost]` | Multiple per hour across 5 instances | None — the other runner ships. |
| `[runner-file-mutex]` | Per hour | None — retries next tick. |
| `[runner-toctou:*]` | A few per day | None unless persistent. If persistent, **stop** unsetting assignee mid-run ([[feedback_never_unset_assignee_on_running_ticket]]). |
| `[runner-no-commits-from-cli]` (single revert) | Several per day | None — `transition_back_to_todo` reopens the ticket and re-pickup happens. Investigate only if the same ticket reverts ≥2× in 15 min (you're 1 revert from stoploss). |

### Medium frequency, operator-fixable

| Mode | Frequency | Rescue |
|---|---|---|
| `[runner-bad-area-label]` / silent area-label rc=1 loop | Every time someone files with an unrecognized area | Tail `logs/runner/{claude,codex}-bot-*.log`; fix the `area:*` label to one of 9 ([[feedback_runner_recognized_areas]]; [[feedback_filing_area_labels_must_match_deliverables]]). |
| `[runner-capability-pre-pickup-blocked]` / `[runner-capability-blocked]` mid-flight | Whenever matrix is wrong for the ticket | Extend the matrix or fix the ticket's `area:*`/`tier:*`/`capability:enable=*` labels. See [[feedback_capability_safe_default_leak]]. |
| `[runner-gerrit-setup-fail]` after empty-tree | Whenever push setup races a merged earlier change | Usually self-recovers once the merged change settles. If persistent, check ticket has no orphan `claim:*` ([[feedback_stale_claim_labels]]) and that the worktree fetch succeeded. |

### Low frequency, hard to spot

| Mode | Frequency | Rescue |
|---|---|---|
| Silent rc=1 loop with no JIRA comment | When `area:*` is unknown (`[runner-bad-area-label]` shipped to give a JIRA breadcrumb, but old loop may still hit on unusual labels) | Tail runner logs — JIRA shows nothing. |
| Stoploss circuit trip (§5.5) | A few per week | Investigate root cause **first**, then strip `runner-stoploss:circuit-tripped-*` to re-arm. Don't auto-re-arm. |
| `[runner-workspace-tampered]` (Claude `.runner-cwd-sentinel` deletion) | Specific tickets in specific worktrees | Switch to codex via `class:subscription-codex` ([[feedback_claude_cli_workspace_tampered]]). |
| Stuck `In Progress` with no `claim:*` and no Gerrit change | OP-1400 / push-setup-fail leaves silent zombie | Operator-rescue: strip stale state, return to To Do. See [[feedback_runner_push_setup_fail_leaves_stuck]]. |

---

## 7. Mutex + claim mechanism

The runner has **two independent mutexes**; don't confuse them:

### 7.1 Per-ticket claim mutex (`claim:{instance}:{epoch_us}-{uuid}`)

Source of truth: [`docs/sop/runner-pickup-mutex.md`](../sop/runner-pickup-mutex.md)
(AUDIT-24 / OP-977, supersedes OP-838).

* Per-ticket fencing-token label. Each claim attempt mints a unique
  `claim:{instance_id}:{epoch_us:016d}-{uuid8}`. **Lowest token
  wins** (microsecond timestamp sorts to "earliest claimer"; uuid
  suffix breaks µs ties).
* Two runners reading back the same set of labels agree on the same
  winner via `min(...)` — no winner-flip.
* Stale tokens (crashed runners) swept on the next claim's pre-GET
  once aged past `2× CLI timeout` (env
  `OMNISIGHT_RUNNER_STALE_CLAIM_MAX_AGE_S`, default `7200s`).
* **Operator invariant (OP-783)**: one bot account ↔ one
  `instance_id`. Violating it re-opens the OP-974 dual-claim race.
* Rollback flag: `OMNISIGHT_RUNNER_ATOMIC_CLAIM_LEGACY=1` reverts to
  OP-838's bare-label "mutex" (known-racey but cross-bot-safe).

**Known bug (OP-1524 partially fixed 2026-05-19, full fix tracked
in commit `ff017dcb`)**: the revert paths used to leave the runner's
own `claim:*` label on the ticket, wedging the next pickup behind a
phantom token. `auto-runner-jira.py:2940-2943` now strips the claim
label **before** posting the revert comment. If you see a To-Do
ticket with multiple stacked `claim:default:*` labels that the runner
keeps losing on, strip them manually
([[feedback_stale_claim_labels]]); fix is verified for the
no-commits-from-cli path but other revert paths may still have
holes — open a follow-up if you find one.

See [[project_runner_pickup_mutex]] for the cross-link to the OP-974
incident that originally motivated the rewrite.

### 7.2 File-overlap mutex (`mutex:<path>`)

Source: `backend/agents/file_mutex_check.py` (OP-731/800/687). A
separate, additive gate — keeps two runners from editing
overlapping file sets in parallel. Surface in JIRA via
`[runner-file-mutex]` (§5.1). See [[project_auto_push_gaps]] for
the historical fix family.

### 7.3 Other claim-adjacent state

* `runner_claims` (Postgres alembic 0236, OP-1106). The substrate
  table the rescue CLI operates on. Read via
  `omnisight-runner-rescue dump`; write via `release` /
  `reset`. See [`docs/sop/runner-rescue-cli.md`](../sop/runner-rescue-cli.md).
* `runner-stoploss:revert-<ts>` and `runner-stoploss:circuit-tripped-<ts>`
  labels — per-ticket revert circuit (§5.5).
* `runner-blocked:waiting-*` — operator-set "blocked on something"
  marker; observability, not enforced by JQL ([[feedback_human_only_tickets_tier_x]];
  if you want JQL exclusion, file at `tier:X`).

---

## 8. Common pitfalls operators hit

A non-exhaustive list of failure-shapes that have repeated enough on
this project to deserve operator memory:

| Pitfall | Symptom | Fix |
|---|---|---|
| Unknown `area:*` label (e.g. `area:governance`) | Silent rc=1 loop; no JIRA signal; runner log shows `bad area label`. | Use one of 9: `backend`, `db`, `devops`, `docs`, `embedded`, `frontend`, `security`, `tests`, `tooling`. [[feedback_runner_recognized_areas]] / [[feedback_filing_area_labels_must_match_deliverables]]. |
| `area:*` doesn't cover all 4 ACs | Runner picks up, hits boundary on Deploy/Integration AC, §11-reverts to To Do; repeats → stoploss. | File multi-area or split by area. [[feedback_ticket_area_must_span_all_4_ac_areas]]. |
| `tier:S` on a HUMAN-ONLY ticket | Pickup-revert loop. | File at `tier:X`. [[feedback_human_only_tickets_tier_x]]. |
| `type:meta` with no children | Hits capability-matrix safe-default (`[mcp_search, memory_recall]` only) → no code-edit → §11. | Reserve `type:meta` for genuine roll-ups. [[feedback_type_meta_routing]]. |
| Filing scope that's already shipped on develop | CLI abstains (0 commits) → revert → re-pickup → stoploss after 3 reverts. | Pre-flight grep before filing ([[feedback_filing_existing_impl_check]]). On detection, the runner now archives forward via `[runner-detected-shipped]` ([[feedback_runner_already_shipped_loop]]). |
| Operator unsets assignee mid-run | TOCTOU storm: `[runner-toctou:*]` aborts every active CLI; work lost. | **Never** unset assignee on a running ticket; strip stale labels OK. [[feedback_never_unset_assignee_on_running_ticket]]. |
| Adding `Blocks` without removing the old ones | Cycle deadlock; all runners empty-cycle with `[runner-dependency-blocked]`. | Enumerate existing `Blocks` + DELETE wrong edges before POST. [[feedback_dependency_refresh_must_delete_old_links]]. |
| Reverted ticket stuck with stacked `claim:*` | Runner re-selects, loses mutex to phantom token every tick, exits clean. | Strip all `claim:*`; the OP-1524 fix covers the no-commits path. [[feedback_stale_claim_labels]]. |
| Empty-tree rebase wedge | Stuck `In Progress`, no `claim:*`, no Gerrit change. | Strip stale state; OP-1400 reduced incidence but residual. [[feedback_runner_push_setup_fail_leaves_stuck]]. |
| `class:subscription-claude` on a ticket that trips `.runner-cwd-sentinel` | `[runner-workspace-tampered]` revert loop on certain tickets in claude worktree. | Switch ticket to `class:subscription-codex`. [[feedback_claude_cli_workspace_tampered]]. |
| Tagging `[HOLD]` only on summary (not removing `agent:auto`) | Runner picks up anyway. | Remove `agent:auto` OR set `tier:X` (HUMAN ONLY); summary tags are observability. [[feedback_backlog_promotion_must_rewrite_description]]. |
| `release:approval-pending` / `runner-blocked:*` treated as gate | Same — these are observability, not JQL filters. | Use `tier:X` for hard exclusion. |
| Bulk filing without `pre-flight grep` | Each typo multiplies blast radius. | Sample 3 by hand; inconclusive auto-classification = NO-ACTION. [[feedback_slow_down_be_careful]]. |
| Bridge tmpfs wipe on reboot | All 5 runners silently abstain because heartbeat path is gone. | Path B env override (`~/.local/state/omnisight-bridge/`) is durable; verify it's in the drop-in. [[project_bridge_heartbeat_tmpfs_trap]]. |

---

## 9. Escalation path — when to stop trying via runner

In order of "least invasive first":

1. **Reverted once on `[runner-no-commits-from-cli]`** — leave it.
   The next tick will re-pickup. Most of these self-heal.
2. **Reverted twice in 15 min on the same prefix** — you're one
   revert from stoploss. **Investigate before letting it tick
   again.** Read the runner-bot AC verification comment (the one
   preceding the revert): does the CLI think the work is already
   done? Is the AC ambiguous? Is the boundary wrong?
3. **`[runner-stoploss] Circuit tripped`** — runner refuses pickup.
   Don't strip the label until you've identified the root cause.
   See §5.5; the 2026-05-17 53-ticket day is the textbook example
   of stripping without diagnosing → second trip → exhaustion.
4. **Stuck `In Progress` with no `claim:*` label and no Gerrit
   change** — operator-rescue ([[feedback_runner_push_setup_fail_leaves_stuck]];
   `omnisight-runner-rescue dump` to inspect substrate state).
5. **`[runner-workspace-tampered]` loop** — switch class to codex
   ([[feedback_claude_cli_workspace_tampered]]).
6. **Two reverts at different prefixes, root cause unclear** —
   manually push. Re-file as `tier:X` if humans need to own it. The
   runner is not the right tool when AC has hidden invariants the
   CLI can't see.
7. **Catastrophic case (TOCTOU storm, mutex storm, mass-revert)** —
   stop all runners; read [[feedback_rescue_race_check_inflight_work]]
   **before** mass-stripping labels (another runner may be
   mid-submit); use `omnisight-runner-rescue` to inspect, not to
   bulk-clear. Mass-clear is a last resort.

Rule of thumb: **the runner is a tool, not an obligation.** If two
reverts in a row don't explain themselves, the next action is human
investigation, not a third pickup.

---

## 10. References

### Memories (link by `[[name]]`)

* [[project_runner_pickup_mutex]] — AUDIT-24/OP-977 fencing-token rewrite.
* [[project_codex_capability_envelope]] — Codex routing rubric (2026-05-16 stress tests).
* [[project_runner_strategy]] — Three-branch strategic framing.
* [[project_runner_phase_priority]] — Phase 7 yes, Phase 6 deferred.
* [[project_runner_pivot_strategic_reframe]] — Runner-as-Product reframe (2026-05-14).
* [[project_auto_push_gaps]] — File-overlap mutex history.
* [[project_bridge_heartbeat_tmpfs_trap]] — Reboot wipe + Path B override.
* [[feedback_runner_recognized_areas]] — 9-value `area:*` whitelist.
* [[feedback_runner_pickup_blocker_unenforced]] — Blocks-inward not JQL-enforced.
* [[feedback_runner_push_setup_fail_leaves_stuck]] — OP-1400 silent zombie.
* [[feedback_runner_already_shipped_loop]] — OP-1401 detected-shipped fix.
* [[feedback_runner_authored_ac_acceptable]] — When the runner can fill its own 4-AC.
* [[feedback_stale_claim_labels]] — Stripping orphan `claim:*` (manual).
* [[feedback_claude_cli_workspace_tampered]] — `.runner-cwd-sentinel` deletion.
* [[feedback_never_unset_assignee_on_running_ticket]] — TOCTOU storm guardrail.
* [[feedback_rescue_race_check_inflight_work]] — Non-destructive rescue.
* [[feedback_4_ac_discipline]] — Every impl ticket needs 4 AC.
* [[feedback_ticket_area_must_span_all_4_ac_areas]] — Boundary alignment.
* [[feedback_type_meta_routing]] — `type:meta` safe-default trap.
* [[feedback_capability_safe_default_leak]] — Matrix-resolves but enabled-caps narrower.
* [[feedback_human_only_tickets_tier_x]] — Tier:X is the only JQL-enforced gate.
* [[feedback_dependency_refresh_must_delete_old_links]] — JIRA cycle deadlock.
* [[feedback_filing_existing_impl_check]] — Pre-filing grep.
* [[feedback_filing_area_labels_must_match_deliverables]] — Path-prefix table.
* [[feedback_backlog_promotion_must_rewrite_description]] — `[HOLD]` survives label change.
* [[feedback_slow_down_be_careful]] — Bulk-action blast radius.

### Scripts

* `auto-runner-jira.py` — the dispatch loop (3122 LOC, 2026-05-20). All `[runner-*]` prefixes in §5 are grep-able here.
* `scripts/runner-wrapper/run-ephemeral.sh` — Sprint Boreas-C2 ephemeral wrapper (sets `OMNISIGHT_RUNNER_EPHEMERAL=1`, pins worktree).
* `scripts/runner-mirror/setup.sh` / `health.sh` — Boreas-C1 bare-mirror cache.
* `scripts/runner-cleanup/disk-check.sh` / `sweep-orphans.sh` — Boreas-C disk hygiene.
* `omnisight-runner-rescue` (entry point via `pyproject.toml`) — operator inspect/release/reset of `runner_claims` substrate.

### Code modules

* `backend/agents/jira_dispatch.py` — claim mutex, push, transitions.
* `backend/agents/runner_stoploss.py` — per-ticket revert circuit (§5.5).
* `backend/agents/capability_matrix.py` — `(issuetype, area, tier)` → enabled capabilities.
* `backend/agents/scheduler.py` — ticket scoring + selection.
* `backend/agents/runner_workspace_safety.py` — sentinel + cwd safety checks.
* `backend/agents/runner_sandbox.py` — bubblewrap wrap (when enabled).
* `backend/agents/memory_writeback.py` — OP-906 fan-write.
* `backend/agents/file_mutex_check.py` — file-overlap mutex.

### Docs

* [`docs/operations/runner-strategy.md`](runner-strategy.md) — 3-branch strategic framing.
* [`docs/operations/runner-sandbox.md`](runner-sandbox.md) — bubblewrap jail.
* [`docs/sop/runner-pickup-mutex.md`](../sop/runner-pickup-mutex.md) — fencing-token claim.
* [`docs/sop/runner-comment-dedupe-policy.md`](../sop/runner-comment-dedupe-policy.md) — comment de-dup (so this taxonomy stays readable in JIRA).
* [`docs/sop/runner-rescue-cli.md`](../sop/runner-rescue-cli.md) — operator runbook for substrate state.
* [`docs/sop/runner-runtime-artifacts.md`](../sop/runner-runtime-artifacts.md) — what lives on disk per cycle.
* [`docs/sop/runner-state-authority-precedence.md`](../sop/runner-state-authority-precedence.md) — when JIRA vs Gerrit vs substrate wins.
* [`docs/sop/runner-substrate-contract.md`](../sop/runner-substrate-contract.md) — Postgres substrate schema (alembic 0236+).
* [`docs/sop/jira-ticket-conventions.md`](../sop/jira-ticket-conventions.md) — §11 (surrender), §14 (lessons AC), §16 (pickup JQL).
* [`docs/sop/jira-ticket-creation-checklist.md`](../sop/jira-ticket-creation-checklist.md) — older general checklist.
* [`docs/sop/runner-ticket-filing-checklist.md`](../sop/runner-ticket-filing-checklist.md) — **Runner-UX-B (OP-1527) sibling** — the canonical pre-filing checklist; this guide is the conceptual layer above.
* [`docs/sop/capability-matrix-fallback-policy.md`](../sop/capability-matrix-fallback-policy.md) — capability resolution.
* [`docs/sop/architecture-anti-patterns.md`](../sop/architecture-anti-patterns.md) — patterns the runner has bumped into.

### Incidents worth reading once

* OP-974 (2026-05-12) — dual-claim race; motivated the OP-977 mutex rewrite.
* OP-1124 / OP-1126 (2026-05-15) — overnight area-mismatch loop; motivated Sprint Boreas-C ephemeral cycle + [[feedback_ticket_area_must_span_all_4_ac_areas]].
* 53-ticket stoploss day (2026-05-17) — motivated `[runner-detected-shipped]` ([[feedback_runner_already_shipped_loop]]).
* TOCTOU storm (2026-05-18) — 5 tickets lost work because assignee was unset mid-run ([[feedback_never_unset_assignee_on_running_ticket]]).
