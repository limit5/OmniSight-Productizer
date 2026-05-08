"""Dynamic lessons-learned index support for the docs-site build.

OP-787 moves the lessons-learned index away from a committed generated
``docs/sop/lessons-learned.md`` file. The docs site should call this module
at build time, read per-lesson frontmatter from ``docs/sop/lessons/L-*.md``,
and render the index from source files.

Module-global state audit (SOP 2026-04-21 rule)
------------------------------------------------
Only immutable path/string constants live at module scope. Each build reads
the filesystem from scratch and returns fresh dataclasses, so there is no
cross-worker state to coordinate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
LESSONS_DIR = REPO_ROOT / "docs" / "sop" / "lessons"
LESSON_GLOB = "L-*.md"
DOCS_SITE_LESSON_PREFIX = "/docs/sop/lessons/"
REQUIRED_FRONTMATTER = ("id", "ticket", "title", "date", "tags")
STANDARD_SECTIONS = ("Situation", "Fix", "Verification", "Generalisation")


@dataclass(frozen=True)
class LessonRecord:
    path: Path
    frontmatter: dict[str, str]
    body: str

    @property
    def id(self) -> str:
        return self.frontmatter["id"]

    @property
    def ticket(self) -> str:
        return self.frontmatter["ticket"]

    @property
    def title(self) -> str:
        return self.frontmatter["title"]

    @property
    def date(self) -> str:
        return self.frontmatter["date"]

    @property
    def legacy_lesson(self) -> int | None:
        raw = self.frontmatter.get("legacy_lesson", "").strip()
        return int(raw) if raw.isdigit() else None

    @property
    def docs_site_url(self) -> str:
        return f"{DOCS_SITE_LESSON_PREFIX}{self.path.stem}/"

    @property
    def relpath(self) -> str:
        try:
            return self.path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return self.path.name


def parse_lesson(text: str, path: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing frontmatter block")
    try:
        raw_frontmatter, body = text[4:].split("\n---\n", 1)
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
    return frontmatter, body.rstrip() + "\n"


def load_lessons(lessons_dir: Path = LESSONS_DIR) -> list[LessonRecord]:
    records = [
        LessonRecord(path=path, frontmatter=frontmatter, body=body)
        for path in sorted(lessons_dir.glob(LESSON_GLOB))
        for frontmatter, body in [parse_lesson(path.read_text(encoding="utf-8"), path)]
    ]
    return sorted(records, key=lambda lesson: (lesson.date, lesson.ticket, lesson.id, lesson.path.name))


def legacy_number_note(records: Iterable[LessonRecord]) -> str:
    numbered = [record.legacy_lesson for record in records if record.legacy_lesson is not None]
    if not numbered:
        return ""

    counts = {number: numbered.count(number) for number in sorted(set(numbered))}
    duplicates = [f"L{number}" for number, count in counts.items() if count > 1]
    missing = [f"L{number}" for number in range(1, max(numbered) + 1) if number not in counts]
    if not duplicates and not missing:
        return "Legacy sequential lesson numbers are retained only as migration metadata.\n"

    parts: list[str] = []
    if duplicates:
        parts.append(f"duplicate legacy numbers from concurrent patchsets: {', '.join(duplicates)}")
    if missing:
        parts.append(f"missing legacy numbers: {', '.join(missing)}")
    return (
        "Legacy sequential lesson numbers are retained only as migration metadata; "
        + "; ".join(parts)
        + ".\n"
    )


def render_lessons_index(records: Iterable[LessonRecord]) -> str:
    lessons = list(records)
    lines = [
        "# Lessons Learned — OmniSight",
        "",
        "**Status**: Dynamic docs-site index. Add or edit individual lessons under `docs/sop/lessons/`.",
        "",
        "**Authority**: Owned by operator + AI fleet collectively. Each lesson file must include "
        "`Situation`, `Fix`, `Verification`, and `Generalisation` sections. Vague entries "
        "(`be more careful`, `pay attention`) are auto-rejected by the META retrospective "
        "workflow (per `docs/sop/jira-ticket-conventions.md` §14).",
        "",
        "**How entries land here**:",
        "1. From META retrospective tickets (label `meta:lessons-learned`) once Approved",
        "2. From cross-Phase retrospectives (`docs/retrospectives/YYYY-MM-DD-<slug>.md`) that distil into a generalisable lesson",
        "3. Direct operator append when codifying tribal knowledge",
        "",
        "---",
        "",
        "## Index",
        "",
        "| ID | Date | Ticket | Lesson | Legacy |",
        "|---|---|---|---|---|",
    ]

    for lesson in lessons:
        legacy = f"L{lesson.legacy_lesson}" if lesson.legacy_lesson is not None else ""
        lines.append(
            f"| {lesson.id} | {lesson.date} | {lesson.ticket} | "
            f"[{lesson.title}]({lesson.docs_site_url}) | {legacy} |"
        )

    note = legacy_number_note(lessons)
    if note:
        lines.extend(["", note.rstrip()])

    lines.extend(["", "---", ""])
    for lesson in lessons:
        lines.extend(
            [
                f"## {lesson.id} — {lesson.title} ({lesson.date})",
                "",
                f"Source: [`{lesson.relpath}`]({lesson.docs_site_url})",
                "",
                lesson.body.rstrip(),
                "",
                "---",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def check_standard_sections(records: Iterable[LessonRecord]) -> None:
    for lesson in records:
        missing = [
            section
            for section in STANDARD_SECTIONS
            if not re.search(rf"^\*\*{section}(?:\*\*|\s*\([^)]*\)\*\*)", lesson.body, flags=re.MULTILINE)
        ]
        if missing:
            raise ValueError(f"{lesson.relpath}: missing sections: {', '.join(missing)}")


def build_lessons_index(lessons_dir: Path = LESSONS_DIR) -> str:
    lessons = load_lessons(lessons_dir)
    check_standard_sections(lessons)
    return render_lessons_index(lessons)
