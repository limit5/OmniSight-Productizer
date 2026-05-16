"""OP-906 (F8) — Memory write-back protocol at task completion.

Six AC-traceable test cases, all run against in-memory collaborators
(no Postgres, no real Memory Tool storage, no Cognee). Each test
documents the AC item it covers.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.agents import incident_recorder, memory_writeback
from backend.agents.failure_class import FailureClass
from backend.agents.incident_recorder import RunnerIncidentRecord
from backend.agents.memory_tool_handler import (
    MemoryToolConfig,
    MemoryToolHandler,
)
from backend.agents.memory_writeback import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    STORE_COGNEE,
    STORE_INCIDENTS,
    STORE_MEMORY_TOOL,
    SUCCESS_OUTCOME_FAILURE_CLASS,
    LessonClassificationFailed,
    MemoryWriteback,
    WritebackRequest,
    classify_lesson,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_buffers() -> Iterator[None]:
    memory_writeback.reset_for_tests()
    incident_recorder.reset_for_tests()
    yield
    memory_writeback.reset_for_tests()
    incident_recorder.reset_for_tests()


@pytest.fixture
def memory_tool(tmp_path: Path) -> MemoryToolHandler:
    storage = tmp_path / "fleet"
    storage.mkdir(parents=True, exist_ok=True)
    return MemoryToolHandler(
        MemoryToolConfig(
            fleet_id="fleet-test",
            storage_root=storage,
            cap_mb=4,
            progress_path=tmp_path / "progress.txt",
            ticket_key="OP-906",
        )
    )


@pytest.fixture
def cognee_calls() -> list[str]:
    return []


@pytest.fixture
def cognee_emitter(cognee_calls: list[str]) -> Callable[[str], None]:
    def _emit(ticket_key: str) -> None:
        cognee_calls.append(ticket_key)

    return _emit


# ── Case 1: AC #1 success write-back — lesson append + Cognee tickle ──


def test_success_writeback_appends_lesson_and_tickles_cognee(
    tmp_path: Path,
    memory_tool: MemoryToolHandler,
    cognee_emitter: Callable[[str], None],
    cognee_calls: list[str],
) -> None:
    wb = MemoryWriteback(
        memory_tool=memory_tool,
        cognee_emitter=cognee_emitter,
        incident_queue_dir=tmp_path / "queue",
    )
    request = WritebackRequest(
        ticket_key="OP-906",
        attempt_n=1,
        outcome=OUTCOME_SUCCESS,
        summary="lessons-learned: the new write-back path",
        lesson_id="L-OP-906",
        lesson_content="# lesson body\n",
        area="backend",
        runner_class="subscription-claude",
    )

    result = wb.write(request)

    assert result.memory_tool_lesson_id == "L-OP-906"
    assert result.cognee_tickled is True
    assert result.incident_id is None  # success without positive-feedback flag
    assert result.stores_failed == ()
    assert cognee_calls == ["OP-906"]
    written = memory_tool.handle(
        {"command": "view", "path": "/memories/lesson:L-OP-906.md"}
    )
    assert "lesson body" in written["content"]


# ── Case 2: AC #1 failure write-back — incident insert + Cognee tickle ─


def test_failure_writeback_inserts_incident_and_tickles_cognee(
    tmp_path: Path,
    memory_tool: MemoryToolHandler,
    cognee_emitter: Callable[[str], None],
    cognee_calls: list[str],
) -> None:
    wb = MemoryWriteback(
        memory_tool=memory_tool,
        cognee_emitter=cognee_emitter,
        incident_queue_dir=tmp_path / "queue",
    )
    request = WritebackRequest(
        ticket_key="OP-906",
        attempt_n=1,
        outcome=OUTCOME_FAILURE,
        summary="pytest failed: assertion error in test_foo",
        raw_traceback="E   assertion error in test_foo\nFAILED tests/test_foo.py",
        mutex_label="mutex:backend.tests",
        area="backend",
        runner_class="subscription-claude",
    )

    result = wb.write(request)

    assert result.incident_id is not None
    assert result.memory_tool_lesson_id is None
    assert result.cognee_tickled is True
    assert result.stores_failed == ()

    rows = incident_recorder.get_runner_incidents(ticket_key="OP-906")
    assert len(rows) == 1
    assert rows[0].failure_class is FailureClass.TEST_FAILURE
    assert rows[0].mutex_label == "mutex:backend.tests"
    assert rows[0].area == "backend"
    # No lesson file written because outcome was failure.
    listing = memory_tool.handle({"command": "view", "path": "/memories"})
    assert listing.get("entries", []) == []


# ── Case 3: AC #2 idempotency — same (ticket_key, attempt_n) is a no-op ─


def test_idempotent_rewrite_returns_cached_result(
    tmp_path: Path,
    memory_tool: MemoryToolHandler,
    cognee_emitter: Callable[[str], None],
    cognee_calls: list[str],
) -> None:
    wb = MemoryWriteback(
        memory_tool=memory_tool,
        cognee_emitter=cognee_emitter,
        incident_queue_dir=tmp_path / "queue",
    )
    request = WritebackRequest(
        ticket_key="OP-906",
        attempt_n=2,
        outcome=OUTCOME_FAILURE,
        summary="pytest failed",
        raw_traceback="assertion error",
        area="backend",
    )

    first = wb.write(request)
    second = wb.write(request)

    # Second call returns the cached result, marked idempotent_skip.
    assert second.idempotent_skip is True
    assert first.idempotent_skip is False
    assert second.incident_id == first.incident_id
    assert second.attempt_n == first.attempt_n
    # Cognee emitter only invoked on the first pass.
    assert cognee_calls == ["OP-906"]
    # Only one incident row landed.
    assert len(incident_recorder.get_runner_incidents(ticket_key="OP-906")) == 1


# ── Case 4: AC #3 store-down degrades — outage flows to stores_failed ──


def test_store_down_degrades_gracefully(tmp_path: Path) -> None:
    """One failing collaborator must not block the others or raise."""

    def boom_incident_writer(**_kwargs: Any) -> RunnerIncidentRecord:
        raise RuntimeError("simulated Postgres outage")

    cognee_calls: list[str] = []

    def emit(key: str) -> None:
        cognee_calls.append(key)

    wb = MemoryWriteback(
        memory_tool=None,  # no memory tool — exercises the unset path
        cognee_emitter=emit,
        incident_writer=boom_incident_writer,
        incident_queue_dir=tmp_path / "queue",
    )
    request = WritebackRequest(
        ticket_key="OP-906",
        attempt_n=1,
        outcome=OUTCOME_FAILURE,
        summary="something broke",
        area="backend",
    )

    result = wb.write(request)

    assert STORE_INCIDENTS in result.stores_failed
    assert STORE_MEMORY_TOOL not in result.stores_failed
    assert STORE_COGNEE not in result.stores_failed
    assert result.cognee_tickled is True
    assert result.incident_id is None
    # Recovery: failed incident insert is queued to disk for hourly retry.
    queued = list((tmp_path / "queue").glob("*.json"))
    assert len(queued) == 1
    assert "OP-906" in queued[0].name


# ── Case 5: AC error-catalog lesson classification fallback ────────────


def test_lesson_classification_falls_back_when_no_keyword_matches(
    tmp_path: Path,
    memory_tool: MemoryToolHandler,
    cognee_emitter: Callable[[str], None],
) -> None:
    """Heuristic miss → no memory_tool entry, success path still completes."""
    wb = MemoryWriteback(
        memory_tool=memory_tool,
        cognee_emitter=cognee_emitter,
        incident_queue_dir=tmp_path / "queue",
    )
    request = WritebackRequest(
        ticket_key="OP-906",
        attempt_n=1,
        outcome=OUTCOME_SUCCESS,
        summary="merged change 12345; no lesson keyword in this summary",
        area="backend",
    )

    result = wb.write(request)

    assert result.memory_tool_lesson_id is None
    assert result.stores_failed == ()
    # Caller-side bug (lesson_id without content) raises
    # LessonClassificationFailed from the pure helper, but the
    # orchestrator catches it and skips the memory_tool write rather
    # than failing the whole fan-out.
    with pytest.raises(LessonClassificationFailed):
        classify_lesson(
            WritebackRequest(
                ticket_key="OP-906",
                attempt_n=2,
                outcome=OUTCOME_SUCCESS,
                lesson_id="L-missing-content",
            )
        )
    # When the orchestrator sees the same bug, it logs + returns None.
    bad_request = WritebackRequest(
        ticket_key="OP-906",
        attempt_n=2,
        outcome=OUTCOME_SUCCESS,
        lesson_id="L-missing-content",
    )
    bad_result = wb.write(bad_request)
    assert bad_result.memory_tool_lesson_id is None
    assert STORE_MEMORY_TOOL not in bad_result.stores_failed


# ── Case 6: AC fire-and-forget Cognee tickle — must not block the path ─


def test_cognee_tickle_is_fire_and_forget(tmp_path: Path) -> None:
    """Slow Cognee emitter trips the budget; rest of the fan-out finishes."""
    started = threading.Event()
    release = threading.Event()

    def slow_emitter(_ticket_key: str) -> None:
        started.set()
        # Block past the cognee budget; the wrapper should time out and
        # surface STORE_COGNEE in stores_failed without blocking the
        # incident/memory results.
        release.wait(timeout=3.0)

    incident_calls: list[dict[str, Any]] = []

    def fast_incident_writer(**kwargs: Any) -> RunnerIncidentRecord:
        incident_calls.append(kwargs)
        return incident_recorder.record_runner_incident(
            ticket_key=kwargs["ticket_key"],
            failure_class=kwargs["failure_class"],
            summary=kwargs["summary"],
            raw_traceback=kwargs.get("raw_traceback", ""),
            runner_class=kwargs.get("runner_class", "unknown"),
            mutex_label=kwargs.get("mutex_label"),
            area=kwargs.get("area"),
        )

    wb = MemoryWriteback(
        memory_tool=None,
        cognee_emitter=slow_emitter,
        incident_writer=fast_incident_writer,
        cognee_budget_s=0.1,
        incident_queue_dir=tmp_path / "queue",
    )
    request = WritebackRequest(
        ticket_key="OP-906",
        attempt_n=1,
        outcome=OUTCOME_FAILURE,
        summary="merge conflict in backend/agents/foo.py",
        raw_traceback="merge conflict: unmerged paths",
        area="backend",
    )

    t0 = time.monotonic()
    result = wb.write(request)
    elapsed = time.monotonic() - t0
    release.set()

    assert started.is_set()
    # Total time bounded near the cognee budget — definitely sub-second.
    assert elapsed < 1.5, f"writeback took {elapsed:.2f}s, expected <1.5s"
    assert STORE_COGNEE in result.stores_failed
    # Incident insert still succeeded in parallel.
    assert result.incident_id is not None
    assert len(incident_calls) == 1
    assert incident_calls[0]["failure_class"] is FailureClass.MERGE_CONFLICT


# ── Case 7 (bonus): AC #1 success_outcome path (positive-feedback flag) ─


def test_success_outcome_positive_feedback_writes_incident_row(
    tmp_path: Path,
    memory_tool: MemoryToolHandler,
    cognee_emitter: Callable[[str], None],
) -> None:
    """``record_success_outcome=True`` writes a SUCCESS_OUTCOME-tagged row.

    AC #1 wording: "OR insert to runner_incidents (failure_class=
    SUCCESS_OUTCOME if instrumenting positive feedback)". The closed
    failure_class enum doesn't carry SUCCESS_OUTCOME, so the orchestrator
    coerces to OTHER and tags the summary so dashboards can split
    positive feedback from real failures without an enum migration.
    """
    wb = MemoryWriteback(
        memory_tool=memory_tool,
        cognee_emitter=cognee_emitter,
        incident_queue_dir=tmp_path / "queue",
    )
    request = WritebackRequest(
        ticket_key="OP-906",
        attempt_n=3,
        outcome=OUTCOME_SUCCESS,
        summary="completed under budget",
        record_success_outcome=True,
        area="backend",
    )

    result = wb.write(request)

    assert result.incident_id is not None
    assert result.stores_failed == ()
    rows = incident_recorder.get_runner_incidents(ticket_key="OP-906")
    assert len(rows) == 1
    assert SUCCESS_OUTCOME_FAILURE_CLASS in rows[0].summary
