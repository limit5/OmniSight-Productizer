"""W10 #284 — Browser-error → JIRA ticket router.

Wires the W10 RUM ingest layer to the O5 ``IntentSource`` abstraction
(``backend.intent_source``) so an unhandled JS error in production
becomes a JIRA / GitHub Issues / GitLab Issue ticket — without
touching the Sentry / Datadog UI.

Dedup
-----
Two JS errors with the same ``fingerprint`` (release + message + top
frame) within ``dedup_window_seconds`` (default 24h) collapse into
one ticket. The second occurrence increments an in-memory counter
and (optionally) appends a comment to the existing ticket.

The dedup table is in-memory — process restart wipes it. That's fine
because the durable record lives in JIRA itself; the dedup table is a
short-window de-amplifier, not a system of record. Persistent dedup
is doable later by hooking ``backend.audit`` reads.

Subtask shape
-------------
Each ticket maps onto an ``IntentSource.create_subtasks`` payload:

  * ``title``                  — ``"[browser-error] {message[:120]}"``
  * ``acceptance_criteria``    — multi-line: error message / page /
                                 release / fingerprint / first-seen
                                 timestamp.
  * ``impact_scope_allowed``   — ``["app/", "components/", "lib/"]``
  * ``impact_scope_forbidden`` — ``["test_assets/", "configs/"]``
  * ``handoff_protocol``       — ``["repro_in_browser", "git_blame_top_frame"]``
  * ``domain_context``         — ``"web/{environment}"``
  * ``labels``                 — ``["rum", "browser-error", level]``

The router never blocks the ingest path: failures (adapter unreachable,
auth bad, rate-limited) are logged at ``warning`` and the metric
counter increments — it never raises into the FastAPI handler.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from backend.intent_source import (
    AdapterError,
    IntentSource,
    SubtaskPayload,
    SubtaskRef,
    default_vendor,
    get_source,
)
from backend.observability.base import ErrorEvent

logger = logging.getLogger(__name__)


# ── Dedup table ──────────────────────────────────────────────────


@dataclass
class DedupRecord:
    fingerprint: str
    first_seen: float
    last_seen: float
    count: int
    ticket: Optional[SubtaskRef] = None
    last_message: str = ""


# ── Router ───────────────────────────────────────────────────────


@dataclass
class RouterMetrics:
    """Counters surfaced by ``ErrorToIntentRouter.metrics()``."""

    routed: int = 0
    deduped: int = 0
    dropped_below_min_level: int = 0
    adapter_unavailable: int = 0
    adapter_errors: int = 0
    comment_appended: int = 0
    last_error: str = ""
    last_routed_ticket: str = ""


_LEVEL_RANK = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "fatal": 50,
}


class ErrorToIntentRouter:
    """Convert ``ErrorEvent`` → ``IntentSource`` subtask.

    The router is process-singleton-friendly but deliberately does NOT
    register itself globally — callers (router / scheduler / scaffold
    test) instantiate one with their own vendor / project key / dedup
    knobs.
    """

    def __init__(
        self,
        *,
        vendor: Optional[str] = None,
        parent_ticket: str = "",
        min_level: str = "error",
        dedup_window_seconds: int = 86_400,
        comment_on_duplicate: bool = True,
        clock=time.time,
    ) -> None:
        self._vendor = vendor
        self._parent_ticket = parent_ticket
        self._min_level = min_level.lower().strip()
        if self._min_level not in _LEVEL_RANK:
            raise ValueError(
                f"min_level must be one of {sorted(_LEVEL_RANK)}, got {min_level!r}"
            )
        self._min_rank = _LEVEL_RANK[self._min_level]
        if dedup_window_seconds <= 0:
            raise ValueError("dedup_window_seconds must be > 0")
        self._dedup_window = dedup_window_seconds
        self._comment_on_duplicate = comment_on_duplicate
        self._clock = clock
        self._dedup: dict[str, DedupRecord] = {}
        self._metrics = RouterMetrics()
        self._lock = asyncio.Lock()

    # ── Public ──

    async def route(self, event: ErrorEvent) -> Optional[SubtaskRef]:
        """Process one ``ErrorEvent``.

        Returns the ``SubtaskRef`` of the (newly-created or duplicate)
        ticket, or ``None`` when the event was filtered or routing
        failed gracefully.
        """
        # Level gate first — saves work + adapter calls.
        if _LEVEL_RANK.get(event.level, 0) < self._min_rank:
            self._metrics.dropped_below_min_level += 1
            return None

        async with self._lock:
            self._evict_expired_locked()
            existing = self._dedup.get(event.fingerprint)
            now = self._clock()
            if existing is not None:
                existing.count += 1
                existing.last_seen = now
                existing.last_message = event.message
                self._metrics.deduped += 1
                logger.debug(
                    "rum.error_router dedup hit fp=%s count=%d ticket=%s",
                    event.fingerprint, existing.count,
                    existing.ticket.ticket if existing.ticket else "n/a",
                )
                # Background comment append — does NOT block ingest.
                if self._comment_on_duplicate and existing.ticket:
                    asyncio.create_task(
                        self._append_dup_comment(existing, event)
                    )
                return existing.ticket

            # New fingerprint — record then create.
            record = DedupRecord(
                fingerprint=event.fingerprint,
                first_seen=now,
                last_seen=now,
                count=1,
                last_message=event.message,
            )
            self._dedup[event.fingerprint] = record

        # Outside the lock — adapter call may take seconds.
        ref = await self._create_subtask(event)
        if ref is not None:
            async with self._lock:
                record.ticket = ref
            self._metrics.routed += 1
            self._metrics.last_routed_ticket = ref.ticket
        return ref

    def metrics(self) -> dict:
        """Snapshot of routing counters — safe to call any time."""
        m = self._metrics
        return {
            "routed": m.routed,
            "deduped": m.deduped,
            "dropped_below_min_level": m.dropped_below_min_level,
            "adapter_unavailable": m.adapter_unavailable,
            "adapter_errors": m.adapter_errors,
            "comment_appended": m.comment_appended,
            "last_error": m.last_error,
            "last_routed_ticket": m.last_routed_ticket,
            "active_dedup_keys": len(self._dedup),
        }

    def list_recent(self, *, limit: int = 50) -> list[dict]:
        """List the most recently-seen fingerprints — for the dashboard.

        Sorted by ``last_seen`` desc.
        """
        rows = sorted(
            self._dedup.values(),
            key=lambda r: r.last_seen,
            reverse=True,
        )[:limit]
        return [
            {
                "fingerprint": r.fingerprint,
                "first_seen": r.first_seen,
                "last_seen": r.last_seen,
                "count": r.count,
                "message": r.last_message,
                "ticket": r.ticket.ticket if r.ticket else "",
                "ticket_url": r.ticket.url if r.ticket else "",
            }
            for r in rows
        ]

    def reset(self) -> None:
        """Test helper — wipe dedup + metrics."""
        self._dedup.clear()
        self._metrics = RouterMetrics()

    # ── Private ──

    def _evict_expired_locked(self) -> None:
        cutoff = self._clock() - self._dedup_window
        expired = [k for k, r in self._dedup.items() if r.last_seen < cutoff]
        for k in expired:
            self._dedup.pop(k, None)

    def _resolve_source(self) -> Optional[IntentSource]:
        try:
            return get_source(self._vendor or default_vendor())
        except KeyError:
            self._metrics.adapter_unavailable += 1
            return None

    async def _create_subtask(self, event: ErrorEvent) -> Optional[SubtaskRef]:
        source = self._resolve_source()
        if source is None:
            logger.info(
                "rum.error_router no IntentSource available — skipping fp=%s",
                event.fingerprint,
            )
            return None

        payload = build_subtask_payload(event)
        try:
            refs = await source.create_subtasks(
                self._parent_ticket or _synthetic_parent(event),
                [payload],
            )
        except AdapterError as exc:
            self._metrics.adapter_errors += 1
            self._metrics.last_error = str(exc)
            logger.warning(
                "rum.error_router create_subtasks adapter error fp=%s vendor=%s err=%s",
                event.fingerprint, source.vendor, exc,
            )
            return None
        except Exception as exc:
            self._metrics.adapter_errors += 1
            self._metrics.last_error = repr(exc)
            logger.warning(
                "rum.error_router create_subtasks unexpected error fp=%s vendor=%s err=%r",
                event.fingerprint, source.vendor, exc,
            )
            return None

        if not refs:
            self._metrics.adapter_errors += 1
            self._metrics.last_error = "adapter returned no SubtaskRef"
            return None
        return refs[0]

    async def _append_dup_comment(
        self,
        record: DedupRecord,
        event: ErrorEvent,
    ) -> None:
        source = self._resolve_source()
        if source is None or record.ticket is None:
            return
        body = (
            f"[OmniSight RUM] duplicate occurrence #{record.count} "
            f"at {_iso(record.last_seen)} on {event.page} "
            f"(fingerprint={event.fingerprint[:12]}…)"
        )
        try:
            await source.comment(record.ticket.ticket, body)
            self._metrics.comment_appended += 1
        except AdapterError as exc:
            self._metrics.adapter_errors += 1
            self._metrics.last_error = str(exc)
        except Exception as exc:
            self._metrics.adapter_errors += 1
            self._metrics.last_error = repr(exc)


# ── Helpers ──────────────────────────────────────────────────────


def build_subtask_payload(event: ErrorEvent) -> SubtaskPayload:
    """Convert an ``ErrorEvent`` into a ``SubtaskPayload``.

    Pulled out as module-level so tests can pin the structure without
    constructing a router.
    """
    title = f"[browser-error] {event.message[:120].strip() or 'unknown error'}"
    ac_lines = [
        f"Message: {event.message}",
        f"Level: {event.level}",
        f"Page: {event.page}",
        f"Release: {event.release or '(unset)'}",
        f"Environment: {event.environment}",
        f"Fingerprint: {event.fingerprint}",
        f"First seen: {_iso(event.timestamp)}",
        f"User-Agent: {event.user_agent or '(unknown)'}",
    ]
    if event.stack:
        ac_lines.append("Stack (truncated):")
        for line in event.stack.splitlines()[:10]:
            ac_lines.append(f"  {line}")
    return SubtaskPayload(
        title=title,
        acceptance_criteria="\n".join(ac_lines),
        impact_scope_allowed=["app/", "components/", "lib/"],
        impact_scope_forbidden=["test_assets/", "configs/"],
        handoff_protocol=["repro_in_browser", "git_blame_top_frame"],
        domain_context=f"web/{event.environment}",
        labels=["rum", "browser-error", event.level],
        extra={
            "fingerprint": event.fingerprint,
            "page": event.page,
            "release": event.release,
        },
    )


def _synthetic_parent(event: ErrorEvent) -> str:
    """Adapters that need a parent (JIRA epic) get a synthetic key
    derived from the release. The bridge is best-effort: vendors that
    don't enforce a parent (GitHub Issues) ignore it."""
    return f"OMNI-RUM-{(event.release or 'unreleased').upper()}"


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ── Singleton helper ─────────────────────────────────────────────

_default: Optional[ErrorToIntentRouter] = None


def get_default_router() -> ErrorToIntentRouter:
    """Lazy-init module-level router with safe defaults."""
    global _default
    if _default is None:
        _default = ErrorToIntentRouter()
    return _default


def reset_default_router() -> None:
    """Test helper — wipes the module-level singleton state."""
    global _default
    _default = None


__all__ = [
    "DedupRecord",
    "ErrorToIntentRouter",
    "RouterMetrics",
    "build_subtask_payload",
    "get_default_router",
    "reset_default_router",
]
