---
id: rpg-w21-boss-task-design
title: RPG.W21 — Quarterly Boss Task (Large-Refactor Raid) Design Spec
status: Draft (design-only)
date: 2026-05-18
jira: OP-1462
supersedes: OP-1397
adr: ADR-0008 §"Progressively added post-v0.5.0" — W21 bullet (line 109)
---

# RPG.W21 — Quarterly Boss Task (Large-Refactor Raid) Design Spec

**Ticket**: OP-1462 — *[BOT][RPG.W21 design] Boss task spec — quarterly large-refactor raid (post-v0.5.0)*
**Replaces**: OP-1397 (force-closed; was wrongly filed as impl story)
**ADR anchor**: [ADR-0008 §"Progressively added post-v0.5.0" W21 bullet](../adr/ADR-0008-agent-rpg-class-skill-leveling.md) — line 109:

> W21 Time-gated boss raids (quarterly large refactor by Lv 50+ party of ≥ 4)

This document is the **design contract** for the W21 feature. It does
**not** ship migrations, API routes, or UI. Future impl tickets are
filed against this contract; if the contract changes, this document
amends first and the impl tickets follow.

---

## 0. Scope and non-goals

### In scope (design)

- Trigger model (who files a boss task and on what cadence)
- Eligibility (party shape, level floor, skill prerequisites)
- Time-gate semantics (window length, pass/fail conditions)
- Reward shape (talent points + cosmetic frame) — **wired to W21.2 / W21.3 primitives that already shipped**
- Proposed alembic schema (DDL, not applied)
- Proposed API surface (4 endpoints, signatures only)
- Proposed UI placeholder location under `components/omnisight/`
- Open questions for operator review

### Out of scope (per ticket NON-GOALS)

- ❌ Do **not** implement migrations (proposed shape only)
- ❌ Do **not** add API routes (signatures only)
- ❌ Do **not** modify `CharacterCard.tsx` / `TalentTree` components
- ❌ Do **not** touch `routing_policy.py`
- ❌ No tests, no fixtures, no integration wiring — that lives in the
  per-sub-wave impl tickets the operator files against this spec

### Already shipped (acknowledged, not re-designed)

The W21.1 / W21.2 / W21.3 *pure-function* primitives shipped under the
RPG.W21 sub-waves in TODO.md before this design doc was written. They
are persistence-free, single-file, and exported with `__all__`:

| Sub-wave | Module | Purpose |
|---|---|---|
| W21.1 | `backend/agents/boss_raid.py` | `boss_raid_eligibility(...)` — deterministic eligibility gate |
| W21.2 | `backend/agents/boss_raid_time_gate.py` | `boss_raid_time_gate(...)` + `boss_raid_completed_in_window(...)` — 14-day window state machine |
| W21.3 | `backend/agents/boss_raid_loot.py` | `boss_raid_loot_reward(...)` — talent point + cosmetic frame loot record |

This design spec **wraps** those primitives with the persistence,
orchestration, API, and UI layers they currently lack. No primitive
contract is changed by this spec; if a primitive needs to change, that
is a separate ADR amendment, not a W21 impl ticket.

---

## 1. Trigger model

### 1.1 Who can file a boss task

A boss task is **operator-initiated**, not auto-scheduled. The
quarterly cadence is a *floor* (≥1 raid per quarter is the
expectation; 0 is allowed if no eligible candidate exists), not a
ceiling (the operator may file additional raids in the same quarter,
subject to §1.3).

Filing roles (per ADR-0005 tier authority):

| Role | May file? | Notes |
|---|---|---|
| Operator (human, T-shirt size XL+) | ✅ | Primary trigger path. |
| `merger-agent-bot` | ❌ | Merger scope is conflict resolution; raids are policy. |
| Any AI reviewer / lint-bot / security-bot | ❌ | AI reviewers cap at +1; raid filing is a policy action. |
| Scheduled job (`backend/background_jobs/`) | ⚠ Reminder only | A quarterly cron may *remind* the operator to file (`raid_due_reminder` notification), but does not file the raid itself. |

The reminder/file split is deliberate: a raid commits a Lv 50+ party
to ≤ 14 days of exclusive work (§3, §4) and produces an exclusive
talent point (§4). That is a policy decision, not a scheduling
decision — auto-firing risks committing the strongest party to
boilerplate when no genuine large-refactor candidate exists.

### 1.2 Filing UX

The operator files a raid by POSTing to
`POST /api/v1/agents/boss-raids` (§6.1) with:

- `name` — operator-visible raid name (e.g. *"Q2 2026 — DB layer
  collapse"*)
- `candidate_task_ids` — list of underlying Tier-X task IDs the raid
  covers (typically 1, may be >1 for cross-cutting upgrades)
- `large_refactor` and/or `cross_cutting_upgrade` — at least one must
  be `true` (mirrors `boss_raid_eligibility(...)` contract)
- `proposed_party_id` — an existing W17 party that satisfies §2

The server runs the §1.3 cadence check + §2 eligibility gate + §3
time-gate setup and returns the new raid row (201) or a structured
error (409 / 422; see §6).

### 1.3 Cadence

- **Floor**: ≥ 1 raid per quarter — surfaced in the operator
  Guild Hall view as a `quarter_raid_count` counter. Zero raids in a
  quarter is allowed (no genuine candidate) but flagged so the
  operator can record a deliberate skip note.
- **Concurrency**: at most **one active raid at a time, fleet-wide**.
  Rationale: a raid pins a Lv 50+ party for ≤ 14 days; running two
  raids halves the strongest-party pool. The single-active rule is
  enforced by a partial unique index on the proposed schema (§5).
- **Filing rate-limit**: operator-only, no per-actor limit beyond the
  single-active rule above. The operator is trusted to file at the
  cadence they need; AI agents cannot file at all.

A "quarter" for the cadence floor uses **calendar-quarter UTC** (Q1 =
Jan–Mar, Q2 = Apr–Jun, Q3 = Jul–Sep, Q4 = Oct–Dec), aligned with the
existing `release-notes/` quarter cuts so the raid-count column joins
cleanly against the release dashboard.

---

## 2. Eligibility

### 2.1 Party shape gate

Already enforced by `boss_raid_eligibility(...)` (W21.1):

- `cadence` must be `"quarterly"`
- `large_refactor` OR `cross_cutting_upgrade` must be `true`
- Party must contain **≥ 4 unique** members at **Lv ≥ 50** (the
  helper deduplicates `agent_id` first, then applies the level
  filter, then counts)

The API layer (§6.1) calls `boss_raid_eligibility(...)` and surfaces
its `reasons` tuple as the 422 detail when `eligible == False`.

### 2.2 Skill prerequisites (new — design extends primitive)

The W21.1 primitive only checks **level**. This spec **adds** a skill
prerequisite layer on top, enforced at the API/orchestration layer
(no change to the primitive):

| Skill prerequisite | Why | Source |
|---|---|---|
| At least 1 party member at skill `refactoring` Lv ≥ 3 | A raid is a large refactor; without Lv ≥ 3 refactoring there is no one to drive surgical change | `agent_skill_state` (RPG.W12) |
| At least 1 party member at skill `architecture` Lv ≥ 3 | Cross-cutting upgrades need a single-throat-to-choke arch owner | `agent_skill_state` (RPG.W12) |
| At least 1 party member with a Lv ≥ 50 talent fork in either `schema-first` or `performance-first` Guild track | Talent forks gate the routing weight that puts the raid on this party's queue rather than scattering across smaller agents | `agent_talent_choice` (RPG.W14) |

The skill-prerequisite check is implemented in a new helper
`backend/agents/boss_raid.py::boss_raid_skill_prerequisites(...)`
(future impl ticket — **not in this design**). It returns the same
`reasons: tuple[str, ...]` shape so the API can compose:

```python
eligibility = boss_raid_eligibility(party_members=members, ...)
skill_check = boss_raid_skill_prerequisites(party_skills=skills, ...)
combined_reasons = eligibility.reasons + skill_check.reasons
combined_eligible = eligibility.eligible and skill_check.eligible
```

If any party member is **mid-task** in another W17 party assignment
(`agent_party_state.active_task_id IS NOT NULL`), the raid is
rejected with `PartyMemberBusy` (422) — re-use the existing W17
error pattern.

### 2.3 Party-of-one and class-mix sanity

The 4-member floor already prevents a single hero-agent run. We
additionally pin **at least 2 distinct `class` values** across the
party (mirrors W17's cross-Guild synergy intent). A 4×`api-anthropic`
party is rejected with `BossRaidPartyClassMixInsufficient` (422); the
operator must swap at least one member to a different class. This
guards against the failure mode where the strongest single class
saturates raids and starves cross-class XP accrual.

---

## 3. Time-gate

### 3.1 Window

- **14 days, continuous engagement**, anchored at raid start
  (`agent_boss_raid.started_at`).
- Deadline is **inclusive**: completion exactly at the 14-day mark
  is `complete`; one nanosecond past is `expired`. (Contract pinned
  by `BOSS_RAID_COMPLETION_WINDOW` in
  `backend/agents/boss_raid_time_gate.py`.)
- The window is **wall-clock**, not "14 days of agent runtime" — a
  raid that idles in the queue for 5 days has 9 days left. This
  mirrors the SLA contract documented in the W21.2 module-level
  *Window semantics* docstring.

### 3.2 States (from the W21.2 primitive)

| Status | Trigger |
|---|---|
| `open` | `checked_at` is within the window and the caller did **not** signal completion |
| `complete` | `checked_at` is within the window and the caller passed `completed=True` |
| `expired` | `checked_at` is past `deadline_at`, regardless of `completed` |

The `expired` state is **terminal and SLA-bearing**: a late
completion (the party finishes the work on day 15) does not retro-
collect loot — the W21.3 primitive returns `None` because the W21.1
eligibility check fed into `boss_raid_loot_reward` will not pass once
the time-gate is `expired`. This is a deliberate SLA: raids that
slip are recorded as a fleet learning event, not silently absorbed.

### 3.3 Pass / fail conditions

A raid **passes** iff **all** of:

1. W21.1 eligibility (`boss_raid_eligibility(...)`) was `eligible`
   at start (this is a one-shot check, not re-evaluated mid-raid).
2. The covered task IDs are all in a terminal `complete` state in the
   underlying task tracker by the time `complete_boss_raid` is
   called.
3. The W21.2 time-gate (`boss_raid_time_gate(...)`) returns
   `status == "complete"` for the completion call.
4. No party member was released from the party mid-raid (a release
   triggers a `BossRaidPartyDeserter` SLA event and degrades the raid
   to `failed` — see §3.4).

A raid **fails** iff any of:

- W21.2 returns `status == "expired"` on the completion call
- Any covered task ended in a terminal `failed` state (rollback,
  abandoned)
- Manual operator abort via `DELETE /api/v1/agents/boss-raids/{id}`
  with `reason` (recorded for retrospective)

### 3.4 Mid-raid party churn

Because a W17 party is 2–5 members and the W21 floor is ≥ 4, the
raid can tolerate **at most 1** mid-raid member release without
falling below the W21.1 gate. Two simultaneous releases drop the
party to 3 members and the raid is auto-failed
(`BossRaidPartyDeserter`).

We deliberately do **not** rebalance mid-raid (no "swap in a fresh
member at day 7"). Rationale: the W21 reward is per-completed-raid
exclusive loot (§4); mid-raid swaps create a perverse incentive for
late joiners to coast. If the operator needs a different composition,
they abort the raid and file a new one.

---

## 4. Reward

### 4.1 Wired to existing primitives

The reward layer is already pinned by W21.3
(`backend/agents/boss_raid_loot.py`). This spec does not redesign the
reward shape; it specifies the **invocation contract** between the
orchestration layer and the primitive:

```python
from backend.agents.boss_raid_loot import boss_raid_loot_reward

for member in raid.completed_party:
    loot = boss_raid_loot_reward(
        agent_id=member.agent_id,
        raid_id=raid.raid_id,
        boss_raid_completed=True,     # set by §3.3 pass check
        boss_raid_eligible=True,      # cached from raid start (§2)
        talent_points=BOSS_RAID_TALENT_POINTS,  # 1 by primitive default
    )
    if loot is not None:
        store.persist_loot(loot)  # §5.3 agent_boss_raid_loot table
```

### 4.2 Reward shape (per W21.3 primitive)

| Field | Source | Persistence |
|---|---|---|
| `talent_points: int` (default 1) | `boss_raid_loot_reward(...).talent_points` | Adds to `agent_character_card.talent_points_unspent` (alembic 0239 column) |
| `cosmetic_frame_id: str` | `boss_raid_cosmetic_frame_id(raid_id)` — sha256-derived deterministic ID | Appended to `agent_character_card.cosmetic_frame_ids: text[]` (proposed §5.2 column) |
| `source: str` | Constant `"boss_raid"` | Stored on the `agent_boss_raid_loot` row for audit |

### 4.3 Exclusivity

- The cosmetic frame ID is **per-raid**, not per-class. Two members
  of the same raid receive the **same** `cosmetic_frame_id`. This is
  the "we beat that boss together" badge; the deterministic sha256
  derivation ensures the same `raid_id` always yields the same frame
  ID across replays / audits.
- Talent points are **personal** and add to each member's
  `talent_points_unspent` counter. Spend is governed by RPG.W14
  talent tree gates — W21 does not introduce new spend paths.

### 4.4 Decay / clawback

**None**. Per ADR-0008 §"Consequences" — `Permadeath` is rejected,
and by extension reward clawback. A raid passes or fails; loot once
granted is permanent. The cosmetic frame remains on the card even if
the agent later levels down or is reset (per ADR-0008 the "Lv 80
capstone" idea — long-running agents accumulate value).

---

## 5. Schema (proposed alembic — NOT to be applied by this ticket)

### 5.1 New tables

Two new tables anchor raid lifecycle and loot. Both DDL is **proposed
shape** — the actual alembic migration is a future impl ticket; the
revision number below is a placeholder.

```sql
-- alembic 0250 (PROPOSED — do not apply from this design ticket)
CREATE TABLE IF NOT EXISTS agent_boss_raid (
    raid_id                  TEXT PRIMARY KEY,
    name                     TEXT NOT NULL,
    party_id                 TEXT NOT NULL
        REFERENCES agent_party_state (party_id),
    candidate_task_ids       TEXT[] NOT NULL,
    cadence                  TEXT NOT NULL DEFAULT 'quarterly',
    large_refactor           BOOLEAN NOT NULL DEFAULT FALSE,
    cross_cutting_upgrade    BOOLEAN NOT NULL DEFAULT FALSE,
    started_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deadline_at              TIMESTAMPTZ NOT NULL,  -- started_at + 14 day
    completed_at             TIMESTAMPTZ,
    status                   TEXT NOT NULL DEFAULT 'open',  -- open|complete|expired|failed
    failure_reason           TEXT,
    created_by               TEXT NOT NULL,  -- operator id (no AI fillers)
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_boss_raid_refactor_or_crosscut
        CHECK (large_refactor OR cross_cutting_upgrade),
    CONSTRAINT chk_boss_raid_status
        CHECK (status IN ('open', 'complete', 'expired', 'failed'))
);

-- Single-active-raid invariant (§1.3). Mirrors the W17
-- agent_party_state per-task partial unique index pattern.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_boss_raid_one_active
    ON agent_boss_raid ((TRUE))
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_agent_boss_raid_party
    ON agent_boss_raid (party_id);
CREATE INDEX IF NOT EXISTS idx_agent_boss_raid_deadline
    ON agent_boss_raid (deadline_at)
    WHERE status = 'open';
```

```sql
-- alembic 0251 (PROPOSED — paired with 0250, do not apply)
CREATE TABLE IF NOT EXISTS agent_boss_raid_loot (
    raid_id                  TEXT NOT NULL
        REFERENCES agent_boss_raid (raid_id) ON DELETE CASCADE,
    agent_id                 TEXT NOT NULL,
    talent_points            INTEGER NOT NULL,
    cosmetic_frame_id        TEXT NOT NULL,
    source                   TEXT NOT NULL DEFAULT 'boss_raid',
    granted_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (raid_id, agent_id),
    CONSTRAINT chk_boss_raid_loot_talent_points_positive
        CHECK (talent_points >= 1),
    CONSTRAINT chk_boss_raid_loot_source
        CHECK (source = 'boss_raid')
);

CREATE INDEX IF NOT EXISTS idx_agent_boss_raid_loot_agent
    ON agent_boss_raid_loot (agent_id);
```

### 5.2 Existing-table changes (proposed)

```sql
-- alembic 0252 (PROPOSED — wires cosmetic frames onto the character card)
ALTER TABLE agent_character_card
    ADD COLUMN IF NOT EXISTS cosmetic_frame_ids TEXT[]
        NOT NULL DEFAULT ARRAY[]::TEXT[];
ALTER TABLE agent_character_card
    ADD COLUMN IF NOT EXISTS talent_points_unspent INTEGER
        NOT NULL DEFAULT 0;
```

Both are additive, default-safe, and reversible (downgrade just drops
the columns). No data backfill needed because the defaults are valid
for every existing row.

### 5.3 Drift guards (proposed — file under RPG.W11 family)

- **Schema drift**: a CI guard rejects a `CREATE TABLE agent_boss_raid`
  whose column set does not match the §5.1 contract.
- **Status enum drift**: the four valid `status` values are mirrored
  in a Python `Literal` type alias in
  `backend/agents/boss_raid.py::BossRaidStatus`; a guard rejects a
  divergence between the DDL `CHECK` constraint and the Python
  literal set.
- **Single-active invariant**: a guard asserts that
  `idx_agent_boss_raid_one_active` exists and is partial
  (`WHERE status = 'open'`); without it, the API-layer single-active
  check is defense-in-depth but not airtight.

---

## 6. API surface (proposed — 4 endpoints, signatures only)

All endpoints live under `/api/v1/agents/boss-raids` and follow the
existing W17 party-endpoint conventions in
`backend/routers/agents.py` (dict body, structured 4xx detail,
`get_conn` dependency, `PostgresPartyStore`-style store injection).

### 6.1 `POST /api/v1/agents/boss-raids` — `create_boss_task`

Operator-only (per §1.1). Body:

```json
{
  "name": "Q2 2026 — DB layer collapse",
  "party_id": "party_abc123",
  "candidate_task_ids": ["OP-1502", "OP-1503"],
  "large_refactor": true,
  "cross_cutting_upgrade": false
}
```

Server-side flow:

1. Resolve party + member levels from `agent_party` ⋈ `agent_character_card`.
2. Run `boss_raid_eligibility(...)` (§2.1).
3. Run `boss_raid_skill_prerequisites(...)` (§2.2 — future helper).
4. Run §1.3 single-active check (DB unique index is backup).
5. Insert `agent_boss_raid` row, return 201 with the new row.

Errors:

| HTTP | Code | When |
|---|---|---|
| 400 | bad body | Malformed JSON / missing fields |
| 403 | non-operator caller | Caller is not an operator role |
| 409 | `BossRaidActiveAlready` | A raid is already in `open` status fleet-wide |
| 409 | `MemberAlreadyInParty` | (Re-used from W17 — party has an active non-raid task) |
| 422 | eligibility failure | Concatenated `reasons` tuple from §2 |

### 6.2 `POST /api/v1/agents/boss-raids/{raid_id}/accept` — `accept`

Party-acknowledgement step. The party lead (the first member returned
by `boss_raid_eligibility(...).qualified_agent_ids`) calls this to
transition the raid from `created` → `open` and start the W21.2
clock. Body is empty; response echoes the raid row with `started_at`
and `deadline_at` populated. No-op (200) if already accepted.

Errors:

| HTTP | Code | When |
|---|---|---|
| 404 | unknown | `raid_id` does not exist |
| 409 | `BossRaidAlreadyExpired` | Time-gate already in `expired` state (defensive — should not happen if called promptly) |

### 6.3 `POST /api/v1/agents/boss-raids/{raid_id}/progress` — `progress`

Progress check / heartbeat. Returns the current
`BossRaidTimeGate` projection plus per-candidate-task status from the
tracker. Idempotent, read-mostly (writes only the
`updated_at` column).

Response shape mirrors the W21.2 dataclass:

```json
{
  "raid_id": "raid_q2_db_collapse",
  "status": "open",
  "started_at": "2026-05-18T10:00:00Z",
  "checked_at": "2026-05-22T14:30:00Z",
  "deadline_at": "2026-06-01T10:00:00Z",
  "elapsed_days": 4,
  "remaining_days": 10,
  "candidate_tasks": [
    {"task_id": "OP-1502", "status": "in_progress"},
    {"task_id": "OP-1503", "status": "in_review"}
  ]
}
```

### 6.4 `POST /api/v1/agents/boss-raids/{raid_id}/complete` — `complete`

Terminal-state transition. Server-side flow:

1. Re-run W21.2 with `completed=True` at `now()`.
2. Validate all `candidate_task_ids` are in terminal `complete` state.
3. Mark raid `status = 'complete'` (or `'expired'` if W21.2 reports
   expired — this is the SLA gate from §3.3).
4. For each member: call `boss_raid_loot_reward(...)` and persist via
   `agent_boss_raid_loot`. Update `agent_character_card` counters
   (§5.2).
5. Return 200 with the raid row + loot rows.

Errors:

| HTTP | Code | When |
|---|---|---|
| 404 | unknown raid | `raid_id` does not exist |
| 409 | `BossRaidNotOpen` | Raid is already `complete` / `expired` / `failed` |
| 422 | `CandidateTasksNotAllComplete` | At least one covered task is not in terminal `complete` state |

### 6.5 (Bonus) `DELETE /api/v1/agents/boss-raids/{raid_id}` — abort

Operator-only manual abort with required `reason` query param.
Transitions `status = 'failed'` and writes `failure_reason`. No loot
granted. Recorded for retrospective.

---

## 7. UI placeholder

### 7.1 Location

New component **directory** under the existing RPG components root:

```
components/omnisight/agents/boss-raid/
    BossRaidPanel.tsx          (W21 future impl — operator filing + status panel)
    BossRaidProgressBar.tsx    (W21 future impl — 14-day countdown)
    BossRaidLootToast.tsx      (W21 future impl — completion celebration toast)
```

The directory is **net new**; we deliberately do not nest under
`PartyHall.tsx` because:

- A raid is a **distinct lifecycle** with its own start / deadline /
  loot — not a property of a party. Hanging it off the party card
  would conflate "this party exists" with "this party is on a raid".
- The single-active-raid invariant (§1.3) means there is at most one
  `BossRaidPanel` rendered at a time — it has a top-level placement,
  not a per-party-card placement.

### 7.2 Surfaces (out of scope to build, in scope to name)

| Surface | Lives in | Wires to |
|---|---|---|
| Operator file-a-raid CTA | `BossRaidPanel.tsx` — primary CTA when no active raid | `POST /api/v1/agents/boss-raids` (§6.1) |
| Active-raid status card | `BossRaidPanel.tsx` — primary content when `status == 'open'` | `GET /api/v1/agents/boss-raids/{id}` + `POST .../progress` (§6.3) polled on tab focus |
| 14-day countdown ring | `BossRaidProgressBar.tsx` | Computes from `started_at` + `deadline_at`; respects `prefers-reduced-motion` (mirrors ADR-0008 §"Operator-facing surfaces" level-up animation guidance) |
| Loot grant toast | `BossRaidLootToast.tsx` | Fires once per completion event from `POST .../complete` (§6.4) response |
| Quarterly-floor reminder | Existing `GuildHall.tsx` — adds a `quarter_raid_count` chip | Server-derived, no new endpoint (joins on existing roster query) |
| Cosmetic frame on character card | Existing `CharacterCard.tsx` portrait border | Reads `cosmetic_frame_ids: string[]` from the character card payload (§5.2 new column). **No structural change required** to `CharacterCard.tsx` — the portrait already accepts an optional `frameId` prop; the change is in the data feed. |

### 7.3 What this design ticket does NOT touch

- `components/omnisight/agents/CharacterCard.tsx` — per NON-GOALS
- `components/omnisight/agents/TalentForkModal.tsx` — talent spend
  flow is untouched; raids only mint new points, they do not change
  how points are spent
- `components/omnisight/agents/PartyHall.tsx` — party cards stay as
  they are; the raid panel is a separate top-level surface

---

## 8. Open questions for operator review

These are the questions the operator should resolve before any W21
impl ticket is filed. Each has a **default** that the impl tickets
will assume unless the operator says otherwise.

1. **Q1 — Quarterly floor enforcement.** When a quarter ends with
   zero raids filed, do we (a) auto-create a `quarter_raid_skipped`
   audit record requiring an operator note, (b) just surface a
   counter in Guild Hall and let the operator notice, or (c) hard-
   gate the next quarter's release-cut on a non-zero count?
   *Default proposed*: (b) — counter only. (c) couples release
   cadence to RPG ceremony, which feels like over-reach for a
   post-v0.5.0 progressive feature.

2. **Q2 — Cross-quarter raid that spans the quarter boundary.** A
   raid filed on Mar 25 has a deadline of Apr 8. Does it count
   toward Q1 (filed) or Q2 (completed) for the §1.3 floor?
   *Default proposed*: **filed-quarter** wins. A 14-day raid is
   short enough that the filing decision dominates the cadence
   signal; reporting on completion would let a Q1 floor be
   satisfied by a Q4 raid.

3. **Q3 — Skill prerequisite strictness (§2.2).** Should the
   `refactoring` and `architecture` Lv ≥ 3 checks be **AND**
   (party must satisfy both) or **OR** (either is fine)?
   *Default proposed*: **AND** — a raid needs both surgical skill
   and architectural sight. **OR** opens the door to all-refactor or
   all-arch parties that historically under-deliver on
   cross-cutting upgrades.

4. **Q4 — Mid-raid release tolerance (§3.4).** Confirm: 1 release is
   tolerated (party drops 4 → 3 → fails at *next* release; a single
   release with 5-member starting parties still has 4 left so is
   fine; a single release with 4-member starting parties drops to 3
   and **fails immediately**)?
   *Default proposed*: **yes, as written above**. The W21.1 gate is
   "≥ 4 qualified at start"; we mirror that as "≥ 4 must remain". A
   strict 4-of-4 starting party that loses 1 fails.

5. **Q5 — Failed-raid retrospective.** A failed raid produces no
   loot but consumes the strongest party for up to 14 days. Should
   the runner auto-open a `retrospective:boss-raid` JIRA on failure,
   or rely on the existing `docs/retrospectives/` quarterly review?
   *Default proposed*: **auto-open** with label
   `meta:retrospective` and link from the next quarter's META
   ticket. Failures are rare and load-bearing; we should not let
   them roll up into a bigger retrospective and lose signal.

6. **Q6 — Cosmetic frame visibility.** The W21.3 primitive returns
   a deterministic per-raid frame ID. Should the operator see the
   frame ID in their character card UI as text (e.g. *"Q2 2026
   Boss"*), or only as a portrait border ornament?
   *Default proposed*: **both** — text label as a tooltip on the
   border. Surfacing only the ornament loses the narrative ("which
   raid was this?") that the operator-narrative goal of ADR-0008
   §"Positive consequences" depends on.

7. **Q7 — Talent point default (§4.2).** Primitive default is `1`.
   For raids covering ≥ 3 candidate tasks (i.e. cross-cutting
   upgrades touching many areas), should the talent point award
   scale (e.g. `1 + len(candidate_task_ids) // 3`)?
   *Default proposed*: **no scaling for W21**. The primitive's `1`
   is the floor; if operators want more, file a follow-up ADR
   amendment. Scaling now risks reward inflation before we have
   data on how often raids actually complete.

8. **Q8 — Single-active vs per-Guild parallelism.** Today's design
   is strictly fleet-wide single-active (§1.3). Should it be
   single-active **per-Guild** (so backend and frontend can each run
   one raid in parallel)?
   *Default proposed*: **fleet-wide for v1**. The Lv 50+ floor is
   already binding — even a large fleet rarely has the bench depth
   for two parallel raids without starving regular Tier L+ work.
   Revisit after 6 months of v1 data.

---

## 9. Out-of-area dependencies (none discovered)

This is a docs-only design ticket. The proposed schema, API, and UI
**reference** code in `backend/`, `db`, and `frontend` but **do not
modify** any of those areas. No discovered-dependency surrender
under jira-ticket-conventions §11 is required.

---

## 10. Related references

- [ADR-0008 — Agent RPG Class & Skill Leveling System](../adr/ADR-0008-agent-rpg-class-skill-leveling.md)
  — parent ADR; line 109 is the W21 anchor
- [ADR-0005 — Tier S/M/L/X authority levels](../adr/ADR-0005-tier-authority-levels.md)
  — tier gate that a boss raid task sits at (Tier-X)
- `backend/agents/boss_raid.py` — W21.1 primitive (eligibility)
- `backend/agents/boss_raid_time_gate.py` — W21.2 primitive (14-day window)
- `backend/agents/boss_raid_loot.py` — W21.3 primitive (talent + frame loot)
- `backend/agents/party.py` — W17 party module the raid wraps
- `backend/routers/agents.py` — existing party endpoint patterns the
  W21 endpoints (§6) mirror
- `backend/alembic/versions/0230_agent_party.py` — schema pattern the
  proposed `agent_boss_raid` table (§5.1) mirrors
- `components/omnisight/agents/PartyHall.tsx`,
  `PartyBuilderModal.tsx`, `CharacterCard.tsx` — UI siblings the
  proposed `boss-raid/` directory (§7.1) sits alongside
- TODO.md — RPG.W21 sub-wave rows (§"RPG.W21 — Time-gated boss raids")
- OP-1397 — superseded impl ticket; this design replaces it
