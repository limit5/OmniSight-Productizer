# Cold-start acting-mode blast-radius audit — DEEP AUDIT (for codex review)

**Status**: DRAFT deep-audit (2026-05-23). Triggered by the OP-1619 acting-flip:
two cold-start over-reaches surfaced in shadow preview (Startup-1 tried to start
RETIRED units → fixed by OP-1621; Startup-2 would revert a human META → this audit).
Before filing more piecemeal fixes, this audits the FULL cold-start 4-phase blast
radius in acting mode. Code = shipped `~/sora-bridge/.../pipeline_coordinator.py`
(develop @ OP-1621).

## The systematic finding (codex-corrected 2026-05-23)
**Startup-2 + Startup-3 honor NO operator-intent guard** — their direct gateway
mutators select purely on runner-centric staleness and never check `coord-skip`,
`coord-quarantine`, `tier:X`, human-assignee, or META. They are **weaker than BOTH**
the steady-state poll (`coordinator_jql` :979 excludes `coord-skip` only — note: NOT
`coord-quarantine`) AND the live-action executor (OP-1614 rechecks BOTH coord-skip +
coord-quarantine, :555-561). **L6 is a partial exception** (correction): the primary
current L6 path replays unmatched `tick_intent` records through `LiveActionExecutor`,
which DOES recheck coord-skip/coord-quarantine at execution — but still does NOT
guard `tier:X` / human / META; the legacy `shutdown_began` L6 path uses the raw
gateway with no recheck. Net: cold-start mutates operator/human tickets it cannot
distinguish from abandoned runner work.

## Per-phase mutation inventory (acting=True)
| Phase | selects | mutates | guard today |
|---|---|---|---|
| **Startup-1** infra (`_startup_1_infra` :2079) | audit `down` units | `start_unit` each; halt+`mention_operator` if still down | **OP-1621**: now only `expected=yes` (retired/gated excluded) ✓ |
| **Startup-2** reconcile (`_startup_2_reconcile` :2282; `interrupted_tickets` :1186) | **ALL `進行中`** tickets, `ORDER BY updated ASC` | (b) mergeable→`transition_under_review`; (a) commits→`mark_resumable`; (c) no-commits+stale→**`reset_to_todo`**; (d) recent→`mention_operator` | **NONE** — no coord-skip / tier:X / human / META filter |
| **Startup-3** sweep (`_build_stale_sweep_plan` :1602; `_startup_3_sweep`) | `claim:* OR runner-blocked:waiting-* OR (To Do AND assignee)` | drop `claim:*`; **`clear_assignee`** orphan; drop dead waiting markers | **NONE** — no coord-skip / tier:X / human filter |
| **L6** crash-recovery (`_l6_crash_recovery` :2118) | **(corrected) TWO paths**: (primary) unmatched `tick_intent` replay (:2152-2164) → `_execute_tick_actions` → `LiveActionExecutor` (:2201); (legacy) `shutdown_began` w/o `complete` + preceding real-action tick | replays ALL `LiveActionExecutor` kinds (relabel/transition/mention/followup/escalate/file_ticket); legacy path re-issues via raw gateway | primary path: `LiveActionExecutor` **rechecks coord-skip/coord-quarantine** (:555-561) but NOT tier:X/human/META; legacy path: NONE. Empty under shadow history today |

Per-phase caps (OP-1618, default 1/1/1) bound the COUNT per boot, not WHICH ticket
or whether it is a legitimate target.

## Live blast radius (enumerated against prod JIRA, 2026-05-23)
**Startup-2 interrupted set (`status in In Progress/進行中`) = 2 tickets, BOTH over-reach:**
- `OP-1619` — tier:X, no assignee — **the acting-flip GATE ticket itself** (would be
  reconciled/mentioned). Not runner work.
- `OP-739` — HUMAN (nagita), tier:X, META (`priority:meta`), now also `coord-skip` —
  nagita's "CI parallel-gate program" tracking META. `reset_to_todo` when stale
  (its `updated` was 2026-05-14 — only transiently fresh because adding `coord-skip`
  bumped it; coord-skip is NOT honored by Startup-2, so it goes stale → reverts again).
- **Zero legitimate abandoned-runner tickets** in the set today → Startup-2 would
  currently mutate ONLY over-reach targets.

**Startup-3 sweep set (~50+):** overwhelmingly 公開済み/Archived tickets carrying a
stale `claim:default` + bot assignee → `claim:*` drop (harmless cleanup, but live
writes, 1/boot). Plus To-Do bot-assigned tickets (e.g. OP-231 claude-bot [HOLD]) →
`clear_assignee`. A **HUMAN-assigned To-Do** ticket would ALSO get its assignee
cleared (no human guard).

## Side effects (why this matters for the flip)
1. **Reverts human-owned in-progress work** (OP-739): a long-lived human/META ticket
   legitimately sits in 進行中; cold-start reads stale-`updated` as "abandoned runner
   work" → reverts to To Do, clearing context. Disrupts the human.
2. **Noises operator gate tickets** (OP-1619 tier:X): reconcile/mention on tickets
   that exist precisely as human gates.
3. **Clears human assignees** (Startup-3): an operator who self-assigns a To-Do
   ticket would have it silently cleared.
4. **coord-skip is a false promise at boot**: operators (and this very session) reach
   for `coord-skip` to protect a ticket, but cold-start ignores it — a latent
   foot-gun.
5. Caps (1/1/1) reduce volume but the FIRST-touched (oldest `updated`) is often the
   most-stale legit-looking human/META ticket.

## Recommended fix scope (for the ticket — pending codex)
Add operator-intent guards to the cold-start phase QUERIES so boot recovery matches
the steady-state path's respect for operator intent:
- **S1 (primary):** `interrupted_tickets` (Startup-2) + `_build_stale_sweep_plan`
  (Startup-3) JQL must EXCLUDE `coord-skip` and `coord-quarantine` (mirror
  `coordinator_jql` :979). Closes the false-promise (#4) + protects OP-739.
- **S2:** also exclude **`tier:X`** from both (tier:X is operator/human-gate by
  definition — never runner work; already excluded from runner pickup JQL). Closes
  OP-1619 + OP-739 (both tier:X).
- **S3 (consider):** skip **human-assigned** tickets in reconcile/sweep (assignee not
  a known bot account) — protects human in-progress work + human self-assignments
  regardless of tier. Needs a bot-account allowlist (claude-bot/codex-bot/...).
- **S4 (consider):** skip **META** (`priority:meta`/`type:meta`) tickets in Startup-2
  reconcile — a META legitimately sits 進行中 as a roll-up, never "abandoned runner
  work".

## Open questions for codex
- Q1: is S1+S2 (coord-skip + tier:X exclusion in both queries) sufficient, or are
  S3/S4 (human-assignee / META) needed for correctness (any over-reach S1+S2 misses)?
- Q2: bot-account allowlist for S3 — where is the canonical list (so "human" = not in
  it)? Is there drift risk (Family F per-instance bots)?
- Q3: should the guards live in the JQL (server-side filter) or in the Python
  classification (post-fetch)? JQL is cheaper + can't be forgotten by a caller; but
  `labels not in (...)` + `assignee` filters interact with the existing `ORDER BY`.
- Q4: does excluding tier:X from Startup-2/3 ever drop a LEGITIMATE recovery (is any
  real runner work ever tier:X)? (Runner pickup JQL excludes tier:X, so a tier:X
  ticket should never have been runner-claimed — confirm.)
- Q5: Startup-3 claim-drop on 公開済み/Archived tickets (50+) — harmless cleanup or
  should done-state tickets be excluded too (avoid 50 pointless writes over 50 boots)?
- Q6: anything else in the cold-start path (L6, the halt/mention) that mutates
  operator/human state and this audit missed.

## ✅ codex verdict: audit-needs-revision → corrected (2026-05-23, core blocker SOUND + SEVERE)
`coldstart-acting-blast-radius-codex-audit-2026-05-23.txt`. Startup-2/3 over-reach
confirmed accurate (Startup-3 slightly worse — clear_assignee fires in two places
:2364/:2371). Corrections applied above: L6 tick-intent-replay path added; the
operator-intent-guard claim scoped to Startup-2/3 (+ L6 legacy); coord-quarantine
gap in coordinator_jql noted. **Fix scope ruling: implement ALL of S1+S2+S3+S4** —
S1+S2 alone are insufficient (miss human-assigned non-tier:X + unassigned non-tier:X
META). codex Q-answers folded into the fix plan:
- **S1+S2 (JQL, coarse):** exclude `coord-skip`, **`coord-quarantine`**, `tier:X`
  from BOTH Startup-2 `interrupted_tickets` and Startup-3 `_build_stale_sweep_plan`
  JQL. (Q3: JQL for coarse always-true exclusions.)
- **S3 (Python last-mile):** skip human-assigned tickets — needs a maintained bot
  **accountId allowlist**, which does NOT exist today (Q2: `DispatchClient` knows
  only the current client's `bot_account_id`, jira_dispatch.py:248; per-instance bot
  drift risk). The fix must establish that allowlist (config) + a Python guard before
  any cold-start gateway mutation. Human-vs-bot cannot be done in JQL.
- **S4 (META):** skip `priority:meta`/`type:meta` in Startup-2 reconcile (META sits
  進行中 as a roll-up — never abandoned runner work). JQL where expressible.
- **Last-mile recheck:** the Startup-2/3 raw gateway mutators should also recheck
  coord-skip/coord-quarantine immediately before mutating (mirror LiveActionExecutor
  :555-561), so a TOCTOU label-add between fetch and mutate is honored.
- **Q4 (confirmed):** excluding tier:X strands no legitimate work — runner pickup JQL
  already excludes tier:X (jira_dispatch.py:364), so a tier:X ticket was never
  runner-claimed; any tier:X "runner residue" is exceptional → operator-visible, not
  blind cold-start mutation.
- **Q5 (follow-up, NOT flip-blocking):** Startup-3 drops stale `claim:*` off 50+
  公開済み/Archived done tickets (harmless but live write-churn, 1/boot). Excluding
  terminal statuses from claim-cleanup is a lower-priority separate follow-up.

## Relationship to the flip (OP-1619)
HARD pre-flip blocker (in addition to OP-1621, already merged). Until cold-start
Startup-2/3 honor operator intent (S1-S4), flipping `acting=True` mutates
human/operator tickets on boot. Fix ticket to be filed next; OP-1619 blockedBy it.
The Q5 done-state claim churn is a non-blocking follow-up.
