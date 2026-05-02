"""FS.4.1 -- Unified email service adapter interface.

Resend / Postmark / AWS SES expose provider APIs for transactional email
delivery before later FS.4 rows add template registry and webhook
handling. This module mirrors ``backend.storage_provisioning.base``:
callers construct a provider adapter from an encrypted or plaintext
token, call ``send_email()``, then persist the returned provider message
id in the caller-owned workflow.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable classes/functions only. No module-level
cache, singleton, or mutable registry is read or written; provider
factory functions in ``backend.email_delivery`` materialize fresh lists
per call, so uvicorn workers do not share runtime state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from backend import secret_store
from backend.deploy.base import token_fingerprint


class EmailDeliveryError(Exception):
    """Base for all email delivery adapter errors."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidEmailDeliveryTokenError(EmailDeliveryError):
    """401 -- API token invalid / revoked."""


class MissingEmailDeliveryScopeError(EmailDeliveryError):
    """403 -- API token lacks required permission."""


class EmailDeliveryConflictError(EmailDeliveryError):
    """409 / 422 -- provider rejected message shape or sender domain."""


class EmailDeliveryRateLimitError(EmailDeliveryError):
    """429 -- provider rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


@dataclass
class EmailAddress:
    """Email address with optional display name."""

    email: str
    name: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.email or "@" not in self.email:
            raise ValueError("email address is required")
        self.email = self.email.strip()
        if self.name is not None:
            self.name = self.name.strip() or None

    def formatted(self) -> str:
        if self.name:
            return f"{self.name} <{self.email}>"
        return self.email

    def to_dict(self) -> dict[str, str]:
        data = {"email": self.email}
        if self.name:
            data["name"] = self.name
        return data


@dataclass
class EmailAttachment:
    """Outbound attachment payload."""

    filename: str
    content: str
    content_type: Optional[str] = None
    content_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.filename:
            raise ValueError("attachment filename is required")
        if not self.content:
            raise ValueError("attachment content is required")

    def to_dict(self) -> dict[str, str]:
        data = {
            "filename": self.filename,
            "content": self.content,
        }
        if self.content_type:
            data["content_type"] = self.content_type
        if self.content_id:
            data["content_id"] = self.content_id
        return data


@dataclass
class EmailMessage:
    """Provider-neutral transactional email payload."""

    sender: EmailAddress
    to: list[EmailAddress]
    subject: str
    text: Optional[str] = None
    html: Optional[str] = None
    cc: list[EmailAddress] = field(default_factory=list)
    bcc: list[EmailAddress] = field(default_factory=list)
    reply_to: list[EmailAddress] = field(default_factory=list)
    attachments: list[EmailAttachment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.to:
            raise ValueError("at least one recipient is required")
        if not self.subject or not self.subject.strip():
            raise ValueError("subject is required")
        if not (self.text and self.text.strip()) and not (self.html and self.html.strip()):
            raise ValueError("text or html body is required")
        self.subject = self.subject.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.sender.to_dict(),
            "to": [a.to_dict() for a in self.to],
            "subject": self.subject,
            "text": self.text,
            "html": self.html,
            "cc": [a.to_dict() for a in self.cc],
            "bcc": [a.to_dict() for a in self.bcc],
            "reply_to": [a.to_dict() for a in self.reply_to],
            "attachments": [a.to_dict() for a in self.attachments],
            "headers": dict(self.headers),
            "tags": dict(self.tags),
        }


@dataclass
class EmailDeliveryResult:
    """Outcome of ``adapter.send_email(...)``."""

    provider: str
    message_id: str
    status: str = "sent"
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "message_id": self.message_id,
            "status": self.status,
            "accepted": list(self.accepted),
            "rejected": list(self.rejected),
        }


class EmailDeliveryAdapter(ABC):
    """Abstract base for every transactional email provider adapter."""

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        token: str,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        self._token = token
        self._timeout = timeout
        self._configure(**kwargs)

    @classmethod
    def from_encrypted_token(
        cls,
        ciphertext: str,
        **kwargs: Any,
    ) -> "EmailDeliveryAdapter":
        """Decrypt via ``backend.secret_store`` and build an adapter."""
        token = secret_store.decrypt(ciphertext)
        return cls(token=token, **kwargs)

    @classmethod
    def from_plaintext_token(
        cls,
        token: str,
        **kwargs: Any,
    ) -> "EmailDeliveryAdapter":
        """Build an adapter from a plaintext token for tests / CLI paths."""
        return cls(token=token, **kwargs)

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup."""
        pass

    def token_fp(self) -> str:
        return token_fingerprint(self._token)

    @abstractmethod
    async def send_email(self, message: EmailMessage, **kwargs: Any) -> EmailDeliveryResult:
        """Send one transactional email via the provider API."""


__all__ = [
    "EmailAddress",
    "EmailAttachment",
    "EmailDeliveryAdapter",
    "EmailDeliveryConflictError",
    "EmailDeliveryError",
    "EmailDeliveryRateLimitError",
    "EmailDeliveryResult",
    "EmailMessage",
    "InvalidEmailDeliveryTokenError",
    "MissingEmailDeliveryScopeError",
]
