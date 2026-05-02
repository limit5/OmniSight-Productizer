"""FS.4.1 -- Resend email delivery adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.email_delivery.base import (
    EmailAddress,
    EmailAttachment,
    EmailDeliveryError,
    EmailDeliveryRateLimitError,
    EmailMessage,
    InvalidEmailDeliveryTokenError,
)
from backend.email_delivery.resend import RESEND_API_BASE, ResendEmailDeliveryAdapter

S = RESEND_API_BASE


def _mk_adapter(**kw):
    return ResendEmailDeliveryAdapter(
        token="re_ABCDEF0123456789",
        **kw,
    )


def _message() -> EmailMessage:
    return EmailMessage(
        sender=EmailAddress("ops@example.com", "Ops"),
        to=[EmailAddress("alice@example.com")],
        subject="Welcome",
        text="hello",
        html="<p>hello</p>",
        reply_to=[EmailAddress("support@example.com")],
        attachments=[EmailAttachment("a.txt", "SGVsbG8=", "text/plain")],
        tags={"template": "welcome"},
    )


class TestResendEmailDelivery:

    @respx.mock
    async def test_send_email_happy(self):
        route = respx.post(f"{S}/emails").mock(
            return_value=httpx.Response(200, json={"id": "em_123"}),
        )

        result = await _mk_adapter().send_email(_message())

        assert result.provider == "resend"
        assert result.message_id == "em_123"
        assert result.accepted == ["alice@example.com"]
        req = route.calls.last.request
        assert req.headers["authorization"] == "Bearer re_ABCDEF0123456789"
        body = httpx.Response(200, content=req.read()).json()
        assert body["from"] == "Ops <ops@example.com>"
        assert body["to"] == ["alice@example.com"]
        assert body["reply_to"] == ["support@example.com"]
        assert body["attachments"][0]["filename"] == "a.txt"
        assert body["tags"] == [{"name": "template", "value": "welcome"}]

    @respx.mock
    async def test_401_maps_to_invalid_token(self):
        respx.post(f"{S}/emails").mock(
            return_value=httpx.Response(401, json={"message": "bad token"}),
        )
        with pytest.raises(InvalidEmailDeliveryTokenError):
            await _mk_adapter().send_email(_message())

    @respx.mock
    async def test_429_maps_to_rate_limit(self):
        respx.post(f"{S}/emails").mock(
            return_value=httpx.Response(
                429,
                json={"message": "slow"},
                headers={"Retry-After": "9"},
            ),
        )
        with pytest.raises(EmailDeliveryRateLimitError) as excinfo:
            await _mk_adapter().send_email(_message())
        assert excinfo.value.retry_after == 9

    @respx.mock
    async def test_missing_id_rejected(self):
        respx.post(f"{S}/emails").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(EmailDeliveryError, match="missing id"):
            await _mk_adapter().send_email(_message())
