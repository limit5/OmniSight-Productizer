"""OP-115 -- hardened A2A handoff envelope contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


HandoffHandler = Callable[[HandoffEnvelope], Awaitable[dict[str, Any]]]


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


def error_envelope(
    envelope: HandoffEnvelope,
    *,
    code: str,
    message: str,
) -> HandoffEnvelope:
    """Return a structured rejection to the original sender."""

    return HandoffEnvelope(
        handoff_id=envelope.handoff_id,
        from_agent=envelope.to_agent,
        to_agent=envelope.from_agent,
        payload={
            "status": "error",
            "error": {
                "code": code,
                "message": message,
            },
        },
    )


class HandoffReceiver:
    """Receiver-side idempotency boundary for one peer agent."""

    def __init__(self, handler: HandoffHandler) -> None:
        self._handler = handler
        self._completed: set[str] = set()
        self._in_flight: set[str] = set()

    async def receive(self, raw: HandoffEnvelope | dict[str, Any]) -> HandoffEnvelope:
        envelope = (
            raw
            if isinstance(raw, HandoffEnvelope)
            else HandoffEnvelope.model_validate(raw)
        )
        if (
            envelope.handoff_id in self._completed
            or envelope.handoff_id in self._in_flight
        ):
            return error_envelope(
                envelope,
                code="duplicate_handoff_id",
                message=f"handoff_id {envelope.handoff_id} was already received",
            )

        self._in_flight.add(envelope.handoff_id)
        try:
            payload = await self._handler(envelope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return error_envelope(
                envelope,
                code="receiver_rejected",
                message=str(exc),
            )
        else:
            self._completed.add(envelope.handoff_id)
            return HandoffEnvelope(
                handoff_id=envelope.handoff_id,
                from_agent=envelope.to_agent,
                to_agent=envelope.from_agent,
                payload=payload,
            )
        finally:
            self._in_flight.discard(envelope.handoff_id)


async def send_handoff(
    receiver: HandoffReceiver,
    envelope: HandoffEnvelope,
    *,
    timeout_s: float,
) -> HandoffEnvelope:
    """Send one handoff and convert receiver timeout into an error envelope."""

    try:
        return await asyncio.wait_for(receiver.receive(envelope), timeout=timeout_s)
    except TimeoutError:
        return error_envelope(
            envelope,
            code="handoff_timeout",
            message=f"receiver timed out after {timeout_s:g}s",
        )


__all__ = [
    "HandoffEnvelope",
    "HandoffReceiver",
    "error_envelope",
    "send_handoff",
    "validate_handoff_envelopes",
]
