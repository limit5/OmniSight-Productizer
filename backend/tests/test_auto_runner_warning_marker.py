"""OP-1057 runner handling for post-push self-fix warnings."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from backend.agents import jira_dispatch as jd


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_warning_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_warning_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    agent_class = "subscription-codex"


def test_warning_uses_distinct_marker(monkeypatch) -> None:
    mod = _load_jira_runner()
    comments: list[str] = []
    result = jd.GerritPushResult(
        success=True,
        change_number=42,
        change_url="https://x/+/42",
        detail="ok",
        post_push_warning="self-fix REST 503",
    )

    monkeypatch.setattr(mod, "_finalize_under_review", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda client, key, text, idem_key=None: comments.append(text),
    )

    mod._finalize_successful_push(_StubClient(), "OP-1057", result)

    assert len(comments) == 1
    assert comments[0].startswith("[runner-pre-review-self-fix-warning]")
    assert not comments[0].startswith("[runner-push-fail]")
    assert "self-fix REST 503" in comments[0]


def test_warning_path_calls_finalize_under_review(monkeypatch) -> None:
    mod = _load_jira_runner()
    finalize_calls: list[tuple[str, str, int | None]] = []
    result = jd.GerritPushResult(
        success=True,
        change_number=42,
        change_url="https://x/+/42",
        detail="ok",
        post_push_warning="self-fix REST 503",
    )

    def fake_finalize(client, key, change_url, change_number=None):
        finalize_calls.append((key, change_url, change_number))

    monkeypatch.setattr(mod, "_finalize_under_review", fake_finalize)
    monkeypatch.setattr(mod.jira_dispatch, "add_comment", lambda *args, **kwargs: None)

    mod._finalize_successful_push(_StubClient(), "OP-1057", result)

    assert finalize_calls == [("OP-1057", "https://x/+/42", 42)]
