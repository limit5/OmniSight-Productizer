"""V6 #6 (issue #322) — Mobile build-error → agent auto-fix loop.

Closes the **mobile** ReAct auto-fix loop end-to-end: every attempt
runs ``mobile_sandbox`` (V6 #1) ``build`` + (optional) ``install`` /
``screenshot``, parses Gradle / Xcode diagnostics into structured
:class:`backend.mobile_sandbox.BuildError` records, hands those plus
the per-device multimodal payload from ``mobile_agent_visual_context``
(V6 #5) to a caller-supplied agent callback, and — if the agent
patches files — re-runs the loop until the build is clean or the
attempt cap is hit.

Where this sits in the V6 stack
-------------------------------

* V6 #1 ``mobile_sandbox.py`` owns the per-session
  ``build → install → run → screenshot`` lifecycle and surfaces
  ``BuildError`` records on its ``MobileSandboxInstance.build``.
* V6 #2 ``mobile_screenshot.py`` is the ad-hoc screenshot primitive
  reused by V6 #5 to feed multimodal pixels into the agent context.
* V6 #3 / #4 device-frame + device-grid render the screenshots in
  the operator UI.
* V6 #5 ``mobile_agent_visual_context.py`` packs a
  ``MobileAgentVisualContextPayload`` per turn (text + images +
  optional build-error summary) for Opus 4.7's multimodal endpoint.

**V6 #6 (this module)** is the *orchestrator* that wires V6 #1 +
V6 #5 + a caller-supplied agent fix callback together so the
build-error → patch-code → rebuild → re-screenshot loop runs with a
deterministic terminal state, an audit trail of every attempt, and
zero hidden side effects on the sandbox manager.

Design decisions
----------------

* **Composition, not inheritance.**
  :class:`MobileAutoFixLoop` *holds* a
  :class:`backend.mobile_sandbox.MobileSandboxManager`,
  a :class:`backend.mobile_agent_visual_context.MobileAgentVisualContextBuilder`,
  and an :data:`AgentFixFn` callback.  All three are dependency-
  injected so tests substitute fakes that record the calls and serve
  canned responses without touching docker / adb / xcrun / Anthropic.
* **Frozen attempt records.**  :class:`MobileAutoFixAttempt` and
  :class:`MobileAutoFixOutcome` are frozen dataclasses with
  :meth:`to_dict` for SSE / audit / snapshot consumers — every
  invocation produces the same JSON shape, audit-friendly.
* **Idempotent sandbox reset between attempts.**  The manager API
  forbids ``build()`` from a ``failed`` or ``built`` instance; the
  loop's :meth:`_reset_for_rebuild` performs ``stop`` (no-op for
  terminal states) → ``remove`` → ``create`` so each attempt starts
  from a fresh ``pending`` instance using the same config.
* **Per-attempt isolation.**  Agent callback raise → attempt status
  flips to ``agent_error`` and the outcome closes as ``failed`` —
  the manager is left in whatever state the attempt produced; we
  never propagate the agent's exception to the orchestrator caller.
* **Non-blocking by default.**  ``failure_mode="continue"`` (default)
  never raises; ``failure_mode="abort"`` re-raises the agent's
  exception (or :class:`MobileAutoFixError` for sandbox-side
  failures) so CI callers can hard-fail.
* **Visual context optional.**  When ``visual_context_builder=None``
  the loop runs the build / install / screenshot cycle but skips the
  multimodal payload — agent callbacks that don't need pixels
  (heuristic fixers, lint-style auto-rewriters) save the encode +
  base64 work.  When wired, the builder is asked for one payload
  per failed attempt with a transient ``error_source`` closure that
  exposes the freshly-built :class:`MobileBuildErrorSummary`.
* **Agent contract.**  :data:`AgentFixFn` returns
  :class:`MobileAutoFixResponse` whose ``action`` is one of
  ``patched`` (loop continues) / ``no_op`` (loop ends as
  ``failed``) / ``give_up`` (loop ends as ``failed``).  Files the
  agent touched flow through ``files_touched`` purely as audit
  metadata — the loop *does not* re-read the workspace; it trusts
  the next ``manager.build()`` to discover whether the patch worked.
* **Event namespace.**  ``mobile_sandbox.autofix.*`` — eight topics
  ``started`` / ``attempt_started`` / ``build_passed`` /
  ``build_failed`` / ``fix_applied`` / ``attempt_finished`` /
  ``succeeded`` / ``failed`` / ``exhausted`` / ``skipped``.
  Disjoint from V6 #1 (``mobile_sandbox.<state>``) and V6 #5
  (``mobile_sandbox.agent_visual_context.*``) so SSE bus subscribers
  filter on prefix without colliding.
* **Mock-aware.**  When the build returns ``status="mock"`` (docker
  / adb / ssh missing on the CI host) the loop short-circuits to
  ``skipped`` — never calls the agent.  This matches V6 #1's
  "tooling missing" semantics.

Public API
----------
* :class:`MobileAutoFixLoop` — the orchestrator.
* :class:`MobileAutoFixRequest` / :class:`MobileAutoFixResponse` —
  the agent fix callback contract.
* :class:`MobileAutoFixAttempt` / :class:`MobileAutoFixOutcome` —
  audit records.
* :class:`AutoFixStatus` / :class:`AutoFixAttemptStatus` /
  :class:`AgentFixAction` — closed enums.
* :func:`summarise_build_errors` — convert
  ``tuple[BuildError]`` → :class:`MobileBuildErrorSummary` for V6 #5.
* :func:`render_autofix_outcome_markdown` — operator-readable
  outcome summary.

Contract pinned by ``backend/tests/test_mobile_build_error_autofix.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from backend.mobile_agent_visual_context import (
    MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
    MobileAgentVisualContextBuilder,
    MobileAgentVisualContextPayload,
    MobileBuildErrorSummary,
    MobileDeviceTarget,
)
from backend.mobile_sandbox import (
    MOBILE_SANDBOX_SCHEMA_VERSION,
    SUPPORTED_PLATFORMS,
    BuildError,
    MobileSandboxAlreadyExists,
    MobileSandboxConfig,
    MobileSandboxError,
    MobileSandboxInstance,
    MobileSandboxManager,
)

logger = logging.getLogger(__name__)


__all__ = [
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
]


# ───────────────────────────────────────────────────────────────────
#  Constants
# ───────────────────────────────────────────────────────────────────


#: Bump on shape changes to :class:`MobileAutoFixAttempt.to_dict` /
#: :class:`MobileAutoFixOutcome.to_dict` /
#: :class:`MobileAutoFixRequest.to_dict` /
#: :class:`MobileAutoFixResponse.to_dict`.
MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION = "1.0.0"

#: Default cap on the build-fix-rebuild cycle.  4 attempts gives the
#: agent ``initial fail → patch1 → patch2 → patch3`` budget — more
#: than that and a human likely needs to step in.  Callers can raise
#: per :meth:`MobileAutoFixLoop.run`.
DEFAULT_MAX_ATTEMPTS = 4

#: Failure-mode vocabulary.  ``continue`` (default) never raises out
#: of :meth:`MobileAutoFixLoop.run` — every condition surfaces as a
#: terminal :class:`MobileAutoFixOutcome`.  ``abort`` re-raises agent
#: callback exceptions and unrecoverable sandbox errors so CI callers
#: can hard-fail.
FAILURE_MODES: tuple[str, ...] = ("continue", "abort")

#: Default failure mode — agent loops use ``continue`` so a single
#: bad fix doesn't escape into the orchestration layer.
DEFAULT_FAILURE_MODE = "continue"


# ───────────────────────────────────────────────────────────────────
#  Events
# ───────────────────────────────────────────────────────────────────


AUTOFIX_EVENT_STARTED = "mobile_sandbox.autofix.started"
AUTOFIX_EVENT_ATTEMPT_STARTED = "mobile_sandbox.autofix.attempt_started"
AUTOFIX_EVENT_BUILD_PASSED = "mobile_sandbox.autofix.build_passed"
AUTOFIX_EVENT_BUILD_FAILED = "mobile_sandbox.autofix.build_failed"
AUTOFIX_EVENT_FIX_APPLIED = "mobile_sandbox.autofix.fix_applied"
AUTOFIX_EVENT_ATTEMPT_FINISHED = "mobile_sandbox.autofix.attempt_finished"
AUTOFIX_EVENT_SUCCEEDED = "mobile_sandbox.autofix.succeeded"
AUTOFIX_EVENT_FAILED = "mobile_sandbox.autofix.failed"
AUTOFIX_EVENT_EXHAUSTED = "mobile_sandbox.autofix.exhausted"
AUTOFIX_EVENT_SKIPPED = "mobile_sandbox.autofix.skipped"


#: Full roster of topics the loop emits — SSE bus subscribes on the
#: ``mobile_sandbox.autofix.`` prefix.
AUTOFIX_EVENT_TYPES: tuple[str, ...] = (
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
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class MobileAutoFixError(RuntimeError):
    """Base class for ``mobile_build_error_autofix`` errors.  Routers
    can catch this single type to translate every failure into one
    structured HTTP / event payload."""


class MobileAutoFixConfigError(MobileAutoFixError):
    """Raised when constructor / :meth:`MobileAutoFixLoop.run`
    arguments fail validation."""


class MobileAutoFixSandboxError(MobileAutoFixError):
    """Raised when the underlying :class:`MobileSandboxManager`
    refuses an operation in a way the loop cannot recover from
    (e.g. a peer holding the same session_id sandbox the loop is
    trying to (re)create)."""


# ───────────────────────────────────────────────────────────────────
#  Enums
# ───────────────────────────────────────────────────────────────────


class AutoFixStatus(str, Enum):
    """Terminal status of one full :meth:`MobileAutoFixLoop.run`."""

    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    exhausted = "exhausted"
    skipped = "skipped"


class AutoFixAttemptStatus(str, Enum):
    """Terminal status of one attempt inside the loop."""

    pending = "pending"
    build_passed = "build_passed"
    build_failed = "build_failed"
    agent_patched = "agent_patched"
    agent_no_op = "agent_no_op"
    agent_give_up = "agent_give_up"
    agent_error = "agent_error"
    sandbox_error = "sandbox_error"
    skipped = "skipped"


class AgentFixAction(str, Enum):
    """Action the agent reported in :class:`MobileAutoFixResponse`."""

    patched = "patched"
    no_op = "no_op"
    give_up = "give_up"


#: Stable string vocabulary the agent callback uses on
#: :class:`MobileAutoFixResponse.action`.
AGENT_FIX_ACTIONS: tuple[str, ...] = tuple(a.value for a in AgentFixAction)


# ───────────────────────────────────────────────────────────────────
#  Records
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MobileAutoFixRequest:
    """Input handed to the agent fix callback each attempt.

    Carries the structured build errors the agent should focus on,
    the V6 #5 :class:`MobileAgentVisualContextPayload` (text + images)
    when the loop is wired with a visual context builder, plus a
    snapshot of the sandbox state for context.

    Frozen + JSON-safe via :meth:`to_dict`.
    """

    session_id: str
    attempt_index: int
    workspace_path: str
    platform: str
    build_errors: tuple[BuildError, ...]
    error_summary: MobileBuildErrorSummary
    visual_payload: MobileAgentVisualContextPayload | None
    sandbox_snapshot: Mapping[str, Any]
    previous_attempts: tuple["MobileAutoFixAttempt", ...]
    requested_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(self.attempt_index, int) or self.attempt_index < 1:
            raise ValueError("attempt_index must be a positive int")
        if not isinstance(self.workspace_path, str) or not self.workspace_path.strip():
            raise ValueError("workspace_path must be a non-empty string")
        if self.platform not in SUPPORTED_PLATFORMS:
            raise ValueError(
                f"platform must be one of {SUPPORTED_PLATFORMS!r}, got "
                f"{self.platform!r}"
            )
        if not isinstance(self.build_errors, tuple):
            raise ValueError("build_errors must be a tuple")
        for err in self.build_errors:
            if not isinstance(err, BuildError):
                raise ValueError(
                    "build_errors entries must be BuildError"
                )
        if not isinstance(self.error_summary, MobileBuildErrorSummary):
            raise ValueError(
                "error_summary must be a MobileBuildErrorSummary"
            )
        if self.visual_payload is not None and not isinstance(
            self.visual_payload, MobileAgentVisualContextPayload
        ):
            raise ValueError(
                "visual_payload must be MobileAgentVisualContextPayload or None"
            )
        if not isinstance(self.sandbox_snapshot, Mapping):
            raise ValueError("sandbox_snapshot must be a Mapping")
        if not isinstance(self.previous_attempts, tuple):
            raise ValueError("previous_attempts must be a tuple")
        for a in self.previous_attempts:
            if not isinstance(a, MobileAutoFixAttempt):
                raise ValueError(
                    "previous_attempts entries must be MobileAutoFixAttempt"
                )
        if self.requested_at < 0:
            raise ValueError("requested_at must be non-negative")

    @property
    def has_visual_payload(self) -> bool:
        return self.visual_payload is not None

    @property
    def error_count(self) -> int:
        return len(self.build_errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
            "session_id": self.session_id,
            "attempt_index": int(self.attempt_index),
            "workspace_path": self.workspace_path,
            "platform": self.platform,
            "build_errors": [e.to_dict() for e in self.build_errors],
            "error_summary": self.error_summary.to_dict(),
            "visual_payload": (
                self.visual_payload.to_dict()
                if self.visual_payload is not None
                else None
            ),
            "sandbox_snapshot": dict(self.sandbox_snapshot),
            "previous_attempts": [a.to_dict() for a in self.previous_attempts],
            "requested_at": float(self.requested_at),
            "has_visual_payload": self.has_visual_payload,
            "error_count": self.error_count,
        }


@dataclass(frozen=True)
class MobileAutoFixResponse:
    """Output the agent fix callback returns each attempt.

    ``action`` drives the loop:
      * ``patched`` — loop rebuilds and re-evaluates.
      * ``no_op`` — agent declined to change anything; loop ends as
        ``failed`` so the operator sees the unfixable build.
      * ``give_up`` — agent explicitly refused (e.g. exceeded its
        own confidence threshold); loop ends as ``failed``.

    Frozen + JSON-safe.
    """

    action: str
    summary: str = ""
    files_touched: tuple[str, ...] = ()
    raw_response: str = ""

    def __post_init__(self) -> None:
        if self.action not in AGENT_FIX_ACTIONS:
            raise ValueError(
                f"action must be one of {AGENT_FIX_ACTIONS!r}, got "
                f"{self.action!r}"
            )
        if not isinstance(self.summary, str):
            raise ValueError("summary must be a string")
        if not isinstance(self.files_touched, tuple):
            raise ValueError("files_touched must be a tuple")
        for f in self.files_touched:
            if not isinstance(f, str) or not f:
                raise ValueError(
                    "files_touched entries must be non-empty strings"
                )
        if not isinstance(self.raw_response, str):
            raise ValueError("raw_response must be a string")

    @property
    def is_patch(self) -> bool:
        return self.action == AgentFixAction.patched.value

    @property
    def is_no_op(self) -> bool:
        return self.action == AgentFixAction.no_op.value

    @property
    def is_give_up(self) -> bool:
        return self.action == AgentFixAction.give_up.value

    @property
    def file_count(self) -> int:
        return len(self.files_touched)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
            "action": self.action,
            "summary": self.summary,
            "files_touched": list(self.files_touched),
            "raw_response": self.raw_response,
            "is_patch": self.is_patch,
            "is_no_op": self.is_no_op,
            "is_give_up": self.is_give_up,
            "file_count": self.file_count,
        }


@dataclass(frozen=True)
class MobileAutoFixAttempt:
    """One iteration of the build-fix-rebuild loop.

    Records the build outcome, agent action (if invoked), screenshot
    status (if captured), and warnings.  Frozen + JSON-safe.
    """

    attempt_index: int
    started_at: float
    finished_at: float
    status: AutoFixAttemptStatus
    build_status: str = ""
    build_error_count: int = 0
    build_errors: tuple[BuildError, ...] = ()
    install_status: str = ""
    screenshot_status: str = ""
    screenshot_path: str = ""
    agent_action: str = ""
    agent_summary: str = ""
    agent_files_touched: tuple[str, ...] = ()
    visual_payload_built: bool = False
    visual_image_count: int = 0
    detail: str = ""
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_index, int) or self.attempt_index < 1:
            raise ValueError("attempt_index must be a positive int")
        if self.started_at < 0 or self.finished_at < 0:
            raise ValueError("timestamps must be non-negative")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must be >= started_at")
        if not isinstance(self.status, AutoFixAttemptStatus):
            raise ValueError("status must be AutoFixAttemptStatus")
        if not isinstance(self.build_status, str):
            raise ValueError("build_status must be a string")
        if not isinstance(self.build_error_count, int) or self.build_error_count < 0:
            raise ValueError("build_error_count must be a non-negative int")
        if not isinstance(self.build_errors, tuple):
            raise ValueError("build_errors must be a tuple")
        for err in self.build_errors:
            if not isinstance(err, BuildError):
                raise ValueError("build_errors entries must be BuildError")
        if not isinstance(self.install_status, str):
            raise ValueError("install_status must be a string")
        if not isinstance(self.screenshot_status, str):
            raise ValueError("screenshot_status must be a string")
        if not isinstance(self.screenshot_path, str):
            raise ValueError("screenshot_path must be a string")
        if self.agent_action and self.agent_action not in AGENT_FIX_ACTIONS:
            raise ValueError(
                "agent_action must be empty or one of "
                f"{AGENT_FIX_ACTIONS!r}, got {self.agent_action!r}"
            )
        if not isinstance(self.agent_summary, str):
            raise ValueError("agent_summary must be a string")
        if not isinstance(self.agent_files_touched, tuple):
            raise ValueError("agent_files_touched must be a tuple")
        for f in self.agent_files_touched:
            if not isinstance(f, str) or not f:
                raise ValueError(
                    "agent_files_touched entries must be non-empty strings"
                )
        if not isinstance(self.visual_payload_built, bool):
            raise ValueError("visual_payload_built must be bool")
        if (
            not isinstance(self.visual_image_count, int)
            or self.visual_image_count < 0
        ):
            raise ValueError("visual_image_count must be a non-negative int")
        if not isinstance(self.detail, str):
            raise ValueError("detail must be a string")
        if not isinstance(self.warnings, tuple):
            raise ValueError("warnings must be a tuple")
        for w in self.warnings:
            if not isinstance(w, str) or not w:
                raise ValueError("warnings entries must be non-empty strings")

    @property
    def duration_ms(self) -> int:
        return int(max(0.0, self.finished_at - self.started_at) * 1000)

    @property
    def did_call_agent(self) -> bool:
        return bool(self.agent_action)

    @property
    def is_terminal_for_loop(self) -> bool:
        """Return True when this attempt should stop the outer loop.

        ``build_passed`` → outer loop succeeds.
        ``agent_no_op`` / ``agent_give_up`` / ``agent_error`` /
        ``sandbox_error`` / ``skipped`` → outer loop ends without
        retrying.  ``agent_patched`` (rebuild requested) and any
        unrecognised state → loop continues.
        """

        return self.status in {
            AutoFixAttemptStatus.build_passed,
            AutoFixAttemptStatus.agent_no_op,
            AutoFixAttemptStatus.agent_give_up,
            AutoFixAttemptStatus.agent_error,
            AutoFixAttemptStatus.sandbox_error,
            AutoFixAttemptStatus.skipped,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
            "attempt_index": int(self.attempt_index),
            "started_at": float(self.started_at),
            "finished_at": float(self.finished_at),
            "duration_ms": self.duration_ms,
            "status": self.status.value,
            "build_status": self.build_status,
            "build_error_count": int(self.build_error_count),
            "build_errors": [e.to_dict() for e in self.build_errors],
            "install_status": self.install_status,
            "screenshot_status": self.screenshot_status,
            "screenshot_path": self.screenshot_path,
            "agent_action": self.agent_action,
            "agent_summary": self.agent_summary,
            "agent_files_touched": list(self.agent_files_touched),
            "visual_payload_built": bool(self.visual_payload_built),
            "visual_image_count": int(self.visual_image_count),
            "did_call_agent": self.did_call_agent,
            "detail": self.detail,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MobileAutoFixOutcome:
    """Final result of one :meth:`MobileAutoFixLoop.run` invocation.

    Aggregates every :class:`MobileAutoFixAttempt`, the terminal
    status, and rolled-up counters.  Frozen + JSON-safe.
    """

    session_id: str
    platform: str
    workspace_path: str
    final_status: AutoFixStatus
    started_at: float
    finished_at: float
    attempts: tuple[MobileAutoFixAttempt, ...]
    initial_error_count: int = 0
    final_error_count: int = 0
    final_build_status: str = ""
    final_screenshot_status: str = ""
    final_screenshot_path: str = ""
    detail: str = ""
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if self.platform not in SUPPORTED_PLATFORMS:
            raise ValueError(
                f"platform must be one of {SUPPORTED_PLATFORMS!r}, got "
                f"{self.platform!r}"
            )
        if not isinstance(self.workspace_path, str) or not self.workspace_path.strip():
            raise ValueError("workspace_path must be a non-empty string")
        if not isinstance(self.final_status, AutoFixStatus):
            raise ValueError("final_status must be AutoFixStatus")
        if self.started_at < 0 or self.finished_at < 0:
            raise ValueError("timestamps must be non-negative")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must be >= started_at")
        if not isinstance(self.attempts, tuple):
            raise ValueError("attempts must be a tuple")
        for a in self.attempts:
            if not isinstance(a, MobileAutoFixAttempt):
                raise ValueError("attempts entries must be MobileAutoFixAttempt")
        if (
            not isinstance(self.initial_error_count, int)
            or self.initial_error_count < 0
        ):
            raise ValueError("initial_error_count must be a non-negative int")
        if (
            not isinstance(self.final_error_count, int)
            or self.final_error_count < 0
        ):
            raise ValueError("final_error_count must be a non-negative int")
        if not isinstance(self.final_build_status, str):
            raise ValueError("final_build_status must be a string")
        if not isinstance(self.final_screenshot_status, str):
            raise ValueError("final_screenshot_status must be a string")
        if not isinstance(self.final_screenshot_path, str):
            raise ValueError("final_screenshot_path must be a string")
        if not isinstance(self.detail, str):
            raise ValueError("detail must be a string")
        if not isinstance(self.warnings, tuple):
            raise ValueError("warnings must be a tuple")
        for w in self.warnings:
            if not isinstance(w, str) or not w:
                raise ValueError("warnings entries must be non-empty strings")

    @property
    def total_attempts(self) -> int:
        return len(self.attempts)

    @property
    def duration_ms(self) -> int:
        return int(max(0.0, self.finished_at - self.started_at) * 1000)

    @property
    def succeeded(self) -> bool:
        return self.final_status is AutoFixStatus.succeeded

    @property
    def was_skipped(self) -> bool:
        return self.final_status is AutoFixStatus.skipped

    @property
    def did_invoke_agent(self) -> bool:
        return any(a.did_call_agent for a in self.attempts)

    @property
    def total_files_touched(self) -> int:
        return sum(len(a.agent_files_touched) for a in self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
            "session_id": self.session_id,
            "platform": self.platform,
            "workspace_path": self.workspace_path,
            "final_status": self.final_status.value,
            "started_at": float(self.started_at),
            "finished_at": float(self.finished_at),
            "duration_ms": self.duration_ms,
            "total_attempts": self.total_attempts,
            "attempts": [a.to_dict() for a in self.attempts],
            "initial_error_count": int(self.initial_error_count),
            "final_error_count": int(self.final_error_count),
            "final_build_status": self.final_build_status,
            "final_screenshot_status": self.final_screenshot_status,
            "final_screenshot_path": self.final_screenshot_path,
            "succeeded": self.succeeded,
            "was_skipped": self.was_skipped,
            "did_invoke_agent": self.did_invoke_agent,
            "total_files_touched": self.total_files_touched,
            "detail": self.detail,
            "warnings": list(self.warnings),
        }


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def summarise_build_errors(
    errors: Sequence[BuildError],
    *,
    platform: str = "android",
    auto_fix_hint: str = "",
) -> MobileBuildErrorSummary:
    """Convert a sequence of :class:`BuildError` into a
    :class:`MobileBuildErrorSummary` ready for V6 #5's
    ``error_source`` plug-in slot.

    Renders a deterministic markdown block:

        ### Build errors

        - **error** [gradle] app/src/Foo.kt:42:7 — Unresolved reference: Bar
        - **warning** [gradle] app/src/Foo.kt:55 — Unused parameter
        ...

    Empty input renders a "no build errors reported" block + a clean
    auto-fix hint (so the agent can use the same template both before
    and after a successful fix).  ``platform`` is purely cosmetic —
    it shows up in the heading so the agent does not confuse Gradle
    output for an iOS build.
    """

    if not isinstance(platform, str) or not platform.strip():
        raise ValueError("platform must be a non-empty string")
    plat_norm = platform.strip().lower()
    if plat_norm not in SUPPORTED_PLATFORMS:
        raise ValueError(
            f"platform must be one of {SUPPORTED_PLATFORMS!r}, got "
            f"{platform!r}"
        )
    if not isinstance(auto_fix_hint, str):
        raise ValueError("auto_fix_hint must be a string")
    err_tuple = tuple(errors)
    for err in err_tuple:
        if not isinstance(err, BuildError):
            raise TypeError("errors entries must be BuildError")

    plat_label = "iOS" if plat_norm == "ios" else "Android"
    if not err_tuple:
        return MobileBuildErrorSummary(
            summary_markdown=(
                f"### Build errors ({plat_label})\n\n"
                "No build errors reported.\n"
            ),
            auto_fix_hint=(
                auto_fix_hint
                or "Build is clean — no auto-fix needed this turn."
            ),
            has_blocking_errors=False,
            active_error_count=0,
        )

    lines = [f"### Build errors ({plat_label})\n"]
    blocking = 0
    for err in err_tuple:
        loc_parts: list[str] = []
        if err.file:
            loc_parts.append(err.file)
        if err.line is not None:
            loc_parts.append(str(err.line))
        if err.column is not None:
            loc_parts.append(str(err.column))
        location = ":".join(loc_parts) if loc_parts else "(no location)"
        lines.append(
            f"- **{err.severity}** [{err.tool}] {location} — {err.message}\n"
        )
        if err.severity == "error":
            blocking += 1
    summary_md = "".join(lines)

    if auto_fix_hint:
        hint = auto_fix_hint
    else:
        if blocking:
            hint = (
                f"Patch the {blocking} blocking build error"
                f"{'s' if blocking != 1 else ''} above before the next "
                "rebuild — do not introduce new failures."
            )
        else:
            hint = (
                f"All {len(err_tuple)} diagnostics are warnings — "
                "fix only if doing so won't introduce regressions."
            )
    return MobileBuildErrorSummary(
        summary_markdown=summary_md,
        auto_fix_hint=hint,
        has_blocking_errors=blocking > 0,
        active_error_count=len(err_tuple),
    )


def render_autofix_outcome_markdown(outcome: MobileAutoFixOutcome) -> str:
    """Operator-readable summary of the loop run.

    Stable across runs — used by SSE bus consumers and by the operator
    HUD to render a one-glance "did the auto-fix loop close the gap"
    report.
    """

    if not isinstance(outcome, MobileAutoFixOutcome):
        raise TypeError("outcome must be a MobileAutoFixOutcome")
    lines = [
        f"### Mobile auto-fix `{outcome.session_id}`",
        "",
        f"- platform: `{outcome.platform}`",
        f"- workspace: `{outcome.workspace_path}`",
        f"- final status: **{outcome.final_status.value}**",
        f"- attempts: {outcome.total_attempts}",
        f"- initial errors: {outcome.initial_error_count}",
        f"- final errors: {outcome.final_error_count}",
        f"- final build: `{outcome.final_build_status or '(none)'}`",
        f"- final screenshot: `{outcome.final_screenshot_status or '(none)'}`",
        f"- duration: {outcome.duration_ms} ms",
        f"- agent invoked: {outcome.did_invoke_agent}",
        f"- files touched: {outcome.total_files_touched}",
    ]
    if outcome.detail:
        lines.append(f"- detail: {outcome.detail}")
    if outcome.warnings:
        lines.append(f"- warnings: {', '.join(outcome.warnings)}")
    if outcome.attempts:
        lines.append("")
        lines.append("**Attempts**")
        lines.append("")
        for a in outcome.attempts:
            head = (
                f"- #{a.attempt_index} ({a.duration_ms} ms) "
                f"build=`{a.build_status or '(none)'}` "
                f"errors={a.build_error_count} "
                f"status=**{a.status.value}**"
            )
            if a.agent_action:
                head += f" agent=`{a.agent_action}`"
            if a.screenshot_status:
                head += f" screenshot=`{a.screenshot_status}`"
            lines.append(head)
            if a.detail:
                lines.append(f"  - {a.detail}")
    return "\n".join(lines) + "\n"


def format_autofix_attempt_id(session_id: str, attempt_index: int) -> str:
    """Stable id for one attempt — used for SSE / log correlation
    and for V6 #5 ``turn_id`` so the visual payload aligns with the
    autofix iteration it was built for.
    """

    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(attempt_index, int) or attempt_index < 1:
        raise ValueError("attempt_index must be a positive int")
    safe = "".join(
        c if c.isalnum() or c in "_.-" else "-" for c in session_id.strip()
    )
    return f"autofix-{safe}-{attempt_index:03d}"


# ───────────────────────────────────────────────────────────────────
#  Loop
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]
AgentFixFn = Callable[[MobileAutoFixRequest], MobileAutoFixResponse]


class MobileAutoFixLoop:
    """Per-session orchestrator for the mobile build-fix-rebuild loop.

    Wire-up::

        loop = MobileAutoFixLoop(
            sandbox_manager=manager,
            agent_fix_fn=my_agent_callback,
            visual_context_builder=visual_builder,  # optional
            event_cb=sse_bus.emit,                  # optional
        )
        outcome = loop.run(
            session_id="sess-1",
            config=mobile_sandbox_config,
            output_dir="/var/run/omnisight/captures",
            max_attempts=4,
        )
        if not outcome.succeeded:
            log.warning(render_autofix_outcome_markdown(outcome))

    Thread-safe — counters + last-outcome access is guarded by an
    ``RLock``.  Concurrent ``run()`` calls for the *same* session_id
    are not supported (the manager is the source of truth and rejects
    duplicate sessions); concurrent calls for *different* sessions
    are safe.
    """

    def __init__(
        self,
        *,
        sandbox_manager: MobileSandboxManager,
        agent_fix_fn: AgentFixFn,
        visual_context_builder: MobileAgentVisualContextBuilder | None = None,
        clock: Callable[[], float] = time.time,
        event_cb: EventCallback | None = None,
        default_max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        default_failure_mode: str = DEFAULT_FAILURE_MODE,
        capture_after_success: bool = True,
        attempt_devices: Sequence[MobileDeviceTarget] | None = None,
    ) -> None:
        if not isinstance(sandbox_manager, MobileSandboxManager):
            raise MobileAutoFixConfigError(
                "sandbox_manager must be a MobileSandboxManager"
            )
        if not callable(agent_fix_fn):
            raise MobileAutoFixConfigError("agent_fix_fn must be callable")
        if visual_context_builder is not None and not isinstance(
            visual_context_builder, MobileAgentVisualContextBuilder
        ):
            raise MobileAutoFixConfigError(
                "visual_context_builder must be a "
                "MobileAgentVisualContextBuilder or None"
            )
        if not callable(clock):
            raise MobileAutoFixConfigError("clock must be callable")
        if event_cb is not None and not callable(event_cb):
            raise MobileAutoFixConfigError("event_cb must be callable or None")
        if (
            not isinstance(default_max_attempts, int)
            or default_max_attempts < 1
        ):
            raise MobileAutoFixConfigError(
                "default_max_attempts must be a positive int"
            )
        if default_failure_mode not in FAILURE_MODES:
            raise MobileAutoFixConfigError(
                f"default_failure_mode must be one of {FAILURE_MODES!r}, "
                f"got {default_failure_mode!r}"
            )
        if not isinstance(capture_after_success, bool):
            raise MobileAutoFixConfigError(
                "capture_after_success must be bool"
            )
        if attempt_devices is not None:
            attempt_devices_t = tuple(attempt_devices)
            if not attempt_devices_t:
                raise MobileAutoFixConfigError(
                    "attempt_devices must not be empty"
                )
            for d in attempt_devices_t:
                if not isinstance(d, MobileDeviceTarget):
                    raise MobileAutoFixConfigError(
                        "attempt_devices entries must be MobileDeviceTarget"
                    )
        else:
            attempt_devices_t = None

        self._manager = sandbox_manager
        self._agent_fix_fn = agent_fix_fn
        self._visual_builder = visual_context_builder
        self._clock = clock
        self._event_cb = event_cb
        self._default_max_attempts = int(default_max_attempts)
        self._default_failure_mode = default_failure_mode
        self._capture_after_success = bool(capture_after_success)
        self._attempt_devices = attempt_devices_t

        self._lock = threading.RLock()
        self._run_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._exhausted_count = 0
        self._skipped_count = 0
        self._agent_invocations = 0
        self._last_outcome: MobileAutoFixOutcome | None = None
        self._error_summary_cache: dict[str, MobileBuildErrorSummary] = {}

    # ─────────────── Accessors ───────────────

    @property
    def sandbox_manager(self) -> MobileSandboxManager:
        return self._manager

    @property
    def agent_fix_fn(self) -> AgentFixFn:
        return self._agent_fix_fn

    @property
    def visual_context_builder(self) -> MobileAgentVisualContextBuilder | None:
        return self._visual_builder

    @property
    def default_max_attempts(self) -> int:
        return self._default_max_attempts

    @property
    def default_failure_mode(self) -> str:
        return self._default_failure_mode

    @property
    def capture_after_success(self) -> bool:
        return self._capture_after_success

    @property
    def attempt_devices(self) -> tuple[MobileDeviceTarget, ...] | None:
        return self._attempt_devices

    def run_count(self) -> int:
        with self._lock:
            return self._run_count

    def success_count(self) -> int:
        with self._lock:
            return self._success_count

    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def exhausted_count(self) -> int:
        with self._lock:
            return self._exhausted_count

    def skipped_count(self) -> int:
        with self._lock:
            return self._skipped_count

    def agent_invocations(self) -> int:
        with self._lock:
            return self._agent_invocations

    def last_outcome(self) -> MobileAutoFixOutcome | None:
        with self._lock:
            return self._last_outcome

    # ─────────────── Core API ───────────────

    def run(
        self,
        *,
        session_id: str,
        config: MobileSandboxConfig,
        output_dir: str,
        max_attempts: int | None = None,
        failure_mode: str | None = None,
        attempt_devices: Sequence[MobileDeviceTarget] | None = None,
    ) -> MobileAutoFixOutcome:
        """Execute the build-fix-rebuild loop for one session.

        Returns a terminal :class:`MobileAutoFixOutcome` recording
        every attempt.  ``failure_mode="continue"`` (default) never
        raises; ``failure_mode="abort"`` re-raises agent callback
        exceptions and unrecoverable sandbox errors.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise MobileAutoFixConfigError(
                "session_id must be a non-empty string"
            )
        if not isinstance(config, MobileSandboxConfig):
            raise MobileAutoFixConfigError(
                "config must be a MobileSandboxConfig"
            )
        if config.session_id != session_id:
            raise MobileAutoFixConfigError(
                "config.session_id must match session_id — got "
                f"{config.session_id!r} vs {session_id!r}"
            )
        if not isinstance(output_dir, str) or not output_dir.strip():
            raise MobileAutoFixConfigError(
                "output_dir must be a non-empty string"
            )
        effective_max = (
            int(max_attempts)
            if max_attempts is not None
            else self._default_max_attempts
        )
        if effective_max < 1:
            raise MobileAutoFixConfigError(
                "max_attempts must be a positive int"
            )
        effective_mode = (
            failure_mode if failure_mode is not None else self._default_failure_mode
        )
        if effective_mode not in FAILURE_MODES:
            raise MobileAutoFixConfigError(
                f"failure_mode must be one of {FAILURE_MODES!r}, got "
                f"{effective_mode!r}"
            )
        effective_devices: tuple[MobileDeviceTarget, ...] | None
        if attempt_devices is not None:
            effective_devices = tuple(attempt_devices)
            for d in effective_devices:
                if not isinstance(d, MobileDeviceTarget):
                    raise MobileAutoFixConfigError(
                        "attempt_devices entries must be MobileDeviceTarget"
                    )
            if not effective_devices:
                raise MobileAutoFixConfigError(
                    "attempt_devices must not be empty"
                )
        else:
            effective_devices = self._attempt_devices

        started_at = float(self._clock())
        warnings: list[str] = []
        attempts: list[MobileAutoFixAttempt] = []
        initial_error_count = 0
        final_status = AutoFixStatus.pending
        final_build_status = ""
        final_screenshot_status = ""
        final_screenshot_path = ""
        final_error_count = 0
        detail = ""

        self._emit(
            AUTOFIX_EVENT_STARTED,
            {
                "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
                "session_id": session_id,
                "platform": config.platform,
                "workspace_path": config.workspace_path,
                "max_attempts": effective_max,
                "failure_mode": effective_mode,
                "started_at": started_at,
            },
        )

        for attempt_index in range(1, effective_max + 1):
            attempt_started = float(self._clock())
            self._emit(
                AUTOFIX_EVENT_ATTEMPT_STARTED,
                {
                    "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
                    "session_id": session_id,
                    "attempt_index": attempt_index,
                    "max_attempts": effective_max,
                    "started_at": attempt_started,
                },
            )

            try:
                instance = self._reset_for_rebuild(session_id, config)
            except MobileAutoFixSandboxError as exc:
                attempt = MobileAutoFixAttempt(
                    attempt_index=attempt_index,
                    started_at=attempt_started,
                    finished_at=float(self._clock()),
                    status=AutoFixAttemptStatus.sandbox_error,
                    detail=f"sandbox reset failed: {exc}",
                    warnings=("sandbox_reset_failed",),
                )
                attempts.append(attempt)
                self._emit_attempt_finished(attempt, session_id)
                if effective_mode == "abort":
                    self._finalise_outcome(
                        session_id=session_id,
                        config=config,
                        started_at=started_at,
                        attempts=attempts,
                        final_status=AutoFixStatus.failed,
                        warnings=warnings,
                        detail=str(exc),
                        initial_error_count=initial_error_count,
                        final_build_status="",
                        final_screenshot_status="",
                        final_screenshot_path="",
                        final_error_count=0,
                        emit_event=AUTOFIX_EVENT_FAILED,
                    )
                    raise
                final_status = AutoFixStatus.failed
                detail = str(exc)
                break

            # ---- Build phase ----
            try:
                instance = self._manager.build(session_id)
            except MobileSandboxError as exc:
                attempt = MobileAutoFixAttempt(
                    attempt_index=attempt_index,
                    started_at=attempt_started,
                    finished_at=float(self._clock()),
                    status=AutoFixAttemptStatus.sandbox_error,
                    detail=f"manager.build raised: {exc}",
                    warnings=("manager_build_raised",),
                )
                attempts.append(attempt)
                self._emit_attempt_finished(attempt, session_id)
                if effective_mode == "abort":
                    self._finalise_outcome(
                        session_id=session_id,
                        config=config,
                        started_at=started_at,
                        attempts=attempts,
                        final_status=AutoFixStatus.failed,
                        warnings=warnings,
                        detail=str(exc),
                        initial_error_count=initial_error_count,
                        final_build_status="",
                        final_screenshot_status="",
                        final_screenshot_path="",
                        final_error_count=0,
                        emit_event=AUTOFIX_EVENT_FAILED,
                    )
                    raise
                final_status = AutoFixStatus.failed
                detail = str(exc)
                break

            build_report = instance.build
            error_count = len(build_report.errors)
            if attempt_index == 1:
                initial_error_count = error_count
            final_build_status = build_report.status
            final_error_count = error_count

            # ---- Mock short-circuit ----
            if build_report.status == "mock":
                attempt = MobileAutoFixAttempt(
                    attempt_index=attempt_index,
                    started_at=attempt_started,
                    finished_at=float(self._clock()),
                    status=AutoFixAttemptStatus.skipped,
                    build_status=build_report.status,
                    build_error_count=error_count,
                    build_errors=build_report.errors,
                    detail=(
                        build_report.detail
                        or "build returned mock — toolchain not available"
                    ),
                    warnings=("toolchain_missing",),
                )
                attempts.append(attempt)
                self._emit_attempt_finished(attempt, session_id)
                final_status = AutoFixStatus.skipped
                detail = "build toolchain unavailable — auto-fix skipped"
                break

            # ---- Build pass ----
            if build_report.status == "pass":
                install_status = ""
                screenshot_status = ""
                screenshot_path = ""
                if self._capture_after_success:
                    try:
                        instance = self._manager.install(session_id)
                        install_status = instance.install.status
                    except MobileSandboxError as exc:
                        warnings.append(f"install_failed:{exc}")
                        install_status = "fail"
                    if install_status in ("pass", "mock"):
                        try:
                            instance = self._manager.screenshot(session_id)
                            screenshot_status = instance.screenshot.status
                            screenshot_path = instance.screenshot.path
                        except MobileSandboxError as exc:
                            warnings.append(f"screenshot_failed:{exc}")
                            screenshot_status = "fail"
                final_screenshot_status = screenshot_status
                final_screenshot_path = screenshot_path
                attempt = MobileAutoFixAttempt(
                    attempt_index=attempt_index,
                    started_at=attempt_started,
                    finished_at=float(self._clock()),
                    status=AutoFixAttemptStatus.build_passed,
                    build_status=build_report.status,
                    build_error_count=error_count,
                    build_errors=build_report.errors,
                    install_status=install_status,
                    screenshot_status=screenshot_status,
                    screenshot_path=screenshot_path,
                    detail=build_report.detail or "build passed",
                )
                attempts.append(attempt)
                self._emit(
                    AUTOFIX_EVENT_BUILD_PASSED,
                    {
                        "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
                        "session_id": session_id,
                        "attempt_index": attempt_index,
                        "build_status": build_report.status,
                        "screenshot_status": screenshot_status,
                        "screenshot_path": screenshot_path,
                    },
                )
                self._emit_attempt_finished(attempt, session_id)
                final_status = AutoFixStatus.succeeded
                detail = "build clean"
                break

            # ---- Build failed → invoke agent ----
            self._emit(
                AUTOFIX_EVENT_BUILD_FAILED,
                {
                    "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
                    "session_id": session_id,
                    "attempt_index": attempt_index,
                    "build_status": build_report.status,
                    "error_count": error_count,
                    "exit_code": int(build_report.exit_code),
                },
            )

            error_summary = summarise_build_errors(
                build_report.errors, platform=config.platform,
            )

            visual_payload, visual_warnings = self._build_visual_payload(
                session_id=session_id,
                attempt_index=attempt_index,
                output_dir=output_dir,
                error_summary=error_summary,
                devices=effective_devices,
            )
            for w in visual_warnings:
                warnings.append(w)

            request = MobileAutoFixRequest(
                session_id=session_id,
                attempt_index=attempt_index,
                workspace_path=config.workspace_path,
                platform=config.platform,
                build_errors=build_report.errors,
                error_summary=error_summary,
                visual_payload=visual_payload,
                sandbox_snapshot=instance.to_dict(),
                previous_attempts=tuple(attempts),
                requested_at=float(self._clock()),
            )

            try:
                response = self._agent_fix_fn(request)
                with self._lock:
                    self._agent_invocations += 1
            except Exception as exc:
                attempt = MobileAutoFixAttempt(
                    attempt_index=attempt_index,
                    started_at=attempt_started,
                    finished_at=float(self._clock()),
                    status=AutoFixAttemptStatus.agent_error,
                    build_status=build_report.status,
                    build_error_count=error_count,
                    build_errors=build_report.errors,
                    visual_payload_built=visual_payload is not None,
                    visual_image_count=(
                        visual_payload.image_count if visual_payload else 0
                    ),
                    detail=f"agent_fix_fn raised {type(exc).__name__}: {exc}",
                    warnings=("agent_callback_raised",),
                )
                attempts.append(attempt)
                self._emit_attempt_finished(attempt, session_id)
                if effective_mode == "abort":
                    self._finalise_outcome(
                        session_id=session_id,
                        config=config,
                        started_at=started_at,
                        attempts=attempts,
                        final_status=AutoFixStatus.failed,
                        warnings=warnings,
                        detail=f"agent raised {type(exc).__name__}: {exc}",
                        initial_error_count=initial_error_count,
                        final_build_status=final_build_status,
                        final_screenshot_status=final_screenshot_status,
                        final_screenshot_path=final_screenshot_path,
                        final_error_count=final_error_count,
                        emit_event=AUTOFIX_EVENT_FAILED,
                    )
                    raise
                final_status = AutoFixStatus.failed
                detail = f"agent_fix_fn raised {type(exc).__name__}: {exc}"
                break

            if not isinstance(response, MobileAutoFixResponse):
                attempt = MobileAutoFixAttempt(
                    attempt_index=attempt_index,
                    started_at=attempt_started,
                    finished_at=float(self._clock()),
                    status=AutoFixAttemptStatus.agent_error,
                    build_status=build_report.status,
                    build_error_count=error_count,
                    build_errors=build_report.errors,
                    visual_payload_built=visual_payload is not None,
                    visual_image_count=(
                        visual_payload.image_count if visual_payload else 0
                    ),
                    detail=(
                        "agent_fix_fn returned non-MobileAutoFixResponse: "
                        f"{type(response).__name__}"
                    ),
                    warnings=("agent_callback_bad_return",),
                )
                attempts.append(attempt)
                self._emit_attempt_finished(attempt, session_id)
                if effective_mode == "abort":
                    self._finalise_outcome(
                        session_id=session_id,
                        config=config,
                        started_at=started_at,
                        attempts=attempts,
                        final_status=AutoFixStatus.failed,
                        warnings=warnings,
                        detail=attempt.detail,
                        initial_error_count=initial_error_count,
                        final_build_status=final_build_status,
                        final_screenshot_status=final_screenshot_status,
                        final_screenshot_path=final_screenshot_path,
                        final_error_count=final_error_count,
                        emit_event=AUTOFIX_EVENT_FAILED,
                    )
                    raise MobileAutoFixError(attempt.detail)
                final_status = AutoFixStatus.failed
                detail = attempt.detail
                break

            if response.is_patch:
                attempt = MobileAutoFixAttempt(
                    attempt_index=attempt_index,
                    started_at=attempt_started,
                    finished_at=float(self._clock()),
                    status=AutoFixAttemptStatus.agent_patched,
                    build_status=build_report.status,
                    build_error_count=error_count,
                    build_errors=build_report.errors,
                    agent_action=response.action,
                    agent_summary=response.summary,
                    agent_files_touched=response.files_touched,
                    visual_payload_built=visual_payload is not None,
                    visual_image_count=(
                        visual_payload.image_count if visual_payload else 0
                    ),
                    detail=response.summary or "agent applied a patch",
                )
                attempts.append(attempt)
                self._emit(
                    AUTOFIX_EVENT_FIX_APPLIED,
                    {
                        "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
                        "session_id": session_id,
                        "attempt_index": attempt_index,
                        "files_touched": list(response.files_touched),
                        "summary": response.summary,
                    },
                )
                self._emit_attempt_finished(attempt, session_id)
                continue

            if response.is_no_op:
                attempt = MobileAutoFixAttempt(
                    attempt_index=attempt_index,
                    started_at=attempt_started,
                    finished_at=float(self._clock()),
                    status=AutoFixAttemptStatus.agent_no_op,
                    build_status=build_report.status,
                    build_error_count=error_count,
                    build_errors=build_report.errors,
                    agent_action=response.action,
                    agent_summary=response.summary,
                    agent_files_touched=response.files_touched,
                    visual_payload_built=visual_payload is not None,
                    visual_image_count=(
                        visual_payload.image_count if visual_payload else 0
                    ),
                    detail=response.summary or "agent declined to patch",
                )
                attempts.append(attempt)
                self._emit_attempt_finished(attempt, session_id)
                final_status = AutoFixStatus.failed
                detail = "agent returned no_op"
                break

            # is_give_up
            attempt = MobileAutoFixAttempt(
                attempt_index=attempt_index,
                started_at=attempt_started,
                finished_at=float(self._clock()),
                status=AutoFixAttemptStatus.agent_give_up,
                build_status=build_report.status,
                build_error_count=error_count,
                build_errors=build_report.errors,
                agent_action=response.action,
                agent_summary=response.summary,
                agent_files_touched=response.files_touched,
                visual_payload_built=visual_payload is not None,
                visual_image_count=(
                    visual_payload.image_count if visual_payload else 0
                ),
                detail=response.summary or "agent gave up",
            )
            attempts.append(attempt)
            self._emit_attempt_finished(attempt, session_id)
            final_status = AutoFixStatus.failed
            detail = "agent gave up"
            break
        else:
            final_status = AutoFixStatus.exhausted
            detail = (
                f"build still failing after {effective_max} attempts"
            )

        return self._finalise_outcome(
            session_id=session_id,
            config=config,
            started_at=started_at,
            attempts=attempts,
            final_status=final_status,
            warnings=warnings,
            detail=detail,
            initial_error_count=initial_error_count,
            final_build_status=final_build_status,
            final_screenshot_status=final_screenshot_status,
            final_screenshot_path=final_screenshot_path,
            final_error_count=final_error_count,
            emit_event=self._terminal_event(final_status),
        )

    # ─────────────── Internal plumbing ───────────────

    def _reset_for_rebuild(
        self, session_id: str, config: MobileSandboxConfig,
    ) -> MobileSandboxInstance:
        """Bring the sandbox to a fresh ``pending`` state.

        Uses only the public manager API.  ``stop()`` is a no-op on
        terminal states (failed / stopped); ``remove()`` purges any
        existing record; ``create()`` re-registers from the same
        config.  When no prior instance exists this collapses to a
        single ``create()``.
        """

        try:
            existing = self._manager.get(session_id)
        except Exception as exc:
            raise MobileAutoFixSandboxError(
                f"manager.get raised: {exc}"
            ) from exc
        if existing is not None:
            if not existing.is_terminal:
                try:
                    self._manager.stop(session_id)
                except Exception as exc:
                    raise MobileAutoFixSandboxError(
                        f"manager.stop raised: {exc}"
                    ) from exc
            try:
                self._manager.remove(session_id)
            except MobileSandboxError as exc:
                raise MobileAutoFixSandboxError(
                    f"manager.remove rejected: {exc}"
                ) from exc
            except Exception as exc:
                raise MobileAutoFixSandboxError(
                    f"manager.remove raised: {exc}"
                ) from exc
        try:
            return self._manager.create(config)
        except MobileSandboxAlreadyExists as exc:
            raise MobileAutoFixSandboxError(
                f"sandbox already exists for session_id={session_id!r}: {exc}"
            ) from exc
        except MobileSandboxError as exc:
            raise MobileAutoFixSandboxError(
                f"manager.create rejected: {exc}"
            ) from exc
        except Exception as exc:
            raise MobileAutoFixSandboxError(
                f"manager.create raised: {exc}"
            ) from exc

    def _build_visual_payload(
        self,
        *,
        session_id: str,
        attempt_index: int,
        output_dir: str,
        error_summary: MobileBuildErrorSummary,
        devices: Sequence[MobileDeviceTarget] | None,
    ) -> tuple[MobileAgentVisualContextPayload | None, list[str]]:
        """Assemble the V6 #5 multimodal payload for one attempt.

        When the loop is constructed without a visual builder we
        return ``(None, [])`` — agents that don't consume pixels
        skip the encode + base64 cost.

        Caller-provided builder is treated as a black box: we publish
        the freshly-built :class:`MobileBuildErrorSummary` via
        :meth:`current_error_summary` so callers can wire the builder's
        ``error_source`` to read from the loop, and we then call
        :meth:`MobileAgentVisualContextBuilder.build` with the
        per-attempt ``turn_id`` so the visual payload aligns with the
        autofix iteration it belongs to.  The fresh summary stays
        published for the lifetime of the attempt and is cleared
        afterwards so concurrent sessions never see stale data.
        """

        if self._visual_builder is None:
            return None, []
        builder = self._visual_builder
        warnings: list[str] = []
        with self._lock:
            self._error_summary_cache[session_id] = error_summary
        try:
            kwargs: dict[str, Any] = {
                "session_id": session_id,
                "output_dir": output_dir,
                "turn_id": format_autofix_attempt_id(session_id, attempt_index),
                "attach_bytes": True,
                "include_errors": True,
                "failure_mode": "collect",
            }
            if devices is not None:
                kwargs["devices"] = devices
            try:
                payload = builder.build(**kwargs)
            except Exception as exc:  # noqa: BLE001 - defensive, never propagate
                warnings.append(
                    f"visual_payload_failed:{type(exc).__name__}:{exc}"
                )
                return None, warnings
            return payload, warnings
        finally:
            with self._lock:
                self._error_summary_cache.pop(session_id, None)

    def current_error_summary(
        self, session_id: str,
    ) -> MobileBuildErrorSummary | None:
        """Return the in-flight :class:`MobileBuildErrorSummary` for
        ``session_id`` while a :meth:`run` is mid-attempt.

        Designed to be wired directly into the visual context builder's
        ``error_source`` so the multimodal text block renders the same
        Gradle / Xcode diagnostics the agent receives via
        :class:`MobileAutoFixRequest.error_summary`::

            builder = MobileAgentVisualContextBuilder(
                error_source=lambda sid: loop.current_error_summary(sid),
            )
            loop = MobileAutoFixLoop(
                visual_context_builder=builder,
                ...,
            )

        Returns ``None`` when no attempt is in progress for the
        session — V6 #5's contract treats that as "no errors tracked"
        and renders the clean-build hint.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            return None
        with self._lock:
            return self._error_summary_cache.get(session_id)

    def _finalise_outcome(
        self,
        *,
        session_id: str,
        config: MobileSandboxConfig,
        started_at: float,
        attempts: list[MobileAutoFixAttempt],
        final_status: AutoFixStatus,
        warnings: list[str],
        detail: str,
        initial_error_count: int,
        final_build_status: str,
        final_screenshot_status: str,
        final_screenshot_path: str,
        final_error_count: int,
        emit_event: str,
    ) -> MobileAutoFixOutcome:
        finished_at = float(self._clock())
        outcome = MobileAutoFixOutcome(
            session_id=session_id,
            platform=config.platform,
            workspace_path=config.workspace_path,
            final_status=final_status,
            started_at=started_at,
            finished_at=finished_at,
            attempts=tuple(attempts),
            initial_error_count=initial_error_count,
            final_error_count=final_error_count,
            final_build_status=final_build_status,
            final_screenshot_status=final_screenshot_status,
            final_screenshot_path=final_screenshot_path,
            detail=detail,
            warnings=tuple(warnings),
        )
        with self._lock:
            self._run_count += 1
            self._last_outcome = outcome
            if final_status is AutoFixStatus.succeeded:
                self._success_count += 1
            elif final_status is AutoFixStatus.failed:
                self._failure_count += 1
            elif final_status is AutoFixStatus.exhausted:
                self._exhausted_count += 1
            elif final_status is AutoFixStatus.skipped:
                self._skipped_count += 1
        self._emit(emit_event, self._envelope_for_outcome(outcome))
        return outcome

    def _terminal_event(self, status: AutoFixStatus) -> str:
        if status is AutoFixStatus.succeeded:
            return AUTOFIX_EVENT_SUCCEEDED
        if status is AutoFixStatus.failed:
            return AUTOFIX_EVENT_FAILED
        if status is AutoFixStatus.exhausted:
            return AUTOFIX_EVENT_EXHAUSTED
        if status is AutoFixStatus.skipped:
            return AUTOFIX_EVENT_SKIPPED
        return AUTOFIX_EVENT_FAILED

    def _envelope_for_outcome(
        self, outcome: MobileAutoFixOutcome,
    ) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
            "session_id": outcome.session_id,
            "platform": outcome.platform,
            "final_status": outcome.final_status.value,
            "total_attempts": outcome.total_attempts,
            "initial_error_count": outcome.initial_error_count,
            "final_error_count": outcome.final_error_count,
            "final_build_status": outcome.final_build_status,
            "final_screenshot_status": outcome.final_screenshot_status,
            "duration_ms": outcome.duration_ms,
            "did_invoke_agent": outcome.did_invoke_agent,
            "total_files_touched": outcome.total_files_touched,
            "warning_count": len(outcome.warnings),
        }

    def _emit_attempt_finished(
        self, attempt: MobileAutoFixAttempt, session_id: str,
    ) -> None:
        self._emit(
            AUTOFIX_EVENT_ATTEMPT_FINISHED,
            {
                "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
                "session_id": session_id,
                "attempt_index": attempt.attempt_index,
                "status": attempt.status.value,
                "build_status": attempt.build_status,
                "build_error_count": attempt.build_error_count,
                "agent_action": attempt.agent_action,
                "duration_ms": attempt.duration_ms,
            },
        )

    def _emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, dict(data))
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning(
                "mobile_build_error_autofix event callback raised: %s", exc
            )

    # ─────────────── Snapshot ───────────────

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe operator snapshot — counters + last outcome
        metadata.  The last outcome's full attempt list is *not*
        inlined (use :meth:`last_outcome` for the full record).
        """

        with self._lock:
            last = self._last_outcome
            last_summary: dict[str, Any] | None = None
            if last is not None:
                last_summary = {
                    "session_id": last.session_id,
                    "platform": last.platform,
                    "final_status": last.final_status.value,
                    "total_attempts": last.total_attempts,
                    "initial_error_count": last.initial_error_count,
                    "final_error_count": last.final_error_count,
                    "duration_ms": last.duration_ms,
                    "succeeded": last.succeeded,
                    "did_invoke_agent": last.did_invoke_agent,
                    "warning_count": len(last.warnings),
                }
            return {
                "schema_version": MOBILE_BUILD_ERROR_AUTOFIX_SCHEMA_VERSION,
                "sandbox_schema_version": MOBILE_SANDBOX_SCHEMA_VERSION,
                "visual_context_schema_version": MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
                "default_max_attempts": int(self._default_max_attempts),
                "default_failure_mode": self._default_failure_mode,
                "capture_after_success": bool(self._capture_after_success),
                "visual_builder_wired": self._visual_builder is not None,
                "run_count": int(self._run_count),
                "success_count": int(self._success_count),
                "failure_count": int(self._failure_count),
                "exhausted_count": int(self._exhausted_count),
                "skipped_count": int(self._skipped_count),
                "agent_invocations": int(self._agent_invocations),
                "last_outcome": last_summary,
                "now": float(self._clock()),
            }
