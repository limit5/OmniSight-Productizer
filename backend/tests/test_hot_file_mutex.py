"""OP-1174 hot-file claim-level pickup mutex tests."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd
from backend.agents import scheduler
from backend.agents import scope_to_paths


def _snapshot(
    key: str = "OP-1174",
    labels: tuple[str, ...] = ("class:subscription-codex",),
) -> scheduler.TicketSnapshot:
    return scheduler.TicketSnapshot(
        key=key,
        component="META",
        fix_version=None,
        created_at="2026-05-16T00:00:00.000+0000",
        days_since_created=0.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=labels,
    )


def _client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic test",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


def test_non_hot_file_skip_no_overlap_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jd, "_open_bot_owned_file_owners", lambda: {})
    monkeypatch.setattr(
        jd,
        "_check_active_claim_hot_overlap",
        lambda *a, **kw: pytest.fail("non-hot file must skip JIRA claim query"),
    )

    ok, reason = jd.file_mutex_check(
        _snapshot(),
        description="## Files / Paths\n- backend/agents/jira_dispatch.py\n",
    )

    assert ok is True
    assert reason == "no collision"


def test_hot_file_with_no_other_claim_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_request(client, method, path, body=None):
        captured["client"] = client
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"issues": []}

    monkeypatch.setattr(jd, "_open_bot_owned_file_owners", lambda: {})
    monkeypatch.setattr(jd, "make_client", lambda agent_class, instance_id=None: _client())
    monkeypatch.setattr(jd, "_request", fake_request)

    ok, reason = jd.file_mutex_check(
        _snapshot(),
        description="## Files / Paths\n- backend/agents/reflection_rag.py\n",
    )

    assert ok is True
    assert reason == "no collision"
    assert captured["method"] == "POST"
    assert captured["path"] == "/search/jql"
    jql = captured["body"]["jql"]
    assert 'labels ~ "claim:*"' in jql
    assert 'key != "OP-1174"' in jql


def test_hot_file_with_concurrent_claim_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(client, method, path, body=None):
        assert method == "POST"
        assert path == "/search/jql"
        return {
            "issues": [{
                "key": "OP-142",
                "fields": {"labels": ["claim:codex-1:0000000000000001-aaaaaaaa"]},
            }],
        }

    monkeypatch.setattr(jd, "_open_bot_owned_file_owners", lambda: {})
    monkeypatch.setattr(jd, "make_client", lambda agent_class, instance_id=None: _client())
    monkeypatch.setattr(jd, "_request", fake_request)
    monkeypatch.setattr(
        jd,
        "fetch_description",
        lambda client, key: "## Files / Paths\n- backend/agents/reflection_rag.py\n",
    )

    ok, reason = jd.file_mutex_check(
        _snapshot(key="OP-143"),
        description="## Files / Paths\n- backend/agents/reflection_rag.py\n",
    )

    assert ok is False
    assert "hot-file claim collision" in reason
    assert "backend/agents/reflection_rag.py" in reason
    assert "OP-142" in reason
    assert "[runner-hot-file-mutex]" in reason


def test_hot_file_jira_unreachable_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jd, "_open_bot_owned_file_owners", lambda: {})
    monkeypatch.setattr(jd, "make_client", lambda agent_class, instance_id=None: _client())
    monkeypatch.setattr(
        jd,
        "_request",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("JIRA 503")),
    )

    ok, reason = jd.file_mutex_check(
        _snapshot(),
        description="## Files / Paths\n- backend/agents/reflection_rag.py\n",
    )

    assert ok is True
    assert "no open-PS collision" in reason
    assert "hot-file claim query failed" in reason
    assert "RuntimeError" in reason


def test_hot_file_set_is_documented_with_rationale() -> None:
    assert scope_to_paths.HOT_FILES == frozenset({
        "backend/agents/reflection_rag.py",
        "docs/operations/agent-rpg-system.md",
        "backend/metrics.py",
        "backend/agents/capability_matrix.py",
    })

    source = inspect.getsource(scope_to_paths)
    assert "OP-142 vs OP-143 conflict 2026-05-16" in source
    assert "OP-130 vs OP-137 vs others 2026-05-16" in source
    assert "OP-1148/1165/1167/1168 codex pool storm 2026-05-16" in source
    assert "OP-1148/1165 pair" in source
