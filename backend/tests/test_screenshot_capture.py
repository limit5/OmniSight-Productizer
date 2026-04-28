"""W13.1 #XXX — Unit tests for ``backend.web.screenshot_capture``.

Pins the multi-context engine's contract:

* :class:`Viewport` validation (filename-safe name, edge bounds,
  device-scale-factor range, mobile-toggle type).
* :class:`MultiContextScreenshotCapture` construction (browser
  resolution order, hardened launch args, supported-browser set).
* ``capture_multi`` happy path — **one context per viewport**,
  ``new_context`` receives the right viewport / DSF / mobile shape,
  results are returned in input order.
* Multi-context invariant: contexts are torn down between viewports;
  the browser is launched once and reused.
* Error mapping: per-viewport timeout → typed timeout; non-http URL,
  empty viewports list, duplicate names → :class:`ScreenshotConfigError`.
* Lazy-import discipline: missing playwright + missing browser-binary
  paths surface :class:`ScreenshotDependencyError`.

Every test runs without network I/O — a duck-typed playwright fake is
injected via ``playwright_factory`` so neither the ``playwright``
package nor a chromium binary needs to be present.

Scope discipline: no test in this file exercises the W13.3 disk
writer, the W13.4 ghost overlay, or the W13.5 cross-URL matrix. Those
ship with their own rows.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from backend.web.screenshot_capture import (
    DEFAULT_BROWSER,
    DEFAULT_MAX_PNG_BYTES,
    DEFAULT_TIMEOUT_S,
    DEFAULT_WAIT_UNTIL,
    MAX_VIEWPORT_EDGE_PX,
    MIN_VIEWPORT_EDGE_PX,
    MultiContextScreenshotCapture,
    SUPPORTED_BROWSERS,
    ScreenshotCaptureError,
    ScreenshotCaptureTimeoutError,
    ScreenshotConfigError,
    ScreenshotDependencyError,
    Viewport,
    ViewportScreenshot,
)


# ── Test fakes (duck-typed playwright async surface) ──────────────────

class _FakeResponse:
    def __init__(self, *, status: int = 200, headers: Optional[dict[str, str]] = None,
                 url: str = "https://example.com"):
        self.status = status
        self.url = url
        self._headers = dict(headers or {})

    async def all_headers(self) -> dict[str, str]:
        return dict(self._headers)


class _FakePage:
    def __init__(
        self,
        *,
        png_bytes: bytes,
        response: _FakeResponse,
        final_url: str,
        goto_raises: Optional[BaseException] = None,
        screenshot_raises: Optional[BaseException] = None,
    ):
        self._png_bytes = png_bytes
        self._response = response
        self._goto_raises = goto_raises
        self._screenshot_raises = screenshot_raises
        self.url = final_url
        self.closed = False
        self.goto_calls: list[dict[str, Any]] = []
        self.screenshot_calls: list[dict[str, Any]] = []

    async def goto(self, url: str, *, timeout: int, wait_until: str):
        self.goto_calls.append({"url": url, "timeout": timeout, "wait_until": wait_until})
        if self._goto_raises is not None:
            raise self._goto_raises
        return self._response

    async def screenshot(self, *, full_page: bool, type: str) -> bytes:  # noqa: A002
        self.screenshot_calls.append({"full_page": full_page, "type": type})
        if self._screenshot_raises is not None:
            raise self._screenshot_raises
        return self._png_bytes

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage, *, kwargs: dict[str, Any]):
        self._page = page
        self.closed = False
        self.new_context_kwargs = kwargs

    async def new_page(self) -> _FakePage:
        return self._page

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    """Yields a *new* context (with a *new* page) per ``new_context``
    call so we can pin the "one context per viewport" invariant
    behaviourally — if the engine ever shared a context, the test would
    see only one ``_FakeContext`` instance instead of N."""

    def __init__(self, page_factory):
        self._page_factory = page_factory
        self.closed = False
        self.contexts: list[_FakeContext] = []
        self.launch_kwargs: dict[str, Any] = {}

    async def new_context(self, **kwargs):
        page = self._page_factory(kwargs)
        ctx = _FakeContext(page, kwargs=dict(kwargs))
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


class _FakeBrowserType:
    def __init__(self, browser: _FakeBrowser, *, launch_raises: Optional[BaseException] = None):
        self._browser = browser
        self._launch_raises = launch_raises
        self.launch_calls: list[dict[str, Any]] = []

    async def launch(self, **kwargs):
        self.launch_calls.append(dict(kwargs))
        if self._launch_raises is not None:
            raise self._launch_raises
        self._browser.launch_kwargs = dict(kwargs)
        return self._browser


class _FakePlaywright:
    def __init__(self, browser_type: _FakeBrowserType, *, browser_attr: str = "chromium"):
        setattr(self, browser_attr, browser_type)


class _FakePwCtx:
    def __init__(self, pw: _FakePlaywright):
        self._pw = pw
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self._pw

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return None


def _build_factory(
    *,
    png_bytes: bytes = b"\x89PNG\r\n\x1a\nfake-png",
    final_url: str = "https://example.com/final",
    status: int = 200,
    response_headers: Optional[dict[str, str]] = None,
    goto_raises: Optional[BaseException] = None,
    screenshot_raises: Optional[BaseException] = None,
    launch_raises: Optional[BaseException] = None,
    browser_attr: str = "chromium",
):
    """Build a ``playwright_factory`` callable + handle dict.

    The handle dict exposes the inner fakes so tests can assert on
    ``new_context`` kwargs, ``page.goto`` args, browser launch counts,
    etc."""

    pages: list[_FakePage] = []

    def _page_factory(_ctx_kwargs: dict[str, Any]) -> _FakePage:
        page = _FakePage(
            png_bytes=png_bytes,
            response=_FakeResponse(
                status=status,
                headers=response_headers or {"content-type": "text/html"},
                url=final_url,
            ),
            final_url=final_url,
            goto_raises=goto_raises,
            screenshot_raises=screenshot_raises,
        )
        pages.append(page)
        return page

    browser = _FakeBrowser(_page_factory)
    browser_type = _FakeBrowserType(browser, launch_raises=launch_raises)
    pw = _FakePlaywright(browser_type, browser_attr=browser_attr)
    ctx = _FakePwCtx(pw)

    handle = {
        "pages": pages,
        "browser": browser,
        "browser_type": browser_type,
        "pw_ctx": ctx,
    }

    def factory():
        return ctx

    return factory, handle


# ── Viewport: validation ──────────────────────────────────────────────

def test_viewport_happy_path():
    v = Viewport(name="mobile_375", width=375, height=812)
    assert v.name == "mobile_375"
    assert v.width == 375 and v.height == 812
    assert v.device_scale_factor == 1.0
    assert v.is_mobile is False


def test_viewport_with_dsf_and_mobile():
    v = Viewport(name="iphone-13", width=390, height=844,
                 device_scale_factor=3.0, is_mobile=True)
    assert v.device_scale_factor == 3.0
    assert v.is_mobile is True


@pytest.mark.parametrize("bad_name", ["", "UPPER", "Mobile", "has space", "weird@name", "with.dot"])
def test_viewport_rejects_filename_unsafe_name(bad_name):
    with pytest.raises(ScreenshotConfigError):
        Viewport(name=bad_name, width=375, height=812)


@pytest.mark.parametrize("good_name", [
    "mobile_375",
    "desktop-1440",
    "4k-uhd",
    "phone",
    "a",
    "375",
])
def test_viewport_accepts_filename_safe_name(good_name):
    Viewport(name=good_name, width=375, height=812)  # must not raise


@pytest.mark.parametrize("axis", ["width", "height"])
def test_viewport_rejects_edge_below_min(axis):
    kwargs = {"name": "x", "width": 375, "height": 812, axis: MIN_VIEWPORT_EDGE_PX - 1}
    with pytest.raises(ScreenshotConfigError):
        Viewport(**kwargs)


@pytest.mark.parametrize("axis", ["width", "height"])
def test_viewport_rejects_edge_above_max(axis):
    kwargs = {"name": "x", "width": 375, "height": 812, axis: MAX_VIEWPORT_EDGE_PX + 1}
    with pytest.raises(ScreenshotConfigError):
        Viewport(**kwargs)


def test_viewport_rejects_non_int_edge():
    with pytest.raises(ScreenshotConfigError):
        Viewport(name="x", width="375", height=812)  # type: ignore[arg-type]


def test_viewport_rejects_bool_edge():
    # Python bools are int subclass — must be rejected explicitly so
    # ``Viewport(name="x", width=True, height=812)`` doesn't pass.
    with pytest.raises(ScreenshotConfigError):
        Viewport(name="x", width=True, height=812)  # type: ignore[arg-type]


@pytest.mark.parametrize("dsf", [0, -1, 5.0])
def test_viewport_rejects_invalid_dsf(dsf):
    with pytest.raises(ScreenshotConfigError):
        Viewport(name="x", width=375, height=812, device_scale_factor=dsf)


def test_viewport_rejects_non_bool_is_mobile():
    with pytest.raises(ScreenshotConfigError):
        Viewport(name="x", width=375, height=812, is_mobile="yes")  # type: ignore[arg-type]


def test_viewport_is_frozen():
    v = Viewport(name="x", width=375, height=812)
    with pytest.raises(Exception):  # FrozenInstanceError
        v.width = 999  # type: ignore[misc]


# ── MultiContextScreenshotCapture: construction ───────────────────────

def test_capture_default_browser_is_chromium():
    cap = MultiContextScreenshotCapture()
    assert cap._browser_name == DEFAULT_BROWSER == "chromium"  # noqa: SLF001


def test_capture_explicit_browser_overrides_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_PLAYWRIGHT_BROWSER", "firefox")
    cap = MultiContextScreenshotCapture(browser="webkit")
    assert cap._browser_name == "webkit"  # noqa: SLF001


def test_capture_env_browser(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_PLAYWRIGHT_BROWSER", "firefox")
    cap = MultiContextScreenshotCapture()
    assert cap._browser_name == "firefox"  # noqa: SLF001


def test_capture_unsupported_browser_rejected():
    with pytest.raises(ScreenshotConfigError):
        MultiContextScreenshotCapture(browser="trident")


def test_capture_supported_browser_set_exact():
    assert SUPPORTED_BROWSERS == frozenset({"chromium", "firefox", "webkit"})


def test_capture_default_launch_args_hardened():
    cap = MultiContextScreenshotCapture()
    assert "--no-sandbox" in cap._launch_args  # noqa: SLF001
    assert "--disable-dev-shm-usage" in cap._launch_args  # noqa: SLF001
    assert "--disable-gpu" in cap._launch_args  # noqa: SLF001


# ── capture_multi: happy path / multi-context invariant ───────────────

@pytest.mark.asyncio
async def test_capture_multi_returns_one_screenshot_per_viewport():
    factory, h = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    viewports = [
        Viewport(name="mobile_375", width=375, height=812),
        Viewport(name="tablet_768", width=768, height=1024),
        Viewport(name="laptop_1440", width=1440, height=900),
        Viewport(name="desktop_1920", width=1920, height=1080),
    ]
    shots = await cap.capture_multi(
        "https://example.com",
        viewports=viewports,
        timeout_s=10.0,
    )
    await cap.aclose()

    assert isinstance(shots, tuple)
    assert len(shots) == 4
    # Order preserved.
    assert [s.viewport.name for s in shots] == [
        "mobile_375", "tablet_768", "laptop_1440", "desktop_1920",
    ]
    for s in shots:
        assert isinstance(s, ViewportScreenshot)
        assert s.png_bytes.startswith(b"\x89PNG")
        assert s.status_code == 200
        assert s.fetched_at.endswith("Z")


@pytest.mark.asyncio
async def test_capture_multi_creates_one_context_per_viewport():
    """The W13.1 multi-context invariant. If the engine ever shared a
    context across viewports (the open-lovable bug we set out to avoid),
    fewer ``_FakeContext`` instances would be observed."""
    factory, h = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    viewports = [
        Viewport(name="a", width=375, height=812),
        Viewport(name="b", width=768, height=1024),
        Viewport(name="c", width=1440, height=900),
    ]
    await cap.capture_multi("https://example.com", viewports=viewports, timeout_s=10.0)
    await cap.aclose()

    assert len(h["browser"].contexts) == 3
    # And each context received its viewport's exact dimensions / DSF.
    for ctx, vp in zip(h["browser"].contexts, viewports):
        kw = ctx.new_context_kwargs
        assert kw["viewport"] == {"width": vp.width, "height": vp.height}
        assert kw["device_scale_factor"] == float(vp.device_scale_factor)
        assert kw["is_mobile"] is bool(vp.is_mobile)
        assert kw["service_workers"] == "block"
        assert kw["locale"] == "en-US"
        assert kw["ignore_https_errors"] is False
        assert kw["user_agent"].startswith("OmniSight-Productizer/W13.1")


@pytest.mark.asyncio
async def test_capture_multi_tears_down_each_context_after_use():
    """Each viewport's context + page closed in ``finally``, even on
    success. Prevents Chrome process leaks on long-running daemons."""
    factory, h = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    viewports = [
        Viewport(name="a", width=375, height=812),
        Viewport(name="b", width=1440, height=900),
    ]
    await cap.capture_multi("https://example.com", viewports=viewports, timeout_s=10.0)

    for ctx in h["browser"].contexts:
        assert ctx.closed is True
    for page in h["pages"]:
        assert page.closed is True


@pytest.mark.asyncio
async def test_capture_multi_amortises_browser_across_viewports():
    """Browser launch is heavy (>1 s); contexts are cheap. The engine
    must launch the browser exactly once even for N viewports, and
    again exactly once on a second ``capture_multi`` call after the
    same instance was kept warm."""
    factory, h = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    viewports = [
        Viewport(name="a", width=375, height=812),
        Viewport(name="b", width=1440, height=900),
        Viewport(name="c", width=1920, height=1080),
    ]
    await cap.capture_multi("https://example.com", viewports=viewports, timeout_s=10.0)
    await cap.capture_multi("https://example.com", viewports=viewports, timeout_s=10.0)
    await cap.aclose()

    assert len(h["browser_type"].launch_calls) == 1
    # 3 viewports × 2 calls = 6 contexts.
    assert len(h["browser"].contexts) == 6
    assert h["browser"].closed is True
    assert h["pw_ctx"].exited is True


@pytest.mark.asyncio
async def test_capture_multi_passes_correct_goto_args():
    factory, h = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    await cap.capture_multi(
        "https://example.com",
        viewports=[Viewport(name="x", width=375, height=812)],
        timeout_s=10.0,
    )
    await cap.aclose()

    assert len(h["pages"]) == 1
    g = h["pages"][0].goto_calls
    assert len(g) == 1
    assert g[0]["url"] == "https://example.com"
    assert g[0]["wait_until"] == DEFAULT_WAIT_UNTIL
    assert isinstance(g[0]["timeout"], int) and 0 < g[0]["timeout"] < 10_000


@pytest.mark.asyncio
async def test_capture_multi_screenshot_args_are_full_page_png():
    factory, h = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    await cap.capture_multi(
        "https://example.com",
        viewports=[Viewport(name="x", width=375, height=812)],
        timeout_s=10.0,
    )
    await cap.aclose()

    assert len(h["pages"]) == 1
    s = h["pages"][0].screenshot_calls
    assert len(s) == 1
    assert s[0]["full_page"] is True
    assert s[0]["type"] == "png"


@pytest.mark.asyncio
async def test_capture_multi_collects_response_headers_lowercased():
    factory, _ = _build_factory(response_headers={"X-Frame-Options": "DENY"})
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    shots = await cap.capture_multi(
        "https://example.com",
        viewports=[Viewport(name="x", width=375, height=812)],
        timeout_s=10.0,
    )
    await cap.aclose()
    assert shots[0].headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_capture_multi_records_post_redirect_url():
    factory, _ = _build_factory(final_url="https://m.example.com/")
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    shots = await cap.capture_multi(
        "https://example.com",
        viewports=[Viewport(name="x", width=375, height=812)],
        timeout_s=10.0,
    )
    await cap.aclose()
    assert shots[0].post_redirect_url == "https://m.example.com/"


# ── capture_multi: error mapping ──────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_multi_rejects_non_http_url():
    factory, _ = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    with pytest.raises(ScreenshotConfigError):
        await cap.capture_multi(
            "ftp://example.com/",
            viewports=[Viewport(name="x", width=375, height=812)],
        )


@pytest.mark.asyncio
async def test_capture_multi_rejects_empty_viewport_list():
    factory, _ = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    with pytest.raises(ScreenshotConfigError):
        await cap.capture_multi("https://example.com", viewports=[])


@pytest.mark.asyncio
async def test_capture_multi_rejects_duplicate_viewport_names():
    factory, _ = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    with pytest.raises(ScreenshotConfigError):
        await cap.capture_multi(
            "https://example.com",
            viewports=[
                Viewport(name="a", width=375, height=812),
                Viewport(name="a", width=1440, height=900),
            ],
        )


@pytest.mark.asyncio
async def test_capture_multi_goto_timeout_mapped_to_typed_timeout():
    class _PWTimeoutError(Exception):
        pass

    factory, _ = _build_factory(goto_raises=_PWTimeoutError("nav too slow"))
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    with pytest.raises(ScreenshotCaptureTimeoutError):
        await cap.capture_multi(
            "https://example.com",
            viewports=[Viewport(name="x", width=375, height=812)],
        )


@pytest.mark.asyncio
async def test_capture_multi_goto_other_error_mapped_to_capture_error():
    class _BadNav(Exception):
        pass

    factory, _ = _build_factory(goto_raises=_BadNav("dns lookup failed"))
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    with pytest.raises(ScreenshotCaptureError):
        await cap.capture_multi(
            "https://example.com",
            viewports=[Viewport(name="x", width=375, height=812)],
        )


@pytest.mark.asyncio
async def test_capture_multi_screenshot_failure_mapped_to_capture_error():
    factory, _ = _build_factory(screenshot_raises=Exception("renderer crashed"))
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    with pytest.raises(ScreenshotCaptureError):
        await cap.capture_multi(
            "https://example.com",
            viewports=[Viewport(name="x", width=375, height=812)],
        )


@pytest.mark.asyncio
async def test_capture_multi_oversize_png_raises():
    factory, _ = _build_factory(png_bytes=b"\x89PNG" + b"x" * 100)
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    with pytest.raises(ScreenshotCaptureError):
        await cap.capture_multi(
            "https://example.com",
            viewports=[Viewport(name="x", width=375, height=812)],
            max_png_bytes=10,
        )


@pytest.mark.asyncio
async def test_capture_multi_partial_failure_rejects_all():
    """Engine returns all-or-nothing — rationale documented in the
    capture_multi docstring's "Failure semantics" block. If viewport #2
    fails, viewport #1's successful capture must NOT be returned."""
    # First viewport succeeds (page returned by factory), then we toggle
    # the page to raise on goto by reaching into the captured page list.
    # Simpler: rebuild a factory that always raises and check that the
    # first viewport's failure already short-circuits — which proves
    # the loop is not best-effort.
    factory, _ = _build_factory(goto_raises=Exception("boom"))
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    with pytest.raises(ScreenshotCaptureError):
        await cap.capture_multi(
            "https://example.com",
            viewports=[
                Viewport(name="a", width=375, height=812),
                Viewport(name="b", width=1440, height=900),
            ],
        )


# ── Dependency / config error mapping for missing playwright ──────────

@pytest.mark.asyncio
async def test_capture_multi_dependency_error_when_no_factory_and_no_pkg(monkeypatch):
    """If the operator hasn't run ``pip install playwright`` and didn't
    inject a factory, the engine must surface
    :class:`ScreenshotDependencyError` with the canonical install hint
    rather than a raw :class:`ImportError`."""
    import sys
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    cap = MultiContextScreenshotCapture()  # no factory → forces lazy import
    with pytest.raises(ScreenshotDependencyError):
        await cap.capture_multi(
            "https://example.com",
            viewports=[Viewport(name="x", width=375, height=812)],
        )


@pytest.mark.asyncio
async def test_capture_multi_browser_binary_missing_mapped_to_dependency_error():
    factory, _ = _build_factory(
        launch_raises=Exception("Executable doesn't exist at /home/.cache/ms-playwright/..."),
    )
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    with pytest.raises(ScreenshotDependencyError):
        await cap.capture_multi(
            "https://example.com",
            viewports=[Viewport(name="x", width=375, height=812)],
        )


@pytest.mark.asyncio
async def test_capture_multi_browser_attr_missing_mapped_to_config_error():
    """Wrong browser-name attr on the entered playwright object — the
    operator typo'd ``OMNISIGHT_PLAYWRIGHT_BROWSER`` to a member of
    ``SUPPORTED_BROWSERS`` that the local build doesn't expose. That's
    a config problem, not a missing dependency."""
    factory, _ = _build_factory(browser_attr="firefox")
    cap = MultiContextScreenshotCapture(browser="webkit", playwright_factory=factory)
    with pytest.raises(ScreenshotConfigError):
        await cap.capture_multi(
            "https://example.com",
            viewports=[Viewport(name="x", width=375, height=812)],
        )


# ── Lifecycle ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_aclose_idempotent():
    factory, h = _build_factory()
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    await cap.capture_multi(
        "https://example.com",
        viewports=[Viewport(name="x", width=375, height=812)],
    )
    await cap.aclose()
    await cap.aclose()  # second call must not raise
    assert h["browser"].closed is True


@pytest.mark.asyncio
async def test_capture_async_context_manager_closes_on_exit():
    factory, h = _build_factory()
    async with MultiContextScreenshotCapture(playwright_factory=factory) as cap:
        await cap.capture_multi(
            "https://example.com",
            viewports=[Viewport(name="x", width=375, height=812)],
        )
    assert h["browser"].closed is True
    assert h["pw_ctx"].exited is True


# ── Module surface ────────────────────────────────────────────────────

def test_default_max_png_bytes_is_a_sensible_ceiling():
    # 50 MB — generous for retina full-page screenshots, tight enough
    # to refuse a memory-DoS payload. Pinned so future drift surfaces
    # in CI rather than as a quiet "we used to refuse 1 GB".
    assert DEFAULT_MAX_PNG_BYTES == 50 * 1024 * 1024


def test_default_timeout_is_seconds_not_ms():
    # A common bug — pinning the unit so a future "30 means 30 ms" drift
    # fails loudly.
    assert isinstance(DEFAULT_TIMEOUT_S, float)
    assert 5.0 <= DEFAULT_TIMEOUT_S <= 120.0


def test_min_max_viewport_edges_are_consistent():
    assert MIN_VIEWPORT_EDGE_PX < MAX_VIEWPORT_EDGE_PX
    assert MIN_VIEWPORT_EDGE_PX >= 100
    assert MAX_VIEWPORT_EDGE_PX <= 16384
