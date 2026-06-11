"""OP-2100 - Productizer/vendor-mirrors-nda cross-repo lock-step guard.

The mirror catalog lives in a separate, SHA-pinned checkout.  Until that
snapshot is vendored locally this guard skips; once present, every
``configs/embedded_catalog`` NDA-host install URL must have a matching
``catalog_id`` in the mirror catalog.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import yaml

from backend.config import settings


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
EMBEDDED_CATALOG = PROJECT_ROOT / "configs" / "embedded_catalog"
FIXTURES = Path(__file__).parent / "fixtures" / "mirror_catalog_crossref"
NDA_MIRROR_HOST = "vendor-mirrors-nda.omnisight.local"


@dataclass(frozen=True)
class EmbeddedNdaInstallUrl:
    catalog_id: str
    install_url: str
    source_path: Path


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} must parse as a yaml mapping"
    return loaded


def list_embedded_catalog_nda_host_install_urls(
    catalog_dir: Path = EMBEDDED_CATALOG,
) -> list[EmbeddedNdaInstallUrl]:
    """List embedded catalog entries that install from the NDA mirror host."""
    out: list[EmbeddedNdaInstallUrl] = []
    for path in sorted(catalog_dir.glob("*.yaml")):
        if path.name == "_schema.yaml":
            continue
        doc = _load_yaml(path)
        for entry in doc.get("entries", []) or []:
            install_url = entry.get("install_url")
            if not isinstance(install_url, str):
                continue
            if urlparse(install_url).hostname != NDA_MIRROR_HOST:
                continue
            out.append(
                EmbeddedNdaInstallUrl(
                    catalog_id=entry["id"],
                    install_url=install_url,
                    source_path=path,
                )
            )
    return out


def _mirror_catalog_dir_from_settings() -> Path:
    configured = Path(settings.mirror_catalog_dir)
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


def _load_mirror_catalog_ids(mirror_catalog_dir: Path) -> set[str]:
    if not mirror_catalog_dir.exists():
        pytest.skip(f"mirror catalog snapshot not vendored: {mirror_catalog_dir}")

    paths = sorted(
        path
        for path in mirror_catalog_dir.rglob("*.yaml")
        if not path.name.startswith("_")
    )
    if not paths:
        pytest.skip(f"mirror catalog snapshot has no yaml files: {mirror_catalog_dir}")

    catalog_ids: set[str] = set()
    for path in paths:
        doc = _load_yaml(path)
        for entry in doc.get("entries", []) or []:
            catalog_id = entry.get("catalog_id")
            if isinstance(catalog_id, str):
                catalog_ids.add(catalog_id)
    return catalog_ids


def _assert_nda_install_urls_have_mirror_catalog_ids(
    refs: list[EmbeddedNdaInstallUrl],
    mirror_catalog_ids: set[str],
) -> None:
    missing = [
        f"{ref.catalog_id} ({ref.source_path.relative_to(PROJECT_ROOT)} "
        f"install_url={ref.install_url})"
        for ref in refs
        if ref.catalog_id not in mirror_catalog_ids
    ]
    assert not missing, (
        "NDA-host embedded_catalog entries missing from mirror catalog: "
        + ", ".join(missing)
    )


def test_helper_lists_embedded_catalog_nda_host_install_urls() -> None:
    refs = list_embedded_catalog_nda_host_install_urls()

    assert refs == [
        EmbeddedNdaInstallUrl(
            catalog_id="qualcomm-qcs6490-aarch64",
            install_url=(
                "https://vendor-mirrors-nda.omnisight.local/qualcomm/"
                "qcs6490/qcs6490-linux-sdk-aarch64-toolchain.tar.xz"
            ),
            source_path=EMBEDDED_CATALOG / "cross-toolchain.yaml",
        )
    ]


def test_matching_mirror_catalog_fixture_passes() -> None:
    refs = list_embedded_catalog_nda_host_install_urls()
    mirror_catalog_ids = _load_mirror_catalog_ids(FIXTURES / "matching")

    _assert_nda_install_urls_have_mirror_catalog_ids(refs, mirror_catalog_ids)


def test_missing_mirror_catalog_id_fixture_fails_red() -> None:
    refs = list_embedded_catalog_nda_host_install_urls()
    mirror_catalog_ids = _load_mirror_catalog_ids(FIXTURES / "missing")

    with pytest.raises(
        AssertionError,
        match="qualcomm-qcs6490-aarch64",
    ):
        _assert_nda_install_urls_have_mirror_catalog_ids(refs, mirror_catalog_ids)


def test_absent_mirror_catalog_snapshot_skips(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception):
        _load_mirror_catalog_ids(tmp_path / "not-vendored-yet")


def test_nda_host_install_urls_have_pinned_mirror_catalog_ids() -> None:
    refs = list_embedded_catalog_nda_host_install_urls()
    mirror_catalog_ids = _load_mirror_catalog_ids(
        _mirror_catalog_dir_from_settings()
    )

    _assert_nda_install_urls_have_mirror_catalog_ids(refs, mirror_catalog_ids)
