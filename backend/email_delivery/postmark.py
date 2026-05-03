"""FS.4.1 -- Postmark transactional email adapter."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.email_delivery.base import (
    EmailDeliveryAdapter,
    EmailDeliveryError,
    EmailDeliveryResult,
    EmailMessage,
)
from backend.email_delivery.http import raise_for_email_response

logger = logging.getLogger(__name__)

POSTMARK_API_BASE = "https://api.postmarkapp.com"


class PostmarkEmailDeliveryAdapter(EmailDeliveryAdapter):
    """Postmark API adapter (``provider='postmark'``)."""

    provider = "postmark"

    def _configure(
        self,
        *,
        message_stream: str = "outbound",
        api_base: str = POSTMARK_API_BASE,
        **_: Any,
    ) -> None:
        self._message_stream = message_stream
        self._api_base = api_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "X-Postmark-Server-Token": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _payload(self, message: EmailMessage) -> dict[str, Any]:
        body: dict[str, Any] = {
            "From": message.sender.formatted(),
            "To": ",".join(a.formatted() for a in message.to),
            "Subject": message.subject,
            "MessageStream": self._message_stream,
        }
        if message.text:
            body["TextBody"] = message.text
        if message.html:
            body["HtmlBody"] = message.html
        if message.cc:
            body["Cc"] = ",".join(a.formatted() for a in message.cc)
        if message.bcc:
            body["Bcc"] = ",".join(a.formatted() for a in message.bcc)
        if message.reply_to:
            body["ReplyTo"] = ",".join(a.formatted() for a in message.reply_to)
        if message.headers:
            body["Headers"] = [
                {"Name": key, "Value": value}
                for key, value in sorted(message.headers.items())
            ]
        if message.attachments:
            body["Attachments"] = [
                {
                    "Name": a.filename,
                    "Content": a.content,
                    "ContentType": a.content_type or "application/octet-stream",
                    **({"ContentID": a.content_id} if a.content_id else {}),
                }
                for a in message.attachments
            ]
        if message.tags:
            body["Metadata"] = dict(message.tags)
        return body

    async def send_email(self, message: EmailMessage, **kwargs: Any) -> EmailDeliveryResult:
        del kwargs
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._api_base}/email",
                headers=self._headers(),
                json=self._payload(message),
            )
        raise_for_email_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        message_id = str(data.get("MessageID") or data.get("MessageId") or "")
        if not message_id:
            raise EmailDeliveryError(
                "Postmark response missing MessageID",
                status=resp.status_code,
                provider=self.provider,
            )
        rejected = []
        if data.get("ErrorCode") not in (None, 0):
            rejected = [a.email for a in message.to]
        logger.info(
            "postmark.email_send message_id=%s to=%s fp=%s",
            message_id, len(message.to), self.token_fp(),
        )
        return EmailDeliveryResult(
            provider=self.provider,
            message_id=message_id,
            accepted=[] if rejected else [a.email for a in message.to],
            rejected=rejected,
            raw=data,
        )


__all__ = ["POSTMARK_API_BASE", "PostmarkEmailDeliveryAdapter"]
