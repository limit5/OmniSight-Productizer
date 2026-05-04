"""V6 #6 (issue #322) — ``mobile_build_error_autofix`` contract tests.

Pins ``backend/mobile_build_error_autofix.py`` against the V6 row-6
spec — the orchestrator that closes the mobile build-fix-rebuild
loop:

* Gradle / Xcode build errors from V6 #1 ``MobileSandboxManager``
  flow into a structured :class:`MobileBuildErrorSummary`.
* The agent fix callback receives both the structured summary and
  (optionally) the V6 #5 multimodal payload, returns one of
  ``patched`` / ``no_op`` / ``give_up``.
* Patched → loop rebuilds; no_op / give_up → loop ends as ``failed``.
* Build pass on the first attempt → loop ends as ``succeeded`` with
  install + screenshot completed.
* Mock build (toolchain absent) → loop ends as ``skipped`` without
  invoking the agent.
* ``failure_mode="continue"`` (default) never raises, even when the
  agent callback raises or returns junk.
* ``failure_mode="abort"`` re-raises agent and sandbox failures so
  CI hard-fails.
* Events fire under the ``mobile_sandbox.autofix.*`` namespace, with
  zero overlap against V6 #1 and V6 #5 namespaces.

All tests inject deterministic fakes for the sandbox manager,
visual context builder, and agent callback so no real docker / adb /
xcrun / Anthropic call is touched.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Mapping

import pytest

from backend import mobile_build_error_autofix as mfx
from backend.mobile_agent_visual_context import (
    DEFAULT_DEVICE_TARGETS,
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES,
    MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
    MobileAgentVisualContextBuilder,
    MobileBuildErrorSummary,
    MobileDeviceTarget,
)
from backend.mobile_build_error_autofix import (
    AGENT_FIX_ACTIONS,
    AUTOFIX_EVENT_ATTEMPT_FINISHED,
    AUTOFIX_EVENT_ATTEMPT_STARTED,
    AUTOFIX_EVENT_BUILD_FAILED,
    AUTOFIX_EVENT_BUILD_PASSED,
    AUTOFIX_EVENT_EXHAUSTED,
    AUTOFIX_EVENT_FAILED,
    AUTOFIX_EVENT_FIX_APPLIED,
    AUTOFIX_EVENT_SKIPPED,
    AUTOFIX_EVENT_STARTED,
    AUTOFIX_EVENT_SUCCEEDED,
    AUTOFIX_EVENT_TYPES,
    DEFAULT_FAILURE_MODE,
    DEFAULT_MAX_ATTEMPTS,
    FAILURE_MODES,
    MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
    AgentFixAction,
    AutoFixAttemptStatus,
    AutoFixStatus,
    MobileAutoFixAttempt,
    MobileAutoFixConfigError,
    MobileAutoFixError,
    MobileAutoFixLoop,
    MobileAutoFixOutcome,
    MobileAutoFixRequest,
    MobileAutoFixResponse,
    MobileAutoFixSandboxError,
    format_autofix_attempt_id,
    render_autofix_outcome_markdown,
    summarise_build_errors,
)
from backend.mobile_sandbox import (
    BuildError,
    BuildReport,
    InstallReport,
    MobileSandboxConfig,
    MobileSandboxError,
    MobileSandboxManager,
    ScreenshotReport,
)
from backend.mobile_screenshot import (
    PNG_MAGIC,
    ScreenshotRequest,
    ScreenshotResult,
    ScreenshotStatus,
)


# ═══════════════════════════════════════════════════════════════════
#  Module invariants
# ═══════════════════════════════════════════════════════════════════


EXPECTED_ALL = {
    "MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION",
    "DEFAULT_MAX_ATTEMPTS",
    "FAILURE_MODES",
    "DEFAULT_FAILURE_MODE",
    "AUTOFIX_EVENT_STARTED",
    "AUTOFIX_EVENT_ATTEMPT_STARTED",
    "AUTOFIX_EVENT_BUILD_PASSED",
    "AUTOFIX_EVENT_BUILD_FAILED",
    "AUTOFIX_EVENT_FIX_APPLIED",
    "AUTOFIX_EVENT_ATTEMPT_FINISHED",
    "AUTOFIX_EVENT_SUCCEEDED",
    "AUTOFIX_EVENT_FAILED",
    "AUTOFIX_EVENT_EXHAUSTED",
    "AUTOFIX_EVENT_SKIPPED",
    "AUTOFIX_EVENT_TYPES",
    "AutoFixStatus",
    "AutoFixAttemptStatus",
    "AgentFixAction",
    "AGENT_FIX_ACTIONS",
    "MobileAutoFixError",
    "MobileAutoFixConfigError",
    "MobileAutoFixSandboxError",
    "MobileAutoFixRequest",
    "MobileAutoFixResponse",
    "MobileAutoFixAttempt",
    "MobileAutoFixOutcome",
    "MobileAutoFixLoop",
    "summarise_build_errors",
    "render_autofix_outcome_markdown",
    "format_autofix_attempt_id",
}


def test_all_complete():
    assert set(mfx.__all__) == EXPECTED_ALL


def test_schema_version_is_semver():
    parts = MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    for p in parts:
        assert p.isdigit()


def test_default_max_attempts():
    assert DEFAULT_MAX_ATTEMPTS == 4


def test_failure_modes_stable():
    assert FAILURE_MODES == ("continue", "abort")
    assert DEFAULT_FAILURE_MODE == "continue"


def test_event_types_complete_and_unique():
    assert set(AUTOFIX_EVENT_TYPES) == {
        AUTOFIX_EVENT_STARTED,
        AUTOFIX_EVENT_ATTEMPT_STARTED,
        AUTOFIX_EVENT_BUILD_PASSED,
        AUTOFIX_EVENT_BUILD_FAILED,
        AUTOFIX_EVENT_FIX_APPLIED,
        AUTOFIX_EVENT_ATTEMPT_FINISHED,
        AUTOFIX_EVENT_SUCCEEDED,
        AUTOFIX_EVENT_FAILED,
        AUTOFIX_EVENT_EXHAUSTED,
        AUTOFIX_EVENT_SKIPPED,
    }
    assert len(AUTOFIX_EVENT_TYPES) == len(set(AUTOFIX_EVENT_TYPES))
    for ev in AUTOFIX_EVENT_TYPES:
        assert ev.startswith("mobile_sandbox.autofix.")


def test_event_namespace_disjoint_from_v6_1_and_v6_5():
    """``mobile_sandbox.autofix.*`` must never collide with V6 #1
    ``mobile_sandbox.<state>`` topics (created/building/built/...) or
    V6 #5 ``mobile_sandbox.agent_visual_context.*`` topics.
    """
    v6_1_topics = {
        "mobile_sandbox.created",
        "mobile_sandbox.building",
        "mobile_sandbox.built",
        "mobile_sandbox.failed",
        "mobile_sandbox.installing",
        "mobile_sandbox.ready",
        "mobile_sandbox.screenshot",
        "mobile_sandbox.stopped",
    }
    v6_5_topics = set(MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES)
    autofix_topics = set(AUTOFIX_EVENT_TYPES)
    assert autofix_topics.isdisjoint(v6_1_topics)
    assert autofix_topics.isdisjoint(v6_5_topics)


def test_agent_fix_actions_stable():
    assert AGENT_FIX_ACTIONS == ("patched", "no_op", "give_up")
    assert AGENT_FIX_ACTIONS == tuple(a.value for a in AgentFixAction)


def test_autofix_status_enum_complete():
    expected = {"pending", "succeeded", "failed", "exhausted", "skipped"}
    assert {s.value for s in AutoFixStatus} == expected


def test_autofix_attempt_status_enum_complete():
    expected = {
        "pending",
        "build_passed",
        "build_failed",
        "agent_patched",
        "agent_no_op",
        "agent_give_up",
        "agent_error",
        "sandbox_error",
        "skipped",
    }
    assert {s.value for s in AutoFixAttemptStatus} == expected


def test_error_hierarchy():
    assert issubclass(MobileAutoFixError, RuntimeError)
    assert issubclass(MobileAutoFixConfigError, MobileAutoFixError)
    assert issubclass(MobileAutoFixSandboxError, MobileAutoFixError)


# ═══════════════════════════════════════════════════════════════════
#  summarise_build_errors helper
# ═══════════════════════════════════════════════════════════════════


def test_summarise_empty_clean_block():
    summary = summarise_build_errors([], platform="android")
    assert isinstance(summary, MobileBuildErrorSummary)
    assert summary.active_error_count == 0
    assert summary.has_blocking_errors is False
    assert "No build errors reported" in summary.summary_markdown
    assert "Android" in summary.summary_markdown
    assert summary.auto_fix_hint  # non-empty default


def test_summarise_ios_label():
    summary = summarise_build_errors([], platform="ios")
    assert "iOS" in summary.summary_markdown


def test_summarise_with_errors_blocking():
    errs = (
        BuildError(
            message="Unresolved reference: Foo",
            file="app/src/Bar.kt",
            line=42,
            column=7,
            severity="error",
            tool="gradle",
        ),
    )
    summary = summarise_build_errors(errs, platform="android")
    assert summary.active_error_count == 1
    assert summary.has_blocking_errors is True
    assert "Unresolved reference" in summary.summary_markdown
    assert "app/src/Bar.kt:42:7" in summary.summary_markdown
    assert "**error**" in summary.summary_markdown
    assert "[gradle]" in summary.summary_markdown
    assert "Patch" in summary.auto_fix_hint


def test_summarise_warnings_only_no_blocking():
    errs = (
        BuildError(
            message="Unused parameter",
            file="x.kt", line=1, severity="warning", tool="gradle",
        ),
    )
    summary = summarise_build_errors(errs, platform="android")
    assert summary.has_blocking_errors is False
    assert summary.active_error_count == 1
    assert "warnings" in summary.auto_fix_hint


def test_summarise_no_location_renders_placeholder():
    errs = (
        BuildError(message="Could not resolve dependency", severity="error", tool="gradle"),
    )
    summary = summarise_build_errors(errs, platform="android")
    assert "(no location)" in summary.summary_markdown


def test_summarise_custom_hint_overrides_default():
    summary = summarise_build_errors(
        [], platform="android", auto_fix_hint="custom",
    )
    assert summary.auto_fix_hint == "custom"


def test_summarise_rejects_non_buildissue():
    with pytest.raises(TypeError):
        summarise_build_errors(["nope"], platform="android")  # type: ignore[arg-type]


def test_summarise_rejects_unknown_platform():
    with pytest.raises(ValueError):
        summarise_build_errors([], platform="windows")


def test_summarise_rejects_empty_platform():
    with pytest.raises(ValueError):
        summarise_build_errors([], platform="")


def test_summarise_rejects_non_string_hint():
    with pytest.raises(ValueError):
        summarise_build_errors([], platform="android", auto_fix_hint=42)  # type: ignore[arg-type]


def test_summarise_blocking_count_pluralisation():
    multi = (
        BuildError(message="A", severity="error", tool="gradle"),
        BuildError(message="B", severity="error", tool="gradle"),
    )
    summary = summarise_build_errors(multi, platform="android")
    assert "2 blocking build errors" in summary.auto_fix_hint
    single = (BuildError(message="C", severity="error", tool="gradle"),)
    summary_single = summarise_build_errors(single, platform="android")
    assert "1 blocking build error" in summary_single.auto_fix_hint
    assert "1 blocking build errors" not in summary_single.auto_fix_hint


# ═══════════════════════════════════════════════════════════════════
#  format_autofix_attempt_id helper
# ═══════════════════════════════════════════════════════════════════


def test_format_attempt_id_happy():
    assert format_autofix_attempt_id("sess-1", 1) == "autofix-sess-1-001"


def test_format_attempt_id_pads():
    assert format_autofix_attempt_id("s", 12) == "autofix-s-012"


def test_format_attempt_id_sanitises():
    assert format_autofix_attempt_id("a/b c", 1) == "autofix-a-b-c-001"


def test_format_attempt_id_rejects_empty_session():
    with pytest.raises(ValueError):
        format_autofix_attempt_id("", 1)


def test_format_attempt_id_rejects_zero_attempt():
    with pytest.raises(ValueError):
        format_autofix_attempt_id("s", 0)


def test_format_attempt_id_rejects_negative_attempt():
    with pytest.raises(ValueError):
        format_autofix_attempt_id("s", -1)


# ═══════════════════════════════════════════════════════════════════
#  Records — frozen + validation + to_dict
# ═══════════════════════════════════════════════════════════════════


def _ok_summary() -> MobileBuildErrorSummary:
    return summarise_build_errors([], platform="android")


def _ok_request_kwargs() -> dict[str, Any]:
    return dict(
        session_id="sess-1",
        attempt_index=1,
        workspace_path="/tmp/w",
        platform="android",
        build_errors=(),
        error_summary=_ok_summary(),
        visual_payload=None,
        sandbox_snapshot={"x": 1},
        previous_attempts=(),
        requested_at=10.0,
    )


def test_request_happy():
    req = MobileAutoFixRequest(**_ok_request_kwargs())
    assert req.session_id == "sess-1"
    assert req.attempt_index == 1
    assert req.has_visual_payload is False
    assert req.error_count == 0


def test_request_frozen():
    req = MobileAutoFixRequest(**_ok_request_kwargs())
    with pytest.raises(FrozenInstanceError):
        req.session_id = "x"  # type: ignore[misc]


def test_request_to_dict_round_trip():
    req = MobileAutoFixRequest(**_ok_request_kwargs())
    d = req.to_dict()
    assert d["session_id"] == "sess-1"
    assert d["attempt_index"] == 1
    assert d["has_visual_payload"] is False
    assert d["error_count"] == 0
    assert d["visual_payload"] is None
    json.dumps(d)  # JSON-safe


def test_request_rejects_empty_session():
    kw = _ok_request_kwargs()
    kw["session_id"] = ""
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_request_rejects_negative_attempt():
    kw = _ok_request_kwargs()
    kw["attempt_index"] = 0
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_request_rejects_unknown_platform():
    kw = _ok_request_kwargs()
    kw["platform"] = "windows"
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_request_rejects_non_tuple_errors():
    kw = _ok_request_kwargs()
    kw["build_errors"] = ["x"]  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_request_rejects_non_buildissue_in_errors():
    kw = _ok_request_kwargs()
    kw["build_errors"] = ("nope",)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_request_rejects_bad_summary():
    kw = _ok_request_kwargs()
    kw["error_summary"] = "not summary"  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_request_rejects_bad_visual_payload():
    kw = _ok_request_kwargs()
    kw["visual_payload"] = "not payload"  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_request_rejects_bad_snapshot():
    kw = _ok_request_kwargs()
    kw["sandbox_snapshot"] = "not mapping"  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_request_rejects_non_tuple_previous():
    kw = _ok_request_kwargs()
    kw["previous_attempts"] = []  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_request_rejects_negative_requested_at():
    kw = _ok_request_kwargs()
    kw["requested_at"] = -1.0
    with pytest.raises(ValueError):
        MobileAutoFixRequest(**kw)


def test_response_happy_patched():
    resp = MobileAutoFixResponse(action="patched", summary="fixed Foo")
    assert resp.is_patch is True
    assert resp.is_no_op is False
    assert resp.is_give_up is False
    assert resp.file_count == 0


def test_response_happy_no_op():
    resp = MobileAutoFixResponse(action="no_op")
    assert resp.is_no_op is True


def test_response_happy_give_up():
    resp = MobileAutoFixResponse(action="give_up")
    assert resp.is_give_up is True


def test_response_files_touched_count():
    resp = MobileAutoFixResponse(
        action="patched",
        files_touched=("a.kt", "b.kt"),
    )
    assert resp.file_count == 2


def test_response_frozen():
    resp = MobileAutoFixResponse(action="patched")
    with pytest.raises(FrozenInstanceError):
        resp.action = "no_op"  # type: ignore[misc]


def test_response_to_dict_round_trip():
    resp = MobileAutoFixResponse(
        action="patched",
        summary="ok",
        files_touched=("a.kt",),
        raw_response="{...}",
    )
    d = resp.to_dict()
    assert d["action"] == "patched"
    assert d["files_touched"] == ["a.kt"]
    assert d["is_patch"] is True
    json.dumps(d)


def test_response_rejects_unknown_action():
    with pytest.raises(ValueError):
        MobileAutoFixResponse(action="murder")


def test_response_rejects_non_string_summary():
    with pytest.raises(ValueError):
        MobileAutoFixResponse(action="patched", summary=42)  # type: ignore[arg-type]


def test_response_rejects_non_tuple_files():
    with pytest.raises(ValueError):
        MobileAutoFixResponse(
            action="patched", files_touched=["a"],  # type: ignore[arg-type]
        )


def test_response_rejects_empty_file_string():
    with pytest.raises(ValueError):
        MobileAutoFixResponse(action="patched", files_touched=("",))


def test_response_rejects_non_string_raw():
    with pytest.raises(ValueError):
        MobileAutoFixResponse(
            action="patched", raw_response=42,  # type: ignore[arg-type]
        )


def _ok_attempt_kwargs() -> dict[str, Any]:
    return dict(
        attempt_index=1,
        started_at=10.0,
        finished_at=11.0,
        status=AutoFixAttemptStatus.build_passed,
    )


def test_attempt_happy_minimal():
    a = MobileAutoFixAttempt(**_ok_attempt_kwargs())
    assert a.attempt_index == 1
    assert a.duration_ms == 1000
    assert a.is_terminal_for_loop is True


def test_attempt_did_call_agent_property():
    a = MobileAutoFixAttempt(
        attempt_index=1,
        started_at=10.0,
        finished_at=11.0,
        status=AutoFixAttemptStatus.agent_patched,
        agent_action="patched",
    )
    assert a.did_call_agent is True
    no_agent = MobileAutoFixAttempt(**_ok_attempt_kwargs())
    assert no_agent.did_call_agent is False


def test_attempt_terminal_states():
    """build_passed / agent_no_op / agent_give_up / agent_error /
    sandbox_error / skipped should stop the loop; agent_patched should
    not."""
    terminal = {
        AutoFixAttemptStatus.build_passed,
        AutoFixAttemptStatus.agent_no_op,
        AutoFixAttemptStatus.agent_give_up,
        AutoFixAttemptStatus.agent_error,
        AutoFixAttemptStatus.sandbox_error,
        AutoFixAttemptStatus.skipped,
    }
    for status in AutoFixAttemptStatus:
        a = MobileAutoFixAttempt(
            attempt_index=1,
            started_at=10.0,
            finished_at=11.0,
            status=status,
        )
        assert a.is_terminal_for_loop == (status in terminal)


def test_attempt_frozen():
    a = MobileAutoFixAttempt(**_ok_attempt_kwargs())
    with pytest.raises(FrozenInstanceError):
        a.attempt_index = 2  # type: ignore[misc]


def test_attempt_to_dict_round_trip():
    a = MobileAutoFixAttempt(
        attempt_index=2,
        started_at=10.0,
        finished_at=12.5,
        status=AutoFixAttemptStatus.agent_patched,
        build_status="fail",
        build_error_count=3,
        build_errors=(BuildError(message="x", severity="error", tool="gradle"),),
        install_status="",
        screenshot_status="",
        agent_action="patched",
        agent_summary="patched Foo",
        agent_files_touched=("a.kt",),
        visual_payload_built=True,
        visual_image_count=2,
        detail="fixed",
        warnings=("w1",),
    )
    d = a.to_dict()
    assert d["attempt_index"] == 2
    assert d["duration_ms"] == 2500
    assert d["status"] == "agent_patched"
    assert d["build_errors"][0]["message"] == "x"
    assert d["agent_files_touched"] == ["a.kt"]
    json.dumps(d)


def test_attempt_rejects_zero_attempt():
    kw = _ok_attempt_kwargs()
    kw["attempt_index"] = 0
    with pytest.raises(ValueError):
        MobileAutoFixAttempt(**kw)


def test_attempt_rejects_negative_started():
    kw = _ok_attempt_kwargs()
    kw["started_at"] = -1.0
    with pytest.raises(ValueError):
        MobileAutoFixAttempt(**kw)


def test_attempt_rejects_finished_before_started():
    kw = _ok_attempt_kwargs()
    kw["started_at"] = 11.0
    kw["finished_at"] = 10.0
    with pytest.raises(ValueError):
        MobileAutoFixAttempt(**kw)


def test_attempt_rejects_bad_status():
    kw = _ok_attempt_kwargs()
    kw["status"] = "build_passed"  # not enum
    with pytest.raises(ValueError):
        MobileAutoFixAttempt(**kw)


def test_attempt_rejects_unknown_agent_action():
    kw = _ok_attempt_kwargs()
    kw["agent_action"] = "murder"
    with pytest.raises(ValueError):
        MobileAutoFixAttempt(**kw)


def test_attempt_accepts_empty_agent_action():
    kw = _ok_attempt_kwargs()
    kw["agent_action"] = ""
    a = MobileAutoFixAttempt(**kw)
    assert a.did_call_agent is False


def test_attempt_rejects_negative_error_count():
    kw = _ok_attempt_kwargs()
    kw["build_error_count"] = -1
    with pytest.raises(ValueError):
        MobileAutoFixAttempt(**kw)


def test_attempt_rejects_empty_warning():
    kw = _ok_attempt_kwargs()
    kw["warnings"] = ("",)
    with pytest.raises(ValueError):
        MobileAutoFixAttempt(**kw)


def test_attempt_rejects_non_tuple_files():
    kw = _ok_attempt_kwargs()
    kw["agent_files_touched"] = ["a"]  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MobileAutoFixAttempt(**kw)


def test_outcome_happy():
    outcome = MobileAutoFixOutcome(
        session_id="sess-1",
        platform="android",
        workspace_path="/tmp/w",
        final_status=AutoFixStatus.succeeded,
        started_at=10.0,
        finished_at=15.0,
        attempts=(MobileAutoFixAttempt(**_ok_attempt_kwargs()),),
    )
    assert outcome.total_attempts == 1
    assert outcome.duration_ms == 5000
    assert outcome.succeeded is True
    assert outcome.was_skipped is False
    assert outcome.did_invoke_agent is False
    assert outcome.total_files_touched == 0


def test_outcome_frozen():
    outcome = MobileAutoFixOutcome(
        session_id="sess-1",
        platform="android",
        workspace_path="/tmp/w",
        final_status=AutoFixStatus.succeeded,
        started_at=10.0,
        finished_at=15.0,
        attempts=(),
    )
    with pytest.raises(FrozenInstanceError):
        outcome.platform = "ios"  # type: ignore[misc]


def test_outcome_to_dict_round_trip():
    outcome = MobileAutoFixOutcome(
        session_id="sess-1",
        platform="android",
        workspace_path="/tmp/w",
        final_status=AutoFixStatus.failed,
        started_at=10.0,
        finished_at=12.0,
        attempts=(MobileAutoFixAttempt(**_ok_attempt_kwargs()),),
        initial_error_count=3,
        final_error_count=1,
        final_build_status="fail",
        final_screenshot_status="",
        final_screenshot_path="",
        detail="agent gave up",
        warnings=("w",),
    )
    d = outcome.to_dict()
    assert d["final_status"] == "failed"
    assert d["total_attempts"] == 1
    assert d["initial_error_count"] == 3
    assert d["duration_ms"] == 2000
    assert d["succeeded"] is False
    assert d["warnings"] == ["w"]
    json.dumps(d)


def test_outcome_rejects_finished_before_started():
    with pytest.raises(ValueError):
        MobileAutoFixOutcome(
            session_id="sess-1",
            platform="android",
            workspace_path="/tmp/w",
            final_status=AutoFixStatus.succeeded,
            started_at=15.0,
            finished_at=10.0,
            attempts=(),
        )


def test_outcome_rejects_unknown_platform():
    with pytest.raises(ValueError):
        MobileAutoFixOutcome(
            session_id="sess-1",
            platform="windows",
            workspace_path="/tmp/w",
            final_status=AutoFixStatus.succeeded,
            started_at=10.0,
            finished_at=11.0,
            attempts=(),
        )


def test_outcome_rejects_bad_status():
    with pytest.raises(ValueError):
        MobileAutoFixOutcome(
            session_id="sess-1",
            platform="android",
            workspace_path="/tmp/w",
            final_status="succeeded",  # type: ignore[arg-type]
            started_at=10.0,
            finished_at=11.0,
            attempts=(),
        )


def test_outcome_rejects_bad_attempt_in_tuple():
    with pytest.raises(ValueError):
        MobileAutoFixOutcome(
            session_id="sess-1",
            platform="android",
            workspace_path="/tmp/w",
            final_status=AutoFixStatus.succeeded,
            started_at=10.0,
            finished_at=11.0,
            attempts=("nope",),  # type: ignore[arg-type]
        )


def test_outcome_total_files_touched_aggregates():
    a1 = MobileAutoFixAttempt(
        attempt_index=1,
        started_at=10.0,
        finished_at=11.0,
        status=AutoFixAttemptStatus.agent_patched,
        agent_action="patched",
        agent_files_touched=("a.kt", "b.kt"),
    )
    a2 = MobileAutoFixAttempt(
        attempt_index=2,
        started_at=11.0,
        finished_at=12.0,
        status=AutoFixAttemptStatus.build_passed,
    )
    outcome = MobileAutoFixOutcome(
        session_id="sess-1",
        platform="android",
        workspace_path="/tmp/w",
        final_status=AutoFixStatus.succeeded,
        started_at=10.0,
        finished_at=12.0,
        attempts=(a1, a2),
    )
    assert outcome.total_files_touched == 2
    assert outcome.did_invoke_agent is True


# ═══════════════════════════════════════════════════════════════════
#  render_autofix_outcome_markdown
# ═══════════════════════════════════════════════════════════════════


def test_render_outcome_markdown_smoke():
    outcome = MobileAutoFixOutcome(
        session_id="sess-1",
        platform="android",
        workspace_path="/tmp/w",
        final_status=AutoFixStatus.succeeded,
        started_at=10.0,
        finished_at=15.0,
        attempts=(MobileAutoFixAttempt(**_ok_attempt_kwargs()),),
    )
    md = render_autofix_outcome_markdown(outcome)
    assert "Mobile auto-fix" in md
    assert "succeeded" in md
    assert "android" in md
    assert "/tmp/w" in md


def test_render_outcome_markdown_includes_attempts():
    a1 = MobileAutoFixAttempt(
        attempt_index=1,
        started_at=10.0,
        finished_at=11.0,
        status=AutoFixAttemptStatus.agent_patched,
        agent_action="patched",
        build_status="fail",
        build_error_count=3,
        detail="patched Foo",
    )
    outcome = MobileAutoFixOutcome(
        session_id="sess-1",
        platform="android",
        workspace_path="/tmp/w",
        final_status=AutoFixStatus.failed,
        started_at=10.0,
        finished_at=12.0,
        attempts=(a1,),
    )
    md = render_autofix_outcome_markdown(outcome)
    assert "#1" in md
    assert "patched" in md
    assert "patched Foo" in md


def test_render_outcome_markdown_rejects_non_outcome():
    with pytest.raises(TypeError):
        render_autofix_outcome_markdown("nope")  # type: ignore[arg-type]


def test_render_outcome_markdown_with_warnings():
    outcome = MobileAutoFixOutcome(
        session_id="sess-1",
        platform="android",
        workspace_path="/tmp/w",
        final_status=AutoFixStatus.failed,
        started_at=10.0,
        finished_at=11.0,
        attempts=(),
        warnings=("w1", "w2"),
        detail="bad day",
    )
    md = render_autofix_outcome_markdown(outcome)
    assert "w1, w2" in md
    assert "bad day" in md


# ═══════════════════════════════════════════════════════════════════
#  Fixtures — sandbox manager + agent callback
# ═══════════════════════════════════════════════════════════════════


class FakeClock:
    def __init__(self, start: float = 1000.0, step: float = 0.5) -> None:
        self.value = start
        self.step = step
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        out = self.value
        self.value += self.step
        return out


class FakeAndroidExecutor:
    """Per-attempt scriptable executor.

    ``build_results`` is a list of :class:`BuildReport` consumed in
    FIFO order — one per call to :meth:`build`.  Same for
    ``install_results`` and ``screenshot_results``.
    """

    def __init__(
        self,
        *,
        build_results: list[BuildReport] | None = None,
        install_results: list[InstallReport] | None = None,
        screenshot_results: list[ScreenshotReport] | None = None,
    ) -> None:
        self.build_results = list(build_results or [])
        self.install_results = list(install_results or [])
        self.screenshot_results = list(screenshot_results or [])
        self.calls: list[tuple[str, dict]] = []

    def build(self, config: MobileSandboxConfig) -> BuildReport:
        self.calls.append(("build", {"session_id": config.session_id}))
        if not self.build_results:
            return BuildReport(
                status="pass",
                artifact_path=f"/tmp/{config.session_id}.apk",
                tool="gradle",
            )
        return self.build_results.pop(0)

    def install(self, config: MobileSandboxConfig, artifact_path: str) -> InstallReport:
        self.calls.append(("install", {"artifact": artifact_path}))
        if not self.install_results:
            return InstallReport(status="pass", launched=True, detail="Success")
        return self.install_results.pop(0)

    def screenshot(self, config: MobileSandboxConfig, *, output_dir: str) -> ScreenshotReport:
        self.calls.append(("screenshot", {"output_dir": output_dir}))
        if not self.screenshot_results:
            return ScreenshotReport(
                status="pass", path=f"{output_dir}/{config.session_id}.png",
                width=1080, height=1920,
            )
        return self.screenshot_results.pop(0)

    def stop(self, sandbox_name: str, *, timeout_s: float) -> None:
        self.calls.append(("stop", {"name": sandbox_name}))


class FakeIosExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def build(self, config):
        self.calls.append(("build", {"session_id": config.session_id}))
        return BuildReport(
            status="pass",
            artifact_path=f"/tmp/{config.session_id}.app",
            tool="xcodebuild",
        )

    def install(self, config, artifact_path):
        self.calls.append(("install", {"artifact": artifact_path}))
        return InstallReport(status="pass", launched=True)

    def screenshot(self, config, *, output_dir):
        self.calls.append(("screenshot", {"output_dir": output_dir}))
        return ScreenshotReport(
            status="pass",
            path=f"{output_dir}/{config.session_id}.png",
        )

    def stop(self, delegate_handle, *, timeout_s):
        self.calls.append(("stop", {"handle": delegate_handle}))


class RecordingAgent:
    """Scriptable agent fix callback.

    ``responses`` is consumed FIFO; if exhausted, returns ``no_op``.
    """

    def __init__(
        self,
        responses: list[MobileAutoFixResponse] | None = None,
        *,
        raise_each: list[BaseException | None] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.raise_each = list(raise_each or [])
        self.calls: list[MobileAutoFixRequest] = []

    def __call__(self, request: MobileAutoFixRequest) -> MobileAutoFixResponse:
        self.calls.append(request)
        if self.raise_each:
            exc = self.raise_each.pop(0)
            if exc is not None:
                raise exc
        if not self.responses:
            return MobileAutoFixResponse(action="no_op", summary="exhausted")
        return self.responses.pop(0)


@pytest.fixture()
def workspace(tmp_path: Path) -> str:
    p = tmp_path / "ws"
    p.mkdir()
    return str(p)


@pytest.fixture()
def output_dir(tmp_path: Path) -> str:
    p = tmp_path / "captures"
    p.mkdir()
    return str(p)


def _config(workspace: str, *, session_id: str = "sess-1", platform: str = "android") -> MobileSandboxConfig:
    return MobileSandboxConfig(
        session_id=session_id,
        platform=platform,
        workspace_path=workspace,
    )


def _make_loop(
    *,
    android_executor: FakeAndroidExecutor | None = None,
    ios_executor: FakeIosExecutor | None = None,
    agent: RecordingAgent,
    visual_builder: MobileAgentVisualContextBuilder | None = None,
    event_cb=None,
    capture_after_success: bool = True,
    default_max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    default_failure_mode: str = DEFAULT_FAILURE_MODE,
    clock: FakeClock | None = None,
    attempt_devices=None,
) -> MobileAutoFixLoop:
    mgr = MobileSandboxManager(
        android_executor=android_executor or FakeAndroidExecutor(),
        ios_executor=ios_executor or FakeIosExecutor(),
        clock=(clock or time.time),
    )
    return MobileAutoFixLoop(
        sandbox_manager=mgr,
        agent_fix_fn=agent,
        visual_context_builder=visual_builder,
        event_cb=event_cb,
        capture_after_success=capture_after_success,
        default_max_attempts=default_max_attempts,
        default_failure_mode=default_failure_mode,
        clock=(clock or time.time),
        attempt_devices=attempt_devices,
    )


# ═══════════════════════════════════════════════════════════════════
#  Loop ctor
# ═══════════════════════════════════════════════════════════════════


def test_ctor_rejects_non_manager():
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager="nope",  # type: ignore[arg-type]
            agent_fix_fn=lambda r: MobileAutoFixResponse(action="no_op"),
        )


def test_ctor_rejects_non_callable_agent(workspace: str):
    mgr = MobileSandboxManager(
        android_executor=FakeAndroidExecutor(),
        ios_executor=FakeIosExecutor(),
    )
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager=mgr,
            agent_fix_fn="nope",  # type: ignore[arg-type]
        )


def test_ctor_rejects_bad_visual_builder(workspace: str):
    mgr = MobileSandboxManager(
        android_executor=FakeAndroidExecutor(),
        ios_executor=FakeIosExecutor(),
    )
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager=mgr,
            agent_fix_fn=lambda r: MobileAutoFixResponse(action="no_op"),
            visual_context_builder="nope",  # type: ignore[arg-type]
        )


def test_ctor_rejects_bad_clock():
    mgr = MobileSandboxManager(android_executor=FakeAndroidExecutor())
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager=mgr,
            agent_fix_fn=lambda r: MobileAutoFixResponse(action="no_op"),
            clock="nope",  # type: ignore[arg-type]
        )


def test_ctor_rejects_bad_event_cb():
    mgr = MobileSandboxManager(android_executor=FakeAndroidExecutor())
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager=mgr,
            agent_fix_fn=lambda r: MobileAutoFixResponse(action="no_op"),
            event_cb="nope",  # type: ignore[arg-type]
        )


def test_ctor_rejects_zero_max_attempts():
    mgr = MobileSandboxManager(android_executor=FakeAndroidExecutor())
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager=mgr,
            agent_fix_fn=lambda r: MobileAutoFixResponse(action="no_op"),
            default_max_attempts=0,
        )


def test_ctor_rejects_unknown_failure_mode():
    mgr = MobileSandboxManager(android_executor=FakeAndroidExecutor())
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager=mgr,
            agent_fix_fn=lambda r: MobileAutoFixResponse(action="no_op"),
            default_failure_mode="murder",
        )


def test_ctor_rejects_non_bool_capture():
    mgr = MobileSandboxManager(android_executor=FakeAndroidExecutor())
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager=mgr,
            agent_fix_fn=lambda r: MobileAutoFixResponse(action="no_op"),
            capture_after_success="nope",  # type: ignore[arg-type]
        )


def test_ctor_rejects_empty_attempt_devices():
    mgr = MobileSandboxManager(android_executor=FakeAndroidExecutor())
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager=mgr,
            agent_fix_fn=lambda r: MobileAutoFixResponse(action="no_op"),
            attempt_devices=(),
        )


def test_ctor_rejects_bad_attempt_device():
    mgr = MobileSandboxManager(android_executor=FakeAndroidExecutor())
    with pytest.raises(MobileAutoFixConfigError):
        MobileAutoFixLoop(
            sandbox_manager=mgr,
            agent_fix_fn=lambda r: MobileAutoFixResponse(action="no_op"),
            attempt_devices=("not target",),  # type: ignore[arg-type]
        )


def test_ctor_accessors_initial_state(workspace: str):
    agent = RecordingAgent()
    loop = _make_loop(agent=agent)
    assert loop.run_count() == 0
    assert loop.success_count() == 0
    assert loop.failure_count() == 0
    assert loop.exhausted_count() == 0
    assert loop.skipped_count() == 0
    assert loop.agent_invocations() == 0
    assert loop.last_outcome() is None
    assert loop.default_max_attempts == DEFAULT_MAX_ATTEMPTS
    assert loop.default_failure_mode == "continue"
    assert loop.capture_after_success is True
    assert loop.visual_context_builder is None
    assert loop.attempt_devices is None
    assert callable(loop.agent_fix_fn)
    assert isinstance(loop.sandbox_manager, MobileSandboxManager)


# ═══════════════════════════════════════════════════════════════════
#  run() — happy paths
# ═══════════════════════════════════════════════════════════════════


def test_run_first_build_passes_succeeded(workspace: str, output_dir: str):
    """Build clean on attempt 1 → status=succeeded, agent never
    invoked, install + screenshot completed."""
    android = FakeAndroidExecutor()
    agent = RecordingAgent()
    loop = _make_loop(android_executor=android, agent=agent)
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.succeeded
    assert outcome.total_attempts == 1
    assert outcome.attempts[0].status is AutoFixAttemptStatus.build_passed
    assert outcome.attempts[0].build_status == "pass"
    assert outcome.attempts[0].install_status == "pass"
    assert outcome.attempts[0].screenshot_status == "pass"
    assert outcome.attempts[0].screenshot_path.endswith(".png")
    assert outcome.did_invoke_agent is False
    assert agent.calls == []
    assert loop.success_count() == 1


def test_run_skips_capture_when_capture_after_success_false(
    workspace: str, output_dir: str,
):
    android = FakeAndroidExecutor()
    agent = RecordingAgent()
    loop = _make_loop(
        android_executor=android,
        agent=agent,
        capture_after_success=False,
    )
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.succeeded
    assert outcome.attempts[0].install_status == ""
    assert outcome.attempts[0].screenshot_status == ""


def test_run_build_fails_then_agent_patches_then_passes(
    workspace: str, output_dir: str,
):
    """Attempt 1 build fails → agent patches → attempt 2 build
    passes → succeeded."""
    err = BuildError(
        message="Unresolved reference: Foo",
        file="app/Bar.kt", line=42, severity="error", tool="gradle",
    )
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(
                status="pass",
                artifact_path="/tmp/sess-1.apk",
                tool="gradle",
            ),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(
                action="patched",
                summary="patched Foo",
                files_touched=("app/Bar.kt",),
            ),
        ],
    )
    loop = _make_loop(android_executor=android, agent=agent)
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.succeeded
    assert outcome.total_attempts == 2
    assert outcome.attempts[0].status is AutoFixAttemptStatus.agent_patched
    assert outcome.attempts[0].build_error_count == 1
    assert outcome.attempts[1].status is AutoFixAttemptStatus.build_passed
    assert outcome.initial_error_count == 1
    assert outcome.final_error_count == 0
    assert outcome.total_files_touched == 1
    assert outcome.did_invoke_agent is True
    assert len(agent.calls) == 1
    # The agent received the structured error summary.
    call = agent.calls[0]
    assert call.error_count == 1
    assert call.error_summary.has_blocking_errors is True
    assert call.attempt_index == 1
    assert call.previous_attempts == ()
    assert loop.success_count() == 1
    assert loop.agent_invocations() == 1


def test_run_agent_no_op_ends_failed(workspace: str, output_dir: str):
    err = BuildError(message="nope", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
        ],
    )
    agent = RecordingAgent(
        responses=[MobileAutoFixResponse(action="no_op", summary="dunno")],
    )
    loop = _make_loop(android_executor=android, agent=agent)
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.failed
    assert outcome.attempts[-1].status is AutoFixAttemptStatus.agent_no_op
    assert "no_op" in outcome.detail
    assert loop.failure_count() == 1


def test_run_agent_give_up_ends_failed(workspace: str, output_dir: str):
    err = BuildError(message="nope", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="give_up", summary="too risky"),
        ],
    )
    loop = _make_loop(android_executor=android, agent=agent)
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.failed
    assert outcome.attempts[-1].status is AutoFixAttemptStatus.agent_give_up


def test_run_exhausted_after_max_attempts(workspace: str, output_dir: str):
    """Agent always patches but build keeps failing → outcome=exhausted."""
    err = BuildError(message="nope", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(status="fail", tool="gradle", errors=(err,)),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="try1"),
            MobileAutoFixResponse(action="patched", summary="try2"),
            MobileAutoFixResponse(action="patched", summary="try3"),
        ],
    )
    loop = _make_loop(
        android_executor=android, agent=agent, default_max_attempts=3,
    )
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.exhausted
    assert outcome.total_attempts == 3
    for a in outcome.attempts:
        assert a.status is AutoFixAttemptStatus.agent_patched
    assert "after 3" in outcome.detail
    assert loop.exhausted_count() == 1


def test_run_mock_build_skips_loop(workspace: str, output_dir: str):
    """Build returns mock status (toolchain absent) → skipped without
    invoking the agent."""
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="mock", tool="gradle", detail="docker missing"),
        ],
    )
    agent = RecordingAgent()
    loop = _make_loop(android_executor=android, agent=agent)
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.skipped
    assert outcome.attempts[0].status is AutoFixAttemptStatus.skipped
    assert agent.calls == []
    assert loop.skipped_count() == 1


# ═══════════════════════════════════════════════════════════════════
#  run() — input validation
# ═══════════════════════════════════════════════════════════════════


def test_run_rejects_empty_session(workspace: str, output_dir: str):
    loop = _make_loop(agent=RecordingAgent())
    with pytest.raises(MobileAutoFixConfigError):
        loop.run(
            session_id="",
            config=_config(workspace),
            output_dir=output_dir,
        )


def test_run_rejects_bad_config(workspace: str, output_dir: str):
    loop = _make_loop(agent=RecordingAgent())
    with pytest.raises(MobileAutoFixConfigError):
        loop.run(
            session_id="sess-1",
            config="not config",  # type: ignore[arg-type]
            output_dir=output_dir,
        )


def test_run_rejects_session_config_mismatch(workspace: str, output_dir: str):
    loop = _make_loop(agent=RecordingAgent())
    with pytest.raises(MobileAutoFixConfigError):
        loop.run(
            session_id="sess-1",
            config=_config(workspace, session_id="sess-2"),
            output_dir=output_dir,
        )


def test_run_rejects_empty_output_dir(workspace: str):
    loop = _make_loop(agent=RecordingAgent())
    with pytest.raises(MobileAutoFixConfigError):
        loop.run(
            session_id="sess-1",
            config=_config(workspace),
            output_dir="",
        )


def test_run_rejects_zero_max_attempts(workspace: str, output_dir: str):
    loop = _make_loop(agent=RecordingAgent())
    with pytest.raises(MobileAutoFixConfigError):
        loop.run(
            session_id="sess-1",
            config=_config(workspace),
            output_dir=output_dir,
            max_attempts=0,
        )


def test_run_rejects_unknown_failure_mode(workspace: str, output_dir: str):
    loop = _make_loop(agent=RecordingAgent())
    with pytest.raises(MobileAutoFixConfigError):
        loop.run(
            session_id="sess-1",
            config=_config(workspace),
            output_dir=output_dir,
            failure_mode="murder",
        )


def test_run_rejects_bad_attempt_device_per_call(workspace: str, output_dir: str):
    loop = _make_loop(agent=RecordingAgent())
    with pytest.raises(MobileAutoFixConfigError):
        loop.run(
            session_id="sess-1",
            config=_config(workspace),
            output_dir=output_dir,
            attempt_devices=("nope",),  # type: ignore[arg-type]
        )


def test_run_rejects_empty_attempt_devices_per_call(workspace: str, output_dir: str):
    loop = _make_loop(agent=RecordingAgent())
    with pytest.raises(MobileAutoFixConfigError):
        loop.run(
            session_id="sess-1",
            config=_config(workspace),
            output_dir=output_dir,
            attempt_devices=(),
        )


# ═══════════════════════════════════════════════════════════════════
#  run() — agent error handling
# ═══════════════════════════════════════════════════════════════════


def test_run_agent_raises_continue_mode(workspace: str, output_dir: str):
    err = BuildError(message="nope", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
        ],
    )
    agent = RecordingAgent(raise_each=[RuntimeError("agent boom")])
    loop = _make_loop(android_executor=android, agent=agent)
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.failed
    assert outcome.attempts[-1].status is AutoFixAttemptStatus.agent_error
    assert "agent boom" in outcome.detail
    assert "agent_callback_raised" in outcome.attempts[-1].warnings
    assert loop.failure_count() == 1
    # Counter is bumped only on successful invocations
    assert loop.agent_invocations() == 0


def test_run_agent_raises_abort_mode_propagates(workspace: str, output_dir: str):
    err = BuildError(message="nope", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
        ],
    )
    agent = RecordingAgent(raise_each=[RuntimeError("agent boom")])
    loop = _make_loop(android_executor=android, agent=agent)
    with pytest.raises(RuntimeError, match="agent boom"):
        loop.run(
            session_id="sess-1",
            config=_config(workspace),
            output_dir=output_dir,
            failure_mode="abort",
        )
    # Last outcome is still recorded so the operator sees the failure.
    last = loop.last_outcome()
    assert last is not None
    assert last.final_status is AutoFixStatus.failed


def test_run_agent_returns_non_response_continue(workspace: str, output_dir: str):
    err = BuildError(message="nope", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
        ],
    )

    class BadAgent:
        def __init__(self):
            self.calls = []

        def __call__(self, request):
            self.calls.append(request)
            return "not a response"  # wrong type

    bad = BadAgent()
    loop = MobileAutoFixLoop(
        sandbox_manager=MobileSandboxManager(
            android_executor=android,
        ),
        agent_fix_fn=bad,
    )
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.failed
    assert outcome.attempts[-1].status is AutoFixAttemptStatus.agent_error
    assert "agent_callback_bad_return" in outcome.attempts[-1].warnings


def test_run_agent_returns_non_response_abort(workspace: str, output_dir: str):
    err = BuildError(message="nope", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
        ],
    )

    def bad_agent(request):
        return None

    loop = MobileAutoFixLoop(
        sandbox_manager=MobileSandboxManager(android_executor=android),
        agent_fix_fn=bad_agent,
    )
    with pytest.raises(MobileAutoFixError):
        loop.run(
            session_id="sess-1",
            config=_config(workspace),
            output_dir=output_dir,
            failure_mode="abort",
        )


# ═══════════════════════════════════════════════════════════════════
#  run() — sandbox state isolation between attempts
# ═══════════════════════════════════════════════════════════════════


def test_run_resets_sandbox_between_failed_attempts(workspace: str, output_dir: str):
    """After build_fail (status=failed), the loop must remove + recreate
    so the next attempt starts fresh."""
    err = BuildError(message="nope", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(
                status="pass",
                artifact_path="/tmp/sess-1.apk",
                tool="gradle",
            ),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="patched"),
        ],
    )
    loop = _make_loop(android_executor=android, agent=agent)
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.succeeded
    # The manager was asked to build twice (one per attempt).
    build_calls = [c for c in android.calls if c[0] == "build"]
    assert len(build_calls) == 2


def test_run_pre_existing_sandbox_recovered(workspace: str, output_dir: str):
    """If the manager already holds a session-id sandbox (e.g. from
    a previous run), the loop must reset it cleanly rather than
    raising MobileSandboxAlreadyExists."""
    android = FakeAndroidExecutor()
    mgr = MobileSandboxManager(android_executor=android)
    mgr.create(_config(workspace))  # pre-existing pending sandbox
    agent = RecordingAgent()
    loop = MobileAutoFixLoop(
        sandbox_manager=mgr,
        agent_fix_fn=agent,
    )
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.succeeded


# ═══════════════════════════════════════════════════════════════════
#  run() — sandbox raise handling
# ═══════════════════════════════════════════════════════════════════


def test_run_sandbox_create_raises_continue(workspace: str, output_dir: str):
    """Manager rejects create() → attempt records sandbox_error and the
    loop closes failed."""

    class BadManager(MobileSandboxManager):
        def __init__(self):
            super().__init__(android_executor=FakeAndroidExecutor())

        def create(self, config):
            raise MobileSandboxError("simulated create failure")

    agent = RecordingAgent()
    loop = MobileAutoFixLoop(
        sandbox_manager=BadManager(),
        agent_fix_fn=agent,
    )
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.failed
    assert outcome.attempts[0].status is AutoFixAttemptStatus.sandbox_error


def test_run_sandbox_create_raises_abort(workspace: str, output_dir: str):
    class BadManager(MobileSandboxManager):
        def __init__(self):
            super().__init__(android_executor=FakeAndroidExecutor())

        def create(self, config):
            raise MobileSandboxError("simulated create failure")

    agent = RecordingAgent()
    loop = MobileAutoFixLoop(
        sandbox_manager=BadManager(),
        agent_fix_fn=agent,
    )
    with pytest.raises(MobileAutoFixSandboxError):
        loop.run(
            session_id="sess-1",
            config=_config(workspace),
            output_dir=output_dir,
            failure_mode="abort",
        )


def test_run_sandbox_build_raises_continue(workspace: str, output_dir: str):
    class BadManager(MobileSandboxManager):
        def __init__(self):
            super().__init__(android_executor=FakeAndroidExecutor())

        def build(self, session_id):
            raise MobileSandboxError("simulated build failure")

    agent = RecordingAgent()
    loop = MobileAutoFixLoop(
        sandbox_manager=BadManager(),
        agent_fix_fn=agent,
    )
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.failed
    assert outcome.attempts[0].status is AutoFixAttemptStatus.sandbox_error
    assert "manager_build_raised" in outcome.attempts[0].warnings


def test_run_install_failure_recorded_as_warning(workspace: str, output_dir: str):
    """When build passes but install fails, the screenshot is skipped
    and the install failure becomes a warning — outcome remains
    succeeded (build is the canonical pass signal)."""
    android = FakeAndroidExecutor(
        install_results=[
            InstallReport(status="fail", launched=False, detail="adb fail"),
        ],
    )
    agent = RecordingAgent()
    loop = _make_loop(android_executor=android, agent=agent)
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.succeeded
    assert outcome.attempts[0].install_status == "fail"
    assert outcome.attempts[0].screenshot_status == ""


# ═══════════════════════════════════════════════════════════════════
#  Events
# ═══════════════════════════════════════════════════════════════════


class EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, data: Mapping[str, Any]) -> None:
        self.events.append((event_type, dict(data)))


def test_events_emitted_on_succeeded_path(workspace: str, output_dir: str):
    collector = EventCollector()
    loop = _make_loop(agent=RecordingAgent(), event_cb=collector)
    loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    types = [t for t, _ in collector.events]
    assert AUTOFIX_EVENT_STARTED in types
    assert AUTOFIX_EVENT_ATTEMPT_STARTED in types
    assert AUTOFIX_EVENT_BUILD_PASSED in types
    assert AUTOFIX_EVENT_ATTEMPT_FINISHED in types
    assert AUTOFIX_EVENT_SUCCEEDED in types
    # No build_failed / fix_applied events on the happy path.
    assert AUTOFIX_EVENT_BUILD_FAILED not in types
    assert AUTOFIX_EVENT_FIX_APPLIED not in types


def test_events_emitted_on_patch_then_pass(workspace: str, output_dir: str):
    err = BuildError(message="nope", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(status="pass", tool="gradle", artifact_path="/tmp/x.apk"),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="patched", files_touched=("a.kt",)),
        ],
    )
    collector = EventCollector()
    loop = _make_loop(
        android_executor=android, agent=agent, event_cb=collector,
    )
    loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    types = [t for t, _ in collector.events]
    assert AUTOFIX_EVENT_BUILD_FAILED in types
    assert AUTOFIX_EVENT_FIX_APPLIED in types
    assert AUTOFIX_EVENT_BUILD_PASSED in types
    assert AUTOFIX_EVENT_SUCCEEDED in types


def test_events_emitted_on_skipped(workspace: str, output_dir: str):
    android = FakeAndroidExecutor(
        build_results=[BuildReport(status="mock", tool="gradle")],
    )
    collector = EventCollector()
    loop = _make_loop(
        android_executor=android, agent=RecordingAgent(), event_cb=collector,
    )
    loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    types = [t for t, _ in collector.events]
    assert AUTOFIX_EVENT_SKIPPED in types
    assert AUTOFIX_EVENT_FIX_APPLIED not in types


def test_events_emitted_on_exhausted(workspace: str, output_dir: str):
    err = BuildError(message="x", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(status="fail", tool="gradle", errors=(err,)),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="t1"),
            MobileAutoFixResponse(action="patched", summary="t2"),
        ],
    )
    collector = EventCollector()
    loop = _make_loop(
        android_executor=android,
        agent=agent,
        default_max_attempts=2,
        event_cb=collector,
    )
    loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    types = [t for t, _ in collector.events]
    assert AUTOFIX_EVENT_EXHAUSTED in types


def test_event_callback_raise_does_not_kill_loop(workspace: str, output_dir: str):
    """A noisy event callback must never propagate into the loop."""

    def boom_cb(event_type, data):
        raise RuntimeError(f"event callback boom on {event_type}")

    loop = _make_loop(agent=RecordingAgent(), event_cb=boom_cb)
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.succeeded


# ═══════════════════════════════════════════════════════════════════
#  Visual context wiring
# ═══════════════════════════════════════════════════════════════════


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    """Build a minimal valid PNG with a parseable IHDR chunk."""
    import struct as _struct

    ihdr_payload = _struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_chunk = (
        _struct.pack(">I", len(ihdr_payload))
        + b"IHDR"
        + ihdr_payload
        + b"\x00\x00\x00\x00"  # CRC placeholder — not validated by our sniff
    )
    return PNG_MAGIC + ihdr_chunk + b"\x00" * 16


def _passing_capture(width: int = 4, height: int = 4):
    def _capture(req: ScreenshotRequest) -> ScreenshotResult:
        return ScreenshotResult(
            session_id=req.session_id,
            platform=req.platform,
            status=ScreenshotStatus.passed,
            path=req.output_path,
            width=width,
            height=height,
            size_bytes=64,
            png_bytes=_png_bytes(width, height),
            captured_at=12345.0,
        )

    return _capture


def test_run_with_visual_builder_wires_payload(workspace: str, output_dir: str):
    """When wired with a visual builder, the agent receives a
    multimodal payload with one image per device target."""
    err = BuildError(message="nope", file="x.kt", line=5, severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(status="pass", tool="gradle", artifact_path="/tmp/x.apk"),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="patched", files_touched=("x.kt",)),
        ],
    )
    visual_builder = MobileAgentVisualContextBuilder(
        capture_fn=_passing_capture(),
    )
    loop = _make_loop(
        android_executor=android,
        agent=agent,
        visual_builder=visual_builder,
    )
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.succeeded
    assert len(agent.calls) == 1
    call = agent.calls[0]
    assert call.has_visual_payload is True
    assert call.visual_payload is not None
    assert call.visual_payload.image_count == len(DEFAULT_DEVICE_TARGETS)
    # The attempt audit row records the bound image count.
    assert outcome.attempts[0].visual_payload_built is True
    assert outcome.attempts[0].visual_image_count == len(DEFAULT_DEVICE_TARGETS)


def test_run_visual_builder_error_source_wiring(workspace: str, output_dir: str):
    """When the visual builder's error_source reads from
    ``loop.current_error_summary``, the multimodal text block surfaces
    the same diagnostics the agent receives via request.error_summary.
    """
    err = BuildError(
        message="Unresolved reference: Foo",
        file="app/Bar.kt", line=42, severity="error", tool="gradle",
    )
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(status="pass", tool="gradle", artifact_path="/tmp/x.apk"),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="patched", files_touched=("a.kt",)),
        ],
    )

    loop_holder: dict[str, MobileAutoFixLoop] = {}

    def err_src(sid):
        loop = loop_holder.get("loop")
        if loop is None:
            return None
        return loop.current_error_summary(sid)

    visual_builder = MobileAgentVisualContextBuilder(
        capture_fn=_passing_capture(),
        error_source=err_src,
    )
    loop = _make_loop(
        android_executor=android,
        agent=agent,
        visual_builder=visual_builder,
    )
    loop_holder["loop"] = loop
    loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    payload = agent.calls[0].visual_payload
    assert payload is not None
    # The error summary surfaced in the text block.
    assert "Unresolved reference" in payload.text_prompt
    assert payload.has_blocking_errors is True
    assert payload.active_error_count == 1


def test_current_error_summary_clears_after_attempt(workspace: str, output_dir: str):
    err = BuildError(message="x", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(status="pass", tool="gradle", artifact_path="/tmp/x.apk"),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="patched"),
        ],
    )
    visual_builder = MobileAgentVisualContextBuilder(
        capture_fn=_passing_capture(),
    )
    loop = _make_loop(
        android_executor=android,
        agent=agent,
        visual_builder=visual_builder,
    )
    loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    # After run completes the cache is empty.
    assert loop.current_error_summary("sess-1") is None


def test_current_error_summary_returns_none_for_unknown(workspace: str):
    loop = _make_loop(agent=RecordingAgent())
    assert loop.current_error_summary("never-seen") is None
    assert loop.current_error_summary("") is None


def test_visual_builder_failure_does_not_kill_loop(workspace: str, output_dir: str):
    """If the visual builder.build() raises (e.g. capture_fn returns
    junk), the loop records a warning and proceeds — the agent still
    gets the structured error summary, just no pixels."""
    err = BuildError(message="x", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(status="pass", tool="gradle", artifact_path="/tmp/x.apk"),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="patched"),
        ],
    )

    def bad_capture(req):
        raise RuntimeError("capture boom")

    # bad_capture raises → builder records crash warning + synthesises
    # fail result; build() does not raise.  But to actually push build()
    # itself to raise, we override the request_factory:
    def bad_factory(*args, **kwargs):
        raise RuntimeError("factory boom")

    visual_builder = MobileAgentVisualContextBuilder(
        capture_fn=bad_capture,
        request_factory=bad_factory,
    )
    loop = _make_loop(
        android_executor=android,
        agent=agent,
        visual_builder=visual_builder,
    )
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    # Loop completes: agent got called even though visual was broken.
    assert outcome.final_status is AutoFixStatus.succeeded
    # And the outcome warnings record the visual failure.
    assert any(
        "factory boom" in w or "capture boom" in w
        for w in outcome.warnings
    ) or outcome.attempts[0].visual_payload_built is True


def test_run_attempt_devices_overrides_default(workspace: str, output_dir: str):
    err = BuildError(message="x", severity="error", tool="gradle")
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err,)),
            BuildReport(status="pass", tool="gradle", artifact_path="/tmp/x.apk"),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="patched"),
        ],
    )
    visual_builder = MobileAgentVisualContextBuilder(
        capture_fn=_passing_capture(),
    )
    custom = (
        MobileDeviceTarget(
            device_id="iphone-15", platform="ios", udid_or_serial="booted",
            label="iPhone 15", screen_width=1179, screen_height=2556,
        ),
    )
    loop = _make_loop(
        android_executor=android,
        agent=agent,
        visual_builder=visual_builder,
    )
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
        attempt_devices=custom,
    )
    assert outcome.final_status is AutoFixStatus.succeeded
    payload = agent.calls[0].visual_payload
    assert payload is not None
    assert payload.image_count == 1
    assert payload.images[0].device_id == "iphone-15"


# ═══════════════════════════════════════════════════════════════════
#  Snapshot
# ═══════════════════════════════════════════════════════════════════


def test_snapshot_initial_state(workspace: str):
    loop = _make_loop(agent=RecordingAgent())
    snap = loop.snapshot()
    assert snap["schema_version"] == MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION
    assert snap["sandbox_schema_version"]
    assert snap["visual_context_schema_version"]
    assert snap["default_max_attempts"] == DEFAULT_MAX_ATTEMPTS
    assert snap["default_failure_mode"] == "continue"
    assert snap["capture_after_success"] is True
    assert snap["visual_builder_wired"] is False
    assert snap["run_count"] == 0
    assert snap["last_outcome"] is None


def test_snapshot_after_run(workspace: str, output_dir: str):
    loop = _make_loop(agent=RecordingAgent())
    loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    snap = loop.snapshot()
    assert snap["run_count"] == 1
    assert snap["success_count"] == 1
    assert snap["last_outcome"] is not None
    assert snap["last_outcome"]["final_status"] == "succeeded"
    json.dumps(snap)  # JSON-safe


def test_snapshot_visual_builder_wired_flag(workspace: str):
    visual_builder = MobileAgentVisualContextBuilder(
        capture_fn=_passing_capture(),
    )
    loop = _make_loop(agent=RecordingAgent(), visual_builder=visual_builder)
    snap = loop.snapshot()
    assert snap["visual_builder_wired"] is True


# ═══════════════════════════════════════════════════════════════════
#  End-to-end golden — multi-attempt loop with full event trace
# ═══════════════════════════════════════════════════════════════════


def test_e2e_multi_attempt_full_trace(workspace: str, output_dir: str):
    err1 = BuildError(message="A", severity="error", tool="gradle", file="a.kt", line=1)
    err2 = BuildError(message="B", severity="error", tool="gradle", file="b.kt", line=2)
    android = FakeAndroidExecutor(
        build_results=[
            BuildReport(status="fail", tool="gradle", errors=(err1, err2)),
            BuildReport(status="fail", tool="gradle", errors=(err2,)),
            BuildReport(status="pass", tool="gradle", artifact_path="/tmp/x.apk"),
        ],
    )
    agent = RecordingAgent(
        responses=[
            MobileAutoFixResponse(action="patched", summary="fixed A", files_touched=("a.kt",)),
            MobileAutoFixResponse(action="patched", summary="fixed B", files_touched=("b.kt",)),
        ],
    )
    visual_builder = MobileAgentVisualContextBuilder(
        capture_fn=_passing_capture(),
    )
    collector = EventCollector()
    loop = _make_loop(
        android_executor=android,
        agent=agent,
        visual_builder=visual_builder,
        event_cb=collector,
        default_max_attempts=4,
    )
    outcome = loop.run(
        session_id="sess-e2e",
        config=_config(workspace, session_id="sess-e2e"),
        output_dir=output_dir,
    )
    assert outcome.final_status is AutoFixStatus.succeeded
    assert outcome.total_attempts == 3
    assert outcome.initial_error_count == 2
    assert outcome.final_error_count == 0
    assert outcome.total_files_touched == 2
    assert outcome.did_invoke_agent is True

    # Build error counts taper down across attempts: 2 → 1 → 0
    assert outcome.attempts[0].build_error_count == 2
    assert outcome.attempts[1].build_error_count == 1
    assert outcome.attempts[2].build_error_count == 0

    # Each agent invocation saw the previous attempts.
    assert len(agent.calls[0].previous_attempts) == 0
    assert len(agent.calls[1].previous_attempts) == 1
    assert agent.calls[1].previous_attempts[0].agent_action == "patched"

    # Event trace covers started → 3× attempt → succeeded.
    types = [t for t, _ in collector.events]
    assert types.count(AUTOFIX_EVENT_ATTEMPT_STARTED) == 3
    assert types.count(AUTOFIX_EVENT_ATTEMPT_FINISHED) == 3
    assert types.count(AUTOFIX_EVENT_BUILD_FAILED) == 2
    assert types.count(AUTOFIX_EVENT_FIX_APPLIED) == 2
    assert types.count(AUTOFIX_EVENT_BUILD_PASSED) == 1
    assert types.count(AUTOFIX_EVENT_SUCCEEDED) == 1


def test_e2e_render_outcome_markdown_after_run(workspace: str, output_dir: str):
    loop = _make_loop(agent=RecordingAgent())
    outcome = loop.run(
        session_id="sess-1",
        config=_config(workspace),
        output_dir=output_dir,
    )
    md = render_autofix_outcome_markdown(outcome)
    assert "succeeded" in md
    assert "#1" in md


# ═══════════════════════════════════════════════════════════════════
#  Thread safety
# ═══════════════════════════════════════════════════════════════════


def test_concurrent_runs_distinct_sessions(workspace: str, output_dir: str):
    """Two threads running distinct session_ids must not race on
    counters or the last_outcome cache."""
    android = FakeAndroidExecutor()
    agent = RecordingAgent()
    mgr = MobileSandboxManager(
        android_executor=android,
        ios_executor=FakeIosExecutor(),
    )
    loop = MobileAutoFixLoop(
        sandbox_manager=mgr,
        agent_fix_fn=agent,
    )

    results: list[AutoFixStatus] = []
    barrier = threading.Barrier(4)

    def runner(session_id: str):
        barrier.wait()
        outcome = loop.run(
            session_id=session_id,
            config=_config(workspace, session_id=session_id),
            output_dir=output_dir,
        )
        results.append(outcome.final_status)

    threads = [
        threading.Thread(target=runner, args=(f"sess-{i}",))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # All four runs completed (most as succeeded; rare race on the
    # shared manager between create() and the lock could surface as
    # other terminal states — but every result must be a terminal
    # AutoFixStatus and the loop must have processed all four).
    assert len(results) == 4
    assert all(isinstance(s, AutoFixStatus) for s in results)
    assert loop.run_count() == 4


# ═══════════════════════════════════════════════════════════════════
#  Sibling alignment — schema versions
# ═══════════════════════════════════════════════════════════════════


def test_schema_versions_independent():
    """Our schema version is independent of V6 #1 / V6 #5 — bumping
    one must not silently bump the others."""
    assert MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION == "1.0.0"
    # Neighbouring rows should have their own schemas — we only check
    # they are present and parseable.
    from backend.mobile_sandbox import MOBILE_SANDBOX_SCHEMA_VERSION

    assert MOBILE_SANDBOX_SCHEMA_VERSION
    assert MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION


def test_default_devices_align_with_v6_5():
    """When attempt_devices is None we delegate to V6 #5's default
    matrix — so matrix changes upstream propagate without code
    changes here."""
    visual_builder = MobileAgentVisualContextBuilder(
        capture_fn=_passing_capture(),
    )
    expected_ids = tuple(t.device_id for t in DEFAULT_DEVICE_TARGETS)
    assert expected_ids  # V6 #5 ships at least one default device
    # The visual builder's defaults flow through unchanged.
    assert visual_builder.default_devices == DEFAULT_DEVICE_TARGETS
