"""FS.4.1 -- Postmark email delivery adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.email_delivery.base import (
    EmailAddress,
    EmailDeliveryConflictError,
    EmailDeliveryError,
    EmailMessage,
)
from backend.email_delivery.postmark import POSTMARK_API_BASE, PostmarkEmailDeliveryAdapter

S = POSTMARK_API_BASE


def _mk_adapter(**kw):
    return PostmarkEmailDeliveryAdapter(
        token="pm_ABCDEF0123456789",
        **kw,
    )


def _message() -> EmailMessage:
    return EmailMessage(
        sender=EmailAddress("ops@example.com", "Ops"),
        to=[EmailAddress("alice@example.com"), EmailAddress("bob@example.com")],
        subject="Welcome",
        text="hello",
        cc=[EmailAddress("lead@example.com")],
        tags={"template": "welcome"},
    )


class TestPostmarkEmailDelivery:

    @respx.mock
    async def test_send_email_happy(self):
        route = respx.post(f"{S}/email").mock(
            return_value=httpx.Response(
                200,
                json={"MessageID": "pm_123", "ErrorCode": 0},
            ),
        )

        result = await _mk_adapter(message_stream="broadcast").send_email(_message())

        assert result.provider == "postmark"
        assert result.message_id == "pm_123"
        assert result.accepted == ["alice@example.com", "bob@example.com"]
        req = route.calls.last.request
        assert req.headers["x-postmark-server-token"] == "pm_ABCDEF0123456789"
        body = httpx.Response(200, content=req.read()).json()
        assert body["From"] == "Ops <ops@example.com>"
        assert body["To"] == "alice@example.com,bob@example.com"
        assert body["Cc"] == "lead@example.com"
        assert body["MessageStream"] == "broadcast"
        assert body["Metadata"] == {"template": "welcome"}

    @respx.mock
    async def test_422_maps_to_conflict(self):
        respx.post(f"{S}/email").mock(
            return_value=httpx.Response(422, json={"Message": "sender rejected"}),
        )
        with pytest.raises(EmailDeliveryConflictError):
            await _mk_adapter().send_email(_message())

    @respx.mock
    async def test_nonzero_error_code_marks_rejected(self):
        respx.post(f"{S}/email").mock(
            return_value=httpx.Response(
                200,
                json={"MessageID": "pm_123", "ErrorCode": 300},
            ),
        )

        result = await _mk_adapter().send_email(_message())

        assert result.accepted == []
        assert result.rejected == ["alice@example.com", "bob@example.com"]

    @respx.mock
    async def test_missing_message_id_rejected(self):
        respx.post(f"{S}/email").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(EmailDeliveryError, match="MessageID"):
            await _mk_adapter().send_email(_message())
