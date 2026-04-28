"""W11.2 #XXX — Firecrawl SaaS ``CloneSource`` backend.

Adapter that satisfies the W11.1 ``CloneSource`` Protocol by delegating
the actual fetch to the Firecrawl SaaS API
(``https://api.firecrawl.dev/v1/scrape``). Pairs with
``backend.web.playwright_source.PlaywrightSource`` (self-host, air-gap
mandatory) so operators can pick the backend that matches their
deployment posture:

    * **Firecrawl SaaS** — fastest path, zero infra, charged per call.
      Required: ``OMNISIGHT_FIRECRAWL_API_KEY``.
    * **Playwright self-host** — air-gap-friendly, no third-party data
      egress, requires ``playwright`` python package + browser binary
      pre-installed by the operator.

Why a SaaS option at all
------------------------
Firecrawl handles JS-rendering, bot-evasion, and HTTP retry semantics
better than a roll-your-own Playwright runner does, *and* keeps the
OmniSight production stack thin (no headless Chromium baked into the
backend image). Operators that can tolerate third-party fetch egress
should prefer it.

Why a self-host option at all
-----------------------------
Air-gapped customers (regulated / on-prem / classified deployments) cannot
hit ``api.firecrawl.dev`` at all. ``PlaywrightSource`` covers that case.
Both speak the W11.1 ``CloneSource`` Protocol so the rest of the W11
pipeline (W11.4 robots gate, W11.5 LLM classifier, W11.6 transformer,
W11.7 manifest, W11.8 rate limiter) is backend-agnostic.

Module-global state audit (SOP §1)
----------------------------------
``FirecrawlSource`` carries an optional ``httpx.AsyncClient`` per
*instance*; there is **no** module-level mutable state. Cross-worker
consistency: trivially answer #1 — every worker constructs its own
client from the same env-derived constants. The Firecrawl SaaS service
itself is the single source of truth for upstream rate-limit / quota.

Inspired by firecrawl/open-lovable (MIT). Attribution + license text
land in the W11.13 row alongside ``LICENSES/open-lovable-mit.txt``.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from backend.web.site_cloner import (
    CloneCaptureTimeoutError,
    CloneSourceError,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_TIMEOUT_S,
    RawCapture,
    SiteClonerError,
)

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

#: Stable identifier emitted into ``RawCapture.backend``. The W11.7
#: manifest pins this so operators can audit which backend produced a
#: given clone.
FIRECRAWL_BACKEND_NAME: str = "firecrawl"

#: Public Firecrawl SaaS endpoint. Operators running self-host Firecrawl
#: (rare; the OSS server exists but isn't the primary use case) point
#: the constructor at their own URL via ``base_url=...``.
DEFAULT_FIRECRAWL_BASE_URL: str = "https://api.firecrawl.dev"

#: Firecrawl ``/v1/scrape`` path. Pinned as a constant so a future
#: API-version bump only edits one site.
FIRECRAWL_SCRAPE_PATH: str = "/v1/scrape"

#: Headroom we keep below the caller's ``timeout_s`` to give the upstream
#: response time to surface before our outer ``asyncio.wait_for`` in the
#: orchestrator triggers. Without this, the orchestrator times out at
#: exactly ``timeout_s`` and the backend never gets to raise its own
#: typed timeout, which obscures the failure mode in audit logs.
TIMEOUT_INTERNAL_HEADROOM_S: float = 1.0


# ── Errors ────────────────────────────────────────────────────────────

class FirecrawlConfigError(SiteClonerError):
    """Firecrawl backend was constructed without a usable API key (and
    no ``OMNISIGHT_FIRECRAWL_API_KEY`` env var was set). Distinct from
    ``CloneSourceError`` because the orchestrator can fall back to the
    Playwright self-host backend in this case rather than reporting
    "capture failed"."""


class FirecrawlDependencyError(SiteClonerError):
    """``httpx`` (the HTTP client this backend uses) is not importable.
    httpx ships in production ``requirements.in`` so this is a build-
    image regression rather than a runtime config issue. Air-gapped
    deployments that strip httpx for size should switch to Playwright
    via ``OMNISIGHT_CLONE_BACKEND=playwright``."""


# ── Backend ───────────────────────────────────────────────────────────

class FirecrawlSource:
    """``CloneSource`` adapter that delegates to Firecrawl SaaS.

    Construction options (CLI use):

        >>> src = FirecrawlSource(api_key="fc-...")

    Construction options (env-driven, production):

        >>> src = FirecrawlSource()  # reads OMNISIGHT_FIRECRAWL_API_KEY

    Per-call usage runs through the W11.1 orchestrator:

        >>> from backend.web import clone_site
        >>> spec = await clone_site("https://example.com", source=src)

    Airlock note
    ------------
    Calling this backend exits the OmniSight perimeter (the URL + any
    cookies / referers Firecrawl sees are visible to a third party).
    Air-gapped tenants MUST use ``PlaywrightSource`` instead — there is
    no "lite" mode that keeps the URL secret while still using the SaaS.
    """

    name: str = FIRECRAWL_BACKEND_NAME

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        """Construct a Firecrawl-backed clone source.

        Args:
            api_key: Firecrawl API key. Falls back to
                ``OMNISIGHT_FIRECRAWL_API_KEY`` env var when ``None``.
                Empty / missing → ``FirecrawlConfigError`` at construct
                time (so misconfigured deployments fail fast at boot,
                not on first clone request).
            base_url: Override the Firecrawl base URL. Useful for
                operators running the Firecrawl OSS server in a private
                network. Defaults to ``DEFAULT_FIRECRAWL_BASE_URL``.
                Falls back to ``OMNISIGHT_FIRECRAWL_BASE_URL`` env var.
            client: An ``httpx.AsyncClient``-compatible object — used
                by tests + DI. Production callers leave this ``None``
                and the backend constructs one lazily on first use so
                import does not require httpx, only call does.
        """
        resolved_key = api_key if api_key is not None else os.environ.get(
            "OMNISIGHT_FIRECRAWL_API_KEY", ""
        )
        if not (isinstance(resolved_key, str) and resolved_key.strip()):
            raise FirecrawlConfigError(
                "FirecrawlSource requires an api_key (or "
                "OMNISIGHT_FIRECRAWL_API_KEY env var)"
            )

        resolved_base = base_url if base_url is not None else os.environ.get(
            "OMNISIGHT_FIRECRAWL_BASE_URL", DEFAULT_FIRECRAWL_BASE_URL
        )
        if not (isinstance(resolved_base, str) and resolved_base.strip()):
            resolved_base = DEFAULT_FIRECRAWL_BASE_URL

        self._api_key: str = resolved_key.strip()
        # Strip trailing slashes so we always join with FIRECRAWL_SCRAPE_PATH
        # cleanly regardless of how the operator typed the URL.
        self._base_url: str = resolved_base.strip().rstrip("/")
        self._client: Optional[Any] = client  # async http client (httpx.AsyncClient)
        self._owns_client: bool = client is None

    # -- Public surface ----------------------------------------------------

    async def capture(
        self,
        url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    ) -> RawCapture:
        """Run a Firecrawl ``/v1/scrape`` against ``url``.

        ``url`` is the canonical, validated URL the W11.1 orchestrator
        already gated through ``validate_clone_url`` — this backend
        does NOT re-run SSRF checks (it merely forwards the URL to
        Firecrawl). Re-validating would create a divergence point
        between backends; SSRF is the orchestrator's concern.

        Returns:
            ``RawCapture`` with ``backend="firecrawl"``, the
            post-redirect URL Firecrawl actually fetched, the rendered
            HTML, the HTTP status code Firecrawl observed, the asset
            URLs (links / images) Firecrawl extracted, and the response
            headers.

        Raises:
            CloneCaptureTimeoutError: outer ``timeout_s`` elapsed before
                Firecrawl returned. The orchestrator translates to
                HTTP 504.
            CloneSourceError: every other failure (HTTP error from
                Firecrawl, malformed JSON response, payload too large,
                missing fields). ``__cause__`` carries the underlying
                exception when applicable.
        """
        client = await self._get_client(timeout_s=timeout_s)

        # Headroom keeps our internal request timeout slightly below the
        # caller's overall budget so Firecrawl gets a chance to surface
        # its own typed error before the orchestrator's outer
        # asyncio.wait_for forces a generic CancelledError.
        internal_timeout = max(0.5, float(timeout_s) - TIMEOUT_INTERNAL_HEADROOM_S)
        # Firecrawl expresses timeouts in *milliseconds* in their
        # request body — translate once at the boundary.
        firecrawl_timeout_ms = int(internal_timeout * 1000)

        payload: dict[str, Any] = {
            "url": url,
            # The W11 pipeline only consumes html (raw rendered) +
            # links (asset_urls). Asking for more wastes Firecrawl
            # quota for fields we'd discard. ``rawHtml`` is on this
            # list (rather than ``html``) because Firecrawl's
            # post-processed ``html`` strips inline scripts/styles —
            # we want the source-of-truth markup so W11.3 can parse
            # color tokens and font-family declarations from it.
            "formats": ["rawHtml", "links"],
            "timeout": firecrawl_timeout_ms,
            # We never render server-side assets; W11.6 L3 mandates
            # placeholder substitution so there's no point pulling the
            # bytes back through the SaaS.
            "skipTlsVerification": False,
            # Force a desktop UA — most landing pages serve a bigger,
            # more spec-friendly DOM to desktop than to mobile and the
            # W11 pipeline is desktop-first. ``W13`` covers
            # multi-breakpoint capture.
            "mobile": False,
        }

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OmniSight-Productizer/W11.2-Firecrawl",
        }

        endpoint = f"{self._base_url}{FIRECRAWL_SCRAPE_PATH}"

        try:
            resp = await client.post(
                endpoint,
                content=_json.dumps(payload).encode("utf-8"),
                headers=headers,
                timeout=internal_timeout,
            )
        except asyncio.TimeoutError as e:
            raise CloneCaptureTimeoutError(
                f"firecrawl scrape exceeded internal timeout "
                f"{internal_timeout:.2f}s for {url!r}"
            ) from e
        except Exception as e:
            # Catch httpx.TimeoutException + httpx.ConnectError +
            # httpx.HTTPError without importing httpx symbols at
            # module load time (lazy-import discipline).
            ename = type(e).__name__
            if "Timeout" in ename:
                raise CloneCaptureTimeoutError(
                    f"firecrawl scrape timed out ({ename}) for {url!r}"
                ) from e
            raise CloneSourceError(
                f"firecrawl scrape transport error ({ename}) for {url!r}: {e!s}"
            ) from e

        status = getattr(resp, "status_code", None)
        if status is None or not (200 <= int(status) < 300):
            # Body is small enough to surface — Firecrawl returns
            # JSON error envelopes, not multi-MB pages.
            text = ""
            try:
                text = resp.text  # type: ignore[attr-defined]
            except Exception:
                pass
            raise CloneSourceError(
                f"firecrawl scrape returned HTTP {status} for {url!r}: "
                f"{text[:512]!s}"
            )

        try:
            body = resp.json()
        except Exception as e:
            raise CloneSourceError(
                f"firecrawl scrape response was not JSON for {url!r}: {e!s}"
            ) from e

        if not isinstance(body, Mapping) or not body.get("success"):
            err = body.get("error") if isinstance(body, Mapping) else None
            raise CloneSourceError(
                f"firecrawl scrape reported failure for {url!r}: {err!r}"
            )

        data = body.get("data")
        if not isinstance(data, Mapping):
            raise CloneSourceError(
                f"firecrawl scrape returned no data envelope for {url!r}"
            )

        # Firecrawl emits both ``rawHtml`` and ``html`` (transformed); we
        # asked for ``rawHtml`` above so prefer that. Fall back to
        # ``html`` if the API omits ``rawHtml`` for some response shape
        # so the backend stays compatible with API versions that haven't
        # promoted ``rawHtml`` yet.
        html_value: Any = data.get("rawHtml")
        if not isinstance(html_value, str) or not html_value:
            html_value = data.get("html")
        if not isinstance(html_value, str) or not html_value:
            raise CloneSourceError(
                f"firecrawl scrape returned empty html for {url!r}"
            )

        # Hard size cap — refuse to materialise a multi-MB blob into a
        # ``RawCapture`` if the SaaS happened to ignore our timeout
        # request and pulled a giant page anyway. The orchestrator
        # repeats this check post-return; we duplicate it here so we
        # don't surface a giant payload at all.
        if len(html_value.encode("utf-8", errors="ignore")) > int(max_html_bytes):
            raise CloneSourceError(
                f"firecrawl scrape returned html exceeding "
                f"max_html_bytes={max_html_bytes} for {url!r}"
            )

        # ``links`` carries discovered URLs (anchors + image src + etc).
        # Filter to strings and dedupe while preserving order.
        raw_links = data.get("links") or []
        seen: set[str] = set()
        asset_urls: list[str] = []
        for link in raw_links:
            if isinstance(link, str) and link and link not in seen:
                seen.add(link)
                asset_urls.append(link)

        meta = data.get("metadata") or {}
        post_redirect_url = (
            meta.get("sourceURL") if isinstance(meta, Mapping) else None
        ) or (
            meta.get("url") if isinstance(meta, Mapping) else None
        ) or url
        if not isinstance(post_redirect_url, str) or not post_redirect_url:
            post_redirect_url = url

        observed_status_code = (
            meta.get("statusCode") if isinstance(meta, Mapping) else None
        )
        try:
            observed_status_code = int(observed_status_code)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # Firecrawl always *should* surface the upstream status code
            # in metadata; default to 200 only because the response
            # envelope already passed the success check above.
            observed_status_code = 200

        # Firecrawl returns response headers under ``metadata`` with
        # case-preserved keys — lower-case for ``RawCapture`` consumers
        # (W11.4 ai.txt / X-Robots-Tag check assumes lower-case keys).
        upstream_headers: dict[str, str] = {}
        meta_headers = meta.get("headers") if isinstance(meta, Mapping) else None
        if isinstance(meta_headers, Mapping):
            for k, v in meta_headers.items():
                if isinstance(k, str) and isinstance(v, (str, int, float)):
                    upstream_headers[k.lower()] = str(v)

        return RawCapture(
            url=post_redirect_url,
            html=html_value,
            status_code=int(observed_status_code),
            fetched_at=_utc_iso8601_now(),
            backend=FIRECRAWL_BACKEND_NAME,
            asset_urls=tuple(asset_urls),
            headers=upstream_headers,
        )

    async def aclose(self) -> None:
        """Release the underlying HTTP client. Safe to call multiple
        times. Tests that DI'd a client SHOULD NOT rely on this method
        to close it; the backend only closes clients it owns."""
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception as e:  # pragma: no cover — diagnostic only
                logger.debug("firecrawl client aclose ignored: %s", e)
        # Drop reference either way so reuse re-creates a fresh one.
        if self._owns_client:
            self._client = None

    async def __aenter__(self) -> "FirecrawlSource":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- Internals ---------------------------------------------------------

    async def _get_client(self, *, timeout_s: float) -> Any:
        """Return the underlying ``httpx.AsyncClient`` (creating one on
        first use). Lazy import isolates httpx absence from import
        time — that matters because tests construct ``FirecrawlSource``
        with ``client=...`` to bypass httpx entirely."""
        if self._client is not None:
            return self._client
        try:
            import httpx  # noqa: PLC0415  (lazy import is intentional)
        except Exception as e:  # pragma: no cover — httpx is a hard dep
            raise FirecrawlDependencyError(
                "httpx is required for FirecrawlSource but failed to import"
            ) from e

        # Default httpx timeout matches caller intent. Per-request
        # timeouts in ``capture`` override this anyway.
        self._client = httpx.AsyncClient(timeout=float(timeout_s))
        return self._client


# ── Helpers ───────────────────────────────────────────────────────────

def _utc_iso8601_now() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z``
    suffix. Pinned format because the W11.7 manifest spec mandates it."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = [
    "DEFAULT_FIRECRAWL_BASE_URL",
    "FIRECRAWL_BACKEND_NAME",
    "FIRECRAWL_SCRAPE_PATH",
    "FirecrawlConfigError",
    "FirecrawlDependencyError",
    "FirecrawlSource",
    "TIMEOUT_INTERNAL_HEADROOM_S",
]
