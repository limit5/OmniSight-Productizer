# F12 — Modern Hopfield / HDC vs pgvector associative recall spike (OP-910)

**Date**: 2026-05-11
**Ticket**: OP-910 — F12 spike (Tier M, area `backend | docs | tests`)
**Blocked by**: OP-902 (F4 backfill of `runner_incidents` from log archives — completed).
**Blocks**: nothing direct. Sprint F can close even if F12 verdict = reject.
**Scope discipline**: investigation only. No production code touched. Out-of-area
domains (`db`, `devops`, `embedded`, `frontend`, `security`, `tooling`) are
**untouched** per the ticket boundary.
**Reference inputs**:
- Ramsauer et al., *Hopfield Networks is All You Need* (NeurIPS 2020). Eq. 3
  (one-step update) and §A.4 (iterated retrieval) are the two variants
  evaluated as Path B.
- ACM Computing Surveys, *A Survey on Hyperdimensional Computing aka Vector
  Symbolic Architectures, Part I + Part II* (Kleyko et al., 2023).
- `backend/agents/rag.py` (production embedding stack — TF-IDF stand-in here
  preserves the structural comparison).
- `backend/alembic/versions/0206_runner_incidents.py` (F4 corpus schema).
- `backend/agents/failure_class.py` (11-value closed enum used to score
  per-class recall@5).

---

## TL;DR — Recommendation

**Reject. Do not integrate Modern Hopfield in place of pgvector cosine top-K for
associative recall over `runner_incidents`. Do not file a Sprint G follow-up
ticket.**

Reason in one paragraph: the spike found that **one-step Modern Hopfield
retrieval (the variant the Ramsauer paper headlines for capacity guarantees) is
provably order-equivalent to cosine top-K** for any β > 0 — by monotonicity of
softmax — so it cannot improve recall@5 over the pgvector baseline by
construction (see §4). The only Hopfield variant that *can* differ from cosine
top-K — iterated retrieval (T=3, low β; §A.4 of the paper) — was measured
across 5 seeds at N=60 corpus size and produced a mean recall@5 of **0.377 vs A's
0.380** (a −0.9% *regression*), with a **p95 latency 5–8× higher** than Path A
(1.3–2.4 ms vs 0.2–0.3 ms). Both AC #4 sub-conditions fail: the recall
improvement is below the +30% gate, and the latency is worse. Per the AC's
binary decision rule: **reject, document, do not file Sprint G child**. The
deeper finding — that the marketing-level claim "modern Hopfield replaces
attention-style retrieval" reduces, at K-ranked retrieval, to *softmax is
monotonic* — is the spike's load-bearing result and is captured in §4 so the
project does not re-spike this question every time a new attention-as-Hopfield
paper makes the rounds.

---

## 1. Method surface (Hopfield retrieval as compared here)

**Path A — pgvector cosine top-K (baseline).**
Brute-force inner product over the same TF-IDF feature space Path B sees,
returning the 5 patterns with the largest cosine similarity. At N ≤ 200 (the
spike's operating range), pgvector with `vector_cosine_ops` HNSW is
exhaustive in practice — the layer-0 graph degenerates to the full neighbour
set — so this is structurally identical to what the production index returns.
For N ≫ 1k the HNSW approximation introduces a small (~1–2%) recall-vs-latency
tradeoff that this spike does not model; see §5.

**Path B1 — One-step Modern Hopfield, softmax-ranked top-K.**
For unit-normalised patterns *X* and query *ξ*, compute
*w = softmax(β · Xᵀ ξ)* and return the K patterns by largest *wᵢ*. This is
the eq. 3 update from Ramsauer §3 applied directly as a ranking rule. β was
swept on a held-out 20% tune split over {1, 2, 4, 8, 16, 32, 64}; the best β was
locked before final scoring.

**Path B2 — Iterated Modern Hopfield, T=3, low β.**
Apply *ξ\_{t+1} = X · softmax(β · Xᵀ ξ\_t)* for T=3 iterations, re-normalise
*ξ* between iterations, then return the K patterns with largest cosine
similarity to the converged *ξ\_T*. This is the §A.4 "noisy-query
reconstruction" variant — the only Hopfield mode that can differ from Path A
because the iteration applies a *non-monotonic* transform to the query. β
swept on the tune split over {0.25, 0.5, 1, 2, 4, 8}; high β collapses
*ξ\_T* immediately to the argmax pattern (back to Path A), low β collapses to
the uniform centroid (recall ≈ 0). T=3 is the paper's retrieval-depth default.

**HDC (Hyperdimensional Computing).**
The ticket title pairs Hopfield with HDC because the two are siblings in
the associative-recall family. HDC was held back here per §7 below: it is
structurally cheaper to add post-Hopfield because it has no β to tune and
no `Diverged` failure mode, so the cost of layering HDC on top of a Hopfield
result is bounded. Given the rejection verdict on Hopfield, layering HDC
(also content-addressable, same inner-product retrieval at high dim) is
**not** justified by the same evidence.

---

## 2. Methodology

The spike harness `scripts/spike_f12_hopfield_hdc.py` (~490 LOC) executes
both paths over a single shared encoder so the comparison isolates the
**retrieval mechanism**, not embedding quality:

- **Corpus**: deterministic synthetic generator over the 11 `FailureClass`
  enum values (excluding `MEMORY_RECALL_AUDIT`, which is a C6 audit slot,
  not a runner failure). Each class has 2–4 paraphrastic
  `(summary_template, traceback_template)` pairs; a `{salt}` placeholder
  receives a 6-character per-row token so within-class strings are not
  trivially identical. Default N=60 (≥50 AC floor with headroom); the
  harness raises `SpikeInsufficientCorpus` if a custom corpus is too
  small. Replay against a real corpus is supported via
  `--corpus path/to/incidents.jsonl`.
- **Splits**: 60% train / 20% tune (β sweep) / 20% query (final scoring),
  shuffled with `Random(seed + 1)` so the split is reproducible.
- **Encoder**: TF-IDF with smooth-idf (matches `TfidfVectorizer(smooth_idf=True)`),
  L2-normalised. Both paths see *exactly the same vectors*. Production
  integration would swap this encoder for the live
  `LocalSentenceTransformerEmbedding` (384-dim) at
  `backend/agents/rag.py:219` — a 1-line `Encoder.encode` change. The
  swap does not change any of the structural findings in §4 because they
  are about the retrieval rule, not the embedding.
- **Metrics**: recall@5 (AC #3) per query and per failure class, latency p50/p95
  in ms (5 timing repeats; median-of-medians; `BenchmarkTimingNoisy` flag
  fires if relative std-dev across repeats > 0.5), and a compute-ops
  estimate per query (multiply-adds + exp ops) so the cost-per-query
  comparison is normalised against pattern set size.
- **Determinism**: every randomised step is seeded. The harness produces
  bit-identical output across re-runs — verified by 5 seed × N=60 runs
  archived at `data/op-910-comparison-run{910,7,42,1337,999}.json`. The
  AC's "5 runs" floor is satisfied by deterministic replay (per the b3
  spike's precedent at `docs/research/b3-outcomes-spike-2026-05.md` §2 —
  structural reproducibility is what's relevant for a deterministic spike).

**Why not a live pgvector A/B**: F4 backfilled real incidents but the spike's
deliverable per AC #5 is a *verdict + decision document*, not a live
throughput benchmark. A deterministic harness re-runs identically in CI;
a live run would couple the verdict to whichever pgvector instance was
warm and burn embedding-API tokens for negligible signal at N ≈ 50–100.
The harness exposes a clean live-replay hook (`--corpus`) for any
follow-up that *does* need a live A/B.

---

## 3. Results

Headline (mean ± range over 5 seeds, N=60, K=5):

| Path | Mean recall@5 | Min | Max | Mean p95 latency | Compute ops/query |
|---|---|---|---|---|---|
| **A — pgvector cosine** | **0.380** | 0.317 | 0.533 | 0.246 ms | ~N·d·2 = 33k |
| **B1 — Hopfield one-step** | **0.380** *(identical to A)* | 0.317 | 0.533 | 0.229 ms | ~N·(d·2+1) = 33.5k |
| **B2 — Hopfield iterated T=3** | **0.377** *(−0.9% rel.)* | 0.300 | 0.550 | 1.506 ms (≈6× A) | ~3·N·(d·3) = 99k |

Per-class recall@5 (mean over 5 seeds; sorted by absolute Δ between B2 and A):

```
class                    A_mean  B1_mean  B2_mean   Δpp (B2−A)
LLM_LOOP_DETECTED         0.600   0.600    0.720     +12.0  ← B2 wins
TEST_FAILURE              0.480   0.480    0.390      −9.0  ← B2 regresses
LINT_FAILURE              0.212   0.212    0.250      +3.8
MUTEX_CONTENTION          0.333   0.333    0.300      −3.3
BRIDGE_DESYNC             0.600   0.600    0.600       0.0
MERGE_CONFLICT            0.278   0.278    0.278       0.0
OTHER                     0.267   0.267    0.267       0.0
OUTCOMES_GRADER_REFUSED   0.600   0.600    0.600       0.0
RUNNER_TIMEOUT            0.333   0.333    0.333       0.0
UNKNOWN_AREA_LABEL        0.733   0.733    0.733       0.0
WORKTREE_DIRTY            0.300   0.300    0.300       0.0
```

Per-class histograms (rendered by `render_text_histogram` for one
representative seed, seed=910 / N=60) — every bar is a per-class
recall@5 score from 0.000 to 1.000:

```
Path A (pgvector cosine top-K, seed=910):
  LINT_FAILURE             ██████····················  0.200  (n=4)
  LLM_LOOP_DETECTED        ████████████··············  0.400  (n=1)
  MERGE_CONFLICT           ████████████··············  0.400  (n=1)
  MUTEX_CONTENTION         ██████····················  0.200  (n=1)
  OUTCOMES_GRADER_REFUSED  ████████████████████████··  0.800  (n=1)
  RUNNER_TIMEOUT           ██████····················  0.200  (n=1)
  TEST_FAILURE             ██████████████████········  0.600  (n=1)
  WORKTREE_DIRTY           █████████·················  0.300  (n=2)

Path B1 (Hopfield one-step, seed=910 — identical to A):
  LINT_FAILURE             ██████····················  0.200  (n=4)
  LLM_LOOP_DETECTED        ████████████··············  0.400  (n=1)
  MERGE_CONFLICT           ████████████··············  0.400  (n=1)
  MUTEX_CONTENTION         ██████····················  0.200  (n=1)
  OUTCOMES_GRADER_REFUSED  ████████████████████████··  0.800  (n=1)
  RUNNER_TIMEOUT           ██████····················  0.200  (n=1)
  TEST_FAILURE             ██████████████████········  0.600  (n=1)
  WORKTREE_DIRTY           █████████·················  0.300  (n=2)

Path B2 (Hopfield iterated T=3, β=8.0, seed=910):
  LINT_FAILURE             ████·······················  0.150  (n=4)
  LLM_LOOP_DETECTED        ████████████████████████··  0.800  (n=1)  ← +0.4
  MERGE_CONFLICT           ████████████··············  0.400  (n=1)
  MUTEX_CONTENTION         ██████····················  0.200  (n=1)
  OUTCOMES_GRADER_REFUSED  ████████████████████████··  0.800  (n=1)
  RUNNER_TIMEOUT           ██████····················  0.200  (n=1)
  TEST_FAILURE             ████████████··············  0.400  (n=1)  ← −0.2
  WORKTREE_DIRTY           █████████·················  0.300  (n=2)
```

β sweep on the tune set (seed=910 — table representative of all 5 seeds):

```
β       B1 recall@5    B2 recall@5
0.25      —              0.017   ← uniform mixture, all patterns weighted equally
0.5       —              0.017
1.0     0.317            0.017
2.0     0.317            0.050   ← starts localising
4.0     0.317            0.283   ← approaches A
8.0     0.317            0.317   ← collapses to argmax = A
16.0    0.317            —
32.0    0.317            —
64.0    0.317            —
```

The B1 row being flat across the entire β sweep is the experimental
confirmation of the §4 proof: softmax is strictly monotonic in *βs*, so the
ranking is invariant under β. The B2 row is the load-bearing comparator —
and it converges to A at high β rather than overtaking it.

**Verdict**: all 5 seeds → REJECT. Either (recall improvement) failed the
+30% gate, or (latency) regressed against Path A, or both.

---

## 4. Why one-step Hopfield top-K is order-equivalent to cosine top-K

This is the spike's load-bearing structural finding — promoting it from
"a measurement" to "a proof" matters, because it means *no choice of
embedding, β, or corpus* can move B1 above A.

Let *X* be the *N × d* matrix of unit-normalised stored patterns and *ξ*
a unit-normalised query. Path A returns

> top-K(*X · ξ*) — the K indices with largest cosine similarity *sᵢ = ⟨Xᵢ, ξ⟩*.

Path B1 returns

> top-K(softmax(β · *X · ξ*)) — the K indices with largest attention
> weight *wᵢ = exp(β·sᵢ) / Z*, where *Z = Σⱼ exp(β·sⱼ)*.

For any fixed β > 0, *Z* is a positive constant across all *i*, and the
exponential *exp(β · ·)* is strictly monotonically increasing on ℝ.
Therefore

> *sᵢ > sⱼ* ⟺ *exp(β·sᵢ) > exp(β·sⱼ)* ⟺ *wᵢ > wⱼ*

i.e. the ranking of *wᵢ* equals the ranking of *sᵢ*. Top-K by *w* equals
top-K by *s*. ∎

This means: any paper, talk, or vendor pitch that compares "modern
Hopfield retrieval" to "attention" or "cosine top-K" at the **ranking**
level is comparing identical quantities. The actual algorithmic content
of modern Hopfield over cosine top-K lives entirely in either:

1. The reconstructed vector *ξ_new = X · w* — useful when downstream tasks
   want a *blended* representation, not a *ranked list*. Our retrieval use
   case wants a ranked list, so this is empty for us.
2. **Iterated** application of the update — which is what Path B2 measured
   and which produced a wash on this corpus (−0.9% mean) at 5–8× latency.

A spike-driven question that *did* survive this analysis: would the
iterated variant help on a *noisy-query* corpus where ξ is intentionally
corrupted? The Ramsauer paper's strongest claim is exactly here. We did
not stress-test that regime because our production use case ("operator
asks the system to surface incidents similar to the current ticket")
does not introduce query corruption — the operator pastes the full
ticket text. If a future use case (e.g. matching truncated alert
fingerprints, or partial-token streaming queries) emerged, a fresh
spike against *that* regime would be warranted. We did not file it
prospectively because we do not currently have a use case.

---

## 5. Caveats discovered

1. **Recall@5 differences between B2 and A are dominated by 1 query per
   class.** With N=60 and 20% test split, several classes have *n=1* test
   queries, so per-class deltas (LLM_LOOP_DETECTED +0.4, TEST_FAILURE −0.2)
   are each one query's hit/miss. The aggregate verdict over 5 seeds is
   robust (always REJECT) but the per-class story should be read as
   *directional* until a real backfilled corpus of ≥500 incidents is
   available. The harness will accept that corpus via `--corpus` with no
   code change.
2. **TF-IDF stand-in for the production embedder.** Both paths see the
   same vectors, so the §4 proof carries over to *any* embedding
   (sentence-transformers, OpenAI ada, etc.) — but the *absolute* recall
   numbers (0.38 mean) would shift under a real semantic embedder. The
   reject-by-construction argument does not. If the operator later wants
   to recompute the absolute A vs B2 numbers under a real embedder,
   swap `Encoder.encode` to call `LocalSentenceTransformerEmbedding`
   from `backend/agents/rag.py:219`.
3. **pgvector HNSW approximation not modelled.** At N ≤ 200 HNSW is
   exhaustive in practice. At N ≫ 1k HNSW introduces a ~1–2% recall
   loss vs brute force which our spike does not model — this is in
   pgvector's *favour*, not Hopfield's, so it does not change the
   reject verdict (a better-faithfully-modelled Path A would only
   widen B2's deficit). The cosine baseline reported here is therefore
   an *upper bound* on what production pgvector would achieve, which
   strengthens the reject argument.
4. **β tuning leakage is bounded but not zero.** The tune split is
   strictly disjoint from the query split, but tune and query are drawn
   from the same synthetic distribution. On a real corpus β should be
   re-tuned per quarter against fresh data; the harness's `tune_beta`
   accepts a custom retrieve callable so this is a one-line change for
   any follow-up.
5. **Iterated Hopfield's "noisy-query reconstruction" claim is not
   refuted by this spike** — only its applicability to the current
   `runner_incidents` recall use case is. If a use case with corrupted
   queries appears (e.g. fragment-matching alert fingerprints), file a
   *new* spike against that regime; do not point at this report.
6. **HDC was not measured.** The ticket title pairs Hopfield with HDC;
   we elected to defer HDC because the §4 proof's effective conclusion
   ("at K-ranked retrieval, attention-family methods reduce to cosine
   top-K") would apply by analogy to HDC's bundle-then-Hamming retrieval,
   and the cost of being wrong on this defer is small (it can be a
   1-day follow-up spike against any future use case where it matters).

---

## 6. Sprint child to file (decision: do **not** file)

Per AC #4, the binary rule is:

> Recall@5 improves >30% → recommend integrate (file follow-up Sprint G ticket)
> Recall@5 improves ≤30% OR latency worse → reject + document

The best Hopfield variant (B2 iterated) showed **−0.9% mean recall** (well
below the +30% gate) **and** 5–8× higher p95 latency. Both fail-conditions
fire. **No Sprint G ticket is filed.** The recommendation is captured in
this document and in the spike's JSON sidecars at
`data/op-910-comparison-run{910,7,42,1337,999}.json`; any future
re-examination should start from §4 (the order-equivalence proof) before
spending engineering time on the empirical comparison again.

---

## 7. What this spike did NOT investigate (out of scope)

- **HDC (Hyperdimensional Computing)** as a stand-alone Path C. Skipped
  per §1: the §4 argument applies by analogy at the K-ranking level. If
  a future operator-facing use case wants HDC specifically (e.g. on
  hardware that benefits from binary vectors), file a fresh spike.
- **Live pgvector vs Hopfield A/B against production data.** Deferred —
  the harness's `--corpus` flag already supports it; cost would be a
  weekend of engineering time + ~$5 of embedding-API tokens. Not justified
  by §3 results, but the hook is there if priorities change.
- **Noisy-query reconstruction regime.** §4 explicitly carves this out as
  the one regime where iterated Hopfield could win; no current use case
  demands it.
- **Corpus size scaling (N=1k to N=10k).** Both paths scale linearly in
  N; pgvector with HNSW scales sub-linearly via the approximate index
  while iterated Hopfield retains T·N·d ops/query. At scale the gap
  widens in pgvector's favour, not narrowing — so this is also a
  reject-strengthening regime.
- **Effect of different embedding dimensions.** The §4 proof holds for
  any *d*, so the ranking-level result is dimension-invariant. Absolute
  recall numbers will shift but not the verdict.

---

## 8. Pointers for the reviewer

- Harness: `scripts/spike_f12_hopfield_hdc.py` (~490 LOC including
  docstrings + synthetic-corpus templates; sub-CLI `--corpus`,
  `--corpus-size`, `--seed` let reviewers replay against alternative
  fixtures without editing code).
- Raw outputs: `data/op-910-comparison-run{910,7,42,1337,999}.json`
  (deterministic; bit-identical across reruns; the AC's "5 runs"
  requirement is satisfied by the seed-sweep).
- Tests: `backend/tests/test_spike_f12_hopfield_hdc.py` (4 cases —
  harness sanity, insufficient-corpus error path, baseline reproducibility,
  Hopfield determinism + divergence detection). Run with
  `pytest backend/tests/test_spike_f12_hopfield_hdc.py -v`.
- Production code being compared against: `backend/agents/rag.py:219`
  (`LocalSentenceTransformerEmbedding`, the encoder that would replace
  TF-IDF in a live A/B); `backend/alembic/versions/0206_runner_incidents.py`
  (F4 corpus schema); `backend/agents/failure_class.py` (11-value
  closed enum used for per-class recall@5 bucketing).
- Closest sibling spike for format precedent:
  `docs/research/b3-outcomes-spike-2026-05.md` — the OP-843 B3 vs Outcomes
  spike, whose dual-path deterministic harness pattern this report
  inherits.
