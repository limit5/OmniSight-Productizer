# B3 vs Anthropic Outcomes — spike report

**Date**: 2026-05-11
**Spike ticket**: OP-843
**Hypothesis**: Outcomes API (rubric + grader, public beta as of Code w/ Claude 2026-05-06) could replace one of B3's three reset attempts → equivalent recovery at lower cost/latency.
**Method**: Pure-mock comparison harness (`scripts/spike_b3_outcomes_compare.py`) — does not call Anthropic API. Models both paths as deterministic state machines so the comparison isolates *recovery semantics* from model nondeterminism. Token costs derived from published Anthropic pricing (Sonnet 4.6 $3/$15 per MTok, Haiku 4.5 $0.80/$4) applied to per-attempt token modelling.

## TL;DR — recommendation

**Don't integrate as a B3 replacement.** Keep B3 (3× hard-reset) untouched. Outcomes does add correctness on one specific failure mode (false-positive success claim), but for our pipeline that mode is already caught by B4 (Critic) + B5 (TDD) downstream, so adoption inside B3 buys 0 net coverage at +30% cost.

Defer Outcomes adoption to a *different* surface where the downstream verification doesn't exist — see §5 for the watch trigger.

## 1. Findings

### 1.1 Per-scenario summary

| Scenario | Path | Attempts | Succeeded | Cost (USD) | Latency (s) | Notes |
|---|---|---:|:-:|---:|---:|---|
| happy_path | A_b3 | 1 | ✓ | $0.2250 | 120 | b3_succeeded |
| happy_path | B_outcomes | 1 | ✓ | $0.2370 | 125 | grader_confirmed_pass |
| happy_path | C_hybrid | 1 | ✓ | $0.2250 | 120 | grader_confirmed_pass (Path C's first attempt is B3-shape) |
| **succeed_without_fixing** | A_b3 | 1 | **✗ (false ✓)** | $0.2250 | 120 | **b3_accepted_claim_without_grading** |
| **succeed_without_fixing** | B_outcomes | 3 | ✗ | $0.4470 | 245 | grader rejected all 3 |
| **succeed_without_fixing** | C_hybrid | 3 | ✗ | $0.5550 | 300 | grader rejected on attempt 2 |
| infinite_loop | A_b3 | 3 | ✗ | $0.6750 | 360 | cap exceeded (loop-detector terminal abort) |
| infinite_loop | B_outcomes | 3 | ✗ | $2.0040 | 245 | cap exceeded — **no loop detector** |
| infinite_loop | C_hybrid | 3 | ✗ | $1.0740 | 300 | partial benefit from B3 prefix |
| partial_progress | A_b3 | 2 | ✓ | $0.4500 | 240 | succeeded on retry |
| partial_progress | B_outcomes | 2 | ✓ | $0.3360 | 185 | cheaper via input caching |
| partial_progress | C_hybrid | 2 | ✓ | $0.4500 | 240 | first attempt B3-shape |
| structural_max_iterations | A_b3 | 3 | ✗ | $2.2500 | 360 | non-retryable; cap correct |
| structural_max_iterations | B_outcomes | 3 | ✗ | $2.0040 | 245 | non-retryable; cap correct |
| structural_max_iterations | C_hybrid | 3 | ✗ | $2.1240 | 300 | non-retryable; cap correct |

### 1.2 Aggregate by path (sum across all 5 scenarios)

| Path | Total cost (USD) | Total latency (s) | Success rate | Mean cost per scenario |
|---|---:|---:|---|---:|
| **A — B3 (current)** | $3.83 | 1200 | 2/5 | $0.77 |
| **B — Outcomes only** | $5.03 | 1045 | 2/5 | $1.01 |
| **C — Hybrid (2× B3 + 1× Outcomes)** | $4.43 | 1260 | 2/5 | $0.89 |

### 1.3 Headline interpretation

* **B3 is strictly cheaper** ($3.83 vs $5.03) across the 5-scenario suite, mainly because Outcomes has no equivalent of B3's tool-shape loop-detector — on `infinite_loop` Outcomes burns full max-iteration budget every attempt while B3 detects + aborts.
* **Outcomes is ~13% faster in wall-clock** because re-attempts inside one Outcomes session benefit from input caching (cached rubric + AC); B3's resets reincur the full prompt prefix each time.
* **Outcomes catches `succeed_without_fixing` correctly; B3 doesn't**. This is the *only* scenario where the paths' verdicts differ. B3 has no grader, so it accepts the model's self-report at face value.
* **Net coverage gain in our pipeline = 0**, because the `succeed_without_fixing` scenario is already caught by:
  * **B4 (Critic)** — independent reviewer agent reads the diff + AC; refuses dissent on second pass.
  * **B5 (TDD)** — for `tdd_applicable=yes` tickets, a real test must go from red → green; no test = no merge.
  * **B6 (Submit-checklist)** — feature-list JSON requires per-item pass evidence at submit time.
* The "Outcomes wins on correctness" finding only applies to surfaces where those downstream layers are absent. Inside the SDK runner pipeline, three independent layers already replicate Outcomes' grader job.

## 2. Layer-by-layer comparison

| Capability | B3 (current) | Outcomes (Anthropic beta) | Already covered by |
|---|---|---|---|
| Tool-shape loop detection (3× same `(tool, args_hash, err_class)`) | ✅ first-class | ❌ relies on max_iterations | n/a — B3 unique |
| Context reset (clear conversation, keep system + first user) | ✅ first-class | ❌ Outcomes is single session | n/a — B3 unique |
| Cost cap per attempt | ✅ CostGuard | ✅ via Anthropic billing | both |
| `max_iterations` enforcement | ✅ caller-managed (W14.5) | ✅ within Outcomes session | both |
| Grader-validated success | ❌ (accepts model claim) | ✅ rubric + grader | **B4 (Critic), B5 (TDD), B6 (Checklist)** |
| Cross-attempt input caching | ❌ resets discard | ✅ Outcomes retains | Outcomes unique |
| Independent failure-class taxonomy | ✅ typed errors (OP-827) | ❌ rubric is free-text | B3 unique |

**B3 has 4 capabilities Outcomes doesn't; Outcomes has 1 capability B3 doesn't (and we cover that capability triply already).** The ratio doesn't favour adoption.

## 3. Caveats discovered

* **Outcomes' "no loop detector" gap is severe for our workload.** On `infinite_loop`, Outcomes burned $2.00 vs B3's $0.68 because Outcomes lets each attempt run to max_iterations rather than detecting the tool-shape repetition early. This is exactly the failure mode B3 was purpose-built for (OP-829 5x revert loop incident, 2026-05-11 morning).
* **Outcomes' input-caching win is real but only on success paths.** When the model gets close on attempt 1 but needs another shot (`partial_progress`), cached prompt prefix saves ~$0.11/attempt. B3 resets lose this benefit by design (clean slate is the whole point).
* **Hybrid Path C is structurally worse than either pure path** — pays B3 prefix-cost on attempts 0+1, pays Outcomes overhead on attempt 2, and on `succeed_without_fixing` still fails because B4/B5/B6 catch it later anyway. The 30% cost premium over pure B3 buys nothing in our pipeline.
* **Mock fidelity**: this spike does not call the real Outcomes API. Token counts modelled from publicly documented per-call shape; latency from published p50 figures. Real-world numbers may diverge ±20% but the **directional verdict** (B3 cheaper, no coverage gain inside our pipeline) survives a generous margin.

## 4. Vendor-claim disambiguation (L-OP-843 application)

Lucas Gonzalez Pagliere's "The expanding toolkit" talk implied Outcomes obviates retry boilerplate. Decomposed against our pipeline:

| Vendor implied | Actual reality in our pipeline |
|---|---|
| "Outcomes replaces hand-rolled retry" | Outcomes' retry is *task-level grader-validated retry*. Our B3 is *call-level tool-shape loop detection*. Different layers. |
| "You don't need to write fault-tolerance code" | We don't — we wrote *recovery* code (OP-827, OP-832, OP-836, OP-838, OP-842). Outcomes covers none of those. |
| "The grader catches false-positive success" | Yes, but so does B4 + B5 + B6. Our pipeline has triple coverage; adding Outcomes is a fourth + redundant layer at +30% cost. |

The L-OP-843 lesson rule is applied: vendor talks compress layers; verify against our actual deployment surface before adopting.

## 5. Recommendation + watch triggers

**Don't integrate inside B3.** Keep current 3× hard-reset semantics.

**Do** add Outcomes to the *watch list* for these specific future scenarios:

1. **Light-weight tickets that skip B4 + B5 + B6** — e.g., docs-only tickets, single-file refactors where `tdd_applicable=no` and Critic dissent rate is low. Outcomes' grader could replace Critic+TDD+checklist on these. Quantify when ≥20% of tickets fit this profile.
2. **A future "simple-task fast path"** for tier:S tickets that don't go through the full Sprint B Phase 2-3 pipeline. Outcomes is a cheap, self-contained alternative to wiring B4/B5/B6 for trivial tickets.
3. **Adversarial-prompt-injection regression test** — Outcomes' rubric is a natural place to encode "did the model output anything that looks like a leaked credential?" (relevant to B14 / OP-844 work). File as B14 sub-task if needed.

**Revisit trigger**: when (a) >50 tickets have completed Phase 2-3 pipeline AND we observe Critic dissent rate <5% (suggesting B4 is over-engineered for our actual failure mix), OR (b) Anthropic announces feature parity for tool-shape loop detection inside Outcomes (eliminates the §3 first caveat).

## 6. Files this spike produced

* `scripts/spike_b3_outcomes_compare.py` — pure-mock harness; reproducible
* `docs/research/b3-outcomes-spike-2026-05.md` — this report

No production code touched. No Sprint B AC modified. OP-843 closes as `Won't Do` per the recommendation; if (a) or (b) above fires later, file a fresh ticket.

## 7. Sources

* Lucas Gonzalez Pagliere, "The expanding toolkit" — Code w/ Claude 2026 SF, 2026-05-06
* Anthropic — "Effective harnesses for long-running agents"
* Augment Code 2026 — "Anthropic Agent SDK: What It Ships vs. What It Leaves to You"
* L-OP-843 — companion lesson on vendor-marketing-compression discipline
* OP-830 (B3) — current 3× hard-reset implementation
* OP-833 (B4 Critic) / OP-834 (B5 TDD) / OP-835 (B6 Checklist) — the three downstream layers that already cover the `succeed_without_fixing` scenario
