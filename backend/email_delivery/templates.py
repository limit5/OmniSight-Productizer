"""FS.4.2 -- Transactional email template registry.

The registry renders provider-neutral ``EmailMessage`` payloads for the
FS.4.1 delivery adapters. It mirrors the static catalog shape used by
``backend.auth_provisioning.outbound_oauth``: immutable catalog items,
``MappingProxyType`` lookup, and small list/get/render helpers.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable catalog tuples and a read-only mapping
proxy. Every render derives a fresh ``EmailMessage`` from explicit
context values; no cache, singleton, env read, network IO, or mutable
shared state is used across uvicorn workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from string import Formatter
from types import MappingProxyType
from typing import Any, Mapping

from backend.email_delivery.base import EmailAddress, EmailMessage


class MissingEmailTemplateVariableError(ValueError):
    """Raised when a template render context is missing a required value."""


@dataclass(frozen=True)
class EmailTemplateRenderOptions:
    """Inputs for rendering a transactional email template."""

    template_id: str
    sender: EmailAddress
    to: tuple[EmailAddress, ...]
    context: Mapping[str, Any]
    cc: tuple[EmailAddress, ...] = ()
    bcc: tuple[EmailAddress, ...] = ()
    reply_to: tuple[EmailAddress, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.template_id or not self.template_id.strip():
            raise ValueError("template_id is required")
        if not self.to:
            raise ValueError("at least one recipient is required")


@dataclass(frozen=True)
class EmailTemplateItem:
    """One FS.4.2 transactional email template catalog entry."""

    template_id: str
    display_name: str
    subject: str
    text: str
    html: str
    required_variables: tuple[str, ...]
    category: str = "transactional"

    def render(self, options: EmailTemplateRenderOptions) -> EmailMessage:
        options.validate()
        context = _render_context(options.context)
        missing = [
            name
            for name in self.required_variables
            if name not in context or context[name] == ""
        ]
        if missing:
            raise MissingEmailTemplateVariableError(
                f"missing template variables for {self.template_id}: "
                f"{', '.join(missing)}"
            )
        tags = {"template": self.template_id, **dict(options.tags)}
        return EmailMessage(
            sender=options.sender,
            to=list(options.to),
            subject=_format_template(self.subject, context),
            text=_format_template(self.text, context),
            html=_format_template(self.html, context),
            cc=list(options.cc),
            bcc=list(options.bcc),
            reply_to=list(options.reply_to),
            headers=dict(options.headers),
            tags=tags,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "display_name": self.display_name,
            "subject": self.subject,
            "required_variables": list(self.required_variables),
            "category": self.category,
        }


EMAIL_TEMPLATE_IDS: tuple[str, ...] = (
    "welcome",
    "email-verification",
    "password-reset",
    "dsar-request-received",
    "dsar-export-ready",
    "invite",
    "mfa-code",
)


EMAIL_TEMPLATE_ITEMS: tuple[EmailTemplateItem, ...] = (
    EmailTemplateItem(
        template_id="welcome",
        display_name="Welcome",
        subject="Welcome to {product_name}",
        text=(
            "Hi {user_name},\n\n"
            "Welcome to {product_name}. You can open your workspace here:\n"
            "{app_url}\n\n"
            "If you did not create this account, contact {support_email}."
        ),
        html=(
            "<p>Hi {user_name},</p>"
            "<p>Welcome to {product_name}. You can open your workspace here:</p>"
            '<p><a href="{app_url}">{app_url}</a></p>'
            "<p>If you did not create this account, contact {support_email}.</p>"
        ),
        required_variables=(
            "user_name",
            "product_name",
            "app_url",
            "support_email",
        ),
    ),
    EmailTemplateItem(
        template_id="email-verification",
        display_name="Email verification",
        subject="Verify your {product_name} email",
        text=(
            "Hi {user_name},\n\n"
            "Verify your email address for {product_name}:\n"
            "{verification_url}\n\n"
            "This link expires in {expires_in}."
        ),
        html=(
            "<p>Hi {user_name},</p>"
            "<p>Verify your email address for {product_name}:</p>"
            '<p><a href="{verification_url}">Verify email</a></p>'
            "<p>This link expires in {expires_in}.</p>"
        ),
        required_variables=(
            "user_name",
            "product_name",
            "verification_url",
            "expires_in",
        ),
    ),
    EmailTemplateItem(
        template_id="password-reset",
        display_name="Password reset",
        subject="Reset your {product_name} password",
        text=(
            "Hi {user_name},\n\n"
            "Reset your {product_name} password here:\n"
            "{reset_url}\n\n"
            "This link expires in {expires_in}. If you did not request this, "
            "you can ignore this email."
        ),
        html=(
            "<p>Hi {user_name},</p>"
            "<p>Reset your {product_name} password here:</p>"
            '<p><a href="{reset_url}">Reset password</a></p>'
            "<p>This link expires in {expires_in}. If you did not request this, "
            "you can ignore this email.</p>"
        ),
        required_variables=(
            "user_name",
            "product_name",
            "reset_url",
            "expires_in",
        ),
    ),
    EmailTemplateItem(
        template_id="dsar-request-received",
        display_name="DSAR request received",
        subject="{product_name} privacy request {request_id} received",
        text=(
            "Hi {user_name},\n\n"
            "We received your {request_type} privacy request {request_id}. "
            "The response SLA is 30 days, and the current due date is "
            "{due_at}.\n\n"
            "If you did not submit this request, contact {support_email}."
        ),
        html=(
            "<p>Hi {user_name},</p>"
            "<p>We received your {request_type} privacy request "
            "{request_id}. The response SLA is 30 days, and the current "
            "due date is {due_at}.</p>"
            "<p>If you did not submit this request, contact "
            "{support_email}.</p>"
        ),
        required_variables=(
            "user_name",
            "product_name",
            "request_type",
            "request_id",
            "due_at",
            "support_email",
        ),
        category="privacy",
    ),
    EmailTemplateItem(
        template_id="dsar-export-ready",
        display_name="DSAR export ready",
        subject="Your {product_name} data export is ready",
        text=(
            "Hi {user_name},\n\n"
            "Your data export for request {request_id} is ready:\n"
            "{download_url}\n\n"
            "This link expires in {expires_in}."
        ),
        html=(
            "<p>Hi {user_name},</p>"
            "<p>Your data export for request {request_id} is ready:</p>"
            '<p><a href="{download_url}">Download export</a></p>'
            "<p>This link expires in {expires_in}.</p>"
        ),
        required_variables=(
            "user_name",
            "product_name",
            "request_id",
            "download_url",
            "expires_in",
        ),
        category="privacy",
    ),
    EmailTemplateItem(
        template_id="invite",
        display_name="Invite",
        subject="{inviter_name} invited you to {workspace_name}",
        text=(
            "Hi {user_name},\n\n"
            "{inviter_name} invited you to join {workspace_name} on "
            "{product_name}:\n{invite_url}\n\n"
            "This invite expires in {expires_in}."
        ),
        html=(
            "<p>Hi {user_name},</p>"
            "<p>{inviter_name} invited you to join {workspace_name} on "
            "{product_name}:</p>"
            '<p><a href="{invite_url}">Accept invite</a></p>'
            "<p>This invite expires in {expires_in}.</p>"
        ),
        required_variables=(
            "user_name",
            "inviter_name",
            "workspace_name",
            "product_name",
            "invite_url",
            "expires_in",
        ),
    ),
    EmailTemplateItem(
        template_id="mfa-code",
        display_name="MFA code",
        subject="Your {product_name} sign-in code",
        text=(
            "Hi {user_name},\n\n"
            "Your {product_name} sign-in code is {code}.\n\n"
            "This code expires in {expires_in}."
        ),
        html=(
            "<p>Hi {user_name},</p>"
            "<p>Your {product_name} sign-in code is <strong>{code}</strong>.</p>"
            "<p>This code expires in {expires_in}.</p>"
        ),
        required_variables=("user_name", "product_name", "code", "expires_in"),
    ),
)


EMAIL_TEMPLATES: Mapping[str, EmailTemplateItem] = MappingProxyType(
    {item.template_id: item for item in EMAIL_TEMPLATE_ITEMS}
)


def list_email_templates() -> list[str]:
    """Return FS.4.2 transactional email template ids."""
    return list(EMAIL_TEMPLATE_IDS)


def get_email_template(template_id: str) -> EmailTemplateItem:
    """Return one FS.4.2 transactional email template entry."""
    key = template_id.strip().lower().replace("_", "-")
    try:
        return EMAIL_TEMPLATES[key]
    except KeyError:
        raise KeyError(
            f"unknown email template {template_id!r}; "
            f"known: {', '.join(EMAIL_TEMPLATE_IDS)}"
        ) from None


def render_email_template(options: EmailTemplateRenderOptions) -> EmailMessage:
    """Render a catalog template into a provider-neutral email message."""
    options.validate()
    item = get_email_template(options.template_id)
    return item.render(options)


def _render_context(context: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): escape(str(value), quote=True) for key, value in context.items()}


def _format_template(template: str, context: Mapping[str, str]) -> str:
    placeholders = {
        field_name
        for _, field_name, _, _ in Formatter().parse(template)
        if field_name
    }
    missing = sorted(name for name in placeholders if name not in context)
    if missing:
        raise MissingEmailTemplateVariableError(
            f"missing template variables: {', '.join(missing)}"
        )
    return template.format_map(dict(context))


__all__ = [
    "EMAIL_TEMPLATE_IDS",
    "EMAIL_TEMPLATE_ITEMS",
    "EMAIL_TEMPLATES",
    "EmailTemplateItem",
    "EmailTemplateRenderOptions",
    "MissingEmailTemplateVariableError",
    "get_email_template",
    "list_email_templates",
    "render_email_template",
]
