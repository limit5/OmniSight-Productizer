"""OP-788 dynamic ADR index contract for the docs-site build."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import docs_site_adr as adr


REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"
README = ADR_DIR / "README.md"
SOP_DIR = REPO_ROOT / "docs" / "sop"
OPERATIONS_DIR = REPO_ROOT / "docs" / "operations"


def _write_adr(
    directory: Path,
    *,
    filename: str,
    adr_id: str,
    title: str,
    status: str,
    date: str,
) -> Path:
    path = directory / filename
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {adr_id}",
                f"title: {title}",
                f"status: {status}",
                f"date: {date}",
                "---",
                "",
                f"# {adr_id} - {title}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_load_adrs_reads_adr_glob_frontmatter_and_sorts_date_status_id(tmp_path: Path) -> None:
    _write_adr(
        tmp_path,
        filename="ADR-0003-third.md",
        adr_id="ADR-0003",
        title="Third",
        status="Accepted",
        date="2026-05-04",
    )
    _write_adr(
        tmp_path,
        filename="ADR-0002-second.md",
        adr_id="ADR-0002",
        title="Second",
        status="Proposed",
        date="2026-05-03",
    )
    _write_adr(
        tmp_path,
        filename="ADR-0001-first.md",
        adr_id="ADR-0001",
        title="First",
        status="Accepted",
        date="2026-05-03",
    )
    (tmp_path / "0004-legacy.md").write_text("# ignored\n", encoding="utf-8")

    records = adr.load_adrs(tmp_path)

    assert [record.id for record in records] == ["ADR-0001", "ADR-0002", "ADR-0003"]
    assert [record.status for record in records] == ["Accepted", "Proposed", "Accepted"]


def test_render_adr_index_uses_docs_site_urls(tmp_path: Path) -> None:
    _write_adr(
        tmp_path,
        filename="ADR-0007-routing.md",
        adr_id="ADR-0007",
        title="Routing",
        status="Accepted",
        date="2026-05-06",
    )

    rendered = adr.build_adr_index(tmp_path)

    assert "| ADR-0007 | 2026-05-06 | Accepted | [Routing](/docs/adr/ADR-0007-routing/) |" in rendered
    assert "ADR-0007-routing.md" not in rendered


def test_repo_adr_sources_have_required_frontmatter() -> None:
    records = adr.load_adrs(ADR_DIR)

    assert records, "docs/adr/ADR-*.md must contain the ADR source files"
    assert {record.id for record in records} >= {"ADR-0001", "ADR-0008", "ADR-0011"}


def test_adr_readme_no_longer_contains_hand_maintained_index() -> None:
    body = README.read_text(encoding="utf-8")

    assert "backend.docs_site_adr" in body
    assert "| # | Title | Status | Date |" not in body
    assert "Update this README index" not in body


@pytest.mark.parametrize("directory", [SOP_DIR, OPERATIONS_DIR])
def test_sop_and_operations_links_do_not_target_old_numeric_adr_paths(directory: Path) -> None:
    offenders: list[str] = []
    for path in directory.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        if "../adr/000" in text or "../adr/001" in text or "docs/adr/000" in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert offenders == []


@pytest.mark.parametrize("directory", [SOP_DIR, OPERATIONS_DIR])
def test_sop_and_operations_adr_markdown_links_use_docs_site_urls(directory: Path) -> None:
    offenders: list[str] = []
    for path in directory.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        if "../adr/ADR-" in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert offenders == []
