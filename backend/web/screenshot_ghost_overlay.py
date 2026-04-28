"""W13.4 #XXX — Ghost-overlay diff between reference + live screenshots.

Pairs the persisted reference set produced by W13.3
(:func:`backend.web.screenshot_writer.write_screenshots` →
``.omnisight/refs/manifest.json`` + ``.omnisight/refs/{name}.png``) with
a freshly-captured in-memory tuple from W13.1
(:meth:`backend.web.screenshot_capture.MultiContextScreenshotCapture.capture_multi`),
keyed by viewport name, and produces a deterministic
:class:`GhostOverlayDiff` describing the per-viewport drift status.

Why this row, why now
---------------------
W14 (Live Web Sandbox Preview) ships a ``LivePreviewPanel`` frontend that
renders the running Vite dev server inside an ``<iframe>``. The "ghost
overlay" UI metaphor: the reference PNG (captured by W13.1 → W13.3
against the source URL the operator scaffolded from) is composited at
~50 % alpha over the live preview at the same breakpoint, so a developer
can immediately see drift between "what we agreed to ship" and "what the
agent has now built". The visual overlay itself lives in the W14
frontend; the **comparison contract** — how do we decide a viewport is
identical / drifted / dimensionally stale / orphaned — is a backend
responsibility this row pins.

W14 cannot be the home of that comparison: a single ``LivePreviewPanel``
tab serves multiple workspace types (Next / Nuxt / Astro), and the
ghost-overlay UI is one of several W14 readers (W13.5 reference matrix
test will also call this comparator). Putting the diff in
``backend/web/screenshot_ghost_overlay.py`` gives every W14 / W13.5 / W16
caller one canonical answer.

W13.4 also lands **before** W14 by design — the W13 epic owns the
screenshot lifecycle (capture → write → diff); W14 is a pure consumer
of the W13 surface. This row pins the surface so the W14 row can build
the iframe + overlay against a stable contract instead of co-evolving
the diff and the renderer at the same time (the same separation
discipline that kept W11.1 → W11.7 incremental).

Drift status taxonomy
---------------------
The W14 frontend only renders five badge colours, not pixel-level diff
maps — so the backend output is a five-way enum rather than a
percentage. Five statuses exhaust the comparison space::

    +------------------------+--------------------------------------------+
    | identical              | sha256 + width + height + DSF all match.   |
    |                        | The live preview matches the reference at  |
    |                        | this breakpoint; the ghost overlay is a    |
    |                        | no-op (operator can hide it).              |
    +------------------------+--------------------------------------------+
    | pixel_drift            | Same dimensions, different sha256. The     |
    |                        | live preview rendered to the same canvas   |
    |                        | size but the pixels diverge — the typical  |
    |                        | "agent shipped a visual change" case.      |
    +------------------------+--------------------------------------------+
    | dimension_drift        | Same name but width / height / DSF differ. |
    |                        | The reference was captured at a different  |
    |                        | viewport spec than the live preview is     |
    |                        | running at — the reference is stale and    |
    |                        | should be re-captured before any pixel     |
    |                        | comparison is meaningful.                  |
    +------------------------+--------------------------------------------+
    | missing_in_live        | Reference manifest has the entry but the   |
    |                        | live capture set does not. The live        |
    |                        | preview wasn't captured at this breakpoint |
    |                        | this run (operator may have toggled the    |
    |                        | viewport off, or the capture pipeline      |
    |                        | failed for that breakpoint).               |
    +------------------------+--------------------------------------------+
    | missing_in_reference   | Live capture has the viewport but the      |
    |                        | reference manifest does not. New           |
    |                        | breakpoint added since the reference was   |
    |                        | pinned, or the operator captured a         |
    |                        | one-off custom viewport this run.          |
    +------------------------+--------------------------------------------+

The five statuses are mutually exclusive — every paired entry resolves
to exactly one badge.

Why sha256-equality not pixel-equality
--------------------------------------
PNG encoding is non-deterministic across encoder versions, but the same
encoder fed the same pixel buffer produces a byte-identical PNG, and
W13.1 + W14 are both expected to use Playwright's chromium screenshot
path. That makes ``sha256`` a sound "is this exactly the same render"
signal at the budget this row needs (one comparison per viewport, ~µs).
Future rows MAY add an opt-in pixel-percentage diff (PIL is already in
``backend/requirements.in`` via ``qrcode[pil]``) but the backend
contract this row pins is identity-only — the W14 frontend is the place
where "show me the per-pixel diff overlay" lives because that's a UI
concern, not a router-payload concern. Keep the diff narrow.

What this module deliberately does NOT do
-----------------------------------------
* **Decode PNGs / compare pixels** — see above.
* **Write its output to disk** — the diff is request-scoped (a router
  computes it on demand for the W14 panel; the panel doesn't persist
  it). If a future row wants a "diff history" timeline, that row owns
  the writer + retention policy.
* **Render the ghost overlay** — the W14 frontend layers two ``<img>``
  / ``<iframe>`` elements with CSS opacity. This module's job ends at
  "tell me the per-viewport status".
* **Trigger a re-capture** — when a viewport reports
  ``dimension_drift``, the operator decides whether to re-capture the
  reference. The diff just surfaces the fact; orchestration is W14 / W16.

Module-global state audit (SOP §1)
----------------------------------
Only immutable string constants (the five status literals + the diff
schema version + ``frozenset`` of statuses) plus the module-level
:data:`logger`. Cross-worker consistency is SOP answer #1 (each
``uvicorn`` worker derives the same constants from the same source).

Read-after-write timing audit (SOP §2)
--------------------------------------
N/A — pure-function diff computation, no DB read-after-write surface,
no asyncio.gather race window. The file-system read inside
:func:`compute_ghost_overlay_diff_from_disk` delegates to W13.3's
:func:`backend.web.screenshot_writer.read_screenshot_manifest`, which
itself is single-syscall (a ``read_text`` after the W13.3 writer's
``os.replace`` rename — atomic).

Compat fingerprint grep (SOP §3)
--------------------------------
Clean — no SQL, no DB, no asyncpg pool / aiosqlite_compat references.
``grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]"``
returns 0 hits in this module.

Scope (this row only)
---------------------
* Pin the five-way drift status taxonomy.
* :class:`GhostOverlayEntry` / :class:`GhostOverlayDiff` frozen
  dataclasses representing one paired viewport and the aggregate diff.
* :func:`compute_ghost_overlay_diff` — pure-function comparator that
  takes a :class:`backend.web.screenshot_writer.ScreenshotManifest` (or
  ``None`` for "no reference yet") and a sequence of W13.1
  :class:`backend.web.screenshot_capture.ViewportScreenshot` (or empty
  for "no live capture yet") and produces a deterministic
  :class:`GhostOverlayDiff`.
* :func:`compute_ghost_overlay_diff_from_disk` — convenience wrapper
  that reads the reference manifest off the project root.
* :func:`ghost_overlay_diff_to_dict` / :func:`ghost_overlay_diff_from_dict`
  / :func:`serialize_ghost_overlay_diff_json` — serialisation helpers
  for HTTP transport (the future W14 router will return the diff as a
  JSON body).

Out of scope (future rows):

* W14 — live-preview iframe + visual overlay rendering + per-tenant
  capture pipeline that *produces* the live screenshots this comparator
  consumes.
* W13.5 — 5-URL × 4-breakpoint reference matrix that pins the full
  capture-then-diff pipeline end-to-end.
* Pixel-level diff overlay (``%-of-pixels-different``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from backend.web.screenshot_capture import (
    ScreenshotCaptureError,
    Viewport,
    ViewportScreenshot,
)
import hashlib
import os
from pathlib import Path
from typing import Union

from backend.web.screenshot_writer import (
    SCREENSHOT_REFS_DIR,
    SHA256_HASH_PREFIX,
    ScreenshotManifest,
    ScreenshotManifestEntry,
    read_screenshot_manifest_if_exists,
)

# ``project_root`` arguments accept the same shapes as W13.3's writer.
# Mirrors :data:`backend.web.screenshot_writer._ProjectRoot` (the W13.3
# module's internal alias). We re-state it here rather than reaching
# into a sibling module's underscore-prefixed name.
_ProjectRoot = Union[str, "os.PathLike[str]", Path]

logger = logging.getLogger(__name__)


# ── Public constants ──────────────────────────────────────────────────

#: Schema version of the on-the-wire JSON payload. Bumped when the
#: payload shape changes in a non-backward-compatible way; readers
#: reject unknown versions to make a stale recipient fail loud rather
#: than silently mis-parse a future shape.
GHOST_OVERLAY_DIFF_VERSION: str = "1"

#: The five drift statuses one paired viewport can carry. Pinned as
#: separate string constants (rather than ``enum.Enum``) so JSON
#: round-trip stays trivial — the wire format is a plain string field.
GHOST_OVERLAY_STATUS_IDENTICAL: str = "identical"
GHOST_OVERLAY_STATUS_PIXEL_DRIFT: str = "pixel_drift"
GHOST_OVERLAY_STATUS_DIMENSION_DRIFT: str = "dimension_drift"
GHOST_OVERLAY_STATUS_MISSING_IN_LIVE: str = "missing_in_live"
GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE: str = "missing_in_reference"

#: Frozen set of every legal status value. Future readers / drift
#: guards use this to validate a received status without re-listing
#: the five literals.
GHOST_OVERLAY_STATUSES: frozenset[str] = frozenset({
    GHOST_OVERLAY_STATUS_IDENTICAL,
    GHOST_OVERLAY_STATUS_PIXEL_DRIFT,
    GHOST_OVERLAY_STATUS_DIMENSION_DRIFT,
    GHOST_OVERLAY_STATUS_MISSING_IN_LIVE,
    GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE,
})


# ── Errors ────────────────────────────────────────────────────────────


class GhostOverlayError(ScreenshotCaptureError):
    """Base class for everything raised by
    :mod:`backend.web.screenshot_ghost_overlay`.

    Subclasses :class:`backend.web.screenshot_capture.ScreenshotCaptureError`
    so existing W13 ``except`` chains catch us as well.
    """


class GhostOverlayInputError(GhostOverlayError):
    """The caller passed something that does not satisfy the
    comparator contract (wrong type, malformed manifest entry,
    duplicate viewport name within the live capture sequence, etc.).

    Distinct from a "diff says drift exists" outcome — drift is part
    of the normal output, not an error. This error is reserved for
    "we cannot even attempt the diff because the input is bad".
    """


# ── Data shapes ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class GhostOverlayEntry:
    """One paired viewport's drift record.

    The ``status`` field carries one of :data:`GHOST_OVERLAY_STATUSES`.
    Reference-side fields are populated whenever the reference manifest
    contributed an entry for this viewport name; live-side fields are
    populated whenever the live capture contributed one. When only one
    side contributes the other side's fields are ``None``.

    Frontend rendering note: ``reference_relative_path`` is the
    project-relative path of the reference PNG on disk
    (``.omnisight/refs/{name}.png``). The W14 ``LivePreviewPanel``
    serves the file from a static-file route and overlays it on the
    iframe. The live PNG bytes are NOT carried here — the live capture's
    bytes still live in the caller's :class:`ViewportScreenshot`; W14
    decides separately how to ferry those bytes (base64 in the same
    payload, multipart upload, temp-file, etc.).
    """

    name: str
    status: str
    # Reference side (None when status == missing_in_reference).
    reference_width: Optional[int] = None
    reference_height: Optional[int] = None
    reference_device_scale_factor: Optional[float] = None
    reference_is_mobile: Optional[bool] = None
    reference_sha256: Optional[str] = None
    reference_byte_size: Optional[int] = None
    reference_relative_path: Optional[str] = None
    reference_fetched_at: Optional[str] = None
    # Live side (None when status == missing_in_live).
    live_width: Optional[int] = None
    live_height: Optional[int] = None
    live_device_scale_factor: Optional[float] = None
    live_is_mobile: Optional[bool] = None
    live_sha256: Optional[str] = None
    live_byte_size: Optional[int] = None
    live_post_redirect_url: Optional[str] = None
    live_fetched_at: Optional[str] = None
    # Computed deltas — None when either side is absent.
    width_delta: Optional[int] = None
    height_delta: Optional[int] = None
    byte_size_delta: Optional[int] = None


@dataclass(frozen=True)
class GhostOverlayDiff:
    """The aggregate diff payload returned to the W14 frontend.

    ``entries`` is iterated in a deterministic order: every reference-
    side viewport first (preserving the manifest's order, which is the
    W13.2 ``DEFAULT_BREAKPOINTS`` small-to-large width ordering), then
    every live-only viewport (preserving the live capture's order)
    appended after. This keeps the rendered ghost-overlay panel in a
    stable left-to-right tab order across runs.
    """

    diff_version: str
    created_at: str
    source_url: str
    live_source_url: str
    entries: tuple[GhostOverlayEntry, ...] = field(default_factory=tuple)
    counts_by_status: Mapping[str, int] = field(default_factory=dict)
    has_drift: bool = False


# ── Helpers ───────────────────────────────────────────────────────────


def _utc_iso8601_now() -> str:
    """ISO-8601 UTC timestamp with a ``Z`` suffix. Same format pinned
    by W11.2 / W11.7 / W13.1 / W13.3 — cross-row timestamp diffs
    aren't tripped by stringification drift."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _validate_manifest(manifest: Optional[ScreenshotManifest]) -> None:
    """Defence-in-depth: reject obviously malformed manifests before we
    iterate them. The W13.3 ``read_screenshot_manifest`` already validates
    the shape, but a caller that hand-builds a :class:`ScreenshotManifest`
    might pass us garbage. We don't re-validate every field — that's the
    dataclass's job — but we do reject ``None``-shaped placeholders that
    would crash the iteration loop with a confusing ``AttributeError``.
    """
    if manifest is None:
        return
    if not isinstance(manifest, ScreenshotManifest):
        raise GhostOverlayInputError(
            "manifest must be ScreenshotManifest or None, "
            f"got {type(manifest).__name__}"
        )
    if not isinstance(manifest.screenshots, tuple):
        raise GhostOverlayInputError(
            "manifest.screenshots must be a tuple, "
            f"got {type(manifest.screenshots).__name__}"
        )
    seen: set[str] = set()
    for entry in manifest.screenshots:
        if not isinstance(entry, ScreenshotManifestEntry):
            raise GhostOverlayInputError(
                "manifest.screenshots entries must be "
                f"ScreenshotManifestEntry, got {type(entry).__name__}"
            )
        if entry.name in seen:
            raise GhostOverlayInputError(
                "manifest.screenshots contains duplicate viewport name "
                f"{entry.name!r}"
            )
        seen.add(entry.name)


def _validate_live(
    live: Sequence[ViewportScreenshot],
) -> None:
    """Reject obviously malformed live capture sequences before we
    iterate them. Mirrors the W13.3 writer's pre-flight."""
    seen: set[str] = set()
    for shot in live:
        if not isinstance(shot, ViewportScreenshot):
            raise GhostOverlayInputError(
                "live entries must be ViewportScreenshot, "
                f"got {type(shot).__name__}"
            )
        if not isinstance(shot.viewport, Viewport):
            raise GhostOverlayInputError(
                "live entry's viewport must be Viewport, "
                f"got {type(shot.viewport).__name__}"
            )
        if not isinstance(shot.png_bytes, (bytes, bytearray)) or \
                not shot.png_bytes:
            raise GhostOverlayInputError(
                f"live entry {shot.viewport.name!r} carries empty png_bytes"
            )
        if shot.viewport.name in seen:
            raise GhostOverlayInputError(
                "live entries contain duplicate viewport name "
                f"{shot.viewport.name!r}"
            )
        seen.add(shot.viewport.name)


def _live_sha256_of(png_bytes: bytes) -> str:
    """Compute the ``sha256:`` digest of a live capture's PNG bytes
    using the same prefix W13.3 stamps onto manifest entries. This is
    the equality check at the heart of the diff: a live ``sha256`` that
    equals the reference ``sha256`` means byte-identical PNGs."""
    return f"{SHA256_HASH_PREFIX}{hashlib.sha256(png_bytes).hexdigest()}"


def _classify_drift(
    ref: Optional[ScreenshotManifestEntry],
    live: Optional[tuple[ViewportScreenshot, str]],
) -> str:
    """Return the drift status for one paired (reference, live) entry.

    ``live`` is a tuple of ``(ViewportScreenshot, sha256_str)`` so the
    caller doesn't recompute the digest. ``ref`` carries its sha256
    inside the manifest entry already.
    """
    if ref is None and live is None:
        # Should never happen — caller only invokes us when at least
        # one side contributes.
        raise GhostOverlayInputError(
            "_classify_drift requires at least one side; both were None"
        )
    if ref is None:
        return GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE
    if live is None:
        return GHOST_OVERLAY_STATUS_MISSING_IN_LIVE
    live_shot, live_sha = live
    vp = live_shot.viewport
    if (
        ref.width != vp.width
        or ref.height != vp.height
        or float(ref.device_scale_factor) != float(vp.device_scale_factor)
    ):
        return GHOST_OVERLAY_STATUS_DIMENSION_DRIFT
    if ref.sha256 == live_sha:
        return GHOST_OVERLAY_STATUS_IDENTICAL
    return GHOST_OVERLAY_STATUS_PIXEL_DRIFT


def _build_entry(
    name: str,
    ref: Optional[ScreenshotManifestEntry],
    live_pair: Optional[tuple[ViewportScreenshot, str]],
) -> GhostOverlayEntry:
    """Construct a :class:`GhostOverlayEntry` from the pair. Pure
    function so tests can drive each branch independently."""
    status = _classify_drift(ref, live_pair)

    ref_width = ref_height = ref_byte_size = None
    ref_dsf: Optional[float] = None
    ref_is_mobile: Optional[bool] = None
    ref_sha = ref_path = ref_fetched = None
    if ref is not None:
        ref_width = int(ref.width)
        ref_height = int(ref.height)
        ref_dsf = float(ref.device_scale_factor)
        ref_is_mobile = bool(ref.is_mobile)
        ref_sha = ref.sha256
        ref_byte_size = int(ref.byte_size)
        ref_path = ref.relative_path
        ref_fetched = ref.fetched_at

    live_width = live_height = live_byte_size = None
    live_dsf: Optional[float] = None
    live_is_mobile: Optional[bool] = None
    live_sha = live_redirect = live_fetched = None
    if live_pair is not None:
        live_shot, live_sha_value = live_pair
        live_width = int(live_shot.viewport.width)
        live_height = int(live_shot.viewport.height)
        live_dsf = float(live_shot.viewport.device_scale_factor)
        live_is_mobile = bool(live_shot.viewport.is_mobile)
        live_sha = live_sha_value
        live_byte_size = len(live_shot.png_bytes)
        live_redirect = live_shot.post_redirect_url
        live_fetched = live_shot.fetched_at

    width_delta = (
        live_width - ref_width
        if ref_width is not None and live_width is not None
        else None
    )
    height_delta = (
        live_height - ref_height
        if ref_height is not None and live_height is not None
        else None
    )
    byte_size_delta = (
        live_byte_size - ref_byte_size
        if ref_byte_size is not None and live_byte_size is not None
        else None
    )

    return GhostOverlayEntry(
        name=name,
        status=status,
        reference_width=ref_width,
        reference_height=ref_height,
        reference_device_scale_factor=ref_dsf,
        reference_is_mobile=ref_is_mobile,
        reference_sha256=ref_sha,
        reference_byte_size=ref_byte_size,
        reference_relative_path=ref_path,
        reference_fetched_at=ref_fetched,
        live_width=live_width,
        live_height=live_height,
        live_device_scale_factor=live_dsf,
        live_is_mobile=live_is_mobile,
        live_sha256=live_sha,
        live_byte_size=live_byte_size,
        live_post_redirect_url=live_redirect,
        live_fetched_at=live_fetched,
        width_delta=width_delta,
        height_delta=height_delta,
        byte_size_delta=byte_size_delta,
    )


# ── Comparator ────────────────────────────────────────────────────────


def compute_ghost_overlay_diff(
    reference: Optional[ScreenshotManifest],
    live: Sequence[ViewportScreenshot],
    *,
    live_source_url: Optional[str] = None,
    now: Optional[str] = None,
) -> GhostOverlayDiff:
    """Compare a reference manifest with a freshly-captured live set.

    Args:
        reference: The persisted reference manifest read from
            ``.omnisight/refs/manifest.json`` (W13.3). Pass ``None``
            for the "no reference set has been pinned yet" case — every
            live capture surfaces as :data:`GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE`,
            which the W14 frontend renders as "all viewports are new
            since this is the first run".
        live: The :class:`ViewportScreenshot` tuple returned by W13.1's
            :meth:`MultiContextScreenshotCapture.capture_multi`. Pass an
            empty sequence for the "couldn't capture live this run" case
            — every reference entry surfaces as
            :data:`GHOST_OVERLAY_STATUS_MISSING_IN_LIVE`.
        live_source_url: Optional URL the live screenshots were captured
            from. Defaults to the reference manifest's ``source_url`` if
            ``reference`` is provided, else the empty string. The W14
            frontend embeds it next to the ghost overlay so the operator
            sees "what URL is this preview compared against".
        now: Optional ISO-8601 timestamp override for the diff's
            ``created_at`` field. Tests inject this for determinism;
            production callers leave it ``None``.

    Returns:
        A frozen :class:`GhostOverlayDiff` with one
        :class:`GhostOverlayEntry` per viewport name, ordered with the
        reference's viewport-name order first followed by any live-only
        viewports in their capture order. Empty inputs on both sides
        produce an empty entries tuple, ``has_drift=False``,
        ``counts_by_status={}``.

    Raises:
        GhostOverlayInputError: malformed reference (non-tuple
            ``screenshots``, non-:class:`ScreenshotManifestEntry`
            entry, duplicate viewport name in the manifest) or
            malformed live set (non-:class:`ViewportScreenshot` entry,
            empty ``png_bytes``, duplicate viewport name).
    """
    _validate_manifest(reference)
    _validate_live(live)

    # Pre-compute live sha256 once per entry — referenced from
    # _classify_drift and from the entry construction below.
    live_pairs: dict[str, tuple[ViewportScreenshot, str]] = {}
    for shot in live:
        live_pairs[shot.viewport.name] = (
            shot,
            _live_sha256_of(bytes(shot.png_bytes)),
        )

    # Iterate references first (preserving manifest order) so the
    # frontend's left-to-right tab strip stays in the W13.2 small-to-
    # large width order whenever the reference set is the canonical one.
    seen_names: set[str] = set()
    entries: list[GhostOverlayEntry] = []

    if reference is not None:
        for ref_entry in reference.screenshots:
            live_pair = live_pairs.get(ref_entry.name)
            entries.append(_build_entry(ref_entry.name, ref_entry, live_pair))
            seen_names.add(ref_entry.name)

    # Then append live-only viewports in their original capture order
    # (Python 3.7+ dicts are insertion-ordered, so iterating live_pairs
    # preserves the W13.1 capture sequence). Filter out names already
    # consumed via the reference pass.
    for name, live_pair in live_pairs.items():
        if name in seen_names:
            continue
        entries.append(_build_entry(name, None, live_pair))
        seen_names.add(name)

    # Aggregate counts.
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
    has_drift = any(
        entry.status != GHOST_OVERLAY_STATUS_IDENTICAL for entry in entries
    )

    # Resolve source URLs.
    ref_source_url = reference.source_url if reference is not None else ""
    resolved_live_url = (
        live_source_url if live_source_url is not None else ref_source_url
    )

    return GhostOverlayDiff(
        diff_version=GHOST_OVERLAY_DIFF_VERSION,
        created_at=now if now is not None else _utc_iso8601_now(),
        source_url=ref_source_url,
        live_source_url=resolved_live_url,
        entries=tuple(entries),
        counts_by_status=dict(counts),
        has_drift=has_drift,
    )


def compute_ghost_overlay_diff_from_disk(
    project_root: _ProjectRoot,
    live: Sequence[ViewportScreenshot],
    *,
    live_source_url: Optional[str] = None,
    now: Optional[str] = None,
) -> GhostOverlayDiff:
    """Convenience wrapper: read the reference manifest from
    ``<project_root>/.omnisight/refs/manifest.json`` (or treat as
    "no reference yet" when absent) and delegate to
    :func:`compute_ghost_overlay_diff`.

    Use this from a router that already has the project root in hand
    and doesn't want to plumb the reference manifest through.
    """
    reference = read_screenshot_manifest_if_exists(project_root)
    return compute_ghost_overlay_diff(
        reference,
        live,
        live_source_url=live_source_url,
        now=now,
    )


# ── Serialisation ─────────────────────────────────────────────────────


def _entry_to_dict(entry: GhostOverlayEntry) -> dict[str, Any]:
    """Render one :class:`GhostOverlayEntry` as a JSON-friendly dict."""
    return {
        "name": entry.name,
        "status": entry.status,
        "reference_width": entry.reference_width,
        "reference_height": entry.reference_height,
        "reference_device_scale_factor": entry.reference_device_scale_factor,
        "reference_is_mobile": entry.reference_is_mobile,
        "reference_sha256": entry.reference_sha256,
        "reference_byte_size": entry.reference_byte_size,
        "reference_relative_path": entry.reference_relative_path,
        "reference_fetched_at": entry.reference_fetched_at,
        "live_width": entry.live_width,
        "live_height": entry.live_height,
        "live_device_scale_factor": entry.live_device_scale_factor,
        "live_is_mobile": entry.live_is_mobile,
        "live_sha256": entry.live_sha256,
        "live_byte_size": entry.live_byte_size,
        "live_post_redirect_url": entry.live_post_redirect_url,
        "live_fetched_at": entry.live_fetched_at,
        "width_delta": entry.width_delta,
        "height_delta": entry.height_delta,
        "byte_size_delta": entry.byte_size_delta,
    }


def ghost_overlay_diff_to_dict(diff: GhostOverlayDiff) -> dict[str, Any]:
    """Render a :class:`GhostOverlayDiff` as a JSON-friendly dict.

    Pure function. Tests use it to assert the wire format; the future
    W14 router uses it to build the JSON response body.
    """
    if not isinstance(diff, GhostOverlayDiff):
        raise GhostOverlayInputError(
            "diff must be GhostOverlayDiff, "
            f"got {type(diff).__name__}"
        )
    return {
        "diff_version": diff.diff_version,
        "created_at": diff.created_at,
        "source_url": diff.source_url,
        "live_source_url": diff.live_source_url,
        "refs_dir": SCREENSHOT_REFS_DIR,
        "entries": [_entry_to_dict(e) for e in diff.entries],
        "counts_by_status": dict(diff.counts_by_status),
        "has_drift": bool(diff.has_drift),
    }


def serialize_ghost_overlay_diff_json(
    diff: GhostOverlayDiff,
    *,
    indent: Optional[int] = 2,
) -> str:
    """Render ``diff`` as a JSON string. Canonical:
    ``sort_keys=True`` + ``ensure_ascii=False`` so a diff diff is byte-
    meaningful — same discipline as W11.7 / W13.3."""
    payload = ghost_overlay_diff_to_dict(diff)
    return json.dumps(
        payload, indent=indent, sort_keys=True, ensure_ascii=False,
    )


def _entry_from_dict(payload: Mapping[str, Any]) -> GhostOverlayEntry:
    """Inverse of :func:`_entry_to_dict`. Strict — requires every
    documented key to be present (missing key → :class:`GhostOverlayInputError`)."""
    if not isinstance(payload, Mapping):
        raise GhostOverlayInputError(
            "ghost-overlay entry payload must be a mapping, "
            f"got {type(payload).__name__}"
        )
    try:
        status = str(payload["status"])
        if status not in GHOST_OVERLAY_STATUSES:
            raise GhostOverlayInputError(
                f"unknown ghost-overlay status {status!r}"
            )
        return GhostOverlayEntry(
            name=str(payload["name"]),
            status=status,
            reference_width=_optional_int(payload.get("reference_width")),
            reference_height=_optional_int(payload.get("reference_height")),
            reference_device_scale_factor=_optional_float(
                payload.get("reference_device_scale_factor")
            ),
            reference_is_mobile=_optional_bool(payload.get("reference_is_mobile")),
            reference_sha256=_optional_str(payload.get("reference_sha256")),
            reference_byte_size=_optional_int(payload.get("reference_byte_size")),
            reference_relative_path=_optional_str(
                payload.get("reference_relative_path")
            ),
            reference_fetched_at=_optional_str(payload.get("reference_fetched_at")),
            live_width=_optional_int(payload.get("live_width")),
            live_height=_optional_int(payload.get("live_height")),
            live_device_scale_factor=_optional_float(
                payload.get("live_device_scale_factor")
            ),
            live_is_mobile=_optional_bool(payload.get("live_is_mobile")),
            live_sha256=_optional_str(payload.get("live_sha256")),
            live_byte_size=_optional_int(payload.get("live_byte_size")),
            live_post_redirect_url=_optional_str(
                payload.get("live_post_redirect_url")
            ),
            live_fetched_at=_optional_str(payload.get("live_fetched_at")),
            width_delta=_optional_int(payload.get("width_delta")),
            height_delta=_optional_int(payload.get("height_delta")),
            byte_size_delta=_optional_int(payload.get("byte_size_delta")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GhostOverlayInputError(
            f"ghost-overlay entry missing or malformed field: {exc!s}"
        ) from exc


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def ghost_overlay_diff_from_dict(payload: Mapping[str, Any]) -> GhostOverlayDiff:
    """Inverse of :func:`ghost_overlay_diff_to_dict`.

    Strict — rejects unknown ``diff_version`` so a stale recipient
    fails loud rather than silently mis-parsing under a future schema
    bump. Round-tripping a freshly-computed diff through
    ``to_dict`` → ``from_dict`` returns an equal object.
    """
    if not isinstance(payload, Mapping):
        raise GhostOverlayInputError(
            "ghost-overlay diff payload must be a mapping, "
            f"got {type(payload).__name__}"
        )
    version = payload.get("diff_version")
    if version != GHOST_OVERLAY_DIFF_VERSION:
        raise GhostOverlayInputError(
            f"diff_version {version!r} unsupported "
            f"(expected {GHOST_OVERLAY_DIFF_VERSION!r})"
        )
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise GhostOverlayInputError(
            "ghost-overlay diff 'entries' field must be a list, "
            f"got {type(raw_entries).__name__}"
        )
    entries = tuple(_entry_from_dict(e) for e in raw_entries)
    raw_counts = payload.get("counts_by_status", {}) or {}
    if not isinstance(raw_counts, Mapping):
        raise GhostOverlayInputError(
            "ghost-overlay diff 'counts_by_status' field must be a mapping, "
            f"got {type(raw_counts).__name__}"
        )
    # Defence-in-depth: reject unknown status keys in the counts map.
    for status_key in raw_counts:
        if status_key not in GHOST_OVERLAY_STATUSES:
            raise GhostOverlayInputError(
                f"unknown ghost-overlay status in counts: {status_key!r}"
            )
    counts = {str(k): int(v) for k, v in raw_counts.items()}
    try:
        return GhostOverlayDiff(
            diff_version=str(payload["diff_version"]),
            created_at=str(payload["created_at"]),
            source_url=str(payload.get("source_url") or ""),
            live_source_url=str(payload.get("live_source_url") or ""),
            entries=entries,
            counts_by_status=counts,
            has_drift=bool(payload.get("has_drift", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GhostOverlayInputError(
            f"ghost-overlay diff missing or malformed field: {exc!s}"
        ) from exc


__all__ = [
    "GHOST_OVERLAY_DIFF_VERSION",
    "GHOST_OVERLAY_STATUSES",
    "GHOST_OVERLAY_STATUS_DIMENSION_DRIFT",
    "GHOST_OVERLAY_STATUS_IDENTICAL",
    "GHOST_OVERLAY_STATUS_MISSING_IN_LIVE",
    "GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE",
    "GHOST_OVERLAY_STATUS_PIXEL_DRIFT",
    "GhostOverlayDiff",
    "GhostOverlayEntry",
    "GhostOverlayError",
    "GhostOverlayInputError",
    "compute_ghost_overlay_diff",
    "compute_ghost_overlay_diff_from_disk",
    "ghost_overlay_diff_from_dict",
    "ghost_overlay_diff_to_dict",
    "serialize_ghost_overlay_diff_json",
]
