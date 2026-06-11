"""Vendor mirror artifact resolution (OP-2101 / vmnda C5).

Reads the local snapshot of the isolated ``vendor-mirrors-nda/catalog`` repo
from ``settings.mirror_catalog_dir`` and resolves a Productizer catalog id to
the mirror fact row in ``mirror/*.yaml``. This is the Productizer-side resolver
only: it does not fetch blobs, clone overlays, or carry credentials inline.

Credential handling mirrors :mod:`backend.agents.product_source`: catalog rows
may name a ``git_account_ref`` and callers resolve that reference through the
existing ``git_accounts`` model. Any inline token-like field in a mirror row is
rejected fail-closed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from backend.config import settings

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_INLINE_SECRET_FIELDS = frozenset({
    "access_token",
    "api_token",
    "deploy_token",
    "gitlab_token",
    "private_token",
    "token",
})


class MirrorArtifactError(RuntimeError):
    """Base class for mirror artifact resolution failures."""


class MirrorCatalogNotVendoredError(MirrorArtifactError):
    """The mirror catalog snapshot is absent or has no row for the artifact."""


class PointerOnlyArtifactError(MirrorArtifactError):
    """The artifact is NDA pointer-only and must be manually staged."""

    def __init__(
        self,
        *,
        portal_url: str,
        sha256: Optional[str],
        stage_path: Optional[str],
    ) -> None:
        super().__init__(
            "artifact is pointer-only; stage it from the vendor portal "
            f"{portal_url!r}"
        )
        self.portal_url = portal_url
        self.sha256 = sha256
        self.stage_path = stage_path


class BotPullDeniedError(MirrorArtifactError):
    """The mirror catalog marks this artifact human-build-only."""


@dataclass(frozen=True)
class ResolvedBlob:
    """A mirrored blob URL and expected digest."""

    mirror_url: str
    sha256: str
    git_account_ref: Optional[str] = None


@dataclass(frozen=True)
class ResolvedOverlay:
    """A mirrored git overlay ref and the vendor ref it rebases onto."""

    overlay_ref: str
    vendor_ref: str
    git_account_ref: Optional[str] = None


def resolve(
    catalog_id: str,
    *,
    is_bot_identity: bool = False,
) -> ResolvedBlob | ResolvedOverlay:
    """Resolve *catalog_id* against ``settings.mirror_catalog_dir``.

    ``artifact_shape: blob`` rows with a usable ``mirror_url`` resolve to
    :class:`ResolvedBlob`; ``artifact_shape: git`` rows resolve to
    :class:`ResolvedOverlay`. Pointer-only rows raise
    :class:`PointerOnlyArtifactError`. Missing snapshots or missing rows raise
    :class:`MirrorCatalogNotVendoredError` so C6 can vendor the real catalog
    without changing callers.
    """
    clean_id = catalog_id.strip()
    if not clean_id:
        raise MirrorCatalogNotVendoredError("catalog_id is required")

    entry = _find_entry(clean_id)
    _reject_inline_secret(entry, clean_id)

    if is_bot_identity and _clean(entry.get("bot_pull")).lower() == "deny":
        raise BotPullDeniedError(
            f"artifact {clean_id!r} has bot_pull='deny' in the mirror catalog"
        )

    nda_posture = _clean(entry.get("nda_posture")).upper()
    mirror_url = _clean(entry.get("mirror_url"))
    sha256 = _clean(entry.get("sha256")) or None
    git_account_ref = _clean(entry.get("git_account_ref")) or None
    shape = _clean(entry.get("artifact_shape")).lower()

    if nda_posture == "NO" or not mirror_url and shape != "git":
        portal_url = _clean(entry.get("portal_url"))
        if not portal_url:
            raise MirrorArtifactError(
                f"mirror catalog row {clean_id!r} is pointer-only but has no portal_url"
            )
        raise PointerOnlyArtifactError(
            portal_url=portal_url,
            sha256=sha256,
            stage_path=_clean(entry.get("stage_path")) or None,
        )

    if shape == "blob":
        if not mirror_url or not sha256:
            raise MirrorArtifactError(
                f"mirror catalog row {clean_id!r} blob requires mirror_url and sha256"
            )
        return ResolvedBlob(
            mirror_url=mirror_url,
            sha256=sha256,
            git_account_ref=git_account_ref,
        )

    if shape == "git":
        overlay_ref = _clean(entry.get("overlay_ref"))
        vendor_ref = _clean(entry.get("vendor_ref"))
        if not overlay_ref or not vendor_ref:
            raise MirrorArtifactError(
                f"mirror catalog row {clean_id!r} git requires overlay_ref and vendor_ref"
            )
        return ResolvedOverlay(
            overlay_ref=overlay_ref,
            vendor_ref=vendor_ref,
            git_account_ref=git_account_ref,
        )

    raise MirrorArtifactError(
        f"mirror catalog row {clean_id!r} has invalid artifact_shape {shape!r}"
    )


async def resolve_mirror_artifact_credential(
    artifact: ResolvedBlob | ResolvedOverlay,
    *,
    tenant_id: Optional[str] = None,
) -> dict:
    """Resolve the ``git_accounts`` row backing a mirrored artifact."""
    if not artifact.git_account_ref:
        raise MirrorArtifactError("mirror artifact has no git_account_ref")

    from backend import git_credentials

    row = await git_credentials.pick_by_id(
        artifact.git_account_ref, tenant_id=tenant_id
    )
    if row is None:
        raise MirrorArtifactError(
            f"git_accounts row {artifact.git_account_ref!r} not found for mirror artifact"
        )
    return row


def _catalog_root() -> Path:
    raw = _clean(settings.mirror_catalog_dir)
    path = Path(raw or "third_party/vendor-mirror-catalog")
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def _find_entry(catalog_id: str) -> dict[str, Any]:
    mirror_dir = _catalog_root() / "mirror"
    if not mirror_dir.is_dir():
        raise MirrorCatalogNotVendoredError(
            f"mirror catalog snapshot not vendored at {mirror_dir}"
        )

    for path in sorted(mirror_dir.glob("*.yaml")):
        for entry in _entries_from_file(path):
            if _clean(entry.get("catalog_id")) == catalog_id:
                return entry

    raise MirrorCatalogNotVendoredError(
        f"catalog_id {catalog_id!r} is not present in the mirror catalog snapshot"
    )


def _entries_from_file(path: Path) -> list[dict[str, Any]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise MirrorArtifactError(f"mirror catalog YAML failed to parse: {path}") from exc

    if not isinstance(raw, dict):
        log.warning("mirror catalog file %s did not parse as a mapping", path)
        return []
    entries = raw.get("entries") or []
    if not isinstance(entries, list):
        raise MirrorArtifactError(f"mirror catalog file {path} has non-list entries")
    return [entry for entry in entries if isinstance(entry, dict)]


def _reject_inline_secret(entry: dict[str, Any], catalog_id: str) -> None:
    present = sorted(k for k in _INLINE_SECRET_FIELDS if _clean(entry.get(k)))
    if present:
        raise MirrorArtifactError(
            f"mirror catalog row {catalog_id!r} contains inline credential fields "
            f"{present}; use git_account_ref"
        )


def _clean(value: Any) -> str:
    return str(value or "").strip()
