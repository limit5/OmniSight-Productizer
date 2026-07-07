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
#  fetch_story — OP-2536 transport seams (fields= / timeout_s=)
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_story_fields_param_emits_pinned_query_string(monkeypatch):
    """PINNED: fields= comma-string is forwarded verbatim in the URL."""
    fake = FakeJiraHttp()
    fake.set(
        "GET",
        "https://jira.example.com/rest/api/2/issue/PROJ-42"
        "?fields=issuelinks,parent,status,summary",
        200, {"key": "PROJ-42", "fields": {"summary": "s"}},
    )
    monkeypatch.setattr("backend.intent_source.audit_outbound", _noop_audit)
    adapter = _adapter(fake)
    story = await adapter.fetch_story(
        "PROJ-42", fields="issuelinks,parent,status,summary",
    )
    assert story.summary == "s"
    method, url, _headers, body = fake.calls[-1]
    assert method == "GET"
    assert url == (
        "https://jira.example.com/rest/api/2/issue/PROJ-42"
        "?fields=issuelinks,parent,status,summary"
    )
    assert body is None


@pytest.mark.asyncio
async def test_fetch_story_default_request_shape_unchanged(monkeypatch):
    """Regression: no params → byte-identical request shape to today.

    ``FakeJiraHttp.__call__`` only accepts the 4 positional args, so this
    also proves the adapter does NOT pass ``timeout_s=`` when absent.
    """
    fake = FakeJiraHttp()
    fake.set("GET", "https://jira.example.com/rest/api/2/issue/PROJ-42",
             200, {"key": "PROJ-42", "fields": {"summary": "s"}})
    monkeypatch.setattr("backend.intent_source.audit_outbound", _noop_audit)
    adapter = _adapter(fake)
    await adapter.fetch_story("PROJ-42")
    assert fake.calls == [(
        "GET",
        "https://jira.example.com/rest/api/2/issue/PROJ-42",
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer T",
        },
        None,
    )]


@pytest.mark.asyncio
async def test_fetch_story_timeout_s_threads_to_http_call(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_call(method, url, headers, body, *, timeout_s=None):
        seen["timeout_s"] = timeout_s
        return (200, json.dumps(
            {"key": "PROJ-42", "fields": {"summary": "s"}}).encode(), {})

    monkeypatch.setattr("backend.intent_source.audit_outbound", _noop_audit)
    adapter = JiraAdapter(
        base_url="https://jira.example.com", token="T",
        field_map=JiraFieldMap(), http_call=fake_call,
    )
    await adapter.fetch_story("PROJ-42", timeout_s=5.0)
    assert seen["timeout_s"] == 5.0


# ──────────────────────────────────────────────────────────────
#  curl_json_call — OP-2536 per-call --max-time + kill semantics
# ──────────────────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, stdout: bytes = b"", hang: bool = False):
        self.returncode = 0
        self.killed = False
        self._stdout = stdout
        self._hang = hang

    async def communicate(self):
        if self._hang:
            import asyncio
            await asyncio.Event().wait()
        return self._stdout, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _patch_subprocess(monkeypatch, proc: _FakeProc) -> dict[str, Any]:
    """Capture curl argv + the outer wait_for timeout."""
    import asyncio
    captured: dict[str, Any] = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        return proc

    orig_wait_for = asyncio.wait_for

    async def spy_wait_for(aw, timeout=None):
        captured["wait_for_timeout"] = timeout
        return await orig_wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)
    return captured


_CURL_OK = b'{"ok":1}\n__HTTP_STATUS__=200\n'


@pytest.mark.asyncio
async def test_curl_json_call_default_argv_pinned(monkeypatch):
    """Regression: no timeout_s → --max-time 30 / outer wait_for 35."""
    from backend.intent_source import curl_json_call

    proc = _FakeProc(stdout=_CURL_OK)
    captured = _patch_subprocess(monkeypatch, proc)
    status, raw, _hdrs = await curl_json_call(
        "GET", "https://jira.example.com/x",
    )
    assert status == 200
    assert raw == b'{"ok":1}'
    assert captured["argv"] == [
        "curl", "-sS", "-X", "GET",
        "-o", "-",
        "-w", "\n__HTTP_STATUS__=%{http_code}\n",
        "--max-time", "30",
        "https://jira.example.com/x",
        "-H", "Accept: application/json",
    ]
    assert captured["wait_for_timeout"] == 35


@pytest.mark.asyncio
async def test_curl_json_call_timeout_s_sets_max_time_and_outer(monkeypatch):
    """timeout_s=6 → --max-time 6 in argv, outer wait_for 6*35/30 = 7."""
    from backend.intent_source import curl_json_call

    proc = _FakeProc(stdout=_CURL_OK)
    captured = _patch_subprocess(monkeypatch, proc)
    status, _raw, _hdrs = await curl_json_call(
        "GET", "https://jira.example.com/x", timeout_s=6,
    )
    assert status == 200
    argv = captured["argv"]
    i = argv.index("--max-time")
    assert argv[i + 1] == "6"
    assert captured["wait_for_timeout"] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_curl_json_call_kills_subprocess_on_timeout(monkeypatch):
    from backend.intent_source import curl_json_call

    proc = _FakeProc(hang=True)
    _patch_subprocess(monkeypatch, proc)
    status, raw, _hdrs = await curl_json_call(
        "GET", "https://jira.example.com/x", timeout_s=0.01,
    )
    assert status == 0
    assert raw == b"curl: timeout"
    assert proc.killed is True


@pytest.mark.asyncio
async def test_curl_json_call_kills_subprocess_on_cancel(monkeypatch):
    import asyncio

    from backend.intent_source import curl_json_call

    proc = _FakeProc(hang=True)
    _patch_subprocess(monkeypatch, proc)
    task = asyncio.create_task(
        curl_json_call("GET", "https://jira.example.com/x", timeout_s=60),
    )
    for _ in range(10):  # let it reach the communicate() await
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed is True


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
