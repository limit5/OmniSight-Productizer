"""Phase 63-D — Daily IQ Benchmark schema + loader + scorer.

The IIS signal layer (Phase 63-A) tells us "is the *current* model
drifting?". This module answers the complementary "is the *baseline*
model still as smart as it was last week?" — by replaying a curated
fixed-question set against the live model on a nightly cadence
(scheduled in 63-D D3) and watching the aggregate pass rate.

Design constraints (from HANDOFF / design doc):

  * Questions are HAND-CURATED in `configs/iq_benchmark/*.yaml` —
    NOT auto-generated from `episodic_memory` (avoids the
    self-reference bias that would let a degrading model also
    pick easier-for-itself questions).
  * Scoring is DETERMINISTIC — keyword + regex match — so the
    benchmark itself doesn't need an LLM judge (which would itself
    drift). The trade-off is shallower semantic checking; v1
    accepts that.
  * Token budget cap per benchmark run; a runaway scoring loop
    must not blow the daily budget (D3 enforces).

This module is pure: load → score → return BenchmarkScore. The
nightly loop and Notification/metrics emission live in D3.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = _PROJECT_ROOT / "configs" / "iq_benchmark"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class IQQuestion:
    """A single benchmark item.

    Scoring: an answer passes if it satisfies AT LEAST ONE non-empty
    matcher. Keyword matchers do CASE-INSENSITIVE substring search;
    every keyword in `expected_keywords` must appear (AND-of-keywords).
    Regex matchers run as written.
    """
    id: str
    prompt: str
    expected_keywords: list[str] = field(default_factory=list)
    expected_regex: Optional[str] = None
    forbidden_keywords: list[str] = field(default_factory=list)
    weight: float = 1.0
    tags: list[str] = field(default_factory=list)

    def matches(self, answer: str) -> bool:
        """Return True iff `answer` satisfies the matcher contract.
        Empty `answer` always fails (a model that returns nothing
        cannot be passing)."""
        if not answer or not answer.strip():
            return False
        a_lower = answer.lower()

        # Forbidden keywords always disqualify (e.g. "I don't know" /
        # the literal placeholder used by some failure modes).
        for fk in self.forbidden_keywords:
            if fk and fk.lower() in a_lower:
                return False

        # Question must have at least one positive matcher; otherwise
        # it's malformed and we conservatively fail it.
        has_positive = bool(self.expected_keywords) or bool(self.expected_regex)
        if not has_positive:
            return False

        if self.expected_keywords:
            if not all(kw.lower() in a_lower for kw in self.expected_keywords):
                return False

        if self.expected_regex:
            try:
                if not re.search(self.expected_regex, answer, re.IGNORECASE | re.DOTALL):
                    return False
            except re.error as exc:
                logger.warning("bad regex on %s: %s", self.id, exc)
                return False

        return True


@dataclass
class IQBenchmark:
    """A bundle of questions loaded from one YAML file."""
    name: str
    schema_version: int
    description: str
    questions: list[IQQuestion]

    def total_weight(self) -> float:
        return sum(q.weight for q in self.questions) or 1.0


@dataclass
class BenchmarkScore:
    """Result of running one model against one benchmark."""
    benchmark: str
    model: str
    pass_count: int
    total_count: int
    weighted_score: float       # 0..1, sum(weight*pass) / total_weight
    per_question: list[tuple[str, bool]]  # (question_id, passed)

    @property
    def pass_rate(self) -> float:
        return 0.0 if self.total_count == 0 else self.pass_count / self.total_count


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Loader
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SUPPORTED_VERSION = 1


def _coerce_question(raw: dict) -> IQQuestion:
    return IQQuestion(
        id=str(raw["id"]),
        prompt=str(raw["prompt"]),
        expected_keywords=list(raw.get("expected_keywords") or []),
        expected_regex=raw.get("expected_regex"),
        forbidden_keywords=list(raw.get("forbidden_keywords") or []),
        weight=float(raw.get("weight", 1.0)),
        tags=list(raw.get("tags") or []),
    )


def load_benchmark(path: Path) -> IQBenchmark:
    """Load + validate one YAML benchmark file. Raises on bad shape so
    the nightly cron fails LOUDLY on a curated-set typo (the curator
    intended each question to be runnable)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    schema_version = int(raw.get("schema_version") or 0)
    if schema_version != SUPPORTED_VERSION:
        raise ValueError(
            f"{path.name}: schema_version {schema_version} unsupported "
            f"(expected {SUPPORTED_VERSION})"
        )
    questions_raw = raw.get("questions") or []
    if not questions_raw:
        raise ValueError(f"{path.name}: no questions defined")
    questions = [_coerce_question(q) for q in questions_raw]
    # Detect duplicate ids — curator typo.
    ids = [q.id for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path.name}: duplicate question id(s) in set")
    return IQBenchmark(
        name=str(raw.get("name") or path.stem),
        schema_version=schema_version,
        description=str(raw.get("description") or ""),
        questions=questions,
    )


def load_all(directory: Path | None = None) -> list[IQBenchmark]:
    """Load every `*.yaml` benchmark in `directory` (default
    `configs/iq_benchmark/`). Sorted by name for deterministic
    nightly run order."""
    d = directory or BENCHMARK_DIR
    if not d.exists():
        return []
    out: list[IQBenchmark] = []
    for p in sorted(d.glob("*.yaml")):
        try:
            out.append(load_benchmark(p))
        except Exception as exc:
            logger.error("benchmark %s skipped: %s", p.name, exc)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scoring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def score_answers(benchmark: IQBenchmark, model: str,
                  answers: dict[str, str]) -> BenchmarkScore:
    """Score a model's answers against a benchmark.

    `answers` is `{question_id: response_text}`. Missing ids are
    treated as failed (a model that didn't answer cannot pass).
    """
    per_q: list[tuple[str, bool]] = []
    weighted_total = 0.0
    pass_count = 0
    for q in benchmark.questions:
        ans = answers.get(q.id, "")
        passed = q.matches(ans)
        per_q.append((q.id, passed))
        if passed:
            pass_count += 1
            weighted_total += q.weight

    return BenchmarkScore(
        benchmark=benchmark.name,
        model=model,
        pass_count=pass_count,
        total_count=len(benchmark.questions),
        weighted_score=weighted_total / benchmark.total_weight(),
        per_question=per_q,
    )


def aggregate_scores(scores: Iterable[BenchmarkScore]) -> dict[str, float]:
    """Average weighted_score per model across multiple benchmark sets."""
    by_model: dict[str, list[float]] = {}
    for s in scores:
        by_model.setdefault(s.model, []).append(s.weighted_score)
    return {m: sum(vs) / len(vs) for m, vs in by_model.items() if vs}
