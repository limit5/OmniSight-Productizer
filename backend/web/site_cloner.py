"""W11.1 #XXX — URL → ``CloneSpec`` orchestrator.

Single entry point for the W11 *Website Cloning Capability* epic. Given
a public URL, it normalises + safety-validates the URL, hands the fetch
to a pluggable ``CloneSource`` backend (W11.2 will ship two: Firecrawl
SaaS and self-hosted Playwright), and returns a structured ``CloneSpec``
the downstream productizer pipeline (Next / Nuxt / Astro / Vue / Svelte
scaffolders) can consume to build a fresh, *transformed* clone.

This row (W11.1) deliberately keeps scope tight — it ships:

    * The public ``clone_site(url, *, source, ...)`` entry point.
    * The ``CloneSpec`` container with the W11.3-spec'd categories
      (title / meta / hero / nav / sections / footer / images / colors /
      fonts / spacing) — each defaults to an empty / ``None`` placeholder.
      W11.3 fills the population logic; W11.1 just guarantees the shape.
    * The ``CloneSource`` Protocol (W11.2 plugs Firecrawl + Playwright
      into this).
    * URL safety validation (scheme allowlist, userinfo reject, SSRF
      destination guard via ``ipaddress``).
    * A minimal ``build_clone_spec_from_capture`` that maps the raw HTML
      ``<title>`` / meta description into the spec — every other field
      remains the W11.3 row's responsibility.
    * Typed error hierarchy so the W11.12 audit row can categorise
      every failure mode without string-matching exception messages.

Defense-in-depth integration
----------------------------
W11.1 is the *orchestration* row. The 5-layer defenses from the epic
spec land in dedicated rows and call into this module:

    L1 (W11.4)  robots.txt / noai meta / ai.txt / CF ai-bot rule check
                — invoked *before* ``clone_site()`` by the calling
                router; this module exposes ``RawCapture.html`` so the
                L1 scanner can re-read meta tags after capture.
    L2 (W11.5)  LLM content classifier — invoked on the ``CloneSpec``
                returned by ``clone_site()``.
    L3 (W11.6)  Output transformation — operates on ``CloneSpec`` (text
                rewrite, image → placeholder). ``CloneSpec`` deliberately
                stores text + image URLs not bytes so this is enforceable.
    L4 (W11.7)  Traceability — consumes ``RawCapture.fetched_at`` +
                ``CloneSpec.source_url`` for the manifest.
    L5 (W11.8)  Rate limit + PEP HOLD — gate the *call* to
                ``clone_site()``; this module does not own quota state.

Pure helpers (URL normalisation, hostname extraction, ``is_public_destination``)
have no I/O so unit tests run in-process with no fixtures. The async
``clone_site()`` body is also pure given a ``CloneSource`` mock, which is
what the W11.11 reference-URL × snapshot tests will use.

Inspired by firecrawl/open-lovable (MIT). The full attribution +
license text lands in the W11.13 row.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

#: URL schemes the cloner will accept. Anything else (file://, ftp://,
#: javascript:, data:, gopher:, etc.) is rejected at the validation step
#: so backend implementations never see them.
SUPPORTED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Default capture timeout passed to a ``CloneSource`` when the caller
#: doesn't override. 30 s matches the upper bound Firecrawl's free tier
#: documents and is enough for a Playwright cold-start + render.
DEFAULT_TIMEOUT_S: float = 30.0

#: Hard ceiling on raw HTML payload size to refuse pre-parse. 5 MiB
#: covers every "real" landing page; pages above this are almost
#: certainly bot-traps / SPA bundle dumps that wouldn't yield a useful
#: ``CloneSpec`` anyway.
DEFAULT_MAX_HTML_BYTES: int = 5 * 1024 * 1024

#: Hostnames whose textual form alone is enough to refuse — even before
#: DNS resolution. The ipaddress check below covers the literal-IP cases;
#: this set covers the textual aliases for those literals.
_BLOCKED_HOSTNAME_LITERALS: frozenset[str] = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
})

#: Hostname suffixes we refuse outright. ``.local`` is the mDNS suffix
#: (every laptop on the LAN), ``.internal`` is GCP / common k8s service
#: convention, ``.lan`` / ``.home`` / ``.home.arpa`` are common router
#: defaults, and ``.onion`` is Tor (we only fetch clearnet).
_BLOCKED_HOSTNAME_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".localhost",
    ".internal",
    ".lan",
    ".home",
    ".home.arpa",
    ".onion",
)

#: The cloud-metadata IP that AWS / GCP / Azure / DO all expose at the
#: same address. Already covered by 169.254.0.0/16 link-local rejection
#: but recorded here as an explicit landmark — every SSRF write-up names
#: this address, and the constant doubles as documentation.
CLOUD_METADATA_IP: str = "169.254.169.254"

#: Hostname character lint. RFC 1123 + Punycode (a-z 0-9 hyphen + dot).
#: Rejects anything with whitespace / control chars / unicode that didn't
#: get IDNA-encoded by ``urlsplit``.
_HOSTNAME_CHAR_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

#: Minimal HTML scrapers — only used by the W11.1 placeholder
#: ``build_clone_spec_from_capture``. The W11.3 row will replace these
#: with a proper parser (BeautifulSoup / selectolax / etc.).
_TITLE_RE = re.compile(
    r"<title[^>]*>(?P<t>.*?)</title>",
    re.IGNORECASE | re.DOTALL,
)
_META_DESC_RE = re.compile(
    r"""<meta\s+[^>]*name=['"]description['"][^>]*content=['"](?P<d>[^'"]*)['"]""",
    re.IGNORECASE,
)


# ── Errors ────────────────────────────────────────────────────────────

class SiteClonerError(Exception):
    """Base class for everything raised by ``site_cloner``."""


class InvalidCloneURLError(SiteClonerError):
    """URL failed the syntactic validation gate (bad scheme, missing
    host, userinfo present, malformed hostname, etc.)."""


class BlockedDestinationError(SiteClonerError):
    """URL host resolves to / textually matches a destination this module
    refuses to fetch (loopback, link-local, RFC1918, cloud-metadata IP,
    .onion, .local mDNS, etc.). Distinct from ``InvalidCloneURLError`` so
    SSRF attempts get their own audit-log severity."""


class CloneSourceError(SiteClonerError):
    """A ``CloneSource`` backend (Firecrawl / Playwright / mock) raised
    while attempting the capture. The original exception is preserved on
    ``__cause__``."""


class CloneCaptureTimeoutError(CloneSourceError):
    """The capture exceeded the per-call timeout. Subclass of
    ``CloneSourceError`` so generic backend-error handlers still catch
    it; routers that want to map to HTTP 504 can match the subclass."""


class CloneSpecBuildError(SiteClonerError):
    """Building the ``CloneSpec`` from the raw capture failed. Not the
    same as a *partial* spec — partial specs return successfully with
    ``warnings`` populated; this is reserved for unrecoverable parse
    failures (e.g. binary response masquerading as HTML)."""


# ── Data structures ───────────────────────────────────────────────────

@dataclass(frozen=True)
class RawCapture:
    """The raw output of a ``CloneSource.capture()`` call.

    Intentionally minimal — backends should return *exactly* this shape
    so the orchestrator never has to branch on the source type. Image /
    asset bytes are deliberately *not* in here: W11.6 (L3) mandates that
    OmniSight never copies raw bytes from the source site, so backends
    return the URLs of discovered assets and the L3 transformer
    substitutes placeholders.
    """

    url: str
    """The final URL after any redirects the backend followed."""

    html: str
    """Rendered HTML (post-JS execution if the backend supports it)."""

    status_code: int
    """HTTP status code of the final response."""

    fetched_at: str
    """ISO-8601 UTC timestamp when the capture completed (W11.7 manifest
    consumes this)."""

    backend: str
    """Identifier of the backend that produced the capture
    (``"firecrawl"`` / ``"playwright"`` / ``"mock"`` etc.)."""

    asset_urls: tuple[str, ...] = ()
    """URLs of assets (images, fonts, stylesheets) the backend
    discovered. *Not* the bytes — see class docstring."""

    headers: Mapping[str, str] = field(default_factory=dict)
    """Final-response HTTP headers (lower-cased keys recommended).
    L1 (W11.4) ai.txt / X-Robots-Tag check reads from here."""


@dataclass
class CloneSpec:
    """Structured, transformer-friendly description of a cloned page.

    The categories below match the W11.3 spec line:

        title / meta / hero / nav / sections[] / footer / images[] /
        colors[] / fonts[] / spacing

    W11.1 ships the *container* with safe defaults so downstream code
    (W11.6 L3 transformer, W11.9 framework adapter, W11.10 agent prompt
    context) can be built against a stable shape. W11.3 ships the
    *population* logic that turns rendered HTML into a fully-filled
    spec. ``warnings`` collects non-fatal issues encountered during the
    build so callers can surface partial-success states.
    """

    source_url: str
    """The validated, normalised URL the spec was built from."""

    fetched_at: str
    """ISO-8601 UTC timestamp inherited from the raw capture (W11.7
    manifest pins this)."""

    backend: str
    """Backend identifier inherited from the raw capture."""

    title: Optional[str] = None
    meta: dict[str, str] = field(default_factory=dict)
    hero: Optional[dict[str, Any]] = None
    nav: list[dict[str, Any]] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    footer: Optional[dict[str, Any]] = None
    images: list[dict[str, Any]] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    fonts: list[str] = field(default_factory=list)
    spacing: dict[str, Any] = field(default_factory=dict)

    warnings: list[str] = field(default_factory=list)
    """Non-fatal issues encountered while populating the spec
    (e.g. ``"hero block not detected"``). Empty list = clean build."""


# ── Backend protocol ──────────────────────────────────────────────────

@runtime_checkable
class CloneSource(Protocol):
    """Pluggable capture backend.

    W11.2 ships two implementations:

        * Firecrawl SaaS adapter
        * Self-hosted Playwright adapter (mandatory for air-gapped
          deployments)

    Tests / CI / unit work substitute a ``MockCloneSource`` that returns
    a pre-baked ``RawCapture`` so the W11.1 orchestrator can be
    exercised without any network I/O.

    The protocol is deliberately tiny — single async method, one
    typed return value. ``timeout_s`` and ``max_html_bytes`` are passed
    by the orchestrator so all backends honour the same caller-specified
    budgets. A backend that overruns either MUST raise (the orchestrator
    will translate to ``CloneCaptureTimeoutError`` or
    ``CloneSourceError`` as appropriate).
    """

    name: str
    """Stable identifier emitted into ``RawCapture.backend``."""

    async def capture(
        self,
        url: str,
        *,
        timeout_s: float,
        max_html_bytes: int,
    ) -> RawCapture: ...


# ── URL validation ────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """Return a canonical form of ``url``.

    * Strips fragment (``#anchor``) — irrelevant for cloning the page.
    * Lower-cases scheme + host.
    * Drops default ports (``:80`` for http, ``:443`` for https).
    * Preserves path / query exactly (case-sensitive).
    * Drops trailing slash on bare-host URLs (``https://x.com/`` →
      ``https://x.com``) so two equivalent inputs hash the same in the
      W11.8 rate-limiter key.

    Raises ``InvalidCloneURLError`` for syntactically broken URLs.
    """
    if not isinstance(url, str):
        raise InvalidCloneURLError(
            f"url must be str, got {type(url).__name__}"
        )
    raw = url.strip()
    if not raw:
        raise InvalidCloneURLError("url is empty")

    try:
        parts = urlsplit(raw)
    except ValueError as e:
        raise InvalidCloneURLError(f"url failed to parse: {e}") from e

    scheme = (parts.scheme or "").lower()
    if scheme not in SUPPORTED_URL_SCHEMES:
        raise InvalidCloneURLError(
            f"unsupported scheme {scheme!r}; expected one of "
            f"{sorted(SUPPORTED_URL_SCHEMES)}"
        )

    if not parts.hostname:
        raise InvalidCloneURLError("url has no hostname")

    # urlsplit folds userinfo into ``netloc`` — refusing it here closes
    # the user:pass@evil.com classic phishing-of-cloner trick.
    if "@" in (parts.netloc or ""):
        raise InvalidCloneURLError(
            "url contains userinfo (user:pass@host); refused"
        )

    host = parts.hostname.lower()
    if not _HOSTNAME_CHAR_RE.match(host):
        # urlsplit's IDNA encoding should have produced ascii. Anything
        # else is a malformed input we shouldn't pass to a backend.
        raise InvalidCloneURLError(
            f"hostname {host!r} contains characters outside RFC 1123"
        )

    port = parts.port
    if port is not None:
        # Drop default ports for canonicalisation but keep non-default.
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            netloc = host
        else:
            netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parts.path or ""
    # Normalise the empty path to "" not "/" so two equivalent forms
    # rate-limit-key the same.
    if path == "/":
        path = ""

    canonical = urlunsplit((scheme, netloc, path, parts.query, ""))
    return canonical


def extract_hostname(url: str) -> str:
    """Return the lower-case ascii hostname from ``url``.

    Raises ``InvalidCloneURLError`` if the URL has no host. Performs no
    SSRF check — that's ``is_public_destination``'s job.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError as e:
        raise InvalidCloneURLError(f"url failed to parse: {e}") from e
    if not host:
        raise InvalidCloneURLError("url has no hostname")
    return host.lower()


def is_public_destination(host: str) -> bool:
    """Return ``True`` iff ``host`` is safe to fetch from a server-side
    cloner.

    A "public" destination is anything that is *not*:

        * A hostname literal we blocklist (``localhost`` etc.).
        * A hostname suffix we blocklist (``.local``, ``.onion`` etc.).
        * An IP address that ``ipaddress`` flags as loopback /
          link-local / private / reserved / multicast / unspecified.

    This is **not** a DNS-resolved check — DNS rebinding (where the host
    resolves to a public IP at validation time then a private IP at
    fetch time) is the W11.4 / W11.8 layer's responsibility (combine
    with HTTP-level "fetch-once" semantics + final-IP audit). At the
    URL-validation layer we only catch the static cases, which already
    rules out 99% of SSRF probes.
    """
    if not isinstance(host, str) or not host:
        return False

    h = host.strip().lower()
    if h in _BLOCKED_HOSTNAME_LITERALS:
        return False
    for suffix in _BLOCKED_HOSTNAME_SUFFIXES:
        if h.endswith(suffix):
            return False

    # If the host parses as an IP literal, run the ipaddress safety net.
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # Not an IP literal — only the suffix/literal blocklists apply.
        return True

    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False
    return True


def validate_clone_url(url: str) -> str:
    """End-to-end URL safety gate.

    Pipeline:

        1. ``normalize_url`` — syntactic validation + canonicalisation.
        2. ``is_public_destination(host)`` — SSRF blocklist.

    Returns the canonical URL on success. Raises
    ``InvalidCloneURLError`` for syntactic failures and
    ``BlockedDestinationError`` for SSRF / unsafe-destination hits.
    """
    canonical = normalize_url(url)
    host = extract_hostname(canonical)
    if not is_public_destination(host):
        raise BlockedDestinationError(
            f"refused to clone {host!r}: destination matches the "
            "loopback / link-local / private / reserved / mDNS / Tor "
            "blocklist"
        )
    return canonical


# ── Spec construction ────────────────────────────────────────────────

def build_clone_spec_from_capture(
    capture: RawCapture,
    *,
    source_url: Optional[str] = None,
) -> CloneSpec:
    """Map a ``RawCapture`` into a ``CloneSpec`` shell.

    W11.1 deliberately implements only the trivial mappings:

        * ``title`` from the first ``<title>`` tag.
        * ``meta["description"]`` from the first ``<meta name="description">``.
        * ``images`` populated from ``capture.asset_urls`` (URLs only —
          W11.6 L3 will replace each entry with a placeholder).

    Everything else (hero, nav, sections, footer, colors, fonts,
    spacing) is the W11.3 row's responsibility — the fields exist with
    safe defaults so downstream consumers can be written now.

    ``source_url`` lets the caller pin the *requested* URL (the
    validated, pre-redirect form) into the spec; defaults to
    ``capture.url`` (the post-redirect URL).
    """
    if not isinstance(capture, RawCapture):
        raise CloneSpecBuildError(
            f"capture must be RawCapture, got {type(capture).__name__}"
        )
    if not isinstance(capture.html, str):
        raise CloneSpecBuildError(
            f"capture.html must be str, got {type(capture.html).__name__}"
        )

    spec = CloneSpec(
        source_url=source_url or capture.url,
        fetched_at=capture.fetched_at,
        backend=capture.backend,
    )

    title_match = _TITLE_RE.search(capture.html)
    if title_match:
        spec.title = title_match.group("t").strip() or None
    else:
        spec.warnings.append("title tag not found")

    desc_match = _META_DESC_RE.search(capture.html)
    if desc_match:
        spec.meta["description"] = desc_match.group("d").strip()

    if capture.asset_urls:
        spec.images = [{"url": u} for u in capture.asset_urls]

    # Categories the W11.3 row populates remain at their dataclass
    # defaults; we annotate so partial-success consumers can detect
    # "W11.1 placeholder" vs. "W11.3 fully populated" specs.
    spec.warnings.append("W11.1 placeholder build — W11.3 will populate "
                         "hero / nav / sections / footer / colors / fonts / spacing")

    return spec


# ── Orchestrator ──────────────────────────────────────────────────────

async def clone_site(
    url: str,
    *,
    source: CloneSource,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
) -> CloneSpec:
    """Public W11.1 entry point — URL → ``CloneSpec``.

    Flow:

        1. ``validate_clone_url(url)`` — syntactic + SSRF gate.
        2. ``await source.capture(canonical_url, timeout_s=...)`` — hand
           the fetch to the pluggable backend (W11.2).
        3. ``build_clone_spec_from_capture(capture, source_url=...)`` —
           map raw HTML to ``CloneSpec`` shell.

    All three steps may raise; every raised exception is a
    ``SiteClonerError`` subclass so a single ``except SiteClonerError``
    in the calling router is enough.

    The orchestrator is deliberately stateless — no module-global mutable
    cache, no implicit rate-limit bucket. Quota / PEP-HOLD enforcement
    is the W11.8 row's responsibility and runs *before* this function is
    invoked. (Module-global state audit: the only module-level mutables
    are ``frozenset`` literals and compiled regexes — both immutable.
    Cross-worker consistency: trivially answer #1 — every worker derives
    the same constants from source.)
    """
    if not isinstance(source, CloneSource):
        raise SiteClonerError(
            "source must implement the CloneSource protocol "
            "(name: str, async capture(url, *, timeout_s, max_html_bytes))"
        )
    if not (isinstance(timeout_s, (int, float)) and timeout_s > 0):
        raise SiteClonerError(
            f"timeout_s must be a positive number, got {timeout_s!r}"
        )
    if not (isinstance(max_html_bytes, int) and max_html_bytes > 0):
        raise SiteClonerError(
            f"max_html_bytes must be a positive int, got {max_html_bytes!r}"
        )

    canonical = validate_clone_url(url)

    try:
        # Outer asyncio guard — the backend SHOULD enforce ``timeout_s``
        # internally; this guard catches a misbehaving backend that
        # blocks longer than declared. Bound at ``timeout_s`` directly so
        # callers get the timeout they asked for; backends that need
        # internal headroom must subtract from their own sub-timer.
        capture = await asyncio.wait_for(
            source.capture(
                canonical,
                timeout_s=float(timeout_s),
                max_html_bytes=int(max_html_bytes),
            ),
            timeout=float(timeout_s),
        )
    except asyncio.TimeoutError as e:
        raise CloneCaptureTimeoutError(
            f"backend {getattr(source, 'name', '?')!r} exceeded "
            f"timeout_s={timeout_s}s for {canonical!r}"
        ) from e
    except SiteClonerError:
        # Backend already raised a typed cloner error — let it through.
        raise
    except Exception as e:
        raise CloneSourceError(
            f"backend {getattr(source, 'name', '?')!r} raised while "
            f"capturing {canonical!r}: {e!s}"
        ) from e

    if not isinstance(capture, RawCapture):
        raise CloneSourceError(
            f"backend {getattr(source, 'name', '?')!r} returned "
            f"{type(capture).__name__}, expected RawCapture"
        )

    if isinstance(capture.html, str) and len(capture.html.encode("utf-8", errors="ignore")) > max_html_bytes:
        raise CloneSourceError(
            f"capture HTML size {len(capture.html)} bytes exceeds "
            f"max_html_bytes={max_html_bytes}"
        )

    return build_clone_spec_from_capture(capture, source_url=canonical)


# Sequence is exported for tests that want to assert the protocol shape
# without instantiating it.
_PROTOCOL_REQUIRED_ATTRS: Sequence[str] = ("name", "capture")

__all__ = [
    "BlockedDestinationError",
    "CLOUD_METADATA_IP",
    "CloneCaptureTimeoutError",
    "CloneSource",
    "CloneSourceError",
    "CloneSpec",
    "CloneSpecBuildError",
    "DEFAULT_MAX_HTML_BYTES",
    "DEFAULT_TIMEOUT_S",
    "InvalidCloneURLError",
    "RawCapture",
    "SUPPORTED_URL_SCHEMES",
    "SiteClonerError",
    "build_clone_spec_from_capture",
    "clone_site",
    "extract_hostname",
    "is_public_destination",
    "normalize_url",
    "validate_clone_url",
]
