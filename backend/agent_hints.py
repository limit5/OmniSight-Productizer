"""R1 (#307) — Agent Human-Hint Blackboard.

Operator-injected text is parked in a per-agent ``human_hint`` slot.  When
the agent state machine ticks, it reads and clears the slot and folds the
hint into its context (the ``human_hint`` slot in the context, **not** the
system prompt tail — that was the anti-pattern called out in the R1
design spec).

Invariants
==========

* **Sanitize** — strip XML/HTML-like tags (``<system_override>`` style
  prompt-injection) and clamp to ``chatops_hint_max_length``.
* **Rate limit** — per-agent sliding window (default 3 hints per 5 min);
  beyond that, ``inject()`` raises :class:`HintRateLimitError` and the
  caller surfaces a 429 to ChatOps.
* **Audit** — every inject goes through :func:`backend.audit.log` with
  ``action="chatops.inject"`` so it lands on the hash chain.
* **Hot resume** — ``inject()`` pokes the per-agent resume event so a
  suspended agent wakes up immediately; otherwise the next scheduled
  tick picks the hint up.

The module is intentionally kept decoupled from the agent runtime — the
actual state-machine consumer calls :func:`consume` when it's ready.
That indirection keeps unit tests straightforward.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from backend.config import settings

logger = logging.getLogger(__name__)


_TAG_RE = re.compile(r"<[^>]{0,200}>")  # strip XML/HTML tags inc. prompt-inject markers
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class HintRateLimitError(RuntimeError):
    """Raised when ``inject()`` exceeds the per-agent sliding window."""


@dataclass
class Hint:
    agent_id: str
    text: str
    author: str          # ChatOps username / email
    channel: str         # discord | teams | line | dashboard
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "text": self.text,
            "author": self.author,
            "channel": self.channel,
            "ts": self.ts,
        }


# agent_id → Hint (single-slot; newer hint replaces older so the agent
# doesn't wake up N times if ChatOps spams the window).
_blackboard: dict[str, Hint] = {}
_rate_window: dict[str, list[float]] = {}
_resume_events: dict[str, asyncio.Event] = {}
_lock = threading.Lock()

_RATE_WINDOW_SECONDS = 300.0  # 5 minutes


def sanitize(text: str, *, max_length: int | None = None) -> str:
    """Strip tags + control chars, clamp length.

    The sanitize layer is the hard guard against prompt injection — the
    CLAUDE.md rule banning ``<system_override>`` style markers exists
    precisely because without stripping them, an operator with ChatOps
    write access could trivially escalate to system-prompt authority.
    """
    if not text:
        return ""
    limit = max_length if max_length is not None else settings.chatops_hint_max_length
    text = _TAG_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    text = text.strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _check_rate(agent_id: str, rate: int, window: float) -> None:
    """Raises HintRateLimitError if the agent busted its sliding window."""
    now = time.time()
    cutoff = now - window
    with _lock:
        hits = _rate_window.setdefault(agent_id, [])
        # drop expired
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= rate:
            raise HintRateLimitError(
                f"agent {agent_id!r}: {rate} hints/{int(window)}s exceeded"
            )
        hits.append(now)


def _get_resume_event(agent_id: str) -> asyncio.Event:
    """Return (create on first use) the asyncio.Event used for hot resume.

    ``asyncio.Event`` is cheap — one per agent is fine. The event is
    created eagerly in the running loop so callers can ``await
    resume_event(agent_id).wait()`` from the agent state machine.
    """
    with _lock:
        ev = _resume_events.get(agent_id)
        if ev is None:
            ev = asyncio.Event()
            _resume_events[agent_id] = ev
    return ev


def inject(
    agent_id: str,
    text: str,
    *,
    author: str = "unknown",
    channel: str = "dashboard",
    rate_per_window: int | None = None,
    window_seconds: float = _RATE_WINDOW_SECONDS,
) -> Hint:
    """Write a hint to the agent's blackboard slot.

    Raises :class:`HintRateLimitError` if the caller exceeds the sliding
    window. On success:

    1. The hint replaces any pending hint for the same agent.
    2. The resume event fires (hot resume).
    3. An ``emit_debug_finding`` with ``finding_type="human_hint"`` is
       issued for UI observability.
    4. An async audit entry is scheduled (hash-chain persisted).
    """
    if not agent_id:
        raise ValueError("agent_id required")
    clean = sanitize(text)
    if not clean:
        raise ValueError("hint text is empty after sanitize")

    rate = rate_per_window if rate_per_window is not None else max(
        1, int(settings.chatops_hint_rate_per_5min)
    )
    _check_rate(agent_id, rate, window_seconds)

    hint = Hint(agent_id=agent_id, text=clean, author=author, channel=channel)
    with _lock:
        _blackboard[agent_id] = hint
    # Hot resume — wake a suspended agent if it's awaiting hint.
    try:
        _get_resume_event(agent_id).set()
    except RuntimeError:
        # No running loop (e.g. test context that synchronously called
        # inject before starting the loop). Falls back to next tick.
        pass

    # SSE observability — reuses emit_debug_finding so the existing debug
    # blackboard panel lights up automatically.
    try:
        from backend.events import emit_debug_finding
        emit_debug_finding(
            task_id="",
            agent_id=agent_id,
            finding_type="human_hint",
            severity="info",
            message=clean[:200],
            context={"author": author, "channel": channel},
        )
    except Exception as exc:
        logger.debug("agent_hints: emit_debug_finding skipped: %s", exc)

    # Audit log — always best-effort but critical for compliance.
    _schedule_audit("chatops.inject", hint)
    return hint


def _schedule_audit(action: str, hint: Hint, extra: dict | None = None) -> None:
    try:
        from backend import audit as _audit
        loop = asyncio.get_running_loop()
        loop.create_task(_audit.log(
            action=action,
            entity_kind="agent_hint",
            entity_id=hint.agent_id,
            after={
                "agent_id": hint.agent_id,
                "text": hint.text[:500],
                "author": hint.author,
                "channel": hint.channel,
                "ts": hint.ts,
                **(extra or {}),
            },
            actor=f"chatops:{hint.author}",
        ))
    except RuntimeError:
        # No running loop — skip (unit-test fast path).
        pass
    except Exception as exc:
        logger.debug("agent_hints: audit skipped: %s", exc)


def peek(agent_id: str) -> Optional[Hint]:
    with _lock:
        return _blackboard.get(agent_id)


def consume(agent_id: str) -> Optional[Hint]:
    """Read + clear the slot. Called from the agent's tick loop."""
    with _lock:
        hint = _blackboard.pop(agent_id, None)
        ev = _resume_events.get(agent_id)
    if ev is not None:
        ev.clear()
    return hint


def resume_event(agent_id: str) -> asyncio.Event:
    """Public accessor so an agent's ``await`` loop can block on it."""
    return _get_resume_event(agent_id)


def snapshot() -> list[dict]:
    """UI helper: list pending hints (does not consume)."""
    with _lock:
        return [h.to_dict() for h in _blackboard.values()]


def reset_for_tests() -> None:
    with _lock:
        _blackboard.clear()
        _rate_window.clear()
        _resume_events.clear()
