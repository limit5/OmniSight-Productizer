"""Phase 65 S2 — fine-tune hold-out evaluation.

Wraps the Phase 63-D `iq_benchmark` machinery for the post-finetune
gate: we run the candidate model AND the baseline model against the
same hold-out set, and refuse to promote the candidate unless its
weighted score is within `OMNISIGHT_FINETUNE_REGRESSION_PP` (default
5pp) of the baseline.

This module is pure: load → score → compare. It does NOT promote
anything itself (no DB writes); the Phase 65 S4 nightly orchestrator
files the Decision Engine `finetune/regression` proposal when this
returns `decision="reject"`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from backend import iq_benchmark as ib
from backend import iq_runner as ir

logger = logging.getLogger(__name__)

DEFAULT_REGRESSION_PP = 5.0
DEFAULT_HOLDOUT_NAME = "holdout-finetune-v1"


def _regression_pp() -> float:
    raw = (os.environ.get("OMNISIGHT_FINETUNE_REGRESSION_PP") or "5").strip()
    try:
        return max(0.0, min(50.0, float(raw)))
    except ValueError:
        return DEFAULT_REGRESSION_PP


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Result types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class HoldoutEvaluation:
    """Outcome of comparing baseline vs candidate on the hold-out."""
    baseline_model: str
    candidate_model: str
    benchmark_name: str
    baseline_score: float    # 0..1 weighted
    candidate_score: float
    delta_pp: float          # candidate - baseline, in percentage points
    threshold_pp: float
    decision: str            # "promote" | "reject" | "no_baseline"
    reason: str

    @property
    def ok(self) -> bool:
        return self.decision == "promote"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hold-out lookup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_holdout(name: str = DEFAULT_HOLDOUT_NAME) -> Optional[ib.IQBenchmark]:
    """Find the hold-out benchmark by `name` from the standard
    benchmark dir. Returns None if not present (caller decides whether
    that's a hard failure)."""
    for b in ib.load_all():
        if b.name == name:
            return b
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Compare
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def compare_models(
    baseline_model: str,
    candidate_model: str,
    *,
    ask_fn: ir.AskFn,
    benchmark: Optional[ib.IQBenchmark] = None,
    regression_pp: Optional[float] = None,
    token_budget_per_model: int = 50_000,
) -> HoldoutEvaluation:
    """Run BOTH models on the hold-out, return promote-or-reject.

    Empty / missing baseline_model is treated as "no_baseline" — caller
    must decide the policy (typically reject the promotion since we
    have no anchor)."""
    bench = benchmark or load_holdout()
    if bench is None:
        return HoldoutEvaluation(
            baseline_model=baseline_model, candidate_model=candidate_model,
            benchmark_name=DEFAULT_HOLDOUT_NAME,
            baseline_score=0.0, candidate_score=0.0, delta_pp=0.0,
            threshold_pp=regression_pp or _regression_pp(),
            decision="reject",
            reason=f"hold-out benchmark '{DEFAULT_HOLDOUT_NAME}' not found",
        )

    pp = regression_pp if regression_pp is not None else _regression_pp()

    if not baseline_model or not baseline_model.strip():
        # No baseline → cannot compare; caller should treat as reject.
        cand_report = await ir.run_benchmark(
            bench, candidate_model, ask_fn=ask_fn,
            token_budget=token_budget_per_model,
        )
        return HoldoutEvaluation(
            baseline_model=baseline_model, candidate_model=candidate_model,
            benchmark_name=bench.name,
            baseline_score=0.0,
            candidate_score=cand_report.score.weighted_score,
            delta_pp=0.0, threshold_pp=pp,
            decision="no_baseline",
            reason="no baseline_model supplied — cannot judge regression",
        )

    base_report = await ir.run_benchmark(
        bench, baseline_model, ask_fn=ask_fn,
        token_budget=token_budget_per_model,
    )
    cand_report = await ir.run_benchmark(
        bench, candidate_model, ask_fn=ask_fn,
        token_budget=token_budget_per_model,
    )

    delta_pp = (cand_report.score.weighted_score
                - base_report.score.weighted_score) * 100
    cutoff = -pp
    decision = "promote" if delta_pp >= cutoff else "reject"
    reason = (
        f"candidate {cand_report.score.weighted_score:.2%} "
        f"vs baseline {base_report.score.weighted_score:.2%} "
        f"(Δ={delta_pp:+.1f}pp; threshold ≥ {cutoff:.1f}pp)"
    )

    try:
        from backend import metrics as _m
        _m.finetune_eval_score.labels(model=candidate_model).set(
            cand_report.score.weighted_score,
        )
        _m.finetune_eval_score.labels(model=baseline_model).set(
            base_report.score.weighted_score,
        )
    except Exception:
        pass

    logger.info(
        "[finetune-eval] %s → %s : %s (%s)",
        baseline_model, candidate_model, decision, reason,
    )
    return HoldoutEvaluation(
        baseline_model=baseline_model,
        candidate_model=candidate_model,
        benchmark_name=bench.name,
        baseline_score=base_report.score.weighted_score,
        candidate_score=cand_report.score.weighted_score,
        delta_pp=delta_pp,
        threshold_pp=pp,
        decision=decision,
        reason=reason,
    )
