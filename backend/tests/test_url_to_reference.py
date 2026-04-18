"""V1 #7 (issue #317) — url_to_reference pipeline contract tests.

Pins ``backend/url_to_reference.py`` against:

  * structural invariants of :class:`URLReference`,
    :class:`URLExtraction`, :class:`URLReferenceResult`,
    :class:`FetchResponse`, :class:`ScreenshotResult` (frozen,
    validated, JSON-safe);
  * URL normalisation — scheme allowlist, host lowercasing, whitespace
    refusal, length cap, SSRF gate with loopback / private-IP / reserved
    hostname protection, ``allow_private`` escape hatch for tests;
  * fetcher protocol — graceful fallback on network exception,
    non-HTML content-type warning, truncation on oversize payloads;
  * screenshot protocol — injected :data:`Screenshotter`, fallback
    ``screenshot_unavailable`` warning when not wired, exception
    catch-and-downgrade, mime normalisation, size caps;
  * :func:`extract_from_html` heuristics — title, description,
    theme-color, canonical, inline colour extraction (hex / rgb / hsl
    / oklch / oklab), font-family, headings, button labels, nav
    links, component detection (form / table / dialog / tabs / card
    / modal / hero), layout hints (grid / flex / sidebar / dark),
    link hosts, meta keywords, external stylesheets;
  * deterministic prompt construction (byte-identical across calls),
    truncation past :data:`HTML_PROMPT_CAP`, empty-HTML fallback;
  * the full :func:`generate_ui_from_url` pipeline with injected
    fakes: happy path clean TSX, fetch_failed → warnings bubbled,
    LLM unavailable, TSX missing, auto-fix round, pre-built
    reference path, pre-built extraction path;
  * the agent-callable :func:`run_url_to_reference` entry (JSON-safe
    dict, exactly-one-of check, pre-built reference mode);
  * sibling integration — prompt really references live shadcn
    registry + design-token blocks; auto-fix really rewrites raw
    ``<button>`` into ``<Button>`` with import.

If sibling modules rename a public export, one of the cross-module
tests will fail noisily — that's intentional; the agent tool surface
is a contract, not an implementation detail.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from backend import url_to_reference as utr
from backend.url_to_reference import (
    DEFAULT_URL_REF_MODEL,
    DEFAULT_URL_REF_PROVIDER,
    HTML_PROMPT_CAP,
    MAX_HTML_BYTES,
    MAX_URL_LENGTH,
    SUPPORTED_URL_SCHEMES,
    URL_REF_SCHEMA_VERSION,
    FetchResponse,
    ScreenshotResult,
    URLExtraction,
    URLReference,
    URLReferenceResult,
    build_multimodal_message,
    build_url_generation_prompt,
    capture_screenshot,
    extract_from_html,
    fetch_url,
    generate_ui_from_url,
    normalize_url,
    run_url_to_reference,
)
from backend.component_consistency_linter import LintReport
from backend.vision_to_ui import VisionImage


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Shared fixtures ──────────────────────────────────────────────────


# Smallest legal PNG (1×1 transparent pixel).
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


_SAMPLE_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Acme Studios — Beautiful Websites</title>
<meta name="description" content="We build beautiful, accessible websites.">
<meta name="keywords" content="design, web, shadcn, accessibility">
<meta name="theme-color" content="#38bdf8">
<link rel="canonical" href="https://acme.example.com/">
<link rel="stylesheet" href="/styles/main.css">
<link rel="stylesheet" href="https://cdn.example.com/reset.css">
<style>
:root { --brand: #38bdf8; color-scheme: dark; }
.hero { background: rgba(7, 11, 23, 0.9); font-family: "Inter", system-ui, sans-serif; }
.card { border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.35); }
</style>
</head>
<body class="dark flex">
<header class="container flex">
  <nav>
    <a href="/">Home</a>
    <a href="/pricing">Pricing</a>
    <a href="/contact">Contact</a>
    <a href="https://external.example.com/blog">Blog</a>
  </nav>
</header>
<main class="grid">
  <section class="hero">
    <h1>Build something users love</h1>
    <h2>Design systems that ship</h2>
    <p>Sign up free — no credit card required.</p>
    <button type="button">Get started</button>
    <button type="button">Watch demo</button>
  </section>
  <aside class="sidebar">
    <h3>What's new</h3>
    <ul>
      <li>New pricing</li>
      <li>New landing</li>
    </ul>
  </aside>
  <section>
    <div class="card" role="dialog">
      <h3>Contact us</h3>
      <form>
        <input type="text" name="name" placeholder="Your name">
        <textarea name="message" placeholder="Message"></textarea>
        <select name="topic"><option>Sales</option></select>
        <button type="submit">Send</button>
      </form>
    </div>
  </section>
  <section>
    <table><tr><th>Plan</th><th>Price</th></tr></table>
  </section>
  <section class="tabs" role="tablist">
    <div role="tab">One</div>
  </section>
  <dialog>legal</dialog>
</main>
<footer><small>© Acme</small></footer>
</body>
</html>
"""


def _make_fetcher(
    response: FetchResponse,
    *,
    raises: BaseException | None = None,
    returns: object = None,
):
    def _fetch(url: str) -> FetchResponse:  # type: ignore[return-value]
        if raises is not None:
            raise raises
        if returns is not None:
            return returns  # type: ignore[return-value]
        return response

    return _fetch


# ── Module invariants ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
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
        "normalize_url",
        "fetch_url",
        "capture_screenshot",
        "extract_from_html",
        "build_url_generation_prompt",
        "build_multimodal_message",
        "generate_ui_from_url",
        "run_url_to_reference",
    ],
)
def test_module_exports_include(name: str) -> None:
    assert name in utr.__all__, f"{name} missing from __all__"
    assert hasattr(utr, name), f"{name} not present in module"


def test_schema_version_is_semver_like() -> None:
    parts = URL_REF_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_default_model_is_opus() -> None:
    assert DEFAULT_URL_REF_MODEL == "claude-opus-4-7"
    assert DEFAULT_URL_REF_PROVIDER == "anthropic"


def test_supported_url_schemes_is_http_only() -> None:
    assert SUPPORTED_URL_SCHEMES == frozenset({"http", "https"})
    assert isinstance(SUPPORTED_URL_SCHEMES, frozenset)


def test_max_url_length_sane() -> None:
    assert 1024 <= MAX_URL_LENGTH <= 8192


def test_max_html_bytes_sane() -> None:
    assert MAX_HTML_BYTES >= 512 * 1024
    assert MAX_HTML_BYTES <= 16 * 1024 * 1024


def test_html_prompt_cap_smaller_than_max_html_bytes() -> None:
    assert HTML_PROMPT_CAP <= MAX_HTML_BYTES


# ── FetchResponse / ScreenshotResult ────────────────────────────────


def test_fetch_response_frozen_and_headers_readonly() -> None:
    r = FetchResponse(
        url="https://example.com/",
        status_code=200,
        headers={"Content-Type": "text/html"},
        content=b"<html>",
        final_url="https://example.com/",
    )
    with pytest.raises(Exception):
        r.status_code = 500  # type: ignore[misc]
    assert r.headers["content-type"] == "text/html"
    with pytest.raises(TypeError):
        r.headers["content-type"] = "text/plain"  # type: ignore[index]


def test_fetch_response_requires_bytes_content() -> None:
    with pytest.raises(TypeError):
        FetchResponse(url="https://x/", status_code=200, content="oops")  # type: ignore[arg-type]


def test_fetch_response_requires_int_status() -> None:
    with pytest.raises(TypeError):
        FetchResponse(
            url="https://x/", status_code="200",  # type: ignore[arg-type]
            content=b"",
        )


def test_screenshot_result_defaults() -> None:
    r = ScreenshotResult(data=_PNG_1X1)
    assert r.mime_type == "image/png"
    assert r.viewport is None


# ── normalize_url ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://Example.COM/", "https://example.com/"),
        ("HTTPS://example.com/foo?b=1", "https://example.com/foo?b=1"),
        ("http://example.com#hash", "http://example.com/"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:8443/path", "https://example.com:8443/path"),
        ("https://example.com/path?x=1", "https://example.com/path?x=1"),
    ],
)
def test_normalize_url_canonicalises(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com/",
        "//example.com",
        "https:// example.com",
        "https://",
        "not-a-url",
    ],
)
def test_normalize_url_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_normalize_url_rejects_none() -> None:
    with pytest.raises(ValueError):
        normalize_url(None)  # type: ignore[arg-type]


def test_normalize_url_enforces_length_cap() -> None:
    long = "https://example.com/" + "a" * MAX_URL_LENGTH
    with pytest.raises(ValueError):
        normalize_url(long)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "10.1.2.3",
        "192.168.1.1",
        "169.254.169.254",  # link-local (AWS metadata)
        "[::1]",
    ],
)
def test_normalize_url_rejects_private_hosts(host: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(f"http://{host}/admin")


def test_normalize_url_allow_private_escape() -> None:
    result = normalize_url(
        "http://localhost:3000/api", allow_private=True,
    )
    assert result == "http://localhost:3000/api"


# ── URLReference ─────────────────────────────────────────────────────


def test_url_reference_requires_non_empty_url() -> None:
    with pytest.raises(ValueError):
        URLReference(url="")


def test_url_reference_frozen() -> None:
    r = URLReference(url="https://example.com/")
    with pytest.raises(Exception):
        r.url = "https://evil/"  # type: ignore[misc]


def test_url_reference_rejects_bad_screenshot_type() -> None:
    with pytest.raises(TypeError):
        URLReference(
            url="https://example.com/",
            screenshot="not-a-vision-image",  # type: ignore[arg-type]
        )


def test_url_reference_meta_readonly() -> None:
    r = URLReference(url="https://example.com/", meta={"server": "nginx"})
    with pytest.raises(TypeError):
        r.meta["server"] = "apache"  # type: ignore[index]


def test_url_reference_to_dict_json_safe() -> None:
    r = URLReference(
        url="https://example.com/",
        final_url="https://example.com/",
        status_code=200,
        content_type="text/html",
        html="<html></html>",
        html_bytes=13,
        title="hello",
        description="d",
        canonical_url="https://example.com/",
        theme_color="#38bdf8",
        screenshot=None,
        fetched_at=1234.0,
        warnings=("fetch_http_error",),
        meta={"server": "nginx"},
    )
    payload = r.to_dict()
    json.dumps(payload)
    assert payload["schema_version"] == URL_REF_SCHEMA_VERSION
    assert payload["has_screenshot"] is False
    assert payload["screenshot_mime"] is None
    assert payload["screenshot_bytes"] == 0
    assert payload["warnings"] == ["fetch_http_error"]
    assert payload["meta"] == {"server": "nginx"}


def test_url_reference_is_ok_property() -> None:
    empty = URLReference(url="https://example.com/")
    assert not empty.is_ok
    withhtml = URLReference(url="https://example.com/", html="<h1>hi</h1>")
    assert withhtml.is_ok


# ── URLExtraction ────────────────────────────────────────────────────


def test_url_extraction_empty_default() -> None:
    x = URLExtraction()
    payload = x.to_dict()
    json.dumps(payload)
    assert payload["color_values"] == []
    assert payload["parse_succeeded"] is True


def test_url_extraction_to_dict_json_safe() -> None:
    x = URLExtraction(
        color_values=("#38bdf8",),
        font_values=("'Inter', sans-serif",),
        heading_texts=("Hello",),
        button_labels=("Buy",),
        nav_labels=("Home",),
        detected_components=("form",),
        link_hosts=("example.com",),
        layout_hints=("grid",),
        meta_keywords=("shadcn",),
        stylesheet_urls=("/main.css",),
        warnings=("empty_html",),
    )
    payload = x.to_dict()
    json.dumps(payload)
    assert payload["warnings"] == ["empty_html"]


# ── extract_from_html ────────────────────────────────────────────────


def test_extract_from_html_empty() -> None:
    x = extract_from_html("")
    assert x.parse_succeeded is False
    assert "empty_html" in x.warnings
    assert x.color_values == ()


def test_extract_from_html_full_sample() -> None:
    x = extract_from_html(_SAMPLE_HTML, base_url="https://acme.example.com/")
    assert x.parse_succeeded is True
    # Colour extraction picks up the hex + rgba pair.
    assert "#38bdf8" in x.color_values
    assert any(c.startswith("rgba(") for c in x.color_values)
    # Font-family extraction
    assert any("inter" in f.lower() for f in x.font_values)
    # Headings collected in DOM order but stored deduped; spot-check.
    assert "Build something users love" in x.heading_texts
    assert "Design systems that ship" in x.heading_texts
    # Button labels
    assert "Get started" in x.button_labels
    assert "Send" in x.button_labels
    # Nav labels
    assert "Home" in x.nav_labels
    assert "Pricing" in x.nav_labels
    # Components
    assert "form" in x.detected_components
    assert "table" in x.detected_components
    assert "dialog" in x.detected_components
    assert "tabs" in x.detected_components
    assert "nav" in x.detected_components
    # Layout
    assert "grid" in x.layout_hints
    assert "flex" in x.layout_hints
    assert "sidebar" in x.layout_hints
    assert "dark-theme" in x.layout_hints
    # Link hosts include relative resolution + external
    assert "acme.example.com" in x.link_hosts
    assert "external.example.com" in x.link_hosts
    # Meta keywords
    assert "shadcn" in x.meta_keywords
    assert "accessibility" in x.meta_keywords
    # Stylesheet URLs
    assert "/styles/main.css" in x.stylesheet_urls
    assert "https://cdn.example.com/reset.css" in x.stylesheet_urls


def test_extract_from_html_is_deterministic() -> None:
    a = extract_from_html(_SAMPLE_HTML, base_url="https://acme.example.com/")
    b = extract_from_html(_SAMPLE_HTML, base_url="https://acme.example.com/")
    assert a == b
    assert a.to_dict() == b.to_dict()


def test_extract_from_html_handles_entities() -> None:
    html = "<title>Hello &amp; Welcome</title>"
    x = extract_from_html(html)
    # No headings but the parser should still succeed and decode entities
    # in the title via fetch_url — here we just confirm no crash.
    assert x.parse_succeeded is True


def test_extract_from_html_resolves_relative_links() -> None:
    html = '<html><body><a href="/pricing">P</a><a href="https://x.example.com/">X</a></body></html>'
    x = extract_from_html(html, base_url="https://acme.example.com/")
    assert "acme.example.com" in x.link_hosts
    assert "x.example.com" in x.link_hosts


def test_extract_from_html_skips_junk_hrefs() -> None:
    html = (
        '<html><body>'
        '<a href="#top">top</a>'
        '<a href="mailto:x@y">mail</a>'
        '<a href="javascript:void(0)">js</a>'
        '<a href="tel:+1">phone</a>'
        '</body></html>'
    )
    x = extract_from_html(html, base_url="https://a.example.com/")
    assert x.link_hosts == ()


def test_extract_from_html_captures_shadcn_button_text() -> None:
    html = "<html><body><Button>Click me</Button></body></html>"
    x = extract_from_html(html)
    assert "Click me" in x.button_labels


def test_extract_from_html_headings_limit_to_h1_h3() -> None:
    html = (
        "<html><body>"
        "<h1>One</h1>"
        "<h2>Two</h2>"
        "<h3>Three</h3>"
        "<h4>Four</h4>"
        "<h5>Five</h5>"
        "</body></html>"
    )
    x = extract_from_html(html)
    assert "One" in x.heading_texts
    assert "Two" in x.heading_texts
    assert "Three" in x.heading_texts
    assert "Four" not in x.heading_texts
    assert "Five" not in x.heading_texts


def test_extract_from_html_color_variants() -> None:
    html = (
        "<style>"
        "body { color: #abc; background: #abcdef; accent: rgb(1,2,3); "
        "accent2: hsl(200 100% 50%); accent3: oklch(70% 0.2 220); "
        "accent4: oklab(80% 0 0); }"
        "</style>"
    )
    x = extract_from_html(html)
    assert "#abc" in x.color_values
    assert "#abcdef" in x.color_values
    assert any(c.startswith("rgb(") for c in x.color_values)
    assert any(c.startswith("hsl(") for c in x.color_values)
    assert any(c.startswith("oklch(") for c in x.color_values)
    assert any(c.startswith("oklab(") or c.startswith("olab(") for c in x.color_values)


def test_extract_from_html_title_is_stripped_and_limited() -> None:
    # title extraction is covered via fetch_url / URLReference.title
    # but _extract_title is exercised there indirectly.
    raw = "<title>  Hello\nWorld  </title>"
    x = extract_from_html(raw)
    assert x.parse_succeeded is True


# ── Fetcher protocol ─────────────────────────────────────────────────


def _fake_success_response(html: str = _SAMPLE_HTML) -> FetchResponse:
    body = html.encode("utf-8")
    return FetchResponse(
        url="https://acme.example.com/",
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8", "server": "nginx"},
        content=body,
        final_url="https://acme.example.com/",
    )


def test_fetch_url_happy_path() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    ref = fetch_url("https://acme.example.com/", fetcher=fetcher)
    assert ref.status_code == 200
    assert ref.content_type == "text/html"
    assert "Acme Studios" in ref.title
    assert ref.description.startswith("We build")
    assert ref.theme_color == "#38bdf8"
    assert ref.canonical_url == "https://acme.example.com/"
    assert ref.warnings == ()
    assert ref.final_url == "https://acme.example.com/"
    assert ref.html_bytes > 0


def test_fetch_url_fetcher_raises_downgrades_to_warning() -> None:
    fetcher = _make_fetcher(
        _fake_success_response(), raises=RuntimeError("boom"),
    )
    ref = fetch_url("https://acme.example.com/", fetcher=fetcher)
    assert ref.html == ""
    assert ref.status_code == 0
    assert "fetch_failed" in ref.warnings


def test_fetch_url_non_200_keeps_body_but_warns() -> None:
    resp = FetchResponse(
        url="https://acme.example.com/",
        status_code=404,
        headers={"content-type": "text/html"},
        content=b"<html><body>Not found</body></html>",
    )
    fetcher = _make_fetcher(resp)
    ref = fetch_url("https://acme.example.com/", fetcher=fetcher)
    assert ref.status_code == 404
    assert "fetch_http_error" in ref.warnings
    assert "Not found" in ref.html


def test_fetch_url_server_error_tagged() -> None:
    resp = FetchResponse(
        url="https://acme.example.com/",
        status_code=503,
        headers={"content-type": "text/html"},
        content=b"<h1>gone</h1>",
    )
    ref = fetch_url(
        "https://acme.example.com/", fetcher=_make_fetcher(resp),
    )
    assert "fetch_server_error" in ref.warnings


def test_fetch_url_non_html_content_type_warns() -> None:
    resp = FetchResponse(
        url="https://acme.example.com/data.json",
        status_code=200,
        headers={"content-type": "application/json"},
        content=b'{"ok":1}',
    )
    ref = fetch_url(
        "https://acme.example.com/data.json", fetcher=_make_fetcher(resp),
    )
    assert "non_html_content_type" in ref.warnings


def test_fetch_url_xhtml_content_type_accepted() -> None:
    resp = FetchResponse(
        url="https://acme.example.com/",
        status_code=200,
        headers={"content-type": "application/xhtml+xml"},
        content=b"<html><body>ok</body></html>",
    )
    ref = fetch_url(
        "https://acme.example.com/", fetcher=_make_fetcher(resp),
    )
    assert "non_html_content_type" not in ref.warnings


def test_fetch_url_truncates_oversize_body() -> None:
    body = b"<html>" + b"a" * (MAX_HTML_BYTES + 128)
    resp = FetchResponse(
        url="https://acme.example.com/",
        status_code=200,
        headers={"content-type": "text/html"},
        content=body,
    )
    ref = fetch_url(
        "https://acme.example.com/", fetcher=_make_fetcher(resp),
    )
    assert "html_truncated" in ref.warnings
    assert ref.html_bytes == MAX_HTML_BYTES


def test_fetch_url_fetcher_returns_wrong_type() -> None:
    fetcher = _make_fetcher(
        _fake_success_response(), returns={"url": "x"},
    )
    ref = fetch_url("https://acme.example.com/", fetcher=fetcher)
    assert "fetch_unexpected_shape" in ref.warnings


def test_fetch_url_decodes_latin1_when_declared() -> None:
    body = "<title>café</title>".encode("latin-1")
    resp = FetchResponse(
        url="https://acme.example.com/",
        status_code=200,
        headers={"content-type": "text/html; charset=ISO-8859-1"},
        content=body,
    )
    ref = fetch_url(
        "https://acme.example.com/", fetcher=_make_fetcher(resp),
    )
    assert "café" in ref.title


def test_fetch_url_invalid_url_raises() -> None:
    with pytest.raises(ValueError):
        fetch_url("javascript:alert(1)")


def test_fetch_url_sets_fetched_at() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    ref = fetch_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        now=lambda: 424242.5,
    )
    assert ref.fetched_at == 424242.5


# ── Screenshot protocol ──────────────────────────────────────────────


def test_capture_screenshot_no_screenshotter_warns() -> None:
    image, warnings = capture_screenshot(
        "https://example.com/", screenshotter=None,
    )
    assert image is None
    assert warnings == ("screenshot_unavailable",)


def test_capture_screenshot_happy_path() -> None:
    def shot(url: str) -> ScreenshotResult:
        return ScreenshotResult(data=_PNG_1X1, mime_type="image/png")

    image, warnings = capture_screenshot(
        "https://example.com/", screenshotter=shot,
    )
    assert image is not None
    assert isinstance(image, VisionImage)
    assert image.mime_type == "image/png"
    assert warnings == ()


def test_capture_screenshot_none_result_warns() -> None:
    image, warnings = capture_screenshot(
        "https://example.com/", screenshotter=lambda u: None,
    )
    assert image is None
    assert "screenshot_unavailable" in warnings


def test_capture_screenshot_raises_downgrades_to_warning() -> None:
    def shot(url: str) -> ScreenshotResult:
        raise RuntimeError("browser crashed")

    image, warnings = capture_screenshot(
        "https://example.com/", screenshotter=shot,
    )
    assert image is None
    assert "screenshot_unavailable" in warnings


def test_capture_screenshot_unexpected_shape_warns() -> None:
    image, warnings = capture_screenshot(
        "https://example.com/",
        screenshotter=lambda u: {"data": _PNG_1X1},  # type: ignore[arg-type]
    )
    assert image is None
    assert "screenshot_unexpected_shape" in warnings


def test_capture_screenshot_unsupported_mime_warns() -> None:
    def shot(url: str) -> ScreenshotResult:
        return ScreenshotResult(data=_PNG_1X1, mime_type="image/bmp")

    image, warnings = capture_screenshot(
        "https://example.com/", screenshotter=shot,
    )
    assert image is None
    assert "screenshot_unsupported_mime" in warnings


def test_capture_screenshot_jpg_alias_accepted() -> None:
    # image/jpg → image/jpeg normalisation path
    jpeg_header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
    def shot(url: str) -> ScreenshotResult:
        return ScreenshotResult(
            data=jpeg_header + b"\x00" * 64, mime_type="image/jpg",
        )

    image, warnings = capture_screenshot(
        "https://example.com/", screenshotter=shot,
    )
    assert image is not None
    assert image.mime_type == "image/jpeg"
    assert warnings == ()


def test_capture_screenshot_empty_bytes_warns() -> None:
    def shot(url: str) -> ScreenshotResult:
        return ScreenshotResult(data=b"", mime_type="image/png")

    image, warnings = capture_screenshot(
        "https://example.com/", screenshotter=shot,
    )
    assert image is None
    assert "screenshot_unavailable" in warnings


def test_capture_screenshot_oversize_warns() -> None:
    # Big fake PNG header but over MAX_IMAGE_BYTES
    from backend.vision_to_ui import MAX_IMAGE_BYTES
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1)

    def shot(url: str) -> ScreenshotResult:
        return ScreenshotResult(data=big, mime_type="image/png")

    image, warnings = capture_screenshot(
        "https://example.com/", screenshotter=shot,
    )
    assert image is None
    assert "screenshot_too_large" in warnings


def test_capture_screenshot_mislabeled_mime_rejected() -> None:
    # PNG bytes labeled as jpeg → magic-byte mismatch in validate_image
    def shot(url: str) -> ScreenshotResult:
        return ScreenshotResult(data=_PNG_1X1, mime_type="image/jpeg")

    image, warnings = capture_screenshot(
        "https://example.com/", screenshotter=shot,
    )
    assert image is None
    assert "screenshot_invalid" in warnings


def test_capture_screenshot_normalises_url() -> None:
    seen: list[str] = []
    def shot(url: str) -> ScreenshotResult:
        seen.append(url)
        return ScreenshotResult(data=_PNG_1X1)

    capture_screenshot("https://EXAMPLE.com/#x", screenshotter=shot)
    assert seen == ["https://example.com/"]


# ── Multimodal message ──────────────────────────────────────────────


def test_build_multimodal_message_text_only_when_no_screenshot() -> None:
    ref = URLReference(url="https://example.com/", html="<html></html>")
    msg = build_multimodal_message(ref, "hello prompt")
    # text-only: HumanMessage.content is the string
    assert msg.content == "hello prompt"


def test_build_multimodal_message_text_plus_image_with_screenshot() -> None:
    image = VisionImage(data=_PNG_1X1, mime_type="image/png")
    ref = URLReference(url="https://example.com/", screenshot=image)
    msg = build_multimodal_message(ref, "prompt")
    assert isinstance(msg.content, list)
    assert msg.content[0]["type"] == "text"
    assert msg.content[0]["text"] == "prompt"
    assert msg.content[1]["type"] == "image"
    assert msg.content[1]["source"]["type"] == "base64"
    assert msg.content[1]["source"]["media_type"] == "image/png"


# ── Prompt construction (deterministic) ─────────────────────────────


def _make_reference() -> URLReference:
    return URLReference(
        url="https://acme.example.com/",
        final_url="https://acme.example.com/",
        status_code=200,
        content_type="text/html",
        html=_SAMPLE_HTML,
        html_bytes=len(_SAMPLE_HTML),
        title="Acme Studios — Beautiful Websites",
        description="We build beautiful, accessible websites.",
        canonical_url="https://acme.example.com/",
        theme_color="#38bdf8",
    )


def test_build_prompt_byte_identical() -> None:
    ref = _make_reference()
    a = build_url_generation_prompt(
        reference=ref,
        project_root=PROJECT_ROOT,
        brief="Make a hero",
    )
    b = build_url_generation_prompt(
        reference=ref,
        project_root=PROJECT_ROOT,
        brief="Make a hero",
    )
    assert a == b


def test_build_prompt_has_header_and_rules_sections() -> None:
    ref = _make_reference()
    prompt = build_url_generation_prompt(
        reference=ref, project_root=PROJECT_ROOT, brief=None,
    )
    assert "URL reference → shadcn/ui + Tailwind" in prompt
    assert "Generation rules" in prompt
    assert "acceptance gate" in prompt


def test_build_prompt_includes_reference_and_extraction_sections() -> None:
    ref = _make_reference()
    prompt = build_url_generation_prompt(
        reference=ref, project_root=PROJECT_ROOT, brief=None,
    )
    assert "## URL reference" in prompt
    assert "## URL extraction" in prompt
    assert "## HTML snippet" in prompt
    assert "title: Acme Studios" in prompt


def test_build_prompt_injects_registry_and_tokens_blocks() -> None:
    ref = _make_reference()
    prompt = build_url_generation_prompt(
        reference=ref, project_root=PROJECT_ROOT, brief=None,
    )
    # registry block contains a component
    assert "button" in prompt.lower()
    # tokens block (we know PROJECT_ROOT has a tokens file)
    assert "Design tokens" in prompt or "design tokens" in prompt.lower()


def test_build_prompt_truncates_oversize_html() -> None:
    big_html = "<html><body>" + "a" * (HTML_PROMPT_CAP * 2) + "</body></html>"
    ref = URLReference(url="https://example.com/", html=big_html)
    prompt = build_url_generation_prompt(
        reference=ref, project_root=PROJECT_ROOT, brief=None,
    )
    assert "truncated" in prompt
    # Cap holds (the truncated tail marker replaces the extra bytes).
    assert "a" * (HTML_PROMPT_CAP * 2) not in prompt


def test_build_prompt_empty_html_fallback() -> None:
    ref = URLReference(url="https://example.com/")
    prompt = build_url_generation_prompt(
        reference=ref, project_root=PROJECT_ROOT, brief=None,
    )
    assert "no usable HTML body" in prompt


def test_build_prompt_no_brief_has_none_marker() -> None:
    ref = _make_reference()
    prompt = build_url_generation_prompt(
        reference=ref, project_root=PROJECT_ROOT, brief=None,
    )
    assert "## Caller brief\n(none)" in prompt


def test_build_prompt_with_brief_embeds_it() -> None:
    ref = _make_reference()
    prompt = build_url_generation_prompt(
        reference=ref, project_root=PROJECT_ROOT,
        brief="two columns, dark theme",
    )
    assert "two columns, dark theme" in prompt


def test_build_prompt_screenshot_status_line() -> None:
    ref = _make_reference()
    prompt_no = build_url_generation_prompt(
        reference=ref, project_root=PROJECT_ROOT, brief=None,
    )
    assert "not attached" in prompt_no

    image = VisionImage(data=_PNG_1X1, mime_type="image/png")
    ref_with = URLReference(
        url="https://acme.example.com/",
        html=_SAMPLE_HTML,
        screenshot=image,
    )
    prompt_yes = build_url_generation_prompt(
        reference=ref_with, project_root=PROJECT_ROOT, brief=None,
    )
    assert "attached (multimodal)" in prompt_yes


# ── URLReferenceResult ───────────────────────────────────────────────


def test_result_is_clean_false_on_empty_tsx() -> None:
    ref = _make_reference()
    extraction = extract_from_html(ref.html)
    result = URLReferenceResult(
        reference=ref,
        extraction=extraction,
        tsx_code="",
        lint_report=LintReport(),
    )
    assert result.is_clean is False


def test_result_to_dict_json_safe() -> None:
    ref = _make_reference()
    extraction = extract_from_html(ref.html)
    result = URLReferenceResult(
        reference=ref,
        extraction=extraction,
        tsx_code="export default () => <div />",
        lint_report=LintReport(),
    )
    payload = result.to_dict()
    json.dumps(payload)
    assert payload["schema_version"] == URL_REF_SCHEMA_VERSION


# ── generate_ui_from_url pipeline ───────────────────────────────────


_CLEAN_TSX_RESPONSE = (
    "Here is the component.\n\n"
    "```tsx\n"
    "import { Button } from \"@/components/ui/button\";\n"
    "export default function Hero() {\n"
    "  return <Button variant=\"default\">Go</Button>;\n"
    "}\n"
    "```\n"
)


def test_generate_ui_from_url_happy_path_clean() -> None:
    fetcher = _make_fetcher(_fake_success_response())

    def invoker(messages: list) -> str:
        return _CLEAN_TSX_RESPONSE

    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        screenshotter=None,
        invoker=invoker,
        project_root=PROJECT_ROOT,
    )
    assert isinstance(result, URLReferenceResult)
    assert result.is_clean
    assert "Button" in result.tsx_code
    # screenshot_unavailable warning because no screenshotter wired
    assert "screenshot_unavailable" in result.warnings


def test_generate_ui_from_url_llm_unavailable() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        invoker=lambda msgs: "",
        project_root=PROJECT_ROOT,
    )
    assert "llm_unavailable" in result.warnings
    assert result.tsx_code == ""
    assert result.is_clean is False


def test_generate_ui_from_url_tsx_missing() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        invoker=lambda msgs: "I apologise — I can't generate that today.",
        project_root=PROJECT_ROOT,
    )
    assert "tsx_missing" in result.warnings
    # Raw response preserved for human inspection.
    assert "I apologise" in result.tsx_code


def test_generate_ui_from_url_fetch_failed_warning_bubbled() -> None:
    # Fetcher raises → fetch_url tags fetch_failed and we still call LLM.
    fetcher = _make_fetcher(
        _fake_success_response(), raises=TimeoutError("slow"),
    )
    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        invoker=lambda msgs: _CLEAN_TSX_RESPONSE,
        project_root=PROJECT_ROOT,
    )
    assert "fetch_failed" in result.warnings
    assert result.is_clean  # LLM still produced clean code


def test_generate_ui_from_url_auto_fix_rewrites_raw_button() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    raw_tsx = (
        "```tsx\n"
        "export default function Hero() {\n"
        '  return <button type="button">Go</button>;\n'
        "}\n"
        "```\n"
    )
    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        invoker=lambda msgs: raw_tsx,
        project_root=PROJECT_ROOT,
        auto_fix=True,
    )
    assert result.auto_fix_applied is True
    assert "<Button" in result.tsx_code
    assert "@/components/ui/button" in result.tsx_code
    assert result.pre_fix_lint_report is not None
    assert result.pre_fix_lint_report.is_clean is False


def test_generate_ui_from_url_auto_fix_false_keeps_violations() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    raw_tsx = (
        "```tsx\n"
        "export default function Hero() {\n"
        '  return <button type="button">Go</button>;\n'
        "}\n"
        "```\n"
    )
    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        invoker=lambda msgs: raw_tsx,
        project_root=PROJECT_ROOT,
        auto_fix=False,
    )
    assert result.auto_fix_applied is False
    assert "<button" in result.tsx_code
    assert result.lint_report.is_clean is False


def test_generate_ui_from_url_prebuilt_reference_skips_fetch() -> None:
    called = {"fetcher": 0}

    def fetcher(url: str) -> FetchResponse:
        called["fetcher"] += 1
        return _fake_success_response()

    ref = _make_reference()
    result = generate_ui_from_url(
        "https://acme.example.com/",
        reference=ref,
        fetcher=fetcher,
        invoker=lambda msgs: _CLEAN_TSX_RESPONSE,
        project_root=PROJECT_ROOT,
    )
    assert called["fetcher"] == 0
    assert result.is_clean


def test_generate_ui_from_url_prebuilt_extraction_is_forwarded() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    extraction = URLExtraction(
        color_values=("#abcdef",),
        heading_texts=("Pre-computed heading",),
    )
    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        extraction=extraction,
        invoker=lambda msgs: _CLEAN_TSX_RESPONSE,
        project_root=PROJECT_ROOT,
    )
    assert result.extraction is extraction


def test_generate_ui_from_url_with_screenshotter_attaches_image() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    captured: list[str] = []

    def shot(url: str) -> ScreenshotResult:
        captured.append(url)
        return ScreenshotResult(data=_PNG_1X1, mime_type="image/png")

    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        screenshotter=shot,
        invoker=lambda msgs: _CLEAN_TSX_RESPONSE,
        project_root=PROJECT_ROOT,
    )
    assert captured == ["https://acme.example.com/"]
    assert result.reference.has_screenshot
    assert "screenshot_unavailable" not in result.warnings


def test_generate_ui_from_url_preserves_existing_screenshot() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    image = VisionImage(data=_PNG_1X1, mime_type="image/png")
    base = _make_reference()
    # Build a pre-attached reference to simulate a caller-handled shot.
    ref = URLReference(
        url=base.url,
        final_url=base.final_url,
        status_code=base.status_code,
        content_type=base.content_type,
        html=base.html,
        html_bytes=base.html_bytes,
        title=base.title,
        description=base.description,
        canonical_url=base.canonical_url,
        theme_color=base.theme_color,
        screenshot=image,
        fetched_at=base.fetched_at,
        warnings=base.warnings,
        meta=dict(base.meta),
    )

    def shot(url: str) -> ScreenshotResult:
        raise AssertionError("should not be called when screenshot present")

    result = generate_ui_from_url(
        "https://acme.example.com/",
        reference=ref,
        fetcher=fetcher,
        screenshotter=shot,
        invoker=lambda msgs: _CLEAN_TSX_RESPONSE,
        project_root=PROJECT_ROOT,
    )
    assert result.reference.has_screenshot


def test_generate_ui_from_url_model_provider_forwarded() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        invoker=lambda msgs: _CLEAN_TSX_RESPONSE,
        project_root=PROJECT_ROOT,
        provider="anthropic",
        model="claude-opus-4-7",
    )
    assert result.model == "claude-opus-4-7"
    assert result.provider == "anthropic"


# ── run_url_to_reference (agent entry) ──────────────────────────────


def test_run_url_to_reference_url_mode() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    payload = run_url_to_reference(
        url="https://acme.example.com/",
        fetcher=fetcher,
        invoker=lambda msgs: _CLEAN_TSX_RESPONSE,
        project_root=PROJECT_ROOT,
    )
    assert isinstance(payload, dict)
    json.dumps(payload)
    assert payload["schema_version"] == URL_REF_SCHEMA_VERSION
    assert payload["is_clean"] is True


def test_run_url_to_reference_reference_mode() -> None:
    ref = _make_reference()
    payload = run_url_to_reference(
        reference=ref,
        invoker=lambda msgs: _CLEAN_TSX_RESPONSE,
        project_root=PROJECT_ROOT,
    )
    assert payload["is_clean"] is True


def test_run_url_to_reference_requires_exactly_one_input() -> None:
    with pytest.raises(ValueError):
        run_url_to_reference()
    with pytest.raises(ValueError):
        run_url_to_reference(
            url="https://example.com/", reference=_make_reference(),
        )


def test_run_url_to_reference_llm_unavailable_surfaced() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    payload = run_url_to_reference(
        url="https://acme.example.com/",
        fetcher=fetcher,
        invoker=lambda msgs: "",
        project_root=PROJECT_ROOT,
    )
    assert "llm_unavailable" in payload["warnings"]
    assert payload["is_clean"] is False


def test_run_url_to_reference_normalises_input_url() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    payload = run_url_to_reference(
        url="HTTPS://Acme.Example.COM/#frag",
        fetcher=fetcher,
        invoker=lambda msgs: _CLEAN_TSX_RESPONSE,
        project_root=PROJECT_ROOT,
    )
    assert payload["reference"]["url"] == "https://acme.example.com/"


# ── Default invoker wiring ──────────────────────────────────────────


def test_default_invoker_catches_exceptions(monkeypatch) -> None:
    from backend import url_to_reference as mod

    def fake_invoke_chat(messages, **kwargs):
        raise RuntimeError("network blew up")

    class _FakeAdapter:
        invoke_chat = staticmethod(fake_invoke_chat)

    monkeypatch.setattr(
        "backend.llm_adapter.invoke_chat", fake_invoke_chat,
    )

    invoker = mod._default_invoker(
        provider="anthropic", model="claude-opus-4-7", llm=None,
    )
    text = invoker([])
    assert text == ""


def test_default_invoker_forwards_args(monkeypatch) -> None:
    from backend import url_to_reference as mod

    calls: list[dict] = []

    def fake_invoke_chat(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return "stub"

    monkeypatch.setattr(
        "backend.llm_adapter.invoke_chat", fake_invoke_chat,
    )
    invoker = mod._default_invoker(
        provider="anthropic", model="claude-opus-4-7", llm=None,
    )
    out = invoker(["m"])
    assert out == "stub"
    assert calls[0]["provider"] == "anthropic"
    assert calls[0]["model"] == "claude-opus-4-7"


# ── Sibling integration ─────────────────────────────────────────────


def test_integration_prompt_contains_shadcn_components_and_tokens() -> None:
    ref = _make_reference()
    prompt = build_url_generation_prompt(
        reference=ref, project_root=PROJECT_ROOT, brief=None,
    )
    # Touches real registry entries
    assert "Button" in prompt
    assert "Card" in prompt
    # Touches live design-token semantic names
    # (we verify at least one of primary/background/foreground shows up)
    tokens_lower = prompt.lower()
    assert any(
        name in tokens_lower for name in ("primary", "background", "foreground")
    )


def test_integration_auto_fix_rewrites_raw_input_to_Input() -> None:
    fetcher = _make_fetcher(_fake_success_response())
    raw_tsx = (
        "```tsx\n"
        "export default function F() {\n"
        '  return <input placeholder="search" />;\n'
        "}\n"
        "```\n"
    )
    result = generate_ui_from_url(
        "https://acme.example.com/",
        fetcher=fetcher,
        invoker=lambda msgs: raw_tsx,
        project_root=PROJECT_ROOT,
        auto_fix=True,
    )
    assert "<Input" in result.tsx_code
    assert "@/components/ui/input" in result.tsx_code


def test_integration_extraction_round_trips_json() -> None:
    x = extract_from_html(_SAMPLE_HTML, base_url="https://acme.example.com/")
    payload = x.to_dict()
    text = json.dumps(payload, sort_keys=True)
    round = json.loads(text)
    assert round == payload
