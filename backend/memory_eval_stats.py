"""Pure paired-eval statistics core (U4-F-math, OP-2574).

The promotion gate's teeth, done honestly: an EXACT two-sided McNemar
test on the discordant pairs (stdlib ``math.comb`` — +3pp at N=10-20 is
below noise, codex B1; small N is the whole point, so no chi-square or
normal approximations), a majority-vote collapse for clustered
N-sample/k-repeat runs (treating repeats as independent is
pseudoreplication, codex E7), and the FROZEN decision table with
``insufficient_evidence`` as a first-class terminal. NO scalar +pp
thresholds exist anywhere in this module, and none may be added.

PURE: stdlib ``math``/``dataclasses`` only — no I/O, no DB, no clock,
no network. The executor half (LLM client, suite loading, cost control,
and the ``infra_invalid`` terminal for infrastructure failures, codex
E8) is a SEPARATE ticket (U4-F-exec); the eval ledger rows (migration
0258) are written by that executor, never here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PairedCase:
    """One eval case run against BOTH sides of the same paired suite."""

    case_id: str
    baseline_pass: bool
    candidate_pass: bool


@dataclass(frozen=True)
class PairedSummary:
    """Counts over collapsed (one-row-per-case) paired results."""

    n: int
    improvements: int
    regressions: int
    net_flips: int
    mcnemar_p: float


def mcnemar_exact_p(improvements: int, regressions: int) -> float:
    """EXACT two-sided McNemar (binomial) p on the discordant pairs.

    PINNED formula: two-sided exact binomial with theta=0.5 — the
    doubled smaller tail, capped at 1.0 (the standard exact McNemar;
    NOT hypergeometric). Do not substitute approximations.
    """
    if improvements < 0 or regressions < 0:
        raise ValueError(
            "mcnemar_exact_p: counts must be non-negative, got "
            f"improvements={improvements}, regressions={regressions}"
        )
    n = improvements + regressions
    if n == 0:
        return 1.0
    k = min(improvements, regressions)
    return min(
        1.0,
        2.0 * sum(math.comb(n, i) for i in range(0, k + 1)) * (0.5 ** n),
    )


def collapse_clustered(samples: list[PairedCase]) -> list[PairedCase]:
    """Collapse k-repeat samples to one row per case_id.

    Majority vote on each side INDEPENDENTLY; TIES -> False
    (conservative: a tie is not a pass). Output ordered by case_id
    ascending — input order is irrelevant.
    """
    grouped: dict[str, list[PairedCase]] = {}
    for sample in samples:
        grouped.setdefault(sample.case_id, []).append(sample)
    collapsed: list[PairedCase] = []
    for case_id in sorted(grouped):
        group = grouped[case_id]
        total = len(group)
        baseline_true = sum(1 for s in group if s.baseline_pass)
        candidate_true = sum(1 for s in group if s.candidate_pass)
        collapsed.append(
            PairedCase(
                case_id=case_id,
                baseline_pass=baseline_true * 2 > total,
                candidate_pass=candidate_true * 2 > total,
            )
        )
    return collapsed


def summarize(cases: list[PairedCase]) -> PairedSummary:
    """Counts + exact McNemar p over one-row-per-case paired results.

    Duplicate case_ids -> ValueError: ``collapse_clustered`` must run
    first. Fail loud, never silently double-count.
    """
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise ValueError(
                f"summarize: duplicate case_id {case.case_id!r} — "
                "run collapse_clustered first"
            )
        seen.add(case.case_id)
    improvements = sum(
        1 for c in cases if not c.baseline_pass and c.candidate_pass
    )
    regressions = sum(
        1 for c in cases if c.baseline_pass and not c.candidate_pass
    )
    return PairedSummary(
        n=len(cases),
        improvements=improvements,
        regressions=regressions,
        net_flips=improvements - regressions,
        mcnemar_p=mcnemar_exact_p(improvements, regressions),
    )


def decide(
    summary: PairedSummary,
    *,
    alpha: float = 0.05,
    min_cases: int = 10,
    min_discordant: int = 3,
) -> str:
    """The FROZEN decision table: promote|reject|insufficient_evidence.

    min_discordant mirrors the graduation MIN_SAMPLES=3 precedent
    (backend/agents/learning_loop.py:112). ``infra_invalid`` is NOT
    produced here — it is the executor's terminal.
    """
    if summary.n < min_cases:
        return "insufficient_evidence"
    if (summary.improvements + summary.regressions) < min_discordant:
        return "insufficient_evidence"
    if summary.mcnemar_p < alpha and summary.net_flips > 0:
        return "promote"
    if summary.mcnemar_p < alpha and summary.net_flips < 0:
        return "reject"
    return "insufficient_evidence"


_STAT_SUMMARY_DECISIONS = frozenset(
    {"promote", "reject", "insufficient_evidence", "infra_invalid"}
)


def build_stat_summary(summary: PairedSummary, decision: str) -> dict:
    """EXACTLY the four approval-card keys — this shape is frozen.

    decision must be a ``decide`` output or ``infra_invalid`` (the
    executor may pass it through); anything else -> ValueError.
    """
    if decision not in _STAT_SUMMARY_DECISIONS:
        raise ValueError(
            f"build_stat_summary: unknown decision {decision!r} — "
            f"allowed: {sorted(_STAT_SUMMARY_DECISIONS)}"
        )
    return {
        "decision": decision,
        "mcnemar_p": summary.mcnemar_p,
        "net_flips": summary.net_flips,
        "n": summary.n,
    }


def build_stat_detail(summary: PairedSummary) -> dict:
    """Richer detail than the frozen 4-key card shape allows."""
    return {
        "improvements": summary.improvements,
        "regressions": summary.regressions,
    }
