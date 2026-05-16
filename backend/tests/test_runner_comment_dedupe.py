"""OP-1150 runner JIRA comment dedupe contract tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend.agents import runner_comment_dedupe as dedupe


BASE_NOW = datetime(2026, 5, 16, 8, 0, tzinfo=timezone.utc)
TAG = "[runner-capability-blocked]"
BODY = f"{TAG}\nCapability blocked."


class FakeJiraClient:
    agent_class = "subscription-codex"
    bot_email = "codex-bot@example.com"

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    def post_comment(self, ticket_id: str, body: str) -> None:
        self.posts.append((ticket_id, body))


@pytest.fixture(autouse=True)
def reset_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    dedupe._reset_for_tests()
    monkeypatch.setattr(dedupe, "_now", lambda: BASE_NOW)
    monkeypatch.delenv(dedupe.COMMENT_DEDUPE_ENABLED_ENV, raising=False)


def _jira_comment(tag: str, created: datetime) -> dict[str, Any]:
    return {
        "created": created.isoformat(),
        "body": {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": f"{tag}\nbody"}],
                }
            ],
        },
    }


def test_first_post_returns_true() -> None:
    assert dedupe.should_post("OP-1150", TAG, BODY)


def test_same_tag_within_window_returns_false() -> None:
    from backend import metrics as m

    m.reset_for_tests()

    assert dedupe.should_post("OP-1150", TAG, BODY)
    assert not dedupe.should_post("OP-1150", TAG, BODY)

    if m.is_available():
        samples = list(m.runner_comment_suppressed_total.collect()[0].samples)
        total = sum(
            sample.value
            for sample in samples
            if sample.labels.get("ticket") == "OP-1150"
            and sample.labels.get("tag") == TAG
            and sample.name.endswith("_total")
        )
        assert total == 1


def test_different_tag_returns_true() -> None:
    assert dedupe.should_post("OP-1150", TAG, BODY)
    assert dedupe.should_post(
        "OP-1150",
        "[runner-pushed-to-gerrit]",
        "[runner-pushed-to-gerrit]\nPushed.",
    )


def test_post_after_window_expires_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    assert dedupe.should_post("OP-1150", TAG, BODY)

    monkeypatch.setattr(dedupe, "_now", lambda: BASE_NOW + timedelta(minutes=6))

    assert dedupe.should_post("OP-1150", TAG, BODY)


def test_different_ticket_returns_true() -> None:
    assert dedupe.should_post("OP-1150", TAG, BODY)
    assert dedupe.should_post("OP-1151", TAG, BODY)


def test_cross_instance_dedupe_via_jira_query(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.agents import jira_dispatch

    calls: list[tuple[Any, str, str]] = []

    def fake_request(client: Any, method: str, path: str) -> dict[str, Any]:
        calls.append((client, method, path))
        return {
            "comments": [
                _jira_comment(TAG, BASE_NOW - timedelta(minutes=1)),
            ],
        }

    monkeypatch.setattr(jira_dispatch, "_request", fake_request)

    assert not dedupe.should_post(
        "OP-1150",
        TAG,
        BODY,
        jira_client=FakeJiraClient(),  # empty local cache; JIRA is source of truth
    )
    assert len(calls) == 1
    assert calls[0][1] == "GET"
    assert "/issue/OP-1150/comment" in calls[0][2]


def test_jira_api_failure_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.agents import jira_dispatch

    def fail_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("jira down")

    monkeypatch.setattr(jira_dispatch, "_request", fail_request)

    assert dedupe.should_post(
        "OP-1150",
        TAG,
        BODY,
        jira_client=FakeJiraClient(),
    )


def test_maybe_post_comment_env_flag_default_off_posts_without_dedupe() -> None:
    client = FakeJiraClient()

    assert dedupe.maybe_post_comment(client, "OP-1150", TAG, BODY)
    assert dedupe.maybe_post_comment(client, "OP-1150", TAG, BODY)

    assert client.posts == [("OP-1150", BODY), ("OP-1150", BODY)]


def test_maybe_post_comment_env_flag_enabled_suppresses_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents import jira_dispatch

    client = FakeJiraClient()
    monkeypatch.setenv(dedupe.COMMENT_DEDUPE_ENABLED_ENV, "1")
    monkeypatch.setattr(
        jira_dispatch,
        "_request",
        lambda *_args, **_kwargs: {"comments": []},
    )

    assert dedupe.maybe_post_comment(client, "OP-1150", TAG, BODY)
    assert not dedupe.maybe_post_comment(client, "OP-1150", TAG, BODY)

    assert client.posts == [("OP-1150", BODY)]
