# Sprint B — SDK Runner Reliability Layer (planning draft)

**Status**: PLANNING (PS2 — 2026-05-10 review pass) — not yet filed in JIRA.
**Predecessor**: Sprint A (OP-808 META + OP-809..OP-823) + audit findings (`docs/audit/2026-05-09-aider-swe-agent-audit.md`)
**Triggering question**: "目前還有其他更適用的方法嗎？實作缺陷怎麼補？耗損/錯誤率怎麼降？"
**Research basis**: 10-direction audit covering CodeAct/PTC, Reflexion-in-prod, SWE-bench Verified 2025-2026 leaders, Anthropic engineering blog, Cognition Devin, CRITIC, Best-of-N, static-analysis pre-flight, model routing, AutoCodeRover/CodeR/MetaGPT/Replit/Cursor.
**PS2 delta (2026-05-10 morning)**: Operator-supplied 6 additional candidates (LLMLingua-2, DSPy, Outlines/LMQL, AST blast-radius, JIT-HTN, Capability Matrix) reviewed. Net: +1 child (B13 AST blast-radius), B3 strengthened with Anthropic context-reset mechanism. Other 4 deferred or rejected with reasons in §9.
**PS3 delta (2026-05-10 afternoon)**: Operator question — *"each phase 的 error/exception/exit/state machine 規畫完整嗎?"* — triggered deep audit. Self-assessed PS2 coverage was ~14%; PS3 raises to ≥85%. Companion engineering doc written: `docs/architecture/sdk-runner-sprint-b-error-handling.md` (covers per-child FSM/error catalog, 25 past-failure incidents, 8 cross-cutting concerns, recovery primitives, sprint charter gates). Main plan updated with §12 summary matrix + new §13 charter prerequisites. Phase 1 children CANNOT enter In Progress until §13 G1-G4 gates pass.

---

## 1. Executive summary

Sprint A focused on **tool-shape fixes** (give the model the right tools). Sprint B focuses on **behavior-shape fixes** (constrain how the model uses them). Three combined inputs:

| Source | Items considered | Items recommended |
|---|---:|---:|
| `audit-2026-05-09` (prior) | 7 findings | 5 (built-in tools, repo-map, cache, checklist, reflection) |
| User's 3 methods (TDD / Locator+Coder / ToM-3xloop) | 3 | 3 (with applicability gates) |
| Industry research (this round) | 10 directions | **5 strong buys, 1 lite buy, 4 skip** |
| Operator's 6 follow-up candidates (PS2) | 6 | **1 strong buy (AST blast-radius), 1 strengthening of B3, 4 defer/reject** |

**Net Sprint B = 10 children** organised in 4 phases. Total estimate: **~16 dev-days, ~$130 pilot-test budget**. **Single linear pipeline**, no parallel sub-agents (validated by Cognition's "Don't Build Multi-Agents" + Anthropic's harness blog).

**Bottom line — three big wins absent from Sprint A**:

1. **Programmatic Tool Calling** (Anthropic GA 2026-01-20) — emit Python that calls tools in sandbox; intermediate output never enters model context. **-30–60% input tokens, -2–5× latency** on retrieval-heavy tickets. Builds on top of Sprint A's built-in-tool migration.
2. **Static-analysis pre-flight gate** — cheapest single win in the entire research output. ~100 LOC, 0.5 day, **-25% trivial-cycle patchsets**. Aider's `lint-cmd: FIX_COMMAND && LINT_COMMAND` resolves >90% nits in one shot.
3. **Anthropic harness pattern** (`progress.txt` + feature-list JSON + 3-step session opener + Planner/Generator/Evaluator) — published as the canonical playbook with **+2–4× completion rate on >2hr tickets**. Replaces our freeform AC + ad-hoc session resume.

**Skip** (research-validated): Best-of-N (k=3+ diminishing returns), aggressive model routing (premature without baseline), AutoCodeRover orchestration (no SWE-bench-Verified gain over single-agent), Replit/Magnus/Devin (closed-box marketing).

---

## 2. Industry research — Tier 1/2/3 with citations

### Tier 1 — STRONG BUY (file as Sprint B children)

| # | Method | Mechanism | Cost | Expected gain | Source |
|---|---|---|---|---|---|
| **R1** | **Programmatic Tool Calling (PTC)** | `code_execution_20260120` + `allowed_callers` — model emits Python that `await`s tool functions in sandbox; only final value enters context | ~200 LOC + 1d | **-30–60% tokens, -2–5× latency** on multi-tool fanout (Anthropic-published) | [Anthropic PTC docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling) |
| **R2** | **Static-analysis pre-flight** | Run ruff+mypy (or eslint+tsc) on edited files, feed errors back as observations. Hard cap at 3 lint rounds (Aider issue #1090 mitigation) | ~100 LOC + 0.5d | **-20–40% trivial-review patchsets** | [Aider lint docs](https://aider.chat/docs/usage/lint-test.html), [Factory.ai linters-as-guardrails](https://factory.ai/news/using-linters-to-direct-agents) |
| **R3** | **Pre-commit Critic (lite Evaluator)** | Independent agent reads diff + has read-only tools (run-tests, run-linter), emits go/no-go + structured reasons | ~200 LOC + 1d | **-15–25% bad-commit rate** | [CRITIC arXiv 2305.11738](https://arxiv.org/abs/2305.11738), Anthropic harness #4 below |
| **R4** | **Anthropic harness pattern** | `progress.txt` session-bridge artifact + feature-list JSON (200+ items, pass/fail) replacing freeform AC + 3-step session opener (verify cwd → read progress → smoke test) + Planner/Generator/Evaluator triad | ~500 LOC + 3d | **+2–4× completion rate on >2hr tickets** (Anthropic-reported) | [Effective harnesses](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [Harness design](https://www.anthropic.com/engineering/harness-design-long-running-apps) |

### Tier 2 — LITE BUY

| # | Method | Mechanism | Cost | Gain | Source |
|---|---|---|---|---|---|
| **R5** | **Lesson auto-injection** | BM25 retrieval over `docs/sop/lessons-learned.md` keyed by ticket title; inject top-3 entries into runner system prompt | ~150 LOC + 0.5d | **-5–15% repeat-error rate** (anecdotal) | Aider `--read CONVENTIONS.md` pattern |

### Tier 3 — SKIP (research-validated)

| # | Method | Why skip |
|---|---|---|
| **R6** | Best-of-N parallel sampling (k=3–5) | k×cost; gains saturate at k=2; not Pareto-improving for our budget |
| **R7** | Aggressive model routing (Haiku/Sonnet/Opus classifier) | Premature — masks reliability bugs without baseline. Defer to Sprint+1 after we have stable per-tier metrics |
| **R8** | AutoCodeRover-style AST orchestration | +5pp resolution rate doesn't outweigh ~600 LOC + 4d cost; better captured by simpler grep+text_editor |
| **R9** | Replit Agent / Magnus / Cursor parallel sub-agents | Closed-source, mostly marketing; Cognition explicitly recommends against parallel sub-agents for coding |
| **R10** | Full Reflexion self-critique loop | Token-heavy; no production case-study showing it beats lite Critic + lesson auto-injection at lower cost |

### Validations — already aligned

- **Cognition "Don't Build Multi-Agents"** → validates our Locator+Coder split (it's *sequential pipeline*, not parallel) and rejects Best-of-N for coding tasks.
- **AlphaCodium TDD flow** → already covered by user's M1 method.
- **Augment / Refact.ai SWE-bench Verified leaderboard** → confirms single-threaded linear agent + reproduce-test-as-spec is the dominant winning paradigm.
- **Aider issue #1090 (infinite lint loop)** → exactly the failure mode our 3x-loop hard reset (M3) prevents.

---

## 3. Sprint B proposed structure

Single META + 9 children, 4 phases. Each child has owner / cost / blockedBy / success criterion specified at filing time.

### Phase 1 — Core uplift (must-have before re-pilot)

These are pre-requisites for re-running the failed S1 pilot. Sprint A items in italics (already filed); new items in bold.

| ID | Title | Source | Days | Why P0 |
|---|---|---|---:|---|
| *(OP-824)* | *Migrate to Anthropic built-in tools (`text_editor_20250728` + `bash_20250124`)* | Audit Finding A | 2 | Tool surface RLHF-aligned with Sonnet |
| **B1** | **Programmatic Tool Calling for retrieval/test tools** | Research R1 | 1 | -30–60% input cost; -2–5× latency on multi-file work |
| **B2** | **Static-analysis pre-flight gate (ruff/mypy/eslint, 3-round cap)** | Research R2 | 0.5 | Cheapest win in the entire backlog |
| **B3** | **3x-loop hard reset + structured failure log + ToM scratchpad + Anthropic-style context reset** | User M3 + research validates + PS2 strengthening | 1.5 | Pure observability layer; doesn't depend on model cooperation; would have caught all 3 prior pilot failures. **PS2 addition**: when 3x-loop fires, instead of `abort`, run Anthropic-harness-style **context reset** — clear conversation history, summarise failure sequence as 1 structured paragraph, restart agent with clean state. Cheaper than HTN task-splitting (operator candidate #5) per Anthropic blog data. |

**Phase 1 total: ~5 days. Re-pilot gate: re-run 3 failed S1 tickets, success criterion ≥ 2/3 PSes.**

### Phase 2 — Quality gates (pre-merge)

Run after Phase 1 hits its gate. These tighten the merge-eligibility bar.

| ID | Title | Source | Days | Why P1 |
|---|---|---|---:|---|
| **B4** | **Pre-commit Critic agent (Haiku critic, Sonnet coder)** | Research R3 + audit Finding D refined | 1 | Independent review pre-commit; -15–25% bad-commit rate. Haiku critic = ~$0.005/ticket, Sonnet coder unchanged |
| **B5** | **TDD enforcement with applicability gate** | User M1 + audit Finding E | 1.5 | Tool-ordered enforcement of test-first; gated by per-ticket `tdd_applicable: yes/no/conditional` field (avoids breaking infra tickets) |
| **B6** | **Submit-review checklist (feature-list JSON, not freeform AC)** | Audit Finding E + research R4 partial | 1 | Replaces our freeform AC field with structured checklist (pass/fail per item); tool-enforced before submit |

**Phase 2 total: ~3.5 days.**

### Phase 3 — Architecture (after Phase 1+2 stable)

Higher-leverage but higher-risk; only attempt after Phase 1+2 prove stable.

| ID | Title | Source | Days | Why P1/P2 |
|---|---|---|---:|---|
| **B7** | **Locator (Haiku) → Coder (Sonnet) sequential pipeline** | User M2 + audit Finding D | 2 | Cleaner context separation; locator output is filtered list, not raw grep dump. Haiku locator ≈ $0.001/ticket |
| **B8** | **Repo-map preamble** *(was OP-825)* | Audit Finding B | 2 | tree-sitter PageRank → 1k token structural overview; 70.3% file-id rate (Aider SWE-bench). Synergises with B7 (locator uses repo-map as starting point) |
| **B9** | **Anthropic harness session-resume (`progress.txt` + 3-step opener)** | Research R4 | 2 | For tickets that span multiple runner sessions; +2–4× completion rate on >2hr work |
| **B13** | **AST blast-radius gate inside Locator** | Operator candidate #4 (PS2) | 0.5 | Locator computes dependency-depth + LOC across candidate files; if `>3 files OR >300 LOC`, return `oversize_refuse` to main runner instead of proceeding. Reuses B8's tree-sitter graph; ~150 LOC. Catches "粒度過大" S1 failure pattern proactively (vs B3's reactive kill). Pairs with D9 mitigation (component-aware threshold). |

**Phase 3 total: ~6.5 days.**

### Phase 4 — Long-term reliability (defer until Phase 1–3 land)

| ID | Title | Source | Days | Why P2 |
|---|---|---|---:|---|
| **B10** | Lesson auto-injection (BM25 over `lessons-learned.md`) | Research R5 | 0.5 | -5–15% repeat-error rate; very cheap |
| **B11** | Cache control on last 2 messages | Audit Finding F | 0.1 | 10-LOC quick win; -40–60% input cost across all tickets |
| **B12** | Reflection loop on test/lint failure (bounded retries) | Audit Finding C | 0.5 | Reuses B3 infrastructure; structured failure → bounded retry |

**Phase 4 total: ~1 day.**

### **Sprint B grand total: ~16 days dev + ~$130 pilot budget**

**PS2 delta**: +0.5d B13 AST blast-radius gate; +0.5d B3 context-reset strengthening. Operator candidates #1 (LLMLingua), #2 (DSPy), #5 (HTN), #6 (Capability Matrix) deferred to Sprint C; #3 (Outlines/LMQL) rejected as not applicable to Anthropic API. Full reasoning in §11.

---

## 4. Defect mitigations — known weakpoints + new from research

### From prior conversation (already identified)

**D1 — TDD applicability gate**
- *Problem*: forcing TDD on infra/scaffold tickets (most of Sprint A) creates dead loops where model can't write a meaningful test first.
- *Mitigation*: ticket-level field `tdd_applicable: yes | no | conditional` set during refinement. Runner checks field; if `no`, skip TDD phase. If `conditional`, model decides post-locator. **B5 ships with this gate, not as add-on.**

**D2 — Locator/Coder handoff format**
- *Problem*: locator output too verbose (raw grep dump) → coder context pollution. Too terse → coder lacks signal.
- *Mitigation*: structured handoff schema: `{files: [{path, line_ranges, why_relevant}], hypotheses: [...], confidence: 0.0-1.0}`. Coder gets ONLY this JSON, not locator's tool-call history. **B7 ships with format spec frozen in code, not prose.**

**D3 — 3x-loop args_hash false-positive guard**
- *Problem*: legitimate retry where each attempt narrows the input (e.g. progressively-tighter regex search) would trip the 3x detector.
- *Mitigation*: detector compares `(tool_name, args_hash, error_class)` — if `args_hash` differs from previous attempt by ≥ 1 token, count as progress, not loop. Hard cap remains at 3 truly-identical retries. **B3 ships with hash-tolerance test.**

### New from research

**D4 — Lint-loop infinite recursion (Aider issue #1090)**
- *Problem*: model "fixes" a lint warning, new fix triggers different warning, loops indefinitely.
- *Mitigation*: B2 ships with hard cap at 3 lint iterations. After cap, mark commit as `lint_partial` and escalate to human review (don't block merge for cosmetic-only).

**D5 — PTC sandbox isolation**
- *Problem*: Programmatic Tool Calling executes Python in sandbox; if sandbox writes to main repo (vs worktree), parallel runners conflict.
- *Mitigation*: B1 ships with sandbox cwd forced to `--worktree-path` from the runner; refuse to launch if worktree path not set. Cross-references OP-817 (worktree isolation, Sprint A).

**D6 — Critic/Coder agreement protocol**
- *Problem*: when critic dissents, what's the resolution rule? 3-way: (a) coder retries, (b) critic auto-overrides, (c) human escalation.
- *Mitigation*: B4 ships with explicit policy:
  - 1st dissent → coder retries with critic feedback (1 free retry)
  - 2nd dissent → mark ticket as `under_review:critic_dissent`, leave for operator
  - Critic NEVER unilaterally rewrites code — only emits go/no-go + reasons.

**D7 — Feature-list JSON migration cost**
- *Problem*: replacing freeform AC with structured feature-list breaks all in-flight Sprint S1 placeholders + every existing ticket template.
- *Mitigation*: B6 ships with **dual-mode** — accept both freeform AC (legacy) and feature-list JSON (new). New tickets default to feature-list; old tickets transparently up-converted by a one-shot migration script. Re-evaluate hard-cutover date after Sprint B+1.

**D8 — Critic model cost amplification**
- *Problem*: Sonnet critic doubles per-ticket cost. Haiku critic might be too weak for nuanced reviews.
- *Mitigation*: B4 starts with Haiku critic (cheap). If critic false-positive rate > 20% in pilot, upgrade to Sonnet. Pilot the Haiku version first; metric: critic-dissent agreement rate with operator post-hoc.

### Added in PS2 (operator candidate #4 follow-up)

**D9 — AST blast-radius threshold one-size-doesn't-fit-all**
- *Problem*: hard-coded `>3 files OR >300 LOC` threshold is too tight for area:tests (test files are typically larger and span multiple modules) and too loose for area:migrations (a single 50-LOC alembic file can be high-stakes).
- *Mitigation*: B13 ships with `config/blast_radius_thresholds.yaml` keyed by JIRA `area:` label — defaults to `(3 files, 300 LOC)`, but `area:tests` raises to `(8 files, 800 LOC)`, `area:migrations` lowers to `(1 file, 100 LOC)`, etc. Component-aware. Tunable without code change. Operator owns the YAML.

---

## 5. Cost / error-rate reduction summary

Stacked expected improvements (rough, assuming all phases land):

| Layer | Cumulative pilot success | Per-ticket input cost | Per-ticket latency |
|---|---:|---:|---:|
| Current baseline | 0/3 (0%) | ~50k tokens | ~12 min |
| + Phase 1 (built-in tools + PTC + static gate + 3x-loop **with context-reset**) | **~72%** | -40% | -50% |
| + Phase 2 (Critic + TDD + checklist) | **~80%** | +5% (Critic call) | +20% (Critic call) |
| + Phase 3 (Locator/Coder + repo-map + progress.txt **+ B13 AST blast-radius**) | **~88%** | -18% (cleaner context + oversize refusal) | +10% (extra phases) |
| + Phase 4 (lesson injection + cache + reflection) | **~90%** | -52% from baseline | -32% from baseline |

**Net at full deployment**: pilot success 0 → ~90%, input cost -52%, latency -32%. Theoretical ceiling per research is ~85–90% — Sprint B should land at the top of that range; anything past that needs frontier-model upgrade or human-in-loop.

**PS2 delta**: B13 adds ~3pp to Phase 3 success (cuts oversize-ticket failures), B3 context-reset adds ~2pp to Phase 1 success (cleaner restarts vs raw aborts). Total Sprint B ceiling rises from 88% → 90%.

---

## 6. Open decisions for tomorrow's discussion

**O1 — Sprint A vs Sprint B boundary**
Currently audit's OP-824 (built-in tools) + OP-825 (repo-map) live in Sprint A. Should they move into Sprint B Phase 1 + 3 for cleaner separation? Argument for moving: they're prerequisites for the new Sprint B phases. Argument against: Sprint A is already filed; reshuffling adds churn.

**O2 — Phase ordering: full Phase 1 before any Phase 2, or interleave?**
Sequential gives cleanest attribution of gains. Interleave gives faster cycle-time. Research recommends sequential (single-pipeline doctrine).

**O3 — Re-pilot cadence**
Re-run failed S1 tickets after each phase, or only at end of Sprint B? Per-phase pilot = more data, more cost (~$15 each pilot run). End-of-Sprint = single pilot, no incremental visibility.

**O4 — Critic model: Haiku vs Sonnet**
Plan above defaults to Haiku. Haiku is fast/cheap but may miss nuanced review issues. Sonnet doubles cost but matches coder quality. Pilot recommendation: start Haiku, escalate based on data.

**O5 — Anthropic harness adoption: full vs subset**
B6 (feature-list JSON) + B9 (progress.txt + 3-step opener) are the two harness pieces. Full adoption also includes Planner/Generator/Evaluator triad which overlaps with B4 (Critic) + B7 (Locator/Coder). Recommend: take feature-list + progress.txt, treat triad as already satisfied by B4+B7.

**O6 — Skip/include final review**
Research recommends skipping Best-of-N, model routing, AutoCodeRover, Replit-style. Confirm none are interesting enough to override?

**O7 — Budget cap for Sprint B**
~15 dev-days + ~$120 pilot. If we want a tighter-scope variant (Phase 1+2 only), it's ~8 days + ~$60 and reaches ~80% pilot success — may be the right minimum-viable Sprint.

**O8 — Auto-filing trigger**
After tomorrow's discussion, do we file Sprint B as one META + 9 children immediately, or stage filings phase-by-phase as gates pass?

---

## 7. What's NOT in Sprint B (and why)

For honesty / future-reference:

- **CodeAct** standalone (separate from PTC) — PTC supersedes it; Anthropic's GA implementation is the productionised form.
- **Best-of-N** (R6) — k×cost not Pareto-improving for our budget; revisit only if we hit a hard ticket where reliability matters more than cost.
- **Aggressive model routing** (R7) — premature; revisit Sprint+2 after we have per-complexity baselines.
- **AutoCodeRover orchestration** (R8) — not enough lift over single-agent; the AST retrieval is partly captured by built-in `text_editor` + B8 (repo-map).
- **Reflexion full self-critique loop** (R10) — captured more cheaply by B4 (Critic) + B10 (lesson injection).
- **HTN runtime task decomposition** — complexity not justified; manual ticket refinement covers it.
- **Trajectory replay / hindsight prompting** — high engineering cost, no public production case-study; defer indefinitely.
- **Constitutional AI for code** — partly covered by CLAUDE.md L1 rules + Critic (B4).

PS2 additions to skip-list with reasoning in §11:
- **LLMLingua-2** (operator candidate #1) — outclassed by Anthropic prompt cache (B11).
- **DSPy** (operator candidate #2) — paradigm shift, not a tool addition; weak fit for our every-ticket-unique workload.
- **Outlines / LMQL** (operator candidate #3) — **technically inapplicable** to Anthropic API (no logits access).
- **JIT-HTN** (operator candidate #5) — superseded for our scope by B3 context-reset + B13 blast-radius.
- **Capability Matrix** (operator candidate #6) — same reason as research R7; defer to Sprint C.

---

## 8. Sprint B → Sprint C teaser (not for filing)

If Sprint B lands at ~85% pilot success, Sprint C would target the remaining 15% via:
- **Model routing** (R7) — once baseline is stable per tier
- **Spec-driven generation** — runner refuses ambiguous tickets, requires structured spec
- **Persistent execution sandbox snapshotting** (beyond worktree)
- **Cross-ticket pattern detection** — auto-extend `mutex_with` from history
- **Adversarial AC validation** — separate model generates adversarial tests against AC

Sprint C is hypothetical — file only after Sprint B lands.

---

## 9. Operator-supplied candidates evaluation (PS2 — 2026-05-10)

Operator surfaced 6 additional industry methods. Per-item verdict + reasoning:

### ① LLMLingua-2 (Microsoft) — 🟡 LITE / SKIP
**Mechanism**: Local BERT-class model performs token-level binary classification, prunes stop words / fillers from prompt before sending to main model. Claims 3-6× compression on instructional text.

**Verdict**: SKIP. Three reasons:
1. **Wrong target distribution**. Our context is ~70% code + structured tool output (already maximally dense), ~10% AC prose, ~5% system-prompt boilerplate, ~15% error logs. LLMLingua works on prose, not code. Net global saving ~10-15%, not the headline 70%.
2. **Lossy compression risk**. Compressed AC may drop a "must" qualifier; compressed traceback may drop the relevant frame. Silent failure mode.
3. **Outclassed by cache_control (B11)**. Anthropic prompt caching is byte-exact, zero risk, 10 LOC, 40-60% input cost saving on multi-iteration tickets. LLMLingua is the "for vendors without cache" backup; we have cache. Don't pay the abstraction tax.

**Revisit trigger**: If B11 cache-hit rate stays below 50% in production for >2 weeks, reconsider.

### ② DSPy (Stanford) — 🔴 SKIP
**Mechanism**: Declarative `Signatures` (input/output specs); compiler optimizes prompts via test-case-driven search; "prompting is dead, programming is alive."

**Verdict**: SKIP for v1. Three reasons:
1. **Workflow shape mismatch**. DSPy excels with stable signature + golden samples + high-frequency repetition. Our every-ticket-unique workload provides none of those.
2. **Strong-model diminishing returns**. DSPy's published gains are largely on GPT-3.5-era models where prompt sensitivity is huge. Sonnet 4.6 / Opus 4.7 are far more robust to prompt phrasing — the optimisation surface is much flatter.
3. **Refactor cost**. Adopting DSPy means rewriting our entire prompt construction layer + introducing a new abstraction the team must learn. Not worth it for marginal gains.

**Revisit trigger**: If we ever stabilise a tight repeated subtask (e.g. "given lint output, produce a patch"), pilot DSPy on that ONE subtask. 6-month exploration timeframe, not a Sprint B child.

### ③ Outlines / LMQL — 🔴 NOT APPLICABLE (technical impossibility, not a judgment call)
**Mechanism**: Logits-level grammar enforcement. Force model output to match Pydantic schema / regex by zeroing probabilities of non-conforming tokens.

**Verdict**: NOT APPLICABLE. **Anthropic API does not expose logits.** Outlines/LMQL only work when you control inference (vLLM, llama.cpp, HuggingFace transformers). Our `client.beta.messages.create()` runs on Anthropic's GPUs; logits never leave their boundary.

For the "model wraps tool calls in 'Sure, here's...' filler" sub-problem the source article cites — Anthropic's `tool_use` block is already strict-JSON-schema enforced server-side. Filler appears in `text` blocks alongside `tool_use`, not inside them; mitigate via `system` prompt directive ("Do not preface tool calls with explanation") + `stop_sequences`.

**Revisit trigger**: If we ever activate the paused 4th-runner local LLM (qwen3.6 27B / RTX 4080) for production, Outlines becomes an option for that runner.

### ④ AST blast-radius (Tree-sitter dependency-depth + LOC) — 🟢 STRONG BUY → ADDED AS B13
**Mechanism**: Before pickup (or during locator phase), compute the dependency depth + LOC of candidate files. Hard physical bounds (e.g. `>3 files OR >300 LOC`) force task split or refusal. Auditable, LLM-free.

**Verdict**: STRONG BUY. **Filed as B13 in Phase 3.** Three reasons:
1. **Direct counter to S1 failure mode**. One of the recurring patterns in the 3 failed pilots was unclear ticket scope — model couldn't tell if it was supposed to do 1 file or 10. Physical bounds remove the ambiguity.
2. **Marginal cost ≈ 0**. We're already adopting tree-sitter for B8 (repo-map). B13 reuses the same graph; only adds the LOC/depth aggregation pass. ~150 LOC, 0.5d.
3. **Composes with B7 (Locator)**. Locator already enumerates candidate files; blast-radius check is a natural gate at the locator → coder boundary. Refuses to pass to coder if oversized; main runner then either splits or escalates.

**Defect mitigation**: D9 (component-aware threshold) — see §4.

### ⑤ JIT recursive decomposition (HTN with Task_Splitter_Agent) — 🟡 DEFER TO SPRINT C
**Mechanism**: After N (e.g. 5) failed iterations, dedicated splitter agent looks at partial state + error, decomposes ticket into smaller sub-tickets, re-queues.

**Verdict**: DEFER. Three reasons:
1. **Anthropic harness blog explicitly recommends `context reset` over decomposition**. Cheaper, simpler, empirically equivalent on their workloads. PS2 strengthens B3 to add this mechanism — that captures the bulk of the value.
2. **Splitter quality risk**. Bad decomposition can create more failures than retries. There's no public production case-study showing splitter agents net-improve, vs the well-documented "single linear agent + better state management" pattern.
3. **B3 + B13 already cover both ends**. B13 refuses oversize at the front (proactive); B3 + context-reset handles loop death (reactive). HTN sits between them. Unclear that the middle ground needs a separate solution.

**Revisit trigger**: After Sprint B lands, measure the failure mode distribution. If a meaningful fraction of failures are "not oversized + not loop, but model gives up mid-task on tractable work", reconsider HTN.

### ⑥ Capability Matrix (per-model AST-feature-based routing) — 🟡 DEFER TO SPRINT C
**Mechanism**: Per-model declared safe-operating-granularity (e.g. Opus = cross-file refactor OK; Haiku = single function only); runtime dispatch based on ticket's AST features.

**Verdict**: DEFER. Same conclusion as research agent's R7 (aggressive model routing) for the same reason: **routing variance masks reliability bugs in the absence of a stable per-tier baseline**. We are at 0/3 baseline. Adding routing now means a Phase-1 pilot failure could be model-fit OR routing-fit; we wouldn't know.

Additional blocker: the local-model cell (qwen3.6 / RTX 4080) is paused since 2026-05-03 (speed unstable). Until that's resolved, Capability Matrix is missing its cheapest tier.

**Revisit trigger**: After Sprint B lands at ≥85% baseline, file Sprint C child for capability-matrix routing. Local-LLM unblocking is independent; don't gate on it.

---

### Updated Sprint C candidate list (PS2 additions)

- HTN dynamic task splitting (depends on Sprint B failure-mode data)
- Capability Matrix / heterogeneous routing (depends on stable baseline + local LLM unblock)
- DSPy on a single stable subtask (depends on identifying one)
- LLMLingua selective application (depends on cache_control proving insufficient)

---

## 10. Cross-references

- Sprint A META: OP-808
- Audit (priors): `docs/audit/2026-05-09-aider-swe-agent-audit.md` (Gerrit #332)
- Lessons archive: `docs/sop/lessons-learned.md`
- Live pilot failures: 3× S1 attempts logged on OP-803/804/805
- ADR-0005 Tier S/M/L/X: most Sprint B children are Tier M (AI+1+Human+1) — high-stakes ones (B1 PTC sandbox, B7 Locator routing, B9 harness migration) flagged Tier L (Human+2) at filing time

---

## 11. Action items pre-discussion

Before tomorrow:
- [ ] Operator reads this plan + flags any of O1–O8 as discussion priorities
- [ ] Operator confirms the Phase 1 → Phase 4 ordering (or proposes alternative)
- [ ] Operator confirms scope: full 15d Sprint B vs 8d minimum-viable (Phase 1+2 only)
- [ ] Operator decides: file as JIRA META+9-children immediately, or stage by phase

After discussion:
- [ ] AI fleet files Sprint B JIRA tickets per agreed scope
- [ ] First child (B2 — static-analysis pre-flight, cheapest win) becomes the entry pilot to validate the Sprint B mechanics before larger children

PS3 additions:
- [ ] Operator reviews companion engineering doc (`docs/architecture/sdk-runner-sprint-b-error-handling.md`) — flags any §3 incident or §6 cross-cutting concern that needs scope upgrade
- [ ] Charter sign-off (§13 below): operator + AI fleet acknowledge G1–G4 gates before Phase 1 children file

---

## 12. Coverage matrix — child × 7 dimensions (PS3 summary)

Per-child summary of error/exit/state/recovery coverage. **Detailed catalogs in `docs/architecture/sdk-runner-sprint-b-error-handling.md` §4–§5.** This table is the dashboard view; cells link to that doc.

Legend: 🟢 covered (specified) · 🟡 partial (gap noted) · 🔴 gap (must fill before file)

| Child | Errors | Exceptions | Exit paths | Internal FSM | Cross-child | Recovery | Idempotency |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **B1** built-in tools + PTC | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 (PTC sandbox boundary, C7) | 🟢 | 🟡 (text_editor non-idempotent, token needed) |
| **B2** static-analysis gate | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 (cap counter must persist) | 🟢 |
| **B3** 3x-loop + context reset | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 (CostGuard cross-reset, C6) | 🟡 (mid-reset crash) | 🟢 |
| **B4** Critic | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 (NEVER posts Gerrit, F9) | 🟢 (read-only) | 🟢 |
| **B5** TDD enforcement | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 (TDD count vs B3 iter count interaction) | 🟢 | 🟡 (test file write token) |
| **B6** feature-list checklist | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 (bridge-state divergence, F5/F14) | 🟢 | 🔴 (staging analogue to F20 — must mandate "feature-list staged before commit") |
| **B7** Locator / Coder | 🟢 | 🟢 | 🟢 | 🟢 | 🔴 (cross-ticket peer detect, F6/F7/F12) | 🟡 (prior-PS rebase, F11) | 🟢 |
| **B8** repo-map preamble | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 (cache invalidate per commit) | 🟢 | 🟢 (pure func of HEAD) |
| **B9** harness session-resume | 🟢 | 🟢 | 🟢 | 🟢 | 🔴 (bridge currency, F4/F10) | 🟢 | 🟡 (concurrent-runner write race) |
| **B10** lesson auto-injection | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 (read-only) |
| **B11** cache control | 🟢 | 🟢 | 🟢 | 🟢 (trivial) | 🟢 | 🟢 | 🟢 |
| **B12** reflection loop | inherits B3 | inherits B3 | inherits B3 | inherits B3 | inherits B3 | inherits B3 | inherits B3 |
| **B13** AST blast-radius | 🟢 | 🟢 | 🟡 (oversize_refuse downstream policy) | 🟢 | 🔴 (cross-ticket peer, F6/F7/F12) | 🟢 | 🟢 |

**Gap counts**: 🔴 = 4 cells across 3 children (B6, B7, B9, B13). Each maps to a documented past-failure (F4, F6, F11, F20). All 4 must convert to 🟡 or 🟢 before respective Phase children enter In Progress.

**Cross-cutting concerns** (§6 of companion doc) coverage:

| # | Concern | Status |
|---|---|:-:|
| C1 | Process crash mid-execution | 🟡 (PTC sandbox state on resume open) |
| C2 | Concurrent runners | 🔴 (orphan-reaper not yet in code) |
| C3 | TOCTOU | 🟢 (codified as Invariant I6) |
| C4 | External cascade (Gerrit + JIRA) | 🟡 (Gerrit-first ordering specified, not yet enforced) |
| C5 | Semantic drift (cached stale content) | 🟢 (periodic file-view refresh new requirement) |
| C6 | CostGuard race | 🟢 (pre-call abort policy) |
| C7 | Sandbox boundary | 🟡 (PTC `cwd=worktree` required, not yet codified) |
| C8 | API surface drift | 🟡 (weekly cron new requirement, not built) |

**Cross-cutting 🔴 + 🟡 = 6 of 8**. These are addressed in §13 charter G2.

---

## 13. Sprint charter — pre-flight gates (PS3)

Before Phase 1 children enter `In Progress`, the following gates must all pass.

### G1 — Filing-time per-child spec lint

Every Sprint B JIRA ticket description MUST contain three required sections:
- `## Error catalog` — specific error codes (enum) + free-text fallback distinguished
- `## State transitions` — FSM nodes + legal/illegal transition handling
- `## Recovery / rollback` — what survives crash, where rollback target is

**Enforcement**: extend `docs/sop/jira-ticket-conventions.md` §14 vague-rejection lint with three required-section regex. CI check on ticket creation/edit.

**Owner**: AI fleet to add lint rule before any Sprint B child files. ~30 LOC.

### G2 — Cross-cutting mitigation in code/runbook

8 cross-cutting concerns (companion §6 C1–C8) each require a documented mitigation **landed** in code or runbook before Phase 1 starts. Status today:

| # | Mitigation needed | Where | Status |
|---|---|---|:-:|
| C1 | Phase-boundary git-stash + 1h reaper | `runner_recovery.py` (new, ~80 LOC) | 🔴 not started |
| C2 | Orphan-reaper cron in bridge daemon | bridge daemon extension (~50 LOC) | 🔴 not started |
| C3 | Live-state re-read at every transition (Invariant I6) | runner main loop discipline | 🟡 documented, not enforced |
| C4 | Gerrit-first JIRA-second ordering with idempotency tokens | `jira_dispatch.py` extension | 🟡 partial |
| C5 | Periodic file-view refresh (every 10 iter) | runner main loop (~20 LOC) | 🔴 not started |
| C6 | Pre-call abort if `current_spend + estimate > cap` | `cost_estimator.py` | 🟢 already in current launcher |
| C7 | PTC `cwd=worktree` + strict `allowed_callers` | B1 implementation | 🟡 spec'd in B1 |
| C8 | Weekly Anthropic spec watch cron | new ops cron | 🔴 not started |

**Gate decision**: if more than 3 of 8 are 🔴 at Phase 1 start, defer Phase 1 by 1 week to ship missing mitigations.

### G3 — Past-failure regression tests (Sprint B child B0)

The 4 🔴 incidents in §3 (F4, F6, F7, F11, F12, F20 — counted as 5 distinct test classes) need regression tests landed BEFORE other children file. Filing approach: **B0 = "Sprint B prerequisite — past-failure regression coverage"** as a Phase-0 child, blocking everything else.

Test classes:
- `test_runner_pickup_bridge_currency` (F4, F10) — fail if bridge >1h behind develop
- `test_b13_detects_cross_ticket_peer` (F6, F7, F12) — synthetic 2-ticket fixture, peer detected
- `test_b7_detects_prior_ps_for_change_id` (F11) — fixture with existing PS, rebase triggered
- `test_b6_feature_list_staged_before_commit` (F20) — assert no committed-without-staged
- `test_b9_warns_on_dead_stream_events` (F25) — fixture with stalled bridge

Estimated cost: 1.5 days. Inserted as new B0; total Sprint B = 11 children, ~17 days.

### G4 — Kill switch matrix (Sprint-level abort criteria)

Documented in companion doc §8 G4. Summary:

| Trigger | Action | Recovery seed |
|---|---|---|
| Phase 1 re-pilot < 2/3 success | Abort Sprint B | File Sprint C ("B1 didn't lift baseline") |
| Critic dissent rate > 50% on first 5 tickets | Pause Phase 2; review B4 model choice | Switch B4 to Sonnet |
| 3+ consecutive `loop_aborted_terminal` (B3) on different tickets | Pause; B3 detector mistuned | Tune `args_hash` |
| CostGuard hits global cap before Phase 3 entry | Pause + escalate | Operator scope reduction |
| 2+ orphan tickets in 24h (C2) | Pause + investigate | Fix mutex_with / reaper |
| Anthropic API breaking change (C8) | Pause Sprint B | Operator pin model version |

### Charter sign-off

Operator and AI fleet sign this charter before Phase 1 children enter In Progress. Sign-off = explicit approval of:
1. The 11 children + their FSM/error spec (per §3 + companion §4–5)
2. The 8 cross-cutting mitigations + their land-by-Phase-1 plan (G2)
3. B0 regression-test child as Phase-0 prerequisite (G3)
4. Kill switch matrix (G4)
5. Charter immutability — deviations require amend + re-sign, no silent drift

**Sign date**: TBD (after 2026-05-10 discussion).
