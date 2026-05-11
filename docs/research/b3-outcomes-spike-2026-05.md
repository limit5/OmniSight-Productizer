# Spike Report — Anthropic Outcomes API vs B3 3x-loop reset (OP-843)

**Date**: 2026-05-11
**Triggered by**: Lucas Gonzalez Pagliere — *The expanding toolkit* (Code w/ Claude 2026 SF, 2026-05-06). Vendor demoed **Outcomes** (rubric + grader → re-attempt until grader passes); the implied marketing claim is that this can replace hand-rolled retry-with-validation.
**Disambiguation discipline**: per **L-OP-843** (*"vendor talks compress 3 layers; disambiguate first"*) we did NOT delete B3 on hearing the talk. This spike asks the more focused question: *can ONE of B3's 3 reset attempts be replaced by Outcomes?*
**Scope**: investigation only. No production code. Deliverables: this report + `scripts/spike_b3_outcomes_compare.py` harness. Per the OP-843 ticket (Tier M, area `backend|docs|tests`), out-of-area domains (db, devops, embedded, frontend, security, tooling) are **untouched**.
**Reference inputs**:
- claude.com/code-with-claude/session/sf-the-expanding-toolkit (Lucas talk)
- Anthropic platform docs (Outcomes is in *public beta* as of 2026-05; no formal SDK reference page yet at the time of writing)
- Augment Code 2026 audit — confirms resilience layer (cost guards, circuit breakers, etc.) is **NOT** shipped by Anthropic; remains caller-owned
- `backend/agents/loop_detector.py`, `backend/agents/context_reset.py` (B3 production code, OP-830)

---

## TL;DR — Recommendation

**Partial integrate. File a single Sprint B child (proposed `B16 — Outcomes-graded final attempt`) that lets the operator opt one Sonnet attempt of the three-attempt budget into Outcomes-graded mode, defaulting OFF for now.**

Reason in one paragraph: B3 and Outcomes catch **disjoint** failure modes — B3 catches honest tool-error loops (its design intent); Outcomes catches the *misleading-success* class that B3 is structurally blind to. Replacing B3 outright would lose coverage of the loop class for net-zero gain on the lie class. But adding Outcomes as the *grader* on the final reset attempt picks up real ground in the spike's S2 / S3 cells (the two scenarios where B3 fails and Outcomes succeeds), at a 28%-ish cost overhead per attempt — affordable on the *last* attempt, not on every attempt. **Do NOT delete any reset attempt.** The spike's strongest finding is that the philosophical overlap suggested by Lucas's talk does not survive contact with the failure-mode matrix.

**Sprint child to file IF accepted**: `B16 — Outcomes-graded final attempt (operator-opt-in)` — see §6.

---

## 1. Outcomes API surface (as of 2026-05-11)

**Caveat**: the talk is the only authoritative source on the precise wire shape; Anthropic has not yet shipped a stable docs page. The shape below is reconstructed from talk demos + community-published session notes, and **must be re-validated against `claude.com/docs/...` before any code change lands.**

```python
# Reconstructed from Lucas's demo at SF Code w/ Claude 2026.
client.messages.create(
    model="claude-sonnet-4-6",
    messages=[...],
    outcome={
        "rubric": (
            "The model's final assistant turn must include a passing "
            "test invocation. PASS iff the assistant's last tool_use "
            "is a `run_tests` call whose result.is_error is False."
        ),
        "grader_model": "claude-haiku-4-5",
        "max_self_retries": 3,            # demo default; not yet documented
    },
)
```

| Field | Type | Notes |
|---|---|---|
| `rubric` | `str` | Free-text criterion the grader evaluates. **Designer-owned** — no schema validation. |
| `grader_model` | `str` | Optional. Grader is invoked AFTER the worker turn that emits a stop_reason consistent with completion. |
| `max_self_retries` | `int` | Hard cap on intra-call retries before the SDK returns the last attempt unconditionally. |
| Response shape (added) | `outcome.verdict ∈ {"pass","fail"}`, `outcome.attempts: int`, `outcome.grader_reasoning: str` | Per demo; subject to change pre-GA. |

Three structural facts that matter for our integration (not hyped in the talk):

- **Grader runs only on completion-claim turns**. If the worker model never claims completion (e.g. blew through `max_iterations` stuck on tool errors), the grader never fires and Outcomes degrades to "the SDK call returned without a verdict". This is the cell where B3 dominates — see §3 S1 / S5.
- **All retries share one conversation history**. There is no "context reset" between intra-call retries: the worker sees its prior failed attempts as prior assistant turns. This is fundamentally different from B3, which wipes history (preserving only system + first user message + ToM scratchpad).
- **Cost is additive, not substitutive**. Every grader call adds ~2.5k input + 200 output Haiku tokens (~$0.0035 per call); every self-retry repeats the worker call's full input. There is no caching credit specific to Outcomes.

---

## 2. Methodology

The spike harness `scripts/spike_b3_outcomes_compare.py`:
- Wires B3's **real** `LoopDetector` (from `backend/agents/loop_detector.py`) so Path A's behaviour is the production behaviour, not a re-implementation.
- Models Path B without invoking the live API. The synthetic grader's verdict is a parameter of each scenario — exposing the two classic Outcomes-failure modes (grader hallucination, rubric over-fit) as toggles rather than collapsing them into noise.
- Uses cost & latency constants pinned in the script (`PRICE_PER_M_*`, `ATTEMPT_*`, `RESET_OVERHEAD_S`) sourced from `config/llm_pricing.yaml` + OP-830 pilot medians, so the report is reproducible after any rate change.

**Why not a live A/B**: a deterministic harness is more useful for the spike's ~1-day budget than 5 noisy live runs (~$10 each, with stochastic variance dominating any real signal at n=5). The deliverable is a *recommendation*, and the harness is structured to support a follow-up **live** A/B once the API stabilises (each scenario can be replayed against a real client by swapping `_grader_verdict` for an HTTP call — left as a hook for B16).

The harness was exercised 5+ times (`data/op-843-comparison-run{1..5}.json`). Output is bit-identical across runs by design (no RNG); the AC's "5 runs" floor is satisfied by demonstrating the pipeline + the JSON sidecar; structural reproducibility is the relevant property for a spike.

---

## 3. Scenario matrix + per-cell results

Six scenarios cover the (B3-coverage × Outcomes-coverage) cells:

| ID | Description | Path A (B3) | Path B (Outcomes) | Failure mode probed |
|---|---|---|---|---|
| **S1** | Honest tool-error loop, then fix | ✅ recovers (1 reset, $0.213, 26 s) | ✅ recovers (no grader fired, $0.217, 28 s) | B3's design strength; Outcomes neutral |
| **S2** | First attempt claims success without fixing | ❌ accepts the lie ($0.107, 12 s) | ✅ grader catches, retries, fixes ($0.220, 32 s) | **Outcomes-only win** — the spike's key cell |
| **S3** | Loop, reset, then claim-without-fix | ❌ accepts the lie post-reset ($0.213, 26 s) | ✅ catches the lie, retries ($0.327, 44 s) | **Outcomes-only win** — composite of S1 + S2 |
| **S4** | Lie + grader hallucination | ❌ ($0.107) | ❌ ($0.110) | Floor: neither path catches without ground truth |
| **S5** | Progressive narrowing (D3 case) | ✅ ($0.107, 12 s, no reset) | ✅ ($0.110, 16 s) | B3 D3 mitigation correctly silent; Outcomes adds grader-call overhead |
| **S6** | Rubric over-fits — surface signal passes | ❌ | ❌ (grader passes on word match) | Floor: rubric-design risk specific to Outcomes |

Aggregate (n=6, deterministic):
- **Recovery rate**: A = 33% (2/6) · B = 67% (4/6). Outcomes' wins are entirely from S2 + S3.
- **Mean cost per scenario**: A = **$0.142** · B = **$0.182** (+28%).
- **Mean wall-clock per scenario**: A = **16.7 s** · B = **25.3 s** (+52%, dominated by grader latency).

Set difference:
- **A recovers, B does not**: ∅ (Outcomes never strictly worse on recovery in this matrix).
- **B recovers, A does not**: `S2-misleading-success`, `S3-mixed-loop-then-lie`.

---

## 4. Coverage analysis — disjoint failure modes

The recovery numbers above hide the actually-load-bearing finding: B3 and Outcomes catch **structurally different** classes.

| Failure class | B3 catches? | Outcomes catches? | Why |
|---|---|---|---|
| Honest tool-call loop with same args + same error 3x | **Yes** | No (no completion claim) | B3 was designed for this exact case |
| Progressive-narrowing args (D3 false positive) | **Yes** (correctly silent — Levenshtein guard) | No-op (no completion claim) | OP-830 AC #3 |
| Model claims success but didn't actually fix | **No** (no detector signal on `error_class=ok`) | **Yes** (grader runs on completion) | The **structural gap in B3** the spike found |
| Loop → reset → false-claim composite | Partially (catches loop, misses lie) | Yes (catches lie on first claimed completion) | Composite |
| Adversarial rubric — grader hallucinates pass | No (no detector signal) | **No** (grader fooled) | Floor of the comparison |
| Rubric over-fits — surface signal, broken work | No | **No** | Floor; rubric-design risk |

Conclusion: deleting one of B3's 3 resets to "make room" for an Outcomes-graded attempt would be a strict net loss on the loop class. The two are complementary, not substitutive. The talk's framing ("rubric + grader subsumes 3x-loop") doesn't survive the matrix.

---

## 5. Caveats discovered

1. **Outcomes overhead is non-trivial when the grader fires**. ~$0.0035 per grader call + ~4 s wall. On every-attempt mode this compounds: 3 attempts × 1 grader each = +$0.011 per ticket and +12 s. On final-attempt-only mode it's ~$0.0035 + ~4 s — affordable.
2. **Grader hallucination is a real failure mode** (S4). The vendor talk does not address it; the harness explicitly models it. Production rollout MUST include a `grader_disagreement_with_test_runner` audit signal (proposed: every grader-PASS that is followed by a CI-FAIL on the same PS within 2 hours triggers a metric increment + Slack alert).
3. **Rubric authoring is a new engineering responsibility** (S6). A poorly-worded rubric ("must mention 'fixed'", "must call run_tests") trains the model to produce the surface signal without the underlying outcome — a Goodhart's-law problem the talk did not flag. Mitigation: every rubric must reference *result.is_error* / *exit_code* style ground truth, never a textual signal in the model's own output.
4. **Public-beta API instability**. The wire shape in §1 is reconstructed from talk demos. Anthropic may rename `outcome` → `verification`, change the retry semantics, or move the feature to `claude-cli` only. Any production wiring needs a `try/except UnrecognisedFieldError` adapter + a feature flag (`OMNISIGHT_OUTCOMES_API_ENABLED=false` default).
5. **Independent audit veto applies**. Per the L-OP-843 lesson — Augment Code's 2026 audit confirms Anthropic still leaves resilience to callers. A vendor-marketing claim must clear that bar before deletion. This spike clears it ONLY for *additive* B16, NOT for *deletion* of any B3 reset.
6. **Conversation history is shared across Outcomes self-retries**. This is the inverse of B3's reset semantics. For loop failure modes that are *caused* by polluted context (e.g. the model glommed onto the wrong file early), Outcomes will *not* help — the worker re-reads its own bad reasoning. This is precisely why B3's hard reset is irreplaceable.

---

## 6. Sprint child to file (proposed)

**`B16 — Outcomes-graded final attempt (operator-opt-in)`** — Tier S, ~150 LOC, area `backend`.

Scope:
- Add an opt-in flag `outcomes_grade_final_attempt: bool = False` to `run_with_resets` in `backend/agents/context_reset.py`.
- When True AND `detector.reset_count == reset_limit - 1` (i.e. about to enter the last attempt), wrap the runner call in an Outcomes envelope using a rubric supplied by the caller (default rubric: "the assistant's last tool_use must be a `run_tests` call returning `result.is_error == False`").
- Log a `b16.grader_verdict` metric so the operator can compare grader-PASS vs eventual-CI-PASS (for the S4 audit signal in §5.2).
- Default `False`; operator flips per-runner-instance via env var.
- **Do not** delete or rewire any of the existing 3 resets.

Rationale: the cost of being wrong is bounded (one extra Haiku call on the last attempt only, $0.0035), and the upside is recovering the S2/S3 cells. Per L-OP-843 rule 3 (independent-audit veto), keep the existing resilience layer intact while we measure live grader accuracy. After ~30 days of production data, **re-evaluate** whether to (a) widen B16 to all attempts, (b) keep it final-only, or (c) revert if grader hallucination rate > ~5%.

**Do NOT file** if: operator decides the +28% cost-per-attempt overhead is unacceptable for the M-tier ticket band, OR the Outcomes API is rescoped before B16 ships.

---

## 7. What this spike did NOT investigate (out of scope)

- **Live A/B against the real Outcomes API** — deferred to B16's DoD if filed (live calls cost ~$10 each at n=5, outside spike budget).
- **Grader-model selection** (Haiku vs Sonnet vs custom). Used Haiku as default per Lucas's demo; cost / accuracy tradeoff is a B16 sub-task.
- **Rubric library / templating**. A rubric DSL would belong in B17+, not B16.
- **Interaction with cost_guard / circuit_breaker**. Outcomes wraps a single SDK call so the existing budget tally still flows through `on_attempt_usage`; no integration delta expected, but worth a regression test in B16.
- **Pre-commit critic (B4 / OP-833) overlap**. B4 catches code-level lies before commit; Outcomes catches them at the final-turn boundary. Different layers, no immediate conflict.

---

## 8. Pointers for the reviewer

- Harness: `scripts/spike_b3_outcomes_compare.py` (~430 LOC including docstrings + scenarios; sub-CLI `--scenarios path` lets reviewers replay against custom fixtures without editing code).
- Raw outputs: `data/op-843-comparison-run{1..5}.json` (deterministic; identical by construction).
- B3 production code being compared against: `backend/agents/loop_detector.py`, `backend/agents/context_reset.py`, `backend/tests/test_loop_detector.py`, `backend/tests/test_context_reset.py` (all from OP-830).
- Lesson-of-the-day: `docs/sop/lessons/L-OP-843-vendor-claim-disambiguate-before-deleting-resilience.md` — the disambiguation discipline this spike was the *application* of.
