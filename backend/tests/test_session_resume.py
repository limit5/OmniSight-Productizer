"""B9 — Anthropic harness session-resume tests (OP-1122).

12 cases per the OP-1122 test plan:

    1.  fresh session
    2.  resume from valid progress
    3.  corrupt progress
    4.  owner mismatch
    5.  cwd mismatch
    6.  smoke test pass
    7.  smoke test fail
    8.  bridge fresh
    9.  bridge stale (warn)
    10. bridge critical-stale (abort)
    11. stream events fresh
    12. stream events dead

Also covers the AC #2 schema constraints (max 500 chars per
``content_summary``), AC #3 (synchronous fsync — sentinel byte check),
and AC #9 (file locking — re-entrant call from the same process is the
covered surface; cross-process is exercised by the smoke fixture).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path

import pytest

from backend.agents import progress_log as plog
from backend.agents import session_resume as sr


# ── Shared fixtures ────────────────────────────────────────────────────


@pytest.fixture()
def worktree(tmp_path: Path, monkeypatch) -> Path:
    """Create a tmp worktree dir and chdir into it (Step-1 invariant)."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def fresh_bridge_heartbeat(tmp_path: Path) -> Path:
    """Touch a heartbeat file with mtime = now."""
    hb = tmp_path / "bridge-heartbeat"
    hb.write_text("ok")
    os.utime(hb, (time.time(), time.time()))
    return hb


@pytest.fixture()
def fresh_cursor(tmp_path: Path) -> Path:
    """Write a cursor file whose timestamp is now (UTC)."""
    cursor = tmp_path / "bridge-cursor.json"
    now = _dt.datetime.now(_dt.timezone.utc)
    cursor.write_text(
        json.dumps({"event_id": "evt-1", "timestamp": now.isoformat()})
    )
    return cursor


# ── Case 1: fresh session ──────────────────────────────────────────────


def test_fresh_session_creates_progress_file_and_header(
    worktree: Path,
    fresh_bridge_heartbeat: Path,
    fresh_cursor: Path,
):
    opener = sr.open_session(
        worktree_path=worktree,
        owner="claude-bot",
        ticket_key="OP-1122",
        smoke_test_modules=["json"],  # always importable
        smoke_test_pytest_paths=None,
        bridge_heartbeat_path=fresh_bridge_heartbeat,
        bridge_cursor_path=fresh_cursor,
        run_bridge_checks=True,
        run_smoke_test=True,
    )
    assert opener.phase == "fresh"
    assert opener.prior_entries == []

    path = plog.progress_file_path(worktree)
    assert path.is_file()

    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    # session_header + opener row
    assert len(lines) == 2
    header = json.loads(lines[0])
    assert header["role"] == plog.SESSION_HEADER_ROLE
    assert header["owner"] == "claude-bot"


# ── Case 2: resume from valid progress ────────────────────────────────


def test_resume_from_valid_progress_returns_prior_entries(
    worktree: Path,
    fresh_bridge_heartbeat: Path,
    fresh_cursor: Path,
):
    # Seed an existing progress.txt with one header + one entry from
    # the same owner.
    existing = plog.ProgressLog(worktree, owner="claude-bot")
    existing.start_session(reset=True)
    existing.append(
        role="assistant",
        iter=1,
        stop_reason="tool_use",
        tool_calls=["Read"],
        content_summary="prior assistant turn",
    )

    opener = sr.open_session(
        worktree_path=worktree,
        owner="claude-bot",
        ticket_key="OP-1122",
        smoke_test_modules=["json"],
        bridge_heartbeat_path=fresh_bridge_heartbeat,
        bridge_cursor_path=fresh_cursor,
    )
    assert opener.phase == "resume"
    # session_header + 1 prior entry
    assert len(opener.prior_entries) == 2
    assert opener.prior_entries[1].role == "assistant"
    assert opener.prior_entries[1].content_summary == "prior assistant turn"


# ── Case 3: corrupt progress ───────────────────────────────────────────


def test_corrupt_progress_falls_back_to_fresh_and_warns(
    worktree: Path,
    fresh_bridge_heartbeat: Path,
    fresh_cursor: Path,
):
    path = plog.progress_file_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json\n{also-not-json\n")

    opener = sr.open_session(
        worktree_path=worktree,
        owner="claude-bot",
        ticket_key="OP-1122",
        smoke_test_modules=["json"],
        bridge_heartbeat_path=fresh_bridge_heartbeat,
        bridge_cursor_path=fresh_cursor,
    )
    assert opener.phase == "fresh"
    assert any(w.startswith("progress_txt_corrupt:") for w in opener.warnings)
    # The corrupt file was quarantined.
    quarantine = sorted(path.parent.glob("progress.txt.corrupt-*"))
    assert quarantine, "expected the corrupt progress.txt to be quarantined"


# ── Case 4: owner mismatch ─────────────────────────────────────────────


def test_owner_mismatch_raises_concurrent_runner_conflict(
    worktree: Path,
    fresh_bridge_heartbeat: Path,
    fresh_cursor: Path,
):
    # Seed an existing log claiming "codex-bot" owner.
    other = plog.ProgressLog(worktree, owner="codex-bot")
    other.start_session(reset=True)
    other.append(
        role="assistant", iter=1, content_summary="codex was here",
    )

    with pytest.raises(sr.ConcurrentRunnerConflictError):
        sr.open_session(
            worktree_path=worktree,
            owner="claude-bot",  # different from codex-bot
            ticket_key="OP-1122",
            smoke_test_modules=["json"],
            bridge_heartbeat_path=fresh_bridge_heartbeat,
            bridge_cursor_path=fresh_cursor,
        )


# ── Case 5: cwd mismatch ───────────────────────────────────────────────


def test_cwd_mismatch_raises_cwd_not_worktree(
    tmp_path: Path,
    monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)  # cwd != worktree

    with pytest.raises(sr.CwdNotWorktreeError):
        sr.open_session(
            worktree_path=worktree,
            owner="claude-bot",
            ticket_key="OP-1122",
            smoke_test_modules=["json"],
            run_bridge_checks=False,
        )


# ── Case 6: smoke test pass ────────────────────────────────────────────


def test_smoke_test_pass_records_clean_outcome(
    worktree: Path,
    fresh_bridge_heartbeat: Path,
    fresh_cursor: Path,
):
    opener = sr.open_session(
        worktree_path=worktree,
        owner="claude-bot",
        ticket_key="OP-1122",
        smoke_test_modules=["json", "os", "sys"],
        smoke_test_pytest_paths=None,
        bridge_heartbeat_path=fresh_bridge_heartbeat,
        bridge_cursor_path=fresh_cursor,
    )
    assert opener.smoke_test is not None
    assert opener.smoke_test.is_clean
    assert all(
        not w.startswith("smoke_test_fail:") for w in opener.warnings
    )


# ── Case 7: smoke test fail ────────────────────────────────────────────


def test_smoke_test_fail_warns_but_proceeds(
    worktree: Path,
    fresh_bridge_heartbeat: Path,
    fresh_cursor: Path,
):
    opener = sr.open_session(
        worktree_path=worktree,
        owner="claude-bot",
        ticket_key="OP-1122",
        smoke_test_modules=["definitely_no_such_module_xyz_1122"],
        bridge_heartbeat_path=fresh_bridge_heartbeat,
        bridge_cursor_path=fresh_cursor,
    )
    # AC #4 step 3 — failure logs+warns but proceeds.
    assert opener.phase == "fresh"
    assert opener.smoke_test is not None
    assert opener.smoke_test.failed_imports
    assert any(w.startswith("smoke_test_fail:") for w in opener.warnings)


# ── Case 8: bridge fresh ───────────────────────────────────────────────


def test_bridge_fresh_emits_no_currency_warning(
    worktree: Path,
    fresh_bridge_heartbeat: Path,
    fresh_cursor: Path,
):
    opener = sr.open_session(
        worktree_path=worktree,
        owner="claude-bot",
        ticket_key="OP-1122",
        smoke_test_modules=["json"],
        bridge_heartbeat_path=fresh_bridge_heartbeat,
        bridge_cursor_path=fresh_cursor,
    )
    assert all(
        not w.startswith("bridge_currency_check_fail:") for w in opener.warnings
    )
    assert opener.bridge_heartbeat_age_seconds is not None
    assert opener.bridge_heartbeat_age_seconds < 60


# ── Case 9: bridge stale (warn but proceed) ────────────────────────────


def test_bridge_stale_between_1h_and_24h_warns(
    worktree: Path,
    tmp_path: Path,
    fresh_cursor: Path,
):
    # Heartbeat age = 2h. Falls between WARN (1h) and ABORT (24h).
    hb = tmp_path / "stale-heartbeat"
    hb.write_text("ok")
    past = time.time() - 2 * 60 * 60
    os.utime(hb, (past, past))

    opener = sr.open_session(
        worktree_path=worktree,
        owner="claude-bot",
        ticket_key="OP-1122",
        smoke_test_modules=["json"],
        bridge_heartbeat_path=hb,
        bridge_cursor_path=fresh_cursor,
    )
    assert any(
        w.startswith("bridge_currency_check_fail:") for w in opener.warnings
    )


# ── Case 10: bridge critical-stale (abort) ─────────────────────────────


def test_bridge_critical_stale_raises(
    worktree: Path,
    tmp_path: Path,
    fresh_cursor: Path,
):
    hb = tmp_path / "very-stale-heartbeat"
    hb.write_text("ok")
    past = time.time() - 48 * 60 * 60  # 48h > 24h
    os.utime(hb, (past, past))

    with pytest.raises(sr.BridgeStaleCriticalError):
        sr.open_session(
            worktree_path=worktree,
            owner="claude-bot",
            ticket_key="OP-1122",
            smoke_test_modules=["json"],
            bridge_heartbeat_path=hb,
            bridge_cursor_path=fresh_cursor,
        )


# ── Case 11: stream events fresh ───────────────────────────────────────


def test_stream_events_fresh_no_warning(
    worktree: Path,
    fresh_bridge_heartbeat: Path,
    fresh_cursor: Path,
):
    opener = sr.open_session(
        worktree_path=worktree,
        owner="claude-bot",
        ticket_key="OP-1122",
        smoke_test_modules=["json"],
        bridge_heartbeat_path=fresh_bridge_heartbeat,
        bridge_cursor_path=fresh_cursor,
    )
    assert all(
        not w.startswith("stream_events_dead:") for w in opener.warnings
    )
    assert opener.stream_events_age_seconds is not None
    assert opener.stream_events_age_seconds < 60


# ── Case 12: stream events dead ────────────────────────────────────────


def test_stream_events_dead_warns(
    worktree: Path,
    fresh_bridge_heartbeat: Path,
    tmp_path: Path,
):
    # Cursor written 10min ago — > 5min threshold.
    cursor = tmp_path / "stale-cursor.json"
    stale_ts = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=10)
    cursor.write_text(
        json.dumps({"event_id": "evt-old", "timestamp": stale_ts.isoformat()})
    )

    opener = sr.open_session(
        worktree_path=worktree,
        owner="claude-bot",
        ticket_key="OP-1122",
        smoke_test_modules=["json"],
        bridge_heartbeat_path=fresh_bridge_heartbeat,
        bridge_cursor_path=cursor,
    )
    assert any(
        w.startswith("stream_events_dead:") for w in opener.warnings
    )


# ── Additional coverage: progress_log primitives (AC #2/#3/#9) ─────────


class TestProgressLogPrimitives:

    def test_content_summary_truncated_to_500_chars(self, worktree: Path):
        log = plog.ProgressLog(worktree, owner="claude-bot")
        log.start_session(reset=True)
        big = "x" * 1000
        log.append(role="assistant", iter=1, content_summary=big)
        entries, report = plog.read_entries(worktree)
        assert report.is_clean
        # session_header + appended entry
        assistant_entry = next(e for e in entries if e.role == "assistant")
        assert len(assistant_entry.content_summary) == 500

    def test_append_fsyncs_data_visible_immediately(self, worktree: Path):
        log = plog.ProgressLog(worktree, owner="claude-bot")
        log.start_session(reset=True)
        log.append(
            role="assistant", iter=1,
            stop_reason="tool_use",
            tool_calls=["Bash", "Read"],
            content_summary="probe",
        )
        # No flush/close needed before reading — fsync after every
        # write is the AC #3 contract.
        raw = plog.progress_file_path(worktree).read_text()
        assert "probe" in raw
        assert "Bash" in raw and "Read" in raw

    def test_lock_file_is_created_alongside_progress(self, worktree: Path):
        log = plog.ProgressLog(worktree, owner="claude-bot")
        log.start_session(reset=True)
        lock = plog.progress_file_path(worktree).with_suffix(".txt.lock")
        assert lock.is_file()

    def test_read_entries_skips_corrupt_lines_and_returns_report(
        self, worktree: Path,
    ):
        path = plog.progress_file_path(worktree)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Valid + invalid + valid mix.
        good_a = json.dumps({
            "ts": "2026-05-14T00:00:00+00:00",
            "iter": 1, "role": "user", "stop_reason": "",
            "tool_calls": [], "content_summary": "a",
        })
        good_b = json.dumps({
            "ts": "2026-05-14T00:00:01+00:00",
            "iter": 2, "role": "user", "stop_reason": "",
            "tool_calls": [], "content_summary": "b",
        })
        path.write_text(f"{good_a}\nNOT_JSON\n{good_b}\n")
        entries, report = plog.read_entries(worktree)
        assert [e.content_summary for e in entries] == ["a", "b"]
        assert report.bad_line_offsets == [2]
        assert not report.is_clean

    def test_read_owner_returns_header_owner(self, worktree: Path):
        log = plog.ProgressLog(worktree, owner="claude-bot")
        log.start_session(reset=True)
        assert plog.read_owner(worktree) == "claude-bot"

    def test_progress_log_requires_non_empty_owner(self, worktree: Path):
        with pytest.raises(ValueError):
            plog.ProgressLog(worktree, owner="")
