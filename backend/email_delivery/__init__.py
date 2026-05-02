"""FS.4.1 -- Transactional email service adapters package."""

from __future__ import annotations

from backend.email_delivery.base import (
    EmailAddress,
    EmailAttachment,
    EmailDeliveryAdapter,
    EmailDeliveryConflictError,
    EmailDeliveryError,
    EmailDeliveryRateLimitError,
    EmailDeliveryResult,
    EmailMessage,
    InvalidEmailDeliveryTokenError,
    MissingEmailDeliveryScopeError,
)
from backend.email_delivery.templates import (
    EMAIL_TEMPLATE_IDS,
    EMAIL_TEMPLATE_ITEMS,
    EMAIL_TEMPLATES,
    EmailTemplateItem,
    EmailTemplateRenderOptions,
    MissingEmailTemplateVariableError,
    get_email_template,
    list_email_templates,
    render_email_template,
)


def list_providers() -> list[str]:
    """Return the canonical id for every shipped email delivery adapter."""
    return ["resend", "postmark", "aws-ses"]


def get_adapter(provider: str) -> type[EmailDeliveryAdapter]:
    """Look up an adapter class by canonical provider string."""
    key = provider.strip().lower().replace("_", "-")
    if key == "resend":
        from backend.email_delivery.resend import ResendEmailDeliveryAdapter
        return ResendEmailDeliveryAdapter
    if key == "postmark":
        from backend.email_delivery.postmark import PostmarkEmailDeliveryAdapter
        return PostmarkEmailDeliveryAdapter
    if key in ("aws-ses", "ses"):
        from backend.email_delivery.ses import SESEmailDeliveryAdapter
        return SESEmailDeliveryAdapter
    raise ValueError(
        f"Unknown email delivery provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "EmailAddress",
    "EmailAttachment",
    "EmailDeliveryAdapter",
    "EmailDeliveryConflictError",
    "EmailDeliveryError",
    "EmailDeliveryRateLimitError",
    "EmailDeliveryResult",
    "EmailMessage",
    "EMAIL_TEMPLATE_IDS",
    "EMAIL_TEMPLATE_ITEMS",
    "EMAIL_TEMPLATES",
    "EmailTemplateItem",
    "EmailTemplateRenderOptions",
    "InvalidEmailDeliveryTokenError",
    "MissingEmailDeliveryScopeError",
    "MissingEmailTemplateVariableError",
    "get_adapter",
    "get_email_template",
    "list_email_templates",
    "list_providers",
    "render_email_template",
]
