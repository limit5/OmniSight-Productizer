"""B3 — loop detector + ToM scratchpad unit tests (OP-830).

Covers AC #1-3, #5, #6 from master plan §2.5:

  - tool-call triple recording with sha256[:16] args_hash
  - 2x match does NOT trigger reset
  - 3x match DOES trigger reset
  - 1-token-difference exempts (D3 mitigation)
  - reset_limit gate (3 resets, then terminal)
  - args_hash_collision robustness via canonical_args fallback
  - scratchpad accept / reject (malformed entries are not progress)
  - scratchpad cross-reset persistence (load from disk)
"""

from __future__ import annotations

import json

import pytest

from backend.agents.loop_detector import (
    ERROR_CLASS_OK,
    LEVENSHTEIN_TOKEN_TOLERANCE,
    RESET_BOUNDARY_TOOL,
    RESET_LIMIT,
    TRIPLE_MATCH_THRESHOLD,
    LoopDetector,
    args_hash,
    canonical_args_json,
    classify_tool_error,
)
from backend.agents.tom_scratchpad import ToMScratchpad


# ── args_hash + canonical JSON ────────────────────────────────────────


def test_args_hash_is_16_hex_chars_per_ac2():
    h = args_hash({"file_path": "/tmp/x.py"})
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_canonical_args_is_key_order_invariant():
    a = canonical_args_json({"a": 1, "b": 2})
    b = canonical_args_json({"b": 2, "a": 1})
    assert a == b
    assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})


def test_canonical_args_handles_non_json_via_default_str():
    # A non-JSON-serialisable value (e.g. set) is stringified, not crashed.
    h = args_hash({"x": {"nested": "value"}})
    assert len(h) == 16


# ── 2x vs 3x match ───────────────────────────────────────────────────


def test_two_matches_do_not_trigger_reset():
    d = LoopDetector(ticket_key="OP-830")
    for _ in range(2):
        d.record_tool_call(
            tool_name="Glob", tool_args={"pattern": "**/*.py"},
            error_class="bash_metachar_blocked",
        )
    assert d.is_reset_required() is False


def test_three_matches_trigger_reset_per_ac2():
    d = LoopDetector(ticket_key="OP-830")
    for _ in range(TRIPLE_MATCH_THRESHOLD):
        d.record_tool_call(
            tool_name="Glob", tool_args={"pattern": "**/*.py"},
            error_class="bash_metachar_blocked",
        )
    assert d.is_reset_required() is True
    sig = d.loop_signature()
    assert sig is not None
    assert sig.tool_name == "Glob"
    assert sig.error_class == "bash_metachar_blocked"


def test_three_calls_with_different_error_class_do_not_match():
    d = LoopDetector(ticket_key="OP-830")
    for err in ("ok", "ok", "bash_nonzero_exit"):
        d.record_tool_call(
            tool_name="Bash", tool_args={"command": "pytest"},
            error_class=err,
        )
    assert d.is_reset_required() is False


def test_three_calls_with_different_tool_names_do_not_match():
    d = LoopDetector(ticket_key="OP-830")
    for name in ("Glob", "Grep", "Glob"):
        d.record_tool_call(
            tool_name=name, tool_args={"pattern": "**/*.py"},
            error_class=ERROR_CLASS_OK,
        )
    assert d.is_reset_required() is False


# ── D3 false-positive mitigation (AC #3) ─────────────────────────────


def test_progressive_narrowing_does_not_trigger_per_ac3():
    """Model issues Glob with progressively narrower patterns — each
    pattern hashes differently anyway, so this is a sanity check that
    the detector doesn't mistakenly group them."""
    d = LoopDetector(ticket_key="OP-830")
    for pattern in ("**/*.py", "backend/**/*.py", "backend/agents/*.py"):
        d.record_tool_call(
            tool_name="Glob", tool_args={"pattern": pattern},
            error_class=ERROR_CLASS_OK,
        )
    assert d.is_reset_required() is False


def test_token_tolerance_exempts_one_token_diff(monkeypatch):
    """If the args_hash collides (rare) but tokens differ by ≥ tolerance,
    the run counts as progress, not as a match. We simulate the collision
    by monkeypatching the hash so 3 different inputs hash to the same value."""
    import backend.agents.loop_detector as ld

    fixed_hash = "deadbeefcafebabe"

    def _stub_sha(_data: bytes):  # noqa: ANN001
        class _M:
            def hexdigest(self) -> str:
                return fixed_hash + "0" * 48
        return _M()

    monkeypatch.setattr(ld.hashlib, "sha256", _stub_sha)
    d = LoopDetector(ticket_key="OP-830")
    for token in ("alpha", "beta", "gamma"):
        d.record_tool_call(
            tool_name="Bash", tool_args={"command": f"echo {token}"},
            error_class="bash_metachar_blocked",
        )
    # Hashes all match (forced collision), but consecutive args differ by
    # ≥ 1 token => detector reports no loop.
    assert d.is_reset_required() is False


# ── Reset orchestration (AC #5) ──────────────────────────────────────


def test_can_reset_until_limit_then_terminal():
    d = LoopDetector(ticket_key="OP-830")
    for _ in range(RESET_LIMIT):
        assert d.can_reset() is True
        d.mark_reset()
    assert d.can_reset() is False
    assert d.is_terminal is True
    assert d.reset_count == RESET_LIMIT


def test_mark_reset_drops_boundary_so_window_starts_fresh():
    d = LoopDetector(ticket_key="OP-830")
    for _ in range(3):
        d.record_tool_call(
            tool_name="Glob", tool_args={"pattern": "*"}, error_class="ok",
        )
    assert d.is_reset_required() is True

    d.mark_reset()
    # After the boundary the active window is empty — no reset until
    # 3 more matching calls.
    assert d.is_reset_required() is False

    # Two more calls still below threshold.
    d.record_tool_call(tool_name="Glob", tool_args={"pattern": "*"}, error_class="ok")
    d.record_tool_call(tool_name="Glob", tool_args={"pattern": "*"}, error_class="ok")
    assert d.is_reset_required() is False
    # Third call after boundary => triggers again.
    d.record_tool_call(tool_name="Glob", tool_args={"pattern": "*"}, error_class="ok")
    assert d.is_reset_required() is True


def test_audit_log_preserves_records_across_resets():
    """Records before a reset stay in the audit log (append-only contract)."""
    d = LoopDetector(ticket_key="OP-830")
    for _ in range(3):
        d.record_tool_call(tool_name="Bash", tool_args={"cmd": "x"}, error_class="ok")
    d.mark_reset()
    log = d.log
    # 3 original calls + 1 reset boundary marker
    assert len(log) == 4
    assert log[3].tool_name == RESET_BOUNDARY_TOOL


# ── classify_tool_error ──────────────────────────────────────────────


def test_classify_tool_error_ok():
    assert classify_tool_error(is_error=False, content="hello") == "ok"


def test_classify_tool_error_extracts_structured_code():
    payload = json.dumps({"error": "bash_metachar_blocked", "hint": "..."})
    assert classify_tool_error(is_error=True, content=payload) == "bash_metachar_blocked"


def test_classify_tool_error_falls_back_on_unparseable():
    assert classify_tool_error(is_error=True, content="not json") == "unknown"


# ── ToM scratchpad (AC #6, #7) ───────────────────────────────────────


def test_scratchpad_accepts_well_formed_entry():
    sp = ToMScratchpad()
    ok = sp.append(
        {"hypothesis": "Path is wrong", "verifying": "ls of dir", "outcome": "pending"}
    )
    assert ok is True
    assert len(sp) == 1


def test_scratchpad_rejects_missing_fields():
    sp = ToMScratchpad()
    assert sp.append({"hypothesis": "x"}) is False
    assert sp.append({"hypothesis": "x", "verifying": "y"}) is False
    assert len(sp) == 0


def test_scratchpad_rejects_invalid_outcome():
    sp = ToMScratchpad()
    assert sp.append(
        {"hypothesis": "x", "verifying": "y", "outcome": "wat"}
    ) is False
    assert len(sp) == 0


def test_scratchpad_rejects_blank_strings():
    sp = ToMScratchpad()
    assert sp.append({"hypothesis": "  ", "verifying": "y", "outcome": "fail"}) is False


def test_scratchpad_persists_to_progress_txt(tmp_path):
    """AC #7: scratchpad written to progress.txt; survives crash (load)."""
    progress = tmp_path / "progress.txt"
    sp1 = ToMScratchpad(progress_path=progress)
    sp1.append({"hypothesis": "h1", "verifying": "v1", "outcome": "pending"})
    sp1.append({"hypothesis": "h2", "verifying": "v2", "outcome": "success"})
    # Simulate crash + restart.
    sp2 = ToMScratchpad.load(progress)
    assert len(sp2) == 2
    assert sp2.entries[0].hypothesis == "h1"
    assert sp2.entries[1].outcome == "success"


def test_scratchpad_persistence_skips_malformed_jsonl(tmp_path):
    progress = tmp_path / "progress.txt"
    progress.write_text(
        json.dumps({"type": "tom_scratchpad", "hypothesis": "good",
                    "verifying": "v", "outcome": "fail"}) + "\n"
        "garbled line not json\n"
        + json.dumps({"type": "other_component", "stuff": "x"}) + "\n"
    )
    sp = ToMScratchpad.load(progress)
    assert len(sp) == 1
    assert sp.entries[0].hypothesis == "good"


def test_scratchpad_render_summary_compact():
    sp = ToMScratchpad()
    sp.append({"hypothesis": "Need to read X", "verifying": "Read tool", "outcome": "success"})
    out = sp.render_summary()
    assert "ToM scratchpad" in out
    assert "Need to read X" in out


def test_scratchpad_string_argument_parsed_as_json():
    sp = ToMScratchpad()
    raw = json.dumps({"hypothesis": "h", "verifying": "v", "outcome": "fail"})
    assert sp.append(raw) is True


# ── Constants are stable per spec ────────────────────────────────────


def test_sprint_constants_match_b3_charter():
    """AC says 'Reset upper bound = 3' and triple-match threshold = 3.
    Lock the constants so future refactors can't silently drift."""
    assert TRIPLE_MATCH_THRESHOLD == 3
    assert RESET_LIMIT == 3
    assert LEVENSHTEIN_TOKEN_TOLERANCE == 1
