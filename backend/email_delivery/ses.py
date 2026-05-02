"""FS.4.1 -- AWS SES transactional email adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.email_delivery.base import (
    EmailDeliveryAdapter,
    EmailDeliveryError,
    EmailDeliveryResult,
    EmailMessage,
)
from backend.email_delivery.http import raise_for_email_response

logger = logging.getLogger(__name__)

SES_SERVICE = "ses"


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    key_date = hmac.new(f"AWS4{secret}".encode(), datestamp.encode(), hashlib.sha256).digest()
    key_region = hmac.new(key_date, region.encode(), hashlib.sha256).digest()
    key_service = hmac.new(key_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()


class SESEmailDeliveryAdapter(EmailDeliveryAdapter):
    """AWS SES v2 API adapter (``provider='aws-ses'``)."""

    provider = "aws-ses"
    service = SES_SERVICE

    def _configure(
        self,
        *,
        access_key_id: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        configuration_set_name: str | None = None,
        **_: Any,
    ) -> None:
        if not access_key_id:
            raise ValueError("SESEmailDeliveryAdapter requires access_key_id")
        self._access_key_id = access_key_id
        self._region = region
        self._endpoint_url = (
            endpoint_url.rstrip("/")
            if endpoint_url
            else f"https://email.{region}.amazonaws.com"
        )
        self._configuration_set_name = configuration_set_name

    def _url(self) -> str:
        return f"{self._endpoint_url}/v2/email/outbound-emails"

    def _headers(self, url: str, payload: bytes) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        parsed = urlparse(url)
        host = parsed.netloc
        canonical_uri = parsed.path or "/"
        headers = {
            "content-type": "application/json",
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
        canonical_request = "\n".join([
            "POST",
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        scope = f"{datestamp}/{self._region}/{self.service}/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signature = hmac.new(
            _signing_key(self._token, datestamp, self._region, self.service),
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    def _payload(self, message: EmailMessage) -> dict[str, Any]:
        body: dict[str, Any] = {
            "FromEmailAddress": message.sender.formatted(),
            "Destination": {
                "ToAddresses": [a.formatted() for a in message.to],
            },
            "Content": {
                "Simple": {
                    "Subject": {"Data": message.subject},
                    "Body": {},
                },
            },
        }
        simple = body["Content"]["Simple"]
        if message.text:
            simple["Body"]["Text"] = {"Data": message.text}
        if message.html:
            simple["Body"]["Html"] = {"Data": message.html}
        if message.cc:
            body["Destination"]["CcAddresses"] = [a.formatted() for a in message.cc]
        if message.bcc:
            body["Destination"]["BccAddresses"] = [a.formatted() for a in message.bcc]
        if message.reply_to:
            body["ReplyToAddresses"] = [a.formatted() for a in message.reply_to]
        if self._configuration_set_name:
            body["ConfigurationSetName"] = self._configuration_set_name
        if message.tags:
            body["EmailTags"] = [
                {"Name": key, "Value": value}
                for key, value in sorted(message.tags.items())
            ]
        return body

    async def send_email(self, message: EmailMessage, **kwargs: Any) -> EmailDeliveryResult:
        del kwargs
        if message.attachments or message.headers:
            raise EmailDeliveryError(
                "SES adapter supports simple text/html mail only in FS.4.1",
                provider=self.provider,
            )
        body = self._payload(message)
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        url = self._url()
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(url, headers=self._headers(url, payload), content=payload)
        raise_for_email_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        message_id = str(data.get("MessageId") or data.get("MessageID") or "")
        if not message_id:
            raise EmailDeliveryError(
                "SES response missing MessageId",
                status=resp.status_code,
                provider=self.provider,
            )
        logger.info(
            "aws_ses.email_send message_id=%s to=%s fp=%s",
            message_id, len(message.to), self.token_fp(),
        )
        return EmailDeliveryResult(
            provider=self.provider,
            message_id=message_id,
            accepted=[a.email for a in message.to],
            raw=data,
        )


__all__ = ["SESEmailDeliveryAdapter", "SES_SERVICE"]
