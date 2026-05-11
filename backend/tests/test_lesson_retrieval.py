"""OP-848 lesson BM25 retrieval tests."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from backend.agents import lesson_retrieval as lr

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "run_s1_via_anthropic_sdk.py"
)


@pytest.fixture(autouse=True)
def _clear_index() -> None:
    lr._INDEX = None


def _lesson(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_build_index_reads_lesson_files_as_documents(tmp_path: Path) -> None:
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    _lesson(lessons / "L-OP-1-alpha.md", "Alpha deployment lesson")
    _lesson(lessons / "L-OP-2-beta.md", "Beta review lesson")
    _lesson(lessons / "_TEMPLATE.md", "Template ignored")

    index = lr.build_index(lessons)

    assert index is not None
    assert [doc.path.name for doc in index.documents] == [
        "L-OP-1-alpha.md",
        "L-OP-2-beta.md",
    ]


def test_query_uses_title_plus_acceptance_criteria_and_returns_top_three(
    tmp_path: Path,
) -> None:
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    _lesson(lessons / "L-OP-1-payments.md", "payments webhook retry retry")
    _lesson(lessons / "L-OP-2-webhook.md", "webhook idempotency replay")
    _lesson(lessons / "L-OP-3-runner.md", "runner retry budget")
    _lesson(lessons / "L-OP-4-docs.md", "documentation only")

    results = lr.retrieve_lessons(
        lessons,
        ticket_title="Webhook retries",
        acceptance_criteria="Add idempotency and retry budget handling",
        top_k=3,
    )

    assert [result.path.name for result in results] == [
        "L-OP-2-webhook.md",
        "L-OP-3-runner.md",
        "L-OP-1-payments.md",
    ]


def test_rebuilds_when_lesson_mtime_is_newer_than_index_timestamp(tmp_path: Path) -> None:
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    lesson = lessons / "L-OP-1-alpha.md"
    _lesson(lesson, "alpha only")
    os.utime(lesson, (1_700_000_000, 1_700_000_000))
    assert lr.retrieve_lessons(lessons, ticket_title="alpha", acceptance_criteria="")

    _lesson(lesson, "gamma only")
    os.utime(lesson, (1_700_000_010, 1_700_000_010))
    results = lr.retrieve_lessons(lessons, ticket_title="gamma", acceptance_criteria="")

    assert results[0].text == "gamma only"


def test_empty_and_unavailable_indexes_degrade_without_injection(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    _lesson(lessons / "L-OP-1-alpha.md", "alpha only")

    with caplog.at_level("INFO"):
        empty = lr.retrieve_lessons(
            lessons,
            ticket_title="zzz",
            acceptance_criteria="yyy",
        )
    with caplog.at_level("WARNING"):
        missing = lr.retrieve_lessons(
            tmp_path / "missing",
            ticket_title="alpha",
            acceptance_criteria="",
        )
        lr._INDEX = object()  # type: ignore[assignment]
        corrupt = lr.retrieve_lessons(
            lessons,
            ticket_title="alpha",
            acceptance_criteria="",
        )

    assert empty == missing == corrupt == ()
    assert lr.build_lessons_system_message(empty) == ""
    assert "lesson BM25 retrieval empty" in caplog.text
    assert lr.LESSONS_DIR_UNREADABLE in caplog.text
    assert "lesson BM25 index unavailable" in caplog.text


def test_runner_injects_retrieved_lessons_into_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("run_s1_via_anthropic_sdk", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "WORKTREE_PATH", Path("/repo"))

    seen = {}

    def fake_retrieve_lessons(lessons_dir: Path, **kwargs):
        seen["lessons_dir"] = lessons_dir
        seen.update(kwargs)
        return (
            lr.LessonSearchResult(
                Path("L-OP-19-stream-consumers.md"),
                "Use catchup plus idempotency.",
                1.0,
            ),
        )

    monkeypatch.setattr(module, "retrieve_lessons", fake_retrieve_lessons)
    prompt = module._build_lesson_system_prompt(
        "Event consumer catchup",
        "## Acceptance criteria\n- Add replay idempotency",
    )

    assert seen["lessons_dir"] == Path("/repo/docs/sop/lessons")
    assert seen["ticket_title"] == "Event consumer catchup"
    assert "Add replay idempotency" in seen["acceptance_criteria"]
    assert prompt.startswith(lr.LESSON_PROMPT_HEADER)
