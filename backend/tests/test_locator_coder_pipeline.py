"""B7 (OP-839) — Locator → Coder pipeline integration tests.

14 cases per master-plan §2.9 test plan:

 1. ``test_few_candidates_happy_path`` — locator returns 1-N files,
    coder runs, ``status="ok"`` (AC #1/#2/#3).
 2. ``test_zero_candidates_fallback_monolithic`` — AC #5.
 3. ``test_too_many_candidates_escalates_refinement`` — AC #6.
 4. ``test_locator_timeout_fallback_monolithic`` — AC #7.
 5. ``test_locator_malformed_retry_succeeds`` — AC #4 retry path.
 6. ``test_locator_malformed_twice_terminal`` — AC #4 retry-exhausted.
 7. ``test_locator_handoff_pollution_rejects`` — AC #4 pollution path.
 8. ``test_coder_rejects_locator_input_expands_then_passes`` — error
    catalog ``coder_rejects_locator_input`` happy expansion.
 9. ``test_coder_rejects_after_expansion_fallback_monolithic`` —
    expansion fails, surrender to monolithic.
10. ``test_peer_conflict_detected`` — AC #8 / F6/F7/F12.
11. ``test_prior_ps_rebase_detected`` — AC #9 / F11.
12. ``test_monolithic_fallback_runs_full_repo_sonnet`` — AC #5/#7
    fallback executor (``run_monolithic_coder``).
13. ``test_coder_prompt_isolates_handoff_no_tool_history`` — AC #3
    context-isolation invariant; the focused-coder prompt MUST NOT
    contain any locator tool-call transcript markers.
14. ``test_locator_tools_are_read_only`` — AC #1 governance guard.

All cases use scripted backends; no Anthropic API call, no Gerrit
SSH. Pipeline runs are < 50 ms each.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.agents.coder_agent import (
    CODER_TOOLS,
    INSUFFICIENT_CONTEXT_SENTINEL,
    CoderBackendResult,
    CoderRunRequest,
    build_focused_coder_prompt,
    coder_prompt_contains_only_handoff_and_ac,
    detect_insufficient_context,
    run_focused_coder,
    run_monolithic_coder,
)
from backend.agents.locator_agent import (
    LOCATOR_TOOLS,
    PeerConflict,
    PriorPS,
    locate,
)
from backend.agents.locator_handoff_schema import (
    SCHEMA_VERSION,
    LocatorHandoff,
    parse_locator_handoff,
)


# ── Scripted backends ────────────────────────────────────────────────


def _envelope(
    *,
    files: list | None = None,
    confidence: float = 0.8,
    summary: str = "Test handoff summary.",
    hypotheses: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "files": files
            if files is not None
            else [
                {
                    "path": "backend/agents/foo.py",
                    "line_ranges": [[1, 50]],
                    "why_relevant": "primary impl",
                }
            ],
            "hypotheses": hypotheses or ["bug in foo"],
            "confidence": confidence,
            "summary": summary,
        }
    )


class _ScriptedLocatorBackend:
    """Returns scripted responses; records invocation args."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.invocations: list[dict[str, Any]] = []

    async def invoke(
        self,
        *,
        prompt: str,
        model: str,
        tools: tuple[str, ...],
        timeout_s: float,
    ) -> str:
        self.invocations.append(
            {"prompt": prompt, "model": model, "tools": tools, "timeout_s": timeout_s}
        )
        if not self._responses:
            raise RuntimeError("locator backend ran out of scripted responses")
        return self._responses.pop(0)


class _SlowLocatorBackend:
    """Sleeps longer than the timeout so asyncio.wait_for fires."""

    def __init__(self, sleep_s: float) -> None:
        self._sleep_s = sleep_s

    async def invoke(
        self,
        *,
        prompt: str,
        model: str,
        tools: tuple[str, ...],
        timeout_s: float,
    ) -> str:
        await asyncio.sleep(self._sleep_s)
        return _envelope()


class _ScriptedCoderBackend:
    """Coder backend that returns scripted final-text + tool counts."""

    def __init__(self, responses: list[CoderBackendResult]) -> None:
        self._responses = list(responses)
        self.requests: list[CoderRunRequest] = []

    async def run(
        self,
        *,
        request: CoderRunRequest,
        timeout_s: float,
    ) -> CoderBackendResult:
        self.requests.append(request)
        if not self._responses:
            raise RuntimeError("coder backend ran out of scripted responses")
        return self._responses.pop(0)


@pytest.fixture
def ticket_ctx() -> dict[str, str]:
    return {
        "ticket_key": "OP-839",
        "ticket_summary": "B7 locator → coder pipeline",
        "ac_text": "1. Locator returns JSON envelope.\n2. Coder reads only handoff.",
    }


# ── 1. Happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_few_candidates_happy_path(ticket_ctx):
    backend = _ScriptedLocatorBackend([_envelope()])
    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        timeout_s=5.0,
    )
    assert outcome.status == "ok"
    assert outcome.handoff is not None
    assert outcome.handoff.candidate_count == 1
    assert outcome.is_terminal_for_coder is False
    # AC #1: Haiku model + read-only tool set.
    assert backend.invocations[0]["model"].startswith("claude-haiku")
    assert backend.invocations[0]["tools"] == LOCATOR_TOOLS


# ── 2. Zero candidates → monolithic fallback ────────────────────────


@pytest.mark.asyncio
async def test_zero_candidates_fallback_monolithic(ticket_ctx):
    backend = _ScriptedLocatorBackend([_envelope(files=[])])
    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        timeout_s=5.0,
    )
    assert outcome.status == "fallback_monolithic"
    assert outcome.fallback_reason == "locator_zero_candidates"
    assert outcome.handoff is not None
    assert outcome.handoff.is_zero_candidates is True


# ── 3. >50 candidates → needs:refinement ────────────────────────────


@pytest.mark.asyncio
async def test_too_many_candidates_escalates_refinement(ticket_ctx):
    files = [
        {
            "path": f"backend/x{i}.py",
            "line_ranges": [[1, 5]],
            "why_relevant": "x",
        }
        for i in range(51)
    ]
    backend = _ScriptedLocatorBackend([_envelope(files=files)])
    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        timeout_s=5.0,
    )
    assert outcome.status == "needs_refinement"
    assert outcome.is_terminal_for_coder is True
    assert outcome.handoff is not None
    assert outcome.handoff.candidate_count == 51


# ── 4. Locator timeout → monolithic ─────────────────────────────────


@pytest.mark.asyncio
async def test_locator_timeout_fallback_monolithic(ticket_ctx):
    backend = _SlowLocatorBackend(sleep_s=2.0)
    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        timeout_s=0.05,
    )
    assert outcome.status == "fallback_monolithic"
    assert outcome.fallback_reason == "locator_timeout"
    assert "locator_timeout" in outcome.history


# ── 5. Malformed → retry once → pass ───────────────────────────────


@pytest.mark.asyncio
async def test_locator_malformed_retry_succeeds(ticket_ctx):
    backend = _ScriptedLocatorBackend(
        [
            "garbage not json at all",
            _envelope(),
        ]
    )
    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        timeout_s=5.0,
    )
    assert outcome.status == "ok"
    assert "locator_malformed_handoff" in outcome.history
    assert len(backend.invocations) == 2
    # The retry attempt MUST include the strict-format reminder.
    assert "STRICT FORMAT REMINDER" in backend.invocations[1]["prompt"]


# ── 6. Malformed twice → terminal ──────────────────────────────────


@pytest.mark.asyncio
async def test_locator_malformed_twice_terminal(ticket_ctx):
    backend = _ScriptedLocatorBackend(
        ["garbage 1", "still garbage"]
    )
    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        timeout_s=5.0,
    )
    assert outcome.status == "malformed_handoff"
    # Two malformed entries in history (both attempts failed).
    assert outcome.history.count("locator_malformed_handoff") == 2


# ── 7. Pollution → terminal (no retry) ─────────────────────────────


@pytest.mark.asyncio
async def test_locator_handoff_pollution_rejects(ticket_ctx):
    # ~10 KB envelope blows past the 8 KB budget.
    bloated = '{"files": ' + ("x" * 9000) + "}"
    backend = _ScriptedLocatorBackend([bloated])
    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        timeout_s=5.0,
    )
    assert outcome.status == "handoff_pollution"
    assert outcome.fallback_reason == "locator_handoff_pollution"
    # Pollution is NOT retried per AC #4 — only one backend call.
    assert len(backend.invocations) == 1


# ── 8. Coder rejects → expand candidates → pass ────────────────────


@pytest.mark.asyncio
async def test_coder_rejects_locator_input_expands_then_passes(ticket_ctx):
    handoff = parse_locator_handoff(_envelope())
    expanded_handoff = parse_locator_handoff(
        _envelope(
            files=[
                {
                    "path": "backend/agents/foo.py",
                    "line_ranges": [[1, 50]],
                    "why_relevant": "primary",
                },
                {
                    "path": "backend/agents/bar.py",
                    "line_ranges": [[1, 30]],
                    "why_relevant": "expanded peer",
                },
            ],
            summary="Expanded handoff.",
        )
    )
    coder = _ScriptedCoderBackend(
        [
            CoderBackendResult(
                final_text=f"Reading files...\n{INSUFFICIENT_CONTEXT_SENTINEL}",
                tool_calls_made=1,
            ),
            CoderBackendResult(
                final_text="Done editing backend/agents/foo.py and bar.py.",
                tool_calls_made=4,
            ),
        ]
    )

    async def expander(_h: LocatorHandoff) -> LocatorHandoff:
        return expanded_handoff

    outcome = await run_focused_coder(
        coder,
        handoff=handoff,
        ac_text=ticket_ctx["ac_text"],
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        candidate_expander=expander,
        timeout_s=5.0,
    )
    assert outcome.status == "ok"
    assert outcome.retries_used == 1
    assert "coder_rejects_locator_input" in outcome.history
    # 2nd prompt must reference the expanded file (bar.py).
    assert "bar.py" in coder.requests[1].prompt


# ── 9. Coder rejects twice → monolithic fallback signal ───────────


@pytest.mark.asyncio
async def test_coder_rejects_after_expansion_fallback_monolithic(ticket_ctx):
    handoff = parse_locator_handoff(_envelope())
    coder = _ScriptedCoderBackend(
        [
            CoderBackendResult(
                final_text=INSUFFICIENT_CONTEXT_SENTINEL,
                tool_calls_made=0,
            ),
            CoderBackendResult(
                final_text=f"Tried expansion, still {INSUFFICIENT_CONTEXT_SENTINEL}",
                tool_calls_made=0,
            ),
        ]
    )

    async def expander(h: LocatorHandoff) -> LocatorHandoff:
        return h  # no real expansion possible

    outcome = await run_focused_coder(
        coder,
        handoff=handoff,
        ac_text=ticket_ctx["ac_text"],
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        candidate_expander=expander,
        timeout_s=5.0,
    )
    assert outcome.status == "fallback_monolithic_required"
    assert outcome.needs_monolithic_fallback is True
    assert outcome.fallback_reason == "coder_rejects_locator_input"
    # Retried once (the expansion attempt).
    assert outcome.retries_used == 2


# ── 10. Peer conflict detected (AC #8 / F6/F7/F12) ─────────────────


@pytest.mark.asyncio
async def test_peer_conflict_detected(ticket_ctx):
    backend = _ScriptedLocatorBackend([_envelope()])

    def peer_probe(ticket: str, path: str) -> list[PeerConflict]:
        if path == "backend/agents/foo.py":
            return [
                PeerConflict(
                    file_path=path,
                    peer_ticket="OP-9999",
                    peer_change_number=1234,
                )
            ]
        return []

    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        peer_probe=peer_probe,
        timeout_s=5.0,
    )
    assert outcome.status == "peer_conflict"
    assert outcome.is_terminal_for_coder is True
    assert len(outcome.peer_conflicts) == 1
    assert outcome.peer_conflicts[0].peer_ticket == "OP-9999"


@pytest.mark.asyncio
async def test_peer_conflict_same_ticket_is_not_flagged(ticket_ctx):
    """Defence: a hit where peer_ticket == current ticket is OUR own
    in-flight PS, not a peer conflict — must NOT escalate (AC #8)."""
    backend = _ScriptedLocatorBackend([_envelope()])

    def peer_probe(ticket: str, path: str) -> list[PeerConflict]:
        return [
            PeerConflict(
                file_path=path,
                peer_ticket=ticket,  # same as current
                peer_change_number=42,
            )
        ]

    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        peer_probe=peer_probe,
        timeout_s=5.0,
    )
    assert outcome.status == "ok"


# ── 11. Prior-PS rebase detected (AC #9 / F11) ─────────────────────


@pytest.mark.asyncio
async def test_prior_ps_rebase_detected(ticket_ctx):
    backend = _ScriptedLocatorBackend([_envelope()])  # never reached

    def prior_ps_probe(change_id: str) -> PriorPS:
        return PriorPS(
            change_id=change_id,
            change_number=777,
            change_url="https://gerrit/c/777",
        )

    outcome = await locate(
        backend,
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        ac_text=ticket_ctx["ac_text"],
        change_id="I" + "a" * 40,
        prior_ps_probe=prior_ps_probe,
        timeout_s=5.0,
    )
    assert outcome.status == "prior_ps_exists"
    assert outcome.is_terminal_for_coder is True
    assert outcome.prior_ps is not None
    assert outcome.prior_ps.change_number == 777
    # Locator backend MUST NOT have been called — prior-PS short-circuits.
    assert backend.invocations == []


# ── 12. Monolithic coder fallback ──────────────────────────────────


@pytest.mark.asyncio
async def test_monolithic_fallback_runs_full_repo_sonnet(ticket_ctx):
    coder = _ScriptedCoderBackend(
        [
            CoderBackendResult(
                final_text="Edited 3 files in full-repo mode.",
                tool_calls_made=12,
            )
        ]
    )
    outcome = await run_monolithic_coder(
        coder,
        ac_text=ticket_ctx["ac_text"],
        ticket_key=ticket_ctx["ticket_key"],
        ticket_summary=ticket_ctx["ticket_summary"],
        fallback_reason="locator_zero_candidates",
        timeout_s=5.0,
    )
    assert outcome.status == "ok"
    assert outcome.history == ("monolithic:locator_zero_candidates",)
    # Monolithic prompt MUST NOT carry a locator JSON envelope.
    prompt = coder.requests[0].prompt
    assert SCHEMA_VERSION not in prompt
    assert '"files":' not in prompt
    # And it MUST include the AC verbatim.
    assert ticket_ctx["ac_text"].split("\n")[0] in prompt


# ── 13. Context-isolation invariant (AC #3) ────────────────────────


def test_coder_prompt_isolates_handoff_no_tool_history():
    """The focused-coder prompt must not contain tool-call transcript
    markers — AC #3 forbids leaking locator's tool-use history."""
    handoff = parse_locator_handoff(_envelope())
    prompt = build_focused_coder_prompt(
        handoff=handoff,
        ac_text="1. Test AC line.",
        ticket_key="OP-839",
        ticket_summary="test",
    )
    assert coder_prompt_contains_only_handoff_and_ac(
        prompt, handoff=handoff, ac_text="1. Test AC line.",
    )
    # And the guard function detects the failure mode if a caller ever
    # accidentally concatenates a tool-use transcript:
    tainted = prompt + "\n<tool_call>foo</tool_call>"
    assert not coder_prompt_contains_only_handoff_and_ac(
        tainted, handoff=handoff, ac_text="1. Test AC line.",
    )


# ── 14. Locator tool-set governance guard (AC #1) ──────────────────


def test_locator_tools_are_read_only():
    """AC #1 — Locator tools MUST be exactly the read-only set.

    Coder tools include text_editor (write), but locator must not. We
    pin the exact tuple and assert no write-capable tool leaks in.
    """
    # Frozen tuple (no Bash, no text_editor without `.view`).
    assert LOCATOR_TOOLS == ("text_editor.view", "Grep", "Glob")
    write_capable = {"text_editor", "Bash", "Write", "Edit"}
    assert not (set(LOCATOR_TOOLS) & write_capable), (
        f"locator tool-set leaked a write tool: "
        f"{set(LOCATOR_TOOLS) & write_capable}"
    )
    # Coder DOES include the write tool — counterproof that the
    # two-phase split is meaningful.
    assert "text_editor" in CODER_TOOLS


# ── Bonus: detect_insufficient_context smoke ───────────────────────


def test_detect_insufficient_context_sentinel():
    assert detect_insufficient_context(
        f"I need more files.\n{INSUFFICIENT_CONTEXT_SENTINEL}"
    )
    assert not detect_insufficient_context("All edits done.")
    # Substring inside a larger token must still trigger (the runner
    # parses on substring presence, by design).
    assert detect_insufficient_context(
        f"line one\nprefix{INSUFFICIENT_CONTEXT_SENTINEL}suffix\nline three"
    )
