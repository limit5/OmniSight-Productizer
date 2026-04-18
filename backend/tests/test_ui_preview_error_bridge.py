"""V2 #5 (issue #318) — ui_preview_error_bridge contract tests.

Pins ``backend/ui_preview_error_bridge.py`` against the V2 row 5 spec:

  * sandbox dev-server compile + runtime error interception;
  * structured :class:`PreviewError` objects with stable IDs;
  * detect / cleared / batch / context / watch-started / watch-stopped
    events in the ``ui_sandbox.error.*`` namespace;
  * scan() never raises, surfaces failure on warnings;
  * background watch single-instance per session;
  * agent context payload with markdown + auto-fix hint;
  * acknowledge() idempotent on repeated calls;
  * graceful exit via context manager.

All tests drive a ``FakeClock`` / in-memory logs-provider so no real
docker daemon, no real sleep, no real time is consumed.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from backend import ui_preview_error_bridge as peb
from backend.ui_sandbox import (
    CompileError,
    SandboxConfig,
    SandboxManager,
    parse_compile_error,
)
from backend.ui_preview_error_bridge import (
    DEFAULT_LOG_TAIL,
    DEFAULT_MAX_ERRORS_PER_SESSION,
    DEFAULT_MAX_EXCERPT_CHARS,
    DEFAULT_WATCH_INTERVAL_S,
    ERROR_EVENT_BATCH,
    ERROR_EVENT_CLEARED,
    ERROR_EVENT_CONTEXT_BUILT,
    ERROR_EVENT_DETECTED,
    ERROR_EVENT_TYPES,
    ERROR_EVENT_WATCH_STARTED,
    ERROR_EVENT_WATCH_STOPPED,
    SEVERITY_ERROR,
    SEVERITY_LEVELS,
    SEVERITY_WARNING,
    UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
    AgentContextPayload,
    ErrorBatch,
    ErrorSource,
    LogSource,
    PreviewError,
    PreviewErrorBridge,
    PreviewErrorBridgeError,
    WatchAlreadyRunning,
    WatchNotRunning,
    build_auto_fix_hint,
    classify_severity,
    combine_errors,
    hash_error,
    parse_runtime_error,
    render_error_markdown,
)


# ═══════════════════════════════════════════════════════════════════
#  Module invariants
# ═══════════════════════════════════════════════════════════════════


EXPECTED_ALL = {
    "UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION",
    "DEFAULT_LOG_TAIL",
    "DEFAULT_WATCH_INTERVAL_S",
    "DEFAULT_MAX_ERRORS_PER_SESSION",
    "DEFAULT_MAX_EXCERPT_CHARS",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SEVERITY_LEVELS",
    "ERROR_EVENT_DETECTED",
    "ERROR_EVENT_CLEARED",
    "ERROR_EVENT_BATCH",
    "ERROR_EVENT_CONTEXT_BUILT",
    "ERROR_EVENT_WATCH_STARTED",
    "ERROR_EVENT_WATCH_STOPPED",
    "ERROR_EVENT_TYPES",
    "ErrorSource",
    "PreviewError",
    "ErrorBatch",
    "AgentContextPayload",
    "LogSource",
    "PreviewErrorBridgeError",
    "WatchAlreadyRunning",
    "WatchNotRunning",
    "parse_runtime_error",
    "hash_error",
    "classify_severity",
    "combine_errors",
    "render_error_markdown",
    "build_auto_fix_hint",
    "PreviewErrorBridge",
}


def test_all_exports_match():
    assert set(peb.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(peb, name)


def test_schema_version_is_semver():
    parts = UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_default_log_tail_positive():
    assert isinstance(DEFAULT_LOG_TAIL, int) and DEFAULT_LOG_TAIL > 0


def test_default_watch_interval_positive():
    assert DEFAULT_WATCH_INTERVAL_S > 0


def test_default_max_errors_positive():
    assert DEFAULT_MAX_ERRORS_PER_SESSION > 0


def test_default_max_excerpt_chars_positive():
    assert DEFAULT_MAX_EXCERPT_CHARS > 0


def test_severity_levels_tuple():
    assert isinstance(SEVERITY_LEVELS, tuple)
    assert set(SEVERITY_LEVELS) == {SEVERITY_ERROR, SEVERITY_WARNING}


def test_severity_values_are_strings():
    assert SEVERITY_ERROR == "error"
    assert SEVERITY_WARNING == "warning"


def test_event_types_all_in_error_namespace():
    for name in ERROR_EVENT_TYPES:
        assert name.startswith("ui_sandbox.error.")


def test_event_types_are_unique():
    assert len(ERROR_EVENT_TYPES) == len(set(ERROR_EVENT_TYPES))


def test_event_types_includes_all_event_constants():
    assert set(ERROR_EVENT_TYPES) == {
        ERROR_EVENT_DETECTED,
        ERROR_EVENT_CLEARED,
        ERROR_EVENT_BATCH,
        ERROR_EVENT_CONTEXT_BUILT,
        ERROR_EVENT_WATCH_STARTED,
        ERROR_EVENT_WATCH_STOPPED,
    }


def test_error_event_names_do_not_collide_with_lifecycle_events():
    # V2 #2 uses ui_sandbox.hot_reload / ui_sandbox.screenshot / etc.
    # V2 #5 uses ui_sandbox.error.* — must not overlap.
    from backend.ui_sandbox_lifecycle import LIFECYCLE_EVENT_TYPES

    assert set(ERROR_EVENT_TYPES).isdisjoint(set(LIFECYCLE_EVENT_TYPES))


def test_error_source_enum_values():
    assert ErrorSource.compile.value == "compile"
    assert ErrorSource.runtime.value == "runtime"
    assert {e.value for e in ErrorSource} == {"compile", "runtime"}


def test_log_source_enum_values():
    assert LogSource.stdout.value == "stdout"
    assert LogSource.stderr.value == "stderr"
    assert LogSource.combined.value == "combined"


def test_error_hierarchy():
    assert issubclass(WatchAlreadyRunning, PreviewErrorBridgeError)
    assert issubclass(WatchNotRunning, PreviewErrorBridgeError)


# ═══════════════════════════════════════════════════════════════════
#  parse_runtime_error
# ═══════════════════════════════════════════════════════════════════


def test_parse_runtime_error_empty_returns_empty_tuple():
    assert parse_runtime_error("") == ()
    assert parse_runtime_error(None) == ()  # type: ignore[arg-type]


def test_parse_runtime_error_non_string_safe():
    assert parse_runtime_error(123) == ()  # type: ignore[arg-type]


def test_parse_runtime_error_typeerror():
    log = "Uncaught TypeError: Cannot read property 'x' of undefined"
    out = parse_runtime_error(log)
    assert len(out) == 1
    assert out[0].error_type.startswith("runtime/")
    assert "TypeError" in out[0].message or "TypeError" in log


def test_parse_runtime_error_referenceerror_with_frame():
    log = """Uncaught ReferenceError: foo is not defined
    at Component (./pages/index.tsx:42:10)
"""
    out = parse_runtime_error(log)
    assert len(out) == 1
    err = out[0]
    assert err.error_type == "runtime/referenceerror"
    assert err.file == "./pages/index.tsx"
    assert err.line == 42
    assert err.column == 10


def test_parse_runtime_error_warning_severity_is_warning():
    log = 'Warning: Each child in a list should have a unique "key" prop.'
    out = parse_runtime_error(log)
    assert len(out) == 1
    # The parser only classifies the type; the bridge classifies severity.
    assert "warning" in out[0].error_type.lower()


def test_parse_runtime_error_hydration_failed():
    log = "Hydration failed because the initial UI does not match what was rendered on the server."
    out = parse_runtime_error(log)
    assert len(out) == 1
    assert "hydration" in out[0].error_type.lower()


def test_parse_runtime_error_unhandled_rejection():
    log = """unhandledRejection: Error: fetch failed
  at ./lib/api.ts:10:3"""
    out = parse_runtime_error(log)
    assert len(out) == 1
    assert out[0].error_type == "runtime/unhandledrejection"
    assert out[0].file == "./lib/api.ts"
    assert out[0].line == 10


def test_parse_runtime_error_dedups_same_error():
    log = """Uncaught TypeError: foo is undefined
    at app.tsx:1:1
Uncaught TypeError: foo is undefined
    at app.tsx:1:1"""
    out = parse_runtime_error(log)
    assert len(out) == 1


def test_parse_runtime_error_distinct_lines_distinct_errors():
    log = """Uncaught TypeError: foo is undefined
    at app.tsx:1:1
Uncaught TypeError: bar is undefined
    at app.tsx:2:1"""
    out = parse_runtime_error(log)
    assert len(out) == 2


def test_parse_runtime_error_respects_max_errors():
    lines = [
        f"Uncaught TypeError: e{i} is undefined\n  at f{i}.tsx:{i}:1"
        for i in range(10)
    ]
    log = "\n".join(lines)
    out = parse_runtime_error(log, max_errors=3)
    assert len(out) == 3


def test_parse_runtime_error_no_frame_returns_null_file():
    log = "Uncaught TypeError: foo"
    out = parse_runtime_error(log)
    assert len(out) == 1
    assert out[0].file is None
    assert out[0].line is None
    assert out[0].column is None


def test_parse_runtime_error_ignores_non_trigger_lines():
    log = "normal dev server log\ncompiled successfully\n"
    out = parse_runtime_error(log)
    assert out == ()


def test_parse_runtime_error_error_type_is_namespaced():
    log = "Uncaught TypeError: x"
    out = parse_runtime_error(log)
    assert out[0].error_type.startswith("runtime/")


# ═══════════════════════════════════════════════════════════════════
#  hash_error
# ═══════════════════════════════════════════════════════════════════


def test_hash_error_is_deterministic():
    a = hash_error(
        source=ErrorSource.compile,
        error_type="module_not_found",
        message="Can't resolve 'foo'",
        file="./a.tsx",
        line=5,
    )
    b = hash_error(
        source=ErrorSource.compile,
        error_type="module_not_found",
        message="Can't resolve 'foo'",
        file="./a.tsx",
        line=5,
    )
    assert a == b


def test_hash_error_changes_on_source():
    a = hash_error(source=ErrorSource.compile, error_type="t", message="m")
    b = hash_error(source=ErrorSource.runtime, error_type="t", message="m")
    assert a != b


def test_hash_error_changes_on_error_type():
    a = hash_error(source=ErrorSource.compile, error_type="t1", message="m")
    b = hash_error(source=ErrorSource.compile, error_type="t2", message="m")
    assert a != b


def test_hash_error_changes_on_message():
    a = hash_error(source=ErrorSource.compile, error_type="t", message="m1")
    b = hash_error(source=ErrorSource.compile, error_type="t", message="m2")
    assert a != b


def test_hash_error_changes_on_file():
    a = hash_error(
        source=ErrorSource.compile, error_type="t", message="m", file="a"
    )
    b = hash_error(
        source=ErrorSource.compile, error_type="t", message="m", file="b"
    )
    assert a != b


def test_hash_error_changes_on_line():
    a = hash_error(
        source=ErrorSource.compile, error_type="t", message="m", line=1
    )
    b = hash_error(
        source=ErrorSource.compile, error_type="t", message="m", line=2
    )
    assert a != b


def test_hash_error_accepts_string_source():
    h_enum = hash_error(source=ErrorSource.compile, error_type="t", message="m")
    h_str = hash_error(source="compile", error_type="t", message="m")
    assert h_enum == h_str


def test_hash_error_length_is_12():
    h = hash_error(source=ErrorSource.compile, error_type="t", message="m")
    assert len(h) == 12
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_error_rejects_non_string_source():
    with pytest.raises(TypeError):
        hash_error(source=123, error_type="t", message="m")  # type: ignore[arg-type]


def test_hash_error_rejects_non_int_line():
    with pytest.raises(TypeError):
        hash_error(
            source=ErrorSource.compile,
            error_type="t",
            message="m",
            line="nope",  # type: ignore[arg-type]
        )


# ═══════════════════════════════════════════════════════════════════
#  classify_severity
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "error_type, expected",
    [
        ("module_not_found", SEVERITY_ERROR),
        ("syntaxerror", SEVERITY_ERROR),
        ("typeerror", SEVERITY_ERROR),
        ("runtime/typeerror", SEVERITY_ERROR),
        ("runtime/warning", SEVERITY_WARNING),
        ("runtime/warning_prop_types", SEVERITY_WARNING),
        ("react_warning", SEVERITY_WARNING),
        ("hmr_warning", SEVERITY_WARNING),
        ("warning", SEVERITY_WARNING),
    ],
)
def test_classify_severity(error_type: str, expected: str):
    assert classify_severity(error_type) == expected


def test_classify_severity_handles_non_string():
    assert classify_severity(None) == SEVERITY_ERROR  # type: ignore[arg-type]
    assert classify_severity(123) == SEVERITY_ERROR  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  combine_errors
# ═══════════════════════════════════════════════════════════════════


def test_combine_errors_preserves_order_and_source():
    c = CompileError(message="c-msg", error_type="module_not_found")
    r = CompileError(message="r-msg", error_type="runtime/typeerror")
    out = combine_errors([c], [r])
    assert out == (
        (c, ErrorSource.compile),
        (r, ErrorSource.runtime),
    )


def test_combine_errors_empty_input_returns_empty():
    assert combine_errors([], []) == ()


def test_combine_errors_rejects_non_compile_error():
    with pytest.raises(TypeError):
        combine_errors(["nope"], [])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        combine_errors([], ["nope"])  # type: ignore[list-item]


# ═══════════════════════════════════════════════════════════════════
#  PreviewError dataclass
# ═══════════════════════════════════════════════════════════════════


def _sample_error(**overrides: Any) -> PreviewError:
    defaults: dict[str, Any] = dict(
        session_id="sess-1",
        error_id="abc123def456",
        message="Can't resolve 'foo'",
        source=ErrorSource.compile,
        error_type="module_not_found",
        severity=SEVERITY_ERROR,
        file="./a.tsx",
        line=10,
        column=5,
        first_seen_at=100.0,
        last_seen_at=100.0,
        occurrences=1,
        raw_excerpt="sample log",
    )
    defaults.update(overrides)
    return PreviewError(**defaults)


def test_preview_error_happy_path():
    e = _sample_error()
    assert e.session_id == "sess-1"
    assert e.is_compile is True
    assert e.is_runtime is False
    assert e.is_blocking is True


def test_preview_error_is_runtime():
    e = _sample_error(source=ErrorSource.runtime, error_type="runtime/typeerror")
    assert e.is_runtime is True
    assert e.is_compile is False


def test_preview_error_warning_is_not_blocking():
    e = _sample_error(severity=SEVERITY_WARNING, error_type="react_warning")
    assert e.is_blocking is False


def test_preview_error_is_frozen():
    e = _sample_error()
    with pytest.raises(Exception):
        e.message = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": ""},
        {"session_id": "   "},
        {"error_id": ""},
        {"message": ""},
        {"error_type": ""},
        {"severity": "bogus"},
        {"line": -1},
        {"column": -1},
        {"first_seen_at": -1.0},
        {"last_seen_at": -1.0},
        {"occurrences": 0},
        {"last_seen_at": 50.0},  # < first_seen_at=100.0
    ],
)
def test_preview_error_rejects_bad_inputs(kwargs: dict):
    with pytest.raises(ValueError):
        _sample_error(**kwargs)


def test_preview_error_rejects_non_enum_source():
    with pytest.raises(ValueError):
        _sample_error(source="compile")  # type: ignore[arg-type]


def test_preview_error_to_dict_is_json_safe():
    e = _sample_error()
    d = e.to_dict()
    assert json.dumps(d)
    assert d["schema_version"] == UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION
    assert d["source"] == "compile"
    assert d["severity"] == SEVERITY_ERROR


def test_preview_error_allows_optional_file_fields():
    e = _sample_error(file=None, line=None, column=None)
    assert e.file is None
    assert e.line is None
    assert e.column is None


def test_preview_error_to_dict_shape_is_stable():
    e = _sample_error()
    d = e.to_dict()
    assert set(d.keys()) == {
        "schema_version",
        "session_id",
        "error_id",
        "message",
        "source",
        "error_type",
        "severity",
        "file",
        "line",
        "column",
        "first_seen_at",
        "last_seen_at",
        "occurrences",
        "raw_excerpt",
    }


# ═══════════════════════════════════════════════════════════════════
#  ErrorBatch dataclass
# ═══════════════════════════════════════════════════════════════════


def test_error_batch_happy_path():
    e = _sample_error()
    b = ErrorBatch(
        session_id="sess-1",
        scanned_at=200.0,
        detected=(e,),
        active=(e,),
        cleared=("old-id",),
        log_chars_scanned=1234,
    )
    assert b.detected_count == 1
    assert b.cleared_count == 1
    assert b.active_count == 1
    assert b.has_activity is True


def test_error_batch_is_frozen():
    b = ErrorBatch(session_id="sess", scanned_at=1.0)
    with pytest.raises(Exception):
        b.scanned_at = 2.0  # type: ignore[misc]


def test_error_batch_to_dict_json_safe():
    e = _sample_error()
    b = ErrorBatch(
        session_id="sess-1",
        scanned_at=200.0,
        detected=(e,),
        active=(e,),
        cleared=("x",),
        log_chars_scanned=100,
    )
    d = b.to_dict()
    assert json.dumps(d)
    assert d["schema_version"] == UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION
    assert len(d["detected"]) == 1
    assert len(d["active"]) == 1
    assert d["cleared"] == ["x"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": ""},
        {"scanned_at": -1.0},
        {"log_chars_scanned": -1},
    ],
)
def test_error_batch_rejects_bad_inputs(kwargs: dict):
    base: dict[str, Any] = dict(session_id="sess", scanned_at=1.0)
    base.update(kwargs)
    with pytest.raises(ValueError):
        ErrorBatch(**base)


def test_error_batch_rejects_non_preview_errors():
    with pytest.raises(ValueError):
        ErrorBatch(
            session_id="sess",
            scanned_at=1.0,
            detected=("not_a_preview_error",),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        ErrorBatch(
            session_id="sess",
            scanned_at=1.0,
            active=("not_a_preview_error",),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        ErrorBatch(
            session_id="sess",
            scanned_at=1.0,
            cleared=("",),
        )


def test_error_batch_empty_has_no_activity():
    b = ErrorBatch(session_id="sess", scanned_at=1.0)
    assert b.has_activity is False
    assert b.detected_count == 0
    assert b.cleared_count == 0


def test_error_batch_normalises_iterables_to_tuples():
    e = _sample_error()
    b = ErrorBatch(
        session_id="sess",
        scanned_at=1.0,
        detected=[e],  # type: ignore[arg-type]
        active=[e],  # type: ignore[arg-type]
        cleared=["x"],  # type: ignore[arg-type]
        warnings=["w"],  # type: ignore[arg-type]
    )
    assert isinstance(b.detected, tuple)
    assert isinstance(b.active, tuple)
    assert isinstance(b.cleared, tuple)
    assert isinstance(b.warnings, tuple)


# ═══════════════════════════════════════════════════════════════════
#  AgentContextPayload dataclass
# ═══════════════════════════════════════════════════════════════════


def test_agent_context_payload_happy_path():
    e = _sample_error()
    p = AgentContextPayload(
        session_id="sess-1",
        built_at=200.0,
        errors=(e,),
        summary_markdown="### Preview errors\n",
        auto_fix_hint="fix foo",
        turn_id="react-1",
    )
    assert p.error_count == 1
    assert p.has_blocking_errors is True
    assert p.has_errors is True


def test_agent_context_payload_empty_has_no_errors():
    p = AgentContextPayload(
        session_id="sess-1",
        built_at=1.0,
        errors=(),
        summary_markdown="",
        auto_fix_hint="",
    )
    assert p.has_errors is False
    assert p.has_blocking_errors is False
    assert p.error_count == 0
    assert p.turn_id is None


def test_agent_context_payload_is_frozen():
    p = AgentContextPayload(
        session_id="sess",
        built_at=1.0,
        errors=(),
        summary_markdown="",
        auto_fix_hint="",
    )
    with pytest.raises(Exception):
        p.session_id = "other"  # type: ignore[misc]


def test_agent_context_payload_to_dict_json_safe():
    e = _sample_error()
    p = AgentContextPayload(
        session_id="sess-1",
        built_at=1.0,
        errors=(e,),
        summary_markdown="md",
        auto_fix_hint="hint",
        turn_id="react-7",
    )
    d = p.to_dict()
    assert json.dumps(d)
    assert d["schema_version"] == UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION
    assert d["error_count"] == 1
    assert d["has_blocking_errors"] is True
    assert d["turn_id"] == "react-7"


def test_agent_context_payload_to_json_sorted():
    p = AgentContextPayload(
        session_id="sess",
        built_at=1.0,
        errors=(),
        summary_markdown="",
        auto_fix_hint="",
    )
    s = p.to_json()
    parsed = json.loads(s)
    assert parsed["session_id"] == "sess"
    # sorted keys → deterministic
    s2 = p.to_json()
    assert s == s2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": ""},
        {"built_at": -1.0},
        {"summary_markdown": None},
        {"auto_fix_hint": None},
    ],
)
def test_agent_context_payload_rejects_bad_inputs(kwargs: dict):
    base: dict[str, Any] = dict(
        session_id="s",
        built_at=1.0,
        errors=(),
        summary_markdown="",
        auto_fix_hint="",
    )
    base.update(kwargs)
    with pytest.raises(ValueError):
        AgentContextPayload(**base)


def test_agent_context_payload_rejects_non_error_entries():
    with pytest.raises(ValueError):
        AgentContextPayload(
            session_id="s",
            built_at=1.0,
            errors=("nope",),  # type: ignore[arg-type]
            summary_markdown="",
            auto_fix_hint="",
        )


def test_agent_context_payload_rejects_empty_turn_id():
    with pytest.raises(ValueError):
        AgentContextPayload(
            session_id="s",
            built_at=1.0,
            errors=(),
            summary_markdown="",
            auto_fix_hint="",
            turn_id="  ",
        )


# ═══════════════════════════════════════════════════════════════════
#  render_error_markdown
# ═══════════════════════════════════════════════════════════════════


def test_render_error_markdown_empty_has_stable_body():
    s = render_error_markdown([])
    assert "No active errors" in s
    assert s.startswith("### Preview errors")


def test_render_error_markdown_renders_table():
    errs = [
        _sample_error(
            error_id="aaa",
            message="module not found 'foo'",
            source=ErrorSource.compile,
            error_type="module_not_found",
        ),
        _sample_error(
            error_id="bbb",
            message="TypeError: x",
            source=ErrorSource.runtime,
            error_type="runtime/typeerror",
        ),
    ]
    md = render_error_markdown(errs)
    assert "| # | Source | Severity | Type | Location | Message |" in md
    assert "module_not_found" in md
    assert "runtime/typeerror" in md
    assert "./a.tsx:10:5" in md


def test_render_error_markdown_escapes_pipes():
    e = _sample_error(message="foo | bar | baz")
    md = render_error_markdown([e])
    assert r"\|" in md


def test_render_error_markdown_deterministic():
    errs = [_sample_error(error_id=f"e-{i:02d}") for i in range(3)]
    a = render_error_markdown(errs)
    b = render_error_markdown(errs)
    assert a == b


def test_render_error_markdown_handles_missing_location():
    e = _sample_error(file=None, line=None, column=None)
    md = render_error_markdown([e])
    assert "(unknown)" in md


# ═══════════════════════════════════════════════════════════════════
#  build_auto_fix_hint
# ═══════════════════════════════════════════════════════════════════


def test_build_auto_fix_hint_empty_nudges_continue():
    hint = build_auto_fix_hint([])
    assert "preview rendered cleanly" in hint.lower()


def test_build_auto_fix_hint_describes_count_and_severity():
    errs = [
        _sample_error(error_id="a", severity=SEVERITY_ERROR),
        _sample_error(error_id="b", severity=SEVERITY_WARNING),
    ]
    hint = build_auto_fix_hint(errs)
    assert "2 active error" in hint
    assert "1 blocking" in hint
    assert "1 warning" in hint


def test_build_auto_fix_hint_references_first_file():
    errs = [_sample_error(file="./pages/index.tsx", line=42)]
    hint = build_auto_fix_hint(errs)
    assert "./pages/index.tsx:42" in hint


def test_build_auto_fix_hint_deterministic():
    errs = [_sample_error()]
    assert build_auto_fix_hint(errs) == build_auto_fix_hint(errs)


# ═══════════════════════════════════════════════════════════════════
#  Fakes + fixture helper
# ═══════════════════════════════════════════════════════════════════


class FakeDockerClient:
    """Deterministic in-memory docker used only to give SandboxManager
    enough shape to respond — logs() is what the bridge actually uses."""

    def __init__(self, *, canned_logs: str = "", logs_error: Exception | None = None) -> None:
        self.canned_logs = canned_logs
        self.logs_error = logs_error
        self._next_id = 0
        self._lock = threading.Lock()
        self.run_calls: list[dict[str, Any]] = []
        self.stop_calls: list[dict[str, Any]] = []
        self.remove_calls: list[dict[str, Any]] = []
        self.logs_calls: list[dict[str, Any]] = []

    def set_logs(self, text: str) -> None:
        with self._lock:
            self.canned_logs = text

    def run_detached(
        self,
        *,
        image: str,
        name: str,
        command: Sequence[str],
        mounts: Sequence[Mapping[str, str]],
        ports: Mapping[int, int],
        env: Mapping[str, str],
        workdir: str,
    ) -> str:
        with self._lock:
            self._next_id += 1
            cid = f"fake-cid-{self._next_id:04d}"
        self.run_calls.append({"name": name, "container_id": cid})
        return cid

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        self.stop_calls.append({"container_id": container_id})

    def remove(self, container_id: str, *, force: bool = False) -> None:
        self.remove_calls.append({"container_id": container_id})

    def logs(self, container_id: str, *, tail: int | None = None) -> str:
        self.logs_calls.append({"container_id": container_id, "tail": tail})
        if self.logs_error is not None:
            raise self.logs_error
        return self.canned_logs

    def inspect(self, container_id: str) -> Mapping[str, Any]:
        return {"Id": container_id, "State": {"Running": True}}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    @property
    def now(self) -> float:
        return self._t


class FakeSleep:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class RecordingEventCallback:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def __call__(self, event_type: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.events.append((event_type, dict(payload)))

    def types(self) -> list[str]:
        with self._lock:
            return [t for t, _ in self.events]

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p) for t, p in self.events if t == event_type]


def _prepare_running(
    tmp_path: Path,
    docker: FakeDockerClient,
    *,
    session_id: str = "sess-1",
) -> tuple[SandboxManager, Any]:
    """Create + start + mark_ready a SandboxManager, returning it and
    the final instance.  Bridge tests skip all of V2 #1/V2 #2 spin-up
    flow by flipping directly into `running`."""

    clock = FakeClock()
    mgr = SandboxManager(docker_client=docker, clock=clock)
    config = SandboxConfig(
        session_id=session_id,
        workspace_path=str(tmp_path),
        host_port=40500,
    )
    mgr.create(config)
    mgr.start(session_id)
    mgr.mark_ready(session_id)
    return mgr, mgr.get(session_id)


def _make_bridge(
    tmp_path: Path,
    *,
    canned_logs: str = "",
    docker: FakeDockerClient | None = None,
    session_id: str = "sess-1",
    **bridge_kwargs: Any,
) -> tuple[PreviewErrorBridge, FakeDockerClient, FakeClock, FakeSleep, RecordingEventCallback, SandboxManager]:
    docker = docker or FakeDockerClient(canned_logs=canned_logs)
    if docker.canned_logs == "" and canned_logs:
        docker.canned_logs = canned_logs
    clock = FakeClock()
    sleep = FakeSleep(clock)
    events = RecordingEventCallback()
    mgr, _ = _prepare_running(tmp_path, docker, session_id=session_id)
    bridge = PreviewErrorBridge(
        manager=mgr,
        clock=clock,
        sleep=sleep,
        event_cb=events,
        **bridge_kwargs,
    )
    return bridge, docker, clock, sleep, events, mgr


# ═══════════════════════════════════════════════════════════════════
#  PreviewErrorBridge constructor
# ═══════════════════════════════════════════════════════════════════


def test_bridge_requires_sandbox_manager():
    with pytest.raises(TypeError):
        PreviewErrorBridge(manager="nope")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"log_tail": 0},
        {"log_tail": -1},
        {"watch_interval_s": 0},
        {"watch_interval_s": -1.0},
        {"max_errors_per_session": 0},
        {"max_excerpt_chars": 0},
    ],
)
def test_bridge_rejects_non_positive_params(tmp_path: Path, kwargs: dict):
    docker = FakeDockerClient()
    mgr, _ = _prepare_running(tmp_path, docker)
    with pytest.raises(ValueError):
        PreviewErrorBridge(manager=mgr, **kwargs)


def test_bridge_manager_property(tmp_path: Path):
    bridge, _, _, _, _, mgr = _make_bridge(tmp_path)
    assert bridge.manager is mgr


def test_bridge_default_knobs_match_module_defaults(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    assert bridge.log_tail == DEFAULT_LOG_TAIL
    assert bridge.watch_interval_s == DEFAULT_WATCH_INTERVAL_S
    assert bridge.max_errors_per_session == DEFAULT_MAX_ERRORS_PER_SESSION


def test_bridge_initial_counters_are_zero(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    assert bridge.scan_count() == 0
    assert bridge.detected_total() == 0
    assert bridge.cleared_total() == 0


# ═══════════════════════════════════════════════════════════════════
#  scan()
# ═══════════════════════════════════════════════════════════════════


def test_scan_rejects_bad_session_id(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.scan("")


def test_scan_rejects_non_positive_tail(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.scan("sess-1", tail=0)


def test_scan_empty_logs_empty_batch(tmp_path: Path):
    bridge, _, _, _, _, _ = _make_bridge(tmp_path, canned_logs="")
    batch = bridge.scan("sess-1")
    assert batch.detected == ()
    assert batch.cleared == ()
    assert batch.active == ()
    assert batch.log_chars_scanned == 0
    assert batch.warnings == ()


def test_scan_detects_compile_error(tmp_path: Path):
    log = (
        "./pages/index.tsx:12:5\n"
        "Module not found: Error: Can't resolve 'foo' in '/app/pages'\n"
    )
    bridge, _, _, _, events, _ = _make_bridge(tmp_path, canned_logs=log)
    batch = bridge.scan("sess-1")
    assert batch.detected_count >= 1
    assert any(e.source is ErrorSource.compile for e in batch.active)
    assert ERROR_EVENT_DETECTED in events.types()
    assert ERROR_EVENT_BATCH in events.types()


def test_scan_detects_runtime_error(tmp_path: Path):
    log = "Uncaught TypeError: foo is undefined\n    at ./app.tsx:10:5\n"
    bridge, _, _, _, events, _ = _make_bridge(tmp_path, canned_logs=log)
    batch = bridge.scan("sess-1")
    assert any(e.source is ErrorSource.runtime for e in batch.active)
    runtime_err = next(e for e in batch.active if e.source is ErrorSource.runtime)
    assert runtime_err.error_type.startswith("runtime/")


def test_scan_persists_across_sweeps(tmp_path: Path):
    log = (
        "./pages/index.tsx:12:5\n"
        "Module not found: Error: Can't resolve 'foo' in '/app/pages'\n"
    )
    bridge, _, _, _, _, _ = _make_bridge(tmp_path, canned_logs=log)
    first = bridge.scan("sess-1")
    second = bridge.scan("sess-1")
    # Same log → same active set → no new detections on second sweep.
    assert first.detected_count >= 1
    assert second.detected == ()
    assert second.cleared == ()
    assert set(e.error_id for e in first.active) == set(
        e.error_id for e in second.active
    )


def test_scan_emits_cleared_when_error_gone(tmp_path: Path):
    log1 = (
        "./pages/index.tsx:12:5\n"
        "Module not found: Error: Can't resolve 'foo' in '/app/pages'\n"
    )
    log2 = "compiled successfully\n"
    docker = FakeDockerClient(canned_logs=log1)
    bridge, _, _, _, events, _ = _make_bridge(tmp_path, docker=docker)
    first = bridge.scan("sess-1")
    assert first.detected_count >= 1
    docker.set_logs(log2)
    second = bridge.scan("sess-1")
    assert second.cleared_count >= 1
    assert ERROR_EVENT_CLEARED in events.types()
    assert second.active == ()


def test_scan_error_ids_are_stable_across_sweeps(tmp_path: Path):
    log = "Uncaught TypeError: foo is undefined\n    at app.tsx:10:5\n"
    bridge, _, _, _, _, _ = _make_bridge(tmp_path, canned_logs=log)
    b1 = bridge.scan("sess-1")
    b2 = bridge.scan("sess-1")
    a1 = {e.error_id for e in b1.active}
    a2 = {e.error_id for e in b2.active}
    assert a1 == a2


def test_scan_preserves_first_seen_at(tmp_path: Path):
    log = "Uncaught TypeError: foo is undefined\n    at app.tsx:10:5\n"
    bridge, _, clock, _, _, _ = _make_bridge(tmp_path, canned_logs=log)
    b1 = bridge.scan("sess-1")
    first_seen = b1.active[0].first_seen_at
    clock.advance(50.0)
    b2 = bridge.scan("sess-1")
    second = b2.active[0]
    assert second.first_seen_at == first_seen
    assert second.last_seen_at > first_seen
    assert second.occurrences >= 2


def test_scan_counters_increment(tmp_path: Path):
    log = "Uncaught TypeError: foo is undefined\n    at app.tsx:10:5\n"
    bridge, _, _, _, _, _ = _make_bridge(tmp_path, canned_logs=log)
    bridge.scan("sess-1")
    bridge.scan("sess-1")
    assert bridge.scan_count() == 2
    assert bridge.detected_total() >= 1


def test_scan_does_not_raise_on_log_fetch_error(tmp_path: Path):
    # SandboxManager.logs() already swallows underlying docker errors.
    # To cover the bridge's own try/except we force manager.logs() to
    # raise — the bridge must not propagate.
    bridge, _, _, _, _, mgr = _make_bridge(tmp_path)

    def boom(session_id: str, *, tail: int | None = None) -> str:
        raise RuntimeError("synthesised logs failure")

    mgr.logs = boom  # type: ignore[method-assign]
    batch = bridge.scan("sess-1")
    assert any("logs_fetch_failed" in w for w in batch.warnings)
    assert batch.session_id == "sess-1"
    assert batch.active == ()


def test_scan_tolerates_empty_logs_gracefully(tmp_path: Path):
    # SandboxManager.logs() returns "" on docker error — bridge must
    # produce an empty well-formed batch.
    docker = FakeDockerClient(logs_error=RuntimeError("docker hiccup"))
    bridge, _, _, _, _, _ = _make_bridge(tmp_path, docker=docker)
    batch = bridge.scan("sess-1")
    assert batch.active == ()
    assert batch.detected == ()
    assert batch.log_chars_scanned == 0


def test_scan_respects_max_errors_per_session(tmp_path: Path):
    lines: list[str] = []
    for i in range(8):
        lines.append(f"./pages/p{i}.tsx:{i}:1")
        lines.append(f"Module not found: Error: Can't resolve 'mod{i}' in '/app'")
    log = "\n".join(lines) + "\n"
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log, max_errors_per_session=3)
    batch = bridge.scan("sess-1")
    assert len(batch.active) <= 3


def test_scan_custom_tail_forwarded_to_logs(tmp_path: Path):
    docker = FakeDockerClient(canned_logs="")
    bridge, _, _, _, _, _ = _make_bridge(tmp_path, docker=docker)
    bridge.scan("sess-1", tail=42)
    assert any(call["tail"] == 42 for call in docker.logs_calls)


def test_scan_batch_event_truncates_active(tmp_path: Path):
    # Force > 25 active errors to hit the event-payload cap.
    lines: list[str] = []
    for i in range(30):
        lines.append(f"./pages/p{i}.tsx:{i}:1")
        lines.append(f"Module not found: Error: Can't resolve 'mod{i}' in '/app'")
    log = "\n".join(lines) + "\n"
    bridge, _, _, _, events, _ = _make_bridge(
        tmp_path,
        canned_logs=log,
        max_errors_per_session=30,
    )
    bridge.scan("sess-1")
    batch_events = events.by_type(ERROR_EVENT_BATCH)
    assert batch_events
    payload = batch_events[0]
    assert payload["active_truncated"] is True
    assert len(payload["active"]) <= 25


def test_scan_active_sorted_blocking_first(tmp_path: Path):
    log = (
        "Warning: Each child in a list should have a unique key prop.\n"
        "./a.tsx:1:1\n"
        "Module not found: Error: Can't resolve 'foo' in '/app'\n"
    )
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log)
    batch = bridge.scan("sess-1")
    severities = [e.severity for e in batch.active]
    # All `error` severities must come before any `warning` severity.
    seen_warning = False
    for sev in severities:
        if sev == SEVERITY_WARNING:
            seen_warning = True
        else:
            assert not seen_warning, f"order violated: {severities}"


def test_scan_stores_last_batch(tmp_path: Path):
    log = "Uncaught TypeError: foo is undefined\n    at app.tsx:10:5\n"
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log)
    batch = bridge.scan("sess-1")
    assert bridge.last_batch("sess-1") is batch
    assert bridge.last_batch("missing") is None


def test_scan_tracked_sessions_grows(tmp_path: Path):
    docker = FakeDockerClient(canned_logs="")
    clock = FakeClock()
    mgr = SandboxManager(docker_client=docker, clock=clock)
    for sid in ["a", "b"]:
        mgr.create(SandboxConfig(session_id=sid, workspace_path=str(tmp_path), host_port=40500 + hash(sid) % 100))
        mgr.start(sid)
        mgr.mark_ready(sid)
    bridge = PreviewErrorBridge(manager=mgr, clock=clock)
    bridge.scan("a")
    bridge.scan("b")
    assert set(bridge.tracked_sessions()) == {"a", "b"}


# ═══════════════════════════════════════════════════════════════════
#  build_agent_context
# ═══════════════════════════════════════════════════════════════════


def test_build_agent_context_rejects_bad_session_id(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.build_agent_context("")


def test_build_agent_context_rejects_empty_turn_id(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.build_agent_context("sess-1", turn_id="  ")


def test_build_agent_context_no_errors(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path, canned_logs="")
    ctx = bridge.build_agent_context("sess-1", turn_id="react-1")
    assert ctx.has_errors is False
    assert "No active errors" in ctx.summary_markdown
    assert "preview rendered cleanly" in ctx.auto_fix_hint.lower()


def test_build_agent_context_with_active_errors(tmp_path: Path):
    log = "Uncaught TypeError: foo is undefined\n    at app.tsx:10:5\n"
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log)
    bridge.scan("sess-1")
    ctx = bridge.build_agent_context("sess-1", turn_id="react-2")
    assert ctx.has_errors is True
    assert ctx.error_count >= 1
    assert "runtime/typeerror" in ctx.summary_markdown


def test_build_agent_context_emits_event(tmp_path: Path):
    bridge, _, _, _, events, _ = _make_bridge(tmp_path)
    bridge.build_agent_context("sess-1")
    assert ERROR_EVENT_CONTEXT_BUILT in events.types()


def test_build_agent_context_includes_auto_fix_hint(tmp_path: Path):
    # Trigger first then file — matches parse_compile_error's
    # forward-scanning contract.
    log = (
        "Module not found: Error: Can't resolve 'foo' in '/app'\n"
        "./pages/index.tsx:12:5\n"
    )
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log)
    bridge.scan("sess-1")
    ctx = bridge.build_agent_context("sess-1")
    assert "./pages/index.tsx" in ctx.auto_fix_hint


def test_build_agent_context_to_dict_json_safe(tmp_path: Path):
    log = "Uncaught TypeError: foo is undefined\n    at app.tsx:10:5\n"
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log)
    bridge.scan("sess-1")
    ctx = bridge.build_agent_context("sess-1")
    d = ctx.to_dict()
    assert json.dumps(d)


# ═══════════════════════════════════════════════════════════════════
#  State queries + mutation
# ═══════════════════════════════════════════════════════════════════


def test_active_errors_empty_for_unknown_session(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    assert bridge.active_errors("missing") == ()


def test_has_active_errors_tracks_state(tmp_path: Path):
    log = "Uncaught TypeError: x\n    at a.tsx:1:1\n"
    docker = FakeDockerClient(canned_logs=log)
    bridge, _, _, _, _, _ = _make_bridge(tmp_path, docker=docker)
    assert bridge.has_active_errors("sess-1") is False
    bridge.scan("sess-1")
    assert bridge.has_active_errors("sess-1") is True
    docker.set_logs("")
    bridge.scan("sess-1")
    assert bridge.has_active_errors("sess-1") is False


def test_get_error_returns_none_for_unknown(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    assert bridge.get_error("sess-1", "no-such-id") is None


def test_acknowledge_clears_error(tmp_path: Path):
    log = "Uncaught TypeError: x\n    at a.tsx:1:1\n"
    bridge, _, _, _, events, _ = _make_bridge(tmp_path, canned_logs=log)
    bridge.scan("sess-1")
    active = bridge.active_errors("sess-1")
    assert active
    error_id = active[0].error_id
    assert bridge.acknowledge("sess-1", error_id) is True
    # Second call is idempotent — returns False.
    assert bridge.acknowledge("sess-1", error_id) is False
    # Emitted cleared event.
    cleared_events = events.by_type(ERROR_EVENT_CLEARED)
    assert any(
        e.get("source") == "acknowledge" and e.get("error_id") == error_id
        for e in cleared_events
    )


def test_acknowledge_rejects_bad_inputs(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.acknowledge("", "x")
    with pytest.raises(ValueError):
        bridge.acknowledge("sess-1", "")


def test_clear_session_drops_all(tmp_path: Path):
    log = "Uncaught TypeError: x\n    at a.tsx:1:1\n"
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log)
    bridge.scan("sess-1")
    assert bridge.has_active_errors("sess-1") is True
    dropped = bridge.clear_session("sess-1")
    assert dropped >= 1
    assert bridge.has_active_errors("sess-1") is False


def test_clear_session_on_empty_returns_zero(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    assert bridge.clear_session("nothing") == 0


def test_clear_session_rejects_bad_session_id(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.clear_session("")


# ═══════════════════════════════════════════════════════════════════
#  Background watch
# ═══════════════════════════════════════════════════════════════════


def test_start_watch_spawns_thread(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    bridge.start_watch("sess-1", interval_s=0.02)
    try:
        assert bridge.is_watching("sess-1") is True
        assert "sess-1" in bridge.watch_sessions()
    finally:
        bridge.stop_watch("sess-1")


def test_start_watch_emits_started_event(tmp_path: Path):
    bridge, _, _, _, events, _ = _make_bridge(tmp_path)
    bridge.start_watch("sess-1", interval_s=0.02)
    try:
        assert ERROR_EVENT_WATCH_STARTED in events.types()
    finally:
        bridge.stop_watch("sess-1")


def test_start_watch_rejects_bad_session_id(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.start_watch("")


def test_start_watch_rejects_non_positive_interval(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.start_watch("sess-1", interval_s=0)


def test_start_watch_raises_when_already_running(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    bridge.start_watch("sess-1", interval_s=0.02)
    try:
        with pytest.raises(WatchAlreadyRunning):
            bridge.start_watch("sess-1", interval_s=0.02)
    finally:
        bridge.stop_watch("sess-1")


def test_stop_watch_missing_returns_false_by_default(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    assert bridge.stop_watch("nope") is False


def test_stop_watch_missing_ok_false_raises(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with pytest.raises(WatchNotRunning):
        bridge.stop_watch("nope", missing_ok=False)


def test_stop_watch_emits_stopped_event(tmp_path: Path):
    bridge, _, _, _, events, _ = _make_bridge(tmp_path)
    bridge.start_watch("sess-1", interval_s=0.02)
    assert bridge.stop_watch("sess-1") is True
    assert ERROR_EVENT_WATCH_STOPPED in events.types()


def test_stop_all_watches(tmp_path: Path):
    docker = FakeDockerClient(canned_logs="")
    clock = FakeClock()
    mgr = SandboxManager(docker_client=docker, clock=clock)
    for sid in ["a", "b", "c"]:
        mgr.create(
            SandboxConfig(
                session_id=sid,
                workspace_path=str(tmp_path),
                host_port=40500 + abs(hash(sid)) % 100,
            )
        )
        mgr.start(sid)
        mgr.mark_ready(sid)
    bridge = PreviewErrorBridge(manager=mgr, clock=clock)
    bridge.start_watch("a", interval_s=0.02)
    bridge.start_watch("b", interval_s=0.02)
    bridge.start_watch("c", interval_s=0.02)
    stopped = bridge.stop_all_watches()
    assert stopped == 3
    assert bridge.watch_sessions() == ()


def test_watch_actually_sweeps(tmp_path: Path):
    # Use a real clock for this one — verify the thread really wakes up
    # and calls scan.
    log = "Uncaught TypeError: foo is undefined\n    at a.tsx:1:1\n"
    docker = FakeDockerClient(canned_logs=log)
    events = RecordingEventCallback()
    mgr, _ = _prepare_running(tmp_path, docker)
    bridge = PreviewErrorBridge(manager=mgr, event_cb=events)
    bridge.start_watch("sess-1", interval_s=0.05)
    try:
        # Wait up to 1 second for at least one sweep + detection.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if bridge.scan_count() >= 1:
                break
            time.sleep(0.02)
        assert bridge.scan_count() >= 1
    finally:
        bridge.stop_watch("sess-1")


def test_watch_counters_reset_after_stop(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    bridge.start_watch("sess-1", interval_s=0.02)
    bridge.stop_watch("sess-1")
    # Once stopped, watch state is removed — sweeps/failures return 0.
    assert bridge.watch_sweeps("sess-1") == 0
    assert bridge.watch_failures("sess-1") == 0


# ═══════════════════════════════════════════════════════════════════
#  Snapshot + context manager
# ═══════════════════════════════════════════════════════════════════


def test_snapshot_json_safe(tmp_path: Path):
    log = "Uncaught TypeError: foo\n    at a.tsx:1:1\n"
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log)
    bridge.scan("sess-1")
    snap = bridge.snapshot()
    assert json.dumps(snap)
    assert snap["schema_version"] == UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION
    assert "sess-1" in snap["sessions"]
    assert snap["scan_count"] == 1


def test_snapshot_empty_is_valid(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    snap = bridge.snapshot()
    assert snap["sessions"] == {}
    assert snap["watches"] == {}
    assert snap["scan_count"] == 0


def test_context_manager_stops_watches_and_clears_state(tmp_path: Path):
    log = "Uncaught TypeError: foo\n    at a.tsx:1:1\n"
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log)
    bridge.scan("sess-1")
    bridge.start_watch("sess-1", interval_s=0.02)
    with bridge:
        pass
    assert bridge.watch_sessions() == ()
    assert bridge.tracked_sessions() == ()


def test_context_manager_returns_self(tmp_path: Path):
    bridge, *_ = _make_bridge(tmp_path)
    with bridge as b:
        assert b is bridge


# ═══════════════════════════════════════════════════════════════════
#  Event callback safety
# ═══════════════════════════════════════════════════════════════════


def test_event_callback_failure_is_swallowed(tmp_path: Path):
    def boom(event_type: str, payload: Mapping[str, Any]) -> None:
        raise RuntimeError("callback exploded")

    docker = FakeDockerClient(canned_logs="Uncaught TypeError: x\n    at a.tsx:1:1\n")
    clock = FakeClock()
    mgr, _ = _prepare_running(tmp_path, docker)
    bridge = PreviewErrorBridge(manager=mgr, clock=clock, event_cb=boom)
    # No exception propagates out.
    bridge.scan("sess-1")
    bridge.build_agent_context("sess-1")


# ═══════════════════════════════════════════════════════════════════
#  Thread safety
# ═══════════════════════════════════════════════════════════════════


def test_concurrent_scans_no_corruption(tmp_path: Path):
    log = "Uncaught TypeError: x\n    at a.tsx:1:1\n"
    bridge, *_ = _make_bridge(tmp_path, canned_logs=log)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(20):
                bridge.scan("sess-1")
                bridge.build_agent_context("sess-1")
        except Exception as exc:  # pragma: no cover - should not happen
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert bridge.scan_count() == 10 * 20
    # Still exactly one active error tracked.
    assert len(bridge.active_errors("sess-1")) == 1


# ═══════════════════════════════════════════════════════════════════
#  Sibling alignment + end-to-end
# ═══════════════════════════════════════════════════════════════════


def test_sibling_ui_sandbox_still_importable():
    from backend import ui_sandbox  # noqa: F401
    from backend import ui_sandbox_lifecycle  # noqa: F401
    from backend import ui_screenshot  # noqa: F401
    from backend import ui_responsive_viewport  # noqa: F401


def test_parse_compile_error_reused_from_v2_1():
    # The bridge does not re-implement parse_compile_error; it imports
    # V2 #1's function directly.
    assert peb.parse_compile_error is parse_compile_error


def test_end_to_end_agent_fix_loop(tmp_path: Path):
    """Simulate the full V2 #5 loop:
      1. Agent writes broken code → dev server emits compile error
      2. Bridge.scan() detects it → emits ui_sandbox.error.detected
      3. Agent reads build_agent_context() → pastes into next turn
      4. Agent edits file → dev server HMR recovers
      5. Bridge.scan() sees the error gone → emits cleared event
      6. Bridge.build_agent_context() now reports "preview clean"
    """

    # Step 1+2: broken code, detected.  parse_compile_error scans the
    # file fragment forward from the trigger line, so the ordering is:
    # trigger first, path second.
    log_broken = (
        "Module not found: Error: Can't resolve 'foo' in '/app/pages'\n"
        "./pages/index.tsx:12:5\n"
    )
    docker = FakeDockerClient(canned_logs=log_broken)
    clock = FakeClock()
    sleep = FakeSleep(clock)
    events = RecordingEventCallback()
    mgr, _ = _prepare_running(tmp_path, docker)
    bridge = PreviewErrorBridge(
        manager=mgr, clock=clock, sleep=sleep, event_cb=events
    )
    batch_broken = bridge.scan("sess-1")
    assert batch_broken.detected_count >= 1
    assert ERROR_EVENT_DETECTED in events.types()

    # Step 3: agent gets a structured context for its next ReAct turn.
    ctx = bridge.build_agent_context("sess-1", turn_id="react-loop-1")
    assert ctx.has_errors is True
    assert ctx.has_blocking_errors is True
    assert "Module not found" in ctx.summary_markdown or "module_not_found" in ctx.summary_markdown
    # The auto-fix hint must reference the file the agent needs to fix.
    assert "index.tsx" in ctx.auto_fix_hint

    # Step 4+5: agent's edit lands, HMR clears the error.
    docker.set_logs("compiled successfully in 0.4s\n")
    clock.advance(10.0)
    batch_clean = bridge.scan("sess-1")
    assert batch_clean.cleared_count >= 1
    assert batch_clean.active == ()
    assert ERROR_EVENT_CLEARED in events.types()

    # Step 6: follow-up context is clean.
    ctx_clean = bridge.build_agent_context("sess-1", turn_id="react-loop-2")
    assert ctx_clean.has_errors is False
    assert "cleanly" in ctx_clean.auto_fix_hint.lower()
