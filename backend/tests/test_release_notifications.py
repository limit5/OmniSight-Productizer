"""OP-942 — release transition Slack/email fan-out tests."""
from __future__ import annotations

from pathlib import Path

from backend.agents import operator_notifier, release_notifications as rn
from backend.release_conductor.event_handlers import jira_handlers


class RecordingChannel:
    def __init__(self, name: str, *, raises: Exception | None = None) -> None:
        self.name = name
        self.calls: list[operator_notifier.Notification] = []
        self._raises = raises

    def send(self, payload: operator_notifier.Notification) -> None:
        self.calls.append(payload)
        if self._raises is not None:
            raise self._raises


def _routing(path: Path) -> Path:
    path.write_text(
        """version: 1
tiers:
  default:
    slack_channel: "#omnisight-releases"
    email_recipient: "releases@sora.services"
  rc:
    slack_channel: "#releases-rc"
    email_recipient: "releases@sora.services"
  prod:
    slack_channel: "#releases-prod"
    email_recipient: "releases@sora.services"
""",
        encoding="utf-8",
    )
    return path


def _event(
    *,
    key: str = "OP-1001",
    version: str = "v1.2.3",
    child: str = "R8",
    summary: str = "R8 (v1.2.3) — Capture prod ship approval",
    from_status: str = "承認済み",
    to_status: str = "公開済み",
    meta_key: str = "OP-1000",
    meta_summary: str | None = None,
    meta_labels: list[str] | None = None,
) -> dict:
    labels = [f"RELEASE-{version}", f"release-child:{child}", "tier:S"]
    status_item = {"field": "status", "fromString": from_status, "toString": to_status}
    meta_issue = {
        "key": meta_key,
        "fields": {
            "summary": meta_summary or f"RELEASE-{version} META",
            "labels": (
                meta_labels
                if meta_labels is not None
                else ["meta:release", f"RELEASE-{version}"]
            ),
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
        "changelog": {"items": [status_item]},
    }


def _notifier_factory(channels: dict[str, RecordingChannel]):
    def factory(_route: rn.RoutingTarget) -> operator_notifier.Notifier:
        return operator_notifier.Notifier(
            channels,  # type: ignore[arg-type]
            operator_notifier.NotifierConfig(dedup_window_seconds=0.0),
            rate_limit_config=operator_notifier.RateLimitConfig(),
        )

    return factory


def test_happy_fanout_sends_slack_and_email_for_published_transition(tmp_path: Path) -> None:
    """AC: child -> 公開済み sends release payload to Slack + email."""
    channels = {"slack": RecordingChannel("slack"), "email": RecordingChannel("email")}

    result = rn.notify_jira_transition(
        _event(),
        routing_path=_routing(tmp_path / "routing.yaml"),
        notifier_factory=_notifier_factory(channels),
    )

    assert result["outcome"] == "notified"
    assert result["dispatch_count"] == 1
    assert len(channels["slack"].calls) == 1
    assert len(channels["email"].calls) == 1
    payload = channels["slack"].calls[0]
    assert payload.context["release_version"] == "v1.2.3"
    assert payload.context["child_name"].startswith("R8 R8")
    assert payload.context["meta_url"].endswith("/OP-1000")
    assert "Release v1.2.3" in payload.message


def test_routing_per_tier_uses_rc_and_prod_channels(tmp_path: Path) -> None:
    """AC: rc vs prod routing is configurable in YAML."""
    routing_path = _routing(tmp_path / "routing.yaml")
    rc_transition = rn.transition_from_jira_event(_event(version="v1.2.3-rc1"))
    prod_transition = rn.transition_from_jira_event(_event(version="v1.2.3"))

    assert rc_transition is not None
    assert prod_transition is not None
    rc_route, _ = rn.route_for(rc_transition, routing_path=routing_path)
    prod_route, _ = rn.route_for(prod_transition, routing_path=routing_path)

    assert rc_route.slack_channel == "#releases-rc"
    assert prod_route.slack_channel == "#releases-prod"
    assert rc_route.email_recipient == "releases@sora.services"


def test_meta_summary_detection_tolerates_action_tag_prefix() -> None:
    """AC: tagged release META summary is treated as the linked release parent."""
    transition = rn.transition_from_jira_event(
        _event(
            version="v0.5.0-rc1",
            meta_summary="[GATE] RELEASE-v0.5.0-rc1 META",
            meta_labels=[],
        )
    )

    assert transition is not None
    assert transition.meta_key == "OP-1000"


def test_meta_summary_detection_accepts_raw_release_prefix() -> None:
    """AC: untagged release META summary is still treated as the linked release parent."""
    transition = rn.transition_from_jira_event(
        _event(
            version="v0.5.0-rc1",
            meta_summary="RELEASE-v0.5.0-rc1 META",
            meta_labels=[],
        )
    )

    assert transition is not None
    assert transition.meta_key == "OP-1000"


def test_meta_summary_detection_rejects_mid_string_release_word() -> None:
    """AC: non-release summaries containing RELEASE- mid-string are ignored."""
    transition = rn.transition_from_jira_event(
        _event(
            version="v0.5.0-rc1",
            meta_summary="not a release parent RELEASE-v0.5.0-rc1",
            meta_labels=[],
        )
    )

    assert transition is None


def test_missing_config_falls_back_to_default_channel(tmp_path: Path) -> None:
    """AC: RoutingConfigMissing falls back to default channel."""
    channels = {"slack": RecordingChannel("slack"), "email": RecordingChannel("email")}

    result = rn.notify_jira_transition(
        _event(),
        routing_path=tmp_path / "missing.yaml",
        notifier_factory=_notifier_factory(channels),
    )

    assert result["outcome"] == "notified"
    assert result["warning"] == "RoutingConfigMissing"
    assert result["slack_channel"] == "#omnisight-releases"
    assert channels["slack"].calls[0].context["email_recipient"] == "releases@sora.services"


def test_bridge_down_degrades_silently_and_logs(tmp_path: Path, caplog) -> None:
    """AC: NotificationBridgeDown does not raise into transition handling."""
    routing_path = _routing(tmp_path / "routing.yaml")

    def broken_factory(_route: rn.RoutingTarget) -> operator_notifier.Notifier:
        raise RuntimeError("bridge unavailable")

    result = rn.notify_jira_transition(
        _event(),
        routing_path=routing_path,
        notifier_factory=broken_factory,
    )

    assert result["outcome"] == "bridge_down"
    assert result["error"] == "NotificationBridgeDown"
    assert "NotificationBridgeDown" in caplog.text


def test_all_done_status_transitions_are_covered(tmp_path: Path) -> None:
    """AC: every release Published/Done status synonym triggers fan-out."""
    routing_path = _routing(tmp_path / "routing.yaml")
    for done_status in sorted(rn.PUBLISHED_STATUS_NAMES):
        channels = {"slack": RecordingChannel("slack"), "email": RecordingChannel("email")}
        result = rn.notify_jira_transition(
            _event(to_status=done_status),
            routing_path=routing_path,
            notifier_factory=_notifier_factory(channels),
        )
        assert result["outcome"] == "notified"
        assert len(channels["slack"].calls) == 1
        assert len(channels["email"].calls) == 1


def test_jira_handler_returns_notification_result_for_release_done(monkeypatch) -> None:
    """AC: H2/JIRA listener path matches transition then queues fan-out."""
    calls: list[dict] = []

    def fake_notify(event: dict) -> dict:
        calls.append(event)
        return {"outcome": "queued", "slack_channel": "#releases-prod"}

    monkeypatch.setattr(jira_handlers.release_notifications, "notify_release_event", fake_notify)

    result = jira_handlers.on_issue_updated(_event())

    assert result["outcome"] == "advance_next"
    assert result["notification"]["outcome"] == "queued"
    assert len(calls) == 1
