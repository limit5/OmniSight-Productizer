"""W13.5 #XXX — 5-URL × 4-breakpoint screenshot reference matrix.

End-to-end drift guard pinning the full W13 pipeline. The earlier
W13.x rows verified each module in isolation:

* W13.1 (`backend/tests/test_screenshot_capture.py`, 55 tests) —
  multi-context engine.
* W13.2 (`backend/tests/test_screenshot_breakpoints.py`, 41 tests) —
  4 production breakpoints + resolver.
* W13.3 (`backend/tests/test_screenshot_writer.py`, 76 tests) —
  ``.omnisight/refs/{name}.png`` + ``manifest.json`` writer.
* W13.4 (`backend/tests/test_screenshot_ghost_overlay.py`, 50 tests) —
  reference-vs-live diff comparator.

W13.5 pins the **end-to-end behaviour** so any regression in viewport
selection, capture order, manifest schema, atomic-write, or diff
classification surfaces here as a single 1-line snapshot diff.

Pipeline under test (per URL)
-----------------------------

::

    resolve_breakpoints()                  ── W13.2
            │  4 viewports (small→large)
            ▼
    capture_multi(url, viewports=...)      ── W13.1
            │  4 ViewportScreenshot in order
            ▼
    write_screenshots(...)                  ── W13.3
            │  4 PNGs + manifest.json on disk
            ▼
    compute_ghost_overlay_diff_from_disk    ── W13.4
            │  GhostOverlayDiff with 4 entries
            ▼
    assertions on counts / statuses / determinism

Why 5 URLs × 4 breakpoints
--------------------------

Five representative reserved-TLD URLs (RFC 2606 ``.example``) cover the
typical archetypes a productisation flow targets:

* ``landing`` — a marketing landing page (single-column long scroll).
* ``marketing`` — a marketing campaign landing
  (hero + CTA + below-the-fold sections).
* ``dashboard`` — an authenticated app shell.
* ``blog`` — long-form editorial content.
* ``shop`` — an e-commerce product detail page.

Four breakpoints come from ``DEFAULT_BREAKPOINTS`` (W13.2) verbatim —
the production policy is pinned, not paraphrased here.

5 × 4 = 20 screenshots — enough variety to catch a regression that only
shows on one URL or at one width, small enough to run in <1 s without a
real browser.

Network discipline
------------------

Zero real HTTP. Every URL uses the RFC 2606 reserved ``.example`` TLD
(no real DNS resolution); every capture goes through a duck-typed
``_MatrixFakePlaywright`` injected via the W13.1 engine's
``playwright_factory`` seam — the ``playwright`` package isn't even
required to be installed for this file to pass. This matches the W13.1
test discipline.

Determinism
-----------

The fake page returns a deterministic PNG payload for each
``(url, viewport_name)`` pair (``sha256(url|name).digest() * 8`` plus a
PNG magic prefix), so every captured PNG has a stable ``sha256:``
digest the matrix can pin as an exact-equality snapshot. The W13.4
diff against the persisted reference therefore produces a stable
``identical`` outcome whenever the pipeline is byte-stable, and a
stable ``pixel_drift`` outcome whenever a downstream test deliberately
mutates the bytes.

Module-global state audit (SOP §1)
----------------------------------

Read-only fixture constants only — :data:`REFERENCE_URLS` 5-tuple +
``_VP_BY_DIMS`` immutable mapping derived from
:data:`DEFAULT_BREAKPOINTS`. Zero mutable module state; cross-worker
irrelevant (tests run single-process under pytest).

Read-after-write timing audit (SOP §2)
--------------------------------------

N/A — pure-function tests over W13.x surface; every E2E test uses
pytest's ``tmp_path`` fixture for filesystem isolation, guaranteeing
no two tests race over the same ``.omnisight/refs/``.

Compat-fingerprint grep (SOP §3)
--------------------------------

N/A — no SQL, no DB. ``grep -nE
"_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]"
backend/tests/test_screenshot_matrix.py`` returns 0 hits.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

import pytest

from backend.web.screenshot_breakpoints import (
    DEFAULT_BREAKPOINTS,
    DEFAULT_BREAKPOINT_NAMES,
    resolve_breakpoints,
)
from backend.web.screenshot_capture import (
    MultiContextScreenshotCapture,
    Viewport,
    ViewportScreenshot,
)
from backend.web.screenshot_ghost_overlay import (
    GHOST_OVERLAY_STATUS_DIMENSION_DRIFT,
    GHOST_OVERLAY_STATUS_IDENTICAL,
    GHOST_OVERLAY_STATUS_MISSING_IN_LIVE,
    GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE,
    GHOST_OVERLAY_STATUS_PIXEL_DRIFT,
    GhostOverlayDiff,
    compute_ghost_overlay_diff,
    compute_ghost_overlay_diff_from_disk,
)
from backend.web.screenshot_writer import (
    SCREENSHOT_MANIFEST_FILENAME,
    SCREENSHOT_MANIFEST_RELATIVE_PATH,
    SCREENSHOT_MANIFEST_VERSION,
    SCREENSHOT_PNG_SUFFIX,
    SCREENSHOT_REFS_DIR,
    SHA256_HASH_PREFIX,
    ScreenshotManifest,
    read_screenshot_manifest,
    read_screenshot_manifest_if_exists,
    resolve_refs_dir,
    resolve_screenshot_manifest_path,
    resolve_screenshot_path,
    write_screenshots,
)


# ── Reference matrix fixtures ─────────────────────────────────────────

#: The five reference URLs the matrix exercises end-to-end. Every URL
#: uses the RFC 2606 reserved ``.example`` TLD so even if a future test
#: change accidentally drops the fake-fetch injection, no real DNS
#: resolves and no real HTTP egress can happen.
REFERENCE_URLS: tuple[str, ...] = (
    "https://landing.example/",
    "https://marketing.example/launch",
    "https://dashboard.example/app",
    "https://blog.example/2026-04-29-launch",
    "https://shop.example/products/featured",
)

#: Total expected screenshots for the matrix: 5 URLs × 4 breakpoints.
TOTAL_SCREENSHOTS: int = 20

# Reverse lookup table: viewport (width, height) → name. Built from
# DEFAULT_BREAKPOINTS so a future re-naming or width change in the W13.2
# defaults flows through without manual sync. Each (w, h) pair is unique
# in the production defaults so a dict lookup is unambiguous.
_VP_BY_DIMS: dict[tuple[int, int], str] = {
    (vp.width, vp.height): vp.name for vp in DEFAULT_BREAKPOINTS
}


def _deterministic_png(url: str, viewport_name: str) -> bytes:
    """Return a stable byte payload for ``(url, viewport_name)``.

    PNG magic prefix + 8 × ``sha256(url|name).digest()`` so:

    * The bytes start with the PNG signature byte sequence (downstream
      sniffers won't trip on ``b'\\xfftest'``-style fake payloads).
    * Each ``(url, viewport)`` pair gets a different blob the W13.3
      writer pins as a unique ``sha256:`` digest.
    * Repeated calls with the same arguments return the same bytes,
      so re-runs produce byte-identical PNGs and the W13.4 diff
      collapses to ``identical``.
    """
    body = hashlib.sha256(f"{url}|{viewport_name}".encode("utf-8")).digest()
    return b"\x89PNG\r\n\x1a\n" + body * 8


def _expected_sha256(url: str, viewport_name: str) -> str:
    """The ``sha256:<hex>`` digest the W13.3 writer will record for this
    cell. Pinned in the writer / diff tests so a regression in either
    side surfaces here too."""
    return f"{SHA256_HASH_PREFIX}{hashlib.sha256(_deterministic_png(url, viewport_name)).hexdigest()}"


# ── Duck-typed playwright fake (matrix-aware) ─────────────────────────

class _FakeResponse:
    def __init__(self, *, status: int = 200, url: str = "https://example.com",
                 headers: Optional[dict[str, str]] = None):
        self.status = status
        self.url = url
        self._headers = dict(headers or {})

    async def all_headers(self) -> dict[str, str]:
        return dict(self._headers)


class _MatrixFakePage:
    """Per-context page that returns deterministic PNGs keyed by the
    URL passed to ``goto`` and the viewport dimensions of its parent
    context."""

    def __init__(self, *, viewport_kwargs: dict[str, Any],
                 png_override: Optional[bytes] = None):
        self._viewport_kwargs = viewport_kwargs
        self._png_override = png_override
        self._url: str = ""
        self.closed = False
        self.goto_calls: list[dict[str, Any]] = []
        self.screenshot_calls: list[dict[str, Any]] = []

    async def goto(self, url: str, *, timeout: int, wait_until: str):
        self.goto_calls.append({"url": url, "timeout": timeout,
                                "wait_until": wait_until})
        self._url = url
        return _FakeResponse(status=200, url=url,
                             headers={"content-type": "text/html"})

    @property
    def url(self) -> str:  # noqa: A003 — playwright's API uses .url
        return self._url

    async def screenshot(self, *, full_page: bool, type: str) -> bytes:  # noqa: A002
        self.screenshot_calls.append({"full_page": full_page, "type": type})
        if self._png_override is not None:
            return self._png_override
        viewport = self._viewport_kwargs["viewport"]
        dims = (viewport["width"], viewport["height"])
        # Default breakpoints use unique (w,h); fall back to any custom
        # caller's viewport name we cached.
        name = _VP_BY_DIMS.get(dims, self._viewport_kwargs.get("__custom_name__", "unknown"))
        return _deterministic_png(self._url, name)

    async def close(self) -> None:
        self.closed = True


class _MatrixFakeContext:
    def __init__(self, kwargs: dict[str, Any], *,
                 png_override: Optional[bytes] = None):
        self.new_context_kwargs = dict(kwargs)
        self._page = _MatrixFakePage(
            viewport_kwargs=self.new_context_kwargs,
            png_override=png_override,
        )
        self.closed = False

    async def new_page(self) -> _MatrixFakePage:
        return self._page

    async def close(self) -> None:
        self.closed = True


class _MatrixFakeBrowser:
    def __init__(self, *, png_override: Optional[bytes] = None,
                 custom_name_for_dims: Optional[dict[tuple[int, int], str]] = None):
        self._png_override = png_override
        self._custom_name_for_dims = custom_name_for_dims or {}
        self.contexts: list[_MatrixFakeContext] = []
        self.closed = False
        self.launch_kwargs: dict[str, Any] = {}

    async def new_context(self, **kwargs):
        # Inject the operator's custom-viewport name (if any) so the
        # page's screenshot dispatch can resolve it from dimensions.
        viewport = kwargs.get("viewport") or {}
        dims = (viewport.get("width"), viewport.get("height"))
        if dims in self._custom_name_for_dims:
            kwargs = dict(kwargs)
            kwargs["__custom_name__"] = self._custom_name_for_dims[dims]
        ctx = _MatrixFakeContext(kwargs, png_override=self._png_override)
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


class _MatrixFakeBrowserType:
    def __init__(self, browser: _MatrixFakeBrowser):
        self._browser = browser
        self.launch_calls: list[dict[str, Any]] = []

    async def launch(self, **kwargs):
        self.launch_calls.append(dict(kwargs))
        self._browser.launch_kwargs = dict(kwargs)
        return self._browser


class _MatrixFakePlaywright:
    def __init__(self, browser_type: _MatrixFakeBrowserType):
        self.chromium = browser_type
        self.firefox = browser_type  # tolerant — same fake regardless
        self.webkit = browser_type


class _MatrixFakePwCtx:
    def __init__(self, pw: _MatrixFakePlaywright):
        self._pw = pw
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self._pw

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return None


def _build_matrix_factory(
    *,
    png_override: Optional[bytes] = None,
    custom_name_for_dims: Optional[dict[tuple[int, int], str]] = None,
):
    browser = _MatrixFakeBrowser(
        png_override=png_override,
        custom_name_for_dims=custom_name_for_dims,
    )
    browser_type = _MatrixFakeBrowserType(browser)
    pw = _MatrixFakePlaywright(browser_type)
    ctx = _MatrixFakePwCtx(pw)
    handle = {
        "browser": browser,
        "browser_type": browser_type,
        "pw_ctx": ctx,
    }
    return (lambda: ctx), handle


# ── Capture helpers ───────────────────────────────────────────────────

async def _capture_url(
    url: str,
    *,
    viewports: Optional[Iterable[Viewport]] = None,
    png_override: Optional[bytes] = None,
    custom_name_for_dims: Optional[dict[tuple[int, int], str]] = None,
) -> tuple[tuple[ViewportScreenshot, ...], dict[str, Any]]:
    """Run the W13.1 capture engine end-to-end against the matrix fake
    and return ``(shots, fake_handle)``.

    Default ``viewports`` resolves to :data:`DEFAULT_BREAKPOINTS` via
    :func:`resolve_breakpoints` — exercises the W13.2 entry point with
    no arguments so a regression in the resolver-default tuple
    surfaces here too.
    """
    factory, handle = _build_matrix_factory(
        png_override=png_override,
        custom_name_for_dims=custom_name_for_dims,
    )
    cap = MultiContextScreenshotCapture(playwright_factory=factory)
    try:
        resolved = (
            tuple(viewports) if viewports is not None else resolve_breakpoints()
        )
        shots = await cap.capture_multi(url, viewports=resolved, timeout_s=10.0)
    finally:
        await cap.aclose()
    return shots, handle


# ── TestMatrixShape — 5 URLs × 4 breakpoints contract ─────────────────

class TestMatrixShape:
    def test_reference_url_count_is_five(self):
        assert len(REFERENCE_URLS) == 5

    def test_reference_urls_are_unique(self):
        assert len(set(REFERENCE_URLS)) == 5

    def test_reference_urls_use_https(self):
        for url in REFERENCE_URLS:
            assert url.startswith("https://"), url

    def test_reference_urls_use_reserved_tld(self):
        # RFC 2606 reserves ``.example`` for documentation / examples
        # and guarantees it never resolves to a real host.
        for url in REFERENCE_URLS:
            assert ".example/" in url or url.endswith(".example"), url

    def test_default_breakpoint_count_is_four(self):
        assert len(DEFAULT_BREAKPOINTS) == 4
        assert len(DEFAULT_BREAKPOINT_NAMES) == 4

    def test_total_screenshots_is_twenty(self):
        assert TOTAL_SCREENSHOTS == len(REFERENCE_URLS) * len(DEFAULT_BREAKPOINTS)
        assert TOTAL_SCREENSHOTS == 20

    def test_default_breakpoint_names_pinned(self):
        assert DEFAULT_BREAKPOINT_NAMES == (
            "mobile_375", "tablet_768", "desktop_1440", "desktop_1920",
        )

    def test_default_breakpoint_dims_unique(self):
        # Drift guard for the matrix's reverse-lookup fake — a future
        # row that adds a default with a duplicate (w,h) would silently
        # break dispatch in ``_MatrixFakePage.screenshot``.
        dims = [(vp.width, vp.height) for vp in DEFAULT_BREAKPOINTS]
        assert len(set(dims)) == len(dims), dims


# ── TestPerUrlCapture — W13.1 + W13.2 happy path ──────────────────────

class TestPerUrlCapture:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_each_url_produces_four_screenshots(self, url):
        shots, _ = await _capture_url(url)
        assert isinstance(shots, tuple)
        assert len(shots) == 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_each_url_iterates_default_breakpoint_order(self, url):
        shots, _ = await _capture_url(url)
        assert [s.viewport.name for s in shots] == list(DEFAULT_BREAKPOINT_NAMES)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_each_url_creates_one_context_per_viewport(self, url):
        # The W13.1 multi-context invariant lifted to the matrix:
        # 4 contexts per URL, each with its viewport's exact dimensions.
        shots, handle = await _capture_url(url)
        browser = handle["browser"]
        assert len(browser.contexts) == 4
        for ctx, vp in zip(browser.contexts, DEFAULT_BREAKPOINTS):
            kw = ctx.new_context_kwargs
            assert kw["viewport"] == {"width": vp.width, "height": vp.height}
            assert kw["device_scale_factor"] == float(vp.device_scale_factor)
            assert kw["is_mobile"] is bool(vp.is_mobile)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_each_url_amortises_browser_to_one_launch(self, url):
        shots, handle = await _capture_url(url)
        # 1 capture_multi call → 1 launch (4 contexts inside).
        assert len(handle["browser_type"].launch_calls) == 1
        assert handle["browser"].closed is True
        assert handle["pw_ctx"].exited is True

    @pytest.mark.asyncio
    async def test_total_matrix_yields_twenty_screenshots(self):
        # Drives the headline contract: 5 × 4 == 20.
        total = 0
        for url in REFERENCE_URLS:
            shots, _ = await _capture_url(url)
            total += len(shots)
        assert total == TOTAL_SCREENSHOTS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_each_capture_carries_status_and_post_redirect(self, url):
        shots, _ = await _capture_url(url)
        for shot in shots:
            assert shot.status_code == 200
            assert shot.post_redirect_url == url
            assert shot.fetched_at.endswith("Z")
            assert shot.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


# ── TestPerCellSnapshot — pin per-(url, viewport) sha256 ──────────────

class TestPerCellSnapshot:
    """Pin each of the 20 cells' ``sha256:`` digest. A regression in
    the fake's payload generator, in W13.3's manifest digest, or in
    W13.4's live-side digest computation surfaces here as a 1-line
    snapshot diff per cell."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_per_cell_sha256_matches_expected(self, url):
        shots, _ = await _capture_url(url)
        for shot in shots:
            actual = (
                f"{SHA256_HASH_PREFIX}"
                f"{hashlib.sha256(shot.png_bytes).hexdigest()}"
            )
            expected = _expected_sha256(url, shot.viewport.name)
            assert actual == expected, (
                f"sha256 mismatch for ({url}, {shot.viewport.name})"
            )

    @pytest.mark.asyncio
    async def test_cross_url_same_viewport_yields_distinct_digests(self):
        # Same breakpoint at two different URLs must produce different
        # ``sha256:`` — otherwise downstream "did this URL change"
        # checks would silently collapse.
        digests: set[str] = set()
        for url in REFERENCE_URLS:
            digests.add(_expected_sha256(url, "mobile_375"))
        assert len(digests) == len(REFERENCE_URLS)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_same_url_across_viewports_yields_distinct_digests(self, url):
        digests = {
            _expected_sha256(url, name) for name in DEFAULT_BREAKPOINT_NAMES
        }
        assert len(digests) == len(DEFAULT_BREAKPOINT_NAMES)


# ── TestWriterRoundTrip — W13.3 disk persistence per URL ──────────────

class TestWriterRoundTrip:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_per_url_write_creates_four_pngs_and_manifest(
        self, url, tmp_path
    ):
        shots, _ = await _capture_url(url)
        manifest = write_screenshots(
            shots,
            project_root=tmp_path,
            source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )

        refs_dir = resolve_refs_dir(tmp_path)
        # 4 PNGs + 1 manifest.json on disk.
        actual_files = sorted(p.name for p in refs_dir.iterdir())
        expected_pngs = sorted(
            f"{name}{SCREENSHOT_PNG_SUFFIX}" for name in DEFAULT_BREAKPOINT_NAMES
        )
        assert SCREENSHOT_MANIFEST_FILENAME in actual_files
        assert all(name in actual_files for name in expected_pngs)

        # Manifest schema sanity.
        assert isinstance(manifest, ScreenshotManifest)
        assert manifest.manifest_version == SCREENSHOT_MANIFEST_VERSION
        assert manifest.source_url == url
        assert manifest.refs_dir == SCREENSHOT_REFS_DIR
        assert len(manifest.screenshots) == 4
        # Order preserved.
        assert tuple(e.name for e in manifest.screenshots) == DEFAULT_BREAKPOINT_NAMES

        # Per-entry sha256 matches the per-cell pin.
        for entry in manifest.screenshots:
            assert entry.sha256 == _expected_sha256(url, entry.name)
            png_path = resolve_screenshot_path(tmp_path, entry.name)
            assert png_path.exists()
            assert png_path.stat().st_size == entry.byte_size

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_per_url_strict_read_round_trip(self, url, tmp_path):
        shots, _ = await _capture_url(url)
        written = write_screenshots(
            shots,
            project_root=tmp_path,
            source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )
        loaded = read_screenshot_manifest(tmp_path)
        # Equal manifests round-trip byte-for-byte.
        assert loaded == written

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_per_url_manifest_relative_path_well_formed(
        self, url, tmp_path
    ):
        shots, _ = await _capture_url(url)
        write_screenshots(
            shots, project_root=tmp_path, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )
        manifest_path = resolve_screenshot_manifest_path(tmp_path)
        # Pinned literal — drift here means W13.3's relative-path
        # constant migrated and the W14 frontend's static-file route
        # would 404.
        assert str(manifest_path).endswith(SCREENSHOT_MANIFEST_RELATIVE_PATH)
        for entry in read_screenshot_manifest(tmp_path).screenshots:
            assert entry.relative_path == (
                f"{SCREENSHOT_REFS_DIR}/{entry.name}{SCREENSHOT_PNG_SUFFIX}"
            )

    @pytest.mark.asyncio
    async def test_total_writes_yield_twenty_pngs_across_five_projects(
        self, tmp_path
    ):
        # 5 distinct project roots (one per URL) → 20 PNGs total + 5
        # manifests. Drift guard for the matrix's "5 × 4 = 20" claim
        # under the writer surface.
        total_pngs = 0
        total_manifests = 0
        for idx, url in enumerate(REFERENCE_URLS):
            project = tmp_path / f"project_{idx}"
            project.mkdir()
            shots, _ = await _capture_url(url)
            write_screenshots(
                shots, project_root=project, source_url=url,
                now="2026-04-29T12:00:00.000000Z",
            )
            refs_dir = resolve_refs_dir(project)
            for child in refs_dir.iterdir():
                if child.suffix == SCREENSHOT_PNG_SUFFIX:
                    total_pngs += 1
                elif child.name == SCREENSHOT_MANIFEST_FILENAME:
                    total_manifests += 1
        assert total_pngs == TOTAL_SCREENSHOTS
        assert total_manifests == len(REFERENCE_URLS)


# ── TestProjectIsolation — no cross-URL bleed on disk ─────────────────

class TestProjectIsolation:
    @pytest.mark.asyncio
    async def test_two_urls_in_distinct_project_roots_dont_clobber(
        self, tmp_path
    ):
        url_a = REFERENCE_URLS[0]
        url_b = REFERENCE_URLS[1]
        project_a = tmp_path / "a"
        project_a.mkdir()
        project_b = tmp_path / "b"
        project_b.mkdir()

        shots_a, _ = await _capture_url(url_a)
        shots_b, _ = await _capture_url(url_b)

        write_screenshots(
            shots_a, project_root=project_a, source_url=url_a,
            now="2026-04-29T12:00:00.000000Z",
        )
        write_screenshots(
            shots_b, project_root=project_b, source_url=url_b,
            now="2026-04-29T12:00:00.000000Z",
        )

        m_a = read_screenshot_manifest(project_a)
        m_b = read_screenshot_manifest(project_b)
        assert m_a.source_url == url_a
        assert m_b.source_url == url_b
        # Same breakpoint name across the two projects → distinct sha256
        # because URL differs.
        for vp_name in DEFAULT_BREAKPOINT_NAMES:
            sha_a = next(e.sha256 for e in m_a.screenshots if e.name == vp_name)
            sha_b = next(e.sha256 for e in m_b.screenshots if e.name == vp_name)
            assert sha_a != sha_b


# ── TestGhostOverlayIdentical — pipeline byte-stable round-trip ───────

class TestGhostOverlayIdentical:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_re_capture_against_reference_is_identical(
        self, url, tmp_path
    ):
        # First run: persist reference set on disk.
        shots1, _ = await _capture_url(url)
        write_screenshots(
            shots1, project_root=tmp_path, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )

        # Second run: re-capture; the deterministic fake produces the
        # same bytes, so every viewport must classify as ``identical``.
        shots2, _ = await _capture_url(url)
        diff = compute_ghost_overlay_diff_from_disk(
            tmp_path,
            list(shots2),
            live_source_url=url,
            now="2026-04-29T12:30:00.000000Z",
        )

        assert isinstance(diff, GhostOverlayDiff)
        assert len(diff.entries) == 4
        assert all(
            e.status == GHOST_OVERLAY_STATUS_IDENTICAL for e in diff.entries
        ), [e.status for e in diff.entries]
        assert diff.has_drift is False
        assert diff.counts_by_status == {GHOST_OVERLAY_STATUS_IDENTICAL: 4}
        # Order preserved: reference order = small-to-large width.
        assert tuple(e.name for e in diff.entries) == DEFAULT_BREAKPOINT_NAMES

    @pytest.mark.asyncio
    async def test_aggregate_identical_run_zero_drift_across_all_urls(
        self, tmp_path
    ):
        # One project per URL; every cell must be identical → total
        # drift count == 0 across the whole 20-cell matrix.
        identical_count = 0
        drift_count = 0
        for idx, url in enumerate(REFERENCE_URLS):
            project = tmp_path / f"p{idx}"
            project.mkdir()
            shots1, _ = await _capture_url(url)
            write_screenshots(
                shots1, project_root=project, source_url=url,
                now="2026-04-29T12:00:00.000000Z",
            )
            shots2, _ = await _capture_url(url)
            diff = compute_ghost_overlay_diff_from_disk(
                project, list(shots2), live_source_url=url,
                now="2026-04-29T12:30:00.000000Z",
            )
            for entry in diff.entries:
                if entry.status == GHOST_OVERLAY_STATUS_IDENTICAL:
                    identical_count += 1
                else:
                    drift_count += 1
        assert identical_count == TOTAL_SCREENSHOTS
        assert drift_count == 0


# ── TestGhostOverlayPixelDrift — mutated bytes, same dimensions ───────

class TestGhostOverlayPixelDrift:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_re_capture_with_mutated_bytes_classifies_pixel_drift(
        self, url, tmp_path,
    ):
        shots1, _ = await _capture_url(url)
        write_screenshots(
            shots1, project_root=tmp_path, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )

        # Force every page to return the same mutated PNG so dimensions
        # remain unchanged but bytes diverge → pixel_drift.
        mutated_png = b"\x89PNG\r\n\x1a\n" + b"X" * 64
        shots2, _ = await _capture_url(url, png_override=mutated_png)
        diff = compute_ghost_overlay_diff_from_disk(
            tmp_path, list(shots2), live_source_url=url,
            now="2026-04-29T12:30:00.000000Z",
        )

        assert all(
            e.status == GHOST_OVERLAY_STATUS_PIXEL_DRIFT for e in diff.entries
        ), [e.status for e in diff.entries]
        assert diff.has_drift is True
        assert diff.counts_by_status == {GHOST_OVERLAY_STATUS_PIXEL_DRIFT: 4}
        for entry in diff.entries:
            # Width / height match the reference (no dimension drift).
            assert entry.width_delta == 0
            assert entry.height_delta == 0
            # byte_size_delta != 0 because we forced different bytes
            # (mutated_png length ≠ deterministic_png length).
            assert entry.byte_size_delta is not None


# ── TestGhostOverlayDimensionDrift — same name, different size ───────

class TestGhostOverlayDimensionDrift:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_recapture_with_different_viewport_dims_is_dimension_drift(
        self, url, tmp_path,
    ):
        # Reference set: standard mobile_375 (375 × 812).
        shots1, _ = await _capture_url(url)
        write_screenshots(
            shots1, project_root=tmp_path, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )

        # Live set: same name "mobile_375" but at a different size.
        # We must call resolve_breakpoints with include_defaults=False
        # because reusing a default name with custom_viewports +
        # include_defaults=True is correctly rejected by the W13.2
        # resolver — the operator's path is to flip include_defaults
        # off and supply a fully-bespoke list.
        bespoke = [Viewport(name="mobile_375", width=414, height=896)]
        shots2, _ = await _capture_url(
            url,
            viewports=resolve_breakpoints(
                bespoke, include_defaults=False,
            ),
            custom_name_for_dims={(414, 896): "mobile_375"},
        )

        ref_manifest = read_screenshot_manifest(tmp_path)
        diff = compute_ghost_overlay_diff(
            ref_manifest, list(shots2), live_source_url=url,
            now="2026-04-29T12:30:00.000000Z",
        )

        # 1 dimension_drift (mobile_375 dims differ) + 3 missing_in_live
        # (tablet/desktop_1440/desktop_1920 captured by ref, not by live).
        statuses = {e.name: e.status for e in diff.entries}
        assert statuses["mobile_375"] == GHOST_OVERLAY_STATUS_DIMENSION_DRIFT
        for vp_name in ("tablet_768", "desktop_1440", "desktop_1920"):
            assert statuses[vp_name] == GHOST_OVERLAY_STATUS_MISSING_IN_LIVE
        assert diff.has_drift is True
        # Width / height delta carry the actual gap.
        m_entry = next(e for e in diff.entries if e.name == "mobile_375")
        assert m_entry.width_delta == 414 - 375
        assert m_entry.height_delta == 896 - 812


# ── TestGhostOverlayMissing — partial / extra viewport coverage ──────

class TestGhostOverlayMissingInLive:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_partial_live_capture_marks_absent_breakpoints_missing(
        self, url, tmp_path,
    ):
        # Reference: full 4-breakpoint set.
        shots_full, _ = await _capture_url(url)
        write_screenshots(
            shots_full, project_root=tmp_path, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )

        # Live: only mobile + tablet captured. Missing desktop_1440 and
        # desktop_1920 must show up as ``missing_in_live`` in the diff.
        partial_live = [s for s in shots_full
                        if s.viewport.name in ("mobile_375", "tablet_768")]
        diff = compute_ghost_overlay_diff_from_disk(
            tmp_path, partial_live, live_source_url=url,
            now="2026-04-29T12:30:00.000000Z",
        )

        statuses = {e.name: e.status for e in diff.entries}
        assert statuses["mobile_375"] == GHOST_OVERLAY_STATUS_IDENTICAL
        assert statuses["tablet_768"] == GHOST_OVERLAY_STATUS_IDENTICAL
        assert statuses["desktop_1440"] == GHOST_OVERLAY_STATUS_MISSING_IN_LIVE
        assert statuses["desktop_1920"] == GHOST_OVERLAY_STATUS_MISSING_IN_LIVE
        assert diff.has_drift is True
        assert diff.counts_by_status == {
            GHOST_OVERLAY_STATUS_IDENTICAL: 2,
            GHOST_OVERLAY_STATUS_MISSING_IN_LIVE: 2,
        }


class TestGhostOverlayMissingInReference:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_extra_live_viewport_marks_unseen_breakpoint_missing_in_ref(
        self, url, tmp_path,
    ):
        # Reference: only the 4 defaults.
        shots1, _ = await _capture_url(url)
        write_screenshots(
            shots1, project_root=tmp_path, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )

        # Live: 4 defaults + 1 ultrawide custom. The custom viewport
        # has no reference counterpart → ``missing_in_reference``.
        ultrawide = Viewport(name="ultrawide_3840", width=3840, height=1600)
        viewports = resolve_breakpoints((ultrawide,))
        shots2, _ = await _capture_url(
            url,
            viewports=viewports,
            custom_name_for_dims={(3840, 1600): "ultrawide_3840"},
        )
        diff = compute_ghost_overlay_diff_from_disk(
            tmp_path, list(shots2), live_source_url=url,
            now="2026-04-29T12:30:00.000000Z",
        )

        statuses = {e.name: e.status for e in diff.entries}
        for vp_name in DEFAULT_BREAKPOINT_NAMES:
            assert statuses[vp_name] == GHOST_OVERLAY_STATUS_IDENTICAL
        assert statuses["ultrawide_3840"] == GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE
        # Iteration order: 4 reference (canonical) → 1 live-only at end.
        assert tuple(e.name for e in diff.entries) == (
            *DEFAULT_BREAKPOINT_NAMES, "ultrawide_3840",
        )
        assert diff.has_drift is True


# ── TestNoReferenceYet — first-run diff ──────────────────────────────

class TestNoReferenceYet:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_first_run_marks_every_live_capture_missing_in_ref(
        self, url, tmp_path,
    ):
        # Empty project — no manifest pinned. Every live capture is
        # "new since the first run".
        shots, _ = await _capture_url(url)
        assert read_screenshot_manifest_if_exists(tmp_path) is None
        diff = compute_ghost_overlay_diff_from_disk(
            tmp_path, list(shots), live_source_url=url,
            now="2026-04-29T12:30:00.000000Z",
        )
        assert all(
            e.status == GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE
            for e in diff.entries
        )
        assert diff.has_drift is True
        assert diff.counts_by_status == {
            GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE: 4,
        }
        assert diff.live_source_url == url


# ── TestDeterminism — re-runs produce byte-stable artefacts ──────────

class TestDeterminism:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_same_url_two_captures_yield_byte_identical_pngs(self, url):
        shots_a, _ = await _capture_url(url)
        shots_b, _ = await _capture_url(url)
        assert len(shots_a) == len(shots_b) == 4
        for a, b in zip(shots_a, shots_b):
            assert a.viewport.name == b.viewport.name
            assert a.png_bytes == b.png_bytes

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_same_url_two_writer_runs_yield_equal_manifest_payload(
        self, url, tmp_path,
    ):
        # Two project roots, identical pipeline → identical manifest
        # payload (after pinning ``created_at`` via ``now=`` and
        # stripping ``fetched_at`` which the engine reads from the
        # wall clock at capture time).
        project_a = tmp_path / "a"
        project_a.mkdir()
        project_b = tmp_path / "b"
        project_b.mkdir()
        shots1, _ = await _capture_url(url)
        shots2, _ = await _capture_url(url)
        m_a = write_screenshots(
            shots1, project_root=project_a, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )
        m_b = write_screenshots(
            shots2, project_root=project_b, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )

        def _structural(manifest):
            return (
                manifest.manifest_version,
                manifest.created_at,
                manifest.source_url,
                manifest.refs_dir,
                tuple(
                    (
                        e.name, e.width, e.height,
                        e.device_scale_factor, e.is_mobile,
                        e.filename, e.relative_path,
                        e.byte_size, e.sha256,
                        e.status_code, e.post_redirect_url,
                    )
                    for e in manifest.screenshots
                ),
            )

        assert _structural(m_a) == _structural(m_b)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", REFERENCE_URLS)
    async def test_same_url_two_diff_runs_yield_equal_diff_payload(
        self, url, tmp_path,
    ):
        # Diff is request-scoped + pure-function; pinning ``now`` makes
        # the JSON wire payload byte-stable across reruns.
        shots1, _ = await _capture_url(url)
        write_screenshots(
            shots1, project_root=tmp_path, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )
        shots2, _ = await _capture_url(url)
        shots3, _ = await _capture_url(url)
        diff_a = compute_ghost_overlay_diff_from_disk(
            tmp_path, list(shots2), live_source_url=url,
            now="2026-04-29T12:30:00.000000Z",
        )
        diff_b = compute_ghost_overlay_diff_from_disk(
            tmp_path, list(shots3), live_source_url=url,
            now="2026-04-29T12:30:00.000000Z",
        )
        # Strip the live-side fetched_at (timestamp differs across
        # captures even with the same payload) before comparing.
        def _strip(d):
            return tuple(
                (e.name, e.status, e.reference_sha256, e.live_sha256,
                 e.width_delta, e.height_delta, e.byte_size_delta)
                for e in d.entries
            )
        assert _strip(diff_a) == _strip(diff_b)
        assert diff_a.has_drift == diff_b.has_drift
        assert dict(diff_a.counts_by_status) == dict(diff_b.counts_by_status)


# ── TestNetworkDiscipline — air-gapped enforcement ───────────────────

class TestNetworkDiscipline:
    def test_no_real_url_in_reference_fixtures(self):
        # Belt-and-braces: even a typo'd URL must use the reserved TLD.
        for url in REFERENCE_URLS:
            assert ".example" in url, url
            assert "://" in url, url
            assert not url.startswith("http://"), url  # http(s)-only

    def test_module_does_not_import_urllib_or_requests(self):
        # The matrix must never reach the network. A future refactor
        # that pulls in ``urllib`` / ``requests`` here would risk
        # silently dropping the fake-fetch injection in some path.
        import backend.tests.test_screenshot_matrix as mod
        for forbidden in ("urllib", "requests", "httpx", "playwright"):
            assert not any(
                attr.startswith(forbidden)
                for attr in dir(mod)
            ), f"unexpected {forbidden}-shaped symbol leaked into module"

    @pytest.mark.asyncio
    async def test_fake_factory_never_calls_real_playwright(self):
        # If the engine ever bypassed the injected ``playwright_factory``
        # and reached for the real ``playwright`` package, this would
        # raise ScreenshotDependencyError on hosts without the package.
        # Capturing through the matrix fake must succeed even when
        # ``playwright`` is missing from the environment.
        shots, handle = await _capture_url(REFERENCE_URLS[0])
        assert handle["pw_ctx"].entered is True
        assert handle["pw_ctx"].exited is True


# ── TestEndToEndPipeline — single-URL drive of all four W13 surfaces ──

class TestEndToEndPipeline:
    @pytest.mark.asyncio
    async def test_full_pipeline_capture_write_diff_round_trip(self, tmp_path):
        # One URL, drive every W13 surface in order, assert the
        # produced artefacts line up (the headline contract of W13.5).
        url = REFERENCE_URLS[0]
        shots, handle = await _capture_url(url)
        manifest = write_screenshots(
            shots, project_root=tmp_path, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )
        diff = compute_ghost_overlay_diff(
            manifest, list(shots), live_source_url=url,
            now="2026-04-29T12:30:00.000000Z",
        )

        # Capture: 4 viewports, 1 browser launch.
        assert len(shots) == 4
        assert len(handle["browser_type"].launch_calls) == 1
        # Write: manifest matches captures.
        assert len(manifest.screenshots) == 4
        # Diff: every viewport identical.
        assert len(diff.entries) == 4
        assert all(
            e.status == GHOST_OVERLAY_STATUS_IDENTICAL for e in diff.entries
        )
        assert diff.has_drift is False
        assert diff.source_url == url
        assert diff.live_source_url == url

    @pytest.mark.asyncio
    async def test_disk_artefacts_byte_match_in_memory_manifest(
        self, tmp_path,
    ):
        # Round-trip through the on-disk manifest.json must equal the
        # in-memory ``ScreenshotManifest`` returned by the writer —
        # JSON serialisation discipline and reader symmetry are pinned
        # in W13.3, but a regression here would still surface in the
        # matrix.
        url = REFERENCE_URLS[2]
        shots, _ = await _capture_url(url)
        in_memory = write_screenshots(
            shots, project_root=tmp_path, source_url=url,
            now="2026-04-29T12:00:00.000000Z",
        )
        manifest_path = resolve_screenshot_manifest_path(tmp_path)
        on_disk_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        # The on-disk payload's ``screenshots`` count + per-entry
        # sha256 line up with the in-memory manifest.
        assert len(on_disk_payload["screenshots"]) == len(in_memory.screenshots)
        for disk_entry, mem_entry in zip(
            on_disk_payload["screenshots"], in_memory.screenshots,
        ):
            assert disk_entry["name"] == mem_entry.name
            assert disk_entry["sha256"] == mem_entry.sha256
            assert disk_entry["byte_size"] == mem_entry.byte_size
            assert disk_entry["relative_path"] == mem_entry.relative_path
