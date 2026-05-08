"""Dynamic ADR index support for the docs-site build.

OP-788 moves the ADR index away from the hand-maintained
``docs/adr/README.md`` table. The docs site should call this module at
build time, read the per-ADR frontmatter from ``docs/adr/ADR-*.md``, and
render the index from source files.

Module-global state audit (SOP 2026-04-21 rule)
------------------------------------------------
Only immutable path/string constants live at module scope. Each build
reads the filesystem from scratch and returns fresh dataclasses, so
there is no cross-worker state to coordinate.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "docs" / "adr"
ADR_GLOB = "ADR-*.md"
DOCS_SITE_ADR_PREFIX = "/docs/adr/"
REQUIRED_FRONTMATTER = ("id", "title", "status", "date")


@dataclass(frozen=True)
class AdrRecord:
    path: Path
    frontmatter: dict[str, str]

    @property
    def id(self) -> str:
        return self.frontmatter["id"]

    @property
    def title(self) -> str:
        return self.frontmatter["title"]

    @property
    def status(self) -> str:
        return self.frontmatter["status"]

    @property
    def date(self) -> str:
        return self.frontmatter["date"]

    @property
    def docs_site_url(self) -> str:
        return f"{DOCS_SITE_ADR_PREFIX}{self.path.stem}/"


def parse_frontmatter(text: str, path: Path) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing frontmatter block")
    try:
        raw_frontmatter, _body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError(f"{path}: unterminated frontmatter block") from exc

    frontmatter: dict[str, str] = {}
    for lineno, line in enumerate(raw_frontmatter.splitlines(), start=2):
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"{path}:{lineno}: invalid frontmatter line")
        frontmatter[key.strip()] = value.strip()

    missing = [key for key in REQUIRED_FRONTMATTER if key not in frontmatter]
    if missing:
        raise ValueError(f"{path}: missing frontmatter keys: {', '.join(missing)}")
    return frontmatter


def load_adrs(adr_dir: Path = ADR_DIR) -> list[AdrRecord]:
    records = [
        AdrRecord(path=path, frontmatter=parse_frontmatter(path.read_text(encoding="utf-8"), path))
        for path in sorted(adr_dir.glob(ADR_GLOB))
        if path.name != "README.md"
    ]
    return sorted(records, key=lambda adr: (adr.date, adr.status, adr.id, adr.path.name))


def render_adr_index(records: Iterable[AdrRecord]) -> str:
    lines = [
        "# Architecture Decision Records",
        "",
        "| ADR | Date | Status | Title |",
        "|---|---|---|---|",
    ]
    for adr in records:
        lines.append(
            f"| {adr.id} | {adr.date} | {adr.status} | "
            f"[{adr.title}]({adr.docs_site_url}) |"
        )
    return "\n".join(lines).rstrip() + "\n"


def build_adr_index(adr_dir: Path = ADR_DIR) -> str:
    return render_adr_index(load_adrs(adr_dir))
