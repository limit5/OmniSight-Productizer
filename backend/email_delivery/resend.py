"""FS.4.1 -- Resend transactional email adapter."""

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

RESEND_API_BASE = "https://api.resend.com"


class ResendEmailDeliveryAdapter(EmailDeliveryAdapter):
    """Resend API adapter (``provider='resend'``)."""

    provider = "resend"

    def _configure(
        self,
        *,
        api_base: str = RESEND_API_BASE,
        **_: Any,
    ) -> None:
        self._api_base = api_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _payload(self, message: EmailMessage) -> dict[str, Any]:
        body: dict[str, Any] = {
            "from": message.sender.formatted(),
            "to": [a.formatted() for a in message.to],
            "subject": message.subject,
        }
        if message.text:
            body["text"] = message.text
        if message.html:
            body["html"] = message.html
        if message.cc:
            body["cc"] = [a.formatted() for a in message.cc]
        if message.bcc:
            body["bcc"] = [a.formatted() for a in message.bcc]
        if message.reply_to:
            body["reply_to"] = [a.formatted() for a in message.reply_to]
        if message.headers:
            body["headers"] = dict(message.headers)
        if message.attachments:
            body["attachments"] = [
                {
                    "filename": a.filename,
                    "content": a.content,
                    **({"content_type": a.content_type} if a.content_type else {}),
                    **({"content_id": a.content_id} if a.content_id else {}),
                }
                for a in message.attachments
            ]
        if message.tags:
            body["tags"] = [
                {"name": key, "value": value}
                for key, value in sorted(message.tags.items())
            ]
        return body

    async def send_email(self, message: EmailMessage, **kwargs: Any) -> EmailDeliveryResult:
        del kwargs
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._api_base}/emails",
                headers=self._headers(),
                json=self._payload(message),
            )
        raise_for_email_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        message_id = str(data.get("id") or "")
        if not message_id:
            raise EmailDeliveryError(
                "Resend response missing id",
                status=resp.status_code,
                provider=self.provider,
            )
        logger.info(
            "resend.email_send message_id=%s to=%s fp=%s",
            message_id, len(message.to), self.token_fp(),
        )
        return EmailDeliveryResult(
            provider=self.provider,
            message_id=message_id,
            accepted=[a.email for a in message.to],
            raw=data,
        )


__all__ = ["RESEND_API_BASE", "ResendEmailDeliveryAdapter"]
