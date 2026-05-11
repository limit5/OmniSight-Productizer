#!/usr/bin/env python3
"""F12 — Modern Hopfield vs pgvector associative-recall spike (OP-910).

Dual-path retrieval benchmark over the ``runner_incidents`` failure-class
corpus produced by F4 (OP-902).

  * **Path A — pgvector ANN baseline.** Cosine top-K over the same TF-IDF
    encoding both paths see. For the spike's corpus size (N ≤ 200) pgvector
    with ``vector_cosine_ops`` is exhaustive even with an HNSW index, so
    brute-force cosine is structurally identical to what the production
    table would return; this is documented in the report §2.
  * **Path B — Modern Hopfield (Ramsauer et al. 2020).** Two evaluated
    variants of the log-sum-exp energy update::

        ξ_new = X · softmax(β · Xᵀ ξ)

    Patterns X (rows = stored incidents, columns = TF-IDF features) and
    the query ξ are unit-normalised. The two variants are:

      - **B1 (one-step, softmax-ranked top-K)** — return the K patterns
        ranked by ``softmax(β · Xᵀ ξ)`` weight. Note: this is
        *provably order-equivalent* to Path A by monotonicity of softmax —
        any β > 0 induces the same ranking as ranking by raw inner
        product. B1 is reported anyway because the marketing-level claim
        is "Hopfield replaces top-K retrieval" and the spike's job is to
        surface that this is structurally vacuous for K-ranked
        retrieval. See report §4 for the proof sketch.
      - **B2 (iterated, T=3, low β)** — apply the update T times before
        ranking the K patterns nearest the converged ξ_T. This is the
        variant Ramsauer §4 motivates as *noisy-query reconstruction*:
        the iterated update mixes correlated patterns into a denoised
        representative which can pull cluster-mate incidents into the
        top-K that the raw cosine baseline would have ranked below
        unrelated near-neighbours. β is tuned on a held-out set; T is
        fixed to 3 (paper-default for retrieval depth in §A.4).

Why this design rather than a live pgvector vs Hopfield run:
  - F4 backfill landed an empirical corpus, but the spike's deliverable per
    the AC is a *verdict + decision doc*, not a live throughput benchmark.
    A deterministic harness reruns identically in CI; a live run would
    couple the verdict to whichever pgvector instance happened to be warm
    (and would burn embedding-API tokens for negligible signal at N≈50).
  - Both paths share the same encoder (TF-IDF over the corpus vocabulary)
    so the comparison isolates the **retrieval mechanism**, not embedding
    quality. Swapping to a live ``sentence-transformers/all-MiniLM-L6-v2``
    encoder is a 1-line change (see ``Encoder.encode``); the report calls
    this out as the follow-up A/B if Path B wins.
  - HDC (Hyperdimensional Computing) was scoped in the ticket title
    alongside Hopfield. It is structurally a sibling associative-recall
    family (binary bundling + Hamming) and gets the same dual-path
    treatment if Hopfield wins; held back here per §7 of the decision doc
    to keep the LOC budget bounded.

Error catalog (per ticket):
  - ``SpikeInsufficientCorpus`` — fewer than 50 incidents available.
  - ``HopfieldTrainingDiverged`` — log-sum-exp produced ``nan`` / ``inf``.
  - ``BenchmarkTimingNoisy`` — relative std-dev of 5 latency repeats > 0.5
    (median used regardless; the flag goes into the report).

Usage::

    python scripts/spike_f12_hopfield_hdc.py \
        --output data/op-910-comparison.json

    # Override corpus size / seed (must still pass >=50).
    python scripts/spike_f12_hopfield_hdc.py --corpus-size 80 --seed 7

    # Replay against a custom corpus dumped from production
    # (one JSON object per line: {failure_class, summary, raw_traceback}).
    python scripts/spike_f12_hopfield_hdc.py --corpus path/to/incidents.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("spike_f12_hopfield_hdc")

_TOP_K = 5
_MIN_CORPUS = 50
_DECISION_THRESHOLD_PCT = 30.0   # AC #4: >30% recall@5 improvement = integrate
_TIMING_REPEATS = 5              # AC error catalog: BenchmarkTimingNoisy → median of 5
_TIMING_NOISE_RATIO = 0.5        # std/median above this → flag noisy
_BETA_SWEEP: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
_ITER_BETA_SWEEP: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
_ITER_STEPS = 3                  # Ramsauer §A.4 retrieval-depth default


# ── Errors ─────────────────────────────────────────────────────────────


class SpikeInsufficientCorpus(RuntimeError):
    """Raised when the corpus has fewer than 50 incidents (AC #2)."""


class HopfieldTrainingDiverged(RuntimeError):
    """Raised when the log-sum-exp energy update produced nan/inf."""


# ── Incident schema ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Incident:
    """Minimal projection of ``runner_incidents`` rows needed for retrieval.

    The production table (alembic 0206) carries more columns
    (``ticket_key``, ``mutex_label``, ``created_at`` etc.); the spike only
    needs the three fields used by both retrieval paths. Replaying with a
    custom corpus only requires these three.
    """

    failure_class: str
    summary: str
    raw_traceback: str

    def text(self) -> str:
        return f"{self.summary}\n{self.raw_traceback}"


# ── Synthetic corpus generator ─────────────────────────────────────────

# Templates chosen to (a) cover every FailureClass in
# backend/agents/failure_class.py except MEMORY_RECALL_AUDIT (audit row,
# not a runner failure), and (b) produce paraphrastic intra-class variance
# so neither cosine nor Hopfield is trivially "perfect by lookup".

# Each entry: failure_class → list of (summary_template, traceback_template)
# pairs. ``{salt}`` placeholders take a deterministic per-row token to
# break exact-string overlaps within a class.

_CLASS_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "LINT_FAILURE": [
        ("checkpatch flagged style violations in {salt} commit",
         "ERROR:CODE_INDENT_SPACES:patch missing tabs at line {salt}"),
        ("ruff failed on backend/agents/{salt}.py",
         "E501 line too long ({salt} > 120 characters)"),
        ("mypy strict mode rejected return type in {salt}",
         "error: Incompatible return value type (got Optional[{salt}])"),
        ("eslint blocked frontend module {salt}",
         "no-unused-vars: '{salt}' is defined but never used"),
    ],
    "TEST_FAILURE": [
        ("pytest red on tests/test_{salt}.py",
         "AssertionError: expected {salt} but got None"),
        ("integration suite failed at stage {salt}",
         "FAILED tests/integration/test_{salt}.py::test_happy_path"),
        ("playwright nightly failing visual diff for {salt}",
         "Screenshot comparison failed for component {salt} (pixel diff > 0.5%)"),
        ("flaky test re-run also red: {salt}",
         "test_{salt} failed 3/3 retries; not retried again"),
    ],
    "MERGE_CONFLICT": [
        ("merger-agent could not auto-resolve {salt}.py conflict",
         "CONFLICT (content): Merge conflict in {salt}.py near hunk @@ -10,7 +10,7 @@"),
        ("rebase onto develop produced ambiguous deletion in {salt}",
         "warning: deleted file but conflicting modification in {salt}"),
        ("three-way merge over alembic migration {salt} aborted",
         "fatal: refusing to merge unrelated histories near {salt}"),
    ],
    "WORKTREE_DIRTY": [
        ("runner pickup found uncommitted .runner-cwd-sentinel for {salt}",
         "working tree not clean: .runner-cwd-sentinel modified ({salt})"),
        ("untracked file {salt}.tmp blocked push",
         "error: untracked working tree files would be overwritten: {salt}.tmp"),
        ("staged changes leaked across tickets for ticket {salt}",
         "git status: M backend/agents/{salt}.py (unexpectedly modified)"),
    ],
    "LLM_LOOP_DETECTED": [
        ("loop_detector fired 3x repeat on Glob args for {salt}",
         "LoopDetector: tool=Glob args_hash={salt} error=bash_metachar_blocked count=3"),
        ("repeat 2 error guard triggered on Bash command {salt}",
         "LoopDetector: tool=Bash error_class=path_traversal_blocked count=3 ticket={salt}"),
        ("reflection loop on Edit tool args for {salt}",
         "loop_aborted_terminal after RESET_LIMIT exhausted (ticket {salt})"),
        ("Levenshtein progressive narrowing absent for {salt}",
         "LoopDetector exempt failed: token edit-distance < 0.1 ({salt})"),
    ],
    "UNKNOWN_AREA_LABEL": [
        ("ticket {salt} carried area label outside the closed set",
         "scope_resolver: area '{salt}' not in known buckets"),
        ("area label 'embed-frontend' unknown for {salt}",
         "UnknownAreaLabel: '{salt}' has no scope-to-paths mapping"),
    ],
    "BRIDGE_DESYNC": [
        ("gerrit→jira bridge dropped change-set update for {salt}",
         "BridgeDesync: gerrit_change_id={salt} not mirrored to JIRA"),
        ("jira→gerrit catchup found missed transition on {salt}",
         "bridge_state_catchup: applied {salt} backlog event(s)"),
        ("webhook missed Code-Review +2 mirror for change {salt}",
         "Bridge missed CR+2 event for change {salt}; reapplied via catchup"),
    ],
    "RUNNER_TIMEOUT": [
        ("runner wall-clock budget exceeded on {salt}",
         "RunnerTimeout: ticket {salt} exceeded 45m wall-clock budget"),
        ("agent loop hung past M-tier limit for {salt}",
         "timeout after 2700s; last tool_use=Bash args_hash={salt}"),
    ],
    "MUTEX_CONTENTION": [
        ("file_coordinator could not acquire mutex {salt}",
         "MutexContention: could not lock backend/agents/{salt}.py within 30s"),
        ("two runners raced on shared label {salt}",
         "lock acquisition timeout: holder=runner-{salt} waiter=runner-other"),
    ],
    "OUTCOMES_GRADER_REFUSED": [
        ("outcomes grader returned 'insufficient signal' for {salt}",
         "OutcomesGraderRefused: rubric_id={salt} verdict=refused reason=signal"),
        ("grader rejected attempt {salt} citing rubric ambiguity",
         "outcomes.grader_verdict=refused; rubric_id={salt} cites missing run_tests"),
    ],
    "OTHER": [
        ("unclassified runner failure on {salt}",
         "ValueError: unexpected token in branch metadata ({salt})"),
        ("misc terminal abort observed for {salt}",
         "RuntimeError: orchestrator hit unreachable state ({salt})"),
    ],
}


def generate_synthetic_corpus(n: int, seed: int) -> list[Incident]:
    """Return ``n`` synthetic incidents with paraphrastic intra-class variance.

    The generator is deterministic given ``(n, seed)``. It cycles failure
    classes round-robin so each class appears at least
    ``floor(n / len(classes))`` times, ensuring per-class recall@5 numbers
    are computable (a class with <5 examples is structurally unable to hit
    recall@5 = 1.0).
    """
    rng = random.Random(seed)
    classes = list(_CLASS_TEMPLATES.keys())
    out: list[Incident] = []
    salt_alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # readable, no l/i/0/1
    for i in range(n):
        cls = classes[i % len(classes)]
        templates = _CLASS_TEMPLATES[cls]
        summary_tpl, tb_tpl = rng.choice(templates)
        salt = "".join(rng.choices(salt_alphabet, k=6))
        out.append(Incident(
            failure_class=cls,
            summary=summary_tpl.format(salt=salt),
            raw_traceback=tb_tpl.format(salt=salt),
        ))
    return out


def load_corpus(path: str | None, n: int, seed: int) -> list[Incident]:
    if path is None:
        corpus = generate_synthetic_corpus(n, seed)
    else:
        raw = Path(path).read_text(encoding="utf-8").splitlines()
        corpus = [
            Incident(
                failure_class=row["failure_class"],
                summary=row["summary"],
                raw_traceback=row["raw_traceback"],
            )
            for row in (json.loads(line) for line in raw if line.strip())
        ]
    if len(corpus) < _MIN_CORPUS:
        raise SpikeInsufficientCorpus(
            f"corpus has {len(corpus)} incidents; need ≥{_MIN_CORPUS} (AC #2). "
            "Either expand the synthetic generator or wait for F4 backfill "
            "to produce more historical incidents."
        )
    return corpus


# ── Encoder (shared between paths) ─────────────────────────────────────


def _tokenize(text: str) -> list[str]:
    """Lowercase + split on non-word characters. Stable across Python versions."""
    out: list[str] = []
    cur: list[str] = []
    for ch in text.lower():
        if ch.isalnum() or ch == "_":
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur.clear()
    if cur:
        out.append("".join(cur))
    return out


@dataclass
class Encoder:
    """TF-IDF encoder fit on the corpus vocabulary.

    The spike's correctness rests on Path A and Path B seeing **identical**
    vectors so the comparison isolates the retrieval mechanism. Production
    integration would swap this encoder for the live
    ``LocalSentenceTransformerEmbedding`` (384-dim) wired in
    ``backend/agents/rag.py`` — the public surface here (``encode`` returns
    a unit-norm dense vector) is the same.
    """

    vocabulary: dict[str, int]
    idf: list[float]

    @classmethod
    def fit(cls, corpus: Iterable[Incident]) -> "Encoder":
        documents = [_tokenize(inc.text()) for inc in corpus]
        df: Counter[str] = Counter()
        for doc in documents:
            for token in set(doc):
                df[token] += 1
        n_docs = len(documents)
        vocab = {tok: idx for idx, tok in enumerate(sorted(df))}
        idf = [0.0] * len(vocab)
        for tok, idx in vocab.items():
            # smooth idf — matches sklearn TfidfVectorizer(smooth_idf=True)
            idf[idx] = math.log((1 + n_docs) / (1 + df[tok])) + 1.0
        return cls(vocabulary=vocab, idf=idf)

    def encode(self, text: str) -> list[float]:
        tokens = _tokenize(text)
        if not tokens:
            return [0.0] * len(self.vocabulary)
        tf: Counter[str] = Counter(tokens)
        vec = [0.0] * len(self.vocabulary)
        for tok, count in tf.items():
            idx = self.vocabulary.get(tok)
            if idx is None:
                continue
            vec[idx] = count * self.idf[idx]
        # L2 normalise → cosine similarity becomes plain dot product.
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ── Path A — pgvector cosine top-K ─────────────────────────────────────


def pgvector_topk(
    query: list[float],
    patterns: list[list[float]],
    k: int = _TOP_K,
) -> list[int]:
    """Brute-force cosine top-K over pre-normalised patterns.

    At N ≤ 200, pgvector with an HNSW ``vector_cosine_ops`` index is
    effectively exhaustive (the layer-0 graph degenerates to the full
    neighbour set), so brute-force cosine is structurally identical to
    what production would return. For N ≫ 1k the HNSW approximation
    introduces a small recall-vs-latency tradeoff; explicitly out of
    scope for this spike — see report §5.
    """
    scores = [_dot(query, p) for p in patterns]
    indexed = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return indexed[:k]


# ── Path B — Modern Hopfield (log-sum-exp) ─────────────────────────────


def _stable_softmax(scores: list[float]) -> list[float]:
    """Numerically stable softmax (subtract max trick).

    Raises :class:`HopfieldTrainingDiverged` if any nan/inf shows up
    post-exponentiation (the AC error catalog requirement).
    """
    if not scores:
        return []
    m = max(scores)
    exps = [math.exp(s - m) for s in scores]
    z = sum(exps)
    if z == 0.0 or math.isnan(z) or math.isinf(z):
        raise HopfieldTrainingDiverged(
            f"softmax denominator unstable: z={z!r}, max(scores)={m!r}, "
            f"n_scores={len(scores)}"
        )
    out = [e / z for e in exps]
    if any(math.isnan(v) or math.isinf(v) for v in out):
        raise HopfieldTrainingDiverged(
            "softmax produced nan/inf after normalisation; β likely too high "
            f"(max score={m!r}, denom z={z!r})"
        )
    return out


def hopfield_topk(
    query: list[float],
    patterns: list[list[float]],
    beta: float,
    k: int = _TOP_K,
) -> list[int]:
    """B1 — Modern Hopfield one-step associative recall.

    Per Ramsauer et al. (2020 §3, eqn 3), retrieval of pattern ``i`` is
    given by the attention weight ``w_i = softmax(β · Xᵀ ξ)_i``. We return
    the top-K patterns by attention weight.

    Note (called out in the report §4): softmax is strictly monotonic, so
    ranking by ``softmax(β·s)`` is identical to ranking by ``s`` for any
    β > 0 — making this variant *order-equivalent to Path A*. We compute
    the softmax anyway to surface the ``HopfieldTrainingDiverged`` error
    mode the AC requires us to model.
    """
    raw = [beta * _dot(query, p) for p in patterns]
    weights = _stable_softmax(raw)
    indexed = sorted(range(len(weights)), key=lambda i: weights[i], reverse=True)
    return indexed[:k]


def hopfield_iterated_topk(
    query: list[float],
    patterns: list[list[float]],
    beta: float,
    k: int = _TOP_K,
    steps: int = _ITER_STEPS,
) -> list[int]:
    """B2 — Iterated Hopfield retrieval (noisy-query reconstruction).

    Apply the Ramsauer update ``ξ_{t+1} = X · softmax(β · Xᵀ ξ_t)`` for
    ``steps`` iterations, then rank patterns by cosine similarity to the
    converged ξ_T. With low β, the iteration mixes correlated patterns
    into a denoised cluster-representative; this is the *non-trivial*
    Hopfield variant — it is **not** order-equivalent to Path A because
    ξ_T differs from ξ_0 by a non-monotonic transform.

    Returns top-K pattern indices.
    """
    xi = list(query)
    d = len(query)
    for _ in range(steps):
        raw = [beta * _dot(xi, p) for p in patterns]
        weights = _stable_softmax(raw)
        # ξ_new = X · w (column-wise mixture of patterns weighted by w).
        new_xi = [0.0] * d
        for w, pat in zip(weights, patterns):
            if w == 0.0:
                continue
            for j in range(d):
                new_xi[j] += w * pat[j]
        # Re-normalise so subsequent inner products remain in [-1, 1].
        norm = math.sqrt(sum(v * v for v in new_xi))
        if norm == 0.0:
            raise HopfieldTrainingDiverged(
                "iterated update collapsed to zero vector — β likely too low "
                "for the pattern set (all weights concentrate equally)."
            )
        xi = [v / norm for v in new_xi]
    # Final ranking: cosine similarity of converged ξ to each stored pattern.
    return pgvector_topk(xi, patterns, k=k)


def tune_beta(
    train: list[Incident],
    tune: list[Incident],
    encoder: Encoder,
    retrieve,
    sweep: Iterable[float],
) -> tuple[float, dict[float, float]]:
    """Pick β that maximises tune-set recall@5. Returns (best, sweep_table).

    ``retrieve`` is a callable ``(query_vec, pattern_vecs, beta) -> top_k_idx``
    so the same tuning logic serves both B1 and B2.
    """
    train_vecs = [encoder.encode(inc.text()) for inc in train]
    table: dict[float, float] = {}
    for beta in sweep:
        try:
            hits = 0
            for q in tune:
                qv = encoder.encode(q.text())
                top = retrieve(qv, train_vecs, beta)
                hits += sum(
                    1 for i in top if train[i].failure_class == q.failure_class
                )
            table[beta] = hits / (len(tune) * _TOP_K)
        except HopfieldTrainingDiverged:
            table[beta] = 0.0  # divergence ≡ failure; eliminate from sweep
    best = max(table, key=lambda b: table[b])
    return best, table


# ── Metric collection ──────────────────────────────────────────────────


@dataclass
class PerClassMetric:
    """Recall@5 per failure class."""

    failure_class: str
    n_queries: int
    recall_at_5: float


@dataclass
class PathMetrics:
    path: str
    overall_recall_at_5: float
    per_class: list[PerClassMetric]
    latency_p50_ms: float
    latency_p95_ms: float
    latency_median_repeats_ms: list[float]
    timing_noise_flag: bool
    compute_ops_per_query: int
    notes: list[str] = field(default_factory=list)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _recall_at_k(
    queries: list[Incident],
    query_vecs: list[list[float]],
    pattern_incidents: list[Incident],
    retriever,
    k: int = _TOP_K,
) -> tuple[float, dict[str, list[float]]]:
    """Compute overall recall@k and per-class hit rate (list of per-query hits/k).

    "recall@k" in the AC #3 sense: of the top-k retrieved patterns, what
    fraction share the query's failure_class. Averaged across queries.
    Per-class returns a dict keyed by query.failure_class of per-query
    hit rates (each in [0, 1]) so the report can build a histogram.
    """
    per_class: dict[str, list[float]] = defaultdict(list)
    all_hits: list[float] = []
    for q, qv in zip(queries, query_vecs):
        top = retriever(qv)
        hits = sum(1 for i in top if pattern_incidents[i].failure_class == q.failure_class)
        rate = hits / k
        all_hits.append(rate)
        per_class[q.failure_class].append(rate)
    overall = statistics.mean(all_hits) if all_hits else 0.0
    return overall, per_class


def _measure_latency(
    queries: list[Incident],
    query_vecs: list[list[float]],
    retriever,
    repeats: int = _TIMING_REPEATS,
) -> tuple[float, float, list[float], bool]:
    """Return (p50_ms, p95_ms, per_repeat_median_ms, noise_flag).

    Per AC error catalog ``BenchmarkTimingNoisy`` — we always repeat 5×
    and use the median of medians; the noise flag tells the report whether
    individual queries had high jitter.
    """
    medians: list[float] = []
    last_per_query: list[float] = []
    for _ in range(repeats):
        per_query_ms: list[float] = []
        for qv in query_vecs:
            t0 = time.perf_counter()
            retriever(qv)
            per_query_ms.append((time.perf_counter() - t0) * 1000.0)
        medians.append(statistics.median(per_query_ms))
        last_per_query = per_query_ms
    median_of_medians = statistics.median(medians)
    p95 = _percentile(last_per_query, 95.0)
    # Noise flag: stdev across repeat-medians relative to the median itself.
    if len(medians) >= 2 and median_of_medians > 0.0:
        rel = statistics.pstdev(medians) / median_of_medians
        noise = rel > _TIMING_NOISE_RATIO
    else:
        noise = False
    return median_of_medians, p95, medians, noise


# ── Top-level orchestration ────────────────────────────────────────────


@dataclass
class SpikeResult:
    corpus_size: int
    seed: int
    train_size: int
    tune_size: int
    query_size: int
    best_beta_b1: float
    best_beta_b2: float
    beta_sweep_b1: dict[float, float]
    beta_sweep_b2: dict[float, float]
    path_a: PathMetrics
    path_b1: PathMetrics
    path_b2: PathMetrics
    decision: str
    decision_reason: str
    decision_threshold_pct: float = _DECISION_THRESHOLD_PCT


def _split(
    corpus: list[Incident],
    seed: int,
) -> tuple[list[Incident], list[Incident], list[Incident]]:
    """60% train / 20% tune (β sweep) / 20% query (final scoring)."""
    rng = random.Random(seed + 1)
    shuffled = corpus[:]
    rng.shuffle(shuffled)
    n = len(shuffled)
    n_train = int(round(n * 0.6))
    n_tune = int(round(n * 0.2))
    train = shuffled[:n_train]
    tune = shuffled[n_train:n_train + n_tune]
    query = shuffled[n_train + n_tune:]
    return train, tune, query


def run_spike(corpus: list[Incident], seed: int) -> SpikeResult:
    if len(corpus) < _MIN_CORPUS:
        raise SpikeInsufficientCorpus(
            f"corpus has {len(corpus)} incidents; need ≥{_MIN_CORPUS}"
        )

    train, tune, query = _split(corpus, seed)
    encoder = Encoder.fit(train)
    train_vecs = [encoder.encode(inc.text()) for inc in train]
    query_vecs = [encoder.encode(inc.text()) for inc in query]

    # β sweep on the tune set (no leakage into final scoring) — one sweep
    # per Hopfield variant since the productive β scale differs (one-step
    # tolerates very large β; iterated retrieval breaks down at high β
    # because the update collapses immediately to the argmax pattern).
    best_beta_b1, beta_table_b1 = tune_beta(
        train, tune, encoder,
        retrieve=lambda qv, pv, b: hopfield_topk(qv, pv, beta=b),
        sweep=_BETA_SWEEP,
    )
    best_beta_b2, beta_table_b2 = tune_beta(
        train, tune, encoder,
        retrieve=lambda qv, pv, b: hopfield_iterated_topk(qv, pv, beta=b),
        sweep=_ITER_BETA_SWEEP,
    )

    # Path A — pgvector cosine top-K.
    def retr_a(qv: list[float]) -> list[int]:
        return pgvector_topk(qv, train_vecs)

    overall_a, per_class_a = _recall_at_k(query, query_vecs, train, retr_a)
    p50_a, p95_a, medians_a, noise_a = _measure_latency(query, query_vecs, retr_a)
    metrics_a = PathMetrics(
        path="A_pgvector_cosine",
        overall_recall_at_5=overall_a,
        per_class=[
            PerClassMetric(cls, len(rates), statistics.mean(rates))
            for cls, rates in sorted(per_class_a.items())
        ],
        latency_p50_ms=p50_a,
        latency_p95_ms=p95_a,
        latency_median_repeats_ms=medians_a,
        timing_noise_flag=noise_a,
        # Cosine: N dot products of dim D, each D multiply-adds.
        compute_ops_per_query=len(train) * len(encoder.vocabulary) * 2,
        notes=[
            "pgvector cosine top-K modelled as brute-force; HNSW degenerate at N<200.",
            "embedding cost identical to Paths B1/B2 (one TF-IDF transform per query).",
        ],
    )

    # Path B1 — one-step Hopfield (order-equivalent to A by construction).
    def retr_b1(qv: list[float]) -> list[int]:
        return hopfield_topk(qv, train_vecs, beta=best_beta_b1)

    overall_b1, per_class_b1 = _recall_at_k(query, query_vecs, train, retr_b1)
    p50_b1, p95_b1, med_b1, noise_b1 = _measure_latency(query, query_vecs, retr_b1)
    metrics_b1 = PathMetrics(
        path="B1_hopfield_onestep",
        overall_recall_at_5=overall_b1,
        per_class=[
            PerClassMetric(cls, len(rates), statistics.mean(rates))
            for cls, rates in sorted(per_class_b1.items())
        ],
        latency_p50_ms=p50_b1,
        latency_p95_ms=p95_b1,
        latency_median_repeats_ms=med_b1,
        timing_noise_flag=noise_b1,
        compute_ops_per_query=len(train) * (len(encoder.vocabulary) * 2 + 1) + 1,
        notes=[
            f"β tuned on held-out set; locked β={best_beta_b1}.",
            "one-step retrieval; order-equivalent to Path A (softmax monotonic).",
        ],
    )

    # Path B2 — iterated Hopfield (the variant that *can* differ from A).
    def retr_b2(qv: list[float]) -> list[int]:
        return hopfield_iterated_topk(qv, train_vecs, beta=best_beta_b2)

    overall_b2, per_class_b2 = _recall_at_k(query, query_vecs, train, retr_b2)
    p50_b2, p95_b2, med_b2, noise_b2 = _measure_latency(query, query_vecs, retr_b2)
    metrics_b2 = PathMetrics(
        path="B2_hopfield_iterated",
        overall_recall_at_5=overall_b2,
        per_class=[
            PerClassMetric(cls, len(rates), statistics.mean(rates))
            for cls, rates in sorted(per_class_b2.items())
        ],
        latency_p50_ms=p50_b2,
        latency_p95_ms=p95_b2,
        latency_median_repeats_ms=med_b2,
        timing_noise_flag=noise_b2,
        compute_ops_per_query=(
            _ITER_STEPS * (len(train) * (len(encoder.vocabulary) * 2 + 1)
                           + len(train) * len(encoder.vocabulary))
            + len(train) * len(encoder.vocabulary) * 2
        ),
        notes=[
            f"β tuned on held-out set; locked β={best_beta_b2}, T={_ITER_STEPS}.",
            "iterated low-β retrieval (noisy-query reconstruction per Ramsauer §A.4).",
        ],
    )

    # Decision per AC #4: pick the BEST Hopfield variant and compare it
    # to A. If even the best B can't beat A by >30% with no latency
    # regression, REJECT.
    if overall_b2 >= overall_b1:
        best_b_recall, best_b_p95, best_b_path = overall_b2, p95_b2, "B2"
    else:
        best_b_recall, best_b_p95, best_b_path = overall_b1, p95_b1, "B1"
    decision, reason = _decide(
        overall_a, best_b_recall, p95_a, best_b_p95, best_b_path,
    )
    return SpikeResult(
        corpus_size=len(corpus),
        seed=seed,
        train_size=len(train),
        tune_size=len(tune),
        query_size=len(query),
        best_beta_b1=best_beta_b1,
        best_beta_b2=best_beta_b2,
        beta_sweep_b1=beta_table_b1,
        beta_sweep_b2=beta_table_b2,
        path_a=metrics_a,
        path_b1=metrics_b1,
        path_b2=metrics_b2,
        decision=decision,
        decision_reason=reason,
    )


def _decide(
    recall_a: float, recall_b: float,
    p95_a_ms: float, p95_b_ms: float,
    best_b_path: str,
) -> tuple[str, str]:
    """AC #4 binary decision.

    integrate iff:
      - recall_b - recall_a > 30% of recall_a (relative improvement), AND
      - p95_b <= p95_a (latency not worse)
    else reject.
    """
    if recall_a <= 0.0:
        rel_improve = float("inf") if recall_b > 0.0 else 0.0
    else:
        rel_improve = (recall_b - recall_a) / recall_a * 100.0
    if rel_improve > _DECISION_THRESHOLD_PCT and p95_b_ms <= p95_a_ms:
        return ("integrate", (
            f"best Hopfield variant ({best_b_path}) recall@5={recall_b:.3f} "
            f"vs A={recall_a:.3f} (+{rel_improve:.1f}%); p95 latency "
            f"{p95_b_ms:.3f}ms ≤ {p95_a_ms:.3f}ms; "
            "follow-up Sprint G ticket to be filed per AC #4."
        ))
    if p95_b_ms > p95_a_ms:
        return ("reject", (
            f"latency regressed: best Hopfield variant ({best_b_path}) p95 "
            f"{p95_b_ms:.3f}ms > A p95 {p95_a_ms:.3f}ms "
            f"(recall delta {rel_improve:+.1f}%)."
        ))
    return ("reject", (
        f"best Hopfield variant ({best_b_path}) recall@5 improvement "
        f"{rel_improve:+.1f}% ≤ threshold {_DECISION_THRESHOLD_PCT:.0f}%; "
        "latency parity is not enough to integrate."
    ))


# ── Report rendering (used by both CLI and tests) ──────────────────────


def render_text_histogram(
    metrics: PathMetrics, width: int = 30,
) -> str:
    """ASCII histogram of per-class recall@5. Used in the decision doc."""
    if not metrics.per_class:
        return "(no per-class data)"
    lines: list[str] = []
    max_len = max(len(m.failure_class) for m in metrics.per_class)
    for m in metrics.per_class:
        bar_len = int(round(m.recall_at_5 * width))
        bar = "█" * bar_len + "·" * (width - bar_len)
        lines.append(
            f"  {m.failure_class:<{max_len}} {bar} "
            f"{m.recall_at_5:.3f}  (n={m.n_queries})"
        )
    return "\n".join(lines)


def render_summary(result: SpikeResult) -> str:
    a = result.path_a
    b1 = result.path_b1
    b2 = result.path_b2
    return (
        f"[OP-910] corpus={result.corpus_size} "
        f"(train={result.train_size}/tune={result.tune_size}/"
        f"query={result.query_size}) β_b1={result.best_beta_b1} "
        f"β_b2={result.best_beta_b2} "
        f"recall@5 A={a.overall_recall_at_5:.3f} "
        f"B1={b1.overall_recall_at_5:.3f} "
        f"B2={b2.overall_recall_at_5:.3f} "
        f"p95_ms A={a.latency_p95_ms:.3f} "
        f"B1={b1.latency_p95_ms:.3f} B2={b2.latency_p95_ms:.3f} "
        f"→ {result.decision.upper()}: {result.decision_reason}"
    )


# ── Serialisation ──────────────────────────────────────────────────────


def result_to_dict(result: SpikeResult) -> dict:
    d = asdict(result)
    # beta sweep tables come back with float keys — JSON keys must be strings.
    d["beta_sweep_b1"] = {str(k): v for k, v in result.beta_sweep_b1.items()}
    d["beta_sweep_b2"] = {str(k): v for k, v in result.beta_sweep_b2.items()}
    return d


# ── CLI ────────────────────────────────────────────────────────────────


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="spike_f12_hopfield_hdc",
        description=(
            "OP-910 F12 spike: Modern Hopfield (log-sum-exp) vs pgvector "
            "cosine top-K, scored on runner_incidents failure-class corpus."
        ),
    )
    p.add_argument(
        "--output", default="data/op-910-comparison.json",
        help="JSON sidecar output path (default: %(default)s)",
    )
    p.add_argument(
        "--corpus", default=None,
        help="Optional JSONL file overriding the synthetic corpus. Each "
             "line must be {failure_class, summary, raw_traceback}.",
    )
    p.add_argument(
        "--corpus-size", type=int, default=60, dest="corpus_size",
        help="Synthetic corpus size when --corpus is not given (default: 60; "
             f"min: {_MIN_CORPUS}).",
    )
    p.add_argument(
        "--seed", type=int, default=910,
        help="RNG seed for the synthetic generator + split (default: 910).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _parse_cli(argv if argv is not None else sys.argv[1:])
    started = time.monotonic()
    corpus = load_corpus(args.corpus, args.corpus_size, args.seed)
    result = run_spike(corpus, args.seed)
    elapsed = time.monotonic() - started

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result_to_dict(result), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(render_summary(result), f"({elapsed:.2f}s) → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
