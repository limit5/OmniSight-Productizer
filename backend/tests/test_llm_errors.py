"""Tests for LLM Error Handling (Phase 42).

Covers:
- Error classifier (all categories)
- Status code extraction
- Retry-After extraction
- Provider action mapping
- _handle_llm_error integration
"""

from __future__ import annotations

import pytest

from backend.llm_errors import (
    classify_llm_error,
    LLMErrorCategory,
    RETRY_BEHAVIOR,
    _extract_status_code,
    _extract_retry_after,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Error Classification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClassifyByStatusCode:

    def test_429_rate_limited(self):
        r = classify_llm_error(Exception("Error code: 429 - Rate limit exceeded"))
        assert r["category"] == LLMErrorCategory.RATE_LIMITED
        assert r["status_code"] == 429
        assert r["retryable"] is True
        assert r["failover"] is True

    def test_529_anthropic_overloaded(self):
        r = classify_llm_error(Exception("status_code: 529 Overloaded"))
        assert r["category"] == LLMErrorCategory.RATE_LIMITED
        assert r["status_code"] == 529

    def test_401_auth_failed(self):
        r = classify_llm_error(Exception("status_code: 401 Invalid API Key"))
        assert r["category"] == LLMErrorCategory.AUTH_FAILED
        assert r["provider_action"] == "permanent_disable"
        assert r["retryable"] is False

    def test_403_forbidden(self):
        r = classify_llm_error(Exception("403 Forbidden"))
        assert r["category"] == LLMErrorCategory.AUTH_FAILED

    def test_402_billing(self):
        r = classify_llm_error(Exception("402 insufficient_quota"))
        assert r["category"] == LLMErrorCategory.BILLING_EXHAUSTED
        assert r["provider_action"] == "permanent_disable"

    def test_404_model_not_found(self):
        r = classify_llm_error(Exception("404 Model not found: gpt-5"))
        assert r["category"] == LLMErrorCategory.MODEL_NOT_FOUND
        assert r["retryable"] is False

    def test_500_server_error(self):
        r = classify_llm_error(Exception("status_code: 500 Internal Server Error"))
        assert r["category"] == LLMErrorCategory.SERVER_ERROR
        assert r["failover"] is True

    def test_502_server_overload(self):
        r = classify_llm_error(Exception("502 Bad Gateway"))
        assert r["category"] == LLMErrorCategory.SERVER_OVERLOAD
        assert r["retryable"] is True

    def test_503_service_unavailable(self):
        r = classify_llm_error(Exception("503 Service Unavailable"))
        assert r["category"] == LLMErrorCategory.SERVER_OVERLOAD

    def test_504_gateway_timeout(self):
        r = classify_llm_error(Exception("504 Gateway Timeout"))
        assert r["category"] == LLMErrorCategory.GATEWAY_TIMEOUT

    def test_413_context_overflow(self):
        r = classify_llm_error(Exception("413 Request too large"))
        assert r["category"] == LLMErrorCategory.CONTEXT_OVERFLOW
        assert r["retryable"] is True
        assert r["failover"] is False


class TestClassifyByMessage:

    def test_context_length_in_400(self):
        r = classify_llm_error(Exception("400 This model's maximum context length is 128000"))
        assert r["category"] == LLMErrorCategory.CONTEXT_OVERFLOW

    def test_rate_limit_no_code(self):
        r = classify_llm_error(Exception("Rate limit exceeded, please retry"))
        assert r["category"] == LLMErrorCategory.RATE_LIMITED

    def test_invalid_api_key(self):
        r = classify_llm_error(Exception("invalid_api_key: The key you provided is not valid"))
        assert r["category"] == LLMErrorCategory.AUTH_FAILED

    def test_insufficient_quota(self):
        r = classify_llm_error(Exception("insufficient_quota: You exceeded your billing limit"))
        assert r["category"] == LLMErrorCategory.BILLING_EXHAUSTED

    def test_content_blocked(self):
        r = classify_llm_error(Exception("Content blocked by safety filter"))
        assert r["category"] == LLMErrorCategory.CONTENT_BLOCKED
        assert r["retryable"] is False

    def test_gemini_recitation(self):
        r = classify_llm_error(Exception("RECITATION: blocked due to content policy"))
        assert r["category"] == LLMErrorCategory.CONTENT_BLOCKED

    def test_connection_refused(self):
        r = classify_llm_error(ConnectionError("Connection refused"))
        assert r["category"] == LLMErrorCategory.NETWORK_ERROR
        assert r["retryable"] is True

    def test_timeout_error(self):
        r = classify_llm_error(TimeoutError("Request timed out"))
        assert r["category"] == LLMErrorCategory.NETWORK_ERROR

    def test_ollama_gpu_oom(self):
        r = classify_llm_error(Exception("CUDA out of memory"))
        assert r["category"] == LLMErrorCategory.SERVER_ERROR

    def test_model_not_found_message(self):
        r = classify_llm_error(Exception("model_not_found: llama-999b does not exist"))
        assert r["category"] == LLMErrorCategory.MODEL_NOT_FOUND

    def test_unknown_error(self):
        r = classify_llm_error(Exception("Something completely unexpected"))
        assert r["category"] == LLMErrorCategory.UNKNOWN


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Status Code + Retry-After Extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStatusCodeExtraction:

    def test_from_message_pattern(self):
        assert _extract_status_code(Exception("status_code: 429")) == 429

    def test_from_attribute(self):
        exc = Exception("error")
        exc.status_code = 503  # type: ignore
        assert _extract_status_code(exc) == 503

    def test_429_in_message(self):
        assert _extract_status_code(Exception("Error 429 too many requests")) == 429

    def test_no_code(self):
        assert _extract_status_code(Exception("generic error")) is None


class TestRetryAfterExtraction:

    def test_from_message(self):
        assert _extract_retry_after(Exception("Retry-After: 30 seconds")) == 30

    def test_from_headers_dict(self):
        exc = Exception("error")
        exc.headers = {"Retry-After": "15"}  # type: ignore
        assert _extract_retry_after(exc) == 15

    def test_no_retry_after(self):
        assert _extract_retry_after(Exception("generic error")) is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Retry Behavior Config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRetryBehavior:

    def test_all_categories_have_behavior(self):
        for cat in [LLMErrorCategory.RATE_LIMITED, LLMErrorCategory.SERVER_OVERLOAD,
                     LLMErrorCategory.GATEWAY_TIMEOUT, LLMErrorCategory.SERVER_ERROR,
                     LLMErrorCategory.AUTH_FAILED, LLMErrorCategory.BILLING_EXHAUSTED,
                     LLMErrorCategory.CONTEXT_OVERFLOW, LLMErrorCategory.MODEL_NOT_FOUND,
                     LLMErrorCategory.CONTENT_BLOCKED, LLMErrorCategory.NETWORK_ERROR,
                     LLMErrorCategory.UNKNOWN]:
            assert cat in RETRY_BEHAVIOR

    def test_rate_limited_is_retryable_with_backoff(self):
        b = RETRY_BEHAVIOR[LLMErrorCategory.RATE_LIMITED]
        assert b["retryable"] is True
        assert b["backoff"] is True
        assert b["max_retries"] >= 3

    def test_auth_failed_not_retryable(self):
        b = RETRY_BEHAVIOR[LLMErrorCategory.AUTH_FAILED]
        assert b["retryable"] is False
        assert b["max_retries"] == 0

    def test_context_overflow_retryable_once(self):
        b = RETRY_BEHAVIOR[LLMErrorCategory.CONTEXT_OVERFLOW]
        assert b["retryable"] is True
        assert b["failover"] is False
        assert b["max_retries"] == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _handle_llm_error Integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHandleLLMError:

    def test_context_overflow_returns_message(self):
        from backend.agents.nodes import _handle_llm_error
        result = _handle_llm_error(
            Exception("400 maximum context length exceeded"),
            "firmware", "",
        )
        assert result is not None
        assert result.get("messages") is not None

    def test_auth_failed_returns_none(self):
        from backend.agents.nodes import _handle_llm_error
        result = _handle_llm_error(
            Exception("401 Invalid API Key"),
            "firmware", "",
        )
        # Returns None to fall through to rule-based
        assert result is None

    def test_unknown_error_returns_none(self):
        from backend.agents.nodes import _handle_llm_error
        result = _handle_llm_error(
            Exception("Something weird happened"),
            "software", "",
        )
        assert result is None
