# Phase U4 — Eval-Gated Promotion Harness + Quarantine Substrate (Phase U safety rails)

- **Date**: 2026-07-10
- **Status**: v2 — AUDIT FOLDED (2026-07-10). Two adversarial audits (safety lens + correctness/cost/feasibility lens, both code-grounded) + my verification pass. Verdict **GO-WITH-CHANGES**. Codex leg unavailable (CLI/model/auth mismatch — see fold §D). The v2 fold below is AUTHORITATIVE where it contradicts the original §§1-8.

---

## v2 — AUDIT FOLD (2026-07-10) — authoritative

**Both auditors: GO-WITH-CHANGES.** The substrate (quarantine + provenance + fail-closed + human sign-off + proposal + metrics) is sound, ~70% composed from tested primitives, and offline-buildable (the `test_finetune_eval.py` mock-`ask_fn` pattern is the proven template). But the design as drafted ships on a **false premise** AND with a **statistically unsound eval method**. Both are fixable; the direction survives. Must-fix before ticketing:

### A. THE PREMISE WAS WRONG — U4 must CLOSE AN EXISTING LIVE HOLE, not add a dormant gate
- **A1 (BLOCKER, code-verified):** the promote-to-injected-set path already EXISTS and is UNGATED. `backend/routers/auto_skills.py:351 POST /{id}/promote` (`require_admin`) writes the distilled markdown straight into `configs/skills/<slug>/SKILL.md` (`:385`) + vectorises it into retrieval memory (`:420`) — which `prompt_loader.build_system_prompt` injects (`prompt_loader.py:77,300-308,1083-1121`). No eval. The design's "injection not reachable yet / one future human-owned gate" (orig §3-E/§3-H) is REFUTED. **And prod runs `OMNISIGHT_SELF_IMPROVE_LEVEL=l1`** (`/home/user/omnisight-prod/.env:196`) so the distiller path is not fully dormant. ⇒ **U4 = REPLACE that promote transition**: `promote` must hard-require `status=reviewed` ∧ eval-PASS ∧ an *approved* `memory/promotion` proposal id ∧ `_assert_human_operator`; fail-closed if any absent. Shipping U4 "dormant" is NOT acceptable — it leaves the hole open. First ticket closes the hole (even as a flag-gated interim: block the existing endpoint unless eval-PASS+human-approved).
- **A2 (BLOCKER, code-verified):** the "human approval" U4 counts on is BOT-APPROVABLE. `backend/routers/decisions.py:172-193 approve_decision` = `_require_decision_token` + `require_operator` only — NOT `_assert_human_operator`. The real human-only gate (403s `apikey:*`/`*-bot`/`ai-*`) lives ONLY in `backend/routers/proposed_actions.py:62,88` → `backend/api/release_approval.py:78-99`. ⇒ `memory/promotion` approval MUST route through the `/proposed-actions` P5 endpoint (or add `_assert_human_operator` to the promotion-kind approve path). "Decision Engine = human-only" is FALSE; only `/proposed-actions` is.
- **A3 (HIGH):** writer/actor separation is COSMETIC as designed (distiller, promote endpoint, resolver all share one process + DB pool + creds; migration 0192 says "transitions remain application-owned"). ⇒ enforce at the DB: a separate producer role with INSERT-only on `learned_item_quarantine` (no UPDATE, no `configs/skills` write), + a `BEFORE UPDATE` trigger rejecting any `status→promoted` without a matching human-approved proposal ∧ `eval_neg_control_ok ∧ eval_delta>=threshold`. Make the illegal transition impossible at the DB, not just unlikely in Python.
- **A4 (HIGH):** provenance `ground_truth_label` is producer-supplied FREE TEXT → forgeable (a buggy/compromised distiller writes `merged`/`ci_pass` for a poisoned item). ⇒ derive the label SERVER-SIDE from an authenticated Gerrit/JIRA lookup keyed by `origin_ticket`/change-id at INSERT, or store the change-id and RE-VERIFY at promotion time; reject rows whose claimed label can't be re-confirmed.
- **A5 (HIGH):** injection-via-candidate-payload — `scrub()` strips secrets only, not instruction-injection. The candidate text becomes future system-prompt content. ⇒ datamark/fence promoted payloads in an untrusted-content delimiter, STRIP markdown headers that mimic the L1/security sections, and add injection-pattern NEGATIVE CONTROLS to the suite (a candidate containing "ignore previous instructions" / fake `# Core Rules` must auto-reject). Scrub alone is insufficient.

### B. THE EVAL METHOD IS STATISTICALLY UNSOUND AS SPECIFIED — redesign the scorer
- **B1 (BLOCKER):** +3pp threshold at N=10-20 is BELOW single-case granularity (one flip = 5-10pp) AND below the run-to-run noise floor — and the eval path samples at **T=0.3** (`config.py:79`), no per-call override, so the SAME item promotes one night, rejects the next. ⇒ redesign: **paired per-case comparison + McNemar exact test** (the two arms are the same model on the same prompts ± the item = naturally paired; unpaired subtraction throws away ~2× variance reduction) at fixed α; **N-sample majority vote per case** (k≈5) to stabilise each Bernoulli cell; **pin temperature=0 + fixed seed for the eval `ask_fn` specifically**; and be honest that a 10-20 case suite can only resolve LARGE effects (~+15pp / ≥3 net case-flips), not +3pp — grow the suite (its own header targets 100) or raise the effect-size bar. Mirror the in-repo precedent `learning_loop.py:112 DEFAULT_GRADUATION_MIN_SAMPLES=3` (consistency requirement before promoting).
- **B2 (BLOCKER):** the reused `compare_models` decision rule is INVERTED — it is a NON-REGRESSION gate (`promote iff delta >= -pp`, accept a different model if not much worse; `finetune_eval.py:133-134`). U4 needs an IMPROVEMENT gate (`promote iff delta >= +pp`). ⇒ write a NEW scorer (`memory_promotion_eval.py`), do NOT reuse `compare_models` verbatim (orig §3-C "generalise the candidate" was wrong — it's a rewrite + sign-flip; the candidate item must be injected into the `ask_fn` prompt slot with the model held FIXED, not swapped as a model id).
- **B3 (HIGH, construct validity):** the grader is trivially keyword-gameable — `IQQuestion.matches` is case-insensitive substring-AND, and the one shipped hold-out uses SINGLE-token positive matchers with ZERO negative controls; an injected item that enumerates the answer key is the globally optimal reward-hack. ⇒ enforce AT SUITE-LOAD: reject any case with a single-token positive matcher; require ≥1 `forbidden_keywords` per case + ≥N distractor cases; reject any candidate whose payload literally contains ≥K of the suite's expected keywords (answer-key memorisation). PLUS a one-time **construct-validity correlation study** (operator, post-U1): prove plan-delta actually predicts real-ticket ground-truth outcomes BEFORE trusting the metric — make it a precondition, not a nice-to-have. Also fix `matches()` doc/code drift + make forbidden-keyword matching word-boundary-aware ("reject" shouldn't trip on "rejection").
- **B4 (HIGH, cost):** naive nightly ≈ 80K tok/candidate at k=1, 200-400K at k=5; × U1's dozens/night = millions/night — unaffordable un-bounded. ⇒ design INVARIANTS: baseline answers CACHED/frozen per `(suite_version, model)` so only the candidate arm runs per candidate (also removes baseline nondeterminism from the delta); two-stage gate (k=1 first, k=5 only near the decision boundary); hard per-night token budget (reuse `iq_runner` budget + `truncated_at`); dedupe candidates via the existing `_markdown_sha256`; cap candidates/night, overflow QUEUED not dropped.
- **B5 (offline-AC wording — stoploss protection):** two ACs leak an impossible green. Reword orig §6 Integration "scorer produces a deterministic delta" → "…deterministic GIVEN THE INJECTED MOCK `ask_fn`" (a real eval-agent is never deterministic — that's B1); reword Code "wire promote path to require eval-PASS+approval" → "promote path BLOCKS when `eval_status != pass OR proposal != approved`, verified with BOTH mocked to fail and to pass." Everything touching a REAL `ask_fn` is Exercised/operator-only. This is the exact wording that stops a runner chasing a green the sandbox can't produce (the S3 stoploss lesson).
- **B6 (scope/validation):** "apply U4 to existing `auto_distilled_skills` drafts first" may be applying to a near-EMPTY table (self-improve historically off) → validates PLUMBING, not SIGNAL. ⇒ verify the real draft inventory first; ship a first-class MANUAL golden-candidate slice (3-5 known-good + 3-5 known-bad hand-authored candidates) run end-to-end on the live stack as the Exercised proof; be honest U4 ships structurally-validated but signal-unvalidated, signal-proof gated on U1's real candidate stream.
- **B7 (open Q4 resolved):** prefer a NEW `learned_item_quarantine` table over generalising `auto_distilled_skills` (skill-specific, single-kind, has a live producer + BP.M.3 promote surface — bolting kind+provenance+eval+decision columns on is a riskier migration). Add an FK/adapter FROM the skill path INTO the quarantine table.

### C. Premise-table corrections (orig §3)
- §3-B: `compare_models` files NOTHING; `finetune_nightly.py` files the proposal, and only on REJECT (regression) — U4's proposal-on-PASS is a NEW path.
- §3-C: `compare_models` is welded to model-id comparison; the with/without-item scorer is a NEW harness + inverted rule (see B2).
- §3-E/§3-H: REFUTED — the human-owned promote gate HAS landed (`auto_skills.py`) and already injects, ungated (see A1). The `skill_distiller.py:316-317` "when it lands" comment is STALE.
- §3-F: `is_safe_to_promote` only checks `sum(scrub_hits) <= 25` — a secret-leak gate, NOT semantic/injection review. Keep it but AND it with eval-PASS + proposal-approved.
- §3-I: CONFIRMED and robust — but it guards ONLY `/proposed-actions`, not the promote/approve path U4 uses (see A2).

### D. Codex leg — UNAVAILABLE (tooling)
`~/.codex/config.toml` pins `model = "gpt-5.6-sol"` (rejected: "requires a newer version of Codex" than the installed CLI v0.128.0) + `model_reasoning_effort = "max"` (invalid variant; fixed to `xhigh`). Override to `gpt-5-codex` also failed ("not supported when using Codex with a ChatGPT account"). The valid model set for this CLI+auth is unknown; codex is unusable until the operator upgrades the CLI or sets a supported model. The 3rd audit leg was substituted with a second diverse subagent lens.

### E. CODEX THIRD-LEG AUDIT — VERDICT: NO-GO (2026-07-10, codex v0.144.1 gpt-5.6-sol)
The codex leg (now working) went deeper than both Claude subagents and returns **NO-GO — not build-ready** (34 findings). It confirms A1-A5/B1-B7 and adds structural must-haves the Claude lenses missed. Spot-verified by me (3/3 citations accurate):
- **E1 (verified) — TWO live ungated promotion endpoints, not one:** `auto_skills.py:351` AND `skills.py:98` (`/pending/{name}/promote`, `shutil.move`→`configs/skills/`). Both `require_admin`, no eval.
- **E2 (verified) — api-keys ARE admins:** `auth.py:1848-1849` returns `User(id="apikey:…", role="admin")` for any valid bearer key ⇒ `require_admin` passes for ANY bot/service principal; neither promote endpoint calls `_assert_human_operator`. Combined with prod `l1` (producers active) = a LIVE self-injection surface any api-key can drive; a prompt-injected runner could escalate to it. Decision-Engine approve is also non-human-only + can AUTO-RESOLVE by mode/rule (`decisions.py:172`, `decision_engine.py:1270`, codex-claimed, unverified).
- **E3 — eval/approval NOT bound to immutable content (TOCTOU):** reviewed rows stay editable (`auto_skills.py:225` PATCH blocks only `status==promoted`) → evaluate N, edit to N+1, promote on stale evidence. FIX: immutable candidate versions; bind every eval/approval/publication to `{candidate_version_id, payload_sha256, rendered_payload_sha256, suite_sha256, live_set_sha256, model/prompt/config fingerprint}`; any edit → new version, invalidates prior evals/approvals.
- **E4 — publication NOT atomic; "status flip = reversible" is FALSE:** FS write outside txn + best-effort vectorization = two non-transactional sinks a row-flip can't undo (`auto_skills.py:383`). FIX: approved DB view = source of truth, OR content-addressed immutable artifacts via transactional outbox + signed manifest; loader reads only manifest hashes; explicit `approved→publishing→published|publish_failed→revoked`.
- **E5 — tenant isolation ABSENT + broken:** schema has no tenant/project/audience scope; `auto_distilled_skills` rows ARE tenant-scoped but promotion writes into the GLOBAL `configs/skills` tree `prompt_loader` scans unfiltered ⇒ a tenant-admin turns tenant content into global guidance. FIX: non-null tenant/project/repo/audience + RLS; global item needs PLATFORM (super_admin) approval; never publish tenant content to a shared FS namespace.
- **E6 — arbitrary Markdown IS executable prompt policy:** lazy skill body → fresh late `SystemMessage` (`nodes.py:856`); `prompt_registry.py:90` resolves arbitrary paths. FIX: DON'T promote free Markdown — use a CONSTRAINED SCHEMA (scope/preconditions/procedure_steps/verification/known_failures/prohibited_actions/evidence_references) rendered through a trusted template; reject role/priority/security-heading/tool-policy/load directives. (Supersedes A5's "datamark/fence".)
- **E7 — eval method deeper problems:** (a) plan/answer keyword ≠ ticket success — plan-scoring is TRIAGE ONLY; always-injected items need FROZEN HISTORICAL-TICKET REPLAYS at fixed commits w/ executable tests + policy checks; (b) +3pp at N=10-20 is below one-case granularity — McNemar needs 6/0 discordances (=+30pp@N=20); detecting a true +3pp@80% power ≈ 428-1736 cases ⇒ at 10-20 cases the ONLY honest outcome is `insufficient_evidence`, NEVER promote; (c) k=5 majority = PSEUDOREPLICATION (5 samples of one case ≠ 5 independent obs; 0/5 harm → 95% upper bound on harm still ~45%) → spend budget on MORE DISTINCT CASES + cluster analysis; (d) evaluator not pinned + provider FAILOVER (`llm.py:1209`) silently swaps model behind the label → need a dedicated no-failover eval client w/ recorded request IDs; (e) forced full-candidate injection ≠ live delivery (eager cap 4000 / lazy catalog-select / vector truncated ~240 in runner) → eval the ACTUAL rendered+selected artifact; (f) public in-repo suite = an ANSWER KEY the coding agent can read → need a PRIVATE access-separated holdout + multiple-testing correction; (g) item-by-item misses TOXIC INTERACTIONS → set-level regression before publishing a batch.
- **E8 — infra failure → fraudulent positive delta:** `run_benchmark` scores exceptions/timeouts/truncation as failed-but-returns-a-score (`iq_runner.py:56`) → baseline flaky + candidate recovers = fake improvement. FIX: any error/timeout/fallback/truncation/empty-arm ⇒ whole eval `infra_invalid` (retry quarantined; NEVER semantic-reject or propose).
- **E9 — cost reality:** live-shaped k=5 ≈ $51.75/candidate; 30/night ≈ $776-1550/night; sequential 200 calls×~30s ≈ 100 min/candidate (can't fit a nightly window). Needs hard $ budget + admission control + bounded concurrency + canonical-hash dedupe (raw sha256 fails — payloads carry timestamps).
- **E10 — fail-closed/audit gaps:** `load_all()` returns `[]` on missing dir + skips malformed YAML (`iq_benchmark.py:171`) = silent suite-weakening → need a STRICT manifest loader (one bad shard invalidates all); in-repo checksum has no trust root (bind to signed release); in-process metrics ≠ liveness → external heartbeat monitor "enabled candidates exist but no successful eval within SLO"; single `eval_delta_pp` column overwrites history → append-only tables (`learned_item_versions/_evidence/_eval_runs/_approvals/_publications/_transition_events`).
- **Premise re-level:** codex says "~70% composed from tested primitives" is OVERSTATED — the safety-critical core (representative scorer, statistical protocol, private holdout, immutable version binding, DB authority separation, `memory/promotion` approval kind, atomic publication, tenant RLS) is ALL NEW. P5 is NOT drop-in reusable: `/proposed-actions` producer only accepts deploy/promote/restart/rollback (`tools.py:3785`), executor doesn't know `memory/promotion` (`action_executor.py:44`) → approving it ends `refused`.

### F. RE-SEQUENCING (codex #32, adopted)
"Gate-first" is coherent ONLY as an EMERGENCY INTERLOCK. New order:
0. **NOW (urgent, small): emergency deny-by-default interlock** — block BOTH promote endpoints (`auto_skills.py:351`, `skills.py:98`) + the decisions.py skill-promote approve unless human-only (`_assert_human_operator`) AND (interim) a feature-flag default-deny. Closes the live hole while the substrate is built. Standalone safety ticket.
1. Build the immutable substrate (candidate-versions + hashes + append-only eval/approval/publication + transactional outbox + tenant/RLS + real credential/process separation).
2. Real eval (plan-triage + historical-ticket-replay + private holdout + powered paired stats + infra_invalid handling).
3. Migrate BOTH existing producers into the substrate (canonical new table; `auto_distilled_skills` → thin FK/view, no dual truth).
4. Live pilots (hand-authored known-good/bad/injection/stale/failure + historical replays).
5. Enable publication ONLY after signal-validation (correlation study proving plan-delta predicts ground truth).
6. Full U1 producer later. (A minimal U1 slice — the provenance-verification adapter + candidate schema — should ride with step 1 so ingestion assumptions get tested.)

**STATUS: design is NO-GO as written; full v3 redesign PENDING a user scope decision (emergency interlock now? + how large a substrate build to commit to). Do NOT ticket the substrate until re-scoped.**

### Net (superseded by E/F above)
Direction confirmed. U4 reframed from "dormant gate" → "**close the live ungated self-injection hole** (`auto_skills` promote) with a DB-enforced, human-only, eval-gated, fail-closed transition, injecting a redesigned paired-McNemar/N-sample/cached-baseline scorer with anti-gaming suite controls, validated end-to-end on hand-authored golden candidates, signal-validation deferred to a post-U1 correlation study." Ticket-split should lead with the hole-closure + substrate (offline-buildable, high-value) and treat the scorer as the delicate piece needing the anti-gaming + statistical rigor.

---

### (Original pre-audit draft follows — superseded by the v2 fold above where they conflict)
- **Author**: Sora
- **Supersedes/relates**: `project_3d_memory_revival` roadmap (R→S→U); Phase R (structural axis LIVE), Phase S (anti-hollow tripwire LIVE). Literature: `docs/research/2026-07-07-self-improving-agents-survey.md`, `docs/research/2026-07-07-agent-memory-architectures-survey.md`. P5 supervised execution (`project_p5_supervised_execution`) supplies the human-sign-off primitive.

---

## 0. One-paragraph thesis

Phase U makes runners "truly autonomously evolve and learn." Every published result says the same thing: **nobody ships an ungated self-judged learning loop in production** (Reflexion regressed MBPP 77.1<80.1 on self-judgment; DGM faked its own tests; Misevolution shows even *benign* memory accumulation decays safety). So the FIRST thing Phase U builds is not the learning — it is the **gate that makes learning safe to ship**: an eval-gated promotion harness with a quarantine staging area and a human sign-off, so that **no learned item can reach the always-injected / retrieved set without passing a frozen golden-suite eval (incl. negative controls) AND a human +2.** Build the rails before the train. This is the direct continuation of the user's standing directive — "make it structurally impossible to silently break" — applied to the memory system's write path.

## 1. Why U4 is first (and why it is the ONLY safe first)

- The rest of Phase U (U1 experience loop, U2 playbook tier, U3 lifecycle, U6 Sora semantic memory) all **write into sets that get injected into future prompts.** Injecting a learned item with no eval gate is precisely the ungated-reflection failure mode the literature universally avoids.
- A "quick win" like wiring the already-distilled `auto_distilled_skills` drafts into prompts (they exist but are orphaned) is **unsafe without U4** — it is ungated injection by another name.
- Therefore U4 (the gate) strictly precedes U1/U2/U6. The roadmap already sequenced it first; the 2026-07-10 codebase recon confirms it is sound and mostly an EXTENSION, not greenfield.

## 2. What U4 IS / IS NOT (scope fence)

**IS:**
1. A **quarantine + provenance** staging table for candidate learned items (the only place automated distillation may write).
2. A **frozen golden-suite eval** (deterministic graders first, incl. negative controls) that scores a candidate item by its *effect on graded outcomes*.
3. A **promotion gate** = eval-pass AND human sign-off (Decision-Engine proposal → human approve), reusing the P5 supervised-execution primitive (propose-only for bots; human approves).
4. Ships **dormant** (flag-gated) **with metrics** (anti-hollow: fail-closed + observable), applied FIRST to the already-existing skill draft→promote path.

**IS NOT (deferred, explicitly out of this increment):**
- U1 experience-loop / completion-time reasoning distillation (that *produces* candidates; U4 only *gates* them). U4 ships with a manual/CLI candidate-injection path for testing; U1 wires the automated producer later.
- U3 lesson lifecycle (decay/utility/supersede/re-validate).
- U5 Graphiti temporal axis.
- U6 Sora 3-tier persistent memory (reuses this quarantine/provenance + pending-confirm substrate later).
- Full-ticket-execution eval (expensive, nondeterministic). U4 v1 uses the deterministic **plan/answer-level** proxy the literature endorses ("deterministic graders first").

## 3. Existing infrastructure U4 EXTENDS (grounded anchors, 2026-07-10 recon)

| Primitive U4 needs | Already exists | Anchor | Gap U4 fills |
|---|---|---|---|
| Deterministic golden grader incl. negative controls | `IQQuestion.matches()` — `expected_keywords` (AND), `expected_regex`, `forbidden_keywords` | `backend/iq_benchmark.py:47-95` | Reuse verbatim as the grader; author a golden **suite for learned-item effect** (new content, same schema). `forbidden_keywords` = negative controls. |
| Baseline-vs-candidate delta gate + human proposal | `compare_models()` → `EvalResult(delta_pp, decision∈{promote,reject,no_baseline})`; files Decision-Engine `finetune/regression` proposal on reject | `backend/finetune_eval.py:79-138` | Generalise the *candidate* from "a model" to "a prompt-set WITH vs WITHOUT the learned item"; file a **`memory/promotion` proposal** for human sign-off on PASS (not just reject). |
| Frozen hold-out set loader | `load_holdout()` + `configs/iq_benchmark/*.yaml` | `backend/finetune_eval.py:65`, `configs/iq_benchmark/holdout-finetune.yaml` | Add a **frozen** learned-item golden suite (versioned, checksummed) alongside. |
| Quarantine-shaped draft staging + human-owned promote | `skill_distiller` writes `status:draft` rows to `auto_distilled_skills`; "review/promote gate remains human-owned" (BP.M.3); `is_safe_to_promote()` scrub gate | `backend/skill_distiller.py:9-22,61-94`; `backend/skills_scrubber.py:96` | Add the **missing eval dimension** to that promote gate + generalise the staging table to all learned-item kinds with provenance. |
| Human sign-off / propose-only-for-bots | P5 supervised-execution Decision Engine (Sora proposes, human admin approves; bots 403) | `project_p5_supervised_execution` memory; `backend/orchestrator` decision path | Reuse as the promotion approver (dual-gate, Gerrit-+2 shape). |
| Self-improve master flag | `OMNISIGHT_SELF_IMPROVE_LEVEL` (off\|l1\|all) gates the distiller | `backend/skill_distiller.py:94` | Reuse; U4 ships behind it, default `off`. |
| Always-injected assembly (promotion TARGET) | `build_system_prompt()` ordered sections (L1/security/role/task…) | `backend/prompt_loader.py:951-1200` | U4 does NOT change injection yet; it only governs what is *eligible*. U2 later adds the playbook section that reads promoted items. |

**Takeaway:** ~70% of U4 is composition of existing, tested primitives. The genuinely new pieces are (a) the quarantine table + provenance schema, (b) the learned-item golden suite, (c) the "inject candidate into the prompt-set and re-score" harness, (d) the `memory/promotion` proposal type.

## 4. Design

### 4.1 Quarantine + provenance table (`learned_item_quarantine`)
New Postgres table — the **only** sink automated distillation may write to. Columns (draft):
- `id` (uuid), `kind` enum(`lesson`|`skill`|`playbook`), `payload` (the candidate text/markdown, scrubbed via existing `skills_scrubber.scrub`),
- **provenance** (non-null): `origin_runner`, `origin_ticket`, `origin_session`, `distilled_by` (model id), `ground_truth_label` (the Gerrit/JIRA outcome that justified it: `merged`|`abandoned`|`reverted`|`ci_pass`|`review_plus2`|`stoploss` — NEVER self-judgment), `created_at`,
- `status` enum(`quarantined`|`promoted`|`rejected`|`superseded`) default `quarantined`,
- eval result: `eval_delta_pp`, `eval_neg_control_ok` (bool), `eval_suite_version`, `eval_ran_at`,
- decision: `decided_by` (human accountId), `decided_at`, `decision_reason`.
Invariant: rows are INSERT-only for producers; status transitions only via the governed promotion job. **Writer/actor separation (Letta):** the distilling agent/runner has an INSERT grant to `quarantined` only — it can never write `promoted` nor touch the live set.

### 4.2 Frozen golden suite (`configs/learned_item_eval/*.yaml`, versioned + checksummed)
- 10–20 cases per learned-item **domain** (per OpenAI eval-skills template), each = `{context_prompt, expected_keywords/expected_regex (positive), forbidden_keywords (negative control)}` reusing `IQQuestion`.
- **Negative controls are mandatory**: cases where the *correct* behaviour is to NOT change / to reject a plausible-but-wrong candidate. A candidate that trips a forbidden_keyword on any negative control → auto-reject regardless of positive delta.
- The suite is **frozen**: loaded read-only, content-hashed; `eval_suite_version` recorded on every decision so a suite change forces re-validation (ties to U3 later).
- Suite lives in-repo (reviewable, Gerrit-gated to change) — the suite itself is human-curated, never auto-generated (avoids the iq_benchmark self-reference-bias trap, `iq_benchmark.py:1`).

### 4.3 The scorer (`memory_promotion_eval.py`, extends `finetune_eval` pattern)
- `baseline` = the runner's plan/answer over the golden suite **without** the candidate item in context.
- `candidate` = same, **with** the candidate item injected into the same slot it would occupy live.
- Score both with `IQQuestion.matches()` → weighted pass-rate; `delta_pp = candidate - baseline`.
- Decision: `promote` iff `delta_pp >= OMNISIGHT_MEMORY_PROMOTION_PP` (default e.g. +3pp, tunable) **AND** every negative control passes (`eval_neg_control_ok`). Else `reject` with reason.
- Deterministic + cheap: plan/answer level, not full-ticket execution. Runs offline (nightly or on-demand), never on the hot pickup path.
- **Anti-hollow fail-closed:** if the suite is missing/empty/hash-mismatch, or the baseline can't be produced → decision = `reject` (NEVER `promote`), and a metric/alarm fires (Phase S discipline: a silently-empty eval must not read as "all clear").

### 4.4 Promotion gate + human sign-off
- On scorer `promote`: file a **Decision-Engine `memory/promotion` proposal** (candidate id, kind, provenance, delta_pp, suite version, diff of what enters the live set). Bots are propose-only (P5: strict endpoint, bots 403); a **human approves** → job flips `status=promoted`, `decided_by/at`. This is the dual-gate (eval PASS ∧ human +2), the same shape as Gerrit's AI-+1 / human-+2.
- On approve, the item becomes *eligible* for the live set (skills: the existing BP.M.3 promote path now additionally requires an eval-PASS + approved proposal; lessons/playbook: eligibility flag the retrieval/injection layer will honor once U1/U2 wire them).
- On `reject`: `status=rejected` + reason; retained for the U7 audit trail (never silently dropped).

### 4.5 First application target
Apply U4 to the **existing skill draft→promote path** first (smallest, real, already-human-owned): `auto_distilled_skills` drafts must pass `memory_promotion_eval` + an approved `memory/promotion` proposal before they can be promoted/injected. This proves the whole gate end-to-end on a live-but-currently-orphaned producer, without needing U1 yet. Lessons/playbook kinds ship the schema + scorer support but stay behind the flag until U1/U2.

### 4.6 Observability (ship-with-metrics-or-don't-ship)
Prometheus (reuse `backend/metrics.py` + the Phase S ratio/absence discipline): `omnisight_memory_quarantine_depth{kind}`, `..._promotion_total{kind,decision}`, `..._eval_delta_pp` (histogram), `..._neg_control_catch_total`, `..._proposal_total{decision}` (human approve/reject), `..._eval_failclosed_total{reason}`. A NoEval / suite-stale absence alert (Phase S S4 shape). The gate's own health must be observable — an eval harness that silently stops running is the hollow anti-pattern reincarnated.

## 5. Safety invariants (must survive audit)
1. **Writer/actor separation** — producers write `quarantined` only; promotion is a separate governed job + human sign-off. No agent self-promotes.
2. **Default-OFF** — behind `OMNISIGHT_SELF_IMPROVE_LEVEL` (+ a dedicated `OMNISIGHT_MEMORY_PROMOTION_ENABLED`); dormant ship changes nothing live.
3. **Fail-closed** — any eval-infra degradation → reject, never promote; + alarm.
4. **Ground-truth-keyed provenance** — a candidate's justification is a Gerrit/JIRA outcome label, never the agent's self-judgment (Reflexion-MBPP is the exhibit).
5. **Negative controls mandatory** — no suite without them; a neg-control trip is a hard reject.
6. **Frozen, human-curated, Gerrit-gated suite** — the eval can't be quietly weakened; suite version is recorded per decision.
7. **Reversible** — promotion is a status flip with full provenance; demotion/rollback is a symmetric flip (feeds U3).

## 6. 4-AC shape (for ticketing later — placeholder)
- **Code** = quarantine table + migration; `memory_promotion_eval.py` (offline, deterministic, fail-closed); golden-suite loader + a seed suite w/ negative controls; `memory/promotion` proposal type; wire the skill promote path to require eval-PASS+approval; metrics; tests incl. neg-control catch + fail-closed + writer/actor-separation (producer cannot write `promoted`). All OFFLINE-achievable (no live stack) — S3 lesson.
- **Deploy** = next release-train cut, DORMANT (flags off).
- **Integration** = offline: seed a candidate → scorer produces a deterministic delta → proposal filed → simulated human approve → status=promoted; a poisoned candidate trips a negative control → auto-reject; empty suite → fail-closed reject + metric. No docker/live server needed.
- **Exercised** = operator: on a real stack, run the nightly eval on the existing `auto_distilled_skills` drafts, confirm proposals appear for human review and the metrics populate. (Not a runner gate.)

## 7. Sequencing after U4
U4 (this) → **U1** experience loop (completion-time distillation → writes candidates into U4's quarantine, keyed to Gerrit/JIRA ground truth) → **U2** capped playbook tier (reads U4-promoted items; adds the `build_system_prompt` section) → **U3** lifecycle (citation-hits × post-hit success-delta, decay/supersede, re-validate on suite-version bump) → **U5** Graphiti temporal axis → **U6** Sora 3-tier persistent memory (reuses quarantine/provenance + pending-confirm; Letta writer/actor separation; consolidation drafts, human confirms semantic) → **U7** quarterly poisoning/misevolution audit (reads the reject/promote provenance trail).

## 8. Open questions for the audit
1. Plan/answer-level proxy vs occasional full-ticket eval — is the deterministic proxy a faithful enough signal, or does it need a periodic expensive-but-real check? (Cost vs fidelity.)
2. Where does the "runner's plan/answer over the golden suite" actually get produced — a dedicated eval agent call, or replaying a cached trajectory? (Determinism + metered-API cost.)
3. Baseline stability: the same prompt-set may yield nondeterministic model output run-to-run → how to make `delta_pp` robust (temperature 0? N-sample majority? cache baseline per suite-version?).
4. Should the quarantine table be a NEW table or a generalisation of `auto_distilled_skills` (migration risk vs unification)?
5. Proposal volume: could nightly eval flood the human with `memory/promotion` proposals? (Batch? auto-reject-below-floor without a proposal? — the S3 "flood" concern.)
6. Interaction with the existing BP.M.3 human-owned promote surface — extend it or parallel it?
7. Threshold defaults (+3pp? neg-control weighting) — pick defensible starting values; U3 tunes.
