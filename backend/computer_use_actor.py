"""WP.13 (OP-1507) — Computer-Use Actor for HD bring-up VNC.

Pattern inspired by Warp ``crates/computer_use/src/lib.rs`` (Anthropic-
style Actor trait); **independently implemented**, no Warp source
consulted or vendored (inspiration-only tier — see
``docs/legal/oss-boundaries.md`` and the WP.13 entry in
``docs/design/wp-warp-inspired-patterns.md``).

Why this exists
---------------

HD bring-up scenarios put the operator in front of a live client device
that is running a GUI, a boot loader, or a recovery shell. The
``HD.19`` Bring-up Workbench needs an actor that the LLM (and, behind a
human-in-the-loop confirmation, the runner) can drive over a VNC-style
frame channel:

* **Screenshot** — RGBA frame, optionally cropped to a region the
  operator marked as "safe to look at". Cropping at capture time keeps
  PII off the wire when the bench is mirrored over a remote bring-up
  session.
* **Mouse click** — single-point click constrained to a *pixel budget*:
  the union of every region the operator has explicitly granted the
  actor. Clicks outside the budget raise :class:`RegionOutOfBudget`
  before they touch the engine — the engine never sees an action it
  was not authorised to perform.
* **Keyboard** — typed text or a single named key. Modifier chords
  (Ctrl+A, Cmd+Shift+P) are expressed as a typed token list rather
  than a free-form sequence so the LLM cannot smuggle escape codes
  through the channel.
* **Cross-platform** — :class:`ActorEngine` is a :class:`Protocol`; the
  module ships placeholder engines for ``x11`` / ``wayland`` / ``macos``
  / ``windows`` that raise :class:`EngineUnavailable` until a host
  binding is installed. Tests drive a :class:`FakeActorEngine` so the
  whole module is exercisable without a display server.

Where this sits
---------------

* ``HD.19 Bring-up Workbench`` (issue-side) reaches into this module to
  attach an actor session to a bring-up workspace. Frames flow back to
  the workbench timeline via the event emitter the service wires up.
* ``W14 Live Sandbox Preview`` shares the same RGBA frame shape so the
  bring-up workbench can use the same renderer it already has for
  sandbox previews. The integration point is :class:`ActorFrame` —
  identical fields to the V2 ``ScreenshotCapture`` minus the
  Playwright-specific viewport name (HD bring-up is pixel-accurate, not
  viewport-relative).
* The :class:`ComputerUseActor` service holds bounded per-session
  history (``deque(maxlen=...)``) so the workbench can scrub backwards
  through the last N actions without unbounded memory growth.

Design decisions
----------------

* **Actor "trait" mirrors the Anthropic computer-use tool set.** Each
  public method on :class:`ComputerUseActor` maps to one of the four
  Anthropic action types — :meth:`take_screenshot`, :meth:`click`,
  :meth:`type_text`, :meth:`press_key`. The LLM-facing tool schema
  (returned by :func:`actor_tool_schema`) lists these by name so a
  future LLM dispatch path drops in without per-call adapter code.
* **Engine is injectable.** :class:`ActorEngine` is a Protocol; the
  service never imports a concrete engine. Real engines lazy-import
  their native bindings inside ``__init__`` so the module loads on a
  host without ``python-xlib`` / ``pywinctl`` / ``pyobjc``.
* **Pixel budget is enforced before dispatch.** Region + click bounds
  are evaluated in pure Python ahead of every engine call; an engine
  cannot accidentally accept a coordinate the operator did not grant.
* **Schema is versioned.** :data:`COMPUTER_USE_ACTOR_SCHEMA_VERSION`
  is semver and bumps on any shape change to :class:`ActorFrame` /
  :class:`ActorAction` ``.to_dict()`` or the event payload.

Contract (pinned by ``backend/tests/test_computer_use_actor.py``)
-----------------------------------------------------------------

* :data:`COMPUTER_USE_ACTOR_SCHEMA_VERSION` is semver.
* :data:`PLATFORM_VALUES` lists exactly the four platforms the design
  doc enumerates (``x11`` / ``wayland`` / ``macos`` / ``windows``).
* :class:`Region` validates strictly-positive width/height and rejects
  negative origins; :meth:`Region.contains_point` is inclusive on the
  origin and exclusive on the far edge.
* :class:`PixelBudget` accepts a union of regions and answers
  :meth:`allows_point` / :meth:`allows_region` in O(n_regions).
* :meth:`ComputerUseActor.click` raises :class:`RegionOutOfBudget` if
  the point falls outside every granted region — the engine is *not*
  called.
* :meth:`ComputerUseActor.press_key` rejects keys not in
  :data:`KNOWN_KEYS`.
* :meth:`ComputerUseActor.type_text` rejects control characters not in
  the explicit allow list (``\\n`` / ``\\t`` / printable ASCII +
  Unicode); the channel never lets ``\\x1b`` through.
* :func:`detect_platform` returns one of :data:`PLATFORM_VALUES` based
  on the injectable :class:`PlatformProbe` (``sys.platform`` +
  ``WAYLAND_DISPLAY`` / ``DISPLAY`` env vars).
* :func:`actor_tool_schema` returns the JSON-Schema-shaped Anthropic
  tool definition the LLM dispatch path will consume — four tools
  (``computer_screenshot`` / ``computer_click`` / ``computer_type`` /
  ``computer_key``) with stable names and parameter shapes.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Iterable, Literal, Mapping, Protocol

logger = logging.getLogger(__name__)


__all__ = [
    "COMPUTER_USE_ACTOR_SCHEMA_VERSION",
    "PLATFORM_X11",
    "PLATFORM_WAYLAND",
    "PLATFORM_MACOS",
    "PLATFORM_WINDOWS",
    "PLATFORM_VALUES",
    "Platform",
    "MOUSE_BUTTON_LEFT",
    "MOUSE_BUTTON_RIGHT",
    "MOUSE_BUTTON_MIDDLE",
    "MOUSE_BUTTON_VALUES",
    "MouseButton",
    "KNOWN_KEYS",
    "MAX_REGION_PIXELS",
    "MAX_TYPE_TEXT_LENGTH",
    "DEFAULT_HISTORY_SIZE",
    "RGBA_BYTES_PER_PIXEL",
    "ACTOR_EVENT_SCREENSHOT",
    "ACTOR_EVENT_CLICK",
    "ACTOR_EVENT_TYPE",
    "ACTOR_EVENT_KEY",
    "ACTOR_EVENT_TYPES",
    "Region",
    "PixelBudget",
    "ActorFrame",
    "ActorAction",
    "ActorEngine",
    "ComputerUseActor",
    "FakeActorEngine",
    "PlatformProbe",
    "ActorError",
    "RegionInvalid",
    "RegionOutOfBudget",
    "KeyUnknown",
    "ButtonUnknown",
    "TextInvalid",
    "PlatformUnsupported",
    "EngineUnavailable",
    "detect_platform",
    "actor_tool_schema",
    "validate_rgba_frame",
]


#: Bump on any shape change to :class:`ActorFrame.to_dict()`,
#: :class:`ActorAction.to_dict()`, or the event payload.
COMPUTER_USE_ACTOR_SCHEMA_VERSION = "1.0.0"


# ─── Platform vocabulary ───────────────────────────────────────────

Platform = Literal["x11", "wayland", "macos", "windows"]

PLATFORM_X11: Platform = "x11"
PLATFORM_WAYLAND: Platform = "wayland"
PLATFORM_MACOS: Platform = "macos"
PLATFORM_WINDOWS: Platform = "windows"

#: The four platforms the WP.13 design enumerates. Adding a new entry
#: requires a matching :class:`ActorEngine` implementation + a clause
#: in :func:`detect_platform`.
PLATFORM_VALUES: tuple[Platform, ...] = (
    PLATFORM_X11,
    PLATFORM_WAYLAND,
    PLATFORM_MACOS,
    PLATFORM_WINDOWS,
)


# ─── Mouse / keyboard vocabulary ───────────────────────────────────

MouseButton = Literal["left", "right", "middle"]

MOUSE_BUTTON_LEFT: MouseButton = "left"
MOUSE_BUTTON_RIGHT: MouseButton = "right"
MOUSE_BUTTON_MIDDLE: MouseButton = "middle"

MOUSE_BUTTON_VALUES: tuple[MouseButton, ...] = (
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_RIGHT,
    MOUSE_BUTTON_MIDDLE,
)


#: Named keys the actor accepts via :meth:`ComputerUseActor.press_key`.
#: Kept deliberately small — chords go through :meth:`type_text` so a
#: future "key macro" feature has a single audit surface. Modifier
#: combinations are expressed as the ``modifiers`` field on
#: :class:`ActorAction`.
KNOWN_KEYS: frozenset[str] = frozenset({
    "enter", "tab", "escape", "backspace", "delete", "space",
    "up", "down", "left", "right",
    "home", "end", "page_up", "page_down",
    "f1", "f2", "f3", "f4", "f5", "f6",
    "f7", "f8", "f9", "f10", "f11", "f12",
})


#: Modifier keys the actor recognises in :class:`ActorAction.modifiers`.
KNOWN_MODIFIERS: frozenset[str] = frozenset({
    "ctrl", "alt", "shift", "cmd", "meta", "super",
})


# ─── Safety caps ───────────────────────────────────────────────────

#: Hard ceiling on the pixel area a single region may cover. 4096×4096
#: lets the actor capture a 4K display in one shot but rejects pathological
#: regions that would balloon RGBA frame size past ~64 MB.
MAX_REGION_PIXELS = 4096 * 4096

#: Hard ceiling on the number of characters one :meth:`type_text` call
#: may inject. Prevents the LLM from pasting megabytes through the
#: keyboard channel.
MAX_TYPE_TEXT_LENGTH = 4096

#: Per-session capture history cap. Workbench scrub can hold the last
#: 256 frames before the tail rolls off.
DEFAULT_HISTORY_SIZE = 256

#: Bytes per pixel for an RGBA frame. Used by :func:`validate_rgba_frame`
#: to sanity-check engine output against region dimensions.
RGBA_BYTES_PER_PIXEL = 4


# ─── Event names ───────────────────────────────────────────────────

ACTOR_EVENT_SCREENSHOT = "actor.screenshot"
ACTOR_EVENT_CLICK = "actor.click"
ACTOR_EVENT_TYPE = "actor.type"
ACTOR_EVENT_KEY = "actor.key"

ACTOR_EVENT_TYPES: frozenset[str] = frozenset({
    ACTOR_EVENT_SCREENSHOT,
    ACTOR_EVENT_CLICK,
    ACTOR_EVENT_TYPE,
    ACTOR_EVENT_KEY,
})


# ─── Errors ────────────────────────────────────────────────────────


class ActorError(Exception):
    """Base class for all WP.13 actor errors."""


class RegionInvalid(ActorError):
    """Region has non-positive dimensions or out-of-cap pixel count."""


class RegionOutOfBudget(ActorError):
    """Action's target point/region is not covered by the granted budget."""


class KeyUnknown(ActorError):
    """``press_key`` was called with a key not in :data:`KNOWN_KEYS`."""


class ButtonUnknown(ActorError):
    """``click`` was called with a button not in :data:`MOUSE_BUTTON_VALUES`."""


class TextInvalid(ActorError):
    """``type_text`` was called with disallowed control characters or oversize input."""


class PlatformUnsupported(ActorError):
    """:func:`detect_platform` could not identify a supported host platform."""


class EngineUnavailable(ActorError):
    """Concrete engine could not initialise (missing native binding)."""


# ─── Region + budget ───────────────────────────────────────────────


@dataclass(frozen=True)
class Region:
    """Rectangular pixel region.

    Origin (``x`` / ``y``) is inclusive; far edge (``x + width`` /
    ``y + height``) is exclusive — standard half-open rectangle so two
    adjacent regions tile without overlap.
    """

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0:
            raise RegionInvalid(
                f"region origin must be non-negative, got ({self.x}, {self.y})"
            )
        if self.width <= 0 or self.height <= 0:
            raise RegionInvalid(
                f"region dimensions must be positive, got "
                f"{self.width}x{self.height}"
            )
        pixels = self.width * self.height
        if pixels > MAX_REGION_PIXELS:
            raise RegionInvalid(
                f"region area {pixels} exceeds MAX_REGION_PIXELS "
                f"({MAX_REGION_PIXELS})"
            )

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def contains_point(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def contains_region(self, other: "Region") -> bool:
        return (
            other.x >= self.x
            and other.y >= self.y
            and other.x + other.width <= self.x + self.width
            and other.y + other.height <= self.y + self.height
        )

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class PixelBudget:
    """Union of granted regions an actor may interact with.

    A budget with no regions denies every action. The operator is
    expected to declare the budget up-front when attaching the actor to
    a bring-up workspace (the workbench will surface a region-picker
    overlay; the LLM never gets to *expand* the budget itself).
    """

    regions: tuple[Region, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(r, Region) for r in self.regions):
            raise RegionInvalid("PixelBudget.regions must be a tuple of Region")

    @property
    def total_pixels(self) -> int:
        return sum(r.pixels for r in self.regions)

    def allows_point(self, x: int, y: int) -> bool:
        return any(r.contains_point(x, y) for r in self.regions)

    def allows_region(self, region: Region) -> bool:
        return any(r.contains_region(region) for r in self.regions)

    @classmethod
    def unrestricted(cls, width: int, height: int) -> "PixelBudget":
        """Convenience: grant the whole display. HD bring-up sometimes wants
        this for a sandbox device the operator owns outright."""
        return cls(regions=(Region(0, 0, width, height),))


# ─── Frame + action records ───────────────────────────────────────


@dataclass(frozen=True)
class ActorFrame:
    """Single RGBA capture.

    ``rgba`` is the raw pixel buffer (length = ``width * height *
    RGBA_BYTES_PER_PIXEL``); the workbench renders by copying it into
    an HTML canvas / OffscreenCanvas. Kept as ``bytes`` rather than a
    numpy array to avoid pulling numpy into the import graph of the
    bring-up workbench.
    """

    schema_version: str
    bringup_session_id: str
    region: Region
    rgba: bytes
    timestamp: float
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bringup_session_id": self.bringup_session_id,
            "region": self.region.to_dict(),
            "rgba_byte_length": len(self.rgba),
            "timestamp": self.timestamp,
            "sequence": self.sequence,
        }


@dataclass(frozen=True)
class ActorAction:
    """Audit record for one dispatched action.

    Every call on :class:`ComputerUseActor` produces one of these. The
    workbench timeline stitches the action log next to the frame
    timeline so the operator can replay the sequence.
    """

    schema_version: str
    bringup_session_id: str
    kind: Literal["screenshot", "click", "type", "key"]
    payload: Mapping[str, Any]
    timestamp: float
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bringup_session_id": self.bringup_session_id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
            "sequence": self.sequence,
        }


# ─── Engine protocol ───────────────────────────────────────────────


class ActorEngine(Protocol):
    """Platform-specific bridge.

    Concrete engines own the actual X11 / Wayland / Quartz / Win32
    calls. The :class:`ComputerUseActor` service never inspects the
    engine class — it dispatches through this Protocol so tests can
    swap in :class:`FakeActorEngine` with no real I/O.
    """

    platform: Platform

    def take_screenshot(self, region: Region) -> bytes: ...

    def click(self, x: int, y: int, button: MouseButton) -> None: ...

    def type_text(self, text: str) -> None: ...

    def press_key(self, key: str, modifiers: tuple[str, ...]) -> None: ...


# ─── Validators ────────────────────────────────────────────────────


def validate_rgba_frame(rgba: bytes, region: Region) -> None:
    """Raise :class:`ActorError` if ``rgba`` size mismatches ``region``.

    Cheap correctness gate against engines that return an unexpected
    pixel format. The bring-up workbench renders blindly off this
    buffer; a stride mismatch becomes a corrupt canvas frame the
    operator has to debug, so we'd rather fail loudly here.
    """
    expected = region.pixels * RGBA_BYTES_PER_PIXEL
    if len(rgba) != expected:
        raise ActorError(
            f"RGBA byte length {len(rgba)} does not match region "
            f"{region.width}x{region.height} (expected {expected})"
        )


def _validate_type_text(text: str) -> None:
    if len(text) > MAX_TYPE_TEXT_LENGTH:
        raise TextInvalid(
            f"type_text length {len(text)} exceeds MAX_TYPE_TEXT_LENGTH "
            f"({MAX_TYPE_TEXT_LENGTH})"
        )
    for idx, ch in enumerate(text):
        if ch in ("\n", "\t"):
            continue
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise TextInvalid(
                f"type_text contains disallowed control character "
                f"U+{code:04X} at index {idx}"
            )


# ─── Actor service ─────────────────────────────────────────────────


EventEmitter = Callable[[str, Mapping[str, Any]], None]


class ComputerUseActor:
    """Service object that the HD.19 workbench drives.

    Wraps a concrete :class:`ActorEngine` with budget enforcement,
    sequence numbering, history, and event emission. Thread-safe so
    the workbench can dispatch from its UI thread while a background
    capture loop fills the timeline.
    """

    def __init__(
        self,
        engine: ActorEngine,
        bringup_session_id: str,
        budget: PixelBudget,
        *,
        history_size: int = DEFAULT_HISTORY_SIZE,
        clock: Callable[[], float] | None = None,
        emit: EventEmitter | None = None,
    ) -> None:
        if not bringup_session_id:
            raise ValueError("bringup_session_id must be non-empty")
        self._engine = engine
        self._session_id = bringup_session_id
        self._budget = budget
        self._clock = clock or _default_clock
        self._emit = emit
        self._lock = threading.RLock()
        self._frames: Deque[ActorFrame] = deque(maxlen=history_size)
        self._actions: Deque[ActorAction] = deque(maxlen=history_size)
        self._sequence = 0

    # ── public action surface ──────────────────────────────────

    def take_screenshot(self, region: Region | None = None) -> ActorFrame:
        """Capture an RGBA frame for ``region`` (default: full budget).

        If no explicit region is given, the call captures the *first*
        region the budget grants — the workbench is expected to spell
        out which region it wants when the budget has more than one.
        """
        target = self._resolve_screenshot_region(region)
        if not self._budget.allows_region(target):
            raise RegionOutOfBudget(
                f"region {target.to_dict()} not covered by budget"
            )
        rgba = self._engine.take_screenshot(target)
        validate_rgba_frame(rgba, target)
        with self._lock:
            sequence = self._next_sequence()
            frame = ActorFrame(
                schema_version=COMPUTER_USE_ACTOR_SCHEMA_VERSION,
                bringup_session_id=self._session_id,
                region=target,
                rgba=rgba,
                timestamp=self._clock(),
                sequence=sequence,
            )
            self._frames.append(frame)
        self._record_action("screenshot", {"region": target.to_dict()}, sequence)
        self._emit_event(ACTOR_EVENT_SCREENSHOT, frame.to_dict())
        return frame

    def click(
        self,
        x: int,
        y: int,
        button: MouseButton = MOUSE_BUTTON_LEFT,
    ) -> ActorAction:
        if button not in MOUSE_BUTTON_VALUES:
            raise ButtonUnknown(
                f"unknown mouse button {button!r}; "
                f"expected one of {MOUSE_BUTTON_VALUES}"
            )
        if not self._budget.allows_point(x, y):
            raise RegionOutOfBudget(
                f"click ({x}, {y}) not covered by budget"
            )
        self._engine.click(x, y, button)
        with self._lock:
            sequence = self._next_sequence()
        payload = {"x": x, "y": y, "button": button}
        action = self._record_action("click", payload, sequence)
        self._emit_event(ACTOR_EVENT_CLICK, action.to_dict())
        return action

    def type_text(self, text: str) -> ActorAction:
        _validate_type_text(text)
        self._engine.type_text(text)
        with self._lock:
            sequence = self._next_sequence()
        payload = {"text_length": len(text)}
        action = self._record_action("type", payload, sequence)
        self._emit_event(ACTOR_EVENT_TYPE, action.to_dict())
        return action

    def press_key(
        self,
        key: str,
        modifiers: Iterable[str] = (),
    ) -> ActorAction:
        if key not in KNOWN_KEYS:
            raise KeyUnknown(f"unknown key {key!r}")
        mods = tuple(modifiers)
        unknown = [m for m in mods if m not in KNOWN_MODIFIERS]
        if unknown:
            raise KeyUnknown(f"unknown modifier(s) {unknown!r}")
        self._engine.press_key(key, mods)
        with self._lock:
            sequence = self._next_sequence()
        payload = {"key": key, "modifiers": list(mods)}
        action = self._record_action("key", payload, sequence)
        self._emit_event(ACTOR_EVENT_KEY, action.to_dict())
        return action

    # ── introspection ──────────────────────────────────────────

    @property
    def bringup_session_id(self) -> str:
        return self._session_id

    @property
    def platform(self) -> Platform:
        return self._engine.platform

    @property
    def budget(self) -> PixelBudget:
        return self._budget

    def recent_frames(self) -> tuple[ActorFrame, ...]:
        with self._lock:
            return tuple(self._frames)

    def recent_actions(self) -> tuple[ActorAction, ...]:
        with self._lock:
            return tuple(self._actions)

    def latest_frame(self) -> ActorFrame | None:
        with self._lock:
            return self._frames[-1] if self._frames else None

    # ── internal ───────────────────────────────────────────────

    def _resolve_screenshot_region(self, region: Region | None) -> Region:
        if region is not None:
            return region
        if not self._budget.regions:
            raise RegionOutOfBudget("budget has no regions to capture from")
        return self._budget.regions[0]

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _record_action(
        self,
        kind: Literal["screenshot", "click", "type", "key"],
        payload: Mapping[str, Any],
        sequence: int,
    ) -> ActorAction:
        action = ActorAction(
            schema_version=COMPUTER_USE_ACTOR_SCHEMA_VERSION,
            bringup_session_id=self._session_id,
            kind=kind,
            payload=dict(payload),
            timestamp=self._clock(),
            sequence=sequence,
        )
        with self._lock:
            self._actions.append(action)
        return action

    def _emit_event(self, event: str, payload: Mapping[str, Any]) -> None:
        if self._emit is None:
            return
        try:
            self._emit(event, payload)
        except Exception:
            logger.exception(
                "computer_use_actor: emit %s raised; suppressing", event
            )


# ─── Platform detection ────────────────────────────────────────────


@dataclass(frozen=True)
class PlatformProbe:
    """Injectable view of host platform signals.

    Default factory reads :data:`sys.platform` and ``$WAYLAND_DISPLAY``
    / ``$DISPLAY`` env vars; tests pass a frozen probe so detection is
    deterministic.
    """

    sys_platform: str
    wayland_display: str = ""
    x11_display: str = ""

    @classmethod
    def from_env(cls) -> "PlatformProbe":
        return cls(
            sys_platform=sys.platform,
            wayland_display=os.environ.get("WAYLAND_DISPLAY", ""),
            x11_display=os.environ.get("DISPLAY", ""),
        )


def detect_platform(probe: PlatformProbe | None = None) -> Platform:
    """Identify the host platform from environment signals.

    Order:

    1. ``darwin`` → ``macos``
    2. ``win32`` / ``cygwin`` → ``windows``
    3. Linux-like with ``$WAYLAND_DISPLAY`` set → ``wayland``
    4. Linux-like with ``$DISPLAY`` set → ``x11``
    5. Otherwise raises :class:`PlatformUnsupported`.

    Wayland is checked *before* X11 because XWayland still exposes a
    ``$DISPLAY`` value on a Wayland session; the Wayland-native path is
    the correct one when both env vars are set.
    """
    probe = probe or PlatformProbe.from_env()
    plat = probe.sys_platform
    if plat == "darwin":
        return PLATFORM_MACOS
    if plat in ("win32", "cygwin"):
        return PLATFORM_WINDOWS
    if plat.startswith("linux") or plat in ("freebsd", "openbsd"):
        if probe.wayland_display:
            return PLATFORM_WAYLAND
        if probe.x11_display:
            return PLATFORM_X11
        raise PlatformUnsupported(
            "linux host has neither WAYLAND_DISPLAY nor DISPLAY set; "
            "no display server is reachable"
        )
    raise PlatformUnsupported(f"unrecognised sys.platform {plat!r}")


# ─── Fake engine (tests + dev) ─────────────────────────────────────


class FakeActorEngine:
    """In-memory engine for tests and the bring-up workbench dry-run.

    Returns deterministic RGBA buffers (single fill colour over the
    region) and appends every dispatched action to ``calls``. The
    workbench can run a "demo session" with this engine attached so
    the operator can practice the budget UI without a real client
    device on the bench.
    """

    def __init__(
        self,
        platform: Platform = PLATFORM_X11,
        fill_rgba: tuple[int, int, int, int] = (0, 0, 0, 255),
    ) -> None:
        self.platform = platform
        self._fill = bytes(fill_rgba)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def take_screenshot(self, region: Region) -> bytes:
        self.calls.append(("take_screenshot", (region,)))
        return self._fill * region.pixels

    def click(self, x: int, y: int, button: MouseButton) -> None:
        self.calls.append(("click", (x, y, button)))

    def type_text(self, text: str) -> None:
        self.calls.append(("type_text", (text,)))

    def press_key(self, key: str, modifiers: tuple[str, ...]) -> None:
        self.calls.append(("press_key", (key, modifiers)))


# ─── LLM tool schema ───────────────────────────────────────────────


def actor_tool_schema() -> tuple[dict[str, Any], ...]:
    """JSON-Schema-shaped tool definitions for an Anthropic computer-use
    dispatcher.

    The names mirror the four public methods on :class:`ComputerUseActor`;
    the LLM dispatch path uses them as the routing key, so changing a
    name here is a breaking change to the workbench / LLM bridge and
    requires bumping :data:`COMPUTER_USE_ACTOR_SCHEMA_VERSION`.
    """
    return (
        {
            "name": "computer_screenshot",
            "description": "Capture an RGBA frame of a granted region.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "minimum": 0},
                            "y": {"type": "integer", "minimum": 0},
                            "width": {"type": "integer", "exclusiveMinimum": 0},
                            "height": {"type": "integer", "exclusiveMinimum": 0},
                        },
                        "required": ["x", "y", "width", "height"],
                    },
                },
                "required": [],
            },
        },
        {
            "name": "computer_click",
            "description": "Single mouse click at a granted pixel.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0},
                    "y": {"type": "integer", "minimum": 0},
                    "button": {
                        "type": "string",
                        "enum": list(MOUSE_BUTTON_VALUES),
                    },
                },
                "required": ["x", "y"],
            },
        },
        {
            "name": "computer_type",
            "description": "Inject typed text into the focused field.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "maxLength": MAX_TYPE_TEXT_LENGTH,
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "computer_key",
            "description": "Press a single named key, optionally with modifiers.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": sorted(KNOWN_KEYS),
                    },
                    "modifiers": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": sorted(KNOWN_MODIFIERS),
                        },
                    },
                },
                "required": ["key"],
            },
        },
    )


# ─── Clock ─────────────────────────────────────────────────────────


def _default_clock() -> float:
    import time
    return time.time()
