"""W11.1 + W11.3 #XXX — URL → ``CloneSpec`` orchestrator + populator.

Single entry point for the W11 *Website Cloning Capability* epic. Given
a public URL, it normalises + safety-validates the URL, hands the fetch
to a pluggable ``CloneSource`` backend (W11.2: Firecrawl SaaS or
self-hosted Playwright), and returns a structured ``CloneSpec`` the
downstream productizer pipeline (Next / Nuxt / Astro / Vue / Svelte
scaffolders) can consume to build a fresh, *transformed* clone.

W11.1 (orchestrator) and W11.3 (full ``CloneSpec`` population) are both
implemented here. The module ships:

    * The public ``clone_site(url, *, source, ...)`` entry point.
    * The ``CloneSpec`` container — title / meta / hero / nav /
      sections[] / footer / images[] / colors[] / fonts[] / spacing
      (W11.3 spec line). All categories default to empty / ``None`` so
      partial-success consumers can branch on emptiness.
    * The ``CloneSource`` Protocol (W11.2 plugs Firecrawl + Playwright
      into this).
    * URL safety validation (scheme allowlist, userinfo reject, SSRF
      destination guard via ``ipaddress``).
    * ``build_clone_spec_from_capture`` — full W11.3 populator that
      extracts every category in a single ``html.parser`` pass plus a
      few targeted regex sweeps for inline ``style=`` declarations.
      Stdlib-only (no new pip deps → no production image rebuild needed,
      Production Readiness Gate §158 satisfied for free).
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
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence, runtime_checkable
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

#: Colour-token extraction. Matches the syntactic shape of a CSS
#: colour value — hex (3/4/6/8 digits), ``rgb(...)``, ``rgba(...)``,
#: ``hsl(...)``, ``hsla(...)``. Order is significant: the 6-8 digit hex
#: alternative must come BEFORE the 3-4 digit one; the regex is greedy
#: but the alternation tries left-to-right and won't backtrack across
#: the alternation boundary, so an 8-digit colour (``#111827``) would
#: otherwise be eaten by the 3-4 alt as ``#1118``.
#:
#: Run only against CSS-context strings (``style=`` attribute values
#: and ``<style>`` block bodies) — anywhere else risks false positives
#: from page text that happens to contain a hex literal.
#:
#: Named colours (``red``, ``rebeccapurple``) are intentionally NOT
#: matched: the substring ``red`` shows up in class names, ids, and
#: copy ("Required field…") far more often than as an actual colour.
_STYLE_COLOR_RE = re.compile(
    r"""(#[0-9a-fA-F]{6,8}|#[0-9a-fA-F]{3,4}|rgba?\([^)]+\)|hsla?\([^)]+\))""",
    re.IGNORECASE,
)

#: ``font-family: 'Inter', sans-serif`` → captures the comma-separated
#: stack (group 1). The negated character class deliberately allows
#: quotes inside the value because real declarations look like
#: ``font-family: 'Inter', sans-serif`` and ``"Helvetica Neue"``; we
#: strip the quotes per-piece in ``_add_font``.
_FONT_FAMILY_RE = re.compile(
    r"""font-family\s*:\s*([^;}\n]+)""",
    re.IGNORECASE,
)

#: Spacing token candidates pulled from inline / ``<style>`` declarations.
#: ``padding: 16px``, ``margin-top: 1.5rem``, ``gap: 12px`` etc.
_SPACING_RE = re.compile(
    r"""(?P<prop>padding|margin|gap)(?:-[a-z]+)?\s*:\s*(?P<val>[^;"'}\n]+)""",
    re.IGNORECASE,
)

#: ``max-width: 1200px`` declared anywhere — used as the primary content
#: width hint when present (downstream W11.9 framework adapter consumes
#: this to seed a Tailwind ``max-w-*`` or CSS variable).
_MAX_WIDTH_RE = re.compile(
    r"""max-width\s*:\s*(?P<val>[0-9.]+\s*(?:px|rem|em|%|vw))""",
    re.IGNORECASE,
)

#: Hosts whose ``<link href>`` we treat as web-font references (in
#: addition to ``<link rel=preload as=font>`` and any ``rel`` mentioning
#: ``font``). Lower-case substring match is enough — we just record the
#: URL string for the W11.6 L3 transformer / W11.9 adapter.
_FONT_HOST_HINTS: tuple[str, ...] = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "use.typekit.net",
    "fast.fonts.net",
    "use.fontawesome.com",
)

#: Cap on the per-category list lengths in ``CloneSpec``. Real landing
#: pages have ≤ 50 nav links / images / sections in 99% of cases; the
#: cap prevents a pathological / adversarial page (long-list attack)
#: from blowing up the productizer's downstream prompt budget.
_MAX_LIST_ITEMS_PER_CATEGORY: int = 100

#: Cap on the number of distinct colours / fonts / spacing tokens we
#: keep. CSS frameworks ship 100s of utility classes — we want a curated
#: design-token snapshot, not the full palette.
_MAX_DESIGN_TOKENS: int = 24

#: Cap on the per-section text summary length (chars). Sections are
#: condensed to a one-line summary; longer text gets truncated with an
#: ellipsis sentinel. Downstream LLM rewriters (W11.6 L3) work on
#: summaries, not full body copy.
_MAX_SECTION_SUMMARY_CHARS: int = 280

#: Tags whose enclosed text contributes to the *visible* document
#: outline (used to produce ``sections[]`` summaries). ``script`` /
#: ``style`` / ``svg`` text is intentionally ignored.
_VISIBLE_TEXT_TAGS: frozenset[str] = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "blockquote", "summary", "figcaption",
    "div", "span", "td", "th", "a", "strong", "em",
    "small", "label", "legend", "caption", "dt", "dd",
})

#: Tags whose entire subtree is dropped from the outline / hero /
#: footer / section text capture.
_NON_VISIBLE_TAGS: frozenset[str] = frozenset({
    "script", "style", "noscript", "template", "svg", "math",
    "object", "embed", "video", "audio", "iframe",
})


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


# ── Spec construction (W11.3) ────────────────────────────────────────
#
# Single-pass HTML parser that fills every ``CloneSpec`` category from
# one walk of the document, plus a few targeted regex sweeps over the
# raw HTML for inline-style declarations the parser doesn't see
# semantically. The parser is stdlib-only (``html.parser.HTMLParser``)
# so adding W11.3 does NOT require a Production image rebuild — every
# Python the project already runs has it. Production Readiness Gate
# §158 is therefore satisfied without any new pip dep.
#
# Module-global state audit: this section adds no module-level mutables
# beyond compiled regex literals (immutable). Each ``_SpecCollector``
# instance owns its own per-call buffers; no singleton, no cache, no
# cross-worker coordination needed (SOP §1 answer #1).


def _normalize_text(s: str) -> str:
    """Collapse runs of whitespace to single spaces, strip ends. Keeps
    visible glyphs only — the parser already converted entities."""
    return " ".join((s or "").split())


def _truncate(s: str, limit: int) -> str:
    """Truncate to ``limit`` chars, appending an ellipsis when cut."""
    s = s or ""
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _ordered_unique(items: Iterable[str]) -> list[str]:
    """Stable de-duplication of an iterable of strings."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


class _SpecCollector(HTMLParser):
    """Single-pass HTML walker that populates every ``CloneSpec``
    category in one DOM traversal.

    Design notes:

    * ``convert_charrefs=True`` so ``handle_data`` already receives
      decoded text (``&amp;`` → ``&``).
    * A small ``self._stack`` of ``(tag, attrs)`` tracks nesting so we
      know whether the current ``<a>`` lives inside a ``<nav>`` or
      ``<footer>``, and whether the current text run belongs to a
      ``<section>``'s heading vs. body.
    * Self-closing / void elements (``img``, ``meta``, ``link``,
      ``input``, ``br``, ``hr``, ``source``) are explicitly handled
      via ``handle_startendtag`` AND ``handle_starttag`` because real
      pages mix XHTML-style ``<img/>`` with HTML5-style ``<img>``.
    * Subtrees rooted at ``script`` / ``style`` / ``noscript`` /
      ``svg`` etc. (``_NON_VISIBLE_TAGS``) are *not* skipped at the
      parser level (HTMLParser doesn't expose subtree skip cheaply),
      but their text is suppressed via ``self._suppress_depth``.
      Unbalanced tags decrement defensively; clamps at 0 so a
      tag-soup page doesn't go negative and accidentally re-enable
      capture.
    """

    _VOID_ELEMENTS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)

        # Outputs populated as we walk.
        self.title: Optional[str] = None
        self.meta: dict[str, str] = {}
        self.hero_h1: Optional[str] = None
        self.hero_tagline: Optional[str] = None
        self.hero_cta: Optional[dict[str, str]] = None
        self.nav_links: list[dict[str, str]] = []
        self.section_records: list[dict[str, Any]] = []
        self.footer_text_buf: list[str] = []
        self.footer_links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        # Use list+dedupe rather than set so insertion order is preserved
        # for the W11.6 L3 transformer (deterministic LLM prompt input).
        self._color_seen: set[str] = set()
        self.colors: list[str] = []
        self._font_seen: set[str] = set()
        self.fonts: list[str] = []

        # Internal state.
        self._stack: list[tuple[str, dict[str, str]]] = []
        self._suppress_depth: int = 0
        self._title_buf: list[str] = []
        self._in_title: bool = False
        # Per-section accumulator: heading text + body text + link list.
        self._section_buf: Optional[dict[str, Any]] = None
        # While inside a heading (h1-h6) we accumulate to a heading-text
        # buffer instead of the section body buffer.
        self._heading_depth: int = 0
        self._heading_level: Optional[int] = None
        self._heading_text_buf: list[str] = []
        # First H1 outside a section is the hero candidate; we capture
        # *its* text here.
        self._hero_h1_buf: Optional[list[str]] = None
        # Once we have a hero H1, the *next* paragraph's first chunk of
        # text becomes the tagline.
        self._tagline_pending: bool = False
        self._tagline_buf: Optional[list[str]] = None
        # Buffer for the current ``<a>`` link's anchor text. ``None``
        # outside an ``<a>``.
        self._a_buf: Optional[list[str]] = None
        self._a_href: Optional[str] = None
        # Hero CTA candidate: first ``<a>`` with a button-ish class /
        # role after the hero H1.
        self._hero_cta_pending: bool = False

    # ── Helpers ──────────────────────────────────────────────────────

    def _attrs_dict(self, attrs: list[tuple[str, Optional[str]]]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in attrs:
            if v is None:
                out[k.lower()] = ""
            else:
                out[k.lower()] = v
        return out

    def _is_inside(self, tag: str) -> bool:
        return any(t == tag for t, _ in self._stack)

    def _add_color(self, color: str) -> None:
        norm = color.strip()
        if not norm or norm in self._color_seen:
            return
        if len(self.colors) >= _MAX_DESIGN_TOKENS:
            return
        self._color_seen.add(norm)
        self.colors.append(norm)

    def _add_font(self, font: str) -> None:
        norm = font.strip().strip("'\"")
        if not norm or norm in self._font_seen:
            return
        if len(self.fonts) >= _MAX_DESIGN_TOKENS:
            return
        self._font_seen.add(norm)
        self.fonts.append(norm)

    def _harvest_inline_style(self, style: str) -> None:
        if not style:
            return
        for m in _STYLE_COLOR_RE.finditer(style):
            self._add_color(m.group(1))
        for m in _FONT_FAMILY_RE.finditer(style):
            for piece in m.group(1).split(","):
                self._add_font(piece)

    # ── HTMLParser hooks ─────────────────────────────────────────────

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attrs_dict = self._attrs_dict(attrs)

        # Push the open tag onto the stack (void tags get popped
        # immediately at the bottom of this method).
        self._stack.append((tag, attrs_dict))

        if tag in _NON_VISIBLE_TAGS:
            self._suppress_depth += 1

        # Inline-style harvest applies to *every* tag.
        style = attrs_dict.get("style", "")
        if style:
            self._harvest_inline_style(style)

        if tag == "title":
            self._in_title = True
            self._title_buf = []

        elif tag == "meta":
            name = (attrs_dict.get("name") or attrs_dict.get("property") or "").strip().lower()
            content = (attrs_dict.get("content") or "").strip()
            if name and content and name not in self.meta:
                self.meta[name] = content
                if name == "theme-color":
                    self._add_color(content)

        elif tag == "link":
            rel = (attrs_dict.get("rel") or "").lower()
            href = (attrs_dict.get("href") or "").strip()
            if href:
                lo = href.lower()
                is_font = (
                    "font" in rel
                    or (rel == "preload" and (attrs_dict.get("as", "").lower() == "font"))
                    or any(h in lo for h in _FONT_HOST_HINTS)
                )
                if is_font:
                    self._add_font(href)

        elif tag == "img":
            src = (attrs_dict.get("src") or "").strip()
            if src and not src.lower().startswith("data:"):
                if len(self.images) < _MAX_LIST_ITEMS_PER_CATEGORY:
                    rec: dict[str, str] = {"url": src}
                    alt = (attrs_dict.get("alt") or "").strip()
                    if alt:
                        rec["alt"] = alt
                    self.images.append(rec)

        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            self._heading_depth += 1
            self._heading_level = level
            self._heading_text_buf = []
            if (
                tag == "h1"
                and self.hero_h1 is None
                and self._section_buf is None
                and not self._is_inside("footer")
            ):
                # First H1 outside a section/footer = hero candidate.
                self._hero_h1_buf = self._heading_text_buf

        elif tag == "section":
            # Begin a new section accumulator (nest is unusual but we
            # cope by replacing — we always emit on close).
            if len(self.section_records) < _MAX_LIST_ITEMS_PER_CATEGORY:
                self._section_buf = {
                    "heading": None,
                    "body": [],
                    "links": [],
                }

        elif tag == "a":
            href = (attrs_dict.get("href") or "").strip()
            self._a_href = href or None
            self._a_buf = []
            # Hero CTA detection — first ``<a>`` after we recorded a
            # hero H1, with a button-ish hint, no explicit nav/footer
            # ancestor.
            if (
                self._hero_cta_pending
                and self.hero_cta is None
                and not self._is_inside("nav")
                and not self._is_inside("footer")
            ):
                cls = (attrs_dict.get("class") or "").lower()
                role = (attrs_dict.get("role") or "").lower()
                if "btn" in cls or "button" in cls or role == "button" or "cta" in cls:
                    # Tentatively claim the CTA — finalise on </a>.
                    self.hero_cta = {"href": href or "", "label": ""}

        elif tag == "p":
            # Capture first paragraph after hero H1 as tagline.
            if self._tagline_pending and self._tagline_buf is None:
                self._tagline_buf = []

        # Pop voids immediately — they have no closing tag.
        if tag in self._VOID_ELEMENTS:
            self._stack.pop()
            if tag in _NON_VISIBLE_TAGS:
                # No void tag is in _NON_VISIBLE_TAGS today, but keep
                # this defensive in case the set is extended.
                self._suppress_depth = max(0, self._suppress_depth - 1)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # XHTML-style self-closing ``<img/>`` — treat as a normal start
        # for void elements (they auto-pop in handle_starttag).
        self.handle_starttag(tag, attrs)
        # Non-void tags written as self-closing in tag soup: pop them
        # immediately so the stack stays balanced.
        if tag.lower() not in self._VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        # Pop the matching open tag (best-effort — tag soup may have
        # mismatches; we walk the stack from the top to find the most
        # recent matching entry, drop everything above it).
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                # Maintain suppression depth for any popped non-visible
                # tags. (HTMLParser fires endtag for explicit closes,
                # which is the only case that matters for our subtree
                # skip.)
                for popped, _ in self._stack[i:]:
                    if popped in _NON_VISIBLE_TAGS:
                        self._suppress_depth = max(0, self._suppress_depth - 1)
                self._stack = self._stack[:i]
                break

        if tag == "title":
            self._in_title = False
            text = _normalize_text("".join(self._title_buf))
            self.title = text or None
            self._title_buf = []

        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_depth = max(0, self._heading_depth - 1)
            heading_text = _normalize_text("".join(self._heading_text_buf))
            # Hero H1 finalisation.
            if self._hero_h1_buf is not None and self.hero_h1 is None:
                self.hero_h1 = heading_text or None
                self._hero_h1_buf = None
                self._tagline_pending = bool(self.hero_h1)
                self._hero_cta_pending = bool(self.hero_h1)
            # Section heading finalisation: the first heading inside
            # the section becomes its title.
            if self._section_buf is not None and not self._section_buf.get("heading"):
                self._section_buf["heading"] = heading_text or None
            self._heading_text_buf = []
            self._heading_level = None

        elif tag == "section":
            if self._section_buf is not None:
                body = _normalize_text(" ".join(self._section_buf["body"]))
                rec: dict[str, Any] = {
                    "heading": self._section_buf.get("heading"),
                    "summary": _truncate(body, _MAX_SECTION_SUMMARY_CHARS) or None,
                }
                if self._section_buf["links"]:
                    rec["links"] = self._section_buf["links"][:20]
                self.section_records.append(rec)
                self._section_buf = None

        elif tag == "a":
            label = _normalize_text("".join(self._a_buf or []))
            href = self._a_href or ""
            if label or href:
                if self._is_inside("nav"):
                    if len(self.nav_links) < _MAX_LIST_ITEMS_PER_CATEGORY:
                        self.nav_links.append({"label": label, "href": href})
                elif self._is_inside("footer"):
                    if len(self.footer_links) < _MAX_LIST_ITEMS_PER_CATEGORY:
                        self.footer_links.append({"label": label, "href": href})
                elif self._section_buf is not None:
                    self._section_buf["links"].append({"label": label, "href": href})
            # Finalise hero CTA — overwrite the placeholder label.
            if self.hero_cta is not None and not self.hero_cta.get("label"):
                self.hero_cta["label"] = label
                self._hero_cta_pending = False
            self._a_buf = None
            self._a_href = None

        elif tag == "p":
            if self._tagline_pending and self._tagline_buf is not None:
                tagline = _normalize_text("".join(self._tagline_buf))
                if tagline:
                    self.hero_tagline = _truncate(tagline, _MAX_SECTION_SUMMARY_CHARS)
                    self._tagline_pending = False
                self._tagline_buf = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
            return
        if self._suppress_depth > 0:
            return

        # Heading buffer (highest priority — heading text is also
        # considered visible for sections/hero).
        if self._heading_depth > 0:
            self._heading_text_buf.append(data)

        # Anchor label.
        if self._a_buf is not None:
            self._a_buf.append(data)

        # Tagline candidate.
        if self._tagline_pending and self._tagline_buf is not None:
            self._tagline_buf.append(data)

        # Section body accumulator (skip while inside heading — heading
        # already captured separately).
        if self._section_buf is not None and self._heading_depth == 0:
            self._section_buf["body"].append(data)

        # Footer text accumulator — capture body copy that lives inside
        # a ``<footer>`` (©-line, contact info etc.) so the populator
        # can synthesise a footer summary even when the footer has no
        # ``<a>`` links. Also gated on heading depth so a footer ``<h2>``
        # doesn't double-count.
        if self._is_inside("footer") and self._heading_depth == 0:
            self.footer_text_buf.append(data)


def _harvest_style_blocks(html: str, collector: _SpecCollector) -> None:
    """``<style>`` block contents are not visible to ``handle_data`` (we
    suppress them) so we sweep them via regex once, after parsing, to
    pull design tokens. Each ``<style>...</style>`` block is scanned
    with the same colour / font / spacing regexes used on inline styles.
    """
    block_re = re.compile(r"<style[^>]*>(?P<body>.*?)</style>", re.IGNORECASE | re.DOTALL)
    for m in block_re.finditer(html):
        body = m.group("body") or ""
        for cm in _STYLE_COLOR_RE.finditer(body):
            collector._add_color(cm.group(1))
        for fm in _FONT_FAMILY_RE.finditer(body):
            for piece in fm.group(1).split(","):
                collector._add_font(piece)


def _extract_spacing(html: str) -> dict[str, Any]:
    """Pull a small spacing snapshot from inline + ``<style>`` content.

    We deliberately do NOT try to compute a token system — that's the
    W11.6 L3 transformer's job. We just expose the *raw* observed
    values so the LLM rewriter has something to anchor on.

    The shape:
        {
            "padding": [...],   # up to _MAX_DESIGN_TOKENS distinct values
            "margin":  [...],
            "gap":     [...],
            "max_width": "1200px"  # first observed, when any
        }
    """
    out: dict[str, list[str]] = {"padding": [], "margin": [], "gap": []}
    seen: dict[str, set[str]] = {"padding": set(), "margin": set(), "gap": set()}

    for m in _SPACING_RE.finditer(html):
        prop = m.group("prop").lower()
        val = " ".join(m.group("val").split())
        if not val:
            continue
        bucket = seen[prop]
        if val in bucket:
            continue
        bucket.add(val)
        if len(out[prop]) < _MAX_DESIGN_TOKENS:
            out[prop].append(val)

    spacing: dict[str, Any] = {k: v for k, v in out.items() if v}

    mw = _MAX_WIDTH_RE.search(html)
    if mw:
        spacing["max_width"] = " ".join(mw.group("val").split())

    return spacing


def _merge_asset_images(
    parsed_images: list[dict[str, str]],
    asset_urls: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Combine ``<img>`` tag captures with ``RawCapture.asset_urls``,
    deduping on the URL field. Backend-discovered assets that didn't
    appear as ``<img>`` tags (CSS background images, fetch() requests
    etc.) join the list as plain ``{"url": ...}`` entries. Cap at
    ``_MAX_LIST_ITEMS_PER_CATEGORY``."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rec in parsed_images:
        url = rec.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(dict(rec))
        if len(out) >= _MAX_LIST_ITEMS_PER_CATEGORY:
            return out
    for url in asset_urls:
        if not url or url in seen:
            continue
        if url.lower().startswith("data:"):
            continue  # W11.6 L3: never inline bytes
        seen.add(url)
        out.append({"url": url})
        if len(out) >= _MAX_LIST_ITEMS_PER_CATEGORY:
            break
    return out


def build_clone_spec_from_capture(
    capture: RawCapture,
    *,
    source_url: Optional[str] = None,
) -> CloneSpec:
    """Map a ``RawCapture`` into a fully-populated ``CloneSpec`` (W11.3).

    The populator walks the rendered HTML once with ``html.parser``
    (stdlib, no new pip deps) and collects every category in the W11.3
    schema:

        title       — ``<title>`` text (falls back to first ``<h1>``).
        meta        — every ``<meta name="...">`` and ``<meta property="...">``
                      with non-empty content (description / og:* /
                      twitter:* / theme-color / viewport / etc.).
        hero        — first ``<h1>`` outside ``<section>`` / ``<footer>``,
                      with the next ``<p>`` as ``tagline`` and the next
                      button-ish ``<a>`` as ``cta`` when present.
        nav         — ``<a>`` links inside any ``<nav>``.
        sections[]  — each ``<section>``: heading + condensed summary +
                      its internal links.
        footer      — text + links inside ``<footer>``.
        images[]    — every ``<img src=...>`` plus ``capture.asset_urls``
                      (deduped by URL; ``data:`` URLs dropped per W11.6).
        colors[]    — colours found in inline ``style=`` declarations,
                      ``<style>`` blocks, and ``<meta name="theme-color">``.
        fonts[]     — ``<link>`` references to web-font hosts +
                      ``font-family`` declarations.
        spacing     — observed ``padding`` / ``margin`` / ``gap`` values
                      and the page's ``max-width`` token if declared.

    All collections are capped (``_MAX_LIST_ITEMS_PER_CATEGORY`` = 100,
    ``_MAX_DESIGN_TOKENS`` = 24, ``_MAX_SECTION_SUMMARY_CHARS`` = 280)
    so a pathological landing page can't blow up the downstream
    productizer's prompt budget. ``warnings`` lists categories that
    came up empty so callers can branch on partial-success states.

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

    collector = _SpecCollector()
    try:
        collector.feed(capture.html)
        collector.close()
    except Exception as e:  # pragma: no cover — html.parser is forgiving
        raise CloneSpecBuildError(f"HTML parse failed: {e!s}") from e

    # ── title ────────────────────────────────────────────────────────
    spec.title = collector.title or collector.hero_h1 or None
    if not spec.title:
        spec.warnings.append("title tag not found")

    # ── meta ─────────────────────────────────────────────────────────
    spec.meta = dict(collector.meta)

    # ── hero ─────────────────────────────────────────────────────────
    if collector.hero_h1:
        hero: dict[str, Any] = {"heading": collector.hero_h1}
        if collector.hero_tagline:
            hero["tagline"] = collector.hero_tagline
        if collector.hero_cta and collector.hero_cta.get("label"):
            hero["cta"] = {
                "label": collector.hero_cta["label"],
                "href": collector.hero_cta.get("href", ""),
            }
        spec.hero = hero
    else:
        spec.warnings.append("hero block not detected")

    # ── nav ──────────────────────────────────────────────────────────
    spec.nav = list(collector.nav_links)
    if not spec.nav:
        spec.warnings.append("nav links not detected")

    # ── sections[] ───────────────────────────────────────────────────
    spec.sections = list(collector.section_records)
    if not spec.sections:
        spec.warnings.append("no <section> elements found")

    # ── footer ───────────────────────────────────────────────────────
    footer_text = _normalize_text(" ".join(collector.footer_text_buf))
    if collector.footer_links or footer_text:
        footer: dict[str, Any] = {"links": list(collector.footer_links)}
        if footer_text:
            footer["text"] = _truncate(footer_text, _MAX_SECTION_SUMMARY_CHARS)
        spec.footer = footer
    else:
        spec.warnings.append("footer block not detected")

    # ── images[] ─────────────────────────────────────────────────────
    spec.images = _merge_asset_images(collector.images, capture.asset_urls)
    if not spec.images:
        spec.warnings.append("no images detected")

    # ── colors[] ─────────────────────────────────────────────────────
    # Sweep <style> blocks (parser doesn't expose script/style text).
    _harvest_style_blocks(capture.html, collector)
    spec.colors = list(collector.colors)
    if not spec.colors:
        spec.warnings.append("no colour tokens detected")

    # ── fonts[] ──────────────────────────────────────────────────────
    spec.fonts = list(collector.fonts)
    if not spec.fonts:
        spec.warnings.append("no font tokens detected")

    # ── spacing ──────────────────────────────────────────────────────
    spec.spacing = _extract_spacing(capture.html)
    if not spec.spacing:
        spec.warnings.append("no spacing tokens detected")

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
