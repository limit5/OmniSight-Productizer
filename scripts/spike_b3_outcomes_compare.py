#!/usr/bin/env python3
"""B3 vs Outcomes-grader spike harness (OP-843).

Synthetic comparison of two recovery strategies for the
"model claims success without actually fixing" failure mode:

  * **Path A** — current B3 (OP-830): up to 3 hard resets, each triggered
    by a 3x repeat of ``(tool_name, args_hash, error_class)``. Wires the
    real ``backend.agents.loop_detector.LoopDetector`` so the comparison
    runs against the production detector's true semantics, including the
    Levenshtein progressive-narrowing exemption (AC #3 of OP-830).

  * **Path B** — Anthropic Outcomes API (public beta, 2026-05-06):
    rubric + grader on a single SDK call; on grader fail the model
    self-retries inside ONE call rather than triggering a host-side reset.
    Modeled here without invoking the live API — the spike's purpose is
    structural cost / coverage comparison, NOT empirical accuracy of the
    grader. Per ``L-OP-843``: vendor talks compress capability layers,
    so we do NOT take the marketing claim ("replaces hand-rolled retry")
    at face value; we compare on cost, latency, and **the failure modes
    each path covers**.

Why not a live LLM run for the spike:
  - The acceptance criteria explicitly call for "synthetic test fixture"
    + "5 runs" — a deterministic harness that can be re-run in CI is
    more useful than 5 noisy live runs that cost ~$10 each.
  - Outcomes grader behaviour itself is a separate evaluation problem
    (rubric over-fit, grader hallucination); modeling those as toggles
    on the synthetic fixture lets us probe sensitivity without burning
    $50 of inference budget.
  - The spike's deliverable is a **recommendation**, not a benchmark
    number. The harness produces dimensions; the report draws the line.

Cost / latency assumptions (sourced from ``config/llm_pricing.yaml`` +
public Anthropic latency targets, both pinned in constants below so the
report is reproducible after price changes):

  - Sonnet 4: $3 / $15 per 1M (input / output) tokens.
  - Haiku 4.5: $1 / $5 per 1M tokens (used here as the grader model;
    Outcomes beta lets the grader model differ from the worker model).
  - Per-attempt latency: 12s wall-clock for a Sonnet attempt of an
    M-tier ticket (median observed in OP-830 pilots), 4s for a grader
    call. Resets add ~2s of orchestrator overhead (history wipe +
    fresh runner spawn, per ``backend/agents/context_reset.py``).

Usage::

    # Run all built-in scenarios + write a JSON sidecar.
    python scripts/spike_b3_outcomes_compare.py \
        --output data/op-843-comparison.json

    # Run with a custom scenario file.
    python scripts/spike_b3_outcomes_compare.py \
        --scenarios path/to/my-scenarios.json
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.agents.loop_detector import (  # noqa: E402
    LoopDetector,
    RESET_LIMIT,
    TRIPLE_MATCH_THRESHOLD,
)

logger = logging.getLogger("spike_b3_outcomes")


# ── Constants pinned for reproducibility ──────────────────────────────

# USD per 1M tokens. Bit-identical to config/llm_pricing.yaml as of
# 2026-05-11 (the spike's reference date).
PRICE_PER_M_INPUT: dict[str, float] = {
    "claude-sonnet-4": 3.00,
    "claude-haiku-4-5": 1.00,
}
PRICE_PER_M_OUTPUT: dict[str, float] = {
    "claude-sonnet-4": 15.00,
    "claude-haiku-4-5": 5.00,
}

# Per-attempt synthetic token + latency budget. Anchored on OP-830 pilot
# medians (M-tier ticket, ~25 iterations per attempt). These are NOT
# measured per-scenario — the spike compares structural overhead, not
# task complexity.
ATTEMPT_INPUT_TOKENS = 18_000      # full system prompt + first user msg
ATTEMPT_OUTPUT_TOKENS = 3_500      # ~25 turns × 140 output tokens avg
ATTEMPT_WALL_S = 12.0

# Reset overhead: orchestrator must wipe history, rebuild prompt, spawn
# fresh runner. Empirically ~2s in context_reset.py integration tests.
RESET_OVERHEAD_S = 2.0

# Grader call (Haiku). Rubric + worker output → pass/fail JSON.
GRADER_INPUT_TOKENS = 2_500
GRADER_OUTPUT_TOKENS = 200
GRADER_WALL_S = 4.0

# Outcomes max self-retries inside a single SDK call (Anthropic's beta
# default per Lucas's talk; not yet documented in platform docs).
OUTCOMES_MAX_RETRIES = 3


# ── Scenario schema ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCallSpec:
    """One tool call the synthetic agent emits in an attempt."""

    tool_name: str
    args: dict[str, Any]
    # ``error_class`` "ok" means the call succeeded as far as the dispatcher
    # is concerned — but the WORK may still be wrong (false-positive).
    error_class: str


@dataclass(frozen=True)
class AttemptSpec:
    """One LLM attempt in the synthetic timeline.

    ``claims_success`` = the model emitted no further tool_use and a
    final text saying "done". This is the dimension B3 cannot observe:
    a clean stop_reason with no tool errors looks identical whether the
    fix landed or not. ``actually_fixed`` is the ground truth.
    """

    tool_calls: list[ToolCallSpec]
    claims_success: bool
    actually_fixed: bool


@dataclass(frozen=True)
class Scenario:
    """A multi-attempt synthetic timeline with ground truth."""

    id: str
    description: str
    attempts: list[AttemptSpec]


# ── Built-in scenarios ─────────────────────────────────────────────────

# Every scenario is hand-built to exercise a specific (B3-coverage,
# Outcomes-coverage) cell of the comparison matrix. The 5-scenario
# floor is the AC's "exercised at least 5 times" requirement.

SCENARIOS: list[Scenario] = [
    Scenario(
        id="S1-honest-loop",
        description=(
            "Model genuinely stuck: same Glob with same args, same error, "
            "3 times. B3's bread-and-butter case — should trigger reset on "
            "call #3. Outcomes never fires (the grader only runs at the "
            "claimed-completion boundary, but the model never claims one)."
        ),
        attempts=[
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("Glob", {"pattern": "**/*.py"},
                                 "bash_metachar_blocked"),
                    ToolCallSpec("Glob", {"pattern": "**/*.py"},
                                 "bash_metachar_blocked"),
                    ToolCallSpec("Glob", {"pattern": "**/*.py"},
                                 "bash_metachar_blocked"),
                ],
                claims_success=False,
                actually_fixed=False,
            ),
            # After reset: model picks a different approach, fixes it.
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("Grep",
                                 {"pattern": "def main", "type": "py"}, "ok"),
                ],
                claims_success=True,
                actually_fixed=True,
            ),
        ],
    ),
    Scenario(
        id="S2-misleading-success",
        description=(
            "Model claims 'fixed' on first attempt without actually fixing. "
            "Tool calls all return ok. B3 NEVER fires (no tool error loop). "
            "Outcomes grader catches the lie immediately, retries, and the "
            "model corrects on the second self-retry inside one SDK call."
        ),
        attempts=[
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("Read", {"file_path": "/x.py"}, "ok"),
                    ToolCallSpec("Edit",
                                 {"file_path": "/x.py", "old": "a",
                                  "new": "b"}, "ok"),
                ],
                claims_success=True,
                actually_fixed=False,
            ),
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("Edit",
                                 {"file_path": "/x.py", "old": "c",
                                  "new": "d"}, "ok"),
                ],
                claims_success=True,
                actually_fixed=True,
            ),
        ],
    ),
    Scenario(
        id="S3-mixed-loop-then-lie",
        description=(
            "Model first errors 3x (B3 reset triggered), then on the "
            "post-reset attempt claims success without fixing. B3 wastes "
            "1 reset on the loop, then misses the false claim — "
            "burns the remaining 2 resets discovering the fix never "
            "landed (in production: probably aborts terminal). Outcomes "
            "catches the false claim on the very first claimed-completion."
        ),
        attempts=[
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("Bash", {"command": "find ."},
                                 "bash_metachar_blocked"),
                    ToolCallSpec("Bash", {"command": "find ."},
                                 "bash_metachar_blocked"),
                    ToolCallSpec("Bash", {"command": "find ."},
                                 "bash_metachar_blocked"),
                ],
                claims_success=False,
                actually_fixed=False,
            ),
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("Read", {"file_path": "/y.py"}, "ok"),
                ],
                claims_success=True,
                actually_fixed=False,
            ),
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("Edit",
                                 {"file_path": "/y.py", "old": "x",
                                  "new": "y"}, "ok"),
                ],
                claims_success=True,
                actually_fixed=True,
            ),
        ],
    ),
    Scenario(
        id="S4-grader-hallucinates-pass",
        description=(
            "Adversarial: model claims success without fixing, grader "
            "hallucinates a pass (rubric was 'must mention a fix' — the "
            "model wrote the word). B3 also misses (no tool error loop). "
            "BOTH paths fail to recover. This is the floor: neither "
            "approach catches every false-positive without ground truth."
        ),
        attempts=[
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("Read", {"file_path": "/z.py"}, "ok"),
                ],
                claims_success=True,
                actually_fixed=False,
            ),
        ],
    ),
    Scenario(
        id="S5-progressive-narrowing",
        description=(
            "Model issues Glob with progressively-tighter args — D3 case "
            "from OP-830 AC #3. B3's Levenshtein guard correctly classifies "
            "this as progress (no reset). Outcomes is irrelevant here — no "
            "completion claim — but the wall-clock cost is just the work, "
            "no reset overhead. Verifies the spike doesn't penalise B3 on "
            "its strongest case."
        ),
        attempts=[
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("Grep", {"pattern": "**/*.py"}, "ok"),
                    ToolCallSpec("Grep",
                                 {"pattern": "backend/**/*.py"}, "ok"),
                    ToolCallSpec("Grep",
                                 {"pattern": "backend/agents/*.py"}, "ok"),
                    ToolCallSpec("Read",
                                 {"file_path": "backend/agents/x.py"}, "ok"),
                ],
                claims_success=True,
                actually_fixed=True,
            ),
        ],
    ),
    Scenario(
        id="S6-rubric-overfits",
        description=(
            "Rubric is overly literal: 'must call run_tests'. Model calls "
            "run_tests but ignores the failure; claims success. Grader "
            "passes (rubric satisfied at the surface) but the work is "
            "broken. B3 is also blind here (no tool error loop). Same "
            "failure floor as S4 but with a different mechanism — surfaces "
            "rubric-design as a separate engineering risk for any "
            "Outcomes integration."
        ),
        attempts=[
            AttemptSpec(
                tool_calls=[
                    ToolCallSpec("run_tests",
                                 {"target": "//x:y_test"}, "ok"),
                ],
                claims_success=True,
                actually_fixed=False,
            ),
        ],
    ),
]


# ── Path A simulator (real B3 detector) ────────────────────────────────


@dataclass
class PathResult:
    """Per-scenario result for one path."""

    path: str
    recovered: bool
    attempts_consumed: int
    resets_consumed: int
    grader_calls: int
    input_tokens: int
    output_tokens: int
    grader_input_tokens: int
    grader_output_tokens: int
    wall_seconds: float
    notes: list[str] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        sonnet_in = self.input_tokens / 1_000_000 * PRICE_PER_M_INPUT[
            "claude-sonnet-4"]
        sonnet_out = self.output_tokens / 1_000_000 * PRICE_PER_M_OUTPUT[
            "claude-sonnet-4"]
        haiku_in = self.grader_input_tokens / 1_000_000 * PRICE_PER_M_INPUT[
            "claude-haiku-4-5"]
        haiku_out = self.grader_output_tokens / 1_000_000 * PRICE_PER_M_OUTPUT[
            "claude-haiku-4-5"]
        return sonnet_in + sonnet_out + haiku_in + haiku_out


def simulate_path_a(scenario: Scenario) -> PathResult:
    """B3 path: real LoopDetector + 3-attempt cap (RESET_LIMIT)."""
    detector = LoopDetector(ticket_key=f"SPIKE-{scenario.id}")
    notes: list[str] = []
    attempts_consumed = 0
    in_tok = out_tok = 0
    wall = 0.0

    for attempt in scenario.attempts:
        attempts_consumed += 1
        in_tok += ATTEMPT_INPUT_TOKENS
        out_tok += ATTEMPT_OUTPUT_TOKENS
        wall += ATTEMPT_WALL_S

        # Feed tool calls into the detector in order.
        triggered_reset = False
        for call in attempt.tool_calls:
            detector.record_tool_call(
                tool_name=call.tool_name,
                tool_args=call.args,
                error_class=call.error_class,
            )
            if detector.is_reset_required():
                if detector.can_reset():
                    detector.mark_reset()
                    triggered_reset = True
                    wall += RESET_OVERHEAD_S
                    notes.append(
                        f"attempt#{attempts_consumed}: B3 reset "
                        f"#{detector.reset_count} after "
                        f"{TRIPLE_MATCH_THRESHOLD}x "
                        f"{call.tool_name} / {call.error_class}"
                    )
                    break
                # Reset budget exhausted — abort terminal per AC #5.
                notes.append(
                    "B3 abort: loop_aborted_terminal "
                    f"(reset budget {RESET_LIMIT} exhausted)"
                )
                return PathResult(
                    path="A_b3",
                    recovered=False,
                    attempts_consumed=attempts_consumed,
                    resets_consumed=detector.reset_count,
                    grader_calls=0,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    grader_input_tokens=0,
                    grader_output_tokens=0,
                    wall_seconds=wall,
                    notes=notes,
                )

        # Did the attempt claim success? B3 has NO ground-truth check —
        # if claims_success is True and no reset fired, the orchestrator
        # accepts the result. This is the modeled blind spot.
        if attempt.claims_success and not triggered_reset:
            recovered = attempt.actually_fixed
            if not recovered:
                notes.append(
                    f"attempt#{attempts_consumed}: B3 accepted false "
                    "completion (no detector signal)."
                )
            return PathResult(
                path="A_b3",
                recovered=recovered,
                attempts_consumed=attempts_consumed,
                resets_consumed=detector.reset_count,
                grader_calls=0,
                input_tokens=in_tok,
                output_tokens=out_tok,
                grader_input_tokens=0,
                grader_output_tokens=0,
                wall_seconds=wall,
                notes=notes,
            )

    # Ran out of scripted attempts without a success claim.
    notes.append("B3: scenario exhausted without success claim")
    return PathResult(
        path="A_b3",
        recovered=False,
        attempts_consumed=attempts_consumed,
        resets_consumed=detector.reset_count,
        grader_calls=0,
        input_tokens=in_tok,
        output_tokens=out_tok,
        grader_input_tokens=0,
        grader_output_tokens=0,
        wall_seconds=wall,
        notes=notes,
    )


# ── Path B simulator (Outcomes grader) ─────────────────────────────────


def _grader_verdict(attempt: AttemptSpec, scenario: Scenario) -> bool:
    """Synthetic grader. Returns True if grader says PASS.

    Models the two grader-failure modes called out in the report:
      * S4 (grader hallucinates): grader passes when ground truth is
        false (overly-permissive rubric).
      * S6 (rubric over-fits): grader passes on a surface signal that
        decouples from the actual outcome.
    Otherwise, the grader is honest: pass iff actually_fixed.
    """
    if scenario.id in {"S4-grader-hallucinates-pass", "S6-rubric-overfits"}:
        # Grader is fooled — returns pass regardless of ground truth.
        return attempt.claims_success
    return attempt.actually_fixed


def simulate_path_b(scenario: Scenario) -> PathResult:
    """Outcomes path: one SDK call with up to OUTCOMES_MAX_RETRIES self-
    retries, each followed by a grader check. The grader runs ONLY when
    the model claims completion (consistent with Anthropic's beta semantics
    per Lucas's talk: rubric is evaluated at the SDK call's terminal turn).
    """
    notes: list[str] = []
    attempts_consumed = 0
    grader_calls = 0
    in_tok = out_tok = 0
    g_in_tok = g_out_tok = 0
    wall = 0.0

    for attempt in scenario.attempts:
        if attempts_consumed >= OUTCOMES_MAX_RETRIES:
            notes.append(
                f"Outcomes: max self-retries ({OUTCOMES_MAX_RETRIES}) "
                "exhausted"
            )
            return PathResult(
                path="B_outcomes",
                recovered=False,
                attempts_consumed=attempts_consumed,
                resets_consumed=0,
                grader_calls=grader_calls,
                input_tokens=in_tok,
                output_tokens=out_tok,
                grader_input_tokens=g_in_tok,
                grader_output_tokens=g_out_tok,
                wall_seconds=wall,
                notes=notes,
            )

        attempts_consumed += 1
        in_tok += ATTEMPT_INPUT_TOKENS
        out_tok += ATTEMPT_OUTPUT_TOKENS
        wall += ATTEMPT_WALL_S

        # Outcomes does not run the grader if the model never claims
        # completion (e.g. blew through max_iterations stuck in tool errors).
        # In that case the SDK call returns a non-completion stop_reason
        # and the host has to handle it like any other failed run.
        if not attempt.claims_success:
            notes.append(
                f"attempt#{attempts_consumed}: no completion claim — "
                "Outcomes does not run grader; host falls back."
            )
            continue

        grader_calls += 1
        g_in_tok += GRADER_INPUT_TOKENS
        g_out_tok += GRADER_OUTPUT_TOKENS
        wall += GRADER_WALL_S

        verdict = _grader_verdict(attempt, scenario)
        if verdict:
            recovered = attempt.actually_fixed
            if not recovered:
                notes.append(
                    f"attempt#{attempts_consumed}: grader hallucinated "
                    "PASS — Outcomes accepted false completion."
                )
            return PathResult(
                path="B_outcomes",
                recovered=recovered,
                attempts_consumed=attempts_consumed,
                resets_consumed=0,
                grader_calls=grader_calls,
                input_tokens=in_tok,
                output_tokens=out_tok,
                grader_input_tokens=g_in_tok,
                grader_output_tokens=g_out_tok,
                wall_seconds=wall,
                notes=notes,
            )
        notes.append(
            f"attempt#{attempts_consumed}: grader FAIL → self-retry"
        )

    notes.append("Outcomes: scenario exhausted")
    return PathResult(
        path="B_outcomes",
        recovered=False,
        attempts_consumed=attempts_consumed,
        resets_consumed=0,
        grader_calls=grader_calls,
        input_tokens=in_tok,
        output_tokens=out_tok,
        grader_input_tokens=g_in_tok,
        grader_output_tokens=g_out_tok,
        wall_seconds=wall,
        notes=notes,
    )


# ── Comparison + reporting ─────────────────────────────────────────────


def compare(scenarios: list[Scenario]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for sc in scenarios:
        a = simulate_path_a(sc)
        b = simulate_path_b(sc)
        rows.append({
            "scenario": sc.id,
            "description": sc.description,
            "path_a": asdict(a) | {"total_cost_usd": a.total_cost_usd},
            "path_b": asdict(b) | {"total_cost_usd": b.total_cost_usd},
            "delta": {
                "cost_usd": b.total_cost_usd - a.total_cost_usd,
                "wall_seconds": b.wall_seconds - a.wall_seconds,
                "recovery_a": a.recovered,
                "recovery_b": b.recovered,
            },
        })
    summary = _summary(rows)
    return {"summary": summary, "rows": rows}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    a_recover = sum(1 for r in rows if r["path_a"]["recovered"])
    b_recover = sum(1 for r in rows if r["path_b"]["recovered"])
    a_costs = [r["path_a"]["total_cost_usd"] for r in rows]
    b_costs = [r["path_b"]["total_cost_usd"] for r in rows]
    a_walls = [r["path_a"]["wall_seconds"] for r in rows]
    b_walls = [r["path_b"]["wall_seconds"] for r in rows]
    a_only = [r["scenario"] for r in rows
              if r["path_a"]["recovered"] and not r["path_b"]["recovered"]]
    b_only = [r["scenario"] for r in rows
              if r["path_b"]["recovered"] and not r["path_a"]["recovered"]]
    return {
        "scenarios": n,
        "recovery_rate_a": a_recover / n if n else 0.0,
        "recovery_rate_b": b_recover / n if n else 0.0,
        "mean_cost_usd_a": statistics.mean(a_costs) if a_costs else 0.0,
        "mean_cost_usd_b": statistics.mean(b_costs) if b_costs else 0.0,
        "mean_wall_s_a": statistics.mean(a_walls) if a_walls else 0.0,
        "mean_wall_s_b": statistics.mean(b_walls) if b_walls else 0.0,
        "a_recovers_b_does_not": a_only,
        "b_recovers_a_does_not": b_only,
    }


# ── CLI ────────────────────────────────────────────────────────────────


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="spike_b3_outcomes_compare",
        description=(
            "OP-843 B3-vs-Outcomes spike harness. Synthetic comparison of "
            "B3's 3x-loop reset against an Outcomes-graded recovery path."
        ),
    )
    p.add_argument(
        "--output", default="data/op-843-comparison.json",
        help="JSON sidecar output path (default: %(default)s)",
    )
    p.add_argument(
        "--scenarios", default=None,
        help="Optional JSON file overriding the built-in scenarios. "
             "Schema mirrors the Scenario / AttemptSpec / ToolCallSpec "
             "dataclasses defined in this module.",
    )
    return p.parse_args(argv)


def _load_scenarios(path: str | None) -> list[Scenario]:
    if not path:
        return list(SCENARIOS)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[Scenario] = []
    for entry in raw:
        attempts = [
            AttemptSpec(
                tool_calls=[ToolCallSpec(**tc) for tc in a["tool_calls"]],
                claims_success=a["claims_success"],
                actually_fixed=a["actually_fixed"],
            )
            for a in entry["attempts"]
        ]
        out.append(Scenario(
            id=entry["id"],
            description=entry["description"],
            attempts=attempts,
        ))
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _parse_cli(argv if argv is not None else sys.argv[1:])
    started = time.monotonic()
    scenarios = _load_scenarios(args.scenarios)
    result = compare(scenarios)
    elapsed = time.monotonic() - started

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    s = result["summary"]
    print(
        f"[OP-843 spike] scenarios={s['scenarios']} "
        f"recovery A={s['recovery_rate_a']:.0%} B={s['recovery_rate_b']:.0%} "
        f"cost_mean A=${s['mean_cost_usd_a']:.4f} "
        f"B=${s['mean_cost_usd_b']:.4f} "
        f"wall_mean A={s['mean_wall_s_a']:.1f}s "
        f"B={s['mean_wall_s_b']:.1f}s "
        f"(harness {elapsed:.2f}s) → {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
