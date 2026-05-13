---
id: ADR-0035
title: Runner FSM + Error Handling Contract
status: Adopted
date: 2026-05-14
---

# ADR-0035: Runner FSM + Error Handling Contract

## Status: Adopted (2026-05-14)

## Context

Sprint B runner reliability work needs one binding contract for the top-level runner state machine, exit ordering, and retry/idempotency semantics. The original architecture document (`docs/architecture/sdk-runner-sprint-b-error-handling.md`) remains valuable as the detailed audit and implementation appendix, but its length mixes normative requirements with incident history, child-specific notes, and non-binding analysis.

Past runner failures repeatedly came from ambiguous state ownership: Gerrit and JIRA were updated in the wrong order, retries created duplicate external effects, worktree/main boundaries drifted, and long-running sessions resumed without a durable state boundary. Those failures need an ADR-level contract so future implementation tickets can cite stable sections instead of relying on prose buried in the audit.

This ADR therefore extracts the binding top-level FSM, invariants, external-system ordering, and idempotency primitives into split form. The architecture appendix retains its deeper analysis for Sprint B children, but ADR-0035 is the authority for the runner FSM and error-handling contract.

## Decision

The following FSM contract is **binding**:

## 2. Top-level runner FSM

This is the *aggregate* state machine across all Sprint B children. Each child plugs into specific transitions; child-internal FSMs are in §4.

```
                              ┌───────────────────┐
                              │   idle            │
                              └────────┬──────────┘
                                       │ pickup-trigger
                                       ▼
                              ┌───────────────────┐
                              │   picking_up      │
                              └────────┬──────────┘
                                       │ ticket claimed
                                       ▼
                  ┌────────────────────────────────────┐
                  │   restoring_session     (B9)       │  ← reads progress.txt; if absent → fresh
                  └─────────────────┬──────────────────┘
                                    ▼
                  ┌────────────────────────────────────┐
                  │   injecting_lessons     (B10)      │  ← BM25 over lessons-learned.md
                  └─────────────────┬──────────────────┘
                                    ▼
                  ┌────────────────────────────────────┐
                  │   working                          │ ◄────────────┐
                  │   ┌──────────────────────────────┐ │              │
                  │   │ locating              (B7)   │ │              │
                  │   │   ↓                          │ │              │
                  │   │ blast_radius_check    (B13)  │ │ ─── oversize │
                  │   │   ↓                          │ │              │ context
                  │   │ tdd_phase             (B5)   │ │              │  reset
                  │   │   ↓                          │ │              │  (B3)
                  │   │ static_analysis       (B2)   │ │              │
                  │   │   ↓                          │ │              │
                  │   │ coding                       │ │ ─── 3x-loop ─┘
                  │   │   ↓ ↑ reflection (B12)       │ │
                  │   │ critic_review         (B4)   │ │ ── dissent ──► retry coding
                  │   │   ↓                          │ │
                  │   │ committing                   │ │
                  │   └──────────────────────────────┘ │
                  └─────────────────┬──────────────────┘
                                    ▼
                              ┌──────────────┐
                              │ submitting   │ ─── push race ──► classifier ──► retry / abort
                              └──────┬───────┘
                                     ▼
                       ┌───────┬─────────┬──────────┐
                       ▼       ▼         ▼          ▼
                   completed  failed  aborted  partially_done
                                      │
                  ┌───────────────────┴──────────────────────┐
                  │ aborted decomposes:                       │
                  │  loop_aborted          (B3)               │
                  │  oversized_aborted     (B13)              │
                  │  critic_blocked        (B4 2nd dissent)   │
                  │  tdd_unwriteable       (B5)               │
                  │  lint_cap_exceeded     (B2)               │
                  │  budget_exhausted      (CostGuard)        │
                  │  network_aborted       (Anthropic API)    │
                  │  external_aborted      (Gerrit / JIRA)    │
                  │  surrender_structural  (max_iterations)   │
                  └────────────────────────────────────────────┘
```

## Invariants I1-I6

The following invariants are **binding**:

**Critical invariants** (each must hold across every transition):

| Invariant | Why | Where enforced |
|---|---|---|
| **I1** Worktree is the single writable surface | Avoids main-repo contamination (Lesson 17) | Every child's tool handler must reject writes outside `--worktree-path` |
| **I2** JIRA assignee = bot identity throughout `working`, cleared on `aborted`/`completed` | Avoids orphan In-Progress (Sprint A pre-mortem) | `_release_ticket()` in jira_dispatch must run on every exit path |
| **I3** Gerrit Change-Id is **stable across retries**, **fresh per fresh ticket** | Avoids #334-style duplicate change creation; preserves PS history | commit-msg hook + amend discipline |
| **I4** Conversation history is checkpoint-able at any state boundary | B3 context reset requires it | Runner persists every assistant turn to `progress.txt` (B9) |
| **I5** All side-effecting operations are idempotency-token guarded | Bridge daemon resends, stream-event replays | Per-operation token derived from `(ticket_key, operation, args_hash)` |
| **I6** Live-state injection happens at **state boundary**, not at pickup | Lesson 3 (alembic head drift), Lesson 17 (worktree-vs-main cwd) | Each transition that depends on repo state re-reads at transition time |

## External system ordering (§C4)

The following external-system ordering requirement is **binding**:

### C4 — External system cascade failures

**Failure modes**:
- Gerrit rejects push → JIRA still transitioned to Under Review → orphaned state
- JIRA transition succeeds → Gerrit push fails → ticket stuck in wrong state
- Bridge daemon down → change-merged events queue, what happens when it comes back?
- Atlassian Cloud rate-limit → transition fails → runner mid-state

**Coverage**:
- 🟡 `_handle_gerrit_push_failure` covers known classes
- 🔴 No two-phase commit between JIRA + Gerrit
- 🔴 Bridge backlog catchup behaviour unspecified post-F4

**New requirement**: state-transition order is **Gerrit-push first, JIRA-transition second**. If Gerrit push fails, JIRA stays at In Progress (no orphan). If JIRA transition fails after Gerrit success, retry transition with idempotency token (Gerrit Change-URL); next pickup detects existing PS via F11 detector.

## Idempotency primitives (§7)

The following recovery and idempotency primitives are **binding**:

## 7. Recovery primitives — codified spec

This section defines the primitive operations Sprint B children rely on.

### Worktree snapshot

**Format**: `git stash push -m "<phase_name> @ <ISO timestamp> @ <ticket_key>"`

**Trigger points** (per Sprint charter):
- Phase boundary entry (e.g., transitioning into `coding` from `tdd_phase`)
- Pre-commit (so a critic dissent can roll back to pre-edit state)

**Retention policy**: 1 hour after Sprint completion. Reaper deletes older.

### Conversation history checkpoint

**Format**: JSONL append-only, one line per assistant turn, written to `progress.txt`.

**Schema**:
```json
{"ts": "2026-05-09T12:34:56Z", "iter": 17, "role": "assistant", "stop_reason": "tool_use", "tool_calls": [...], "content_summary": "..."}
```

**Trigger**: every assistant turn. Synchronous fsync (durability > throughput).

### Tool call replay log

**Purpose**: post-hoc debugging; required for B3 detector retrospective tuning.

**Format**: same JSONL stream as conversation history; tool-side events (input + result) interleaved.

**Retention**: 30 days, then archive to S3 (per `archive_window_days` in spec).

### JIRA idempotency tokens

**Token format**: `(ticket_key, operation_type, args_hash)` where:
- `operation_type` ∈ {`pickup`, `transition`, `comment`, `assign`, `release`}
- `args_hash` = SHA256(JSON-canonical(args))

**Storage**: Postgres `jira_operations` table, unique index on token. Retried operations match token → no-op.

**TTL**: 24h; older tokens dropped (re-issue allowed for genuinely new operation).

### Gerrit Change-Id reuse rules

**New ticket** (no prior PS): generate fresh Change-Id via commit-msg hook. Pin to ticket via JIRA comment "Gerrit: <change-url>".

**Resume ticket** (PS exists): pickup detects via F11 — `gerrit query change:I<id>`. Reuse Change-Id; amend new PS.

**Forbidden**: regenerating Change-Id on amend. Always pass through original Change-Id explicitly (lesson learned from #334 duplicate during PS2 push).

### Cache invalidation triggers

- **Repo-map cache (B8)**: invalidated on every observed commit (HEAD SHA change).
- **BM25 lessons index (B10)**: invalidated when `lessons-learned.md` mtime changes.
- **Anthropic prompt cache (B11)**: managed by Anthropic; no client-side invalidation needed.
- **Tree-sitter parse cache**: per-file mtime; invalidated on file edit.

Runner-side JIRA idempotency implements the token convention from SP-B-X-001 as `f"{ticket_key}:{operation_type}:{args_hash}"`.

## Consequences

**Positive**:
- Gives runner implementation tickets a single binding FSM and error-handling reference.
- Keeps the audit appendix useful without requiring every implementation to treat all appendix analysis as normative.
- Makes Gerrit/JIRA ordering and idempotency reviewable at ADR level.

**Negative / Tradeoffs**:
- The same content now exists in split form; future edits must update ADR-0035 first, then decide whether the appendix needs a non-binding explanatory update.
- Line-level citations into the original architecture note may drift as the appendix changes.

**Risks**:
- Implementations may cite the appendix instead of ADR-0035 for binding behavior. The appendix header and section prefixes explicitly redirect binding references here.

## Rejected alternatives

- **Leave the architecture document as the only authority**: rejected because it mixes binding requirements with audit narrative and child-specific analysis.
- **Move the entire architecture document into an ADR**: rejected because the appendix is intentionally broad and would make the ADR too large to use as a stable contract.
- **Write a fresh FSM instead of extracting the existing one**: rejected because Sprint B work already depends on the audited FSM wording.

## Relationship to other ADRs

- **ADR-0003 Gerrit Code Review**: ADR-0035 preserves Gerrit-first submission ordering and does not bypass review.
- **ADR-0004 Per-agent JIRA identity**: ADR-0035 relies on bot identity ownership and release semantics for the JIRA assignee invariant.
- **ADR-0005 Tier Authority Levels**: ADR-0035 is an L3 runner contract; higher-authority overrides remain outside this ADR.
- **ADR-0018 Event-driven Release Pipeline**: ADR-0035 follows the same external mutation posture: stable idempotency keys, replay tolerance, and explicit failure routing.
