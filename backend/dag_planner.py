"""Phase 56-DAG-C — DAG mutation planner.

S1: build the mutation prompt, call an LLM, parse the response back
into a `DAG`. Deliberately injectable (`ask_fn`) so the loop in S2
can be unit-tested without touching the network.

Error handling:

  * JSON extraction is tolerant of common LLM sloppiness — strips
    ```json fences, trims leading prose — but does NOT try to
    ad-hoc "fix" malformed structures. If it doesn't parse, raise
    `OrchestratorResponseError` with the raw body so S2 can file
    an audit row and retry.
  * We do NOT validate the returned DAG here. That's the caller's
    job (S2 run_mutation_loop) — the validator decides "retry" vs
    "done" on its own authority.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from backend.dag_schema import DAG
from backend.dag_validator import ValidationError

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_PROMPT_PATH = _PROJECT_ROOT / "backend" / "agents" / "prompts" / "orchestrator.md"


# Injectable ask function: (system_prompt, user_prompt) → (answer, tokens).
# Matches iq_runner.AskFn shape but takes two prompts since the mutation
# call splits system (orchestrator rules) from user (prior DAG + errors).
OrchestratorAskFn = Callable[[str, str], Awaitable[tuple[str, int]]]


class OrchestratorResponseError(ValueError):
    """Orchestrator returned something that didn't parse into a DAG."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  System prompt loading
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_CACHED_SYSTEM_PROMPT: str | None = None


def load_system_prompt() -> str:
    """Read the orchestrator prompt markdown from disk (stripping
    YAML front-matter). Cached after first read."""
    global _CACHED_SYSTEM_PROMPT
    if _CACHED_SYSTEM_PROMPT is not None:
        return _CACHED_SYSTEM_PROMPT
    try:
        body = ORCHESTRATOR_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error(
            "orchestrator prompt missing at %s — using minimal fallback",
            ORCHESTRATOR_PROMPT_PATH,
        )
        _CACHED_SYSTEM_PROMPT = (
            "You are the Lead Orchestrator. Emit exactly one JSON DAG "
            "object fixing ALL validator errors. No prose."
        )
        return _CACHED_SYSTEM_PROMPT
    # Strip YAML front-matter if present.
    if body.startswith("---\n"):
        _, _, after = body.partition("\n---\n")
        body = after
    _CACHED_SYSTEM_PROMPT = body.strip()
    return _CACHED_SYSTEM_PROMPT


def _reset_prompt_cache_for_tests() -> None:
    global _CACHED_SYSTEM_PROMPT
    _CACHED_SYSTEM_PROMPT = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Prompt building
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_user_prompt(prior: DAG, errors: list[ValidationError]) -> str:
    """Deterministic user-side prompt body. The orchestrator sees the
    full failing DAG + a numbered error list and is asked to emit a
    single replacement JSON object."""
    err_lines = []
    for e in errors:
        err_lines.append(
            f"- rule: {e.rule}\n"
            f"  task_id: {e.task_id if e.task_id is not None else 'null'}\n"
            f"  message: {e.message}"
        )
    return (
        "PRIOR DAG (failed validation):\n"
        f"{prior.model_dump_json(indent=2)}\n\n"
        "VALIDATOR ERRORS (must ALL be resolved):\n"
        + ("\n".join(err_lines) if err_lines else "(none — planner bug)")
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Response parsing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*\n?(.*?)\n?```", re.DOTALL,
)


def _extract_json(raw: str) -> str:
    """Pull the JSON object out of an LLM response that may have
    extras. Strategy:

      1. If the whole thing starts with '{', return as-is.
      2. If a ```json fence exists, use its contents.
      3. Else take the first '{' to the matching '}' by brace-count.
      4. Else return the raw string (parse will fail loudly).
    """
    s = raw.strip()
    if not s:
        return s
    if s.startswith("{"):
        return s
    m = _FENCE_RE.search(s)
    if m:
        return m.group(1).strip()
    # Brace-balanced extraction.
    start = s.find("{")
    if start == -1:
        return s
    depth = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:]


def parse_response(raw: str) -> DAG:
    """Parse orchestrator output → `DAG`. Raises
    `OrchestratorResponseError` with the raw body on any failure
    (makes the S2 audit trail meaningful)."""
    if not raw or not raw.strip():
        raise OrchestratorResponseError("orchestrator returned empty response")
    body = _extract_json(raw)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OrchestratorResponseError(
            f"orchestrator response was not valid JSON: {exc}; "
            f"raw head: {raw[:300]!r}"
        ) from exc
    try:
        return DAG.model_validate(data)
    except Exception as exc:
        raise OrchestratorResponseError(
            f"orchestrator response didn't match DAG schema: {exc}; "
            f"keys: {list(data.keys()) if isinstance(data, dict) else 'non-dict'}"
        ) from exc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Single-shot mutation proposal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def propose_mutation(
    prior: DAG,
    errors: list[ValidationError],
    *,
    ask_fn: OrchestratorAskFn,
) -> tuple[DAG, int]:
    """Ask the orchestrator for ONE replacement DAG. Returns
    ``(new_dag, tokens_used)``. Does NOT run the validator — the
    caller (S2 loop) does, so a cycle of {validate → mutate →
    validate} stays transparent at the loop level.

    Raises `OrchestratorResponseError` on a broken response so the
    loop can count retries cleanly.
    """
    if not errors:
        raise ValueError(
            "propose_mutation called with no errors — nothing to fix"
        )
    system = load_system_prompt()
    user = build_user_prompt(prior, errors)
    answer, n_tokens = await ask_fn(system, user)
    new_dag = parse_response(answer)
    # Sanity: dag_id must stay stable — hard-corrected if planner drifts.
    if new_dag.dag_id != prior.dag_id:
        logger.warning(
            "orchestrator changed dag_id %r → %r; restoring original",
            prior.dag_id, new_dag.dag_id,
        )
        new_dag = new_dag.model_copy(update={"dag_id": prior.dag_id})
    return new_dag, int(n_tokens or 0)
