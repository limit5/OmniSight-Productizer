"""W12.5 — Persist :class:`backend.brand_spec.BrandSpec` to ``.omnisight/brand.json``.

The W12 *Brand Style Extraction from URL* epic adds a reverse-mode
brand-style pipeline on top of the B5 forward-mode validator:

* W12.1 — :class:`backend.brand_spec.BrandSpec` 5-dim fingerprint type.
* W12.2 — :func:`backend.brand_extractor.extract_brand_from_url` —
  k-means / parser pipeline.
* W12.3 — :mod:`backend.brand_canonical` — shared canonicalisation
  primitives between forward- and reverse-mode.
* W12.4 — :mod:`backend.scaffold_reference` — argparse helper +
  resolver façade for the ``--reference-url`` flag.
* **W12.5 (this module)** — pin the resolved spec to
  ``<project_root>/.omnisight/brand.json`` so downstream agent edits
  read a single canonical record instead of re-fetching the reference
  URL every invocation.
* W12.6 — 8 reference URL × 5 dim test matrix.

Why a dedicated store module
----------------------------

The 12 per-stack scaffolders (``backend/<stack>_scaffolder.py``) and
their individual ``ScaffoldOptions`` dataclasses do not carry a
``BrandSpec`` field — see :mod:`backend.scaffold_reference` for the
"why not duplicate the field 12 times" rationale.  W12.4 therefore
chose a **side-channel** for the resolved spec: the operator passes
``--reference-url URL`` once, the resolver builds a :class:`BrandSpec`,
and W12.5 writes that spec to a stable on-disk location any later
agent edit (text rewrite, image placeholder, design-token loader)
can read with a one-line import.

Why ``.omnisight/brand.json`` specifically:

* The ``.omnisight/`` directory is **already** the canonical OmniSight
  per-project metadata bucket — see ``.omnisight/clone-manifest.json``
  (W11.7), ``.omnisight/platform`` (T1 platform hint), and the
  framework-adapter ``.gitignore`` entry that already excludes it from
  generated repositories.
* JSON not YAML — the same canonicalisation discipline as W11's clone
  manifest (``sort_keys=True`` + ``ensure_ascii=False`` + trailing
  newline) so a brand-spec diff is byte-meaningful across runs and
  CI guards can hash the file deterministically.
* One file per project — multi-brand workspaces are out of scope; if
  W13 ever introduces multi-brand variants, that lands as a new
  ``.omnisight/brand-<name>.json`` family with W12.5 staying the
  single-brand default.

Sibling pattern from :mod:`backend.web.clone_manifest`
-----------------------------------------------------

This module mirrors the four W11.7 entry points (``write_manifest_file``
/ ``read_manifest_file`` / ``_resolve_manifest_path`` / typed write +
schema errors) but stays narrow on the BrandSpec shape:

* :func:`resolve_brand_store_path` — pure ``project_root → Path``
  resolver.  Same shape as :func:`backend.web.clone_manifest._resolve_manifest_path`.
* :func:`write_brand_spec` — atomic write (``tmp`` + ``os.replace``)
  so a concurrent reader never sees a half-written file.  Creates the
  ``.omnisight/`` directory if missing.
* :func:`read_brand_spec` — strict load, raises on missing file /
  unparseable JSON / schema-version mismatch.
* :func:`read_brand_spec_if_exists` — best-effort load for downstream
  agents that treat "no brand.json" as "no override".  Returns
  ``None`` when the file is absent; raises on corrupted content (a
  hand-edit gone wrong should fail loud, not silently fall back).
* :func:`delete_brand_spec` — for tests, takedown handlers, and
  re-scaffold flows.

Atomic-write contract
---------------------

``write_brand_spec`` writes through a sibling temp file and renames
on success.  This avoids two failure modes:

1. **Half-written file under concurrent read** — without the rename,
   a reader running while the writer is mid-``write_text`` would see
   truncated JSON and ``json.JSONDecodeError``.
2. **Disk-full leaving an empty file** — without the rename, an
   ``OSError`` mid-write would leave a 0-byte ``brand.json`` on disk
   that ``read_brand_spec`` would treat as a corrupted spec rather
   than absence.  With ``os.replace``, the original file (if any)
   stays intact and the temp file is cleaned up on failure.

Module-global state audit (SOP §1)
----------------------------------

This module has **no mutable module-level state**.  Only immutable
constants: ``BRAND_STORE_DIR`` / ``BRAND_STORE_FILENAME`` /
``BRAND_STORE_RELATIVE_PATH`` (string literals) plus the standard
module-level ``logger``.  Cross-worker consistency: SOP answer #1
— each ``uvicorn`` worker derives identical constants from identical
source.

Read-after-write timing audit (SOP §2)
--------------------------------------

The atomic ``os.replace`` rename is a single POSIX syscall: a reader
either sees the old file or the new file, never a partial state.
Concurrent writers race at the rename point — last writer wins,
which is the correct semantics for "the latest scaffold's brand
fingerprint replaces any older one".

Compat-fingerprint grep (SOP §3)
--------------------------------

N/A — no DB code path.  ``grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]"``
returns 0 hits in this module.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Union

from backend.brand_spec import (
    BRAND_SPEC_SCHEMA_VERSION,
    BrandSpec,
    BrandSpecError,
    spec_to_json,
)

logger = logging.getLogger(__name__)


__all__ = [
    "BRAND_STORE_DIR",
    "BRAND_STORE_FILENAME",
    "BRAND_STORE_RELATIVE_PATH",
    "BrandStoreError",
    "BrandStoreReadError",
    "BrandStoreWriteError",
    "delete_brand_spec",
    "read_brand_spec",
    "read_brand_spec_if_exists",
    "resolve_brand_store_path",
    "write_brand_spec",
]


#: Directory inside the generated project where the brand spec lives.
#: Aligned with :data:`backend.web.clone_manifest.MANIFEST_DIR` so a
#: single ``.gitignore`` rule (``.omnisight/``) covers both records.
BRAND_STORE_DIR: str = ".omnisight"

#: Filename inside :data:`BRAND_STORE_DIR`.  Together with the dir,
#: the relative path is ``.omnisight/brand.json``.
BRAND_STORE_FILENAME: str = "brand.json"

#: Project-relative path of the brand spec file.  Pinned as a constant
#: so docs / audit rows / CI guards reference one literal.
BRAND_STORE_RELATIVE_PATH: str = f"{BRAND_STORE_DIR}/{BRAND_STORE_FILENAME}"


# Type alias: callers commonly pass either a ``str`` or ``Path``.
_ProjectRoot = Union[str, "os.PathLike[str]", Path]


# ── Errors ──────────────────────────────────────────────────────────


class BrandStoreError(BrandSpecError):
    """Base error for everything raised by :mod:`backend.brand_store`.

    Subclass of :class:`BrandSpecError` so existing chains that already
    catch ``BrandSpecError`` (the W12.1 spec module) catch us
    transparently — both are :class:`ValueError`.  Callers that need
    finer granularity can ``except BrandStoreError`` (or one of the
    two subtypes below).
    """


class BrandStoreReadError(BrandStoreError):
    """Raised when the on-disk brand spec cannot be loaded.

    Covers four distinct failure modes that all share the same caller
    response (re-extract from ``--reference-url`` or fall back to
    project tokens):

    * file missing,
    * file unreadable (permission denied / disk error),
    * file present but not valid JSON,
    * file present + valid JSON but the payload does not satisfy
      :meth:`BrandSpec.from_dict` (wrong shape / wrong schema version).
    """


class BrandStoreWriteError(BrandStoreError):
    """Raised when the on-disk write cannot complete.

    Distinct from :class:`BrandStoreReadError` so an audit row can
    distinguish "operator engineering fault" (filesystem) from
    "calling code fault" (wrong argument type).  Filesystem errors
    (``mkdir`` denied, disk full, target path is a directory) wrap
    the underlying :class:`OSError` via ``__cause__``.
    """


# ── Path resolver ───────────────────────────────────────────────────


def resolve_brand_store_path(project_root: _ProjectRoot) -> Path:
    """Return the absolute :class:`Path` of the brand-spec file.

    Parameters
    ----------
    project_root : str | os.PathLike
        The project root directory.  Relative paths are resolved
        against the caller's CWD (mirrors :func:`backend.web.clone_manifest._resolve_manifest_path`)
        so an audit row records an absolute location regardless of
        invocation context.

    Returns
    -------
    Path
        ``<project_root>/.omnisight/brand.json`` as an absolute path.
        The path may not exist on disk yet — :func:`write_brand_spec`
        is responsible for creating the directory.

    Raises
    ------
    BrandStoreWriteError
        If ``project_root`` is not a ``str`` / ``os.PathLike`` / ``Path``
        — same fail-loud discipline as the W11.7 manifest resolver.
        ``None`` and other non-path types raise here rather than
        producing a ``TypeError`` later inside :class:`Path`.
    """
    if isinstance(project_root, Path):
        root = project_root
    elif isinstance(project_root, (str, os.PathLike)):
        root = Path(project_root)
    else:
        raise BrandStoreWriteError(
            "project_root must be str / Path / os.PathLike, "
            f"got {type(project_root).__name__}"
        )
    if not root.is_absolute():
        # Resolve relative paths against the caller's CWD.  Use
        # ``resolve(strict=False)`` so a not-yet-created project_root
        # (e.g. a fresh scaffold target) still resolves cleanly.
        root = root.resolve()
    return root / BRAND_STORE_DIR / BRAND_STORE_FILENAME


# ── Writer ──────────────────────────────────────────────────────────


def write_brand_spec(
    spec: BrandSpec,
    *,
    project_root: _ProjectRoot,
    indent: int | None = 2,
) -> Path:
    """Atomically write ``spec`` to ``<project_root>/.omnisight/brand.json``.

    Atomic semantics: the JSON is first written to a sibling temp
    file in the same directory, then renamed into place via
    :func:`os.replace`.  A concurrent reader sees either the old file
    (if any) or the new file — never a partial state.

    Creates the ``.omnisight/`` directory if missing.  Overwrites any
    existing ``brand.json`` (the latest extraction is authoritative).

    Parameters
    ----------
    spec : BrandSpec
        The :class:`BrandSpec` to persist.  Anything else raises
        :class:`BrandStoreWriteError`.
    project_root : str | os.PathLike
        Project root directory.  The ``.omnisight/`` subdirectory is
        created on demand.
    indent : int | None, optional
        JSON indent passed through to :func:`spec_to_json`.  Defaults
        to ``2`` for human-readable diffs; pass ``None`` for the most
        compact form.

    Returns
    -------
    Path
        Absolute path of the written file.

    Raises
    ------
    BrandStoreWriteError
        If ``spec`` is not a :class:`BrandSpec`, if ``project_root``
        cannot be created / written, or if any underlying filesystem
        call fails.  The original :class:`OSError` (when applicable)
        is chained via ``__cause__``.
    """
    if not isinstance(spec, BrandSpec):
        raise BrandStoreWriteError(
            f"spec must be BrandSpec, got {type(spec).__name__}"
        )
    target = resolve_brand_store_path(project_root)
    payload = spec_to_json(spec, indent=indent) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrandStoreWriteError(
            f"failed to create brand-store directory at {target.parent}: {exc!s}"
        ) from exc

    tmp_path: Path | None = None
    try:
        # ``delete=False`` so we can rename it; cleaned up explicitly
        # on success and on every failure branch below.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{BRAND_STORE_FILENAME}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # ``fsync`` failure is non-fatal here — best-effort
                # durability.  The atomic-rename guarantee still
                # holds.  Log so an operator can spot disk problems.
                logger.warning(
                    "brand_store: fsync failed for %s — proceeding with rename",
                    target,
                )
            tmp_path = Path(fh.name)
        os.replace(tmp_path, target)
        tmp_path = None  # rename succeeded, do not unlink in finally
    except OSError as exc:
        raise BrandStoreWriteError(
            f"failed to write brand spec at {target}: {exc!s}"
        ) from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                # Cleaning up a tempfile is best-effort — leave the
                # ``.tmp`` artefact rather than crashing.
                logger.warning(
                    "brand_store: failed to unlink tempfile %s", tmp_path
                )
    logger.info(
        "brand_store: wrote BrandSpec to %s "
        "(palette=%d fonts=%d spacing=%d radius=%d empty=%s)",
        target,
        len(spec.palette),
        len(spec.fonts),
        len(spec.spacing),
        len(spec.radius),
        spec.is_empty,
    )
    return target


# ── Readers ─────────────────────────────────────────────────────────


def read_brand_spec(project_root: _ProjectRoot) -> BrandSpec:
    """Load and reconstruct the :class:`BrandSpec` at ``project_root``.

    Strict — the file must exist, parse as JSON, and satisfy
    :meth:`BrandSpec.from_dict`.  Use :func:`read_brand_spec_if_exists`
    for the soft-not-found variant.

    Raises
    ------
    BrandStoreReadError
        If the file is missing / unreadable / not valid JSON / does
        not satisfy :meth:`BrandSpec.from_dict` / carries an
        unsupported ``schema_version``.
    """
    target = resolve_brand_store_path(project_root)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BrandStoreReadError(
            f"brand spec not found at {target}"
        ) from exc
    except OSError as exc:
        raise BrandStoreReadError(
            f"failed to read brand spec at {target}: {exc!s}"
        ) from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrandStoreReadError(
            f"brand spec at {target} is not valid JSON: {exc!s}"
        ) from exc

    if not isinstance(payload, dict):
        raise BrandStoreReadError(
            f"brand spec at {target} is not a JSON object "
            f"(got {type(payload).__name__})"
        )

    schema_version = payload.get("schema_version")
    if schema_version is not None and not isinstance(schema_version, str):
        raise BrandStoreReadError(
            f"brand spec at {target} has non-string schema_version "
            f"({type(schema_version).__name__})"
        )
    if isinstance(schema_version, str) and schema_version != BRAND_SPEC_SCHEMA_VERSION:
        # Same-major schemas would land here in a future rev — for
        # 1.0.0 we require an exact match.  A future migration helper
        # would relax this check by inspecting the major component.
        raise BrandStoreReadError(
            f"brand spec at {target} has unsupported schema_version "
            f"{schema_version!r} (expected {BRAND_SPEC_SCHEMA_VERSION!r})"
        )

    try:
        return BrandSpec.from_dict(payload)
    except BrandSpecError as exc:
        raise BrandStoreReadError(
            f"brand spec at {target} failed schema validation: {exc!s}"
        ) from exc


def read_brand_spec_if_exists(project_root: _ProjectRoot) -> BrandSpec | None:
    """Return the persisted :class:`BrandSpec` or ``None`` if absent.

    Intended for downstream agent edits that treat "no brand.json"
    as "operator did not pin a brand override — fall back to project
    tokens".  Distinct from :func:`read_brand_spec` only on the
    file-missing case: if the file exists but is unparseable / wrong
    shape, this still raises :class:`BrandStoreReadError` so a
    hand-edit gone wrong fails loud rather than silently degrading
    to "no brand".
    """
    target = resolve_brand_store_path(project_root)
    if not target.exists():
        return None
    return read_brand_spec(project_root)


def delete_brand_spec(project_root: _ProjectRoot) -> bool:
    """Remove ``<project_root>/.omnisight/brand.json`` if present.

    Returns ``True`` when a file was actually deleted, ``False`` when
    the file was already absent (the caller often does not care about
    the difference, but tests and takedown flows occasionally do).
    Does not delete the ``.omnisight/`` directory itself — other
    artefacts (clone manifest, platform hint) may share the dir.

    Raises
    ------
    BrandStoreWriteError
        If the deletion itself fails (e.g. permission denied).
    """
    target = resolve_brand_store_path(project_root)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BrandStoreWriteError(
            f"failed to delete brand spec at {target}: {exc!s}"
        ) from exc
    return True
