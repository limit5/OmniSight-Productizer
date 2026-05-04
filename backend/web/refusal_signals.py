"""W11.4 #XXX — L1 machine refusal-signal scanner.

Layer 1 of the W11 5-layer defense-in-depth pipeline. This module is the
*first* gate the W11 router runs before invoking ``clone_site()`` — it
checks every machine-readable opt-out signal a destination site exposes
and short-circuits the clone with ``MachineRefusedError`` if any signal
forbids automated access.

Five signal sources covered, mapped 1:1 to the W11.4 row spec
(``robots.txt + noai meta + ai.txt + CF ai-bot rule``):

    1. ``robots.txt``                  — pre-capture, fetched separately.
    2. ``ai.txt`` / ``.well-known/ai.txt``
                                       — pre-capture, fetched separately.
                                         Spawning.ai's AI opt-out spec.
    3. ``<meta name="robots" content="noai">`` (and ``noimageai`` / ``none``)
                                       — post-capture, parsed from HTML.
                                         Also matches ``GPTBot`` / ``ClaudeBot``-
                                         scoped meta tags.
    4. ``X-Robots-Tag`` HTTP header    — post-capture, header read.
    5. Cloudflare AI-bot rule          — post-capture, response sniff
                                         (``cf-mitigated`` / 403 +
                                         ``server: cloudflare``).

Why pre- vs post-capture
------------------------
Some signals (robots.txt / ai.txt) live at well-known URLs *separate* from
the page being cloned, so they're fetched in their own tiny round-trip
*before* we burn a Firecrawl scrape or a Playwright session. Some signals
(meta tags, ``X-Robots-Tag``, CF block page) only become visible once we
have a ``RawCapture`` in hand, so the same scanner runs again post-capture
on the captured ``html`` + ``headers`` + a (heuristic) status sniff.

Where it slots into the W11 pipeline
------------------------------------
The router pattern is::

    decision = await check_machine_refusal_pre_capture(url)
    if decision.refused: raise MachineRefusedError(decision, url=url)

    capture = await source.capture(url, ...)   # only if pre-capture green

    decision = check_machine_refusal_post_capture(capture)
    if decision.refused: raise MachineRefusedError(decision, url=capture.url)

    spec = build_clone_spec_from_capture(capture)

``MachineRefusedError`` is a ``SiteClonerError`` subclass so the existing
W11.1 catch-all (``except SiteClonerError``) keeps working without
special-casing; the W11.12 audit row picks the more specific subclass via
``isinstance``.

Module-global state audit (SOP §1)
----------------------------------
Module-level state is limited to immutable constants (UA strings, suffix
tuples, frozensets, compiled regexes). The default ``_HttpxFetcher`` is
constructed *per call* (one-shot client + ``aclose()``) so there is no
cross-worker connection pool to coordinate. Cross-worker consistency:
trivially answer #1 — every worker derives the same constants from
source. Operators that want a long-lived connection pool can pass their
own ``RefusalFetcher`` implementation through.

Read-after-write timing audit (SOP §2)
--------------------------------------
N/A — every entry point is a pure function over in-memory bytes (post-
capture variants) or a single-shot HTTP fetch (pre-capture variants).
No shared writable state, no parallel-vs-serial timing dependence.

Stdlib + ``httpx`` only — both are already in the production image, so
adding W11.4 needs **no** image rebuild (Production Readiness Gate §158
satisfied without a follow-up requirements bump).

Inspired by firecrawl/open-lovable (MIT). The full attribution + license
text live in ``LICENSES/open-lovable-mit.txt`` (W11.13).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import (
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from backend.web.site_cloner import (
    RawCapture,
    SiteClonerError,
    normalize_url,
)

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

#: User-Agent string the cloner identifies itself with when fetching
#: ``robots.txt`` / ``ai.txt`` and (downstream) the actual page. Pinned
#: as a constant so a site-owner can target the exact token in an
#: opt-out file. The trailing URL is a stable pointer to the W11
#: documentation so site owners know who's calling.
DEFAULT_USER_AGENT: str = "OmniSightCloner/1.0 (+https://github.com/omnisight)"

#: AI-bot UA tokens publishers commonly target in ``robots.txt`` /
#: ``ai.txt`` opt-out blocks. We honour any block keyed against any of
#: these (in addition to ``DEFAULT_USER_AGENT`` and ``*``) on the
#: principle that operators have already declared "no AI scraping" — our
#: bot is an AI scraper even if the site owner didn't list us by name.
#:
#: Lower-case strings; matched case-insensitively against the
#: ``User-agent:`` directive value.
AI_BOT_USER_AGENTS: Tuple[str, ...] = (
    "gptbot",
    "chatgpt-user",
    "oai-searchbot",
    "google-extended",
    "googleother",
    "anthropic-ai",
    "claude-web",
    "claudebot",
    "ccbot",
    "perplexitybot",
    "cohere-ai",
    "bytespider",
    "facebookbot",
    "amazonbot",
    "applebot-extended",
    "yandexbot-ai",
    "diffbot",
    "img2dataset",
    "omgilibot",
)

#: Default HTTP timeout for the pre-capture ``robots.txt`` / ``ai.txt``
#: fetches. Kept short — these files are tiny (≤ a few KiB on real sites)
#: and we don't want a slow opt-out file to delay every clone request.
DEFAULT_REFUSAL_FETCH_TIMEOUT_S: float = 5.0

#: Cap on the bytes we'll accept from a ``robots.txt`` / ``ai.txt``
#: response body. RFC 9309 (robots.txt) recommends a 500 KiB minimum
#: parser limit; we double that for ai.txt headroom but truncate longer
#: payloads silently — pathological / adversarial sites that publish
#: 50 MB ``robots.txt`` files don't get to DoS the cloner's pre-flight.
DEFAULT_REFUSAL_FETCH_MAX_BYTES: int = 1024 * 1024  # 1 MiB

#: Path the ``robots.txt`` standard pins.
ROBOTS_TXT_PATH: str = "/robots.txt"

#: Both paths are tried for ai.txt: spawning.ai's original spec puts it
#: at ``/.well-known/ai.txt`` (RFC 8615 well-known URI), but a meaningful
#: number of early-adopter sites ship it at the root ``/ai.txt`` to mirror
#: ``robots.txt``. The pre-capture scanner tries both.
AI_TXT_PATHS: Tuple[str, ...] = ("/.well-known/ai.txt", "/ai.txt")

#: Tokens inside ``<meta name="robots" content="...">`` and the
#: ``X-Robots-Tag`` HTTP header that publishers use to signal "do not
#: train AI on this content". Lower-cased, no whitespace. ``none`` also
#: refuses on the principle that ``noindex, nofollow`` together imply
#: a strong "don't touch" — strict reading of the X-Robots-Tag spec.
META_NOAI_TOKENS: Tuple[str, ...] = (
    "noai",
    "noimageai",
    "noml",        # noindex-ML; rare but seen on a few opt-out templates
    "none",
)

#: Meta-tag ``name`` attribute values that publishers scope AI opt-outs
#: under. Anything in this list whose ``content`` includes a ``noindex``-
#: family token counts as a refusal — that's the publisher saying "this
#: AI bot in particular is not allowed to index/train on me".
META_AI_BOT_NAMES: Tuple[str, ...] = (
    "robots",  # generic bucket — any directive applies to all bots
    *AI_BOT_USER_AGENTS,
)

#: Tokens whose presence inside a CF block-page response body increase
#: confidence that the 403 we just got is the AI-bot rule, not a
#: vanilla per-IP block. Lower-cased substring match against the body.
CLOUDFLARE_AI_BLOCK_BODY_HINTS: Tuple[str, ...] = (
    "ai bot",
    "ai-bot",
    "ai scrapers",
    "ai scraper",
    "block ai",
    "ai training",
    "scraping is not allowed",
    "blocked from accessing this site",
    "this site is protected from",
)

#: ``cf-mitigated`` header values whose presence is, by itself, enough
#: to refuse. ``challenge`` covers managed-challenge / JS challenge
#: responses; ``block`` is the explicit-block variant CF added with the
#: AI Audit / Block AI Bots rollout.
CLOUDFLARE_MITIGATED_REFUSE_VALUES: Tuple[str, ...] = (
    "challenge",
    "block",
    "managed_challenge",
)

# Pre-compiled regex: ``<meta ... name="..." ... content="...">`` (or the
# ``http-equiv`` cousin). HTML attribute order is unpredictable so we
# match each attribute independently rather than positional. ``re.I`` so
# ``Content``/``CONTENT`` work, ``re.S`` so attribute values that span
# newlines still get caught (rare but seen in minified-then-prettified
# pages).
_META_TAG_RE = re.compile(
    r"<meta\b([^>]*?)/?>",
    re.IGNORECASE | re.DOTALL,
)
_META_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.DOTALL,
)


# ── Errors ───────────────────────────────────────────────────────────────


class MachineRefusedError(SiteClonerError):
    """Raised when one or more L1 refusal signals forbid the clone.

    Carries the full ``RefusalDecision`` so the calling router (W11
    public endpoint) can echo per-signal reasons into the audit log
    (W11.12) and the operator-facing 451 / 403 response body.

    Subclass of ``SiteClonerError`` so existing ``except SiteClonerError``
    handlers keep catching, but the W11.12 audit row uses ``isinstance``
    to assign the ``machine-refused`` severity bucket distinct from
    ``InvalidCloneURLError`` / ``BlockedDestinationError``.
    """

    def __init__(self, decision: "RefusalDecision", *, url: str) -> None:
        self.decision = decision
        self.url = url
        joined = "; ".join(decision.reasons) or "machine refusal signal"
        super().__init__(f"refused {url!r}: {joined}")


# ── Data ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RefusalDecision:
    """Aggregate result of running one or more refusal-signal checks.

    A ``RefusalDecision`` is *additive* — multiple checks (e.g. pre +
    post capture) can be merged with :func:`merge_refusal_decisions` into
    a single decision that records every signal that ran and every
    reason that fired.

    Attributes:
        allowed: ``True`` when no refusal signal fired across every check
            recorded in ``signals_checked``. ``False`` otherwise.
        signals_checked: Stable identifiers of the checks that ran
            (``"robots.txt"`` / ``"ai.txt"`` / ``"meta.noai"`` /
            ``"header.x-robots-tag"`` / ``"cloudflare.ai-block"``).
            Empty signals list AND ``allowed=True`` means "no checks
            ran" — distinct from "checks ran and all passed".
        reasons: Human-readable description of every signal that *fired*.
            Empty list when ``allowed=True``.
        details: Per-signal raw detail strings (e.g. the matching
            ``Disallow:`` line from ``robots.txt``). Useful for the
            audit log + the operator-facing 451 body.
    """

    allowed: bool
    signals_checked: Tuple[str, ...]
    reasons: Tuple[str, ...]
    details: Mapping[str, str] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return not self.allowed


@dataclass(frozen=True)
class RefusalFetchResult:
    """Minimal HTTP response shape the ``RefusalFetcher`` Protocol
    returns.

    Public so test fakes can construct one directly without importing
    httpx. The underlying body cap is enforced by the default
    :class:`_HttpxFetcher` (:data:`DEFAULT_REFUSAL_FETCH_MAX_BYTES`);
    custom fetchers SHOULD honour the same cap.
    """

    status: int
    body: bytes
    headers: Mapping[str, str]


@runtime_checkable
class RefusalFetcher(Protocol):
    """One-shot HTTP GET used to fetch ``robots.txt`` / ``ai.txt``.

    Lives separate from ``CloneSource`` because (a) it's tiny and
    synchronous in spirit (one round-trip, ≤ 1 MiB body), (b) tests
    substitute a callable that returns canned bytes without importing
    httpx, (c) operators that want to share a long-lived connection pool
    across pre-capture checks can plug their own implementation in —
    the default ``_HttpxFetcher`` is intentionally one-shot.
    """

    async def __call__(
        self,
        url: str,
        *,
        timeout_s: float,
        headers: Mapping[str, str],
    ) -> RefusalFetchResult: ...


# ── Default fetcher ──────────────────────────────────────────────────────


class _HttpxFetcher:
    """One-shot ``RefusalFetcher`` built on ``httpx.AsyncClient``.

    Used when the caller of ``check_machine_refusal_pre_capture`` does
    not pass a ``fetcher=`` arg. Constructs a fresh client per call,
    sets ``follow_redirects=True`` (so a ``robots.txt`` served via 301
    to ``www.`` resolves), enforces ``timeout_s`` strictly, and caps the
    response body at ``DEFAULT_REFUSAL_FETCH_MAX_BYTES``.

    Lazy import of httpx — operators on the air-gap stack with httpx
    stripped out should pass their own ``fetcher`` rather than crash at
    import time.
    """

    async def __call__(
        self,
        url: str,
        *,
        timeout_s: float,
        headers: Mapping[str, str],
    ) -> _FetchResult:
        try:
            import httpx  # local lazy import per W11.2 discipline
        except ImportError as e:
            raise SiteClonerError(
                "refusal_signals default fetcher requires httpx; "
                "either install httpx or pass an explicit fetcher="
            ) from e

        async with httpx.AsyncClient(
            follow_redirects=True,
            # Strict timeout: connect + read + write under the same budget.
            timeout=httpx.Timeout(timeout_s),
        ) as client:
            try:
                resp = await client.get(url, headers=dict(headers))
            except Exception as e:
                # Translate every httpx error into a typed SiteClonerError
                # so callers can decide policy (most callers treat this
                # as "no signal" — see ``_resolve_optional_fetch``).
                raise SiteClonerError(
                    f"refusal_signals fetch failed for {url!r}: {e!s}"
                ) from e

            body = resp.content[:DEFAULT_REFUSAL_FETCH_MAX_BYTES]
            # Lower-case headers for consistent downstream lookup.
            normed = {k.lower(): v for k, v in resp.headers.items()}
            return RefusalFetchResult(
                status=int(resp.status_code), body=body, headers=normed
            )


def default_refusal_fetcher() -> RefusalFetcher:
    """Public factory returning the lazy-httpx default fetcher.

    Exposed so callers (and the orchestrator wrapper) can hand the
    same instance to multiple checks if they want connection reuse —
    although the bundled ``_HttpxFetcher`` is one-shot per call by
    design (see class docstring).
    """
    return _HttpxFetcher()


# ── Helpers ──────────────────────────────────────────────────────────────


def _origin_of(url: str) -> str:
    """Return ``scheme://host[:port]`` of ``url`` with no path / query.

    Used to derive ``robots.txt`` / ``ai.txt`` URLs from a target URL.
    Raises the W11.1 canonicaliser's ``InvalidCloneURLError`` if ``url``
    is malformed — so callers can treat "bad origin" identically to
    "bad target URL".
    """
    canonical = normalize_url(url)
    parts = urlsplit(canonical)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _ua_token(s: str) -> str:
    """Lower-case + strip an UA token for comparison against the
    constants in :data:`AI_BOT_USER_AGENTS`. ``None``-safe."""
    return (s or "").strip().lower()


def _split_directive_value(value: str) -> Iterable[str]:
    """``X-Robots-Tag`` and ``content`` attributes use comma-separated
    directive lists (``noindex, nofollow, noai``). Yield lower-cased
    pieces with whitespace stripped. Skips empty pieces."""
    for piece in (value or "").replace(";", ",").split(","):
        token = piece.strip().lower()
        # ``X-Robots-Tag`` can prefix a UA: ``GPTBot: noindex``. Strip
        # the prefix — we already evaluated the UA scoping at the
        # caller layer (``check_x_robots_tag`` passes the UA in).
        if ":" in token:
            token = token.split(":", 1)[1].strip()
        if token:
            yield token


def _bytes_to_text(body: bytes) -> str:
    """Decode a ``robots.txt`` / ``ai.txt`` body. Always UTF-8 with
    ``replace`` errors — RFC 9309 mandates UTF-8 for ``robots.txt``;
    a non-conformant publisher who serves Latin-1 will at worst lose
    a few accented chars in the directive comments, never the directives
    themselves (they're ASCII)."""
    return body.decode("utf-8", errors="replace")


def _make_robotparser(text: str, base_url: str) -> RobotFileParser:
    """Build a ``RobotFileParser`` over ``text``. Stdlib parser — handles
    the RFC 9309 syntax (``User-agent:``, ``Allow:``, ``Disallow:``,
    case-insensitive UA matching, longest-match Allow/Disallow precedence)."""
    rp = RobotFileParser()
    rp.set_url(base_url)
    # ``parse`` expects an iterable of lines.
    rp.parse(text.splitlines())
    return rp


# ── Public sub-checks ────────────────────────────────────────────────────


async def check_robots_txt(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    fetcher: Optional[RefusalFetcher] = None,
    timeout_s: float = DEFAULT_REFUSAL_FETCH_TIMEOUT_S,
) -> Optional[str]:
    """Return a refusal reason string if ``robots.txt`` blocks ``url``,
    else ``None``.

    Three-stage evaluation:

        1. Fetch ``{origin}/robots.txt`` (one round trip, ``timeout_s``).
        2. If the file 404s / 5xxes / fails network: treat as "no
           directive" and return ``None`` — RFC 9309 explicitly allows
           clients to assume "fetch allowed" when the file is missing.
           A non-200/404 response (e.g. 503) is logged but not treated
           as a refusal because it would let a flaky upstream defeat
           cloning entirely.
        3. Parse + evaluate against ``user_agent`` AND every UA in
           :data:`AI_BOT_USER_AGENTS` (any of those being blocked is
           equivalent to "the publisher said no AI scraping").

    The block reason includes the matching UA so the W11.12 audit log
    can record exactly which directive fired.
    """
    fetcher = fetcher or _HttpxFetcher()

    origin = _origin_of(url)
    robots_url = origin + ROBOTS_TXT_PATH

    body, status = await _fetch_optional(fetcher, robots_url, timeout_s=timeout_s)
    if body is None:
        return None  # Network / non-200 → treat as no opt-out (RFC 9309 §2.3.1)
    text = _bytes_to_text(body)
    if not text.strip():
        return None

    parser = _make_robotparser(text, robots_url)

    # Evaluate against the cloner's UA + every AI-bot UA. Returning False
    # for any of them is the publisher saying "no". Stop at the first
    # match so the audit log gets a single deterministic reason.
    canonical_target = normalize_url(url)
    for ua in (_ua_token(user_agent), *AI_BOT_USER_AGENTS):
        if not ua:
            continue
        if not parser.can_fetch(ua, canonical_target):
            return f"robots.txt disallows User-agent {ua!r} at this URL"

    return None


async def check_ai_txt(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    fetcher: Optional[RefusalFetcher] = None,
    timeout_s: float = DEFAULT_REFUSAL_FETCH_TIMEOUT_S,
) -> Optional[str]:
    """Return a refusal reason if ``ai.txt`` blocks ``url``, else ``None``.

    Tries every path in :data:`AI_TXT_PATHS` (``/.well-known/ai.txt``
    first, then ``/ai.txt``). Stops on the first 200 — short-circuits
    the second fetch if the canonical well-known path is served.

    Parses the file with the same ``RobotFileParser`` we use for
    ``robots.txt``: spawning.ai's spec re-uses the robots.txt grammar
    (``User-agent:`` / ``Disallow:`` / ``Allow:``) so this works.
    """
    fetcher = fetcher or _HttpxFetcher()

    origin = _origin_of(url)
    canonical_target = normalize_url(url)

    for path in AI_TXT_PATHS:
        ai_url = origin + path
        body, _status = await _fetch_optional(fetcher, ai_url, timeout_s=timeout_s)
        if body is None:
            continue
        text = _bytes_to_text(body)
        if not text.strip():
            continue

        parser = _make_robotparser(text, ai_url)
        for ua in (_ua_token(user_agent), *AI_BOT_USER_AGENTS):
            if not ua:
                continue
            if not parser.can_fetch(ua, canonical_target):
                return f"ai.txt ({path}) disallows User-agent {ua!r}"
        # An ai.txt that exists and is silent on AI bots is treated as
        # *allowing* — be pragmatic, follow the file.
        return None

    return None  # Neither path served an ai.txt → no signal.


def check_meta_noai(html: str) -> Optional[str]:
    """Return a refusal reason if any ``<meta name="...">`` opt-out tag
    fires, else ``None``.

    Inspects every ``<meta>`` tag in ``html``. A meta tag fires when:

        * Its ``name`` (or ``http-equiv``) attribute is ``"robots"`` OR
          one of the AI-bot UA tokens in :data:`META_AI_BOT_NAMES`.
        * Its ``content`` attribute contains any token in
          :data:`META_NOAI_TOKENS` (matched case-insensitively).

    Returns the *first* matching tag's reason — short-circuits to give
    deterministic audit-log output.

    Tag-soup tolerant: each tag is parsed independently, so a malformed
    earlier tag doesn't suppress later ones.
    """
    if not isinstance(html, str) or not html:
        return None

    for tag_match in _META_TAG_RE.finditer(html):
        attr_blob = tag_match.group(1) or ""
        attrs: dict[str, str] = {}
        for m in _META_ATTR_RE.finditer(attr_blob):
            key = (m.group(1) or "").lower()
            val = m.group(2) or m.group(3) or m.group(4) or ""
            # Decode the most common HTML entity in attribute values
            # (``&amp;`` → ``&``); html.parser would do this for us in
            # the W11.3 walker but we're regex-matching here for speed.
            val = val.replace("&amp;", "&")
            attrs[key] = val

        name_ish = (attrs.get("name") or attrs.get("http-equiv") or "").lower()
        if not name_ish:
            continue
        if name_ish not in META_AI_BOT_NAMES:
            continue

        content = attrs.get("content") or ""
        for token in _split_directive_value(content):
            if token in META_NOAI_TOKENS:
                return (
                    f"meta tag <meta name={name_ish!r}> "
                    f"declares {token!r} (refuses AI scraping)"
                )

    return None


def check_x_robots_tag(headers: Mapping[str, str]) -> Optional[str]:
    """Return a refusal reason if the ``X-Robots-Tag`` header opts out,
    else ``None``.

    The header may be:

        * Comma-separated tokens (``noindex, noai``).
        * UA-prefixed (``GPTBot: noindex, noai``) — multiple stacks per
          response, separated by commas. We honour the prefix only if
          it matches our UA *or* an AI-bot UA in :data:`AI_BOT_USER_AGENTS`.
        * Repeated header (httpx flattens with ``\\n``); we coalesce.

    Any single fired ``noai`` / ``noimageai`` / ``none`` is enough.
    """
    if not headers:
        return None

    # Lower-case header lookup — both _HttpxFetcher and the W11.2
    # backends normalise keys, but be defensive against a caller that
    # didn't.
    raw: Optional[str] = None
    for k, v in headers.items():
        if k and k.lower() == "x-robots-tag":
            raw = v
            break
    if not raw:
        return None

    # Split on both '\n' (multi-header coalesce) and ','.
    pieces: list[str] = []
    for line in raw.split("\n"):
        for piece in line.split(","):
            piece = piece.strip()
            if piece:
                pieces.append(piece)

    for piece in pieces:
        ua_scope = ""
        token_part = piece
        if ":" in piece:
            head, tail = piece.split(":", 1)
            ua_scope = head.strip().lower()
            token_part = tail.strip()
        if ua_scope and ua_scope not in META_AI_BOT_NAMES:
            # Scoped to a UA we don't claim to be. Skip.
            continue
        for token in _split_directive_value(token_part):
            if token in META_NOAI_TOKENS:
                ua_label = ua_scope or "*"
                return (
                    f"X-Robots-Tag header declares {token!r} "
                    f"for User-agent {ua_label!r}"
                )

    return None


def check_cloudflare_ai_block(
    *,
    status: int,
    headers: Mapping[str, str],
    body: bytes = b"",
) -> Optional[str]:
    """Return a refusal reason if the response looks like a Cloudflare
    AI-bot block, else ``None``.

    Two confidence tiers:

        * **High confidence** — ``cf-mitigated`` header set to one of
          :data:`CLOUDFLARE_MITIGATED_REFUSE_VALUES`. This is the
          explicit signal CF emits for managed-challenge / block.
        * **Lower confidence** — status ≥ 400, ``server: cloudflare``
          or ``cf-ray`` header present, AND the body contains one of
          :data:`CLOUDFLARE_AI_BLOCK_BODY_HINTS`. We require all three
          conjunctively because ``server: cloudflare`` alone matches
          every site behind CF; combining with the body hint reduces
          false positives to "page that talks about AI block" — which
          is rare enough that the trade-off is worth it.

    Lower-confidence detection is intentionally permissive on the safe
    side — we'd rather refuse a debatable response than fingerprint our
    way around a CF block.
    """
    if not headers:
        return None
    h = {k.lower(): v for k, v in headers.items()}
    mitigated = (h.get("cf-mitigated") or "").strip().lower()
    if mitigated and mitigated in CLOUDFLARE_MITIGATED_REFUSE_VALUES:
        return f"Cloudflare emitted cf-mitigated={mitigated!r} (likely AI-bot rule)"

    if status < 400:
        return None

    server = (h.get("server") or "").strip().lower()
    cf_ray = (h.get("cf-ray") or "").strip()
    if "cloudflare" not in server and not cf_ray:
        return None

    body_lower = (body or b"").decode("utf-8", errors="replace").lower()[:8192]
    for hint in CLOUDFLARE_AI_BLOCK_BODY_HINTS:
        if hint in body_lower:
            return (
                f"Cloudflare returned HTTP {status} with body matching "
                f"{hint!r} (likely AI-bot block)"
            )

    return None


# ── Combined entry points ────────────────────────────────────────────────


async def check_machine_refusal_pre_capture(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    fetcher: Optional[RefusalFetcher] = None,
    timeout_s: float = DEFAULT_REFUSAL_FETCH_TIMEOUT_S,
) -> RefusalDecision:
    """Run every *pre*-capture L1 check (robots.txt + ai.txt).

    Cheap to run — at most two HTTP round trips to small files. Designed
    to be called *before* invoking ``clone_site()`` so a refused URL
    never burns Firecrawl quota / Playwright session time.

    Both checks run concurrently via ``asyncio.gather`` (refusal-fetcher
    instances are one-shot and side-effect free, so concurrent use is
    safe). Either firing fails the decision; both clean → ``allowed=True``.
    """
    fetcher = fetcher or _HttpxFetcher()

    robots_task = asyncio.create_task(
        check_robots_txt(
            url, user_agent=user_agent, fetcher=fetcher, timeout_s=timeout_s
        )
    )
    aitxt_task = asyncio.create_task(
        check_ai_txt(
            url, user_agent=user_agent, fetcher=fetcher, timeout_s=timeout_s
        )
    )
    robots_reason, ai_reason = await asyncio.gather(robots_task, aitxt_task)

    signals: list[str] = ["robots.txt", "ai.txt"]
    reasons: list[str] = []
    details: dict[str, str] = {}
    if robots_reason:
        reasons.append(robots_reason)
        details["robots.txt"] = robots_reason
    if ai_reason:
        reasons.append(ai_reason)
        details["ai.txt"] = ai_reason

    return RefusalDecision(
        allowed=not reasons,
        signals_checked=tuple(signals),
        reasons=tuple(reasons),
        details=details,
    )


def check_machine_refusal_post_capture(
    capture: RawCapture,
) -> RefusalDecision:
    """Run every *post*-capture L1 check on a ``RawCapture``.

    Three checks in order:

        1. ``X-Robots-Tag`` header (cheapest — header dict lookup).
        2. ``<meta name="robots" content="noai">`` family in HTML.
        3. Cloudflare AI-bot block heuristic on (status, headers, body).

    Pure / synchronous — every input is already in memory at this point
    and we just want a deterministic decision the audit log can pin.
    """
    if not isinstance(capture, RawCapture):
        raise SiteClonerError(
            f"check_machine_refusal_post_capture requires RawCapture, "
            f"got {type(capture).__name__}"
        )

    signals: list[str] = []
    reasons: list[str] = []
    details: dict[str, str] = {}

    signals.append("header.x-robots-tag")
    xrt_reason = check_x_robots_tag(capture.headers)
    if xrt_reason:
        reasons.append(xrt_reason)
        details["header.x-robots-tag"] = xrt_reason

    signals.append("meta.noai")
    meta_reason = check_meta_noai(capture.html)
    if meta_reason:
        reasons.append(meta_reason)
        details["meta.noai"] = meta_reason

    signals.append("cloudflare.ai-block")
    cf_reason = check_cloudflare_ai_block(
        status=int(capture.status_code),
        headers=capture.headers,
        body=capture.html.encode("utf-8", errors="ignore")[:8192],
    )
    if cf_reason:
        reasons.append(cf_reason)
        details["cloudflare.ai-block"] = cf_reason

    return RefusalDecision(
        allowed=not reasons,
        signals_checked=tuple(signals),
        reasons=tuple(reasons),
        details=details,
    )


def merge_refusal_decisions(*decisions: RefusalDecision) -> RefusalDecision:
    """Combine several ``RefusalDecision`` objects into one.

    The merged result:
        * ``allowed`` is the AND of every input's ``allowed``.
        * ``signals_checked`` is the deterministic union (insertion order
          preserved, duplicates dropped).
        * ``reasons`` is the concatenation in input order.
        * ``details`` is the merge — later-decision entries shadow
          earlier ones for the same key.
    """
    if not decisions:
        return RefusalDecision(
            allowed=True, signals_checked=(), reasons=(), details={}
        )
    seen_signals: dict[str, None] = {}
    reasons: list[str] = []
    details: dict[str, str] = {}
    allowed = True
    for d in decisions:
        if not isinstance(d, RefusalDecision):
            raise TypeError(
                f"merge_refusal_decisions expected RefusalDecision, "
                f"got {type(d).__name__}"
            )
        allowed = allowed and d.allowed
        for s in d.signals_checked:
            seen_signals.setdefault(s)
        reasons.extend(d.reasons)
        details.update(d.details)
    return RefusalDecision(
        allowed=allowed,
        signals_checked=tuple(seen_signals.keys()),
        reasons=tuple(reasons),
        details=details,
    )


async def assert_clone_allowed_pre_capture(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    fetcher: Optional[RefusalFetcher] = None,
    timeout_s: float = DEFAULT_REFUSAL_FETCH_TIMEOUT_S,
) -> RefusalDecision:
    """Convenience wrapper: pre-capture check + raise on refusal.

    Routers that want a one-line "gate before clone_site()" call this:

        decision = await assert_clone_allowed_pre_capture(url)
        capture = await source.capture(url, ...)
        ...

    Returns the (allowed) ``RefusalDecision`` so the caller can attach
    ``signals_checked`` to the audit log even on success — that's how
    the W11.12 audit row knows L1 actually ran.
    """
    decision = await check_machine_refusal_pre_capture(
        url, user_agent=user_agent, fetcher=fetcher, timeout_s=timeout_s
    )
    if decision.refused:
        raise MachineRefusedError(decision, url=url)
    return decision


def assert_clone_allowed_post_capture(capture: RawCapture) -> RefusalDecision:
    """Convenience wrapper: post-capture check + raise on refusal."""
    decision = check_machine_refusal_post_capture(capture)
    if decision.refused:
        raise MachineRefusedError(decision, url=capture.url)
    return decision


# ── Internals ────────────────────────────────────────────────────────────


async def _fetch_optional(
    fetcher: RefusalFetcher,
    url: str,
    *,
    timeout_s: float,
) -> tuple[Optional[bytes], Optional[int]]:
    """Fetch ``url`` and return ``(body, status)``.

    Returns ``(None, None)`` for any of:

        * Transport-level failure (``SiteClonerError`` raised by the
          fetcher).
        * Non-200 / non-204 / non-206 status.
        * Empty body.

    The contract is "no signal" — ``robots.txt`` / ``ai.txt`` are *opt*-
    out files; the absence of a file means "no opt-out", which is
    operationally indistinguishable from a missing / unreachable file.
    """
    try:
        result = await fetcher(
            url,
            timeout_s=timeout_s,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # Don't let a slow / cancelled robots.txt fetch propagate up.
        # Cancellation is rare here but we keep the loop responsive.
        return None, None
    except SiteClonerError as e:
        logger.debug("refusal_signals fetch %r failed: %s", url, e)
        return None, None
    except Exception as e:  # pragma: no cover — defensive belt
        logger.debug("refusal_signals fetch %r unexpected error: %s", url, e)
        return None, None

    status = int(getattr(result, "status", 0) or 0)
    body = bytes(getattr(result, "body", b"") or b"")
    if not (200 <= status < 300):
        return None, status
    if not body:
        return None, status
    return body, status


__all__ = [
    "AI_BOT_USER_AGENTS",
    "AI_TXT_PATHS",
    "CLOUDFLARE_AI_BLOCK_BODY_HINTS",
    "CLOUDFLARE_MITIGATED_REFUSE_VALUES",
    "DEFAULT_REFUSAL_FETCH_MAX_BYTES",
    "DEFAULT_REFUSAL_FETCH_TIMEOUT_S",
    "DEFAULT_USER_AGENT",
    "MachineRefusedError",
    "META_AI_BOT_NAMES",
    "META_NOAI_TOKENS",
    "RefusalDecision",
    "RefusalFetchResult",
    "RefusalFetcher",
    "ROBOTS_TXT_PATH",
    "assert_clone_allowed_post_capture",
    "assert_clone_allowed_pre_capture",
    "check_ai_txt",
    "check_cloudflare_ai_block",
    "check_machine_refusal_post_capture",
    "check_machine_refusal_pre_capture",
    "check_meta_noai",
    "check_robots_txt",
    "check_x_robots_tag",
    "default_refusal_fetcher",
    "merge_refusal_decisions",
]
