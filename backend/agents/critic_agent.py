"""B4 (OP-833) — Pre-commit Critic.

Read-only Haiku critic invoked between the coder commit and the Gerrit
push. Reviews the proposed diff against the ticket's Acceptance
Criteria, emits a structured verdict, and drives a single-retry dissent
protocol per master-plan §2.6 AC #4.

Critical invariants:

* **Critic NEVER posts to Gerrit Code-Review** (AC #6, F9 / ADR-0003
  governance guard). The verdict lives in JIRA + the commit-message
  footer. ``assert_no_gerrit_review_label`` is the boundary helper.

* **Critic NEVER blocks the coder due to its own infrastructure
  failure** (AC #5). Timeout, OOM, malformed reason-code, tool failure
  → the wrapper returns ``verdict=pass`` with an audit entry. The
  rationale: a buggy critic must not become a P0 blocker for the entire
  Sprint B pipeline.

* **At most one free coder retry** before escalation (AC #4). 2nd
  dissent → ``CriticReviewOutcome.escalated=True`` so the launcher can
  label the ticket ``under_review:critic_dissent`` and skip the push.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from backend.agents.critic_reason_codes import (
    VALID_REASON_CODES,
    CriticReasonCode,
)

log = logging.getLogger(__name__)

DEFAULT_CRITIC_MODEL = "claude-haiku-4-5"
CRITIC_MODEL_ENV_VAR = "OMNISIGHT_CRITIC_MODEL"
CRITIC_TIMEOUT_SECONDS = 30.0
MAX_DISSENT_ROUNDS = 1
CRITIC_TOOL_NAMES: tuple[str, ...] = ("Read", "Grep", "run-tests", "run-linter")

VerdictKind = Literal["pass", "dissent"]


@dataclass(frozen=True)
class CriticVerdict:
    """Structured critic output per AC #3."""

    verdict: VerdictKind
    reason_code: CriticReasonCode
    reason_text: str
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason_code": self.reason_code.value,
            "reason_text": self.reason_text,
        }

    def to_commit_footer(self) -> str:
        """Single line appended to the coder's commit message (AC #7)."""
        if self.verdict == "pass":
            return f"Critic-Verdict: pass ({self.reason_code.value})"
        return (
            f"Critic-Verdict: dissent ({self.reason_code.value}) — "
            f"{self.reason_text}"
        )

    def to_jira_comment(self) -> str:
        """Body for the JIRA comment posted by the launcher (AC #7)."""
        return (
            f"[critic-agent verdict={self.verdict} "
            f"reason_code={self.reason_code.value}]\n{self.reason_text}"
        )


@dataclass(frozen=True)
class CriticReviewOutcome:
    """Result of the dissent-protocol loop in ``review_with_dissent_protocol``."""

    final_verdict: CriticVerdict
    history: tuple[CriticVerdict, ...]
    escalated: bool

    @property
    def passed(self) -> bool:
        return self.final_verdict.verdict == "pass" and not self.escalated


class CriticInfraError(Exception):
    """Internal sentinel — surfaces as ``verdict=pass`` per AC #5."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GovernanceViolationError(RuntimeError):
    """Raised when an outbound Gerrit payload contains a forbidden label."""


class CriticBackend(Protocol):
    """Minimal async interface the critic needs from the LLM client.

    The runtime adapter wraps ``AnthropicClient.simple`` (which is sync)
    in ``asyncio.to_thread`` so the timeout path in :func:`review_once`
    stays uniform; tests pass a coroutine directly.
    """

    async def invoke(self, *, prompt: str, model: str) -> str: ...


def resolve_critic_model() -> str:
    """Read the critic model name, honouring AC #2's env-var override."""
    return os.environ.get(CRITIC_MODEL_ENV_VAR) or DEFAULT_CRITIC_MODEL


_VERDICT_RE = re.compile(r"\{[^{}]*\"verdict\"[^{}]*\}", re.DOTALL)


def _parse_verdict(raw_response: str) -> CriticVerdict:
    """Pull the trailing JSON envelope out of the critic's response.

    The agent is prompted to emit one JSON object on the last line; we
    accept any well-formed JSON that mentions ``verdict`` and reject
    anything else as ``critic_malformed_reason_code`` (AC #5 → pass).
    """
    matches = _VERDICT_RE.findall(raw_response or "")
    if not matches:
        raise CriticInfraError(
            "critic_malformed_reason_code",
            "no JSON object containing 'verdict' found in response",
        )
    try:
        obj = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise CriticInfraError(
            "critic_malformed_reason_code", f"json decode error: {exc}"
        ) from None

    verdict = obj.get("verdict")
    if verdict not in ("pass", "dissent"):
        raise CriticInfraError(
            "critic_malformed_reason_code", f"invalid verdict: {verdict!r}"
        )
    code_raw = obj.get("reason_code", "")
    code = (
        CriticReasonCode.from_string(code_raw) if isinstance(code_raw, str) else None
    )
    if code is None:
        raise CriticInfraError(
            "critic_malformed_reason_code",
            f"invalid reason_code {code_raw!r} (allowed: {sorted(VALID_REASON_CODES)})",
        )
    reason_text = str(obj.get("reason_text", "")).strip()
    return CriticVerdict(
        verdict=verdict, reason_code=code, reason_text=reason_text,
    )


def _safe_fallback_verdict(code: str, detail: str) -> CriticVerdict:
    """Per AC #5, infra failure → verdict=pass with audit entry."""
    log.warning(
        "critic infra failure: code=%s detail=%s — falling back to pass",
        code, detail,
    )
    return CriticVerdict(
        verdict="pass",
        reason_code=CriticReasonCode.OTHER,
        reason_text=f"infrastructure_failure:{code}",
        audit={"failure_code": code, "detail": detail, "fallback": True},
    )


def build_critic_prompt(
    *,
    diff: str,
    ac_text: str,
    prior_dissent: CriticVerdict | None = None,
) -> str:
    """Construct the user prompt fed into the Haiku critic."""
    body = (
        "You are a pre-commit code-review critic. Compare the proposed "
        "diff against the ticket Acceptance Criteria and emit a structured "
        "verdict.\n\n"
        f"=== Acceptance Criteria ===\n{ac_text}\n\n"
        f"=== Proposed diff ===\n{diff}\n\n"
        "Allowed tools (READ-ONLY): Read, Grep, run-tests, run-linter. "
        "You MUST NOT modify any files and MUST NOT post to Gerrit — you "
        "are an internal pre-commit gate, never a Gerrit reviewer.\n\n"
        "Respond with EXACTLY one JSON object on the LAST line:\n"
        "  {\"verdict\": \"pass\" | \"dissent\",\n"
        f"   \"reason_code\": one of {sorted(VALID_REASON_CODES)},\n"
        "   \"reason_text\": short free-form explanation}\n"
        "Use 'pass' when the diff implements the AC correctly. Use 'dissent' "
        "when it does not, with a specific reason_code."
    )
    if prior_dissent is not None:
        body += (
            f"\n\nNOTE: a prior critic round dissented with reason_code="
            f"{prior_dissent.reason_code.value}, reason_text="
            f"\"{prior_dissent.reason_text}\". The coder has revised the diff "
            "in response. Re-evaluate from scratch."
        )
    return body


async def review_once(
    backend: CriticBackend,
    *,
    diff: str,
    ac_text: str,
    model: str | None = None,
    timeout_s: float = CRITIC_TIMEOUT_SECONDS,
    prior_dissent: CriticVerdict | None = None,
) -> CriticVerdict:
    """Run a single critic round, swallowing infrastructure failures.

    AC #5 maps every internal failure (timeout / OOM / malformed
    output / tool failure) to ``verdict=pass`` with audit metadata so
    the critic never blocks the coder due to its own bug.
    """
    chosen_model = model or resolve_critic_model()
    prompt = build_critic_prompt(
        diff=diff, ac_text=ac_text, prior_dissent=prior_dissent,
    )
    try:
        text = await asyncio.wait_for(
            backend.invoke(prompt=prompt, model=chosen_model),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return _safe_fallback_verdict(
            "critic_timeout", f"critic exceeded {timeout_s:.0f}s soft cap",
        )
    except MemoryError:
        return _safe_fallback_verdict(
            "critic_oom", "critic ran out of memory",
        )
    except CriticInfraError as exc:
        return _safe_fallback_verdict(exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 — defence in depth (AC #5)
        return _safe_fallback_verdict(
            "critic_tool_failure", f"{type(exc).__name__}: {exc}",
        )

    try:
        return _parse_verdict(text)
    except CriticInfraError as exc:
        return _safe_fallback_verdict(exc.code, exc.message)


CoderRetry = Callable[[CriticVerdict], Awaitable[str]]


async def review_with_dissent_protocol(
    backend: CriticBackend,
    *,
    diff: str,
    ac_text: str,
    coder_retry: CoderRetry,
    model: str | None = None,
    max_dissent_rounds: int = MAX_DISSENT_ROUNDS,
    timeout_s: float = CRITIC_TIMEOUT_SECONDS,
) -> CriticReviewOutcome:
    """Run the critic with the AC #4 dissent protocol.

    * 1st pass → return immediately, ``escalated=False``.
    * 1st dissent → call ``coder_retry(verdict)`` to obtain a revised
      diff, then re-run the critic.
    * 2nd dissent → return ``escalated=True`` (no further retry).
    """
    history: list[CriticVerdict] = []
    current_diff = diff
    prior: CriticVerdict | None = None

    for round_idx in range(max_dissent_rounds + 1):
        verdict = await review_once(
            backend,
            diff=current_diff,
            ac_text=ac_text,
            model=model,
            timeout_s=timeout_s,
            prior_dissent=prior,
        )
        history.append(verdict)
        if verdict.verdict == "pass":
            return CriticReviewOutcome(
                final_verdict=verdict,
                history=tuple(history),
                escalated=False,
            )
        if round_idx == max_dissent_rounds:
            return CriticReviewOutcome(
                final_verdict=verdict,
                history=tuple(history),
                escalated=True,
            )
        try:
            current_diff = await coder_retry(verdict)
        except Exception as exc:  # noqa: BLE001 — coder failure → escalate
            log.warning(
                "coder retry raised %s — escalating critic dissent loop",
                type(exc).__name__,
            )
            return CriticReviewOutcome(
                final_verdict=verdict,
                history=tuple(history),
                escalated=True,
            )
        prior = verdict

    raise RuntimeError("review_with_dissent_protocol exhausted loop unexpectedly")


# ── Governance guard (AC #6) ──────────────────────────────────────────


GERRIT_REVIEW_LABELS_FORBIDDEN: frozenset[str] = frozenset(
    {"Code-Review", "Verified"}
)


def assert_no_gerrit_review_label(payload: dict[str, Any]) -> None:
    """Reject any Gerrit-bound payload that would set a review label.

    Critical guard for AC #6 / F9 (ADR-0003): the critic is *never* an
    AI Reviewer. The launcher routes any critic-derived comment that
    might be posted to Gerrit through this check; a forbidden label
    raises :class:`GovernanceViolationError` and halts the path.
    """
    labels = payload.get("labels") if isinstance(payload, dict) else None
    if not isinstance(labels, dict):
        return
    forbidden_present = sorted(
        label for label in labels if label in GERRIT_REVIEW_LABELS_FORBIDDEN
    )
    if forbidden_present:
        raise GovernanceViolationError(
            "critic-derived payload tried to set Gerrit review label(s) "
            f"{forbidden_present!r} — F9 / ADR-0003 violation."
        )
