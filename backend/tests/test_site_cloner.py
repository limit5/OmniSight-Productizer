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


def test_build_spec_warns_when_w11_3_categories_unfilled():
    cap = _baseline_capture()
    spec = build_clone_spec_from_capture(cap)
    # The placeholder marker tells callers this is a W11.1 build, not
    # the eventual W11.3 fully-populated spec.
    assert any("W11.1 placeholder" in w for w in spec.warnings)


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
