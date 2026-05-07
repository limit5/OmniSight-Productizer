"""Anthropic subscription adapter backed by the ``claude`` CLI.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants and helper functions only.  It does
not keep mutable module-level state.  ``AnthropicSubscriptionAdapter`` is a
regular instantiable class; its circuit breaker is instance-local.

Import side-effect contract
---------------------------
Importing this module registers one ``AnthropicSubscriptionAdapter`` instance
with ``backend.agents.provider_orchestrator``.  Downstream routing code can
therefore make the adapter available with::

    import backend.agents.provider_adapters.anthropic_subscription
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from backend.agents import provider_quota_tracker
from backend.agents.provider_orchestrator import (
    CircuitBreaker,
    DispatchResult,
    HealthStatus,
    ProviderAdapter,
    TaskSpec,
    register_adapter,
)
from backend.agents.provider_quota_tracker import QuotaState


PROVIDER_ID = "anthropic-subscription"
DEFAULT_DISPATCH_TIMEOUT_S = 30 * 60
DISPATCH_TIMEOUT_ENV = "OMNISIGHT_ANTHROPIC_DISPATCH_TIMEOUT_S"
HEALTH_CHECK_TIMEOUT_S = 5

_TOKEN_KEY_RE = re.compile(r"(?:^|_)(?:input|output|cache|total)?_?tokens?$")
_TEXT_TOKEN_RE = re.compile(r"\b(?:total_)?tokens(?:_used)?\b\D{0,12}(\d+)", re.I)
_RETRY_AFTER_RE = re.compile(r"\bretry-after\b[\"':=\s-]*(\d+)", re.I)


class AnthropicSubscriptionAdapter(ProviderAdapter):
    """Dispatch one task through the caller's configured ``claude`` CLI."""

    def __init__(self) -> None:
        self._circuit_breaker = CircuitBreaker(PROVIDER_ID)

    def provider_id(self) -> str:
        return PROVIDER_ID

    def dispatch(self, task: TaskSpec) -> DispatchResult:
        started = time.monotonic()
        timeout_s = _dispatch_timeout_s()
        proc = subprocess.Popen(
            ["claude", "-p", "--output-format", "json", "--input-format", "text"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(task.prompt, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            latency = time.monotonic() - started
            self._circuit_breaker.record_outcome(False)
            return DispatchResult(
                success=False,
                tokens_used=_tokens_used(stdout, stderr),
                latency_seconds=latency,
                error=json.dumps({"kind": "timeout", "timeout_s": timeout_s}),
                provider_id=PROVIDER_ID,
            )

        latency = time.monotonic() - started
        tokens_used = _tokens_used(stdout, stderr)
        cap_signal = _cap_signal(stdout, stderr)
        if cap_signal is not None:
            _record_usage(tokens_used)
            self._trip_circuit_for_cap()
            return DispatchResult(
                success=False,
                tokens_used=tokens_used,
                latency_seconds=latency,
                error=json.dumps(cap_signal, sort_keys=True),
                provider_id=PROVIDER_ID,
            )

        success = proc.returncode == 0
        if success:
            _record_usage(tokens_used)
        self._circuit_breaker.record_outcome(success)
        return DispatchResult(
            success=success,
            tokens_used=tokens_used,
            latency_seconds=latency,
            error=None if success else _non_cap_error(proc.returncode, stderr),
            provider_id=PROVIDER_ID,
        )

    def _trip_circuit_for_cap(self) -> None:
        for _ in range(CircuitBreaker.trip_threshold):
            self._circuit_breaker.record_outcome(False)

    def health_check(self) -> HealthStatus:
        version = _run_cli(["claude", "--version"])
        cli_installed = version.returncode == 0
        auth = (
            _run_cli(["claude", "auth", "status"])
            if cli_installed
            else _CliResult(127, "", "")
        )
        subscription_active = _subscription_active(auth.stdout, auth.stderr)
        reachable = cli_installed and subscription_active
        return HealthStatus(
            provider_id=PROVIDER_ID,
            reachable=reachable,
            last_checked_at=datetime.now(timezone.utc),
            cli_installed=cli_installed,
            subscription_active=subscription_active,
            detail=_health_detail(version, auth),
        )

    def get_quota_state(self) -> QuotaState:
        return provider_quota_tracker.get_quota_state(PROVIDER_ID)


def _dispatch_timeout_s() -> int:
    raw = (os.environ.get(DISPATCH_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_DISPATCH_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DISPATCH_TIMEOUT_S
    return value if value > 0 else DEFAULT_DISPATCH_TIMEOUT_S


def _record_usage(tokens_used: int) -> None:
    provider_quota_tracker.record_usage(PROVIDER_ID, max(tokens_used, 0))


class _CliResult:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_cli(argv: list[str]) -> _CliResult:
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=HEALTH_CHECK_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _CliResult(127, "", str(exc))
    return _CliResult(proc.returncode, proc.stdout, proc.stderr)


def _subscription_active(stdout: str, stderr: str) -> bool:
    payloads = list(_json_payloads(stdout, stderr))
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        subscription_type = (
            str(payload.get("subscriptionType") or "").strip().lower()
        )
        return (
            payload.get("loggedIn") is True
            and payload.get("authMethod") == "claude.ai"
            and subscription_type not in {"", "none", "unknown"}
        )
    return False


def _health_detail(version: _CliResult, auth: _CliResult) -> str:
    if version.returncode != 0:
        return "claude CLI not installed or not executable"
    if auth.returncode != 0:
        return "claude auth status failed"
    if not _subscription_active(auth.stdout, auth.stderr):
        return "claude subscription is not active"
    return "claude CLI installed and subscription active"


def _tokens_used(stdout: str, stderr: str) -> int:
    total = 0
    for payload in _json_payloads(stdout, stderr):
        total = max(total, _tokens_from_json(payload))
    if total:
        return total
    joined = "\n".join((stdout, stderr))
    matches = [int(match.group(1)) for match in _TEXT_TOKEN_RE.finditer(joined)]
    return max(matches, default=0)


def _tokens_from_json(value: Any) -> int:
    if isinstance(value, dict):
        return sum(
            child
            if isinstance(child, int) and _TOKEN_KEY_RE.search(key)
            else _tokens_from_json(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return sum(_tokens_from_json(item) for item in value)
    return 0


def _cap_signal(stdout: str, stderr: str) -> dict[str, int | str] | None:
    joined = "\n".join((stdout, stderr))
    payloads = list(_json_payloads(stdout, stderr))
    if not _has_cap_marker(joined, payloads):
        return None
    out: dict[str, int | str] = {"kind": "cap_exceeded"}
    retry_after_s = _retry_after_s(joined, payloads)
    if retry_after_s is not None:
        out["retry_after_s"] = retry_after_s
    return out


def _has_cap_marker(text: str, payloads: list[Any]) -> bool:
    lowered = text.lower()
    text_has_cap = (
        "usage_exceeded" in lowered
        or "usage exceeded" in lowered
        or re.search(r"\bhttp(?:\s+status)?\s*[:=]?\s*429\b", lowered)
        or re.search(r"\bstatus(?:_code|code)?\b[\"':=\s]+429\b", lowered)
    )
    return bool(text_has_cap) or any(_json_value_has_cap(payload) for payload in payloads)


def _json_value_has_cap(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _json_pair_has_cap(str(key), child) or _json_value_has_cap(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_json_value_has_cap(item) for item in value)
    if isinstance(value, str):
        return "usage_exceeded" in value.lower() or "usage exceeded" in value.lower()
    return False


def _json_pair_has_cap(key: str, value: Any) -> bool:
    key_l = key.lower()
    if key_l in {"status", "status_code", "code", "http_status"} and value == 429:
        return True
    return key_l in {"type", "error", "error_type", "code"} and "usage_exceeded" in str(value).lower()


def _retry_after_s(text: str, payloads: list[Any]) -> int | None:
    for payload in payloads:
        found = _retry_after_from_json(payload)
        if found is not None:
            return found
    match = _RETRY_AFTER_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def _retry_after_from_json(value: Any) -> int | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower().replace("_", "-") == "retry-after":
                parsed = _parse_retry_after_value(child)
                if parsed is not None:
                    return parsed
            found = _retry_after_from_json(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _retry_after_from_json(item)
            if found is not None:
                return found
    return None


def _parse_retry_after_value(value: Any) -> int | None:
    if isinstance(value, int):
        return max(value, 0)
    raw = str(value).strip()
    if raw.isdigit():
        return int(raw)
    return None


def _json_payloads(*chunks: str) -> Iterator[Any]:
    for chunk in chunks:
        text = chunk.strip()
        if not text:
            continue
        try:
            yield json.loads(text)
            continue
        except json.JSONDecodeError:
            pass
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _non_cap_error(returncode: int | None, stderr: str) -> str:
    return json.dumps(
        {
            "kind": "cli_error",
            "returncode": returncode,
            "stderr": stderr[-2000:],
        },
        sort_keys=True,
    )


register_adapter(AnthropicSubscriptionAdapter())


__all__ = ["AnthropicSubscriptionAdapter"]
