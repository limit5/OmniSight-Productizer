"""Shared rate-limit header/key contract for Z.1 and MP.

Z.1 owns provider HTTP header normalisation; MP subscription adapters consume
the same normalised payload keys when surfacing cap signals to routing.  Keep
the literal keys here so the two paths cannot drift independently.
"""

from __future__ import annotations

REMAINING_REQUESTS_KEY = "remaining_requests"
REMAINING_TOKENS_KEY = "remaining_tokens"
RESET_AT_TS_KEY = "reset_at_ts"
RETRY_AFTER_S_KEY = "retry_after_s"

NORMALIZED_RATELIMIT_KEYS = frozenset(
    {
        REMAINING_REQUESTS_KEY,
        REMAINING_TOKENS_KEY,
        RESET_AT_TS_KEY,
        RETRY_AFTER_S_KEY,
    }
)

PROVIDER_RATELIMIT_HEADER_KEYS = {
    "anthropic": {
        REMAINING_REQUESTS_KEY: "anthropic-ratelimit-requests-remaining",
        REMAINING_TOKENS_KEY: "anthropic-ratelimit-tokens-remaining",
        "reset_at": "anthropic-ratelimit-tokens-reset",
        "retry_after": "retry-after",
    },
    "openai": {
        REMAINING_REQUESTS_KEY: "x-ratelimit-remaining-requests",
        REMAINING_TOKENS_KEY: "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "xai": {
        REMAINING_REQUESTS_KEY: "x-ratelimit-remaining-requests",
        REMAINING_TOKENS_KEY: "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "groq": {
        REMAINING_REQUESTS_KEY: "x-ratelimit-remaining-requests",
        REMAINING_TOKENS_KEY: "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "deepseek": {
        REMAINING_REQUESTS_KEY: "x-ratelimit-remaining-requests",
        REMAINING_TOKENS_KEY: "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "together": {
        REMAINING_REQUESTS_KEY: "x-ratelimit-remaining-requests",
        REMAINING_TOKENS_KEY: "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "openrouter": {
        REMAINING_REQUESTS_KEY: "x-ratelimit-remaining-requests",
        REMAINING_TOKENS_KEY: "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
}

__all__ = [
    "NORMALIZED_RATELIMIT_KEYS",
    "PROVIDER_RATELIMIT_HEADER_KEYS",
    "REMAINING_REQUESTS_KEY",
    "REMAINING_TOKENS_KEY",
    "RESET_AT_TS_KEY",
    "RETRY_AFTER_S_KEY",
]
