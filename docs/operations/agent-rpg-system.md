# Agent RPG System — Operator Guide

> **Status**: v0.5.0 partial ship (RPG.W1-W11 core + W15-W16 + W19-W20
> shipped; W12-W14 + W17 + W18 + W21 deferred). Last reviewed 2026-05-08.
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
| W12-W14, W17 | Skill leveling tables, MCP/A2A proficiency, talent tree, party tables | **Deferred** — schema not yet migrated |

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
| `tool_id` namespace | MCP server registry + A2A tool catalog                                                            | Reader (W13 deferred)    |
| Synergy matrix      | `backend/agents/synergy_registry.py` (W17 deferred — module not yet present)                       | Owner (when W17 lands)   |

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

**Don't, unless you know there are no rows in `agent_skill_state`
(W12, deferred) yet.** Once W12 ships, a `skill_id` is part of a
primary key — renames need a data migration, not a YAML edit.

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

```python
level_threshold(N) = ceil(100 * N ** 1.4)        # cumulative XP to reach Lv N
MAX_LEVEL = 80                                    # hard cap; Lv 80+ XP is discarded
BASE_TASK_XP = 100                                # per task, before multipliers
```

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

- *"Why did Lv 50 → 51 take so long?"* — `100 × 51 ** 1.4 ≈ 21,200`,
  vs `100 × 50 ** 1.4 ≈ 20,560`. The curve is sigmoid-flat by design.
- *"An agent's level dropped."* — Levels never drop. Skill XP (W12,
  deferred) decays toward but cannot cross a level threshold; the
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

## Skill fusion preview (W19)

Two Lv-5 skills can be combined into a hybrid Lv-3 skill. Today this
is a **preview-only** surface — the fusion does not actually create a
new `agent_skill_state` row (W12, deferred). The preview helper is
exposed to the frontend via:

- `components/omnisight/agents/SkillFusionPreview.tsx` — selector + preview
- `components/omnisight/agents/FusionPreviewModal.tsx` — confirmation modal
- Backend logic: `backend/agents/skill_fusion.py`

When W12 lands, the modal's confirm button will write to the skill
state table. Until then it shows a deterministic preview only.

---

## Drift guards in CI

The RPG system ships with two drift guards that fail CI on
divergence — these are the **W11 contract** that this doc lives under:

| Guard                                                | Catches                                                              | Source                                          |
| ---------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------- |
| `assert_skill_id_space_within_matrix()`              | Any `skill_id` in `configs/**.yaml` that is missing from `skill_matrix.yaml` | `backend/agents/skill_matrix.py:154` |
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
| Skill leveling / talent / party feature missing    | These are W12-W14 / W17 — deferred post-v0.5.0   | Don't promise the feature; track in TODO Priority RPG |

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
