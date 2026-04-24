"""V7 row 2689 (#323 second bullet) — ``mobile_iteration_timeline`` contract tests.

Pins ``backend/mobile_iteration_timeline.py`` against the V7 row spec:

* every modification recorded against a session produces one
  :class:`IterationEntry` with the unified diff + multi-device
  screenshots + monotonic version;
* :class:`MobileIterationTimelineBuilder` never raises on well-formed
  input, trims oversize payloads via warnings, and keeps ``version``
  monotonic across ring-buffer churn;
* events fire in the ``mobile_workspace.iteration_timeline.*``
  namespace with zero overlap with V6 #1 / V6 #5 / V6 #6 / V7 #1;
* ``to_dict`` output is JSON-safe (no bytes, no non-primitive types);
* concurrent records on distinct sessions produce coherent timelines;
* :func:`parse_diff_stats` is tolerant of empty / non-diff text and
  strict on obvious unified-diff shapes.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from backend.mobile_iteration_timeline import (
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_MAX_ENTRIES_PER_SESSION,
    DEFAULT_MAX_SCREENSHOTS_PER_ENTRY,
    DEFAULT_MAX_SUMMARY_CHARS,
    MOBILE_ITERATION_TIMELINE_EVENT_RECORDED,
    MOBILE_ITERATION_TIMELINE_EVENT_RECORD_FAILED,
    MOBILE_ITERATION_TIMELINE_EVENT_RECORDING,
    MOBILE_ITERATION_TIMELINE_EVENT_RESET,
    MOBILE_ITERATION_TIMELINE_EVENT_TYPES,
    MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION,
    SUPPORTED_SCREENSHOT_PLATFORMS,
    SUPPORTED_SCREENSHOT_STATUSES,
    IterationDiffStats,
    IterationEntry,
    IterationScreenshot,
    IterationTimeline,
    MobileIterationTimelineBuilder,
    MobileIterationTimelineConfigError,
    MobileIterationTimelineError,
    MobileIterationTimelineNotFoundError,
    format_iteration_entry_id,
    parse_diff_stats,
    render_iteration_entry_markdown,
    render_timeline_markdown,
    screenshot_from_result,
)


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════


class EventRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, topic: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._events.append((topic, dict(payload)))

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return list(self._events)

    def topics(self) -> list[str]:
        with self._lock:
            return [t for t, _ in self._events]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        t = self._t
        self._t += 1.0
        return t


class _FakeScreenshotResult:
    """Lookalike of V6 #2 ``ScreenshotResult`` used by
    :func:`screenshot_from_result` tests."""

    def __init__(
        self,
        *,
        status: str,
        platform: str = "ios",
        path: str = "",
        width: int = 0,
        height: int = 0,
        size_bytes: int = 0,
        captured_at: float = 0.0,
        format: str = "png",
        detail: str = "",
        png_bytes: bytes = b"",
    ) -> None:
        self.status = status
        self.platform = platform
        self.path = path
        self.width = width
        self.height = height
        self.size_bytes = size_bytes
        self.captured_at = captured_at
        self.format = format
        self.detail = detail
        self.png_bytes = png_bytes


class _FakeTarget:
    def __init__(self, device_id: str, label: str = "") -> None:
        self.device_id = device_id
        self.label = label or device_id


def make_shot(
    *,
    device_id: str = "iphone-15",
    platform: str = "ios",
    label: str = "",
    status: str = "pass",
    width: int = 1179,
    height: int = 2556,
    byte_len: int = 45_678,
    captured_at: float = 1000.0,
    detail: str = "",
    image_base64: str = "",
) -> IterationScreenshot:
    return IterationScreenshot(
        device_id=device_id,
        platform=platform,
        label=label or device_id,
        status=status,
        path=f"/tmp/{device_id}.png" if status == "pass" else "",
        format="png",
        width=width if status == "pass" else 0,
        height=height if status == "pass" else 0,
        byte_len=byte_len if status == "pass" else 0,
        captured_at=captured_at,
        detail=detail,
        image_base64=image_base64,
    )


SAMPLE_DIFF = """diff --git a/MyView.swift b/MyView.swift
--- a/MyView.swift
+++ b/MyView.swift
@@ -1,4 +1,5 @@
 struct MyView: View {
+    @State var count = 0
     var body: some View {
-        Text("old")
+        Text("new")
     }
 }
"""


# ═══════════════════════════════════════════════════════════════════
#  Module invariants
# ═══════════════════════════════════════════════════════════════════


class TestModuleInvariants:
    def test_schema_version_is_semver_one_point_zero(self) -> None:
        assert MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION == "1.0.0"

    def test_event_types_tuple_is_four_and_unique(self) -> None:
        assert len(MOBILE_ITERATION_TIMELINE_EVENT_TYPES) == 4
        assert len(set(MOBILE_ITERATION_TIMELINE_EVENT_TYPES)) == 4

    def test_event_types_all_under_mobile_workspace_prefix(self) -> None:
        for topic in MOBILE_ITERATION_TIMELINE_EVENT_TYPES:
            assert topic.startswith("mobile_workspace.iteration_timeline.")

    def test_event_namespace_disjoint_from_v6_topics(self) -> None:
        # V6 #1 mobile_sandbox states + V6 #5 agent_visual_context + V6 #6 autofix
        v6_topics = {
            "mobile_sandbox.created",
            "mobile_sandbox.building",
            "mobile_sandbox.built",
            "mobile_sandbox.failed",
            "mobile_sandbox.agent_visual_context.building",
            "mobile_sandbox.agent_visual_context.built",
            "mobile_sandbox.agent_visual_context.failed",
            "mobile_sandbox.agent_visual_context.skipped",
            "mobile_sandbox.autofix.started",
            "mobile_sandbox.autofix.succeeded",
            "mobile_sandbox.autofix.failed",
        }
        assert set(MOBILE_ITERATION_TIMELINE_EVENT_TYPES).isdisjoint(v6_topics)

    def test_event_namespace_disjoint_from_v7_annotation_topics(self) -> None:
        v7_annotation_topics = {
            "ui_sandbox.mobile_annotation_context.building",
            "ui_sandbox.mobile_annotation_context.built",
            "ui_sandbox.mobile_annotation_context.empty",
        }
        assert set(MOBILE_ITERATION_TIMELINE_EVENT_TYPES).isdisjoint(
            v7_annotation_topics
        )

    def test_supported_screenshot_statuses_match_v6_enum(self) -> None:
        assert SUPPORTED_SCREENSHOT_STATUSES == ("pass", "fail", "skip", "mock")

    def test_supported_screenshot_platforms_match_v6(self) -> None:
        assert SUPPORTED_SCREENSHOT_PLATFORMS == ("android", "ios")

    def test_default_limits_are_sane(self) -> None:
        assert DEFAULT_MAX_ENTRIES_PER_SESSION >= 10
        assert DEFAULT_MAX_DIFF_CHARS >= 10_000
        assert DEFAULT_MAX_SCREENSHOTS_PER_ENTRY >= 2
        assert DEFAULT_MAX_SUMMARY_CHARS >= 200

    def test_error_hierarchy(self) -> None:
        assert issubclass(MobileIterationTimelineError, RuntimeError)
        assert issubclass(
            MobileIterationTimelineConfigError, MobileIterationTimelineError
        )
        assert issubclass(
            MobileIterationTimelineNotFoundError, MobileIterationTimelineError
        )

    def test_all_export_roster_is_complete(self) -> None:
        from backend import mobile_iteration_timeline as mod

        for name in mod.__all__:
            assert hasattr(mod, name), f"missing export: {name}"


# ═══════════════════════════════════════════════════════════════════
#  parse_diff_stats
# ═══════════════════════════════════════════════════════════════════


class TestParseDiffStats:
    def test_empty_string_zero_stats(self) -> None:
        stats = parse_diff_stats("")
        assert stats == IterationDiffStats(0, 0, 0)

    def test_none_returns_zero_stats(self) -> None:
        stats = parse_diff_stats(None)
        assert stats == IterationDiffStats(0, 0, 0)

    def test_non_string_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            parse_diff_stats(123)  # type: ignore[arg-type]

    def test_simple_unified_diff(self) -> None:
        stats = parse_diff_stats(SAMPLE_DIFF)
        assert stats.files_changed == 1
        assert stats.additions == 2
        assert stats.deletions == 1
        assert stats.net_change == 1
        assert stats.total_lines_touched == 3

    def test_multi_file_diff(self) -> None:
        diff = (
            "diff --git a/A.swift b/A.swift\n"
            "--- a/A.swift\n"
            "+++ b/A.swift\n"
            "@@ -1 +1,2 @@\n"
            " keep\n"
            "+newline\n"
            "diff --git a/B.kt b/B.kt\n"
            "--- a/B.kt\n"
            "+++ b/B.kt\n"
            "@@ -1,2 +1 @@\n"
            "-removed\n"
            " keep\n"
        )
        stats = parse_diff_stats(diff)
        assert stats.files_changed == 2
        assert stats.additions == 1
        assert stats.deletions == 1

    def test_ignores_triple_plus_and_minus_headers(self) -> None:
        diff = "+++ b/x.py\n--- a/x.py\n@@ -1 +1 @@\n+a\n-b\n"
        stats = parse_diff_stats(diff)
        assert stats.files_changed == 1
        assert stats.additions == 1
        assert stats.deletions == 1

    def test_crlf_line_endings(self) -> None:
        diff = "diff --git a/x b/x\r\n+++ b/x\r\n@@ -1 +1 @@\r\n+new\r\n-old\r\n"
        stats = parse_diff_stats(diff)
        assert stats.files_changed == 1
        assert stats.additions == 1
        assert stats.deletions == 1

    def test_non_diff_text_returns_zero(self) -> None:
        stats = parse_diff_stats("just a story about a button")
        assert stats == IterationDiffStats(0, 0, 0)

    def test_diff_git_header_without_plus_plus_plus(self) -> None:
        # Some minimal diff emitters skip the +++/--- pair for binary files
        diff = "diff --git a/icon.png b/icon.png\nBinary files differ\n"
        stats = parse_diff_stats(diff)
        assert stats.files_changed == 1
        assert stats.additions == 0
        assert stats.deletions == 0

    def test_lines_outside_hunks_are_ignored(self) -> None:
        # Lines that start with + / - but are not inside an @@ hunk
        # should not be counted as additions / deletions.
        diff = (
            "preamble\n"
            "+ not inside a hunk\n"
            "- also not in a hunk\n"
            "diff --git a/x b/x\n"
            "+++ b/x\n"
            "@@ -1 +1 @@\n"
            "+real add\n"
            "-real del\n"
        )
        stats = parse_diff_stats(diff)
        assert stats.additions == 1
        assert stats.deletions == 1
        assert stats.files_changed == 1


# ═══════════════════════════════════════════════════════════════════
#  format_iteration_entry_id
# ═══════════════════════════════════════════════════════════════════


class TestFormatIterationEntryId:
    def test_happy_case(self) -> None:
        assert format_iteration_entry_id("sess-abc", 1) == "iter-sess-abc-0001"

    def test_zero_pads_to_four_digits(self) -> None:
        assert format_iteration_entry_id("s", 42) == "iter-s-0042"
        assert format_iteration_entry_id("s", 9999) == "iter-s-9999"
        assert format_iteration_entry_id("s", 10_000) == "iter-s-10000"

    def test_sanitises_unsafe_characters(self) -> None:
        result = format_iteration_entry_id("weird/session::id 42", 3)
        assert result.startswith("iter-")
        assert result.endswith("-0003")
        # No /, :, or space in the result.
        for bad in "/: ":
            assert bad not in result

    def test_trailing_unsafe_chars_stripped(self) -> None:
        # trailing '/' sanitised to '-', then strip("-") removes it.
        assert format_iteration_entry_id("sess/", 1) == "iter-sess-0001"

    def test_empty_session_id_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            format_iteration_entry_id("", 1)
        with pytest.raises(MobileIterationTimelineConfigError):
            format_iteration_entry_id("   ", 1)

    def test_zero_or_negative_version_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            format_iteration_entry_id("sess", 0)
        with pytest.raises(MobileIterationTimelineConfigError):
            format_iteration_entry_id("sess", -1)

    def test_bool_version_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            format_iteration_entry_id("sess", True)  # type: ignore[arg-type]

    def test_fallback_session_id_when_fully_sanitised(self) -> None:
        # All unsafe chars → fallback to literal "session".
        assert format_iteration_entry_id("///", 1) == "iter-session-0001"


# ═══════════════════════════════════════════════════════════════════
#  IterationDiffStats
# ═══════════════════════════════════════════════════════════════════


class TestIterationDiffStats:
    def test_defaults_zero(self) -> None:
        s = IterationDiffStats()
        assert (s.files_changed, s.additions, s.deletions) == (0, 0, 0)

    def test_frozen(self) -> None:
        s = IterationDiffStats(1, 1, 1)
        with pytest.raises((AttributeError, TypeError)):
            s.additions = 9  # type: ignore[misc]

    def test_net_change_and_totals(self) -> None:
        s = IterationDiffStats(2, 10, 3)
        assert s.net_change == 7
        assert s.total_lines_touched == 13

    def test_negative_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationDiffStats(-1, 0, 0)
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationDiffStats(0, -1, 0)
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationDiffStats(0, 0, -1)

    def test_non_int_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationDiffStats("1", 0, 0)  # type: ignore[arg-type]
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationDiffStats(0, True, 0)  # type: ignore[arg-type]

    def test_to_dict_shape(self) -> None:
        s = IterationDiffStats(2, 5, 3)
        d = s.to_dict()
        assert d == {
            "files_changed": 2,
            "additions": 5,
            "deletions": 3,
            "net_change": 2,
            "total_lines_touched": 8,
        }
        assert json.dumps(d)


# ═══════════════════════════════════════════════════════════════════
#  IterationScreenshot
# ═══════════════════════════════════════════════════════════════════


class TestIterationScreenshot:
    def test_happy_pass(self) -> None:
        s = make_shot()
        assert s.has_real_capture is True
        assert s.has_image_bytes is False

    def test_label_defaults_to_device_id(self) -> None:
        s = IterationScreenshot(device_id="pixel-8", platform="android")
        assert s.label == "pixel-8"

    def test_platform_lowercased(self) -> None:
        s = IterationScreenshot(device_id="x", platform="IOS")
        assert s.platform == "ios"

    def test_frozen(self) -> None:
        s = make_shot()
        with pytest.raises((AttributeError, TypeError)):
            s.status = "fail"  # type: ignore[misc]

    @pytest.mark.parametrize("bad_device", ["", "   ", "weird/device", "x" * 65])
    def test_bad_device_id_raises(self, bad_device: str) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(device_id=bad_device, platform="ios")

    def test_bad_platform_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(device_id="x", platform="windows")

    def test_empty_platform_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(device_id="x", platform="")

    def test_bad_status_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(device_id="x", platform="ios", status="weird")

    def test_negative_dimensions_raise(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(device_id="x", platform="ios", width=-1)
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(device_id="x", platform="ios", height=-1)
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(device_id="x", platform="ios", byte_len=-1)

    def test_negative_captured_at_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(
                device_id="x", platform="ios", captured_at=-1.0
            )

    def test_empty_format_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(device_id="x", platform="ios", format="")

    def test_non_string_detail_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(
                device_id="x", platform="ios", detail=5  # type: ignore[arg-type]
            )

    def test_non_string_label_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(
                device_id="x", platform="ios", label=5  # type: ignore[arg-type]
            )

    def test_non_string_path_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(
                device_id="x", platform="ios", path=5  # type: ignore[arg-type]
            )

    def test_non_string_image_base64_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationScreenshot(
                device_id="x", platform="ios", image_base64=5  # type: ignore[arg-type]
            )

    def test_to_dict_default_hides_image_base64(self) -> None:
        s = make_shot(image_base64="abc")
        d = s.to_dict()
        assert "image_base64" not in d
        assert d["has_image_bytes"] is True
        assert d["has_real_capture"] is True
        assert json.dumps(d)

    def test_to_dict_include_image_base64(self) -> None:
        s = make_shot(image_base64="abc")
        d = s.to_dict(include_image_base64=True)
        assert d["image_base64"] == "abc"

    def test_has_real_capture_is_false_on_mock(self) -> None:
        s = make_shot(status="mock")
        assert s.has_real_capture is False


# ═══════════════════════════════════════════════════════════════════
#  screenshot_from_result
# ═══════════════════════════════════════════════════════════════════


class TestScreenshotFromResult:
    def test_happy_pass_result(self) -> None:
        res = _FakeScreenshotResult(
            status="pass",
            platform="ios",
            path="/tmp/x.png",
            width=1179,
            height=2556,
            size_bytes=9999,
            captured_at=1234.5,
        )
        shot = screenshot_from_result(
            device_id="iphone-15", label="iPhone 15", result=res
        )
        assert shot.status == "pass"
        assert shot.platform == "ios"
        assert shot.width == 1179
        assert shot.height == 2556
        assert shot.byte_len == 9999
        assert shot.captured_at == 1234.5
        assert shot.has_image_bytes is False

    def test_status_may_be_enum_like(self) -> None:
        class _StatusLike:
            value = "fail"

        res = _FakeScreenshotResult(status="pass", platform="android")
        res.status = _StatusLike()
        shot = screenshot_from_result(
            device_id="pixel-8", label="", result=res
        )
        assert shot.status == "fail"

    def test_none_result_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            screenshot_from_result(device_id="x", label="", result=None)

    def test_missing_status_raises(self) -> None:
        class _Bad:
            platform = "ios"

        with pytest.raises(MobileIterationTimelineConfigError):
            screenshot_from_result(device_id="x", label="", result=_Bad())

    def test_missing_platform_raises(self) -> None:
        res = _FakeScreenshotResult(status="pass")
        res.platform = None  # type: ignore[assignment]
        with pytest.raises(MobileIterationTimelineConfigError):
            screenshot_from_result(device_id="x", label="", result=res)

    def test_include_image_base64_encodes_png_bytes(self) -> None:
        import base64

        res = _FakeScreenshotResult(
            status="pass", platform="ios", png_bytes=b"\x89PNGfoobar"
        )
        shot = screenshot_from_result(
            device_id="x", label="", result=res, include_image_base64=True
        )
        assert shot.image_base64 == base64.b64encode(b"\x89PNGfoobar").decode()
        assert shot.byte_len == len(b"\x89PNGfoobar")

    def test_label_defaults_to_device_id(self) -> None:
        res = _FakeScreenshotResult(status="pass", platform="ios")
        shot = screenshot_from_result(device_id="iphone-se", label="", result=res)
        assert shot.label == "iphone-se"


# ═══════════════════════════════════════════════════════════════════
#  IterationEntry
# ═══════════════════════════════════════════════════════════════════


class TestIterationEntry:
    def _build(self, **overrides: Any) -> IterationEntry:
        defaults: dict[str, Any] = dict(
            session_id="sess-abc",
            version=1,
            entry_id="iter-sess-abc-0001",
            created_at=1000.0,
            code_diff=SAMPLE_DIFF,
            diff_stats=IterationDiffStats(1, 2, 1),
            screenshots=(make_shot(),),
        )
        defaults.update(overrides)
        return IterationEntry(**defaults)

    def test_happy_construction(self) -> None:
        entry = self._build()
        assert entry.version == 1
        assert entry.screenshot_count == 1
        assert entry.real_capture_count == 1
        assert entry.platforms == ("ios",)
        assert entry.device_ids == ("iphone-15",)

    def test_frozen(self) -> None:
        entry = self._build()
        with pytest.raises((AttributeError, TypeError)):
            entry.version = 99  # type: ignore[misc]

    def test_bad_session_id_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(session_id="")
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(session_id="bad session!")

    def test_bad_version_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(version=0)
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(version=-1)
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(version=True)

    def test_bad_entry_id_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(entry_id="")

    def test_bad_created_at_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(created_at=-1.0)
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(created_at=True)

    def test_bad_code_diff_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(code_diff=None)

    def test_bad_diff_stats_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(diff_stats={"a": 1})  # type: ignore[arg-type]

    def test_screenshots_must_be_tuple(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(screenshots=[make_shot()])  # type: ignore[arg-type]

    def test_screenshots_entries_must_be_typed(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(screenshots=("not a shot",))  # type: ignore[arg-type]

    def test_bad_parent_version_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(version=3, parent_version=3)
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(version=3, parent_version=-1)
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(version=1, parent_version=True)

    def test_parent_version_zero_is_ok(self) -> None:
        entry = self._build(version=1, parent_version=0)
        assert entry.parent_version == 0

    def test_bad_tags_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(tags=["ok", ""])  # type: ignore[arg-type]
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(tags=("ok", None))  # type: ignore[arg-type]

    def test_bad_warnings_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(warnings=("",))

    def test_bad_metadata_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(metadata="nope")  # type: ignore[arg-type]
        with pytest.raises(MobileIterationTimelineConfigError):
            self._build(metadata={5: "bad"})  # type: ignore[arg-type]

    def test_to_dict_round_trips(self) -> None:
        entry = self._build(
            screenshots=(
                make_shot(device_id="iphone-15"),
                make_shot(device_id="pixel-8", platform="android", status="mock", width=0, height=0, byte_len=0),
            ),
            tags=("feat",),
            metadata={"pr": "PR-42"},
        )
        d = entry.to_dict()
        blob = json.dumps(d)
        assert json.loads(blob) == d
        assert d["schema_version"] == MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION
        assert d["device_ids"] == ["iphone-15", "pixel-8"]
        assert d["platforms"] == ["ios", "android"]
        assert d["screenshot_count"] == 2
        assert d["real_capture_count"] == 1

    def test_to_dict_image_bytes_opt_in(self) -> None:
        entry = self._build(
            screenshots=(make_shot(image_base64="xyz"),),
        )
        d_no = entry.to_dict()
        d_yes = entry.to_dict(include_image_base64=True)
        assert "image_base64" not in d_no["screenshots"][0]
        assert d_yes["screenshots"][0]["image_base64"] == "xyz"


# ═══════════════════════════════════════════════════════════════════
#  IterationTimeline
# ═══════════════════════════════════════════════════════════════════


class TestIterationTimeline:
    def test_empty_timeline(self) -> None:
        tl = IterationTimeline(
            session_id="s", created_at=1.0, updated_at=1.0
        )
        assert tl.is_empty
        assert tl.entry_count == 0
        assert tl.total_screenshots == 0
        assert tl.latest_entry is None
        assert tl.first_entry is None

    def test_frozen(self) -> None:
        tl = IterationTimeline(session_id="s", created_at=1.0, updated_at=1.0)
        with pytest.raises((AttributeError, TypeError)):
            tl.dropped_count = 9  # type: ignore[misc]

    def test_updated_at_less_than_created_at_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationTimeline(session_id="s", created_at=10.0, updated_at=1.0)

    def test_entries_must_match_session_id(self) -> None:
        entry = IterationEntry(
            session_id="s1",
            version=1,
            entry_id="iter-s1-0001",
            created_at=1.0,
            code_diff="",
            diff_stats=IterationDiffStats(),
        )
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationTimeline(
                session_id="s2",
                created_at=1.0,
                updated_at=1.0,
                entries=(entry,),
            )

    def test_entries_must_be_tuple(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationTimeline(
                session_id="s", created_at=1.0, updated_at=1.0, entries=[]
            )

    def test_to_dict_round_trips(self) -> None:
        entry = IterationEntry(
            session_id="s",
            version=1,
            entry_id="iter-s-0001",
            created_at=1.0,
            code_diff="diff",
            diff_stats=IterationDiffStats(),
            screenshots=(make_shot(device_id="iphone-15"),),
        )
        tl = IterationTimeline(
            session_id="s",
            created_at=1.0,
            updated_at=2.0,
            entries=(entry,),
            next_version=2,
        )
        d = tl.to_dict()
        assert json.loads(json.dumps(d)) == d
        assert d["entry_count"] == 1
        assert d["total_screenshots"] == 1
        assert d["next_version"] == 2
        assert "code_diff" in d["entries"][0]

    def test_to_dict_without_code_diff(self) -> None:
        entry = IterationEntry(
            session_id="s",
            version=1,
            entry_id="iter-s-0001",
            created_at=1.0,
            code_diff="diff",
            diff_stats=IterationDiffStats(),
        )
        tl = IterationTimeline(
            session_id="s", created_at=1.0, updated_at=1.0, entries=(entry,)
        )
        d = tl.to_dict(include_code_diff=False)
        assert "code_diff" not in d["entries"][0]

    def test_bad_dropped_count(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationTimeline(
                session_id="s",
                created_at=1.0,
                updated_at=1.0,
                dropped_count=-1,
            )

    def test_bad_next_version(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            IterationTimeline(
                session_id="s",
                created_at=1.0,
                updated_at=1.0,
                next_version=0,
            )


# ═══════════════════════════════════════════════════════════════════
#  Rendering
# ═══════════════════════════════════════════════════════════════════


class TestRenderIterationEntryMarkdown:
    def _entry(self, **overrides: Any) -> IterationEntry:
        defaults: dict[str, Any] = dict(
            session_id="s",
            version=3,
            entry_id="iter-s-0003",
            created_at=1.0,
            code_diff="",
            diff_stats=IterationDiffStats(2, 5, 1),
            screenshots=(
                make_shot(device_id="iphone-15", width=1179, height=2556, byte_len=10_240),
                make_shot(
                    device_id="pixel-8",
                    platform="android",
                    status="mock",
                    detail="adb missing",
                    width=0,
                    height=0,
                    byte_len=0,
                ),
            ),
            summary="Fix button spacing",
            author="agent-software-beta",
            parent_version=2,
            tags=("feat", "v7"),
        )
        defaults.update(overrides)
        return IterationEntry(**defaults)

    def test_header_and_summary(self) -> None:
        text = render_iteration_entry_markdown(self._entry())
        assert "### Iteration #3 — `iter-s-0003`" in text
        assert "**Summary:** Fix button spacing" in text
        assert "**Author:** agent-software-beta" in text

    def test_parent_version_line(self) -> None:
        text = render_iteration_entry_markdown(self._entry())
        assert "**Parent:** iteration #2" in text

    def test_diff_and_tags_lines(self) -> None:
        text = render_iteration_entry_markdown(self._entry())
        assert "**Diff:** 2 file(s), +5 / -1 (net +4)" in text
        assert "**Tags:** feat, v7" in text

    def test_screenshots_section(self) -> None:
        text = render_iteration_entry_markdown(self._entry())
        assert "**Screenshots:**" in text
        assert "[pass] iphone-15 (iphone-15, ios) — 1179×2556px" in text
        assert "[mock] pixel-8 (pixel-8, android) — unknownpx" in text
        assert "adb missing" in text

    def test_no_screenshots_placeholder(self) -> None:
        text = render_iteration_entry_markdown(
            self._entry(screenshots=())
        )
        assert "**Screenshots:** none recorded this turn" in text

    def test_warnings_section(self) -> None:
        text = render_iteration_entry_markdown(
            self._entry(warnings=("code_diff_truncated:1000:200000",))
        )
        assert "**Warnings:**" in text
        assert "code_diff_truncated" in text

    def test_deterministic_output(self) -> None:
        entry = self._entry()
        assert render_iteration_entry_markdown(entry) == render_iteration_entry_markdown(entry)

    def test_non_entry_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            render_iteration_entry_markdown("not an entry")  # type: ignore[arg-type]


class TestRenderTimelineMarkdown:
    def test_empty_timeline(self) -> None:
        tl = IterationTimeline(session_id="s", created_at=1.0, updated_at=1.0)
        text = render_timeline_markdown(tl)
        assert "## Iteration timeline — session `s`" in text
        assert "_No iterations recorded yet._" in text

    def test_timeline_with_entries(self) -> None:
        entry = IterationEntry(
            session_id="s",
            version=1,
            entry_id="iter-s-0001",
            created_at=1.0,
            code_diff="",
            diff_stats=IterationDiffStats(1, 1, 0),
            screenshots=(make_shot(),),
        )
        tl = IterationTimeline(
            session_id="s",
            created_at=1.0,
            updated_at=1.0,
            entries=(entry,),
            next_version=2,
        )
        text = render_timeline_markdown(tl)
        assert "## Iteration timeline — session `s`" in text
        assert "Entries: 1 / screenshots: 1 / dropped: 0" in text
        assert "### Iteration #1 — `iter-s-0001`" in text

    def test_non_timeline_raises(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            render_timeline_markdown("no")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  MobileIterationTimelineBuilder — construction
# ═══════════════════════════════════════════════════════════════════


class TestBuilderConstruction:
    def test_defaults(self) -> None:
        b = MobileIterationTimelineBuilder()
        assert b.max_entries_per_session == DEFAULT_MAX_ENTRIES_PER_SESSION
        assert b.max_diff_chars == DEFAULT_MAX_DIFF_CHARS
        assert b.max_screenshots_per_entry == DEFAULT_MAX_SCREENSHOTS_PER_ENTRY
        assert b.max_summary_chars == DEFAULT_MAX_SUMMARY_CHARS
        assert b.record_count == 0
        assert b.reset_count == 0
        assert b.last_entry is None

    @pytest.mark.parametrize(
        "field",
        [
            "max_entries_per_session",
            "max_diff_chars",
            "max_screenshots_per_entry",
            "max_summary_chars",
        ],
    )
    def test_rejects_non_positive_limits(self, field: str) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            MobileIterationTimelineBuilder(**{field: 0})
        with pytest.raises(MobileIterationTimelineConfigError):
            MobileIterationTimelineBuilder(**{field: -1})

    def test_rejects_bool_limits(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            MobileIterationTimelineBuilder(max_entries_per_session=True)

    def test_rejects_non_callable_event_cb(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            MobileIterationTimelineBuilder(event_cb=42)  # type: ignore[arg-type]

    def test_rejects_non_callable_clock(self) -> None:
        with pytest.raises(MobileIterationTimelineConfigError):
            MobileIterationTimelineBuilder(clock=42)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  Builder.record — happy paths
# ═══════════════════════════════════════════════════════════════════


class TestBuilderRecordHappy:
    def test_first_record_has_version_1(self) -> None:
        rec = EventRecorder()
        b = MobileIterationTimelineBuilder(event_cb=rec, clock=FakeClock())
        entry = b.record(
            session_id="s",
            code_diff=SAMPLE_DIFF,
            screenshots=(make_shot(),),
            summary="hi",
        )
        assert entry.version == 1
        assert entry.entry_id == "iter-s-0001"
        assert entry.parent_version == 0
        assert entry.created_at == 1000.0
        assert b.record_count == 1
        assert b.last_entry is entry

    def test_version_is_monotonic(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        e1 = b.record(session_id="s", code_diff="")
        e2 = b.record(session_id="s", code_diff="")
        e3 = b.record(session_id="s", code_diff="")
        assert (e1.version, e2.version, e3.version) == (1, 2, 3)
        assert (e1.parent_version, e2.parent_version, e3.parent_version) == (0, 1, 2)

    def test_multiple_sessions_isolated(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(session_id="alpha", code_diff="")
        b.record(session_id="beta", code_diff="")
        b.record(session_id="alpha", code_diff="")
        assert set(b.sessions()) == {"alpha", "beta"}
        assert b.timeline("alpha").entry_count == 2
        assert b.timeline("beta").entry_count == 1

    def test_derived_diff_stats(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        entry = b.record(session_id="s", code_diff=SAMPLE_DIFF)
        assert entry.diff_stats.files_changed == 1
        assert entry.diff_stats.additions == 2
        assert entry.diff_stats.deletions == 1

    def test_caller_supplied_diff_stats(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        stats = IterationDiffStats(7, 70, 30)
        entry = b.record(
            session_id="s", code_diff=SAMPLE_DIFF, diff_stats=stats
        )
        assert entry.diff_stats is stats

    def test_timeline_view_contains_entry(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(session_id="s", code_diff="")
        tl = b.timeline("s")
        assert tl.entry_count == 1
        assert tl.next_version == 2
        assert tl.dropped_count == 0

    def test_events_recorded(self) -> None:
        rec = EventRecorder()
        b = MobileIterationTimelineBuilder(event_cb=rec, clock=FakeClock())
        b.record(session_id="s", code_diff=SAMPLE_DIFF)
        topics = rec.topics()
        assert MOBILE_ITERATION_TIMELINE_EVENT_RECORDING in topics
        assert MOBILE_ITERATION_TIMELINE_EVENT_RECORDED in topics
        recorded_payloads = [
            p
            for t, p in rec.events()
            if t == MOBILE_ITERATION_TIMELINE_EVENT_RECORDED
        ]
        assert recorded_payloads[0]["version"] == 1
        assert recorded_payloads[0]["screenshot_count"] == 0

    def test_tags_dedupe_and_strip(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        entry = b.record(
            session_id="s",
            code_diff="",
            tags=[" feat ", "feat", "bug"],
        )
        assert entry.tags == ("feat", "bug")

    def test_metadata_passthrough(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        entry = b.record(
            session_id="s", code_diff="", metadata={"pr": "PR-42", "branch": "feat/x"}
        )
        assert entry.metadata == {"pr": "PR-42", "branch": "feat/x"}

    def test_screenshots_none_is_legal(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        entry = b.record(session_id="s", code_diff="", screenshots=None)
        assert entry.screenshot_count == 0

    def test_last_entry_updates(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        e1 = b.record(session_id="s", code_diff="")
        e2 = b.record(session_id="s", code_diff="")
        assert b.last_entry is e2
        assert b.last_entry is not e1


# ═══════════════════════════════════════════════════════════════════
#  Builder.record — input validation
# ═══════════════════════════════════════════════════════════════════


class TestBuilderRecordValidation:
    def test_empty_session_id(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="", code_diff="")

    def test_whitespace_session_id(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="   ", code_diff="")

    def test_unsafe_session_id(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="weird/sess", code_diff="")

    def test_non_string_code_diff(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="s", code_diff=None)  # type: ignore[arg-type]

    def test_non_string_summary(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="s", code_diff="", summary=42)  # type: ignore[arg-type]

    def test_non_string_author(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="s", code_diff="", author=42)  # type: ignore[arg-type]

    def test_bad_diff_stats(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(
                session_id="s",
                code_diff="",
                diff_stats={"a": 1},  # type: ignore[arg-type]
            )

    def test_screenshots_single_shot_rejected(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(
                session_id="s",
                code_diff="",
                screenshots=make_shot(),  # type: ignore[arg-type]
            )

    def test_screenshots_str_rejected(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(
                session_id="s",
                code_diff="",
                screenshots="oops",  # type: ignore[arg-type]
            )

    def test_screenshots_entry_bad_type(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(
                session_id="s",
                code_diff="",
                screenshots=[{"device_id": "x"}],  # type: ignore[arg-type]
            )

    def test_tags_single_string_rejected(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="s", code_diff="", tags="bug")  # type: ignore[arg-type]

    def test_tags_empty_entry_rejected(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="s", code_diff="", tags=["   "])

    def test_tags_non_string_entry_rejected(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="s", code_diff="", tags=[1])  # type: ignore[list-item]

    def test_metadata_bad_type_rejected(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(
                session_id="s", code_diff="", metadata="oops"  # type: ignore[arg-type]
            )

    def test_metadata_non_string_key_rejected(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(session_id="s", code_diff="", metadata={1: "bad"})  # type: ignore[dict-item]

    def test_record_failed_event_on_rejection(self) -> None:
        # Validation errors before the write happen before the emit; but
        # if an internal assert in entry construction fires the
        # record_failed topic must fire.  We simulate with a bad
        # diff_stats type slipping past (caller passes an int — caught
        # by the early type check).
        rec = EventRecorder()
        b = MobileIterationTimelineBuilder(event_cb=rec)
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record(
                session_id="s", code_diff="", diff_stats=5  # type: ignore[arg-type]
            )
        # Early validation does NOT emit a recorded event.
        assert MOBILE_ITERATION_TIMELINE_EVENT_RECORDED not in rec.topics()


# ═══════════════════════════════════════════════════════════════════
#  Builder.record — trimming / warnings
# ═══════════════════════════════════════════════════════════════════


class TestBuilderTrimming:
    def test_oversize_diff_trimmed_with_warning(self) -> None:
        b = MobileIterationTimelineBuilder(
            max_diff_chars=32, clock=FakeClock()
        )
        entry = b.record(
            session_id="s", code_diff="x" * 200
        )
        assert len(entry.code_diff) > 32  # trimmed diff + marker line
        assert entry.code_diff.startswith("x" * 32)
        assert "truncated" in entry.code_diff
        assert any(w.startswith("code_diff_truncated:") for w in entry.warnings)

    def test_oversize_screenshots_trimmed_with_warning(self) -> None:
        b = MobileIterationTimelineBuilder(
            max_screenshots_per_entry=2, clock=FakeClock()
        )
        shots = tuple(
            make_shot(device_id=f"dev-{i}", platform="ios")
            for i in range(5)
        )
        entry = b.record(session_id="s", code_diff="", screenshots=shots)
        assert entry.screenshot_count == 2
        assert any(w.startswith("screenshots_truncated:") for w in entry.warnings)

    def test_oversize_summary_trimmed(self) -> None:
        b = MobileIterationTimelineBuilder(
            max_summary_chars=10, clock=FakeClock()
        )
        entry = b.record(
            session_id="s", code_diff="", summary="x" * 50
        )
        assert entry.summary.endswith("…")
        assert any(w.startswith("summary_truncated:") for w in entry.warnings)

    def test_no_warnings_on_well_sized_input(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        entry = b.record(
            session_id="s",
            code_diff="small",
            summary="fine",
            screenshots=(make_shot(),),
        )
        assert entry.warnings == ()


# ═══════════════════════════════════════════════════════════════════
#  Builder.record — ring buffer
# ═══════════════════════════════════════════════════════════════════


class TestBuilderRingBuffer:
    def test_drops_oldest_when_over_capacity(self) -> None:
        b = MobileIterationTimelineBuilder(
            max_entries_per_session=3, clock=FakeClock()
        )
        for _ in range(5):
            b.record(session_id="s", code_diff="")
        tl = b.timeline("s")
        # entry_count capped at 3; dropped_count = 2; next_version = 6.
        assert tl.entry_count == 3
        assert tl.dropped_count == 2
        assert tl.next_version == 6
        # version numbers preserved across drops — the oldest surviving
        # entry is version 3 not 1.
        assert tuple(e.version for e in tl.entries) == (3, 4, 5)

    def test_snapshot_reports_dropped_count(self) -> None:
        b = MobileIterationTimelineBuilder(
            max_entries_per_session=2, clock=FakeClock()
        )
        for _ in range(4):
            b.record(session_id="s", code_diff="")
        snap = b.snapshot()
        sess = snap["sessions"][0]
        assert sess["entry_count"] == 2
        assert sess["dropped_count"] == 2
        assert sess["next_version"] == 5


# ═══════════════════════════════════════════════════════════════════
#  Builder.record_from_screenshot_results
# ═══════════════════════════════════════════════════════════════════


class TestBuilderRecordFromScreenshotResults:
    def test_happy_path(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        targets = (
            _FakeTarget(device_id="iphone-15", label="iPhone 15"),
            _FakeTarget(device_id="pixel-8", label="Pixel 8"),
        )
        results = (
            _FakeScreenshotResult(
                status="pass",
                platform="ios",
                path="/tmp/ios.png",
                width=1179,
                height=2556,
                size_bytes=999,
            ),
            _FakeScreenshotResult(
                status="mock",
                platform="android",
                detail="adb missing",
            ),
        )
        entry = b.record_from_screenshot_results(
            session_id="s", code_diff="", targets=targets, results=results
        )
        assert entry.screenshot_count == 2
        assert entry.real_capture_count == 1
        assert entry.device_ids == ("iphone-15", "pixel-8")

    def test_length_mismatch_raises(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record_from_screenshot_results(
                session_id="s",
                code_diff="",
                targets=(_FakeTarget("x"),),
                results=(),
            )

    def test_bad_target_type_rejected(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record_from_screenshot_results(
                session_id="s",
                code_diff="",
                targets=("not-a-target",),  # type: ignore[tuple-item]
                results=(_FakeScreenshotResult(status="pass"),),
            )

    def test_targets_not_sequence(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record_from_screenshot_results(
                session_id="s",
                code_diff="",
                targets="oops",  # type: ignore[arg-type]
                results=(),
            )

    def test_results_not_sequence(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.record_from_screenshot_results(
                session_id="s",
                code_diff="",
                targets=(),
                results="oops",  # type: ignore[arg-type]
            )

    def test_include_image_base64(self) -> None:
        import base64

        b = MobileIterationTimelineBuilder(clock=FakeClock())
        res = _FakeScreenshotResult(
            status="pass", platform="ios", png_bytes=b"\x89PNGdata"
        )
        entry = b.record_from_screenshot_results(
            session_id="s",
            code_diff="",
            targets=(_FakeTarget("iphone-15", "iPhone 15"),),
            results=(res,),
            include_image_base64=True,
        )
        assert entry.screenshots[0].image_base64 == base64.b64encode(
            b"\x89PNGdata"
        ).decode()


# ═══════════════════════════════════════════════════════════════════
#  Builder.timeline / list_entries / get_entry / latest_entry
# ═══════════════════════════════════════════════════════════════════


class TestBuilderQueries:
    def test_timeline_empty_session_still_valid(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        tl = b.timeline("never-recorded")
        assert tl.is_empty
        assert tl.next_version == 1
        assert tl.dropped_count == 0

    def test_list_entries_empty_session(self) -> None:
        b = MobileIterationTimelineBuilder()
        assert b.list_entries("x") == ()

    def test_list_entries_after_records(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(session_id="s", code_diff="")
        b.record(session_id="s", code_diff="")
        entries = b.list_entries("s")
        assert len(entries) == 2
        assert entries[0].version == 1
        assert entries[1].version == 2

    def test_get_entry_happy(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(session_id="s", code_diff="")
        b.record(session_id="s", code_diff="")
        entry = b.get_entry("s", 2)
        assert entry.version == 2

    def test_get_entry_unknown_session_raises(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineNotFoundError):
            b.get_entry("nope", 1)

    def test_get_entry_unknown_version_raises(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(session_id="s", code_diff="")
        with pytest.raises(MobileIterationTimelineNotFoundError):
            b.get_entry("s", 99)

    def test_get_entry_dropped_version_raises(self) -> None:
        b = MobileIterationTimelineBuilder(
            max_entries_per_session=2, clock=FakeClock()
        )
        for _ in range(5):
            b.record(session_id="s", code_diff="")
        # Version 1 + 2 + 3 dropped.
        with pytest.raises(MobileIterationTimelineNotFoundError):
            b.get_entry("s", 1)
        # Version 5 still there.
        assert b.get_entry("s", 5).version == 5

    def test_get_entry_bad_version(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(session_id="s", code_diff="")
        with pytest.raises(MobileIterationTimelineConfigError):
            b.get_entry("s", 0)
        with pytest.raises(MobileIterationTimelineConfigError):
            b.get_entry("s", True)

    def test_latest_entry(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        assert b.latest_entry("s") is None
        b.record(session_id="s", code_diff="")
        e2 = b.record(session_id="s", code_diff="")
        assert b.latest_entry("s") is e2


# ═══════════════════════════════════════════════════════════════════
#  Builder.reset
# ═══════════════════════════════════════════════════════════════════


class TestBuilderReset:
    def test_reset_known_session(self) -> None:
        rec = EventRecorder()
        b = MobileIterationTimelineBuilder(event_cb=rec, clock=FakeClock())
        b.record(session_id="s", code_diff="")
        assert b.reset("s") is True
        assert b.timeline("s").is_empty
        assert b.sessions() == ()
        assert b.reset_count == 1
        assert MOBILE_ITERATION_TIMELINE_EVENT_RESET in rec.topics()

    def test_reset_unknown_session_false(self) -> None:
        b = MobileIterationTimelineBuilder()
        assert b.reset("nope") is False
        assert b.reset_count == 0

    def test_reset_then_record_restarts_version(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(session_id="s", code_diff="")
        b.record(session_id="s", code_diff="")
        b.reset("s")
        entry = b.record(session_id="s", code_diff="")
        assert entry.version == 1  # counter reset

    def test_reset_bad_session_id(self) -> None:
        b = MobileIterationTimelineBuilder()
        with pytest.raises(MobileIterationTimelineConfigError):
            b.reset("")


# ═══════════════════════════════════════════════════════════════════
#  Builder.snapshot
# ═══════════════════════════════════════════════════════════════════


class TestBuilderSnapshot:
    def test_empty_snapshot(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        snap = b.snapshot()
        assert snap["schema_version"] == MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION
        assert snap["record_count"] == 0
        assert snap["session_count"] == 0
        assert snap["sessions"] == []

    def test_snapshot_never_inlines_image_base64(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(
            session_id="s",
            code_diff="huge diff",
            screenshots=(make_shot(image_base64="secret"),),
        )
        snap = b.snapshot()
        blob = json.dumps(snap)
        assert "secret" not in blob
        # code_diff also omitted from the envelope (only in get_entry).
        assert "huge diff" not in blob

    def test_snapshot_lists_sessions_alphabetically(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(session_id="zz", code_diff="")
        b.record(session_id="aa", code_diff="")
        snap = b.snapshot()
        sids = [s["session_id"] for s in snap["sessions"]]
        assert sids == ["aa", "zz"]

    def test_snapshot_reports_configured_limits(self) -> None:
        b = MobileIterationTimelineBuilder(
            max_entries_per_session=5,
            max_diff_chars=123,
            max_screenshots_per_entry=3,
            max_summary_chars=50,
        )
        snap = b.snapshot()
        assert snap["max_entries_per_session"] == 5
        assert snap["max_diff_chars"] == 123
        assert snap["max_screenshots_per_entry"] == 3
        assert snap["max_summary_chars"] == 50


# ═══════════════════════════════════════════════════════════════════
#  Event delivery + callback safety
# ═══════════════════════════════════════════════════════════════════


class TestBuilderEvents:
    def test_event_callback_raise_does_not_kill_builder(self) -> None:
        def bomb(_topic: str, _payload: dict[str, Any]) -> None:
            raise RuntimeError("kaboom")

        b = MobileIterationTimelineBuilder(event_cb=bomb, clock=FakeClock())
        # Does not raise — callback errors are swallowed.
        entry = b.record(session_id="s", code_diff="")
        assert entry.version == 1

    def test_no_callback_silent(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        # Does not raise even though event_cb is None.
        b.record(session_id="s", code_diff="")

    def test_recording_event_fires_before_recorded(self) -> None:
        rec = EventRecorder()
        b = MobileIterationTimelineBuilder(event_cb=rec, clock=FakeClock())
        b.record(session_id="s", code_diff="")
        topics = rec.topics()
        building_idx = topics.index(MOBILE_ITERATION_TIMELINE_EVENT_RECORDING)
        recorded_idx = topics.index(MOBILE_ITERATION_TIMELINE_EVENT_RECORDED)
        assert building_idx < recorded_idx

    def test_reset_event_payload(self) -> None:
        rec = EventRecorder()
        b = MobileIterationTimelineBuilder(event_cb=rec, clock=FakeClock())
        b.record(session_id="alpha", code_diff="")
        b.reset("alpha")
        reset_events = [
            p
            for t, p in rec.events()
            if t == MOBILE_ITERATION_TIMELINE_EVENT_RESET
        ]
        assert len(reset_events) == 1
        assert reset_events[0]["session_id"] == "alpha"


# ═══════════════════════════════════════════════════════════════════
#  Thread safety
# ═══════════════════════════════════════════════════════════════════


class TestBuilderThreadSafety:
    def test_concurrent_records_distinct_sessions(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())

        def worker(session_id: str) -> None:
            for _ in range(10):
                b.record(session_id=session_id, code_diff="")

        threads = [
            threading.Thread(target=worker, args=(f"sess-{i}",)) for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(4):
            assert b.timeline(f"sess-{i}").entry_count == 10
        assert b.record_count == 40

    def test_concurrent_records_same_session_versions_unique(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())

        def worker() -> None:
            for _ in range(25):
                b.record(session_id="shared", code_diff="")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entries = b.list_entries("shared")
        versions = [e.version for e in entries]
        # All versions unique and monotonic.
        assert len(set(versions)) == len(versions)
        assert versions == sorted(versions)


# ═══════════════════════════════════════════════════════════════════
#  End-to-end golden
# ═══════════════════════════════════════════════════════════════════


class TestBuilderEndToEnd:
    def test_full_iteration_lifecycle(self) -> None:
        rec = EventRecorder()
        b = MobileIterationTimelineBuilder(event_cb=rec, clock=FakeClock())

        # Iteration 1 — add counter state on iPhone + Pixel.
        e1 = b.record(
            session_id="sess-42",
            code_diff=SAMPLE_DIFF,
            screenshots=(
                make_shot(device_id="iphone-15", platform="ios"),
                make_shot(device_id="pixel-8", platform="android"),
            ),
            summary="Add counter state",
            author="agent-software-beta",
            tags=("feat",),
        )
        assert e1.version == 1
        assert e1.real_capture_count == 2
        assert e1.platforms == ("ios", "android")

        # Iteration 2 — pixel-8 reports mock (adb missing on this host).
        e2 = b.record(
            session_id="sess-42",
            code_diff="",  # revert
            screenshots=(
                make_shot(device_id="iphone-15"),
                make_shot(device_id="pixel-8", platform="android", status="mock"),
            ),
            summary="Revert counter initial value",
            author="agent-software-beta",
            tags=("fix",),
        )
        assert e2.version == 2
        assert e2.parent_version == 1
        assert e2.real_capture_count == 1

        # Timeline view.
        tl = b.timeline("sess-42")
        assert tl.entry_count == 2
        assert tl.total_screenshots == 4
        assert tl.latest_entry is e2
        assert tl.first_entry is e1

        # Render
        markdown = render_timeline_markdown(tl)
        assert "### Iteration #1" in markdown
        assert "### Iteration #2" in markdown

        # Snapshot.
        snap = b.snapshot()
        sess = snap["sessions"][0]
        assert sess["latest_version"] == 2

        # get_entry works.
        fetched = b.get_entry("sess-42", 1)
        assert fetched is e1

        # Reset wipes state but next record is version 1 again.
        b.reset("sess-42")
        e3 = b.record(session_id="sess-42", code_diff="")
        assert e3.version == 1

    def test_snapshot_is_json_safe(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(
            session_id="s",
            code_diff=SAMPLE_DIFF,
            screenshots=(make_shot(),),
            summary="hi",
            tags=("x",),
            metadata={"pr": "42"},
        )
        blob = json.dumps(b.snapshot())
        assert json.loads(blob)

    def test_timeline_to_dict_is_json_safe(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        b.record(
            session_id="s",
            code_diff=SAMPLE_DIFF,
            screenshots=(make_shot(), make_shot(device_id="pixel-8", platform="android")),
        )
        tl = b.timeline("s")
        blob = json.dumps(tl.to_dict())
        assert json.loads(blob)

    def test_end_to_end_with_record_from_screenshot_results(self) -> None:
        b = MobileIterationTimelineBuilder(clock=FakeClock())
        targets = (
            _FakeTarget("iphone-15", "iPhone 15"),
            _FakeTarget("pixel-8", "Pixel 8"),
            _FakeTarget("ipad", "iPad"),
        )
        results = (
            _FakeScreenshotResult(
                status="pass", platform="ios", width=1179, height=2556, size_bytes=1000
            ),
            _FakeScreenshotResult(status="fail", platform="android", detail="gradle failed"),
            _FakeScreenshotResult(status="skip", platform="ios"),
        )
        entry = b.record_from_screenshot_results(
            session_id="s",
            code_diff=SAMPLE_DIFF,
            targets=targets,
            results=results,
            summary="Mixed capture result",
            author="agent",
            tags=("mixed",),
        )
        assert entry.screenshot_count == 3
        assert entry.real_capture_count == 1
        assert entry.device_ids == ("iphone-15", "pixel-8", "ipad")
        statuses = [s.status for s in entry.screenshots]
        assert statuses == ["pass", "fail", "skip"]
