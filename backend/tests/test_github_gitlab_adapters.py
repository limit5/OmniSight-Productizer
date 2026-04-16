"""O5 (#268) — GitHub / GitLab IntentSource adapter tests."""

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
from backend.github_adapter import GithubAdapter, parse_github_ticket
from backend.gitlab_adapter import GitlabAdapter, parse_gitlab_ticket


@dataclass
class FakeHttp:
    responses: dict[tuple[str, str], tuple[int, bytes]] = field(default_factory=dict)
    calls: list[tuple[str, str, dict, bytes | None]] = field(default_factory=list)

    async def __call__(self, method, url, headers, body):
        self.calls.append((method.upper(), url, dict(headers), body))
        key = (method.upper(), url)
        if key in self.responses:
            status, raw = self.responses[key]
            return (status, raw, {})
        return (200, b"{}", {})

    def set(self, method: str, url: str, status: int, body: Any):
        if isinstance(body, (dict, list)):
            raw = json.dumps(body).encode()
        elif isinstance(body, str):
            raw = body.encode()
        else:
            raw = body
        self.responses[(method.upper(), url)] = (status, raw)


async def _noop_audit(*, vendor, action, ticket, request, response,
                      status_code=None, actor="x"):
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GitHub
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_parse_github_ticket():
    owner, repo, n = parse_github_ticket("octo/widgets#42")
    assert (owner, repo, n) == ("octo", "widgets", 42)


def test_parse_github_ticket_bad():
    with pytest.raises(AdapterError):
        parse_github_ticket("no-number")


@pytest.mark.asyncio
async def test_github_fetch_story(monkeypatch):
    fake = FakeHttp()
    fake.set("GET", "https://api.github.com/repos/octo/widgets/issues/42",
             200, {
                 "number": 42,
                 "title": "Add RTSP",
                 "body": "body text",
                 "labels": [{"name": "bug"}, {"name": "camera"}],
             })
    monkeypatch.setattr("backend.intent_source.audit_outbound", _noop_audit)
    a = GithubAdapter(token="T", http_call=fake)
    story = await a.fetch_story("octo/widgets#42")
    assert story.summary == "Add RTSP"
    assert story.description == "body text"
    assert story.labels == ["bug", "camera"]


@pytest.mark.asyncio
async def test_github_create_subtasks_appends_checklist(monkeypatch):
    fake = FakeHttp()
    fake.set(
        "POST", "https://api.github.com/repos/octo/widgets/issues", 201,
        {"number": 43, "html_url": "https://github.com/octo/widgets/issues/43",
         "node_id": "N43"},
    )
    fake.set(
        "POST",
        "https://api.github.com/repos/octo/widgets/issues/42/comments",
        201, {"id": 1},
    )
    monkeypatch.setattr("backend.intent_source.audit_outbound", _noop_audit)
    a = GithubAdapter(token="T", http_call=fake)

    payloads = [
        SubtaskPayload(
            title="Sub task 1", acceptance_criteria="AC",
            impact_scope_allowed=["src/**"], impact_scope_forbidden=[],
            handoff_protocol=["Push"],
        ),
        SubtaskPayload(
            title="Sub task 2", acceptance_criteria="AC2",
            impact_scope_allowed=["src/foo/**"], impact_scope_forbidden=[],
            handoff_protocol=[],
        ),
    ]
    # Stub the POST returning two issues by toggling response on call count.
    # Simpler: both calls hit the same URL; default 200 empty doesn't give
    # us a number.  Pre-populate the 201 above — but each POST will return
    # the same (number=43) body.  Tests that need N distinct numbers can
    # set per-call overrides — for this one we just assert call pattern.
    refs = await a.create_subtasks("octo/widgets#42", payloads)
    assert len(refs) == 2
    # Parent comment POST should have happened after the 2 child creates.
    comment_calls = [c for c in fake.calls if c[0] == "POST"
                     and c[1].endswith("/issues/42/comments")]
    assert len(comment_calls) == 1
    body = json.loads(comment_calls[0][3])["body"]
    assert "## OmniSight sub-tasks" in body
    assert "- [ ] octo/widgets#43" in body


@pytest.mark.asyncio
async def test_github_update_status_close_on_done(monkeypatch):
    fake = FakeHttp()
    fake.set(
        "PATCH", "https://api.github.com/repos/octo/widgets/issues/42",
        200, {"number": 42, "state": "closed"},
    )
    fake.set(
        "PUT", "https://api.github.com/repos/octo/widgets/issues/42/labels",
        200, [{"name": "status:done"}],
    )
    monkeypatch.setattr("backend.intent_source.audit_outbound", _noop_audit)
    a = GithubAdapter(token="T", http_call=fake)
    out = await a.update_status("octo/widgets#42", IntentStatus.done)
    assert out["state"] == "closed"
    assert out["label"] == "status:done"


@pytest.mark.asyncio
async def test_github_verify_webhook_hmac(monkeypatch):
    import hashlib
    import hmac as _hmac
    secret = b"s3cr3t"
    body = b'{"action":"opened"}'
    sig = "sha256=" + _hmac.new(secret, body, hashlib.sha256).hexdigest()
    a = GithubAdapter(token="T", webhook_secret="s3cr3t")
    assert await a.verify_webhook({"X-Hub-Signature-256": sig}, body) is True
    assert await a.verify_webhook(
        {"X-Hub-Signature-256": "sha256=wrong"}, body
    ) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GitLab
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_parse_gitlab_ticket():
    proj, iid = parse_gitlab_ticket("my-group/my-proj#17")
    assert (proj, iid) == ("my-group/my-proj", 17)


def test_parse_gitlab_ticket_bad():
    with pytest.raises(AdapterError):
        parse_gitlab_ticket("nope")


@pytest.mark.asyncio
async def test_gitlab_fetch_story(monkeypatch):
    import urllib.parse
    fake = FakeHttp()
    encoded = urllib.parse.quote("g/p", safe="")
    fake.set("GET",
             f"https://gitlab.com/api/v4/projects/{encoded}/issues/17",
             200, {
                 "iid": 17, "title": "Add widget",
                 "description": "d", "labels": ["bug"],
             })
    monkeypatch.setattr("backend.intent_source.audit_outbound", _noop_audit)
    a = GitlabAdapter(token="T", http_call=fake)
    story = await a.fetch_story("g/p#17")
    assert story.summary == "Add widget"
    assert story.labels == ["bug"]


@pytest.mark.asyncio
async def test_gitlab_create_subtasks_and_comment(monkeypatch):
    import urllib.parse
    fake = FakeHttp()
    encoded = urllib.parse.quote("g/p", safe="")
    fake.set(
        "POST", f"https://gitlab.com/api/v4/projects/{encoded}/issues",
        201, {"iid": 18, "id": 9001,
              "web_url": "https://gitlab.com/g/p/-/issues/18"},
    )
    fake.set(
        "POST",
        f"https://gitlab.com/api/v4/projects/{encoded}/issues/17/notes",
        201, {"id": 5},
    )
    monkeypatch.setattr("backend.intent_source.audit_outbound", _noop_audit)
    a = GitlabAdapter(token="T", http_call=fake)
    payloads = [SubtaskPayload(
        title="x", acceptance_criteria="AC",
        impact_scope_allowed=["src/**"], impact_scope_forbidden=[],
        handoff_protocol=[],
    )]
    refs = await a.create_subtasks("g/p#17", payloads)
    assert refs and refs[0].ticket == "g/p#18"


@pytest.mark.asyncio
async def test_gitlab_update_status_close_on_done(monkeypatch):
    import urllib.parse
    fake = FakeHttp()
    encoded = urllib.parse.quote("g/p", safe="")
    fake.set(
        "PUT", f"https://gitlab.com/api/v4/projects/{encoded}/issues/17",
        200, {"state": "closed"},
    )
    monkeypatch.setattr("backend.intent_source.audit_outbound", _noop_audit)
    a = GitlabAdapter(token="T", http_call=fake)
    out = await a.update_status("g/p#17", IntentStatus.done)
    assert out["state_event"] == "close"


@pytest.mark.asyncio
async def test_gitlab_verify_webhook_token_equality():
    a = GitlabAdapter(token="T", webhook_secret="s3cr3t")
    assert await a.verify_webhook({"X-Gitlab-Token": "s3cr3t"}, b"") is True
    assert await a.verify_webhook({"X-Gitlab-Token": "bad"}, b"") is False
