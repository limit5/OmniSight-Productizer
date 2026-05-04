"""W15.3 — Contract tests for ``backend.web.vite_error_prompt`` plus
the ``last_vite_error_banner`` keyword threaded through
:func:`backend.prompt_loader.build_system_prompt` and
:func:`backend.agents.nodes._specialist_node_factory`.

W15.1 shipped the wire shape + per-workspace ring buffer.  W15.2
shipped the projection that folds each ``ViteBuildError`` into a
single-line ``state.error_history`` entry shaped::

    vite[<phase>] <file>:<line>: <kind>: <message>

W15.3 (this row) renders the most recent such entry as a Chinese-
localised system-prompt banner so the agent's next turn opens with
a structured reminder of the last failure the plugin reported::

    上次 build 有 error: [<file>:<line>] [<message>]

§A — Drift guards (template literal, section header, byte cap, no-line
     and unknown tokens — pinned so the W15.4 pattern detector and the
     prompt-snapshot capture stay byte-stable across rows).
§B — :func:`extract_last_vite_error_from_history` parser
     (newest-to-oldest walk, prefix filter, file / line / message
     extraction, mixed-source history, degraded entries, message-body
     colon survival).
§C — :func:`format_vite_error_banner` rendering (template fill, byte
     cap, multi-byte codepoint safety).
§D — :func:`build_last_vite_error_banner` end-to-end (empty history,
     no-vite history, single-vite history, multi-vite picks newest,
     mixed tool+vite picks newest vite).
§E — :func:`backend.prompt_loader.build_system_prompt` integration
     (banner appears, empty banner is no-op, oversize truncated,
     section ordered before handoff and after clone-spec).
§F — :func:`backend.agents.nodes._specialist_node_factory` wiring
     (specialist node calls :func:`build_last_vite_error_banner` with
     ``state.error_history`` and forwards the result to
     ``build_system_prompt`` as ``last_vite_error_banner``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.web.vite_error_relay import (
    VITE_ERROR_HISTORY_NO_FILE_TOKEN,
    VITE_ERROR_HISTORY_UNKNOWN_TOKEN,
    format_vite_error_for_history,
)
from backend.web.vite_error_prompt import (
    MAX_VITE_ERROR_BANNER_BYTES,
    VITE_ERROR_BANNER_NO_LINE_TOKEN,
    VITE_ERROR_BANNER_SECTION_HEADER,
    VITE_ERROR_BANNER_TEMPLATE,
    VITE_ERROR_BANNER_UNKNOWN_TOKEN,
    build_last_vite_error_banner,
    extract_last_vite_error_from_history,
    format_vite_error_banner,
)
from backend.web_sandbox_vite_errors import (
    VITE_ERROR_PLUGIN_NAME,
    VITE_ERROR_PLUGIN_VERSION,
    WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
    ViteBuildError,
)


# ── Fixtures ────────────────────────────────────────────────────────


def _make_error(**overrides: Any) -> ViteBuildError:
    """Baseline :class:`ViteBuildError` so every §B / §D test can
    derive a single-field-difference variant without re-stating the
    whole shape."""

    base: dict[str, Any] = {
        "schema_version": WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
        "kind": "compile",
        "phase": "transform",
        "message": "Failed to parse module",
        "file": "src/App.tsx",
        "line": 42,
        "column": 7,
        "stack": None,
        "plugin": VITE_ERROR_PLUGIN_NAME,
        "plugin_version": VITE_ERROR_PLUGIN_VERSION,
        "occurred_at": 1714760400.123,
        "received_at": 1714760400.456,
    }
    base.update(overrides)
    return ViteBuildError(**base)


def _entry(**overrides: Any) -> str:
    """Build a W15.2-formatted history entry by routing through the
    real :func:`format_vite_error_for_history` so the parser tests
    exercise the contract end-to-end (parser + producer in lock-step)
    rather than against hand-rolled strings that could drift from the
    actual wire shape."""

    return format_vite_error_for_history(_make_error(**overrides))


# ────────────────────────────────────────────────────────────────────
# §A — Drift guards
# ────────────────────────────────────────────────────────────────────


def test_template_literal_is_chinese_localised_row_spec_verbatim() -> None:
    """The W15.3 row spec calls out the template literal verbatim
    ("Error 進 system prompt 模板：「上次 build 有 error: [file:line]
    [message]」").  Pinning the literal here means a translation drift
    fails red and the W15.4 pattern detector's grep target stays
    stable."""

    assert VITE_ERROR_BANNER_TEMPLATE == (
        "上次 build 有 error: [{file}:{line}] [{message}]"
    )


def test_template_uses_format_kwargs_only() -> None:
    """The renderer uses :py:meth:`str.format` with named kwargs.
    Pinning the substitution names protects callers from a
    positional-rewrite that would silently swap file / line /
    message (the W15.2 entry order is file→line→message and the
    banner mirrors that)."""

    rendered = VITE_ERROR_BANNER_TEMPLATE.format(
        file="F", line="L", message="M",
    )
    assert rendered == "上次 build 有 error: [F:L] [M]"


def test_section_header_is_stable() -> None:
    """The prompt-snapshot capture and the W15.4 pattern detector
    both grep for the section header.  Drift here churns every
    snapshot."""

    assert VITE_ERROR_BANNER_SECTION_HEADER == "# Recent Build Error (Vite)"


def test_unknown_token_is_lock_step_with_relay() -> None:
    """The banner reuses the W15.2 unknown-message marker so two
    surfaces (history entry + system prompt banner) speak the same
    opaque "we know nothing" sentinel."""

    assert VITE_ERROR_BANNER_UNKNOWN_TOKEN == VITE_ERROR_HISTORY_UNKNOWN_TOKEN
    assert VITE_ERROR_BANNER_UNKNOWN_TOKEN == "<unknown>"


def test_no_line_token_is_question_mark() -> None:
    """The banner mirrors the W15.2 entry's line slot — ``?`` when
    the JS plugin could not extract a line.  Pinned so the W15.4
    pattern bucketer can rely on this literal."""

    assert VITE_ERROR_BANNER_NO_LINE_TOKEN == "?"


def test_max_banner_bytes_is_bounded() -> None:
    """Banner cap must be (a) ≥ the smallest well-shaped banner so
    the renderer never trips the truncation branch on a normal entry,
    (b) bounded enough that the section + header stays well under
    the prompt's 4 KiB-ish per-section budget."""

    smallest_well_shaped = VITE_ERROR_BANNER_TEMPLATE.format(
        file="x", line="1", message="y",
    )
    assert MAX_VITE_ERROR_BANNER_BYTES >= len(
        smallest_well_shaped.encode("utf-8")
    )
    assert MAX_VITE_ERROR_BANNER_BYTES <= 4096


# ────────────────────────────────────────────────────────────────────
# §B — extract_last_vite_error_from_history
# ────────────────────────────────────────────────────────────────────


def test_extract_returns_none_for_empty_history() -> None:
    assert extract_last_vite_error_from_history([]) is None


def test_extract_returns_none_for_history_without_vite_entries() -> None:
    history = [
        "ToolError: read_file failed: ENOENT: no such file or directory",
        "ToolError: write_file failed: permission denied",
    ]
    assert extract_last_vite_error_from_history(history) is None


def test_extract_canonical_single_vite_entry() -> None:
    history = [_entry()]
    parts = extract_last_vite_error_from_history(history)
    assert parts == ("src/App.tsx", "42", "Failed to parse module")


def test_extract_picks_most_recent_when_multiple_vite_entries() -> None:
    history = [
        _entry(file="src/Old.tsx", line=1, message="old error"),
        _entry(file="src/Mid.tsx", line=2, message="mid error"),
        _entry(file="src/New.tsx", line=3, message="newest error"),
    ]
    parts = extract_last_vite_error_from_history(history)
    assert parts == ("src/New.tsx", "3", "newest error")


def test_extract_picks_most_recent_vite_when_mixed_with_tool_errors() -> None:
    history = [
        _entry(file="src/Old.tsx", line=1, message="old vite error"),
        "ToolError: read_file failed: ENOENT",
        _entry(file="src/Mid.tsx", line=2, message="mid vite error"),
        "ToolError: write_file failed: EACCES",
    ]
    parts = extract_last_vite_error_from_history(history)
    # Most recent VITE entry, not the most recent ANY entry.
    assert parts == ("src/Mid.tsx", "2", "mid vite error")


def test_extract_uses_no_file_token_when_file_blank() -> None:
    history = [_entry(file="")]
    parts = extract_last_vite_error_from_history(history)
    assert parts is not None
    file_token, _, _ = parts
    assert file_token == VITE_ERROR_HISTORY_NO_FILE_TOKEN


def test_extract_uses_no_line_token_when_line_missing() -> None:
    history = [_entry(line=None)]
    parts = extract_last_vite_error_from_history(history)
    assert parts is not None
    _, line_token, _ = parts
    assert line_token == VITE_ERROR_BANNER_NO_LINE_TOKEN


def test_extract_uses_unknown_token_when_message_blank() -> None:
    history = [_entry(message="   \n\t  ")]
    parts = extract_last_vite_error_from_history(history)
    assert parts is not None
    _, _, message_token = parts
    assert message_token == VITE_ERROR_BANNER_UNKNOWN_TOKEN


def test_extract_preserves_message_body_colons() -> None:
    """The W15.2 format separates fields with ``":"`` but the
    *message body* may legitimately contain colons (e.g. parser
    errors that quote a token).  The parser must rejoin everything
    after the third colon to recover the message verbatim."""

    history = [
        _entry(message="ParseError: Unexpected token ':' at column 17"),
    ]
    parts = extract_last_vite_error_from_history(history)
    assert parts is not None
    _, _, message = parts
    assert message == "ParseError: Unexpected token ':' at column 17"


def test_extract_skips_degraded_entry_and_continues_walk() -> None:
    """An entry that matches the prefix but lacks the expected colon
    shape (pathological filename truncation) must be skipped — the
    walk continues looking for an older well-shaped entry rather
    than collapsing into a no-banner state."""

    degraded = "vite[transform] xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    history = [
        _entry(file="src/Older.tsx", line=10, message="older but shaped"),
        degraded,
    ]
    parts = extract_last_vite_error_from_history(history)
    assert parts == ("src/Older.tsx", "10", "older but shaped")


def test_extract_ignores_non_string_entries_without_raising() -> None:
    """Malformed history (a sequence containing a non-str) must not
    crash extraction — ``GraphState.error_history`` is typed as
    ``list[str]`` but defence in depth keeps the prompt build from
    breaking on a bad upstream."""

    history: list[Any] = [None, 42, _entry()]
    parts = extract_last_vite_error_from_history(history)
    assert parts == ("src/App.tsx", "42", "Failed to parse module")


def test_extract_renders_all_allowed_phases() -> None:
    """All six W15.1 phases (config / buildStart / load / transform /
    hmr / client) must round-trip through the parser."""

    for phase in ("config", "buildStart", "load", "transform", "hmr", "client"):
        history = [_entry(phase=phase)]
        parts = extract_last_vite_error_from_history(history)
        assert parts == ("src/App.tsx", "42", "Failed to parse module"), (
            f"phase={phase} did not round-trip"
        )


# ────────────────────────────────────────────────────────────────────
# §C — format_vite_error_banner
# ────────────────────────────────────────────────────────────────────


def test_format_canonical() -> None:
    out = format_vite_error_banner("src/App.tsx", "42", "Failed to parse")
    assert out == "上次 build 有 error: [src/App.tsx:42] [Failed to parse]"


def test_format_with_no_file_token() -> None:
    out = format_vite_error_banner(
        VITE_ERROR_HISTORY_NO_FILE_TOKEN, "?", "runtime crash",
    )
    assert out == "上次 build 有 error: [<no-file>:?] [runtime crash]"


def test_format_with_unknown_message_token() -> None:
    out = format_vite_error_banner(
        "src/App.tsx", "42", VITE_ERROR_BANNER_UNKNOWN_TOKEN,
    )
    assert out == "上次 build 有 error: [src/App.tsx:42] [<unknown>]"


def test_format_truncates_oversize() -> None:
    huge_message = "x" * (MAX_VITE_ERROR_BANNER_BYTES * 2)
    out = format_vite_error_banner("src/App.tsx", "42", huge_message)
    assert len(out.encode("utf-8")) <= MAX_VITE_ERROR_BANNER_BYTES


def test_format_does_not_split_multibyte_codepoints() -> None:
    """Multi-byte chars (CJK + emoji) must truncate on a codepoint
    boundary so the rendered banner is still decodable."""

    emoji_message = "🟥" * 200  # 4-byte codepoints, easily busts the cap
    out = format_vite_error_banner("src/App.tsx", "42", emoji_message)
    # Round-tripping through utf-8 must not raise.
    assert out.encode("utf-8").decode("utf-8") == out


# ────────────────────────────────────────────────────────────────────
# §D — build_last_vite_error_banner end-to-end
# ────────────────────────────────────────────────────────────────────


def test_build_returns_empty_string_for_empty_history() -> None:
    assert build_last_vite_error_banner([]) == ""


def test_build_returns_empty_string_for_no_vite_entries() -> None:
    history = [
        "ToolError: read_file failed",
        "ToolError: timeout exceeded",
    ]
    assert build_last_vite_error_banner(history) == ""


def test_build_renders_canonical_banner_for_single_vite_entry() -> None:
    history = [_entry()]
    banner = build_last_vite_error_banner(history)
    assert banner == (
        "上次 build 有 error: [src/App.tsx:42] [Failed to parse module]"
    )


def test_build_picks_newest_vite_entry_in_mixed_history() -> None:
    history = [
        _entry(file="src/Old.tsx", line=1, message="old vite"),
        "ToolError: write_file failed",
        _entry(file="src/New.tsx", line=99, message="newest vite"),
        "ToolError: another tool error",
    ]
    banner = build_last_vite_error_banner(history)
    assert banner == "上次 build 有 error: [src/New.tsx:99] [newest vite]"


def test_build_handles_no_file_no_line_no_message_gracefully() -> None:
    """A pathological runtime error from the JS plugin (no source
    location, blank message) must still produce a structured banner
    rather than crash or emit garbage."""

    history = [_entry(file="", line=None, message="")]
    banner = build_last_vite_error_banner(history)
    assert banner == "上次 build 有 error: [<no-file>:?] [<unknown>]"


def test_build_starts_with_chinese_prefix() -> None:
    """The W15.4 pattern detector greps for the leading
    ``上次 build 有 error:`` substring.  Pinning here defends against
    a translation refactor that swaps the literal."""

    history = [_entry()]
    banner = build_last_vite_error_banner(history)
    assert banner.startswith("上次 build 有 error: ")


# ────────────────────────────────────────────────────────────────────
# §E — build_system_prompt integration
# ────────────────────────────────────────────────────────────────────


class TestBuildSystemPromptIntegration:

    def test_banner_section_appears_in_assembled_prompt(self) -> None:
        from backend.prompt_loader import build_system_prompt

        history = [_entry()]
        banner = build_last_vite_error_banner(history)
        prompt = build_system_prompt(
            model_name="",
            agent_type="general",
            sub_type="",
            last_vite_error_banner=banner,
        )
        assert VITE_ERROR_BANNER_SECTION_HEADER in prompt
        assert "上次 build 有 error: [src/App.tsx:42]" in prompt

    def test_empty_banner_is_no_op(self) -> None:
        from backend.prompt_loader import build_system_prompt

        prompt = build_system_prompt(
            model_name="",
            agent_type="general",
            sub_type="",
            last_vite_error_banner="",
        )
        assert VITE_ERROR_BANNER_SECTION_HEADER not in prompt
        assert "上次 build 有 error" not in prompt

    def test_oversize_banner_is_truncated(self) -> None:
        from backend.prompt_loader import build_system_prompt

        oversize = "x" * 4096
        prompt = build_system_prompt(
            model_name="",
            agent_type="general",
            sub_type="",
            last_vite_error_banner=oversize,
        )
        assert "[vite error banner truncated]" in prompt

    def test_banner_appears_before_handoff(self) -> None:
        from backend.prompt_loader import build_system_prompt

        prompt = build_system_prompt(
            model_name="",
            agent_type="general",
            sub_type="",
            handoff_context="prior agent context",
            last_vite_error_banner="上次 build 有 error: [a:1] [b]",
        )
        banner_idx = prompt.index(VITE_ERROR_BANNER_SECTION_HEADER)
        handoff_idx = prompt.index("# Previous Task Handoff")
        assert banner_idx < handoff_idx, (
            "banner must precede handoff so recency-bias lifts the "
            "build-error reminder"
        )

    def test_banner_not_emitted_when_last_vite_error_banner_unset(self) -> None:
        """Default value of the new kwarg must be empty so callers
        unaware of W15.3 (legacy callers, tests that don't pass the
        kwarg) get the pre-W15.3 prompt verbatim."""

        from backend.prompt_loader import build_system_prompt

        prompt = build_system_prompt(
            model_name="",
            agent_type="general",
            sub_type="",
        )
        assert VITE_ERROR_BANNER_SECTION_HEADER not in prompt


# ────────────────────────────────────────────────────────────────────
# §F — Specialist node wires last_vite_error_banner from state
# ────────────────────────────────────────────────────────────────────


class TestSpecialistNodeThreadsViteErrorBanner:

    def test_specialist_node_passes_last_vite_error_banner_to_build_system_prompt(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The specialist node must compute the banner from
        ``state.error_history`` and forward it to
        :func:`build_system_prompt` as the ``last_vite_error_banner``
        kwarg.  Pinned via a fake ``build_system_prompt`` that captures
        kwargs — same pattern as the W11.10 clone-spec wiring test in
        ``test_clone_spec_context.py``."""

        captured: dict[str, Any] = {}

        from backend.agents import nodes

        def _fake_build_system_prompt(**kwargs: Any) -> str:
            captured.update(kwargs)
            return "fake prompt"

        class _Resp:
            content = "hello"
            tool_calls: list[Any] = []

        class _FakeLLM:
            def invoke(self, _msgs: Any) -> Any:
                return _Resp()

        monkeypatch.setattr(nodes, "build_system_prompt", _fake_build_system_prompt)
        monkeypatch.setattr(nodes, "_get_llm", lambda **kwargs: _FakeLLM())
        monkeypatch.setattr(
            nodes,
            "_resolve_skill_loading_mode",
            lambda _x: "eager",
        )

        from backend.agents.state import GraphState

        history_entry = _entry(
            file="src/App.tsx", line=42, message="Failed to parse module",
        )
        state = GraphState(
            user_command="fix the build",
            routed_to="general",
            error_history=[history_entry],
        )

        node = nodes._specialist_node_factory("general")
        asyncio.run(node(state))

        assert "last_vite_error_banner" in captured
        assert captured["last_vite_error_banner"] == (
            "上次 build 有 error: [src/App.tsx:42] [Failed to parse module]"
        )

    def test_specialist_node_passes_empty_banner_when_no_vite_errors(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        from backend.agents import nodes

        def _fake_build_system_prompt(**kwargs: Any) -> str:
            captured.update(kwargs)
            return "fake prompt"

        class _Resp:
            content = "hello"
            tool_calls: list[Any] = []

        class _FakeLLM:
            def invoke(self, _msgs: Any) -> Any:
                return _Resp()

        monkeypatch.setattr(nodes, "build_system_prompt", _fake_build_system_prompt)
        monkeypatch.setattr(nodes, "_get_llm", lambda **kwargs: _FakeLLM())
        monkeypatch.setattr(
            nodes,
            "_resolve_skill_loading_mode",
            lambda _x: "eager",
        )

        from backend.agents.state import GraphState

        # error_history populated only with non-Vite entries; banner
        # must come back empty so build_system_prompt is a no-op.
        state = GraphState(
            user_command="do work",
            routed_to="general",
            error_history=["ToolError: not a vite error"],
        )

        node = nodes._specialist_node_factory("general")
        asyncio.run(node(state))

        assert captured.get("last_vite_error_banner", None) == ""

    def test_specialist_node_passes_empty_banner_for_empty_history(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        from backend.agents import nodes

        def _fake_build_system_prompt(**kwargs: Any) -> str:
            captured.update(kwargs)
            return "fake prompt"

        class _Resp:
            content = "hello"
            tool_calls: list[Any] = []

        class _FakeLLM:
            def invoke(self, _msgs: Any) -> Any:
                return _Resp()

        monkeypatch.setattr(nodes, "build_system_prompt", _fake_build_system_prompt)
        monkeypatch.setattr(nodes, "_get_llm", lambda **kwargs: _FakeLLM())
        monkeypatch.setattr(
            nodes,
            "_resolve_skill_loading_mode",
            lambda _x: "eager",
        )

        from backend.agents.state import GraphState

        state = GraphState(user_command="cold start", routed_to="general")

        node = nodes._specialist_node_factory("general")
        asyncio.run(node(state))

        assert captured.get("last_vite_error_banner", None) == ""
