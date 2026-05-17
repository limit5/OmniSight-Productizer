"""Outcomes verdict consumption for the JIRA runner (OP-860).

The runner owns when grading happens. This module owns the narrow mapping
from grader verdict to JIRA/Gerrit side effects so tests can exercise that
contract without loading ``auto-runner-jira.py``.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from backend import pricing
from backend.agents import jira_dispatch
from backend.agents.anthropic_native_client import AnthropicClient, TokenUsage
from backend.agents.loop_detector import DEFAULT_GRADER_MODEL, OutcomesGraderUnavailable

OUTCOMES_GRADER_ENABLED_ENV = "OMNISIGHT_OUTCOMES_GRADER_ENABLED"
OUTCOMES_BUDGET_USD_PER_DAY_ENV = "OMNISIGHT_OUTCOMES_BUDGET_USD_PER_DAY"
OUTCOMES_BUDGET_STATE_PATH_ENV = "OMNISIGHT_OUTCOMES_BUDGET_STATE_PATH"
DEFAULT_OUTCOMES_BUDGET_USD_PER_DAY = 5.00

OUTCOMES_PARTIAL_LABEL = "outcomes:partial"
OUTCOMES_FAIL_LABEL = "outcomes:fail"

# `pricing.get_pricing` returns USD-per-million-tokens; divide by this to
# convert (tokens * USD/Mtok) into total USD.
PRICING_TOKENS_PER_UNIT = 1_000_000

# Truncation limits for grader I/O and downstream JIRA fields. Kept tight
# so a runaway grader response or huge diff can't blow out a JIRA comment
# or transition payload.
MAX_GRADER_REASONING_CHARS = 1000
MAX_COMPLETION_TEXT_CHARS = 8000
MAX_DIFF_TEXT_CHARS = 120000
MAX_TRANSITION_REASON_CHARS = 500
GRADER_RESPONSE_ERROR_PREVIEW_CHARS = 200

OUTCOMES_GRADER_PROMPT_TEMPLATE = """You are the OmniSight Outcomes grader.

Return exactly one JSON object with:
  "verdict": "pass" | "partial" | "fail"
  "grader_reasoning": short rationale grounded in the acceptance criteria

Acceptance Criteria:
{ac_text}

Runner completion:
{completion_text}

Diff:
{diff_text}
"""


class OutcomesGraderRefused(RuntimeError):
    """The grader response no longer matches the expected schema."""


class OutcomesBudgetExceeded(RuntimeError):
    """Daily Outcomes budget has already been spent."""


@dataclass(frozen=True)
class RunnerOutcomesVerdict:
    verdict: str
    grader_reasoning: str
    grader_input_tokens: int = 0
    grader_output_tokens: int = 0
    cost_usd: float = 0.0


def outcomes_enabled(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else os.environ
    return env.get(OUTCOMES_GRADER_ENABLED_ENV, "0") == "1"


def outcomes_budget_usd_per_day(env: dict[str, str] | None = None) -> float:
    env = env if env is not None else os.environ
    raw = env.get(OUTCOMES_BUDGET_USD_PER_DAY_ENV, str(DEFAULT_OUTCOMES_BUDGET_USD_PER_DAY))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_OUTCOMES_BUDGET_USD_PER_DAY
    return max(0.0, value)


def _budget_state_path(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    configured = env.get(OUTCOMES_BUDGET_STATE_PATH_ENV)
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "omnisight-outcomes-budget.json"


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class OutcomesBudgetTracker:
    """Small file-backed daily spend tracker for runner grader calls."""

    def __init__(
        self,
        *,
        daily_budget_usd: float | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.daily_budget_usd = (
            DEFAULT_OUTCOMES_BUDGET_USD_PER_DAY
            if daily_budget_usd is None
            else max(0.0, float(daily_budget_usd))
        )
        self.state_path = state_path or _budget_state_path()

    def _read(self) -> dict[str, object]:
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"date": _today_utc(), "spent_usd": 0.0}
        if data.get("date") != _today_utc():
            return {"date": _today_utc(), "spent_usd": 0.0}
        return data

    def spent_usd(self) -> float:
        data = self._read()
        try:
            return float(data.get("spent_usd", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def ensure_available(self) -> None:
        if self.spent_usd() >= self.daily_budget_usd:
            raise OutcomesBudgetExceeded(
                f"outcomes budget exhausted: {self.spent_usd():.4f} >= "
                f"{self.daily_budget_usd:.4f}"
            )

    def record(self, cost_usd: float) -> None:
        data = self._read()
        spent = self.spent_usd() + max(0.0, float(cost_usd))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"date": data.get("date") or _today_utc(), "spent_usd": spent})
        )


def estimate_grader_cost_usd(
    *,
    model: str,
    usage: TokenUsage,
) -> float:
    input_per_mtok, output_per_mtok = pricing.get_pricing("anthropic", model)
    return (
        (usage.input_tokens * input_per_mtok)
        + (usage.output_tokens * output_per_mtok)
    ) / PRICING_TOKENS_PER_UNIT


def parse_outcomes_grader_response(text: str) -> tuple[str, str]:
    candidate = (text or "").strip()
    payload: dict[str, object] | None = None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if 0 <= start < end:
            try:
                payload = json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                payload = None
    if not isinstance(payload, dict):
        raise OutcomesGraderRefused(
            "grader response did not contain JSON: "
            f"{candidate[:GRADER_RESPONSE_ERROR_PREVIEW_CHARS]!r}"
        )
    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in {"pass", "partial", "fail"}:
        raise OutcomesGraderRefused(
            f"grader verdict not in pass/partial/fail: {verdict!r}"
        )
    reasoning = str(payload.get("grader_reasoning", "")).strip()[:MAX_GRADER_REASONING_CHARS]
    if not reasoning:
        raise OutcomesGraderRefused("grader response omitted grader_reasoning")
    return verdict, reasoning


def grade_outcomes(
    *,
    client: AnthropicClient,
    ticket_key: str,
    ac_text: str,
    completion_text: str,
    diff_text: str,
    grader_model: str = DEFAULT_GRADER_MODEL,
) -> RunnerOutcomesVerdict:
    prompt = OUTCOMES_GRADER_PROMPT_TEMPLATE.format(
        ac_text=(ac_text or "").strip() or "(no acceptance criteria found)",
        completion_text=(completion_text or "").strip()[:MAX_COMPLETION_TEXT_CHARS],
        diff_text=(diff_text or "").strip()[:MAX_DIFF_TEXT_CHARS],
    )
    try:
        text, usage = client.simple(
            prompt=prompt,
            model=grader_model,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - runner degrades on availability
        raise OutcomesGraderUnavailable(
            f"grader API call failed for {ticket_key}: {type(exc).__name__}: {exc}"
        ) from exc

    verdict, reasoning = parse_outcomes_grader_response(text)
    cost_usd = estimate_grader_cost_usd(model=grader_model, usage=usage)
    return RunnerOutcomesVerdict(
        verdict=verdict,
        grader_reasoning=reasoning,
        grader_input_tokens=usage.input_tokens,
        grader_output_tokens=usage.output_tokens,
        cost_usd=cost_usd,
    )


def consume_outcomes_verdict(
    *,
    client: jira_dispatch.DispatchClient,
    key: str,
    verdict: RunnerOutcomesVerdict,
    revert_patchsets: Callable[[], None] | None = None,
) -> None:
    """Apply OP-860 verdict side effects.

    ``pass`` intentionally does nothing. ``partial`` leaves the ticket in
    the normal runner path with a comment + label. ``fail`` abandons/reverts
    the pushed patch set via the injected callback, then reopens the ticket.
    """
    if verdict.verdict == "pass":
        return

    if verdict.verdict == "partial":
        jira_dispatch.add_comment(
            client,
            key,
            "[outcomes:partial]\n\n"
            f"{verdict.grader_reasoning}\n\n"
            f"Estimated grader cost: ${verdict.cost_usd:.4f}",
        )
        jira_dispatch.add_label(client, key, OUTCOMES_PARTIAL_LABEL)
        return

    if verdict.verdict == "fail":
        if revert_patchsets is not None:
            revert_patchsets()
        jira_dispatch.add_comment(
            client,
            key,
            "[outcomes:fail]\n\n"
            f"{verdict.grader_reasoning}\n\n"
            f"Estimated grader cost: ${verdict.cost_usd:.4f}",
        )
        jira_dispatch.add_label(client, key, OUTCOMES_FAIL_LABEL)
        jira_dispatch.transition_back_to_todo(
            client,
            key,
            f"[outcomes:fail] {verdict.grader_reasoning[:MAX_TRANSITION_REASON_CHARS]}",
        )
        return

    raise OutcomesGraderRefused(f"unknown verdict: {verdict.verdict!r}")
