"""Conversation message-sequence hygiene (OP-2530 prod hotfix).

Newer Anthropic models (Sonnet 4.6 / Opus 4.8 / Fable 5) reject an assistant
"prefill" — a message list ending in a non-user message → 400 "The conversation
must end with a user message." A router node can leave a trailing AIMessage on
the state; the old prod default (Sonnet 4.5) tolerated it, the P2-routed models
do not, which broke Sora's chat. `_trim_to_last_user_turn` guarantees the LLM
sees the user's turn last. See backend/agents/nodes.py.
"""

from types import SimpleNamespace

from backend.agents.nodes import _trim_to_last_user_turn


def _msg(cls_name: str):
    return type(cls_name, (), {})()


def _names(msgs):
    return [m.__class__.__name__ for m in msgs]


def test_trims_trailing_assistant_message():
    h, a = _msg("HumanMessage"), _msg("AIMessage")
    assert _names(_trim_to_last_user_turn([h, a])) == ["HumanMessage"]


def test_trims_multiple_trailing_non_user():
    h, a, t = _msg("HumanMessage"), _msg("AIMessage"), _msg("ToolMessage")
    assert _names(_trim_to_last_user_turn([h, a, t])) == ["HumanMessage"]


def test_keeps_history_up_to_last_user():
    h1, a1, h2 = _msg("HumanMessage"), _msg("AIMessage"), _msg("HumanMessage")
    assert _names(_trim_to_last_user_turn([h1, a1, h2])) == [
        "HumanMessage", "AIMessage", "HumanMessage",
    ]


def test_noop_when_ends_with_user():
    h = _msg("HumanMessage")
    assert _names(_trim_to_last_user_turn([h])) == ["HumanMessage"]


def test_unchanged_when_no_user_message():
    a = _msg("AIMessage")
    # No user turn at all → return as-is (don't empty the list).
    assert _names(_trim_to_last_user_turn([a])) == ["AIMessage"]


def test_empty_list():
    assert _trim_to_last_user_turn([]) == []
