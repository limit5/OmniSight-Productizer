# EPIC design — Skill-XP accrual on delivery (RPG.W12 activation)

**Status:** design (SOP Stage 3) · **Date:** 2026-07-01 · **Author:** claude (dogfood)
**Companion SOP:** `docs/sop/epic-decomposition-and-ticket-filing-sop.md`

## Problem
Character/class XP now accrues on every delivery (RPG.W4, Change #1900). Skill XP
(RPG.W12) does **not** — the per-`(agent_id, skill_id)` tree stays empty because
**nothing at runtime maps a delivered ticket to a `skill_id`**. The engine is
otherwise complete.

## Premise check (SOP Stage 1 — first-hand)
**Reusable (built + tested):**
- `skill_leveling.award_skill_xp(store, agent_id, skill_id, *, delta, outcome, tier_l_plus, first_time_skill_use, same_task_hash_within_24h)` — computes delta AND persists `agent_skill_state` (alembic 0226/0242). Thresholds `{1:0,2:25,3:100,4:250,5:600}`, Lv5 decay floor 1500. Lv3 branch fork (`lock_branch_choice`), mastery effects, decay cron — all live.
- `skill_leveling.compute_xp_delta(base_delta, outcome, *, tier_l_plus, first_time_skill_use, same_task_hash_within_24h) -> int` — applies the W12 stacking once to a base token amount. (Note: `xp_engine.skill_xp_delta_for` is dead code and is **deliberately NOT used** by this delivery path — it would double-multiply against `award_skill_xp`/`compute_xp_delta`.)
- `skill_matrix.yaml` — guild-keyed skill registry + `canonical_skill_ids()` + `_assert_skill_id_in_matrix` + W11.2 drift guard. Guilds WITH skills: `algo_cv`(barcode_scanner, depth_sensing), `backend`(enterprise_web), `bsp`(connectivity-5g/ethernet), `hal`(connectivity-ble/can/modbus/opcua/wifi), `isp`(ipcam, uvc).
- Character identity resolution (`_resolve_card_identity` in auto-runner-jira.py, #1899/#1900) — the owning persona/bot + its guild.

**Missing (the whole EPIC):**
1. A **resolver**: delivered ticket → `skill_id`. Nothing produces one. The
   offline `rpg_rebuild_skill_state.py` reads a `tasks.rpg_skill_id` column that
   **no code ever writes**.
2. A **task-completion caller** of `award_skill_xp` (only offline script +
   `teach_other_agent` call it today).
3. An **env/sync persistence helper** for the runner (mirroring
   `character_card.award_task_xp_sync`).

## The design decision — resolver = explicit, guild-constrained `skill:` label
A ticket MAY carry `skill:<skill_id>`. On delivery, award skill XP to
`(character_agent_id, skill_id)` iff the ticket has a `character:` label AND the
skill is valid AND belongs to that **character's registry guild** (authoritative).
No/invalid `skill:`, or a bot-only ticket, → no skill XP (character XP still
accrues). **Opt-in, character-scoped, deterministic, fail-open.**

> **[codex round 1 — folded in]** Scoped to `character:` tickets only. The earlier
> "derive guild from `area:` when no character" is UNSOUND — `area:` values
> (`db`, `embedded`, `tests`, …) are not guild slugs, and the first-task card
> falls back to `backend` for unknown areas, so `area:db` would wrongly permit
> `enterprise_web` while `area:embedded` couldn't map to hal/bsp/isp. A character
> carries an authoritative guild in `character_registry`; a bare bot does not.
> Bot/area→guild is deferred until a real per-agent guild source exists.

**Why (vs alternatives):**
- *Guild-default (auto)* — a guild owns MANY skills (hal has 5); a single default
  collapses them → skills never differentiate → breaks the "different skills,
  different XP" RPG goal. Rejected.
- *Diff/content inference* — non-deterministic, untestable, high runner-drift;
  violates the SOP runner-safe/testable rule. Deferred as a future convenience on
  top of the deterministic floor, never the floor.
- *Explicit label* — deterministic, testable, runner-safe, matches existing
  `class:`/`character:` label routing, composes with `compute_xp_delta`
  (amount, applied once) + the atomic store award (persist) + `skill_matrix` (guild→skills validation).
  **The RPG semantics fall out for free:** your guild defines your trainable
  skills; a quest (ticket) explicitly trains one of them.

**Guild constraint** makes the three layers cohere: guild = your skill space;
class/character levels from every delivery; a skill levels only from deliveries
you tag within your guild — a backend persona can train `enterprise_web`, never
`connectivity-ble`.

## Target architecture — safe-floor-first (SOP Stage 4 granularity)
Data/validation/store-correctness floor lands first (zero behavior change), then
wiring disabled-by-default, then operator-gated activation. **[codex round 1]**
the atomic-award store fix moves into the safe floor (it's a correctness change,
independently testable) and S2's old scope is split so the "wire into the loop"
integration gate is explicit.

### S1 — resolver + label schema + filer (safe floor, INERT). area: backend, tests
- New `backend/agents/skill_resolver.py`: `resolve_skill_for_character(labels) -> str | None` — parse `character:` + `skill:` labels; return the skill_id iff a character is present, the skill is in `skill_matrix`, AND it's in that character's registry guild; else None. Pure, fail-open, never raises.
- `docs/sop/jira-label-schema.yaml`: add `skill:` namespace (single-valued).
- `scripts/file_jira_ticket.py`: `--skill <id>` requires `--character`, validates the skill ∈ that character's guild, emits `skill:<id>`.
- Pure + unit-tested. **No caller** → zero behavior change. MUST NOT touch the runner delivery path.

### S1b — atomic skill-XP award in the store (safe floor, correctness). area: backend, tests, db
- **[codex round 1+2]** Add `PostgresSkillStateStore.award_delta_atomic(agent_id, skill_id, *, base_delta, outcome, tier_l_plus)`. Because the applied delta depends on first-time, and first-time depends on insert-vs-update, **precompute BOTH deltas** before the statement: `first_delta = compute_xp_delta(base_delta, outcome, tier_l_plus=…, first_time_skill_use=True)`, `repeat_delta = compute_xp_delta(base_delta, outcome, tier_l_plus=…, first_time_skill_use=False)`. Then one statement: `INSERT … VALUES (…, skill_xp=first_delta, …) ON CONFLICT (agent_id,skill_id) DO UPDATE SET skill_xp = agent_skill_state.skill_xp + <repeat_delta bound param>` with `RETURNING skill_xp, (xmax = 0) AS inserted`. Recompute level from the returned absolute xp; branch_required from the new level. Atomic — no read-modify-write, so two concurrent slots delivering one character's ticket both apply without a lost update. No caller yet → inert.
- MUST NOT change existing `award_skill_xp` callers' behavior (offline script / teach).

### Skill XP base amount — **BASE_SKILL_XP = 25** (not BASE_TASK_XP)
**[codex round 2]** Skill LEVEL thresholds are `25/100/250/600`. Reusing the
character `BASE_TASK_XP = 100` would jump a skill to Lv4 on the first plain
success (100×3=300) or Lv5 on the first Tier-L+ (100×2×3=600) — instant-max, no
grind. That kills the "different skills level with effort" goal. So the delivery
path awards a dedicated **`BASE_SKILL_XP = 25`** token: a plain success = 25×3 =
75 (→ Lv2), a Tier-L+ = 25×6 = 150 (→ Lv3); Lv5 (600) then takes a genuine string
of deliveries. (The offline `rpg_rebuild_skill_state.py delta=100` is a separate
historical-replay concern and is NOT changed here; a follow-up may reconcile it.)
The multiplier math is unchanged — only the per-delivery base token differs from
the character curve.

### S2 — accrual wiring (disabled-by-default). area: backend
- Capture `skill:` in `_LAST_TICKET_METADATA` (beside `character`) at prompt-build — **[codex round 1]** this is the label-plumbing gate; without it `skill:` never reaches the finalizer.
- New `skill_leveling.award_skill_xp_from_env / _sync` (mirror `character_card.award_task_xp_sync`): env DSN → `PostgresSkillStateStore.award_delta_atomic(base_delta=BASE_SKILL_XP, outcome="success", tier_l_plus=<tier∈{L,X}>)`. **Do NOT pre-multiply via `skill_xp_delta_for`** — the store applies the multiplier exactly once. Fail-open.
- `auto-runner-jira.py`: `_award_skill_xp(ticket_key)` = resolve character slug from metadata → `skill_resolver` → owning agent_id → award. Call it in `_finalize_successful_push` beside `_award_character_xp`, gated behind `OMNISIGHT_RPG_SKILL_XP_ENABLED` (default OFF).
- ACs (**[codex round 1+2]** strengthened): flag-off = no-op; flag-on end-to-end **finalizer** test proving a `skill:` label reaches the award; **flag-on `skill:` WITHOUT a valid `character:` = skip (negative finalizer AC)**; off-guild/invalid skill = skip; valid character-guild = award once; concurrent same-skill award = no lost update.
- MUST NOT change character-XP behavior or run when the flag is off.

### S3 — activation (operator-gated). tier:X / operator
- Flip `OMNISIGHT_RPG_SKILL_XP_ENABLED=1` on the fleet; file a real `--character nova --skill enterprise_web` ticket; verify the skill row levels. Also file the follow-up note: **character-XP award (#1900) shares the same read-modify-write pattern** — harmless while each slot owns a distinct agent_id, but a shared-character-across-slots race mirror; track an atomic-ise follow-up.

### Out of scope (MVP)
- Multi-skill tickets (namespace is single-valued for now).
- Lv3 branch-choice prompting on accrual (award returns `branch_choice_required`; the existing UI/endpoint handles the lock — no new surface here).
- Auto-inference of skill from guild-default or diff.
- Guilds without matrix skills (frontend/sre/…) — no skill XP until the matrix grows; character XP unaffected.

## Codex review round 1 — folded in (VERDICT: NEEDS-REWORK → addressed)
All five findings accepted and folded above:
1. **Double-multiply** (`skill_xp_delta_for` + `award_skill_xp` both multiply → 3600 not 600) → S2 awards with `base_delta=BASE_TASK_XP` and lets the store multiply ONCE; the dead `skill_xp_delta_for` bridge is NOT used.
2. **Concurrency race** (read-modify-write lost update) → S1b atomic `ON CONFLICT DO UPDATE SET skill_xp = skill_xp + delta`, first-time from insert-vs-update.
3. **Guild-from-area unsound** → MVP awards ONLY for `character:` tickets (authoritative registry guild); bot/area→guild deferred.
4. **Label plumbing gap** → S2 captures `skill:` in `_LAST_TICKET_METADATA` + a finalizer-level test that a `skill:` label reaches the award.
5. **S2 too broad** → split (S1/S1b/S2) + strengthened S2 ACs (flag-off no-op, flag-on e2e finalizer, off-guild skip, valid award, concurrency).

## Codex review round 2 — folded in (VERDICT: SOUND-WITH-CHANGES → addressed)
1. **S1b atomic SQL underspecified** — you can't know first-time before the insert → precompute BOTH `first_delta` and `repeat_delta`; insert uses first_delta, conflict-update adds repeat_delta; `RETURNING (xmax=0) AS inserted`. (Folded into S1b.)
2. **Base-amount jump** — `BASE_TASK_XP=100` instant-maxes a skill → introduced **`BASE_SKILL_XP=25`** for a real grind. (New section.)
3. **Dead-bridge prose contradiction** — removed "skill_xp_delta_for ready to use / composes with" wording; it's explicitly NOT used by the delivery path.
4. **Negative finalizer AC** — added "flag-on `skill:` without valid `character:` = skip" to S2 ACs.

Design is now SOUND-WITH-CHANGES with every change specified → ready for Stage 4 ticket-split + filing (4 stories: S1, S1b, S2, S3).
