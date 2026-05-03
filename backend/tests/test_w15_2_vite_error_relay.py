"""W15.2 — Contract tests for ``backend.web.vite_error_relay``.

Locks the projection contract between
:class:`backend.web_sandbox_vite_errors.ViteBuildError` (W15.1 wire
shape) and ``GraphState.error_history`` (single-line ``list[str]``)
so W15.3's system-prompt template, W15.4's 3-strike retry budget,
and the existing ``error_check_node`` loop detector all see a
stable, byte-bounded entry shape.

§A — Drift guards (literals locked at module level).
§B — :func:`format_vite_error_for_history` projection rules
     (token order, null/empty fallbacks, message normalisation,
     byte cap, multi-byte codepoint safety).
§C — :func:`vite_errors_for_history` buffer drain (default limit,
     explicit limit, unknown workspace, ``None`` buffer fallback to
     per-worker default, validation).
§D — :func:`merge_vite_errors_into_history` cap behaviour
     (lock-step with ``error_check_node._ERROR_HISTORY_MAX``,
     order preservation, fresh-list semantics, validation).
§E — :func:`build_vite_error_state_patch` partial-state shape
     (empty patch when nothing to fold, GraphState integration,
     duck-typed state input).
§F — :func:`vite_error_history_signature` pattern stability
     (head extraction, message-body insensitivity, degraded entry
     fallback) — feeds W15.4's retry budget.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from backend.agents.state import GraphState
from backend.agents import nodes as agents_nodes
from backend.web import vite_error_relay as relay
from backend.web.vite_error_relay import (
    MAX_VITE_ERROR_HISTORY_ENTRIES,
    MAX_VITE_ERROR_HISTORY_LINE_BYTES,
    VITE_ERROR_HISTORY_KEY_PREFIX,
    VITE_ERROR_HISTORY_MAX,
    VITE_ERROR_HISTORY_NO_FILE_TOKEN,
    VITE_ERROR_HISTORY_UNKNOWN_TOKEN,
    build_vite_error_state_patch,
    format_vite_error_for_history,
    merge_vite_errors_into_history,
    vite_error_history_signature,
    vite_errors_for_history,
)
from backend.web_sandbox_vite_errors import (
    VITE_ERROR_ALLOWED_KINDS,
    VITE_ERROR_ALLOWED_PHASES,
    VITE_ERROR_PLUGIN_NAME,
    VITE_ERROR_PLUGIN_VERSION,
    WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
    ViteBuildError,
    ViteErrorBuffer,
    set_default_buffer_for_tests,
)


# ── Fixtures ────────────────────────────────────────────────────────


def _make_error(**overrides: Any) -> ViteBuildError:
    """Build a baseline :class:`ViteBuildError` with W15.1-valid
    field defaults; tests override one field at a time to keep the
    failure signal narrow."""

    base: dict[str, Any] = {
        "schema_version": WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
        "kind": "compile",
        "phase": "transform",
        "message": "Failed to parse module",
        "file": "src/App.tsx",
        "line": 42,
        "column": 7,
        "stack": "Error: Failed to parse module\n  at transform",
        "plugin": VITE_ERROR_PLUGIN_NAME,
        "plugin_version": VITE_ERROR_PLUGIN_VERSION,
        "occurred_at": 1714760400.123,
        "received_at": 1714760400.456,
    }
    base.update(overrides)
    return ViteBuildError(**base)


@pytest.fixture
def buffer() -> ViteErrorBuffer:
    return ViteErrorBuffer(capacity=8)


@pytest.fixture(autouse=True)
def _reset_default_buffer() -> Any:
    """Per-process default buffer must be fresh between tests so
    `vite_errors_for_history(None, ...)` calls (which fall back to
    the default) don't see stale entries from earlier tests."""

    set_default_buffer_for_tests(None)
    yield
    set_default_buffer_for_tests(None)


# ────────────────────────────────────────────────────────────────────
# §A — Drift guards
# ────────────────────────────────────────────────────────────────────


def test_history_max_locks_with_error_check_node() -> None:
    """W15.2 ``VITE_ERROR_HISTORY_MAX`` must equal the cap the
    existing self-healing loop applies to ``state.error_history``
    (``_ERROR_HISTORY_MAX = 50`` in
    ``backend/agents/nodes.py:940``).

    If a future refactor changes one without the other the merged
    history cap drifts and either Vite entries get dropped early or
    the LangGraph state grows past the loop-detector's window.
    """

    nodes_src = agents_nodes.error_check_node.__code__.co_consts
    assert 50 in nodes_src, (
        "expected `_ERROR_HISTORY_MAX = 50` literal in "
        "error_check_node — bump VITE_ERROR_HISTORY_MAX in lock-step "
        "if the node's cap moves"
    )
    assert VITE_ERROR_HISTORY_MAX == 50


def test_max_entries_default_does_not_blow_history_cap() -> None:
    """A single drain (default ``limit``) plus the existing tool
    history must not overflow the merged cap on an empty starting
    history.  10 + N tool-error entries ≤ 50 leaves headroom for
    N ≤ 40 — generous enough that W15.4's retry budget can rely on
    the cap not silently dropping its early-window entries."""

    assert MAX_VITE_ERROR_HISTORY_ENTRIES <= VITE_ERROR_HISTORY_MAX
    assert MAX_VITE_ERROR_HISTORY_ENTRIES > 0


def test_history_key_prefix_is_w15_3_grep_target() -> None:
    """W15.3's system-prompt template will lift the most recent
    Vite-source entry by prefix-matching ``vite[``.  If the prefix
    drifts the system prompt grep silently misses every entry."""

    assert VITE_ERROR_HISTORY_KEY_PREFIX == "vite["


def test_no_file_and_unknown_tokens_are_stable() -> None:
    """The two fallback tokens are part of the W15.4 retry-budget
    signature contract — two no-location runtime errors with the
    same message must collapse to the same bucket."""

    assert VITE_ERROR_HISTORY_NO_FILE_TOKEN == "<no-file>"
    assert VITE_ERROR_HISTORY_UNKNOWN_TOKEN == "<unknown>"


def test_max_line_bytes_is_bounded() -> None:
    """Per-line cap must be ≥ the head ceiling (prefix + phase +
    file + line + kind + 4 separators) so the smallest valid
    formatted entry never trips the pathological-degraded branch
    in :func:`format_vite_error_for_history` for a normal
    ``ViteBuildError``."""

    assert MAX_VITE_ERROR_HISTORY_LINE_BYTES >= 64
    # Soft upper bound — keep history entries well under the LLM
    # context budget so 50 entries plus tool errors fit.
    assert MAX_VITE_ERROR_HISTORY_LINE_BYTES <= 4096


# ────────────────────────────────────────────────────────────────────
# §B — format_vite_error_for_history
# ────────────────────────────────────────────────────────────────────


def test_format_canonical_shape() -> None:
    err = _make_error()
    assert format_vite_error_for_history(err) == (
        "vite[transform] src/App.tsx:42: compile: Failed to parse module"
    )


def test_format_uses_no_file_token_when_file_blank() -> None:
    err = _make_error(file="")
    out = format_vite_error_for_history(err)
    assert out.startswith(f"vite[transform] {VITE_ERROR_HISTORY_NO_FILE_TOKEN}:42:")


def test_format_uses_no_file_token_when_file_whitespace() -> None:
    err = _make_error(file="   ")
    out = format_vite_error_for_history(err)
    assert VITE_ERROR_HISTORY_NO_FILE_TOKEN in out


def test_format_uses_question_mark_when_line_missing() -> None:
    err = _make_error(line=None, file=None)
    out = format_vite_error_for_history(err)
    assert f"{VITE_ERROR_HISTORY_NO_FILE_TOKEN}:?:" in out


def test_format_collapses_message_newlines() -> None:
    err = _make_error(message="line1\nline2\tcontinued")
    out = format_vite_error_for_history(err)
    # Single-line guarantee — no newline / tab in output.
    assert "\n" not in out
    assert "\t" not in out
    # Multiple whitespace runs collapse to single spaces.
    assert "line1 line2 continued" in out


def test_format_substitutes_unknown_for_blank_message() -> None:
    err = _make_error(message="   \n\t  ")
    out = format_vite_error_for_history(err)
    assert out.endswith(VITE_ERROR_HISTORY_UNKNOWN_TOKEN)


def test_format_truncates_to_byte_cap() -> None:
    long_msg = "x" * (MAX_VITE_ERROR_HISTORY_LINE_BYTES * 2)
    err = _make_error(message=long_msg)
    out = format_vite_error_for_history(err)
    assert len(out.encode("utf-8")) <= MAX_VITE_ERROR_HISTORY_LINE_BYTES


def test_format_does_not_split_multibyte_codepoints() -> None:
    # 4-byte emoji repeated until the cap is exceeded; truncation
    # must land on a codepoint boundary so the resulting string is
    # decodable.
    emoji = "🟥"
    msg = emoji * 200
    err = _make_error(message=msg)
    out = format_vite_error_for_history(err)
    # Should decode as a normal string without surrogates (proxy
    # for "no codepoint was split mid-byte").
    assert isinstance(out, str)
    assert out.encode("utf-8").decode("utf-8") == out


def test_format_renders_all_allowed_phases() -> None:
    for phase in VITE_ERROR_ALLOWED_PHASES:
        err = _make_error(phase=phase)
        out = format_vite_error_for_history(err)
        assert out.startswith(f"vite[{phase}] ")


def test_format_renders_all_allowed_kinds() -> None:
    for kind in VITE_ERROR_ALLOWED_KINDS:
        err = _make_error(kind=kind)
        out = format_vite_error_for_history(err)
        assert f": {kind}: " in out


def test_format_rejects_non_vitebuilderror_input() -> None:
    with pytest.raises(TypeError):
        format_vite_error_for_history({"phase": "transform"})  # type: ignore[arg-type]


def test_format_pathological_filename_falls_back_to_degraded_head() -> None:
    """A 4 KiB filename (W15.1 caps `file` at 2 KiB so this is
    constructed directly via :class:`ViteBuildError` — the wire path
    cannot reach this in production) must not raise; it returns a
    degraded but still-prefixed line."""

    huge = "src/" + ("a" * MAX_VITE_ERROR_HISTORY_LINE_BYTES)
    err = _make_error(file=huge)
    out = format_vite_error_for_history(err)
    assert out.startswith(VITE_ERROR_HISTORY_KEY_PREFIX)
    assert len(out.encode("utf-8")) <= MAX_VITE_ERROR_HISTORY_LINE_BYTES


# ────────────────────────────────────────────────────────────────────
# §C — vite_errors_for_history (buffer drain)
# ────────────────────────────────────────────────────────────────────


def test_drain_returns_empty_for_unknown_workspace(buffer: ViteErrorBuffer) -> None:
    assert vite_errors_for_history(buffer, "nope") == []


def test_drain_returns_chronological_format(buffer: ViteErrorBuffer) -> None:
    e1 = _make_error(message="first")
    e2 = _make_error(message="second")
    buffer.record("ws", e1)
    buffer.record("ws", e2)
    out = vite_errors_for_history(buffer, "ws")
    assert out[0].endswith("first")
    assert out[1].endswith("second")


def test_drain_default_limit_caps_to_module_default(buffer: ViteErrorBuffer) -> None:
    bigger = ViteErrorBuffer(capacity=MAX_VITE_ERROR_HISTORY_ENTRIES * 3)
    for i in range(MAX_VITE_ERROR_HISTORY_ENTRIES * 3):
        bigger.record("ws", _make_error(message=f"err-{i:03d}"))
    out = vite_errors_for_history(bigger, "ws")
    assert len(out) == MAX_VITE_ERROR_HISTORY_ENTRIES
    # Should be the newest N (the buffer.recent walks oldest-first
    # within the slice — last entry is the most recent).
    last_idx = MAX_VITE_ERROR_HISTORY_ENTRIES * 3 - 1
    assert out[-1].endswith(f"err-{last_idx:03d}")


def test_drain_explicit_limit_overrides_default(buffer: ViteErrorBuffer) -> None:
    for i in range(5):
        buffer.record("ws", _make_error(message=f"err-{i}"))
    assert len(vite_errors_for_history(buffer, "ws", limit=3)) == 3
    assert len(vite_errors_for_history(buffer, "ws", limit=100)) == 5


def test_drain_zero_limit_returns_empty(buffer: ViteErrorBuffer) -> None:
    buffer.record("ws", _make_error())
    assert vite_errors_for_history(buffer, "ws", limit=0) == []


def test_drain_falls_back_to_default_buffer_when_buffer_none() -> None:
    from backend.web_sandbox_vite_errors import get_default_buffer

    default = get_default_buffer()
    default.record("ws", _make_error(message="from-default"))
    out = vite_errors_for_history(None, "ws")
    assert len(out) == 1
    assert out[0].endswith("from-default")


def test_drain_rejects_blank_workspace_id(buffer: ViteErrorBuffer) -> None:
    with pytest.raises(ValueError):
        vite_errors_for_history(buffer, "")


def test_drain_rejects_non_string_workspace_id(buffer: ViteErrorBuffer) -> None:
    with pytest.raises(ValueError):
        vite_errors_for_history(buffer, 42)  # type: ignore[arg-type]


def test_drain_rejects_negative_limit(buffer: ViteErrorBuffer) -> None:
    with pytest.raises(ValueError):
        vite_errors_for_history(buffer, "ws", limit=-1)


def test_drain_rejects_non_int_limit(buffer: ViteErrorBuffer) -> None:
    with pytest.raises(TypeError):
        vite_errors_for_history(buffer, "ws", limit="3")  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────
# §D — merge_vite_errors_into_history
# ────────────────────────────────────────────────────────────────────


def test_merge_appends_in_order() -> None:
    out = merge_vite_errors_into_history(
        ["tool-error: foo"], ["vite[transform] a.tsx:1: compile: bar"]
    )
    assert out == [
        "tool-error: foo",
        "vite[transform] a.tsx:1: compile: bar",
    ]


def test_merge_caps_at_default_max() -> None:
    existing = [f"old-{i}" for i in range(40)]
    new = [f"new-{i}" for i in range(20)]
    out = merge_vite_errors_into_history(existing, new)
    assert len(out) == VITE_ERROR_HISTORY_MAX
    # Newest entries survive — last 50 of the appended list.
    assert out[-1] == "new-19"
    # Oldest 10 of `existing` got evicted.
    assert out[0] == "old-10"


def test_merge_returns_fresh_list_does_not_mutate_inputs() -> None:
    existing = ["a"]
    formatted = ["b"]
    out = merge_vite_errors_into_history(existing, formatted)
    out.append("c")
    assert existing == ["a"]
    assert formatted == ["b"]


def test_merge_supports_iterable_inputs() -> None:
    out = merge_vite_errors_into_history(("x",), iter(["y", "z"]))
    assert out == ["x", "y", "z"]


def test_merge_explicit_max_entries_zero_returns_empty() -> None:
    assert merge_vite_errors_into_history(["a"], ["b"], max_entries=0) == []


def test_merge_rejects_negative_max_entries() -> None:
    with pytest.raises(ValueError):
        merge_vite_errors_into_history([], [], max_entries=-1)


def test_merge_rejects_non_string_entry() -> None:
    with pytest.raises(TypeError):
        merge_vite_errors_into_history([], [123])  # type: ignore[list-item]


# ────────────────────────────────────────────────────────────────────
# §E — build_vite_error_state_patch
# ────────────────────────────────────────────────────────────────────


def test_patch_empty_when_buffer_has_no_entries(buffer: ViteErrorBuffer) -> None:
    patch = build_vite_error_state_patch(
        GraphState(), buffer=buffer, workspace_id="empty-ws"
    )
    assert patch == {}


def test_patch_folds_into_graphstate_error_history(buffer: ViteErrorBuffer) -> None:
    buffer.record("ws", _make_error(message="boom"))
    state = GraphState(error_history=["existing-tool-error"])
    patch = build_vite_error_state_patch(state, buffer=buffer, workspace_id="ws")
    assert "error_history" in patch
    history = patch["error_history"]
    assert history[0] == "existing-tool-error"
    assert history[-1].startswith(VITE_ERROR_HISTORY_KEY_PREFIX)
    assert history[-1].endswith("boom")


def test_patch_does_not_mutate_input_state(buffer: ViteErrorBuffer) -> None:
    buffer.record("ws", _make_error(message="boom"))
    state = GraphState(error_history=["pre"])
    _ = build_vite_error_state_patch(state, buffer=buffer, workspace_id="ws")
    assert state.error_history == ["pre"]


def test_patch_caps_merged_history(buffer: ViteErrorBuffer) -> None:
    big_buffer = ViteErrorBuffer(capacity=VITE_ERROR_HISTORY_MAX * 2)
    for i in range(VITE_ERROR_HISTORY_MAX * 2):
        big_buffer.record("ws", _make_error(message=f"e-{i}"))
    existing = [f"old-{i}" for i in range(VITE_ERROR_HISTORY_MAX)]
    state = GraphState(error_history=existing)
    patch = build_vite_error_state_patch(
        state,
        buffer=big_buffer,
        workspace_id="ws",
        limit=VITE_ERROR_HISTORY_MAX * 2,
    )
    assert len(patch["error_history"]) == VITE_ERROR_HISTORY_MAX


def test_patch_accepts_duck_typed_state(buffer: ViteErrorBuffer) -> None:
    buffer.record("ws", _make_error(message="duck"))

    class FakeState:
        error_history = ["from-fake"]

    patch = build_vite_error_state_patch(FakeState(), buffer=buffer, workspace_id="ws")
    assert patch["error_history"][0] == "from-fake"
    assert patch["error_history"][-1].endswith("duck")


def test_patch_treats_missing_error_history_attr_as_empty(buffer: ViteErrorBuffer) -> None:
    buffer.record("ws", _make_error(message="solo"))

    class StateNoHistory:
        pass

    patch = build_vite_error_state_patch(
        StateNoHistory(), buffer=buffer, workspace_id="ws"
    )
    assert patch["error_history"][0].endswith("solo")
    assert len(patch["error_history"]) == 1


def test_patch_treats_non_iterable_history_as_empty(buffer: ViteErrorBuffer) -> None:
    buffer.record("ws", _make_error(message="defy"))

    class StateBadHistory:
        error_history = 123  # not iterable

    patch = build_vite_error_state_patch(
        StateBadHistory(), buffer=buffer, workspace_id="ws"
    )
    # Falls back to empty + appends the new entry.
    assert patch["error_history"] == [
        format_vite_error_for_history(_make_error(message="defy"))
    ]


def test_patch_rejects_blank_workspace_id(buffer: ViteErrorBuffer) -> None:
    with pytest.raises(ValueError):
        build_vite_error_state_patch(GraphState(), buffer=buffer, workspace_id="")


def test_patch_falls_back_to_default_buffer_when_buffer_none() -> None:
    from backend.web_sandbox_vite_errors import get_default_buffer

    get_default_buffer().record("ws", _make_error(message="default-buffer"))
    patch = build_vite_error_state_patch(
        GraphState(), buffer=None, workspace_id="ws"
    )
    assert patch["error_history"][-1].endswith("default-buffer")


# ────────────────────────────────────────────────────────────────────
# §F — vite_error_history_signature
# ────────────────────────────────────────────────────────────────────


def test_signature_preserves_head_only() -> None:
    formatted = [
        "vite[transform] a.tsx:1: compile: foo is not defined",
        "vite[transform] a.tsx:1: compile: 'foo' is not defined",
        "vite[hmr] b.tsx:?: runtime: undefined",
    ]
    sig = vite_error_history_signature(formatted)
    # Two messages with the same head collapse to the same head —
    # body is dropped, the W15.4 budget treats them as the same
    # pattern.
    assert sig[0] == sig[1]
    assert sig[2] != sig[0]
    # Each signature is hashable + a tuple.
    assert isinstance(sig, tuple)
    assert all(isinstance(x, str) for x in sig)


def test_signature_returns_tuple_for_counter_use() -> None:
    """W15.4's pattern detector wants a hashable signature so it
    can sit directly in a ``Counter``-based budget."""

    sig = vite_error_history_signature(["vite[hmr] a:1: runtime: x"])
    assert hash(sig) == hash(sig)


def test_signature_falls_back_to_full_entry_on_degraded_format() -> None:
    """An entry that does not match the W15.2 head shape (e.g. a
    pathological filename that exhausted the byte cap before the
    body) falls back to the entire entry as the bucket — two
    identical degraded entries still collapse to one bucket."""

    # Construct an entry with only one colon (no body, no kind).
    degraded = "vite[transform] foo"
    sig = vite_error_history_signature([degraded, degraded, "real:other:kind:body"])
    assert sig[0] == degraded
    assert sig[1] == degraded
    # Real entry still extracts its head correctly.
    assert sig[2] == "real:other:kind:"


def test_signature_empty_input_returns_empty_tuple() -> None:
    assert vite_error_history_signature([]) == ()


def test_signature_rejects_non_string_entry() -> None:
    with pytest.raises(TypeError):
        vite_error_history_signature([42])  # type: ignore[list-item]


# ────────────────────────────────────────────────────────────────────
# §G — Cross-section integration
# ────────────────────────────────────────────────────────────────────


def test_round_trip_buffer_to_state_to_signature(buffer: ViteErrorBuffer) -> None:
    """End-to-end: record three errors, drain, fold into state,
    then signature the merged history.  The signature of the
    Vite-source slice must contain three identical heads when the
    same compile error fires three times — exactly the input W15.4
    will use to fire the 3-strike escalation."""

    for _ in range(3):
        buffer.record(
            "ws-1",
            _make_error(message=f"varying-message-{time.time_ns()}"),
        )
    state = GraphState()
    patch = build_vite_error_state_patch(
        state, buffer=buffer, workspace_id="ws-1"
    )
    history = patch["error_history"]
    # All three were vite[transform] src/App.tsx:42: compile —
    # signature collapses them into one bucket.
    sig = vite_error_history_signature(history)
    assert len(sig) == 3
    assert sig[0] == sig[1] == sig[2]
    assert sig[0].startswith(VITE_ERROR_HISTORY_KEY_PREFIX)


def test_re_export_via_backend_web_package() -> None:
    """All five public callables and six constants must be
    re-exported via :mod:`backend.web` so callers (e.g. the future
    W15.3 system-prompt builder, W15.4 retry-budget hook) can
    import them from one place without reaching into
    ``backend.web.vite_error_relay``."""

    from backend import web as backend_web

    for name in (
        "MAX_VITE_ERROR_HISTORY_ENTRIES",
        "MAX_VITE_ERROR_HISTORY_LINE_BYTES",
        "VITE_ERROR_HISTORY_KEY_PREFIX",
        "VITE_ERROR_HISTORY_MAX",
        "VITE_ERROR_HISTORY_NO_FILE_TOKEN",
        "VITE_ERROR_HISTORY_UNKNOWN_TOKEN",
        "build_vite_error_state_patch",
        "format_vite_error_for_history",
        "merge_vite_errors_into_history",
        "vite_error_history_signature",
        "vite_errors_for_history",
    ):
        assert getattr(backend_web, name) is getattr(relay, name), (
            f"{name!r} re-export identity mismatch — `__all__` "
            "and the `from backend.web.vite_error_relay import …` "
            "block in backend/web/__init__.py must agree."
        )
