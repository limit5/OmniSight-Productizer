"""W11.1 #XXX — Contract tests for ``backend.web.site_cloner``.

Pin every public surface the W11 epic's later rows will build on:

    * URL normalisation + canonicalisation invariants
    * SSRF blocklist (loopback / link-local / RFC1918 / cloud-metadata
      IP / .onion / .local / etc.)
    * ``CloneSpec`` dataclass shape + W11.3 placeholder defaults
    * ``CloneSource`` Protocol shape (``name`` + async ``capture``)
    * ``RawCapture`` → ``CloneSpec`` mapping (title + meta description +
      asset URLs only — W11.3 will fill the rest)
    * ``clone_site`` orchestrator end-to-end against a ``MockCloneSource``
    * Typed error hierarchy (every failure mode → distinct subclass)

No network I/O — every backend call goes through a mock that returns a
pre-baked ``RawCapture``. The W11.11 row will add the live snapshot-diff
suite over five reference URLs.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from backend.web import site_cloner as sc
from backend.web.site_cloner import (
    BlockedDestinationError,
    CLOUD_METADATA_IP,
    CloneCaptureTimeoutError,
    CloneSource,
    CloneSourceError,
    CloneSpec,
    CloneSpecBuildError,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_TIMEOUT_S,
    InvalidCloneURLError,
    RawCapture,
    SUPPORTED_URL_SCHEMES,
    SiteClonerError,
    build_clone_spec_from_capture,
    clone_site,
    extract_hostname,
    is_public_destination,
    normalize_url,
    validate_clone_url,
)


# ── Module invariants ─────────────────────────────────────────────────

def test_all_exports_are_present():
    for name in sc.__all__:
        assert hasattr(sc, name), f"missing __all__ symbol: {name}"


def test_supported_schemes_are_http_and_https_only():
    assert SUPPORTED_URL_SCHEMES == frozenset({"http", "https"})


def test_default_timeout_is_positive():
    assert DEFAULT_TIMEOUT_S > 0


def test_default_max_html_bytes_is_5_mib():
    assert DEFAULT_MAX_HTML_BYTES == 5 * 1024 * 1024


def test_cloud_metadata_ip_constant_pinned():
    assert CLOUD_METADATA_IP == "169.254.169.254"


# ── Error hierarchy ───────────────────────────────────────────────────

def test_error_class_hierarchy():
    assert issubclass(InvalidCloneURLError, SiteClonerError)
    assert issubclass(BlockedDestinationError, SiteClonerError)
    assert issubclass(CloneSourceError, SiteClonerError)
    assert issubclass(CloneCaptureTimeoutError, CloneSourceError)
    assert issubclass(CloneCaptureTimeoutError, SiteClonerError)
    assert issubclass(CloneSpecBuildError, SiteClonerError)


# ── normalize_url ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("https://Example.COM/", "https://example.com"),
    ("https://example.com", "https://example.com"),
    ("https://example.com/", "https://example.com"),
    ("HTTP://example.com/path", "http://example.com/path"),
    ("https://example.com:443/", "https://example.com"),
    ("http://example.com:80/", "http://example.com"),
    ("https://example.com:8443/", "https://example.com:8443"),
    ("https://example.com/path?b=2&a=1", "https://example.com/path?b=2&a=1"),
    ("https://example.com/path#frag", "https://example.com/path"),
    ("  https://example.com/  ", "https://example.com"),
])
def test_normalize_url_canonical_forms(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_url_preserves_path_case():
    # Path is case-sensitive per RFC 3986 — must NOT be lower-cased.
    assert normalize_url("https://example.com/Foo/Bar") == "https://example.com/Foo/Bar"


def test_normalize_url_preserves_query_case():
    assert normalize_url("https://example.com/p?Q=Value") == "https://example.com/p?Q=Value"


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "not a url",
    "ftp://example.com/",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "data:text/html,<h1>x</h1>",
    "gopher://example.com/",
    "//example.com/path",   # protocol-relative
    "https://",             # no host
    "https:///path",        # no host
    "https://user:pass@example.com/",  # userinfo refused
])
def test_normalize_url_rejects_bad(bad):
    with pytest.raises(InvalidCloneURLError):
        normalize_url(bad)


def test_normalize_url_rejects_non_str():
    with pytest.raises(InvalidCloneURLError):
        normalize_url(None)  # type: ignore[arg-type]
    with pytest.raises(InvalidCloneURLError):
        normalize_url(123)  # type: ignore[arg-type]


# ── extract_hostname ──────────────────────────────────────────────────

def test_extract_hostname_lowercases():
    assert extract_hostname("https://Example.COM/path") == "example.com"


def test_extract_hostname_drops_port():
    assert extract_hostname("https://example.com:8443/") == "example.com"


def test_extract_hostname_no_host():
    with pytest.raises(InvalidCloneURLError):
        extract_hostname("https:///just/a/path")


# ── is_public_destination ─────────────────────────────────────────────

@pytest.mark.parametrize("good_host", [
    "example.com",
    "www.example.com",
    "sub.domain.example.co.uk",
    "github.io",
    "8.8.8.8",            # public DNS
    "1.1.1.1",            # public DNS
    "2606:4700:4700::1111",  # public IPv6
])
def test_is_public_destination_accepts_public(good_host):
    assert is_public_destination(good_host) is True


@pytest.mark.parametrize("bad_host", [
    "localhost",
    "LOCALHOST",
    "ip6-localhost",
    "ip6-loopback",
    "myhost.local",
    "service.internal",
    "router.lan",
    "router.home",
    "router.home.arpa",
    "exitnode.onion",
    "anything.localhost",
    # IPv4 unsafe ranges
    "127.0.0.1",
    "127.5.5.5",
    "0.0.0.0",
    "10.0.0.1",
    "10.255.255.255",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "169.254.0.1",
    CLOUD_METADATA_IP,    # AWS/GCP/Azure metadata
    "224.0.0.1",          # multicast
    "255.255.255.255",    # broadcast
    # IPv6 unsafe ranges
    "::1",
    "fe80::1",
    "fc00::1",
    "ff02::1",
])
def test_is_public_destination_blocks_unsafe(bad_host):
    assert is_public_destination(bad_host) is False


def test_is_public_destination_handles_garbage():
    assert is_public_destination("") is False
    assert is_public_destination(None) is False  # type: ignore[arg-type]
    assert is_public_destination(123) is False  # type: ignore[arg-type]


# ── validate_clone_url ────────────────────────────────────────────────

def test_validate_clone_url_returns_canonical_form():
    out = validate_clone_url("HTTPS://Example.COM/")
    assert out == "https://example.com"


def test_validate_clone_url_rejects_loopback_literal():
    with pytest.raises(BlockedDestinationError):
        validate_clone_url("http://localhost:8080/admin")


def test_validate_clone_url_rejects_metadata_ip():
    with pytest.raises(BlockedDestinationError):
        validate_clone_url(f"http://{CLOUD_METADATA_IP}/latest/meta-data/")


def test_validate_clone_url_rejects_rfc1918():
    with pytest.raises(BlockedDestinationError):
        validate_clone_url("http://192.168.1.1/")


def test_validate_clone_url_rejects_onion():
    with pytest.raises(BlockedDestinationError):
        validate_clone_url("http://abcdefghijklmnop.onion/")


def test_validate_clone_url_propagates_invalid_url():
    # Syntactic failures still raise InvalidCloneURLError, not the
    # Blocked variant — distinct audit severities.
    with pytest.raises(InvalidCloneURLError):
        validate_clone_url("file:///etc/passwd")
    with pytest.raises(InvalidCloneURLError):
        validate_clone_url("https://user:pass@example.com/")


# ── RawCapture / CloneSpec dataclass shape ────────────────────────────

def _baseline_capture(**overrides: Any) -> RawCapture:
    base = dict(
        url="https://example.com",
        html="<html><head><title>Hello</title></head><body>x</body></html>",
        status_code=200,
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
        asset_urls=("https://example.com/img/a.png",),
        headers={"content-type": "text/html"},
    )
    base.update(overrides)
    return RawCapture(**base)


def test_raw_capture_is_frozen():
    cap = _baseline_capture()
    with pytest.raises(Exception):
        cap.url = "https://other.com"  # type: ignore[misc]


def test_raw_capture_default_assets_is_empty_tuple():
    cap = RawCapture(
        url="https://example.com",
        html="<html></html>",
        status_code=200,
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
    )
    assert cap.asset_urls == ()
    assert cap.headers == {}


def test_clone_spec_w11_3_categories_present_with_safe_defaults():
    spec = CloneSpec(
        source_url="https://example.com",
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
    )
    # W11.3 categories — every one must exist with a safe default so
    # downstream code (W11.6 / W11.9 / W11.10) can be written now.
    assert spec.title is None
    assert spec.meta == {}
    assert spec.hero is None
    assert spec.nav == []
    assert spec.sections == []
    assert spec.footer is None
    assert spec.images == []
    assert spec.colors == []
    assert spec.fonts == []
    assert spec.spacing == {}
    assert spec.warnings == []


def test_clone_spec_default_collections_are_independent_per_instance():
    a = CloneSpec(source_url="https://a.com", fetched_at="t", backend="mock")
    b = CloneSpec(source_url="https://b.com", fetched_at="t", backend="mock")
    a.nav.append({"label": "Home"})
    a.colors.append("#000")
    a.warnings.append("note")
    assert b.nav == []
    assert b.colors == []
    assert b.warnings == []


# ── CloneSource Protocol ──────────────────────────────────────────────

class _MockSource:
    """Minimal CloneSource implementation used across the test suite."""

    name = "mock"

    def __init__(self, capture: Optional[RawCapture] = None,
                 raises: Optional[BaseException] = None,
                 sleep_s: float = 0.0):
        self._capture = capture or _baseline_capture()
        self._raises = raises
        self._sleep_s = sleep_s
        self.calls: list[dict[str, Any]] = []

    async def capture(self, url: str, *, timeout_s: float, max_html_bytes: int) -> RawCapture:
        self.calls.append({
            "url": url,
            "timeout_s": timeout_s,
            "max_html_bytes": max_html_bytes,
        })
        if self._sleep_s:
            await asyncio.sleep(self._sleep_s)
        if self._raises is not None:
            raise self._raises
        return self._capture


def test_mock_source_satisfies_protocol():
    assert isinstance(_MockSource(), CloneSource)


def test_protocol_rejects_object_missing_capture():
    class Broken:
        name = "broken"
    assert not isinstance(Broken(), CloneSource)


# ── build_clone_spec_from_capture ─────────────────────────────────────

def test_build_spec_extracts_title():
    cap = _baseline_capture(html="<html><head><title>Hello World</title></head></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.title == "Hello World"


def test_build_spec_extracts_meta_description():
    cap = _baseline_capture(html=(
        '<html><head><title>T</title>'
        '<meta name="description" content="An OmniSight test page."/>'
        '</head></html>'
    ))
    spec = build_clone_spec_from_capture(cap)
    assert spec.meta.get("description") == "An OmniSight test page."


def test_build_spec_pins_metadata_into_spec():
    cap = _baseline_capture()
    spec = build_clone_spec_from_capture(cap, source_url="https://canonical.example.com")
    assert spec.source_url == "https://canonical.example.com"
    assert spec.fetched_at == cap.fetched_at
    assert spec.backend == cap.backend


def test_build_spec_defaults_source_url_to_capture_url():
    cap = _baseline_capture(url="https://final.example.com/after-redirect")
    spec = build_clone_spec_from_capture(cap)
    assert spec.source_url == "https://final.example.com/after-redirect"


def test_build_spec_carries_asset_urls_into_images():
    cap = _baseline_capture(asset_urls=(
        "https://example.com/a.png",
        "https://example.com/b.jpg",
    ))
    spec = build_clone_spec_from_capture(cap)
    assert spec.images == [
        {"url": "https://example.com/a.png"},
        {"url": "https://example.com/b.jpg"},
    ]


def test_build_spec_no_longer_emits_w11_1_placeholder_warning():
    # Migrated from the W11.1 placeholder check: now that W11.3 fills
    # every category, the orchestrator must NOT keep emitting the
    # legacy ``W11.1 placeholder`` marker. Empty categories are
    # surfaced as per-category warnings instead (covered below in the
    # W11.3 schema-coverage suite).
    cap = _baseline_capture()
    spec = build_clone_spec_from_capture(cap)
    assert not any("W11.1 placeholder" in w for w in spec.warnings)


def test_build_spec_warns_when_title_missing():
    cap = _baseline_capture(html="<html><body>no title</body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.title is None
    assert any("title tag not found" in w for w in spec.warnings)


def test_build_spec_rejects_non_capture_input():
    with pytest.raises(CloneSpecBuildError):
        build_clone_spec_from_capture({"html": "<html/>"})  # type: ignore[arg-type]


def test_build_spec_rejects_non_string_html():
    cap = RawCapture(
        url="https://example.com",
        html=b"<html/>",  # type: ignore[arg-type]
        status_code=200,
        fetched_at="t",
        backend="mock",
    )
    with pytest.raises(CloneSpecBuildError):
        build_clone_spec_from_capture(cap)


# ── clone_site orchestrator ───────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_clone_site_end_to_end_returns_clone_spec():
    cap = _baseline_capture(html=(
        '<html><head><title>Demo</title>'
        '<meta name="description" content="A demo page."/>'
        '</head></html>'
    ))
    src = _MockSource(capture=cap)
    spec = _run(clone_site("HTTPS://Example.COM/", source=src))
    assert isinstance(spec, CloneSpec)
    assert spec.title == "Demo"
    assert spec.meta["description"] == "A demo page."
    assert spec.backend == "mock"
    # source_url is the *canonical* URL, not the raw user input.
    assert spec.source_url == "https://example.com"


def test_clone_site_passes_canonical_url_to_backend():
    src = _MockSource()
    _run(clone_site("HTTPS://Example.COM:443/path?b=2#frag", source=src))
    assert len(src.calls) == 1
    assert src.calls[0]["url"] == "https://example.com/path?b=2"


def test_clone_site_passes_timeout_and_size_to_backend():
    src = _MockSource()
    _run(clone_site("https://example.com", source=src,
                    timeout_s=12.5, max_html_bytes=1024 * 1024))
    assert src.calls[0]["timeout_s"] == 12.5
    assert src.calls[0]["max_html_bytes"] == 1024 * 1024


def test_clone_site_rejects_blocked_destination_before_calling_backend():
    src = _MockSource()
    with pytest.raises(BlockedDestinationError):
        _run(clone_site("http://localhost/admin", source=src))
    assert src.calls == []


def test_clone_site_rejects_invalid_url_before_calling_backend():
    src = _MockSource()
    with pytest.raises(InvalidCloneURLError):
        _run(clone_site("file:///etc/passwd", source=src))
    assert src.calls == []


def test_clone_site_wraps_backend_exception_in_clone_source_error():
    src = _MockSource(raises=RuntimeError("boom"))
    with pytest.raises(CloneSourceError) as ei:
        _run(clone_site("https://example.com", source=src))
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_clone_site_lets_typed_cloner_errors_pass_through():
    # If the backend raises a SiteClonerError subclass directly, the
    # orchestrator should not re-wrap it.
    src = _MockSource(raises=CloneSpecBuildError("bad parse"))
    with pytest.raises(CloneSpecBuildError):
        _run(clone_site("https://example.com", source=src))


def test_clone_site_translates_asyncio_timeout():
    # Backend sleeps longer than the orchestrator's outer guard.
    src = _MockSource(sleep_s=0.5)
    with pytest.raises(CloneCaptureTimeoutError):
        _run(clone_site("https://example.com", source=src,
                        timeout_s=0.01))


def test_clone_site_rejects_non_protocol_source():
    with pytest.raises(SiteClonerError):
        _run(clone_site("https://example.com",
                        source="not-a-source"))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, -0.5, "30", None])
def test_clone_site_rejects_invalid_timeout(bad):
    src = _MockSource()
    with pytest.raises(SiteClonerError):
        _run(clone_site("https://example.com", source=src, timeout_s=bad))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, "1024", 1.5, None])
def test_clone_site_rejects_invalid_max_html_bytes(bad):
    src = _MockSource()
    with pytest.raises(SiteClonerError):
        _run(clone_site("https://example.com", source=src,
                        max_html_bytes=bad))  # type: ignore[arg-type]


def test_clone_site_refuses_oversize_html():
    big = "x" * (DEFAULT_MAX_HTML_BYTES + 100)
    cap = _baseline_capture(html=big)
    src = _MockSource(capture=cap)
    with pytest.raises(CloneSourceError):
        _run(clone_site("https://example.com", source=src,
                        max_html_bytes=DEFAULT_MAX_HTML_BYTES))


def test_clone_site_refuses_non_raw_capture_return():
    class BadSource:
        name = "bad"

        async def capture(self, url, *, timeout_s, max_html_bytes):
            return {"html": "<html/>"}  # wrong type

    with pytest.raises(CloneSourceError):
        _run(clone_site("https://example.com", source=BadSource()))


# ── Backwards-compatible package re-exports ───────────────────────────

def test_package_reexports_match_module():
    from backend import web as pkg
    for name in [
        "BlockedDestinationError",
        "CloneCaptureTimeoutError",
        "CloneSource",
        "CloneSourceError",
        "CloneSpec",
        "CloneSpecBuildError",
        "InvalidCloneURLError",
        "RawCapture",
        "SiteClonerError",
        "build_clone_spec_from_capture",
        "clone_site",
        "extract_hostname",
        "is_public_destination",
        "normalize_url",
        "validate_clone_url",
    ]:
        assert getattr(pkg, name) is getattr(sc, name), (
            f"package re-export {name!r} drifted from module symbol"
        )


# ══════════════════════════════════════════════════════════════════════
# W11.3 — full ``CloneSpec`` populator contract tests
# ══════════════════════════════════════════════════════════════════════
#
# These pin the behaviour of the W11.3 ``build_clone_spec_from_capture``
# expansion across every schema category: title / meta / hero / nav /
# sections[] / footer / images[] / colors[] / fonts[] / spacing. Each
# group has at least one positive ("populator extracts X from HTML
# shape Y") and one negative / edge ("absence-of-X surfaces in
# warnings", "limit caps respected", "data: URL refused", etc.).


_RICH_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <title>OmniSight — Clone Sites Safely</title>
  <meta name="description" content="The safe site cloner."/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta name="theme-color" content="#2563eb"/>
  <meta property="og:title" content="OG Title"/>
  <meta property="og:description" content="OG description text."/>
  <meta name="twitter:card" content="summary_large_image"/>
  <link rel="preload" as="font" href="/fonts/inter.woff2"/>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter"/>
  <style>
    body { font-family: 'Inter', sans-serif; color: #111827; background: #ffffff; }
    .btn { padding: 12px 24px; background-color: rgb(37, 99, 235); }
    .container { max-width: 1200px; margin: 0 auto; gap: 1.5rem; }
  </style>
</head>
<body style="padding:24px;">
  <nav>
    <a href="/about">About</a>
    <a href="/blog">Blog</a>
    <a href="/pricing">Pricing</a>
  </nav>
  <main>
    <h1>Welcome to OmniSight</h1>
    <p>The fastest way to clone any landing page safely.</p>
    <a class="btn-primary" href="/start">Get Started</a>
    <section>
      <h2>Features</h2>
      <p>Five layers of defence in depth keep clones policy-compliant.</p>
      <a href="/features">Read the docs</a>
    </section>
    <section>
      <h2>Pricing</h2>
      <p>Free to try; pay only when you scale.</p>
    </section>
    <img src="/img/hero.png" alt="Hero illustration"/>
    <img src="data:image/png;base64,iVBORw0KGgo=" alt="inline"/>
  </main>
  <footer>
    <a href="/privacy">Privacy</a>
    <a href="/terms">Terms</a>
    <span>© 2026 OmniSight</span>
  </footer>
  <script>console.log('should not leak into outline');</script>
  <noscript>fallback noise</noscript>
</body>
</html>
"""


def _rich_capture(**overrides) -> RawCapture:
    base = dict(
        url="https://omnisight.example.com",
        html=_RICH_HTML,
        status_code=200,
        fetched_at="2026-04-28T00:00:00Z",
        backend="mock",
        asset_urls=(
            "https://omnisight.example.com/css/site.css",
            "/img/hero.png",  # duplicate of the <img> src above
        ),
        headers={"content-type": "text/html"},
    )
    base.update(overrides)
    return RawCapture(**base)


# ── title ─────────────────────────────────────────────────────────────

def test_w11_3_title_extracted_from_title_tag():
    spec = build_clone_spec_from_capture(_rich_capture())
    assert spec.title == "OmniSight — Clone Sites Safely"


def test_w11_3_title_falls_back_to_first_h1_when_title_missing():
    cap = _baseline_capture(html="<html><body><h1>Fallback Heading</h1></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.title == "Fallback Heading"


def test_w11_3_title_warning_emitted_when_no_title_or_h1():
    cap = _baseline_capture(html="<html><body><p>just text</p></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.title is None
    assert any("title tag not found" in w for w in spec.warnings)


# ── meta ──────────────────────────────────────────────────────────────

def test_w11_3_meta_collects_name_and_property_attributes():
    spec = build_clone_spec_from_capture(_rich_capture())
    # name= form
    assert spec.meta["description"] == "The safe site cloner."
    assert spec.meta["viewport"] == "width=device-width, initial-scale=1"
    assert spec.meta["theme-color"] == "#2563eb"
    # property= form (Open Graph + Twitter)
    assert spec.meta["og:title"] == "OG Title"
    assert spec.meta["og:description"] == "OG description text."
    assert spec.meta["twitter:card"] == "summary_large_image"


def test_w11_3_meta_skips_empty_content():
    cap = _baseline_capture(html=(
        '<html><head>'
        '<meta name="description" content=""/>'
        '<meta name="keywords"/>'
        '<meta name="author" content="OmniSight"/>'
        '</head><body/></html>'
    ))
    spec = build_clone_spec_from_capture(cap)
    assert "description" not in spec.meta
    assert "keywords" not in spec.meta
    assert spec.meta["author"] == "OmniSight"


def test_w11_3_meta_first_wins_on_duplicate_names():
    # Real-world pages sometimes ship both a head <meta name=description>
    # and a duplicate inside <body>. We pin first-occurrence-wins so the
    # populator is deterministic regardless of how messy the page is.
    cap = _baseline_capture(html=(
        '<html><head>'
        '<meta name="description" content="first"/>'
        '<meta name="description" content="second"/>'
        '</head><body/></html>'
    ))
    spec = build_clone_spec_from_capture(cap)
    assert spec.meta["description"] == "first"


# ── hero ──────────────────────────────────────────────────────────────

def test_w11_3_hero_extracts_h1_tagline_and_cta():
    spec = build_clone_spec_from_capture(_rich_capture())
    assert spec.hero is not None
    assert spec.hero["heading"] == "Welcome to OmniSight"
    assert spec.hero["tagline"] == (
        "The fastest way to clone any landing page safely."
    )
    # CTA was the .btn-primary <a> right after the H1+paragraph.
    assert spec.hero["cta"] == {"label": "Get Started", "href": "/start"}


def test_w11_3_hero_only_takes_first_h1_outside_section():
    cap = _baseline_capture(html=(
        "<html><body>"
        "<h1>Outside</h1><p>Outside tagline.</p>"
        "<section><h1>Inside Section</h1><p>Inside text.</p></section>"
        "</body></html>"
    ))
    spec = build_clone_spec_from_capture(cap)
    assert spec.hero["heading"] == "Outside"
    assert spec.hero["tagline"] == "Outside tagline."


def test_w11_3_hero_warning_when_no_h1_present():
    cap = _baseline_capture(html="<html><body><p>just paragraphs</p></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.hero is None
    assert any("hero block not detected" in w for w in spec.warnings)


def test_w11_3_hero_no_cta_when_no_button_class():
    cap = _baseline_capture(html=(
        "<html><body><h1>Hero</h1><p>Tag.</p>"
        '<a href="/no-cta">Just a regular link</a>'
        "</body></html>"
    ))
    spec = build_clone_spec_from_capture(cap)
    assert spec.hero["heading"] == "Hero"
    assert "cta" not in spec.hero


# ── nav ───────────────────────────────────────────────────────────────

def test_w11_3_nav_extracts_anchors_inside_nav_tag():
    spec = build_clone_spec_from_capture(_rich_capture())
    assert spec.nav == [
        {"label": "About", "href": "/about"},
        {"label": "Blog", "href": "/blog"},
        {"label": "Pricing", "href": "/pricing"},
    ]


def test_w11_3_nav_excludes_anchors_outside_nav_tag():
    cap = _baseline_capture(html=(
        '<html><body>'
        '<nav><a href="/in-nav">In Nav</a></nav>'
        '<a href="/loose">Loose</a>'
        '</body></html>'
    ))
    spec = build_clone_spec_from_capture(cap)
    assert spec.nav == [{"label": "In Nav", "href": "/in-nav"}]


def test_w11_3_nav_warning_when_no_nav_present():
    cap = _baseline_capture(html="<html><body><p>no nav here</p></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.nav == []
    assert any("nav links not detected" in w for w in spec.warnings)


# ── sections[] ────────────────────────────────────────────────────────

def test_w11_3_sections_extract_heading_and_summary():
    spec = build_clone_spec_from_capture(_rich_capture())
    assert len(spec.sections) == 2
    assert spec.sections[0]["heading"] == "Features"
    assert "Five layers of defence" in spec.sections[0]["summary"]
    assert spec.sections[0]["links"] == [
        {"label": "Read the docs", "href": "/features"}
    ]
    assert spec.sections[1]["heading"] == "Pricing"
    assert "Free to try" in spec.sections[1]["summary"]


def test_w11_3_sections_summary_is_truncated_to_cap():
    long_text = "lorem " * 200  # 1.2k chars
    cap = _baseline_capture(html=(
        f"<html><body><section><h2>Long</h2><p>{long_text}</p></section></body></html>"
    ))
    spec = build_clone_spec_from_capture(cap)
    summary = spec.sections[0]["summary"]
    assert len(summary) <= 280
    assert summary.endswith("…")


def test_w11_3_sections_warning_when_no_section_present():
    cap = _baseline_capture(html="<html><body><p>flat page</p></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.sections == []
    assert any("no <section> elements found" in w for w in spec.warnings)


# ── footer ────────────────────────────────────────────────────────────

def test_w11_3_footer_extracts_links_and_text():
    spec = build_clone_spec_from_capture(_rich_capture())
    assert spec.footer is not None
    assert {l["label"]: l["href"] for l in spec.footer["links"]} == {
        "Privacy": "/privacy",
        "Terms": "/terms",
    }
    assert "© 2026 OmniSight" in spec.footer["text"]


def test_w11_3_footer_warning_when_no_footer_present():
    cap = _baseline_capture(html="<html><body><p>no footer</p></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.footer is None
    assert any("footer block not detected" in w for w in spec.warnings)


# ── images[] ──────────────────────────────────────────────────────────

def test_w11_3_images_merge_dom_and_asset_urls_with_dedupe():
    spec = build_clone_spec_from_capture(_rich_capture())
    urls = [img["url"] for img in spec.images]
    # /img/hero.png appears in both the <img> tag and asset_urls — the
    # merge must dedupe and keep the DOM version (which has alt).
    assert urls.count("/img/hero.png") == 1
    # css/site.css from asset_urls came along too.
    assert "https://omnisight.example.com/css/site.css" in urls
    hero_entry = next(i for i in spec.images if i["url"] == "/img/hero.png")
    assert hero_entry["alt"] == "Hero illustration"


def test_w11_3_images_drop_data_uri_per_w11_6_l3():
    # W11.6 L3: never copy bytes. data: URIs ARE bytes-as-URL, so the
    # populator must refuse to enrol them as image entries.
    cap = _baseline_capture(html=(
        '<html><body>'
        '<img src="data:image/png;base64,AAAA" alt="inline"/>'
        '<img src="https://cdn.example.com/ok.png" alt="ok"/>'
        '</body></html>'
    ), asset_urls=("data:image/svg+xml;utf8,<svg/>",))
    spec = build_clone_spec_from_capture(cap)
    assert all(not i["url"].startswith("data:") for i in spec.images)
    assert any(i["url"] == "https://cdn.example.com/ok.png" for i in spec.images)


def test_w11_3_images_warning_when_none_found():
    cap = _baseline_capture(html="<html><body><p>no images</p></body></html>",
                            asset_urls=())
    spec = build_clone_spec_from_capture(cap)
    assert spec.images == []
    assert any("no images detected" in w for w in spec.warnings)


# ── colors[] ──────────────────────────────────────────────────────────

def test_w11_3_colors_extracted_from_inline_style_block_and_meta():
    spec = build_clone_spec_from_capture(_rich_capture())
    # theme-color meta + style block hex + style block rgb(...).
    assert "#2563eb" in spec.colors  # from <meta name=theme-color>
    assert "#111827" in spec.colors  # 6-digit hex preserved (not truncated)
    assert "#ffffff" in spec.colors
    assert any(c.startswith("rgb(") for c in spec.colors)


def test_w11_3_colors_preserve_six_digit_hex_intact():
    # Regression guard: an earlier draft had a regex that ate 4 chars
    # of an 8-char hex first ("#111827" → "#1118"). Lock the fix.
    cap = _baseline_capture(html=(
        '<html><body style="color:#111827;background:#abcdef">x</body></html>'
    ))
    spec = build_clone_spec_from_capture(cap)
    assert "#111827" in spec.colors
    assert "#abcdef" in spec.colors
    assert "#1118" not in spec.colors


def test_w11_3_colors_capture_rgb_rgba_hsl_hsla_forms():
    cap = _baseline_capture(html=(
        '<html><body><style>'
        '.a{color:rgb(1,2,3);}'
        '.b{color:rgba(4,5,6,0.5);}'
        '.c{color:hsl(120,50%,50%);}'
        '.d{color:hsla(120,50%,50%,0.5);}'
        '</style></body></html>'
    ))
    spec = build_clone_spec_from_capture(cap)
    assert "rgb(1,2,3)" in spec.colors
    assert "rgba(4,5,6,0.5)" in spec.colors
    assert "hsl(120,50%,50%)" in spec.colors
    assert "hsla(120,50%,50%,0.5)" in spec.colors


def test_w11_3_colors_warning_when_none_detected():
    cap = _baseline_capture(html="<html><body><p>no colours here</p></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.colors == []
    assert any("no colour tokens detected" in w for w in spec.warnings)


# ── fonts[] ───────────────────────────────────────────────────────────

def test_w11_3_fonts_extract_link_preload_and_known_font_hosts():
    spec = build_clone_spec_from_capture(_rich_capture())
    assert "/fonts/inter.woff2" in spec.fonts
    assert any("fonts.googleapis.com" in f for f in spec.fonts)


def test_w11_3_fonts_extract_font_family_declarations():
    cap = _baseline_capture(html=(
        '<html><body><style>'
        "body { font-family: 'Inter', system-ui, sans-serif; }"
        "h1 { font-family: 'Playfair Display', serif; }"
        '</style></body></html>'
    ))
    spec = build_clone_spec_from_capture(cap)
    assert "Inter" in spec.fonts
    assert "Playfair Display" in spec.fonts


def test_w11_3_fonts_warning_when_none_detected():
    cap = _baseline_capture(html="<html><body><p>no fonts</p></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.fonts == []
    assert any("no font tokens detected" in w for w in spec.warnings)


# ── spacing ───────────────────────────────────────────────────────────

def test_w11_3_spacing_extracts_padding_margin_gap_and_max_width():
    spec = build_clone_spec_from_capture(_rich_capture())
    assert "padding" in spec.spacing
    assert "12px 24px" in spec.spacing["padding"] or "24px" in spec.spacing["padding"]
    assert spec.spacing.get("max_width") == "1200px"
    assert "1.5rem" in spec.spacing.get("gap", [])


def test_w11_3_spacing_warning_when_none_detected():
    cap = _baseline_capture(html="<html><body><p>no styles</p></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert spec.spacing == {}
    assert any("no spacing tokens detected" in w for w in spec.warnings)


# ── Whole-spec invariants ─────────────────────────────────────────────

def test_w11_3_full_pipeline_emits_no_warnings_on_rich_input():
    # Sanity: a fully-formed page populates every category cleanly.
    spec = build_clone_spec_from_capture(_rich_capture())
    assert spec.warnings == [], (
        f"unexpected warnings on rich page: {spec.warnings!r}"
    )


def test_w11_3_script_and_style_text_does_not_leak_into_outline():
    # W11.6 L3 + LLM-prompt-budget concern: <script> / <noscript> text
    # must not show up in title / hero / sections / footer outputs.
    spec = build_clone_spec_from_capture(_rich_capture())
    flat = " ".join([
        spec.title or "",
        (spec.hero or {}).get("heading", "") or "",
        (spec.hero or {}).get("tagline", "") or "",
        " ".join(s.get("summary") or "" for s in spec.sections),
        (spec.footer or {}).get("text", "") or "",
    ])
    assert "should not leak" not in flat
    assert "fallback noise" not in flat


def test_w11_3_caps_image_list_at_max_items():
    # Long-list defence: a page with 500 <img> tags is condensed to
    # the cap so downstream prompts stay bounded.
    many_imgs = "".join(f'<img src="/{i}.png"/>' for i in range(500))
    cap = _baseline_capture(html=f"<html><body>{many_imgs}</body></html>",
                            asset_urls=())
    spec = build_clone_spec_from_capture(cap)
    assert len(spec.images) == sc._MAX_LIST_ITEMS_PER_CATEGORY


def test_w11_3_caps_color_tokens_at_max_design_tokens():
    blocks = "".join(
        f'.c{i} {{ color: #{i:06x}; }}' for i in range(200)
    )
    cap = _baseline_capture(html=f"<html><body><style>{blocks}</style></body></html>")
    spec = build_clone_spec_from_capture(cap)
    assert len(spec.colors) == sc._MAX_DESIGN_TOKENS


def test_w11_3_clone_site_orchestrator_returns_fully_populated_spec():
    # End-to-end: clone_site → CloneSpec is W11.3-populated (not the
    # old W11.1 placeholder). Acts as a regression guard against any
    # future refactor that bypasses build_clone_spec_from_capture.
    src = _MockSource(capture=_rich_capture())
    spec = _run(clone_site("https://omnisight.example.com", source=src))
    assert isinstance(spec, CloneSpec)
    assert spec.title == "OmniSight — Clone Sites Safely"
    assert spec.hero is not None
    assert spec.nav and spec.sections and spec.footer
    assert spec.colors and spec.fonts and spec.spacing
    assert "W11.1 placeholder" not in " ".join(spec.warnings)


def test_w11_3_handles_completely_empty_html():
    # Boundary case: empty document. Every category should default to
    # empty / None, every per-category warning should fire, and the
    # builder must not crash.
    cap = _baseline_capture(html="", asset_urls=())
    spec = build_clone_spec_from_capture(cap)
    assert spec.title is None
    assert spec.meta == {}
    assert spec.hero is None
    assert spec.nav == []
    assert spec.sections == []
    assert spec.footer is None
    assert spec.images == []
    assert spec.colors == []
    assert spec.fonts == []
    assert spec.spacing == {}
    expected = {
        "title tag not found",
        "hero block not detected",
        "nav links not detected",
        "no <section> elements found",
        "footer block not detected",
        "no images detected",
        "no colour tokens detected",
        "no font tokens detected",
        "no spacing tokens detected",
    }
    actual = set(spec.warnings)
    missing = expected - actual
    assert not missing, f"missing per-category warnings: {missing!r}"


def test_w11_3_tolerates_malformed_tag_soup():
    # Real landing pages ship <p><div>nested</p></div>-style tag soup.
    # The populator must not raise and must still surface what it can.
    cap = _baseline_capture(html=(
        "<html><body>"
        "<h1>Tag<span> soup</h1>"  # </span> missing
        "<p>Tagline.<div>x</p>"     # nested + early close
        "<section><h2>S</h2><p>body"  # unclosed <section> + <p>
        "</body></html>"
    ))
    spec = build_clone_spec_from_capture(cap)
    # Best-effort: title falls back to the <h1>, hero captured.
    assert spec.title is not None
    assert spec.hero is not None
    # Did not raise — the contract.
