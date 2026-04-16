"""W9 #283 — Unified CMSSource interface.

Every Headless CMS provider (Sanity / Strapi / Contentful / Directus /
any future) implements this single abstract class so upstream consumers
(skill-astro scaffolds, HMI forms, batch importers) can swap providers
without branching on vendor strings.

The interface is intentionally small — two operations:

    fetch(query)            Read entries from the CMS. ``query`` shape is
                            provider-native (GROQ for Sanity, REST filter
                            dict for Strapi/Contentful/Directus) — the
                            adapter is responsible for normalising the
                            response into ``CMSEntry`` objects.
    webhook_handler(...)    Verify the inbound webhook signature and
                            normalise the payload into a ``CMSWebhookEvent``
                            suitable for the caller's rebuild / cache-bust
                            pipeline.

Secret handling
---------------
API tokens enter through ``from_encrypted_token()`` (ciphertext decrypted
via ``backend.secret_store``) or ``from_plaintext_token()`` (test / CLI
path). The instance never logs the raw token — only ``token_fingerprint()``.

Error handling
--------------
All adapters raise ``CMSError`` (or subclasses); HTTP 401 / 403 / 404 /
429 map to typed subclasses so upstream routers can select HTTP status
codes without pattern-matching on strings. Signature failures raise
``CMSSignatureError`` (ALWAYS 401 — never leak which field failed).

Async vs sync
-------------
All network operations are async — adapters share ``httpx.AsyncClient``
to match the rest of the backend (``backend/deploy``, ``backend/agents``).
Signature verification (``verify_signature``) is sync because every
supported provider's scheme is pure HMAC / shared-secret comparison.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Optional, Union

from backend import secret_store

logger = logging.getLogger(__name__)


# ── Error hierarchy ──────────────────────────────────────────────

class CMSError(Exception):
    """Base for all CMS adapter errors."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidCMSTokenError(CMSError):
    """401 — API token invalid / revoked."""


class MissingCMSScopeError(CMSError):
    """403 — token lacks required permission / dataset visibility."""


class CMSNotFoundError(CMSError):
    """404 — requested entry / content type does not exist."""


class CMSRateLimitError(CMSError):
    """429 — provider rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class CMSSignatureError(CMSError):
    """Webhook signature verification failed — caller must return HTTP 401.

    Kept distinct from ``InvalidCMSTokenError`` because the trust domain
    differs: the token authenticates the OmniSight process to the CMS,
    whereas the webhook signature authenticates the CMS to OmniSight.
    """


class CMSQueryError(CMSError):
    """400 — query syntax invalid for the provider (malformed GROQ,
    unknown filter operator, etc.)."""


# ── Data models ──────────────────────────────────────────────────

@dataclass
class CMSEntry:
    """Normalised CMS entry — same shape regardless of provider.

    ``id`` is the provider's stable identifier (``_id`` for Sanity,
    ``sys.id`` for Contentful, numeric primary key for Strapi / Directus).
    ``content_type`` is the schema / collection name (``post``, ``article``,
    etc.). ``fields`` is the deserialised document body. ``raw`` carries
    the untouched provider response so advanced callers can read
    provider-specific metadata (locale, draft flag, system fields) without
    the adapter having to promote every hint into the normalised schema.
    """

    id: str
    content_type: str
    fields: dict[str, Any]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    locale: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content_type": self.content_type,
            "fields": self.fields,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "locale": self.locale,
        }


@dataclass
class CMSWebhookEvent:
    """Normalised webhook event.

    ``action`` is a coarse-grained lifecycle verb
    (``create`` / ``update`` / ``delete`` / ``publish`` / ``unpublish``);
    unknown / provider-specific actions fall through as ``other``.
    ``entry_id`` identifies the document that changed (may be ``None`` for
    bulk events). ``content_type`` is the schema / collection name.
    """

    provider: str
    action: str
    entry_id: Optional[str] = None
    content_type: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "action": self.action,
            "entry_id": self.entry_id,
            "content_type": self.content_type,
        }


# ── Token utilities ──────────────────────────────────────────────

def token_fingerprint(token: Optional[str]) -> str:
    """Return a log-safe fingerprint — never the full token."""
    if not token or len(token) <= 8:
        return "****"
    return f"…{token[-4:]}"


def hmac_sha256_hex(secret: Union[str, bytes], raw_body: Union[str, bytes]) -> str:
    """HMAC-SHA256 over ``raw_body`` keyed with ``secret`` → lowercase hex.

    Sanity and Strapi webhook schemes both use HMAC-SHA256; pulling the
    primitive into the base module lets every adapter call it without
    importing ``hashlib``/``hmac`` locally.
    """
    key = secret.encode() if isinstance(secret, str) else secret
    body = raw_body.encode() if isinstance(raw_body, str) else raw_body
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def constant_time_equals(a: Optional[str], b: Optional[str]) -> bool:
    """Length-safe constant-time string comparison.

    ``hmac.compare_digest`` raises on ``None`` — we pre-filter so
    caller code can pass raw header values without a guard.
    """
    if a is None or b is None:
        return False
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


# ── Interface ────────────────────────────────────────────────────

class CMSSource(ABC):
    """Abstract base for every Headless CMS adapter.

    Subclasses MUST set a ``provider`` classvar and implement the two
    abstract methods (``fetch`` + ``webhook_handler``). They SHOULD NOT
    override ``__init__`` — instead, override ``_configure()`` for
    provider-specific init (base URL, project id, dataset, etc.).
    """

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        self._token = token
        self._webhook_secret = webhook_secret
        self._timeout = timeout
        self._configure(**kwargs)

    # ── Construction helpers ──

    @classmethod
    def from_encrypted_token(
        cls,
        ciphertext: str,
        *,
        webhook_secret: Optional[str] = None,
        webhook_secret_ciphertext: Optional[str] = None,
        **kwargs: Any,
    ) -> "CMSSource":
        """Decrypt the ciphertext via ``backend.secret_store`` and build
        an adapter. This is the preferred entry point from routers — the
        plaintext token never appears in a log or dict dump.

        The optional ``webhook_secret_ciphertext`` is decrypted through
        the same store so operators can keep *both* secrets at rest.
        """
        token = secret_store.decrypt(ciphertext)
        if webhook_secret_ciphertext and not webhook_secret:
            webhook_secret = secret_store.decrypt(webhook_secret_ciphertext)
        return cls(token=token, webhook_secret=webhook_secret, **kwargs)

    @classmethod
    def from_plaintext_token(
        cls,
        token: Optional[str] = None,
        *,
        webhook_secret: Optional[str] = None,
        **kwargs: Any,
    ) -> "CMSSource":
        """Build an adapter from a plaintext token. Only the CLI / tests
        should call this; production code paths go through
        ``from_encrypted_token``."""
        return cls(token=token, webhook_secret=webhook_secret, **kwargs)

    # ── Hooks ──

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup (base URL, project id,
        dataset, collection prefix, etc.)."""
        # Default no-op — adapters that need no extra config use it as-is.
        pass

    # ── Public logging helpers ──

    def token_fp(self) -> str:
        return token_fingerprint(self._token)

    def webhook_secret_fp(self) -> str:
        return token_fingerprint(self._webhook_secret)

    # ── Signature verification (shared) ──

    def verify_signature(
        self,
        signature: Optional[str],
        raw_body: Union[str, bytes],
        *,
        scheme: str = "hmac-sha256",
    ) -> bool:
        """Verify a webhook signature against the configured secret.

        ``scheme`` selects the verification algorithm:
          * ``hmac-sha256`` — Sanity, Strapi default, Directus (computes
            HMAC-SHA256 hex over the raw body).
          * ``shared-secret`` — Contentful, Directus custom-header mode
            (constant-time compare of the header to the secret).
        """
        if not self._webhook_secret:
            return False
        if scheme == "hmac-sha256":
            expected = hmac_sha256_hex(self._webhook_secret, raw_body)
            return constant_time_equals(signature, expected)
        if scheme == "shared-secret":
            return constant_time_equals(signature, self._webhook_secret)
        raise ValueError(f"Unknown signature scheme: {scheme!r}")

    # ── Abstract interface ──

    @abstractmethod
    async def fetch(
        self,
        query: Union[str, Mapping[str, Any]],
        *,
        params: Optional[Mapping[str, Any]] = None,
        content_type: Optional[str] = None,
    ) -> list[CMSEntry]:
        """Fetch entries from the CMS.

        ``query`` shape is provider-native:
          * Sanity — a GROQ query string.
          * Strapi / Directus — a filter dict merged into the REST query
            string (``{"filters": {"published": True}}`` style).
          * Contentful — a filter dict passed to ``/entries``
            (``{"content_type": "post", "limit": 20}``).

        ``params`` is merged into the provider query string for
        pagination, ordering, field selection. ``content_type`` is a
        convenience hint — when set, the adapter scopes the query to
        that collection / schema even if the caller did not include it
        in ``query``.
        """

    @abstractmethod
    async def webhook_handler(
        self,
        payload: Union[str, bytes, Mapping[str, Any]],
        *,
        headers: Optional[Mapping[str, str]] = None,
    ) -> CMSWebhookEvent:
        """Verify the inbound webhook signature and normalise the event.

        ``payload`` is the raw request body (bytes or str) when
        signature verification is needed; pre-parsed dicts are accepted
        for test harnesses. ``headers`` carries the provider-specific
        signature / topic headers (case-insensitive lookup).

        Raises ``CMSSignatureError`` when the signature does not verify.
        """


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
    "hmac_sha256_hex",
    "token_fingerprint",
]
