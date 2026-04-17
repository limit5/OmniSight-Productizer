"""P10 #295 — Unified MobileObservabilityAdapter interface.

Mobile-vertical sibling of ``backend.observability`` (W10 #284). Where
the W10 layer captures browser RUM (Core Web Vitals + JS errors), this
layer captures four mobile-specific signal classes that the web layer
does not (and cannot) have:

    1. **Native crash** — JVM stacktrace (Android) or Mach exception
       (iOS / signal 6 / EXC_BAD_ACCESS / etc.).
    2. **ANR / watchdog termination** — Android "Application Not
       Responding" (main thread blocked > 5 s) or iOS watchdog
       termination (system kill due to startup / responsiveness budget).
    3. **Render metric** — frame draw time and frame-drop ratio
       (Android Choreographer / iOS CADisplayLink).
    4. **Custom event log** — structured breadcrumbs for diagnosis.

Like the W10 layer, every adapter implements a single abstract base so
the SKILL-IOS / SKILL-ANDROID / SKILL-FLUTTER / SKILL-RN scaffolds can
swap providers (Firebase Crashlytics ↔ Sentry Mobile ↔ future
Bugsnag / Embrace) by changing one knob.

Secret handling
---------------
DSN / API key enters through ``from_encrypted_dsn()`` (ciphertext
decrypted via ``backend.secret_store``) or ``from_plaintext_dsn()``
(test / CLI path). The instance never logs the raw secret — only
``dsn_fingerprint()`` (last 4 chars).

Native snippet rendering
------------------------
The adapter exposes ``android_snippet()`` / ``ios_snippet()`` /
``flutter_snippet()`` / ``react_native_snippet()`` returning a small
init block to embed in the platform-specific entry point
(``Application.onCreate()``, ``AppDelegate.application(_:)``,
``main.dart``, ``index.js``). All snippets are pure string formatting;
no I/O.
"""

from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from backend import secret_store

logger = logging.getLogger(__name__)


# ── Error hierarchy (mirrors backend.observability.base) ─────────


class MobileObservabilityError(Exception):
    """Base for every mobile observability adapter error."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidMobileTokenError(MobileObservabilityError):
    """401 — DSN / API key invalid / revoked."""


class MissingMobileScopeError(MobileObservabilityError):
    """403 — token lacks required permission."""


class MobileRateLimitError(MobileObservabilityError):
    """429 — provider rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class MobilePayloadError(MobileObservabilityError):
    """400 — payload rejected (malformed envelope, missing field)."""


# ── Render-metric thresholds ─────────────────────────────────────
#
# Source for "good / poor" boundaries:
#   * Android: Google Play Vitals — slow-frame threshold = 16 ms (60 FPS).
#     "Slow rendering" warning fires when > 25% of frames are slow.
#     "ANR rate" warning fires when ANR rate > 0.47 % over 28 days.
#   * iOS:     Apple "Hang Rate" diagnostic flags any main-thread block
#     longer than 250 ms (sub-classed: 250-1000 ms minor, > 1 s major).

GOOD_RENDER_MS: dict[str, float] = {
    "frame_draw": 16.0,    # ms — 60 FPS budget
    "frame_total": 33.0,   # ms — 30 FPS floor
    "ttid": 1000.0,        # ms — Time-To-Initial-Display
    "ttfd": 2000.0,        # ms — Time-To-Full-Display
    "hang": 250.0,         # ms — iOS hang threshold
}

POOR_RENDER_MS: dict[str, float] = {
    "frame_draw": 33.0,
    "frame_total": 100.0,  # severe jank
    "ttid": 2500.0,
    "ttfd": 5000.0,
    "hang": 1000.0,        # iOS major hang
}

KNOWN_RENDER_METRICS: tuple[str, ...] = (
    "frame_draw", "frame_total", "ttid", "ttfd", "hang",
)


def classify_render(name: str, value: float) -> str:
    """Return ``good`` / ``needs-improvement`` / ``poor`` for a render sample.

    Unknown metric names default to ``"unknown"`` so the upstream
    aggregator can still bucket the sample without dropping it.
    """
    key = (name or "").lower()
    if key not in GOOD_RENDER_MS:
        return "unknown"
    if value <= GOOD_RENDER_MS[key]:
        return "good"
    if value <= POOR_RENDER_MS[key]:
        return "needs-improvement"
    return "poor"


# ── Platform constants ───────────────────────────────────────────

KNOWN_PLATFORMS: tuple[str, ...] = ("android", "ios", "flutter", "react-native")

# ANR is Android's name; iOS calls the equivalent a "watchdog termination"
# (XNU kernel kills the process when the launch / scene-init budget is
# exceeded). We model both with the same ``HangEvent`` shape but tag the
# kind so dashboards can split them.
HANG_KINDS: tuple[str, ...] = ("anr", "watchdog_termination")


# ── Data models ──────────────────────────────────────────────────


@dataclass
class MobileCrash:
    """Normalised native crash event (Android JVM or iOS Mach).

    ``platform`` distinguishes Android vs iOS. ``signal`` is the iOS
    Mach exception name (``EXC_BAD_ACCESS`` / ``SIGSEGV`` / ``SIGABRT``)
    or the Android ``Throwable`` class (``java.lang.NullPointerException``).
    ``stacktrace`` is one frame per line (top = most recent).
    """

    message: str
    platform: str = "android"
    signal: str = ""
    stacktrace: str = ""
    fatal: bool = True
    app_version: str = ""
    os_version: str = ""
    device_model: str = ""
    session_id: str = ""
    user_id: str = ""
    fingerprint: str = ""
    timestamp: float = 0.0
    breadcrumbs: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.platform = (self.platform or "android").lower()
        if self.platform not in KNOWN_PLATFORMS:
            raise ValueError(
                f"unknown platform {self.platform!r}; "
                f"expected one of {KNOWN_PLATFORMS}"
            )
        if self.timestamp <= 0:
            self.timestamp = time.time()
        if not self.fingerprint:
            self.fingerprint = derive_fingerprint(
                release=self.app_version,
                message=self.message,
                stack=self.stacktrace,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "platform": self.platform,
            "signal": self.signal,
            "stacktrace": self.stacktrace,
            "fatal": self.fatal,
            "app_version": self.app_version,
            "os_version": self.os_version,
            "device_model": self.device_model,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "fingerprint": self.fingerprint,
            "timestamp": self.timestamp,
            "breadcrumbs": list(self.breadcrumbs),
        }


@dataclass
class HangEvent:
    """ANR (Android) / watchdog termination (iOS).

    ``kind`` selects the platform-native semantics; both share the same
    "main thread blocked for N ms" body so dashboards / tickets are
    consistent across platforms.
    """

    duration_ms: float
    platform: str = "android"
    kind: str = "anr"
    main_thread_stack: str = ""
    app_version: str = ""
    os_version: str = ""
    device_model: str = ""
    session_id: str = ""
    in_foreground: bool = True
    fingerprint: str = ""
    timestamp: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.platform = (self.platform or "android").lower()
        if self.platform not in KNOWN_PLATFORMS:
            raise ValueError(
                f"unknown platform {self.platform!r}; "
                f"expected one of {KNOWN_PLATFORMS}"
            )
        self.kind = (self.kind or "anr").lower()
        if self.kind not in HANG_KINDS:
            raise ValueError(
                f"unknown hang kind {self.kind!r}; "
                f"expected one of {HANG_KINDS}"
            )
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")
        if self.timestamp <= 0:
            self.timestamp = time.time()
        if not self.fingerprint:
            self.fingerprint = derive_fingerprint(
                release=self.app_version,
                message=f"{self.kind}:{int(self.duration_ms)}ms",
                stack=self.main_thread_stack,
            )

    @property
    def severity(self) -> str:
        """Map the duration to a severity string the router can route on.

        Thresholds line up with each platform's official guidance:
            * Android ANR: 5 s = warning, 10 s = critical (Play Vitals).
            * iOS watchdog (launch): 20 s = critical kill threshold.
        """
        if self.kind == "anr":
            if self.duration_ms >= 10_000:
                return "critical"
            if self.duration_ms >= 5_000:
                return "warning"
            return "info"
        # watchdog_termination: every event is critical (process died).
        return "critical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_ms": self.duration_ms,
            "platform": self.platform,
            "kind": self.kind,
            "main_thread_stack": self.main_thread_stack,
            "app_version": self.app_version,
            "os_version": self.os_version,
            "device_model": self.device_model,
            "session_id": self.session_id,
            "in_foreground": self.in_foreground,
            "fingerprint": self.fingerprint,
            "severity": self.severity,
            "timestamp": self.timestamp,
        }


@dataclass
class RenderMetric:
    """Mobile render-time / frame-drop sample.

    ``name`` identifies the metric (``frame_draw`` / ``frame_total``
    / ``ttid`` / ``ttfd`` / ``hang``). ``value`` is in ms. ``screen``
    is the route / view-controller / Activity / Fragment label so the
    aggregator can bucket by surface.
    """

    name: str
    value: float
    platform: str = "android"
    screen: str = "/"
    session_id: str = ""
    rating: str = ""
    timestamp: float = 0.0
    app_version: str = ""
    device_model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = (self.name or "").lower()
        self.platform = (self.platform or "android").lower()
        if self.platform not in KNOWN_PLATFORMS:
            raise ValueError(
                f"unknown platform {self.platform!r}; "
                f"expected one of {KNOWN_PLATFORMS}"
            )
        if self.timestamp <= 0:
            self.timestamp = time.time()
        if not self.rating:
            self.rating = classify_render(self.name, self.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "platform": self.platform,
            "screen": self.screen,
            "session_id": self.session_id,
            "rating": self.rating,
            "timestamp": self.timestamp,
            "app_version": self.app_version,
            "device_model": self.device_model,
        }


# ── Helpers ──────────────────────────────────────────────────────


def derive_fingerprint(*, release: str, message: str, stack: str) -> str:
    """Stable dedup key for a mobile event: release + message + top frame."""
    top_frame = ""
    if stack:
        for line in stack.splitlines():
            line = line.strip()
            if not line:
                continue
            top_frame = _strip_addr(line)
            break
    payload = f"{release}|{message.strip()}|{top_frame}"
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()


def _strip_addr(line: str) -> str:
    """Drop hex addresses (``0xdeadbeef``) so two builds with shifted
    code addresses still hash to the same fingerprint.

    Also drops ``:line:col`` tails (mirrors the W10 helper) so two
    builds with shifted line numbers still hash the same.
    """
    parts = line.split(" ")
    out: list[str] = []
    for p in parts:
        if p.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in p[2:]):
            continue
        out.append(p)
    cleaned = " ".join(out)
    # Strip trailing :line:col like the W10 helper.
    tail = cleaned.rsplit(":", 2)
    if len(tail) == 3 and tail[1].isdigit() and tail[2].isdigit():
        return tail[0]
    if len(tail) >= 2 and tail[-1].isdigit():
        return ":".join(tail[:-1])
    return cleaned


def dsn_fingerprint(dsn: Optional[str]) -> str:
    """Log-safe fingerprint for a DSN / API key — never the full value."""
    if not dsn or len(dsn) <= 8:
        return "****"
    return f"…{dsn[-4:]}"


# ── Interface ────────────────────────────────────────────────────


class MobileObservabilityAdapter(ABC):
    """Abstract base for every mobile observability adapter.

    Subclasses MUST set a ``provider`` classvar and implement
    ``send_crash`` / ``send_hang`` / ``send_render``. They also implement
    *at least one* native init snippet — most ship all four since the
    same provider supports all four toolchains.
    """

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        dsn: Optional[str] = None,
        api_key: Optional[str] = None,
        application_id: Optional[str] = None,
        environment: str = "production",
        release: Optional[str] = None,
        sample_rate: float = 1.0,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(
                f"{type(self).__name__} must set classvar 'provider'"
            )
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError(
                f"sample_rate must be in [0.0, 1.0], got {sample_rate}"
            )
        self._dsn = dsn
        self._api_key = api_key
        self._application_id = application_id
        self._environment = environment
        self._release = release
        self._sample_rate = sample_rate
        self._timeout = timeout
        self._configure(**kwargs)

    # ── Construction helpers ──

    @classmethod
    def from_encrypted_dsn(
        cls,
        ciphertext: str,
        *,
        api_key_ciphertext: Optional[str] = None,
        **kwargs: Any,
    ) -> "MobileObservabilityAdapter":
        """Decrypt the ciphertext via ``backend.secret_store`` and build
        an adapter. Preferred entry point from routers — the plaintext
        DSN never appears in a log or dict dump.
        """
        dsn = secret_store.decrypt(ciphertext)
        api_key: Optional[str] = None
        if api_key_ciphertext:
            api_key = secret_store.decrypt(api_key_ciphertext)
        return cls(dsn=dsn, api_key=api_key, **kwargs)

    @classmethod
    def from_plaintext_dsn(
        cls,
        dsn: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "MobileObservabilityAdapter":
        return cls(dsn=dsn, api_key=api_key, **kwargs)

    # ── Hooks ──

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup."""
        pass

    # ── Public logging helpers ──

    def dsn_fp(self) -> str:
        return dsn_fingerprint(self._dsn)

    def api_key_fp(self) -> str:
        return dsn_fingerprint(self._api_key)

    # ── Abstract interface ──

    @abstractmethod
    async def send_crash(self, crash: MobileCrash) -> None:
        """Forward a native crash event. Crashes are NEVER sampled."""

    @abstractmethod
    async def send_hang(self, hang: HangEvent) -> None:
        """Forward an ANR (Android) / watchdog termination (iOS).

        Critical-severity events are NEVER sampled; warning / info
        respect ``self._sample_rate``.
        """

    @abstractmethod
    async def send_render(self, metric: RenderMetric) -> None:
        """Forward a render-time / frame-drop sample.

        Implementations should respect ``self._sample_rate`` — render
        metrics are high-frequency and can swamp the vendor quota.
        """

    @abstractmethod
    def native_snippet(self, platform: str) -> str:
        """Return the native init snippet for ``platform``.

        ``platform`` is one of ``KNOWN_PLATFORMS``. Snippets are pure
        text — drop them into the platform's entry point.
        """

    # ── Sampling ──

    def _should_sample(self) -> bool:
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        import random
        return random.random() < self._sample_rate


__all__ = [
    "GOOD_RENDER_MS",
    "HANG_KINDS",
    "HangEvent",
    "InvalidMobileTokenError",
    "KNOWN_PLATFORMS",
    "KNOWN_RENDER_METRICS",
    "MissingMobileScopeError",
    "MobileCrash",
    "MobileObservabilityAdapter",
    "MobileObservabilityError",
    "MobilePayloadError",
    "MobileRateLimitError",
    "POOR_RENDER_MS",
    "RenderMetric",
    "classify_render",
    "derive_fingerprint",
    "dsn_fingerprint",
]
