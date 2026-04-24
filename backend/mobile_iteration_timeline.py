"""V7 row 2689 (#323 second bullet) — Mobile iteration timeline.

Every time the mobile agent (or operator) ships a modification to a
SwiftUI / Compose / Flutter / React-Native source tree, the workspace
needs to preserve the *before / after* story so future iterations can
reason about drift across device form-factors.  Concretely, one entry
of the timeline bundles:

* the **unified code diff** that was applied to the workspace tree,
* one **emulator / simulator screenshot per device target** captured
  *after* the change landed (across the V6 #3 device-frame roster —
  iPhone 15 / SE / iPad / Pixel 8 / Fold / Galaxy Tab — whichever the
  caller exercises this turn),
* operator-friendly metadata (summary, author, free-form tags).

Where this sits in the V6 / V7 stack
------------------------------------

* V6 #1 ``mobile_sandbox.py`` owns the ``build → install → run``
  lifecycle; it knows *which* sandbox is rebuilt each turn.
* V6 #2 ``mobile_screenshot.py`` is the ad-hoc capture primitive —
  each call returns a :class:`ScreenshotResult` with ``status`` /
  ``path`` / ``width`` / ``height`` / ``png_bytes``.
* V6 #5 ``mobile_agent_visual_context.py`` bundles those results for
  the next *ReAct turn* (multimodal prompt).
* V7 #1 ``mobile_annotation_context.py`` converts operator rects on
  a single device frame into native-file hints.

**V7 #2 (this module)** sits *after* a turn finishes.  The orchestrator
records each shipped change so downstream UIs — Mobile Workspace
timeline sidebar (V7 #3-#5 rows), Store-submission change-log builder
— can replay "what did the agent change, and what did every device
look like afterwards".

Why a dedicated module rather than extending V6 #5
--------------------------------------------------

* V6 #5 builds a *per-turn* multimodal message that goes straight to
  the LLM.  V7 #2 accumulates a *per-session* history that outlives
  the turn, survives agent restarts (in-memory by default;
  persistence is caller-supplied) and is queried by the operator UI.
  Two completely different lifetimes — co-locating them would force
  either V6 #5 to carry history it does not need or V7 #2 to couple
  to Anthropic's multimodal shape.
* V6 #5 emits on the ``mobile_sandbox.agent_visual_context.*``
  topic.  V7 #2 emits on ``mobile_workspace.iteration_timeline.*`` —
  different SSE bus prefix, consumed by a different React panel.
* The diff metadata (``files_changed`` / ``additions`` /
  ``deletions``) has no home in V6 #5's ``MobileAgentVisualContextPayload``;
  adding it would break V6 #5's 1.0.0 schema pin.

Design decisions
----------------

* **Composition, not inheritance.**
  :class:`MobileIterationTimelineBuilder` holds per-session state
  behind a single :class:`threading.RLock`.  Callers pass parsed
  :class:`IterationScreenshot` objects (or, via
  :meth:`record_from_screenshot_results`, raw
  :class:`backend.mobile_screenshot.ScreenshotResult` tuples with the
  matching :class:`backend.mobile_agent_visual_context.MobileDeviceTarget`
  matrix) and we build :class:`IterationEntry` records.  The module
  never imports V6 #1 / V6 #5 into its construction path — the
  :class:`ScreenshotResult` import is conditional and cheap.
* **Frozen dataclasses, ``to_dict`` JSON-safe.**
  Entries, screenshots, and the envelope timeline are all frozen;
  ``to_dict`` output is deterministic and passes through
  :func:`json.dumps` without custom encoders.  ``image_base64`` is
  *dropped* from the envelope ``to_dict`` because listing a 10-entry
  timeline would otherwise blow past SSE frame budgets — callers that
  need the bytes fetch the entry on demand.
* **Ring-buffer per session.**
  :data:`DEFAULT_MAX_ENTRIES_PER_SESSION` caps the history at 100 to
  keep operator UX snappy; overflow drops the oldest entry and
  surfaces a ``iteration_dropped:<version>`` warning on the next
  record.  Session counter keeps incrementing so versions are stable
  across ring-buffer churn (no re-use).
* **Diff parsing is lossless.**
  The stored ``code_diff`` is the caller's raw text.  Stats
  (``files_changed`` / ``additions`` / ``deletions``) are derived via
  :func:`parse_diff_stats` — a strict unified-diff scanner that
  recognises ``diff --git`` / ``+++ b/<path>`` / ``+ ``` / ``- `` ``
  lines but is tolerant of empty or non-diff text (no additions, no
  deletions, no file counts, zero raise).
* **Version monotonicity.**
  ``IterationEntry.version`` is 1-based and strictly monotonic per
  session.  Even after the ring-buffer drops old entries, a new
  record's version is the next integer — so an operator who already
  referenced "iteration #37" keeps that stable reference.
* **Event namespace isolation.**
  Four topics under ``mobile_workspace.iteration_timeline.*``.  A
  contract test asserts :func:`set.isdisjoint` against the V6 #1 /
  V6 #5 / V6 #6 / V7 #1 roster so SSE subscribers can bind on prefix
  without cross-talk.
* **Operator-visible byte budgets.**
  ``DEFAULT_MAX_DIFF_CHARS`` (200 KB) and
  ``DEFAULT_MAX_SCREENSHOTS_PER_ENTRY`` (12) are advisory caps; the
  builder emits a warning when trimmed rather than raising so a
  pathological 5 MB diff does not bring down the workspace.

Public API (pinned by
``backend/tests/test_mobile_iteration_timeline.py``)
---------------------------------------------------

* :data:`MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION` — semver bump gate.
* :data:`MOBILE_ITERATION_TIMELINE_EVENT_TYPES` — four event names.
* :class:`IterationScreenshot`, :class:`IterationDiffStats`,
  :class:`IterationEntry`, :class:`IterationTimeline`.
* :class:`MobileIterationTimelineError`,
  :class:`MobileIterationTimelineConfigError`,
  :class:`MobileIterationTimelineNotFoundError`.
* :func:`parse_diff_stats`,
  :func:`format_iteration_entry_id`,
  :func:`render_iteration_entry_markdown`,
  :func:`render_timeline_markdown`,
  :func:`screenshot_from_result`.
* :class:`MobileIterationTimelineBuilder` with ``record`` /
  ``record_from_screenshot_results`` / ``timeline`` /
  ``list_entries`` / ``get_entry`` / ``reset`` / ``sessions`` /
  ``snapshot``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)


__all__ = [
    "MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION",
    "DEFAULT_MAX_ENTRIES_PER_SESSION",
    "DEFAULT_MAX_DIFF_CHARS",
    "DEFAULT_MAX_SCREENSHOTS_PER_ENTRY",
    "DEFAULT_MAX_SUMMARY_CHARS",
    "SUPPORTED_SCREENSHOT_STATUSES",
    "SUPPORTED_SCREENSHOT_PLATFORMS",
    "MOBILE_ITERATION_TIMELINE_EVENT_RECORDING",
    "MOBILE_ITERATION_TIMELINE_EVENT_RECORDED",
    "MOBILE_ITERATION_TIMELINE_EVENT_RESET",
    "MOBILE_ITERATION_TIMELINE_EVENT_RECORD_FAILED",
    "MOBILE_ITERATION_TIMELINE_EVENT_TYPES",
    "MobileIterationTimelineError",
    "MobileIterationTimelineConfigError",
    "MobileIterationTimelineNotFoundError",
    "IterationDiffStats",
    "IterationScreenshot",
    "IterationEntry",
    "IterationTimeline",
    "MobileIterationTimelineBuilder",
    "parse_diff_stats",
    "format_iteration_entry_id",
    "render_iteration_entry_markdown",
    "render_timeline_markdown",
    "screenshot_from_result",
]


# ───────────────────────────────────────────────────────────────────
#  Constants
# ───────────────────────────────────────────────────────────────────


#: Bump whenever :meth:`IterationEntry.to_dict` / :meth:`IterationTimeline.to_dict`
#: / :meth:`IterationScreenshot.to_dict` shape changes.  Major = breaking.
MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION = "1.0.0"

#: Ring-buffer depth per session.  At 100 entries × ~50 KB diff +
#: ~2 KB metadata = ~5 MB of text per session before screenshots; the
#: Mobile Workspace timeline sidebar renders these as a virtualised
#: list so this is plenty of history.  Overflow drops the oldest
#: entry; ``version`` keeps incrementing.
DEFAULT_MAX_ENTRIES_PER_SESSION = 100

#: Maximum characters of ``code_diff`` we keep per entry.  A real
#: Gradle / Xcode patch is under 30 KB; 200 KB leaves headroom for
#: generated resource changes without letting a pathological 5 MB
#: vendored-in lib-diff eat the operator UI.
DEFAULT_MAX_DIFF_CHARS = 200_000

#: Screenshot cap per entry.  Default device matrix is 6; operator can
#: drive more (e.g. 12 for fold variants) — 12 is a hard upper bound.
DEFAULT_MAX_SCREENSHOTS_PER_ENTRY = 12

#: Operator-facing summary cap.  Keep to a sentence-ish; long prose
#: belongs in the diff commit message.
DEFAULT_MAX_SUMMARY_CHARS = 2_000


#: Screenshot statuses this module accepts when a caller builds
#: :class:`IterationScreenshot` by hand.  Mirrors V6 #2
#: :class:`ScreenshotStatus` string values but lives as plain strings
#: so this module never has to import V6 #2 at definition time.
SUPPORTED_SCREENSHOT_STATUSES: tuple[str, ...] = ("pass", "fail", "skip", "mock")


#: Platforms this module tolerates on :class:`IterationScreenshot`.
#: Matches V6 #2 ``SUPPORTED_PLATFORMS``.
SUPPORTED_SCREENSHOT_PLATFORMS: tuple[str, ...] = ("android", "ios")


#: Device-id safe character class.  Mirrors V6 #3 ``DEVICE_PROFILE_IDS``
#: shape + allows generic operator-authored ids ("iphone-15", "fold-7").
_SAFE_DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")

#: Session-id safe character class.  Deliberately matches
#: ``mobile_screenshot._SAFE_SESSION_RE`` + ``mobile_sandbox`` so an
#: entry recorded here is reachable from both sibling modules without
#: translation.
_SAFE_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


# ───────────────────────────────────────────────────────────────────
#  Events
# ───────────────────────────────────────────────────────────────────


MOBILE_ITERATION_TIMELINE_EVENT_RECORDING = (
    "mobile_workspace.iteration_timeline.recording"
)
MOBILE_ITERATION_TIMELINE_EVENT_RECORDED = (
    "mobile_workspace.iteration_timeline.recorded"
)
MOBILE_ITERATION_TIMELINE_EVENT_RESET = (
    "mobile_workspace.iteration_timeline.reset"
)
MOBILE_ITERATION_TIMELINE_EVENT_RECORD_FAILED = (
    "mobile_workspace.iteration_timeline.record_failed"
)


MOBILE_ITERATION_TIMELINE_EVENT_TYPES: tuple[str, ...] = (
    MOBILE_ITERATION_TIMELINE_EVENT_RECORDING,
    MOBILE_ITERATION_TIMELINE_EVENT_RECORDED,
    MOBILE_ITERATION_TIMELINE_EVENT_RESET,
    MOBILE_ITERATION_TIMELINE_EVENT_RECORD_FAILED,
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class MobileIterationTimelineError(RuntimeError):
    """Base class for ``mobile_iteration_timeline`` errors.  Routers
    can catch this single type to translate every failure into one
    structured HTTP / event payload."""


class MobileIterationTimelineConfigError(MobileIterationTimelineError):
    """Input-validation error — dataclass ctor failed, caller passed a
    bad type, etc.  FastAPI routers map this to 422."""


class MobileIterationTimelineNotFoundError(MobileIterationTimelineError):
    """Raised by :meth:`MobileIterationTimelineBuilder.get_entry` when
    the requested ``session_id`` / ``version`` pair does not exist."""


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


#: Matches a unified-diff file header line.  Both ``--- a/<path>`` and
#: ``+++ b/<path>`` qualify; we key on ``+++ b/`` because that's the
#: post-change path and is what the timeline wants to display.
_DIFF_POST_FILE_RE = re.compile(r"^\+\+\+ b/(?P<path>.+)$")

#: Secondary file-header marker — ``diff --git a/... b/...``.  Some
#: callers ship the ``diff --git`` line without the subsequent
#: ``+++``/``---`` pair (minimal diffs); we count it as a file touch.
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def parse_diff_stats(diff_text: str) -> "IterationDiffStats":
    """Best-effort unified-diff stats extractor.

    Tolerates empty / non-diff text (returns zeros), binary-diff
    markers (counts the file touch, no +/- lines), and CRLF line
    endings.  Does not validate the diff's consistency — the operator
    may have hand-edited it.  A file is "changed" once per distinct
    post-change path; ``+++ b/dev/null`` is treated as a delete and
    does contribute a file touch.  Lines inside ``@@ ... @@`` hunks
    that begin with ``+`` / ``-`` (other than the ``+++`` / ``---``
    headers) are counted as additions / deletions.
    """

    if diff_text is None:
        return IterationDiffStats()
    if not isinstance(diff_text, str):
        raise MobileIterationTimelineConfigError(
            "diff_text must be a string or None"
        )

    additions = 0
    deletions = 0
    touched: set[str] = set()
    in_hunk = False

    for raw_line in diff_text.splitlines():
        line = raw_line.rstrip("\r")

        # ``diff --git`` headers always count as a file touch even if
        # the subsequent ``+++`` line is absent (some minimal diff
        # formatters emit only the git header).
        git_match = _DIFF_GIT_HEADER_RE.match(line)
        if git_match:
            touched.add(git_match.group("b"))
            in_hunk = False
            continue

        # ``+++ b/<path>`` is the canonical post-change path.
        post_match = _DIFF_POST_FILE_RE.match(line)
        if post_match:
            touched.add(post_match.group("path"))
            in_hunk = False
            continue

        # ``--- a/<path>`` delimits a pre-change file — skip without
        # counting as an addition.
        if line.startswith("--- "):
            in_hunk = False
            continue

        if line.startswith("@@"):
            in_hunk = True
            continue

        if in_hunk:
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1

    return IterationDiffStats(
        files_changed=len(touched),
        additions=additions,
        deletions=deletions,
    )


_SAFE_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]")


def format_iteration_entry_id(session_id: str, version: int) -> str:
    """Deterministic entry id — ``iter-<safe-session>-<version:04d>``.

    ``session_id`` characters outside ``[A-Za-z0-9._-]`` are replaced
    with ``-`` so the id is safe to splice into URLs / log lines.
    ``version`` zero-pads to 4 digits (1 → ``0001``) — matches V6 #6's
    ``format_autofix_attempt_id`` pattern so operator dashboards can
    render both id shapes with the same CSS column.
    """

    if not isinstance(session_id, str) or not session_id.strip():
        raise MobileIterationTimelineConfigError(
            "session_id must be a non-empty string"
        )
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise MobileIterationTimelineConfigError(
            "version must be a positive int"
        )

    sanitized_chars = [
        c if _SAFE_ID_TOKEN_RE.match(c) else "-" for c in session_id
    ]
    sanitized = "".join(sanitized_chars).strip("-") or "session"
    return f"iter-{sanitized}-{version:04d}"


def screenshot_from_result(
    *,
    device_id: str,
    label: str,
    result: Any,
    include_image_base64: bool = False,
) -> "IterationScreenshot":
    """Adapt a V6 #2 :class:`ScreenshotResult` into an
    :class:`IterationScreenshot`.

    Uses ``getattr`` so callers can pass lookalike objects in tests
    without requiring the real :mod:`backend.mobile_screenshot`
    module on the import path.  The ``result.status`` field may be a
    :class:`ScreenshotStatus` enum or the underlying string value;
    both resolve to the same :data:`SUPPORTED_SCREENSHOT_STATUSES`
    member.
    """

    if result is None:
        raise MobileIterationTimelineConfigError(
            "result must not be None"
        )

    status_obj = getattr(result, "status", None)
    if status_obj is None:
        raise MobileIterationTimelineConfigError(
            "result must expose a status attribute"
        )
    status_value = getattr(status_obj, "value", status_obj)
    if not isinstance(status_value, str):
        raise MobileIterationTimelineConfigError(
            "status must resolve to a string"
        )

    platform = getattr(result, "platform", None)
    if not isinstance(platform, str):
        raise MobileIterationTimelineConfigError(
            "result.platform must be a string"
        )

    path = getattr(result, "path", "") or ""
    width = int(getattr(result, "width", 0) or 0)
    height = int(getattr(result, "height", 0) or 0)
    size_bytes = int(getattr(result, "size_bytes", 0) or 0)
    captured_at = float(getattr(result, "captured_at", 0.0) or 0.0)
    fmt = getattr(result, "format", "png") or "png"
    detail = getattr(result, "detail", "") or ""

    png_bytes = getattr(result, "png_bytes", b"") or b""
    image_base64 = ""
    if include_image_base64 and png_bytes:
        import base64

        image_base64 = base64.b64encode(png_bytes).decode("ascii")

    return IterationScreenshot(
        device_id=device_id,
        platform=platform,
        label=label or device_id,
        status=status_value,
        path=path,
        format=fmt,
        width=width,
        height=height,
        byte_len=size_bytes or len(png_bytes),
        captured_at=captured_at,
        detail=detail,
        image_base64=image_base64,
    )


# ───────────────────────────────────────────────────────────────────
#  Dataclasses
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IterationDiffStats:
    """Lightweight summary of a unified diff.

    The fields are derived from :func:`parse_diff_stats` or
    caller-supplied when the diff was produced by a tool that already
    knows these numbers (e.g. ``git apply --stat``).
    """

    files_changed: int = 0
    additions: int = 0
    deletions: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("files_changed", self.files_changed),
            ("additions", self.additions),
            ("deletions", self.deletions),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise MobileIterationTimelineConfigError(
                    f"{name} must be a non-negative int"
                )
            if value < 0:
                raise MobileIterationTimelineConfigError(
                    f"{name} must be >= 0"
                )

    @property
    def net_change(self) -> int:
        """Signed line-count delta (``additions - deletions``)."""
        return self.additions - self.deletions

    @property
    def total_lines_touched(self) -> int:
        return self.additions + self.deletions

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_changed": int(self.files_changed),
            "additions": int(self.additions),
            "deletions": int(self.deletions),
            "net_change": int(self.net_change),
            "total_lines_touched": int(self.total_lines_touched),
        }


@dataclass(frozen=True)
class IterationScreenshot:
    """One device's screenshot as recorded on an :class:`IterationEntry`.

    Mirrors the data :class:`backend.mobile_screenshot.ScreenshotResult`
    exposes but is storage-friendly: fields are primitive,
    ``image_base64`` is opt-in (callers that want bytes pay for them),
    and there are no sandbox / subprocess dependencies.
    """

    device_id: str
    platform: str
    label: str = ""
    status: str = "pass"
    path: str = ""
    format: str = "png"
    width: int = 0
    height: int = 0
    byte_len: int = 0
    captured_at: float = 0.0
    detail: str = ""
    image_base64: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.device_id, str) or not self.device_id.strip():
            raise MobileIterationTimelineConfigError(
                "device_id must be a non-empty string"
            )
        if not _SAFE_DEVICE_ID_RE.fullmatch(self.device_id):
            raise MobileIterationTimelineConfigError(
                "device_id must match [A-Za-z0-9_.-]{1,64} — got "
                f"{self.device_id!r}"
            )
        if not isinstance(self.platform, str) or not self.platform:
            raise MobileIterationTimelineConfigError(
                "platform must be a non-empty string"
            )
        plat = self.platform.strip().lower()
        if plat not in SUPPORTED_SCREENSHOT_PLATFORMS:
            raise MobileIterationTimelineConfigError(
                f"platform must be one of {SUPPORTED_SCREENSHOT_PLATFORMS!r}"
                f" — got {self.platform!r}"
            )
        object.__setattr__(self, "platform", plat)

        if not isinstance(self.label, str):
            raise MobileIterationTimelineConfigError("label must be a string")
        if not self.label:
            object.__setattr__(self, "label", self.device_id)

        if not isinstance(self.status, str) or self.status not in SUPPORTED_SCREENSHOT_STATUSES:
            raise MobileIterationTimelineConfigError(
                f"status must be one of {SUPPORTED_SCREENSHOT_STATUSES!r}"
                f" — got {self.status!r}"
            )

        if not isinstance(self.path, str):
            raise MobileIterationTimelineConfigError("path must be a string")
        if not isinstance(self.format, str) or not self.format:
            raise MobileIterationTimelineConfigError(
                "format must be a non-empty string"
            )
        for field_name, value in (
            ("width", self.width),
            ("height", self.height),
            ("byte_len", self.byte_len),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise MobileIterationTimelineConfigError(
                    f"{field_name} must be a non-negative int"
                )
            if value < 0:
                raise MobileIterationTimelineConfigError(
                    f"{field_name} must be >= 0"
                )
        if not isinstance(self.captured_at, (int, float)) or isinstance(
            self.captured_at, bool
        ):
            raise MobileIterationTimelineConfigError(
                "captured_at must be a non-negative number"
            )
        if self.captured_at < 0:
            raise MobileIterationTimelineConfigError(
                "captured_at must be >= 0"
            )
        if not isinstance(self.detail, str):
            raise MobileIterationTimelineConfigError("detail must be a string")
        if not isinstance(self.image_base64, str):
            raise MobileIterationTimelineConfigError(
                "image_base64 must be a string"
            )

    @property
    def has_image_bytes(self) -> bool:
        return bool(self.image_base64)

    @property
    def has_real_capture(self) -> bool:
        return self.status == "pass"

    def to_dict(self, *, include_image_base64: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "device_id": self.device_id,
            "platform": self.platform,
            "label": self.label,
            "status": self.status,
            "path": self.path,
            "format": self.format,
            "width": int(self.width),
            "height": int(self.height),
            "byte_len": int(self.byte_len),
            "captured_at": float(self.captured_at),
            "detail": self.detail,
            "has_image_bytes": self.has_image_bytes,
            "has_real_capture": self.has_real_capture,
        }
        if include_image_base64:
            data["image_base64"] = self.image_base64
        return data


@dataclass(frozen=True)
class IterationEntry:
    """One recorded iteration on a session's timeline.

    Carries the diff text, multi-device screenshots, and the metadata
    the operator UI surfaces.  Frozen — the builder produces a fresh
    entry every time rather than mutating in place so concurrent
    readers see atomic snapshots.
    """

    session_id: str
    version: int
    entry_id: str
    created_at: float
    code_diff: str
    diff_stats: IterationDiffStats
    screenshots: tuple[IterationScreenshot, ...] = ()
    summary: str = ""
    author: str = ""
    parent_version: int = 0
    tags: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise MobileIterationTimelineConfigError(
                "session_id must be a non-empty string"
            )
        if not _SAFE_SESSION_ID_RE.fullmatch(self.session_id):
            raise MobileIterationTimelineConfigError(
                "session_id must match [A-Za-z0-9_.-]{1,64} — got "
                f"{self.session_id!r}"
            )
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise MobileIterationTimelineConfigError(
                "version must be a positive int"
            )
        if self.version < 1:
            raise MobileIterationTimelineConfigError("version must be >= 1")
        if not isinstance(self.entry_id, str) or not self.entry_id.strip():
            raise MobileIterationTimelineConfigError(
                "entry_id must be a non-empty string"
            )
        if not isinstance(self.created_at, (int, float)) or isinstance(
            self.created_at, bool
        ):
            raise MobileIterationTimelineConfigError(
                "created_at must be a non-negative number"
            )
        if self.created_at < 0:
            raise MobileIterationTimelineConfigError(
                "created_at must be >= 0"
            )
        if not isinstance(self.code_diff, str):
            raise MobileIterationTimelineConfigError(
                "code_diff must be a string"
            )
        if not isinstance(self.diff_stats, IterationDiffStats):
            raise MobileIterationTimelineConfigError(
                "diff_stats must be an IterationDiffStats"
            )

        if not isinstance(self.screenshots, tuple):
            raise MobileIterationTimelineConfigError(
                "screenshots must be a tuple"
            )
        for shot in self.screenshots:
            if not isinstance(shot, IterationScreenshot):
                raise MobileIterationTimelineConfigError(
                    "screenshots entries must be IterationScreenshot"
                )

        if not isinstance(self.summary, str):
            raise MobileIterationTimelineConfigError("summary must be a string")
        if not isinstance(self.author, str):
            raise MobileIterationTimelineConfigError("author must be a string")
        if isinstance(self.parent_version, bool) or not isinstance(
            self.parent_version, int
        ):
            raise MobileIterationTimelineConfigError(
                "parent_version must be a non-negative int"
            )
        if self.parent_version < 0:
            raise MobileIterationTimelineConfigError(
                "parent_version must be >= 0"
            )
        if self.parent_version >= self.version:
            raise MobileIterationTimelineConfigError(
                "parent_version must be < version"
            )

        if not isinstance(self.tags, tuple):
            raise MobileIterationTimelineConfigError("tags must be a tuple")
        for tag in self.tags:
            if not isinstance(tag, str) or not tag.strip():
                raise MobileIterationTimelineConfigError(
                    "tags entries must be non-empty strings"
                )

        if not isinstance(self.warnings, tuple):
            raise MobileIterationTimelineConfigError("warnings must be a tuple")
        for w in self.warnings:
            if not isinstance(w, str) or not w.strip():
                raise MobileIterationTimelineConfigError(
                    "warnings entries must be non-empty strings"
                )

        if not isinstance(self.metadata, Mapping):
            raise MobileIterationTimelineConfigError(
                "metadata must be a Mapping"
            )
        for key in self.metadata:
            if not isinstance(key, str):
                raise MobileIterationTimelineConfigError(
                    "metadata keys must be strings"
                )

    @property
    def screenshot_count(self) -> int:
        return len(self.screenshots)

    @property
    def real_capture_count(self) -> int:
        return sum(1 for s in self.screenshots if s.has_real_capture)

    @property
    def device_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for shot in self.screenshots:
            if shot.device_id not in seen:
                seen.append(shot.device_id)
        return tuple(seen)

    @property
    def platforms(self) -> tuple[str, ...]:
        seen: list[str] = []
        for shot in self.screenshots:
            if shot.platform not in seen:
                seen.append(shot.platform)
        return tuple(seen)

    def to_dict(self, *, include_image_base64: bool = False) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "version": int(self.version),
            "entry_id": self.entry_id,
            "created_at": float(self.created_at),
            "code_diff": self.code_diff,
            "diff_stats": self.diff_stats.to_dict(),
            "screenshots": [
                s.to_dict(include_image_base64=include_image_base64)
                for s in self.screenshots
            ],
            "screenshot_count": self.screenshot_count,
            "real_capture_count": self.real_capture_count,
            "device_ids": list(self.device_ids),
            "platforms": list(self.platforms),
            "summary": self.summary,
            "author": self.author,
            "parent_version": int(self.parent_version),
            "tags": list(self.tags),
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class IterationTimeline:
    """Immutable view of one session's recorded iterations.

    :class:`MobileIterationTimelineBuilder` returns a fresh timeline
    on every :meth:`MobileIterationTimelineBuilder.timeline` call so
    consumers can safely iterate without holding the builder's lock.
    """

    session_id: str
    created_at: float
    updated_at: float
    entries: tuple[IterationEntry, ...] = ()
    dropped_count: int = 0
    next_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise MobileIterationTimelineConfigError(
                "session_id must be a non-empty string"
            )
        if not isinstance(self.created_at, (int, float)) or isinstance(
            self.created_at, bool
        ):
            raise MobileIterationTimelineConfigError(
                "created_at must be a non-negative number"
            )
        if self.created_at < 0:
            raise MobileIterationTimelineConfigError("created_at must be >= 0")
        if not isinstance(self.updated_at, (int, float)) or isinstance(
            self.updated_at, bool
        ):
            raise MobileIterationTimelineConfigError(
                "updated_at must be a non-negative number"
            )
        if self.updated_at < 0:
            raise MobileIterationTimelineConfigError("updated_at must be >= 0")
        if self.updated_at < self.created_at:
            raise MobileIterationTimelineConfigError(
                "updated_at must be >= created_at"
            )
        if not isinstance(self.entries, tuple):
            raise MobileIterationTimelineConfigError("entries must be a tuple")
        for entry in self.entries:
            if not isinstance(entry, IterationEntry):
                raise MobileIterationTimelineConfigError(
                    "entries must be IterationEntry instances"
                )
            if entry.session_id != self.session_id:
                raise MobileIterationTimelineConfigError(
                    f"entry.session_id {entry.session_id!r} does not match "
                    f"timeline session_id {self.session_id!r}"
                )
        if isinstance(self.dropped_count, bool) or not isinstance(
            self.dropped_count, int
        ):
            raise MobileIterationTimelineConfigError(
                "dropped_count must be a non-negative int"
            )
        if self.dropped_count < 0:
            raise MobileIterationTimelineConfigError(
                "dropped_count must be >= 0"
            )
        if isinstance(self.next_version, bool) or not isinstance(
            self.next_version, int
        ):
            raise MobileIterationTimelineConfigError(
                "next_version must be a positive int"
            )
        if self.next_version < 1:
            raise MobileIterationTimelineConfigError(
                "next_version must be >= 1"
            )

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def total_screenshots(self) -> int:
        return sum(e.screenshot_count for e in self.entries)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def latest_entry(self) -> IterationEntry | None:
        if not self.entries:
            return None
        return self.entries[-1]

    @property
    def first_entry(self) -> IterationEntry | None:
        if not self.entries:
            return None
        return self.entries[0]

    def to_dict(
        self, *, include_code_diff: bool = True, include_image_base64: bool = False
    ) -> dict[str, Any]:
        entries_dicts: list[dict[str, Any]] = []
        for entry in self.entries:
            data = entry.to_dict(include_image_base64=include_image_base64)
            if not include_code_diff:
                data.pop("code_diff", None)
            entries_dicts.append(data)
        return {
            "schema_version": MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "entry_count": self.entry_count,
            "total_screenshots": self.total_screenshots,
            "dropped_count": int(self.dropped_count),
            "next_version": int(self.next_version),
            "entries": entries_dicts,
        }


# ───────────────────────────────────────────────────────────────────
#  Rendering helpers
# ───────────────────────────────────────────────────────────────────


def _format_screenshot_line(index: int, shot: IterationScreenshot) -> str:
    size_kb = shot.byte_len / 1024 if shot.byte_len else 0.0
    dims = (
        f"{shot.width}×{shot.height}"
        if shot.width and shot.height
        else "unknown"
    )
    base = (
        f"  {index}. [{shot.status}] {shot.label} ({shot.device_id}, "
        f"{shot.platform}) — {dims}px, {size_kb:.1f} KB"
    )
    if shot.detail:
        base += f" — {shot.detail}"
    return base


def render_iteration_entry_markdown(entry: IterationEntry) -> str:
    """Render a single entry as human-readable markdown.

    Byte-stable for identical inputs — operator-facing dashboards can
    diff successive renderings.
    """

    if not isinstance(entry, IterationEntry):
        raise MobileIterationTimelineConfigError(
            "entry must be an IterationEntry"
        )

    lines: list[str] = []
    header = f"### Iteration #{entry.version} — `{entry.entry_id}`"
    lines.append(header)
    if entry.summary:
        lines.append(f"**Summary:** {entry.summary}")
    if entry.author:
        lines.append(f"**Author:** {entry.author}")
    if entry.parent_version:
        lines.append(f"**Parent:** iteration #{entry.parent_version}")
    stats = entry.diff_stats
    lines.append(
        f"**Diff:** {stats.files_changed} file(s), "
        f"+{stats.additions} / -{stats.deletions} (net {stats.net_change:+d})"
    )
    if entry.tags:
        lines.append(f"**Tags:** {', '.join(entry.tags)}")
    if entry.screenshots:
        lines.append("")
        lines.append("**Screenshots:**")
        for i, shot in enumerate(entry.screenshots, start=1):
            lines.append(_format_screenshot_line(i, shot))
    else:
        lines.append("**Screenshots:** none recorded this turn")
    if entry.warnings:
        lines.append("")
        lines.append("**Warnings:**")
        for w in entry.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines)


def render_timeline_markdown(timeline: IterationTimeline) -> str:
    """Render the whole timeline, newest entry last.

    Deterministic — operator dashboards pin the exact string in
    snapshot tests.
    """

    if not isinstance(timeline, IterationTimeline):
        raise MobileIterationTimelineConfigError(
            "timeline must be an IterationTimeline"
        )
    lines: list[str] = [
        f"## Iteration timeline — session `{timeline.session_id}`",
        (
            f"Entries: {timeline.entry_count} / screenshots: "
            f"{timeline.total_screenshots} / dropped: {timeline.dropped_count}"
        ),
    ]
    if timeline.is_empty:
        lines.append("")
        lines.append("_No iterations recorded yet._")
        return "\n".join(lines)
    for entry in timeline.entries:
        lines.append("")
        lines.append(render_iteration_entry_markdown(entry))
    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────
#  Builder
# ───────────────────────────────────────────────────────────────────


def _default_clock() -> float:
    return time.time()


class MobileIterationTimelineBuilder:
    """In-memory registry of per-session iteration timelines.

    The builder is thread-safe and stateless-on-construction: no
    per-session resources are provisioned until the first
    :meth:`record` call.  The default limits
    (:data:`DEFAULT_MAX_ENTRIES_PER_SESSION` /
    :data:`DEFAULT_MAX_DIFF_CHARS` /
    :data:`DEFAULT_MAX_SCREENSHOTS_PER_ENTRY`) are advisory — when a
    record exceeds one, the builder trims the payload and surfaces a
    ``code_diff_truncated`` / ``screenshots_truncated`` /
    ``iteration_dropped`` warning on the entry rather than raising.

    Multi-worker note
    -----------------
    The builder's state is module-private per-instance; in a
    multi-worker uvicorn deployment each worker keeps its own
    registry.  Operator dashboards must query the owning worker (or
    subscribe to the SSE bus, which is worker-local today).  The
    design is deliberately **"each worker independent"** per SOP
    Step 1 question #3 — the timeline is an operator-observable view,
    not a source of truth; the source of truth is the sandbox /
    git / filesystem that each worker already shares.
    """

    def __init__(
        self,
        *,
        max_entries_per_session: int = DEFAULT_MAX_ENTRIES_PER_SESSION,
        max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
        max_screenshots_per_entry: int = DEFAULT_MAX_SCREENSHOTS_PER_ENTRY,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
        event_cb: Callable[[str, Mapping[str, Any]], None] | None = None,
        clock: Callable[[], float] = _default_clock,
    ) -> None:
        for name, value in (
            ("max_entries_per_session", max_entries_per_session),
            ("max_diff_chars", max_diff_chars),
            ("max_screenshots_per_entry", max_screenshots_per_entry),
            ("max_summary_chars", max_summary_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise MobileIterationTimelineConfigError(
                    f"{name} must be a positive int — got {value!r}"
                )
        if event_cb is not None and not callable(event_cb):
            raise MobileIterationTimelineConfigError(
                "event_cb must be callable or None"
            )
        if not callable(clock):
            raise MobileIterationTimelineConfigError("clock must be callable")

        self._max_entries = int(max_entries_per_session)
        self._max_diff_chars = int(max_diff_chars)
        self._max_screenshots = int(max_screenshots_per_entry)
        self._max_summary_chars = int(max_summary_chars)
        self._event_cb = event_cb
        self._clock = clock

        self._lock = threading.RLock()
        self._entries: dict[str, list[IterationEntry]] = {}
        self._counters: dict[str, int] = {}
        self._created_at: dict[str, float] = {}
        self._updated_at: dict[str, float] = {}
        self._dropped: dict[str, int] = {}
        self._record_count = 0
        self._reset_count = 0
        self._last_entry: IterationEntry | None = None

    # ── configuration accessors ─────────────────────────────
    @property
    def max_entries_per_session(self) -> int:
        return self._max_entries

    @property
    def max_diff_chars(self) -> int:
        return self._max_diff_chars

    @property
    def max_screenshots_per_entry(self) -> int:
        return self._max_screenshots

    @property
    def max_summary_chars(self) -> int:
        return self._max_summary_chars

    # ── counter accessors (mostly for tests / ops dashboards) ──
    @property
    def record_count(self) -> int:
        with self._lock:
            return self._record_count

    @property
    def reset_count(self) -> int:
        with self._lock:
            return self._reset_count

    @property
    def last_entry(self) -> IterationEntry | None:
        with self._lock:
            return self._last_entry

    def _emit(self, topic: str, payload: Mapping[str, Any]) -> None:
        cb = self._event_cb
        if cb is None:
            return
        try:
            cb(topic, dict(payload))
        except Exception:  # pragma: no cover - callback must not kill builder
            logger.exception(
                "mobile_iteration_timeline event callback failed for %s", topic
            )

    def _require_session_id(self, session_id: Any) -> str:
        if not isinstance(session_id, str) or not session_id.strip():
            raise MobileIterationTimelineConfigError(
                "session_id must be a non-empty string"
            )
        if not _SAFE_SESSION_ID_RE.fullmatch(session_id):
            raise MobileIterationTimelineConfigError(
                "session_id must match [A-Za-z0-9_.-]{1,64} — got "
                f"{session_id!r}"
            )
        return session_id

    def _coerce_screenshots(
        self, screenshots: Any
    ) -> tuple[IterationScreenshot, ...]:
        if screenshots is None:
            return ()
        if isinstance(screenshots, IterationScreenshot):
            raise MobileIterationTimelineConfigError(
                "screenshots must be an iterable, not a single IterationScreenshot"
            )
        if isinstance(screenshots, (str, bytes)):
            raise MobileIterationTimelineConfigError(
                "screenshots must be an iterable of IterationScreenshot"
            )
        if not isinstance(screenshots, Sequence):
            try:
                screenshots = tuple(screenshots)
            except TypeError as exc:
                raise MobileIterationTimelineConfigError(
                    "screenshots must be iterable"
                ) from exc
        shots: list[IterationScreenshot] = []
        for s in screenshots:
            if not isinstance(s, IterationScreenshot):
                raise MobileIterationTimelineConfigError(
                    "screenshots entries must be IterationScreenshot"
                )
            shots.append(s)
        return tuple(shots)

    def _coerce_tags(self, tags: Any) -> tuple[str, ...]:
        if tags is None:
            return ()
        if isinstance(tags, str):
            raise MobileIterationTimelineConfigError(
                "tags must be an iterable of strings, not a single string"
            )
        result: list[str] = []
        for t in tags:
            if not isinstance(t, str):
                raise MobileIterationTimelineConfigError(
                    "tags entries must be strings"
                )
            stripped = t.strip()
            if not stripped:
                raise MobileIterationTimelineConfigError(
                    "tags entries must be non-empty"
                )
            if stripped in result:
                continue
            result.append(stripped)
        return tuple(result)

    def _coerce_metadata(self, metadata: Any) -> Mapping[str, Any]:
        if metadata is None:
            return {}
        if not isinstance(metadata, Mapping):
            raise MobileIterationTimelineConfigError(
                "metadata must be a Mapping"
            )
        clean: dict[str, Any] = {}
        for k, v in metadata.items():
            if not isinstance(k, str):
                raise MobileIterationTimelineConfigError(
                    "metadata keys must be strings"
                )
            clean[k] = v
        return clean

    def record(
        self,
        *,
        session_id: str,
        code_diff: str,
        screenshots: Sequence[IterationScreenshot] | None = None,
        summary: str = "",
        author: str = "",
        tags: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        diff_stats: IterationDiffStats | None = None,
    ) -> IterationEntry:
        """Record one iteration against ``session_id``.

        Returns the resulting :class:`IterationEntry` (post-trim,
        post-validation).  Raises
        :class:`MobileIterationTimelineConfigError` on invalid input;
        otherwise never raises.  The returned entry's ``version`` is
        monotonic per session — even if the ring-buffer drops the
        previous first entry, the new version keeps climbing.
        """

        sid = self._require_session_id(session_id)

        if not isinstance(code_diff, str):
            raise MobileIterationTimelineConfigError(
                "code_diff must be a string"
            )
        if not isinstance(summary, str):
            raise MobileIterationTimelineConfigError("summary must be a string")
        if not isinstance(author, str):
            raise MobileIterationTimelineConfigError("author must be a string")
        if diff_stats is not None and not isinstance(diff_stats, IterationDiffStats):
            raise MobileIterationTimelineConfigError(
                "diff_stats must be an IterationDiffStats or None"
            )

        shots = self._coerce_screenshots(screenshots)
        coerced_tags = self._coerce_tags(tags)
        coerced_metadata = self._coerce_metadata(metadata)

        warnings: list[str] = []

        # Trim oversize diff.  Keep the head + a marker so the operator
        # can still inspect the leading context in the timeline UI.
        trimmed_diff = code_diff
        if len(trimmed_diff) > self._max_diff_chars:
            head = trimmed_diff[: self._max_diff_chars]
            omitted = len(trimmed_diff) - self._max_diff_chars
            trimmed_diff = (
                head
                + f"\n… truncated {omitted} character(s) "
                "(exceeded max_diff_chars)"
            )
            warnings.append(
                f"code_diff_truncated:{omitted}:{self._max_diff_chars}"
            )

        # Trim oversize screenshot list.  Keep head — matches the V6 #5
        # budget helper pattern (preserve deterministic order).
        trimmed_shots = shots
        if len(trimmed_shots) > self._max_screenshots:
            overflow = len(trimmed_shots) - self._max_screenshots
            trimmed_shots = trimmed_shots[: self._max_screenshots]
            warnings.append(
                f"screenshots_truncated:{overflow}:{self._max_screenshots}"
            )

        trimmed_summary = summary
        if len(trimmed_summary) > self._max_summary_chars:
            trimmed_summary = (
                trimmed_summary[: self._max_summary_chars].rstrip() + "…"
            )
            warnings.append(
                f"summary_truncated:{len(summary) - self._max_summary_chars}"
            )

        stats = diff_stats if diff_stats is not None else parse_diff_stats(trimmed_diff)

        now = float(self._clock())

        self._emit(
            MOBILE_ITERATION_TIMELINE_EVENT_RECORDING,
            {
                "session_id": sid,
                "code_diff_length": len(trimmed_diff),
                "screenshot_count": len(trimmed_shots),
                "files_changed": stats.files_changed,
                "additions": stats.additions,
                "deletions": stats.deletions,
            },
        )

        try:
            with self._lock:
                previous_version = self._counters.get(sid, 0)
                version = previous_version + 1
                entry_id = format_iteration_entry_id(sid, version)

                entry = IterationEntry(
                    session_id=sid,
                    version=version,
                    entry_id=entry_id,
                    created_at=now,
                    code_diff=trimmed_diff,
                    diff_stats=stats,
                    screenshots=trimmed_shots,
                    summary=trimmed_summary,
                    author=author,
                    parent_version=previous_version,
                    tags=coerced_tags,
                    warnings=tuple(warnings),
                    metadata=coerced_metadata,
                )

                bucket = self._entries.setdefault(sid, [])
                bucket.append(entry)
                self._counters[sid] = version
                self._created_at.setdefault(sid, now)
                self._updated_at[sid] = now

                dropped = 0
                while len(bucket) > self._max_entries:
                    bucket.pop(0)
                    dropped += 1
                if dropped:
                    self._dropped[sid] = self._dropped.get(sid, 0) + dropped

                self._record_count += 1
                self._last_entry = entry
        except MobileIterationTimelineError:
            self._emit(
                MOBILE_ITERATION_TIMELINE_EVENT_RECORD_FAILED,
                {"session_id": sid},
            )
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._emit(
                MOBILE_ITERATION_TIMELINE_EVENT_RECORD_FAILED,
                {"session_id": sid, "error": type(exc).__name__},
            )
            raise MobileIterationTimelineError(
                f"unexpected error recording iteration: {exc!r}"
            ) from exc

        self._emit(
            MOBILE_ITERATION_TIMELINE_EVENT_RECORDED,
            {
                "session_id": sid,
                "version": entry.version,
                "entry_id": entry.entry_id,
                "screenshot_count": entry.screenshot_count,
                "real_capture_count": entry.real_capture_count,
                "files_changed": entry.diff_stats.files_changed,
                "dropped_count": self._dropped.get(sid, 0),
                "warnings": list(entry.warnings),
            },
        )
        return entry

    def record_from_screenshot_results(
        self,
        *,
        session_id: str,
        code_diff: str,
        targets: Sequence[Any],
        results: Sequence[Any],
        summary: str = "",
        author: str = "",
        tags: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        include_image_base64: bool = False,
    ) -> IterationEntry:
        """Convenience wrapper for callers that already have V6 #5
        :class:`MobileDeviceTarget` / V6 #2
        :class:`ScreenshotResult` tuples paired 1:1.

        Internally uses :func:`screenshot_from_result` so this module
        never has to import V6 #2 / V6 #5 into its top-level
        dependency graph.  Targets and results must have the same
        length; mismatches raise
        :class:`MobileIterationTimelineConfigError`.
        """

        if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
            raise MobileIterationTimelineConfigError(
                "targets must be a sequence"
            )
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
            raise MobileIterationTimelineConfigError(
                "results must be a sequence"
            )
        if len(targets) != len(results):
            raise MobileIterationTimelineConfigError(
                f"targets and results must match in length — got "
                f"{len(targets)} vs {len(results)}"
            )

        shots: list[IterationScreenshot] = []
        for target, result in zip(targets, results):
            device_id = getattr(target, "device_id", None)
            label = getattr(target, "label", "") or ""
            if not isinstance(device_id, str) or not device_id.strip():
                raise MobileIterationTimelineConfigError(
                    "target must expose a non-empty device_id"
                )
            shots.append(
                screenshot_from_result(
                    device_id=device_id,
                    label=label,
                    result=result,
                    include_image_base64=include_image_base64,
                )
            )
        return self.record(
            session_id=session_id,
            code_diff=code_diff,
            screenshots=tuple(shots),
            summary=summary,
            author=author,
            tags=tags,
            metadata=metadata,
        )

    def timeline(self, session_id: str) -> IterationTimeline:
        """Return an immutable :class:`IterationTimeline` for ``session_id``.

        Creates an empty timeline (``entry_count=0``, ``created_at`` =
        now, ``updated_at`` = now) for a session that has never been
        recorded against — the Mobile Workspace UI can bind to an
        empty timeline without special-casing.
        """

        sid = self._require_session_id(session_id)
        with self._lock:
            if sid not in self._entries:
                now = float(self._clock())
                return IterationTimeline(
                    session_id=sid,
                    created_at=now,
                    updated_at=now,
                    entries=(),
                    dropped_count=0,
                    next_version=1,
                )
            entries = tuple(self._entries[sid])
            return IterationTimeline(
                session_id=sid,
                created_at=self._created_at[sid],
                updated_at=self._updated_at[sid],
                entries=entries,
                dropped_count=self._dropped.get(sid, 0),
                next_version=self._counters.get(sid, 0) + 1,
            )

    def list_entries(self, session_id: str) -> tuple[IterationEntry, ...]:
        """Return the session's entries in chronological order."""

        sid = self._require_session_id(session_id)
        with self._lock:
            if sid not in self._entries:
                return ()
            return tuple(self._entries[sid])

    def get_entry(self, session_id: str, version: int) -> IterationEntry:
        """Fetch one entry by version.

        Raises :class:`MobileIterationTimelineNotFoundError` if the
        session does not exist or the entry has been dropped by the
        ring buffer.
        """

        sid = self._require_session_id(session_id)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise MobileIterationTimelineConfigError(
                "version must be a positive int"
            )
        with self._lock:
            bucket = self._entries.get(sid)
            if not bucket:
                raise MobileIterationTimelineNotFoundError(
                    f"no iterations recorded for session {sid!r}"
                )
            for entry in bucket:
                if entry.version == version:
                    return entry
        raise MobileIterationTimelineNotFoundError(
            f"iteration {version} not found for session {sid!r} "
            "(dropped by ring buffer or never recorded)"
        )

    def latest_entry(self, session_id: str) -> IterationEntry | None:
        sid = self._require_session_id(session_id)
        with self._lock:
            bucket = self._entries.get(sid)
            if not bucket:
                return None
            return bucket[-1]

    def reset(self, session_id: str) -> bool:
        """Drop *all* state for ``session_id``.

        Returns ``True`` if any state was dropped, ``False`` if the
        session was unknown.  Version counter also resets so the next
        recorded entry is version 1 again — this is the "start a new
        codegen conversation" escape hatch for operators.
        """

        sid = self._require_session_id(session_id)
        with self._lock:
            had_state = (
                sid in self._entries
                or sid in self._counters
                or sid in self._created_at
            )
            self._entries.pop(sid, None)
            self._counters.pop(sid, None)
            self._created_at.pop(sid, None)
            self._updated_at.pop(sid, None)
            self._dropped.pop(sid, None)
            if had_state:
                self._reset_count += 1
        if had_state:
            self._emit(
                MOBILE_ITERATION_TIMELINE_EVENT_RESET,
                {"session_id": sid},
            )
        return had_state

    def sessions(self) -> tuple[str, ...]:
        """Return every session the builder has an active timeline for."""

        with self._lock:
            return tuple(sorted(self._entries.keys()))

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe operator-dashboard view of every session.

        Never inlines ``image_base64`` (timelines can stretch across
        tens of screenshots — an SSE frame dump would blow past the
        proxy limit).  Never inlines full ``code_diff`` for the same
        reason — the dashboard shows per-session entry counts and the
        latest entry id; consumers call
        :meth:`get_entry` on demand for the raw diff.
        """

        with self._lock:
            sessions_payload: list[dict[str, Any]] = []
            for sid in sorted(self._entries.keys()):
                bucket = self._entries[sid]
                latest = bucket[-1] if bucket else None
                sessions_payload.append(
                    {
                        "session_id": sid,
                        "entry_count": len(bucket),
                        "total_screenshots": sum(e.screenshot_count for e in bucket),
                        "dropped_count": self._dropped.get(sid, 0),
                        "next_version": self._counters.get(sid, 0) + 1,
                        "created_at": self._created_at.get(sid, 0.0),
                        "updated_at": self._updated_at.get(sid, 0.0),
                        "latest_entry_id": latest.entry_id if latest else None,
                        "latest_version": latest.version if latest else 0,
                    }
                )
            return {
                "schema_version": MOBILE_ITERATION_TIMELINE_SCHEMA_VERSION,
                "record_count": self._record_count,
                "reset_count": self._reset_count,
                "session_count": len(self._entries),
                "max_entries_per_session": self._max_entries,
                "max_diff_chars": self._max_diff_chars,
                "max_screenshots_per_entry": self._max_screenshots,
                "max_summary_chars": self._max_summary_chars,
                "sessions": sessions_payload,
            }
