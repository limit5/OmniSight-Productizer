"""OP-843 spike — synthetic comparison of B3's 3x-reset path vs Outcomes-graded path.

Pure-mock harness. Does NOT call Anthropic API. Models both paths as
deterministic state machines so the comparison isolates *recovery semantics*
from *token costs / network noise / model nondeterminism*. The token + latency
numbers reported are derived from publicly documented Anthropic billing
(Sonnet 4.6 / Haiku 4.5 input+output per-MTok rates 2026-05) applied to
modelled token counts per scenario.

What we are comparing:

* **Path A (B3 current)**: After every tool call, append
  ``(tool_name, args_hash, error_class)``. Triple-match → reset (clear
  conversation; restart with system+first-user only + 1-paragraph failure
  summary). Hard cap 3 resets ≤ 9 raw attempts. See
  ``backend/agents/loop_detector.py`` for the implementation this mock
  mirrors.
* **Path B (Outcomes-graded)**: Single SDK call carries a rubric. After the
  model claims completion, an independent grader-model checks the rubric.
  Grader-fail → re-attempt inside the same conceptual session (Outcomes
  primitive). Hard cap 3 outcomes-attempts (matches B3's budget).
* **Path C (hybrid)**: 2× B3 hard-reset + 1× Outcomes-graded attempt. The
  hypothesis Sprint B's spike was filed to test.

Scenario library (each is a deterministic failure-pattern model):

1. **happy_path** — model succeeds on attempt 1
2. **succeed_without_fixing** — model claims success but the rubric would
   fail. This is the FAILURE MODE Outcomes is supposed to catch sooner than
   B3's 3x-detector.
3. **infinite_loop** — model emits same tool call 3+ times (the FAILURE MODE
   B3's detector is purpose-built for).
4. **partial_progress** — model improves with each attempt but needs 2-3
   tries; both paths recover.
5. **structural_max_iterations** — model legitimately hits max_iterations
   (W14.5 lesson); non-retryable. Both paths must surrender, not retry.

Output: a JSONL row per (scenario, path, attempt) so the report can plot
attempt-count + cost + latency curves.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
from pathlib import Path
from typing import Callable


# 2026-05 published rates per million tokens (Anthropic claude.com pricing).
_SONNET_INPUT_USD_PER_MTOK = 3.00
_SONNET_OUTPUT_USD_PER_MTOK = 15.00
_HAIKU_INPUT_USD_PER_MTOK = 0.80
_HAIKU_OUTPUT_USD_PER_MTOK = 4.00


@dataclasses.dataclass(frozen=True)
class AttemptOutcome:
    """One attempt's modelled cost. ``passed`` is the ground-truth verdict
    after grading; ``model_claimed_done`` is the model's self-report."""

    scenario: str
    path: str
    attempt_index: int
    input_tokens: int
    output_tokens: int
    grader_input_tokens: int  # 0 for Path A
    grader_output_tokens: int  # 0 for Path A
    latency_s: float
    model_claimed_done: bool
    passed: bool

    def cost_usd(self) -> float:
        coder = (
            (self.input_tokens * _SONNET_INPUT_USD_PER_MTOK) / 1_000_000
            + (self.output_tokens * _SONNET_OUTPUT_USD_PER_MTOK) / 1_000_000
        )
        grader = (
            (self.grader_input_tokens * _HAIKU_INPUT_USD_PER_MTOK) / 1_000_000
            + (self.grader_output_tokens * _HAIKU_OUTPUT_USD_PER_MTOK) / 1_000_000
        )
        return coder + grader


@dataclasses.dataclass
class PathSummary:
    """Aggregate per (scenario, path)."""

    scenario: str
    path: str
    attempts: int
    succeeded: bool
    total_cost_usd: float
    total_latency_s: float
    notes: str


# ─── Scenario models ─────────────────────────────────────────────────


def _attempt_b3(scenario: str, attempt_index: int) -> AttemptOutcome:
    """Path A — B3 current. Each attempt = full Sonnet 4.6 call with ~50k
    input tokens (system prompt + AC + locator output + tool history) and
    ~5k output tokens (tool calls + final commit message). Each reset
    discards the conversation and reincurs the prompt-prefix cost.

    Latency ~120s/attempt at typical 40-iteration cap.
    """
    # Default token shape per attempt
    in_t, out_t, lat = 50_000, 5_000, 120.0

    # Scenario-specific success / claim semantics
    if scenario == "happy_path":
        claimed, passed = (attempt_index == 0), (attempt_index == 0)
    elif scenario == "succeed_without_fixing":
        # The whole point: B3 has no grader, so model's claim IS accepted.
        # Outer pipeline (Critic B4, lint B2, tests) would have caught it,
        # but the spike isolates the loop-detector layer. From B3's POV
        # the model claims done on attempt 0 and B3 returns "success".
        claimed, passed = True, False
    elif scenario == "infinite_loop":
        # Model emits the SAME tool-call shape every attempt → B3 detector
        # triggers reset; after 3 resets, terminal abort.
        claimed, passed = False, False
    elif scenario == "partial_progress":
        # Recovers on attempt 2 (index 1).
        claimed, passed = (attempt_index >= 1), (attempt_index >= 1)
    elif scenario == "structural_max_iterations":
        # Non-retryable per W14.5 lesson. Each attempt hits the cap.
        claimed, passed = False, False
        out_t = 40 * 1_000  # 40 iterations worth of tool-call tokens
    else:
        raise ValueError(scenario)

    return AttemptOutcome(
        scenario=scenario, path="A_b3", attempt_index=attempt_index,
        input_tokens=in_t, output_tokens=out_t,
        grader_input_tokens=0, grader_output_tokens=0,
        latency_s=lat, model_claimed_done=claimed, passed=passed,
    )


def _attempt_outcomes(scenario: str, attempt_index: int) -> AttemptOutcome:
    """Path B — Outcomes-graded. Single Sonnet call with rubric (slightly
    larger input ~52k for rubric prose), grader-Haiku call after each claim
    (~5k input including diff snippet, ~500 output verdict+reason).

    The grader fires only when the model claims done; non-claims pass
    through without grader cost. Re-attempts inside Outcomes don't reincur
    the full prompt prefix (rubric + AC are cached) — model gets a short
    "your prior attempt failed because <grader-reason>; try again" turn.
    """
    in_t = 52_000 if attempt_index == 0 else 8_000  # cached subsequent
    out_t = 5_000
    lat = 125.0 if attempt_index == 0 else 60.0  # cached attempts faster
    grader_in, grader_out = 0, 0

    if scenario == "happy_path":
        claimed = (attempt_index == 0)
        if claimed:
            grader_in, grader_out = 5_000, 500
            passed = True  # grader agrees
        else:
            passed = False
    elif scenario == "succeed_without_fixing":
        # Model claims done every attempt; grader disagrees every attempt.
        # Outcomes loops up to its cap. THIS IS THE WIN: B3 would have
        # accepted attempt 0; Outcomes catches it AND retries.
        claimed = True
        grader_in, grader_out = 5_000, 500
        passed = False
    elif scenario == "infinite_loop":
        # Model emits same tool calls; never claims done. Outcomes has no
        # tool-shape detector — it just runs until max_iterations.
        claimed, passed = False, False
        # Each attempt burns full iteration budget.
        out_t = 40 * 1_000
        # No grader call (claimed=False).
    elif scenario == "partial_progress":
        claimed = (attempt_index >= 1)
        if claimed:
            grader_in, grader_out = 5_000, 500
            passed = True
        else:
            passed = False
    elif scenario == "structural_max_iterations":
        claimed, passed = False, False
        out_t = 40 * 1_000
    else:
        raise ValueError(scenario)

    return AttemptOutcome(
        scenario=scenario, path="B_outcomes", attempt_index=attempt_index,
        input_tokens=in_t, output_tokens=out_t,
        grader_input_tokens=grader_in, grader_output_tokens=grader_out,
        latency_s=lat, model_claimed_done=claimed, passed=passed,
    )


def _attempt_hybrid(scenario: str, attempt_index: int) -> AttemptOutcome:
    """Path C — 2× B3 hard-reset + 1× Outcomes-graded (last attempt).

    For attempts 0 and 1: same shape as Path A.
    For attempt 2: Outcomes path (rubric + grader).
    """
    if attempt_index < 2:
        a = _attempt_b3(scenario, attempt_index)
        return dataclasses.replace(a, path="C_hybrid")
    a = _attempt_outcomes(scenario, attempt_index)
    return dataclasses.replace(a, path="C_hybrid")


# ─── Path drivers ────────────────────────────────────────────────────


def _drive_path(
    scenario: str, path_name: str, fn: Callable[[str, int], AttemptOutcome], cap: int = 3,
) -> tuple[list[AttemptOutcome], PathSummary]:
    """Run up to ``cap`` attempts. Stop when the path declares success."""
    outcomes: list[AttemptOutcome] = []
    notes: list[str] = []
    succeeded = False

    for attempt_index in range(cap):
        a = fn(scenario, attempt_index)
        outcomes.append(a)

        # Stop semantics differ by path:
        if path_name == "A_b3":
            # B3 accepts ``model_claimed_done`` at face value (no grader).
            # The 3x detector only fires on repeated tool-call shape; in
            # this synthetic harness we model the detector as "after 3
            # attempts with no claim, give up". Loop-detector reset would
            # fire on infinite_loop / structural_max_iterations.
            if a.model_claimed_done:
                succeeded = a.passed
                notes.append(
                    "b3_accepted_claim_without_grading"
                    if a.model_claimed_done and not a.passed
                    else "b3_succeeded"
                )
                break
        else:
            # Outcomes / hybrid: success requires grader-confirmed pass.
            if a.passed:
                succeeded = True
                notes.append(f"{path_name}_grader_confirmed_pass")
                break
            if a.model_claimed_done:
                # Grader rejected; loop continues unless cap reached.
                notes.append(f"{path_name}_grader_rejected_attempt_{attempt_index}")
    if not succeeded and not notes:
        notes.append(f"{path_name}_cap_exceeded")

    return outcomes, PathSummary(
        scenario=scenario, path=path_name, attempts=len(outcomes),
        succeeded=succeeded,
        total_cost_usd=sum(a.cost_usd() for a in outcomes),
        total_latency_s=sum(a.latency_s for a in outcomes),
        notes="; ".join(notes),
    )


SCENARIOS = [
    "happy_path",
    "succeed_without_fixing",
    "infinite_loop",
    "partial_progress",
    "structural_max_iterations",
]


def run_comparison() -> tuple[list[AttemptOutcome], list[PathSummary]]:
    all_attempts: list[AttemptOutcome] = []
    all_summaries: list[PathSummary] = []
    for scenario in SCENARIOS:
        for path, fn in [
            ("A_b3", _attempt_b3),
            ("B_outcomes", _attempt_outcomes),
            ("C_hybrid", _attempt_hybrid),
        ]:
            attempts, summary = _drive_path(scenario, path, fn)
            all_attempts.extend(attempts)
            all_summaries.append(summary)
    return all_attempts, all_summaries


# ─── Reporting ───────────────────────────────────────────────────────


def _format_summary_table(summaries: list[PathSummary]) -> str:
    rows = []
    rows.append("| Scenario | Path | Attempts | Succeeded | Cost (USD) | Latency (s) | Notes |")
    rows.append("|---|---|---:|:-:|---:|---:|---|")
    for s in summaries:
        rows.append(
            f"| {s.scenario} | {s.path} | {s.attempts} | "
            f"{'✓' if s.succeeded else '✗'} | "
            f"${s.total_cost_usd:.4f} | {s.total_latency_s:.0f} | {s.notes} |"
        )
    return "\n".join(rows)


def _aggregate_by_path(summaries: list[PathSummary]) -> dict[str, dict]:
    agg: dict[str, dict] = {}
    for path in {s.path for s in summaries}:
        rows = [s for s in summaries if s.path == path]
        agg[path] = {
            "total_cost_usd": round(sum(s.total_cost_usd for s in rows), 4),
            "total_latency_s": round(sum(s.total_latency_s for s in rows), 0),
            "success_rate": f"{sum(s.succeeded for s in rows)}/{len(rows)}",
            "mean_cost_per_scenario": round(
                statistics.mean(s.total_cost_usd for s in rows), 4
            ),
        }
    return agg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jsonl-out", type=Path, default=None,
        help="Write per-attempt JSONL to this path (else stdout)",
    )
    parser.add_argument(
        "--markdown-out", type=Path, default=None,
        help="Write Markdown summary table to this path (else stdout)",
    )
    args = parser.parse_args()

    attempts, summaries = run_comparison()

    # JSONL output
    jsonl_lines = "\n".join(
        json.dumps(dataclasses.asdict(a) | {"cost_usd": a.cost_usd()})
        for a in attempts
    )
    if args.jsonl_out:
        args.jsonl_out.write_text(jsonl_lines + "\n", encoding="utf-8")
    else:
        print(jsonl_lines, file=sys.stderr)

    # Markdown summary
    table = _format_summary_table(summaries)
    agg = _aggregate_by_path(summaries)
    agg_block = "\n".join(
        f"- **{path}** — cost=${a['total_cost_usd']:.4f}, "
        f"latency={a['total_latency_s']:.0f}s, success={a['success_rate']}, "
        f"mean_per_scenario=${a['mean_cost_per_scenario']:.4f}"
        for path, a in sorted(agg.items())
    )
    md = (
        "## Per-scenario summary\n\n" + table + "\n\n"
        "## Aggregate by path\n\n" + agg_block + "\n"
    )
    if args.markdown_out:
        args.markdown_out.write_text(md, encoding="utf-8")
    else:
        print(md)

    # Pivotal assertion: outcomes catches succeed_without_fixing,
    # b3 does not (the key win condition for Outcomes adoption).
    swf = [s for s in summaries if s.scenario == "succeed_without_fixing"]
    b3_swf = next(s for s in swf if s.path == "A_b3")
    out_swf = next(s for s in swf if s.path == "B_outcomes")
    if b3_swf.succeeded and not out_swf.succeeded:
        # B3 declared success on a non-passing claim; Outcomes correctly
        # refused. This is the headline finding.
        print(
            "[spike] HEADLINE: B3 accepted 'succeed_without_fixing' claim "
            "(false-positive success); Outcomes correctly refused.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
