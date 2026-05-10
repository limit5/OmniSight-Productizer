"""META OP-814 / A6 — MCP-Gerrit integration tests.

Locks the read-only contract from the ticket:

  * mcp_gerrit__queryChanges(filter) parses ``gerrit query --format=JSON``
    output, skips ``{"type":"stats"}`` rows, and respects ``limit``.
  * mcp_gerrit__getReview(change_id) extracts approvals + comments + the
    current patch-set revision; returns None for empty Gerrit response.
  * mcp_gerrit__hasOpenPsForTicket(ticket_key) — synthetic AC: model
    checks if its ticket has open PS before starting work.
  * Read-only: only the three query tools are registered. No push,
    submit, or review-vote handler is exposed (PS push remains in
    jira_dispatch — AC #3 audit-trail preservation).
  * mcp_integration registration: local MCP server appears in the
    runtime registry under the ``mcp_gerrit`` server name.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.agents import mcp_gerrit, mcp_integration


# ─── Helpers ─────────────────────────────────────────────────────


class _FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _stub_subprocess(monkeypatch: pytest.MonkeyPatch, stdout: str = "", returncode: int = 0):
    """Patch subprocess.run inside mcp_gerrit so no real ssh fires."""
    captured: dict[str, Any] = {}

    def fake_run(cmd, *args: Any, **kwargs: Any) -> _FakeCompleted:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(stdout=stdout, returncode=returncode)

    monkeypatch.setattr(mcp_gerrit.subprocess, "run", fake_run)
    return captured


def _gerrit_change_blob(
    *,
    number: int = 12345,
    change_id: str = "I1234567890abcdef",
    subject: str = "[OP-700] do thing",
    status: str = "NEW",
    owner_username: str = "claude-bot",
    project: str = mcp_gerrit.GERRIT_PROJECT_PATH,
    branch: str = "develop",
    url: str | None = None,
    current_revision: str | None = None,
    approvals: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    blob: dict[str, Any] = {
        "number": number,
        "id": change_id,
        "subject": subject,
        "status": status,
        "owner": {"username": owner_username},
        "project": project,
        "branch": branch,
    }
    if url is not None:
        blob["url"] = url
    if current_revision is not None or approvals is not None:
        blob["currentPatchSet"] = {}
        if current_revision is not None:
            blob["currentPatchSet"]["revision"] = current_revision
        if approvals is not None:
            blob["currentPatchSet"]["approvals"] = approvals
    if comments is not None:
        blob["comments"] = comments
    return blob


def _gerrit_stats_line(rows: int) -> str:
    return json.dumps({"type": "stats", "rowCount": rows, "runTimeMilliseconds": 7})


def _gerrit_stdout(*changes: dict[str, Any]) -> str:
    lines = [json.dumps(c) for c in changes]
    lines.append(_gerrit_stats_line(len(changes)))
    return "\n".join(lines) + "\n"


# ─── query_changes ───────────────────────────────────────────────


def test_query_changes_parses_rows_and_skips_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = _gerrit_stdout(
        _gerrit_change_blob(number=101, subject="[OP-700] A"),
        _gerrit_change_blob(number=102, subject="[OP-701] B"),
    )
    captured = _stub_subprocess(monkeypatch, stdout=stdout)

    rows = mcp_gerrit.query_changes("is:open owner:claude-bot")

    assert [r.change_number for r in rows] == [101, 102]
    assert rows[0].subject == "[OP-700] A"
    assert rows[0].status == "NEW"
    assert rows[0].owner == "claude-bot"
    # SSH argv contains the gerrit query subcommand and JSON flag
    cmd = captured["cmd"]
    assert "gerrit" in cmd and "query" in cmd
    assert "--format=JSON" in cmd
    assert "is:open owner:claude-bot" in cmd


def test_query_changes_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    blobs = [_gerrit_change_blob(number=200 + i, subject=f"#{i}") for i in range(10)]
    stdout = _gerrit_stdout(*blobs)
    _stub_subprocess(monkeypatch, stdout=stdout)

    rows = mcp_gerrit.query_changes("is:open", limit=3)

    assert len(rows) == 3
    assert [r.change_number for r in rows] == [200, 201, 202]


def test_query_changes_rejects_empty_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_subprocess(monkeypatch)
    with pytest.raises(ValueError, match="filter must be a non-empty"):
        mcp_gerrit.query_changes("")
    with pytest.raises(ValueError, match="filter must be a non-empty"):
        mcp_gerrit.query_changes("   ")


def test_query_changes_synthesises_url_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Gerrit omits the ``url`` field, fall back to a deterministic URL."""
    stdout = _gerrit_stdout(
        _gerrit_change_blob(number=4242, url=None),
    )
    _stub_subprocess(monkeypatch, stdout=stdout)

    rows = mcp_gerrit.query_changes("change:4242")

    assert rows[0].url.endswith(f"/+/{4242}")
    assert mcp_gerrit.GERRIT_PROJECT_PATH in rows[0].url


def test_query_changes_raises_on_nonzero_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, *args: Any, **kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(stdout="", returncode=1, stderr="boom")

    monkeypatch.setattr(mcp_gerrit.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gerrit-ssh-cli failed"):
        mcp_gerrit.query_changes("is:open")


def test_query_changes_skips_malformed_json_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = (
        "this is not json\n"
        + json.dumps(_gerrit_change_blob(number=999))
        + "\n"
        + _gerrit_stats_line(1)
        + "\n"
    )
    _stub_subprocess(monkeypatch, stdout=stdout)
    rows = mcp_gerrit.query_changes("anything")
    assert [r.change_number for r in rows] == [999]


# ─── get_review ──────────────────────────────────────────────────


def test_get_review_extracts_current_patchset_and_approvals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob = _gerrit_change_blob(
        number=555,
        change_id="Iabc",
        current_revision="deadbeef" * 5,
        approvals=[
            {"type": "Code-Review", "value": "+2", "by": {"username": "human"}},
            {"type": "Verified", "value": "+1", "by": {"username": "ci-bot"}},
        ],
        comments=[
            {
                "timestamp": 1700000000,
                "reviewer": {"username": "rev-1"},
                "message": "looks good",
            },
        ],
    )
    stdout = _gerrit_stdout(blob)
    captured = _stub_subprocess(monkeypatch, stdout=stdout)

    review = mcp_gerrit.get_review("Iabc")

    assert review is not None
    assert review.change_number == 555
    assert review.change_id == "Iabc"
    assert review.current_revision == "deadbeef" * 5
    assert len(review.approvals) == 2
    assert review.approvals[0]["type"] == "Code-Review"
    assert review.comments[0]["reviewer"] == "rev-1"
    assert review.comments[0]["message"] == "looks good"
    # Argv routes through change:<id> with --comments flag
    cmd = captured["cmd"]
    assert "change:Iabc" in cmd
    assert "--current-patch-set" in cmd
    assert "--all-approvals" in cmd
    assert "--comments" in cmd


def test_get_review_returns_none_when_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = _gerrit_stats_line(0) + "\n"
    _stub_subprocess(monkeypatch, stdout=stdout)
    assert mcp_gerrit.get_review("Idoesnotexist") is None


def test_get_review_rejects_blank_change_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_subprocess(monkeypatch)
    with pytest.raises(ValueError, match="change_id"):
        mcp_gerrit.get_review("")


def test_get_review_handles_missing_current_patchset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale/abandoned changes may lack ``currentPatchSet``."""
    blob = _gerrit_change_blob(number=42)
    blob.pop("currentPatchSet", None)
    blob.pop("comments", None)
    _stub_subprocess(monkeypatch, stdout=_gerrit_stdout(blob))
    review = mcp_gerrit.get_review("42")
    assert review is not None
    assert review.current_revision is None
    assert review.approvals == ()
    assert review.comments == ()


# ─── has_open_ps_for_ticket — synthetic AC ───────────────────────


def test_has_open_ps_for_ticket_true_when_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic AC: model checks if its ticket has open PS before starting work."""
    blob = _gerrit_change_blob(number=900, subject="[OP-700] in flight")
    _stub_subprocess(monkeypatch, stdout=_gerrit_stdout(blob))

    assert mcp_gerrit.has_open_ps_for_ticket("OP-700") is True


def test_has_open_ps_for_ticket_false_when_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_subprocess(monkeypatch, stdout=_gerrit_stats_line(0) + "\n")
    assert mcp_gerrit.has_open_ps_for_ticket("OP-9999") is False


def test_has_open_ps_for_ticket_false_when_subject_doesnt_mention_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated change matched by Gerrit's broad search must not trigger
    a false positive — defence in depth on top of Gerrit's own filter."""
    blob = _gerrit_change_blob(number=901, subject="[OP-705] unrelated work")
    _stub_subprocess(monkeypatch, stdout=_gerrit_stdout(blob))
    assert mcp_gerrit.has_open_ps_for_ticket("OP-700") is False


def test_has_open_ps_for_ticket_rejects_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_subprocess(monkeypatch)
    with pytest.raises(ValueError, match="ticket_key"):
        mcp_gerrit.has_open_ps_for_ticket("")


# ─── Tool dispatch surface (mcp_gerrit__*) ───────────────────────


def test_tool_handlers_are_read_only_set() -> None:
    """AC #3: only read-only tools must be registered. No push/submit handler."""
    expected = {
        "mcp_gerrit__queryChanges",
        "mcp_gerrit__getReview",
        "mcp_gerrit__hasOpenPsForTicket",
    }
    assert set(mcp_gerrit.TOOL_HANDLERS) == expected
    # Defence in depth: nothing whose name suggests a write op slipped in.
    write_words = ("push", "submit", "vote", "abandon", "merge", "create")
    for name in mcp_gerrit.TOOL_HANDLERS:
        assert not any(w in name.lower() for w in write_words), name


def test_tool_schemas_match_handler_keys() -> None:
    schema_names = {s["name"] for s in mcp_gerrit.MCP_GERRIT_TOOL_SCHEMAS}
    assert schema_names == set(mcp_gerrit.TOOL_HANDLERS)
    for schema in mcp_gerrit.MCP_GERRIT_TOOL_SCHEMAS:
        assert schema["input_schema"]["type"] == "object"
        assert "required" in schema["input_schema"]


def test_dispatch_query_changes_returns_jsonable_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _gerrit_stdout(_gerrit_change_blob(number=11), _gerrit_change_blob(number=12))
    _stub_subprocess(monkeypatch, stdout=stdout)

    result = mcp_gerrit.dispatch_tool(
        "mcp_gerrit__queryChanges",
        {"filter": "is:open", "limit": 5},
    )

    assert isinstance(result, list)
    assert [r["change_number"] for r in result] == [11, 12]
    # Result must be JSON serialisable (round-trips for tool_result content).
    json.dumps(result)


def test_dispatch_get_review_returns_jsonable(monkeypatch: pytest.MonkeyPatch) -> None:
    blob = _gerrit_change_blob(number=77, current_revision="aa" * 20)
    _stub_subprocess(monkeypatch, stdout=_gerrit_stdout(blob))

    result = mcp_gerrit.dispatch_tool(
        "mcp_gerrit__getReview",
        {"change_id": "77"},
    )
    assert result is not None
    assert result["change_number"] == 77
    json.dumps(result)


def test_dispatch_get_review_returns_none_jsonable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_subprocess(monkeypatch, stdout=_gerrit_stats_line(0) + "\n")
    result = mcp_gerrit.dispatch_tool(
        "mcp_gerrit__getReview",
        {"change_id": "Idoesnotexist"},
    )
    assert result is None


def test_dispatch_has_open_ps_for_ticket_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    blob = _gerrit_change_blob(number=42, subject="[OP-814] runner work")
    _stub_subprocess(monkeypatch, stdout=_gerrit_stdout(blob))

    result = mcp_gerrit.dispatch_tool(
        "mcp_gerrit__hasOpenPsForTicket",
        {"ticket_key": "OP-814"},
    )
    assert result == {"ticket_key": "OP-814", "has_open_ps": True}


def test_dispatch_unknown_tool_raises() -> None:
    with pytest.raises(KeyError, match="Unknown mcp_gerrit tool"):
        mcp_gerrit.dispatch_tool("mcp_gerrit__doSomethingEvil", {})


def test_is_mcp_gerrit_tool_predicate() -> None:
    assert mcp_gerrit.is_mcp_gerrit_tool("mcp_gerrit__queryChanges")
    assert not mcp_gerrit.is_mcp_gerrit_tool("Read")
    assert not mcp_gerrit.is_mcp_gerrit_tool("mcp__claude_ai_Figma__whoami")


# ─── mcp_integration registration ────────────────────────────────


def test_mcp_gerrit_registered_in_local_registry() -> None:
    """AC #1: MCP server wraps existing gerrit-ssh-cli — visible to the runner
    via the local-MCP registry."""
    assert "mcp_gerrit" in mcp_integration.registered_local_mcp_servers()


def test_local_mcp_tool_schemas_include_gerrit_tools() -> None:
    schemas = mcp_integration.local_mcp_tool_schemas("mcp_gerrit")
    names = {s["name"] for s in schemas}
    assert names == {
        "mcp_gerrit__queryChanges",
        "mcp_gerrit__getReview",
        "mcp_gerrit__hasOpenPsForTicket",
    }


def test_is_local_mcp_tool_recognises_gerrit_tools() -> None:
    assert mcp_integration.is_local_mcp_tool("mcp_gerrit__queryChanges")
    assert not mcp_integration.is_local_mcp_tool("mcp__claude_ai_Figma__whoami")
    assert not mcp_integration.is_local_mcp_tool("Read")


def test_dispatch_local_mcp_tool_routes_to_gerrit_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #2: model can call mcp_gerrit__queryChanges through the integration."""
    blob = _gerrit_change_blob(number=88, subject="[OP-814] integration check")
    _stub_subprocess(monkeypatch, stdout=_gerrit_stdout(blob))

    result = mcp_integration.dispatch_local_mcp_tool(
        "mcp_gerrit__queryChanges",
        {"filter": "is:open"},
    )
    assert isinstance(result, list)
    assert result[0]["change_number"] == 88


def test_dispatch_local_mcp_tool_unknown_raises() -> None:
    with pytest.raises(KeyError, match="Unknown local MCP tool"):
        mcp_integration.dispatch_local_mcp_tool("mcp_gerrit__nonsense", {})


def test_register_local_mcp_server_rejects_mismatched_prefix() -> None:
    with pytest.raises(ValueError, match="must start with"):
        mcp_integration.register_local_mcp_server(
            "mcp_test_server",
            tool_handlers={"different_prefix__foo": lambda i: None},
            tool_schemas=[],
        )


def test_register_local_mcp_server_replaces_on_reregister() -> None:
    """Re-registration is idempotent so module-reload during tests is safe."""
    calls: list[str] = []

    def hot_handler(input: dict[str, Any]) -> str:
        calls.append("hot")
        return "hot"

    mcp_integration.register_local_mcp_server(
        "mcp_unit_test",
        tool_handlers={"mcp_unit_test__ping": hot_handler},
        tool_schemas=[
            {
                "name": "mcp_unit_test__ping",
                "description": "ping",
                "input_schema": {"type": "object"},
            }
        ],
    )
    assert mcp_integration.is_local_mcp_tool("mcp_unit_test__ping")

    # Re-register with a different schema list — shouldn't double or fail.
    mcp_integration.register_local_mcp_server(
        "mcp_unit_test",
        tool_handlers={"mcp_unit_test__ping": hot_handler},
        tool_schemas=[
            {
                "name": "mcp_unit_test__ping",
                "description": "ping (v2)",
                "input_schema": {"type": "object"},
            }
        ],
    )
    schemas = mcp_integration.local_mcp_tool_schemas("mcp_unit_test")
    assert len(schemas) == 1
    assert schemas[0]["description"] == "ping (v2)"


# ─── Audit-trail boundary: jira_dispatch.push_to_gerrit_for_review
#     is NOT shadowed by an mcp_gerrit handler.
# ─────────────────────────────────────────────────────────────────


def test_no_push_handler_in_mcp_gerrit() -> None:
    """AC #3: PS push must still go through jira_dispatch (audit trail)."""
    from backend.agents import jira_dispatch  # noqa: F401 — import smoke-tests path

    # No handler in mcp_gerrit references push_to_gerrit_for_review.
    for name in mcp_gerrit.TOOL_HANDLERS:
        assert "push" not in name.lower()
    # The push helper still lives on jira_dispatch — single source of truth.
    assert hasattr(jira_dispatch, "push_to_gerrit_for_review")


# ─── SSH argv shape (smoke) ──────────────────────────────────────


def test_ssh_argv_uses_jira_dispatch_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argv must be built via the same bot/key resolution that jira_dispatch uses;
    we don't roll our own host/port constants."""
    captured = _stub_subprocess(monkeypatch, stdout=_gerrit_stats_line(0) + "\n")

    mcp_gerrit.query_changes("is:open", agent_class="subscription-claude")

    cmd = captured["cmd"]
    assert cmd[0] == "ssh"
    assert "-i" in cmd
    assert "-p" in cmd
    port_index = cmd.index("-p") + 1
    assert cmd[port_index] == str(mcp_gerrit.GERRIT_SSH_PORT)
    target = next(arg for arg in cmd if "@" in arg and arg.endswith(mcp_gerrit.GERRIT_SSH_HOST))
    assert "claude-bot" in target
