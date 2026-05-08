#!/usr/bin/env python3
"""One-shot migration from the legacy lessons file to per-lesson files."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from build_lessons_index import INDEX_PATH, LESSONS_DIR, REPO_ROOT

LESSON_RE = re.compile(
    r"^## Lesson (?P<number>\d+) — (?P<title>.+?) \((?P<date>\d{4}-\d{2}-\d{2})\)\n(?P<body>.*?)(?=^## Lesson \d+ — |\Z)",
    re.MULTILINE | re.DOTALL,
)
OP_RE = re.compile(r"\bOP-\d+\b")
SUPPLEMENT_REFS = {
    "OP-741": ("3620f850", 30),
    "OP-742": ("1904bf5d", 29),
}
TITLE_OVERRIDES = {
    "OP-741": "CI recovery needs a small explicit state machine",
    "OP-742": "Test-impact analysis must fail closed on the conservative side",
}
LEGACY_TICKET_OVERRIDES = {
    25: "OP-729",
    26: "OP-736",
    27: "OP-737",
}


@dataclass(frozen=True)
class MigratedLesson:
    legacy_lesson: int
    ticket: str
    title: str
    date: str
    body: str

    @property
    def lesson_id(self) -> str:
        if self.ticket.startswith("OP-"):
            return f"L-{self.ticket}"
        return f"L-{self.ticket}"

    @property
    def filename(self) -> str:
        return f"{self.lesson_id}-{_slug(self.title)}.md"


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60].rstrip("-") or "lesson"


def _strip_conflict_markers(text: str) -> str:
    cleaned: list[str] = []
    skip_until_else = False
    skip_until_end = False
    for line in text.splitlines():
        if line.startswith("<<<<<<< "):
            skip_until_else = False
            continue
        if line.startswith("======="):
            skip_until_else = True
            continue
        if line.startswith(">>>>>>> "):
            skip_until_else = False
            skip_until_end = False
            continue
        if skip_until_else or skip_until_end:
            continue
        cleaned.append(line)
    return "\n".join(cleaned) + "\n"


def _ticket_for(number: int, title: str, body: str) -> str:
    if number in LEGACY_TICKET_OVERRIDES:
        return LEGACY_TICKET_OVERRIDES[number]
    text = f"{title}\n{body}"
    match = OP_RE.search(text)
    if match:
        return match.group(0)
    return f"LEGACY-{number:03d}"


def _normalise_body(body: str) -> str:
    body = body.strip()
    body = re.sub(r"^## Amendment block.*\Z", "", body, flags=re.MULTILINE | re.DOTALL).strip()
    body = re.sub(r"\n---\s*\Z", "", body).strip()
    body = "\n".join(line.rstrip() for line in body.splitlines())
    return body + "\n"


def _parse_lessons(text: str) -> list[MigratedLesson]:
    lessons: list[MigratedLesson] = []
    for match in LESSON_RE.finditer(_strip_conflict_markers(text)):
        number = int(match.group("number"))
        title = match.group("title").strip()
        body = _normalise_body(match.group("body"))
        lessons.append(
            MigratedLesson(
                legacy_lesson=number,
                ticket=_ticket_for(number, title, body),
                title=title,
                date=match.group("date"),
                body=body,
            )
        )
    return lessons


def _git_show(ref: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{ref}:docs/sop/lessons-learned.md"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _supplemental_lessons() -> list[MigratedLesson]:
    lessons: list[MigratedLesson] = []
    for ticket, (ref, legacy_lesson) in SUPPLEMENT_REFS.items():
        found = [
            lesson
            for lesson in _parse_lessons(_git_show(ref))
            if lesson.legacy_lesson == legacy_lesson or lesson.title == TITLE_OVERRIDES[ticket]
        ]
        if not found:
            raise RuntimeError(f"{ref}: could not find supplemental lesson for {ticket}")
        lesson = found[-1]
        lessons.append(
            MigratedLesson(
                legacy_lesson=legacy_lesson,
                ticket=ticket,
                title=TITLE_OVERRIDES[ticket],
                date=lesson.date,
                body=lesson.body,
            )
        )
    return lessons


def _tags_for(lesson: MigratedLesson) -> list[str]:
    text = f"{lesson.title}\n{lesson.body}".lower()
    tags = ["legacy"] if lesson.ticket.startswith("LEGACY-") else ["runner"]
    if "gerrit" in text:
        tags.append("gerrit")
    if "jira" in text:
        tags.append("jira")
    if "git" in text or "rebase" in text or "worktree" in text:
        tags.append("git")
    if "ci" in text or "test" in text or "verified" in text:
        tags.append("ci")
    if "webhook" in text or "stream" in text:
        tags.append("events")
    return sorted(set(tags))


def _render_lesson(lesson: MigratedLesson) -> str:
    tags = ", ".join(_tags_for(lesson))
    return (
        "---\n"
        f"id: {lesson.lesson_id}\n"
        f"ticket: {lesson.ticket}\n"
        f"title: {lesson.title}\n"
        f"date: {lesson.date}\n"
        f"tags: [{tags}]\n"
        f"legacy_lesson: {lesson.legacy_lesson}\n"
        "---\n\n"
        f"# {lesson.title}\n\n"
        f"{lesson.body.rstrip()}\n"
    )


def migrate(include_supplemental: bool = True) -> list[Path]:
    lessons = _parse_lessons(INDEX_PATH.read_text(encoding="utf-8"))
    if not lessons:
        lessons = _parse_lessons(_git_show("HEAD"))
    if include_supplemental:
        existing_tickets = {lesson.ticket for lesson in lessons}
        for lesson in _supplemental_lessons():
            if lesson.ticket not in existing_tickets:
                lessons.append(lesson)

    LESSONS_DIR.mkdir(parents=True, exist_ok=True)
    for path in LESSONS_DIR.glob("*.md"):
        if path.name != "_TEMPLATE.md":
            path.unlink()
    written: list[Path] = []
    used_names: set[str] = set()
    for lesson in sorted(lessons, key=lambda item: (item.date, item.ticket, item.legacy_lesson)):
        path = LESSONS_DIR / lesson.filename
        if path.name in used_names:
            path = LESSONS_DIR / f"{lesson.lesson_id}-legacy-{lesson.legacy_lesson}-{_slug(lesson.title)}.md"
        used_names.add(path.name)
        path.write_text(_render_lesson(lesson), encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-supplemental",
        action="store_true",
        help="do not recover OP-741/OP-742 lessons from known conflicting commits",
    )
    args = parser.parse_args()
    written = migrate(include_supplemental=not args.no_supplemental)
    print(f"migrated {len(written)} lessons into {LESSONS_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
