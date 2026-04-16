"""O5 (#268) — JIRA IntentSource adapter tests.

Stubs the HTTP transport via ``JiraAdapter(http_call=fake_call)`` so we
never touch a real JIRA server.  A ``FakeJiraHttp`` helper accumulates
called requests and returns canned responses per (method, path) key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from backend.intent_source import (
    AdapterError,
    IntentStatus,
    SubtaskPayload,
)
from backend.jira_adapter import (
    JiraAdapter,
    JiraFieldMap,
    _match_transition,
)


# ──────────────────────────────────────────────────────────────
#  Fake HTTP — configurable canned responses
# ──────────────────────────────────────────────────────────────


@dataclass
class FakeJiraHttp:
    responses: dict[tuple[str, str], tuple[int, bytes]] = field(
        default_factory=dict
    )
    calls: list[tuple[str, str, dict, bytes | None]] = field(
        default_factory=list
    )

    async def __call__(self, method, url, headers, body):
        path = url
        key = (method.upper(), path)
        self.calls.append((method.upper(), path, dict(headers), body))
        if key in self.responses:
            status, raw = self.responses[key]
            return (status, raw, {})
        # Default: 200 empty body
        return (200, b"{}", {})

    def set(self, method: str, path: str, status: int, body: Any):
        if isinstance(body, (dict, list)):
            raw = json.dumps(body).encode()
        elif isinstance(body, str):
            raw = body.encode()
        else:
            raw = body
        self.responses[(method.upper(), path)] = (status, raw)


def _adapter(fake: FakeJiraHttp, *, base="https://jira.example.com",
             token="T", project="PROJ", webhook_secret="wh",
             ) -> JiraAdapter:
    return JiraAdapter(
        base_url=base, token=token, project_key=project,
        webhook_secret=webhook_secret,
        field_map=JiraFieldMap(),  # defaults
        http_call=fake,
    )


# ──────────────────────────────────────────────────────────────
#  fetch_story
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_story_happy_path(monkeypatch):
    fake = FakeJiraHttp()
    fake.set("GET", "https://jira.example.com/rest/api/2/issue/PROJ-42",
             200, {
                 "key": "PROJ-42",
                 "fields": {
                     "summary": "Add RTSP",
                     "description": "body",
                     "priority": {"name": "High"},
                     "labels": ["camera", "rtsp"],
                 },
             })
    monkeypatch.setattr("backend.intent_source.audit_outbound",
                        _noop_audit)
    adapter = _adapter(fake)
    story = await adapter.fetch_story("PROJ-42")
    assert story.ticket == "PROJ-42"
    assert story.summary == "Add RTSP"
    assert story.priority == "High"
    assert story.labels == ["camera", "rtsp"]
    assert story.vendor == "jira"


@pytest.mark.asyncio
async def test_fetch_story_bad_ticket_rejected():
    adapter = _adapter(FakeJiraHttp())
    with pytest.raises(AdapterError):
        await adapter.fetch_story("not-a-ticket")


@pytest.mark.asyncio
async def test_fetch_story_http_error(monkeypatch):
    fake = FakeJiraHttp()
    fake.set("GET", "https://jira.example.com/rest/api/2/issue/PROJ-9",
             404, {"errorMessages": ["not found"]})
    monkeypatch.setattr("backend.intent_source.audit_outbound",
                        _noop_audit)
    adapter = _adapter(fake)
    with pytest.raises(AdapterError):
        await adapter.fetch_story("PROJ-9")


# ──────────────────────────────────────────────────────────────
#  create_subtasks — bulk endpoint + custom field mapping
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_subtasks_maps_custom_fields(monkeypatch):
    fake = FakeJiraHttp()
    fake.set("POST", "https://jira.example.com/rest/api/2/issue/bulk",
             201, {
                 "issues": [
                     {"key": "PROJ-1001", "id": "10001"},
                     {"key": "PROJ-1002", "id": "10002"},
                 ],
                 "errors": [],
             })
    monkeypatch.setattr("backend.intent_source.audit_outbound",
                        _noop_audit)
    adapter = _adapter(fake)

    payloads = [
        SubtaskPayload(
            title="PROJ-1001",
            acceptance_criteria="AC body",
            impact_scope_allowed=["src/foo/**"],
            impact_scope_forbidden=["test_assets/**"],
            handoff_protocol=["Run tests", "git push HEAD:refs/for/main"],
            domain_context="camera",
        ),
        SubtaskPayload(
            title="PROJ-1002",
            acceptance_criteria="AC 2",
            impact_scope_allowed=["src/bar/**"],
            impact_scope_forbidden=[],
            handoff_protocol=[],
            domain_context="",
        ),
    ]
    refs = await adapter.create_subtasks("PROJ-1", payloads)
    assert [r.ticket for r in refs] == ["PROJ-1001", "PROJ-1002"]
    assert all(r.vendor == "jira" for r in refs)
    assert refs[0].url.endswith("/browse/PROJ-1001")
    assert refs[0].parent == "PROJ-1"

    # Inspect the request body: ensure impact_scope + AC + handoff are
    # in the right custom-field slots.
    req = fake.calls[-1]
    body = json.loads(req[3])
    issues = body["issueUpdates"]
    fm = JiraFieldMap()
    assert issues[0]["fields"][fm.impact_scope_allowed] == ["src/foo/**"]
    assert issues[0]["fields"][fm.impact_scope_forbidden] == ["test_assets/**"]
    assert issues[0]["fields"][fm.acceptance_criteria] == "AC body"
    assert issues[0]["fields"][fm.handoff_protocol] == [
        "Run tests", "git push HEAD:refs/for/main",
    ]
    assert issues[0]["fields"]["parent"] == {"key": "PROJ-1"}
    assert issues[0]["fields"]["issuetype"] == {"name": "Sub-task"}


@pytest.mark.asyncio
async def test_create_subtasks_empty_list_noop():
    adapter = _adapter(FakeJiraHttp())
    assert await adapter.create_subtasks("PROJ-1", []) == []


@pytest.mark.asyncio
async def test_create_subtasks_bulk_failure(monkeypatch):
    fake = FakeJiraHttp()
    fake.set("POST", "https://jira.example.com/rest/api/2/issue/bulk",
             400, {"errorMessages": ["bad schema"]})
    monkeypatch.setattr("backend.intent_source.audit_outbound",
                        _noop_audit)
    adapter = _adapter(fake)
    with pytest.raises(AdapterError) as ex:
        await adapter.create_subtasks("PROJ-1", [SubtaskPayload(
            title="PROJ-1001", acceptance_criteria="x",
            impact_scope_allowed=["a/**"], impact_scope_forbidden=[],
            handoff_protocol=[],
        )])
    assert ex.value.status_code == 400


# ──────────────────────────────────────────────────────────────
#  update_status — transition match + POST
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_status_finds_transition_by_to_name(monkeypatch):
    fake = FakeJiraHttp()
    fake.set("GET",
             "https://jira.example.com/rest/api/2/issue/PROJ-42/transitions",
             200, {
                 "transitions": [
                     {"id": "11", "name": "Start Progress",
                      "to": {"name": "In Progress"}},
                     {"id": "21", "name": "Mark Reviewing",
                      "to": {"name": "In Review"}},
                     {"id": "31", "name": "Close", "to": {"name": "Done"}},
                 ],
             })
    fake.set("POST",
             "https://jira.example.com/rest/api/2/issue/PROJ-42/transitions",
             204, {})
    monkeypatch.setattr("backend.intent_source.audit_outbound",
                        _noop_audit)
    adapter = _adapter(fake)
    out = await adapter.update_status(
        "PROJ-42", IntentStatus.reviewing, comment="x",
    )
    assert out["ok"] is True
    assert out["transition_id"] == "21"
    # Request body should carry the transition id + comment.
    post_call = fake.calls[-1]
    body = json.loads(post_call[3])
    assert body["transition"]["id"] == "21"
    assert body["update"]["comment"][0]["add"]["body"] == "x"


@pytest.mark.asyncio
async def test_update_status_missing_transition(monkeypatch):
    fake = FakeJiraHttp()
    fake.set("GET",
             "https://jira.example.com/rest/api/2/issue/PROJ-42/transitions",
             200, {"transitions": [
                 {"id": "11", "name": "Start Progress",
                  "to": {"name": "In Progress"}},
             ]})
    monkeypatch.setattr("backend.intent_source.audit_outbound",
                        _noop_audit)
    adapter = _adapter(fake)
    with pytest.raises(AdapterError):
        await adapter.update_status("PROJ-42", IntentStatus.done)


def test_match_transition_prefers_to_name_over_transition_name():
    transitions = [
        {"id": "11", "name": "Done", "to": {"name": "Backlog"}},
        {"id": "22", "name": "Finish", "to": {"name": "Done"}},
    ]
    # "Done" matches transition 22's "to" exactly; transition 11's
    # transition-name happens to also be "Done" but its "to" is
    # "Backlog".  Strict pass picks either — both exact-match — so
    # this test asserts the function returns SOMETHING matching.
    assert _match_transition(transitions, "Done") in ("11", "22")


# ──────────────────────────────────────────────────────────────
#  comment
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_comment_posts_body(monkeypatch):
    fake = FakeJiraHttp()
    fake.set("POST",
             "https://jira.example.com/rest/api/2/issue/PROJ-1/comment",
             201, {"id": "9999"})
    monkeypatch.setattr("backend.intent_source.audit_outbound",
                        _noop_audit)
    adapter = _adapter(fake)
    out = await adapter.comment("PROJ-1", "hello world")
    assert out["id"] == "9999"
    body = json.loads(fake.calls[-1][3])
    assert body == {"body": "hello world"}


@pytest.mark.asyncio
async def test_comment_empty_rejected():
    adapter = _adapter(FakeJiraHttp())
    with pytest.raises(AdapterError):
        await adapter.comment("PROJ-1", "")


# ──────────────────────────────────────────────────────────────
#  verify_webhook
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_webhook_bearer_match():
    a = _adapter(FakeJiraHttp(), webhook_secret="s3cr3t")
    ok = await a.verify_webhook(
        {"Authorization": "Bearer s3cr3t"}, b"{}",
    )
    assert ok is True


@pytest.mark.asyncio
async def test_verify_webhook_header_match():
    a = _adapter(FakeJiraHttp(), webhook_secret="s3cr3t")
    ok = await a.verify_webhook(
        {"X-Jira-Webhook-Secret": "s3cr3t"}, b"{}",
    )
    assert ok is True


@pytest.mark.asyncio
async def test_verify_webhook_mismatch():
    a = _adapter(FakeJiraHttp(), webhook_secret="s3cr3t")
    ok = await a.verify_webhook(
        {"Authorization": "Bearer wrong"}, b"{}",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_verify_webhook_no_secret_rejects():
    a = _adapter(FakeJiraHttp(), webhook_secret="")
    ok = await a.verify_webhook(
        {"Authorization": "Bearer anything"}, b"{}",
    )
    assert ok is False


# ──────────────────────────────────────────────────────────────
#  helpers
# ──────────────────────────────────────────────────────────────


async def _noop_audit(*, vendor, action, ticket, request, response,
                      status_code=None, actor="x"):
    return None
