"""Release-conductor notification fan-out (OP-942)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from backend.agents import operator_notifier


logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROUTING_PATH = REPO_ROOT / "config" / "release_notification_routing.yaml"
DEFAULT_SLACK_CHANNEL = "#omnisight-releases"
DEFAULT_EMAIL_RECIPIENT = "releases@sora.services"
DEFAULT_META_BASE_URL = os.environ.get(
    "OMNISIGHT_JIRA_BASE_URL",
    "https://soraapp.atlassian.net/browse",
)
PUBLISHED_STATUS_NAMES = frozenset({"Published", "Done", "Closed", "Resolved", "公開済み"})


@dataclass(frozen=True)
class RoutingTarget:
    tier: str
    slack_channel: str
    email_recipient: str


@dataclass(frozen=True)
class ReleaseTransition:
    version: str
    tier: str
    child_key: str
    child_name: str
    meta_key: str
    meta_url: str
    from_status: str
    to_status: str


class RoutingConfigMissing(RuntimeError):
    """Routing config file is absent/unreadable; caller should use defaults."""


class NotificationBridgeDown(RuntimeError):
    """OP-721 bridge construction or dispatch failed."""


def _status_change(event: dict[str, Any]) -> tuple[str, str]:
    changelog = event.get("changelog") or {}
    items = changelog.get("items") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("field") == "status":
            return (
                str(item.get("fromString") or item.get("from") or ""),
                str(item.get("toString") or item.get("to") or ""),
            )
    return ("", "")


def _labels(fields: dict[str, Any]) -> list[str]:
    raw = fields.get("labels") or []
    return [str(label) for label in raw if isinstance(label, str)]


def _label_value(labels: list[str], prefix: str) -> str | None:
    for label in labels:
        if label.startswith(prefix):
            return label.removeprefix(prefix)
    return None


def _version_from_labels(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith("RELEASE-v"):
            return label.removeprefix("RELEASE-")
        if label.startswith("HOTFIX-v"):
            return label.removeprefix("HOTFIX-").split("+", 1)[0]
    return None


def _tier_for_version(version: str) -> str:
    return "rc" if "-rc" in version else "prod"


def _meta_key_from_issue(fields: dict[str, Any]) -> str | None:
    parent = fields.get("parent")
    if isinstance(parent, dict) and parent.get("key"):
        return str(parent["key"])

    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        if isinstance(link_type, dict) and link_type.get("name") != "Relates":
            continue
        for side in ("outwardIssue", "inwardIssue"):
            issue = link.get(side)
            if not isinstance(issue, dict) or not issue.get("key"):
                continue
            linked_fields = issue.get("fields") or {}
            linked_labels = _labels(linked_fields) if isinstance(linked_fields, dict) else []
            summary = (
                str(linked_fields.get("summary") or "")
                if isinstance(linked_fields, dict)
                else ""
            )
            if "meta:release" in linked_labels or summary.startswith("RELEASE-"):
                return str(issue["key"])
    return None


def transition_from_jira_event(event: dict[str, Any]) -> ReleaseTransition | None:
    """Extract a release-child Published transition from a JIRA webhook.

    Non-release issues and non-Published status changes return ``None``.
    """

    from_status, to_status = _status_change(event)
    if to_status not in PUBLISHED_STATUS_NAMES:
        return None

    issue = event.get("issue") or {}
    fields = issue.get("fields") or {}
    if not isinstance(fields, dict):
        return None

    labels = _labels(fields)
    child_r_id = _label_value(labels, "release-child:")
    version = _version_from_labels(labels)
    meta_key = _meta_key_from_issue(fields)
    if not child_r_id or not version or not meta_key:
        return None

    child_key = str(issue.get("key") or "")
    summary = str(fields.get("summary") or child_key or child_r_id)
    base_url = DEFAULT_META_BASE_URL.rstrip("/")
    return ReleaseTransition(
        version=version,
        tier=_tier_for_version(version),
        child_key=child_key,
        child_name=f"{child_r_id} {summary}",
        meta_key=meta_key,
        meta_url=f"{base_url}/{meta_key}",
        from_status=from_status,
        to_status=to_status,
    )


def load_routing(path: Path = DEFAULT_ROUTING_PATH) -> dict[str, RoutingTarget]:
    """Load per-tier routing, falling back to built-in defaults."""

    defaults = {
        "default": RoutingTarget(
            tier="default",
            slack_channel=DEFAULT_SLACK_CHANNEL,
            email_recipient=DEFAULT_EMAIL_RECIPIENT,
        ),
    }
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RoutingConfigMissing(f"routing config missing: {path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise RoutingConfigMissing(f"routing config unreadable: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        return defaults
    tiers = raw.get("tiers")
    if not isinstance(tiers, dict):
        return defaults

    out = dict(defaults)
    for tier, target in tiers.items():
        if not isinstance(tier, str) or not isinstance(target, dict):
            continue
        slack_channel = str(target.get("slack_channel") or DEFAULT_SLACK_CHANNEL)
        email_recipient = str(target.get("email_recipient") or DEFAULT_EMAIL_RECIPIENT)
        out[tier] = RoutingTarget(
            tier=tier,
            slack_channel=slack_channel,
            email_recipient=email_recipient,
        )
    return out


def route_for(
    transition: ReleaseTransition,
    *,
    routing_path: Path = DEFAULT_ROUTING_PATH,
) -> tuple[RoutingTarget, str | None]:
    try:
        routing = load_routing(routing_path)
        missing_reason = None
    except RoutingConfigMissing as exc:
        logger.warning("RoutingConfigMissing: %s; falling back to default channel", exc)
        routing = {
            "default": RoutingTarget(
                tier="default",
                slack_channel=DEFAULT_SLACK_CHANNEL,
                email_recipient=DEFAULT_EMAIL_RECIPIENT,
            )
        }
        missing_reason = str(exc)
    return (
        routing.get(transition.tier)
        or routing.get("default")
        or RoutingTarget("default", DEFAULT_SLACK_CHANNEL, DEFAULT_EMAIL_RECIPIENT),
        missing_reason,
    )


def render_payload(
    transition: ReleaseTransition,
    route: RoutingTarget,
) -> tuple[str, dict[str, Any]]:
    message = (
        f"Release {transition.version}: {transition.child_name} "
        f"transitioned {transition.from_status or '(unknown)'} -> {transition.to_status}. "
        f"META: {transition.meta_url}"
    )
    context = {
        "release_version": transition.version,
        "release_tier": transition.tier,
        "child_key": transition.child_key,
        "child_name": transition.child_name,
        "meta_key": transition.meta_key,
        "meta_url": transition.meta_url,
        "slack_channel": route.slack_channel,
        "email_recipient": route.email_recipient,
    }
    return message, context


def _build_release_notifier(route: RoutingTarget) -> operator_notifier.Notifier:
    env = dict(os.environ)
    env["OMNISIGHT_NOTIFIER_EMAIL_RECIPIENTS"] = route.email_recipient
    channels = operator_notifier.build_channels_from_env(env)
    channels.pop("jira", None)
    channels.pop("line", None)
    cfg = operator_notifier.NotifierConfig(dedup_window_seconds=0.0)
    return operator_notifier.Notifier(
        channels,
        cfg,
        rate_limit_config=operator_notifier.RateLimitConfig(),
    )


def notify_transition(
    transition: ReleaseTransition,
    *,
    routing_path: Path = DEFAULT_ROUTING_PATH,
    notifier_factory: Callable[
        [RoutingTarget],
        operator_notifier.Notifier,
    ] = _build_release_notifier,
) -> dict[str, Any]:
    """Fan out a release transition through OP-721 bridge semantics."""

    route, routing_warning = route_for(transition, routing_path=routing_path)
    message, context = render_payload(transition, route)
    outcome: dict[str, Any] = {
        "outcome": "notified",
        "slack_channel": route.slack_channel,
        "email_recipient": route.email_recipient,
    }
    if routing_warning:
        outcome["warning"] = "RoutingConfigMissing"

    try:
        notifier = notifier_factory(route)
        notifier.notify(
            operator_notifier.Severity.CRITICAL,
            "release_transition_published",
            message,
            context=context,
            scope=f"release:{transition.version}",
            root_cause_key=f"release_transition:{transition.child_key}:{transition.to_status}",
        )
        dispatched = notifier.flush_all()
        outcome["dispatch_count"] = len(dispatched)
    except Exception as exc:  # noqa: BLE001 — fire-and-forget contract
        logger.exception("NotificationBridgeDown: release transition notification failed")
        outcome.update(
            {
                "outcome": "bridge_down",
                "error": "NotificationBridgeDown",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )
    return outcome


def notify_jira_transition(
    event: dict[str, Any],
    *,
    routing_path: Path = DEFAULT_ROUTING_PATH,
    notifier_factory: Callable[
        [RoutingTarget],
        operator_notifier.Notifier,
    ] = _build_release_notifier,
) -> dict[str, Any]:
    transition = transition_from_jira_event(event)
    if transition is None:
        return {"outcome": "ignored"}
    result = notify_transition(
        transition,
        routing_path=routing_path,
        notifier_factory=notifier_factory,
    )
    result.update(
        {
            "release_version": transition.version,
            "child_key": transition.child_key,
            "meta_key": transition.meta_key,
        }
    )
    return result
