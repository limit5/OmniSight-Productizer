"""OP-1281 - input-validation tests for ``backend.routers.orchestrator``.

These tests keep to the router surface: malformed bodies must fail
before downstream orchestrator / arbiter collaborators run, while large
but valid values are threaded through to the collaborator unchanged.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import auth as _au
from backend.routers import orchestrator as orch


def _user() -> _au.User:
    return _au.User(
        id="operator-1",
        email="operator@test.local",
        name="Operator",
        role="operator",
        tenant_id="tenantA",
    )


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(orch.settings, "jira_webhook_secret", "", raising=False)

    app = FastAPI()
    app.dependency_overrides[_au.require_operator] = _user
    app.dependency_overrides[_au.require_viewer] = _user
    app.include_router(orch.router)
    return TestClient(app, raise_server_exceptions=False)


class _Outcome:
    jira_ticket = "OP-1281"

    def to_dict(self) -> dict:
        return {
            "jira_ticket": self.jira_ticket,
            "state": "queued",
            "n_cards_queued": 0,
            "cards": [],
        }


class _ArbiterOutcome:
    def to_dict(self) -> dict:
        return {"reason": "stubbed"}


def test_intake_rejects_none_body(client: TestClient) -> None:
    r = client.post("/orchestrator/intake", json=None)

    assert r.status_code == 400
    assert r.json()["detail"] == "Empty body"


@pytest.mark.parametrize("payload", [[], "wrong-type"])
def test_intake_rejects_non_object_json(client: TestClient, payload) -> None:
    r = client.post("/orchestrator/intake", json=payload)

    assert r.status_code == 400
    assert r.json()["detail"] == "Body must be a JSON object"


def test_intake_rejects_empty_object(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _reject_missing_fields(*_args, **_kwargs):
        raise orch.og.IntakeError(
            orch.og.IntakeRejectReason.missing_fields,
            "missing Jira issue fields",
        )

    monkeypatch.setattr(orch.og, "intake", _reject_missing_fields)

    r = client.post("/orchestrator/intake", json={})

    assert r.status_code == 400
    assert r.json()["reason"] == "missing_fields"


def test_intake_rejects_very_large_body(client: TestClient) -> None:
    body = b'{"description":"' + (b"x" * 1_048_576) + b'"}'

    r = client.post(
        "/orchestrator/intake",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert r.status_code == 413
    assert r.json()["detail"] == "Payload too large"


@pytest.mark.parametrize("payload", [None, {}, []])
def test_replan_rejects_none_empty_and_wrong_type(
    client: TestClient, payload,
) -> None:
    r = client.post("/orchestrator/replan", json=payload)

    assert r.status_code == 422


def test_replan_threads_very_large_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def _replan(jira_ticket: str, **kwargs):
        calls.append({"jira_ticket": jira_ticket, **kwargs})
        return _Outcome()

    monkeypatch.setattr(orch.og, "replan", _replan)
    large_story = "x" * 200_000

    r = client.post(
        "/orchestrator/replan",
        json={
            "jira_ticket": "OP-1281",
            "approver": "pm@test.local",
            "new_story": large_story,
            "token_budget": 10**12,
            "forbidden_globs": ["backend/tests/**"],
        },
    )

    assert r.status_code == 200, r.text
    assert calls[0]["new_story"] == large_story
    assert calls[0]["token_budget"] == 10**12


@pytest.mark.asyncio
@pytest.mark.parametrize("jira_ticket", [None, "", [], "OP-" + ("9" * 200_000)])
async def test_status_endpoint_rejects_missing_unknown_or_wrong_type(
    monkeypatch: pytest.MonkeyPatch, jira_ticket,
) -> None:
    monkeypatch.setattr(orch.og, "get_status", lambda _ticket: None)

    with pytest.raises(HTTPException) as exc:
        await orch.status_endpoint(jira_ticket, _user=_user())

    assert exc.value.status_code == 404


def test_list_status_endpoint_returns_empty_collection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch.og, "list_sessions", lambda: [])

    r = client.get("/orchestrator/status")

    assert r.status_code == 200
    assert r.json() == {"sessions": []}


@pytest.mark.parametrize("payload", [None, {}, [], "wrong-type"])
def test_merge_conflict_rejects_none_empty_and_wrong_type(
    client: TestClient, payload,
) -> None:
    r = client.post("/orchestrator/merge-conflict", json=payload)

    assert r.status_code == 400


def test_merge_conflict_rejects_very_large_body(client: TestClient) -> None:
    body = b'{"change_id":"' + (b"x" * 1_048_576) + b'"}'

    r = client.post(
        "/orchestrator/merge-conflict",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert r.status_code == 413
    assert r.json()["detail"] == "Payload too large"


@pytest.mark.parametrize("path", ["/orchestrator/human-vote", "/orchestrator/check-change-ready"])
@pytest.mark.parametrize("payload", [None, {}, []])
def test_vote_endpoints_reject_none_empty_and_wrong_type(
    client: TestClient, path: str, payload,
) -> None:
    r = client.post(path, json=payload)

    assert r.status_code == 422


def test_human_vote_threads_very_large_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def _recorded(**kwargs):
        calls.append(kwargs)
        return _ArbiterOutcome()

    monkeypatch.setattr(orch._arb, "on_human_vote_recorded", _recorded)
    large_voter = "voter-" + ("x" * 200_000)

    r = client.post(
        "/orchestrator/human-vote",
        json={
            "change_id": "I" + ("a" * 200_000),
            "project": "omnisight",
            "commit": "cafebabe",
            "votes": [
                {
                    "voter": large_voter,
                    "groups": ["non-ai-reviewer"],
                    "score": 10**9,
                },
            ],
        },
    )

    assert r.status_code == 200, r.text
    assert calls[0]["votes"][0].voter == large_voter
    assert calls[0]["votes"][0].score == 10**9


def test_check_change_ready_handles_very_large_values(client: TestClient) -> None:
    r = client.post(
        "/orchestrator/check-change-ready",
        json={
            "change_id": "I" + ("b" * 200_000),
            "project": "omnisight",
            "commit": "cafebabe",
            "votes": [
                {
                    "voter": "human-" + ("x" * 200_000),
                    "groups": ["non-ai-reviewer"],
                    "score": 10**9,
                },
            ],
        },
    )

    assert r.status_code == 200, r.text
    assert r.json()["human_plus_twos"] == 1


def test_merge_conflict_threads_very_large_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    async def _merge_conflict(task):
        calls.append(task)
        return _ArbiterOutcome()

    monkeypatch.setattr(orch._arb, "on_merge_conflict_webhook", _merge_conflict)
    conflict_text = "<<<<<<< HEAD\n" + ("x" * 200_000)

    r = client.post(
        "/orchestrator/merge-conflict",
        json={
            "change_id": "Ilarge",
            "project": "omnisight",
            "file_path": "backend/tests/test_orchestrator.py",
            "conflict_text": conflict_text,
        },
    )

    assert r.status_code == 200, r.text
    assert getattr(calls[0], "conflict_text") == conflict_text
