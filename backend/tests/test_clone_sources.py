"""W11.2 #XXX — Contract tests for the two ``CloneSource`` backends.

Pins:

    * ``FirecrawlSource`` (SaaS adapter) — request payload shape, header
      shape, response parsing, error mapping, env-var resolution, lazy
      httpx import discipline.
    * ``PlaywrightSource`` (self-host adapter) — bootstrap sequence,
      ``page.goto`` arg shape, response listener wiring, error mapping
      for missing playwright / browser binary, env-var resolution.
    * ``backend.web.make_clone_source`` factory — name resolution order
      (explicit > settings > env > auto) + ``UnknownCloneBackendError``.

Every test runs without network I/O: ``FirecrawlSource`` is exercised
with an injected mock client; ``PlaywrightSource`` is exercised with an
injected ``playwright_factory`` so the real ``playwright`` package + the
chromium binary are never needed.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Mapping, Optional

import pytest

from backend.web import (
    KNOWN_CLONE_BACKENDS,
    UnknownCloneBackendError,
    make_clone_source,
)
from backend.web.firecrawl_source import (
    DEFAULT_FIRECRAWL_BASE_URL,
    FIRECRAWL_BACKEND_NAME,
    FIRECRAWL_SCRAPE_PATH,
    FirecrawlConfigError,
    FirecrawlSource,
)
from backend.web.playwright_source import (
    DEFAULT_BROWSER,
    DEFAULT_WAIT_UNTIL,
    PLAYWRIGHT_BACKEND_NAME,
    PlaywrightConfigError,
    PlaywrightDependencyError,
    PlaywrightSource,
    SUPPORTED_BROWSERS,
)
from backend.web.site_cloner import (
    CloneCaptureTimeoutError,
    CloneSource,
    CloneSourceError,
    RawCapture,
    SiteClonerError,
)


# ── Firecrawl test doubles ────────────────────────────────────────────

class _MockResponse:
    """Just enough of an ``httpx.Response`` to satisfy FirecrawlSource."""

    def __init__(self, *, status_code: int = 200, body: Any = None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _MockHttpxClient:
    """Records ``post`` calls + returns a pre-baked response.

    Substituted for ``httpx.AsyncClient`` via the constructor's
    ``client=`` DI hook so tests never import httpx and never touch the
    network."""

    def __init__(
        self,
        response: Optional[_MockResponse] = None,
        *,
        raises: Optional[BaseException] = None,
        sleep_s: float = 0.0,
    ):
        self._response = response
        self._raises = raises
        self._sleep_s = sleep_s
        self.calls: list[dict[str, Any]] = []
        self.closed: bool = False

    async def post(self, url: str, *, content: bytes, headers: Mapping[str, str], timeout: float):
        self.calls.append({
            "url": url,
            "content": content,
            "headers": dict(headers),
            "timeout": timeout,
        })
        if self._sleep_s:
            await asyncio.sleep(self._sleep_s)
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response

    async def aclose(self) -> None:
        self.closed = True


def _firecrawl_success_body(
    *,
    raw_html: str = "<html><body>x</body></html>",
    links: Optional[list[Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Minimal envelope mirroring Firecrawl's /v1/scrape success shape."""
    return {
        "success": True,
        "data": {
            "rawHtml": raw_html,
            "links": list(links) if links is not None else [],
            "metadata": dict(metadata) if metadata is not None else {
                "sourceURL": "https://example.com",
                "statusCode": 200,
                "headers": {"Content-Type": "text/html; charset=utf-8"},
            },
        },
    }


# ── FirecrawlSource: construction ────────────────────────────────────

def test_firecrawl_source_satisfies_protocol():
    src = FirecrawlSource(api_key="fc-test", client=_MockHttpxClient())
    assert isinstance(src, CloneSource)
    assert src.name == FIRECRAWL_BACKEND_NAME


def test_firecrawl_requires_api_key(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(FirecrawlConfigError):
        FirecrawlSource()


def test_firecrawl_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_FIRECRAWL_API_KEY", "fc-from-env")
    src = FirecrawlSource(client=_MockHttpxClient())
    assert src._api_key == "fc-from-env"  # noqa: SLF001 — testing private surface


def test_firecrawl_explicit_arg_beats_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_FIRECRAWL_API_KEY", "fc-from-env")
    src = FirecrawlSource(api_key="fc-explicit", client=_MockHttpxClient())
    assert src._api_key == "fc-explicit"  # noqa: SLF001


def test_firecrawl_blank_key_rejected(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_FIRECRAWL_API_KEY", "   ")
    with pytest.raises(FirecrawlConfigError):
        FirecrawlSource()


def test_firecrawl_base_url_strips_trailing_slash():
    src = FirecrawlSource(
        api_key="fc-test",
        base_url="https://custom.firecrawl.example/",
        client=_MockHttpxClient(),
    )
    assert src._base_url == "https://custom.firecrawl.example"  # noqa: SLF001


def test_firecrawl_base_url_default_when_blank(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_FIRECRAWL_BASE_URL", raising=False)
    src = FirecrawlSource(api_key="fc-test", client=_MockHttpxClient())
    assert src._base_url == DEFAULT_FIRECRAWL_BASE_URL  # noqa: SLF001


# ── FirecrawlSource: capture happy path ──────────────────────────────

@pytest.mark.asyncio
async def test_firecrawl_capture_returns_raw_capture():
    body = _firecrawl_success_body(
        raw_html="<html><title>Hi</title><body/></html>",
        links=["https://example.com/a.png", "https://example.com/b.css"],
        metadata={
            "sourceURL": "https://example.com/final",
            "statusCode": 201,
            "headers": {"X-Robots-Tag": "noai", "Cache-Control": "no-store"},
        },
    )
    client = _MockHttpxClient(_MockResponse(status_code=200, body=body))
    src = FirecrawlSource(api_key="fc-test", client=client)

    cap = await src.capture("https://example.com", timeout_s=10.0, max_html_bytes=5_000_000)

    assert isinstance(cap, RawCapture)
    assert cap.backend == FIRECRAWL_BACKEND_NAME
    assert cap.url == "https://example.com/final"
    assert cap.status_code == 201
    assert "<title>Hi</title>" in cap.html
    assert cap.asset_urls == (
        "https://example.com/a.png",
        "https://example.com/b.css",
    )
    # Header keys lower-cased for downstream W11.4 ai.txt scanners.
    assert cap.headers["x-robots-tag"] == "noai"
    assert cap.headers["cache-control"] == "no-store"
    # Pin ISO-8601 with Z suffix (W11.7 manifest reads it).
    assert cap.fetched_at.endswith("Z")


@pytest.mark.asyncio
async def test_firecrawl_capture_request_shape():
    """Body, headers, endpoint, and timeout-conversion must match what
    the upstream API expects so a future refactor doesn't silently break
    the SaaS contract."""
    client = _MockHttpxClient(_MockResponse(body=_firecrawl_success_body()))
    src = FirecrawlSource(api_key="fc-test", client=client)

    await src.capture("https://example.com", timeout_s=10.0, max_html_bytes=1_000_000)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == f"{DEFAULT_FIRECRAWL_BASE_URL}{FIRECRAWL_SCRAPE_PATH}"
    assert call["headers"]["Authorization"] == "Bearer fc-test"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["User-Agent"].startswith("OmniSight-Productizer/")

    payload = json.loads(call["content"].decode("utf-8"))
    assert payload["url"] == "https://example.com"
    # rawHtml + links — the only formats the W11 pipeline consumes.
    assert payload["formats"] == ["rawHtml", "links"]
    assert payload["mobile"] is False
    # Firecrawl wants ms; orchestrator passed seconds.
    assert isinstance(payload["timeout"], int)
    assert payload["timeout"] > 0
    # Internal timeout sits below caller's outer budget (headroom).
    assert call["timeout"] < 10.0


@pytest.mark.asyncio
async def test_firecrawl_capture_dedupes_links():
    body = _firecrawl_success_body(links=[
        "https://example.com/a.png",
        "https://example.com/a.png",   # duplicate
        "https://example.com/b.css",
        12345,                          # non-string — must be filtered
        "",                             # empty — must be filtered
    ])
    client = _MockHttpxClient(_MockResponse(body=body))
    src = FirecrawlSource(api_key="fc-test", client=client)

    cap = await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)

    assert cap.asset_urls == (
        "https://example.com/a.png",
        "https://example.com/b.css",
    )


@pytest.mark.asyncio
async def test_firecrawl_capture_falls_back_to_html_when_rawhtml_missing():
    body = {
        "success": True,
        "data": {
            "html": "<html><body>fallback</body></html>",
            "metadata": {"sourceURL": "https://example.com", "statusCode": 200},
        },
    }
    client = _MockHttpxClient(_MockResponse(body=body))
    src = FirecrawlSource(api_key="fc-test", client=client)

    cap = await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)
    assert "fallback" in cap.html


# ── FirecrawlSource: error mapping ───────────────────────────────────

@pytest.mark.asyncio
async def test_firecrawl_capture_http_error_raises_clone_source_error():
    client = _MockHttpxClient(
        _MockResponse(status_code=502, body={"error": "upstream"}, text='{"error":"upstream"}')
    )
    src = FirecrawlSource(api_key="fc-test", client=client)

    with pytest.raises(CloneSourceError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_firecrawl_capture_non_json_raises_clone_source_error():
    resp = _MockResponse(status_code=200, body=ValueError("not json"), text="<html/>")
    client = _MockHttpxClient(resp)
    src = FirecrawlSource(api_key="fc-test", client=client)

    with pytest.raises(CloneSourceError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_firecrawl_capture_success_false_raises():
    client = _MockHttpxClient(_MockResponse(body={"success": False, "error": "blocked"}))
    src = FirecrawlSource(api_key="fc-test", client=client)

    with pytest.raises(CloneSourceError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_firecrawl_capture_empty_html_raises():
    body = _firecrawl_success_body(raw_html="")
    body["data"]["html"] = ""
    client = _MockHttpxClient(_MockResponse(body=body))
    src = FirecrawlSource(api_key="fc-test", client=client)

    with pytest.raises(CloneSourceError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_firecrawl_capture_oversize_html_raises():
    big = "x" * 10
    body = _firecrawl_success_body(raw_html=big)
    client = _MockHttpxClient(_MockResponse(body=body))
    src = FirecrawlSource(api_key="fc-test", client=client)

    with pytest.raises(CloneSourceError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=4)


@pytest.mark.asyncio
async def test_firecrawl_capture_transport_error_mapped_to_clone_source_error():
    class _BoomConnect(Exception):
        pass

    client = _MockHttpxClient(raises=_BoomConnect("connect refused"))
    src = FirecrawlSource(api_key="fc-test", client=client)

    with pytest.raises(CloneSourceError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_firecrawl_capture_timeout_named_exception_mapped_to_typed_timeout():
    class _BoomConnectTimeout(Exception):
        pass

    client = _MockHttpxClient(raises=_BoomConnectTimeout("read timeout"))
    src = FirecrawlSource(api_key="fc-test", client=client)

    with pytest.raises(CloneCaptureTimeoutError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_firecrawl_capture_asyncio_timeout_mapped_to_typed_timeout():
    client = _MockHttpxClient(raises=asyncio.TimeoutError())
    src = FirecrawlSource(api_key="fc-test", client=client)

    with pytest.raises(CloneCaptureTimeoutError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


# ── FirecrawlSource: lifecycle ───────────────────────────────────────

@pytest.mark.asyncio
async def test_firecrawl_aclose_does_not_close_injected_client():
    client = _MockHttpxClient(_MockResponse(body=_firecrawl_success_body()))
    src = FirecrawlSource(api_key="fc-test", client=client)

    await src.aclose()
    assert client.closed is False  # backend doesn't own the client


@pytest.mark.asyncio
async def test_firecrawl_async_context_manager():
    client = _MockHttpxClient(_MockResponse(body=_firecrawl_success_body()))
    async with FirecrawlSource(api_key="fc-test", client=client) as src:
        cap = await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)
        assert cap.backend == FIRECRAWL_BACKEND_NAME
    # Injected client must NOT be closed by aclose().
    assert client.closed is False


# ── Playwright test doubles ───────────────────────────────────────────

class _MockPlaywrightResponse:
    def __init__(self, *, status: int = 200, headers: Optional[dict[str, str]] = None,
                 url: str = "https://example.com"):
        self.status = status
        self.url = url
        self._headers = dict(headers or {})

    async def all_headers(self) -> dict[str, str]:
        return dict(self._headers)


class _MockPage:
    def __init__(self, *, html: str, response: _MockPlaywrightResponse,
                 final_url: str, asset_urls: list[str],
                 goto_raises: Optional[BaseException] = None):
        self._html = html
        self._response = response
        self._asset_urls = list(asset_urls)
        self._goto_raises = goto_raises
        self.url = final_url
        self._listener = None
        self.closed = False
        self.goto_calls: list[dict[str, Any]] = []

    def on(self, event: str, fn) -> None:
        assert event == "response"
        self._listener = fn

    async def goto(self, url: str, *, timeout: int, wait_until: str):
        self.goto_calls.append({"url": url, "timeout": timeout, "wait_until": wait_until})
        if self._goto_raises is not None:
            raise self._goto_raises
        # Simulate firing the response listener for each discovered asset.
        if self._listener is not None:
            for a in self._asset_urls:
                self._listener(_MockPlaywrightResponse(url=a, status=200))
        return self._response

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        self.closed = True


class _MockContext:
    def __init__(self, page: _MockPage):
        self._page = page
        self.closed = False
        self.new_context_kwargs: dict[str, Any] = {}

    async def new_page(self) -> _MockPage:
        return self._page

    async def close(self) -> None:
        self.closed = True


class _MockBrowser:
    def __init__(self, page: _MockPage):
        self._page = page
        self.closed = False
        self.launch_kwargs: dict[str, Any] = {}

    async def new_context(self, **kwargs):
        ctx = _MockContext(self._page)
        ctx.new_context_kwargs = dict(kwargs)
        self.last_context = ctx
        return ctx

    async def close(self) -> None:
        self.closed = True


class _MockBrowserType:
    def __init__(self, browser: _MockBrowser, *, launch_raises: Optional[BaseException] = None):
        self._browser = browser
        self._launch_raises = launch_raises
        self.launch_calls: list[dict[str, Any]] = []

    async def launch(self, **kwargs):
        self.launch_calls.append(dict(kwargs))
        if self._launch_raises is not None:
            raise self._launch_raises
        self._browser.launch_kwargs = dict(kwargs)
        return self._browser


class _MockPlaywright:
    """Stand-in for the entered ``async_playwright()`` object exposing
    ``chromium`` / ``firefox`` / ``webkit`` browser-type attributes."""

    def __init__(self, browser_type: _MockBrowserType, *, browser_attr: str = "chromium"):
        setattr(self, browser_attr, browser_type)


class _MockPlaywrightCtx:
    """Duck-typed ``async_playwright()`` context manager."""

    def __init__(self, pw: _MockPlaywright):
        self._pw = pw
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self._pw

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return None


def _build_playwright_factory(
    *,
    html: str = "<html><title>Hi</title></html>",
    final_url: str = "https://example.com/final",
    status: int = 200,
    asset_urls: Optional[list[str]] = None,
    response_headers: Optional[dict[str, str]] = None,
    goto_raises: Optional[BaseException] = None,
    launch_raises: Optional[BaseException] = None,
    browser_attr: str = "chromium",
):
    """Return a callable suitable for ``PlaywrightSource(playwright_factory=...)``."""
    response = _MockPlaywrightResponse(
        status=status,
        headers=response_headers or {"X-Robots-Tag": "noai"},
        url=final_url,
    )
    page = _MockPage(
        html=html,
        response=response,
        final_url=final_url,
        asset_urls=asset_urls or [],
        goto_raises=goto_raises,
    )
    browser = _MockBrowser(page)
    browser_type = _MockBrowserType(browser, launch_raises=launch_raises)
    pw = _MockPlaywright(browser_type, browser_attr=browser_attr)
    ctx = _MockPlaywrightCtx(pw)

    handle = {"page": page, "browser": browser, "browser_type": browser_type, "ctx": ctx}

    def factory():
        return ctx

    return factory, handle


# ── PlaywrightSource: construction ────────────────────────────────────

def test_playwright_source_satisfies_protocol():
    factory, _ = _build_playwright_factory()
    src = PlaywrightSource(playwright_factory=factory)
    assert isinstance(src, CloneSource)
    assert src.name == PLAYWRIGHT_BACKEND_NAME


def test_playwright_default_browser_is_chromium():
    factory, _ = _build_playwright_factory()
    src = PlaywrightSource(playwright_factory=factory)
    assert src._browser_name == DEFAULT_BROWSER == "chromium"  # noqa: SLF001


def test_playwright_explicit_browser_overrides_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_PLAYWRIGHT_BROWSER", "firefox")
    factory, _ = _build_playwright_factory(browser_attr="webkit")
    src = PlaywrightSource(browser="webkit", playwright_factory=factory)
    assert src._browser_name == "webkit"  # noqa: SLF001


def test_playwright_env_browser(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_PLAYWRIGHT_BROWSER", "firefox")
    factory, _ = _build_playwright_factory(browser_attr="firefox")
    src = PlaywrightSource(playwright_factory=factory)
    assert src._browser_name == "firefox"  # noqa: SLF001


def test_playwright_unsupported_browser_rejected():
    with pytest.raises(PlaywrightConfigError):
        PlaywrightSource(browser="trident")


def test_playwright_supported_browsers_exact_set():
    assert SUPPORTED_BROWSERS == frozenset({"chromium", "firefox", "webkit"})


def test_playwright_default_launch_args_hardened():
    factory, _ = _build_playwright_factory()
    src = PlaywrightSource(playwright_factory=factory)
    # Required for unprivileged Docker (no SYS_ADMIN); /dev/shm 64 MB workaround.
    assert "--no-sandbox" in src._launch_args  # noqa: SLF001
    assert "--disable-dev-shm-usage" in src._launch_args  # noqa: SLF001


# ── PlaywrightSource: capture happy path ─────────────────────────────

@pytest.mark.asyncio
async def test_playwright_capture_returns_raw_capture():
    factory, h = _build_playwright_factory(
        html="<html><title>Hello</title></html>",
        final_url="https://example.com/final",
        status=200,
        asset_urls=["https://example.com/a.png", "https://example.com/b.css"],
        response_headers={"X-Robots-Tag": "noai"},
    )
    src = PlaywrightSource(playwright_factory=factory)

    cap = await src.capture("https://example.com", timeout_s=10.0, max_html_bytes=5_000_000)
    await src.aclose()

    assert isinstance(cap, RawCapture)
    assert cap.backend == PLAYWRIGHT_BACKEND_NAME
    assert cap.url == "https://example.com/final"
    assert cap.status_code == 200
    assert "<title>Hello</title>" in cap.html
    assert cap.asset_urls == (
        "https://example.com/a.png",
        "https://example.com/b.css",
    )
    assert cap.headers["x-robots-tag"] == "noai"
    assert cap.fetched_at.endswith("Z")


@pytest.mark.asyncio
async def test_playwright_capture_goto_args():
    factory, h = _build_playwright_factory()
    src = PlaywrightSource(playwright_factory=factory)
    await src.capture("https://example.com", timeout_s=10.0, max_html_bytes=5_000_000)
    await src.aclose()

    assert len(h["page"].goto_calls) == 1
    args = h["page"].goto_calls[0]
    assert args["url"] == "https://example.com"
    assert args["wait_until"] == DEFAULT_WAIT_UNTIL
    # Internal timeout sits below outer budget — ms, positive.
    assert isinstance(args["timeout"], int)
    assert args["timeout"] > 0
    assert args["timeout"] < 10_000


@pytest.mark.asyncio
async def test_playwright_capture_new_context_locks_locale_and_blocks_service_workers():
    factory, h = _build_playwright_factory()
    src = PlaywrightSource(playwright_factory=factory)
    await src.capture("https://example.com", timeout_s=10.0, max_html_bytes=5_000_000)
    await src.aclose()

    kwargs = h["browser"].last_context.new_context_kwargs
    assert kwargs["locale"] == "en-US"
    assert kwargs["service_workers"] == "block"
    assert kwargs["ignore_https_errors"] is False
    assert kwargs["user_agent"].startswith("OmniSight-Productizer/")


@pytest.mark.asyncio
async def test_playwright_capture_dedupes_assets_and_skips_main_document():
    factory, h = _build_playwright_factory(
        asset_urls=[
            "https://example.com/a.png",
            "https://example.com/a.png",   # duplicate within same nav
            "https://example.com",          # main doc — must be skipped
        ],
    )
    src = PlaywrightSource(playwright_factory=factory)
    cap = await src.capture("https://example.com", timeout_s=10.0, max_html_bytes=5_000_000)
    await src.aclose()

    assert cap.asset_urls == ("https://example.com/a.png",)


@pytest.mark.asyncio
async def test_playwright_capture_amortises_browser_across_calls():
    factory, h = _build_playwright_factory()
    src = PlaywrightSource(playwright_factory=factory)

    await src.capture("https://example.com", timeout_s=10.0, max_html_bytes=5_000_000)
    await src.capture("https://example.com", timeout_s=10.0, max_html_bytes=5_000_000)

    # Browser launched exactly once — second capture reused it.
    assert len(h["browser_type"].launch_calls) == 1

    await src.aclose()
    assert h["browser"].closed is True
    assert h["ctx"].exited is True


# ── PlaywrightSource: error mapping ───────────────────────────────────

@pytest.mark.asyncio
async def test_playwright_capture_rejects_non_http_url():
    factory, _ = _build_playwright_factory()
    src = PlaywrightSource(playwright_factory=factory)
    with pytest.raises(CloneSourceError):
        await src.capture("ftp://example.com/", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_playwright_capture_goto_timeout_mapped_to_typed_timeout():
    class TimeoutError_(Exception):
        pass

    factory, _ = _build_playwright_factory(goto_raises=TimeoutError_("nav too slow"))
    src = PlaywrightSource(playwright_factory=factory)
    with pytest.raises(CloneCaptureTimeoutError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_playwright_capture_goto_other_error_mapped_to_clone_source_error():
    class _BadNav(Exception):
        pass

    factory, _ = _build_playwright_factory(goto_raises=_BadNav("dns lookup failed"))
    src = PlaywrightSource(playwright_factory=factory)
    with pytest.raises(CloneSourceError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_playwright_capture_oversize_html_raises():
    factory, _ = _build_playwright_factory(html="x" * 100)
    src = PlaywrightSource(playwright_factory=factory)
    with pytest.raises(CloneSourceError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=10)


@pytest.mark.asyncio
async def test_playwright_dependency_error_when_factory_missing_and_no_pkg(monkeypatch):
    """If the operator hasn't run ``pip install playwright`` and didn't
    inject a factory, the backend must surface the typed install-hint
    error rather than a generic ``ImportError``."""
    # Hide playwright from import system.
    import sys
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    src = PlaywrightSource()  # no factory → forces lazy import
    with pytest.raises(PlaywrightDependencyError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_playwright_browser_binary_missing_mapped_to_dependency_error():
    """A ``BrowserType.launch`` raising "Executable doesn't exist" must
    surface ``PlaywrightDependencyError`` so the operator knows to run
    ``playwright install chromium``, not chase a generic capture
    failure."""
    factory, h = _build_playwright_factory(
        launch_raises=Exception("Executable doesn't exist at /home/.cache/ms-playwright/..."),
    )
    src = PlaywrightSource(playwright_factory=factory)
    with pytest.raises(PlaywrightDependencyError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_playwright_browser_attr_missing_mapped_to_config_error():
    """If the entered playwright object lacks the requested browser
    attribute (operator typo'd ``OMNISIGHT_PLAYWRIGHT_BROWSER`` to a
    valid set member but the local playwright build doesn't expose it),
    that's a config problem, not a missing dependency."""
    factory, _ = _build_playwright_factory(browser_attr="firefox")
    src = PlaywrightSource(browser="webkit", playwright_factory=factory)
    with pytest.raises(PlaywrightConfigError):
        await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)


@pytest.mark.asyncio
async def test_playwright_aclose_idempotent():
    factory, h = _build_playwright_factory()
    src = PlaywrightSource(playwright_factory=factory)
    await src.capture("https://example.com", timeout_s=5.0, max_html_bytes=5_000_000)
    await src.aclose()
    await src.aclose()  # second call must not raise
    assert h["browser"].closed is True


# ── make_clone_source factory ─────────────────────────────────────────

def test_known_clone_backends_set_pinned():
    assert KNOWN_CLONE_BACKENDS == frozenset({"firecrawl", "playwright"})


def test_make_clone_source_explicit_firecrawl(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_FIRECRAWL_API_KEY", "fc-test")
    src = make_clone_source("firecrawl")
    assert isinstance(src, FirecrawlSource)


def test_make_clone_source_explicit_playwright():
    src = make_clone_source("playwright")
    assert isinstance(src, PlaywrightSource)


def test_make_clone_source_unknown_backend_rejected():
    with pytest.raises(UnknownCloneBackendError):
        make_clone_source("selenium")


def test_make_clone_source_env_resolves(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_CLONE_BACKEND", "playwright")
    monkeypatch.delenv("OMNISIGHT_FIRECRAWL_API_KEY", raising=False)
    src = make_clone_source()
    assert isinstance(src, PlaywrightSource)


def test_make_clone_source_auto_prefers_firecrawl_when_key_present(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_CLONE_BACKEND", raising=False)
    monkeypatch.setenv("OMNISIGHT_FIRECRAWL_API_KEY", "fc-test")
    src = make_clone_source()
    assert isinstance(src, FirecrawlSource)


def test_make_clone_source_auto_falls_back_to_playwright_without_key(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_CLONE_BACKEND", raising=False)
    monkeypatch.delenv("OMNISIGHT_FIRECRAWL_API_KEY", raising=False)
    src = make_clone_source()
    assert isinstance(src, PlaywrightSource)


def test_make_clone_source_settings_object_resolves(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_CLONE_BACKEND", raising=False)
    monkeypatch.delenv("OMNISIGHT_FIRECRAWL_API_KEY", raising=False)

    class _Settings:
        clone_backend = "firecrawl"
        firecrawl_api_key = "fc-from-settings"
        firecrawl_base_url = ""

    src = make_clone_source(settings=_Settings())
    assert isinstance(src, FirecrawlSource)
    assert src._api_key == "fc-from-settings"  # noqa: SLF001


def test_make_clone_source_explicit_arg_beats_env_and_settings(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_CLONE_BACKEND", "playwright")

    class _Settings:
        clone_backend = "firecrawl"
        firecrawl_api_key = "fc-from-settings"
        firecrawl_base_url = ""
        playwright_browser = ""

    src = make_clone_source("playwright", settings=_Settings())
    assert isinstance(src, PlaywrightSource)
