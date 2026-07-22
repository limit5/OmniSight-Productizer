# Leg-2 Worker Experience Loop — Operator Activation Runbook

**As of develop `9daa257b` (2026-07-22). Leg-2 is CODE-COMPLETE and fully
dormant: 8 increments (Gerrit #2184–#2191, OP-2711..OP-2718), every gate
fail-closed. This runbook is the ONLY remaining work — each step is an
operator decision; nothing self-activates.**

The loop when lit: Gerrit merge → independent non-bot +2 verify (HTTP-first)
→ `curator_merge_candidates` ledger → curator (quarantined minimal record) →
cheap-LLM distiller (hard-gated) → 2-leg plan-triage eval (in-repo +
holdout, neg-control forcing) → human approval (bearer-token + DB-invariant)
→ publication → per-worker cache → runner/graph injection (fenced,
descriptive u4r2) → citations → human-only utility → revocation proposals.

## Step 0 — prerequisites (one-time)
- Deploy an image whose alembic head ≥ **0278** (migrate BEFORE code).
- Serving env: `gerrit_http_user` / `gerrit_http_password` (scoped read-only
  Gerrit REST account — else merge-verify is `degraded` and the ledger
  starves; watch `merge_verify_total{outcome="degraded"}`).
- Assert `gerrit_webhook_secret` is set on serving (else merge ingest is
  unauthenticated — one-shot warning fires in logs).

## Step 1 — private holdout battery (HARD precondition for any publication)
- Author a manifest + shard OUTSIDE the repo (the in-repo battery is an
  answer key): `<holdout-dir>/manifest.yml` + shard, same schema as
  `configs/plan_triage/` (sha256 computed from shard bytes). ≥10 questions,
  ≥4 neg-controls, DIFFERENT phrasings from the in-repo set.
- Set `OMNISIGHT_EVAL_HOLDOUT_DIR=<holdout-dir>` on serving.
- **No holdout ⇒ nothing is ever approvable/publishable** (0277 fail-closed).

## Step 2 — staging calibration run (before ANY flag)
- Pin the eval model (defaults: `anthropic` / `claude-haiku-4-5-20251001`);
  set `llm_temperature=0` for the calibration window if feasible.
- Run the battery baselines (both suites): ALL neg-controls must
  baseline-REFUSE and ALL positives must baseline-PASS on the pinned model.
  Any neg baseline-compliance ⇒ rewrite that question (the
  `neg_baseline_contaminated` alarm also guards this at runtime).
- Watch for `memory_promotion_eval_anomaly_total{promote_decision}` — a
  promote is an ANOMALY under a calibrated battery (answer-key tripwire).

## Step 3 — flag sequence (staging first, one at a time, observe between)
1. `OMNISIGHT_WORKER_CURATOR=1` — ledger→quarantine spine.
   Watch `worker_curator_candidates_total{result}` (submitted vs deferred).
2. `OMNISIGHT_WORKER_CURATOR_LLM=1` — distiller lane (per-tick 3, daily 50).
   Watch `distilled_llm` vs `gate_skip_*` / `llm_*` labels.
3. `OMNISIGHT_MEMORY_PROMOTION_EVAL=1` — 2-leg eval scheduler.
   Watch `memory_proposal_outcome_total{decision}` + the anomaly counter.
4. `OMNISIGHT_MEMORY_UTILITY_ROLLUP=1` — utility + revocation proposals
   (proposals land in `proposed_actions`; human decides).
5. After review mileage (cards look sane in `GET /memory-promotions/pending`):
   `OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED=1` (publish side) — then a
   HUMAN approves via `POST /memory-promotions/{id}/approve` and publishes.
6. Read-ON (each its own decision):
   - `OMNISIGHT_LEARNED_ITEM_READ=1` — per-worker cache refresh starts;
     graph plane serves blocks. ⚠ When wiring a graph caller, thread
     `build_system_prompt(learned_provenance_sink=…)` →
     `run_with_tools(injected_provenance=…)` (unwired = fail-closed).
   - `OMNISIGHT_RUNNER_LEARNED_ITEMS=1` (runner env) — runner splice live;
     watch `learned_items.result=` stderr lines on every pickup.

## Step 4 — prod
Repeat 0→3 on prod ONLY after the leg-1 release cut ships heads ≥0278 and
staging has ≥1 week of clean decisions. Rollback = flip flags off (everything
degrades to empty blocks; nothing breaks).

## Standing invariants (never violate)
- Publication requires: clean bound in-repo run + clean holdout sibling +
  HUMAN approval (bot-shaped principals rejected) — no exceptions.
- Utility is a HUMAN-ONLY signal; revocation is proposal-only.
- Battery shards (in-repo AND holdout) are gate-code: review accordingly.
