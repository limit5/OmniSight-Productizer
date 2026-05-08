"""OP-735 R5 -- /admin/batch-merge router contract.

Validates:
  - GET /admin/batch-merge returns the in-process candidate registry,
    optionally filtered by agent_class / tier / file_glob.
  - POST /admin/batch-merge/approve fans out one Gerrit +2 per
    selected change_id and writes one ``batch_merge_approve`` audit row
    each.
  - Both endpoints are gated by ``auth.require_admin`` (CLAUDE.md L1
    invariant: human +2 only).
  - The router preserves a per-row error path (e.g. unknown change_id
    returns ``ok=False`` without aborting the whole batch).
"""

from __future__ import annotations

import inspect
import json
import time
from typing import Any

import pytest

from backend import auth


def _user(role: str) -> auth.User:
    return auth.User(
        id=f"u-{role}",
        email=f"{role}@example.com",
        name=role,
        role=role,
        tenant_id="t-default",
    )


@pytest.fixture(autouse=True)
def _clear_registry():
    from backend.routers import batch_merge as br
    br._reset_for_tests()
    yield
    br._reset_for_tests()


def _seed_candidate(**overrides: Any):
    from backend.routers.batch_merge import BatchMergeCandidate, register_candidate

    base: dict[str, Any] = {
        "change_id": "I0001",
        "project": "omnisight",
        "bot": "claude-bot",
        "file_class": "docs",
        "insertions": 12,
        "deletions": 3,
        "files": ["docs/foo.md"],
        "ai_summary": "LGTM",
        "tagged_at": time.time(),
        "revision": "abc123",
        "subject": "[OP-XXX] tweak docs",
        "agent_class": "subscription-claude",
        "tier": "S",
    }
    base.update(overrides)
    register_candidate(BatchMergeCandidate(**base))
    return base


def test_router_uses_admin_auth_dependency():
    from backend.routers import batch_merge

    list_src = inspect.getsource(batch_merge.list_batch_merge_candidates)
    approve_src = inspect.getsource(batch_merge.approve_batch_merge_candidates)

    assert "Depends(auth.require_admin)" in list_src
    assert "Depends(auth.require_admin)" in approve_src


# ── GET /admin/batch-merge ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_registered_candidates():
    from backend.routers import batch_merge

    _seed_candidate(change_id="I0001", subject="docs A", tagged_at=100.0)
    _seed_candidate(change_id="I0002", subject="docs B", tagged_at=200.0)

    res = await batch_merge.list_batch_merge_candidates(
        None, actor=_user("admin"),
    )
    body = json.loads(res.body)

    ids = [c["change_id"] for c in body["candidates"]]
    # Sorted by tagged_at desc
    assert ids == ["I0002", "I0001"]
    assert body["hashtag"] == batch_merge.BATCH_MERGE_HASHTAG
    assert body["operator"] == "admin@example.com"


@pytest.mark.asyncio
async def test_list_filters_by_agent_class():
    from backend.routers import batch_merge

    _seed_candidate(change_id="I0010", agent_class="subscription-claude")
    _seed_candidate(change_id="I0011", agent_class="subscription-codex")

    res = await batch_merge.list_batch_merge_candidates(
        None, actor=_user("admin"), agent_class="subscription-codex",
    )
    body = json.loads(res.body)
    assert [c["change_id"] for c in body["candidates"]] == ["I0011"]


@pytest.mark.asyncio
async def test_list_filters_by_tier():
    from backend.routers import batch_merge

    _seed_candidate(change_id="I0020", tier="S")
    _seed_candidate(change_id="I0021", tier="L")

    res = await batch_merge.list_batch_merge_candidates(
        None, actor=_user("admin"), tier="L",
    )
    body = json.loads(res.body)
    assert [c["change_id"] for c in body["candidates"]] == ["I0021"]


@pytest.mark.asyncio
async def test_list_filters_by_file_glob_substring():
    from backend.routers import batch_merge

    _seed_candidate(change_id="I0030", files=["docs/x.md"])
    _seed_candidate(change_id="I0031", files=["backend/agents/x.py"])

    res = await batch_merge.list_batch_merge_candidates(
        None, actor=_user("admin"), file_glob="backend/",
    )
    body = json.loads(res.body)
    assert [c["change_id"] for c in body["candidates"]] == ["I0031"]


# ── POST /admin/batch-merge/approve ──────────────────────────────────


class _FakeAuditCalls:
    def __init__(self):
        self.entries: list[dict] = []

    async def log(self, **kwargs):
        self.entries.append(kwargs)
        return len(self.entries)


class _FakeGerritClient:
    """Captures post_review calls instead of touching SSH.

    ``responses`` lets a test override a per-change result; default is
    ``{"status": "ok"}`` (success)."""

    def __init__(self, responses: dict[str, dict] | None = None):
        self.calls: list[dict] = []
        self._responses = responses or {}

    async def post_review(self, *, commit, message, labels, project=""):
        self.calls.append({
            "commit": commit, "message": message,
            "labels": labels, "project": project,
        })
        return self._responses.get(commit, {"status": "ok", "commit": commit})


@pytest.mark.asyncio
async def test_approve_fans_out_plus_two_and_writes_audit(monkeypatch):
    from backend.routers import batch_merge

    _seed_candidate(change_id="I0100", project="omnisight",
                    ai_summary="LGTM doc tweak")
    _seed_candidate(change_id="I0101", project="omnisight",
                    ai_summary="LGTM rename only")

    fake_gerrit = _FakeGerritClient()
    fake_audit = _FakeAuditCalls()
    monkeypatch.setattr("backend.gerrit.gerrit_client", fake_gerrit)
    monkeypatch.setattr("backend.audit.log", fake_audit.log)

    req = batch_merge.BatchApproveRequest(
        change_ids=["I0100", "I0101"],
        message="LGTM batch",
    )
    res = await batch_merge.approve_batch_merge_candidates(
        req, request=None, actor=_user("admin"),
    )
    body = json.loads(res.body)

    # All succeeded
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    assert {r["change_id"] for r in body["results"] if r["ok"]} == {
        "I0100", "I0101",
    }

    # Two Gerrit +2 calls posted
    assert len(fake_gerrit.calls) == 2
    for call in fake_gerrit.calls:
        assert call["labels"] == {"Code-Review": 2}
        assert call["message"] == "LGTM batch"
        assert call["project"] == "omnisight"

    # Two audit entries
    assert len(fake_audit.entries) == 2
    actions = {e["action"] for e in fake_audit.entries}
    assert actions == {"batch_merge_approve"}
    for e in fake_audit.entries:
        assert e["entity_kind"] == "gerrit_change"
        assert e["actor"] == "admin@example.com"
        assert e["after"]["code_review"] == 2
        assert "ai_summary" in e["after"]

    # Successful rows fall off the registry server-side so the next
    # GET no longer surfaces them.
    assert batch_merge.list_candidates() == []


@pytest.mark.asyncio
async def test_approve_unknown_change_id_records_per_row_failure(monkeypatch):
    from backend.routers import batch_merge

    _seed_candidate(change_id="I0200")

    fake_gerrit = _FakeGerritClient()
    fake_audit = _FakeAuditCalls()
    monkeypatch.setattr("backend.gerrit.gerrit_client", fake_gerrit)
    monkeypatch.setattr("backend.audit.log", fake_audit.log)

    req = batch_merge.BatchApproveRequest(
        change_ids=["I0200", "I-does-not-exist"],
    )
    res = await batch_merge.approve_batch_merge_candidates(
        req, request=None, actor=_user("admin"),
    )
    body = json.loads(res.body)

    assert body["succeeded"] == 1
    assert body["failed"] == 1
    by_id = {r["change_id"]: r for r in body["results"]}
    assert by_id["I0200"]["ok"] is True
    assert by_id["I-does-not-exist"]["ok"] is False
    assert "not a current" in by_id["I-does-not-exist"]["reason"]

    # Unknown id should NOT trigger a Gerrit call or an audit row
    assert len(fake_gerrit.calls) == 1
    assert len(fake_audit.entries) == 1


@pytest.mark.asyncio
async def test_approve_propagates_gerrit_error_per_row(monkeypatch):
    from backend.routers import batch_merge

    _seed_candidate(change_id="I0300", project="omnisight")
    _seed_candidate(change_id="I0301", project="omnisight")

    fake_gerrit = _FakeGerritClient(responses={
        "I0301": {"error": "permission denied"},
    })
    fake_audit = _FakeAuditCalls()
    monkeypatch.setattr("backend.gerrit.gerrit_client", fake_gerrit)
    monkeypatch.setattr("backend.audit.log", fake_audit.log)

    req = batch_merge.BatchApproveRequest(change_ids=["I0300", "I0301"])
    res = await batch_merge.approve_batch_merge_candidates(
        req, request=None, actor=_user("admin"),
    )
    body = json.loads(res.body)

    by_id = {r["change_id"]: r for r in body["results"]}
    assert by_id["I0300"]["ok"] is True
    assert by_id["I0301"]["ok"] is False
    assert "permission denied" in by_id["I0301"]["reason"]

    # Only the successful change generated an audit row
    assert len(fake_audit.entries) == 1
    assert fake_audit.entries[0]["entity_id"] == "I0300"


def test_batch_approve_request_caps_input_size():
    from backend.routers.batch_merge import BatchApproveRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BatchApproveRequest(change_ids=[])  # too few
    with pytest.raises(ValidationError):
        BatchApproveRequest(change_ids=[f"I{i:03d}" for i in range(101)])  # too many


def test_remove_candidate_returns_pop_value():
    from backend.routers.batch_merge import (
        BatchMergeCandidate,
        register_candidate,
        remove_candidate,
    )

    register_candidate(BatchMergeCandidate(
        change_id="I-pop", project="x", bot="claude-bot",
        file_class="docs", insertions=1, deletions=0,
        files=["docs/x.md"], ai_summary="ok", tagged_at=time.time(),
    ))
    popped = remove_candidate("I-pop")
    assert popped is not None and popped.change_id == "I-pop"
    assert remove_candidate("I-pop") is None  # idempotent
