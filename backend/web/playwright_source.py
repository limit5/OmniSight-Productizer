"""W11.2 #XXX — Self-hosted Playwright ``CloneSource`` backend.

Adapter that satisfies the W11.1 ``CloneSource`` Protocol by driving a
local headless browser via the ``playwright`` python package. This is
the **air-gap mandatory** backend — required for deployments that
cannot egress to ``api.firecrawl.dev`` (regulated / on-prem /
classified). Pairs with ``backend.web.firecrawl_source.FirecrawlSource``;
both speak the same protocol so the rest of the W11 pipeline doesn't
care which backend ran.

Air-gap operator setup
----------------------
Playwright is **not** in ``backend/requirements.in`` — adding it would
bake ~50 MB of browser binaries into every image, regardless of whether
the deployment ever clones a site. Instead, operators that need this
backend run::

    pip install playwright
    playwright install chromium

inside the backend container (or pre-bake into a Dockerfile derivative).
The backend then:

    * imports ``playwright.async_api`` lazily on first ``capture()`` call
    * raises ``PlaywrightDependencyError`` with the install hint if the
      package is missing.

Module-global state audit (SOP §1)
----------------------------------
``PlaywrightSource`` carries a per-*instance* lazily-instantiated
``Browser`` reference; there is **no** module-level mutable state.
Cross-worker consistency: trivially answer #1 — every uvicorn worker
spins up its own browser process when first asked to clone, and the
browser binary on disk is read-only. There is no shared cache, no
shared cookie jar, no shared rate-limit bucket here. The W11.8 row
owns rate limiting at the orchestrator layer.

Inspired by firecrawl/open-lovable (MIT). Attribution + license text
land alongside the W11.13 row in ``LICENSES/open-lovable-mit.txt``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

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
PLAYWRIGHT_BACKEND_NAME: str = "playwright"

#: Browsers Playwright supports. We pin chromium as the default — it
#: matches Firecrawl's underlying renderer the closest, which keeps
#: cross-backend snapshot diff (W11.11) small. Firefox / WebKit are
#: opt-in for operators that want to stress-test alternative engines.
SUPPORTED_BROWSERS: frozenset[str] = frozenset({"chromium", "firefox", "webkit"})

#: Default browser. Operators flip via ``OMNISIGHT_PLAYWRIGHT_BROWSER``
#: or constructor arg. Chromium is the safest default — biggest engine
#: parity with the SaaS option.
DEFAULT_BROWSER: str = "chromium"

#: Headroom we keep below the caller's ``timeout_s`` to give the page
#: a chance to surface a typed Playwright error (TimeoutError) before
#: the orchestrator's outer ``asyncio.wait_for`` triggers. Without this,
#: every overrun looks like a generic CancelledError and we lose the
#: failure-mode signal in audit logs.
TIMEOUT_INTERNAL_HEADROOM_S: float = 1.0

#: ``page.goto`` ``wait_until`` argument. ``"networkidle"`` waits for
#: the network to go quiet for 500 ms, which gives modern SPAs enough
#: time to hydrate without waiting forever for analytics beacons. The
#: per-call ``timeout_s`` still bounds the whole operation.
DEFAULT_WAIT_UNTIL: str = "networkidle"


# ── Errors ────────────────────────────────────────────────────────────

class PlaywrightDependencyError(SiteClonerError):
    """The ``playwright`` python package is not installed (or the
    browser binary is not installed). Carries the canonical install
    instructions in ``args[0]`` so an operator hitting this error in a
    log can copy/paste the fix.

    This is **expected** for stock OmniSight images that haven't opted
    into the air-gap clone path — production stacks that don't need
    self-host clone capability simply don't see this error because they
    keep ``OMNISIGHT_CLONE_BACKEND=firecrawl``.
    """


class PlaywrightConfigError(SiteClonerError):
    """``PlaywrightSource`` was constructed with an unsupported browser
    or other invalid config. Distinct from ``PlaywrightDependencyError``
    so misconfigurations don't get misattributed to "playwright not
    installed"."""


# ── Backend ───────────────────────────────────────────────────────────

class PlaywrightSource:
    """``CloneSource`` adapter that drives a local Playwright browser.

    Construction (CLI / direct use):

        >>> src = PlaywrightSource()
        >>> async with src:
        ...     spec = await clone_site("https://example.com", source=src)

    Construction (test / DI):

        >>> # Inject a fully-mocked playwright object that quacks like
        >>> # ``playwright.async_api.async_playwright()`` so tests don't
        >>> # need a real browser binary.
        >>> src = PlaywrightSource(playwright_factory=lambda: mock)

    The backend amortises browser-startup cost across calls — the first
    ``capture()`` launches the browser, subsequent ``capture()`` calls
    reuse it. ``aclose()`` (or ``async with``) tears it down.

    Air-gap reminder: Playwright reaches out to download browser
    binaries on first ``playwright install``. That step happens at
    *operator setup* time on the open internet. Once binaries are on
    disk, the runtime path needs zero outbound network *to playwright
    infrastructure* — only to the target URL the operator asked us to
    clone, which is by definition a fetch they wanted.
    """

    name: str = PLAYWRIGHT_BACKEND_NAME

    def __init__(
        self,
        *,
        browser: Optional[str] = None,
        playwright_factory: Optional[Any] = None,
        launch_args: Optional[list[str]] = None,
    ) -> None:
        """Construct a Playwright-backed clone source.

        Args:
            browser: One of ``SUPPORTED_BROWSERS``. Defaults to
                ``DEFAULT_BROWSER`` (chromium). Falls back to the
                ``OMNISIGHT_PLAYWRIGHT_BROWSER`` env var when not
                supplied.
            playwright_factory: A callable returning a context-manager-
                like object compatible with
                ``playwright.async_api.async_playwright()``. Used by
                tests to inject a mock; production callers leave this
                ``None`` and the backend imports playwright lazily.
            launch_args: Extra ``--flag`` strings passed to
                ``browser_type.launch(args=...)``. Defaults to a hardened
                set: ``--no-sandbox`` (required inside Docker without
                cap-add), ``--disable-dev-shm-usage`` (avoid the famous
                /dev/shm 64MB limit), and ``--disable-gpu``. Operators
                that bake a custom Chromium can override.
        """
        import os  # local — settings read happens at construct time

        resolved_browser = (
            browser
            or os.environ.get("OMNISIGHT_PLAYWRIGHT_BROWSER", "")
            or DEFAULT_BROWSER
        )
        resolved_browser = resolved_browser.strip().lower()
        if resolved_browser not in SUPPORTED_BROWSERS:
            raise PlaywrightConfigError(
                f"unsupported playwright browser {resolved_browser!r}; "
                f"expected one of {sorted(SUPPORTED_BROWSERS)}"
            )

        self._browser_name: str = resolved_browser
        self._playwright_factory: Optional[Any] = playwright_factory
        self._launch_args: list[str] = list(launch_args) if launch_args is not None else [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]

        # Cached references — lazily filled on first capture().
        self._pw_ctx: Any = None     # async_playwright() async context-manager
        self._pw: Any = None         # the entered playwright object
        self._browser: Any = None    # the launched Browser

    # -- Public surface ----------------------------------------------------

    async def capture(
        self,
        url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    ) -> RawCapture:
        """Render ``url`` in the local browser and return a
        ``RawCapture``.

        ``url`` is the canonical, validated URL the W11.1 orchestrator
        already gated through ``validate_clone_url`` — this backend
        does NOT re-run SSRF checks. It does, however, refuse to render
        any non-http(s) URL just in case (defence-in-depth).

        Returns:
            ``RawCapture`` with ``backend="playwright"``, the
            post-redirect URL the browser ended on, the rendered HTML
            (post-JS execution), the HTTP status code of the *main
            document* response, the deduped asset URLs the page
            requested, and the main-document response headers.

        Raises:
            CloneCaptureTimeoutError: ``timeout_s`` elapsed before the
                page reached ``DEFAULT_WAIT_UNTIL``. Translates to
                HTTP 504 at the router layer.
            CloneSourceError: every other failure (browser launch
                failure, page navigation failure, payload too large).
                ``__cause__`` carries the underlying exception when
                applicable.
        """
        if not (isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))):
            raise CloneSourceError(
                f"playwright capture refusing non-http(s) URL: {url!r}"
            )

        browser = await self._ensure_browser()

        # Sub-second headroom keeps the playwright-side timeout below
        # the orchestrator-side outer timeout so playwright surfaces a
        # typed TimeoutError instead of getting cancelled mid-flight.
        internal_timeout = max(0.5, float(timeout_s) - TIMEOUT_INTERNAL_HEADROOM_S)
        # Playwright takes timeouts in milliseconds.
        nav_timeout_ms = int(internal_timeout * 1000)

        # Asset URL collector — populated by the response listener.
        # Kept as a list (then deduped) to preserve discovery order so
        # downstream W11.6 transformer can keep stable ordering across
        # runs.
        asset_urls: list[str] = []
        seen_assets: set[str] = set()

        context: Any = None
        page: Any = None

        try:
            context = await browser.new_context(
                user_agent="OmniSight-Productizer/W11.2-Playwright",
                # Don't follow the operator's locale — pages serve
                # localised content otherwise and W11 cloning is meant
                # to capture the canonical English version unless the
                # caller pins one explicitly (deferred to W11.9).
                locale="en-US",
                # No service-worker — they often install bot-traps that
                # never settle network-idle.
                service_workers="block",
                # Strict so we never accidentally accept a self-signed
                # cert. The orchestrator already validated the URL is
                # public; if a public host has a broken cert it's the
                # operator's choice to ignore (and we don't expose that
                # opt-out at the W11 layer).
                ignore_https_errors=False,
            )

            page = await context.new_page()

            def _on_response(response: Any) -> None:
                # Best-effort URL collector — never raise out of the
                # listener (would unwind the playwright internals).
                try:
                    rurl = response.url
                except Exception:
                    return
                if not isinstance(rurl, str) or not rurl:
                    return
                if rurl == url:
                    return  # main document — captured separately
                if rurl in seen_assets:
                    return
                seen_assets.add(rurl)
                asset_urls.append(rurl)

            page.on("response", _on_response)

            try:
                response = await page.goto(
                    url,
                    timeout=nav_timeout_ms,
                    wait_until=DEFAULT_WAIT_UNTIL,
                )
            except Exception as e:
                ename = type(e).__name__
                if "TimeoutError" in ename:
                    raise CloneCaptureTimeoutError(
                        f"playwright goto exceeded internal timeout "
                        f"{internal_timeout:.2f}s for {url!r}"
                    ) from e
                raise CloneSourceError(
                    f"playwright goto failed ({ename}) for {url!r}: {e!s}"
                ) from e

            if response is None:
                raise CloneSourceError(
                    f"playwright goto returned no response for {url!r}"
                )

            html_value: str = await page.content()
            if not isinstance(html_value, str) or not html_value:
                raise CloneSourceError(
                    f"playwright capture returned empty html for {url!r}"
                )

            # Hard size cap — refuse to materialise an oversize blob
            # (the orchestrator repeats this check post-return as a
            # belt-and-braces safety net).
            if len(html_value.encode("utf-8", errors="ignore")) > int(max_html_bytes):
                raise CloneSourceError(
                    f"playwright capture returned html exceeding "
                    f"max_html_bytes={max_html_bytes} for {url!r}"
                )

            try:
                status_code = int(response.status)
            except Exception:
                status_code = 200  # response existed; default optimistic

            # Main-document response headers, lower-cased keys (W11.4
            # ai.txt / X-Robots-Tag check assumes lower-case).
            response_headers: dict[str, str] = {}
            try:
                hdrs = await response.all_headers()
            except Exception:
                try:
                    hdrs = response.headers  # property fallback
                except Exception:
                    hdrs = {}
            if isinstance(hdrs, dict):
                for k, v in hdrs.items():
                    if isinstance(k, str) and isinstance(v, (str, int, float)):
                        response_headers[k.lower()] = str(v)

            # Final URL after redirects (page.url tracks redirects in
            # the navigation chain).
            try:
                post_redirect_url = page.url or url
            except Exception:
                post_redirect_url = url
            if not isinstance(post_redirect_url, str) or not post_redirect_url:
                post_redirect_url = url

            return RawCapture(
                url=post_redirect_url,
                html=html_value,
                status_code=status_code,
                fetched_at=_utc_iso8601_now(),
                backend=PLAYWRIGHT_BACKEND_NAME,
                asset_urls=tuple(asset_urls),
                headers=response_headers,
            )

        finally:
            # Always tear down the page + context; keep the browser
            # alive for reuse on subsequent captures (browser launch
            # is the heavy cost; per-page is cheap). The browser is
            # torn down by ``aclose()`` / ``__aexit__``.
            for _ref in (page, context):
                if _ref is None:
                    continue
                try:
                    await _ref.close()
                except Exception as e:
                    logger.debug(
                        "playwright cleanup ignored on %s: %s",
                        type(_ref).__name__, e,
                    )

    async def aclose(self) -> None:
        """Tear down the launched browser + playwright context. Safe
        to call multiple times. After ``aclose()``, a subsequent
        ``capture()`` re-launches the browser."""
        # Order matters — browser first, then the playwright context.
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:  # pragma: no cover — diagnostic only
                logger.debug("playwright browser close ignored: %s", e)
            self._browser = None

        if self._pw_ctx is not None:
            try:
                await self._pw_ctx.__aexit__(None, None, None)
            except Exception as e:  # pragma: no cover — diagnostic only
                logger.debug("playwright context aexit ignored: %s", e)
            self._pw_ctx = None
            self._pw = None

    async def __aenter__(self) -> "PlaywrightSource":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- Internals ---------------------------------------------------------

    async def _ensure_browser(self) -> Any:
        """Return the launched ``Browser`` (lazily launching on first
        use). Lazy because the orchestrator only spins up the
        playwright stack when a clone request actually arrives — boot
        path stays fast for stacks that never clone.
        """
        if self._browser is not None:
            return self._browser

        factory = self._playwright_factory
        if factory is None:
            try:
                from playwright.async_api import async_playwright  # noqa: PLC0415
            except Exception as e:
                raise PlaywrightDependencyError(
                    "playwright is not installed. Air-gap operators run: "
                    "`pip install playwright && playwright install chromium`"
                ) from e
            factory = async_playwright

        try:
            self._pw_ctx = factory()
            self._pw = await self._pw_ctx.__aenter__()
        except Exception as e:
            raise CloneSourceError(
                f"playwright bootstrap failed: {type(e).__name__}: {e!s}"
            ) from e

        try:
            browser_type = getattr(self._pw, self._browser_name)
        except AttributeError as e:
            raise PlaywrightConfigError(
                f"playwright object has no browser attribute "
                f"{self._browser_name!r}"
            ) from e

        try:
            self._browser = await browser_type.launch(
                headless=True,
                args=list(self._launch_args),
            )
        except Exception as e:
            # Surface a typed dependency error if the binary itself is
            # absent — the most common operator failure mode.
            ename = type(e).__name__
            msg = str(e)
            if "Executable doesn't exist" in msg or "BrowserType.launch" in msg:
                raise PlaywrightDependencyError(
                    f"playwright browser binary for {self._browser_name!r} "
                    "missing. Run: "
                    f"`playwright install {self._browser_name}`"
                ) from e
            raise CloneSourceError(
                f"playwright browser launch failed ({ename}): {msg}"
            ) from e

        return self._browser


# ── Helpers ───────────────────────────────────────────────────────────

def _utc_iso8601_now() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z``
    suffix. Pinned format because the W11.7 manifest spec mandates it."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = [
    "DEFAULT_BROWSER",
    "DEFAULT_WAIT_UNTIL",
    "PLAYWRIGHT_BACKEND_NAME",
    "PlaywrightConfigError",
    "PlaywrightDependencyError",
    "PlaywrightSource",
    "SUPPORTED_BROWSERS",
    "TIMEOUT_INTERNAL_HEADROOM_S",
]
