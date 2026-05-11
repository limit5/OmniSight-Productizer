"""B4 (OP-833) — Pre-commit Critic tests.

8 cases per master-plan §2.6 test plan:

1. ``test_pass_verdict_returns_pass_outcome`` — critic emits valid pass.
2. ``test_dissent_then_retry_pass`` — 1st dissent → coder retry → pass.
3. ``test_two_dissents_escalate`` — 2nd dissent triggers escalation.
4. ``test_critic_timeout_falls_back_to_pass`` — 30s timeout → pass+audit.
5. ``test_critic_oom_falls_back_to_pass`` — MemoryError → pass+audit.
6. ``test_malformed_reason_code_falls_back_to_pass`` — invalid JSON.
7. ``test_governance_guard_blocks_gerrit_review_label`` — AC #6.
8. ``test_critic_tool_failure_falls_back_to_pass`` — generic tool error.

All cases use mock backends; no real Anthropic API calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.agents.critic_agent import (
    CriticVerdict,
    GERRIT_REVIEW_LABELS_FORBIDDEN,
    GovernanceViolationError,
    assert_no_gerrit_review_label,
    resolve_critic_model,
    review_once,
    review_with_dissent_protocol,
)
from backend.agents.critic_reason_codes import (
    VALID_REASON_CODES,
    CriticReasonCode,
)


# ── Fixtures / helpers ────────────────────────────────────────────────


def _verdict_json(
    verdict: str,
    reason_code: str = "logic_bug",
    reason_text: str = "missing null check",
) -> str:
    return (
        "Reviewed the diff against AC.\n\n"
        f'{json.dumps({"verdict": verdict, "reason_code": reason_code, "reason_text": reason_text})}'
    )


class _ScriptedBackend:
    """Returns scripted responses; tracks call count + last prompt."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, *, prompt: str, model: str) -> str:
        self.calls.append({"prompt": prompt, "model": model})
        if not self._responses:
            raise RuntimeError("test ran out of scripted responses")
        return self._responses.pop(0)


class _RaisingBackend:
    """Always raises the supplied exception class."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    async def invoke(self, *, prompt: str, model: str) -> str:
        self.calls += 1
        raise self._exc


class _SlowBackend:
    """Sleeps longer than the timeout to exercise the asyncio.wait_for path."""

    def __init__(self, sleep_s: float) -> None:
        self._sleep_s = sleep_s
        self.calls = 0

    async def invoke(self, *, prompt: str, model: str) -> str:
        self.calls += 1
        await asyncio.sleep(self._sleep_s)
        return _verdict_json("pass", "other", "ignored — should not arrive")


@pytest.fixture
def ac_text() -> str:
    return (
        "1. Function returns sum.\n"
        "2. Handles None gracefully.\n"
        "3. Tests cover the None branch."
    )


@pytest.fixture
def diff() -> str:
    return (
        "diff --git a/foo.py b/foo.py\n"
        "+def add(a, b):\n"
        "+    return a + b\n"
    )


# ── 1. Pass verdict ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pass_verdict_returns_pass_outcome(diff: str, ac_text: str) -> None:
    backend = _ScriptedBackend(
        [_verdict_json("pass", "other", "AC fully met")],
    )
    outcome = await review_with_dissent_protocol(
        backend,
        diff=diff,
        ac_text=ac_text,
        coder_retry=_unused_coder_retry,
    )
    assert outcome.passed is True
    assert outcome.escalated is False
    assert outcome.final_verdict.verdict == "pass"
    assert outcome.final_verdict.reason_code is CriticReasonCode.OTHER
    assert len(outcome.history) == 1
    assert len(backend.calls) == 1


# ── 2. Dissent + 1st-retry pass ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dissent_then_retry_pass(diff: str, ac_text: str) -> None:
    backend = _ScriptedBackend(
        [
            _verdict_json("dissent", "test_inadequacy", "no None test case"),
            _verdict_json("pass", "test_inadequacy", "None case now covered"),
        ],
    )
    revised_diff = diff + "+def test_none(): assert add(None, 1) is None\n"
    retries: list[CriticVerdict] = []

    async def _retry(verdict: CriticVerdict) -> str:
        retries.append(verdict)
        return revised_diff

    outcome = await review_with_dissent_protocol(
        backend,
        diff=diff,
        ac_text=ac_text,
        coder_retry=_retry,
    )

    assert outcome.passed is True
    assert outcome.escalated is False
    assert len(outcome.history) == 2
    assert outcome.history[0].verdict == "dissent"
    assert outcome.history[0].reason_code is CriticReasonCode.TEST_INADEQUACY
    assert outcome.history[1].verdict == "pass"
    # Retry callback was invoked exactly once with the dissent verdict.
    assert len(retries) == 1
    assert retries[0].reason_code is CriticReasonCode.TEST_INADEQUACY
    # 2nd critic call must include prior-dissent context per AC #4.
    assert "prior critic round dissented" in backend.calls[1]["prompt"]
    # 2nd critic prompt must reference the revised diff, not the original.
    assert "test_none" in backend.calls[1]["prompt"]


# ── 3. Dissent + 2nd-retry escalate ──────────────────────────────────


@pytest.mark.asyncio
async def test_two_dissents_escalate(diff: str, ac_text: str) -> None:
    backend = _ScriptedBackend(
        [
            _verdict_json("dissent", "logic_bug", "off-by-one"),
            _verdict_json("dissent", "logic_bug", "still off-by-one after retry"),
        ],
    )

    async def _retry(verdict: CriticVerdict) -> str:
        return diff + "+# tried fixing\n"

    outcome = await review_with_dissent_protocol(
        backend,
        diff=diff,
        ac_text=ac_text,
        coder_retry=_retry,
    )
    assert outcome.passed is False
    assert outcome.escalated is True
    assert len(outcome.history) == 2
    assert outcome.final_verdict.verdict == "dissent"
    assert outcome.final_verdict.reason_code is CriticReasonCode.LOGIC_BUG


# ── 4. Critic timeout → pass + audit ────────────────────────────────


@pytest.mark.asyncio
async def test_critic_timeout_falls_back_to_pass(diff: str, ac_text: str) -> None:
    backend = _SlowBackend(sleep_s=2.0)
    verdict = await review_once(
        backend,
        diff=diff,
        ac_text=ac_text,
        timeout_s=0.05,
    )
    assert verdict.verdict == "pass"
    assert verdict.audit.get("failure_code") == "critic_timeout"
    assert verdict.audit.get("fallback") is True
    assert verdict.reason_code is CriticReasonCode.OTHER
    assert "infrastructure_failure:critic_timeout" in verdict.reason_text


# ── 5. Critic OOM → pass + audit ────────────────────────────────────


@pytest.mark.asyncio
async def test_critic_oom_falls_back_to_pass(diff: str, ac_text: str) -> None:
    backend = _RaisingBackend(MemoryError("simulated oom"))
    verdict = await review_once(backend, diff=diff, ac_text=ac_text)
    assert verdict.verdict == "pass"
    assert verdict.audit.get("failure_code") == "critic_oom"
    assert verdict.audit.get("fallback") is True


# ── 6. Malformed reason_code → pass + audit ─────────────────────────


@pytest.mark.asyncio
async def test_malformed_reason_code_falls_back_to_pass(
    diff: str, ac_text: str,
) -> None:
    # Invalid reason_code "definitely_not_an_enum_value".
    backend = _ScriptedBackend(
        [_verdict_json("dissent", "definitely_not_an_enum_value", "junk")],
    )
    verdict = await review_once(backend, diff=diff, ac_text=ac_text)
    assert verdict.verdict == "pass"
    assert verdict.audit.get("failure_code") == "critic_malformed_reason_code"

    # Also test: response with no JSON envelope at all.
    backend2 = _ScriptedBackend(["I think the diff looks fine, no JSON here"])
    verdict2 = await review_once(backend2, diff=diff, ac_text=ac_text)
    assert verdict2.verdict == "pass"
    assert verdict2.audit.get("failure_code") == "critic_malformed_reason_code"

    # And: invalid `verdict` value.
    backend3 = _ScriptedBackend(
        ['{"verdict": "maybe", "reason_code": "logic_bug", "reason_text": "x"}'],
    )
    verdict3 = await review_once(backend3, diff=diff, ac_text=ac_text)
    assert verdict3.verdict == "pass"
    assert verdict3.audit.get("failure_code") == "critic_malformed_reason_code"


# ── 7. Governance guard — AC #6 ─────────────────────────────────────


def test_governance_guard_blocks_gerrit_review_label() -> None:
    """assert_no_gerrit_review_label must reject any payload that would
    set Code-Review or Verified — the critic NEVER posts to Gerrit
    (F9 / ADR-0003)."""
    # Forbidden labels are exactly the Gerrit review labels.
    assert "Code-Review" in GERRIT_REVIEW_LABELS_FORBIDDEN
    assert "Verified" in GERRIT_REVIEW_LABELS_FORBIDDEN

    # Plain message-only payload is accepted.
    assert_no_gerrit_review_label({"message": "looks good"})

    # Code-Review label is rejected.
    with pytest.raises(GovernanceViolationError) as excinfo:
        assert_no_gerrit_review_label(
            {"message": "looks good", "labels": {"Code-Review": 1}},
        )
    assert "Code-Review" in str(excinfo.value)

    # Verified label is rejected.
    with pytest.raises(GovernanceViolationError):
        assert_no_gerrit_review_label({"labels": {"Verified": -1}})


def test_governance_guard_no_gerrit_call_in_review_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing/using the critic must NOT pull in any Gerrit client.

    Defence in depth for AC #6: track whether anything in the critic
    code path ever calls into ``jira_dispatch``-side Gerrit helpers.
    """
    import sys
    forbidden_attrs = (
        "push_to_gerrit_for_review",
        "ensure_change_ids",
        "_GERRIT_AUTH_BY_CLASS",
    )
    # The critic_agent module import has already happened at the top of
    # this test file — confirm no Gerrit symbols leaked into its globals.
    critic_mod = sys.modules["backend.agents.critic_agent"]
    for attr in forbidden_attrs:
        assert not hasattr(critic_mod, attr), (
            f"critic_agent leaked Gerrit symbol {attr!r} — F9 violation"
        )


# ── 8. Critic tool failure → pass + audit ──────────────────────────


@pytest.mark.asyncio
async def test_critic_tool_failure_falls_back_to_pass(
    diff: str, ac_text: str,
) -> None:
    backend = _RaisingBackend(RuntimeError("pytest hung — sandbox killed it"))
    verdict = await review_once(backend, diff=diff, ac_text=ac_text)
    assert verdict.verdict == "pass"
    assert verdict.audit.get("failure_code") == "critic_tool_failure"
    assert "RuntimeError" in verdict.audit.get("detail", "")


# ── Auxiliary checks (model env override, footer, JIRA comment) ────


def test_env_var_overrides_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNISIGHT_CRITIC_MODEL", "claude-sonnet-4-6")
    assert resolve_critic_model() == "claude-sonnet-4-6"
    monkeypatch.delenv("OMNISIGHT_CRITIC_MODEL", raising=False)
    assert resolve_critic_model() == "claude-haiku-4-5"


def test_verdict_to_commit_footer_pass() -> None:
    v = CriticVerdict(
        verdict="pass",
        reason_code=CriticReasonCode.OTHER,
        reason_text="ok",
    )
    assert v.to_commit_footer() == "Critic-Verdict: pass (other)"


def test_verdict_to_commit_footer_dissent_includes_reason() -> None:
    v = CriticVerdict(
        verdict="dissent",
        reason_code=CriticReasonCode.AC_DRIFT,
        reason_text="implements AC #1 not AC #2",
    )
    footer = v.to_commit_footer()
    assert "Critic-Verdict: dissent" in footer
    assert "ac_drift" in footer
    assert "AC #2" in footer


def test_reason_code_enum_matches_spec() -> None:
    """AC #3: closed enum {missing_context, style_violation, logic_bug,
    test_inadequacy, ac_drift, governance_violation, other}."""
    assert VALID_REASON_CODES == frozenset({
        "missing_context",
        "style_violation",
        "logic_bug",
        "test_inadequacy",
        "ac_drift",
        "governance_violation",
        "other",
    })


# ── Helpers ─────────────────────────────────────────────────────────


async def _unused_coder_retry(verdict: CriticVerdict) -> str:
    raise AssertionError(
        "coder_retry must not be invoked when first round passes",
    )
