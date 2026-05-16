"""MP.W10.1 -- provider rate-limit cap signal detection."""

from __future__ import annotations

import json

from backend.agents.provider_ratelimit_cap import ratelimit_cap_signal


def test_unknown_provider_returns_none() -> None:
    assert ratelimit_cap_signal(
        "ollama",
        "x-ratelimit-remaining-requests: 0",
        kind="provider_cap",
    ) is None


def test_json_headers_remaining_request_exhaustion_returns_signal() -> None:
    payload = {
        "headers": {
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-remaining-tokens": "100",
            "retry-after": "15.9",
            "x-ratelimit-reset-tokens": "123",
        },
    }

    assert ratelimit_cap_signal(
        "openai-subscription",
        json.dumps(payload),
        kind="provider_cap",
    ) == {
        "kind": "provider_cap",
        "retry_after_s": 15,
        "reset_at": 123,
    }


def test_nested_list_rate_limit_remaining_tokens_exhaustion_returns_signal() -> None:
    payload = {
        "events": [
            {"message": "not headers"},
            {
                "rate_limit": {
                    "x-ratelimit-remaining-requests": "3",
                    "x-ratelimit-remaining-tokens": 0,
                    "retry-after": "-5",
                    "x-ratelimit-reset-tokens": "-1",
                },
            },
        ],
    }

    assert ratelimit_cap_signal(
        "xai",
        json.dumps(payload),
        kind="provider_cap",
    ) == {
        "kind": "provider_cap",
        "retry_after_s": 0,
        "reset_at": 0,
    }


def test_text_headers_are_used_when_json_payloads_do_not_signal() -> None:
    assert ratelimit_cap_signal(
        "deepseek",
        '{"headers": {"x-ratelimit-remaining-requests": "4"}}',
        "stderr: x-ratelimit-remaining-tokens=0 retry-after: 30",
        kind="provider_cap",
    ) == {
        "kind": "provider_cap",
        "retry_after_s": 30,
    }


def test_non_exhausted_or_unparseable_remaining_values_return_none() -> None:
    assert ratelimit_cap_signal(
        "openrouter",
        """
        not json
        {"response_headers": {
            "x-ratelimit-remaining-requests": true,
            "x-ratelimit-remaining-tokens": "many",
            "retry-after": "60"
        }}
        """,
        kind="provider_cap",
    ) is None
