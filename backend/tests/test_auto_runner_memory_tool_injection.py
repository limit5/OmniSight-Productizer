"""OP-1681 (F8) — Memory Tool handler injection at the runner write-back.

Proves the runner injects a real :class:`MemoryToolHandler` into
``memory_writeback.MemoryWriteback`` so a classified lesson is *persisted*
to disk under the fleet dir — not merely id-returned via the
``memory_tool=None`` silent-skip branch (the OP-1681 root cause).

Coverage (AC #integration):
  * success-lesson-persisted-to-disk
  * failure-incident-inserted
  * idempotent-skip-on-replay
  * fail-open-on-store-outage (never raises) — both at build time
    (guarded handler build) and at write time (store handle raises)
  * opt-out-logs-explicit-line

SAFETY (AC MUST-NOT): an AUTOUSE fixture pins
``OMNISIGHT_MEMORY_TOOL_ROOT`` to ``tmp_path`` so no test can resolve the
store root to the live ``/var/omnisight/memory`` (a stray write there
triggers real-lesson eviction/deletion). Every test that writes also
asserts the resolved fleet root is under ``tmp_path`` before the write.

Network-free: the runner is loaded via importlib.spec_from_file_location
(hyphenated filename) and ``jira_dispatch.add_comment`` is patched at the
module-attribute level.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend.agents import incident_recorder, memory_writeback
from backend.agents.memory_tool_handler import MemoryDirNotWritable
from backend.agents.memory_writeback import OUTCOME_SUCCESS, WritebackRequest

_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    """Load auto-runner-jira.py fresh (re-reads env-derived constants)."""
    sys.modules.pop("jira_runner_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    """Minimal stand-in for jira_dispatch.DispatchClient."""

    agent_class = "subscription-codex"
    bot_email = "rt3628+codex-bot@gmail.com"


@pytest.fixture(autouse=True)
def _pin_store_root_and_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Pin the store root under tmp_path and reset cross-test buffers.

    Guards the live ``/var/omnisight/memory`` tree from any stray write
    (AC MUST-NOT). The opt-out env is cleared so the default
    inject-by-default posture is exercised unless a test opts in.
    """
    monkeypatch.setenv("OMNISIGHT_MEMORY_TOOL_ROOT", str(tmp_path))
    monkeypatch.delenv("OMNISIGHT_RUNNER_MEMORY_TOOL_DISABLED", raising=False)
    memory_writeback.reset_for_tests()
    incident_recorder.reset_for_tests()
    yield
    memory_writeback.reset_for_tests()
    incident_recorder.reset_for_tests()


def _fleet_root(tmp_path: Path, mod: Any) -> Path:
    """Resolved per-fleet root (``<root>/<INSTANCE_ID>``) for this runner."""
    return tmp_path / mod.INSTANCE_ID


def _assert_root_under_tmp(handler: Any, tmp_path: Path) -> None:
    """AC MUST-NOT guard: the resolved root must live under tmp_path."""
    root = handler.config.storage_root.resolve()
    assert root.is_relative_to(tmp_path.resolve()), (
        f"store root {root} escaped the tmp_path sandbox {tmp_path}"
    )


# ── Case 1: success → a classified lesson lands on disk ───────────────


def test_success_lesson_persisted_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_jira_runner()
    comments: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda _c, key, text: comments.append((key, text)),
    )

    # AC MUST-NOT: prove the resolved root is under tmp_path before writing.
    handler = mod._memory_tool_handler()
    assert handler is not None
    _assert_root_under_tmp(handler, tmp_path)

    mod._run_memory_writeback(
        _StubClient(),
        "OP-1681T",
        outcome="success",
        summary="lessons-learned: inject the Memory Tool handler at 1640",
        area="backend",
    )

    lesson_file = _fleet_root(tmp_path, mod) / "lesson:L-OP-1681T-attempt1.md"
    assert lesson_file.exists(), f"lesson not persisted at {lesson_file}"
    assert "inject the Memory Tool handler" in lesson_file.read_text()
    # Marker still posts (healthy-looking) AND the file actually exists.
    assert any("[memory-writeback] lesson=L-OP-1681T-attempt1" in t for _, t in comments)


# ── Case 2: failure → incident row inserted, no lesson file ───────────


def test_failure_incident_inserted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_jira_runner()
    monkeypatch.setattr(mod.jira_dispatch, "add_comment", lambda *a, **k: None)

    mod._run_memory_writeback(
        _StubClient(),
        "OP-1681F",
        outcome="failure",
        summary="pytest failed: assertion error in test_foo",
        raw_traceback="E   assertion error in test_foo",
        mutex_label="mutex:backend.tests",
        area="backend",
    )

    rows = incident_recorder.get_runner_incidents(ticket_key="OP-1681F")
    assert len(rows) == 1
    assert rows[0].mutex_label == "mutex:backend.tests"
    # Failure outcome writes no lesson file under the fleet dir.
    assert not list(_fleet_root(tmp_path, mod).glob("lesson:*.md"))


# ── Case 3: replay of the same (ticket_key, attempt_n) is idempotent ──


def test_idempotent_skip_on_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_jira_runner()
    handler = mod._memory_tool_handler()
    assert handler is not None
    _assert_root_under_tmp(handler, tmp_path)

    wb = memory_writeback.MemoryWriteback(memory_tool=handler)
    request = WritebackRequest(
        ticket_key="OP-1681I",
        attempt_n=1,
        outcome=OUTCOME_SUCCESS,
        summary="lessons-learned: idempotency",
        lesson_id="L-OP-1681I",
        lesson_content="# idem body\n",
        area="backend",
    )

    first = wb.write(request)
    second = wb.write(request)

    assert first.idempotent_skip is False
    assert second.idempotent_skip is True
    # Replay must not produce a second file (idempotency keying untouched).
    assert len(list(_fleet_root(tmp_path, mod).glob("lesson:*.md"))) == 1


# ── Case 4a: fail-open at BUILD time — guarded, never raises ──────────


def test_fail_open_on_build_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_jira_runner()

    def _boom(**_kwargs: Any) -> Any:
        raise MemoryDirNotWritable("simulated unprovisioned fleet root")

    monkeypatch.setattr(mod.memory_tool_handler, "build_memory_tool_handler", _boom)

    # The guarded build degrades to None instead of propagating.
    handler = mod._memory_tool_handler()
    assert handler is None
    err = capsys.readouterr().err
    assert "memory_tool handler build failed" in err

    # And the runner write-back still completes without raising.
    monkeypatch.setattr(mod.jira_dispatch, "add_comment", lambda *a, **k: None)
    mod._run_memory_writeback(
        _StubClient(),
        "OP-1681B",
        outcome="success",
        summary="lessons-learned: build-degraded path",
        area="backend",
    )


# ── Case 4b: fail-open at WRITE time — store handle raises ────────────


def test_fail_open_on_store_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_jira_runner()

    class _BoomHandler:
        def handle(self, _tool_input: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("simulated memory store outage")

    monkeypatch.setattr(mod, "_memory_tool_handler", lambda: _BoomHandler())
    comments: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda _c, key, text: comments.append((key, text)),
    )

    # Must not raise even though the memory store handle blows up.
    mod._run_memory_writeback(
        _StubClient(),
        "OP-1681O",
        outcome="success",
        summary="lessons-learned: store outage path",
        area="backend",
    )

    # Store failure routes to stores_failed → no lesson id → no marker.
    assert not any("[memory-writeback]" in t for _, t in comments)


# ── Case 5: opt-out logs an explicit "intentionally unset" line ───────


def test_opt_out_logs_explicit_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNISIGHT_RUNNER_MEMORY_TOOL_DISABLED", "1")
    mod = _load_jira_runner()
    assert mod.MEMORY_TOOL_DISABLED is True

    handler = mod._memory_tool_handler()
    assert handler is None
    err = capsys.readouterr().err
    assert "memory_tool handler intentionally unset" in err

    # Disabled fleet: no file is written even on a lesson-worthy success.
    monkeypatch.setattr(mod.jira_dispatch, "add_comment", lambda *a, **k: None)
    mod._run_memory_writeback(
        _StubClient(),
        "OP-1681D",
        outcome="success",
        summary="lessons-learned: disabled fleet",
        area="backend",
    )
    assert not list(_fleet_root(tmp_path, mod).glob("lesson:*.md"))
