"""Phase 63-D D2 — IQ benchmark runner.

Owns the "ask each question against each model and return scores"
loop. Deliberately decoupled from the actual LLM call: the caller
injects an `ask_fn(model, prompt) -> (answer_text, token_estimate)`
so this module is fully testable without touching the network.

Token budget cap: a daily run can hit hundreds of K tokens if all
fallback chain models are evaluated. We accept a budget in tokens
and SKIP remaining questions once exceeded — the partial run is
still a valid signal (returned scores carry `truncated_at` so the
nightly aggregator (D3) can downweight it).

The default `ask_fn` (`live_ask_fn`) wraps `agents/llm.get_llm` but
we don't import it at module load to keep this file lightweight and
import-cycle-free.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Sequence

from backend import iq_benchmark as ib

logger = logging.getLogger(__name__)


# Type alias for the injectable: (model, prompt) -> (answer, token_count).
AskFn = Callable[[str, str], Awaitable[tuple[str, int]]]


@dataclass
class RunReport:
    """One benchmark × one model.

    `truncated_at_question` is None for a clean run, or the question_id
    after which the budget cap kicked in (everything beyond that index
    is recorded as failed-by-skip in the underlying score).
    """
    score: ib.BenchmarkScore
    tokens_used: int
    truncated_at_question: Optional[str] = None
    errors: list[str] = field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_PER_QUESTION_TIMEOUT_S = 30.0


async def run_benchmark(
    benchmark: ib.IQBenchmark,
    model: str,
    *,
    ask_fn: AskFn,
    token_budget: int = 50_000,
    per_question_timeout_s: float = DEFAULT_PER_QUESTION_TIMEOUT_S,
) -> RunReport:
    """Run one benchmark against one model.

    Stops early (records the question_id at which we stopped) if:
      * `tokens_used` reaches `token_budget`, OR
      * `ask_fn` raises asyncio.TimeoutError per question (we move on
        but record an error)
    Other exceptions in `ask_fn` are caught and logged; that question
    counts as a fail.
    """
    answers: dict[str, str] = {}
    errors: list[str] = []
    tokens_used = 0
    truncated_at: Optional[str] = None

    for q in benchmark.questions:
        if tokens_used >= token_budget:
            truncated_at = q.id
            logger.warning(
                "[IQ] token budget %d exhausted before %s/%s on %s",
                token_budget, benchmark.name, q.id, model,
            )
            break
        try:
            ask_coro = ask_fn(model, q.prompt)
            answer, n_tokens = await asyncio.wait_for(
                ask_coro, timeout=per_question_timeout_s,
            )
        except asyncio.TimeoutError:
            msg = f"timeout: {q.id}"
            errors.append(msg)
            logger.warning("[IQ] %s on %s/%s", msg, benchmark.name, model)
            continue
        except Exception as exc:
            msg = f"exception: {q.id}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            logger.warning("[IQ] %s on %s/%s", msg, benchmark.name, model)
            continue
        answers[q.id] = answer or ""
        tokens_used += max(0, int(n_tokens or 0))

    score = ib.score_answers(benchmark, model, answers)
    return RunReport(
        score=score, tokens_used=tokens_used,
        truncated_at_question=truncated_at, errors=errors,
    )


async def run_all(
    benchmarks: Sequence[ib.IQBenchmark],
    models: Sequence[str],
    *,
    ask_fn: AskFn,
    token_budget_per_model: int = 50_000,
    per_question_timeout_s: float = DEFAULT_PER_QUESTION_TIMEOUT_S,
) -> list[RunReport]:
    """Cross-product run: each benchmark × each model. Token budget is
    PER MODEL across all benchmarks (not per benchmark) so a single
    bloated set doesn't starve the others. Returns reports in
    deterministic (model, benchmark) order.
    """
    reports: list[RunReport] = []
    for model in models:
        remaining = token_budget_per_model
        for bench in benchmarks:
            r = await run_benchmark(
                bench, model,
                ask_fn=ask_fn,
                token_budget=remaining,
                per_question_timeout_s=per_question_timeout_s,
            )
            reports.append(r)
            remaining = max(0, remaining - r.tokens_used)
            if remaining == 0:
                logger.info(
                    "[IQ] model %s exhausted budget after %s; remaining "
                    "benchmarks scored as zero-answer",
                    model, bench.name,
                )
                # Continue: each subsequent benchmark gets budget=0 →
                # truncates at first Q, score=0. Caller still sees the
                # full matrix.
    return reports


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Live ask_fn (production wiring; opt-in)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def live_ask_fn(model: str, prompt: str) -> tuple[str, int]:
    """Default real-LLM `ask_fn`. Imported lazily to avoid pulling
    LangChain at module load time (tests stub this entirely)."""
    try:
        from backend.agents.llm import get_llm
    except Exception as exc:
        logger.error("live_ask_fn: cannot import LLM layer: %s", exc)
        return ("", 0)
    # `model` is a "<provider>/<name>" spec — match the same convention
    # used by token_fallback_provider / fallback_chain.
    if "/" in model:
        provider, name = model.split("/", 1)
    else:
        provider, name = model, None
    llm = get_llm(provider=provider, model=name)
    if llm is None:
        return ("", 0)
    # LangChain BaseChatModel: prefer `ainvoke` if present.
    try:
        if hasattr(llm, "ainvoke"):
            resp = await llm.ainvoke(prompt)
        else:
            resp = await asyncio.to_thread(llm.invoke, prompt)
    except Exception as exc:
        logger.warning("live_ask_fn: %s/%s call failed: %s", provider, name, exc)
        return ("", 0)
    text = getattr(resp, "content", str(resp))
    # Best-effort token estimate: prefer SDK metadata, else /4 chars.
    n_tokens = 0
    meta = getattr(resp, "response_metadata", {}) or {}
    usage = meta.get("token_usage") or meta.get("usage") or {}
    if isinstance(usage, dict):
        n_tokens = int(usage.get("total_tokens") or usage.get("output_tokens") or 0)
    if n_tokens == 0:
        n_tokens = max(1, len(prompt) // 4 + len(text) // 4)
    return (text, n_tokens)
