"""OP-1165 pre-pickup capability gate tests."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agents import runner_comment_dedupe
from backend.agents import scheduler


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_pre_pickup_cap_gate_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_pre_pickup_cap_gate_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snapshot(labels: tuple[str, ...]) -> scheduler.TicketSnapshot:
    return scheduler.TicketSnapshot(
        key="OP-1165",
        component="META",
        fix_version=None,
        created_at="2026-05-16T00:00:00Z",
        days_since_created=1.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=labels,
    )


class _Client:
    agent_class = "subscription-codex"
    bot_email = "codex-bot@example.com"

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    def post_comment(self, ticket_id: str, body: str) -> None:
        self.posts.append((ticket_id, body))


class _Matrix:
    def __init__(self, caps: set[str]) -> None:
        self.caps = frozenset(caps)
        self.calls: list[
            tuple[str, tuple[str, ...], str, tuple[str, ...], str | None]
        ] = []

    def resolve_for_areas(
        self,
        ticket_type: str,
        areas: list[str],
        tier: str,
        *,
        labels: list[str],
        ticket_id: str | None = None,
    ) -> frozenset[str]:
        self.calls.append((ticket_type, tuple(areas), tier, tuple(labels), ticket_id))
        return self.caps

    def resolve(
        self,
        ticket_type: str,
        area: str,
        tier: str,
        *,
        labels: list[str],
        ticket_id: str | None = None,
    ) -> frozenset[str]:
        self.calls.append((ticket_type, (area,), tier, tuple(labels), ticket_id))
        return self.caps


def _patch_issue_fetch(
    monkeypatch: pytest.MonkeyPatch,
    mod: Any,
    labels: list[str],
) -> None:
    def fake_request(_client: Any, method: str, path: str) -> dict[str, Any]:
        assert method == "GET"
        assert path == "/issue/OP-1165?fields=labels,issuetype"
        return {"fields": {"labels": labels, "issuetype": {"name": "Story"}}}

    monkeypatch.setattr(mod.jira_dispatch, "_request", fake_request)


def test_gate_allows_pickup_when_matrix_grants_required_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    labels = ["area:docs", "tier:S", "type:docs"]
    _patch_issue_fetch(monkeypatch, mod, labels)
    matrix = _Matrix({"jira_update", "code_edit", "gerrit_push"})

    ok, reason = mod._pre_pickup_capability_ok(
        _Client(), _snapshot(tuple(labels)), matrix
    )

    assert ok
    assert reason is None
    assert matrix.calls == [("Story", ("docs",), "S", tuple(labels), "OP-1165")]


def test_gate_blocks_pickup_when_matrix_returns_safe_default_for_required_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend import metrics as m

    mod = _load_jira_runner()
    labels = ["area:docs", "tier:S", "type:docs"]
    _patch_issue_fetch(monkeypatch, mod, labels)
    matrix = _Matrix({"mcp_search", "memory_recall"})
    if m.is_available():
        m.reset_for_tests()

    ok, reason = mod._pre_pickup_capability_ok(
        _Client(), _snapshot(tuple(labels)), matrix
    )

    assert not ok
    assert reason == (
        "capability-mismatch: need=code_edit,gerrit_push,jira_update "
        "have=mcp_search,memory_recall"
    )
    if m.is_available():
        body = m.render_exposition()[0].decode()
        assert (
            'omnisight_runner_pre_pickup_cap_gate_blocked_total'
            '{area="docs",issuetype="Story",tier="S"} 1.0'
        ) in body


def test_gate_off_via_env_flag_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_jira_runner()
    matrix = _Matrix(set())
    monkeypatch.setenv(mod.PRE_PICKUP_CAP_GATE_ENV, "off")

    def fail_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("gate=off must not fetch JIRA ticket metadata")

    monkeypatch.setattr(mod.jira_dispatch, "_request", fail_request)

    ok, reason = mod._pre_pickup_capability_ok(
        _Client(),
        _snapshot(("area:docs", "tier:S", "type:docs")),
        matrix,
    )

    assert ok
    assert reason is None
    assert matrix.calls == []


def test_gate_emits_dedupe_safe_comment_via_OP_1150_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    client = _Client()
    snapshot = _snapshot(("area:docs", "tier:S", "type:docs"))
    runner_comment_dedupe._reset_for_tests()
    monkeypatch.setenv(runner_comment_dedupe.COMMENT_DEDUPE_ENABLED_ENV, "1")

    def fake_request(_client: Any, method: str, path: str) -> dict[str, Any]:
        assert method == "GET"
        assert path == "/issue/OP-1165/comment?orderBy=-created&maxResults=50"
        return {"comments": []}

    monkeypatch.setattr(mod.jira_dispatch, "_request", fake_request)

    mod._post_pre_pickup_capability_block(
        client, snapshot, "capability-mismatch: need=code_edit have=mcp_search"
    )
    mod._post_pre_pickup_capability_block(
        client, snapshot, "capability-mismatch: need=code_edit have=mcp_search"
    )

    assert len(client.posts) == 1
    assert client.posts[0][0] == "OP-1165"
    assert client.posts[0][1].startswith(mod.PRE_PICKUP_CAP_BLOCKED_TAG)
