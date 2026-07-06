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


# ── cross-turn idempotency (audit r2 codex#2) ─────────────────────────
class _FakeConn:
    pass


class _FakePool:
    def acquire(self):
        class _Ctx:
            async def __aenter__(self_):
                return _FakeConn()
            async def __aexit__(self_, *a):
                return False
        return _Ctx()


def test_create_task_dedupes_same_title_same_session(monkeypatch):
    """A refresh/double-submit in the same session within the window returns the
    existing ticket instead of filing a duplicate — the adapter is NOT called."""
    from backend import db as _db

    created = {"count": 0}

    class _NoCreateAdapter:
        async def create_story(self, **kw):
            created["count"] += 1
            return _Ref()

    monkeypatch.setattr(tools, "get_chat_context",
                        lambda: {"session_id": "s1", "tenant_id": "t1", "user_id": "u1"})
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(ja, "build_default_jira_adapter", lambda: _NoCreateAdapter())

    async def _fake_find(conn, *, tenant_id, session_id, title, since):
        return {"ticket_key": "OP-DUP", "browse_url": "http://x/OP-DUP", "title": title}
    monkeypatch.setattr(_db, "find_recent_orchestrator_task_by_title", _fake_find)

    out = asyncio.run(tools.create_task.coroutine("Title", "why", "backend"))
    assert out.startswith("[OK]") and "OP-DUP" in out and "duplicate" in out
    assert created["count"] == 0        # never hit the JIRA create path


def test_create_task_files_when_no_recent_duplicate(monkeypatch):
    """No recent duplicate → normal create proceeds (dedup fails open too)."""
    from backend import db as _db

    ad = _CapturingAdapter()
    monkeypatch.setattr(tools, "get_chat_context",
                        lambda: {"session_id": "s2", "tenant_id": "t1", "user_id": "u1"})
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(ja, "build_default_jira_adapter", lambda: ad)

    async def _none(conn, **kw):
        return None
    monkeypatch.setattr(_db, "find_recent_orchestrator_task_by_title", _none)

    out = asyncio.run(tools.create_task.coroutine("Fresh", "why", "backend"))
    assert "GATED" in out
    assert ad.labels is not None        # the create actually happened
