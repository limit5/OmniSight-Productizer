"""W10 #284 — Unified RUMAdapter interface.

Every Real-User-Monitoring provider (Sentry / Datadog / future Grafana
Faro / New Relic Browser) implements this single abstract class so the
upstream consumers — the W6/W7/W8 web-vertical scaffolds, the
in-process Web Vitals aggregator, the error→JIRA router — can swap
providers without branching on vendor strings.

The interface is intentionally small — three operations:

    send_vital(WebVital)         Forward a Core Web Vital sample to the
                                 vendor's beacon endpoint.
    send_error(ErrorEvent)       Forward a browser error event.
    browser_snippet()            Return a JS snippet to drop in <head>
                                 — wires up the vendor's Browser SDK so
                                 the page emits vitals + errors without
                                 the user touching scaffold templates.

Secret handling
---------------
DSN / API key enters through ``from_encrypted_dsn()`` (ciphertext
decrypted via ``backend.secret_store``) or ``from_plaintext_dsn()``
(test / CLI path). The instance never logs the raw secret — only
``dsn_fingerprint()`` (last 4 chars).

Error handling
--------------
All adapters raise ``RUMError`` (or subclasses); HTTP 401 / 403 / 429
map to typed subclasses so the upstream router can pick HTTP status
codes without pattern-matching on strings.

Async vs sync
-------------
Network operations are async — adapters share ``httpx.AsyncClient`` to
match the rest of the backend (``backend/deploy``, ``backend/cms``).
Browser snippet rendering is sync — pure string formatting, no I/O.
"""

from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Optional

from backend import secret_store

logger = logging.getLogger(__name__)


# ── Error hierarchy ──────────────────────────────────────────────


class RUMError(Exception):
    """Base for all RUM adapter errors."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidRUMTokenError(RUMError):
    """401 — DSN / API key invalid / revoked."""


class MissingRUMScopeError(RUMError):
    """403 — token lacks required permission (write:events)."""


class RUMRateLimitError(RUMError):
    """429 — provider rate limit hit (Sentry / Datadog quotas)."""

    def __init__(self, message: str, retry_after: int = 60, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class RUMPayloadError(RUMError):
    """400 — payload rejected (malformed envelope, missing required field)."""


# ── Core Web Vitals thresholds ───────────────────────────────────
#
# Source: https://web.dev/articles/vitals (Google Core Web Vitals 2024-2026).
# INP replaced FID on 2024-03-12 — we no longer track FID.

GOOD_THRESHOLDS: dict[str, float] = {
    "LCP": 2500.0,    # ms
    "INP": 200.0,     # ms
    "CLS": 0.1,       # unitless
    "TTFB": 800.0,    # ms
    "FCP": 1800.0,    # ms
}

POOR_THRESHOLDS: dict[str, float] = {
    "LCP": 4000.0,
    "INP": 500.0,
    "CLS": 0.25,
    "TTFB": 1800.0,
    "FCP": 3000.0,
}

KNOWN_VITALS: tuple[str, ...] = ("LCP", "INP", "CLS", "TTFB", "FCP")


def classify_vital(name: str, value: float) -> str:
    """Return ``good`` / ``needs-improvement`` / ``poor`` for a CWV sample.

    Unknown metric names default to ``"unknown"`` so the upstream
    aggregator can still bucket the sample without dropping it.
    """
    key = (name or "").upper()
    if key not in GOOD_THRESHOLDS:
        return "unknown"
    if value <= GOOD_THRESHOLDS[key]:
        return "good"
    if value <= POOR_THRESHOLDS[key]:
        return "needs-improvement"
    return "poor"


# ── Data models ──────────────────────────────────────────────────


@dataclass
class WebVital:
    """Normalised Core Web Vitals sample.

    ``name`` is the metric label (LCP / INP / CLS / TTFB / FCP).
    ``value`` is in ms for time-based metrics; CLS is unitless.
    ``rating`` is the bucket — when omitted, ``__post_init__`` derives
    it from the thresholds. ``page`` is the route or URL path the
    sample was emitted from. ``session_id`` lets the dashboard split
    real users from bots / synthetic load.
    """

    name: str
    value: float
    page: str = "/"
    session_id: str = ""
    rating: str = ""
    timestamp: float = 0.0
    nav_type: str = "navigate"
    user_agent: str = ""
    locale: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = (self.name or "").upper()
        if self.timestamp <= 0:
            self.timestamp = time.time()
        if not self.rating:
            self.rating = classify_vital(self.name, self.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "rating": self.rating,
            "page": self.page,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "nav_type": self.nav_type,
            "user_agent": self.user_agent,
            "locale": self.locale,
        }


@dataclass
class ErrorEvent:
    """Normalised browser error event.

    ``fingerprint`` is the dedup key — when omitted, ``__post_init__``
    derives a SHA-1 of (release || message || top-of-stack-frame) so
    the same error in the same release collapses into one ticket.
    """

    message: str
    page: str = "/"
    session_id: str = ""
    level: str = "error"
    stack: str = ""
    fingerprint: str = ""
    release: str = ""
    environment: str = "production"
    user_agent: str = ""
    timestamp: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.level = (self.level or "error").lower()
        if self.timestamp <= 0:
            self.timestamp = time.time()
        if not self.fingerprint:
            self.fingerprint = derive_fingerprint(
                release=self.release,
                message=self.message,
                stack=self.stack,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "level": self.level,
            "page": self.page,
            "session_id": self.session_id,
            "stack": self.stack,
            "fingerprint": self.fingerprint,
            "release": self.release,
            "environment": self.environment,
            "user_agent": self.user_agent,
            "timestamp": self.timestamp,
        }


def derive_fingerprint(*, release: str, message: str, stack: str) -> str:
    """Stable dedup key for an error: release + message + top frame.

    The top frame strips columns / line numbers so the same logical bug
    across two builds groups together; the ``release`` segment keeps
    fixed-and-regressed errors apart.
    """
    top_frame = ""
    if stack:
        for line in stack.splitlines():
            line = line.strip()
            if not line:
                continue
            # Drop numeric ":<line>:<col>" tails so trivially shifted
            # source lines still hash the same.
            top_frame = _strip_line_col(line)
            break
    payload = f"{release}|{message.strip()}|{top_frame}"
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()


def _strip_line_col(line: str) -> str:
    """``foo.js:42:10`` → ``foo.js`` so two columns of the same frame match."""
    parts = line.rsplit(":", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return parts[0]
    if len(parts) >= 2 and parts[-1].isdigit():
        return ":".join(parts[:-1])
    return line


# ── DSN / token utilities ────────────────────────────────────────


def dsn_fingerprint(dsn: Optional[str]) -> str:
    """Log-safe fingerprint for a DSN / API key — never the full value."""
    if not dsn or len(dsn) <= 8:
        return "****"
    return f"…{dsn[-4:]}"


# ── Interface ────────────────────────────────────────────────────


class RUMAdapter(ABC):
    """Abstract base for every Real-User-Monitoring adapter.

    Subclasses MUST set a ``provider`` classvar and implement the three
    abstract methods. They SHOULD NOT override ``__init__`` — instead,
    override ``_configure()`` for provider-specific init (region /
    application id / cluster / etc.).
    """

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        dsn: Optional[str] = None,
        api_key: Optional[str] = None,
        application_id: Optional[str] = None,
        environment: str = "production",
        release: Optional[str] = None,
        sample_rate: float = 1.0,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError(
                f"sample_rate must be in [0.0, 1.0], got {sample_rate}"
            )
        self._dsn = dsn
        self._api_key = api_key
        self._application_id = application_id
        self._environment = environment
        self._release = release
        self._sample_rate = sample_rate
        self._timeout = timeout
        self._configure(**kwargs)

    # ── Construction helpers ──

    @classmethod
    def from_encrypted_dsn(
        cls,
        ciphertext: str,
        *,
        api_key_ciphertext: Optional[str] = None,
        **kwargs: Any,
    ) -> "RUMAdapter":
        """Decrypt the ciphertext via ``backend.secret_store`` and build
        an adapter. Preferred entry point from routers — the plaintext
        DSN never appears in a log or dict dump.

        ``api_key_ciphertext`` lets adapters that need a separate write
        key (Datadog ingest API key vs RUM client token) carry both
        secrets at rest.
        """
        dsn = secret_store.decrypt(ciphertext)
        api_key: Optional[str] = None
        if api_key_ciphertext:
            api_key = secret_store.decrypt(api_key_ciphertext)
        return cls(dsn=dsn, api_key=api_key, **kwargs)

    @classmethod
    def from_plaintext_dsn(
        cls,
        dsn: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "RUMAdapter":
        """Build an adapter from a plaintext DSN. Only the CLI / tests
        should call this; production code paths go through
        ``from_encrypted_dsn``."""
        return cls(dsn=dsn, api_key=api_key, **kwargs)

    # ── Hooks ──

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup (region / cluster /
        application id / etc.)."""
        pass

    # ── Public logging helpers ──

    def dsn_fp(self) -> str:
        return dsn_fingerprint(self._dsn)

    def api_key_fp(self) -> str:
        return dsn_fingerprint(self._api_key)

    # ── Abstract interface ──

    @abstractmethod
    async def send_vital(self, vital: WebVital) -> None:
        """Forward a Web Vitals sample to the vendor.

        Implementations should respect ``self._sample_rate`` — the base
        helper ``_should_sample()`` returns True with probability
        ``sample_rate``.
        """

    @abstractmethod
    async def send_error(self, event: ErrorEvent) -> None:
        """Forward a browser error event to the vendor.

        Errors are NEVER sampled — every error reaches the vendor
        regardless of ``sample_rate``. Sampling errors hides regressions.
        """

    @abstractmethod
    def browser_snippet(self) -> str:
        """Return the JS snippet to embed in the page <head>.

        The snippet wires the vendor's Browser SDK to capture Core Web
        Vitals + uncaught errors and POST them to the vendor's beacon.
        Returned string is safe to drop into a Jinja template — no
        ``<script>`` wrapper, no inline secrets that the caller didn't
        explicitly enable via ``include_dsn=True``.
        """

    # ── Sampling ──

    def _should_sample(self) -> bool:
        """Probability check — True with probability ``sample_rate``.

        Uses ``random.random()`` (not crypto) — sampling is statistical
        not security-critical. Always True at sample_rate >= 1.0 and
        always False at sample_rate <= 0.0 to avoid floating-point edge
        cases at the ends.
        """
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        import random
        return random.random() < self._sample_rate


__all__ = [
    "ErrorEvent",
    "GOOD_THRESHOLDS",
    "InvalidRUMTokenError",
    "KNOWN_VITALS",
    "MissingRUMScopeError",
    "POOR_THRESHOLDS",
    "RUMAdapter",
    "RUMError",
    "RUMPayloadError",
    "RUMRateLimitError",
    "WebVital",
    "classify_vital",
    "derive_fingerprint",
    "dsn_fingerprint",
]
