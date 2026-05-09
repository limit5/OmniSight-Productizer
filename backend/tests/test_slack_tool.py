"""OP-113 — Slack agent tool tests (respx-mocked)."""

from __future__ import annotations

import httpx
import respx

from backend.agents.tool_dispatcher import get_default_dispatcher
from backend.agents.tools.slack_tool import (
    SLACK_POST_MESSAGE_URL,
    SCOPE_SURRENDERED,
    slack_post_message,
)


@respx.mock
def test_slack_post_message_posts_expected_payload(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SLACK_TOKEN", "xoxb-test-token")
    route = respx.post(SLACK_POST_MESSAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "channel": "C123", "ts": "1710000000.000100"},
        ),
    )

    result = slack_post_message("C123", "hello from OmniSight")

    assert result["ok"] is True
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer xoxb-test-token"
    assert req.headers["content-type"] == "application/json; charset=utf-8"
    assert httpx.Response(200, content=req.read()).json() == {
        "channel": "C123",
        "text": "hello from OmniSight",
    }


@respx.mock
def test_slack_post_message_skips_when_token_missing(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_SLACK_TOKEN", raising=False)

    result = slack_post_message("C123", "hello")

    assert result == {
        "ok": False,
        "skipped": True,
        "reason": f"{SCOPE_SURRENDERED} Slack token not provisioned",
    }
    assert respx.calls.call_count == 0


def test_slack_tool_registered_in_default_dispatcher():
    assert get_default_dispatcher().has_handler("SlackPostMessage") is True
