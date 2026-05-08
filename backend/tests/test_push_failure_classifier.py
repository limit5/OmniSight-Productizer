"""OP-760 Gerrit push-failure classifier tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.agents import jira_dispatch as jd
from backend.agents import runner_failure_classifier as pfc


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    """Load auto-runner-jira.py despite the hyphen in its filename."""

    sys.modules.pop("jira_runner_push_failure_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_push_failure_under_test",
        _RUNNER_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    agent_class = "subscription-codex"


def _patch_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    mod: Any,
    *,
    merged: bool = False,
) -> dict[str, list]:
    calls: dict[str, list] = {
        "already_merged_in_gerrit": [],
        "force_walk_to_published": [],
        "transition_back_to_todo": [],
        "add_comment": [],
        "add_label": [],
    }

    def recorder(name: str, return_value: Any = None):
        def fn(*args, **kwargs):
            calls[name].append((args, kwargs))
            return return_value
        return fn

    monkeypatch.setattr(
        mod.jira_dispatch,
        "already_merged_in_gerrit",
        recorder(
            "already_merged_in_gerrit",
            SimpleNamespace(change_number="55", subject="[OP-760] sibling")
            if merged
            else None,
        ),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "force_walk_to_published",
        recorder("force_walk_to_published"),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "transition_back_to_todo",
        recorder("transition_back_to_todo"),
    )
    monkeypatch.setattr(mod.jira_dispatch, "add_comment", recorder("add_comment"))
    monkeypatch.setattr(mod.jira_dispatch, "add_label", recorder("add_label"))
    return calls


# ── Failure categorizer ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            "remote: ! [remote rejected] HEAD -> refs/for/develop (no new changes)",
            ("no_new_changes", "force-publish"),
        ),
        ("remote: invalid author codex-bot cannot forge author", ("invalid_author", "revert")),
        (
            "remote: invalid committer codex-bot cannot forge committer",
            ("invalid_committer", "revert"),
        ),
        # OP-771 race: missing_tree now routes to force-publish so the handler
        # can detect a just-merged PS (codex CLI internal push) and walk the
        # ticket forward instead of duplicating work.
        ("remote: Missing tree 3f7abc", ("missing_tree", "force-publish")),
        (
            "remote rejected: commit 123 has missing Change-Id footer",
            ("change_id_problem", "revert"),
        ),
        ("ssh: Connection timed out", ("transient_network", "retry")),
        ("merge conflict while submitting\nConflicts: backend/x.py", ("merge_conflict", "revert")),
        ("remote: unrelated Gerrit rejection", ("unknown", "escalate")),
    ],
)
def test_categorize_push_failure_known_patterns(
    stderr: str,
    expected: tuple[str, str],
) -> None:
    assert pfc.categorize_push_failure(stderr) == expected


# ── Strategy dispatch ───────────────────────────────────────────────


def test_no_new_changes_with_merged_sibling_force_walks_to_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls = _patch_dispatch(monkeypatch, mod, merged=True)

    decision = mod._handle_gerrit_push_failure(
        _StubClient(),
        "OP-760",
        "remote: no new changes",
    )

    assert decision == ("no_new_changes", "force-publish")
    assert len(calls["already_merged_in_gerrit"]) == 1
    assert calls["already_merged_in_gerrit"][0][0][0] == "OP-760"
    assert calls["force_walk_to_published"][0][0][1] == "OP-760"
    assert "#55" in calls["add_comment"][0][0][2]
    assert "公開済み" in calls["add_comment"][0][0][2]
    assert calls["transition_back_to_todo"] == []


def test_no_new_changes_without_merged_sibling_reverts_to_todo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls = _patch_dispatch(monkeypatch, mod, merged=False)

    decision = mod._handle_gerrit_push_failure(
        _StubClient(),
        "OP-760",
        "remote: no new changes",
    )

    assert decision == ("no_new_changes", "force-publish")
    assert len(calls["already_merged_in_gerrit"]) == 1
    assert calls["force_walk_to_published"] == []
    assert "no committable changes" in calls["transition_back_to_todo"][0][0][2]


@pytest.mark.parametrize(
    ("stderr", "category"),
    [
        ("remote: invalid author cannot forge author", "invalid_author"),
        ("remote: invalid committer cannot forge committer", "invalid_committer"),
        # missing_tree moved to force-publish (OP-771 race) — covered by separate test.
        ("remote rejected because Change-Id footer is malformed", "change_id_problem"),
        ("merge conflict\nConflicts: backend/x.py", "merge_conflict"),
    ],
)
def test_revert_categories_transition_back_to_todo(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    category: str,
) -> None:
    mod = _load_jira_runner()
    calls = _patch_dispatch(monkeypatch, mod)

    decision = mod._handle_gerrit_push_failure(_StubClient(), "OP-760", stderr)

    assert decision == (category, "revert")
    reason = calls["transition_back_to_todo"][0][0][2]
    assert f"[runner-push-fail:{category}]" in reason
    assert calls["add_label"] == []


def test_missing_tree_with_merged_walks_to_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP-771 race: Missing tree + already-merged PS → walk to published."""
    mod = _load_jira_runner()
    calls = _patch_dispatch(monkeypatch, mod)
    # Stub already_merged_in_gerrit to return a merged-info object
    from types import SimpleNamespace
    monkeypatch.setattr(
        mod.jira_dispatch, "already_merged_in_gerrit",
        lambda *a, **kw: SimpleNamespace(change_number=240, change_url="x"),
    )

    decision = mod._handle_gerrit_push_failure(
        _StubClient(), "OP-771", "remote: Missing tree 4cf036f9"
    )

    assert decision == ("missing_tree", "force-publish")
    assert calls["force_walk_to_published"] != []
    assert calls["transition_back_to_todo"] == []


def test_missing_tree_without_merged_leaves_ticket_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP-771 race hardening: Missing tree + NO merged PS found → leave alone
    rather than reverting (could be timing race with submit; bridge daemon
    will walk forward when the merge event lands)."""
    mod = _load_jira_runner()
    calls = _patch_dispatch(monkeypatch, mod)
    monkeypatch.setattr(mod.jira_dispatch, "already_merged_in_gerrit", lambda *a, **kw: None)

    decision = mod._handle_gerrit_push_failure(
        _StubClient(), "OP-771", "remote: Missing tree 4cf036f9"
    )

    assert decision == ("missing_tree", "force-publish")
    assert calls["force_walk_to_published"] == []
    assert calls["transition_back_to_todo"] == []  # ← key assertion: no revert


def test_transient_network_leaves_ticket_in_progress_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls = _patch_dispatch(monkeypatch, mod)

    decision = mod._handle_gerrit_push_failure(
        _StubClient(),
        "OP-760",
        "ssh: Connection timed out",
    )

    assert decision == ("transient_network", "retry")
    assert calls["transition_back_to_todo"] == []
    assert "[runner-push-fail:transient]" in calls["add_comment"][0][0][2]


def test_unknown_failure_sets_loop_paused_label_and_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls = _patch_dispatch(monkeypatch, mod)

    decision = mod._handle_gerrit_push_failure(
        _StubClient(),
        "OP-760",
        "remote: project policy rejected this push",
    )

    assert decision == ("unknown", "escalate")
    assert "[runner-push-fail:unknown]" in calls["add_comment"][0][0][2]
    assert calls["add_label"][0][0][2] == "runner-loop-paused-pending-review"
    assert calls["transition_back_to_todo"] == []


# ── Gerrit/JIRA helpers ─────────────────────────────────────────────


def test_already_merged_in_gerrit_parses_merged_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_call(fn, cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            stdout=(
                '{"project":"omnisight/OmniSight-Productizer",'
                '"number":"55","subject":"[OP-760] sibling merged"}\n'
                '{"type":"stats","rowCount":1}\n'
            )
        )

    monkeypatch.setattr(jd.BREAKERS["gerrit_ssh"], "call", fake_call)

    merged = jd.already_merged_in_gerrit("OP-760", agent_class="subscription-codex")

    assert merged == jd.GerritMergedInfo("55", "[OP-760] sibling merged")
    assert (
        "status:merged project:omnisight/OmniSight-Productizer OP-760"
        in captured["cmd"]
    )
    assert captured["kwargs"]["timeout"] == 30


def test_force_walk_to_published_transitions_in_progress_through_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transitions: list[dict[str, str]] = []

    monkeypatch.setattr(jd, "get_issue_status", lambda client, key: "In Progress")

    def fake_request_idempotent(client, method, path, body, idem_key):
        assert method == "POST"
        assert path == "/issue/OP-760/transitions"
        transitions.append(body["transition"])

    monkeypatch.setattr(jd, "_request_idempotent", fake_request_idempotent)

    jd.force_walk_to_published(_StubClient(), "OP-760", idem_key="force-test")

    assert transitions == [
        {"id": jd.TRANSITION_IDS["to_under_review"]},
        {"id": jd.TRANSITION_IDS["to_approved"]},
        {"id": jd.TRANSITION_IDS["to_published"]},
    ]
