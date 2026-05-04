"""R9 row 2942 (#315) — unified watchdog event taxonomy.

This module defines the three canonical watchdog event names

  * ``watchdog.p1_system_down``        — system-level outage
  * ``watchdog.p2_cognitive_deadlock`` — agent stuck in semantic loop
  * ``watchdog.p3_auto_recovery``      — automatic remediation success

and the *single* dispatch surface :func:`emit` that wires each event
to its severity tag + L1-L4 tier set, then delegates to the row 2941
:func:`backend.notifications.send_notification` tier-explicit
dispatcher. Watchdog call-sites (M1 cgroup OOM watcher, R2 semantic
entropy detector, R8 worktree auto-recreate, R4 checkpoint resume,
etc.) hand :func:`emit` an event name + payload and the rest of the
pipeline (Slack / Jira / PagerDuty / SMS / ChatOps interactive / log+
email digest) is decided centrally — no scattered ``notify(level=...)``
calls each guessing the right severity and tier set.

Why a dedicated module instead of constants in :mod:`backend.severity`
─────────────────────────────────────────────────────────────────────
:mod:`backend.severity` is the *spec* layer (severity enum + tier ID
constants + SEVERITY_TIER_MAPPING). Mixing watchdog-specific event
names into it would make the spec layer depend on a domain (watchdog
emitters) it should not know about. The clean split is:

  spec layer        → ``backend.severity``     (P1/P2/P3 → tier set)
  taxonomy layer    → ``backend.watchdog_events`` (event → severity
                                                   + explicit tier
                                                   + default level
                                                   + default source)
  transport layer   → ``backend.notifications.send_notification``

This is also the pattern :mod:`backend.finding_types` follows for the
``FindingType`` enum (single source of truth for finding-name strings
shared across producers + consumers).

Why the EVENT_SPEC tiers are spelled out explicitly (not derived from
SEVERITY_TIER_MAPPING)
─────────────────────────────────────────────────────────────────────
Two options were considered:

  (A) ``tier=None, severity="P1"`` → ``send_notification`` falls back
      to ``tiers_for("P1")`` (the row 2941 implicit fallback path).
  (B) ``tier=<explicit set>, severity="P1"`` → ``send_notification``
      uses the explicit set verbatim (the row 2941 tier-explicit
      contract).

(B) was chosen because:

  1. The row 2942 sub-bullet wording is "各自映射 L1-L4 + severity
     tag" — i.e. the event taxonomy is itself the *canonical caller*
     of the tier-explicit API, not the implicit fallback. Locking the
     tier set per event makes the spec auditable in one file.
  2. A drift-guard test (see ``test_watchdog_events.py``) cross-checks
     ``EVENT_SPEC[event].tiers == SEVERITY_TIER_MAPPING[severity]``
     so any future tier change in :mod:`backend.severity` (or the
     reverse) immediately CI-reds. With option (A) the two specs
     would silently agree-by-construction, hiding intentional
     divergences (e.g. if a future watchdog event needs to omit a
     tier that the generic severity mapping includes).
  3. The whole point of having both APIs (``notify`` implicit vs
     ``send_notification`` tier-explicit, see row 2941 docstring) is
     that watchdog events go through the explicit path. Routing them
     through the implicit fallback would defeat that distinction.

Module-global state
───────────────────
Pure constants — :class:`WatchdogEvent` enum + :data:`EVENT_SPEC`
``MappingProxyType`` (immutable). No module-level mutable state, no
singletons. Every uvicorn worker imports the same frozen spec
(qualifying answer #1: derived deterministically from source). No
cross-worker coordination required. Mirrors :mod:`backend.severity`
which is pure-spec for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from backend.severity import (
    L1_LOG_EMAIL,
    L2_CHATOPS_INTERACTIVE,
    L2_IM_WEBHOOK,
    L3_JIRA,
    L4_PAGERDUTY,
    L4_SMS,
    Severity,
)


class WatchdogEvent(str, Enum):
    """Canonical watchdog event names — the one place all watchdog
    emitters (M1 cgroup, R2 semantic entropy, R4 checkpoint, R8
    worktree, etc.) draw event-name strings from. Adding a new event
    means adding a member here AND a corresponding :data:`EVENT_SPEC`
    entry — the drift-guard test ensures both halves stay in sync.
    """

    P1_SYSTEM_DOWN = "watchdog.p1_system_down"
    P2_COGNITIVE_DEADLOCK = "watchdog.p2_cognitive_deadlock"
    P3_AUTO_RECOVERY = "watchdog.p3_auto_recovery"


@dataclass(frozen=True)
class EventSpec:
    """Per-event dispatch parameters.

    ``severity`` and ``tiers`` together describe the L1-L4 fan-out;
    ``default_level`` provides a sensible :class:`NotificationLevel`
    floor for callers that don't supply one explicitly; ``default_source``
    is a short provenance tag that surfaces in Slack / Jira / log lines
    when the caller doesn't override it.
    """

    severity: Severity
    tiers: frozenset[str]
    default_level: str
    default_source: str


# ─────────────────────────────────────────────────────────────────
#  Event → (severity, tier set, default level, default source)
# ─────────────────────────────────────────────────────────────────

EVENT_SPEC: "MappingProxyType[WatchdogEvent, EventSpec]" = MappingProxyType({
    # P1 系統崩潰 — pages on-call, opens Jira ticket, broadcasts to
    # Slack/Discord, fires SMS. Source class: kernel-level OOM,
    # cgroup violation, container restart loop, DB connection storm.
    WatchdogEvent.P1_SYSTEM_DOWN: EventSpec(
        severity=Severity.P1,
        tiers=frozenset({
            L4_PAGERDUTY,
            L4_SMS,
            L3_JIRA,
            L2_IM_WEBHOOK,
        }),
        default_level="critical",
        default_source="watchdog.system",
    ),
    # P2 任務卡死 — files Jira ticket with ``blocked`` label, surfaces
    # ChatOps interactive card with ack / inject-hint / view-logs
    # buttons (row 2939's default button set). Source class: R2
    # semantic-entropy detector, R3 scratchpad timeout, agent
    # repeat_error stuck reasoner.
    WatchdogEvent.P2_COGNITIVE_DEADLOCK: EventSpec(
        severity=Severity.P2,
        tiers=frozenset({
            L3_JIRA,
            L2_CHATOPS_INTERACTIVE,
        }),
        default_level="action",
        default_source="watchdog.agent",
    ),
    # P3 自動修復中 — log line + per-worker email digest only. Source
    # class: R4 checkpoint resume success, R8 worktree auto-recreate,
    # transient external API retry recovered, DLQ replay drained.
    WatchdogEvent.P3_AUTO_RECOVERY: EventSpec(
        severity=Severity.P3,
        tiers=frozenset({
            L1_LOG_EMAIL,
        }),
        default_level="info",
        default_source="watchdog.recovery",
    ),
})
"""Read-only mapping of watchdog event → dispatch spec.

Wrapped in :class:`MappingProxyType` so callers cannot mutate the spec
at runtime (any attempted ``EVENT_SPEC[event] = ...`` raises
``TypeError``). Mirrors :data:`backend.severity.SEVERITY_TIER_MAPPING`
immutability discipline so spec drift cannot sneak in via test
monkeypatching or accidental dispatcher assignment.
"""


def spec_for(event: "WatchdogEvent | str") -> EventSpec:
    """Return the :class:`EventSpec` for ``event``.

    Accepts either the :class:`WatchdogEvent` enum or its string value
    (``"watchdog.p1_system_down"`` etc.) for ergonomic call-site use.
    Unknown event names raise :class:`ValueError` — drift guard so a
    typo in a watchdog emitter fails fast at CI time rather than
    silently routing nowhere.
    """
    if isinstance(event, str):
        try:
            event = WatchdogEvent(event)
        except ValueError as exc:
            raise ValueError(
                f"watchdog_events.spec_for: unknown event {event!r}; "
                f"valid: {[e.value for e in WatchdogEvent]}",
            ) from exc
    spec = EVENT_SPEC.get(event)
    if spec is None:  # pragma: no cover — guarded by Enum + drift test
        raise ValueError(
            f"watchdog_events.spec_for: no spec for {event!r}",
        )
    return spec


async def emit(
    event: "WatchdogEvent | str",
    payload: "dict[str, Any] | None" = None,
    *,
    conn=None,
):
    """Dispatch a watchdog event through the row 2941 tier-explicit
    notification path.

    Args:
        event: The :class:`WatchdogEvent` enum value (or its string
            name). Unknown event names raise :class:`ValueError`.
        payload: Optional dict with at least ``title`` (required —
            human-readable summary) and any of ``message``, ``source``,
            ``level``, ``action_url``, ``action_label`` (forwarded to
            :func:`backend.notifications.send_notification`'s payload
            argument). Missing ``level`` falls back to the event's
            ``default_level``; missing ``source`` falls back to the
            event's ``default_source`` (e.g. ``"watchdog.agent"``).
        conn: Optional DB connection — polymorphic with
            :func:`backend.notifications.send_notification`.

    Returns:
        The persisted :class:`backend.models.Notification`.

    Raises:
        ValueError: ``event`` is unknown or ``payload`` is missing
            ``title``.

    Why we look up + pass tier explicitly instead of relying on the
    severity-implicit fallback: see module docstring "Why the
    EVENT_SPEC tiers are spelled out explicitly" — locking the tier
    set per event keeps the watchdog taxonomy auditable in one file
    and lets a drift-guard test cross-check against the generic
    :data:`backend.severity.SEVERITY_TIER_MAPPING`.

    Module-global state: this function is pure dispatch — no module
    state read or written. All routing parameters are derived from
    the immutable :data:`EVENT_SPEC` at call time.
    """
    # Local import keeps ``backend.notifications`` (which pulls in
    # heavy db / chatops / settings deps) out of module-level import
    # graphs that just want the constants from this module (e.g.
    # ``from backend.watchdog_events import WatchdogEvent`` for
    # type-checking a finding emitter).
    from backend.notifications import send_notification

    spec = spec_for(event)

    payload = dict(payload or {})
    if not str(payload.get("title") or "").strip():
        raise ValueError(
            "watchdog_events.emit: payload['title'] is required "
            f"(event={spec_for(event).severity.value})",
        )

    # Default-fill level + source from the event spec so callers can
    # fire an event with just a ``title`` and the rest is implicit.
    payload.setdefault("level", spec.default_level)
    payload.setdefault("source", spec.default_source)

    return await send_notification(
        tier=spec.tiers,
        severity=spec.severity,
        payload=payload,
        conn=conn,
    )


__all__ = [
    "WatchdogEvent",
    "EventSpec",
    "EVENT_SPEC",
    "spec_for",
    "emit",
]
