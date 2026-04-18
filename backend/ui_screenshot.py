"""V2 #3 (issue #318) — Playwright headless screenshot service.

Turns a running dev-server (spawned by :mod:`backend.ui_sandbox` +
policy-wrapped by :mod:`backend.ui_sandbox_lifecycle`) into
deterministic PNG captures the agent can look at.

Where this sits in the V2 stack
--------------------------------

V2 #1 (``ui_sandbox.py``) brings the Next.js dev server up inside a
Docker container and exposes a ``preview_url``.

V2 #2 (``ui_sandbox_lifecycle.py``) orchestrates the session (ensure /
hot-reload / teardown / idle-reap) and exposes a ``screenshot_hook``
injection point.  V2 #2 deliberately does **not** import Playwright —
the hook signature is the boundary, and this module provides the
real implementation behind that boundary.

V2 #3 (this module) is the Playwright side of that hook:

  * **on-demand capture** — ``ScreenshotService.capture(session_id,
    preview_url, viewport=, path=)`` returns a
    :class:`ScreenshotCapture` record (PNG bytes + metadata).
  * **periodic capture** — ``start_periodic`` spins a daemon thread
    that invokes ``capture`` every ``interval_s``; ``stop_periodic``
    reaps it cleanly.  V2 row 6 (SSE bus) will subscribe to the
    emitted events to push live previews to the UI.
  * **as_hook()** — exposes a callable matching the
    :class:`~backend.ui_sandbox_lifecycle.ScreenshotHook` Protocol so
    callers can drop this service straight into ``SandboxLifecycle(
    screenshot_hook=service.as_hook())``.

Responsive viewport matrix
--------------------------

The three viewport presets (``desktop`` 1440×900 / ``tablet`` 768×1024
/ ``mobile`` 375×812) are defined here so row 4 ("three-viewport
batch capture") is a thin wrapper around ``capture(viewport=...)``
rather than architectural churn.  The module itself only exposes
single-viewport capture — batch capture is V2 row 4 scope.

Design decisions
----------------

* **Playwright is an injectable engine, not a hard import.**  The
  :class:`ScreenshotEngine` Protocol defines ``capture(request) ->
  bytes``.  :class:`PlaywrightEngine` lazy-imports
  :mod:`playwright.sync_api` inside ``__init__`` — absent, it raises
  :class:`PlaywrightUnavailable` with install guidance.  Tests
  provide a :class:`FakeScreenshotEngine` that returns canned PNG
  bytes; the module under test never touches a real browser.
* **Engine vs service split.**  Engine does the mechanical "take one
  screenshot with these params"; service layers policy on top:
  viewport resolution, PNG validation, in-memory history, periodic
  loop, SSE event emission, thread-safe locking.  This mirrors the
  V2 #1 primitives-vs-V2 #2 policy split.
* **Deterministic time.**  Captured timestamps come from an injected
  ``clock=`` callable; periodic loop waits on
  ``threading.Event.wait(timeout=)`` so ``stop_periodic`` returns
  promptly even with a 60-second interval.
* **Bounded history.**  Per-session captures are held in a
  ``deque(maxlen=capture_history_size)`` so periodic capture can run
  forever without unbounded memory growth.  The public
  :meth:`recent` / :meth:`latest` accessors return immutable tuples.
* **PNG signature validation.**  :func:`validate_png_bytes` guards
  against hooks that return JPEG / empty / text.  Bad bytes surface
  as :class:`InvalidPngData` rather than silently getting into an SSE
  payload or multimodal message.
* **No SandboxManager dependency.**  This module captures what the
  caller tells it to capture — it does not reach back into the
  manager to discover preview URLs.  The lifecycle module already
  owns that resolution; decoupling here keeps the module testable
  without docker *or* playwright.

Contract (pinned by ``backend/tests/test_ui_screenshot.py``)
------------------------------------------------------------

* :data:`UI_SCREENSHOT_SCHEMA_VERSION` is semver; bump on shape
  changes to :class:`ScreenshotCapture.to_dict()` /
  :class:`Viewport.to_dict()` / :meth:`ScreenshotService.snapshot`.
* :data:`VIEWPORT_PRESETS` pins the three viewport specs the V2
  matrix requires — desktop / tablet / mobile with the exact pixel
  dimensions V2 row 4 spec calls out.
* :func:`get_viewport` resolves a name to a :class:`Viewport` or
  raises :class:`ViewportUnknown`.
* :func:`build_target_url` joins preview_url + path safely (leading
  slash, no double-slashes, no path-traversal fragments).
* :func:`validate_png_bytes` raises :class:`InvalidPngData` for
  empty / missing-signature / too-large inputs.
* :func:`encode_png_base64` is a pure helper for V2 row 5 multimodal
  injection.
* :class:`ScreenshotService.capture` emits a
  ``ui_sandbox.screenshot`` event with the capture payload.
* :class:`ScreenshotService.start_periodic` is single-instance per
  session and joinable via :meth:`stop_periodic`.
* :meth:`ScreenshotService.as_hook` returns a callable with the
  exact shape the lifecycle ``ScreenshotHook`` Protocol expects.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


__all__ = [
    "UI_SCREENSHOT_SCHEMA_VERSION",
    "DEFAULT_VIEWPORT",
    "DEFAULT_CAPTURE_TIMEOUT_S",
    "DEFAULT_NAVIGATION_TIMEOUT_MS",
    "DEFAULT_WAIT_UNTIL",
    "DEFAULT_PERIODIC_INTERVAL_S",
    "DEFAULT_HISTORY_SIZE",
    "MAX_CAPTURE_BYTES",
    "PNG_SIGNATURE",
    "VIEWPORT_DESKTOP",
    "VIEWPORT_TABLET",
    "VIEWPORT_MOBILE",
    "VIEWPORT_PRESETS",
    "SCREENSHOT_EVENT_CAPTURED",
    "SCREENSHOT_EVENT_PERIODIC_STARTED",
    "SCREENSHOT_EVENT_PERIODIC_STOPPED",
    "SCREENSHOT_EVENT_FAILED",
    "SCREENSHOT_EVENT_TYPES",
    "Viewport",
    "ScreenshotRequest",
    "ScreenshotCapture",
    "ScreenshotEngine",
    "PlaywrightEngine",
    "ScreenshotService",
    "ScreenshotError",
    "PlaywrightUnavailable",
    "ViewportUnknown",
    "CaptureTimeout",
    "InvalidPngData",
    "PeriodicAlreadyRunning",
    "get_viewport",
    "list_viewports",
    "build_target_url",
    "validate_png_bytes",
    "encode_png_base64",
]


#: Bump on any shape change to :class:`ScreenshotCapture.to_dict()`,
#: :class:`Viewport.to_dict()`, or :meth:`ScreenshotService.snapshot`.
UI_SCREENSHOT_SCHEMA_VERSION = "1.0.0"

#: Canonical PNG signature — every PNG starts with these eight bytes.
#: Used by :func:`validate_png_bytes` to reject non-PNG engine output.
PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"

#: Default viewport name when callers don't specify one. Desktop
#: matches the 1440×900 target the V2 spec uses as primary reference.
DEFAULT_VIEWPORT = "desktop"

#: Hard ceiling on a single screenshot.  Next.js dev-server pages at
#: 1440×900 come in well under 2 MB; 10 MB leaves headroom for future
#: full-page captures while capping runaway output before it pollutes
#: SSE payloads or multimodal messages.
MAX_CAPTURE_BYTES = 10_000_000

#: Cap on how long one ``capture`` will wait for the Playwright page
#: to settle.  30 s is enough for a cold Next.js dev-server page on
#: the Alpine base image V2 #1 ships.
DEFAULT_CAPTURE_TIMEOUT_S = 30.0

#: Navigation timeout handed to Playwright's ``page.goto``.  Playwright
#: takes milliseconds at this layer so we keep the native unit.
DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000

#: Playwright ``wait_until`` strategy — ``load`` is more resilient
#: than ``networkidle`` on HMR-heavy dev servers that never actually
#: go idle because of the HMR websocket heartbeat.
DEFAULT_WAIT_UNTIL = "load"

#: Default sweep period for :meth:`ScreenshotService.start_periodic`.
#: 5 s gives the UI a smooth preview refresh without hammering the
#: dev-server; callers can override for faster / slower cadence.
DEFAULT_PERIODIC_INTERVAL_S = 5.0

#: Max retained captures per session in the in-memory ring buffer.
#: 32 covers "last 2.5 minutes at 5 s cadence" — plenty for SSE
#: replay and debugging hiccups without unbounded RAM growth.
DEFAULT_HISTORY_SIZE = 32


# ───────────────────────────────────────────────────────────────────
#  Viewport presets
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Viewport:
    """Frozen viewport spec — pixel dimensions + device hints.

    ``device_scale_factor`` and ``is_mobile`` map straight to
    Playwright's ``new_context`` kwargs; the ``user_agent`` gives
    servers a hint about the target form factor so any UA-sniffing
    code path renders the mobile/tablet layout.
    """

    name: str
    width: int
    height: int
    device_scale_factor: float = 1.0
    is_mobile: bool = False
    user_agent: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("viewport name must be non-empty")
        if not _VIEWPORT_NAME_RE.fullmatch(self.name):
            raise ValueError(
                "viewport name must match [a-z0-9_-]{1,32} — got "
                f"{self.name!r}"
            )
        if not isinstance(self.width, int) or self.width < 1:
            raise ValueError("width must be a positive int")
        if not isinstance(self.height, int) or self.height < 1:
            raise ValueError("height must be a positive int")
        if self.device_scale_factor <= 0:
            raise ValueError("device_scale_factor must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_SCREENSHOT_SCHEMA_VERSION,
            "name": self.name,
            "width": int(self.width),
            "height": int(self.height),
            "device_scale_factor": float(self.device_scale_factor),
            "is_mobile": bool(self.is_mobile),
            "user_agent": self.user_agent,
        }


_VIEWPORT_NAME_RE = re.compile(r"[a-z0-9_-]{1,32}")


#: Desktop preset — 1440×900 is the V2 spec primary reference width.
VIEWPORT_DESKTOP = Viewport(
    name="desktop",
    width=1440,
    height=900,
    device_scale_factor=1.0,
    is_mobile=False,
    user_agent=(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
)

#: Tablet preset — iPad-class 768×1024 portrait.
VIEWPORT_TABLET = Viewport(
    name="tablet",
    width=768,
    height=1024,
    device_scale_factor=2.0,
    is_mobile=True,
    user_agent=(
        "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
)

#: Mobile preset — iPhone X-class 375×812 portrait.
VIEWPORT_MOBILE = Viewport(
    name="mobile",
    width=375,
    height=812,
    device_scale_factor=3.0,
    is_mobile=True,
    user_agent=(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
)

#: Canonical preset registry keyed by viewport name.  V2 row 4 iterates
#: over this when producing the three-viewport capture matrix.
VIEWPORT_PRESETS: Mapping[str, Viewport] = {
    VIEWPORT_DESKTOP.name: VIEWPORT_DESKTOP,
    VIEWPORT_TABLET.name: VIEWPORT_TABLET,
    VIEWPORT_MOBILE.name: VIEWPORT_MOBILE,
}


def list_viewports() -> tuple[str, ...]:
    """Names of registered viewport presets, stable ordering."""

    return tuple(VIEWPORT_PRESETS.keys())


def get_viewport(name: str) -> Viewport:
    """Resolve a preset name to a :class:`Viewport` or raise
    :class:`ViewportUnknown`.

    Names are matched case-insensitively to accept callers passing
    ``"Desktop"`` / ``"MOBILE"`` alongside the canonical lowercase
    keys.
    """

    if not isinstance(name, str) or not name.strip():
        raise ViewportUnknown(f"viewport name must be non-empty, got {name!r}")
    key = name.strip().lower()
    if key not in VIEWPORT_PRESETS:
        raise ViewportUnknown(
            f"unknown viewport {name!r} — known: {sorted(VIEWPORT_PRESETS)}"
        )
    return VIEWPORT_PRESETS[key]


# ───────────────────────────────────────────────────────────────────
#  Events
# ───────────────────────────────────────────────────────────────────


SCREENSHOT_EVENT_CAPTURED = "ui_sandbox.screenshot"
SCREENSHOT_EVENT_PERIODIC_STARTED = "ui_sandbox.screenshot.periodic_started"
SCREENSHOT_EVENT_PERIODIC_STOPPED = "ui_sandbox.screenshot.periodic_stopped"
SCREENSHOT_EVENT_FAILED = "ui_sandbox.screenshot.failed"

#: Full roster of events this module emits — mirrors the pattern in
#: :mod:`backend.ui_sandbox_lifecycle` so the SSE bus (V2 row 6) can
#: enumerate topics deterministically.
SCREENSHOT_EVENT_TYPES: tuple[str, ...] = (
    SCREENSHOT_EVENT_CAPTURED,
    SCREENSHOT_EVENT_PERIODIC_STARTED,
    SCREENSHOT_EVENT_PERIODIC_STOPPED,
    SCREENSHOT_EVENT_FAILED,
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class ScreenshotError(RuntimeError):
    """Base class for ``ui_screenshot`` errors."""


class PlaywrightUnavailable(ScreenshotError):
    """Raised by :class:`PlaywrightEngine` when ``playwright.sync_api``
    cannot be imported.  Message includes the install hint so the
    agent loop can relay guidance to the operator."""


class ViewportUnknown(ScreenshotError):
    """Raised when :func:`get_viewport` can't resolve a preset name."""


class CaptureTimeout(ScreenshotError):
    """Raised when the engine exceeds ``timeout_s`` mid-capture."""


class InvalidPngData(ScreenshotError):
    """Raised by :func:`validate_png_bytes` for empty / wrong-signature
    / oversized capture payloads."""


class PeriodicAlreadyRunning(ScreenshotError):
    """Raised by :meth:`ScreenshotService.start_periodic` when a
    periodic loop is already live for the given session."""


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def build_target_url(preview_url: str, path: str = "/") -> str:
    """Join ``preview_url`` + ``path`` into a browsing URL.

    * Preserves scheme + host + port from ``preview_url``.
    * ``path`` must start with ``/`` — callers pass route paths, not
      raw queries.  Empty / missing leading slash raises ValueError.
    * Existing query / fragment on ``preview_url`` is stripped — V2's
      preview surfaces are always clean origins.
    * Trailing slashes on ``preview_url`` are normalised so we never
      emit ``http://x//page``.
    """

    if not isinstance(preview_url, str) or not preview_url.strip():
        raise ValueError("preview_url must be non-empty")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must start with '/'")
    if ".." in path.split("/"):
        raise ValueError("path must not contain '..' segments")

    parts = urlsplit(preview_url.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"preview_url missing scheme/host: {preview_url!r}")
    # Strip any trailing slash on the base path so we don't double it.
    base_path = parts.path.rstrip("/")
    joined = base_path + path
    return urlunsplit((parts.scheme, parts.netloc, joined, "", ""))


def validate_png_bytes(
    data: bytes | bytearray,
    *,
    max_bytes: int = MAX_CAPTURE_BYTES,
) -> None:
    """Assert ``data`` is a non-empty PNG under ``max_bytes`` bytes.

    Raises :class:`InvalidPngData` otherwise.  Pure function — does
    not mutate or allocate beyond the signature slice.
    """

    if not isinstance(data, (bytes, bytearray)):
        raise InvalidPngData(
            f"expected bytes, got {type(data).__name__}"
        )
    if not data:
        raise InvalidPngData("screenshot bytes are empty")
    if len(data) > max_bytes:
        raise InvalidPngData(
            f"screenshot too large: {len(data)} > {max_bytes} bytes"
        )
    if bytes(data[: len(PNG_SIGNATURE)]) != PNG_SIGNATURE:
        raise InvalidPngData("screenshot missing PNG signature")


def encode_png_base64(data: bytes | bytearray) -> str:
    """Encode PNG bytes to an ASCII base64 string — no data URL prefix.

    Callers that need a browser-ready ``data:image/png;base64,…`` URL
    prepend the prefix themselves (V2 row 5 multimodal injection just
    wants the raw base64 payload).
    """

    validate_png_bytes(data)
    return base64.b64encode(bytes(data)).decode("ascii")


# ───────────────────────────────────────────────────────────────────
#  Request + capture records
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScreenshotRequest:
    """Input contract for :class:`ScreenshotEngine.capture`.

    Frozen + deterministic — same values produce the same Playwright
    invocation so tests can assert on the exact request shape.
    """

    session_id: str
    preview_url: str
    viewport: Viewport
    path: str = "/"
    full_page: bool = False
    wait_until: str = DEFAULT_WAIT_UNTIL
    timeout_s: float = DEFAULT_CAPTURE_TIMEOUT_S
    navigation_timeout_ms: int = DEFAULT_NAVIGATION_TIMEOUT_MS

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not isinstance(self.preview_url, str) or not self.preview_url.strip():
            raise ValueError("preview_url must be non-empty")
        if not isinstance(self.viewport, Viewport):
            raise ValueError("viewport must be a Viewport instance")
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError("path must start with '/'")
        if self.wait_until not in ("load", "domcontentloaded", "networkidle"):
            raise ValueError(
                f"wait_until must be load/domcontentloaded/networkidle, got "
                f"{self.wait_until!r}"
            )
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.navigation_timeout_ms <= 0:
            raise ValueError("navigation_timeout_ms must be positive")

    @property
    def target_url(self) -> str:
        """Fully-resolved URL the engine should navigate to."""

        return build_target_url(self.preview_url, self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_SCREENSHOT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "preview_url": self.preview_url,
            "viewport": self.viewport.to_dict(),
            "path": self.path,
            "full_page": bool(self.full_page),
            "wait_until": self.wait_until,
            "timeout_s": float(self.timeout_s),
            "navigation_timeout_ms": int(self.navigation_timeout_ms),
            "target_url": self.target_url,
        }


@dataclass(frozen=True)
class ScreenshotCapture:
    """Result of one capture — PNG bytes + metadata + timing.

    Shape is JSON-safe via :meth:`to_dict`; PNG bytes only surface
    when callers explicitly pass ``include_bytes=True`` (V2 row 5
    multimodal injection).  The default payload is what SSE (V2 row
    6) receives — metadata + byte length, not the raw PNG.
    """

    session_id: str
    preview_url: str
    viewport: Viewport
    path: str
    image_bytes: bytes
    captured_at: float
    duration_ms: float = 0.0
    target_url: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not isinstance(self.preview_url, str) or not self.preview_url.strip():
            raise ValueError("preview_url must be non-empty")
        if not isinstance(self.viewport, Viewport):
            raise ValueError("viewport must be a Viewport instance")
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError("path must start with '/'")
        if not isinstance(self.image_bytes, (bytes, bytearray)):
            raise ValueError("image_bytes must be bytes")
        if not self.image_bytes:
            raise ValueError("image_bytes must be non-empty")
        if self.captured_at < 0:
            raise ValueError("captured_at must be non-negative")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        # Coerce bytearray → immutable bytes so the record stays frozen
        # in spirit as well as by dataclass decoration.
        if isinstance(self.image_bytes, bytearray):
            object.__setattr__(self, "image_bytes", bytes(self.image_bytes))
        if not self.target_url:
            object.__setattr__(
                self, "target_url", build_target_url(self.preview_url, self.path)
            )

    @property
    def byte_len(self) -> int:
        return len(self.image_bytes)

    def to_dict(self, *, include_bytes: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": UI_SCREENSHOT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "preview_url": self.preview_url,
            "viewport": self.viewport.to_dict(),
            "path": self.path,
            "target_url": self.target_url,
            "byte_len": self.byte_len,
            "captured_at": float(self.captured_at),
            "duration_ms": float(self.duration_ms),
        }
        if include_bytes:
            out["image_base64"] = encode_png_base64(self.image_bytes)
        return out

    def to_data_url(self) -> str:
        """``data:image/png;base64,…`` URL — useful for embedding
        captures directly in HTML previews without a separate fetch."""

        return f"data:image/png;base64,{encode_png_base64(self.image_bytes)}"


# ───────────────────────────────────────────────────────────────────
#  Engine protocol + Playwright engine
# ───────────────────────────────────────────────────────────────────


class ScreenshotEngine(Protocol):
    """Callable contract for the thing that actually drives a browser.

    Implementations MUST be thread-safe — the periodic-capture thread
    and on-demand capture callers may invoke ``capture`` concurrently.
    :class:`PlaywrightEngine` serialises internally; tests plug in
    ``FakeScreenshotEngine`` stubs.
    """

    def capture(self, request: ScreenshotRequest) -> bytes:
        ...

    def close(self) -> None:
        ...


#: Signature of the optional Playwright-launcher seam used by
#: :class:`PlaywrightEngine` in tests — accepts no args, returns an
#: object matching ``playwright.sync_api.Playwright`` with a
#: ``.chromium.launch(headless=...)`` chain.
PlaywrightLauncher = Callable[[], Any]


class PlaywrightEngine:
    """Production :class:`ScreenshotEngine` backed by Playwright.

    Playwright is imported lazily in ``__init__`` — absent, raises
    :class:`PlaywrightUnavailable` with install guidance.  One
    browser is launched per engine and pages are spawned per
    capture (cheap; reusing pages across viewports gets flaky when
    device-scale-factor toggles between tablet/mobile contexts).

    The engine serialises capture() calls with an internal lock
    because a single Playwright browser instance is not concurrency-
    safe across threads; the service layer above knows this and does
    not try to parallelise.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        channel: str | None = None,
        launcher: PlaywrightLauncher | None = None,
    ) -> None:
        self._headless = bool(headless)
        self._channel = channel
        self._lock = threading.Lock()
        self._pw: Any = None
        self._browser: Any = None

        if launcher is not None:
            pw = launcher()
        else:
            try:
                from playwright.sync_api import sync_playwright  # type: ignore
            except Exception as exc:  # pragma: no cover - import surface
                raise PlaywrightUnavailable(
                    "playwright is not installed — run "
                    "`pip install playwright && playwright install chromium` "
                    "to enable the real screenshot engine; tests may "
                    "inject a FakeScreenshotEngine instead"
                ) from exc
            pw = sync_playwright().start()

        try:
            launch_kwargs: dict[str, Any] = {"headless": self._headless}
            if self._channel:
                launch_kwargs["channel"] = self._channel
            browser = pw.chromium.launch(**launch_kwargs)
        except Exception as exc:
            # Roll back the playwright start() if launch blew up.
            try:
                pw.stop()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            raise ScreenshotError(f"failed to launch chromium: {exc}") from exc

        self._pw = pw
        self._browser = browser

    def capture(self, request: ScreenshotRequest) -> bytes:
        if not isinstance(request, ScreenshotRequest):
            raise TypeError("request must be a ScreenshotRequest")
        if self._browser is None:
            raise ScreenshotError("engine is closed")

        vp = request.viewport
        with self._lock:
            context = self._browser.new_context(
                viewport={"width": vp.width, "height": vp.height},
                device_scale_factor=vp.device_scale_factor,
                is_mobile=vp.is_mobile,
                user_agent=vp.user_agent,
            )
            try:
                page = context.new_page()
                page.set_default_navigation_timeout(request.navigation_timeout_ms)
                page.set_default_timeout(request.navigation_timeout_ms)
                try:
                    page.goto(request.target_url, wait_until=request.wait_until)
                except Exception as exc:
                    # Playwright surfaces TimeoutError from a private
                    # submodule; wrap anything that looks like a
                    # timeout so the service layer can emit a clean
                    # failed event.
                    if "Timeout" in type(exc).__name__:
                        raise CaptureTimeout(
                            f"navigation timed out for {request.target_url!r}: {exc}"
                        ) from exc
                    raise ScreenshotError(
                        f"navigation failed for {request.target_url!r}: {exc}"
                    ) from exc
                try:
                    png = page.screenshot(
                        full_page=bool(request.full_page),
                        type="png",
                    )
                except Exception as exc:
                    raise ScreenshotError(f"screenshot failed: {exc}") from exc
            finally:
                try:
                    context.close()
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass
        if not isinstance(png, (bytes, bytearray)):
            raise ScreenshotError(
                f"playwright returned {type(png).__name__}, expected bytes"
            )
        return bytes(png)

    def close(self) -> None:
        with self._lock:
            browser, pw = self._browser, self._pw
            self._browser = None
            self._pw = None
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning("browser close failed: %s", exc)
        if pw is not None:
            try:
                pw.stop()
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning("playwright stop failed: %s", exc)

    # Support ``with PlaywrightEngine() as e:`` idiom so CLI / ad-hoc
    # callers don't leak browser processes.
    def __enter__(self) -> "PlaywrightEngine":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ───────────────────────────────────────────────────────────────────
#  Periodic-loop internal state
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


@dataclass
class _PeriodicState:
    """Tracks one session's background capture loop.  Mutable — held
    behind :attr:`ScreenshotService._lock`."""

    thread: threading.Thread
    stop_event: threading.Event
    preview_url: str
    viewport: Viewport
    path: str
    interval_s: float
    started_at: float
    sweeps: int = 0
    failures: int = 0


# ───────────────────────────────────────────────────────────────────
#  Service
# ───────────────────────────────────────────────────────────────────


class ScreenshotService:
    """Policy wrapper around a :class:`ScreenshotEngine`.

    Responsibilities:

      * resolve ``viewport`` names to :class:`Viewport` presets;
      * validate PNG bytes returned by the engine;
      * maintain a bounded per-session capture history for SSE
        replay / debugging;
      * manage on-demand and periodic capture lifecycles;
      * emit events for every capture + lifecycle transition;
      * expose :meth:`as_hook` so the lifecycle module can plug us
        in directly as its ``screenshot_hook=``.

    Thread-safety: all public methods take an internal ``RLock``.
    The underlying engine serialises :meth:`capture` calls itself,
    so callers can safely fire on-demand captures from the agent
    loop while a periodic thread is running.
    """

    def __init__(
        self,
        *,
        engine: ScreenshotEngine,
        clock: Callable[[], float] = time.time,
        event_cb: EventCallback | None = None,
        default_viewport: str = DEFAULT_VIEWPORT,
        history_size: int = DEFAULT_HISTORY_SIZE,
        capture_timeout_s: float = DEFAULT_CAPTURE_TIMEOUT_S,
        navigation_timeout_ms: int = DEFAULT_NAVIGATION_TIMEOUT_MS,
        wait_until: str = DEFAULT_WAIT_UNTIL,
        periodic_interval_s: float = DEFAULT_PERIODIC_INTERVAL_S,
    ) -> None:
        if engine is None:
            raise TypeError("engine must be a ScreenshotEngine")
        if not hasattr(engine, "capture"):
            raise TypeError("engine must implement .capture(request)")
        if history_size < 1:
            raise ValueError("history_size must be >= 1")
        if capture_timeout_s <= 0:
            raise ValueError("capture_timeout_s must be positive")
        if navigation_timeout_ms <= 0:
            raise ValueError("navigation_timeout_ms must be positive")
        if wait_until not in ("load", "domcontentloaded", "networkidle"):
            raise ValueError("wait_until must be load/domcontentloaded/networkidle")
        if periodic_interval_s <= 0:
            raise ValueError("periodic_interval_s must be positive")
        # validate default_viewport now so bad config surfaces at
        # construction, not on first capture.
        get_viewport(default_viewport)

        self._engine = engine
        self._clock = clock
        self._event_cb = event_cb
        self._default_viewport = default_viewport.strip().lower()
        self._history_size = int(history_size)
        self._capture_timeout_s = float(capture_timeout_s)
        self._navigation_timeout_ms = int(navigation_timeout_ms)
        self._wait_until = wait_until
        self._periodic_interval_s = float(periodic_interval_s)

        self._lock = threading.RLock()
        self._history: dict[str, Deque[ScreenshotCapture]] = {}
        self._periodic: dict[str, _PeriodicState] = {}
        self._capture_count = 0
        self._failure_count = 0

    # ─────────────── Public accessors ───────────────

    @property
    def engine(self) -> ScreenshotEngine:
        return self._engine

    @property
    def default_viewport(self) -> str:
        return self._default_viewport

    @property
    def history_size(self) -> int:
        return self._history_size

    def capture_count(self) -> int:
        with self._lock:
            return self._capture_count

    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def sessions_with_history(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._history.keys()))

    # ─────────────── Core capture ───────────────

    def capture(
        self,
        *,
        session_id: str,
        preview_url: str,
        viewport: str | Viewport | None = None,
        path: str = "/",
        full_page: bool = False,
        timeout_s: float | None = None,
    ) -> ScreenshotCapture:
        """Take one screenshot and record it in the session history.

        Emits :data:`SCREENSHOT_EVENT_CAPTURED` on success or
        :data:`SCREENSHOT_EVENT_FAILED` on engine error (the error is
        re-raised — the failed event exists so SSE subscribers can
        surface it even before the exception propagates to the agent
        loop).
        """

        request = self._build_request(
            session_id=session_id,
            preview_url=preview_url,
            viewport=viewport,
            path=path,
            full_page=full_page,
            timeout_s=timeout_s,
        )
        started = self._clock()
        try:
            png = self._engine.capture(request)
        except ScreenshotError:
            with self._lock:
                self._failure_count += 1
            self._emit_failure(request, reason="engine_error")
            raise
        except Exception as exc:
            with self._lock:
                self._failure_count += 1
            self._emit_failure(request, reason=f"unexpected:{type(exc).__name__}")
            raise ScreenshotError(
                f"engine raised unexpectedly: {exc}"
            ) from exc

        try:
            validate_png_bytes(png)
        except InvalidPngData:
            with self._lock:
                self._failure_count += 1
            self._emit_failure(request, reason="invalid_png")
            raise

        finished = self._clock()
        capture = ScreenshotCapture(
            session_id=request.session_id,
            preview_url=request.preview_url,
            viewport=request.viewport,
            path=request.path,
            image_bytes=bytes(png),
            captured_at=finished,
            duration_ms=max(0.0, (finished - started) * 1000.0),
            target_url=request.target_url,
        )

        with self._lock:
            self._capture_count += 1
            buf = self._history.setdefault(
                request.session_id,
                deque(maxlen=self._history_size),
            )
            buf.append(capture)

        self._emit(SCREENSHOT_EVENT_CAPTURED, capture.to_dict())
        return capture

    def _build_request(
        self,
        *,
        session_id: str,
        preview_url: str,
        viewport: str | Viewport | None,
        path: str,
        full_page: bool,
        timeout_s: float | None,
    ) -> ScreenshotRequest:
        if viewport is None:
            vp = get_viewport(self._default_viewport)
        elif isinstance(viewport, Viewport):
            vp = viewport
        elif isinstance(viewport, str):
            vp = get_viewport(viewport)
        else:
            raise TypeError(
                f"viewport must be str, Viewport, or None, got "
                f"{type(viewport).__name__}"
            )
        timeout = (
            float(timeout_s) if timeout_s is not None else self._capture_timeout_s
        )
        return ScreenshotRequest(
            session_id=session_id,
            preview_url=preview_url,
            viewport=vp,
            path=path,
            full_page=bool(full_page),
            wait_until=self._wait_until,
            timeout_s=timeout,
            navigation_timeout_ms=self._navigation_timeout_ms,
        )

    # ─────────────── Hook adapter ───────────────

    def as_hook(self) -> Callable[..., bytes]:
        """Return a callable matching the
        :class:`backend.ui_sandbox_lifecycle.ScreenshotHook` Protocol.

        Usage::

            service = ScreenshotService(engine=PlaywrightEngine())
            lifecycle = SandboxLifecycle(
                manager=mgr, screenshot_hook=service.as_hook()
            )
        """

        def hook(
            *,
            session_id: str,
            preview_url: str,
            viewport: str,
            path: str,
        ) -> bytes:
            capture = self.capture(
                session_id=session_id,
                preview_url=preview_url,
                viewport=viewport,
                path=path,
            )
            return capture.image_bytes

        return hook

    # ─────────────── History accessors ───────────────

    def latest(self, session_id: str) -> ScreenshotCapture | None:
        """Most recent capture for ``session_id`` or ``None`` if none."""

        with self._lock:
            buf = self._history.get(session_id)
            return buf[-1] if buf else None

    def recent(
        self, session_id: str, *, limit: int | None = None
    ) -> tuple[ScreenshotCapture, ...]:
        """Return up to ``limit`` most recent captures for ``session_id``.

        Ordered oldest-to-newest so SSE replay can stream in natural
        timeline order.  ``limit=None`` returns the full buffer.
        """

        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0")
        with self._lock:
            buf = self._history.get(session_id)
            if not buf:
                return ()
            items = tuple(buf)
        if limit is None or limit >= len(items):
            return items
        return items[-limit:] if limit > 0 else ()

    def clear_history(self, session_id: str | None = None) -> int:
        """Clear captured history.  ``session_id=None`` clears all.
        Returns the count of removed captures."""

        with self._lock:
            if session_id is None:
                removed = sum(len(buf) for buf in self._history.values())
                self._history.clear()
                return removed
            buf = self._history.pop(session_id, None)
            return len(buf) if buf else 0

    # ─────────────── Periodic capture ───────────────

    def start_periodic(
        self,
        *,
        session_id: str,
        preview_url: str,
        viewport: str | Viewport | None = None,
        path: str = "/",
        interval_s: float | None = None,
    ) -> None:
        """Spawn a daemon thread that captures every ``interval_s``.

        Single-instance per session — raises
        :class:`PeriodicAlreadyRunning` on the second call.  Use
        :meth:`stop_periodic` then re-start to change parameters.
        """

        if interval_s is not None and interval_s <= 0:
            raise ValueError("interval_s must be positive")
        period = float(interval_s) if interval_s is not None else self._periodic_interval_s

        if viewport is None:
            vp = get_viewport(self._default_viewport)
        elif isinstance(viewport, Viewport):
            vp = viewport
        else:
            vp = get_viewport(viewport)

        with self._lock:
            existing = self._periodic.get(session_id)
            if existing is not None and existing.thread.is_alive():
                raise PeriodicAlreadyRunning(
                    f"periodic capture already running for {session_id!r}"
                )
            stop = threading.Event()
            # State entry goes into the dict *before* the thread
            # starts so the loop sees a consistent view of sweeps=0.
            state = _PeriodicState(
                thread=None,  # set below
                stop_event=stop,
                preview_url=preview_url,
                viewport=vp,
                path=path,
                interval_s=period,
                started_at=self._clock(),
            )
            thread = threading.Thread(
                target=self._periodic_loop,
                args=(session_id,),
                name=f"ui-screenshot-periodic-{session_id}",
                daemon=True,
            )
            state.thread = thread
            self._periodic[session_id] = state
            thread.start()

        self._emit(
            SCREENSHOT_EVENT_PERIODIC_STARTED,
            {
                "schema_version": UI_SCREENSHOT_SCHEMA_VERSION,
                "session_id": session_id,
                "preview_url": preview_url,
                "viewport": vp.to_dict(),
                "path": path,
                "interval_s": period,
                "started_at": float(state.started_at),
            },
        )

    def stop_periodic(
        self,
        session_id: str,
        *,
        wait: bool = True,
        timeout_s: float = 5.0,
    ) -> bool:
        """Signal the periodic loop for ``session_id`` to exit.

        Returns ``True`` if a loop was stopped, ``False`` if none
        was running.  ``wait=True`` joins the thread for up to
        ``timeout_s``.
        """

        with self._lock:
            state = self._periodic.pop(session_id, None)
        if state is None:
            return False
        state.stop_event.set()
        if wait and state.thread is not None:
            state.thread.join(timeout=timeout_s)
        self._emit(
            SCREENSHOT_EVENT_PERIODIC_STOPPED,
            {
                "schema_version": UI_SCREENSHOT_SCHEMA_VERSION,
                "session_id": session_id,
                "sweeps": int(state.sweeps),
                "failures": int(state.failures),
                "ran_for_s": max(0.0, self._clock() - state.started_at),
            },
        )
        return True

    def is_periodic_running(self, session_id: str) -> bool:
        with self._lock:
            state = self._periodic.get(session_id)
            return state is not None and state.thread is not None and state.thread.is_alive()

    def periodic_sessions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sid
                for sid, st in self._periodic.items()
                if st.thread is not None and st.thread.is_alive()
            )

    def periodic_sweeps(self, session_id: str) -> int:
        """Number of sweeps the periodic loop has completed — used by
        tests and operator telemetry.  Returns 0 for unknown sessions."""

        with self._lock:
            state = self._periodic.get(session_id)
            return int(state.sweeps) if state is not None else 0

    def _periodic_loop(self, session_id: str) -> None:
        """Thread target — sweeps until the stop event fires."""

        while True:
            with self._lock:
                state = self._periodic.get(session_id)
            if state is None or state.stop_event.is_set():
                return
            if state.stop_event.wait(timeout=state.interval_s):
                return
            try:
                self.capture(
                    session_id=session_id,
                    preview_url=state.preview_url,
                    viewport=state.viewport,
                    path=state.path,
                )
            except ScreenshotError as exc:
                logger.warning(
                    "periodic screenshot failed for %s: %s", session_id, exc
                )
                with self._lock:
                    cur = self._periodic.get(session_id)
                    if cur is not None:
                        cur.failures += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "periodic screenshot raised unexpectedly for %s: %s",
                    session_id,
                    exc,
                )
                with self._lock:
                    cur = self._periodic.get(session_id)
                    if cur is not None:
                        cur.failures += 1
            with self._lock:
                cur = self._periodic.get(session_id)
                if cur is None or cur.stop_event.is_set():
                    return
                cur.sweeps += 1

    # ─────────────── Bulk teardown / snapshot ───────────────

    def stop_all_periodic(self, *, timeout_s: float = 5.0) -> int:
        """Stop every periodic loop this service owns.  Returns the
        count of loops stopped.  Safe to call during shutdown."""

        with self._lock:
            session_ids = tuple(self._periodic.keys())
        stopped = 0
        for sid in session_ids:
            if self.stop_periodic(sid, wait=True, timeout_s=timeout_s):
                stopped += 1
        return stopped

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe introspection of the service state — counts,
        active periodic loops, history sizes."""

        with self._lock:
            periodic = [
                {
                    "session_id": sid,
                    "interval_s": float(st.interval_s),
                    "preview_url": st.preview_url,
                    "viewport": st.viewport.to_dict(),
                    "path": st.path,
                    "sweeps": int(st.sweeps),
                    "failures": int(st.failures),
                    "started_at": float(st.started_at),
                    "alive": bool(st.thread is not None and st.thread.is_alive()),
                }
                for sid, st in sorted(self._periodic.items())
            ]
            history = {
                sid: len(buf) for sid, buf in sorted(self._history.items())
            }
            return {
                "schema_version": UI_SCREENSHOT_SCHEMA_VERSION,
                "default_viewport": self._default_viewport,
                "history_size": self._history_size,
                "capture_timeout_s": float(self._capture_timeout_s),
                "navigation_timeout_ms": int(self._navigation_timeout_ms),
                "wait_until": self._wait_until,
                "periodic_interval_s": float(self._periodic_interval_s),
                "capture_count": int(self._capture_count),
                "failure_count": int(self._failure_count),
                "periodic": periodic,
                "history": history,
                "now": float(self._clock()),
            }

    def close(self, *, timeout_s: float = 5.0) -> None:
        """Stop every periodic loop and close the engine.  Idempotent."""

        self.stop_all_periodic(timeout_s=timeout_s)
        try:
            self._engine.close()
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("engine close failed: %s", exc)

    def __enter__(self) -> "ScreenshotService":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ─────────────── Internal event plumbing ───────────────

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, dict(payload))
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning("ui_screenshot event callback raised: %s", exc)

    def _emit_failure(
        self,
        request: ScreenshotRequest,
        *,
        reason: str,
    ) -> None:
        payload: dict[str, Any] = {
            "schema_version": UI_SCREENSHOT_SCHEMA_VERSION,
            "session_id": request.session_id,
            "preview_url": request.preview_url,
            "viewport": request.viewport.to_dict(),
            "path": request.path,
            "target_url": request.target_url,
            "reason": reason,
            "at": float(self._clock()),
        }
        self._emit(SCREENSHOT_EVENT_FAILED, payload)
