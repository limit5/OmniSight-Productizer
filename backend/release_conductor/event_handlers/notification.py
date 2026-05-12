"""OP-951 H6 -- event-driven release notification fan-out."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from backend.agents import operator_notifier, release_notifications


logger = logging.getLogger(__name__)

SLOW_HANDLER_THRESHOLD_SECONDS = 1.0

Submitter = Callable[[Callable[[], dict[str, Any]]], None]


class NotificationHandlerSlow(RuntimeError):
    """Notification fan-out exceeded the H6 non-blocking threshold."""


class RoutingConfigDrift(RuntimeError):
    """Per-tier routing was absent or unreadable; default route was used."""


def _spawn_fire_and_forget(work: Callable[[], dict[str, Any]]) -> None:
    thread = threading.Thread(
        target=work,
        name="release-notification-handler",
        daemon=True,
    )
    thread.start()


def _run_notification_task(
    transition: release_notifications.ReleaseTransition,
    *,
    routing_path: Path,
    notifier_factory: Callable[
        [release_notifications.RoutingTarget],
        operator_notifier.Notifier,
    ],
) -> dict[str, Any]:
    started = time.monotonic()
    result = release_notifications.notify_transition(
        transition,
        routing_path=routing_path,
        notifier_factory=notifier_factory,
    )
    elapsed = time.monotonic() - started
    if elapsed > SLOW_HANDLER_THRESHOLD_SECONDS:
        logger.warning(
            "NotificationHandlerSlow: release notification took %.3fs "
            "version=%s child=%s",
            elapsed,
            transition.version,
            transition.child_key,
        )
        result["warning"] = "NotificationHandlerSlow"
        result["latency_seconds"] = elapsed
    return result


def on_release_event(
    event: dict[str, Any],
    *,
    routing_path: Path = release_notifications.DEFAULT_ROUTING_PATH,
    notifier_factory: Callable[
        [release_notifications.RoutingTarget],
        operator_notifier.Notifier,
    ] = release_notifications._build_release_notifier,
    submitter: Submitter = _spawn_fire_and_forget,
) -> dict[str, Any]:
    """Queue Slack/email fan-out for a release event received through H2.

    The H2 worker must not wait on Slack or SMTP. This handler parses
    the same JIRA payload as G6, resolves the route synchronously, and
    pushes the actual bridge call to a daemon background thread.
    """
    transition = release_notifications.transition_from_jira_event(event)
    if transition is None:
        return {"outcome": "ignored"}

    route, routing_warning = release_notifications.route_for(
        transition,
        routing_path=routing_path,
    )
    warning: str | None = None
    if routing_warning or route.tier != transition.tier:
        warning = "RoutingConfigDrift"
        logger.warning(
            "RoutingConfigDrift: release=%s tier=%s using route=%s",
            transition.version,
            transition.tier,
            route.tier,
        )

    def work() -> dict[str, Any]:
        return _run_notification_task(
            transition,
            routing_path=routing_path,
            notifier_factory=notifier_factory,
        )

    submitter(work)

    result: dict[str, Any] = {
        "outcome": "queued",
        "dispatch_mode": "h2_event",
        "release_version": transition.version,
        "child_key": transition.child_key,
        "meta_key": transition.meta_key,
        "slack_channel": route.slack_channel,
        "email_recipient": route.email_recipient,
    }
    if warning:
        result["warning"] = warning
    return result


__all__ = [
    "NotificationHandlerSlow",
    "RoutingConfigDrift",
    "on_release_event",
]
