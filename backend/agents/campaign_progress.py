"""RPG.W20.2 -- deterministic campaign progress chapters.

ADR 0008 defines W20 campaigns as narrative wrappers over multi-task work.
This module keeps the progress-bar math backend-local and persistence-free:
callers pass task counts, and receive chapter slices suitable for a later API
or UI surface.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants and frozen dataclasses only. It
performs no database access, registry mutation, network I/O, or clock reads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

ChapterStatus = Literal["locked", "active", "complete"]

DEFAULT_CHAPTER_COUNT = 4


@dataclass(frozen=True)
class CampaignChapter:
    """One contiguous task slice within a campaign."""

    index: int
    title: str
    start_task_index: int
    end_task_index: int
    completed_tasks: int
    total_tasks: int
    percent: int
    status: ChapterStatus


@dataclass(frozen=True)
class CampaignProgress:
    """Progress-bar state for a multi-task RPG campaign."""

    completed_tasks: int
    total_tasks: int
    percent: int
    current_chapter_index: int
    chapters: tuple[CampaignChapter, ...]


def campaign_progress(
    completed_tasks: int,
    total_tasks: int,
    *,
    chapter_count: int = DEFAULT_CHAPTER_COUNT,
    chapter_titles: Sequence[str] | None = None,
) -> CampaignProgress:
    """Return campaign progress and evenly sliced chapter state.

    Chapters are contiguous over the campaign's task list. When tasks do not
    divide evenly, earlier chapters receive the remainder so every task belongs
    to exactly one chapter and no chapter is empty.
    """
    completed = _clean_task_count("completed_tasks", completed_tasks)
    total = _clean_task_count("total_tasks", total_tasks)
    chapters = _clean_chapter_count(chapter_count, total)
    if completed > total:
        raise ValueError("completed_tasks must be <= total_tasks")

    titles = _chapter_titles(chapter_titles, chapters)
    slices = _chapter_slices(total, chapters)
    chapter_progress = tuple(
        _chapter_progress(index, title, start, end, completed)
        for index, (title, (start, end)) in enumerate(zip(titles, slices), start=1)
    )
    current_chapter = _current_chapter_index(chapter_progress)
    return CampaignProgress(
        completed_tasks=completed,
        total_tasks=total,
        percent=_percent(completed, total),
        current_chapter_index=current_chapter,
        chapters=chapter_progress,
    )


def _chapter_progress(
    index: int,
    title: str,
    start: int,
    end: int,
    completed_tasks: int,
) -> CampaignChapter:
    total = end - start
    completed = min(max(completed_tasks - start, 0), total)
    if completed >= total:
        status: ChapterStatus = "complete"
    elif completed_tasks >= start:
        status = "active"
    else:
        status = "locked"
    return CampaignChapter(
        index=index,
        title=title,
        start_task_index=start,
        end_task_index=end,
        completed_tasks=completed,
        total_tasks=total,
        percent=_percent(completed, total),
        status=status,
    )


def _chapter_slices(total_tasks: int, chapter_count: int) -> tuple[tuple[int, int], ...]:
    base, remainder = divmod(total_tasks, chapter_count)
    slices: list[tuple[int, int]] = []
    cursor = 0
    for index in range(chapter_count):
        size = base + (1 if index < remainder else 0)
        start = cursor
        cursor += size
        slices.append((start, cursor))
    return tuple(slices)


def _current_chapter_index(chapters: tuple[CampaignChapter, ...]) -> int:
    for chapter in chapters:
        if chapter.status != "complete":
            return chapter.index
    return chapters[-1].index


def _chapter_titles(
    titles: Sequence[str] | None,
    chapter_count: int,
) -> tuple[str, ...]:
    if titles is None:
        return tuple(f"Chapter {index}" for index in range(1, chapter_count + 1))
    cleaned = tuple(_required("chapter title", title) for title in titles)
    if len(cleaned) != chapter_count:
        raise ValueError("chapter_titles length must match chapter_count")
    return cleaned


def _clean_task_count(field: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int")
    if value < 0:
        raise ValueError(f"{field} must be >= 0")
    return value


def _clean_chapter_count(chapter_count: int, total_tasks: int) -> int:
    count = _clean_task_count("chapter_count", chapter_count)
    if count < 1:
        raise ValueError("chapter_count must be >= 1")
    if total_tasks < 1:
        raise ValueError("total_tasks must be >= 1")
    if count > total_tasks:
        raise ValueError("chapter_count must be <= total_tasks")
    return count


def _required(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} is required")
    return stripped


def _percent(completed: int, total: int) -> int:
    return min(100, math.floor((completed / total) * 100))


__all__ = [
    "DEFAULT_CHAPTER_COUNT",
    "CampaignChapter",
    "CampaignProgress",
    "ChapterStatus",
    "campaign_progress",
]
