"""MP.W17 — standard tool invocation wrapper.

Small contract layer over :mod:`backend.agents.tool_dispatcher`: retries
transient tool failures, avoids retrying structural errors, and opens a
runner circuit after repeated exhausted failures.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.agents.circuit_breaker import CircuitBreaker
from backend.agents.tool_dispatcher import ToolDispatcher, ToolResult


TRANSIENT_EXCEPTIONS = {
    "ConnectionError",
    "TimeoutError",
    "A2ATimeout",
    "A2ARequestFailed",
    "ExternalToolError",
}
STRUCTURAL_ERRORS = {"no_handler_registered", "invalid_handoff_envelope"}


SleepFn = Callable[[float], Awaitable[Any]]


@dataclass
class ToolWrapper:
    """Retry and circuit-breaker boundary for dispatcher tool calls."""

    dispatcher: ToolDispatcher
    breaker: CircuitBreaker
    max_attempts: int = 3
    sleep: SleepFn = asyncio.sleep

    async def invoke(
        self,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        if self.breaker.is_open():
            return ToolResult(
                tool_use_id=tool_use_id,
                content=json.dumps(
                    {"error": "circuit_open", "tool_name": tool_name}
                ),
                is_error=True,
            )

        last = ToolResult(tool_use_id, "", is_error=True)
        attempts = max(1, int(self.max_attempts))
        for attempt in range(1, attempts + 1):
            last = await self.dispatcher.execute(tool_use_id, tool_name, tool_input)
            if not last.is_error:
                self.breaker.call(lambda: None)
                return last
            if not _is_transient(last) or attempt >= attempts:
                break
            await self.sleep(0)

        if _is_transient(last):
            self._record_failure()
        return last

    def _record_failure(self) -> None:
        def _fail() -> None:
            raise ConnectionError("tool wrapper exhausted retries")

        try:
            self.breaker.call(_fail)
        except ConnectionError:
            pass


class HandoffEnvelope(BaseModel):
    """A2A handoff envelope fragment pinned by MP.W17 contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    handoff_id: str = Field(min_length=1)
    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _distinct_agents(self) -> "HandoffEnvelope":
        if self.from_agent == self.to_agent:
            raise ValueError("from_agent and to_agent must differ")
        return self


def validate_handoff_envelopes(raw: list[dict[str, Any]]) -> list[HandoffEnvelope]:
    """Validate a batch of handoff envelopes and reject duplicate IDs."""

    seen: set[str] = set()
    out: list[HandoffEnvelope] = []
    for item in raw:
        envelope = HandoffEnvelope.model_validate(item)
        if envelope.handoff_id in seen:
            raise ValueError(f"duplicate handoff_id: {envelope.handoff_id}")
        seen.add(envelope.handoff_id)
        out.append(envelope)
    return out


def _is_transient(result: ToolResult) -> bool:
    try:
        payload = json.loads(result.content)
    except json.JSONDecodeError:
        return False
    if payload.get("error") in STRUCTURAL_ERRORS:
        return False
    return payload.get("exception_type") in TRANSIENT_EXCEPTIONS


__all__ = [
    "HandoffEnvelope",
    "ToolWrapper",
    "TRANSIENT_EXCEPTIONS",
    "validate_handoff_envelopes",
]
