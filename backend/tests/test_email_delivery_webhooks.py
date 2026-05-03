"""FS.4.3 -- Email bounce / complaint webhook tests."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest

from backend.email_delivery import (
    EmailFeedbackEvent,
    parse_email_feedback_events,
)


@contextmanager
def _email_webhook_secret(secret: str):
    from backend.config import settings

    original = settings.email_webhook_secret
    try:
        settings.email_webhook_secret = secret
        yield
    finally:
        settings.email_webhook_secret = original


def _sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class TestEmailFeedbackParser:

    def test_resend_bounce_payload(self):
        events = parse_email_feedback_events(
            "resend",
            {
                "type": "email.bounced",
                "data": {
                    "email_id": "em_123",
                    "to": ["alice@example.com"],
                    "reason": "mailbox full",
                },
            },
        )

        assert events == [
            EmailFeedbackEvent(
                provider="resend",
                event_type="bounce",
                recipient="alice@example.com",
                message_id="em_123",
                reason="mailbox full",
                raw_event_type="email.bounced",
                raw=events[0].raw,
            )
        ]

    def test_postmark_spam_complaint_payload(self):
        events = parse_email_feedback_events(
            "postmark",
            {
                "RecordType": "SpamComplaint",
                "MessageID": "pm-1",
                "Email": "bob@example.com",
                "Description": "Spam complaint",
            },
        )

        assert len(events) == 1
        assert events[0].provider == "postmark"
        assert events[0].event_type == "complaint"
        assert events[0].recipient == "bob@example.com"
        assert events[0].message_id == "pm-1"

    def test_ses_sns_bounce_payload(self):
        ses_message = {
            "notificationType": "Bounce",
            "mail": {"messageId": "ses-1"},
            "bounce": {
                "bounceType": "Permanent",
                "bouncedRecipients": [
                    {"emailAddress": "carol@example.com"},
                    {"emailAddress": "dan@example.com"},
                ],
            },
        }

        events = parse_email_feedback_events(
            "ses",
            {"Type": "Notification", "Message": json.dumps(ses_message)},
        )

        assert [event.recipient for event in events] == [
            "carol@example.com",
            "dan@example.com",
        ]
        assert {event.event_type for event in events} == {"bounce"}
        assert {event.message_id for event in events} == {"ses-1"}

    def test_non_feedback_event_is_ignored(self):
        assert parse_email_feedback_events("resend", {"type": "email.sent"}) == []


class TestEmailFeedbackWebhookEndpoint:

    @pytest.mark.asyncio
    async def test_unconfigured_returns_503(self, client):
        with _email_webhook_secret(""):
            resp = await client.post("/api/v1/webhooks/email/resend", json={})

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self, client):
        with _email_webhook_secret("email-secret"):
            resp = await client.post(
                "/api/v1/webhooks/email/resend",
                json={"type": "email.bounced"},
                headers={"Authorization": "Bearer wrong"},
            )

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_bearer_token_routes_feedback(self, client, monkeypatch):
        from backend.routers import webhooks

        mock_handler = AsyncMock()
        monkeypatch.setattr(webhooks, "_on_email_feedback_event", mock_handler)

        with _email_webhook_secret("email-secret"):
            resp = await client.post(
                "/api/v1/webhooks/email/resend",
                json={
                    "type": "email.complained",
                    "data": {"email_id": "em_1", "to": "alice@example.com"},
                },
                headers={"Authorization": "Bearer email-secret"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["provider"] == "resend"
        assert body["count"] == 1
        assert body["events"][0]["event_type"] == "complaint"
        mock_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_valid_hmac_signature_routes_feedback(self, client, monkeypatch):
        from backend.routers import webhooks

        mock_handler = AsyncMock()
        monkeypatch.setattr(webhooks, "_on_email_feedback_event", mock_handler)

        secret = "email-secret-hmac"
        body = {
            "RecordType": "Bounce",
            "MessageID": "pm-2",
            "Email": "bounce@example.com",
        }
        raw = json.dumps(body).encode()

        with _email_webhook_secret(secret):
            resp = await client.post(
                "/api/v1/webhooks/email/postmark",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-OmniSight-Email-Signature": _sign(secret, raw),
                },
            )

        assert resp.status_code == 200
        assert resp.json()["count"] == 1
        mock_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_provider_returns_400(self, client):
        with _email_webhook_secret("email-secret"):
            resp = await client.post(
                "/api/v1/webhooks/email/mailgun",
                json={},
                headers={"Authorization": "Bearer email-secret"},
            )

        assert resp.status_code == 400
