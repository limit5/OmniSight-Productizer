# Phase U4-A0 — Contract Freeze (the decisions every substrate ticket binds to)

- **Date**: 2026-07-10
- **Status**: v2 — AUDIT FOLDED + ANTI-HOLLOW-HARDENED (2026-07-10). Both plan-auditors returned NOT-FROZEN/READY-WITH-FIXES on v1; the v2 fold below is authoritative and SUPERSEDES v1 §§F1-F8 where they conflict. The v1 text is retained beneath for provenance.

---

## v2 — AUDIT FOLD (authoritative). Operator directive: **minimize the "deployed-but-hollow" pattern structurally.**

Both audits agree v1's direction (DB authority, immutable versions, append-only evidence) is right, but v1's **"`prompt_loader` reads a DB view and renders learned items live at assembly time" is the single biggest source of both churn AND hollowness** — `build_system_prompt` is synchronous with zero DB reads (`prompt_loader.py:951`), the RLS GUC `omnisight.tenant_id` is never set on the agent path (`db_context.py` is contextvars; only `rag.py:756` sets it), and under `FORCE RLS` an unset GUC makes the view return **zero rows silently**. v2 re-architects around a **materialized rendered snapshot** and freezes an explicit anti-hollow contract.

### G0. ANTI-HOLLOW is a FROZEN, first-class contract (operator directive)
The 3D-memory audit, Phase S, and this F6 finding are the same failure: a layer degrades to empty/no-op and still looks healthy. v2 makes that **structurally detectable and loud**:
1. **No silent-empty.** Every surface that can return empty MUST distinguish *legitimately-empty* (no published items in scope — expected) from *degraded-empty* (items exist in the ledger but aren't being delivered — a BUG). The latter is impossible to be silent: see the reconciliation invariant.
2. **Reconciliation invariant (the core guard).** For each scope, `COUNT(ledger: published ∧ non-revoked ∧ in-scope)` MUST equal `COUNT(items the loader actually injected from the snapshot)`. A divergence (ledger says N>0, delivery says 0 or ≠N) fires `omnisight_memory_reconcile_divergence_total{scope,reason}` + a CRITICAL alert. This catches "published in DB but not reaching prompts" — the exact hollow signature — mechanically.
3. **Content-ratio metrics (Phase-S shape, RATIO not count).** Every delivery + eval surface emits a non-empty-content RATIO (`..._delivery_total{result=non_empty|empty_expected|empty_degraded}`, `..._eval_total{decision}`); alerts are ratio/absence-based (robust to multiprocess). Shipping any U4 surface WITHOUT its content metric is rejected at review (the metric names are frozen in G7).
4. **Fail-closed ≠ fail-hollow.** Every degradation path (GUC/scope missing, snapshot stale beyond max-staleness, DB outage, renderer error, eval infra down, suite missing) MUST resolve to a LOUD, MEASURED outcome (explicit error/reject + metric + alert), NEVER a silent empty that reads as healthy. Each ticket's DoD enumerates its degradation paths and their loud outcome.
5. **Liveness heartbeat + EXTERNAL monitor.** The publisher, the snapshot-builder, and the eval emit durable heartbeats; an external monitor alerts on "enabled but no successful run within SLO" (in-process counters can't detect a stopped process — the original 3D-memory sin).
6. **Activation only after signal-proof (U4-J).** Publication stays disabled until the correlation study proves the eval signal is real — never ship a gate that looks active but measures nothing.

### G1. Publication model — **materialized tenant-keyed rendered SNAPSHOT** (supersedes v1 F1)
The publisher (which HAS tenant/project context) writes an immutable, per-scope **rendered snapshot**: the EXACT injected bytes (F5 renderer output, STORED — not just hashed), a `live_set_head` revision integer, and the membership list. The loader reads the snapshot by **explicit `(tenant_id, project_id, audience)` key passed in** (NOT via an RLS GUC that isn't set), with a cached read gated on `live_set_head`. This fixes, in one move: the sync/async mismatch (a cheap keyed cache read, refreshed out-of-band on head change), the GUC-never-set hollow (explicit key + app-level `WHERE`, RLS as defense-in-depth only), the re-render drift (bytes are stored + bound to `renderer_version`), the delivery-path gap (the snapshot carries the catalog metadata below), and the churn (A1's schema now includes the snapshot + head, so B/C don't ALTER it later).
- **Tenant/project scope is an EXPLICIT input to every read**, sourced from the run's scope; on the non-request runner path where contextvar tenant is None, the scope MUST be resolved from the ticket/run record (frozen: a run with no resolvable scope reads only `audience=global` + fires a `scope_unresolved` metric — loud, not silent).
- **The prompt-snapshot registry leak is closed here (BLOCKER):** the existing auto-capture (`prompt_loader.py:1202` → `prompt_registry.py:749`, shared/agent-type-keyed, any-authenticated-reader) MUST either exclude learned-item content or become tenant-scoped. Freeze: assembled prompts containing tenant/project learned items are NOT written to the shared snapshot registry (or are written tenant-partitioned with tenant-scoped read auth). A test proves no tenant learned-item body appears in a cross-tenant snapshot read.

### G2. Schema corrections (supersede v1 F2)
- **`scope_key` is REPLACED by real columns:** `tenant_id → tenants(id)` (`0192`) + `project_id → projects(id)` (`0033`) + `audience enum(tenant|project|global)`, with a CHECK that (audience=global ⇒ tenant_id IS NULL) and (audience=project ⇒ project_id NOT NULL) and a tenant/project consistency FK. Uniqueness = `UNIQUE(audience, tenant_id, project_id, canonical_content_hash)`.
- **All state is append-only EVENTS; there is NO mutable `status` column** (fixes the v1 F2 self-contradiction). Version state, publication state, and supersession are each derived by reducing their event/row streams. `memory_publications` holds append-only `state` events; the live set = the latest-state reduction per version.
- **Store the rendered bytes:** `learned_item_versions.rendered_payload text` (the exact renderer output) + `renderer_version text` alongside `rendered_payload_sha256`. Approval binds the stored bytes, not a hash of ephemeral bytes.
- **Supersession via a live-set HEAD pointer, NOT `superseded_by`** (which can't be filled append-only): drop `superseded_by`; supersession is a publication event (`state=superseded_by:<new_version_id>`) that the latest-state reduction honors — publishing a successor atomically retires the predecessor within the same advisory-locked transaction (G3).
- **Delivery contract columns:** `delivery_mode enum(always_injected|retrieved)` + catalog metadata (`name`, `description`, `trigger_condition`, `keywords[]`) so a `retrieved` item plugs into the existing lazy/vector selection path (`prompt_loader.py:489,609`); v1 retrieved-only means `delivery_mode=retrieved` is the only enabled mode. `eligibility` by kind/audience + the "always_injected requires replay-lane eval (U4-G)" rule are frozen here.
- **The ledger is 7 tables** (v1's "6" was a typo): versions, evidence, eval_runs, eval_cases, approvals, publications(events), transition_events.

### G3. Writer/actor enforcement — HONEST (supersede v1 F3)
Single-credential pool ⇒ a DB trigger is **defense-in-depth, not a hard boundary** (the same credential can forge the approval row a trigger trusts — codex). Freeze the honest layered gate:
- **The real gate is the human-only approval** (`/proposed-actions` → `_assert_human_operator`, U4-D) + the eval-pass evidence; the DB trigger + app-level publisher boundary are defense-in-depth.
- **Publication is an advisory-locked, atomic procedure** (`pg_advisory_xact_lock` on a per-scope key): within one transaction — revalidate `live_set_head`, verify approval+eval-pass+matching membership, insert the `published` event, retire the predecessor, bump `live_set_head`. This defeats the TOCTOU race (two publishers can't both validate-then-insert) and enforces the state machine (no `published` without a prior `publishing`, exactly one active publication per version, idempotent retries keyed on approval_id).
- A separate real publisher process/credential is noted as a FUTURE hardening (needs role infra); v1 does NOT claim DB-role separation.

### G4. `live_set_head` + membership (supersede v1 F4)
`live_set_head` is a per-scope monotonic revision. The **membership snapshot** (a persisted, immutable list of `(version_id, rendered_payload_sha256, delivery_mode, publication_event_seq)`) is what approval and eval bind to; `live_set_hash` = hash of that canonical ordered list. The publisher revalidates `live_set_head` under the advisory lock (G3). The loader cache is keyed on `(scope, live_set_head)` → a head bump is the cross-worker invalidation signal (frozen max-staleness = the head-poll interval; DB-outage on refresh = serve last-good snapshot + fire a `snapshot_stale` metric, loud).

### G5. Anti-injection — HONEST layered defense (supersede v1 F5)
A typed record does NOT make leaf values safe — `procedure_steps=["ignore previous rules…"]` is still an instruction (codex BLOCKER). v1's "a field can NEVER emit a directive" is FALSE and is retracted. Frozen layered defense (no single layer is claimed sufficient):
1. **Structural**: the trusted renderer controls ALL headings/roles/structure; leaf values are inserted as escaped, fenced leaf text under an untrusted-content datamark delimiter (reuse the `INJECTION_GUARD_PRELUDE`/`prompt_hardening` fence) that the payload cannot close (delimiter is nonce-based).
2. **Positional**: rendered below L1/security sections, explicitly lower-authority.
3. **Validator** (reject at INSERT): injection-pattern blocklist (`ignore (previous|above)`, fake headings, trailer/`Co-Authored` spoof, tool-policy, load/`@import`, answer-key-keyword leak).
4. **Eval negative-controls** (U4-H, the real teeth): a candidate whose leaf carries an injection string MUST fail an injection negative-control case → auto-reject; and set-level.
5. **Human approval** reads the exact rendered card (U4-D) before promotion.
Defense-in-depth: layers 1-3 mitigate, layer 4 (eval neg-controls) is the gate, layer 5 is the backstop.

### G6. Tenant/audience/global wiring (supersede v1 F6)
- Reads are scoped by **explicit key + app-level `WHERE`** (primary), with RLS `SET omnisight.tenant_id` as defense-in-depth ON the publisher/snapshot-builder connections (which DO control their session), never relied on for the assembly-path read (which doesn't set it).
- `require_super_admin` is at **`auth.py:1996`** (v1's `db_context.py:37-41` cite was a docstring — corrected). `audience=global` approval MUST invoke `require_super_admin`; the `/proposed-actions` path (today `require_admin`) is extended to require super_admin for global-audience promotion.

### G7. Folded contracts (were unfrozen; now pinned)
- **Metrics (frozen names, RATIO-based):** `omnisight_memory_{quarantine_depth,promotion_total{decision},eval_delta,neg_control_catch_total,failclosed_total{reason},proposal_outcome_total{decision},delivery_total{result},reconcile_divergence_total{scope,reason},snapshot_stale_total}` + the liveness heartbeat gauge. (G0.3)
- **Kill-switch:** `OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED` (the step-0 flag) ALSO gates the snapshot-read (kill-switch off ⇒ loader injects zero learned items instantly, globally — a loud, measured empty_degraded=off, not silent). U4-0b makes the LEGACY path unconditionally dead independent of this flag.
- **Digest (U4-D):** an approval card = `{version_id, kind, audience, scope, eval_run summary (decision, McNemar p, net flips, n), live_set_hash, provenance change-id, rendered card body}`; daily ranked (by effect size × significance), oldest-first, ≤M cards, per-card approval binds its `live_set_hash`, no "approve-all".
- **Cache/revocation:** per G4 (keyed on `(scope, live_set_head)`; revoke = a publication event that bumps head → next read misses → item gone; a test proves a revoked version's bytes are unretrievable from the snapshot).

### v3 — RE-AUDIT FOLD (2026-07-10) — the 4 residual items; freeze-ready
codex re-audit of v2: 6/10 prior BLOCKERs CLOSED (table/status, superseded_by, trigger-circularity, TOCTOU, render-drift, injection-overclaim). 4 residual, each fixed here with codex's prescribed fix:

- **V3.1 Scope propagation (was PARTIAL) — v1 audiences = `tenant` + `global` only; `project` DEFERRED.** Tenant is resolvable on the runner path via `runner_tenant.resolve_tenant_id(labels)` (used by `auto-runner-jira.py`); the orchestrator/chat path resolves to `OMNISIGHT_SELF_TENANT`. `GraphState` has NO project field and no reliable project threading (`state.py`), so `audience=project` is OUT of v1 (add it when a run carries `project_id`). The snapshot loader receives the resolved `tenant_id` EXPLICITLY; unresolved ⇒ read `audience=global` only + fire `scope_unresolved` (loud). No reliance on the RLS GUC for the assembly read.
- **V3.2 Nullable-uniqueness (was STILL-OPEN).** Postgres treats NULLs as DISTINCT, so `UNIQUE(audience,tenant_id,project_id,canonical_content_hash)` would NOT dedupe global items (tenant_id NULL). Replace with **partial unique indexes per audience**: `UNIQUE(canonical_content_hash) WHERE audience='global'`; `UNIQUE(tenant_id, canonical_content_hash) WHERE audience='tenant'`. (project variant added with V3.1's deferral.)
- **V3.3 Snapshot table + lifecycle (was under-specified for A1).** Freeze `learned_item_snapshots(scope_key text, live_set_head bigint, membership jsonb, rendered_bundle jsonb, built_at, built_by)` where `scope_key = audience + ':' + coalesce(tenant_id,'-')`. WHO/WHEN: the publisher, INSIDE the advisory-locked publish txn (G3), writes the new snapshot for the scope and bumps `live_set_head` atomically with the publication event — so ledger and snapshot are updated in ONE transaction (never divergent). The loader reads `learned_item_snapshots` by `(scope_key, current head)`, cached; a MISSING/older snapshot than the ledger head ⇒ `empty_degraded` (loud), never silent.
- **V3.4 Reconciliation — split into TWO invariants (replaces the retrieved-incompatible G0.2 equality).** My G0.2 equality was wrong for retrieved mode (top-k is a legitimate subset → it would page on healthy operation). Corrected:
  - **Publication invariant (the hollow guard, at the SNAPSHOT layer):** at a given `(scope, head)`, `eligible latest-state ledger membership == learned_item_snapshots.membership`. Enforced INSIDE the publish txn (a publish that doesn't produce a matching snapshot ROLLS BACK) AND checked periodically by an EXTERNAL reconciler → `reconcile_divergence_total`. NOT per-read (that would reintroduce the DB hot-path v2 removed).
  - **Retrieved/delivery invariant (at the injection layer):** injected item IDs are a hash-valid SUBSET of the snapshot membership and obey the top-k/truncation rules. A no-relevant-match = `empty_expected` (normal); a missing/corrupt/stale snapshot = `empty_degraded` (loud). This is what makes "published but not delivered" detectable WITHOUT false-paging on legitimate top-k selection.
- **V3.5 Cross-tenant snapshot-registry leak (was PARTIAL) — freeze the simple fix:** when the assembled prompt contains non-empty learned-item content, SKIP the shared auto-capture (do not call `_schedule_prompt_snapshot`, `prompt_loader.py:1202`) — the assembled body with tenant content is never written to the shared any-reader registry. (Tenant-partitioning the registry is the heavier alternative, deferred.)
- **V3.6 Kill-switch metric dimension:** `delivery_total{result='empty_expected',reason='kill_switch_off'}` + an `omnisight_memory_enabled` gauge; reconciliation/absence alerts are SUPPRESSED while `OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED` is intentionally off — a bug remains `empty_degraded` (loud). So "intentionally off" and "broken" are distinguishable, not conflated.

**A1 migration MUST contain (so B/C/D/F don't ALTER):** `learned_item_versions` (id, canonical_content_hash, kind, audience, tenant_id FK, project_id FK[nullable, deferred use], payload jsonb, rendered_payload text, renderer_version, rendered_payload_sha256, created_at, created_by, delivery_mode, name/description/trigger_condition/keywords[]) + partial-unique indexes (V3.2) + `learned_item_evidence` + `memory_eval_runs` + `memory_eval_cases` + `memory_approvals` (incl live_set_hash) + `memory_publications` (append-only state events) + `memory_transition_events` + `learned_item_snapshots` (V3.3) + the append-only trigger (0234 shape, PG+SQLite dialect-split). No mutable status column anywhere.

### G8. Verdict of this fold
The freeze is now internally consistent, churn-resistant (A1's schema carries the snapshot/head/rendered-bytes/delivery/scope columns so B/C/D don't ALTER), honest about security + injection (no false "never" claims), and **anti-hollow by construction** (reconciliation invariant + content ratios + loud-not-silent degradation + external liveness). RE-AUDIT this v2 fold (one codex pass) before cutting U4-A1; the previous BLOCKERs (GUC-hollow, snapshot-leak, scope_key, table/mutation contradiction, superseded_by, trigger-circularity, TOCTOU, render-drift, injection-overclaim) are each addressed above — verify none re-opened.

---

## v1 (pre-audit) — retained for provenance; superseded by the v2 fold above
- **Status**: DRAFT (pre-audit). This is the **freeze**: once audited + accepted, U4-A1…J are cut against these decisions and do NOT re-open them. codex made this a BLOCKER — a migration cannot be frozen while these are open.
- **Reads-with**: forward design v2 (`docs/design/2026-07-10-phase-u4-v3-substrate-forward-design.md`), audit fold §§E-F.
- **Scope**: DECISIONS + SCHEMAS + CONTRACTS only. No implementation. Full scope (incl. always-injected + the replay phase) per operator 2026-07-10.

---

## F1. Publication model — **the approved DB view IS the live set** (no file writes)

Promoted learned items do NOT get written to `configs/skills/` (the legacy path stays untouched for legacy skills). A promoted learned item lives in the DB; the "live set" is a **view** joining only `approved ∧ published ∧ non-revoked` versions, RLS-filtered. `prompt_loader` gains an ADDITIVE learned-item source that reads this view and renders each item through the trusted renderer (F5) at prompt-assembly time.

Rationale (audit-verified): no runtime outbox / content-addressed store / manifest verification exists; cosign is CI-only; a whole-loader manifest breaks CLAUDE.md + `~/.claude`/project-memory inheritance (~40% of `prompt_loader.py`). The DB-view model makes publication + **revocation atomic** (a state row flip changes what the view returns) — which is the only way "reversible" is real. Legacy `configs/skills` and the project-memory walk are UNCHANGED.

**Consequence for the interim interlock (U4-0b)**: the legacy FS-writing promote endpoints (`auto_skills.py`, `skills.py`) are being RETIRED, not gated — U4-0b makes them unconditionally dead in prod; the DB-view publisher (U4-C) is the only writer to the live set.

## F2. The full append-only ledger — **6 tables, all owned by U4-A1**

All tables: INSERT-only for producers; state changes are NEW rows (never UPDATE-in-place) except a single controlled `status` column guarded by the F-trigger (F3). Reuse the `0234` `BEFORE UPDATE/DELETE → RAISE EXCEPTION` append-only trigger shape and the `0193` RLS-via-`current_setting('omnisight.tenant_id')` shape. `tenant_id … REFERENCES tenants(id)` per `0192`.

1. **`learned_item_versions`** — `id uuid pk`, `canonical_content_hash text` (sha256 of the typed payload EXCLUDING provenance/timestamps; UNIQUE per `(audience, scope_key, canonical_content_hash)`), `kind` enum(lesson|skill|playbook), `audience` enum(tenant|project|global), `tenant_id` (null iff audience=global), `project_id`/`repository` (null unless scoped), `payload jsonb` (the F5 typed record), `rendered_payload_sha256 text`, `superseded_by uuid` (self-FK, null), `created_at`, `created_by text`. Immutable (append-only trigger; an edit = a new version row with a new hash, which invalidates prior evals/approvals bound to the old hash).
2. **`learned_item_evidence`** — `id`, `version_id fk`, `ground_truth_kind` enum(merged|ci_pass|review_plus2|reverted|stoploss), `source_change_id text` (Gerrit change / JIRA key — the AUTHENTICATED reference, F-provenance U4-E), `verified_at`, `revert_state` enum(none|reverted), `evidence_span jsonb`.
3. **`memory_eval_runs`** — `id`, `version_id fk`, `eval_kind` enum(plan_triage|replay), `suite_sha256`, `live_set_hash text` (F4), `model_fingerprint text`, `decision` enum(promote|reject|insufficient_evidence|infra_invalid), `stat_summary jsonb` (McNemar p, net discordances, n, per-slice), `ran_at`.
4. **`memory_eval_cases`** — `eval_run_id fk`, `case_id`, `baseline_pass bool`, `candidate_pass bool` (the PAIRED per-case cells McNemar consumes; also stores cluster/N-sample detail).
5. **`memory_approvals`** — `id`, `version_id fk`, `eval_run_id fk`, `live_set_hash text` (the EXACT set approved-against — F4), `approved_by text` (human accountId), `approved_at`, `digest_id text`.
6. **`memory_publications`** — `id`, `version_id fk`, `approval_id fk`, `state` enum(approved|publishing|published|publish_failed|revoked), `published_at`, `revoked_at`, `revoked_by`, `revoke_reason`. The live-set view = `state=published` minus any later `state=revoked` for the same version.
7. **`memory_transition_events`** (audit) — `id`, `version_id`, `from_state`, `to_state`, `actor`, `at`, `reason`. Every transition appends one row.

## F3. Writer/actor enforcement — **single role + conditional transition trigger** (NOT separate DB roles)

This deployment is a single-credential pool (audit-verified: zero `CREATE ROLE`/`GRANT` in 121 migrations). So:
- Producers INSERT `learned_item_versions` (status implicitly "quarantined" = no publication row) only.
- The ONLY legal path to a `published` row is: a `memory_approvals` row exists (F human-only, U4-D) AND its `eval_run.decision='promote'` AND its `live_set_hash` matches the current live set. Enforce with a **`BEFORE INSERT` trigger on `memory_publications`** that RAISES unless a matching approved+eval-passed row exists (plpgsql joining approvals+eval_runs). App-level actor separation on top (the publisher is a distinct code path). DB-enforced *transition*, single role — achievable, matches the `0234`/`0235` trigger precedent.

## F4. Fingerprint bound at approval — includes **`live_set_hash`**

Every eval run and approval binds: `{version_id, canonical_content_hash, rendered_payload_sha256, suite_sha256, live_set_hash, model_fingerprint, prompt_assembly_fingerprint}`. **`live_set_hash`** = sha256 of the sorted set of currently-`published` version ids the candidate was evaluated + approved against. The publisher REVALIDATES `live_set_hash` at publish time; if the live set changed since approval, publication fails → re-eval/re-approve. This defeats the "combine individually-approved items into an unapproved batch" attack (codex) and forces set-level regression (U4-F) to be meaningful.

## F5. Constrained record + trusted renderer (the anti-injection core)

- **Typed record** (the `payload jsonb`): fixed keys only — `scope`, `preconditions[]`, `procedure_steps[]`, `verification`, `known_failures[]`, `prohibited_actions[]`, `evidence_references[]`. No free-form top-level markdown.
- **Trusted renderer**: converts the record to prompt text through a FIXED template the renderer fully controls (all headings/structure are the renderer's; field values are inserted as escaped/fenced leaf text). A field value can NEVER emit a heading, role directive, or tool-policy line — the renderer does not pass raw markdown structure through.
- **Datamark/fence**: the whole rendered block is wrapped in an untrusted-content delimiter (the INJECTION_GUARD-style fence) so the model treats it as data-with-lower-authority, below L1/security sections.
- **Validator** (reject at INSERT): any field value matching injection patterns (`ignore (previous|above)`, fake `# Core Rules`/security headings, `Co-Authored`/trailer spoofing, tool-policy edits, load/`@import` directives, or ≥K suite-answer-key keywords) → reject the version. `rendered_payload_sha256` is computed from the renderer output (so approval binds the EXACT rendered bytes).

## F6. Tenant / audience / global

- `audience ∈ {tenant, project, global}`. tenant/project versions are RLS-scoped (injected only for the matching `current_setting('omnisight.tenant_id')`/project); the live-set view enforces this — tenant content is NEVER exposed cross-tenant and NEVER written to a shared FS namespace.
- **global** audience requires **super_admin** approval — bind U4-D's approval for `audience=global` to the real `require_super_admin` (`db_context.py:37-41`), not ordinary admin. A tenant-admin can approve at most tenant/project audience.

## F7. Frozen invariants (do not re-open downstream)
1. Live set = DB view (F1); no learned-item file writes; legacy FS path retired by U4-0b.
2. 6-table append-only ledger (F2); producers INSERT versions only; `published` only via the F3 trigger.
3. Approval binds `live_set_hash` (F4); publisher revalidates it.
4. Promoted content is a typed record rendered by the trusted renderer + fenced (F5); raw markdown is never promoted.
5. Revocation = a `revoked` publication row → the view drops it → next assembly (keyed on live_set_hash → cache miss) omits it; a test proves a revoked hash is unretrievable.
6. audience scope + super_admin-for-global (F6).
7. Every state change appends a `memory_transition_events` row.

## F8. What this unblocks (ticket bindings)
- **U4-A1** = migrate the 7 tables + canonical-hash + append-only trigger + audience/tenant columns + immutability. **U4-A2** = typed record + trusted renderer + fence + validator + `rendered_payload_sha256`.
- **U4-B** = the F3 `memory_publications` transition trigger + app-level publisher boundary.
- **U4-C** = the live-set VIEW + the DB-view publisher (approved→publishing→published) + the revocation action + the additive `prompt_loader` learned-item source (namespace-scoped).
- **U4-D** = `memory/promotion` approval kind binding `{version, eval_run, live_set_hash}`, super_admin for global, digest.
- **U4-E** = server-derived `learned_item_evidence` (authenticated Gerrit/JIRA lookup + reverify).
- **U4-F/H** = eval runs/cases writing `memory_eval_runs`/`_cases` with the F4 fingerprint + suite manifest + set-level regression on `live_set_hash`.

**NEXT**: audit THIS freeze (3-way incl codex) → accept → cut U4-0b (parallel, independent) + U4-A1.
