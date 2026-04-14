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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mutation loop (S2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from dataclasses import dataclass, field  # noqa: E402 — keep near the loop
from typing import Optional  # noqa: E402

MAX_MUTATION_ROUNDS = 3  # locked decision, see HANDOFF


@dataclass
class MutationAttempt:
    """One propose → validate round inside the loop."""
    round_index: int
    dag_before: DAG
    errors_before: list[ValidationError]
    dag_after: Optional[DAG]  # None if the propose itself failed
    errors_after: list[ValidationError] = field(default_factory=list)
    tokens_used: int = 0
    orchestrator_error: Optional[str] = None


@dataclass
class MutationResult:
    """Outcome of run_mutation_loop().

    `status`:
      * ``validated``          final DAG passed the validator
      * ``exhausted``          used all rounds without converging
      * ``orchestrator_error`` every round raised before a DAG
                               could be re-validated (all dag_after
                               are None)
    """
    status: str
    final_dag: DAG
    attempts: list[MutationAttempt]
    total_tokens: int

    @property
    def ok(self) -> bool:
        return self.status == "validated"


async def run_mutation_loop(
    initial: DAG,
    *,
    ask_fn: OrchestratorAskFn,
    max_rounds: int = MAX_MUTATION_ROUNDS,
    file_exhausted_proposal: bool = True,
) -> MutationResult:
    """Validate → mutate → re-validate up to ``max_rounds`` times.

    Returns a ``MutationResult`` carrying the full attempt trace so
    the caller can audit exactly how the DAG evolved. On
    ``status=exhausted`` / ``orchestrator_error`` we file a Decision
    Engine ``kind=dag/exhausted severity=destructive`` proposal (admin)
    unless ``file_exhausted_proposal=False`` (test hook).

    Does NOT persist intermediate attempts — that's Phase 56-DAG-D's
    job. This function is a pure transform:
    (DAG, ask_fn) → MutationResult.
    """
    from backend import dag_validator as dv

    attempts: list[MutationAttempt] = []
    total_tokens = 0
    current = initial

    v0 = dv.validate(current)
    if v0.ok:
        return MutationResult(
            status="validated", final_dag=current,
            attempts=[], total_tokens=0,
        )

    current_errors = v0.errors
    last_orch_error: str | None = None

    for i in range(1, max_rounds + 1):
        att = MutationAttempt(
            round_index=i, dag_before=current,
            errors_before=current_errors, dag_after=None,
        )
        try:
            new_dag, toks = await propose_mutation(
                current, current_errors, ask_fn=ask_fn,
            )
            att.tokens_used = toks
            total_tokens += toks
            att.dag_after = new_dag
        except OrchestratorResponseError as exc:
            att.orchestrator_error = str(exc)
            last_orch_error = str(exc)
            attempts.append(att)
            # A parse failure spends a round — otherwise a broken
            # orchestrator could loop forever against the same prompt.
            continue

        v = dv.validate(new_dag)
        att.errors_after = v.errors
        attempts.append(att)
        current = new_dag
        current_errors = v.errors
        if v.ok:
            try:
                from backend import metrics as _m
                _m.dag_mutation_total.labels(result="recovered").inc()
            except Exception:
                pass
            return MutationResult(
                status="validated", final_dag=current,
                attempts=attempts, total_tokens=total_tokens,
            )

    # Exhausted: budget hit without validation.
    try:
        from backend import metrics as _m
        _m.dag_mutation_total.labels(result="exhausted").inc()
    except Exception:
        pass

    if file_exhausted_proposal:
        await _file_exhausted_proposal(
            initial, current, attempts, last_orch_error,
        )

    status = (
        "orchestrator_error"
        if last_orch_error and all(a.dag_after is None for a in attempts)
        else "exhausted"
    )
    return MutationResult(
        status=status, final_dag=current,
        attempts=attempts, total_tokens=total_tokens,
    )


async def _file_exhausted_proposal(
    initial: DAG,
    last_dag: DAG,
    attempts: list[MutationAttempt],
    last_orch_error: str | None,
) -> None:
    """File a Decision Engine admin-gate proposal so the operator sees
    the broken DAG plan. Best-effort — DE failures must not raise back
    into the mutation loop's caller."""
    try:
        from backend import decision_engine as de
    except Exception as exc:
        logger.warning("dag exhausted: cannot import decision_engine: %s", exc)
        return
    try:
        lines = [
            f"dag_id={initial.dag_id}",
            f"rounds={len(attempts)}/{MAX_MUTATION_ROUNDS}",
        ]
        for a in attempts:
            if a.orchestrator_error:
                lines.append(
                    f"- r{a.round_index}: orchestrator_error: "
                    f"{a.orchestrator_error[:200]}"
                )
            else:
                rules = sorted({e.rule for e in a.errors_after})
                lines.append(
                    f"- r{a.round_index}: unresolved rules={rules} "
                    f"(tokens={a.tokens_used})"
                )
        if last_orch_error:
            lines.append(f"last_orchestrator_error: {last_orch_error[:300]}")

        de.propose(
            kind="dag/exhausted",
            title=f"DAG mutation exhausted: {initial.dag_id}",
            detail="\n".join(lines),
            options=[
                {"id": "abort", "label": "Abort",
                 "description": "Discard this DAG plan; operator will re-file."},
                {"id": "accept_failed",
                 "label": "Accept final draft despite failures",
                 "description": "Record the broken plan as-is; not recommended."},
            ],
            default_option_id="abort",  # safer default
            severity=de.DecisionSeverity.destructive,
            timeout_s=3600.0,
            source={
                "subsystem": "dag_planner",
                "dag_id": initial.dag_id,
                "rounds_used": len(attempts),
                "had_orchestrator_error": last_orch_error is not None,
            },
        )
    except Exception as exc:
        logger.warning("dag exhausted: DE propose failed: %s", exc)
