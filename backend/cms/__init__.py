"""W9 #283 — Shared Headless CMS adapter library.

Exposes the unified ``CMSSource`` interface and a ``get_cms_source(provider)``
factory so routers / HMI forms / skill-astro scaffolds can select a
Headless CMS by its canonical string id:

    sanity      — Sanity.io (GROQ, HMAC-SHA256 webhook)
    strapi      — Strapi v4 / v5 (REST, HMAC or bearer-shared webhook)
    contentful  — Contentful Delivery / Preview (REST, shared-secret webhook)
    directus    — Directus v10+ (REST, HMAC or shared-secret webhook)

Example:

    from backend.cms import get_cms_source

    cls = get_cms_source("sanity")
    src = cls.from_encrypted_token(
        token_ciphertext,
        project_id="abc123", dataset="production",
        webhook_secret_ciphertext=wh_ciphertext,
    )
    entries = await src.fetch('*[_type == "post"]')
    event = await src.webhook_handler(raw_body, headers=req.headers)
"""

from __future__ import annotations

from backend.cms.base import (
    CMSEntry,
    CMSError,
    CMSNotFoundError,
    CMSQueryError,
    CMSRateLimitError,
    CMSSignatureError,
    CMSSource,
    CMSWebhookEvent,
    InvalidCMSTokenError,
    MissingCMSScopeError,
    constant_time_equals,
    hmac_sha256_hex,
    token_fingerprint,
)


def list_providers() -> list[str]:
    """Return the canonical id for every shipped CMS adapter."""
    return ["sanity", "strapi", "contentful", "directus"]


def get_cms_source(provider: str) -> type[CMSSource]:
    """Look up a CMS adapter class by its canonical provider string.

    Imports lazily so a broken/missing optional dependency in one
    adapter does not cascade to the others.
    """
    key = provider.strip().lower().replace("_", "-")
    if key in ("sanity", "sanity.io"):
        from backend.cms.sanity import SanityCMSSource
        return SanityCMSSource
    if key == "strapi":
        from backend.cms.strapi import StrapiCMSSource
        return StrapiCMSSource
    if key in ("contentful", "cf"):
        from backend.cms.contentful import ContentfulCMSSource
        return ContentfulCMSSource
    if key == "directus":
        from backend.cms.directus import DirectusCMSSource
        return DirectusCMSSource
    raise ValueError(
        f"Unknown CMS provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "CMSEntry",
    "CMSError",
    "CMSNotFoundError",
    "CMSQueryError",
    "CMSRateLimitError",
    "CMSSignatureError",
    "CMSSource",
    "CMSWebhookEvent",
    "InvalidCMSTokenError",
    "MissingCMSScopeError",
    "constant_time_equals",
    "get_cms_source",
    "hmac_sha256_hex",
    "list_providers",
    "token_fingerprint",
]
