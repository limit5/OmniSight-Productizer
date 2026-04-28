"""W13.3 #XXX — On-disk writer for the multi-breakpoint screenshot set.

Pins the captured set produced by W13.1's
:class:`backend.web.screenshot_capture.MultiContextScreenshotCapture`
to ``<project_root>/.omnisight/refs/{breakpoint}.png`` plus a
``manifest.json`` sidecar that lists every captured viewport with its
geometry, capture metadata, byte-size and per-PNG ``sha256:`` digest.
The manifest is the single authoritative record any downstream stage
(W13.4 ghost-overlay diff, W13.5 5-URL × 4-breakpoint reference matrix,
future W14 live-preview ghost) reads to understand "what does this site
look like at each breakpoint right now".

Why this row, why now
---------------------
W13.1 deliberately stopped at returning :class:`ViewportScreenshot`
records in memory — the engine docstring carries the receipt::

    W13.3 — writing PNGs to ``.omnisight/refs/{breakpoint}.png`` plus
    the ``manifest.json`` sidecar. This module returns bytes; the disk
    contract lives in the W13.3 writer alongside W11.7's
    ``write_manifest_file`` patterns.

W13.2 then locked the four production breakpoints + the resolver but
**still** kept the engine reusable from a router that wants to return
base64-encoded screenshots over HTTP without ever touching local FS.

This row threads the bytes-to-disk contract: a writer that turns the
:class:`ViewportScreenshot` tuple into a deterministic on-disk record
mirroring the W11.7 ``write_manifest_file`` discipline (atomic write
via tempfile + ``os.replace``, canonical JSON via ``sort_keys=True`` +
``ensure_ascii=False`` + trailing newline, project-relative paths
pinned as constants for grep / audit / CI guards).

On-disk shape
-------------

::

    <project_root>/
    └── .omnisight/
        └── refs/
            ├── manifest.json
            ├── mobile_375.png
            ├── tablet_768.png
            ├── desktop_1440.png
            └── desktop_1920.png

``.omnisight/`` is the canonical OmniSight per-project metadata bucket
(already used by W11.7's ``clone-manifest.json`` and W12.5's
``brand.json``). The framework adapters' ``.gitignore`` already excludes
the umbrella, so reference screenshots travel out-of-band of the
generated source tree.

Manifest schema (v1)
--------------------

::

    {
      "manifest_version": "1",
      "created_at": "2026-04-29T00:00:00.000000Z",
      "source_url": "https://acme.example/landing",
      "refs_dir": ".omnisight/refs",
      "screenshots": [
        {
          "name": "mobile_375",
          "width": 375,
          "height": 812,
          "device_scale_factor": 1.0,
          "is_mobile": false,
          "filename": "mobile_375.png",
          "relative_path": ".omnisight/refs/mobile_375.png",
          "byte_size": 12345,
          "sha256": "sha256:<hex>",
          "fetched_at": "2026-04-29T00:00:00.000000Z",
          "status_code": 200,
          "post_redirect_url": "https://acme.example/landing"
        },
        ...
      ]
    }

What the manifest deliberately does **not** carry
-------------------------------------------------

* **Response headers** — W13.1's :class:`ViewportScreenshot` exposes a
  per-viewport ``headers`` dict but persisting them risks leaking
  ``set-cookie`` session tokens into a check-into-source artefact.
  W13.4 / W14 do not need headers for ghost-overlay diff. If a future
  row needs them (e.g. cache-policy diff), it lands behind an opt-in
  flag rather than retroactively spilling secrets.
* **PNG bytes** — the bytes live on disk in ``{name}.png``; embedding
  them base64 in the manifest would balloon a 5-breakpoint manifest from
  ~1 KB to ~50 MB and lose the "diff this PNG against that PNG" workflow.
* **Manifest hash** — W11.7's L4 traceability layer carries one because
  the manifest is the ground-truth tamper-evident record for an entire
  cloning operation. W13.3 records *reference screenshots* used by
  diff tooling — corrupted entries just trigger a re-capture, no
  forensic chain rebuild. We pin a per-PNG ``sha256:`` instead so a
  later "did this PNG change on disk" check still works.

Atomic-write contract
---------------------

Both PNGs and the manifest are written via the
``NamedTemporaryFile + fsync + os.replace`` discipline copied wholesale
from :mod:`backend.brand_store`. ``os.replace`` is a single POSIX
syscall: a concurrent reader sees the old file or the new file, never
a partial state. PNGs are written **before** the manifest so a
mid-call crash leaves either (a) no manifest + some PNGs (caller's
:func:`read_screenshot_manifest_if_exists` returns ``None`` — caller
re-runs cleanly) or (b) the previous run's manifest + the previous
run's PNGs (the in-flight PNG temp files are unlinked on failure).

Module-global state audit (SOP §1)
----------------------------------

This module owns no module-level **mutable** state. Constants
(:data:`SCREENSHOT_REFS_DIR` / :data:`SCREENSHOT_MANIFEST_FILENAME` /
:data:`SCREENSHOT_MANIFEST_VERSION` / :data:`SCREENSHOT_REFS_RELATIVE_PATH`
/ :data:`SCREENSHOT_MANIFEST_RELATIVE_PATH`) are immutable string
literals; cross-worker consistency is SOP answer #1 (each ``uvicorn``
worker derives the same constants from the same source).

Read-after-write timing audit (SOP §2)
--------------------------------------

The atomic ``os.replace`` rename is a single POSIX syscall — readers
either see the old file or the new file, never a partial state.
Concurrent writers race at the rename point; last writer wins, which
is the correct semantics for "the latest reference capture replaces
the older one". No DB read-after-write surface, no asyncio.gather race.

Compat-fingerprint grep (SOP §3)
--------------------------------

N/A — no DB code path.
``grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]"``
returns 0 hits in this module.

Scope (this row only)
---------------------

* Pin the on-disk shape (``.omnisight/refs/{name}.png`` + sibling
  ``manifest.json``).
* Atomic-write writer + strict reader + soft-not-found reader +
  delete-helper for re-capture / takedown flows.
* :class:`ScreenshotManifest` / :class:`ScreenshotManifestEntry`
  frozen dataclasses representing the on-disk schema.

Out of scope (future rows):

* W13.4 — ghost-overlay diff against the W14 live preview.
* W13.5 — 5-URL × 4-breakpoint integration matrix that pins the
  full pipeline.
* W14 — live-preview ghost overlay rendering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from backend.web.screenshot_capture import (
    ScreenshotCaptureError,
    Viewport,
    ViewportScreenshot,
)

logger = logging.getLogger(__name__)


# ── Public constants ──────────────────────────────────────────────────

#: Schema version of the on-disk manifest. Bumped when the manifest
#: shape changes in a non-backward-compatible way; readers reject
#: unknown versions to make a stale on-disk record fail loud rather
#: than silently mis-parse.
SCREENSHOT_MANIFEST_VERSION: str = "1"

#: Directory inside the project's ``.omnisight/`` umbrella where
#: reference screenshots live. The leading ``.omnisight/`` aligns with
#: :data:`backend.web.clone_manifest.MANIFEST_DIR` and
#: :data:`backend.brand_store.BRAND_STORE_DIR` so a single ``.gitignore``
#: rule (``.omnisight/``) covers every OmniSight per-project record.
SCREENSHOT_REFS_DIR: str = ".omnisight/refs"

#: Project-relative directory path. Same value as
#: :data:`SCREENSHOT_REFS_DIR`; pinned as a separate constant so call
#: sites that mean "directory path" vs "directory name" stay unambiguous
#: when this codebase later grows a multi-project layout.
SCREENSHOT_REFS_RELATIVE_PATH: str = SCREENSHOT_REFS_DIR

#: Filename of the manifest sidecar inside :data:`SCREENSHOT_REFS_DIR`.
SCREENSHOT_MANIFEST_FILENAME: str = "manifest.json"

#: Project-relative path of the manifest. Pinned as a constant so docs
#: / audit rows / CI guards reference one literal.
SCREENSHOT_MANIFEST_RELATIVE_PATH: str = (
    f"{SCREENSHOT_REFS_DIR}/{SCREENSHOT_MANIFEST_FILENAME}"
)

#: PNG file suffix. Pinned so a future "let operators choose JPEG"
#: row doesn't have to grep half a dozen string literals.
SCREENSHOT_PNG_SUFFIX: str = ".png"

#: ``sha256:<hex>`` prefix the per-PNG digest carries. Same shape as
#: W11.7's ``manifest_hash`` so a single regex parses both records.
SHA256_HASH_PREFIX: str = "sha256:"


# Type alias matching :mod:`backend.brand_store`'s ``_ProjectRoot``.
_ProjectRoot = Union[str, "os.PathLike[str]", Path]


# ── Errors ────────────────────────────────────────────────────────────


class ScreenshotWriterError(ScreenshotCaptureError):
    """Base error for everything raised by :mod:`backend.web.screenshot_writer`.

    Subclasses :class:`backend.web.screenshot_capture.ScreenshotCaptureError`
    so existing W13 chains that catch the parent type also catch us.
    """


class ScreenshotWriteError(ScreenshotWriterError):
    """The writer could not finalise the on-disk artefacts.

    Distinct from :class:`ScreenshotReadError` so an audit row can
    distinguish "filesystem fault during write" from "manifest on disk
    is corrupted". Underlying :class:`OSError` is chained via
    ``__cause__``.
    """


class ScreenshotReadError(ScreenshotWriterError):
    """The on-disk manifest could not be loaded or did not satisfy the
    schema. Covers four failure modes that all share the same caller
    response (re-capture):

    * file missing,
    * file unreadable (permission denied / disk error),
    * file present but not valid JSON,
    * file present + valid JSON but the payload does not satisfy
      :class:`ScreenshotManifest` reconstruction.
    """


# ── Data shapes ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScreenshotManifestEntry:
    """One row of the manifest — pins everything a downstream stage
    needs to address one breakpoint's PNG without re-reading the bytes.

    The ``relative_path`` field is the ``.omnisight/refs/{name}.png``
    string a future tool (W13.4 ghost overlay, CI guard) joins onto
    the project root to locate the PNG. ``sha256`` lets that tool
    detect "did this PNG change on disk since the manifest was written".
    """

    name: str
    width: int
    height: int
    device_scale_factor: float
    is_mobile: bool
    filename: str
    relative_path: str
    byte_size: int
    sha256: str
    fetched_at: str
    status_code: int
    post_redirect_url: str


@dataclass(frozen=True)
class ScreenshotManifest:
    """The on-disk ``manifest.json`` payload as a Python object.

    ``screenshots`` is an immutable tuple in the same order the W13.1
    engine returned the captures (which the W13.2 resolver pins to
    ``DEFAULT_BREAKPOINTS`` order, small-to-large width). Downstream
    diff stages iterate this order so the artefact is byte-stable
    across runs that pass the same viewport list.
    """

    manifest_version: str
    created_at: str
    source_url: str
    refs_dir: str
    screenshots: tuple[ScreenshotManifestEntry, ...] = field(default_factory=tuple)


# ── Path resolvers ────────────────────────────────────────────────────


def _coerce_project_root(project_root: _ProjectRoot) -> Path:
    """Normalise the operator-supplied ``project_root`` to an absolute
    :class:`Path`.

    ``str`` / :class:`Path` / :class:`os.PathLike` accepted; everything
    else (``None``, ``int``, ``dict``) raises :class:`ScreenshotWriteError`
    so a typo'd argument fails loud rather than producing a confusing
    ``TypeError`` deep inside :class:`Path`. Relative paths resolve
    against CWD so the manifest's ``relative_path`` field is always
    project-relative regardless of caller invocation directory.
    """
    if isinstance(project_root, Path):
        root = project_root
    elif isinstance(project_root, (str, os.PathLike)):
        root = Path(project_root)
    else:
        raise ScreenshotWriteError(
            "project_root must be str / Path / os.PathLike, "
            f"got {type(project_root).__name__}"
        )
    if not root.is_absolute():
        # ``resolve(strict=False)`` keeps us tolerant of a not-yet-created
        # project_root, matching :mod:`backend.brand_store`.
        root = root.resolve()
    return root


def resolve_refs_dir(project_root: _ProjectRoot) -> Path:
    """Return ``<project_root>/.omnisight/refs`` as an absolute path.

    Pure resolver: never touches the filesystem. The directory may not
    exist yet — :func:`write_screenshots` is responsible for creating
    it.
    """
    return _coerce_project_root(project_root) / SCREENSHOT_REFS_DIR


def resolve_screenshot_path(
    project_root: _ProjectRoot, breakpoint_name: str
) -> Path:
    """Return ``<project_root>/.omnisight/refs/{breakpoint_name}.png``.

    The breakpoint name must already satisfy the W13.1 :class:`Viewport`
    filename-safe alphabet (``[a-z0-9_-]+``); we re-validate here as a
    defence-in-depth so an operator who hand-builds a path can't sneak
    in ``../etc/passwd``. Empty / non-string / containing a path
    separator → :class:`ScreenshotWriteError`.
    """
    if not isinstance(breakpoint_name, str) or not breakpoint_name:
        raise ScreenshotWriteError(
            f"breakpoint_name must be a non-empty string, "
            f"got {type(breakpoint_name).__name__}"
        )
    if not all(
        (ch.islower() and ch.isascii()) or ch.isdigit() or ch in ("-", "_")
        for ch in breakpoint_name
    ):
        raise ScreenshotWriteError(
            f"breakpoint_name must match [a-z0-9_-]+, got {breakpoint_name!r}"
        )
    return resolve_refs_dir(project_root) / f"{breakpoint_name}{SCREENSHOT_PNG_SUFFIX}"


def resolve_screenshot_manifest_path(project_root: _ProjectRoot) -> Path:
    """Return ``<project_root>/.omnisight/refs/manifest.json``."""
    return resolve_refs_dir(project_root) / SCREENSHOT_MANIFEST_FILENAME


# ── Helpers ───────────────────────────────────────────────────────────


def _utc_iso8601_now() -> str:
    """ISO-8601 UTC timestamp with a ``Z`` suffix. Matches the format
    pinned by W11.2 / W11.7 / W13.1 so cross-row timestamp diffs aren't
    tripped by stringification drift."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256_of(data: bytes) -> str:
    """Return ``sha256:<hex>`` for ``data``. Mirrors W11.7's
    ``manifest_hash`` shape so a single regex parses both."""
    return f"{SHA256_HASH_PREFIX}{hashlib.sha256(data).hexdigest()}"


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write ``payload`` to ``target`` atomically.

    Mirrors :mod:`backend.brand_store`: write to a sibling temp file,
    fsync, then ``os.replace``. A reader either sees the old file (if
    any) or the complete new file — never a partial state. The temp
    file is cleaned up on any failure path.
    """
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                logger.warning(
                    "screenshot_writer: fsync failed for %s — "
                    "proceeding with rename", target,
                )
            tmp_path = Path(fh.name)
        os.replace(tmp_path, target)
        tmp_path = None
    except OSError as exc:
        raise ScreenshotWriteError(
            f"failed to atomically write {target}: {exc!s}"
        ) from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.warning(
                    "screenshot_writer: failed to unlink tempfile %s",
                    tmp_path,
                )


def _entry_to_dict(entry: ScreenshotManifestEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "width": int(entry.width),
        "height": int(entry.height),
        "device_scale_factor": float(entry.device_scale_factor),
        "is_mobile": bool(entry.is_mobile),
        "filename": entry.filename,
        "relative_path": entry.relative_path,
        "byte_size": int(entry.byte_size),
        "sha256": entry.sha256,
        "fetched_at": entry.fetched_at,
        "status_code": int(entry.status_code),
        "post_redirect_url": entry.post_redirect_url,
    }


def manifest_to_dict(manifest: ScreenshotManifest) -> dict[str, Any]:
    """Render a :class:`ScreenshotManifest` as the canonical dict that
    serialises 1:1 with :data:`SCREENSHOT_MANIFEST_FILENAME` on disk.

    Pure function. Useful for tests asserting on the wire format and
    for callers that want to embed the manifest in a larger JSON
    response without going through the file system.
    """
    if not isinstance(manifest, ScreenshotManifest):
        raise ScreenshotWriteError(
            f"manifest must be ScreenshotManifest, got {type(manifest).__name__}"
        )
    return {
        "manifest_version": manifest.manifest_version,
        "created_at": manifest.created_at,
        "source_url": manifest.source_url,
        "refs_dir": manifest.refs_dir,
        "screenshots": [_entry_to_dict(e) for e in manifest.screenshots],
    }


def serialize_manifest_json(
    manifest: ScreenshotManifest,
    *,
    indent: Optional[int] = 2,
) -> str:
    """Render ``manifest`` as a JSON string. ``indent=2`` (default) for
    diff-friendly disks; ``indent=None`` for compact embedding.
    Canonical: ``sort_keys=True`` + ``ensure_ascii=False`` so a manifest
    diff is byte-meaningful — same discipline as W11.7."""
    payload = manifest_to_dict(manifest)
    return json.dumps(
        payload, indent=indent, sort_keys=True, ensure_ascii=False,
    )


def _build_manifest_entry(
    screenshot: ViewportScreenshot,
) -> ScreenshotManifestEntry:
    """Derive a :class:`ScreenshotManifestEntry` from a captured
    :class:`ViewportScreenshot`. Pure function — used by the writer
    and by tests that want to inspect manifest shape without a writer
    round-trip."""
    if not isinstance(screenshot, ViewportScreenshot):
        raise ScreenshotWriteError(
            "screenshots must be ViewportScreenshot instances, "
            f"got {type(screenshot).__name__}"
        )
    vp = screenshot.viewport
    if not isinstance(vp, Viewport):
        raise ScreenshotWriteError(
            "screenshot.viewport must be Viewport, "
            f"got {type(vp).__name__}"
        )
    if not isinstance(screenshot.png_bytes, (bytes, bytearray)) or \
            not screenshot.png_bytes:
        raise ScreenshotWriteError(
            f"screenshot {vp.name!r} carries empty png_bytes"
        )
    filename = f"{vp.name}{SCREENSHOT_PNG_SUFFIX}"
    return ScreenshotManifestEntry(
        name=vp.name,
        width=int(vp.width),
        height=int(vp.height),
        device_scale_factor=float(vp.device_scale_factor),
        is_mobile=bool(vp.is_mobile),
        filename=filename,
        relative_path=f"{SCREENSHOT_REFS_DIR}/{filename}",
        byte_size=len(screenshot.png_bytes),
        sha256=_sha256_of(bytes(screenshot.png_bytes)),
        fetched_at=screenshot.fetched_at,
        status_code=int(screenshot.status_code),
        post_redirect_url=screenshot.post_redirect_url,
    )


# ── Writer ────────────────────────────────────────────────────────────


def write_screenshots(
    screenshots: Sequence[ViewportScreenshot],
    *,
    project_root: _ProjectRoot,
    source_url: str,
    indent: Optional[int] = 2,
    now: Optional[str] = None,
) -> ScreenshotManifest:
    """Pin a multi-breakpoint screenshot set to disk.

    Writes one ``.omnisight/refs/{viewport.name}.png`` per screenshot
    plus the sibling ``manifest.json``. Both files are written
    atomically (tempfile + fsync + ``os.replace``). PNGs are written
    **before** the manifest so a mid-call crash leaves the previous
    run's manifest untouched (the partial PNGs that did land are
    overwritten on the next run).

    Args:
        screenshots: Ordered sequence produced by W13.1's
            :meth:`MultiContextScreenshotCapture.capture_multi`. Must be
            non-empty; every entry must be a :class:`ViewportScreenshot`
            with non-empty ``png_bytes``; viewport names must be unique
            (defence-in-depth — the W13.1 engine already enforces this
            but the writer's filename-collision risk is severe enough
            to re-check).
        project_root: Project root directory. The ``.omnisight/refs/``
            subdirectory is created on demand. ``str`` / :class:`Path`
            / :class:`os.PathLike` accepted.
        source_url: The URL the screenshots were captured from. Pinned
            into the manifest so a future tool can answer "which URL
            did this reference set come from" without consulting the
            calling site. Required — pass empty string explicitly if
            the caller really has no URL to record (rare).
        indent: JSON indent for the manifest. Default ``2`` produces a
            human-readable, diff-friendly file. ``None`` for compact.
        now: Optional ISO-8601 timestamp override for the manifest's
            ``created_at`` field. Tests inject this for determinism.
            Production callers leave it ``None`` and the writer reads
            UTC clock at call time.

    Returns:
        The :class:`ScreenshotManifest` that was just written. Callers
        that need the manifest path can derive it via
        :func:`resolve_screenshot_manifest_path`.

    Raises:
        ScreenshotWriteError: malformed input (empty list, duplicate
            names, non-:class:`ViewportScreenshot` entry, non-string
            ``source_url``) or any underlying filesystem error
            (``mkdir`` denied, disk full, etc.). The original
            :class:`OSError` (when applicable) is chained via
            ``__cause__``.
    """
    if not isinstance(source_url, str):
        raise ScreenshotWriteError(
            f"source_url must be str, got {type(source_url).__name__}"
        )
    if not screenshots:
        raise ScreenshotWriteError(
            "screenshots must be a non-empty sequence"
        )
    # Build entries up-front so a malformed payload fails before we
    # mutate the filesystem.
    entries: list[ScreenshotManifestEntry] = []
    seen_names: set[str] = set()
    for shot in screenshots:
        entry = _build_manifest_entry(shot)
        if entry.name in seen_names:
            raise ScreenshotWriteError(
                f"viewport names must be unique on disk, "
                f"duplicate: {entry.name!r}"
            )
        seen_names.add(entry.name)
        entries.append(entry)

    refs_dir = resolve_refs_dir(project_root)
    try:
        refs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScreenshotWriteError(
            f"failed to create refs directory at {refs_dir}: {exc!s}"
        ) from exc

    # Write PNGs first. If any fails, raise — the previous run's
    # manifest (if any) still references the previous-run PNGs that
    # may now be partially overwritten; the caller's response is to
    # re-run, which atomically replaces every PNG + the manifest.
    for shot, entry in zip(screenshots, entries):
        png_target = refs_dir / entry.filename
        _atomic_write_bytes(png_target, bytes(shot.png_bytes))

    # Manifest goes last so an in-flight failure leaves a manifest
    # that points at the previous run's PNGs (or no manifest at all).
    manifest = ScreenshotManifest(
        manifest_version=SCREENSHOT_MANIFEST_VERSION,
        created_at=now if now is not None else _utc_iso8601_now(),
        source_url=source_url,
        refs_dir=SCREENSHOT_REFS_DIR,
        screenshots=tuple(entries),
    )
    manifest_path = refs_dir / SCREENSHOT_MANIFEST_FILENAME
    payload = (serialize_manifest_json(manifest, indent=indent) + "\n").encode("utf-8")
    _atomic_write_bytes(manifest_path, payload)

    logger.info(
        "screenshot_writer: wrote %d screenshot(s) + manifest to %s",
        len(entries), refs_dir,
    )
    return manifest


# ── Readers ───────────────────────────────────────────────────────────


def _entry_from_dict(payload: Mapping[str, Any]) -> ScreenshotManifestEntry:
    if not isinstance(payload, Mapping):
        raise ScreenshotReadError(
            f"manifest entry is not a mapping (got {type(payload).__name__})"
        )
    try:
        return ScreenshotManifestEntry(
            name=str(payload["name"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
            device_scale_factor=float(payload["device_scale_factor"]),
            is_mobile=bool(payload["is_mobile"]),
            filename=str(payload["filename"]),
            relative_path=str(payload["relative_path"]),
            byte_size=int(payload["byte_size"]),
            sha256=str(payload["sha256"]),
            fetched_at=str(payload["fetched_at"]),
            status_code=int(payload["status_code"]),
            post_redirect_url=str(payload["post_redirect_url"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScreenshotReadError(
            f"manifest entry missing or malformed field: {exc!s}"
        ) from exc


def manifest_from_dict(payload: Mapping[str, Any]) -> ScreenshotManifest:
    """Inverse of :func:`manifest_to_dict`.

    Strict — rejects unknown ``manifest_version`` so a stale on-disk
    record fails loud rather than silently mis-parsing under a future
    schema bump.
    """
    if not isinstance(payload, Mapping):
        raise ScreenshotReadError(
            f"manifest payload must be a mapping, got {type(payload).__name__}"
        )
    version = payload.get("manifest_version")
    if version != SCREENSHOT_MANIFEST_VERSION:
        raise ScreenshotReadError(
            f"manifest_version {version!r} unsupported "
            f"(expected {SCREENSHOT_MANIFEST_VERSION!r})"
        )
    raw_screenshots = payload.get("screenshots")
    if not isinstance(raw_screenshots, list):
        raise ScreenshotReadError(
            "manifest 'screenshots' field must be a list, "
            f"got {type(raw_screenshots).__name__}"
        )
    entries = tuple(_entry_from_dict(e) for e in raw_screenshots)
    try:
        return ScreenshotManifest(
            manifest_version=str(payload["manifest_version"]),
            created_at=str(payload["created_at"]),
            source_url=str(payload["source_url"]),
            refs_dir=str(payload.get("refs_dir") or SCREENSHOT_REFS_DIR),
            screenshots=entries,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScreenshotReadError(
            f"manifest missing or malformed field: {exc!s}"
        ) from exc


def read_screenshot_manifest(project_root: _ProjectRoot) -> ScreenshotManifest:
    """Strict load of ``<project_root>/.omnisight/refs/manifest.json``.

    Use :func:`read_screenshot_manifest_if_exists` for the soft-not-
    found variant.

    Raises:
        ScreenshotReadError: file missing / unreadable / not valid JSON
            / does not satisfy :class:`ScreenshotManifest` shape /
            unsupported ``manifest_version``.
    """
    target = resolve_screenshot_manifest_path(project_root)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ScreenshotReadError(
            f"screenshot manifest not found at {target}"
        ) from exc
    except OSError as exc:
        raise ScreenshotReadError(
            f"failed to read screenshot manifest at {target}: {exc!s}"
        ) from exc
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ScreenshotReadError(
            f"screenshot manifest at {target} is not valid JSON: {exc!s}"
        ) from exc
    if not isinstance(payload, dict):
        raise ScreenshotReadError(
            f"screenshot manifest at {target} is not a JSON object"
        )
    return manifest_from_dict(payload)


def read_screenshot_manifest_if_exists(
    project_root: _ProjectRoot,
) -> Optional[ScreenshotManifest]:
    """Return the persisted manifest or ``None`` if the file is absent.

    Distinct from :func:`read_screenshot_manifest` only on the
    file-missing case: if the file exists but is unparseable / wrong
    shape, this still raises :class:`ScreenshotReadError` so a
    hand-edit gone wrong fails loud rather than silently degrading
    to "no reference set".
    """
    target = resolve_screenshot_manifest_path(project_root)
    if not target.exists():
        return None
    return read_screenshot_manifest(project_root)


# ── Delete helper ─────────────────────────────────────────────────────


def delete_screenshots(project_root: _ProjectRoot) -> int:
    """Remove every ``.omnisight/refs/{name}.png`` referenced by the
    on-disk manifest plus the manifest itself.

    Returns the number of files actually deleted (0 when the directory
    did not exist; ≥ 1 otherwise). Idempotent — calling twice in a row
    returns 0 on the second call. Leaves the ``.omnisight/`` umbrella
    intact (sibling W11.7 / W12.5 records may share it). Removes the
    ``.omnisight/refs/`` subdirectory if it ends up empty after the
    sweep — keeping behaviour symmetrical with "scaffold creates dir
    on first write".

    Raises:
        ScreenshotWriteError: any underlying filesystem call failed
            (permission denied, etc.).
    """
    refs_dir = resolve_refs_dir(project_root)
    if not refs_dir.exists():
        return 0
    deleted = 0
    try:
        for child in sorted(refs_dir.iterdir()):
            # Only sweep PNG payloads + the manifest — avoid wiping a
            # stray operator note someone dropped here.
            if child.is_file() and (
                child.suffix == SCREENSHOT_PNG_SUFFIX
                or child.name == SCREENSHOT_MANIFEST_FILENAME
            ):
                try:
                    child.unlink()
                except FileNotFoundError:
                    continue
                deleted += 1
        # Best-effort dir cleanup; leave the umbrella alone.
        try:
            refs_dir.rmdir()
        except OSError:
            # Dir not empty (operator artefact) or permission denied —
            # both are non-fatal: the deletion contract is "PNGs +
            # manifest gone", not "directory pristine".
            pass
    except OSError as exc:
        raise ScreenshotWriteError(
            f"failed to delete screenshot refs at {refs_dir}: {exc!s}"
        ) from exc
    return deleted


__all__ = [
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
]
