"""OP-951 H6 -- event-driven release notification handler tests."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from backend.agents import operator_notifier, release_notifications as rn
from backend.release_conductor.event_handlers import notification


class RecordingChannel:
    def __init__(
        self,
        name: str,
        *,
        seen: threading.Event | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self.name = name
        self.calls: list[operator_notifier.Notification] = []
        self._seen = seen
        self._delay_seconds = delay_seconds

    def send(self, payload: operator_notifier.Notification) -> None:
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        self.calls.append(payload)
        if self._seen is not None:
            self._seen.set()


def _routing(path: Path) -> Path:
    path.write_text(
        """version: 1
tiers:
  default:
    slack_channel: "#omnisight-releases"
    email_recipient: "releases@sora.services"
  rc:
    slack_channel: "#releases-rc"
    email_recipient: "releases-rc@sora.services"
  prod:
    slack_channel: "#releases-prod"
    email_recipient: "releases-prod@sora.services"
""",
        encoding="utf-8",
    )
    return path


def _event(
    *,
    key: str = "OP-951",
    version: str = "v1.2.3",
    child: str = "R8",
    summary: str = "R8 (v1.2.3) - Capture prod ship approval",
    from_status: str = "In Progress",
    to_status: str = "Done",
    meta_key: str = "OP-950",
) -> dict:
    labels = [f"RELEASE-{version}", f"release-child:{child}", "tier:S"]
    meta_issue = {
        "key": meta_key,
        "fields": {
            "summary": f"RELEASE-{version} META",
            "labels": ["meta:release", f"RELEASE-{version}"],
        },
    }
    return {
        "issue": {
            "key": key,
            "fields": {
                "summary": summary,
                "labels": labels,
                "issuelinks": [{"type": {"name": "Relates"}, "outwardIssue": meta_issue}],
            },
        },
        "changelog": {
            "items": [
                {
                    "field": "status",
                    "fromString": from_status,
                    "toString": to_status,
                }
            ]
        },
    }


def _notifier_factory(channels: dict[str, RecordingChannel]):
    def factory(_route: rn.RoutingTarget) -> operator_notifier.Notifier:
        return operator_notifier.Notifier(
            channels,  # type: ignore[arg-type]
            operator_notifier.NotifierConfig(dedup_window_seconds=0.0),
            rate_limit_config=operator_notifier.RateLimitConfig(),
        )

    return factory


def test_h2_release_event_fans_out_to_slack_and_email_within_one_second(
    tmp_path: Path,
) -> None:
    """AC: H2 release event queues Slack/email fan-out within 1s."""
    slack_seen = threading.Event()
    channels = {
        "slack": RecordingChannel("slack", seen=slack_seen),
        "email": RecordingChannel("email"),
    }

    result = notification.on_release_event(
        _event(),
        routing_path=_routing(tmp_path / "routing.yaml"),
        notifier_factory=_notifier_factory(channels),
    )

    assert result["outcome"] == "queued"
    assert result["dispatch_mode"] == "h2_event"
    assert result["slack_channel"] == "#releases-prod"
    assert slack_seen.wait(1.0)
    assert len(channels["slack"].calls) == 1
    assert len(channels["email"].calls) == 1
    assert channels["slack"].calls[0].context["release_version"] == "v1.2.3"


def test_slow_slack_does_not_block_h2_handler(tmp_path: Path, caplog) -> None:
    """AC: NotificationHandlerSlow is async and does not block H2."""
    slack_done = threading.Event()
    channels = {
        "slack": RecordingChannel(
            "slack",
            seen=slack_done,
            delay_seconds=1.05,
        ),
        "email": RecordingChannel("email"),
    }
    started = time.monotonic()

    result = notification.on_release_event(
        _event(),
        routing_path=_routing(tmp_path / "routing.yaml"),
        notifier_factory=_notifier_factory(channels),
    )
    elapsed = time.monotonic() - started

    assert result["outcome"] == "queued"
    assert elapsed < 1.0
    assert slack_done.wait(2.0)
    assert "NotificationHandlerSlow" in caplog.text


def test_h2_handler_uses_routing_per_release_tier(tmp_path: Path) -> None:
    """AC: rc and prod events use the G6 per-tier routing config."""
    routing_path = _routing(tmp_path / "routing.yaml")
    queued: list[dict] = []

    def submitter(work):
        queued.append(work())

    rc = notification.on_release_event(
        _event(version="v1.2.3-rc1"),
        routing_path=routing_path,
        notifier_factory=_notifier_factory(
            {"slack": RecordingChannel("slack"), "email": RecordingChannel("email")}
        ),
        submitter=submitter,
    )
    prod = notification.on_release_event(
        _event(version="v1.2.3"),
        routing_path=routing_path,
        notifier_factory=_notifier_factory(
            {"slack": RecordingChannel("slack"), "email": RecordingChannel("email")}
        ),
        submitter=submitter,
    )

    assert rc["slack_channel"] == "#releases-rc"
    assert rc["email_recipient"] == "releases-rc@sora.services"
    assert prod["slack_channel"] == "#releases-prod"
    assert prod["email_recipient"] == "releases-prod@sora.services"
    assert [item["outcome"] for item in queued] == ["notified", "notified"]


def test_g6_sse_fallback_remains_when_h2_unavailable(tmp_path: Path) -> None:
    """AC: existing synchronous G6 notification path remains available."""
    channels = {"slack": RecordingChannel("slack"), "email": RecordingChannel("email")}

    result = rn.notify_release_event(
        _event(),
        h2_available=False,
        routing_path=_routing(tmp_path / "routing.yaml"),
        notifier_factory=_notifier_factory(channels),
    )

    assert result["outcome"] == "notified"
    assert result["fallback"] == "g6_sse"
    assert result["dispatch_count"] == 1
    assert len(channels["slack"].calls) == 1
    assert len(channels["email"].calls) == 1
