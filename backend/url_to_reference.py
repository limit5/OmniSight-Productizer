"""V1 #7 (issue #317) — URL → reference → UI code pipeline.

Accepts a public web URL ("make me a page that looks like X") and
produces the same shape of artifact the UI Designer skill expects from
the sibling vision / Figma entry points: a
:class:`URLReferenceResult` containing the fetched reference metadata,
an extraction of the structural + colour hints surfaced by the page,
generated TSX, and a :class:`LintReport` from
:mod:`backend.component_consistency_linter` (optionally after one
mechanical auto-fix pass).

The high-level pipeline is::

    normalize_url(raw) ──► fetch_url(url, fetcher) ──► URLReference
                                                       │
                                                       ▼
                                        capture_screenshot(url, screenshotter)
                                                       │
                                                       ▼
                                         extract_from_html(reference.html)
                                                       │
                                                       ▼
                                 build_url_generation_prompt(...)
                                                       │
                                                       ▼
                                         LLM (Opus 4.7 multimodal)
                                                       │
                                                       ▼
                              extract_tsx_from_response → lint → auto_fix

Why this module exists
----------------------

The UI Designer skill (``configs/roles/ui-designer.md``) lists four
"fact-side" entry points for step-zero: registry, design tokens,
Figma MCP, **and URL → reference**. The sibling modules cover the
first three:

* :mod:`backend.ui_component_registry` — what shadcn components exist.
* :mod:`backend.design_token_loader` — which CSS tokens they must use.
* :mod:`backend.figma_to_ui` — MCP ``get_design_context`` wiring.
* :mod:`backend.vision_to_ui` — raw screenshot / sketch wiring.

This module adds **"take this URL as a visual reference"**. The key
word is *reference* — the MCP docs and the skill both emphasise that
the agent should adapt, not 1:1-clone (the URL may sit outside the
project's design system). We surface the reference as two signals:

1. a **screenshot** captured by an injected :data:`Screenshotter`
   (Playwright, Puppeteer, an external service — whatever the caller
   wires in). The screenshot is then fed into the multimodal message
   exactly like a :mod:`backend.vision_to_ui` input.
2. a **textual extraction** from the HTML: title, meta, dominant
   colours, layout / component hints surfaced from inline styles and
   tag text. These feed the deterministic prompt segment so the model
   sees both "what the page looks like" (image) and "what the page
   calls itself" (text).

Playwright is *not* a dependency of the Python backend. This module
only defines the :data:`Screenshotter` callable signature and an
optional default that surfaces a ``screenshot_unavailable`` warning
when no capture agent is wired in. Production callers supply one (the
Edit complexity auto-router + the agent harness both have the
browser wiring available).

Graceful fallback contract
--------------------------

Every entry point returns a well-formed result even on partial
failure; callers inspect ``warnings`` rather than handling exceptions:

* ``url_invalid`` → raised as :class:`ValueError` at normalisation
  time (this is a caller bug, not a pipeline failure).
* ``url_blocked_private`` → raised at normalisation when the host
  resolves into a reserved / private IP range (SSRF gate).
  :func:`normalize_url` exposes an ``allow_private`` escape hatch for
  integration tests against local servers.
* ``fetch_failed`` / ``fetch_timeout`` / ``fetch_http_error`` →
  warning; empty HTML; pipeline still calls the LLM with whatever
  survived (URL + optional screenshot).
* ``screenshot_unavailable`` → warning; multimodal message falls back
  to text-only.
* ``llm_unavailable`` → warning; empty TSX.
* ``tsx_missing`` → warning; raw response kept as ``tsx_code`` for
  human inspection.
* ``html_truncated`` → warning when the HTML exceeded
  :data:`MAX_HTML_BYTES`; the prompt still sees a deterministic
  truncation.

Contract (pinned by ``backend/tests/test_url_to_reference.py``)
---------------------------------------------------------------

* :data:`URL_REF_SCHEMA_VERSION` bumps when the
  :meth:`URLReference.to_dict` /
  :meth:`URLReferenceResult.to_dict` shape changes.
* :class:`URLReference` / :class:`URLExtraction` /
  :class:`URLReferenceResult` are frozen, validated, and
  JSON-serialisable.
* :func:`normalize_url` rejects non-http(s) schemes, whitespace,
  overlong URLs, and — unless ``allow_private=True`` — URLs pointing
  at private / loopback hosts.
* :func:`extract_from_html` is pure: same HTML bytes → same
  extraction tuples (sorted + deduped).
* :func:`build_url_generation_prompt` is pure: same inputs →
  byte-identical prompt across calls (prompt-cache friendly).
* :func:`generate_ui_from_url` **never** raises on network / LLM
  failure; failures surface as ``warnings`` entries.
"""

from __future__ import annotations

import html as _html_lib
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse, urlunparse

from backend.component_consistency_linter import (
    LINTER_SCHEMA_VERSION,
    LintReport,
    auto_fix_code,
    lint_code,
)
from backend.design_token_loader import (
    DesignTokens,
    load_design_tokens,
    render_agent_context_block as render_design_tokens_block,
)
from backend.ui_component_registry import (
    render_agent_context_block as render_registry_block,
)
from backend.vision_to_ui import (
    MAX_IMAGE_BYTES,
    SUPPORTED_MIME_TYPES,
    VisionImage,
    build_multimodal_message as _build_vision_multimodal_message,
    extract_tsx_from_response,
    validate_image,
)

logger = logging.getLogger(__name__)


__all__ = [
    "URL_REF_SCHEMA_VERSION",
    "DEFAULT_URL_REF_MODEL",
    "DEFAULT_URL_REF_PROVIDER",
    "DEFAULT_USER_AGENT",
    "DEFAULT_FETCH_TIMEOUT",
    "MAX_HTML_BYTES",
    "MAX_URL_LENGTH",
    "HTML_PROMPT_CAP",
    "SUPPORTED_URL_SCHEMES",
    "FetchResponse",
    "ScreenshotResult",
    "URLReference",
    "URLExtraction",
    "URLReferenceResult",
    "Screenshotter",
    "URLFetcher",
    "ChatInvoker",
    "normalize_url",
    "fetch_url",
    "capture_screenshot",
    "extract_from_html",
    "build_url_generation_prompt",
    "build_multimodal_message",
    "generate_ui_from_url",
    "run_url_to_reference",
]


# Bump when the shape of URLReference / URLExtraction /
# URLReferenceResult payloads changes — callers cache them keyed on it.
URL_REF_SCHEMA_VERSION = "1.0.0"

#: Default multimodal model for URL → reference. Opus 4.7 because the
#: rebuild-as-shadcn step is a reasoning task (map observed colours to
#: semantic design tokens, flatten absolute layout into flex/grid).
DEFAULT_URL_REF_MODEL = "claude-opus-4-7"
DEFAULT_URL_REF_PROVIDER = "anthropic"

#: Canonical User-Agent. Sites that respect robots will see this as a
#: polite non-browser UA; callers can override per-request.
DEFAULT_USER_AGENT = (
    "OmniSight-URLToReference/1.0 (+https://omnisight.dev; "
    "reference-only UI synthesis)"
)

#: Fetcher timeout (seconds) — matches :mod:`backend.vision_to_ui`
#: latency budgets.
DEFAULT_FETCH_TIMEOUT = 20.0

#: Hard cap on downloaded HTML bytes. Pages past this are marked
#: ``html_truncated`` in warnings — the prompt still sees a
#: deterministic prefix.
MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MiB

#: Hard cap on URL length (defence-in-depth — browsers start to
#: misbehave well before this but we want a refusal rather than a
#: downstream surprise).
MAX_URL_LENGTH = 2048

#: Cap on the HTML bytes interpolated into the LLM prompt. Figma's
#: 8 KB cap is tight for full HTML — 16 KB is the sweet spot.
HTML_PROMPT_CAP = 16_000

#: Only http(s) URLs are supported. Anything else (file://, ftp://,
#: javascript: …) is rejected at :func:`normalize_url`.
SUPPORTED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class FetchResponse:
    """What a :data:`URLFetcher` returns on success.

    Fetchers that fail should raise rather than return a 5xx; the
    pipeline inspects :attr:`status_code` only for 2xx / 3xx / 4xx
    classification and will still pass 4xx HTML to the LLM.
    """

    url: str
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes = b""
    final_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, (bytes, bytearray)):
            raise TypeError("FetchResponse.content must be bytes")
        if not isinstance(self.status_code, int):
            raise TypeError("FetchResponse.status_code must be int")
        object.__setattr__(
            self,
            "headers",
            MappingProxyType({str(k).lower(): str(v) for k, v in dict(self.headers).items()}),
        )


@dataclass(frozen=True)
class URLReference:
    """A fetched + screenshotted reference URL.

    Fields:
        url: caller-normalised URL.
        final_url: final URL after redirects (``None`` if unknown).
        status_code: HTTP status (0 when fetch failed).
        content_type: normalised content-type (lowercase, no params).
        html: decoded HTML text (may be ``""`` on fetch failure or on
            non-HTML resource).
        html_bytes: size of the raw HTML payload (pre-truncation).
        title: extracted ``<title>`` text (may be ``""``).
        description: ``<meta name="description">`` content.
        canonical_url: ``<link rel="canonical">`` value if present.
        theme_color: ``<meta name="theme-color">`` value.
        screenshot: optional :class:`VisionImage` of the page.
        fetched_at: epoch seconds when we fetched (for cache keys).
        warnings: pipeline warnings (fetch_failed, html_truncated, …).
        meta: arbitrary JSON-safe extra metadata (headers snapshot).
    """

    url: str
    final_url: str | None = None
    status_code: int = 0
    content_type: str = ""
    html: str = ""
    html_bytes: int = 0
    title: str = ""
    description: str = ""
    canonical_url: str = ""
    theme_color: str = ""
    screenshot: VisionImage | None = None
    fetched_at: float | None = None
    warnings: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("URLReference.url must be non-empty")
        if self.screenshot is not None and not isinstance(
            self.screenshot, VisionImage
        ):
            raise TypeError("URLReference.screenshot must be a VisionImage")
        if self.html_bytes < 0:
            raise ValueError("URLReference.html_bytes must be non-negative")
        object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))

    @property
    def has_screenshot(self) -> bool:
        return self.screenshot is not None

    @property
    def is_ok(self) -> bool:
        """True if the fetch yielded any usable payload (HTML or image)."""
        return bool(self.html.strip()) or self.has_screenshot

    def to_dict(self) -> dict:
        return {
            "schema_version": URL_REF_SCHEMA_VERSION,
            "url": self.url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "html_bytes": self.html_bytes,
            "html_preview": self.html[:HTML_PROMPT_CAP],
            "title": self.title,
            "description": self.description,
            "canonical_url": self.canonical_url,
            "theme_color": self.theme_color,
            "has_screenshot": self.has_screenshot,
            "screenshot_mime": (
                self.screenshot.mime_type if self.screenshot else None
            ),
            "screenshot_bytes": (
                self.screenshot.size_bytes if self.screenshot else 0
            ),
            "fetched_at": self.fetched_at,
            "warnings": list(self.warnings),
            "meta": dict(self.meta),
        }


@dataclass(frozen=True)
class URLExtraction:
    """Structured hints pulled out of :attr:`URLReference.html`.

    All tuples are sorted + deduped for byte-stable prompt rendering.
    ``parse_succeeded`` is ``False`` only on total failure (e.g. empty
    HTML) — the pipeline still returns a well-formed extraction so the
    downstream prompt construction cannot raise.
    """

    color_values: tuple[str, ...] = ()
    font_values: tuple[str, ...] = ()
    heading_texts: tuple[str, ...] = ()
    button_labels: tuple[str, ...] = ()
    nav_labels: tuple[str, ...] = ()
    detected_components: tuple[str, ...] = ()
    link_hosts: tuple[str, ...] = ()
    layout_hints: tuple[str, ...] = ()
    meta_keywords: tuple[str, ...] = ()
    stylesheet_urls: tuple[str, ...] = ()
    parse_succeeded: bool = True
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "schema_version": URL_REF_SCHEMA_VERSION,
            "color_values": list(self.color_values),
            "font_values": list(self.font_values),
            "heading_texts": list(self.heading_texts),
            "button_labels": list(self.button_labels),
            "nav_labels": list(self.nav_labels),
            "detected_components": list(self.detected_components),
            "link_hosts": list(self.link_hosts),
            "layout_hints": list(self.layout_hints),
            "meta_keywords": list(self.meta_keywords),
            "stylesheet_urls": list(self.stylesheet_urls),
            "parse_succeeded": self.parse_succeeded,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class URLReferenceResult:
    """End-to-end output of :func:`generate_ui_from_url`."""

    reference: URLReference
    extraction: URLExtraction
    tsx_code: str = ""
    lint_report: LintReport = field(default_factory=LintReport)
    pre_fix_lint_report: LintReport | None = None
    auto_fix_applied: bool = False
    warnings: tuple[str, ...] = ()
    model: str | None = None
    provider: str | None = None

    @property
    def is_clean(self) -> bool:
        return self.lint_report.is_clean and bool(self.tsx_code.strip())

    def to_dict(self) -> dict:
        return {
            "schema_version": URL_REF_SCHEMA_VERSION,
            "linter_schema_version": LINTER_SCHEMA_VERSION,
            "reference": self.reference.to_dict(),
            "extraction": self.extraction.to_dict(),
            "tsx_code": self.tsx_code,
            "lint_report": self.lint_report.to_dict(),
            "pre_fix_lint_report": (
                self.pre_fix_lint_report.to_dict()
                if self.pre_fix_lint_report is not None
                else None
            ),
            "auto_fix_applied": self.auto_fix_applied,
            "warnings": list(self.warnings),
            "model": self.model,
            "provider": self.provider,
            "is_clean": self.is_clean,
        }


# ── Injectable callable signatures ───────────────────────────────────


URLFetcher = Callable[[str], FetchResponse]
"""Fetcher signature — synchronous HTTP GET of the URL.

Tests wire in a fake; production wires a :mod:`httpx` wrapper.  The
fetcher MUST raise on network errors rather than returning a bogus
response — :func:`fetch_url` converts exceptions into warnings.
"""


@dataclass(frozen=True)
class ScreenshotResult:
    """What a :data:`Screenshotter` returns when it succeeds."""

    data: bytes
    mime_type: str = "image/png"
    viewport: str | None = None


Screenshotter = Callable[[str], "ScreenshotResult | None"]
"""Screenshot capture — synchronous.

Tests wire in a fake; production wires a Playwright / Puppeteer
adapter. A screenshotter MAY return ``None`` to signal "capture
impossible for this URL" (pipeline adds ``screenshot_unavailable``
warning and falls back to text-only). Exceptions are also caught
and downgraded to the same warning.
"""


ChatInvoker = Callable[[list], str]
"""Chat invocation signature — mirrors sibling modules."""


# ── URL normalisation ────────────────────────────────────────────────


_URL_PRIVATE_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "broadcasthost",
    "ip6-localhost",
    "ip6-loopback",
})


def _host_is_private(host: str) -> bool:
    """True if ``host`` looks like a loopback / private / link-local name.

    Called by :func:`normalize_url` when ``allow_private`` is False.
    Accepts IP literals (IPv4 / IPv6) and known reserved hostnames.
    DNS resolution is attempted as a defence-in-depth — a miss falls
    back to name-based checks only (we do not block on resolution
    failure because CI containers frequently lack DNS).
    """
    lower = host.lower().strip("[]")
    if not lower:
        return True
    if lower in _URL_PRIVATE_HOSTNAMES:
        return True
    # Direct IP literal check.
    try:
        ip = ipaddress.ip_address(lower)
    except ValueError:
        ip = None
    if ip is not None:
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    # DNS lookup — may fail in sandboxed environments; name-check fallthrough.
    try:
        addrinfo = socket.getaddrinfo(lower, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for family, _stype, _proto, _canon, sockaddr in addrinfo:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def normalize_url(raw: str, *, allow_private: bool = False) -> str:
    """Return the canonical URL form, raise :class:`ValueError` otherwise.

    The canonical form:

    * scheme lowercased and restricted to :data:`SUPPORTED_URL_SCHEMES`;
    * hostname lowercased;
    * fragment removed (fragments don't affect fetched bytes and they
      explode the cache key);
    * whitespace stripped;
    * max length enforced via :data:`MAX_URL_LENGTH`.

    ``allow_private`` opens a loopback escape hatch for integration
    tests (``http://localhost:8000``).  Callers invoking this from
    an agent harness should keep the default (``False``).
    """
    if raw is None:
        raise ValueError("url must not be None")
    stripped = str(raw).strip()
    if not stripped:
        raise ValueError("url must be non-empty")
    if any(ch.isspace() for ch in stripped):
        raise ValueError("url must not contain whitespace")
    if len(stripped) > MAX_URL_LENGTH:
        raise ValueError(
            f"url exceeds {MAX_URL_LENGTH} characters — refuse to fetch"
        )

    parts = urlparse(stripped)
    scheme = (parts.scheme or "").lower()
    if scheme not in SUPPORTED_URL_SCHEMES:
        raise ValueError(
            f"Unsupported URL scheme {parts.scheme!r}; "
            f"must be one of {sorted(SUPPORTED_URL_SCHEMES)}"
        )
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("url must include a host")
    if not allow_private and _host_is_private(host):
        raise ValueError(
            f"url {stripped!r} points at a private / loopback host "
            f"({host!r}) — refusing to fetch (SSRF gate). "
            "Pass allow_private=True to override."
        )

    # Reconstruct: lowercase scheme + netloc (preserve port + userinfo
    # if present — but we warn on userinfo via the caller, not here).
    netloc = parts.netloc.lower()
    # Drop fragment, keep path/params/query verbatim.
    return urlunparse((
        scheme,
        netloc,
        parts.path or "/",
        parts.params,
        parts.query,
        "",
    ))


# ── Fetcher ──────────────────────────────────────────────────────────


_CONTENT_TYPE_HTML_RE = re.compile(
    r"^text/(?:html|xhtml\+xml)|application/(?:xhtml\+xml|xml)",
    re.IGNORECASE,
)


def _normalise_content_type(ct: str) -> str:
    if not ct:
        return ""
    # Strip parameters ("; charset=utf-8") and lowercase.
    return ct.split(";", 1)[0].strip().lower()


def _decode_html(content: bytes, content_type: str) -> str:
    """Best-effort decode: honour charset= hint, fall back to utf-8."""
    charset = "utf-8"
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if m:
        charset = m.group(1).strip().lower()
    try:
        return content.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")


def _default_fetcher(
    *,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    max_bytes: int = MAX_HTML_BYTES,
) -> URLFetcher:
    """Return a fetcher that uses :mod:`httpx` if available, else :mod:`urllib`.

    The default fetcher MUST NOT be used from tests — tests inject a
    fake via the ``fetcher=`` parameter. Production callers should
    usually inject their own fetcher too (one with session cookies,
    proxy, retry, etc.).
    """

    def _fetch(url: str) -> FetchResponse:
        try:
            import httpx  # type: ignore
        except Exception:
            httpx = None  # type: ignore

        if httpx is not None:
            with httpx.Client(
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.8"},
            ) as client:
                response = client.get(url)
                data = response.content
                if len(data) > max_bytes:
                    data = data[:max_bytes]
                return FetchResponse(
                    url=url,
                    status_code=int(response.status_code),
                    headers=dict(response.headers),
                    content=bytes(data),
                    final_url=str(response.url),
                )

        # Fallback — urllib. Synchronous, but avoids the httpx dep in
        # minimal CI containers.
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    data = data[:max_bytes]
                headers_dict = {k.lower(): v for k, v in resp.getheaders()}
                return FetchResponse(
                    url=url,
                    status_code=int(resp.status),
                    headers=headers_dict,
                    content=bytes(data),
                    final_url=resp.geturl(),
                )
        except urllib.error.HTTPError as exc:
            return FetchResponse(
                url=url,
                status_code=int(exc.code),
                headers={k.lower(): v for k, v in dict(exc.headers or {}).items()},
                content=exc.read() if exc.fp is not None else b"",
                final_url=url,
            )

    return _fetch


def fetch_url(
    url: str,
    *,
    fetcher: URLFetcher | None = None,
    allow_private: bool = False,
    now: Callable[[], float] = time.time,
) -> URLReference:
    """Fetch ``url`` via the injected or default fetcher.

    Returns a :class:`URLReference` in **all** cases — failures
    surface as ``warnings`` entries, never as exceptions (except when
    the URL itself is malformed / blocked, which is a caller bug).
    """
    normalised = normalize_url(url, allow_private=allow_private)
    resolved_fetcher = fetcher or _default_fetcher()
    started = float(now())
    warnings: list[str] = []

    try:
        response = resolved_fetcher(normalised)
    except Exception as exc:  # defensive — we never crash the pipeline
        logger.warning("url_to_reference fetch failed for %s: %s", normalised, exc)
        warnings.append("fetch_failed")
        return URLReference(
            url=normalised,
            final_url=None,
            status_code=0,
            content_type="",
            html="",
            html_bytes=0,
            fetched_at=started,
            warnings=tuple(warnings),
        )

    if not isinstance(response, FetchResponse):
        warnings.append("fetch_unexpected_shape")
        return URLReference(
            url=normalised,
            final_url=None,
            status_code=0,
            content_type="",
            html="",
            html_bytes=0,
            fetched_at=started,
            warnings=tuple(warnings),
        )

    status = response.status_code
    if status >= 500:
        warnings.append("fetch_server_error")
    elif status >= 400:
        warnings.append("fetch_http_error")
    elif status == 0:
        warnings.append("fetch_failed")

    content_type = _normalise_content_type(
        response.headers.get("content-type", "")
    )
    if content_type and not _CONTENT_TYPE_HTML_RE.match(content_type):
        warnings.append("non_html_content_type")

    raw = bytes(response.content or b"")
    if len(raw) > MAX_HTML_BYTES:
        raw = raw[:MAX_HTML_BYTES]
        warnings.append("html_truncated")

    html = _decode_html(raw, response.headers.get("content-type", "")) if raw else ""

    headers_snapshot = {
        k: v
        for k, v in response.headers.items()
        if k in {"content-type", "server", "x-powered-by", "last-modified"}
    }

    title = _extract_title(html) if html else ""
    description = _extract_meta_content(html, ("description", "og:description")) if html else ""
    theme_color = _extract_meta_content(html, ("theme-color",)) if html else ""
    canonical = _extract_canonical(html) if html else ""

    return URLReference(
        url=normalised,
        final_url=response.final_url or normalised,
        status_code=status,
        content_type=content_type,
        html=html,
        html_bytes=len(raw),
        title=title,
        description=description,
        canonical_url=canonical,
        theme_color=theme_color,
        screenshot=None,
        fetched_at=started,
        warnings=tuple(warnings),
        meta=headers_snapshot,
    )


# ── Screenshot capture ───────────────────────────────────────────────


def capture_screenshot(
    url: str,
    *,
    screenshotter: Screenshotter | None,
    allow_private: bool = False,
) -> tuple[VisionImage | None, tuple[str, ...]]:
    """Invoke the screenshotter; return ``(image, warnings)``.

    ``screenshotter=None`` is the *default* — there is no built-in
    Playwright adapter in this module. Callers who need a screenshot
    must wire one in. Tests inject a fake.
    """
    normalised = normalize_url(url, allow_private=allow_private)
    if screenshotter is None:
        return None, ("screenshot_unavailable",)

    try:
        result = screenshotter(normalised)
    except Exception as exc:  # defensive — never crash the pipeline
        logger.warning(
            "url_to_reference screenshot failed for %s: %s", normalised, exc
        )
        return None, ("screenshot_unavailable",)

    if result is None:
        return None, ("screenshot_unavailable",)

    if not isinstance(result, ScreenshotResult):
        return None, ("screenshot_unexpected_shape",)

    mime = (result.mime_type or "image/png").strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in SUPPORTED_MIME_TYPES:
        return None, ("screenshot_unsupported_mime",)

    data = bytes(result.data or b"")
    if not data:
        return None, ("screenshot_unavailable",)
    if len(data) > MAX_IMAGE_BYTES:
        return None, ("screenshot_too_large",)

    try:
        return validate_image(data, mime, source=normalised), ()
    except (TypeError, ValueError) as exc:
        logger.debug("screenshot validate_image rejected: %s", exc)
        return None, ("screenshot_invalid",)


# ── HTML extraction (regex-based, best-effort) ───────────────────────


_TAG_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_META_RE = re.compile(
    r"<meta\b([^>]*)>", re.IGNORECASE | re.DOTALL,
)
_ATTR_RE = re.compile(
    r"""(?P<key>[a-zA-Z_:][\w:.\-]*)\s*=\s*(?P<quote>["'])(?P<val>.*?)(?P=quote)""",
    re.DOTALL,
)
_TAG_LINK_RE = re.compile(r"<link\b([^>]*)>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")

_INLINE_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")
_INLINE_RGB_RE = re.compile(r"rgba?\(\s*[^)]*\)", re.IGNORECASE)
_INLINE_HSL_RE = re.compile(r"hsla?\(\s*[^)]*\)", re.IGNORECASE)
_INLINE_OKLCH_RE = re.compile(r"oklch\(\s*[^)]*\)", re.IGNORECASE)
_INLINE_OKLAB_RE = re.compile(r"okl?ab\(\s*[^)]*\)", re.IGNORECASE)
_INLINE_FONT_FAMILY_RE = re.compile(
    r"font-family\s*:\s*([^;}\n]+)", re.IGNORECASE
)
_TAILWIND_HEX_ARB_RE = re.compile(
    r"\[(?:bg|text|border|ring|from|to|via|fill|stroke)-?[a-z]*"
    r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\]"
)

# Headings / buttons / nav
_HEADING_RE = re.compile(
    r"<(h[1-3])\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL
)
_BUTTON_RE = re.compile(
    r"<button\b[^>]*>(.*?)</button>", re.IGNORECASE | re.DOTALL
)
_SHADCN_BUTTON_RE = re.compile(
    r"<Button\b[^>]*>(.*?)</Button>", re.DOTALL
)
_NAV_RE = re.compile(
    r"<nav\b[^>]*>(.*?)</nav>", re.IGNORECASE | re.DOTALL
)
_NAV_LINK_RE = re.compile(
    r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL
)

_STYLESHEET_LINK_RE = re.compile(
    r"""<link\b(?=[^>]*rel=["']stylesheet["'])[^>]*>""",
    re.IGNORECASE | re.DOTALL,
)
_HREF_RE = re.compile(
    r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)


def _parse_meta_attrs(raw_attrs: str) -> dict[str, str]:
    return {
        m.group("key").lower(): m.group("val")
        for m in _ATTR_RE.finditer(raw_attrs)
    }


def _extract_title(html: str) -> str:
    if not html:
        return ""
    m = _TAG_TITLE_RE.search(html)
    if not m:
        return ""
    body = _html_lib.unescape(_TAG_STRIP_RE.sub("", m.group(1))).strip()
    return re.sub(r"\s+", " ", body)[:240]


def _extract_meta_content(html: str, names: Sequence[str]) -> str:
    if not html:
        return ""
    wanted = {n.lower() for n in names}
    for m in _TAG_META_RE.finditer(html):
        attrs = _parse_meta_attrs(m.group(1))
        name = (attrs.get("name") or attrs.get("property") or "").lower()
        if name in wanted:
            return _html_lib.unescape(attrs.get("content", "")).strip()[:400]
    return ""


def _extract_canonical(html: str) -> str:
    if not html:
        return ""
    for m in _TAG_LINK_RE.finditer(html):
        attrs = _parse_meta_attrs(m.group(1))
        if attrs.get("rel", "").lower() == "canonical":
            return _html_lib.unescape(attrs.get("href", "")).strip()[:400]
    return ""


def _text_content(fragment: str, limit: int = 120) -> str:
    plain = _html_lib.unescape(_TAG_STRIP_RE.sub(" ", fragment))
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:limit]


def _dedupe_sorted(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        if raw is None:
            continue
        v = str(raw).strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return tuple(sorted(out))


def _dedupe_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = str(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def extract_from_html(
    html: str,
    *,
    base_url: str | None = None,
) -> URLExtraction:
    """Pull structural / colour / layout hints from the HTML body.

    This is a **best-effort** regex pass — we do not build a DOM.
    The goal is to surface hints that help the LLM decide which
    shadcn primitives to use, not to reconstruct the site.
    """
    if not html or not html.strip():
        return URLExtraction(parse_succeeded=False, warnings=("empty_html",))

    colors = _dedupe_sorted(
        [m.group(0) for m in _INLINE_HEX_RE.finditer(html)]
        + [m.group(0).lower() for m in _INLINE_RGB_RE.finditer(html)]
        + [m.group(0).lower() for m in _INLINE_HSL_RE.finditer(html)]
        + [m.group(0).lower() for m in _INLINE_OKLCH_RE.finditer(html)]
        + [m.group(0).lower() for m in _INLINE_OKLAB_RE.finditer(html)]
    )

    fonts = _dedupe_sorted(
        _clean_font_family(m.group(1))
        for m in _INLINE_FONT_FAMILY_RE.finditer(html)
    )

    headings = tuple(
        _dedupe_preserve(
            _text_content(m.group(2))
            for m in _HEADING_RE.finditer(html)
            if _text_content(m.group(2))
        )
    )

    raw_buttons = [
        _text_content(m.group(1)) for m in _BUTTON_RE.finditer(html)
    ] + [
        _text_content(m.group(1)) for m in _SHADCN_BUTTON_RE.finditer(html)
    ]
    buttons = tuple(
        _dedupe_preserve(lbl for lbl in raw_buttons if lbl)
    )

    nav_labels: list[str] = []
    for nav_match in _NAV_RE.finditer(html):
        nav_body = nav_match.group(1)
        for link_match in _NAV_LINK_RE.finditer(nav_body):
            text = _text_content(link_match.group(1))
            if text:
                nav_labels.append(text)
    nav_labels_t = tuple(_dedupe_preserve(nav_labels))

    detected_components = _detect_components(html)

    link_hosts = _collect_link_hosts(html, base_url=base_url)

    layout_hints = _detect_layout_hints(html)

    meta_keywords: list[str] = []
    keywords = _extract_meta_content(html, ("keywords",))
    if keywords:
        for part in keywords.split(","):
            part = part.strip()
            if part:
                meta_keywords.append(part)

    stylesheet_urls = _collect_stylesheet_urls(html)

    return URLExtraction(
        color_values=colors,
        font_values=fonts,
        heading_texts=headings,
        button_labels=buttons,
        nav_labels=nav_labels_t,
        detected_components=detected_components,
        link_hosts=link_hosts,
        layout_hints=layout_hints,
        meta_keywords=tuple(_dedupe_preserve(meta_keywords)),
        stylesheet_urls=stylesheet_urls,
        parse_succeeded=True,
    )


def _clean_font_family(raw: str) -> str:
    cleaned = raw.strip().strip(";").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:160]


_COMPONENT_DETECTORS: tuple[tuple[str, re.Pattern], ...] = (
    ("form", re.compile(r"<form\b", re.IGNORECASE)),
    ("input", re.compile(r"<input\b", re.IGNORECASE)),
    ("textarea", re.compile(r"<textarea\b", re.IGNORECASE)),
    ("select", re.compile(r"<select\b", re.IGNORECASE)),
    ("table", re.compile(r"<table\b", re.IGNORECASE)),
    ("nav", re.compile(r"<nav\b", re.IGNORECASE)),
    ("header", re.compile(r"<header\b", re.IGNORECASE)),
    ("footer", re.compile(r"<footer\b", re.IGNORECASE)),
    ("aside", re.compile(r"<aside\b", re.IGNORECASE)),
    ("main", re.compile(r"<main\b", re.IGNORECASE)),
    ("button", re.compile(r"<button\b|<Button\b")),
    ("section", re.compile(r"<section\b", re.IGNORECASE)),
    ("article", re.compile(r"<article\b", re.IGNORECASE)),
    ("dialog", re.compile(r"<dialog\b", re.IGNORECASE)),
    (
        "card",
        re.compile(
            r"""class=["'][^"']*\bcard\b""",
            re.IGNORECASE,
        ),
    ),
    (
        "hero",
        re.compile(
            r"""class=["'][^"']*\bhero\b""",
            re.IGNORECASE,
        ),
    ),
    (
        "tabs",
        re.compile(
            r"""(?:role=["']tablist["']|class=["'][^"']*\btabs?\b)""",
            re.IGNORECASE,
        ),
    ),
    (
        "modal",
        re.compile(
            r"""(?:role=["']dialog["']|class=["'][^"']*\bmodal\b)""",
            re.IGNORECASE,
        ),
    ),
)


def _detect_components(html: str) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for name, regex in _COMPONENT_DETECTORS:
        if regex.search(html) and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(sorted(out))


_A_HREF_RE = re.compile(
    r"""<a\b[^>]*href\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE | re.DOTALL,
)


def _collect_link_hosts(html: str, *, base_url: str | None) -> tuple[str, ...]:
    from urllib.parse import urljoin

    hosts: set[str] = set()
    for m in _A_HREF_RE.finditer(html):
        href = m.group(1).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if base_url and not urlparse(href).netloc:
            href = urljoin(base_url, href)
        try:
            host = urlparse(href).hostname
        except ValueError:
            continue
        if host:
            hosts.add(host.lower())
    return tuple(sorted(hosts))


_LAYOUT_HINT_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "grid",
        re.compile(r"""(?:display\s*:\s*grid|class=["'][^"']*\bgrid\b)""",
                   re.IGNORECASE),
    ),
    (
        "flex",
        re.compile(r"""(?:display\s*:\s*flex|class=["'][^"']*\bflex\b)""",
                   re.IGNORECASE),
    ),
    (
        "sidebar",
        re.compile(r"""class=["'][^"']*\bsidebar\b""", re.IGNORECASE),
    ),
    (
        "container",
        re.compile(r"""class=["'][^"']*\bcontainer\b""", re.IGNORECASE),
    ),
    (
        "two-column",
        re.compile(r"""grid-template-columns|columns=["']2["']""",
                   re.IGNORECASE),
    ),
    (
        "dark-theme",
        re.compile(r"""class=["'][^"']*\bdark\b|color-scheme\s*:\s*dark""",
                   re.IGNORECASE),
    ),
)


def _detect_layout_hints(html: str) -> tuple[str, ...]:
    hits: list[str] = []
    seen: set[str] = set()
    for name, regex in _LAYOUT_HINT_PATTERNS:
        if regex.search(html) and name not in seen:
            seen.add(name)
            hits.append(name)
    return tuple(sorted(hits))


def _collect_stylesheet_urls(html: str) -> tuple[str, ...]:
    urls: list[str] = []
    seen: set[str] = set()
    for link_tag in _STYLESHEET_LINK_RE.finditer(html):
        href_match = _HREF_RE.search(link_tag.group(0))
        if href_match:
            href = href_match.group(1).strip()
            if href and href not in seen:
                seen.add(href)
                urls.append(href)
    return tuple(sorted(urls))


# ── Prompt construction (deterministic) ──────────────────────────────


_URL_PROMPT_HEADER = (
    "# UI generation — URL reference → shadcn/ui + Tailwind\n"
    "You are the OmniSight UI Designer. The operator asked for a\n"
    "page that looks *like* the URL below. Treat it as a REFERENCE —\n"
    "not a 1:1 clone. Rebuild the same intent using the project's\n"
    "installed shadcn/ui primitives and design tokens. You will be\n"
    "linted by backend.component_consistency_linter — a clean lint\n"
    "pass is the acceptance gate."
)

_URL_PROMPT_RULES = (
    "## Generation rules (MUST follow)\n"
    "1. Output a single self-contained React TSX component. Imports\n"
    "   are limited to the shadcn primitives listed above plus `cn`\n"
    "   from `@/lib/utils`.\n"
    "2. The reference URL is *inspiration*. Match layout intent and\n"
    "   information hierarchy, but map every observed colour /\n"
    "   spacing / radius onto the project's design tokens. NEVER\n"
    "   inline a hex colour from the reference; never pin a\n"
    "   Tailwind palette class (e.g. `bg-slate-900`). If no\n"
    "   matching token exists, pick the closest semantic one and\n"
    "   leave a TODO comment — do NOT invent a hex.\n"
    "3. Replace raw <button> / <input> / <textarea> / <select> /\n"
    "   <dialog> / <progress> / <div onClick> with the shadcn\n"
    "   primitive equivalent. Drop any absolute / fixed widths\n"
    "   carried over from the reference.\n"
    "4. Responsive: mobile-first base + sm/md/lg/xl/2xl.\n"
    "5. Respect WAI-ARIA: icon-only buttons get aria-label; form\n"
    "   inputs get <Label htmlFor> or wrap in <Field>.\n"
    "6. This project is dark-only (html { color-scheme: dark }). Do\n"
    "   NOT emit `dark:` prefixes and do NOT write light fallbacks.\n"
    "7. Do NOT copy copy/paste text verbatim from the reference —\n"
    "   rewrite headings / labels to fit the caller brief. If the\n"
    "   reference is a commercial page, never reuse brand names.\n"
    "8. Output MUST be a single fenced code block:\n"
    "   ```tsx\n"
    "   /* code */\n"
    "   ```\n"
    "   No prose before or after."
)


def _render_reference_block(reference: URLReference) -> str:
    lines: list[str] = ["## URL reference"]
    lines.append(f"- url: {reference.url}")
    if reference.final_url and reference.final_url != reference.url:
        lines.append(f"- final_url: {reference.final_url}")
    lines.append(
        f"- status: {reference.status_code or 'n/a'}"
    )
    lines.append(
        f"- content_type: {reference.content_type or '(unknown)'}"
    )
    lines.append(f"- title: {reference.title or '(no title)'}")
    if reference.description:
        lines.append(f"- description: {reference.description}")
    if reference.theme_color:
        lines.append(f"- theme-color: {reference.theme_color}")
    if reference.canonical_url:
        lines.append(f"- canonical: {reference.canonical_url}")
    lines.append(
        f"- screenshot: {'attached (multimodal)' if reference.has_screenshot else 'not attached'}"
    )
    if reference.warnings:
        lines.append("- warnings: " + ", ".join(reference.warnings))
    return "\n".join(lines)


def _render_extraction_block(extraction: URLExtraction) -> str:
    lines: list[str] = ["## URL extraction"]

    def _list(header: str, items: Sequence[str], limit: int = 24) -> None:
        if not items:
            lines.append(f"{header}: (none detected)")
            return
        shown = items[:limit]
        more = "" if len(items) <= limit else f" (+{len(items) - limit} more)"
        lines.append(f"{header}:")
        for item in shown:
            lines.append(f"  - {item}")
        if more:
            lines.append(f"  … {more.strip()}")

    _list("Colours observed (inline)", extraction.color_values)
    _list("Font families observed", extraction.font_values)
    _list("Heading text", extraction.heading_texts)
    _list("Button labels", extraction.button_labels)
    _list("Nav labels", extraction.nav_labels)
    _list("Detected components", extraction.detected_components)
    _list("Layout hints", extraction.layout_hints)
    _list("Link hosts", extraction.link_hosts)
    _list("Meta keywords", extraction.meta_keywords)
    _list("External stylesheets", extraction.stylesheet_urls)
    if extraction.warnings:
        lines.append("Parse warnings: " + ", ".join(extraction.warnings))
    return "\n".join(lines)


def _render_html_snippet_block(html: str) -> str:
    cleaned = (html or "").strip()
    if not cleaned:
        return (
            "## HTML snippet\n"
            "(reference had no usable HTML body — rely on the screenshot"
            " and extraction above)"
        )
    if len(cleaned) > HTML_PROMPT_CAP:
        head = cleaned[:HTML_PROMPT_CAP]
        dropped = len(cleaned) - HTML_PROMPT_CAP
        cleaned = head + f"\n<!-- … (truncated {dropped} bytes) -->\n"
    return (
        "## HTML snippet\n"
        "(reference HTML — do not quote brand copy verbatim)\n"
        "```html\n"
        f"{cleaned}\n"
        "```"
    )


def build_url_generation_prompt(
    *,
    reference: URLReference,
    extraction: URLExtraction | None = None,
    project_root: Path | str | None,
    brief: str | None = None,
    tokens: DesignTokens | None = None,
) -> str:
    """Return a deterministic TSX-generation prompt.

    Same inputs → byte-identical prompt across calls. The sibling
    registry + tokens blocks are themselves deterministic.
    """
    if extraction is None:
        extraction = extract_from_html(reference.html, base_url=reference.url)

    registry_block = render_registry_block(project_root=project_root)
    if tokens is not None:
        tokens_block = tokens.to_agent_context()
    else:
        tokens_block = render_design_tokens_block(project_root=project_root)

    reference_block = _render_reference_block(reference)
    extraction_block = _render_extraction_block(extraction)
    html_block = _render_html_snippet_block(reference.html)

    if brief and brief.strip():
        brief_block = f"## Caller brief\n{brief.strip()}"
    else:
        brief_block = "## Caller brief\n(none)"

    sections = [
        _URL_PROMPT_HEADER,
        reference_block,
        extraction_block,
        html_block,
        registry_block,
        tokens_block,
        brief_block,
        _URL_PROMPT_RULES,
    ]
    return "\n\n".join(section.strip() for section in sections).strip() + "\n"


# ── Multimodal message assembly ──────────────────────────────────────


def build_multimodal_message(
    reference: URLReference,
    prompt: str,
) -> Any:
    """Return a LangChain ``HumanMessage`` for the given reference.

    If the reference carries a screenshot, the message uses the same
    ``[text, image]`` shape as
    :func:`backend.vision_to_ui.build_multimodal_message`. Otherwise
    a text-only ``HumanMessage`` is returned — the LLM can still
    reason about the reference HTML + extraction blocks.
    """
    if reference.has_screenshot:
        assert reference.screenshot is not None
        return _build_vision_multimodal_message(reference.screenshot, prompt)
    from backend.llm_adapter import HumanMessage
    return HumanMessage(content=prompt)


# ── Pipeline entry points ────────────────────────────────────────────


def _default_invoker(
    *,
    provider: str | None,
    model: str | None,
    llm: Any | None,
) -> ChatInvoker:
    """Return a chat invoker bound to the requested provider/model."""
    from backend.llm_adapter import invoke_chat

    def _invoke(messages: list) -> str:
        try:
            return invoke_chat(
                messages,
                provider=provider,
                model=model,
                llm=llm,
            )
        except Exception as exc:  # defensive — surface as warning
            logger.warning("url_to_reference chat invocation failed: %s", exc)
            return ""

    return _invoke


def generate_ui_from_url(
    url: str,
    *,
    reference: URLReference | None = None,
    extraction: URLExtraction | None = None,
    brief: str | None = None,
    project_root: Path | str | None = None,
    fetcher: URLFetcher | None = None,
    screenshotter: Screenshotter | None = None,
    provider: str | None = DEFAULT_URL_REF_PROVIDER,
    model: str | None = DEFAULT_URL_REF_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    auto_fix: bool = True,
    allow_private: bool = False,
) -> URLReferenceResult:
    """End-to-end: URL → fetch + screenshot → TSX → lint → (auto-fix).

    Callers may skip the fetch step by passing a pre-built
    ``reference`` — useful when the agent harness fetched in parallel
    with other work. Same for ``extraction``.

    Graceful fallback contract:
      * malformed / blocked URL → :class:`ValueError` (caller bug).
      * fetch failure → ``warnings`` carries ``fetch_failed`` and the
        pipeline continues with an empty reference (LLM can still
        work off the URL + screenshot).
      * LLM unavailable → ``warnings=("llm_unavailable",)`` + empty
        TSX; never a traceback.
      * TSX missing → ``warnings`` includes ``"tsx_missing"``;
        ``tsx_code`` carries the raw response.
    """
    warnings: list[str] = []

    if reference is None:
        reference = fetch_url(
            url,
            fetcher=fetcher,
            allow_private=allow_private,
        )

    # Attach screenshot if one isn't already carried on the reference.
    if not reference.has_screenshot:
        image, capture_warnings = capture_screenshot(
            reference.url,
            screenshotter=screenshotter,
            allow_private=allow_private,
        )
        for w in capture_warnings:
            if w not in warnings:
                warnings.append(w)
        if image is not None:
            reference = _attach_screenshot(reference, image)

    # Bubble reference-level warnings (fetch_failed, html_truncated, …).
    for w in reference.warnings:
        if w not in warnings:
            warnings.append(w)

    if extraction is None:
        extraction = extract_from_html(
            reference.html, base_url=reference.url,
        )
    for w in extraction.warnings:
        if w not in warnings:
            warnings.append(w)

    invoke = invoker or _default_invoker(
        provider=provider, model=model, llm=llm,
    )
    tokens = load_design_tokens(project_root) if project_root else None
    prompt = build_url_generation_prompt(
        reference=reference,
        extraction=extraction,
        project_root=project_root,
        brief=brief,
        tokens=tokens,
    )
    message = build_multimodal_message(reference, prompt)

    response_text = invoke([message])
    if not response_text:
        warnings.append("llm_unavailable")
        return URLReferenceResult(
            reference=reference,
            extraction=extraction,
            tsx_code="",
            lint_report=LintReport(),
            pre_fix_lint_report=None,
            auto_fix_applied=False,
            warnings=tuple(_dedupe_preserve(warnings)),
            model=model,
            provider=provider,
        )

    tsx = extract_tsx_from_response(response_text)
    if not tsx:
        warnings.append("tsx_missing")
        return URLReferenceResult(
            reference=reference,
            extraction=extraction,
            tsx_code=response_text,
            lint_report=LintReport(),
            pre_fix_lint_report=None,
            auto_fix_applied=False,
            warnings=tuple(_dedupe_preserve(warnings)),
            model=model,
            provider=provider,
        )

    initial_report = lint_code(tsx, source="url_to_reference.tsx")
    fixed_tsx = tsx
    applied = False
    final_report = initial_report

    if auto_fix and not initial_report.is_clean:
        fixed_tsx, _remaining = auto_fix_code(tsx)
        if fixed_tsx != tsx:
            applied = True
            final_report = lint_code(
                fixed_tsx, source="url_to_reference.tsx",
            )

    return URLReferenceResult(
        reference=reference,
        extraction=extraction,
        tsx_code=fixed_tsx,
        lint_report=final_report,
        pre_fix_lint_report=initial_report if applied else None,
        auto_fix_applied=applied,
        warnings=tuple(_dedupe_preserve(warnings)),
        model=model,
        provider=provider,
    )


def _attach_screenshot(
    reference: URLReference, image: VisionImage,
) -> URLReference:
    """Return a copy of ``reference`` with ``screenshot`` replaced.

    :class:`URLReference` is frozen — use this helper rather than
    mutating in place.
    """
    return URLReference(
        url=reference.url,
        final_url=reference.final_url,
        status_code=reference.status_code,
        content_type=reference.content_type,
        html=reference.html,
        html_bytes=reference.html_bytes,
        title=reference.title,
        description=reference.description,
        canonical_url=reference.canonical_url,
        theme_color=reference.theme_color,
        screenshot=image,
        fetched_at=reference.fetched_at,
        warnings=reference.warnings,
        meta=dict(reference.meta),
    )


def run_url_to_reference(
    *,
    url: str | None = None,
    reference: URLReference | None = None,
    extraction: URLExtraction | None = None,
    brief: str | None = None,
    project_root: Path | str | None = None,
    fetcher: URLFetcher | None = None,
    screenshotter: Screenshotter | None = None,
    provider: str | None = DEFAULT_URL_REF_PROVIDER,
    model: str | None = DEFAULT_URL_REF_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    auto_fix: bool = True,
    allow_private: bool = False,
) -> dict:
    """Agent-callable entry point — returns a JSON-safe dict.

    Exactly one of ``url`` / ``reference`` must be supplied.
    """
    if (url is None) == (reference is None):
        raise ValueError(
            "run_url_to_reference requires exactly one of url / reference"
        )
    if url is not None:
        url_for_pipeline = normalize_url(url, allow_private=allow_private)
    else:
        assert reference is not None
        url_for_pipeline = reference.url

    result = generate_ui_from_url(
        url_for_pipeline,
        reference=reference,
        extraction=extraction,
        brief=brief,
        project_root=project_root,
        fetcher=fetcher,
        screenshotter=screenshotter,
        provider=provider,
        model=model,
        llm=llm,
        invoker=invoker,
        auto_fix=auto_fix,
        allow_private=allow_private,
    )
    return result.to_dict()
