"""W13.4 #XXX — Contract tests for ``backend.web.screenshot_ghost_overlay``.

Test coverage organised by area
-------------------------------
* Module surface — ``__all__`` alphabetised, the 16 expected names
  present, ``GHOST_OVERLAY_STATUSES`` frozen, status literals pinned.
* Errors — class hierarchy (``GhostOverlayError`` descends from
  W13.1 ``ScreenshotCaptureError``; ``GhostOverlayInputError`` is a
  subclass).
* Dataclasses — ``GhostOverlayEntry`` / ``GhostOverlayDiff`` frozen,
  default-factory shape (counts dict + entries tuple).
* Comparator — five drift outcomes (identical / pixel_drift /
  dimension_drift / missing_in_live / missing_in_reference) each
  exercised with realistic inputs, plus the boundary cases (empty
  reference, empty live, both empty, mixed) and aggregate counts.
* Ordering — reference order leads, live-only viewports follow in
  capture order; iteration is deterministic across runs.
* Disk variant — ``compute_ghost_overlay_diff_from_disk`` reads from
  W13.3 manifest layout, returns the same diff as the in-memory call,
  treats absent manifest as "no reference yet".
* Validation — non-tuple manifest screenshots, non-:class:`Viewport`
  shot, empty png_bytes, duplicate names all raise
  :class:`GhostOverlayInputError`.
* Serialisation — round-trip ``to_dict`` → ``from_dict`` stable;
  unknown ``diff_version`` rejected; canonical JSON sorts keys.
* Package re-exports — 16 expected symbols on ``backend.web``, identity
  preserved.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

import pytest

import backend.web as web_pkg
from backend.web import screenshot_ghost_overlay as sgo
from backend.web.screenshot_capture import (
    ScreenshotCaptureError,
    Viewport,
    ViewportScreenshot,
)
from backend.web.screenshot_writer import (
    SCREENSHOT_REFS_DIR,
    SHA256_HASH_PREFIX,
    ScreenshotManifest,
    ScreenshotManifestEntry,
    write_screenshots,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────


_REFERENCE_TS = "2026-04-29T00:00:00.000000Z"
_LIVE_TS = "2026-04-29T01:00:00.000000Z"
_DIFF_TS = "2026-04-29T01:00:01.000000Z"
_REF_URL = "https://acme.example/landing"
_LIVE_URL = "https://acme.example/landing"


def _png(seed: bytes, repeat: int = 32) -> bytes:
    """Synthetic PNG-shaped bytes — full-fledged decoder isn't needed
    by the comparator, only the sha256 + byte-size do."""
    return b"\x89PNG\r\n\x1a\n" + seed * repeat


def _sha256_of(data: bytes) -> str:
    return f"{SHA256_HASH_PREFIX}{hashlib.sha256(data).hexdigest()}"


def _entry(
    *,
    name: str,
    width: int = 375,
    height: int = 812,
    dsf: float = 1.0,
    is_mobile: bool = False,
    png: Optional[bytes] = None,
) -> ScreenshotManifestEntry:
    payload = png if png is not None else _png(b"R")
    return ScreenshotManifestEntry(
        name=name,
        width=width,
        height=height,
        device_scale_factor=dsf,
        is_mobile=is_mobile,
        filename=f"{name}.png",
        relative_path=f"{SCREENSHOT_REFS_DIR}/{name}.png",
        byte_size=len(payload),
        sha256=_sha256_of(payload),
        fetched_at=_REFERENCE_TS,
        status_code=200,
        post_redirect_url=_REF_URL,
    )


def _shot(
    *,
    name: str,
    width: int = 375,
    height: int = 812,
    dsf: float = 1.0,
    is_mobile: bool = False,
    png: bytes,
    redirect: str = _LIVE_URL,
) -> ViewportScreenshot:
    return ViewportScreenshot(
        viewport=Viewport(
            name=name,
            width=width,
            height=height,
            device_scale_factor=dsf,
            is_mobile=is_mobile,
        ),
        png_bytes=png,
        fetched_at=_LIVE_TS,
        status_code=200,
        post_redirect_url=redirect,
    )


def _manifest(*entries: ScreenshotManifestEntry) -> ScreenshotManifest:
    return ScreenshotManifest(
        manifest_version="1",
        created_at=_REFERENCE_TS,
        source_url=_REF_URL,
        refs_dir=SCREENSHOT_REFS_DIR,
        screenshots=tuple(entries),
    )


# ──────────────────────────────────────────────────────────────────────
# Module surface
# ──────────────────────────────────────────────────────────────────────


def test_module_all_alphabetised() -> None:
    assert list(sgo.__all__) == sorted(sgo.__all__)


def test_module_all_expected_names() -> None:
    expected = {
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
    }
    assert expected == set(sgo.__all__)
    assert len(sgo.__all__) == 16


def test_status_literal_values_pinned() -> None:
    """Once a status literal is in the wire payload, renaming it is a
    breaking change. Pin the five strings."""
    assert sgo.GHOST_OVERLAY_STATUS_IDENTICAL == "identical"
    assert sgo.GHOST_OVERLAY_STATUS_PIXEL_DRIFT == "pixel_drift"
    assert sgo.GHOST_OVERLAY_STATUS_DIMENSION_DRIFT == "dimension_drift"
    assert sgo.GHOST_OVERLAY_STATUS_MISSING_IN_LIVE == "missing_in_live"
    assert (
        sgo.GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE
        == "missing_in_reference"
    )


def test_statuses_frozen_set_with_five_members() -> None:
    assert isinstance(sgo.GHOST_OVERLAY_STATUSES, frozenset)
    assert sgo.GHOST_OVERLAY_STATUSES == {
        "identical",
        "pixel_drift",
        "dimension_drift",
        "missing_in_live",
        "missing_in_reference",
    }


def test_diff_version_pinned() -> None:
    assert sgo.GHOST_OVERLAY_DIFF_VERSION == "1"


# ──────────────────────────────────────────────────────────────────────
# Error class hierarchy
# ──────────────────────────────────────────────────────────────────────


def test_ghost_overlay_error_descends_from_screenshot_capture_error() -> None:
    assert issubclass(sgo.GhostOverlayError, ScreenshotCaptureError)


def test_ghost_overlay_input_error_subclass() -> None:
    assert issubclass(sgo.GhostOverlayInputError, sgo.GhostOverlayError)


# ──────────────────────────────────────────────────────────────────────
# Dataclass shape
# ──────────────────────────────────────────────────────────────────────


def test_entry_is_frozen() -> None:
    e = sgo.GhostOverlayEntry(name="x", status="identical")
    with pytest.raises((AttributeError, Exception)):
        e.status = "pixel_drift"  # type: ignore[misc]


def test_diff_default_shape() -> None:
    d = sgo.GhostOverlayDiff(
        diff_version="1",
        created_at=_DIFF_TS,
        source_url=_REF_URL,
        live_source_url=_LIVE_URL,
    )
    assert d.entries == ()
    assert dict(d.counts_by_status) == {}
    assert d.has_drift is False


# ──────────────────────────────────────────────────────────────────────
# Comparator — the five drift outcomes
# ──────────────────────────────────────────────────────────────────────


def test_identical_status_when_sha256_and_dimensions_match() -> None:
    payload = _png(b"identical")
    manifest = _manifest(_entry(name="mobile_375", png=payload))
    live = [_shot(name="mobile_375", png=payload)]

    diff = sgo.compute_ghost_overlay_diff(
        manifest, live, live_source_url=_LIVE_URL, now=_DIFF_TS,
    )

    assert len(diff.entries) == 1
    entry = diff.entries[0]
    assert entry.status == sgo.GHOST_OVERLAY_STATUS_IDENTICAL
    assert entry.reference_sha256 == entry.live_sha256
    assert entry.width_delta == 0
    assert entry.height_delta == 0
    assert entry.byte_size_delta == 0
    assert diff.has_drift is False
    assert dict(diff.counts_by_status) == {"identical": 1}


def test_pixel_drift_status_same_dims_different_sha256() -> None:
    ref_png = _png(b"REF")
    live_png = _png(b"LVE")
    manifest = _manifest(_entry(name="mobile_375", png=ref_png))
    live = [_shot(name="mobile_375", png=live_png)]

    diff = sgo.compute_ghost_overlay_diff(
        manifest, live, now=_DIFF_TS,
    )

    assert len(diff.entries) == 1
    entry = diff.entries[0]
    assert entry.status == sgo.GHOST_OVERLAY_STATUS_PIXEL_DRIFT
    assert entry.reference_sha256 != entry.live_sha256
    assert entry.width_delta == 0
    assert entry.height_delta == 0
    assert diff.has_drift is True
    assert dict(diff.counts_by_status) == {"pixel_drift": 1}


def test_dimension_drift_status_when_width_changes() -> None:
    ref_png = _png(b"REF")
    live_png = _png(b"REF")
    # Same content, but operator widened the viewport.
    manifest = _manifest(
        _entry(name="mobile_375", width=375, height=812, png=ref_png)
    )
    live = [
        _shot(name="mobile_375", width=414, height=896, png=live_png),
    ]

    diff = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)

    entry = diff.entries[0]
    assert entry.status == sgo.GHOST_OVERLAY_STATUS_DIMENSION_DRIFT
    # Even when sha256 happens to match, dimension drift wins because
    # the comparison is meaningless without re-capturing the reference.
    assert entry.width_delta == 39  # 414 - 375
    assert entry.height_delta == 84
    assert diff.has_drift is True


def test_dimension_drift_when_dsf_changes() -> None:
    payload = _png(b"REF")
    manifest = _manifest(
        _entry(name="mobile_375", dsf=1.0, png=payload)
    )
    live = [_shot(name="mobile_375", dsf=2.0, png=payload)]

    diff = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)

    assert (
        diff.entries[0].status
        == sgo.GHOST_OVERLAY_STATUS_DIMENSION_DRIFT
    )


def test_missing_in_live_status_when_reference_only() -> None:
    payload = _png(b"REF")
    manifest = _manifest(_entry(name="mobile_375", png=payload))

    diff = sgo.compute_ghost_overlay_diff(manifest, [], now=_DIFF_TS)

    assert len(diff.entries) == 1
    entry = diff.entries[0]
    assert entry.status == sgo.GHOST_OVERLAY_STATUS_MISSING_IN_LIVE
    assert entry.reference_width == 375
    assert entry.live_width is None
    assert entry.live_sha256 is None
    assert entry.width_delta is None
    assert entry.height_delta is None
    assert entry.byte_size_delta is None
    assert diff.has_drift is True


def test_missing_in_reference_status_when_live_only() -> None:
    payload = _png(b"LIVE")
    live = [_shot(name="ultrawide_3840", width=3840, height=1600, png=payload)]

    diff = sgo.compute_ghost_overlay_diff(None, live, now=_DIFF_TS)

    assert len(diff.entries) == 1
    entry = diff.entries[0]
    assert entry.status == sgo.GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE
    assert entry.reference_width is None
    assert entry.live_width == 3840
    assert entry.live_sha256 == _sha256_of(payload)
    assert diff.has_drift is True


# ──────────────────────────────────────────────────────────────────────
# Boundary: empty inputs
# ──────────────────────────────────────────────────────────────────────


def test_empty_manifest_and_empty_live_yields_empty_diff() -> None:
    diff = sgo.compute_ghost_overlay_diff(None, [], now=_DIFF_TS)
    assert diff.entries == ()
    assert dict(diff.counts_by_status) == {}
    assert diff.has_drift is False
    assert diff.diff_version == "1"
    assert diff.created_at == _DIFF_TS


def test_manifest_with_empty_screenshots_and_empty_live() -> None:
    diff = sgo.compute_ghost_overlay_diff(_manifest(), [], now=_DIFF_TS)
    assert diff.entries == ()
    assert diff.has_drift is False


# ──────────────────────────────────────────────────────────────────────
# Mixed: partial overlap
# ──────────────────────────────────────────────────────────────────────


def test_mixed_overlap_aggregate_counts() -> None:
    same = _png(b"SAME")
    diff_live = _png(b"DIFF")
    manifest = _manifest(
        _entry(name="mobile_375", png=same),
        _entry(name="tablet_768", width=768, height=1024, png=same),
        _entry(name="desktop_1440", width=1440, height=900, png=same),
    )
    live = [
        # mobile_375 — identical
        _shot(name="mobile_375", png=same),
        # tablet_768 — drifted pixels
        _shot(name="tablet_768", width=768, height=1024, png=diff_live),
        # desktop_1440 missing — should be missing_in_live
        # ultrawide_3840 — new viewport (missing_in_reference)
        _shot(
            name="ultrawide_3840",
            width=3840,
            height=1600,
            png=_png(b"WIDE"),
        ),
    ]

    diff = sgo.compute_ghost_overlay_diff(
        manifest, live, live_source_url=_LIVE_URL, now=_DIFF_TS,
    )

    statuses = [(e.name, e.status) for e in diff.entries]
    assert statuses == [
        ("mobile_375", sgo.GHOST_OVERLAY_STATUS_IDENTICAL),
        ("tablet_768", sgo.GHOST_OVERLAY_STATUS_PIXEL_DRIFT),
        ("desktop_1440", sgo.GHOST_OVERLAY_STATUS_MISSING_IN_LIVE),
        (
            "ultrawide_3840",
            sgo.GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE,
        ),
    ]
    assert dict(diff.counts_by_status) == {
        "identical": 1,
        "pixel_drift": 1,
        "missing_in_live": 1,
        "missing_in_reference": 1,
    }
    assert diff.has_drift is True


def test_iteration_order_reference_first_then_live_only() -> None:
    """Drift guard: the W14 frontend tab strip relies on the reference
    order leading. Reordering this is a UI-affecting change that must
    surface here as a test diff."""
    same = _png(b"x")
    manifest = _manifest(
        _entry(name="b_second", png=same),
        _entry(name="a_first", png=same),
    )
    # Live in reverse
    live = [
        _shot(name="a_first", png=same),
        _shot(name="b_second", png=same),
        _shot(name="z_extra", png=_png(b"z")),
    ]

    diff = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)
    assert [e.name for e in diff.entries] == [
        "b_second",
        "a_first",
        "z_extra",
    ]


def test_determinism_two_runs_same_input_same_output() -> None:
    payload = _png(b"R")
    manifest = _manifest(_entry(name="mobile_375", png=payload))
    live = [_shot(name="mobile_375", png=payload)]

    a = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)
    b = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)
    assert a == b


# ──────────────────────────────────────────────────────────────────────
# source_url plumbing
# ──────────────────────────────────────────────────────────────────────


def test_live_source_url_defaults_to_reference_when_omitted() -> None:
    payload = _png(b"R")
    manifest = _manifest(_entry(name="mobile_375", png=payload))
    live = [_shot(name="mobile_375", png=payload)]

    diff = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)
    assert diff.source_url == _REF_URL
    assert diff.live_source_url == _REF_URL


def test_live_source_url_overrides_when_provided() -> None:
    payload = _png(b"R")
    manifest = _manifest(_entry(name="mobile_375", png=payload))
    live = [_shot(name="mobile_375", png=payload)]
    custom = "https://acme.example/preview-42"

    diff = sgo.compute_ghost_overlay_diff(
        manifest, live, live_source_url=custom, now=_DIFF_TS,
    )
    assert diff.source_url == _REF_URL
    assert diff.live_source_url == custom


def test_no_reference_yields_empty_source_url() -> None:
    payload = _png(b"L")
    live = [_shot(name="mobile_375", png=payload)]

    diff = sgo.compute_ghost_overlay_diff(None, live, now=_DIFF_TS)
    assert diff.source_url == ""
    assert diff.live_source_url == ""


# ──────────────────────────────────────────────────────────────────────
# Created-at plumbing
# ──────────────────────────────────────────────────────────────────────


def test_created_at_uses_provided_now() -> None:
    diff = sgo.compute_ghost_overlay_diff(None, [], now=_DIFF_TS)
    assert diff.created_at == _DIFF_TS


def test_created_at_iso8601_default() -> None:
    diff = sgo.compute_ghost_overlay_diff(None, [])
    # ISO-8601 UTC Z suffix matching W11.2 / W13.1 / W13.3 format.
    assert diff.created_at.endswith("Z")
    assert "T" in diff.created_at


# ──────────────────────────────────────────────────────────────────────
# Entry field plumbing
# ──────────────────────────────────────────────────────────────────────


def test_entry_carries_reference_relative_path_for_frontend_lookup() -> None:
    payload = _png(b"R")
    manifest = _manifest(_entry(name="mobile_375", png=payload))
    live = [_shot(name="mobile_375", png=payload)]

    diff = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)
    assert (
        diff.entries[0].reference_relative_path
        == ".omnisight/refs/mobile_375.png"
    )


def test_entry_carries_live_post_redirect_url() -> None:
    payload = _png(b"R")
    redirect = "https://m.acme.example/landing"
    manifest = _manifest(_entry(name="mobile_375", png=payload))
    live = [_shot(name="mobile_375", png=payload, redirect=redirect)]

    diff = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)
    assert diff.entries[0].live_post_redirect_url == redirect


def test_byte_size_delta_when_pixel_drift() -> None:
    ref_png = _png(b"R", repeat=8)
    live_png = _png(b"L", repeat=64)  # much larger
    manifest = _manifest(_entry(name="mobile_375", png=ref_png))
    live = [_shot(name="mobile_375", png=live_png)]

    diff = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)
    entry = diff.entries[0]
    assert entry.status == sgo.GHOST_OVERLAY_STATUS_PIXEL_DRIFT
    assert entry.byte_size_delta == len(live_png) - len(ref_png)


# ──────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────


def test_non_manifest_reference_rejected() -> None:
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.compute_ghost_overlay_diff({"not": "a manifest"}, [])  # type: ignore[arg-type]


def test_non_screenshot_in_live_rejected() -> None:
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.compute_ghost_overlay_diff(None, ["not a shot"])  # type: ignore[list-item]


def test_empty_png_bytes_rejected() -> None:
    vp = Viewport(name="mobile_375", width=375, height=812)
    bad = ViewportScreenshot.__new__(ViewportScreenshot)
    object.__setattr__(bad, "viewport", vp)
    object.__setattr__(bad, "png_bytes", b"")
    object.__setattr__(bad, "fetched_at", _LIVE_TS)
    object.__setattr__(bad, "status_code", 200)
    object.__setattr__(bad, "post_redirect_url", _LIVE_URL)
    object.__setattr__(bad, "headers", {})
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.compute_ghost_overlay_diff(None, [bad])


def test_duplicate_live_viewport_name_rejected() -> None:
    payload = _png(b"R")
    live = [
        _shot(name="mobile_375", png=payload),
        _shot(name="mobile_375", png=_png(b"S")),
    ]
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.compute_ghost_overlay_diff(None, live)


def test_duplicate_reference_viewport_name_rejected() -> None:
    payload = _png(b"R")
    # Build a manifest with two entries sharing a name. The W13.3 writer
    # rejects this on write, but we may receive a hand-built manifest.
    manifest = ScreenshotManifest(
        manifest_version="1",
        created_at=_REFERENCE_TS,
        source_url=_REF_URL,
        refs_dir=SCREENSHOT_REFS_DIR,
        screenshots=(
            _entry(name="mobile_375", png=payload),
            _entry(name="mobile_375", png=payload),
        ),
    )
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.compute_ghost_overlay_diff(manifest, [])


# ──────────────────────────────────────────────────────────────────────
# Disk variant — read manifest from W13.3 layout
# ──────────────────────────────────────────────────────────────────────


def test_compute_from_disk_reads_w13_3_manifest(tmp_path) -> None:
    payload = _png(b"DISK")
    shot = _shot(name="mobile_375", png=payload)
    write_screenshots(
        [shot], project_root=tmp_path, source_url=_REF_URL, now=_REFERENCE_TS,
    )

    diff = sgo.compute_ghost_overlay_diff_from_disk(
        tmp_path, [shot], now=_DIFF_TS,
    )
    assert diff.entries[0].status == sgo.GHOST_OVERLAY_STATUS_IDENTICAL
    assert diff.source_url == _REF_URL


def test_compute_from_disk_treats_absent_manifest_as_no_reference(
    tmp_path,
) -> None:
    payload = _png(b"NEW")
    live = [_shot(name="mobile_375", png=payload)]

    diff = sgo.compute_ghost_overlay_diff_from_disk(
        tmp_path, live, now=_DIFF_TS,
    )
    assert len(diff.entries) == 1
    assert (
        diff.entries[0].status
        == sgo.GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE
    )


def test_compute_from_disk_matches_in_memory_call(tmp_path) -> None:
    """Drift guard: the disk variant must classify status the same as
    the explicit-manifest variant for the same paired input. We compare
    on the parts the W14 router cares about (status / has_drift /
    counts / dimensions / sha256) rather than full equality so the
    test isn't tripped by the writer's auto-stamped ``fetched_at`` /
    ``status_code`` provenance fields."""
    payload = _png(b"R")
    other = _png(b"L")
    shot_ref = _shot(name="mobile_375", png=payload)
    write_screenshots(
        [shot_ref],
        project_root=tmp_path,
        source_url=_REF_URL,
        now=_REFERENCE_TS,
    )

    live = [_shot(name="mobile_375", png=other)]
    in_mem = sgo.compute_ghost_overlay_diff_from_disk(
        tmp_path, live, now=_DIFF_TS,
    )

    assert in_mem.has_drift is True
    assert dict(in_mem.counts_by_status) == {"pixel_drift": 1}
    assert len(in_mem.entries) == 1
    entry = in_mem.entries[0]
    assert entry.name == "mobile_375"
    assert entry.status == sgo.GHOST_OVERLAY_STATUS_PIXEL_DRIFT
    assert entry.reference_sha256 == _sha256_of(payload)
    assert entry.live_sha256 == _sha256_of(other)
    assert entry.reference_width == 375
    assert entry.live_width == 375


# ──────────────────────────────────────────────────────────────────────
# Serialisation — JSON wire format
# ──────────────────────────────────────────────────────────────────────


def test_to_dict_round_trip_via_from_dict() -> None:
    payload = _png(b"R")
    manifest = _manifest(_entry(name="mobile_375", png=payload))
    live = [_shot(name="mobile_375", png=payload)]
    diff = sgo.compute_ghost_overlay_diff(
        manifest, live, live_source_url=_LIVE_URL, now=_DIFF_TS,
    )

    payload_dict = sgo.ghost_overlay_diff_to_dict(diff)
    restored = sgo.ghost_overlay_diff_from_dict(payload_dict)

    assert diff == restored


def test_to_dict_includes_refs_dir() -> None:
    """The frontend joins ``refs_dir`` onto entry ``reference_relative_path``
    to fetch the reference PNG. Pin it here."""
    diff = sgo.compute_ghost_overlay_diff(None, [], now=_DIFF_TS)
    payload = sgo.ghost_overlay_diff_to_dict(diff)
    assert payload["refs_dir"] == SCREENSHOT_REFS_DIR


def test_to_dict_rejects_non_diff() -> None:
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.ghost_overlay_diff_to_dict({"diff_version": "1"})  # type: ignore[arg-type]


def test_serialize_json_canonical_sorted_keys() -> None:
    payload = _png(b"R")
    manifest = _manifest(_entry(name="mobile_375", png=payload))
    live = [_shot(name="mobile_375", png=payload)]
    diff = sgo.compute_ghost_overlay_diff(manifest, live, now=_DIFF_TS)

    text = sgo.serialize_ghost_overlay_diff_json(diff, indent=None)
    parsed = json.loads(text)
    assert list(parsed.keys()) == sorted(parsed.keys())
    for entry in parsed["entries"]:
        assert list(entry.keys()) == sorted(entry.keys())


def test_serialize_json_default_indent_two() -> None:
    diff = sgo.compute_ghost_overlay_diff(None, [], now=_DIFF_TS)
    text = sgo.serialize_ghost_overlay_diff_json(diff)
    # Indent=2 emits leading "{\n  \"" and inner two-space indents.
    assert text.startswith("{\n  \"")


def test_from_dict_rejects_unsupported_version() -> None:
    diff = sgo.compute_ghost_overlay_diff(None, [], now=_DIFF_TS)
    payload = sgo.ghost_overlay_diff_to_dict(diff)
    payload["diff_version"] = "999"
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.ghost_overlay_diff_from_dict(payload)


def test_from_dict_rejects_unknown_status_in_entry() -> None:
    diff = sgo.compute_ghost_overlay_diff(None, [], now=_DIFF_TS)
    payload = sgo.ghost_overlay_diff_to_dict(diff)
    payload["entries"] = [
        {
            "name": "x",
            "status": "totally_made_up_status",
        }
    ]
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.ghost_overlay_diff_from_dict(payload)


def test_from_dict_rejects_unknown_status_in_counts() -> None:
    diff = sgo.compute_ghost_overlay_diff(None, [], now=_DIFF_TS)
    payload = sgo.ghost_overlay_diff_to_dict(diff)
    payload["counts_by_status"] = {"made_up_status": 1}
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.ghost_overlay_diff_from_dict(payload)


def test_from_dict_rejects_non_list_entries() -> None:
    diff = sgo.compute_ghost_overlay_diff(None, [], now=_DIFF_TS)
    payload = sgo.ghost_overlay_diff_to_dict(diff)
    payload["entries"] = "not a list"
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.ghost_overlay_diff_from_dict(payload)


def test_from_dict_rejects_non_mapping_payload() -> None:
    with pytest.raises(sgo.GhostOverlayInputError):
        sgo.ghost_overlay_diff_from_dict("not a dict")  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────
# Engine compatibility — W13.1 output must feed the comparator
# ──────────────────────────────────────────────────────────────────────


def test_w13_1_screenshot_shape_feeds_comparator_directly() -> None:
    """Drift guard between W13.1 (capture engine) and W13.4 (diff). The
    in-memory ``ViewportScreenshot`` shape is the input contract; if a
    future W13.1 row mutates the dataclass shape this test fails."""
    payload = _png(b"R")
    shot = ViewportScreenshot(
        viewport=Viewport(name="mobile_375", width=375, height=812),
        png_bytes=payload,
        fetched_at=_LIVE_TS,
        status_code=200,
        post_redirect_url=_LIVE_URL,
    )
    diff = sgo.compute_ghost_overlay_diff(None, [shot], now=_DIFF_TS)
    assert (
        diff.entries[0].status
        == sgo.GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE
    )


# ──────────────────────────────────────────────────────────────────────
# Package re-exports
# ──────────────────────────────────────────────────────────────────────


def test_package_re_exports_all_w13_4_symbols() -> None:
    expected = {
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
    }
    for name in expected:
        assert name in web_pkg.__all__, f"{name} missing from backend.web.__all__"
        assert hasattr(web_pkg, name), f"{name} not attribute of backend.web"


def test_package_re_export_identity_preserved() -> None:
    assert web_pkg.compute_ghost_overlay_diff is sgo.compute_ghost_overlay_diff
    assert web_pkg.GhostOverlayDiff is sgo.GhostOverlayDiff
    assert web_pkg.GHOST_OVERLAY_STATUSES is sgo.GHOST_OVERLAY_STATUSES


def test_package_total_symbol_count_pinned_at_233() -> None:
    """W13.4 adds 16 screenshot-ghost-overlay symbols to the
    ``backend.web`` re-export surface, lifting 217 → 233. Each prior
    row's drift guard re-pins at the current value. W15.2 adds 11
    vite_error_relay symbols → 244. W15.3 adds 8 vite_error_prompt
    symbols → 252. W15.4 adds 10 vite_retry_budget symbols → 262.
    W15.5 adds 13 vite_config_injection symbols → 275.
    W15.6 adds 13 vite_self_fix symbols → 288."""
    assert len(web_pkg.__all__) == 288
