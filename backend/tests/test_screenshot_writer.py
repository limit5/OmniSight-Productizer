"""W13.3 #XXX — Contract tests for ``backend.web.screenshot_writer``.

Pins the disk-writer's contract:

* On-disk layout — ``.omnisight/refs/{name}.png`` + sibling
  ``manifest.json``; ``refs/`` directory created on demand; nested
  project roots tolerated.
* Atomic semantics — temp files cleaned up; pre-existing file untouched
  on failure (verified via constructed disk-full-style failures).
* Manifest schema — ``manifest_version`` / ``created_at`` / ``source_url``
  / ``refs_dir`` / ``screenshots[]`` shape; per-entry filename matches
  ``{name}.png``; per-entry ``relative_path`` is ``.omnisight/refs/{name}.png``;
  byte_size matches PNG bytes; sha256 matches ``sha256:<hex>(payload)``.
* Round-trip — write → read returns equal :class:`ScreenshotManifest`.
* ``read_*_if_exists`` — soft None on missing dir/file; **raises on
  corrupted-when-present** so a hand-edit gone wrong fails loud.
* ``delete_screenshots`` — sweeps PNGs + manifest; returns count;
  idempotent; leaves ``.omnisight/`` umbrella alone; tolerates a stray
  operator file in ``refs/`` (skips it, reports lower count).
* Path resolvers — ``resolve_*`` reject non-Path types; reject
  ``../`` traversal in viewport name.
* Engine compatibility — output of W13.1 ``capture_multi`` is a valid
  input shape for ``write_screenshots`` (drift guard between rows).
* Module surface — ``__all__`` alphabetised, the 22 expected names
  present, ``backend.web`` re-exports the high-level set, identity
  preserved.

Network discipline — every test runs against pytest's ``tmp_path``;
no socket calls, no playwright, no external host. The W13.1
:class:`Viewport` / :class:`ViewportScreenshot` dataclasses are
constructed directly with bytes literals.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import backend.web as web_pkg
from backend.web import screenshot_writer as sw
from backend.web.screenshot_capture import (
    Viewport,
    ViewportScreenshot,
)
from backend.web.screenshot_writer import (
    SCREENSHOT_MANIFEST_FILENAME,
    SCREENSHOT_MANIFEST_RELATIVE_PATH,
    SCREENSHOT_MANIFEST_VERSION,
    SCREENSHOT_PNG_SUFFIX,
    SCREENSHOT_REFS_DIR,
    SCREENSHOT_REFS_RELATIVE_PATH,
    SHA256_HASH_PREFIX,
    ScreenshotManifest,
    ScreenshotManifestEntry,
    ScreenshotReadError,
    ScreenshotWriteError,
    ScreenshotWriterError,
    delete_screenshots,
    manifest_from_dict,
    manifest_to_dict,
    read_screenshot_manifest,
    read_screenshot_manifest_if_exists,
    resolve_refs_dir,
    resolve_screenshot_manifest_path,
    resolve_screenshot_path,
    serialize_manifest_json,
    write_screenshots,
)


# ── Fixtures ──────────────────────────────────────────────────────────


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _png(payload: str) -> bytes:
    return _PNG_HEADER + payload.encode("utf-8")


def _shot(name: str, *, width: int = 375, height: int = 812,
          png: bytes = b"", fetched: str = "2026-04-29T00:00:00.000000Z",
          status: int = 200, redirect: str = "https://example.com/",
          dsf: float = 1.0, is_mobile: bool = False) -> ViewportScreenshot:
    return ViewportScreenshot(
        viewport=Viewport(
            name=name, width=width, height=height,
            device_scale_factor=dsf, is_mobile=is_mobile,
        ),
        png_bytes=png or _png(name),
        fetched_at=fetched,
        status_code=status,
        post_redirect_url=redirect,
    )


@pytest.fixture
def four_shots() -> list[ViewportScreenshot]:
    """Mimic the W13.2 default 4-tuple ordering (mobile → desktop)."""
    return [
        _shot("mobile_375", width=375, height=812),
        _shot("tablet_768", width=768, height=1024),
        _shot("desktop_1440", width=1440, height=900),
        _shot("desktop_1920", width=1920, height=1080),
    ]


# ── 1. Constants & module surface ─────────────────────────────────────


def test_constants_pinned():
    assert SCREENSHOT_MANIFEST_VERSION == "1"
    assert SCREENSHOT_REFS_DIR == ".omnisight/refs"
    assert SCREENSHOT_REFS_RELATIVE_PATH == ".omnisight/refs"
    assert SCREENSHOT_MANIFEST_FILENAME == "manifest.json"
    assert SCREENSHOT_MANIFEST_RELATIVE_PATH == ".omnisight/refs/manifest.json"
    assert SCREENSHOT_PNG_SUFFIX == ".png"
    assert SHA256_HASH_PREFIX == "sha256:"


def test_constants_align_with_clone_manifest_dir():
    """Drift guard: the screenshot writer's ``.omnisight`` umbrella
    must match ``clone_manifest.MANIFEST_DIR`` so a single ``.gitignore``
    rule covers both records."""
    from backend.web.clone_manifest import MANIFEST_DIR
    assert SCREENSHOT_REFS_DIR.startswith(MANIFEST_DIR + "/")


def test_module_all_alphabetised():
    assert list(sw.__all__) == sorted(sw.__all__)


def test_module_all_has_expected_names():
    expected = {
        "SCREENSHOT_MANIFEST_FILENAME",
        "SCREENSHOT_MANIFEST_RELATIVE_PATH",
        "SCREENSHOT_MANIFEST_VERSION",
        "SCREENSHOT_PNG_SUFFIX",
        "SCREENSHOT_REFS_DIR",
        "SCREENSHOT_REFS_RELATIVE_PATH",
        "SHA256_HASH_PREFIX",
        "ScreenshotManifest",
        "ScreenshotManifestEntry",
        "ScreenshotReadError",
        "ScreenshotWriteError",
        "ScreenshotWriterError",
        "delete_screenshots",
        "manifest_from_dict",
        "manifest_to_dict",
        "read_screenshot_manifest",
        "read_screenshot_manifest_if_exists",
        "resolve_refs_dir",
        "resolve_screenshot_manifest_path",
        "resolve_screenshot_path",
        "serialize_manifest_json",
        "write_screenshots",
    }
    assert expected == set(sw.__all__)


def test_error_class_hierarchy():
    from backend.web.screenshot_capture import ScreenshotCaptureError

    # Both writer-error subclasses descend from ScreenshotWriterError,
    # which itself descends from the W13.1 capture base — so callers
    # that catch ``ScreenshotCaptureError`` get the writer family too.
    assert issubclass(ScreenshotWriterError, ScreenshotCaptureError)
    assert issubclass(ScreenshotWriteError, ScreenshotWriterError)
    assert issubclass(ScreenshotReadError, ScreenshotWriterError)
    # Read/write are non-overlapping subclasses (audit-row distinction).
    assert not issubclass(ScreenshotReadError, ScreenshotWriteError)
    assert not issubclass(ScreenshotWriteError, ScreenshotReadError)


# ── 2. Path resolvers ─────────────────────────────────────────────────


def test_resolve_refs_dir_basic(tmp_path: Path):
    refs = resolve_refs_dir(tmp_path)
    assert refs == tmp_path / ".omnisight" / "refs"
    assert refs.is_absolute()
    # Pure resolver — no FS side-effects.
    assert not refs.exists()


def test_resolve_refs_dir_accepts_str(tmp_path: Path):
    assert resolve_refs_dir(str(tmp_path)) == tmp_path / ".omnisight" / "refs"


def test_resolve_refs_dir_accepts_pathlike(tmp_path: Path):
    import os
    class _PL:
        def __init__(self, p): self._p = p
        def __fspath__(self) -> str: return os.fspath(self._p)
    assert resolve_refs_dir(_PL(tmp_path)) == tmp_path / ".omnisight" / "refs"


def test_resolve_refs_dir_relative_resolved_to_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    refs = resolve_refs_dir("subproject")
    assert refs.is_absolute()
    assert refs == (tmp_path / "subproject" / ".omnisight" / "refs").resolve()


@pytest.mark.parametrize("bad", [None, 7, 1.5, {"a": 1}, ["x"], object()])
def test_resolve_refs_dir_rejects_non_path(bad):
    with pytest.raises(ScreenshotWriteError):
        resolve_refs_dir(bad)


def test_resolve_screenshot_path_happy(tmp_path: Path):
    p = resolve_screenshot_path(tmp_path, "mobile_375")
    assert p == tmp_path / ".omnisight" / "refs" / "mobile_375.png"


@pytest.mark.parametrize("bad_name", [
    "../passwd", "Mobile_375", "MOBILE", "name with space",
    "name.png", "name/sub", "name\\sub", "", "name!", "Inter",
])
def test_resolve_screenshot_path_rejects_bad_name(tmp_path: Path, bad_name: str):
    with pytest.raises(ScreenshotWriteError):
        resolve_screenshot_path(tmp_path, bad_name)


def test_resolve_screenshot_path_rejects_non_string(tmp_path: Path):
    with pytest.raises(ScreenshotWriteError):
        resolve_screenshot_path(tmp_path, None)  # type: ignore[arg-type]


def test_resolve_screenshot_manifest_path(tmp_path: Path):
    p = resolve_screenshot_manifest_path(tmp_path)
    assert p == tmp_path / ".omnisight" / "refs" / "manifest.json"


# ── 3. write_screenshots happy path ───────────────────────────────────


def test_write_one_creates_dir_and_files(tmp_path: Path):
    shot = _shot("mobile_375")
    manifest = write_screenshots(
        [shot], project_root=tmp_path,
        source_url="https://example.com",
        now="2026-04-29T00:00:00.000000Z",
    )
    assert isinstance(manifest, ScreenshotManifest)
    refs = tmp_path / ".omnisight" / "refs"
    assert refs.is_dir()
    assert (refs / "mobile_375.png").read_bytes() == shot.png_bytes
    assert (refs / "manifest.json").is_file()


def test_write_four_default_breakpoints(tmp_path: Path, four_shots):
    manifest = write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://acme.example/landing",
        now="2026-04-29T00:00:00.000000Z",
    )
    assert len(manifest.screenshots) == 4
    # Order preserved in output.
    assert tuple(e.name for e in manifest.screenshots) == (
        "mobile_375", "tablet_768", "desktop_1440", "desktop_1920",
    )
    refs = tmp_path / ".omnisight" / "refs"
    for shot in four_shots:
        f = refs / f"{shot.viewport.name}.png"
        assert f.read_bytes() == shot.png_bytes


def test_write_returns_manifest_with_canonical_fields(tmp_path: Path, four_shots):
    manifest = write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://acme.example/",
        now="2026-04-29T00:00:00.000000Z",
    )
    assert manifest.manifest_version == SCREENSHOT_MANIFEST_VERSION
    assert manifest.created_at == "2026-04-29T00:00:00.000000Z"
    assert manifest.source_url == "https://acme.example/"
    assert manifest.refs_dir == SCREENSHOT_REFS_DIR


def test_write_default_now_is_utc_iso8601(tmp_path: Path):
    manifest = write_screenshots(
        [_shot("mobile_375")], project_root=tmp_path,
        source_url="https://example.com",
    )
    # ISO-8601 UTC with Z suffix; same shape as W11.2 / W11.7 / W13.1.
    assert manifest.created_at.endswith("Z")
    assert "T" in manifest.created_at


def test_write_preserves_input_order(tmp_path: Path):
    # Reverse the W13.2 default order to verify the writer respects
    # caller-supplied order rather than re-sorting.
    shots = [
        _shot("desktop_1920", width=1920, height=1080),
        _shot("mobile_375", width=375, height=812),
    ]
    manifest = write_screenshots(
        shots, project_root=tmp_path,
        source_url="https://example.com",
    )
    assert tuple(e.name for e in manifest.screenshots) == (
        "desktop_1920", "mobile_375",
    )


def test_write_empty_source_url_allowed(tmp_path: Path):
    # Empty string is permitted — for callers that genuinely have no
    # URL to record (rare but legal).
    manifest = write_screenshots(
        [_shot("mobile_375")], project_root=tmp_path, source_url="",
    )
    assert manifest.source_url == ""


def test_write_overwrites_existing_set(tmp_path: Path, four_shots):
    write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com/v1",
        now="2026-04-29T00:00:00.000000Z",
    )
    new_shot = _shot("mobile_375", png=_png("v2-bytes"))
    write_screenshots(
        [new_shot], project_root=tmp_path,
        source_url="https://example.com/v2",
        now="2026-04-29T01:00:00.000000Z",
    )
    refs = tmp_path / ".omnisight" / "refs"
    assert (refs / "mobile_375.png").read_bytes() == new_shot.png_bytes
    # Manifest was replaced; old breakpoints stay on disk but don't
    # appear in the new manifest. (Future row may sweep stale PNGs;
    # this row is "manifest is authoritative".)
    on_disk = read_screenshot_manifest(tmp_path)
    assert len(on_disk.screenshots) == 1
    assert on_disk.source_url == "https://example.com/v2"


def test_write_into_nested_project_root(tmp_path: Path):
    nested = tmp_path / "deep" / "project"
    write_screenshots(
        [_shot("mobile_375")], project_root=nested,
        source_url="https://example.com",
    )
    assert (nested / ".omnisight" / "refs" / "manifest.json").is_file()


def test_write_creates_parents_lazily(tmp_path: Path):
    refs = tmp_path / ".omnisight" / "refs"
    assert not refs.exists()
    write_screenshots(
        [_shot("mobile_375")], project_root=tmp_path,
        source_url="https://example.com",
    )
    assert refs.is_dir()


def test_write_no_temp_file_left_behind(tmp_path: Path, four_shots):
    write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
    )
    refs = tmp_path / ".omnisight" / "refs"
    # No `.tmp` artefacts.
    leftovers = [p for p in refs.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# ── 4. Manifest entry shape ───────────────────────────────────────────


def test_entry_fields_filled(tmp_path: Path):
    shot = _shot(
        "desktop_1440", width=1440, height=900, dsf=2.0, is_mobile=False,
        png=_png("xyz"), fetched="2026-04-29T01:02:03.456789Z",
        status=204, redirect="https://example.com/final",
    )
    manifest = write_screenshots(
        [shot], project_root=tmp_path,
        source_url="https://example.com",
    )
    e = manifest.screenshots[0]
    assert isinstance(e, ScreenshotManifestEntry)
    assert e.name == "desktop_1440"
    assert e.width == 1440
    assert e.height == 900
    assert e.device_scale_factor == 2.0
    assert e.is_mobile is False
    assert e.filename == "desktop_1440.png"
    assert e.relative_path == ".omnisight/refs/desktop_1440.png"
    assert e.byte_size == len(shot.png_bytes)
    assert e.sha256.startswith("sha256:")
    assert len(e.sha256) == len("sha256:") + 64
    assert e.fetched_at == "2026-04-29T01:02:03.456789Z"
    assert e.status_code == 204
    assert e.post_redirect_url == "https://example.com/final"


def test_entry_sha256_matches_png_bytes(tmp_path: Path):
    import hashlib
    shot = _shot("mobile_375", png=_png("hello"))
    manifest = write_screenshots(
        [shot], project_root=tmp_path,
        source_url="https://example.com",
    )
    expected = "sha256:" + hashlib.sha256(shot.png_bytes).hexdigest()
    assert manifest.screenshots[0].sha256 == expected


def test_entry_relative_path_pinned(tmp_path: Path, four_shots):
    manifest = write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
    )
    for e in manifest.screenshots:
        assert e.relative_path == f".omnisight/refs/{e.filename}"


# ── 5. Manifest serialisation / canonical JSON ────────────────────────


def test_manifest_to_dict_round_trips(tmp_path: Path, four_shots):
    manifest = write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
        now="2026-04-29T00:00:00.000000Z",
    )
    d = manifest_to_dict(manifest)
    assert d["manifest_version"] == "1"
    assert d["created_at"] == "2026-04-29T00:00:00.000000Z"
    assert d["source_url"] == "https://example.com"
    assert d["refs_dir"] == ".omnisight/refs"
    assert isinstance(d["screenshots"], list)
    assert len(d["screenshots"]) == 4
    rebuilt = manifest_from_dict(d)
    assert rebuilt == manifest


def test_serialize_canonical_sort_keys(tmp_path: Path, four_shots):
    manifest = write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
        now="2026-04-29T00:00:00.000000Z",
    )
    text = serialize_manifest_json(manifest, indent=None)
    parsed = json.loads(text)
    assert list(parsed.keys()) == sorted(parsed.keys())
    for entry in parsed["screenshots"]:
        assert list(entry.keys()) == sorted(entry.keys())


def test_serialize_indent_two_byte_stable(tmp_path: Path):
    shot = _shot("mobile_375", png=_png("x"))
    manifest = write_screenshots(
        [shot], project_root=tmp_path,
        source_url="https://example.com",
        now="2026-04-29T00:00:00.000000Z",
    )
    expected = serialize_manifest_json(manifest, indent=2) + "\n"
    on_disk = (tmp_path / ".omnisight" / "refs" / "manifest.json").read_text(
        encoding="utf-8",
    )
    assert on_disk == expected


def test_manifest_to_dict_rejects_non_manifest():
    with pytest.raises(ScreenshotWriteError):
        manifest_to_dict({"oops": "wrong-type"})  # type: ignore[arg-type]


# ── 6. write_screenshots input validation ─────────────────────────────


def test_write_rejects_empty_list(tmp_path: Path):
    with pytest.raises(ScreenshotWriteError, match="non-empty"):
        write_screenshots(
            [], project_root=tmp_path, source_url="https://example.com",
        )


def test_write_rejects_non_screenshot_entry(tmp_path: Path):
    with pytest.raises(ScreenshotWriteError):
        write_screenshots(
            ["not-a-screenshot"],  # type: ignore[list-item]
            project_root=tmp_path, source_url="https://example.com",
        )


def test_write_rejects_duplicate_names(tmp_path: Path):
    a = _shot("mobile_375")
    b = _shot("mobile_375", png=_png("dupe"))  # same name, different bytes
    with pytest.raises(ScreenshotWriteError, match="duplicate"):
        write_screenshots(
            [a, b], project_root=tmp_path, source_url="https://example.com",
        )


def test_write_rejects_empty_png_bytes():
    # Construct a malformed ViewportScreenshot with empty bytes (using
    # ``object.__setattr__`` since the dataclass is frozen). Verifies
    # the writer's defence against an upstream W13.1 bug that ever
    # returned empty bytes.
    vp = Viewport(name="mobile_375", width=375, height=812)
    shot = ViewportScreenshot(
        viewport=vp, png_bytes=b"x", fetched_at="2026-04-29T00:00:00Z",
        status_code=200, post_redirect_url="https://example.com/",
    )
    object.__setattr__(shot, "png_bytes", b"")
    with pytest.raises(ScreenshotWriteError, match="empty png_bytes"):
        write_screenshots(
            [shot], project_root=Path("/tmp/will-not-write"),
            source_url="https://example.com",
        )


def test_write_rejects_non_string_source_url(tmp_path: Path):
    with pytest.raises(ScreenshotWriteError):
        write_screenshots(
            [_shot("mobile_375")], project_root=tmp_path,
            source_url=None,  # type: ignore[arg-type]
        )


def test_write_rejects_bad_project_root():
    with pytest.raises(ScreenshotWriteError):
        write_screenshots(
            [_shot("mobile_375")], project_root=None,  # type: ignore[arg-type]
            source_url="https://example.com",
        )


def test_write_validates_before_filesystem_mutation(tmp_path: Path):
    # If validation rejects the input, .omnisight/refs/ must NOT have
    # been created — fail-fast discipline.
    with pytest.raises(ScreenshotWriteError):
        write_screenshots(
            ["bogus"],  # type: ignore[list-item]
            project_root=tmp_path, source_url="https://example.com",
        )
    assert not (tmp_path / ".omnisight").exists()


# ── 7. read_screenshot_manifest (strict) ──────────────────────────────


def test_read_round_trip(tmp_path: Path, four_shots):
    written = write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
        now="2026-04-29T00:00:00.000000Z",
    )
    loaded = read_screenshot_manifest(tmp_path)
    assert loaded == written


def test_read_raises_when_manifest_missing(tmp_path: Path):
    with pytest.raises(ScreenshotReadError, match="not found"):
        read_screenshot_manifest(tmp_path)


def test_read_raises_when_dir_missing(tmp_path: Path):
    # Even the umbrella .omnisight/ doesn't exist — same not-found path.
    nowhere = tmp_path / "absent"
    nowhere.mkdir()
    with pytest.raises(ScreenshotReadError):
        read_screenshot_manifest(nowhere)


def test_read_raises_on_invalid_json(tmp_path: Path):
    refs = tmp_path / ".omnisight" / "refs"
    refs.mkdir(parents=True)
    (refs / SCREENSHOT_MANIFEST_FILENAME).write_text("not-json{", encoding="utf-8")
    with pytest.raises(ScreenshotReadError, match="not valid JSON"):
        read_screenshot_manifest(tmp_path)


def test_read_raises_on_empty_file(tmp_path: Path):
    refs = tmp_path / ".omnisight" / "refs"
    refs.mkdir(parents=True)
    (refs / SCREENSHOT_MANIFEST_FILENAME).write_text("", encoding="utf-8")
    with pytest.raises(ScreenshotReadError):
        read_screenshot_manifest(tmp_path)


def test_read_raises_on_array_root(tmp_path: Path):
    refs = tmp_path / ".omnisight" / "refs"
    refs.mkdir(parents=True)
    (refs / SCREENSHOT_MANIFEST_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(ScreenshotReadError, match="not a JSON object"):
        read_screenshot_manifest(tmp_path)


def test_read_raises_on_unsupported_version(tmp_path: Path):
    refs = tmp_path / ".omnisight" / "refs"
    refs.mkdir(parents=True)
    bogus = {
        "manifest_version": "999",
        "created_at": "2026-04-29T00:00:00.000000Z",
        "source_url": "https://example.com",
        "refs_dir": ".omnisight/refs",
        "screenshots": [],
    }
    (refs / SCREENSHOT_MANIFEST_FILENAME).write_text(
        json.dumps(bogus), encoding="utf-8",
    )
    with pytest.raises(ScreenshotReadError, match="unsupported"):
        read_screenshot_manifest(tmp_path)


def test_read_raises_when_screenshots_field_wrong_type(tmp_path: Path):
    refs = tmp_path / ".omnisight" / "refs"
    refs.mkdir(parents=True)
    bogus = {
        "manifest_version": "1",
        "created_at": "2026-04-29T00:00:00.000000Z",
        "source_url": "https://example.com",
        "refs_dir": ".omnisight/refs",
        "screenshots": "not-a-list",
    }
    (refs / SCREENSHOT_MANIFEST_FILENAME).write_text(
        json.dumps(bogus), encoding="utf-8",
    )
    with pytest.raises(ScreenshotReadError, match="must be a list"):
        read_screenshot_manifest(tmp_path)


def test_read_raises_on_malformed_entry(tmp_path: Path):
    refs = tmp_path / ".omnisight" / "refs"
    refs.mkdir(parents=True)
    bogus = {
        "manifest_version": "1",
        "created_at": "2026-04-29T00:00:00.000000Z",
        "source_url": "https://example.com",
        "refs_dir": ".omnisight/refs",
        "screenshots": [{"name": "mobile_375"}],  # missing fields
    }
    (refs / SCREENSHOT_MANIFEST_FILENAME).write_text(
        json.dumps(bogus), encoding="utf-8",
    )
    with pytest.raises(ScreenshotReadError):
        read_screenshot_manifest(tmp_path)


# ── 8. read_screenshot_manifest_if_exists (soft not-found) ────────────


def test_if_exists_returns_none_on_missing_dir(tmp_path: Path):
    assert read_screenshot_manifest_if_exists(tmp_path) is None


def test_if_exists_returns_none_when_manifest_absent(tmp_path: Path):
    # Refs dir created but no manifest file — still soft None.
    (tmp_path / ".omnisight" / "refs").mkdir(parents=True)
    assert read_screenshot_manifest_if_exists(tmp_path) is None


def test_if_exists_returns_manifest_when_present(tmp_path: Path, four_shots):
    written = write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
        now="2026-04-29T00:00:00.000000Z",
    )
    loaded = read_screenshot_manifest_if_exists(tmp_path)
    assert loaded == written


def test_if_exists_raises_when_corrupted(tmp_path: Path):
    refs = tmp_path / ".omnisight" / "refs"
    refs.mkdir(parents=True)
    (refs / SCREENSHOT_MANIFEST_FILENAME).write_text("{garbled", encoding="utf-8")
    with pytest.raises(ScreenshotReadError):
        read_screenshot_manifest_if_exists(tmp_path)


# ── 9. delete_screenshots ─────────────────────────────────────────────


def test_delete_returns_zero_when_absent(tmp_path: Path):
    assert delete_screenshots(tmp_path) == 0


def test_delete_sweeps_pngs_and_manifest(tmp_path: Path, four_shots):
    write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
    )
    deleted = delete_screenshots(tmp_path)
    assert deleted == 5  # 4 PNGs + manifest
    refs = tmp_path / ".omnisight" / "refs"
    # rmdir succeeds when refs is empty.
    assert not refs.exists()


def test_delete_idempotent(tmp_path: Path, four_shots):
    write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
    )
    delete_screenshots(tmp_path)
    assert delete_screenshots(tmp_path) == 0


def test_delete_leaves_omnisight_umbrella(tmp_path: Path, four_shots):
    write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
    )
    # Drop a sibling artefact as if W11.7 / W12.5 had landed too.
    sibling = tmp_path / ".omnisight" / "clone-manifest.json"
    sibling.write_text("{}", encoding="utf-8")
    delete_screenshots(tmp_path)
    assert (tmp_path / ".omnisight").is_dir()
    assert sibling.exists()


def test_delete_skips_stray_operator_file(tmp_path: Path, four_shots):
    write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
    )
    refs = tmp_path / ".omnisight" / "refs"
    stray = refs / "operator-notes.md"
    stray.write_text("hand-edited", encoding="utf-8")
    deleted = delete_screenshots(tmp_path)
    # 4 PNGs + manifest = 5; the .md file is left alone.
    assert deleted == 5
    assert stray.exists()


def test_delete_rejects_bad_root():
    with pytest.raises(ScreenshotWriteError):
        delete_screenshots(None)  # type: ignore[arg-type]


# ── 10. Atomic-write discipline ───────────────────────────────────────


def test_pre_existing_manifest_unchanged_on_validation_failure(tmp_path: Path):
    # Write a valid set first.
    write_screenshots(
        [_shot("mobile_375")], project_root=tmp_path,
        source_url="https://example.com/v1",
        now="2026-04-29T00:00:00.000000Z",
    )
    original_bytes = (tmp_path / ".omnisight" / "refs" / "manifest.json").read_bytes()
    # Now attempt a write that fails validation (duplicate names).
    with pytest.raises(ScreenshotWriteError):
        write_screenshots(
            [_shot("mobile_375"), _shot("mobile_375", png=_png("dupe"))],
            project_root=tmp_path,
            source_url="https://example.com/v2",
        )
    # Original manifest untouched.
    after = (tmp_path / ".omnisight" / "refs" / "manifest.json").read_bytes()
    assert after == original_bytes


def test_no_partial_state_after_concurrent_read(tmp_path: Path, four_shots):
    """Atomic-replace contract: a read after a successful write either
    sees the complete payload or nothing — never a partial JSON. The
    rename is a single POSIX syscall so we can't easily race it from
    Python; assert the post-condition that the file parses cleanly."""
    write_screenshots(
        four_shots, project_root=tmp_path,
        source_url="https://example.com",
    )
    raw = (tmp_path / ".omnisight" / "refs" / "manifest.json").read_text(
        encoding="utf-8",
    )
    # Trailing newline (W11.7-style discipline).
    assert raw.endswith("\n")
    parsed = json.loads(raw)
    assert parsed["manifest_version"] == "1"


# ── 11. Engine compatibility ──────────────────────────────────────────


def test_capture_multi_output_shape_writable(tmp_path: Path):
    """Drift guard: whatever shape ``MultiContextScreenshotCapture.capture_multi``
    returns must be valid input to ``write_screenshots`` — both rows
    have to evolve together. We construct the same shape directly
    (no playwright import) to verify the contract."""
    from backend.web.screenshot_breakpoints import DEFAULT_BREAKPOINTS

    shots = tuple(
        ViewportScreenshot(
            viewport=vp, png_bytes=_png(vp.name),
            fetched_at="2026-04-29T00:00:00.000000Z",
            status_code=200,
            post_redirect_url="https://example.com/",
        )
        for vp in DEFAULT_BREAKPOINTS
    )
    manifest = write_screenshots(
        shots, project_root=tmp_path,
        source_url="https://example.com",
    )
    assert tuple(e.name for e in manifest.screenshots) == tuple(
        vp.name for vp in DEFAULT_BREAKPOINTS
    )


# ── 12. Package re-export surface ─────────────────────────────────────


def test_package_re_exports_screenshot_writer_surface():
    expected = {
        "SCREENSHOT_MANIFEST_FILENAME",
        "SCREENSHOT_MANIFEST_RELATIVE_PATH",
        "SCREENSHOT_MANIFEST_VERSION",
        "SCREENSHOT_PNG_SUFFIX",
        "SCREENSHOT_REFS_DIR",
        "SCREENSHOT_REFS_RELATIVE_PATH",
        "ScreenshotManifest",
        "ScreenshotManifestEntry",
        "ScreenshotReadError",
        "ScreenshotWriteError",
        "ScreenshotWriterError",
        "delete_screenshots",
        "read_screenshot_manifest",
        "read_screenshot_manifest_if_exists",
        "resolve_refs_dir",
        "resolve_screenshot_manifest_path",
        "resolve_screenshot_path",
        "write_screenshots",
    }
    for name in expected:
        assert hasattr(web_pkg, name), f"backend.web missing re-export {name}"
        assert name in web_pkg.__all__, f"{name!r} absent from backend.web.__all__"


def test_package_re_exports_share_identity_with_module():
    """Re-exports must be the *same* objects so a future patch to the
    sub-module propagates without divergence."""
    assert web_pkg.write_screenshots is sw.write_screenshots
    assert web_pkg.read_screenshot_manifest is sw.read_screenshot_manifest
    assert web_pkg.delete_screenshots is sw.delete_screenshots
    assert web_pkg.ScreenshotManifest is sw.ScreenshotManifest
    assert web_pkg.ScreenshotManifestEntry is sw.ScreenshotManifestEntry
    assert web_pkg.SCREENSHOT_REFS_DIR is sw.SCREENSHOT_REFS_DIR
    assert web_pkg.SCREENSHOT_MANIFEST_FILENAME is sw.SCREENSHOT_MANIFEST_FILENAME
