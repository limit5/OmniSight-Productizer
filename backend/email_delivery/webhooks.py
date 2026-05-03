"""FS.4.3 -- Email bounce / complaint webhook normalization.

Provider webhook payloads differ substantially, but the rest of the
backend only needs the actionable feedback events: bounces and spam
complaints. This module mirrors the FS.4.1 adapter package style by
keeping provider-specific parsing behind small helpers and returning a
provider-neutral dataclass.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable constants and pure parser functions only.
No module-level cache, singleton, or mutable registry is read or written;
every webhook request derives events from its own JSON payload, so there
is no cross-worker state to coordinate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


EMAIL_FEEDBACK_EVENT_TYPES: tuple[str, ...] = ("bounce", "complaint")


@dataclass(frozen=True)
class EmailFeedbackEvent:
    """Provider-neutral bounce / complaint event."""

    provider: str
    event_type: str
    recipient: str
    message_id: str = ""
    reason: str = ""
    raw_event_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.event_type not in EMAIL_FEEDBACK_EVENT_TYPES:
            raise ValueError(f"unsupported email feedback event_type: {self.event_type}")
        if not self.recipient or "@" not in self.recipient:
            raise ValueError("recipient email is required")

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "event_type": self.event_type,
            "recipient": self.recipient,
            "message_id": self.message_id,
            "reason": self.reason,
            "raw_event_type": self.raw_event_type,
        }


def normalize_email_webhook_provider(provider: str) -> str:
    """Return the canonical provider id for an email webhook payload."""
    key = provider.strip().lower().replace("_", "-")
    if key in ("resend", "postmark"):
        return key
    if key in ("ses", "aws-ses", "aws"):
        return "aws-ses"
    raise ValueError(
        f"Unknown email webhook provider '{provider}'. "
        "Expected one of: resend, postmark, aws-ses"
    )


def parse_email_feedback_events(
    provider: str,
    payload: dict[str, Any],
) -> list[EmailFeedbackEvent]:
    """Parse provider payload into actionable bounce / complaint events."""
    canonical = normalize_email_webhook_provider(provider)
    if canonical == "resend":
        return _parse_resend(payload)
    if canonical == "postmark":
        return _parse_postmark(payload)
    if canonical == "aws-ses":
        return _parse_ses(payload)
    raise AssertionError(f"unhandled provider: {canonical}")


def _parse_resend(payload: dict[str, Any]) -> list[EmailFeedbackEvent]:
    event_name = str(payload.get("type") or payload.get("event") or "")
    event_type = {
        "email.bounced": "bounce",
        "email.complained": "complaint",
        "email.complaint": "complaint",
    }.get(event_name)
    if not event_type:
        return []

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    recipients = _recipients(
        data.get("to")
        or data.get("recipient")
        or data.get("email")
    )
    message_id = str(
        data.get("email_id")
        or data.get("message_id")
        or data.get("id")
        or ""
    )
    reason = str(data.get("reason") or data.get("bounce_reason") or "")
    return [
        EmailFeedbackEvent(
            provider="resend",
            event_type=event_type,
            recipient=recipient,
            message_id=message_id,
            reason=reason,
            raw_event_type=event_name,
            raw=payload,
        )
        for recipient in recipients
    ]


def _parse_postmark(payload: dict[str, Any]) -> list[EmailFeedbackEvent]:
    record_type = str(payload.get("RecordType") or payload.get("Type") or "")
    normalized = record_type.strip().lower()
    if normalized == "bounce":
        event_type = "bounce"
    elif normalized in ("spamcomplaint", "spam complaint"):
        event_type = "complaint"
    else:
        return []

    recipients = _recipients(payload.get("Email") or payload.get("Recipient"))
    message_id = str(payload.get("MessageID") or payload.get("MessageId") or "")
    reason = str(
        payload.get("Description")
        or payload.get("Details")
        or payload.get("Name")
        or ""
    )
    return [
        EmailFeedbackEvent(
            provider="postmark",
            event_type=event_type,
            recipient=recipient,
            message_id=message_id,
            reason=reason,
            raw_event_type=record_type,
            raw=payload,
        )
        for recipient in recipients
    ]


def _parse_ses(payload: dict[str, Any]) -> list[EmailFeedbackEvent]:
    message = payload
    if isinstance(payload.get("Message"), str):
        try:
            parsed = json.loads(str(payload["Message"]))
            if isinstance(parsed, dict):
                message = parsed
        except json.JSONDecodeError:
            return []

    notification_type = str(message.get("notificationType") or "")
    mail = message.get("mail") if isinstance(message.get("mail"), dict) else {}
    message_id = str(mail.get("messageId") or "")

    if notification_type == "Bounce":
        bounce = message.get("bounce") if isinstance(message.get("bounce"), dict) else {}
        reason = str(
            bounce.get("bounceType")
            or bounce.get("bounceSubType")
            or ""
        )
        recipients = [
            str(item.get("emailAddress") or "").strip()
            for item in bounce.get("bouncedRecipients") or []
            if isinstance(item, dict)
        ]
        event_type = "bounce"
    elif notification_type == "Complaint":
        complaint = (
            message.get("complaint")
            if isinstance(message.get("complaint"), dict)
            else {}
        )
        reason = str(complaint.get("complaintFeedbackType") or "")
        recipients = [
            str(item.get("emailAddress") or "").strip()
            for item in complaint.get("complainedRecipients") or []
            if isinstance(item, dict)
        ]
        event_type = "complaint"
    else:
        return []

    return [
        EmailFeedbackEvent(
            provider="aws-ses",
            event_type=event_type,
            recipient=recipient,
            message_id=message_id,
            reason=reason,
            raw_event_type=notification_type,
            raw=payload,
        )
        for recipient in recipients
        if recipient and "@" in recipient
    ]


def _recipients(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple)):
        candidates = []
        for item in value:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                candidates.append(str(item.get("email") or item.get("Email") or ""))
    else:
        candidates = []
    return [item.strip() for item in candidates if item.strip() and "@" in item]


__all__ = [
    "EMAIL_FEEDBACK_EVENT_TYPES",
    "EmailFeedbackEvent",
    "normalize_email_webhook_provider",
    "parse_email_feedback_events",
]
