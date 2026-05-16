# Agent RPG System — Operator Guide

> **Status**: v0.5.0 partial ship (RPG.W1-W14 core + W15-W17 + W19-W20
> shipped; W18 + W21 deferred). Last reviewed
> 2026-05-11 (W12 promoted to **Live** via OP-217; W13 promoted to
> **Live** via OP-218; W14 promoted to **Live** via OP-219;
> W17 promoted to **Live** via OP-220).
> **Authoritative spec**: [ADR-0008 — Agent RPG Class & Skill Leveling
> System](/docs/adr/ADR-0008-agent-rpg-class-skill-leveling/). This doc covers
> *operation*, not design — when the two diverge, ADR-0008 wins and this
> doc gets corrected.

This is the operator-facing how-to for the Agent RPG system: viewing
character cards, adding skills, picking which Guild a new agent lives
in, replaying onboarding tours, and reading XP / level fields without
having to re-derive the curve from the ADR every time.

---

## What's shipped vs. deferred

The RPG system ships in waves. Some of the surfaces ADR-0008 describes
are **structural slots** (module + helper present, no live data path
yet); some are fully live. Treat the table below as the source of
truth on what an operator can act on **today** (2026-05-08 — v0.5.0
target ship).

| Wave   | Module / surface                                              | Status today               |
| ------ | ------------------------------------------------------------- | -------------------------- |
| W1.2   | `backend/agents/character_card.py` — CRUD + first-task create | **Live** (in-memory + Postgres store) |
| W1.1   | `agent_character_card` Alembic migration                      | **Pending** — module references the table; migration not yet checked in |
| W2.1   | `backend/agents/guild_registry.py` — Guild metadata + class eligibility | **Live**          |
| W4.1   | `backend/agents/xp_engine.py` — XP curve + outcome multipliers + buff/debuff multipliers | **Live** (no persistence layer; callers apply XpDelta themselves) |
| W9.4   | `backend/agents/guild_hall.py` — roster view-model                                       | **Live** (consumed by Guild Hall UI) |
| W11.2  | `backend/agents/skill_matrix.py` + `skill_matrix.yaml` — canonical skill registry + drift guard | **Live**     |
| W11.3  | This document                                                 | **Live**                   |
| W15.1  | `backend/agents/buff_registry.py` — Fresh Tokens / Streak / Well-Rested  | **Live** (XP multiplier inputs) |
| W15.2  | `backend/agents/debuff_registry.py` — Burnout / Stale Memory             | **Live**                  |
| W16.1  | `backend/agents/achievement_registry.py` + unlock daemon                 | **Live**                  |
| W19.1  | `backend/agents/skill_fusion.py` — Lv5 fusion preview                    | **Live**                  |
| W20.2  | `backend/agents/campaign_progress.py` — chapter ledger                   | **Live**; alembic 0201 adds `tasks.rpg_campaign_id` + `rpg_campaign_title` |
| W3.x   | Daily style fingerprint cron                                  | **Deferred** — fingerprint column exists; recompute job not scheduled |
| W5-W7  | L1/L2/L3 memory hooks, L3 reflection RAG, routing integration | **Deferred**               |
| W12    | `backend/agents/skill_leveling.py` + alembic 0226 `agent_skill_state` | **Live** (OP-217) — branch lock + decay cron live |
| W13    | `backend/agents/tool_proficiency.py` + alembic 0227 `agent_tool_proficiency` + `config/tool_proficiency_gates.yaml` | **Live** (OP-218) — MP.W17.7 telemetry consumer + dispatcher gate live |
| W14    | `backend/agents/talent_tree.py` + `config/talent_tree.yaml` + alembic 0228/0229 | **Live** (OP-219) — milestone lock + capstone gate live; routing weight injection feature-flagged off until W7.1 |
| W17    | `backend/agents/party.py` + `backend/agents/synergy_registry.py` + `config/synergy_matrix.yaml` + alembic 0230 | **Live** (OP-220) — party CRUD + synergy lookup + pre-pickup gate live |

If a runbook step below names a surface that is "Deferred" in this
table, the step is provisional and will start failing the moment the
operator tries it. File an OP ticket when you hit one.

---

## Source-of-truth pointers

| Concept             | Owned by                                                                                          | RPG is a…                |
| ------------------- | ------------------------------------------------------------------------------------------------- | ------------------------ |
| `Guild` enum        | `backend/sandbox_tier.py` (re-exporting BP.B's source of truth)                                   | Importer (`guild_registry.py`) |
| `agent_class` slug  | `config/agent_class_schema.yaml` (MP.W0.1 — shared with ADR-0007)                                 | Reader                   |
| `skill_id` namespace| `backend/agents/skill_matrix.yaml` — **this file is RPG's responsibility**                         | Owner                    |
| `tool_id` namespace | MCP server registry + A2A tool catalog (gate config in `config/tool_proficiency_gates.yaml`)        | Reader (W13 live via OP-218) |
| Synergy matrix      | `backend/agents/synergy_registry.py` + `config/synergy_matrix.yaml` — **this pair is RPG's responsibility** | Owner                    |

When something feels like it belongs in two places, defer to the
column-2 owner, not RPG. The RPG drift guard (`SkillMatrixDriftError`,
`CharacterCardGuildDriftError`) catches divergence at CI time — but
only if the YAML and the registry are consistent in the first place.

---

## Viewing the roster (live today)

The Guild Hall roster + per-instance Character Card are reachable via
one HTTP endpoint. The frontend Guild Hall and Character Card panels
are the same data, formatted.

```bash
# Whole roster, default sort = level desc
curl -H "Authorization: Bearer $TOKEN" \
     "https://omnisight.local/api/v1/agents/cards"

# Filter by Guild slug, sort by activity recency
curl -H "Authorization: Bearer $TOKEN" \
     "https://omnisight.local/api/v1/agents/cards?guild=backend&sort_by=activity"
```

Endpoint definition: `backend/routers/agents.py:128-141`. Accepted
`sort_by` values are `level` (default), `xp`, `activity`. Returns one
`CharacterCardRosterEntry` per agent — the fields are
`agent_id`, `agent_class`, `instance_suffix`, `guild`, `level`, `xp`,
`specialization_label`, `style_fingerprint`, `created_at`,
`last_activity_at`.

If the response is `[]` and you expected agents:

1. Check `agent_character_card` is migrated. The module imports it but
   W1.1 migration is **pending** — on a fresh deploy you may need to
   create the table by hand (schema in `character_card.py` `_CARD_RETURNING_COLS`)
   until the alembic revision lands.
2. Check that the first-task hook (`ensure_card_for_first_task`) is
   wired in your dispatch path. On a clean install with no completed
   tasks, the table will be empty by design — agents only get a card
   on first task.

---

## Skill matrix — adding a new skill

This is the most common day-2 operator task. The canonical skill
registry is `backend/agents/skill_matrix.yaml`; it is the single
source of truth for the `skill_id` namespace and the W11.2 drift guard
fails CI if any other YAML under `configs/` references a `skill_id`
that's missing from this file.

### 1. Edit `backend/agents/skill_matrix.yaml`

```yaml
schema_version: 1
skills:
  backend:                            # Guild slug (must be in Guild enum)
    - skill_id: enterprise_web        # snake_case, ^[a-z][a-z0-9_-]*$
      display_name: Enterprise Web
      summary: One-line description (operator-facing, shows in Character Card).
    - skill_id: my_new_skill          # ← add new entry here
      display_name: My New Skill
      summary: What this skill represents in routing decisions.
```

Rules enforced by `skill_matrix.SkillMatrixError`:

- `schema_version: 1` is required at the top.
- Top-level key under `skills:` must be a Guild slug (lowercase,
  matching `Guild` enum). Unknown Guilds raise on load.
- `skill_id` must match `^[a-z][a-z0-9_-]*$` and is unique across the
  entire matrix (not just within one Guild).

### 2. Run the drift guard locally before pushing

```bash
cd backend
python -c "from backend.agents.skill_matrix import assert_skill_id_space_within_matrix; assert_skill_id_space_within_matrix()"
```

This walks `configs/**.yaml`, collects every `skill_id:` reference,
and raises `SkillMatrixDriftError` listing any IDs not declared in
`skill_matrix.yaml`. If you added a new skill to the matrix to satisfy
a downstream config, this should now pass.

### 3. Updating display name / summary (no `skill_id` change)

Safe — the matrix is a presentation layer for already-stored
`(agent_id, skill_id)` rows. Edit in place, ship in the same PR as the
config that references it.

### 4. Renaming or deleting a `skill_id`

**Don't, unless you know there are no rows in `agent_skill_state`.**
W12 (live as of 2026-05-11 / OP-217) makes `skill_id` part of the
`(agent_id, skill_id)` primary key — renames need a data migration,
not a YAML edit. To audit current rows::

    SELECT skill_id, COUNT(*) FROM agent_skill_state GROUP BY skill_id;

If the rename target has zero rows, the YAML edit is safe; otherwise
plan a forward-only migration alongside `scripts/rpg_rebuild_skill_state.py`.

### 5. Adding or renaming a Lv-3 branch

`skill_matrix.yaml` carries a `branches:` list per skill (W12). Adding
a new option is safe — the drift guard
(`assert_branch_choice_in_matrix()`) only fails when an *existing*
`agent_skill_state.branch_choice` no longer maps to the YAML.

To rename or delete a branch with existing rows, you must do all of
the following in the same change set:

1. Add the new branch entry alongside the old one in YAML.
2. Run `scripts/rpg_rebuild_skill_state.py --clear-branches` against a
   staging DSN to re-derive `branch_choice` from operator intent (the
   `lock_branch_choice` audit log is the source of truth).
3. Remove the old branch from YAML only after no row references it.

---

## Guild registry — assigning agents to Guilds

Guild metadata (display name, summary) lives in
`backend/agents/guild_registry.py:GUILD_DEFINITIONS`. The `Guild` enum
itself is owned by `backend/sandbox_tier.py` (which mirrors BP.B). To
add or rename a Guild, the change must happen in `sandbox_tier.py`
**first**; the registry will pick it up via the imported enum.

A character card's Guild is set:

- Implicitly on first task — `FirstTaskCharacterCard.task_area` is
  mapped via `_guild_from_task_area()` to the closest Guild slug. If
  the area doesn't map cleanly, the card is created with
  `DEFAULT_GUILD = "backend"` and an operator can patch it later.
- Explicitly via `CharacterCardRegistry.update_card(agent_id,
  CharacterCardUpdate(guild=...))`. Only Guild slugs in
  `GUILD_DEFINITIONS` are accepted; everything else raises
  `CharacterCardGuildDriftError`.

There is no `PATCH /api/v1/agents/cards/{id}` HTTP surface yet — the
patch path is internal-only. Operator UI patching ships with W8
(deferred). Until then, run the patch from a Python shell against the
prod DB pool.

### Lv 50 secondary-Guild unlock

Once W18 (multi-class) lands, an agent reaching Lv 50 can pick a
secondary Guild. The constant lives at
`guild_registry.SECONDARY_GUILD_UNLOCK_LEVEL = 50` and the validator
is `validate_secondary_guild_choice()`. Today this is wiring only —
there is no persistence column for the choice yet (W14 talent table
will own it).

---

## XP curve — quick reference

These numbers come from `backend/agents/xp_engine.py`; this section is
a cheat sheet, not a re-spec. The ADR has the design rationale.

### Level curve (W4.2 / OP-133)

```python
level_threshold(N) = ceil(100 * N ** 1.4)        # cumulative XP to reach Lv N
MAX_LEVEL = 80                                    # hard cap; Lv 80+ XP is discarded
BASE_TASK_XP = 100                                # per task, before multipliers
```

The "sigmoid late-game" property called out in ADR-0008 is delivered by
two cooperating pieces:

1. The `100 * N ** 1.4` curve itself -- per-level marginal cost
   `level_threshold(N+1) - level_threshold(N)` grows monotonically with
   `N` (later levels cost more raw XP than earlier ones).
2. The `MAX_LEVEL = 80` hard cap in `level_for_xp()` -- past Lv 80 the
   level computation discards extra XP, so Lv 80 -> 81 is not just
   expensive but unreachable. This is the *flat* tail of the S-curve.

There is no transcendental sigmoid in code; the polynomial curve plus
the absolute cap together produce the operator-facing S-shape. The
character-level field is monotonic by construction (it can rise but
never drop -- see "Common XP-related questions" below).

Outcome multipliers (stack with each other and with buff/debuff):

| Status                                  | ×                                        |
| --------------------------------------- | ---------------------------------------- |
| `success`                               | 1.0                                      |
| `partial`                               | 0.4                                      |
| `fail` / `failed`                       | 0.1                                      |
| `tier_l_plus = True`                    | 2.0 (multiplicative on top of outcome)   |
| `first_time_skill_use = True`           | 3.0                                      |
| `duplicate_task_within_24h = True`      | 0.2 (anti-grinding clamp)                |
| Secondary class below Lv 30             | 0.5 ramp until `SECONDARY_CLASS_FULL_XP_LEVEL` |

Buff / debuff multipliers come from
`buff_registry.xp_multiplier_for_buff_ids()` and
`debuff_registry.xp_multiplier_for_debuff_ids()`; the active set is
passed in `TaskOutcome.active_buff_ids` / `active_debuff_ids` by the
runner.

`award_xp(agent_id, task_outcome) → XpDelta` is **pure** — no DB
writes, no clock reads. The caller is responsible for applying the
returned delta to the character card. Today, that caller is internal
runner code only; there is no operator endpoint to award XP by hand.

### Common XP-related questions

- *"Why did Lv 50 → 51 take so long?"* — cumulative
  `level_threshold(51) = 24,581` vs `level_threshold(50) = 23,909` — the
  Lv 50 → 51 jump costs **672 XP** in marginal terms. Per-level marginal
  cost rises monotonically from ~164 XP at Lv 2 to ~806 XP at Lv 80
  (and is unreachable beyond), so late-game progression flattens by
  design — see "Level curve (W4.2 / OP-133)" above for the derivation.
- *"An agent's level dropped."* — Levels never drop. Skill XP (W12,
  live) decays toward but cannot cross a level threshold; the
  card-level field is monotonic. If you observe a drop, file a bug.
- *"Two agents with the same class are differently leveled."* — That
  is the W3 design (per-instance suffix + style fingerprint).
  `codex-α` and `codex-β` accumulate independent XP.

---

## Onboarding tours

The first-time operator tour is shipped (W10) as part of the
preferences API. There are two tour states, persisted per user:

| Tour                       | Preference key                          | Endpoints                                                             |
| -------------------------- | --------------------------------------- | --------------------------------------------------------------------- |
| Top-level RPG tour         | `seen_rpg_tour`                         | `POST /api/v1/user-preferences/rpg/onboarding-tour/{complete,skip,replay}` + `GET /…/state` |
| Character Card deep tour   | `seen_rpg_character_card_tour`          | `POST /api/v1/user-preferences/rpg/character-card/onboarding-tour/{complete,skip,replay}` + `GET /…/state` |

A user who has already seen the tour can replay it with the `replay`
endpoint; this resets the preference to false and the next page load
re-shows the tooltip sequence. Steps are emitted from
`RPG_CHARACTER_CARD_TOUR_STEPS` constant (server-controlled — operator
can update the copy by editing that list and restarting the API).

---

## Buffs and debuffs (W15)

Buff / debuff state is **input-only** today: the runner computes which
buff IDs apply to a task (e.g. `fresh_tokens` if the provider's quota
is healthy, `streak_3` if the agent has 3 successes in a row,
`burnout` after 5 consecutive failures, `stale_memory` after 30 days
of L2 idle), passes them on the `TaskOutcome`, and the XP engine
multiplies. There is no per-agent buff *state* table yet — the buff is
re-derived per task.

Operator-relevant constants:

- `buff_registry.BUFF_DEFINITIONS` — full set of buff IDs and
  multipliers. Adding a buff is a code change, not a config change.
- `debuff_registry.DEBUFF_DEFINITIONS` — same for debuffs.
- `debuff_registry.active_debuff_ids_for_context()` — derives debuff
  set from `DebuffContext` (consecutive failures, last task age, etc).

If an agent looks "stuck at 0 XP", check the `active_debuff_ids` the
runner is passing in for that agent — `burnout` is a 0.0 multiplier in
some configurations and zeroes out the entire delta.

---

## Achievements (W16)

Achievements are persistent badges attached to a `(agent_id,
achievement_id)` pair. The unlock daemon (`achievement_unlock_daemon.py`)
runs daily and scans the most recent task history for unlock conditions.

Operator can list available achievements:

```bash
python -c "from backend.agents.achievement_registry import ACHIEVEMENT_DEFINITIONS; \
  [print(a.achievement_id, '—', a.display_name) for a in ACHIEVEMENT_DEFINITIONS.values()]"
```

The daemon writes a row per unlock; there is currently no operator
"force-unlock" override path. If an agent missed an achievement that
the operator believes was earned, the unlock criteria may have a bug —
file a ticket against RPG.W16 with the `(agent_id, achievement_id,
date)` triple.

---

## Campaign progress (W20)

Campaigns group multi-task initiatives ("Operation: Phase 2
Migration") onto a shared chapter ledger. Alembic 0201 adds two
columns to the `tasks` table:

- `rpg_campaign_id` — UUID linking tasks to a campaign
- `rpg_campaign_title` — operator-facing label

Tasks without a campaign behave normally; this is a tagging layer.
The `campaign_progress.py` module computes per-campaign chapter state
(open / in-progress / closed). There is no UI for campaign management
today; create campaigns by setting `rpg_campaign_id` on the task row
directly when dispatching.

---

## Skill leveling (W12 — live as of 2026-05-11 / OP-217)

Per-`(agent_id, skill_id)` rows live in `agent_skill_state` (alembic
0226). The helper surface for backend callers is
`backend/agents/skill_leveling.py`:

| Helper | Purpose |
|---|---|
| `await award_skill_xp(store, agent_id, skill_id, delta=..., outcome=..., …)` | Apply XP delta with outcome / Tier-L+ / first-time / anti-grind multipliers |
| `compute_level(xp)` | Pure: returns Lv 1-5 per thresholds `25 / 100 / 250 / 600 / 1500` |
| `await lock_branch_choice(store, agent_id, skill_id, branch)` | Idempotent + refuses re-write (immutable at Lv 3) |
| `await teach_other_agent(store, teacher, student, skill_id)` | Lv-5 only; one-shot +25 XP injection, 7-day cooldown |
| `await decay_idle_skills(store, now=...)` | Sweep: 5%/week on rows idle >= 30 days |

### Per-level unlock effects (set by `award_skill_xp`)

| Lv | Unlock |
|---|---|
| 2  | `extended_thinking_enabled` flag set on agent's next dispatch |
| 3  | `parallel_subtask_enabled` flag set |
| 4  | `prompt_overhead_reduced` (skip preamble in system prompt) |
| 5  | `teach_other_agent` capability unlocked |

Crossing Lv 3 with no `branch_choice` set raises
`branch_choice_required` on the SSE — the Character Card "Skills" tab
shows the picker the operator clicks to choose a fork from
`skill_matrix.yaml`.

### Decay sweep

Driven by `deploy/systemd/rpg-skill-decay.{service,timer}` (Mondays
03:30 UTC) → `scripts/rpg_skill_decay_weekly.sh` → `scripts/rpg_skill_decay.py`.
The sweep is monotonic on level: only `xp` regresses, never below
`next_level_threshold - 1`.

### Recovery

If `agent_skill_state` is corrupted, run
`scripts/rpg_rebuild_skill_state.py` to re-derive rows from
`tasks.success_history`. The replay is idempotent; existing
`branch_choice` values are preserved unless `--clear-branches` is
passed.

---

## MCP/A2A tool proficiency (W13 — live as of 2026-05-11 / OP-218)

Per-`(agent_id, tool_id)` rows live in `agent_tool_proficiency`
(alembic 0227). Levels are derived from invocation count + success
ratio; ADR-0008 thresholds:

| Lv | Min success count | Min success ratio | Capability                                  |
|----|-------------------|-------------------|---------------------------------------------|
| 1  | 0                 | 0.00              | basic invoke (bootstrap)                    |
| 2  | 10                | 0.70              | chain 2 calls (multi-step within dispatch)  |
| 3  | 50                | 0.80              | batch ops (e.g. `write_multiple_files`)     |
| 4  | 200               | 0.85              | advanced flags + cross-Guild A2A handoff    |
| 5  | 500               | 0.90              | author new MCP wrapper                      |

The helper surface for backend callers is
`backend/agents/tool_proficiency.py`:

| Helper | Purpose |
|---|---|
| `await record_tool_invocation(store, agent_id, tool_id, outcome)` | Consumed by `mp_w17_telemetry_consumer`; increments counters + recomputes level |
| `compute_tool_level(invocation_count, success_count)` | Pure: returns Lv 1-5 per the thresholds above |
| `await can_invoke_at_level(store, agent_id, tool_id, required_level)` | Gate function — used by the tool dispatcher |
| `get_required_level(tool_id)` | Reads per-tool gate from `config/tool_proficiency_gates.yaml` |

### Gate enforcement

The tool dispatcher (`backend/agents/tool_dispatcher.py`) exposes
`ToolDispatcher.set_proficiency_gate(gate, agent_id=...)`. Wiring is
opt-in: callers that pass an `agent_id` get the W13 gate; legacy
callers without an `agent_id` bypass the gate entirely (backwards
compat). When a gate refuses, the dispatcher returns a structured
`tool_proficiency_insufficient` tool_result and emits
`tool:gate:blocked` on the SSE bus so operators see the refusal in
real time.

### Per-tool required levels

`config/tool_proficiency_gates.yaml` is the source of truth. The
shipped file lists the W17.2 hardened top-10 at Lv 1 (Read/Edit/Bash/
Grep/Glob/Write/Agent/WebFetch/Skill/ToolSearch) plus a Lv-3 sample
gate on `mcp__filesystem__write_multiple_files` so the gate-refusal
exercise on a fresh agent is reproducible. Tools absent from the
YAML default to Lv 1 (permissive); a missing file raises
`ProficiencyGateConfigMissing` at first read.

### Telemetry consumer

`backend/agents/mp_w17_telemetry_consumer.py` reads the W17.7
`tool_invocation` SSE stream (already live per OP-117) and applies
each event through `record_tool_invocation`. The consumer is
**fail-open on telemetry**: when proficiency state is more than 24h
stale the gate logs a `TelemetryConsumerLag` warning but keeps
allowing invocations. The opposite — refusing when telemetry is
behind — would block valid users on operator outages.

### Recovery

If `agent_tool_proficiency` is corrupted, run
`scripts/rpg_rebuild_tool_proficiency.py` to re-derive rows from
the `tool_invocation` log. The replay is idempotent. Use
`--dry-run` to exercise the replay logic against a static fixture
without touching the DB.

---

## Talent tree (W14 — live as of 2026-05-11 / OP-219)

Per-`(agent_id, milestone_level)` rows live in `agent_talent_choice`
(alembic 0228). The Lv-80 capstone lock lives separately in
`agent_capstone_ability` (alembic 0229). The helper surface for
backend callers is `backend/agents/talent_tree.py`:

| Helper | Purpose |
|---|---|
| `available_talents(agent_id, guild, milestone)` | Read-only: returns 3 options for the Guild × milestone from `config/talent_tree.yaml` |
| `await lock_talent(store, agent_id, guild, milestone, talent_id, *, agent_level=...)` | Idempotent + refuses re-write; gates on `agent_level >= milestone` |
| `await agent_talent_summary(store, agent_id, *, capstone_store=...)` | Full talent chain + Lv-80 capstone lock |
| `await lock_capstone_ability(...)` | Gated by Lv 80 + Lv-80 milestone talent already locked |

### Milestone gates

Locks are gated at **Lv 10 / 30 / 50 / 80** (agent.level from the W4.1
`xp_engine`, NOT W12 per-skill levels). When an agent crosses a
milestone and has no talent locked at that level yet, the
`level_up(N)` hook emits `ui:talent_choice_required` SSE — the
Character Card UI displays the "Pick required" indicator and renders
the 3 buttons returned by `GET /agents/{id}/talents/options`.

### YAML layout

`config/talent_tree.yaml` is the source of truth for the 3 options per
Guild × milestone. The drift guard in `talent_tree.py` validates the
shape at module import — a malformed YAML raises `TalentTreeError` at
boot. Today the file populates **backend** and **frontend** Guilds at
all four milestones; adding a new Guild only requires populating four
milestones + the capstone block.

### Effects of a locked talent

* **Routing weight (feature-flagged):** Tasks whose label matches the
  talent's `routing_label` get a +20% multiplier in MP routing_policy.
  Wired via `routing_policy.talent_routing_weight_multiplier()` and
  gated by `OMNISIGHT_MP_TALENT_ROUTING_ENABLED` — off by default
  until RPG.W7.1 (`prefer_agent_id`) lands.
* **System-prompt enrichment:** On every dispatch,
  `prompt_builder.enrich_system_prompt_with_talents()` appends a
  `Talent reminders (per RPG.W14):` block to the system prompt, one
  bullet per locked milestone (ordered by ascending milestone level).

### Capstone

Lv-80 unlocks a Guild capstone ability (e.g. backend Guild =
`code_archaeologist`: 1M-context legacy-code read + surgical refactor
proposal). The capstone lock is gated by **both** Lv 80 AND the Lv-80
milestone talent being already locked — attempting `POST /agents/{id}/talents/capstone`
before either gate is met returns `409 CapstoneRequiresLv80`.

### Recovery

Talent choices are operator decisions — no auto-rebuild possible.
Alembic 0228 + 0229 are forward-only. On corruption: restore from
daily Postgres backup (D15 dependency once shipped).

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/agents/{id}/talents` | Full talent chain + capstone summary |
| `GET`  | `/agents/{id}/talents/options?guild=...&milestone=...` | 3 options for a Guild × milestone |
| `POST` | `/agents/{id}/talents/lock` | Lock `{guild, milestone, talent_id, agent_level}` |
| `POST` | `/agents/{id}/talents/capstone` | Lock the Lv-80 capstone for `{guild, agent_level}` |

---

## Party / Synergy system (W17 — live as of 2026-05-11 / OP-220)

A *party* is a 2-5 agent group that takes a single Tier L+ task as a
unit. Membership lives in `agent_party` (one row per
`(party_id, member_agent_id)`) and the party-level state (name,
synergy, current active task) lives in `agent_party_state` — both
created by alembic 0230. Synergy lookup is data-driven from
`config/synergy_matrix.yaml` via `backend/agents/synergy_registry.py`.

The backend helper surface is `backend/agents/party.py`:

| Helper | Purpose |
|---|---|
| `await create_party(store, name, member_agent_ids, member_guilds=...)` | Validate 2-5 members, compute synergy, persist |
| `await assign_task(store, party_id, task_id)` | Per-task exclusivity — refuses 2nd active task with `PartyActiveTaskExists` |
| `compute_party_xp_distribution(party, total_xp, personal_xp_by_member=...)` | Even split + per-member personal XP + synergy bonus |
| `await task_complete(store, party_id, total_xp, ...)` | Composes the W17 state transition: distribute XP + release task |
| `await member_is_gated(store, agent_id)` | Pre-pickup probe consumed by `jira_dispatch.pre_pickup_ok` |

### Synergy matrix

`config/synergy_matrix.yaml` declares the cross-Guild combinations.
Three named entries are MUST per ADR-0008:

- **backend × frontend → fullstack** (+15% party XP)
- **security × devops → hardening** (+10% security-skill XP)
- **data × backend → pipeline** (+10% data-skill XP)

The file ships with ~15 entries total; pairs are order-insensitive
(internally normalised to a sorted tuple), and the registry catches
duplicates / malformed rows with `SynergyComputeFailed`. Per AC #3,
`create_party` catches that exception and degrades to a no-bonus base
XP path so a YAML edit accident does not break party creation.

### Per-task exclusivity (pre-pickup gate)

When a party holds an active task, its members cannot accept
individual tasks via the JQL pickup loop.
`backend/agents/jira_dispatch.pre_pickup_ok` accepts an optional
`party_membership_check: PartyMembershipCheck` callable — production
wires it to `party.member_is_gated` behind the asyncpg pool; the
function returns the gating `party_id`, which surfaces in the reason
string as `MemberInActiveParty:<agent_id> gated by party <party_id>
…`. Runners parse this prefix the same way they parse `mutex conflict:`
(OP-687) and fall through to the next pickup candidate.

### Operator-facing surface

`components/omnisight/agents/PartyHall.tsx` renders the active-party
grid for the operator. Each card shows member portraits, the
currently-assigned Tier L+ task (or "Idle — no active task"), and the
synergy badge from `PartyBadge.tsx`. The fetch is `GET
/agents/parties`; synergy metadata is exposed independently at `GET
/agents/parties/synergies` for the legend.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/agents/parties` | List every active (non-disbanded) party |
| `GET`  | `/agents/parties/synergies` | Full synergy matrix legend |
| `POST` | `/agents/parties` | Create a party `{name, member_agent_ids, member_guilds}` |
| `GET`  | `/agents/parties/{party_id}` | Single party + members + synergy |
| `POST` | `/agents/parties/{party_id}/task` | Assign a Tier L+ `{task_id}` (idempotent on same id) |
| `POST` | `/agents/parties/{party_id}/task/complete` | Distribute XP + release task |

### Recovery

Alembic 0230 is forward-only. Party state can be reconstructed from
task assignment history if `agent_party` corrupts (idempotent replay:
re-run `create_party` + `assign_task` from the operator log). Synergy
matrix lives entirely in YAML — replace `config/synergy_matrix.yaml`
to rebalance without code change.

---

## Skill fusion preview (W19)

Two Lv-5 skills can be combined into a hybrid Lv-3 skill. The preview
helper is exposed to the frontend via:

- `components/omnisight/agents/SkillFusionPreview.tsx` — selector + preview
- `components/omnisight/agents/FusionPreviewModal.tsx` — confirmation modal
- Backend logic: `backend/agents/skill_fusion.py`

Now that W12 is live (OP-217), the confirm button writes to
`agent_skill_state`; before W12 shipped, this was a preview-only
surface.

---

## Drift guards in CI

The RPG system ships with two drift guards that fail CI on
divergence — these are the **W11 contract** that this doc lives under:

| Guard                                                | Catches                                                              | Source                                          |
| ---------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------- |
| `assert_skill_id_space_within_matrix()`              | Any `skill_id` in `configs/**.yaml` that is missing from `skill_matrix.yaml` | `backend/agents/skill_matrix.py:154` |
| `assert_branch_choice_in_matrix(skill_id, branch_id)` | Any persisted `agent_skill_state.branch_choice` absent from `skill_matrix.yaml` `branches:` | `backend/agents/skill_matrix.py` (raises `SkillMatrixDriftError`) |
| Character Card Guild slug ⊆ `GUILD_DEFINITIONS`      | Any persisted card row with a Guild not in the registry              | `character_card.py` `CharacterCardGuildDriftError` |

Both raise on first divergence — no soft warnings. If CI is red on
either, treat the failure as an integrity issue, not a flake.

---

## Operator escalation paths

| Symptom                                            | First diagnostic                                  | If unresolved                |
| -------------------------------------------------- | ------------------------------------------------- | ---------------------------- |
| `GET /api/v1/agents/cards` returns 500             | Check `agent_character_card` table exists        | File OP ticket against RPG.W1.1 |
| `SkillMatrixDriftError` in CI                      | `git diff` `skill_matrix.yaml` vs the offending config | Add the missing `skill_id` to YAML, re-push |
| `CharacterCardGuildDriftError` on insert           | Check the Guild slug against `Guild` enum        | Update `sandbox_tier.Guild` or fix the caller |
| Agent stuck at level 1                             | Inspect runner logs for `XpDelta`; check active debuffs | If `burnout` is permanent: reset `consecutive_failures` for that agent |
| Skill leveling missing                             | W12 live as of 2026-05-11 — check alembic 0226 applied | Re-run `scripts/rpg_rebuild_skill_state.py` if rows are missing |
| Tool proficiency missing                           | W13 live as of 2026-05-11 — check alembic 0227 applied + `config/tool_proficiency_gates.yaml` parses | Re-run `scripts/rpg_rebuild_tool_proficiency.py` if rows are missing |
| Talent feature missing                             | W14 live as of 2026-05-11 — check alembic 0228/0229 applied; `config/talent_tree.yaml` present | Verify `GET /agents/{id}/talents` returns the milestone array |
| Party feature missing                              | W17 live as of 2026-05-11 — check alembic 0230 applied + `config/synergy_matrix.yaml` parses | Verify `GET /agents/parties` returns the active-party list; re-form a party via `POST /agents/parties` if rows are missing |

---

## Wave alignment with ADR-0008 (W1-W21)

This table is the explicit pairing of W1-W21 across [TODO.md Priority
RPG](../../TODO.md) and [ADR-0008 §Decision](/docs/adr/ADR-0008-agent-rpg-class-skill-leveling/#decision).
Last verified 2026-05-08 (OP-169 / RPG.W11.4) — all 21 numbers and
names align. If a future TODO edit renumbers or renames a wave, amend
ADR-0008 and this table together; the operator guide must not be the
last surface to learn about the drift.

| Wave | TODO.md heading                                        | ADR-0008 §Decision coverage                                |
| ---- | ------------------------------------------------------ | ---------------------------------------------------------- |
| W1   | Stat sheet schema + character card route               | "Identity model" + MUST table row 1                        |
| W2   | Guild + class registry                                 | "Guild" paragraph + MUST table row 2                       |
| W3   | Instance suffix + style fingerprint generator          | "Style fingerprint" + MUST table row 3                     |
| W4   | XP accrual rule + level curve                          | "XP curve" subsection                                      |
| W5   | Layer 1 (stat sheet PG) + Layer 2 (BP.M dim memory)    | "Memory hierarchy" L1 + L2 rows                            |
| W6   | Layer 3 reflection RAG                                 | "Memory hierarchy" L3 row                                  |
| W7   | Routing integration                                    | "Routing integration" subsection                           |
| W8   | Frontend Character Card panel                          | "Operator-facing surfaces" — Character Card                |
| W9   | Guild Hall view                                        | "Operator-facing surfaces" — Guild Hall                    |
| W10  | Operator-facing onboarding                             | "Operator-facing surfaces" — Onboarding                    |
| W11  | Tests + drift guards + docs                            | MUST table row 11                                          |
| W12  | Skill leveling + branching tree (**MUST**)             | "Skill leveling (W12)" subsection                          |
| W13  | MCP/A2A tool proficiency (**MUST**)                    | "MCP/A2A tool proficiency (W13)" subsection                |
| W14  | Talent tree at level milestones (**MUST**)             | "Talent tree (W14)" subsection                             |
| W15  | Buff / Debuff system (progressive)                     | "Progressively added post-v0.5.0" — W15 bullet             |
| W16  | Achievements / Badges (progressive)                    | "Progressively added post-v0.5.0" — W16 bullet             |
| W17  | Synergy / Party system (**MUST**)                      | "Party / Synergy system (W17)" subsection                  |
| W18  | Multi-class / dual-class mastery (progressive)         | "Progressively added post-v0.5.0" — W18 bullet             |
| W19  | Skill fusion / crafting (progressive)                  | "Progressively added post-v0.5.0" — W19 bullet             |
| W20  | Quest campaigns / narrative wrapping (progressive)     | "Progressively added post-v0.5.0" — W20 bullet             |
| W21  | Time-gated boss raids (progressive)                    | "Progressively added post-v0.5.0" — W21 bullet             |

---

## Related

- [ADR-0008 — Agent RPG Class & Skill Leveling](/docs/adr/ADR-0008-agent-rpg-class-skill-leveling/)
- [ADR-0007 — Multi-Provider Subscription Orchestrator](/docs/adr/ADR-0007-multi-provider-subscription-orchestrator/) — `prefer_agent_id` routing input
- [ADR-0005 — Tier S/M/L/X authority](/docs/adr/ADR-0005-tier-authority-levels/) — Tier gates feed off RPG level + skill (consumer side)
- [`docs/operations/multi-provider-setup.md`](multi-provider-setup.md) — provider-side runbook (sister doc)
- [`backend/agents/skill_matrix.yaml`](../../backend/agents/skill_matrix.yaml) — canonical skill registry
- [TODO.md Priority RPG](../../TODO.md) — full W1-W21 implementation breakdown
