"""Z.1 provider rate-limit header registry shared by LLM + MP code."""

from __future__ import annotations


# Z.1 (#290) checkbox 2 (2026-04-24): per-provider rate-limit header
# name -> unified-key mapping. Keep this as the canonical source so MP cap
# detection does not grow a second provider-specific header table.
_PROVIDER_RATELIMIT_HEADERS: dict[str, dict[str, str]] = {
    "anthropic": {
        "remaining_requests": "anthropic-ratelimit-requests-remaining",
        "remaining_tokens": "anthropic-ratelimit-tokens-remaining",
        "reset_at": "anthropic-ratelimit-tokens-reset",
        "retry_after": "retry-after",
    },
    "openai": {
        "remaining_requests": "x-ratelimit-remaining-requests",
        "remaining_tokens": "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "xai": {
        "remaining_requests": "x-ratelimit-remaining-requests",
        "remaining_tokens": "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "groq": {
        "remaining_requests": "x-ratelimit-remaining-requests",
        "remaining_tokens": "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "deepseek": {
        "remaining_requests": "x-ratelimit-remaining-requests",
        "remaining_tokens": "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "together": {
        "remaining_requests": "x-ratelimit-remaining-requests",
        "remaining_tokens": "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    "openrouter": {
        "remaining_requests": "x-ratelimit-remaining-requests",
        "remaining_tokens": "x-ratelimit-remaining-tokens",
        "reset_at": "x-ratelimit-reset-tokens",
        "retry_after": "retry-after",
    },
    # Google Gemini uses a gRPC/REST API; LangChain's langchain-google-
    # genai does not currently surface per-request rate-limit headers
    # through any of the 5 paths ``_extract_response_headers`` walks,
    # so it's omitted here alongside Ollama. Revisit if an adapter
    # version lands that mirrors the SDK's ``x-goog-quota-*`` headers.
}


def provider_ratelimit_family(provider_id: str | None) -> str | None:
    """Return the Z.1 mapping key for an MP provider id, if known."""
    if not provider_id:
        return None
    provider = provider_id.strip().lower()
    if provider in _PROVIDER_RATELIMIT_HEADERS:
        return provider
    if provider.endswith("-subscription"):
        provider = provider[: -len("-subscription")]
        if provider in _PROVIDER_RATELIMIT_HEADERS:
            return provider
    return None


__all__ = [
    "_PROVIDER_RATELIMIT_HEADERS",
    "provider_ratelimit_family",
]
