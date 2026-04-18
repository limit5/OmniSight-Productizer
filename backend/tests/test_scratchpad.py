"""R3 (#309) — Scratchpad Memory Offload + Auto-Continuation tests.

Integration coverage for the happy path (10-turn save + reload),
crash-recovery (torn .md.tmp), auto-continuation stitching, and the
UI/HTTP surface (``/scratchpad/*`` router). The suite runs against the
real filesystem under ``tmp_path`` via the ``OMNISIGHT_SCRATCHPAD_ROOT``
env override so no test touches ``data/agents/``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from backend import events, scratchpad as sp


@pytest.fixture()
def isolated_scratchpad(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SCRATCHPAD_ROOT", str(tmp_path))
    sp.reset_for_tests()
    yield tmp_path
    sp.reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Markdown round-trip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMarkdown:

    def test_render_round_trips_through_parse(self):
        state = sp.ScratchpadState(
            agent_id="agent-x",
            current_task="Plan R3",
            progress="Scaffolded module",
            blockers="None",
            next_steps="Write tests",
            context_summary="Per-agent encrypted scratchpad",
            turn=3,
        )
        text = sp.render_markdown(state)
        assert "## Current Task" in text
        assert "Plan R3" in text
        assert "## Next Steps" in text
        back = sp.parse_markdown("agent-x", text)
        assert back.current_task == "Plan R3"
        assert back.progress == "Scaffolded module"
        assert back.context_summary == "Per-agent encrypted scratchpad"

    def test_render_handles_empty_sections(self):
        state = sp.ScratchpadState(agent_id="a")
        text = sp.render_markdown(state)
        back = sp.parse_markdown("a", text)
        # Empty sections become the placeholder, which we strip on parse.
        assert back.current_task == ""
        assert back.progress == ""

    def test_sections_count_only_counts_non_empty(self):
        state = sp.ScratchpadState(
            agent_id="a", current_task="x", progress="y",
            blockers="", next_steps="", context_summary="",
        )
        assert state.sections_count() == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Save / reload
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSaveReload:

    def test_save_creates_ciphertext_on_disk(self, isolated_scratchpad):
        state = sp.ScratchpadState(
            agent_id="alpha",
            current_task="Task body",
            progress="Halfway",
            turn=1,
        )
        result = sp.save(state, trigger="turn_interval", emit=False)
        assert result.size_bytes > 0
        on_disk = result.path.read_bytes()
        # Raw bytes should NOT contain the plaintext — either Fernet
        # ciphertext (base64) or the explicit plaintext sentinel.
        assert b"Task body" not in on_disk or on_disk.startswith(b"# PLAINTEXT-FALLBACK")

    def test_reload_roundtrip(self, isolated_scratchpad):
        state = sp.ScratchpadState(
            agent_id="alpha",
            current_task="Task body",
            progress="Halfway",
            blockers="DB slow",
            next_steps="Retry",
            context_summary="Long context",
            turn=4, total_turns=10,
        )
        sp.save(state, trigger="turn_interval", emit=False)
        back = sp.reload_latest("alpha")
        assert back is not None
        assert back.current_task == "Task body"
        assert back.turn == 4
        assert back.total_turns == 10
        assert back.trigger == "turn_interval"

    def test_reload_missing_agent_returns_none(self, isolated_scratchpad):
        assert sp.reload_latest("never-seen") is None

    def test_reload_recovers_from_torn_write(self, isolated_scratchpad):
        state = sp.ScratchpadState(agent_id="beta", current_task="x", turn=1)
        sp.save(state, emit=False)
        # Simulate a torn write: rename the main file to .tmp and delete it.
        main = sp.scratchpad_path("beta")
        tmp = main.with_suffix(".md.tmp")
        os.replace(main, tmp)
        assert not main.exists()
        back = sp.reload_latest("beta")
        assert back is not None
        assert back.current_task == "x"

    def test_invalid_agent_id_rejected(self, isolated_scratchpad):
        with pytest.raises(ValueError):
            sp.agent_dir("../escape")
        with pytest.raises(ValueError):
            sp.agent_dir("")

    def test_meta_json_written(self, isolated_scratchpad):
        state = sp.ScratchpadState(
            agent_id="meta-agent", current_task="t", turn=7, total_turns=10,
        )
        sp.save(state, trigger="tool_done", emit=False)
        meta = sp.read_meta("meta-agent")
        assert meta is not None
        assert meta["turn"] == 7
        assert meta["trigger"] == "tool_done"
        assert meta["total_turns"] == 10
        assert meta["size_bytes"] > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10-turn save loop + reload
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTenTurnLoop:

    def test_tracker_emits_every_interval(self, isolated_scratchpad):
        """Every 10 turns the tracker should signal a save."""
        tracker = sp.get_tracker("loopy", interval=10)
        saves: list[int] = []
        for t in range(1, 26):
            if tracker.note_turn():
                saves.append(t)
        # 10 & 20 → expected flushes; 25 is mid-cycle.
        assert saves == [10, 20]

    def test_ten_turn_integration_writes_scratchpad(self, isolated_scratchpad):
        """10 ReAct rounds → scratchpad is written and latest turn is 10."""
        tracker = sp.get_tracker("ten", interval=10)
        for t in range(1, 11):
            if tracker.note_turn():
                st = sp.ScratchpadState(
                    agent_id="ten",
                    current_task="long-running",
                    progress=f"turn {t}",
                    turn=t, total_turns=10,
                )
                sp.save(st, trigger="turn_interval", emit=False)
        back = sp.reload_latest("ten")
        assert back is not None
        assert back.turn == 10
        assert "turn 10" in back.progress

    def test_tool_done_and_subtask_switch_trigger_save(self, isolated_scratchpad):
        tracker = sp.get_tracker("sw", interval=10)
        # A tool_done always flushes, even mid-cycle.
        assert tracker.note_tool_done() is True
        # First subtask is a transition from None → "phase-1".
        assert tracker.note_subtask("phase-1") is True
        # Same subtask again is a no-op.
        assert tracker.note_subtask("phase-1") is False
        # New subtask flushes.
        assert tracker.note_subtask("phase-2") is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Archive / retain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestArchive:

    def test_archive_on_success_moves_file(self, isolated_scratchpad):
        state = sp.ScratchpadState(agent_id="done-agent", current_task="x", turn=1)
        sp.save(state, emit=False)
        assert sp.scratchpad_path("done-agent").exists()
        archived = sp.archive_on_success("done-agent")
        assert archived is not None
        assert archived.exists()
        assert not sp.scratchpad_path("done-agent").exists()
        items = sp.list_archive("done-agent")
        assert len(items) >= 1

    def test_retain_for_debug_keeps_active_file(self, isolated_scratchpad):
        state = sp.ScratchpadState(agent_id="broken", current_task="y", turn=2)
        sp.save(state, emit=False)
        copy = sp.retain_for_debug("broken", note="crash")
        assert copy is not None
        # Active scratchpad must still exist for post-mortem reload.
        assert sp.scratchpad_path("broken").exists()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auto-continuation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAutoContinuation:

    def test_is_truncated_detects_all_variants(self):
        for s in ("max_tokens", "length", "LENGTH", "max_output_tokens", "MAX_TOKENS"):
            assert sp.is_truncated(s)
        for s in ("end_turn", "stop", "", None):
            assert not sp.is_truncated(s)

    def test_stitches_two_rounds(self):
        def cont(_text):
            return (" continued.", "end_turn")
        ac = sp.AutoContinuation(max_rounds=3, provider="anthropic")
        outcome = ac.run(("First part.", "max_tokens"), cont, emit=False)
        assert outcome.rounds == 1
        assert outcome.reached_limit is False
        assert "First part." in outcome.text and "continued" in outcome.text

    def test_stops_at_max_rounds(self):
        def never_ends(_text):
            return (" more", "max_tokens")
        ac = sp.AutoContinuation(max_rounds=2, provider="openai")
        outcome = ac.run(("A", "max_tokens"), never_ends, emit=False)
        assert outcome.rounds == 2
        assert outcome.reached_limit is True
        assert outcome.text.count("more") == 2

    def test_no_continuation_when_first_call_ended(self):
        calls = {"n": 0}
        def should_not_run(_text):
            calls["n"] += 1
            return ("x", "end_turn")
        ac = sp.AutoContinuation()
        outcome = ac.run(("done.", "end_turn"), should_not_run, emit=False)
        assert outcome.rounds == 0
        assert calls["n"] == 0
        assert outcome.text == "done."

    def test_stitch_inserts_newline_after_punctuation(self):
        assert sp._stitch("Hello.", "World") == "Hello.\nWorld"
        assert sp._stitch("Hello ", "World") == "Hello World"
        assert sp._stitch("partial", "-word") == "partial-word"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UI summary + HTTP router
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUISummary:

    def test_ui_summary_none_without_save(self, isolated_scratchpad):
        assert sp.ui_summary("ghost") is None

    def test_ui_summary_fields(self, isolated_scratchpad):
        state = sp.ScratchpadState(
            agent_id="ui-agent",
            current_task="t", progress="p",
            turn=3, total_turns=10,
            subtask="phase-1",
        )
        sp.save(state, trigger="tool_done", emit=False)
        s = sp.ui_summary("ui-agent")
        assert s is not None
        assert s["turn"] == 3
        assert s["total_turns"] == 10
        assert s["sections_count"] >= 2
        assert s["subtask"] == "phase-1"
        assert s["trigger"] == "tool_done"
        assert s["recoverable"] is True
        assert s["age_seconds"] is not None

    def test_ui_summary_all_excludes_agents_without_scratchpads(self, isolated_scratchpad):
        sp.save(sp.ScratchpadState(agent_id="a1", current_task="x", turn=1), emit=False)
        all_ = sp.ui_summary_all()
        assert len(all_) == 1
        assert all_[0]["agent_id"] == "a1"


class TestRouter:

    def test_list_agents_returns_summary(self, isolated_scratchpad):
        from backend.routers import scratchpad as router

        sp.save(sp.ScratchpadState(agent_id="r-alpha", current_task="x", turn=2, total_turns=5), emit=False)
        out = asyncio.run(router.list_agents())
        assert len(out["agents"]) == 1
        assert out["agents"][0]["agent_id"] == "r-alpha"

    def test_get_summary_404_for_missing(self, isolated_scratchpad):
        from fastapi import HTTPException
        from backend.routers import scratchpad as router

        with pytest.raises(HTTPException) as exc:
            asyncio.run(router.get_summary("missing"))
        assert exc.value.status_code == 404

    def test_get_preview_returns_decrypted_markdown(self, isolated_scratchpad):
        from backend.routers import scratchpad as router

        sp.save(sp.ScratchpadState(agent_id="r-beta", current_task="view me", turn=1), emit=False)
        out = asyncio.run(router.get_preview("r-beta"))
        assert "view me" in out["markdown"]
        assert out["chars"] > 0

    def test_invalid_agent_id_is_400(self, isolated_scratchpad):
        from fastapi import HTTPException
        from backend.routers import scratchpad as router

        with pytest.raises(HTTPException) as exc:
            asyncio.run(router.get_summary("../bad"))
        assert exc.value.status_code == 400


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Crash-recovery end-to-end: save → clear memory → reload
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCrashRecovery:

    def test_crash_and_resume(self, isolated_scratchpad):
        """Full integration: 10 turns → simulate crash → new tracker →
        reload_latest picks up state and the agent can continue.
        """
        tracker = sp.get_tracker("phoenix", interval=10)
        for t in range(1, 11):
            if tracker.note_turn():
                sp.save(sp.ScratchpadState(
                    agent_id="phoenix",
                    current_task="long task",
                    progress=f"turn {t} done",
                    turn=t, total_turns=20,
                ), emit=False)

        # Simulate crash: drop in-memory caches.
        sp.reset_for_tests()

        back = sp.reload_latest("phoenix")
        assert back is not None
        assert back.turn == 10
        assert back.progress == "turn 10 done"

        # Agent resumes — tracker continues from turn 10.
        tracker2 = sp.get_tracker("phoenix", interval=10)
        # Counter starts at 0 again because tracker state is process-local,
        # but the persistent turn count is what matters for UI/metrics.
        assert isinstance(tracker2, sp.AutoSaveTracker)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSE event emission
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSSEEvents:

    def test_save_publishes_scratchpad_saved(self, isolated_scratchpad):
        captured: list[dict] = []

        def fake_publish(event_type, payload, **kwargs):
            captured.append({"event": event_type, "payload": payload})

        orig = events.bus.publish
        events.bus.publish = fake_publish  # type: ignore[assignment]
        try:
            sp.save(
                sp.ScratchpadState(agent_id="sse-a", current_task="t", turn=1),
                trigger="turn_interval",
                emit=True,
            )
        finally:
            events.bus.publish = orig  # type: ignore[assignment]

        saved = [c for c in captured if c["event"] == "agent.scratchpad.saved"]
        assert saved, f"expected agent.scratchpad.saved, got {[c['event'] for c in captured]}"
        p = saved[0]["payload"]
        assert p["agent_id"] == "sse-a"
        assert p["turn"] == 1
        assert p["trigger"] == "turn_interval"
        assert p["size_bytes"] > 0
        assert p["sections_count"] >= 1

    def test_auto_continuation_emits_per_round(self, isolated_scratchpad):
        captured: list[dict] = []

        def fake_publish(event_type, payload, **kwargs):
            captured.append({"event": event_type, "payload": payload})

        orig = events.bus.publish
        events.bus.publish = fake_publish  # type: ignore[assignment]
        try:
            def cont(_text):
                return (" extra.", "end_turn")
            sp.AutoContinuation(provider="anthropic").run(
                ("head.", "max_tokens"), cont, agent_id="sse-b", emit=True,
            )
        finally:
            events.bus.publish = orig  # type: ignore[assignment]

        conts = [c for c in captured if c["event"] == "agent.token_continuation"]
        assert len(conts) == 1
        assert conts[0]["payload"]["provider"] == "anthropic"
        assert conts[0]["payload"]["continuation_round"] == 1
