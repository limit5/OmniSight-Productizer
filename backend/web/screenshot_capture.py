"""W13.1 #XXX — Multi-breakpoint screenshot capture (Playwright multi-context).

Renders one URL across N viewports by giving each viewport its **own**
Playwright ``BrowserContext``, taking a full-page PNG inside that context,
then tearing the context down. Browser launch is amortised across the
whole call. The output is an ordered tuple of :class:`ViewportScreenshot`
records — raw PNG bytes plus per-viewport metadata.

Scope (this row only)
---------------------
This row ships the **multi-context engine**. It deliberately stops short of:

* W13.2 — locking the four production default breakpoints
  (375 / 768 / 1440 / 1920) into a public constant. The engine accepts
  *any* :class:`Viewport` list the caller supplies; defaults / custom-list
  policy belongs to the next row.
* W13.3 — writing PNGs to ``.omnisight/refs/{breakpoint}.png`` plus the
  ``manifest.json`` sidecar. This module returns bytes; the disk
  contract lives in the W13.3 writer alongside W11.7's
  ``write_manifest_file`` patterns.
* W13.4 — ghost-overlay diff against W14 live preview.
* W13.5 — the 5-URL × 4-breakpoint integration matrix that pins the
  full pipeline.

Each of those is a separate TODO row with its own commit. The W13.1
boundary keeps the engine reusable: a future "diff a single existing PNG
against live" tool can call :meth:`MultiContextScreenshotCapture.capture_multi`
with one viewport and never touch the W13.3 writer.

Why **multi-context**, not multi-page-in-one-context
----------------------------------------------------
``BrowserContext`` is Playwright's cookie / storage / cache isolation
boundary. Sharing a context across viewports means:

1. The first viewport's responsive-image cache (e.g. ``srcset`` resolved
   to the 375 px asset) sticks around in HTTP cache for the next viewport,
   so the 1440 px capture sees stale ``srcset`` resolution. This is
   exactly the bug Open Lovable's ``scrape-screenshot`` ships with
   single-context — and the reason their multi-breakpoint output drifts.
2. Service-worker registration carries across, which can install bot
   traps on the first nav and silently fail every subsequent capture.
3. Cookie banners that use ``localStorage`` to remember consent on first
   render look different (dismissed) on subsequent renders.

Spinning a fresh context per viewport pays a few hundred ms per
breakpoint (cheap relative to ``networkidle`` settle time) in exchange
for exact equality between "captured-here" and "what a fresh visitor on
that viewport sees today". W14 ghost-overlay diff downstream is only
trustworthy if the four screenshots are mutually independent renders —
multi-context guarantees that.

Module-global state audit (SOP §1)
----------------------------------
This module owns **no** module-level mutable state. The
:class:`MultiContextScreenshotCapture` instance carries a per-instance
lazily-launched ``Browser``; nothing crosses worker boundaries. Under
``uvicorn --workers N`` each worker that ever screenshots spins its own
browser — no shared cache, no shared cookie jar. If a future row ever
needs cross-worker coordination (e.g. dedup live captures of the same
URL), that coordination layer is its own row and lives in PostgreSQL or
Redis, not here.

Air-gap / dependency note
-------------------------
``playwright`` is **not** in ``backend/requirements.in`` (same
disposition as W11.2 ``playwright_source``) — a 50 MB browser bundle
has no business inside images that never screenshot. Operators that need
this row run ``pip install playwright && playwright install chromium``
inside the backend container. Lazy import + typed
:class:`ScreenshotDependencyError` means stock images don't import any
playwright bytes on boot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Public constants ──────────────────────────────────────────────────

#: ``page.goto`` ``wait_until`` argument. ``"networkidle"`` waits for the
#: network to go quiet for 500 ms; same value W11.2 uses so multi-
#: breakpoint captures and W11 single-shot clones see the same render
#: state when targeting the same URL.
DEFAULT_WAIT_UNTIL: str = "networkidle"

#: Per-call wall-clock budget (s) when the caller doesn't supply one.
#: Sized assuming 4 viewports at ~5 s settle each plus headroom — the
#: outer caller (router / agent) should usually pin its own value.
DEFAULT_TIMEOUT_S: float = 30.0

#: Sub-second cushion subtracted from the caller's per-viewport budget
#: before handing it to Playwright, so ``page.goto`` raises a typed
#: ``TimeoutError`` *before* the orchestrator's outer ``asyncio.wait_for``
#: cancels mid-flight. Same rationale as W11.2's
#: ``TIMEOUT_INTERNAL_HEADROOM_S``.
TIMEOUT_INTERNAL_HEADROOM_S: float = 1.0

#: Hard cap on the per-screenshot PNG payload. Defends downstream
#: serialisers (W13.3 manifest writer, future W14 ghost overlay) from
#: an adversarial / mis-configured page producing a multi-GB PNG. The
#: ceiling is generous — a 1920 × 8000 full-page chrome screenshot at
#: device-scale-factor 2 is ~10 MB; 50 MB leaves headroom for retina
#: 4K-class banners without inviting denial-of-disk attacks.
DEFAULT_MAX_PNG_BYTES: int = 50 * 1024 * 1024

#: Browsers Playwright supports. Chromium is the default because its
#: layout engine matches what most reference / production deployments
#: render under, which keeps W13.4 ghost-overlay diff against the W14
#: live preview minimal.
SUPPORTED_BROWSERS: frozenset[str] = frozenset({"chromium", "firefox", "webkit"})

#: Default browser. Operators flip via constructor arg or
#: ``OMNISIGHT_PLAYWRIGHT_BROWSER`` env (shared knob with W11.2 so a
#: single env var controls all Playwright-backed paths).
DEFAULT_BROWSER: str = "chromium"

#: Lower bound on a viewport edge. Below 200 px most production pages
#: collapse into "smallest-mobile" debug rendering that has no real
#: counterpart in any device family — capturing it is wasted bytes.
MIN_VIEWPORT_EDGE_PX: int = 200

#: Upper bound on a viewport edge. 7680 px (8K) is the largest mainstream
#: display class shipping in 2026; anything past that is video-wall
#: territory, not "what does my site look like for a user". Capping
#: here defends the renderer from a 64 K-wide allocation request.
MAX_VIEWPORT_EDGE_PX: int = 7680


# ── Errors ────────────────────────────────────────────────────────────

class ScreenshotCaptureError(Exception):
    """Base class for every error originating in the W13 capture path.

    Mirrors the W11 ``SiteClonerError`` discipline so callers can
    blanket-catch all screenshot-layer faults without leaking
    Playwright-internal exception types into business code."""


class ScreenshotConfigError(ScreenshotCaptureError):
    """Constructor or per-call arguments are malformed (bad browser
    name, empty viewport list, viewport edge out of range, etc.).

    Distinct from :class:`ScreenshotDependencyError` so a typo'd browser
    name doesn't get misattributed to a missing playwright install."""


class ScreenshotDependencyError(ScreenshotCaptureError):
    """The ``playwright`` python package or its browser binary is not
    installed. ``args[0]`` carries the canonical install hint
    (``pip install playwright && playwright install chromium``) so
    operators can copy-paste the fix straight from a log line.

    This is **expected** for stock images that never screenshot — they
    just never reach this code path."""


class ScreenshotCaptureTimeoutError(ScreenshotCaptureError):
    """A single viewport's nav-and-screenshot exceeded the per-viewport
    budget. Carried separately from :class:`ScreenshotCaptureError` so
    the orchestrator (or a future retry policy) can react to "slow page"
    differently from "browser crashed"."""


# ── Data shapes ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Viewport:
    """One breakpoint specification.

    ``name`` is what W13.3 will pin into output filenames
    (``.omnisight/refs/{name}.png``) and what W13.4 will key the ghost-
    overlay diff on. We constrain it now — even though the writer is a
    separate row — to keep the engine and its consumer using the same
    identifier shape (a-z, 0-9, ``-`` / ``_``); otherwise W13.3 has to
    re-validate or sanitise on the way out, which is exactly the kind
    of "two layers each half-validating" pattern this codebase has been
    burned by before.

    ``device_scale_factor`` defaults to ``1.0``. Setting ``2.0`` mimics
    a HiDPI / Retina render and produces a 2× pixel-density PNG; useful
    for design-doc diff tooling but doubles bytes-on-wire.

    ``is_mobile`` toggles Playwright's mobile-emulation mode (``meta
    viewport`` is honoured, touch events are emitted). Off by default
    because the four W13.2 production breakpoints (375 / 768 / 1440 /
    1920) span phone-to-desktop and most production CSS already serves
    the right layout from width alone."""

    name: str
    width: int
    height: int
    device_scale_factor: float = 1.0
    is_mobile: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ScreenshotConfigError(
                f"viewport name must be a non-empty string, got {self.name!r}"
            )
        # Enforce the filename-safe shape eagerly so W13.3 can write
        # ``.omnisight/refs/{name}.png`` without re-sanitising. Same
        # alphabet as DNS labels (lowercase, digits, dash) plus
        # underscore for callers that want ``mobile_375`` style. We
        # require lowercase explicitly because W13.3's writer targets
        # case-insensitive filesystems (macOS default, Windows) where
        # ``Mobile`` and ``mobile`` would collide silently.
        if not all(
            (ch.islower() and ch.isascii()) or ch.isdigit() or ch in ("-", "_")
            for ch in self.name
        ):
            raise ScreenshotConfigError(
                f"viewport name must be [a-z0-9_-]+, got {self.name!r}"
            )
        for axis, value in (("width", self.width), ("height", self.height)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ScreenshotConfigError(
                    f"viewport {axis} must be int, got {type(value).__name__}"
                )
            if value < MIN_VIEWPORT_EDGE_PX or value > MAX_VIEWPORT_EDGE_PX:
                raise ScreenshotConfigError(
                    f"viewport {axis}={value} outside "
                    f"[{MIN_VIEWPORT_EDGE_PX}, {MAX_VIEWPORT_EDGE_PX}]"
                )
        if not isinstance(self.device_scale_factor, (int, float)) or \
                isinstance(self.device_scale_factor, bool):
            raise ScreenshotConfigError(
                "device_scale_factor must be a number, got "
                f"{type(self.device_scale_factor).__name__}"
            )
        if self.device_scale_factor <= 0 or self.device_scale_factor > 4:
            raise ScreenshotConfigError(
                f"device_scale_factor={self.device_scale_factor} outside (0, 4]"
            )
        if not isinstance(self.is_mobile, bool):
            raise ScreenshotConfigError(
                f"is_mobile must be bool, got {type(self.is_mobile).__name__}"
            )


@dataclass(frozen=True)
class ViewportScreenshot:
    """One captured PNG plus its per-viewport metadata.

    ``png_bytes`` is the raw full-page PNG. The W13.3 writer is the
    only thing that should ever materialise these to disk — keeping the
    bytes in-memory here means the engine is reusable from a request
    handler that returns base64-encoded screenshots over HTTP without
    ever touching local FS.

    ``post_redirect_url`` is the URL the page actually settled on after
    ``page.goto`` — relevant when the target served a viewport-conditional
    redirect (e.g. mobile UA → ``m.example.com``). The URL field is
    captured per viewport because that redirect, by construction, may
    differ across viewports."""

    viewport: Viewport
    png_bytes: bytes
    fetched_at: str
    status_code: int
    post_redirect_url: str
    headers: dict[str, str] = field(default_factory=dict)


# ── Core engine ───────────────────────────────────────────────────────

class MultiContextScreenshotCapture:
    """Captures a full-page screenshot of ``url`` across N viewports,
    using a fresh ``BrowserContext`` per viewport.

    Construction (production):

        >>> cap = MultiContextScreenshotCapture()
        >>> async with cap:
        ...     shots = await cap.capture_multi(
        ...         "https://example.com",
        ...         viewports=[
        ...             Viewport(name="mobile_375", width=375, height=812),
        ...             Viewport(name="desktop_1440", width=1440, height=900),
        ...         ],
        ...     )

    Construction (test / DI): inject a callable that returns a
    Playwright-shaped async context manager — see
    ``backend/tests/test_screenshot_capture.py`` for the duck-typed
    fakes. No real chromium binary is needed.

    Resource discipline: the browser is launched lazily on first
    ``capture_multi`` call and reused across calls. ``aclose()`` (or
    ``async with``) tears it down. Each viewport spins its own context
    + page, which are torn down in a ``finally`` even if mid-capture
    raises — long-running daemons therefore never leak Chrome processes.
    """

    def __init__(
        self,
        *,
        browser: Optional[str] = None,
        playwright_factory: Optional[Any] = None,
        launch_args: Optional[Sequence[str]] = None,
    ) -> None:
        """Construct the engine.

        Args:
            browser: One of :data:`SUPPORTED_BROWSERS`. Defaults to
                :data:`DEFAULT_BROWSER` (chromium). Falls back to
                ``OMNISIGHT_PLAYWRIGHT_BROWSER`` env var when not
                supplied — same env knob W11.2 reads so a single flip
                steers both clone and screenshot paths.
            playwright_factory: Test seam. A zero-arg callable that
                returns an async-context-manager compatible with
                ``playwright.async_api.async_playwright()``. Production
                callers leave this ``None`` and the engine imports
                playwright lazily on first capture.
            launch_args: Extra ``--flag`` strings handed to
                ``browser_type.launch(args=...)``. Defaults to the
                hardened set required for unprivileged Docker
                (``--no-sandbox``, ``--disable-dev-shm-usage``,
                ``--disable-gpu``); identical to W11.2 so both backends
                survive in the same container.
        """
        import os  # local — read settings at construct time, not import

        resolved_browser = (
            browser
            or os.environ.get("OMNISIGHT_PLAYWRIGHT_BROWSER", "")
            or DEFAULT_BROWSER
        )
        resolved_browser = resolved_browser.strip().lower()
        if resolved_browser not in SUPPORTED_BROWSERS:
            raise ScreenshotConfigError(
                f"unsupported playwright browser {resolved_browser!r}; "
                f"expected one of {sorted(SUPPORTED_BROWSERS)}"
            )

        self._browser_name: str = resolved_browser
        self._playwright_factory: Optional[Any] = playwright_factory
        self._launch_args: list[str] = (
            list(launch_args)
            if launch_args is not None
            else ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )

        # Lazily filled on first capture_multi().
        self._pw_ctx: Any = None
        self._pw: Any = None
        self._browser: Any = None

    # -- Public surface ---------------------------------------------------

    async def capture_multi(
        self,
        url: str,
        *,
        viewports: Sequence[Viewport],
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_png_bytes: int = DEFAULT_MAX_PNG_BYTES,
    ) -> tuple[ViewportScreenshot, ...]:
        """Render ``url`` once per viewport in an isolated context.

        Returns one :class:`ViewportScreenshot` per viewport, in the
        same order the caller passed them. The order matters because
        downstream W13.3 / W13.4 stages address screenshots by
        ``viewport.name`` and a stable input order keeps tests
        deterministic.

        Args:
            url: The validated URL to render. SSRF / scheme gating is
                the caller's job — this engine only refuses non-http(s)
                URLs as a defence-in-depth measure (we'd be nesting
                inside a sandboxed browser anyway).
            viewports: Ordered, non-empty sequence of viewport specs.
                Every name must be unique — duplicates would collide
                in W13.3's ``.omnisight/refs/{name}.png`` writer and
                surface as a confusing "one screenshot won". We catch
                it here, where the user / caller still has the input
                in hand.
            timeout_s: Wall-clock budget *per viewport*. The default is
                30 s; a 4-viewport call can therefore take up to 2 min
                if every breakpoint pegs its budget. Production callers
                that need a tighter total should bound the whole call
                with their own ``asyncio.wait_for``.
            max_png_bytes: Per-PNG ceiling. Captures producing a larger
                blob raise :class:`ScreenshotCaptureError` *for that
                viewport only* — earlier successful viewports remain
                viewable… except this engine returns all-or-nothing
                (see Failure semantics below). The ceiling exists to
                stop a single rogue viewport from blowing memory.

        Failure semantics: this is a **partial-success-rejecting**
        operation. If any viewport fails (timeout, browser crash,
        oversize PNG), the whole call raises and no
        :class:`ViewportScreenshot` is returned. Rationale: W13's
        consumers (W13.4 ghost overlay, W13.5 matrix test) reason about
        "the four breakpoints together"; a partial set is misleading,
        not useful. A future "best-effort" mode can be added behind a
        flag without disturbing this contract.

        Raises:
            ScreenshotConfigError: malformed inputs (empty viewports,
                duplicate names, non-http URL).
            ScreenshotDependencyError: playwright not installed or
                browser binary missing.
            ScreenshotCaptureTimeoutError: a viewport exhausted its
                per-viewport budget.
            ScreenshotCaptureError: any other capture failure (browser
                launch failed, oversize PNG, navigation error).
        """
        if not (isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))):
            raise ScreenshotConfigError(
                f"capture_multi refusing non-http(s) URL: {url!r}"
            )
        if not viewports:
            raise ScreenshotConfigError(
                "capture_multi requires at least one viewport"
            )
        seen_names: set[str] = set()
        for v in viewports:
            if not isinstance(v, Viewport):
                raise ScreenshotConfigError(
                    f"viewports must be Viewport instances, got {type(v).__name__}"
                )
            if v.name in seen_names:
                raise ScreenshotConfigError(
                    f"viewport names must be unique, duplicate: {v.name!r}"
                )
            seen_names.add(v.name)

        browser = await self._ensure_browser()

        per_viewport_budget = max(0.5, float(timeout_s) - TIMEOUT_INTERNAL_HEADROOM_S)
        nav_timeout_ms = int(per_viewport_budget * 1000)

        results: list[ViewportScreenshot] = []
        for vp in viewports:
            shot = await self._capture_one_viewport(
                browser=browser,
                url=url,
                viewport=vp,
                nav_timeout_ms=nav_timeout_ms,
                max_png_bytes=max_png_bytes,
            )
            results.append(shot)
        return tuple(results)

    async def aclose(self) -> None:
        """Tear down the launched browser + playwright context. Safe
        to call multiple times. After ``aclose()``, a subsequent
        ``capture_multi()`` re-launches the browser — useful for
        long-running daemons that want to recycle the binary on a
        cadence without holding a reference."""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:  # pragma: no cover — diagnostic only
                logger.debug("screenshot browser close ignored: %s", e)
            self._browser = None

        if self._pw_ctx is not None:
            try:
                await self._pw_ctx.__aexit__(None, None, None)
            except Exception as e:  # pragma: no cover — diagnostic only
                logger.debug("screenshot pw context aexit ignored: %s", e)
            self._pw_ctx = None
            self._pw = None

    async def __aenter__(self) -> "MultiContextScreenshotCapture":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- Internals --------------------------------------------------------

    async def _capture_one_viewport(
        self,
        *,
        browser: Any,
        url: str,
        viewport: Viewport,
        nav_timeout_ms: int,
        max_png_bytes: int,
    ) -> ViewportScreenshot:
        """Render ``url`` inside a fresh context sized to ``viewport``.

        The "one context per viewport" rule lives here. Splitting it out
        keeps :meth:`capture_multi` readable as the loop driver and lets
        a hypothetical future caller (e.g. W14 ghost overlay re-capture)
        reuse the inner primitive against an already-launched browser
        without going through the multi-viewport entry point."""
        context: Any = None
        page: Any = None
        try:
            try:
                context = await browser.new_context(
                    user_agent="OmniSight-Productizer/W13.1-Screenshot",
                    locale="en-US",
                    service_workers="block",
                    ignore_https_errors=False,
                    viewport={
                        "width": viewport.width,
                        "height": viewport.height,
                    },
                    device_scale_factor=float(viewport.device_scale_factor),
                    is_mobile=bool(viewport.is_mobile),
                )
            except Exception as e:
                raise ScreenshotCaptureError(
                    f"playwright new_context failed for viewport "
                    f"{viewport.name!r}: {type(e).__name__}: {e!s}"
                ) from e

            try:
                page = await context.new_page()
            except Exception as e:
                raise ScreenshotCaptureError(
                    f"playwright new_page failed for viewport "
                    f"{viewport.name!r}: {type(e).__name__}: {e!s}"
                ) from e

            try:
                response = await page.goto(
                    url,
                    timeout=nav_timeout_ms,
                    wait_until=DEFAULT_WAIT_UNTIL,
                )
            except Exception as e:
                ename = type(e).__name__
                if "TimeoutError" in ename:
                    raise ScreenshotCaptureTimeoutError(
                        f"viewport {viewport.name!r} goto exceeded "
                        f"{nav_timeout_ms}ms for {url!r}"
                    ) from e
                raise ScreenshotCaptureError(
                    f"viewport {viewport.name!r} goto failed ({ename}) "
                    f"for {url!r}: {e!s}"
                ) from e

            if response is None:
                raise ScreenshotCaptureError(
                    f"viewport {viewport.name!r} got no response for {url!r}"
                )

            try:
                png_bytes = await page.screenshot(full_page=True, type="png")
            except Exception as e:
                ename = type(e).__name__
                if "TimeoutError" in ename:
                    raise ScreenshotCaptureTimeoutError(
                        f"viewport {viewport.name!r} screenshot timed out "
                        f"for {url!r}"
                    ) from e
                raise ScreenshotCaptureError(
                    f"viewport {viewport.name!r} screenshot failed "
                    f"({ename}) for {url!r}: {e!s}"
                ) from e

            if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes:
                raise ScreenshotCaptureError(
                    f"viewport {viewport.name!r} screenshot returned empty bytes"
                )
            if len(png_bytes) > int(max_png_bytes):
                raise ScreenshotCaptureError(
                    f"viewport {viewport.name!r} screenshot exceeded "
                    f"max_png_bytes={max_png_bytes}"
                )

            try:
                status_code = int(response.status)
            except Exception:
                status_code = 200

            response_headers: dict[str, str] = {}
            try:
                hdrs = await response.all_headers()
            except Exception:
                try:
                    hdrs = response.headers
                except Exception:
                    hdrs = {}
            if isinstance(hdrs, dict):
                for k, v in hdrs.items():
                    if isinstance(k, str) and isinstance(v, (str, int, float)):
                        response_headers[k.lower()] = str(v)

            try:
                post_redirect_url = page.url or url
            except Exception:
                post_redirect_url = url
            if not isinstance(post_redirect_url, str) or not post_redirect_url:
                post_redirect_url = url

            return ViewportScreenshot(
                viewport=viewport,
                png_bytes=bytes(png_bytes),
                fetched_at=_utc_iso8601_now(),
                status_code=status_code,
                post_redirect_url=post_redirect_url,
                headers=response_headers,
            )

        finally:
            for ref in (page, context):
                if ref is None:
                    continue
                try:
                    await ref.close()
                except Exception as e:
                    logger.debug(
                        "screenshot cleanup ignored on %s for viewport %s: %s",
                        type(ref).__name__, viewport.name, e,
                    )

    async def _ensure_browser(self) -> Any:
        """Lazily launch + cache the ``Browser``. Same shape as W11.2's
        ``PlaywrightSource._ensure_browser`` so the two paths can share
        an operator's mental model: missing package → typed dependency
        error; missing binary → typed dependency error; unsupported
        browser attr → typed config error; everything else → generic
        capture error with the underlying exception chained."""
        if self._browser is not None:
            return self._browser

        factory = self._playwright_factory
        if factory is None:
            try:
                from playwright.async_api import async_playwright  # noqa: PLC0415
            except Exception as e:
                raise ScreenshotDependencyError(
                    "playwright is not installed. Operators run: "
                    "`pip install playwright && playwright install chromium`"
                ) from e
            factory = async_playwright

        try:
            self._pw_ctx = factory()
            self._pw = await self._pw_ctx.__aenter__()
        except Exception as e:
            raise ScreenshotCaptureError(
                f"playwright bootstrap failed: {type(e).__name__}: {e!s}"
            ) from e

        try:
            browser_type = getattr(self._pw, self._browser_name)
        except AttributeError as e:
            raise ScreenshotConfigError(
                f"playwright object has no browser attribute "
                f"{self._browser_name!r}"
            ) from e

        try:
            self._browser = await browser_type.launch(
                headless=True,
                args=list(self._launch_args),
            )
        except Exception as e:
            ename = type(e).__name__
            msg = str(e)
            if "Executable doesn't exist" in msg or "BrowserType.launch" in msg:
                raise ScreenshotDependencyError(
                    f"playwright browser binary for {self._browser_name!r} "
                    f"missing. Run: `playwright install {self._browser_name}`"
                ) from e
            raise ScreenshotCaptureError(
                f"playwright browser launch failed ({ename}): {msg}"
            ) from e

        return self._browser


# ── Helpers ───────────────────────────────────────────────────────────

def _utc_iso8601_now() -> str:
    """Return current UTC time as ISO-8601 with a ``Z`` suffix. Pinned
    format matches W11.2 / W11.7 manifest spec so a future cross-row
    timestamp diff isn't tripped by stringification drift."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = [
    "DEFAULT_BROWSER",
    "DEFAULT_MAX_PNG_BYTES",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_WAIT_UNTIL",
    "MAX_VIEWPORT_EDGE_PX",
    "MIN_VIEWPORT_EDGE_PX",
    "MultiContextScreenshotCapture",
    "SUPPORTED_BROWSERS",
    "ScreenshotCaptureError",
    "ScreenshotCaptureTimeoutError",
    "ScreenshotConfigError",
    "ScreenshotDependencyError",
    "TIMEOUT_INTERNAL_HEADROOM_S",
    "Viewport",
    "ViewportScreenshot",
]
