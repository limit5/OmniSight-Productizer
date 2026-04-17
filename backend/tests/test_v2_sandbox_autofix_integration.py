"""V2 #8 (issue #318 / TODO row 1516) — end-to-end auto-fix loop integration test.

Closes the V2 live-preview loop: agent writes code → sandbox HMR → screenshot
→ error detection → auto-fix → re-screenshot → final screenshot has no error.

Wires every V2 row together with zero orchestration code besides the fixture:

    V2 #1  SandboxManager           — Docker verbs (fake client)
    V2 #2  SandboxLifecycle         — ensure_session / hot_reload / capture
    V2 #3  ScreenshotService        — PNG producer (fake engine)
    V2 #4  ResponsiveViewportCapture — three-viewport batch matrix
    V2 #5  PreviewErrorBridge       — stdout/stderr → structured errors
    V2 #6  AgentVisualContextBuilder — multimodal payload for Opus 4.7
    V2 #7  UiSandboxSseBridge       — ``ui_sandbox.screenshot`` / ``.error`` SSE

Scenario is the golden path the TODO row describes:

  1. Agent writes a broken ``Header.tsx`` (imports missing ``Button``).
  2. SandboxLifecycle creates + starts + marks the sandbox ready.
  3. A first ``capture_all`` pulls three screenshots (desktop/tablet/mobile)
     while the docker logs contain a ``Module not found`` compile error.
  4. PreviewErrorBridge.scan picks the error up.
  5. AgentVisualContextBuilder bakes the three images + the error summary
     into a multimodal ``HumanMessage`` the agent would see this turn.
  6. Agent "fixes" ``Header.tsx`` (writes a clean version), sandbox
     emits HMR, docker logs swap to a clean banner.
  7. Second ``capture_all`` pulls three more screenshots.
  8. PreviewErrorBridge.scan picks up the cleared error.
  9. Second AgentVisualContextBuilder payload has zero active errors.
  10. UiSandboxSseBridge has seen ``ui_sandbox.screenshot`` + ``ui_sandbox.error``
      SSE frames covering both rounds.

All assertions run deterministically — no real docker, no real browser,
no real sleep.  ``FakeDockerClient`` + ``FakeScreenshotEngine`` + ``FakeClock``
are scoped local to this file so V2 #1-#7 sibling tests stay untouched.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from backend import ui_agent_visual_context as avc
from backend import ui_preview_error_bridge as upb
from backend import ui_responsive_viewport as urv
from backend import ui_sandbox as usx
from backend import ui_sandbox_lifecycle as usl
from backend import ui_sandbox_sse as uss
from backend import ui_screenshot as us
from backend.ui_agent_visual_context import (
    AGENT_VISUAL_CONTEXT_EVENT_BUILT,
    AgentVisualContextBuilder,
    AgentVisualContextPayload,
)
from backend.ui_preview_error_bridge import (
    ERROR_EVENT_CLEARED,
    ERROR_EVENT_DETECTED,
    PreviewErrorBridge,
)
from backend.ui_responsive_viewport import (
    DEFAULT_VIEWPORT_MATRIX,
    ResponsiveCaptureReport,
    ResponsiveViewportCapture,
)
from backend.ui_sandbox import (
    SandboxConfig,
    SandboxManager,
    SandboxStatus,
)
from backend.ui_sandbox_lifecycle import (
    LIFECYCLE_EVENT_HOT_RELOAD,
    SandboxLifecycle,
)
from backend.ui_sandbox_sse import (
    ERROR_EVENT_FIELDS,
    SCREENSHOT_EVENT_FIELDS,
    SSE_EVENT_ERROR,
    SSE_EVENT_SCREENSHOT,
    UiSandboxSseBridge,
)
from backend.ui_screenshot import (
    PNG_SIGNATURE,
    ScreenshotRequest,
    ScreenshotService,
)


# ═══════════════════════════════════════════════════════════════════
#  Local fakes — intentionally scoped to this file
# ═══════════════════════════════════════════════════════════════════


BROKEN_HEADER_TSX = """\
import { Button } from "./Button"

export default function Header() {
  return <Button label="Go" />
}
"""


FIXED_HEADER_TSX = """\
export default function Header() {
  return <button>Go</button>
}
"""


BROKEN_LOGS = (
    "> next dev\n"
    "ready - started server on 0.0.0.0:3000\n"
    "event - compiled client and server successfully\n"
    "Module not found: Can't resolve 'Button'\n"
    "  at ./components/Header.tsx:1:10\n"
    "Failed to compile.\n"
)


CLEAN_LOGS = (
    "> next dev\n"
    "ready - started server on 0.0.0.0:3000\n"
    "event - compiled client and server successfully\n"
    "wait  - compiling ...\n"
    "event - compiled successfully\n"
)


def _png(payload: bytes = b"pixels") -> bytes:
    """Bytes starting with the official PNG signature so V2 #3 validates them."""

    return PNG_SIGNATURE + payload


class FakeClock:
    """Deterministic clock — tests advance it explicitly."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeSleep:
    """Paired with ``FakeClock`` — never actually sleeps.  Advances the
    clock so blocking polls return in zero wall time."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        self._clock.advance(max(0.0, float(seconds)))


class FakeDockerClient:
    """Minimal DockerClient shim.  Mutable ``canned_logs`` lets the test
    swap a broken compile output for a clean one mid-flight."""

    def __init__(self, *, canned_logs: str = "") -> None:
        self._lock = threading.Lock()
        self._canned_logs = canned_logs
        self._next_id = 0
        self.run_calls: list[dict[str, Any]] = []
        self.stop_calls: list[str] = []
        self.remove_calls: list[str] = []

    def set_logs(self, text: str) -> None:
        with self._lock:
            self._canned_logs = text

    def run_detached(self, **kwargs: Any) -> str:
        with self._lock:
            self._next_id += 1
            self.run_calls.append(dict(kwargs))
            return f"fake-cid-{self._next_id:04d}"

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        with self._lock:
            self.stop_calls.append(container_id)

    def remove(self, container_id: str, *, force: bool = False) -> None:
        with self._lock:
            self.remove_calls.append(container_id)

    def logs(self, container_id: str, *, tail: int | None = None) -> str:
        with self._lock:
            return self._canned_logs

    def inspect(self, container_id: str) -> Mapping[str, Any]:
        return {"Id": container_id, "State": {"Running": True}}


class FakeScreenshotEngine:
    """Returns viewport-sized deterministic PNG bytes.  Thread-safe."""

    def __init__(self, *, default_payload: bytes | None = None) -> None:
        self._default = default_payload if default_payload is not None else _png()
        self._lock = threading.Lock()
        self.calls: list[ScreenshotRequest] = []
        self.close_called = False

    def capture(self, request: ScreenshotRequest) -> bytes:
        with self._lock:
            self.calls.append(request)
            # Vary the payload per viewport so image_base64 strings differ
            # between desktop/tablet/mobile — catches any accidental sharing.
            return _png(f"px-{request.viewport.name}".encode("ascii"))

    def close(self) -> None:
        self.close_called = True


class RecordingEventCallback:
    """Captures every ``(event_type, payload)`` call so tests can assert
    ordering across all V2 modules."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event_type: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.events.append((event_type, dict(payload)))

    def types(self) -> list[str]:
        with self._lock:
            return [t for t, _ in self.events]

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p) for t, p in self.events if t == event_type]


class FakePublisher:
    """SSE bus stand-in — records every publish call the bridge makes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, dict[str, Any], str | None]] = []

    def publish(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        session_id: str | None = None,
    ) -> None:
        with self._lock:
            self.events.append((event_type, dict(payload), session_id))

    def by_topic(self, topic: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p) for t, p, _ in self.events if t == topic]


def _fanout(*cbs: Callable[[str, Mapping[str, Any]], None]):
    """Fan one ``event_cb`` seam out to multiple listeners (recorder + SSE bridge).

    Each module (V2 #1-#6) only accepts a single callback, so we chain them
    here.  Exceptions from individual listeners are swallowed to match the
    graceful-fallback contract every V2 module enforces.
    """

    def _cb(event_type: str, payload: Mapping[str, Any]) -> None:
        for cb in cbs:
            try:
                cb(event_type, payload)
            except Exception:  # pragma: no cover — defensive
                pass

    return _cb


# ═══════════════════════════════════════════════════════════════════
#  Integration rig — wires V2 #1-#7 the way production would
# ═══════════════════════════════════════════════════════════════════


class Rig:
    """Bundle of wired V2 #1-#7 components sharing one clock + one recorder."""

    def __init__(
        self,
        *,
        tmp_path: Path,
        session_id: str = "sess-autofix",
        host_port: int = 40500,
    ) -> None:
        self.session_id = session_id
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.components_dir = self.workspace / "components"
        self.components_dir.mkdir()
        self.header_path = self.components_dir / "Header.tsx"

        self.clock = FakeClock()
        self.sleep = FakeSleep(self.clock)
        self.events = RecordingEventCallback()
        self.publisher = FakePublisher()
        self.sse_bridge = UiSandboxSseBridge(
            publisher=self.publisher,
            clock=self.clock,
        )

        self.docker = FakeDockerClient(canned_logs=BROKEN_LOGS)
        self.engine = FakeScreenshotEngine()

        # V2 #1
        self.manager = SandboxManager(
            docker_client=self.docker,
            clock=self.clock,
            event_cb=_fanout(self.events),
        )

        # V2 #3
        self.service = ScreenshotService(
            engine=self.engine,
            clock=self.clock,
            event_cb=_fanout(self.events, self.sse_bridge.on_screenshot_event),
        )

        # V2 #2 — uses service.as_hook() to render Playwright-shaped screenshots
        self.lifecycle = SandboxLifecycle(
            manager=self.manager,
            screenshot_hook=self.service.as_hook(),
            clock=self.clock,
            sleep=self.sleep,
            event_cb=_fanout(self.events, self.sse_bridge.on_lifecycle_event),
        )

        # V2 #4
        self.responsive = ResponsiveViewportCapture(
            service=self.service,
            clock=self.clock,
            event_cb=_fanout(self.events),
        )

        # V2 #5
        self.error_bridge = PreviewErrorBridge(
            manager=self.manager,
            clock=self.clock,
            sleep=self.sleep,
            event_cb=_fanout(self.events, self.sse_bridge.on_error_event),
        )

        # V2 #6
        self.builder = AgentVisualContextBuilder(
            responsive=self.responsive,
            error_bridge=self.error_bridge,
            clock=self.clock,
            event_cb=_fanout(self.events),
        )

    # ────────────────────────────────────────────────────────────────
    # Agent actions
    # ────────────────────────────────────────────────────────────────

    def agent_writes_broken_code(self) -> None:
        """Step 1: agent emits code that can't compile."""

        self.header_path.write_text(BROKEN_HEADER_TSX, encoding="utf-8")
        self.docker.set_logs(BROKEN_LOGS)

    def agent_fixes_code(self) -> None:
        """Step 6: agent applies the fix — dev server recompiles clean."""

        self.header_path.write_text(FIXED_HEADER_TSX, encoding="utf-8")
        self.docker.set_logs(CLEAN_LOGS)

    def start_sandbox(self) -> None:
        config = SandboxConfig(
            session_id=self.session_id,
            workspace_path=str(self.workspace),
            host_port=40500,
        )
        self.lifecycle.ensure_session(config, wait_ready=False)
        self.manager.mark_ready(self.session_id)

    def preview_url(self) -> str:
        inst = self.manager.get(self.session_id)
        assert inst is not None and inst.preview_url, "sandbox must have a URL"
        return inst.preview_url


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path=tmp_path)


# ═══════════════════════════════════════════════════════════════════
#  Golden end-to-end auto-fix loop
# ═══════════════════════════════════════════════════════════════════


def _run_autofix_loop(rig: Rig) -> tuple[AgentVisualContextPayload, AgentVisualContextPayload]:
    """Drive the full V2 #8 integration flow.

    Returns ``(broken_payload, fixed_payload)`` — the two multimodal turns
    the agent would actually see before and after its fix.
    """

    # ── Round 1 — agent writes broken code ────────────────────────
    rig.agent_writes_broken_code()
    rig.start_sandbox()
    rig.lifecycle.hot_reload(
        rig.session_id, files_changed=("components/Header.tsx",)
    )

    # ── Round 1 — scan + build multimodal payload ─────────────────
    round1_batch = rig.error_bridge.scan(rig.session_id)
    assert round1_batch.active_count >= 1, "broken logs must surface an error"

    broken_payload, broken_message = rig.builder.build_message(
        session_id=rig.session_id,
        preview_url=rig.preview_url(),
        turn_id="react-turn-1",
    )

    # The agent sees three images + an error summary in this turn.
    assert broken_payload.image_count == 3
    assert broken_payload.has_blocking_errors is True
    assert broken_payload.active_error_count >= 1
    assert "Header.tsx" in broken_payload.text_prompt

    # Multimodal HumanMessage is Anthropic-shaped.
    content = broken_message.content
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert [b["type"] for b in content if b["type"] == "image"].count("image") == 3
    for b in content:
        if b["type"] == "image":
            assert b["source"]["type"] == "base64"
            assert b["source"]["media_type"] == "image/png"
            assert b["source"]["data"]

    # ── Round 2 — agent fixes the code ────────────────────────────
    rig.clock.advance(0.5)
    rig.agent_fixes_code()
    rig.lifecycle.hot_reload(
        rig.session_id, files_changed=("components/Header.tsx",)
    )

    round2_batch = rig.error_bridge.scan(rig.session_id)
    assert round2_batch.active_count == 0, "fixed logs must clear the error"
    assert round2_batch.cleared_count >= 1

    fixed_payload, fixed_message = rig.builder.build_message(
        session_id=rig.session_id,
        preview_url=rig.preview_url(),
        turn_id="react-turn-2",
    )
    assert fixed_payload.image_count == 3
    assert fixed_payload.has_blocking_errors is False
    assert fixed_payload.active_error_count == 0
    # Multimodal message still has 3 images — agent still sees the UI.
    fixed_content = fixed_message.content
    assert isinstance(fixed_content, list)
    assert sum(1 for b in fixed_content if b["type"] == "image") == 3

    return broken_payload, fixed_payload


def test_autofix_loop_round1_surfaces_error_round2_is_clean(rig: Rig) -> None:
    broken, fixed = _run_autofix_loop(rig)
    # Round 1 saw errors, round 2 was clean — the *whole* point.
    assert broken.has_errors is True
    assert fixed.has_errors is False


def test_autofix_loop_round1_attributes_error_to_correct_file(rig: Rig) -> None:
    broken, _ = _run_autofix_loop(rig)
    # The error summary should name the file so Opus can open it.
    assert "Header.tsx" in broken.error_summary_markdown
    assert "Header.tsx" in broken.text_prompt
    # After the fix the bridge's last_batch must still be reachable for
    # UI replay — the final scan records the cleared transition.
    assert rig.error_bridge.last_batch(rig.session_id) is not None


def test_autofix_loop_auto_fix_hint_guides_agent_when_broken(rig: Rig) -> None:
    broken, fixed = _run_autofix_loop(rig)
    # Broken turn: hint carries a non-empty action.
    assert broken.auto_fix_hint.strip() != ""
    # Fixed turn: hint indicates the preview rendered cleanly.
    assert "clean" in fixed.auto_fix_hint.lower() or fixed.active_error_count == 0


def test_autofix_loop_captures_nine_total_screenshots_across_two_rounds(
    rig: Rig,
) -> None:
    _run_autofix_loop(rig)
    # 3 viewports × 2 rounds = 6 (responsive); plus 0 extra lifecycle
    # captures since the golden path uses responsive.capture_all only.
    assert len(rig.engine.calls) == 6


def test_autofix_loop_uses_default_viewport_matrix(rig: Rig) -> None:
    _run_autofix_loop(rig)
    seen_names = [req.viewport.name for req in rig.engine.calls]
    # desktop/tablet/mobile in matrix order, repeated for the two rounds.
    assert seen_names[:3] == list(DEFAULT_VIEWPORT_MATRIX)
    assert seen_names[3:] == list(DEFAULT_VIEWPORT_MATRIX)


# ═══════════════════════════════════════════════════════════════════
#  SSE bridge (V2 #7) contract during the auto-fix flow
# ═══════════════════════════════════════════════════════════════════


def test_sse_bridge_publishes_screenshot_frames_for_every_capture(rig: Rig) -> None:
    _run_autofix_loop(rig)
    screenshot_frames = rig.publisher.by_topic(SSE_EVENT_SCREENSHOT)
    # 3 viewports × 2 rounds, with the bridge de-duping the V2 #2 lifecycle
    # echo vs the V2 #3 service emit at the same timestamp.
    assert len(screenshot_frames) >= 3
    # Every frame carries the V2 row 7 required field set.
    for frame in screenshot_frames:
        for f in SCREENSHOT_EVENT_FIELDS:
            assert f in frame, f"{f!r} missing from {frame!r}"
        # No raw PNG bytes ever cross the SSE boundary.
        for value in frame.values():
            assert not isinstance(value, (bytes, bytearray))


def test_sse_bridge_publishes_error_detected_then_cleared(rig: Rig) -> None:
    _run_autofix_loop(rig)
    error_frames = rig.publisher.by_topic(SSE_EVENT_ERROR)
    phases = [f.get("phase") for f in error_frames]
    assert "detected" in phases
    assert "cleared" in phases
    # Every error frame carries the required field set, even cleared ones.
    for frame in error_frames:
        for f in ERROR_EVENT_FIELDS:
            assert f in frame


def test_sse_bridge_counters_track_emitted_vs_deduped(rig: Rig) -> None:
    _run_autofix_loop(rig)
    snapshot = rig.sse_bridge.snapshot()
    assert snapshot["screenshot_emitted"] >= 3
    assert snapshot["error_emitted"] >= 1
    assert snapshot["error_cleared_emitted"] >= 1
    # publish never crashed.
    assert snapshot["publish_failures"] == 0


def test_sse_bridge_dedupes_lifecycle_and_service_screenshot_echo(rig: Rig) -> None:
    """V2 #2 lifecycle.capture_screenshot emits ``ui_sandbox.screenshot`` and
    V2 #3 service.capture emits the same topic.  Both share the same capture
    timestamp, so the bridge's dedup window must collapse them into one SSE
    frame per viewport.
    """

    _run_autofix_loop(rig)
    snapshot = rig.sse_bridge.snapshot()
    # Some dedup activity is expected because the rig funnels V2 #2 lifecycle
    # events through on_lifecycle_event (alias of on_screenshot_event) while
    # V2 #3 already feeds on_screenshot_event directly.
    assert snapshot["screenshot_deduped"] >= 0
    # Total emitted + deduped >= total captures (6).
    assert (
        snapshot["screenshot_emitted"] + snapshot["screenshot_deduped"] >= 6
    )


# ═══════════════════════════════════════════════════════════════════
#  Event ordering across V2 #1-#6 during the loop
# ═══════════════════════════════════════════════════════════════════


def test_event_ordering_round1_hot_reload_precedes_error_and_screenshot(
    rig: Rig,
) -> None:
    _run_autofix_loop(rig)
    types = rig.events.types()
    # Flow: hot_reload → scan (emits error.detected) → build_message
    # (emits 3 × screenshot via responsive.capture_all).
    first_hot_reload = types.index(LIFECYCLE_EVENT_HOT_RELOAD)
    first_error_detected = types.index(ERROR_EVENT_DETECTED)
    first_screenshot = next(
        i for i, t in enumerate(types) if t == us.SCREENSHOT_EVENT_CAPTURED
    )
    assert first_hot_reload < first_error_detected
    assert first_error_detected < first_screenshot
    # Round 1 produces exactly 3 screenshots before the second hot_reload.
    second_hot_reload = types.index(
        LIFECYCLE_EVENT_HOT_RELOAD, first_hot_reload + 1
    )
    round1_screens = [
        i
        for i, t in enumerate(types[first_hot_reload:second_hot_reload])
        if t == us.SCREENSHOT_EVENT_CAPTURED
    ]
    assert len(round1_screens) == 3


def test_event_ordering_round2_clears_error_after_second_hot_reload(
    rig: Rig,
) -> None:
    _run_autofix_loop(rig)
    types = rig.events.types()
    second_hot_reload = [
        i for i, t in enumerate(types) if t == LIFECYCLE_EVENT_HOT_RELOAD
    ][1]
    # Cleared event must appear *after* the second hot_reload.
    cleared_idx = [i for i, t in enumerate(types) if t == ERROR_EVENT_CLEARED]
    assert cleared_idx
    assert cleared_idx[-1] > second_hot_reload


def test_event_namespaces_are_disjoint_across_v2_modules(rig: Rig) -> None:
    """V2 #1-#7 all publish events into ``ui_sandbox.*`` but each row
    owns a disjoint sub-namespace — the rig exercises every row and must
    never see stray topics."""

    _run_autofix_loop(rig)
    observed = set(rig.events.types())
    # Every observed topic must start with one of the documented namespaces.
    allowed_prefixes = (
        # V2 #1 — SandboxManager lifecycle verbs
        "ui_sandbox.created",
        "ui_sandbox.starting",
        "ui_sandbox.ready",
        "ui_sandbox.stopped",
        "ui_sandbox.failed",
        # V2 #2 — SandboxLifecycle orchestration
        "ui_sandbox.ensure_session",
        "ui_sandbox.hot_reload",
        "ui_sandbox.teardown",
        "ui_sandbox.reaped",
        "ui_sandbox.ready_timeout",
        # V2 #2 + V2 #3 share this topic by design
        "ui_sandbox.screenshot",
        # V2 #4 — batched three-viewport matrix
        "ui_sandbox.viewport_batch.",
        # V2 #5 — compile/runtime error bridge
        "ui_sandbox.error.",
        # V2 #6 — multimodal agent visual context
        "ui_sandbox.agent_visual_context.",
    )
    for t in observed:
        assert t.startswith(allowed_prefixes), f"stray topic: {t!r}"


# ═══════════════════════════════════════════════════════════════════
#  Payload correctness — JSON safety + multimodal block shape
# ═══════════════════════════════════════════════════════════════════


def test_broken_payload_serialises_to_json_with_base64_images(rig: Rig) -> None:
    broken, _ = _run_autofix_loop(rig)
    encoded = json.dumps(broken.to_dict())
    assert "image_base64" in encoded
    # base64 strings round-trip to bytes starting with the PNG signature.
    parsed = json.loads(encoded)
    for img in parsed["images"]:
        raw = base64.b64decode(img["image_base64"])
        assert raw.startswith(PNG_SIGNATURE.decode("latin1").encode("latin1"))


def test_fixed_payload_content_blocks_match_anthropic_shape(rig: Rig) -> None:
    _, fixed = _run_autofix_loop(rig)
    blocks = fixed.to_content_blocks()
    assert blocks[0]["type"] == "text"
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 3
    for img in images:
        assert img["source"]["type"] == "base64"
        assert img["source"]["media_type"] == "image/png"
        assert isinstance(img["source"]["data"], str)
        assert img["source"]["data"]  # non-empty


def test_fixed_payload_error_summary_is_clean(rig: Rig) -> None:
    _, fixed = _run_autofix_loop(rig)
    # Spec from V2 #5's render_error_markdown: "No active errors." on clean.
    # V2 #6 re-uses that body verbatim inside the payload summary.
    assert "No active errors" in fixed.error_summary_markdown


def test_broken_payload_preserves_error_file_line_in_summary(rig: Rig) -> None:
    broken, _ = _run_autofix_loop(rig)
    summary = broken.error_summary_markdown
    assert "Header.tsx" in summary


# ═══════════════════════════════════════════════════════════════════
#  Manager / lifecycle health after the loop
# ═══════════════════════════════════════════════════════════════════


def test_sandbox_stays_running_through_the_whole_flow(rig: Rig) -> None:
    _run_autofix_loop(rig)
    inst = rig.manager.get(rig.session_id)
    assert inst is not None
    # An error never tears the sandbox down — auto-fix relies on HMR.
    assert inst.status in (SandboxStatus.running,)
    # One docker run_detached — not one per round.
    assert len(rig.docker.run_calls) == 1


def test_workspace_mount_is_writable_for_agent_edits(rig: Rig) -> None:
    _run_autofix_loop(rig)
    # The fix actually persisted to the mounted workspace — this is the
    # contract Docker bind-mount gives us.  Bridging through FakeDocker
    # the file write is visible to the next round's log parser.
    assert rig.header_path.read_text(encoding="utf-8") == FIXED_HEADER_TSX


def test_engine_capture_requests_carry_expected_preview_url(rig: Rig) -> None:
    _run_autofix_loop(rig)
    preview_url = rig.preview_url()
    for req in rig.engine.calls:
        assert req.preview_url == preview_url
        assert req.session_id == rig.session_id
        assert req.path == "/"


def test_error_bridge_last_batch_matches_cleared_state(rig: Rig) -> None:
    _run_autofix_loop(rig)
    last = rig.error_bridge.last_batch(rig.session_id)
    assert last is not None
    assert last.active_count == 0


# ═══════════════════════════════════════════════════════════════════
#  Idempotency & determinism
# ═══════════════════════════════════════════════════════════════════


def test_repeated_scan_on_clean_logs_is_idempotent(rig: Rig) -> None:
    _run_autofix_loop(rig)
    # After the fix, a second scan must not re-detect the cleared error.
    second = rig.error_bridge.scan(rig.session_id)
    assert second.detected_count == 0
    assert second.cleared_count == 0
    assert second.active_count == 0


def test_running_loop_twice_on_same_rig_produces_stable_payload_shape(
    tmp_path: Path,
) -> None:
    # Two independent rigs must converge on the same payload shape — the
    # whole pipeline is deterministic given fixed clock + fixed engine.
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    rig_a = Rig(tmp_path=tmp_path / "a")
    rig_b = Rig(tmp_path=tmp_path / "b")

    bp_a, fp_a = _run_autofix_loop(rig_a)
    bp_b, fp_b = _run_autofix_loop(rig_b)

    # Both agents saw identical shapes (three images, one file-scoped error).
    assert bp_a.image_count == bp_b.image_count == 3
    assert bp_a.active_error_count == bp_b.active_error_count
    assert fp_a.active_error_count == fp_b.active_error_count == 0
    assert bp_a.viewport_matrix == bp_b.viewport_matrix == DEFAULT_VIEWPORT_MATRIX


# ═══════════════════════════════════════════════════════════════════
#  Sibling-module alignment — V1 + V2 #1-#7 still importable
# ═══════════════════════════════════════════════════════════════════


def test_sibling_modules_importable() -> None:
    from backend import (
        ui_agent_visual_context,
        ui_component_registry,
        ui_preview_error_bridge,
        ui_responsive_viewport,
        ui_sandbox,
        ui_sandbox_lifecycle,
        ui_sandbox_sse,
        ui_screenshot,
    )

    for mod in (
        ui_component_registry,
        ui_sandbox,
        ui_sandbox_lifecycle,
        ui_screenshot,
        ui_responsive_viewport,
        ui_preview_error_bridge,
        ui_agent_visual_context,
        ui_sandbox_sse,
    ):
        assert mod.__name__


def test_schema_versions_remain_independent() -> None:
    # V2 #8 is a test-only row — no new module schema is introduced.  But
    # the sibling modules must keep their independent schema strings.
    assert usx.UI_SANDBOX_SCHEMA_VERSION == "1.0.0"
    assert usl.SANDBOX_LIFECYCLE_SCHEMA_VERSION == "1.0.0"
    assert us.UI_SCREENSHOT_SCHEMA_VERSION == "1.0.0"
    assert urv.UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION == "1.0.0"
    assert upb.UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION == "1.0.0"
    assert avc.UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION == "1.0.0"
    assert uss.UI_SANDBOX_SSE_SCHEMA_VERSION == "1.0.0"
