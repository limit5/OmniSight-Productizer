"""OP-114 - Standard tool-call invocation wrapper.

Centralizes retry, timeout, and process-local circuit breaker behavior for
Anthropic tool calls before they reach ``ToolDispatcher`` callers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from backend.agents.tool_dispatcher import (
    ToolDispatcher,
    ToolResult,
    get_default_dispatcher,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry policy for one wrapped tool invocation."""

    transient_retries: int = 1
    backoff_seconds: float = 1.0


@dataclass
class _ToolCircuit:
    failures: list[float]
    opened_at: float | None = None


_FAILURE_WINDOW_SECONDS = 60.0
_CIRCUIT_THRESHOLD = 3
_CIRCUIT_OPEN_SECONDS = 30.0
_CIRCUITS: dict[str, _ToolCircuit] = {}

_STRUCTURAL_ERRORS = {
    "invalid_input",
    "no_handler_registered",
    "not_found",
    "schema_validation",
    "validation",
}
_STRUCTURAL_EXCEPTION_TYPES = {
    "FileNotFoundError",
    "KeyError",
    "LookupError",
    "NotFoundError",
    "ValidationError",
    "ValueError",
}
_TRANSIENT_ERRORS = {
    "network",
    "rate_limit",
    "timeout",
    "too_many_requests",
    "transient",
}
_TRANSIENT_EXCEPTION_TYPES = {
    "ConnectionError",
    "HTTPError",
    "OSError",
    "RateLimitError",
    "TimeoutError",
}


async def invoke_tool(
    name: str,
    args: dict[str, Any],
    retry_policy: RetryPolicy | None = None,
    timeout: float | None = None,
    *,
    tool_use_id: str | None = None,
    dispatcher: ToolDispatcher | None = None,
) -> ToolResult:
    """Invoke ``name`` through the default dispatcher with common safeguards."""
    policy = retry_policy or RetryPolicy()
    effective_dispatcher = dispatcher or get_default_dispatcher()
    effective_tool_use_id = tool_use_id or f"wrapped-{uuid.uuid4().hex}"

    open_result = _circuit_open_result(name, effective_tool_use_id)
    if open_result is not None:
        return open_result

    attempts = policy.transient_retries + 1
    arg_keys = sorted(args.keys()) if args else []
    result: ToolResult | None = None
    for attempt in range(attempts):
        try:
            call = effective_dispatcher.execute(effective_tool_use_id, name, args)
            result = await asyncio.wait_for(call, timeout=timeout) if timeout else await call
        except TimeoutError:
            log.warning(
                "tool_call_wrapper.invoke_tool: tool=%s timed out "
                "tool_use_id=%s attempt=%d/%d timeout_s=%s arg_keys=%s",
                name,
                effective_tool_use_id,
                attempt + 1,
                attempts,
                timeout,
                arg_keys,
            )
            result = _error_result(effective_tool_use_id, "timeout", name, "TimeoutError", "")
        except Exception as exc:  # noqa: BLE001 - wrapper boundary
            log.exception(
                "tool_call_wrapper.invoke_tool: tool=%s raised before returning a ToolResult "
                "tool_use_id=%s attempt=%d/%d exc_type=%s arg_keys=%s",
                name,
                effective_tool_use_id,
                attempt + 1,
                attempts,
                type(exc).__name__,
                arg_keys,
            )
            result = _error_result(
                effective_tool_use_id,
                "tool_raised",
                name,
                type(exc).__name__,
                str(exc)[:1000],
            )

        if not result.is_error:
            _record_success(name)
            return result

        if not _is_transient(result) or attempt == attempts - 1:
            _record_failure(name)
            return result

        await asyncio.sleep(policy.backoff_seconds)

    # Defensive fallback; loop always returns.
    assert result is not None, (
        f"tool_call_wrapper.invoke_tool: retry loop for tool={name!r} "
        f"tool_use_id={effective_tool_use_id!r} exited without setting result "
        f"(attempts={attempts})"
    )
    _record_failure(name)
    return result


def invoke_tool_sync(
    name: str,
    args: dict[str, Any],
    retry_policy: RetryPolicy | None = None,
    timeout: float | None = None,
    *,
    tool_use_id: str | None = None,
    dispatcher: ToolDispatcher | None = None,
) -> ToolResult:
    """Synchronous entry point for callers outside an event loop."""
    return asyncio.run(
        invoke_tool(
            name,
            args,
            retry_policy,
            timeout,
            tool_use_id=tool_use_id,
            dispatcher=dispatcher,
        )
    )


def reset_circuits_for_tests() -> None:
    """Clear process-local breaker state."""
    _CIRCUITS.clear()


def _error_result(
    tool_use_id: str,
    error: str,
    tool_name: str,
    exception_type: str | None = None,
    message: str | None = None,
) -> ToolResult:
    payload: dict[str, Any] = {"error": error, "tool_name": tool_name}
    if exception_type:
        payload["exception_type"] = exception_type
    if message is not None:
        payload["message"] = message
    return ToolResult(tool_use_id=tool_use_id, content=json.dumps(payload), is_error=True)


def _decode_error(result: ToolResult) -> dict[str, Any]:
    try:
        payload = json.loads(result.content)
    except (TypeError, ValueError):
        return {"error": result.content}
    return payload if isinstance(payload, dict) else {"error": str(payload)}


def _is_transient(result: ToolResult) -> bool:
    payload = _decode_error(result)
    error = str(payload.get("error", "")).lower()
    exception_type = str(payload.get("exception_type", ""))
    message = str(payload.get("message", "")).lower()

    if error in _STRUCTURAL_ERRORS or exception_type in _STRUCTURAL_EXCEPTION_TYPES:
        return False
    if error in _TRANSIENT_ERRORS or exception_type in _TRANSIENT_EXCEPTION_TYPES:
        return True
    return any(token in message for token in ("network", "rate limit", "timeout", "timed out"))


def _circuit_open_result(name: str, tool_use_id: str) -> ToolResult | None:
    now = time.monotonic()
    circuit = _CIRCUITS.get(name)
    if circuit is None or circuit.opened_at is None:
        return None
    elapsed = now - circuit.opened_at
    if elapsed < _CIRCUIT_OPEN_SECONDS:
        log.warning(
            "tool_call_wrapper._circuit_open_result: short-circuiting call to tool=%s "
            "tool_use_id=%s open_for_s=%.2f remaining_s=%.2f (threshold=%d failures in %.0fs)",
            name,
            tool_use_id,
            elapsed,
            _CIRCUIT_OPEN_SECONDS - elapsed,
            _CIRCUIT_THRESHOLD,
            _FAILURE_WINDOW_SECONDS,
        )
        return _error_result(tool_use_id, "circuit_open", name)
    log.info(
        "tool_call_wrapper._circuit_open_result: circuit cool-down elapsed for tool=%s "
        "tool_use_id=%s open_for_s=%.2f (open window=%.0fs); allowing probe call",
        name,
        tool_use_id,
        elapsed,
        _CIRCUIT_OPEN_SECONDS,
    )
    circuit.opened_at = None
    circuit.failures.clear()
    return None


def _record_success(name: str) -> None:
    _CIRCUITS.pop(name, None)


def _record_failure(name: str) -> None:
    now = time.monotonic()
    circuit = _CIRCUITS.setdefault(name, _ToolCircuit(failures=[]))
    cutoff = now - _FAILURE_WINDOW_SECONDS
    circuit.failures = [ts for ts in circuit.failures if ts >= cutoff]
    circuit.failures.append(now)
    if len(circuit.failures) >= _CIRCUIT_THRESHOLD:
        if circuit.opened_at is None:
            log.warning(
                "tool_call_wrapper._record_failure: opening circuit for tool=%s "
                "failures=%d threshold=%d window_s=%.0f open_for_s=%.0f",
                name,
                len(circuit.failures),
                _CIRCUIT_THRESHOLD,
                _FAILURE_WINDOW_SECONDS,
                _CIRCUIT_OPEN_SECONDS,
            )
        circuit.opened_at = now
