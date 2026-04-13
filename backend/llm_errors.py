"""LLM Error Classifier — unified error categorization across all 9 providers.

Parses exceptions from LangChain SDK calls and classifies them into
actionable categories for retry, failover, or user notification.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class LLMErrorCategory:
    """Error categories with their retry/failover behavior."""
    RATE_LIMITED = "rate_limited"          # 429, 529 — retry with backoff
    SERVER_OVERLOAD = "server_overload"    # 502, 503 — retry with backoff
    GATEWAY_TIMEOUT = "gateway_timeout"    # 504, 408 — retry with backoff
    SERVER_ERROR = "server_error"          # 500 — failover to next provider
    AUTH_FAILED = "auth_failed"            # 401, 403 — permanent, notify user
    BILLING_EXHAUSTED = "billing_exhausted"  # 402 — permanent, switch provider
    CONTEXT_OVERFLOW = "context_overflow"  # 400 + context_length — compress + retry
    MODEL_NOT_FOUND = "model_not_found"    # 404 — config error, notify user
    CONTENT_BLOCKED = "content_blocked"    # Safety filter — not retryable
    NETWORK_ERROR = "network_error"        # DNS, connection refused — retry briefly
    UNKNOWN = "unknown"                    # Unclassified


# Retry behavior per category
RETRY_BEHAVIOR: dict[str, dict] = {
    LLMErrorCategory.RATE_LIMITED:      {"retryable": True,  "backoff": True,  "failover": True,  "max_retries": 4, "base_delay": 2.0},
    LLMErrorCategory.SERVER_OVERLOAD:   {"retryable": True,  "backoff": True,  "failover": True,  "max_retries": 3, "base_delay": 1.0},
    LLMErrorCategory.GATEWAY_TIMEOUT:   {"retryable": True,  "backoff": True,  "failover": True,  "max_retries": 2, "base_delay": 2.0},
    LLMErrorCategory.SERVER_ERROR:      {"retryable": False, "backoff": False, "failover": True,  "max_retries": 0, "base_delay": 0},
    LLMErrorCategory.AUTH_FAILED:       {"retryable": False, "backoff": False, "failover": True,  "max_retries": 0, "base_delay": 0},
    LLMErrorCategory.BILLING_EXHAUSTED: {"retryable": False, "backoff": False, "failover": True,  "max_retries": 0, "base_delay": 0},
    LLMErrorCategory.CONTEXT_OVERFLOW:  {"retryable": True,  "backoff": False, "failover": False, "max_retries": 1, "base_delay": 0},
    LLMErrorCategory.MODEL_NOT_FOUND:   {"retryable": False, "backoff": False, "failover": False, "max_retries": 0, "base_delay": 0},
    LLMErrorCategory.CONTENT_BLOCKED:   {"retryable": False, "backoff": False, "failover": False, "max_retries": 0, "base_delay": 0},
    LLMErrorCategory.NETWORK_ERROR:     {"retryable": True,  "backoff": True,  "failover": True,  "max_retries": 2, "base_delay": 1.0},
    LLMErrorCategory.UNKNOWN:           {"retryable": False, "backoff": False, "failover": True,  "max_retries": 0, "base_delay": 0},
}

# Patterns to extract HTTP status code from exception messages
_STATUS_CODE_PATTERN = re.compile(r"(?:status[_ ]?code|http|error)[:\s=]*(\d{3})", re.IGNORECASE)
_RETRY_AFTER_PATTERN = re.compile(r"retry[- ]?after[:\s=]*(\d+)", re.IGNORECASE)


def classify_llm_error(exc: Exception) -> dict:
    """Classify an LLM exception into a category with retry guidance.

    Returns:
        {
            "category": str,
            "status_code": int | None,
            "retry_after": int | None,  # seconds from Retry-After header
            "retryable": bool,
            "failover": bool,
            "max_retries": int,
            "base_delay": float,
            "message": str,
            "provider_action": str,  # "none" | "cooldown" | "permanent_disable"
        }
    """
    msg = str(exc).lower()
    exc_type = type(exc).__name__

    # Extract status code from exception message
    status_code = _extract_status_code(exc)

    # Extract Retry-After hint
    retry_after = _extract_retry_after(exc)

    # Classify by status code first (most reliable)
    category = _classify_by_status(status_code, msg)

    # If no status code, classify by message patterns
    if category == LLMErrorCategory.UNKNOWN and status_code is None:
        category = _classify_by_message(msg, exc_type)

    behavior = RETRY_BEHAVIOR.get(category, RETRY_BEHAVIOR[LLMErrorCategory.UNKNOWN])

    # Determine provider action
    if category in (LLMErrorCategory.AUTH_FAILED, LLMErrorCategory.BILLING_EXHAUSTED):
        provider_action = "permanent_disable"
    elif category in (LLMErrorCategory.RATE_LIMITED, LLMErrorCategory.SERVER_OVERLOAD,
                       LLMErrorCategory.SERVER_ERROR, LLMErrorCategory.GATEWAY_TIMEOUT):
        provider_action = "cooldown"
    else:
        provider_action = "none"

    return {
        "category": category,
        "status_code": status_code,
        "retry_after": retry_after,
        "retryable": behavior["retryable"],
        "failover": behavior["failover"],
        "max_retries": behavior["max_retries"],
        "base_delay": behavior["base_delay"],
        "message": str(exc)[:300],
        "provider_action": provider_action,
    }


def _extract_status_code(exc: Exception) -> int | None:
    """Extract HTTP status code from exception."""
    # Some LangChain exceptions have status_code attribute
    for attr in ("status_code", "http_status", "code"):
        code = getattr(exc, attr, None)
        if isinstance(code, int) and 100 <= code <= 599:
            return code

    # Parse from message string
    msg = str(exc)
    m = _STATUS_CODE_PATTERN.search(msg)
    if m:
        code = int(m.group(1))
        if 100 <= code <= 599:
            return code

    # Common patterns without explicit "status_code"
    if "429" in msg:
        return 429
    if "401" in msg or "unauthorized" in msg.lower():
        return 401
    if "402" in msg:
        return 402
    if "403" in msg or "forbidden" in msg.lower():
        return 403
    if "404" in msg and "not found" in msg.lower():
        return 404
    if "413" in msg:
        return 413
    if "529" in msg:
        return 529

    return None


def _extract_retry_after(exc: Exception) -> int | None:
    """Extract Retry-After seconds from exception."""
    # Some exceptions carry headers
    headers = getattr(exc, "headers", None) or getattr(exc, "response_headers", None)
    if headers:
        ra = None
        if isinstance(headers, dict):
            ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra:
            try:
                return int(ra)
            except (ValueError, TypeError):
                pass

    # Parse from message
    msg = str(exc)
    m = _RETRY_AFTER_PATTERN.search(msg)
    if m:
        return int(m.group(1))

    return None


def _classify_by_status(code: int | None, msg: str) -> str:
    """Classify by HTTP status code."""
    if code is None:
        return LLMErrorCategory.UNKNOWN

    if code == 429 or code == 529:
        return LLMErrorCategory.RATE_LIMITED
    if code == 401 or code == 403:
        return LLMErrorCategory.AUTH_FAILED
    if code == 402:
        return LLMErrorCategory.BILLING_EXHAUSTED
    if code == 404:
        return LLMErrorCategory.MODEL_NOT_FOUND
    if code == 413:
        return LLMErrorCategory.CONTEXT_OVERFLOW
    if code == 400:
        # 400 could be context overflow or bad request
        if any(k in msg for k in ("context_length", "max_tokens", "token limit", "too long", "maximum context")):
            return LLMErrorCategory.CONTEXT_OVERFLOW
        if any(k in msg for k in ("content_filter", "safety", "blocked", "recitation")):
            return LLMErrorCategory.CONTENT_BLOCKED
        return LLMErrorCategory.UNKNOWN
    if code == 408 or code == 504:
        return LLMErrorCategory.GATEWAY_TIMEOUT
    if code == 502 or code == 503:
        return LLMErrorCategory.SERVER_OVERLOAD
    if code == 500:
        return LLMErrorCategory.SERVER_ERROR

    return LLMErrorCategory.UNKNOWN


def _classify_by_message(msg: str, exc_type: str) -> str:
    """Classify by exception message patterns when no status code available."""
    # Rate limiting
    if any(k in msg for k in ("rate limit", "rate_limit", "too many requests", "quota exceeded", "resource_exhausted")):
        return LLMErrorCategory.RATE_LIMITED

    # Auth
    if any(k in msg for k in ("invalid api key", "invalid_api_key", "authentication", "unauthorized", "permission denied")):
        return LLMErrorCategory.AUTH_FAILED

    # Billing
    if any(k in msg for k in ("insufficient_quota", "billing", "credit", "payment required")):
        return LLMErrorCategory.BILLING_EXHAUSTED

    # Context overflow
    if any(k in msg for k in ("context_length", "max_tokens", "token limit", "maximum context", "too long")):
        return LLMErrorCategory.CONTEXT_OVERFLOW

    # Content blocked (Gemini RECITATION, OpenAI content_filter)
    if any(k in msg for k in ("content_filter", "safety", "blocked", "recitation", "harmful")):
        return LLMErrorCategory.CONTENT_BLOCKED

    # Network
    if any(k in msg for k in ("connection refused", "connection reset", "dns", "name resolution",
                                "timeout", "timed out", "unreachable")):
        return LLMErrorCategory.NETWORK_ERROR
    if exc_type in ("ConnectionError", "TimeoutError", "ConnectTimeout", "ReadTimeout"):
        return LLMErrorCategory.NETWORK_ERROR

    # Server errors from message
    if any(k in msg for k in ("internal server error", "server error", "service unavailable", "overloaded")):
        return LLMErrorCategory.SERVER_OVERLOAD

    # Model not found
    if any(k in msg for k in ("model not found", "model_not_found", "does not exist", "no such model")):
        return LLMErrorCategory.MODEL_NOT_FOUND

    # GPU OOM (Ollama)
    if any(k in msg for k in ("oom", "out of memory", "cuda", "gpu memory")):
        return LLMErrorCategory.SERVER_ERROR

    return LLMErrorCategory.UNKNOWN
