# EPIC design — Character recruit (DB-backed roster + 創角 UI)

**Status:** design (SOP Stage 3) · **Date:** 2026-07-02 · **Author:** claude (dogfood)
**Vision anchor:** the operator's RPG north-star — "users assemble + reuse their
preferred combos over time". Today the roster is 4 characters HARDCODED in
`backend/agents/character_registry.py`; there is no way to create/retire a
character without a code change. This epic makes the roster data, and gives the
Guild Hall a Recruit flow.

## Premise check (Stage 1 — first-hand, verified 2026-07-02)
**Reusable:**
- `character_registry.CharacterDef` + validation invariants (brain ∈
  `jira_dispatch._BASE_BOT_BY_CLASS`, guild ∈ `GUILDS`, tier ∈ S<M<L<X) and all
  consumers go through `resolve_character` / `character_from_labels` /
  `character_tier_denial_from_labels` — ONE choke point to make DB-backed.
- Sync-PG pattern for runner-side reads exists: `provider_quota_tracker._connect`
  (psycopg2, DSN from OMNISIGHT_DATABASE_URL/DATABASE_URL/OMNI_TEST_PG_URL).
- Progression store `agent_character_card` (level/xp) already exists — character
  DEFINITION (who/brain/guild/ceiling) is a separate concern from PROGRESSION.
- Guild Hall UI (`app/agents/page.tsx` + `components/omnisight/agents/GuildHall.tsx`)
  is the natural Recruit surface; `/agents` router hosts the RPG REST.
- Latest alembic revision: 0252 → new table is 0253.

**Missing:** a `character_def` table; a DB loader in the registry; recruit/retire
API; the Recruit UI; dynamic filer validation (today `--character`
`choices=_CHARACTER_SLUGS` is frozen at import time).

## Consumers of the registry (all must keep working, fail-open)
1. Runner pickup (`jira_dispatch.fetch_pickable_tickets` tier-denial; card
   identity in `auto-runner-jira._resolve_card_identity`) — SYNC, must never
   wedge on DB.
2. Skill resolver (`skill_resolver.resolve_skill_for_character` — guild gate).
3. Filer `scripts/file_jira_ticket.py --character` (derive class, cap tier).
4. Orchestrator `tools.create_task(character=…)`.
5. (new) Recruit API + UI.

## Target architecture — DB over built-ins, fail-open
- **C1 (schema, inert):** alembic `0253_character_def` — table `character_def`
  (slug TEXT PK CHECK `^[a-z][a-z0-9-]*$`, display_name, brain, guild, max_tier
  CHECK IN (S,M,L,X), blurb TEXT DEFAULT '', active BOOL DEFAULT TRUE,
  created_at/updated_at). **Seed the 4 starters** (nova/pixel/sage/rex) in the
  migration so DB and code agree from day one. Brain/guild NOT enum-constrained
  in SQL (both vocabularies grow; app-level validation is the gate — same policy
  as the existing tables).
- **C2 (registry DB-load, the delicate one):** `character_registry` gains
  `load_characters(include_retired: bool = False) -> dict[str, CharacterDef]`:
  read `character_def` rows via a sync psycopg2 read (mirror
  `provider_quota_tracker._connect`, plus an explicit `connect_timeout` ~3s),
  validate each row through `CharacterDef.__post_init__` (a bad row is SKIPPED
  with a warning, never raises), merge with the built-in roster, cache. Policies
  **[codex round 1 — folded in]**:
  * **Stale-if-error cache:** on a refresh failure NEVER replace a
    previously-good DB roster with built-ins — keep the last-good snapshot,
    log its age, retry next TTL. Built-ins-only happens ONLY on cold start with
    no DB. (Else a DB flap mid-poll silently converts character-owned pickups
    into bot-owned work — tier denial bypassed, card identity degraded.)
  * **Jittered TTL** (~30s ± random 0-10s per process) + the connect timeout, so
    ~6 runners don't align into refresh bursts and a hung DB can't stall pickup.
  * **Shadow-protected built-ins [precise branch, codex round 2]:** for a DB
    row whose slug collides with a built-in (nova/pixel/sage/rex), the loader
    ALWAYS uses the code constant; if the row is an EXACT copy of the built-in
    (the C1 seed) it is ignored silently; if it DIVERGES in any field it is
    rejected WITH a warning naming the divergent fields. DB rows can never
    change a built-in's brain/guild/ceiling.
  * **Retired ≠ unresolvable, but retired = no NEW pickup [codex round 2]:**
    rows load with their `active` flag. RESOLUTION paths serving work that
    already STARTED (card identity at/after claim, skill/XP finalization)
    resolve retired characters too — retiring mid-flight never breaks the
    running ticket. But **pickup of a still-To-Do ticket whose character is
    retired is DENIED** (new `character_retired_denial_from_labels`, wired
    beside the tier denial in `fetch_pickable_tickets` with its own audit
    line): retirement means "no new work"; a queued ticket waits until the
    character is reactivated or the ticket is re-filed. FILING paths (filer,
    create_task, recruit-list default) see only active ones.
  * **Max stale age [codex round 2]:** the stale-if-error snapshot carries a
    bound (~15 min). Past it, DB-only characters become
    pickup-denied ("registry stale" audit reason — their To-Do tickets
    safely WAIT for DB recovery; nothing runs on arbitrarily old
    definitions), while built-ins and already-in-flight resolution keep
    working. Card/skill finalization for in-flight work still uses the stale
    snapshot (finishing on a slightly-old definition beats losing XP).
  `resolve_character` / `character_from_labels` /
  `character_tier_denial_from_labels` switch to the loader (retired-inclusive);
  `CHARACTERS` stays as the built-in constant. Filer: drop frozen argparse
  `choices`; `_apply_character` validates against `load_characters()` (active
  only) and, when the DB is unreachable, prints an explicit
  "offline — built-in roster only; DB-recruited characters cannot be validated"
  warning (dev laptops keep working; the error for an unknown slug names both
  the known set and the offline state). **C2 ships WITH runner integration
  tests** (DB-down cold start, stale-if-error, DB-only character pickup,
  retired-in-flight resolution, built-in collision rejection) — the loader is
  the behavioral switch, unit tests alone are not enough.
  * Accepted residual (documented, not built): a ticket stores only the slug;
    a deliberate operator edit of a DB-only character's row mid-flight can
    still change what the slug resolves to. Mitigations above (shadow-protect
    + retired-resolvable + PATCH immutability) cover the realistic paths;
    full per-ticket identity pinning is out of MVP scope.
- **C3 (recruit API):** `/agents/characters` REST — GET list (`?include_retired=`),
  POST recruit (validate slug/brain/guild/tier like CharacterDef; slug must not
  collide with a built-in NOR an existing row; also create the
  `agent_character_card` row via the EXISTING idempotent
  `ensure_card_for_first_task` upsert (ON CONFLICT keeps an existing row —
  never clobbers progression) at Lv1/xp0 + the chosen guild so the recruit
  appears in the roster immediately), PATCH (display_name/blurb/max_tier/active
  — **brain+guild+slug immutable** after creation: progression/skills are
  guild-scoped and the card class is brain-derived; changing them would orphan
  history. Retire = active=false, never DELETE — the card/XP history stays).
  **[codex round 1]** `character_def.guild` vs `agent_character_card.guild`
  drift: `character_def` is authoritative for NEW work (skill gate reads it);
  the card's guild is display/progression provenance — no sync job in MVP,
  divergence is only possible via direct DB edits (accepted residual). ⚠ ROUTE
  ORDER: declare `/characters` BEFORE the `/{agent_id}` catch-all (router line
  ~701) or it's swallowed.
- **C4 (Recruit UI, frontend → pixel):** Guild Hall gets a "Recruit" action →
  modal form: display name + slug (auto-suggest from name), brain picker
  (4 brains), guild picker (canonical guilds), tier ceiling (S/M/L/X), blurb →
  POST `/agents/characters` → roster + AGENT MATRIX show the new character.
  Retired characters render dimmed/hidden toggle.

## Deliberate scope cuts (MVP)
- No auth/permissions on recruit (single-operator deployment; API is already
  behind the app's auth).
- No per-character avatar/art generation.
- No editing brain/guild after creation (immutable, above).
- Chat/orchestrator picks up new characters automatically via C2 (no work).

## Sequencing
C1 (inert schema+seed) → C2 (DB loader + runner integration tests) + C3 (API)
in parallel → C4 (UI, needs C3). C2 is the behavioral switch — its AC carries
the integration-test list above, not just loader unit tests.

## Codex review round 1 — folded in (VERDICT: NEEDS-REWORK → addressed)
1. Fail-open not correctness-preserving (DB flap → character ticket silently
   becomes bot work) → **stale-if-error cache**, built-ins only on cold start.
2. Connection churn/alignment → **connect_timeout ~3s + jittered TTL**.
3. DB-wins too dangerous → **built-in slugs shadow-protected** (DB collision
   rejected at load; seed rows are inert copies).
4. Retire vs in-flight → **active(filing) vs resolvable(runtime) split**;
   retired stays resolvable for card/skill/tier paths.
5. Eager card create → reuse the idempotent `ensure_card_for_first_task`
   (ON CONFLICT keeps existing); guild-drift policy documented.
6. Filer offline → explicit built-ins-only warning + clear unknown-slug error
   naming the offline state.
7. Split safety → C2's AC includes the 5 runner integration tests; "additive
   only" claim now backed by tests, not asserted.
Residual accepted (documented in C2): slug-only ticket identity means a
deliberate mid-flight DB edit of a DB-only character still re-points the slug;
full identity pinning is out of MVP scope.

## Codex review round 2 — folded in (VERDICT: SOUND-WITH-CHANGES → addressed)
1. Retired-inclusive tier-denial left retired characters' To-Do tickets
   pickup-eligible → added `character_retired_denial_from_labels` at pickup
   (retired = no NEW work; in-flight resolution unaffected).
2. Unbounded stale cache → max stale age ~15 min; past it DB-only characters
   are pickup-denied ("registry stale") while built-ins + in-flight
   finalization keep working.
3. Seed-vs-shadow-protection contradiction → precise loader branch: built-in
   slug always uses the code constant; exact seed copy ignored silently;
   divergent row rejected with a field-naming warning.
Design ready for Stage 4 split: C1 schema+seed → C2 loader+denials+integration
tests → C3 recruit API → C4 Recruit UI (pixel).
