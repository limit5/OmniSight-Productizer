# Cold-start operator-intent guards — TICKET DRAFT (codex-audited, for filing)

**Status**: DRAFT (2026-05-23). Spec = `docs/operations/2026-05-23-coldstart-acting-blast-radius-audit.md`
(codex: core blocker sound+severe; fix scope = S1+S2+S3+S4). Two tickets: a
flip-blocking core fix (CG-1) + a defense-in-depth follow-up (CG-2). Both block
OP-1619 conceptually; the immediate flip-blocker is CG-1 (the current Startup-2 blast
radius is OP-1619 + OP-739, BOTH tier:X → fully covered by CG-1's tier:X exclusion;
no human-assigned non-tier:X ticket is at risk today → CG-2 is defense-in-depth).

Filing protocol (same as OP-1621): commit this draft + audit + codex review to
develop tagged with a META/non-impl key (NEVER the impl ticket key — see
[[feedback_doc_commit_impl_key_h12_autowalk]]); file CG-1 pickable; wire OP-1619.

---

## CG-1 — [OP][coord-coldstart] Cold-start Startup-2/3 honor operator-intent guards (coord-skip/coord-quarantine/tier:X/META)
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:tests`
- **labels**: `scope:coord-coldstart`, `type:bug`, `agent:auto`, `tier:M`,
  `area:backend`, `area:tests`, `capability:enable=gerrit_push`, `class:subscription-codex`
- **## Files/Paths**: `backend/agents/pipeline_coordinator.py` —
  `interrupted_tickets` JQL + fields (~:1186-1200), `_build_stale_sweep_plan` JQL +
  loop (~:1602-1660), the Startup-2 reconcile mutators (`_startup_2_reconcile` ~:2282)
  and Startup-3 sweep mutators (`_startup_3_sweep`) last-mile recheck; mirror the
  LiveActionExecutor coord-skip/coord-quarantine recheck (~:555-561).
  `backend/tests/test_pipeline_coordinator_coldstart.py`. **Out of scope**: human-vs-bot
  assignee guard + bot-account allowlist (CG-2); Startup-1 (OP-1621, merged).
- **v4/audit coverage**: S1 (coord-skip + coord-quarantine), S2 (tier:X), S4 (META in reconcile), last-mile recheck.
- **Code AC**:
  - `interrupted_tickets` JQL (Startup-2, :1193): add
    `AND (labels is EMPTY OR labels not in ("coord-skip","coord-quarantine","tier:X","priority:meta","type:meta"))`.
    Also FETCH `labels` (today it fetches only `status`,`updated`) so the last-mile
    recheck has the data.
  - `_build_stale_sweep_plan` JQL (Startup-3, :1619): add
    `AND (labels is EMPTY OR labels not in ("coord-skip","coord-quarantine","tier:X"))`
    (META not relevant to sweep classes, but harmless to include).
  - **Last-mile recheck** (mirror LiveActionExecutor :555-561): immediately before
    each Startup-2 reconcile mutation (`reset_to_todo`/`mark_resumable`/
    `transition_under_review`/`mention_operator`) and Startup-3 mutation
    (`remove_label`/`clear_assignee`), re-check the ticket's live labels and SKIP
    (record `coord_skip`) if `coord-skip`/`coord-quarantine` is present — closes the
    fetch→mutate TOCTOU window.
  - Per codex Q4: tier:X exclusion strands no legitimate work (runner pickup JQL
    already excludes tier:X, jira_dispatch.py:364).
  - **(Q5, folded in per operator)** Startup-3 claim-cleanup must EXCLUDE
    terminal-status tickets: a 公開済み / Archived ticket carrying a stale
    `claim:default` is done — do NOT drop its claim each boot (50+ such tickets →
    pointless live write-churn at 1/boot). Add a terminal-status guard to
    `_build_stale_sweep_plan`'s `stale_claims` classification (skip statuses in
    PUBLISHED_STATUS_NAMES + Archived) so claim-cleanup only targets non-terminal
    tickets with a genuinely orphaned claim.
- **Deploy AC**: ships in coordinator package; behavior change only at cold-start
  boot. `deployed:` = next build + ~/sora-bridge synced + coordinator restarted.
- **Integration AC**: a 進行中 ticket carrying coord-skip / coord-quarantine / tier:X /
  META → NOT in the Startup-2 interrupted set → never reconciled/reverted. A
  claim/assignee ticket carrying those → NOT in the Startup-3 sweep set. A label
  added between fetch and mutate → last-mile recheck skips it. A genuinely-abandoned
  runner ticket (no guards) → still reconciled/swept as today.
- **Exercised AC**: tests — Startup-2 JQL excludes guarded tickets; Startup-3 JQL
  excludes guarded tickets; last-mile recheck skips a TOCTOU coord-skip; a plain
  stale runner ticket still reverts; **host shadow regression**: a fresh shadow
  cold-start no longer lists OP-1619 / OP-739 in Startup-2 reconcile.
- **Go-Live**: shadow-safe; verified in shadow before the OP-1619 flip.
- **Dependencies**: standalone (pickable). **Blocks OP-1619.**

## CG-2 — [OP][coord-coldstart] Skip human-assigned tickets in cold-start + bot accountId allowlist (S3)
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:tests` · **blockedBy**: CG-1
- **labels**: `scope:coord-coldstart`, `type:bug`, `agent:auto`, `tier:M`, `area:backend`, `area:tests`, `capability:enable=gerrit_push` (NO `class:` until CG-1 公開済み)
- **## Files/Paths**: `backend/agents/pipeline_coordinator.py` (Startup-2/3 Python
  last-mile guard), a bot-account allowlist (config — location TBD in the ticket),
  `backend/tests/`. **Out of scope**: the JQL guards (CG-1).
- **Goal / coverage**: S3 — skip tickets assigned to a HUMAN (assignee accountId not
  in a maintained bot allowlist) in Startup-2 reconcile + Startup-3 orphan-assignee
  clear. Human-vs-bot cannot be done in JQL (codex Q3), so it is a Python last-mile
  guard. Requires establishing a **bot accountId allowlist** that today does NOT
  exist (codex Q2: `DispatchClient` knows only the current client's `bot_account_id`,
  jira_dispatch.py:248; per-instance bot drift risk — Family F). The ticket must
  decide the allowlist's canonical source (config list of bot accountIds, refreshed
  from each bot env's `/myself`).
- **Code AC**: a bot accountId allowlist (config) + a Python guard: Startup-2 skips +
  Startup-3 orphan-assignee skips any ticket whose assignee accountId ∉ allowlist
  (i.e. human). Records `human-assigned` skip reason.
- **Deploy / Integration / Exercised AC**: human-assigned To-Do ticket → assignee NOT
  cleared by Startup-3; human-assigned 進行中 ticket → NOT reconciled; bot-assigned →
  handled as today; allowlist drift (unknown accountId) → treated as human
  (default-deny mutation — safer).
- **Go-Live**: shadow-safe.
- **Severity note**: NOT an immediate flip-blocker — the current Startup-2 blast
  radius (OP-1619, OP-739) is entirely tier:X, fully covered by CG-1. CG-2 is
  defense-in-depth for future human-assigned non-tier:X tickets. (Operator to confirm
  whether CG-2 also gates OP-1619 or lands as a fast-follow.)

## Operator decisions (resolved 2026-05-23)
- **Flip gate = CG-1 only**; CG-2 (human-assignee guard + bot allowlist) is a
  fast-follow (no human-non-tier:X ticket is at risk today). OP-1619 blockedBy CG-1.
- **Q5 folded into CG-1** (Startup-3 claim-cleanup skips terminal-status tickets) —
  see the CG-1 Q5 Code AC above; no separate follow-up ticket.

Exercised AC addition (Q5): a 公開済み/Archived ticket with a stale `claim:default` is
NOT swept (claim retained / no write); a non-terminal orphaned claim still dropped.
