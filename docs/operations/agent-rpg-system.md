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
| W4.1   | `backend/agents/xp_engine.py` — XP curve + outcome multipliers + buff/debuff multipliers + W4.4 anti-grinding clamp | **Live** (no persistence layer; callers apply XpDelta themselves) |
| W9.4   | `backend/agents/guild_hall.py` — roster view-model                                       | **Live** (consumed by Guild Hall UI) |
| W11.2  | `backend/agents/skill_matrix.py` + `skill_matrix.yaml` — canonical skill registry + drift guard | **Live**     |
| W11.3  | This document                                                 | **Live**                   |
| W15.1  | `backend/agents/buff_registry.py` — Fresh Tokens / Streak / Well-Rested  | **Live** (XP multiplier inputs) |
| W15.2  | `backend/agents/debuff_registry.py` — Burnout / Stale Memory             | **Live**                  |
| W16.1  | `backend/agents/achievement_registry.py` + unlock daemon                 | **Live**                  |
| W19.1  | `backend/agents/skill_fusion.py` — Lv5 fusion preview                    | **Live**                  |
| W20.2  | `backend/agents/campaign_progress.py` — chapter ledger                   | **Live**; alembic 0201 adds `tasks.rpg_campaign_id` + `rpg_campaign_title` |
| W3.2   | `backend/agents/style_fingerprint.py` — pure SHA-256 generator over last-N `(commit_style / test_pattern / refactor_tendency)` | **Live** (OP-129) |
| W3.3   | `backend/agents/style_fingerprint_cron.py` — daily recompute sweep + drift logging | **Live** (OP-130) — helper + drift threshold live; systemd timer wiring still owned by devops |
| W5-W7  | L1/L2/L3 memory hooks, L3 reflection RAG, routing integration | **Deferred** (W5.1 BP.M dim-memory scoping + W7.2 tier gate landed standalone — see "BP.M dim memory scoping (W5.1)" and "Tier gating (W7.2)" below) |
| W5.1   | `backend/agents/skill_memory.py` — `(agent_id, skill_id)`-tagged BP.M dim memory adapter over pgvector | **Live** (OP-137) — `vectorize_distilled_skills` + `retrieve_distilled_skills` ship; W5.2 auto-distil hook + W5.3 latency tests follow |
| W7.2   | `backend/agents/tier_gate.py` — Tier X requires Lv ≥ 50 + skill ≥ Lv 3 | **Live** (OP-147) — pure helper + async resolver; W7.1 wires the call site once `prefer_agent_id` lands |
| W12    | `backend/agents/skill_leveling.py` + alembic 0226 `agent_skill_state` | **Live** (OP-217) — branch lock + decay cron live |
| W13    | `backend/agents/tool_proficiency.py` + alembic 0227 `agent_tool_proficiency` + `config/tool_proficiency_gates.yaml` | **Live** (OP-218) — MP.W17.7 telemetry consumer + dispatcher gate live |
| W14    | `backend/agents/talent_tree.py` + `config/talent_tree.yaml` + alembic 0228/0229 | **Live** (OP-219; W14.1 OP-185) — milestone lock + capstone gate live; routing weight injection feature-flagged off until W7.1; W14.1 trigger fires `rpg.talent_fork_required` on level-up |
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

### Outcome multipliers (W4.3 / OP-134)

Per ADR-0008 §"Outcome multipliers", the W4.3 contract defines three
multiplier slots that stack multiplicatively in this fixed order:
outcome status (`success` 1.0 / `partial` 0.4 / `fail` 0.1, mutually
exclusive), Tier-L+ task (2.0×), and first-time skill use (3.0×).
W4.4 / W15 / W17 / W18 layer additional multipliers on top -- those
are listed in the wider table below for cross-reference but are not
part of the W4.3 contract.

Listed in code-application order (multiplication is commutative, so the
order does not affect the result -- but reading the table top-down
matches the sequence in `_outcome_multiplier`):

| Status                                  | ×                                              | Wave   |
| --------------------------------------- | ---------------------------------------------- | ------ |
| `success`                               | 1.0                                            | W4.3   |
| `partial`                               | 0.4                                            | W4.3   |
| `fail` / `failed`                       | 0.1                                            | W4.3   |
| `tier_l_plus = True`                    | 2.0 (multiplicative on top of outcome)         | W4.3   |
| `first_time_skill_use = True`           | 3.0                                            | W4.3   |
| Active buff IDs                         | per `buff_registry.xp_multiplier_for_buff_ids` | W15    |
| Active debuff IDs                       | per `debuff_registry.xp_multiplier_for_debuff_ids` | W15 |
| `duplicate_task_within_24h = True`      | 0.2 (anti-grinding clamp)                      | W4.4   |
| Secondary class below Lv 30             | 0.5 ramp until `SECONDARY_CLASS_FULL_XP_LEVEL` | W18.2  |
| Dual-class agent in party task          | 1.15 hybrid-synergy bump                       | W17    |

Worked example: a Tier-L+ task that exercises a brand-new skill and
completes with `partial` status earns `0.4 × 2.0 × 3.0 = 2.4×` of
`BASE_TASK_XP` -- i.e. `floor(100 × 2.4) = 240` XP, before any W15
buff/debuff or W4.4 anti-grind layering.

The `fail` / `failed` duplication is intentional: the runner emits
either spelling depending on which call site classifies the outcome,
and `xp_engine` accepts both with the same 0.1× multiplier. Operators
adding a new outcome value MUST update both keys in lock-step.

Buff / debuff multipliers come from
`buff_registry.xp_multiplier_for_buff_ids()` and
`debuff_registry.xp_multiplier_for_debuff_ids()`; the active set is
passed in `TaskOutcome.active_buff_ids` / `active_debuff_ids` by the
runner.

`award_xp(agent_id, task_outcome) → XpDelta` is **pure** — no DB
writes, no clock reads. The caller is responsible for applying the
returned delta to the character card. Today, that caller is internal
runner code only; there is no operator endpoint to award XP by hand.

### Anti-grinding (W4.4 / OP-135)

Per ADR-0008 §"XP curve", repeating an identical task to farm XP is
clamped to a flat **×0.2** multiplier (an 80% haircut) when the runner
detects the *same canonical task hash* has already paid out to this
agent within the **last 24h**. This is the W4.4 contract — the
anti-grind term in the eight-step stack listed under "Outcome
multipliers" above.

Source-of-truth constants:

| Layer                | Constant                                | Where                                            |
| -------------------- | --------------------------------------- | ------------------------------------------------ |
| Agent-level XP (W4.1)| `DUPLICATE_TASK_MULTIPLIER = 0.2`       | `backend/agents/xp_engine.py`                    |
| Skill XP (W12)       | `ANTI_GRIND_MULTIPLIER = 0.2`           | `backend/agents/skill_leveling.py`               |

The two constants are deliberately parallel — the same anti-grind
contract is applied at both the W4.1 character-level XP path and the
W12 per-skill XP path. Any future tweak to the multiplier MUST update
both files in the same change set; there is no shared import because
the two engines are otherwise decoupled.

Input contract — the flag is **runner-computed**, not engine-computed:

- `xp_engine.award_xp(...)` reads `TaskOutcome.duplicate_task_within_24h`
  (also accepted as `same_task_hash_within_24h` on the mapping/dict
  shape — the two spellings are aliases and both round-trip through
  `_normalise_task_outcome`).
- `skill_leveling.compute_xp_delta(...)` and `award_skill_xp(...)`
  accept `same_task_hash_within_24h=...` directly.
- Neither helper reads a clock or queries a task-history table; the
  *decision* of whether two tasks share a canonical hash and whether
  the 24h window is open lives in the runner's dispatch path, not in
  the XP engines (this is what makes both `award_xp` and
  `compute_xp_delta` deterministic / unit-testable).

Stacking — anti-grind is **multiplicative** and slots in *after* W15
buffs and debuffs but *before* the W18 secondary-class ramp and the
W17 hybrid-synergy bump (see the eight-step table in "Outcome
multipliers (W4.3 / OP-134)" above).

Worked example: a brand-new-skill `success` task on a Tier-L+ ticket
that *would* have earned `floor(100 × 1.0 × 2.0 × 3.0) = 600` XP earns
only `floor(100 × 1.0 × 2.0 × 3.0 × 0.2) = 120` XP if the same task
hash was already awarded to this agent inside the rolling 24h window.
The first-time-skill bonus stacks but the anti-grind clamp dwarfs it
— intentional, because re-running the same task is exactly the
exploit the clamp guards against.

Edge cases the operator may hit:

- **A task that legitimately recurs every day** (e.g. a daily report).
  The clamp will haircut it ×0.2 on the second-and-later run inside
  any 24h window. This is by design — the W4.4 contract is hash-only,
  not intent-only. If you need a recurring task to pay full XP, give
  each instance a distinct task body (or change the hashing rule
  upstream; the XP engine has no opinion).
- **The flag is `True` but the task differs subtly.** The hash is
  computed runner-side; if the operator believes the clamp fired on a
  *different* task, the discrepancy is in the hashing rule, not in
  `xp_engine`. Reproduce by calling `award_xp` directly with the
  observed `TaskOutcome` — the multiplier line in the returned
  `XpDelta` will show the exact stack.
- **Skill XP shows the clamp but agent XP does not (or vice versa).**
  Both engines accept the flag independently; the runner is
  responsible for passing it to both paths consistently. If only one
  side fires, look at the runner's dispatch site, not at the engines.

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

## Style fingerprint daily recompute (W3.3 — live as of 2026-05-16 / OP-130)

The per-instance Character Card carries a `style_fingerprint` —
ADR-0008 §"Style fingerprint" describes it as a daily-recomputed
SHA-256 over the last N tasks' `(commit_style / test_pattern /
refactor_tendency)` tuple. The W3.2 generator
(`backend/agents/style_fingerprint.py`) is the pure hash; the W3.3
helper (`backend/agents/style_fingerprint_cron.py`) is the
cron-side sweep that walks every card, recomputes the hash, logs
per-agent drift, and writes the fresh value back when it changed.

### Public helpers

| Helper | Purpose |
|---|---|
| `style_drift_ratio(samples)` | Pure: fraction of canonical tuples in a window that differ from the dominant tuple. Range `[0.0, 1.0)`; `0.0` = uniform style, approaches `1.0` as the window splits across many distinct styles. |
| `await recompute_style_fingerprints(card_store, task_history_provider, *, drift_threshold=0.5, last_n=20)` | The sweep itself. Walks every Character Card, recomputes via W3.2, persists when changed, emits one log line per card, returns a tuple of `StyleDriftReport` for caller-side audit. |
| `summarize_drift(reports)` | Aggregate counters (`visited / unchanged / drifted / above_threshold`) for operator dashboards. |

### Drift threshold semantics

The hash is binary — it either matches or doesn't — so the
threshold sits on top of an axis-level metric, **not** on the hash
itself. `style_drift_ratio` counts how many of the N tasks in the
window carry a canonical tuple that differs from the window's
*dominant* canonical tuple, divided by the window size. A fresh
fingerprint that has zero canonical-tuple variance (e.g. the
rolling window slid by one task but every sample shares the same
style) is still a real fingerprint change — but it logs at `INFO`
because the agent's style isn't actually drifting; only its
position in history is.

| Outcome | Log level | Log message |
|---|---|---|
| Hash matched the card | `INFO` | `style_fingerprint_unchanged` |
| Hash changed, drift ratio `< drift_threshold` | `INFO` | `style_fingerprint_drift` |
| Hash changed, drift ratio `>= drift_threshold` | `WARNING` | `style_fingerprint_drift_above_threshold` |

The default `drift_threshold = 0.5` means: at least half the
last-N window has to disagree with the dominant style before the
sweep escalates. Operators tuning the noise can pass a different
threshold without touching W3.2 — the hash contract is unchanged.

### Wiring

The module is intentionally IO-free. The runner-side glue:

1. Build an asyncpg-backed `CharacterCardStore` against
   `agent_character_card` (same store W1 uses).
2. Implement a `task_history_provider: agent_id -> Sequence[TaskStyleSignals]`
   that pulls the last 20 completed tasks' commit / test / refactor
   signals from the runner's task ledger.
3. `await recompute_style_fingerprints(store, provider)` once a
   day from a cron entrypoint (devops-owned `.timer` unit — not
   shipped in this ticket; until that lands, operators can invoke
   the sweep ad-hoc from a Python shell against the prod DB pool).

The sweep is idempotent: re-running back-to-back is a no-op on the
second pass because all fingerprints already match. There is no
"force" mode — to recompute against a different `last_n` window,
pass `last_n=...` explicitly.

### Edge cases the sweep handles by design

- **Card with empty `style_fingerprint`** → first run populates
  it; the per-card log line is `style_fingerprint_drift` (not
  `_above_threshold`) when the new window is internally uniform.
- **Agent with zero task history** → `compute_style_fingerprint`
  returns `""`; if the stored fingerprint is also `""` the sweep
  no-ops with `drift_ratio=0.0`. Once the agent's first task
  lands, the next sweep populates the field.
- **Last-N exceeding history length** → the window is silently
  clamped to whatever the provider returns; `samples_considered`
  on the report tells operators how many tasks actually
  contributed.

### Recovery

The fingerprint is fully derivable from task history; corruption
recovers on the next sweep. There is no persisted drift log
beyond the structlog stream — operator dashboards consuming the
JSON log (`style_fingerprint_drift_above_threshold` events) are
the durable surface.

---

## Skill leveling (W12 — live as of 2026-05-11 / OP-217)

Per-`(agent_id, skill_id)` rows live in `agent_skill_state` (alembic
0226). The helper surface for backend callers is
`backend/agents/skill_leveling.py`:

| Helper | Purpose |
|---|---|
| `await award_skill_xp(store, agent_id, skill_id, delta=..., outcome=..., …)` | Apply XP delta with outcome / Tier-L+ / first-time / anti-grind multipliers |
| `compute_level(xp)` | Pure: returns Lv 1-5 per the W12.2 curve below |
| `await lock_branch_choice(store, agent_id, skill_id, branch)` | Idempotent + refuses re-write (immutable at Lv 3) |
| `await teach_other_agent(store, teacher, student, skill_id)` | Lv-5 only; one-shot +25 XP injection, 7-day cooldown |
| `await teach_distilled_summary(store, …, embedder, vector_store)` | W12.6 (OP-175): same-Guild + idle gate around `teach_other_agent` plus distilled-summary write into BP.M dim memory |
| `await decay_idle_skills(store, now=...)` | Sweep: 5%/week on rows idle >= 30 days |

### Skill XP curve (W12.2 / OP-171)

Per-skill cumulative XP thresholds in task-success-tokens. Each
threshold is the XP at which an `(agent_id, skill_id)` row *enters*
that level; the fifth point is the Lv-5 decay floor, not a level gate.

| Skill Lv | Threshold (cumulative XP) | Source symbol                                     |
| -------- | ------------------------- | ------------------------------------------------- |
| 1        | 0                         | `LEVEL_THRESHOLDS[1]`                             |
| 2        | 25                        | `LEVEL_THRESHOLDS[2]` — unlocks `extended_thinking_enabled` |
| 3        | 100                       | `LEVEL_THRESHOLDS[3]` — unlocks `parallel_subtask_enabled`; Lv-3 branch fork required |
| 4        | 250                       | `LEVEL_THRESHOLDS[4]` — unlocks `prompt_overhead_reduced` |
| 5        | 600                       | `LEVEL_THRESHOLDS[5]` — unlocks `teach_other_agent` |
| 5 (cap)  | 1500                      | `LEVEL_5_CAP_THRESHOLD` — decay floor; cannot drop below `LEVEL_5_CAP_THRESHOLD - 1` |

Computed by `compute_level(xp)` in
`backend/agents/skill_leveling.py`. The curve is intentionally short
(only 5 levels) so that the per-skill mastery surface stays operator-
legible — long-tail progression lives on the agent-level XP curve
(W4.2 / OP-133), not here. Skill XP is incremented by
`award_skill_xp(...)` after multiplier stacking; the agent-level XP
curve (`xp_engine.award_xp`) is a *separate* engine and the two never
share thresholds.

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

### Branching tree (W12.4 / OP-173)

At Lv 3 every base skill forks into **exactly two** branches declared
under `branches:` in `skill_matrix.yaml`. The operator picks one from
the Character Card "Skills" tab; the choice is immutable per
`(agent_id, skill_id)` row (alembic 0226). Worked example: the
`backend.enterprise_web` skill forks into `perf_tuning` /
`type_correctness` — once an operator locks `perf_tuning`, that row
cannot be re-pointed to `type_correctness`; a fresh fork requires a
new agent instance (per `SkillBranchAlreadyLocked`).

The "exactly two" invariant is enforced at `skill_matrix.yaml` load
time by `BRANCHES_PER_SKILL = 2` in
`backend/agents/skill_matrix.py`. A YAML edit that adds a third
option, drops one, or otherwise diverges fails module import with
`SkillMatrixError` — the assertion fires before any router can serve
a stale matrix.

#### Operator picker flow

| Step                                              | Surface                                                                   |
| ------------------------------------------------- | ------------------------------------------------------------------------- |
| Skill XP crosses the Lv-3 entry threshold (100)   | `award_skill_xp` returns `branch_choice_required=True` on the `SkillXpAward` |
| Character Card "Skills" tab fetches state         | `GET /api/v1/agents/{agent_id}/skills` — `branch_choice_required: true` is the picker trigger |
| Operator picks one of the two declared branches   | UI lists `canonical_branches_for_skill(skill_id)` from `skill_matrix.yaml` |
| Frontend POSTs the choice                         | `POST /api/v1/agents/{agent_id}/skills/{skill_id}/branch` body `{"branch": "perf_tuning"}` |
| Backend persists immutably                        | `lock_branch_choice(store, agent_id, skill_id, branch)` → `agent_skill_state.branch_choice` |

Idempotency: re-POSTing the *same* branch returns 200 with the
existing row unchanged. POSTing a *different* branch returns 409
(`SkillBranchAlreadyLocked`). A branch that is not declared in the
YAML returns 422 (`SkillMatrixDriftError`).

#### Source symbols (per W12.4 attribution)

| Symbol                                                          | Source                                       |
| --------------------------------------------------------------- | -------------------------------------------- |
| `BRANCH_LOCK_LEVEL = 3`                                         | `backend/agents/skill_leveling.py`            |
| `BRANCHES_PER_SKILL = 2`                                        | `backend/agents/skill_matrix.py`              |
| `lock_branch_choice(store, agent_id, skill_id, branch)`         | `backend/agents/skill_leveling.py`            |
| `SkillBranchAlreadyLocked` (409 on differing re-write)          | `backend/agents/skill_leveling.py`            |
| `canonical_branches_for_skill(skill_id) → (Definition, …)`      | `backend/agents/skill_matrix.py`              |
| `assert_branch_choice_in_matrix(skill_id, branch_id)`           | `backend/agents/skill_matrix.py`              |
| `CharacterSkillEntry.branch_choice_required`                    | `backend/agents/character_card.py`            |
| `POST /agents/{id}/skills/{skill_id}/branch`                    | `backend/routers/agents.py`                   |

### Distilled-summary teach (W12.6 / OP-175)

The W12.3 `teach_other_agent` primitive owns the per-skill `+25 XP`
+ `7-day cooldown` + `Lv 5 teacher → Lv ≤ 2 student` semantics.
ADR-0008 §"Skill leveling (W12)" line 140 narrows that primitive to
*same-Guild* pairs, requires the teacher be *idle* at the moment of
injection, and pairs the XP delta with a *distilled summary* write
into BP.M dim memory. W12.6 ships that thicker contract as
`teach_distilled_summary` in `backend/agents/skill_teaching.py` —
the W12.3 primitive remains the atomic engine underneath.

| Gate                                  | Where it lives                                    | What it does                                                                  |
| ------------------------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------- |
| Same-Guild check                      | `_require_guild` + `TeachGuildMismatch`           | Both card guilds must resolve to the same `GUILDS` slug; pre-write gate       |
| Teacher idle gate                     | `_assert_teacher_idle` + `TeachTeacherNotIdle`    | `now - teacher_last_dispatch_at >= TEACH_IDLE_MIN_SECONDS` (default 30 min)   |
| Atomic +25 XP + cooldown stamp        | `teach_other_agent` (W12.3 primitive)             | Lv 5 teacher / Lv ≤ 2 student / 7-day cooldown / `last_taught_at` UPSERT      |
| Distilled summary into BP.M dim memory | `vectorize_distilled_skills` (W5.1)              | One pgvector upsert tagged `(student_agent_id, skill_id, source_skill_draft_id?)` |

Ordering invariant: the W12.3 XP write commits *before* the summary
upsert. A partial failure therefore leaves the +25 XP awarded with
`summary_written=False` on the returned `DistilledTeachOutcome` — the
caller can replay the summary write without re-running the teach.
The reverse — summary written but XP refused — never happens, because
the teach primitive is the gate.

#### Source symbols (per W12.6 attribution)

| Symbol                                                   | Source                                  |
| -------------------------------------------------------- | --------------------------------------- |
| `TEACH_IDLE_MIN_SECONDS = 1800`                          | `backend/agents/skill_teaching.py`       |
| `teach_distilled_summary(skill_store, …)`                | `backend/agents/skill_teaching.py`       |
| `TeachGuildMismatch` / `TeachTeacherNotIdle`             | `backend/agents/skill_teaching.py`       |
| `DistilledTeachOutcome.summary_written`                  | `backend/agents/skill_teaching.py`       |
| `teach_other_agent` (atomic XP + cooldown gate)          | `backend/agents/skill_leveling.py`       |
| `vectorize_distilled_skills` (BP.M dim memory write)     | `backend/agents/skill_memory.py`         |
| `GUILDS` (same-Guild registry source of truth)           | `backend/agents/guild_registry.py`       |

#### Frontend visualization (W12.7 / OP-176)

The Character Card "Skills" tab (the second tab on the card, per
ADR-0008) renders each forkable skill as an explicit **branching
tree** rather than a flat button row. The tree makes the structure
of the W12.4 fork legible at a glance: the operator can see the
parent skill, the Lv-3 fork point, and the two canonical leaves
side-by-side, with the locked branch highlighted and the unchosen
alternate dimmed.

Component: `components/omnisight/agents/SkillBranchTree.tsx`. It is
nested inside the Skills section (`components/omnisight/agents/CharacterCard.tsx`)
so the existing W8 identity / level / specialization shell remains
the first tab and the tree lives on the second tab alongside the
per-skill XP bars.

| Tree node                                                     | Test ID                                          | State encoding |
| ------------------------------------------------------------- | ------------------------------------------------ | -------------- |
| Tree container (also serves as picker for prior W12.4 tests)  | `character-card-skill-branch-picker`             | `data-branch-tree-locked`, `data-branch-tree-pickable` |
| Tree body                                                     | `character-card-skill-branch-tree`               | — |
| Root node (parent skill_id + Lv)                              | `character-card-skill-branch-tree-root`          | `data-skill-id` |
| Fork node (anchors the Lv-3 split point)                      | `character-card-skill-branch-tree-fork`          | — |
| Leaf node × 2 (canonical branches from `skill_matrix.yaml`)   | `character-card-skill-branch-option`             | `data-branch-id`, `data-branch-leaf-chosen`, `data-branch-leaf-alternate`, `aria-selected` |
| Immutability prompt (only when a pick is required)            | `character-card-skill-branch-tree-prompt`        | — |

Tree state encoding for a skill with two declared branches:

| `branchChoice` | `branchChoiceRequired` | Tree behaviour |
| -------------- | ---------------------- | -------------- |
| `null`         | `true`                 | Both leaves clickable; prompt visible (operator must pick) |
| set            | `false`                | Both leaves disabled; chosen leaf highlighted, alternate dimmed (W12.4 immutability) |
| `null`         | `false`                | Both leaves disabled (skill below Lv 3 — fork not yet reached) |

The tree renders only when `branch_options` carries `≥ 2` entries —
a level-2 skill row therefore collapses cleanly to its XP bar, with
no tree shell.

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

### Feature unlock gating (W13.3 / OP-180)

The "drives feature-unlock gating: a low-Lv agent literally cannot
call high-Lv-only flags" clause in ADR-0008 §"MCP/A2A tool
proficiency (W13)" is implemented as a thin production-wiring layer
on top of `get_required_level` + `can_invoke_at_level`. The W12.4
attribution pattern applies: the helper surface for backend callers
adds two functions but no new persistence, no new YAML, no new
telemetry.

| Helper | Purpose |
|---|---|
| `build_feature_unlock_gate(store, *, config_path=None, now=None)` | Returns a closure `async (tool_name, agent_id) -> bool` that reads the YAML per-call and consults `can_invoke_at_level` against the per-agent row |
| `install_feature_unlock_gate(dispatcher, *, store, agent_id, config_path=None, now=None)` | Builds the closure and installs it on `dispatcher.set_proficiency_gate(...)` |

Production wiring per agent dispatch (api-anthropic agent class —
the W13 owner per ADR-0008 §"Implementation split"):

```python
from backend.agents.tool_dispatcher import get_default_dispatcher
from backend.agents.tool_proficiency import (
    PostgresToolProficiencyStore,
    install_feature_unlock_gate,
)

store = PostgresToolProficiencyStore(conn_factory=app_pool.acquire)
install_feature_unlock_gate(
    get_default_dispatcher(),
    store=store,
    agent_id=current_agent_id,
)
```

The canonical W13.3 sample is the Lv-3 batch op:
`mcp__filesystem__write_multiple_files: 3` in
`config/tool_proficiency_gates.yaml`. A fresh agent at Lv 1 on that
tool gets a structured `tool_proficiency_insufficient` `tool_result`
from the dispatcher (and a `tool:gate:blocked` SSE event), forcing
the agent to demonstrate single-file `Write` competence first. Tools
absent from the YAML stay at Lv 1 required, so adding the gate to a
new MCP tool is a one-line YAML edit — the install helper does not
need to be re-touched.

Source symbols (per W13.3 attribution):

| Symbol | File |
|---|---|
| `build_feature_unlock_gate` | `backend/agents/tool_proficiency.py` |
| `install_feature_unlock_gate` | `backend/agents/tool_proficiency.py` |
| `ToolDispatcher.set_proficiency_gate` (callee) | `backend/agents/tool_dispatcher.py` |
| `mcp__filesystem__write_multiple_files: 3` (canonical sample) | `config/tool_proficiency_gates.yaml` |

### Tool proficiency bars on the Character Card (W13.6 / OP-183)

ADR-0008 §"Implementation split" line 191 lists "tool proficiency
bars" as the third conceptual tab on the Character Card panel
("Stats / Skills / Tools"). W13.6 is the *frontend* surface for the
W13 backend: each row in the Tools section is one
`agent_tool_proficiency` row (alembic 0227) rendered as a Lv header
+ invocation counter + success-rate bar + gate-blocked banner.

The bars ship as an inline section on the W8.1 Character Card shell
(rather than a real tab panel) — the section's `data-testid`
strings already encode the conceptual tab id so a future tabbed
refactor only re-parents the existing JSX. The W12.4 attribution
pattern applies: no new persistence, no new API, no new YAML —
W13.6 is the read-only render of the data
`/api/v1/agents/{agent_id}/tools` already serves from the W13
ship.

| Row element                                | Surface                                                                |
| ------------------------------------------ | ---------------------------------------------------------------------- |
| Per-tool Lv header                         | `CharacterTool.level` (Lv 1-5 per W13 thresholds)                      |
| Invocation count + success-rate text       | `CharacterTool.invocationCount` / `CharacterTool.successCount`         |
| Success-rate progress bar                  | `<div role="progressbar" aria-valuenow="…">` (percent, clamped 0-100)  |
| Gate-blocked banner ("requires Lv N")      | Visible when `level < requiredLevel`; sourced from the W13.3 YAML gate |
| `data-tool-blocked` attribute              | `"true"` mirrors the dispatcher's `tool_proficiency_insufficient` SSE  |

Operator-facing invariant: rows that the dispatcher would refuse
**still render** — the banner explains the refusal rather than
hiding the tool. This is what keeps the SSE `tool:gate:blocked`
event interpretable on the Character Card: an operator who sees
"refused" on the SSE bus can scroll the panel and find the row
that names the same `tool_id` with the gate level called out.

Source symbols (per W13.6 attribution):

| Symbol | File |
|---|---|
| `CharacterTool` (row interface) | `components/omnisight/agents/CharacterCard.tsx` |
| `data-testid="character-card-tools"` (section anchor) | `components/omnisight/agents/CharacterCard.tsx` |
| `data-testid="character-card-tool"` (per-row anchor) | `components/omnisight/agents/CharacterCard.tsx` |
| `data-testid="character-card-tool-gate-blocked"` (banner anchor) | `components/omnisight/agents/CharacterCard.tsx` |
| `getLevelProgressPercent` (shared 0-100 clamp) | `components/omnisight/agents/CharacterCard.tsx` |
| Contract tests (lock the row shape + gate banner) | `test/components/character-card-tools.test.tsx` |

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
| `milestones_crossed(previous_level, new_level)` | W14.1 — pure helper: which milestones a level-up transition crossed (ascending order; `()` when `new_level <= previous_level`) |
| `pending_milestone_forks(agent_level, choices)` | W14.1 — pure helper: milestones the agent has reached but not yet locked a talent for; drives the picker modal |

### Milestone gates (W14.1)

Locks are gated at **Lv 10 / 30 / 50 / 80** (agent.level from the W4.1
`xp_engine`, NOT W12 per-skill levels). When `CharacterCardRegistry.update_card`
raises an agent's level past one of these gates, the `_emit_level_up_safely`
hook in `backend/agents/character_card.py` walks
`talent_tree.milestones_crossed(previous, new)` and emits one
**`rpg.talent_fork_required`** SSE per milestone crossed via
`backend.events.emit_rpg_talent_fork_required`. Payload shape:

```json
{
  "agent_id": "agent-codex-alpha",
  "milestone": 30,
  "agent_level": 31,
  "guild": "backend",
  "toast": {
    "title": "Talent fork unlocked",
    "message": "agent-codex-alpha reached Lv 30 — pick a talent."
  }
}
```

The Character Card "Talents" tab subscribes to this event and renders
the W14.5 picker modal that blocks task assignment until the operator
commits a pick via `POST /agents/{id}/talents/lock`. On cold reads
(no SSE replay) the same gate state is reconstructed from the
`pending_milestone_forks` array in `GET /agents/{id}/talents` —
the endpoint joins the talent rows against the character card's
`level` column and runs `pending_milestone_forks(agent_level,
summary.choices)` so the UI can light up the picker even if the
operator missed the live event.

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

## BP.M dim memory scoping (W5.1 — live as of 2026-05-16 / OP-137)

ADR-0008 §"Memory hierarchy" pins Layer 2 (distilled skills) on
*BP.M dim memory tagged with `(agent_id, skill_id)`* with a top-K
vector lookup. `backend/agents/skill_memory.py` is the canonical
adapter for that rule. It wraps each distilled summary as a
tenant-scoped `VectorDocument` over the existing BP.Q
`embedding_chunks` pgvector table; **no new schema lands with this
row** — the L2 storage reuses the column shape from BP.Q.4 and W6
reflection RAG, with `kind` / `agent_id` / `skill_id` in the `metadata`
JSONB column so the `metadata @>` filter can pin retrieval.

W5.2 owns the trigger that auto-distils a ≤200-token summary on
`lessons_learned` write; W5.3 owns the latency-budget tests (L1 <
50ms, L2 < 300ms). W5.1 only ships the data model + scoping API.

### Public helpers

| Helper | When to use |
|---|---|
| `DistilledSkillMemoryEntry(tenant_id, agent_id, skill_id, summary, source_skill_draft_id=None, metadata={})` | Construct one entry. `source_skill_draft_id` ties the row back to a BP.M.1 `auto_distilled_skills` review-queue row when the summary was distilled there; omit it when W5.2 distils directly from `lessons_learned`. |
| `await vectorize_distilled_skills(entries, *, embedder, store) -> int` | Embed and upsert a one-tenant batch. Mixed-tenant batches are rejected to keep the BP.Q tenant-scope invariant; empty input is a no-op. |
| `await retrieve_distilled_skills(*, tenant_id, query_text, embedder, store, agent_id=None, skill_id=None, top_k=5)` | Run a top-K semantic query, scoped by `agent_id` and/or `skill_id`. `kind` is always pinned so other vector payloads (BP.Q content RAG, W6 reflection summaries) cannot leak into a skill retrieval. |
| `pgvector_skill_memory_store(conn_or_pool)` | Build a `PgvectorStore` against the default `embedding_chunks` table. Same adapter the W6 reflection RAG uses; no new table. |

### Scoping semantics

`agent_id` and `skill_id` are *optional and additive* on retrieval:

- Both `None` → returns every distilled skill in the tenant
  (`kind = distilled_skill_summary` only).
- `agent_id` set → one agent's accumulated skill library.
- `skill_id` set → cross-agent distillations for a single skill
  (useful for W5-W17 teach / synergy flows that ask "what does the
  fleet know about `python`?").
- Both set → the intersection — what *this* agent has learned about
  *this* skill.

The `source_path` for each row is
`distilled-skill://<agent_id>/<skill_id>`, so callers can also reach
the BP.Q list/delete-by-source-path surface to bulk-evict one
`(agent_id, skill_id)` pair without re-walking metadata.

### Chunk identity

Each entry's `chunk_id` is
`distilled-skill:<tenant_id>:<agent_id>:<skill_id>:<identity>` where
`<identity>` is the `source_skill_draft_id` when supplied, falling
back to a 16-hex-char SHA-256 of the trimmed summary. This makes
re-vectorising the same draft idempotent (the upsert lands on the
same row) while allowing multiple distinct summaries to coexist for
the same `(agent_id, skill_id)` pair when callers omit the source id.

### Why no new schema

The L2 storage is **reuse, not extension**:

- The pgvector table (`embedding_chunks`, BP.Q.4) already carries
  `tenant_id` + `metadata JSONB` + RLS enforcement + the `metadata
  @>` GIN index path; W6 reflection RAG already established the
  `kind`-pinned-metadata pattern for multi-payload coexistence.
- Adding columns for `agent_id` / `skill_id` would force an alembic
  migration, an RLS revision, and a drift guard — none of which buy
  retrieval semantics that the metadata filter already gives us.
- W5.1's `kind = "distilled_skill_summary"` is reserved alongside W6's
  `kind = "reflection_summary"` in the same table. New kinds added
  later (Lv-5 teach injections, fusion-skill drafts) must pick a
  distinct `kind` value so retrievers can pin them without leaking.

### Constants

The kind tag and source prefix are exported so call sites and operator
dashboards reference the same strings:

- `DISTILLED_SKILL_RAG_KIND = "distilled_skill_summary"`
- `DISTILLED_SKILL_SOURCE_PREFIX = "distilled-skill://"`
- `DEFAULT_DISTILLED_SKILL_TOP_K = 5`

W5.3 will assert the `< 300ms` retrieval budget against the same
helper; the budget is a *contract* for this layer, not a soft target,
and changing it requires an ADR-0008 amendment.

---

## Tier gating (W7.2 — live as of 2026-05-16 / OP-147)

ADR-0008 §"Routing integration" pins one Tier-X rule:

> Tier X tasks require Lv ≥ 50 + relevant skill ≥ Lv 3

`backend/agents/tier_gate.py` is the canonical home for that rule.
The module ships pure helpers plus an async resolver that fetches
inputs from the W1 character-card store and the W12 skill-state
store; Tier S / M / L are an unconditional pass (ADR-0008 places no
explicit floor on them — W7.3 will layer the BP.C T-shirt minimum-
level table on top later without touching the Tier X policy).

### Public helpers

| Helper | When to use |
|---|---|
| `is_eligible_for_tier(*, tier, agent_level, skill_level) -> bool` | You already hold both levels in hand. |
| `tier_gate_unmet_reasons(*, tier, agent_level, skill_level) -> tuple[str, ...]` | You want to log / surface *why* a candidate failed. Reason strings are stable for telemetry — format `tier_x_agent_level_below_50:46`. |
| `assert_eligible_for_tier(*, tier, agent_level, skill_level, agent_id=None, skill_id=None)` | You want the call site to throw on violation; raises `TierGateViolation` carrying the structured reasons. |
| `await evaluate_tier_gate(card_store, skill_store, *, agent_id, tier, skill_id) -> TierGateDecision` | You only have the `agent_id` / `tier` / `skill_id` triple; this fetches both rows and returns a structured decision. |

### Pre-pickup wire-up

The clean attachment point is the pre-pickup / dispatch path — any
caller resolving a candidate `agent_id` for a task can call
`evaluate_tier_gate` and consult `TierGateDecision.eligible` before
handing the task off. Tier S / M / L short-circuit without touching
either store, so the gate is cheap to call unconditionally; only the
Tier X branch reads the W1 + W12 rows.

The pure helpers are deliberately importable without an event loop so
synchronous routing-policy code that already holds the levels in hand
(e.g. W7.1's `prefer_agent_id` site once it lands) can compose them
without crossing the sync/async boundary.

### Edge cases the gate handles by design

- **Missing character card** → `eligible=False` with reason
  `tier_x_character_card_missing:<agent_id>`. An agent with no card
  has no level and is not a legal Tier X target.
- **Tier X without a `skill_id`** → `eligible=False` with reason
  `tier_x_skill_id_missing`. ADR-0008 names the second floor as "該
  skill" — the caller must say which one.
- **Missing skill-state row** → treated as Lv 0, fails the `≥ 3`
  floor. The W12 store does not auto-create rows on first use; an
  agent who has never accrued XP on a skill is not Tier X-eligible
  for it.
- **Tier S / M / L** → unconditional pass, regardless of card or
  skill state. Future W7.3 work layers a per-Tier minimum-level
  table on top; that landing point must not regress the W7.2
  contract for Tier X.

### Constants

The two floors are exported as module-level constants so call sites
and operator dashboards reference the same numbers:

- `TIER_X_MIN_AGENT_LEVEL = 50`
- `TIER_X_MIN_SKILL_LEVEL = 3`

A future ADR amendment that moves the floors must update the
constants in `tier_gate.py` and the ADR-0008 line in the same change
— the helpers carry no fallback for stale values.

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
| Style fingerprint never refreshes                  | W3.3 live as of 2026-05-16 — check the daily cron wrapper is invoking `recompute_style_fingerprints`; inspect the structlog stream for `style_fingerprint_unchanged` / `_drift` events | Run the sweep ad-hoc from a Python shell against the prod DB pool; persistent absence of log lines means the cron isn't wired |
| `style_fingerprint_drift_above_threshold` spikes   | Cross-reference the agent_id with recent task history — half the window disagreeing with the dominant style is the threshold | Lower `drift_threshold` only if the agent legitimately mixes styles; otherwise treat as a routing-quality signal and open a ticket |

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
| W5   | Layer 1 (stat sheet PG) + Layer 2 (BP.M dim memory; W5.1 scoping live via OP-137) | "Memory hierarchy" L1 + L2 rows                            |
| W6   | Layer 3 reflection RAG                                 | "Memory hierarchy" L3 row                                  |
| W7   | Routing integration (W7.2 tier gate live via OP-147)   | "Routing integration" subsection                           |
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
