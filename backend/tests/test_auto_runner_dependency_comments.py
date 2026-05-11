"""OP-955 — Runner dependency-blocked comment deduplication.

Reproduces the OP-911 50-comment-spam scenario and asserts the
label-presence-gated comment path caps it at 1 comment per blocker
transition. Four cases per the ticket test plan:

1. first-time-blocked        → 1 comment + marker added
2. already-blocked            → 0 comments (the OP-911 bug fix)
3. blocker-changes            → 1 comment + label set swapped
4. all-clear                  → 1 "unblocked" comment + all markers removed
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd
from backend.agents import scheduler

_RUNNER_PATH = REPO_ROOT / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_dep_comments_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_dep_comments_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snapshot(
    key: str = "OP-911",
    labels: tuple[str, ...] = ("class:subscription-codex", "scope:runner-pipeline"),
) -> scheduler.TicketSnapshot:
    return scheduler.TicketSnapshot(
        key=key,
        component="META",
        fix_version=None,
        created_at="2026-05-11T21:17:00.000+0000",
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


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Patch the dispatch helpers so we can assert exact call sequences.

    ``idem_key=None`` in the mock signatures matches the production
    signature so the runner's idempotency keys flow through unchanged.
    """
    mod = _load_jira_runner()
    calls: dict[str, list] = {
        "labels_added": [],
        "labels_removed": [],
        "comments": [],
    }
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_label",
        lambda c, k, label, idem_key=None: calls["labels_added"].append((k, label)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "remove_label",
        lambda c, k, label, idem_key=None: calls["labels_removed"].append((k, label)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda c, k, text, idem_key=None: calls["comments"].append(
            {"key": k, "text": text, "idem_key": idem_key}
        ),
    )
    calls["__mod__"] = mod
    return calls


# Case 1 ─────────────────────────────────────────────────────────────


def test_first_time_blocked_writes_one_comment_and_one_marker(
    captured: dict[str, list],
) -> None:
    """No marker yet → one label + one comment with the dated idem key."""
    mod = captured["__mod__"]
    snapshot = _snapshot("OP-911")  # no waiting-* labels yet

    mod._add_dependency_waiting_marker(
        _client(),
        snapshot,
        "blocked-by:OP-900 blocked by OP-900 (state=Under Review)",
    )

    assert captured["labels_added"] == [("OP-911", "runner-blocked:waiting-OP-900")]
    assert captured["labels_removed"] == []
    assert len(captured["comments"]) == 1
    comment = captured["comments"][0]
    assert comment["key"] == "OP-911"
    assert "[runner-dependency-blocked]" in comment["text"]
    assert "blocked-by:OP-900" in comment["text"]
    assert comment["idem_key"] is not None
    assert comment["idem_key"].startswith("dep-blocked-OP-911-OP-900-")


# Case 2 ─────────────────────────────────────────────────────────────


def test_already_blocked_same_blocker_writes_zero_comments(
    captured: dict[str, list],
) -> None:
    """The OP-911 50-comment-spam scenario: marker present, same blocker.

    Simulates 50 ticks of the same blocked state. Without the OP-955 fix
    each tick posted a redundant ``[runner-dependency-blocked]`` comment
    (50 total). With the fix the comment side is silently skipped.
    """
    mod = captured["__mod__"]
    snapshot = _snapshot(
        "OP-911",
        labels=("runner-blocked:waiting-OP-900", "scope:runner-pipeline"),
    )

    for _ in range(50):
        mod._add_dependency_waiting_marker(
            _client(),
            snapshot,
            "blocked-by:OP-900 blocked by OP-900 (state=Under Review)",
        )

    assert captured["comments"] == []
    assert captured["labels_added"] == []
    assert captured["labels_removed"] == []


# Case 3 ─────────────────────────────────────────────────────────────


def test_blocker_changes_swaps_marker_and_writes_one_new_comment(
    captured: dict[str, list],
) -> None:
    """Was waiting OP-900, blocker shifts to OP-904 → swap + 1 comment."""
    mod = captured["__mod__"]
    snapshot = _snapshot(
        "OP-911",
        labels=("runner-blocked:waiting-OP-900", "scope:runner-pipeline"),
    )

    mod._add_dependency_waiting_marker(
        _client(),
        snapshot,
        "blocked-by:OP-904 blocked by OP-904 (state=In Progress)",
    )

    assert captured["labels_removed"] == [("OP-911", "runner-blocked:waiting-OP-900")]
    assert captured["labels_added"] == [("OP-911", "runner-blocked:waiting-OP-904")]
    assert len(captured["comments"]) == 1
    comment = captured["comments"][0]
    assert "blocked-by:OP-904" in comment["text"]
    assert comment["idem_key"].startswith("dep-blocked-OP-911-OP-904-")


# Case 4 ─────────────────────────────────────────────────────────────


def test_all_clear_writes_one_unblocked_comment_and_removes_markers(
    captured: dict[str, list],
) -> None:
    """All blockers resolved → one "unblocked" comment + all markers gone."""
    mod = captured["__mod__"]
    snapshot = _snapshot(
        "OP-911",
        labels=(
            "runner-blocked:waiting-OP-900",
            "runner-blocked:waiting-OP-904",
            "scope:runner-pipeline",
        ),
    )

    mod._clear_dependency_waiting_markers(_client(), snapshot)

    assert sorted(captured["labels_removed"]) == [
        ("OP-911", "runner-blocked:waiting-OP-900"),
        ("OP-911", "runner-blocked:waiting-OP-904"),
    ]
    assert captured["labels_added"] == []
    assert len(captured["comments"]) == 1
    comment = captured["comments"][0]
    assert comment["key"] == "OP-911"
    assert "[runner-dependency-unblocked]" in comment["text"]
    assert comment["idem_key"].startswith("dep-unblocked-OP-911-")


def test_all_clear_on_never_blocked_ticket_is_silent(
    captured: dict[str, list],
) -> None:
    """Tickets that never carried a waiting-* marker stay silent on pickup."""
    mod = captured["__mod__"]
    snapshot = _snapshot("OP-911")  # no waiting-* labels

    mod._clear_dependency_waiting_markers(_client(), snapshot)

    assert captured["labels_removed"] == []
    assert captured["comments"] == []
