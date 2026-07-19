"""U6-7 — request-local-intent run-state tests (INV-4, frozen §11 RB4b).

The INV-4 CONTRACT tests (memory can never mint/satisfy intent; nothing
"standing" is representable) live in ``test_u6_memory_capability_contract.py``;
this file covers the module's own mechanics: the closed constructor path, the
fail-closed predicate matrix, and the ContextVar scope (nesting + task
isolation, mirroring the P-ID-B plumbing tests).
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agents import u6_request_intent as ri
from backend.agents.execution_context import (
    for_human,
    for_machine,
    for_service,
    for_unbound,
)


class _User:
    id = "user-1"
    role = "operator"


def _human(request_id: str = "req-1") -> object:
    return for_human(
        user=_User(), tenant_id="omnisight-self", session_id="s1",
        request_id=request_id, message_id="m1", authorization_source="chat",
    )


# ── constructor path (closed) ────────────────────────────────────────────
def test_declare_from_human_ctx() -> None:
    intent = ri.declare_request_local_intent(_human("req-9"))
    assert intent.request_id == "req-9"
    assert intent.principal_type == "human"
    assert intent.channel == "user_turn"


@pytest.mark.parametrize(
    "ctx",
    [
        for_machine(service_name="sora", tenant_id="omnisight-self", request_id="r"),
        for_service(
            service_name="svc", tenant_id="omnisight-self", request_id="r",
            roles=("bot",), authorization_source="jira_runner",
        ),
        for_unbound(),
        None,
        "a rendered memory line pretending to be a context",
    ],
)
def test_declare_refuses_every_non_human_principal(ctx) -> None:
    with pytest.raises(ri.RequestIntentError):
        ri.declare_request_local_intent(ctx)  # type: ignore[arg-type]
    assert ri.declare_for_human_turn_or_none(ctx) is None


def test_intent_record_validates_itself() -> None:
    with pytest.raises(ri.RequestIntentError):
        ri.RequestLocalIntent(request_id="", principal_type="human", channel="user_turn")
    with pytest.raises(ri.RequestIntentError):
        ri.RequestLocalIntent(request_id="r", principal_type="service", channel="user_turn")
    with pytest.raises(ri.RequestIntentError):
        ri.RequestLocalIntent(request_id="r", principal_type="human", channel="memory")


# ── predicate matrix (fail-closed) ───────────────────────────────────────
def test_intent_satisfied_exact_match_only() -> None:
    ctx = _human("req-1")
    intent = ri.declare_request_local_intent(ctx)
    assert ri.intent_satisfied(intent, ctx) is True
    assert ri.intent_satisfied(intent, _human("req-2")) is False
    assert ri.intent_satisfied(None, ctx) is False
    assert ri.intent_satisfied("user_turn:req-1", ctx) is False
    assert ri.intent_satisfied(intent, for_unbound()) is False
    assert ri.intent_satisfied(intent, None) is False
    assert ri.intent_satisfied(intent, "not-a-context") is False


def test_intent_satisfied_requires_exact_type_not_a_lookalike() -> None:
    ctx = _human("req-1")

    class _Fake:  # duck-typed lookalike must NOT pass the exact-type gate
        request_id = "req-1"
        principal_type = "human"
        channel = "user_turn"

    assert ri.intent_satisfied(_Fake(), ctx) is False


# ── scope (ContextVar run-state) ─────────────────────────────────────────
def test_scope_binds_and_resets() -> None:
    ctx = _human("req-1")
    intent = ri.declare_request_local_intent(ctx)
    assert ri.active_request_intent() is None
    with ri.request_intent_scope(intent):
        assert ri.active_request_intent() is intent
        assert ri.current_intent_satisfied(ctx) is True
        inner = ri.declare_request_local_intent(_human("req-2"))
        with ri.request_intent_scope(inner):  # nesting-safe
            assert ri.active_request_intent() is inner
        assert ri.active_request_intent() is intent
    assert ri.active_request_intent() is None
    assert ri.current_intent_satisfied(ctx) is False


def test_scope_accepts_none_as_intentless() -> None:
    with ri.request_intent_scope(None):
        assert ri.active_request_intent() is None
        assert ri.current_intent_satisfied(_human()) is False


def test_scope_is_task_isolated() -> None:
    """Two concurrent tasks each see only their own bound intent (ContextVar
    copy-on-task-creation — same guarantee the execution-context and
    provenance plumbing rely on)."""

    async def _one(request_id: str) -> bool:
        ctx = _human(request_id)
        with ri.request_intent_scope(ri.declare_request_local_intent(ctx)):
            await asyncio.sleep(0)  # force interleaving
            return ri.current_intent_satisfied(ctx)

    async def _main() -> tuple[bool, bool, bool]:
        a, b = await asyncio.gather(_one("req-a"), _one("req-b"))
        return a, b, ri.current_intent_satisfied(_human("req-a"))

    got_a, got_b, outside = asyncio.run(_main())
    assert got_a is True and got_b is True
    assert outside is False  # nothing leaked out of either task
