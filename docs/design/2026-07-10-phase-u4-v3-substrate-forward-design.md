# Phase U4 v3 — Forward Design (buildable, sequenced) for the eval-gated promotion substrate

- **Date**: 2026-07-10
- **Status**: v2 — PLAN-AUDIT FOLDED (2026-07-10). Subagent (premise/completeness) = SOUND-WITH-CHANGES; codex (sequencing/feasibility) = PLAN-NOT-BUILDABLE-as-sequenced. Both: direction right, step-0 real, cleanest increments buildable — but restructure required before ticketing. The v2 fold below supersedes §§2-3.

---

## v2 — PLAN AUDIT FOLD (2026-07-10) — authoritative, supersedes §§2-3

Two code-grounded plan audits. Consolidated verdict: **the DIRECTION and the shared contracts (§1) hold, but the increment set + sequencing must be restructured before any ticket is cut.** The restructure:

### R1. Two NEW first steps BEFORE any schema migration
- **U4-0b — HARD-CLOSE the interlock override (true first ticket).** The step-0 flag `OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED` is human-overrideable → a human can reactivate the legacy direct writers (`learned_item_interlock.py:34,42`). ⇒ make legacy publication **unconditionally unavailable in prod** (the flag can no longer re-open the legacy FS/vector writers; only the future canonical publisher may replace that denial). Small, pure-safety, offline-testable.
- **U4-A0 — CONTRACT FREEZE (design-only, no code).** U4-A cannot freeze a migration while cross-cutting choices are open (codex BLOCKER). Resolve UP FRONT, in one reviewed doc: (a) publication model = **approved DB view is the source of truth** (NOT content-addressed artifacts + signed manifest + outbox — all greenfield; cosign is CI-only, not runtime-reusable); (b) the FULL append-only ledger = all 6 tables (`learned_item_versions`, `_evidence`, `memory_eval_runs`+cases, `memory_approvals`, `memory_publications`, `memory_transition_events`); (c) the exact fingerprint fields incl. **`live_set_hash`** bound at approval (so a publisher can't batch individually-approved items into an unapproved set); (d) revocation semantics; (e) the constrained-record schema + trusted-render contract; (f) tenant/audience/global scope columns + super_admin-for-global. Everything downstream binds to this freeze.

### R2. U4-A owns the FULL ledger, split in two
- **U4-A1** — the 6-table append-only ledger migration + canonical-content-hash (excl. provenance/timestamps) + immutability (edit→new version) + append-only trigger (reuse the `0234` precedent) + tenant/audience/project columns (reuse `0193` RLS-via-GUC shape). Pure/offline-green. **This is the true first BUILD ticket** (after 0b + A0).
- **U4-A2** — the constrained typed record + **trusted RENDERER** (a dataclass of arbitrary strings still permits "ignore previous rules" — the renderer is the safety piece) + **datamark/fence** (A5) + validator + `rendered_payload_sha256`. Offline-green.

### R3. Re-scopes (feasibility, code-verified)
- **U4-B** — separate DB roles/keys is GREENFIELD (single-credential pool, zero CREATE ROLE/GRANT in 121 migrations). Re-scope to **single-role + a conditional `BEFORE UPDATE` trigger gating `status→promoted` on an approvals-JOIN + eval-pass, + app-level actor separation.** DB-enforced *transition*, achievable.
- **U4-C** — narrow to **approved-DB-view = source of truth** + build the **revocation ACTION** (not just a `revoked` state name: the command, permission, live-view removal, vector cleanup, cache reconciliation, + proof a revoked hash can't be retrieved). Any manifest is scoped to the **promotion namespace only** (a new dir the loader reads additively) — a whole-loader manifest hides every legacy skill + can't manifest the `~/.claude`/project-memory walk (~40% of `prompt_loader.py`).
- **U4-G** — historical-ticket replay is **near-greenfield, its own PHASE** (replay.py deprecated shim; dag_executor inert; no checkout-at-SHA + test-exec + baseline-compare). **Fork it off the v1 critical path.** v1 promotes only **retrieved-not-always-injected** items (needs plan-triage eval, not replay).

### R4. Sequencing corrections (codex)
- Corrected order: **U4-0b → U4-A0(freeze) → U4-A1 → U4-A2 → {U4-B, U4-D, U4-E} → U4-H → U4-F → U4-I → U4-J(pilot) → U4-J2(enable-decision).**
- **B → C** (C needs B's transition/actor boundary), NOT parallel.
- **H before the F executor** (a valid eval executor needs strict suite loading + signed-suite identity + holdout separation + multiple-test policy; only F's pure math can precede H).
- **U4-D binds the `live_set_hash`** in the approval card, not just the item hash.
- **U4-I depends on A+B+C+D+E+F+H** (canonical ingestion needs E's server-derived provenance; delegation needs F/H eval evidence), and must own **producer backfill + compat semantics** (a "thin FK/view" can't preserve both the update-oriented `skill_distiller`/`skills_extractor` APIs AND immutable-version semantics — decide: adapter vs deprecate-old-API).
- **U4-J splits**: the pilot/correlation-study is an EXPERIMENT ticket (no predetermined green); the enable-publication decision is a SEPARATE operator gate conditioned on the experiment's result.

### R5. Restored / added deliverables (were dropped or unowned)
- **Metrics/observability increment** — the forward design dropped ALL metrics (regression vs the original §4.6; the exact hollow anti-pattern). Add `omnisight_memory_{quarantine_depth,promotion_total,eval_delta,neg_control_catch,failclosed_total,proposal_outcome}` + deployed alert wiring (metrics.py already hosts `skill_promoted_total`).
- **Actual-delivery eval + set-level regression** — U4-F must score the EXACT rendered/selected/**truncated** artifact via the real selection path (eager cap 4000 / lazy catalog / vector ~240-char truncation), not an abstract full-candidate injection; and run a **set-level regression** (does this item, combined with the already-promoted set, regress anything?) before a batch publishes.
- **Global-scope authorization** — bind "GLOBAL needs platform approval" to the real `require_super_admin` (`/proposed-actions` currently accepts ordinary admins).
- **Injection negative-controls** in the suite (U4-H) — a candidate containing "ignore previous instructions"/fake `# Core Rules` must auto-reject.
- **Kill-switch** — a single instant halt for all eval/promotion (AC on U4-0b/A1's flag surface).

### R6. Revised scale
~**16 tickets** (0b, A0-freeze, A1, A2, B, C, C-revoke, D, E, F, F-setregression, H, metrics, I, J-pilot, J2-enable). Critical path (v1, retrieved-only, G forked): **0b → A0 → A1 → A2 → B → C → I → J-pilot → J2**. The rest hang off A1/A0. U4-G (replay) + always-injected promotion = a fast-follow PHASE.

**STATUS: forward design NOT ticketable as v1 was written; the v2 restructure above is the ticketable plan. Recommend cutting U4-0b (hard closure) + U4-A0 (contract freeze) FIRST — both small/pure — then U4-A1. Pending operator go on scope (full ~16 vs retrieved-only v1 with G deferred).**

---
- **Reads-with**: `docs/design/2026-07-10-phase-u4-eval-gated-promotion-design.md` §§E-F (the audit findings + the "why"). This doc is the "what to build, in what order."
- **Precondition DONE**: U4 step-0 emergency interlock (OP-2564, #2040 merged) — the live hole is closed deny-by-default. This forward design is what replaces that coarse interlock with the real gate.

---

## 0. The one hard rule this whole phase serves

No learned item reaches the injected prompt set except as an **immutable, hash-bound, content-schema-constrained version** that passed a **statistically-powered eval** (or historical-ticket replay) **and** a **human-only approval**, published **atomically** into a **tenant-scoped** namespace, with every step **append-only-audited** and **fail-closed**. If any of those adjectives is missing, the gate is theatre. The interim interlock holds the line (deny-by-default) until every increment below lands.

## 1. Shared contracts (every increment obeys these)

- **Immutable versioning**: a candidate is a `learned_item_version` row keyed by `canonical_content_hash` (payload hash EXCLUDING provenance/timestamps — codex #24). Any edit = a NEW version; it invalidates all prior evals/approvals for the old hash. Eval/approval/publication each bind the full fingerprint `{version_id, payload_sha256, rendered_payload_sha256, suite_sha256, live_set_sha256, model/prompt/config fingerprint}` (codex #5).
- **Append-only audit**: no column is overwritten. Tables: `learned_item_versions`, `learned_item_evidence`, `memory_eval_runs` (+ per-case results), `memory_approvals`, `memory_publications`, `memory_transition_events` (codex #28). The live view JOINs only exact, non-revoked, approved publications.
- **Fail-closed everywhere**: missing/empty/stale/hash-mismatch suite, unproducible baseline, any eval error/timeout/fallback/truncation/empty-arm → `infra_invalid` (retry quarantined) or `reject`, NEVER `promote` (codex #15, #25). "insufficient_evidence" is a first-class terminal, distinct from "rejected."
- **Human-only + writer/actor separation**: promotion approval only via a principal that passes `_assert_human_operator` (codex #2); producers can only INSERT quarantined versions (never self-promote); a DB trigger makes the illegal `status→promoted` transition impossible, not merely unlikely (codex #4).
- **Tenant scope**: every row carries non-null tenant/project/repository/audience; a GLOBAL item requires platform (super_admin) approval; tenant content is NEVER published into a shared filesystem namespace (codex #7).
- **Offline-AC honesty**: the LLM-in-the-loop eval CANNOT be a runner green. Each ticket's Code-AC is the deterministic/mock-`ask_fn`/static-contract portion; anything needing a live provider, real Postgres roles/RLS, a running scheduler, or filesystem publication is an **operator/Exercised** step (codex §5). The `test_finetune_eval.py` scripted-mock pattern is the template.
- **Constrained schema, not free Markdown**: promoted content is a typed record (scope/preconditions/procedure_steps/verification/known_failures/prohibited_actions/evidence_references) rendered through a TRUSTED template; role/priority/security-heading/tool-policy/load directives are rejected (codex #8).

## 2. Increment decomposition (each ≈ 1-2 tickets; sequenced)

| ID | Increment | Core deliverable | Offline-testable? | tier |
|----|-----------|------------------|-------------------|------|
| **U4-A** | Immutable candidate substrate | append-only `learned_item_versions` + `_evidence` schema/migration; canonical-hash; immutability (edit→new version); tenant/audience scope columns; constrained-schema dataclass + validator | YES (schema + hash + validator + immutability logic, pure) | L |
| **U4-B** | Writer/actor DB separation | separate DB roles (producer INSERT-only via `submit_quarantined_item(...)` proc; evaluator/approval/publisher roles); RLS; `BEFORE UPDATE` trigger gating `status→promoted` on approved+eval-pass; producers never hold publisher keys | PARTIAL (migration/GRANT/trigger SQL + static contract tests offline; real role enforcement = operator) | L |
| **U4-C** | Atomic publication | approved DB view = source of truth OR content-addressed artifacts + signed manifest via transactional outbox; `prompt_loader` loads only manifest-listed hashes; states `approved→publishing→published\|publish_failed→revoked`; makes rollback real | PARTIAL (state machine + manifest-verify offline w/ faked stores; real FS/vector publication = operator) | L |
| **U4-D** | `memory/promotion` approval kind | add the kind to `/proposed-actions` producer (`tools.py`) + executor (`action_executor.py`); approval does NOT publish — a separate publisher revalidates all hashes then publishes; daily ranked digest (5-10 cards, oldest-first), per-payload-hash approval, no "approve-all" | YES (producer/executor unit + human-only gate + "approve doesn't publish" w/ mock) | M |
| **U4-E** | Provenance ground-truth binding | derive `ground_truth_label` SERVER-SIDE from an authenticated Gerrit/JIRA lookup keyed by origin change-id; reverify at promotion; store change-id + revert-state; reject unconfirmable claims | PARTIAL (binding logic + reverify w/ mock source offline; real Gerrit/JIRA lookup = operator) | M |
| **U4-F** | Plan-triage eval scorer | new `memory_promotion_eval.py`: candidate injected into a FIXED-model `ask_fn`; paired per-case McNemar exact test; N-sample as CLUSTERED (not pseudoreplication); `insufficient_evidence` at small N; NO +3pp scalar; dedicated no-failover eval client (pinned model/temp0/seed); cost controls (baseline distribution cached by full fingerprint, two-stage k1→k5, hard token budget, canonical-hash dedupe, per-night cap→queue) | YES for the math/gate/fail-closed/cost-logic (mock `ask_fn`); real deltas = operator | L |
| **U4-G** | Historical-ticket-replay lane | the STRONGER eval required for always-injected items: replay a frozen mini-set of real tickets at fixed commits with executable tests + policy checks, with/without the candidate. **Hardest / most novel — likely a feasibility SPIKE first** (does replay-at-commit infra exist? cost?) | mostly operator; spike first | L (spike M) |
| **U4-H** | Private holdout + strict loader + liveness | strict suite MANIFEST loader (one bad shard invalidates all; min case/neg-control counts; signed-suite binding, not just in-repo checksum); public dev cases + PRIVATE access-separated holdout + multiple-testing correction; durable eval-run heartbeats + EXTERNAL absence monitor ("enabled candidates but no successful eval within SLO") | YES for loader/manifest/heartbeat-emit; private-holdout access + external monitor = operator | M |
| **U4-I** | Migrate both producers | `skill_distiller` + `skills_extractor` write to the substrate via the restricted path (not draft rows / not `_pending`); `auto_distilled_skills` → thin FK/compatibility view (no dual truth, codex #33); the two interim-interlock promote endpoints now delegate to the one canonical `publish_learned_item_version()` | YES (producer rewrite + delegation, unit-tested) | L |
| **U4-J** | Live pilots + signal-validation | hand-authored known-good/bad/injection/stale-approval/provider-failure/publish-crash candidates run end-to-end on staging; the CONSTRUCT-VALIDITY correlation study (plan-delta predicts real ground-truth outcome) — publication stays disabled until this passes (codex #30) | NO — operator/Exercised gate; this is the enable-decision | — |

## 3. Sequencing / dependency graph

```
U4-A (substrate)  ──►  U4-B (roles/trigger)  ──►  U4-C (atomic publish)  ──►  U4-I (migrate producers)  ──►  U4-J (pilots+enable)
   │                       │                                                       ▲
   ├──► U4-D (approval kind) ───────────────────────────────────────────────────┘
   ├──► U4-E (provenance binding) ──────────────────────────────────────────────┘
   └──► U4-F (plan eval) ──► U4-H (holdout+liveness) ──► U4-G (replay lane) ──────┘
```
- **U4-A is the foundation** — everything binds to its version/hash schema. Build + audit + ship it first.
- U4-B/C/D/E/F can proceed in parallel once A lands (different surfaces).
- U4-G (replay) is the long pole — **spike its feasibility early** (in parallel with A) so we know if it's a v1 requirement or a fast-follow; codex requires it for *always-injected* items, so it gates U4-J's "enable," not the substrate.
- U4-I (migrate) needs A+B+C+D. U4-J (enable) is last and is an operator decision after signal-validation.
- The interim interlock stays deny-by-default through the ENTIRE sequence; publication only enables at U4-J.

## 4. What ships when (activation discipline)

Every increment ships **dormant/closed** and additive; the injected set does not grow until U4-J flips publication on AFTER the correlation study. This is the Phase-S/anti-hollow discipline applied to the whole phase: build the machine fully, observe it, prove the signal, THEN enable — never ship the tripwire (or here, the gate) live-but-unproven.

## 5. Open scope questions for the operator
1. **Full 10-increment commit vs trim?** U4-G (historical-ticket-replay) is the hardest and most expensive; codex requires it only for *always-injected* items. Option: ship the substrate + plan-triage eval (A-F, H, I) as v1 with promotion limited to *retrieved-not-always-injected* items, and defer the replay lane (G) + always-injected promotion to a fast-follow. Decision affects scope by ~30%.
2. **Increment-at-a-time vs batch tickets?** Recommend design→audit→ticket **U4-A first**, verify the rhythm, then fan out B-F. (Avoids a 15-ticket blind-test marathon up front.)
3. **U6 interleave?** U6 (Sora memory) reuses A/B/C/D/E (the substrate) but not F/G/H (the eval). Once A-E land, U6 can start in parallel with F-J.

## 6. Next steps (EPIC SOP)
Audit THIS forward design (3-way incl. codex, now working) → confirm the decomposition/sequencing → ticket **U4-A** (self-contained, offline-AC-scoped) → blind-test the highest-complexity ticket → file → review → +2 → repeat per increment.
