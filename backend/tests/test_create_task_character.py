"""RPG un-weld (c) — orchestrator create_task can dispatch to a character."""
from __future__ import annotations

import asyncio

import backend.jira_adapter as ja
from backend.agents import tools


class _Ref:
    ticket = "OP-TEST"
    url = "http://x/OP-TEST"


class _CapturingAdapter:
    def __init__(self):
        self.labels = None

    async def create_story(self, *, summary, description, labels, priority):
        self.labels = labels
        return _Ref()


def _run(monkeypatch_labels_holder, **kwargs):
    ad = _CapturingAdapter()
    orig = ja.build_default_jira_adapter
    ja.build_default_jira_adapter = lambda: ad
    try:
        out = asyncio.run(tools.create_task.coroutine("Title", "why", "backend", **kwargs))
    finally:
        ja.build_default_jira_adapter = orig
    return out, ad.labels


def test_no_character_is_gated_unchanged():
    out, labels = _run(None)
    assert "GATED" in out
    assert "requires:operator-approval" in labels
    assert not any(l.startswith("class:") for l in labels)
    assert not any(l.startswith("character:") for l in labels)


def test_character_dispatches_with_class_and_tier():
    out, labels = _run(None, character="nova")
    assert "DISPATCHED" in out
    assert "character:nova" in labels
    assert "class:subscription-claude" in labels          # derived brain
    assert "tier:M" in labels                              # default M (<= nova ceiling L)
    assert "capability:enable=gerrit_push" in labels       # subscription-* pushes
    assert "requires:operator-approval" not in labels      # dispatched, not gated


def test_rex_defaults_to_its_ceiling_S():
    _out, labels = _run(None, character="rex")
    assert "tier:S" in labels


def test_in_guild_skill_is_added():
    _out, labels = _run(None, character="nova", skill="enterprise_web")
    assert "skill:enterprise_web" in labels


def test_off_guild_skill_rejected():
    out, labels = _run(None, character="nova", skill="uvc")
    assert out.startswith("[ERROR]") and "guild" in out
    assert labels is None  # never filed


def test_tier_over_ceiling_rejected():
    out, labels = _run(None, character="rex", tier="L")
    assert out.startswith("[ERROR]") and "ceiling" in out
    assert labels is None


def test_unknown_character_rejected():
    out, labels = _run(None, character="ghost")
    assert out.startswith("[ERROR]") and "unknown character" in out
    assert labels is None
