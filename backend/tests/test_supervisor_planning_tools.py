"""Sora supervisor planning tool (P4, supervisor roadmap).

Covers supervisor_link_blocks: OP-* key guard, self-block refusal, correct
Blocks direction (inwardIssue=BLOCKER on the BLOCKED issue), self-verify,
idempotent no-op. Stateful fake JIRA adapter (no network).
"""

import asyncio

import pytest

import backend.jira_adapter as _ja
from backend.agents.tools import (
    SORA_PLANNING_TOOLS,
    TOOL_MAP,
    supervisor_link_blocks,
)


class _FakeAdapter:
    """Models issuelinks keyed by the BLOCKED issue key."""

    def __init__(self, fail_post=False):
        self.fail_post = fail_post
        # {blocked_key: [ {type:{name}, inwardIssue:{key: blocker}} ]}
        self.links: dict[str, list] = {}

    async def _api(self, method, path, body=None):
        if method == "GET" and "fields=issuelinks" in path:
            key = path.split("/issue/")[1].split("?")[0]
            return (200, {"fields": {"issuelinks": self.links.get(key, [])}})
        if method == "POST" and path.endswith("/issueLink"):
            if self.fail_post:
                return (500, {})
            blocker = body["inwardIssue"]["key"]
            blocked = body["outwardIssue"]["key"]
            self.links.setdefault(blocked, []).append(
                {"type": {"name": "Blocks"}, "inwardIssue": {"key": blocker}}
            )
            return (201, {})
        return (200, {})


def _patch(monkeypatch, fake):
    monkeypatch.setattr(_ja, "build_default_jira_adapter", lambda: fake)


def test_planning_tool_registered():
    for t in SORA_PLANNING_TOOLS:
        assert t.name in TOOL_MAP


@pytest.mark.parametrize("a,b", [("nope", "OP-2"), ("OP-1", "bad"), ("x", "y")])
def test_key_guard(a, b):
    out = asyncio.run(supervisor_link_blocks.ainvoke({"blocker_key": a, "blocked_key": b}))
    assert out.startswith("[SUPERVISOR] refused")


def test_self_block_refused():
    out = asyncio.run(supervisor_link_blocks.ainvoke({"blocker_key": "OP-5", "blocked_key": "OP-5"}))
    assert "cannot block itself" in out


def test_link_created_correct_direction_and_verified(monkeypatch):
    fake = _FakeAdapter()
    _patch(monkeypatch, fake)
    # OP-40 (schema) blocks OP-41 (consumer)
    out = asyncio.run(supervisor_link_blocks.ainvoke({"blocker_key": "OP-40", "blocked_key": "OP-41"}))
    assert out.startswith("[OK]") and "now blocks" in out
    # the link is recorded on the BLOCKED issue with inwardIssue = the BLOCKER
    assert fake.links["OP-41"][0]["inwardIssue"]["key"] == "OP-40"


def test_idempotent_noop_when_link_exists(monkeypatch):
    fake = _FakeAdapter()
    _patch(monkeypatch, fake)
    asyncio.run(supervisor_link_blocks.ainvoke({"blocker_key": "OP-40", "blocked_key": "OP-41"}))
    out = asyncio.run(supervisor_link_blocks.ainvoke({"blocker_key": "OP-40", "blocked_key": "OP-41"}))
    assert out.startswith("[OK]") and "no-op" in out
    assert len(fake.links["OP-41"]) == 1  # not duplicated


def test_fail_closed_on_post_error(monkeypatch):
    _patch(monkeypatch, _FakeAdapter(fail_post=True))
    out = asyncio.run(supervisor_link_blocks.ainvoke({"blocker_key": "OP-40", "blocked_key": "OP-41"}))
    assert out.startswith("[FAILED]")
