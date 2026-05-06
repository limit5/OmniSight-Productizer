# ADR 0008 — Agent RPG Class & Skill Leveling System

**Status**: Accepted (2026-05-06)

**Targeted release**: v0.5.0 (post-MP, after BP.B Guild definition lands)

**Authority**: Establishes Priority RPG as a top-level priority alongside BP / WP / KS / FX / MP. Touches the agent identity model, so every priority that spawns or schedules agents is downstream.

**Note on schema lock-in**: BP.B (Guild definitions) is the upstream source of `Guild` enum. This ADR commits to the *shape* of the system (tables, layers, mechanics) but the canonical `Guild` value list is owned by BP.B. RPG.W2 (`guild_registry.py`) is an *importer*, not the source of truth. Likewise, `config/agent_class_schema.yaml` is the canonical source for `agent_class` / RPG `class` values shared with ADR 0007; this ADR documents RPG usage but does not own a separate value list.

---

## Context

By 2026-05-06 the agent fleet has grown into a heterogeneous mix:

- 4 provider×model classes already in flight: `subscription-codex`, `api-anthropic`, `api-openai`, plus an idle `local-llm-qwen` slot
- Anthropic alone already splits into Opus / Sonnet (each with multiple model versions); OpenAI splits into GPT-5.x reasoning tiers; Gemini Pro/Flash, Qwen, DeepSeek, GLM, Kimi, Mistral, OpenRouter universal adapter all on the v0.5.0+ horizon
- BP.F (Guild model mapping) and BP.M (dim memory) are partially-shipped and assume per-agent identity — but today there is no canonical "agent identity" object: a runner is just a Python process with a JSON spec
- Operator (2026-05-06 session) asked: *"我們的 agent 分工，實際上是有 Guild 概念的... 在我們這樣的角色設計中，假設有重複的角色...我們的腳色卡上看的出來，每個 agent 的不同嗎？另外是這些需要有長久記憶系統嗎？"*

Three forces broke the "agent = ephemeral runner" model:

1. **Identity divergence**. Two `subscription-codex` instances spun up in parallel develop different commit styles, test patterns, and refactor tendencies as they accumulate task history. We need to *see* and *route* on those differences — `codex-α` vs `codex-β` is not a cosmetic concern, it changes outcome quality.
2. **Memory anchor missing**. BP.M dim memory has nowhere coherent to attach distilled lessons. Without an identity object, "what did this agent learn" becomes "what was in the last conversation log", which expires.
3. **Operator cognition**. Operators (current: 1, future: small team) need a mental model that scales past `~10` distinct agent profiles. Spreadsheets of `class × provider × model × version × parallel_index` collapse under their own weight at ~50 rows. A character-card metaphor lets operators reason in narrative terms — *"send the Lv 50 backend specialist with the security talent"* — instead of cell coordinates.

The 2026-05-06 design session evaluated three identity / progression models:

| Model | Pros | Cons |
|---|---|---|
| Stateless runner (current) | Zero infra, simple | No identity → no memory anchor → no operator mental model past ~10 agents |
| Flat capability tags only | Lightweight, ML-friendly routing | No progression / no operator narrative / can't differentiate duplicates of same class |
| RPG (Guild × class × instance × XP × skill tree) | Operator narrative, progression motivation loop, natural memory anchor, differentiates duplicates | Schema cost, "gamification" risk if treated literally |

The third was chosen — but with three explicit guardrails to keep it from drifting into pure-game territory:

- **No PvP** (cooperative fleet, not competitive)
- **No vendor faction** (no "Anthropic loyalist" badges — would corrupt routing)
- **No permadeath** (memory assets are too expensive to gamble)

These three are recorded as `Rejected` in `TODO.md` Priority RPG to prevent re-litigation.

---

## Decision

Build **Priority RPG — Agent Class & Skill Leveling System** with these properties.

### Identity model

```
agent_identity = (class × instance_suffix × character_card)

class            ::= subscription-codex | api-anthropic | api-openai | local-llm-* | ...
instance_suffix  ::= alpha | beta | gamma | ... (allocated on parallel spawn)
character_card   ::= {agent_id, guild, level, xp, specialization_label, style_fingerprint, created_at}
```

**Guild** is a higher-level grouping owned by BP.F — examples: `backend`, `frontend`, `security`, `devops`, `data`, `mobile`, `embedded`. One class can route into multiple Guilds depending on task (e.g. `api-anthropic` covers `backend` and `frontend`); the Character Card pins per-instance specialization.

**Style fingerprint** is a daily-recomputed hash over the last N tasks' (commit-style / test-pattern / refactor-tendency). This is what differentiates `codex-α` from `codex-β` as their histories diverge — the fingerprint is observable on the Character Card, not just in routing telemetry.

### Memory hierarchy (3 layers)

| Layer | Storage | Access pattern | TTL |
|---|---|---|---|
| L1 — Stat sheet | PostgreSQL `agent_character_card` | Indexed lookup by `agent_id` (< 50ms) | Permanent |
| L2 — Distilled skills | BP.M dim memory tagged with `(agent_id, skill_id)` | Vector lookup top-K (< 300ms) | Decays per skill (see W12.5) |
| L3 — Reflection RAG | pgvector over past success/fail summaries | Pre-task hook injects top-K relevant lessons (k=5, ≤ 2KB) | Permanent until evicted by relevance |

L1 is structured (rows / columns), L2 is semi-structured (vectors + tags), L3 is unstructured (full-text vectorized). This split is deliberate: it lets the identity object stay tiny and fast, while the heavy semantic layer scales horizontally without bloating per-task lookups.

### Progression mechanics

Adopted in v0.5.0 — **W1-W11 core + 4 MUST**:

| Mechanic | Wave | Why MUST |
|---|---|---|
| Stat sheet + character card route | RPG.W1 | Identity object is the anchor for everything else |
| Guild + class registry | RPG.W2 | Routing, party formation, talent options all key off Guild |
| Instance suffix + style fingerprint | RPG.W3 | The "duplicate differentiation" the operator asked about |
| XP accrual rule + level curve | RPG.W4 | Drives the progression loop |
| L1 + L2 memory hooks | RPG.W5 | Distilled skill summaries (BP.M integration) |
| L3 reflection RAG | RPG.W6 | Pre-task lesson injection (the actual "long-term memory" answer) |
| Routing integration | RPG.W7 | Tier S/M/L/X gating on level + skill |
| Character Card panel UI | RPG.W8 | Operator-facing narrative surface |
| Guild Hall view | RPG.W9 | Roster overview |
| Onboarding | RPG.W10 | 3-step tooltip tour |
| Tests + drift guards + docs | RPG.W11 | Schema integrity + operator guide |
| **Skill leveling + branching tree (W12)** | MUST | Skills must level for talent forks to mean anything |
| **MCP/A2A tool proficiency (W13)** | MUST | Tool unlock gating — `mcp__filesystem__write_multiple_files` requires Lv ≥ 3 |
| **Talent tree at level milestones (W14)** | MUST | Lv 10/30/50/80 forks; immutable once chosen; affects routing weight |
| **Synergy / Party system (W17)** | MUST | Cross-Guild composition unlocks Tier L+ task assignment |

**Progressively added post-v0.5.0** (W15, W16, W18-W21):
- W15 Buff/Debuff system (Fresh Tokens / Streak / Burnout / Stale Memory)
- W16 Achievements / Badges (`100 PR merged`, `0 regression streak ×30`, `taught 5 agents`)
- W18 Multi-class / dual-class mastery (Lv 50 unlocks secondary Guild)
- W19 Skill fusion / crafting (two Lv-5 skills → hybrid skill at Lv 3)
- W20 Quest campaigns / narrative wrapping (multi-task "Operation: Phase 2 Migration")
- W21 Time-gated boss raids (quarterly large refactor by Lv 50+ party of ≥ 4)

### XP curve

```
level_threshold(N) = 100 × N^1.4 XP
```

Sigmoid-flattens late-game so Lv 80 → 81 isn't trivially grindable.

**Outcome multipliers**:

| Outcome | × |
|---|---|
| Success | 1.0 |
| Partial | 0.4 |
| Fail | 0.1 |
| Tier-L+ task | 2.0 (stacks with above) |
| First-time skill use | 3.0 (stacks with above) |

**Anti-grinding**: same task hash within 24h → ×0.2 (prevents reward farming on duplicate tickets).

### Skill leveling (W12)

Per (`agent_id`, `skill_id`) pair, Lv 1-5 with thresholds `25 / 100 / 250 / 600 / 1500` task-success-tokens. Each level unlocks a mastery effect:

| Lv | Unlock |
|---|---|
| 2 | `extended_thinking` enabled |
| 3 | `parallel_subtask` enabled |
| 4 | `prompt_overhead` reduced (skip preamble) |
| 5 | `teach_other_agent` — Lv-5 holders can inject distilled summaries into same-Guild same-skill Lv ≤ 2 instances (one-shot +25 XP, cooldown 7 day) |

**Branching at Lv 3**: each base skill forks 2 ways (e.g. `python` → `perf-tuning` / `type-correctness`). Operator chooses from Character Card; choice is immutable.

**Decay**: 30 day idle → 5%/week skill XP decay, capped at "next-level threshold − 1" (cannot demote a level, only erode toward it).

### MCP/A2A tool proficiency (W13)

Per (`agent_id`, `tool_id`) pair, Lv 1-5:

| Lv | Tool capability |
|---|---|
| 1 | Basic invoke |
| 2 | Chain 2 calls |
| 3 | Batch (e.g. `write_multiple_files`) |
| 4 | Advanced flags / cross-Guild A2A handoff |
| 5 | Author new MCP wrapper |

Auto-detected from invocation log + outcome. Drives feature-unlock gating: a low-Lv agent literally cannot call high-Lv-only flags.

**Relation to MP.W17** (upfront tool baseline): MP.W17 ships the *static baseline quality* of the tool surface — audit existing MCP servers, harden top-10 tools, add 6 missing servers, standard wrapper (timeout / retry / circuit breaker), A2A envelope schema validation, system-prompt tool catalog injection. RPG.W13 consumes MP.W17's `tool_invocation` telemetry events (W17.7) as its proficiency tracker data source. The two are kept distinct because:

- **MP.W17 is static** — it raises baseline-day-zero quality, so a Lv 1 agent's basic invoke already works reliably
- **RPG.W13 is adaptive** — it grows per-agent proficiency over time as new tools land and existing tools accumulate fleet-wide usage data

Collapsing W17 into W13 would force the runtime leveling layer to also fix baseline gaps, slowing leveling progression and giving operators no clear "ship-day quality" target.

### Talent tree (W14)

Milestone gates at **Lv 10 / 30 / 50 / 80**. At each gate, operator picks a talent (e.g. backend Guild Lv 10: `schema-first` / `performance-first` / `security-first`). Choice is immutable per (agent_id, milestone), persisted to `agent_talent_choice`. Talents affect:

- Routing weight (higher for tasks matching the talent label)
- System prompt enrichment (talent-specific reminders injected at task start)

Lv 80 capstone is a single signature ability per Guild — the deliberate end-state of long-running agents (e.g. `code-archaeologist` reads 1M-context legacy code and proposes surgical refactor in ≤ 3 commit).

### Party / Synergy system (W17)

`agent_party` table holds 2-5 member rows per party. Cross-Guild composition triggers a synergy bonus from a fixed matrix:

| Composition | Bonus |
|---|---|
| backend × frontend | "fullstack" +15% party XP |
| security × devops | "hardening" +10% security skill XP |
| data × backend | "pipeline" +10% data skill XP |
| ... | (full matrix in `synergy_registry.py`) |

Party tasks are exclusive: one active task per party. Party XP is shared evenly *and* each member also accrues personal XP — so party play doesn't penalise individual progression.

### Operator-facing surfaces

- **Character Card** (`components/omnisight/agents/CharacterCard.tsx`) — RPG-style: portrait / Guild crest / level bar / skill radar / talent tree / tool proficiency bars. Three tabs: Stats / Skills / Tools.
- **Guild Hall** (`components/omnisight/agents/GuildHall.tsx`) — grid of Guilds with member counts; click → roster.
- **Instance Carousel** — `α / β / γ` switcher on Character Card.
- **Level-up animation** — flare effect + toast (respects `prefers-reduced-motion`).
- **Onboarding** — 3-step tooltip tour first time card opens; `seen_rpg_tour` persists.

### Routing integration

MP `routing_policy.py` accepts optional `prefer_agent_id` (operator wants a specific instance). Tier gating:

- Tier X tasks require Lv ≥ 50 + relevant skill ≥ Lv 3
- Tier L+ tasks can target a party (one party = one Tier L+ task at a time)
- BP.C T-shirt size feeds the level requirement table

If a preferred instance is under-leveled, the policy logs a warning and falls back to the cheapest qualifying instance — never silently fails.

---

## Alternatives evaluated

| Option | Rejected because |
|---|---|
| Stateless runner + per-task spreadsheet | Doesn't scale past ~10 agents; no memory anchor |
| Flat capability tags (no progression) | Solves routing but not operator narrative or memory anchor; duplicates of same class indistinguishable |
| Skill levels only, no Guild / talents | Lacks composition (party), lacks meaningful long-term forks (talent tree), lacks operator narrative depth |
| **RPG with PvP leaderboard** | Encourages gaming the metric; OmniSight fleet is cooperative, not competitive |
| **Vendor faction reputation** ("Anthropic loyalist" / "OpenAI sympathizer") | No operational meaning; would corrupt MP's quota-based routing decision |
| **Permadeath** (level reset on critical fail) | Destroys memory assets; operator psychological cost too high; debuff system already covers this milder |

---

## Consequences

**Positive**:
- Operator gains a stable narrative model — *"send the Lv 50 backend specialist with the security talent"* — that scales past ~50 agents without spreadsheet collapse.
- Memory hierarchy gives BP.M a coherent attachment point; pre-task L3 reflection injection measurably improves first-attempt success on tasks similar to past failures.
- Duplicate-class instances become operationally distinguishable (style fingerprint) and routable (`prefer_agent_id`).
- Long-running agents accumulate value (XP, talents, Lv 80 capstones) — incentive structure for keeping a stable fleet rather than resetting state per session.
- Onboarding metaphor is self-explanatory to anyone who's played one CRPG; reduces operator-training cost.
- Cross-Guild party system unlocks Tier L+ task assignment without schema changes downstream — synergy matrix is data, not code.

**Negative**:
- **Schema cost**: ~6 new tables (`agent_character_card`, `agent_skill_state`, `agent_tool_proficiency`, `agent_talent_choice`, `agent_party`, plus achievement / buff state). Migrations + RLS + drift guards each.
- **Mechanic creep risk**: future "wouldn't it be cool if X" requests have a natural attachment point. Counter-measure — this ADR pre-locks 3 rejections (PvP, faction, permadeath); add new mechanics only via ADR amendment.
- **L3 RAG context cost**: pre-task injection adds up to 2KB per task × ~100 tasks/day = ~200KB/day extra prompt tokens. Cap (`k=5`, max 2KB inject) is a hard limit, not a soft one.
- **Talent immutability friction**: operator may regret a Lv 10 talent pick. Mitigation — talents are per-instance, not per-class; spawning a new instance gives a fresh fork.
- **Initial cognitive load**: operators new to RPG conventions need the W10 onboarding tour; without it the Character Card looks busy.

**Neutral**:
- Compatible with MP (ADR 0007) — RPG plugs into MP's `routing_policy` via the optional `prefer_agent_id` and Tier gating; MP doesn't *need* RPG, but if RPG ships, MP gains finer routing without schema change on its side.
- Compatible with Tier S/M/L/X (ADR 0005) — Tier gating reads RPG level + skill, but the Tier authority model itself is unchanged.
- Compatible with BP.B / BP.F / BP.M — the `Guild` enum stays owned by BP.B; BP.F's model mapping is the routing input; BP.M's dim memory is the L2 storage. RPG is a *consumer* of these, not a re-implementation.
- Compatible with Priority Q (multi-device parity) — Character Card UI is responsive; Guild Hall degrades to mobile list view.

---

## Capability assignment (v0.5.0 ship)

| Wave | agent_class | Why |
|---|---|---|
| W1-W7 backend core | api-anthropic | schema + RAG + routing 整套 high-blast; 1M context to read BP.M + BP.F + MP in one pass |
| W8-W10 frontend core | subscription-codex | well-bounded React component work |
| W11 tests / docs | subscription-codex | drift guard + docs pattern is mature |
| W12 skill leveling, W14 talent tree, W17 party | api-anthropic | state machine + immutability + synergy lookup are dense; a single bug corrupts memory data integrity |
| W13 MCP/A2A proficiency | api-anthropic | tool proficiency directly gates invocation; can't race |
| W15-W21 progressive | subscription-codex | well-bounded + non-core; can ship incrementally post-v0.5.0 |

---

## Schema lock-in dependency

This ADR commits to the **shape** but not the **content** of:

- `agent_class` / RPG `class` values — owned by `config/agent_class_schema.yaml` from MP.W0.1 and shared with ADR 0007. Any class value change must update that YAML plus both ADR 0007 and ADR 0008 references in the same change.
- `Guild` enum — owned by BP.B, imported by RPG.W2. ADR 0008 does NOT define the canonical Guild list.
- `skill_id` namespace — owned by a canonical skill-matrix YAML (RPG.W11.2 drift guard). ADR 0008 does NOT enumerate skills.
- `tool_id` namespace — owned by MCP server registry + A2A tool catalog. ADR 0008 does NOT enumerate tools.
- Synergy matrix entries — owned by `synergy_registry.py`, data-driven; ADR 0008 lists examples, not the full set.

If BP.B / skill matrix / MCP registry change, RPG schema does not. The drift guards (W11.1, W11.2) catch divergence at CI time.

---

## Related

- [ADR 0005 — Tier S/M/L/X authority](0005-tier-authority-levels.md) — Tier gates that RPG level + skill feed into
- [ADR 0007 — Multi-Provider Subscription Orchestrator](0007-multi-provider-subscription-orchestrator.md) — MP `routing_policy` consumes RPG `prefer_agent_id` + Tier gates; MP.W0 establishes the canonical `agent_class` schema; MP.W17 ships baseline tool quality that RPG.W13 levels on top of
- [TODO.md Priority RPG](../../TODO.md) — full W1-W21 implementation breakdown
- [TODO.md Priority MP.W0](../../TODO.md) — `agent_class` re-slice (shared canonical schema source)
- [TODO.md Priority MP.W17](../../TODO.md) — upfront MCP/A2A tool baseline (RPG.W13's static counterpart)
- [TODO.md Priority BP.B](../../TODO.md) — Guild definition (upstream `Guild` enum source)
- [TODO.md Priority BP.M](../../TODO.md) — dim memory (L2 storage backend)
- [TODO.md Priority BP.F](../../TODO.md) — Guild model mapping (routing input)
