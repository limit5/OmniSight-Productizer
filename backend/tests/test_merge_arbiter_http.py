"""O7 (#270) — HTTP surface for merge-conflict + human-vote endpoints.

Exercises ``POST /orchestrator/merge-conflict``,
``POST /orchestrator/human-vote``, and
``POST /orchestrator/check-change-ready`` through the FastAPI test
client, using monkeypatched arbiter collaborators so the test never
touches real Gerrit / JIRA / LLM backends.
"""

from __future__ import annotations

import pytest

from backend import merge_arbiter as arb
from backend import merger_agent as ma


def _plus_two(request: ma.ConflictRequest) -> ma.ResolutionOutcome:
    return ma.ResolutionOutcome(
        change_id=request.change_id,
        file_path=request.file_path,
        reason=ma.MergerReason.plus_two_voted,
        voted_score=ma.LabelVote.plus_two,
        confidence=0.95,
        rationale="deterministic",
        diff_preview="...",
        push_sha="cafebabe",
        review_url="https://gerrit.example/change/42",
    )


class _StubSubmitter:
    def __init__(self):
        self.calls = []

    async def submit(self, *, commit, project):
        self.calls.append({"commit": commit, "project": project})
        return {"status": "submitted"}


class _StubRevoker:
    def __init__(self):
        self.calls = []

    async def revoke(self, *, commit, project, message):
        self.calls.append({"commit": commit, "project": project, "message": message})
        return {"status": "ok"}


class _StubJira:
    async def open_abstain_ticket(self, **kwargs):
        return arb.JiraTicketResult(ok=True, ticket="PROJ-99",
                                    url="https://jira/browse/PROJ-99")


class _StubNotifier:
    def __init__(self):
        self.calls = []

    async def notify(self, *, kind, change_id, payload):
        self.calls.append((kind, change_id, dict(payload)))


@pytest.mark.asyncio
class TestMergeArbiterHttp:

    async def _patch_deps(self, monkeypatch, merger_outcome, *, submitter=None,
                          revoker=None, notifier=None):
        async def _runner(req):
            return merger_outcome(req)

        deps = arb.ArbiterDeps(
            merger=_runner,
            jira=_StubJira(),
            notifier=notifier or _StubNotifier(),
            submitter=submitter or _StubSubmitter(),
            revoker=revoker or _StubRevoker(),
        )
        # Replace the class default so route handlers use our stubs.
        monkeypatch.setattr(arb, "ArbiterDeps", lambda **_kw: deps)
        # Also ensure state is clean.
        arb.reset_arbiter_state_for_tests()
        return deps

    async def test_merge_conflict_webhook_happy_path(self, client, monkeypatch):
        notifier = _StubNotifier()
        await self._patch_deps(monkeypatch, _plus_two, notifier=notifier)
        from backend.config import settings
        settings.jira_webhook_secret = ""

        r = await client.post(
            "/api/v1/orchestrator/merge-conflict",
            json={
                "change_id": "Ihttptest",
                "project": "omnisight",
                "file_path": "backend/greetings.py",
                "conflict_text": (
                    "<<<<<<< HEAD\n"
                    "x = 1\n"
                    "=======\n"
                    "x = 2\n"
                    ">>>>>>> branch\n"
                ),
                "patchset_revision": "cafebabe",
                "jira_ticket": "PROJ-42",
                "catc_owner": "alice@omnisight.internal",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["reason"] == "merger_plus_two_awaiting_human"
        kinds = [c[0] for c in notifier.calls]
        assert "change.awaiting_human_plus_two" in kinds

    async def test_merge_conflict_missing_fields_rejected(self, client, monkeypatch):
        await self._patch_deps(monkeypatch, _plus_two)
        from backend.config import settings
        settings.jira_webhook_secret = ""
        r = await client.post(
            "/api/v1/orchestrator/merge-conflict",
            json={"change_id": "Ifoo"},
        )
        assert r.status_code == 400

    async def test_human_vote_submits_on_dual_plus_two(self, client, monkeypatch):
        submitter = _StubSubmitter()
        await self._patch_deps(monkeypatch, _plus_two, submitter=submitter)

        r = await client.post(
            "/api/v1/orchestrator/human-vote",
            json={
                "change_id": "Iend",
                "project": "omnisight",
                "commit": "cafebabe",
                "votes": [
                    {"voter": "merger-agent-bot",
                     "groups": ["ai-reviewer-bots", "merger-agent-bot"],
                     "score": 2},
                    {"voter": "alice@example.com",
                     "groups": ["non-ai-reviewer"],
                     "score": 2},
                ],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reason"] == "submitted"
        assert submitter.calls == [{"commit": "cafebabe", "project": "omnisight"}]

    async def test_human_vote_minus_one_withdraws_merger(self, client, monkeypatch):
        submitter = _StubSubmitter()
        revoker = _StubRevoker()
        await self._patch_deps(
            monkeypatch, _plus_two, submitter=submitter, revoker=revoker,
        )

        r = await client.post(
            "/api/v1/orchestrator/human-vote",
            json={
                "change_id": "Iend",
                "project": "omnisight",
                "commit": "cafebabe",
                "votes": [
                    {"voter": "merger-agent-bot",
                     "groups": ["ai-reviewer-bots", "merger-agent-bot"],
                     "score": 2},
                    {"voter": "alice@example.com",
                     "groups": ["non-ai-reviewer"],
                     "score": -1},
                ],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reason"] == "human_disagreed_merger_withdrew"
        assert not submitter.calls
        assert len(revoker.calls) == 1

    async def test_check_change_ready_mirrors_evaluator(self, client, monkeypatch):
        r = await client.post(
            "/api/v1/orchestrator/check-change-ready",
            json={
                "change_id": "X",
                "project": "Y",
                "commit": "Z",
                "votes": [
                    {"voter": "merger-agent-bot",
                     "groups": ["ai-reviewer-bots", "merger-agent-bot"],
                     "score": 2},
                ],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["allow"] is False
        assert body["reason"] == "reject_missing_human_plus_two"
        assert body["merger_plus_twos"] == 1
        assert body["human_plus_twos"] == 0
