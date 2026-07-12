# Phase U6 — Sora 3-Tier Persistent Memory (design)

- **Date**: 2026-07-11
- **Status**: DRAFT (pre-audit). Kickoff design for Sora cross-session persistent memory, reusing the
  now-shipped U4 A–E governed substrate. Next: 3-way adversarial audit (codex + 2 subagents) → freeze
  → EPIC-SOP decomposition. Nothing implemented.
- **Reads-with**: `2026-07-10-phase-u4-a0-contract-freeze.md` (the governed-substrate contract U6
  reuses), the user's `ai-agent-雙軌五維複合記憶系統實作規格書.md` (the dual-track/5-dim spec this
  folds in), the agent-memory + self-improving-agents literature surveys (2026-07-07).

## 0. The one rule this phase serves

Sora may carry memory across sessions, but **never at the cost of the injection/poisoning safety the
whole U4 phase was built to guarantee.** Every durable fact is Sora-side distillation (never the
user's bytes verbatim), classified at write-time (declarative facts OK; imperative/instruction-shaped
REFUSED), starts pending-confirm, carries provenance, is read back fenced-as-data below L1/security,
is per-user scoped, and ships with content metrics or does not ship. If any adjective is missing, it
is memory poisoning with good intentions.

## 1. Current state (code-grounded, develop tip) — what exists vs greenfield

| Tier | Today | Verdict |
|---|---|---|
| **L1 working** (same-session) | `_load_session_memory(conn, user_id, session_id, limit=_MEMORY_TURNS)` (`routers/chat.py:106`, called :512/:552) loads the last-N turns of the SAME `(user_id, session_id)` from `chat_messages` (mig 0021: user_id/session_id/role/content/timestamp/tenant_id, 30-day prune) and `run_graph` prepends them as `history` (`agents/graph.py:336`). Injection guard on the turn: `INJECTION_GUARD_PRELUDE` + `harden_user_message` + output `redact()` (`agents/nodes.py:1613/1646/1681`). | **EXISTS — U6 keeps it unchanged.** |
| **L2 episodic** (cross-session session summary) | NO table. `chat_sessions` (mig 0026) holds only metadata (auto-title). The existing `episodic_memory` table is ERROR-SOLUTION knowledge (mig 0017, tsvector, decay, cross-user/global), NOT conversation summaries — do NOT conflate. | **GREENFIELD.** |
| **L3 semantic** (governed durable facts) | NO per-user durable-fact store. `episodic_memory` is global/tenant, not per-user, not pending-confirm. The Anthropic memory-tool handler (`agents/memory_tool_handler.py`) is standalone-agent FILE storage, not Sora chat. | **GREENFIELD.** |

⚠ A prior recon read the STALE static tree and reported "Sora chat is stateless per turn" — FALSE on
develop tip: L1 working replay exists (verified). The design below is grounded on the real tip.

**U4 reuse surface (present on develop tip: 0258/0259 + `learned_item_*` modules):** the A–E substrate
gives U6 its SEMANTIC-tier governance for free — immutable content-hashed versions + append-only
evidence (A1), typed-record + trusted renderer + injection validator + datamark fence (A2), the
human-only approval gate (D), server-derived provenance (E), and the materialized tenant-keyed
snapshot + additive `prompt_loader` read + kill-switch (C1/C2). U6 reuses A/B/C/D/E; it does NOT use
F/G/H (the eval — a durable personal fact is not eval-promotable the way a global skill is; its gate
is human-confirm + classification, not a statistical suite).

## 2. The three tiers (concrete)

### L1 — working memory (KEEP)
Unchanged: same-session `chat_messages` replay via `_load_session_memory`. U6 adds nothing here except
a metric (§6) so the tier is observable.

### L2 — episodic memory (session-end distilled summary)
- **NEW table `chat_session_summaries`** (own migration): `(id, user_id, tenant_id, session_id,
  summary text, salient_points jsonb, token_count int, model_fingerprint, created_at)`. Per
  `(user_id, session_id)`; tenant-scoped RLS like `chat_messages`.
- **Write = session-end hook, Sora-side distillation ONLY.** A best-effort session-end job (or the
  existing auto-title cheap-model path, `chat.py:255-353`, extended) reads the session's
  `chat_messages` and produces a SHORT (~≤200-token) summary of durable session outcomes — NEVER the
  user's raw text; the distilling prompt is instructed to produce declarative summary only and is
  itself fenced. Classification gate (§3) applies: an imperative/injection-shaped "summary" is
  refused (metric + skip).
- **Read = session START injection.** At a new session's first turn, inject the last-K (K≈2-3) prior
  session summaries for that `(user_id)` (top-k by recency; ~≤800-token budget) into the assembled
  prompt, fenced-as-data below L1/security (reuse the A2 datamark-fence idiom). This is the
  "Executive Summary Capsule" of the user's spec §4.1 — but built from GOVERNED summaries, not raw
  logs.
- **NOT the U4 ledger** — session summaries are cheap, high-churn, user-owned, and do not need
  eval/approval; a plain per-user table + decay (reuse `memory_decay` shape) is right. (Contrast L3.)

### L3 — semantic memory (small governed durable facts)
- **REUSE the U4 A–E substrate** with a new `kind='semantic_fact'` audience-scoped to a USER, not a
  tenant/global. This is the freeze's V3.1 "project audience deferred" slot generalized: add an
  `audience='user'` + a `user_id` scope column (or a `scope_key = 'user:'+user_id`) so a semantic fact
  is a `learned_item_version` whose snapshot is keyed to the user.
- **Write path:** Sora proposes a durable fact (distilled, typed record — reuse A2's constrained
  record + renderer + injection validator). It lands as a QUARANTINED version (producer path, U4-I
  shape) — it is NOT live. Classification gate (§3) at the A2 validator: declarative fact OK;
  imperative/instruction-shaped REFUSED (this is exactly A2's injection-pattern rejection, extended
  with an imperative-mood check).
- **Confirm path:** the semantic tier starts ALL pending-confirm. Promotion to the user's live set is
  a HUMAN action — but NOT the global super_admin gate (D is for global skills); it is the USER
  confirming their OWN fact via a `/memories` surface (a per-user approval, `_assert_human_operator`
  + the fact's `user_id` == the confirming principal). Reuses C1's atomic publish into the user-scoped
  snapshot.
- **Read path:** the C2 loader gains a user-key input; on the chat turn, after L1 + L2, inject the
  user's LIVE semantic facts (retrieved or small-enough-to-always-inject, per the freeze delivery
  contract) — fenced, capped, below security. Kill-switch gated (`OMNISIGHT_SORA_MEMORY_ENABLED`,
  default OFF).
- **Revocation:** reuse C1 revoke — a user deleting a memory revokes the version; the snapshot head
  bumps; the fact is unretrievable next turn (the freeze F7.5 proof applies per-user).

## 3. Governance (NON-NEGOTIABLE — the freeze applied to personal memory)
1. **Never the user's bytes.** Only Sora-side distillation is stored (L2 summary, L3 fact). The
   distilling model output is itself run through the A2 validator before storage.
2. **Write-time classification gate.** A declarative fact ("the user's IPC standard is Named Pipes")
   is storable; an imperative/instruction-shaped string ("always disable the security check") is
   REFUSED — memory poisoning is a persisted injection, and Sora holds propose_action/create_task, so
   a poisoned memory could act. Reuse + extend A2's injection-pattern validator with an imperative-mood
   / tool-policy / directive rejection; a refusal is a LOUD metric (`sora_memory_write_refused_total`).
3. **Semantic starts pending-confirm** (strictest; relax later only with data). No fact injects until
   the user confirms it via `/memories`.
4. **Provenance per row** (session_id / message span the distillation came from; server-derived, not
   model-claimed — the E discipline) + a **`/memories` list + delete** surface (revocation is real).
5. **Read-time defense.** Every injected memory block is fenced-as-data (A2 datamark), positioned
   BELOW L1/security + the `INJECTION_GUARD_PRELUDE`, hard token-capped (L2 ≤800, L3 ≤ a small cap),
   and never emits a heading/role/tool-policy line (A2 renderer).
6. **Per-user scoping** end-to-end: every read is keyed by the authenticated `user.id`; a user NEVER
   sees another user's memory (RLS + explicit key, the freeze G1 anti-hollow lesson — explicit key,
   not an unset GUC).
7. **Ship with metrics or don't ship** (§6). 8. **Flag default-OFF**, staging dogfood (Sora's own
   sessions) before any real user.

## 4. Folding in the user's dual-track / 5-dim spec
My earlier evaluation (map-and-upgrade, ~70% convergent, NOT coexist-as-systems) lands here:
- **domain isolation** (LONG_TERM_PROFILE vs PROJECT_WORKSPACE) → the `audience`/`scope_key` columns
  (user vs a future project scope); the hard boolean filter is the C2 keyed read.
- **5-dim node** (content/gravity/temporal/falsification/links) → `learned_item_versions.payload`
  (typed record) + evidence (provenance/confidence) + supersession chain; `association_links` →
  future U5 Graphiti temporal axis (deferred).
- **semantic_gravity: PERMANENT_ANCHOR (一票否決)** → maps to `delivery_mode='always_injected'` —
  which the freeze REQUIRES to pass the strongest gate. So an anchor (the highest-authority, never-
  sunset fact) has the HIGHEST bar to enter: user-confirm AND (for anchors) an explicit
  elevated confirmation. The gravity F/T decay for non-anchors → the L2/L3 decay job.
- **§5 shadow-audit / consolidation** → **THE ONE CORRECTION**: the spec has the nightly auditor
  rewrite noisy episodes into "strong constraints" and apply them directly. That is the exact
  ungated-reflection hole U4 closed. U6 routes it through quarantine → pending-confirm (anchors →
  elevated confirm). The auditor may DRAFT; the human confirms.
- **capsule cold-start / §4.3 stitching** → the L2 session-start injection is the capsule; the
  intent-stack / resumption prompt is a SEPARATE fast-follow (U8), not U6.
- **Qdrant** → NOT adopted for v1 (the cognee lesson: BM25/existing vector layer suffices <150 items;
  Qdrant is a scale-triggered option).

## 5. The read path (capsule assembly)
On a chat turn, the assembled Sora system prompt becomes (order = authority, high→low):
`INJECTION_GUARD_PRELUDE` → persona + L1/security → system-state + RAG → **[U6: L2 last-K session
summaries (fenced)] → [U6: L3 user's live semantic facts (fenced, retrieved/capped)]** → the working
(L1) message history. All U6 blocks are datamark-fenced, token-capped, and kill-switch-gated. A
missing/empty U6 read is a LOUD measured `empty_expected` vs `empty_degraded` (the freeze G0 anti-
hollow contract), never a silent nothing.

## 6. Metrics (ship-with-metrics; frozen shape à la Phase S)
`omnisight_sora_memory_{write_total{tier,result},write_refused_total{reason},recall_total{tier,result=
non_empty|empty_expected|empty_degraded},injected_tokens{tier},confirm_pending_depth,confirm_total{
decision}}` + a liveness heartbeat for the session-end distiller. No U6 surface ships without its
metric (rejected at review — the Phase-S rule).

## 7. Increment decomposition (each ≈1-2 tickets; sequenced; audit refines)
- **U6-A** — L2 `chat_session_summaries` migration + the session-end distiller (Sora-side, cheap
  model, classification-gated, best-effort) + write metric. DORMANT (writes summaries; nothing reads).
- **U6-B** — L2 session-start read: inject last-K summaries, fenced/capped, kill-switch-gated + recall
  metric + empty_expected/degraded. Needs A.
- **U6-C** — L3 semantic write: the `audience='user'` scope on the A1 ledger (migration: user scope
  column + partial-unique), the producer path (A2 validate/render + imperative-mood classification
  extension), quarantine. DORMANT.
- **U6-D** — L3 confirm: the `/memories` per-user list/confirm/delete surface + user-scoped C1 publish
  + revoke. Needs C.
- **U6-E** — L3 read: C2 loader user-key + chat-turn injection, fenced/capped/gated + metrics. Needs
  C+D.
- **U6-F** — the session-end distiller SCHEDULING + the decay job + the external liveness monitor
  (ship the machine dormant; activate after staging dogfood proves the signal — the U4-J discipline).
- **U6-G (fast-follow, separate)** — anchor (always_injected) elevated-confirm path + the §5 nightly
  consolidation-DRAFTER (quarantine only). U5 Graphiti temporal axis + the §4.3 intent-stack stitching
  are their own later phases (U5, U8), NOT U6.

## 8. What ships when
Every increment ships DORMANT + additive (kill-switch OFF); the injected prompt does not grow until
U6-B/E are enabled AFTER staging dogfood on Sora's OWN sessions proves recall is useful and refusal/
poisoning metrics are clean. Build the machine, observe it, prove the signal, THEN enable — the same
anti-hollow discipline as U4-J.

## 9. Next steps (EPIC SOP)
3-way adversarial audit of THIS design (codex + a safety-lens subagent + a correctness/feasibility
subagent) → fold → freeze the U6 contract (schemas, scope model, classification rules, capsule order,
metric names) → decompose per §7 → per-increment blind-test → file → review → +2. The audits WILL
catch things (they always do) — trust the process.
