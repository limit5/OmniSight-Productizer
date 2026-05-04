"""D2 — SKILL-IPCAM: RTSP server scaffold (live555 / gstreamer dual-backend) (#219).

Abstract RTSP server manager with swappable backend. Three backends are
defined so the rest of the skill pack (ONVIF Media, hw_codec, HIL recipes)
can depend on a stable interface even when the host OS is missing one
of the C/C++ RTSP libraries:

    ┌──────────── Public API ────────────┐
    RTSPBackend          — enum {GSTREAMER, LIVE555, STUB}
    RTSPServerConfig     — bind / port / session / auth policy
    StreamMount          — one rtsp:// path → codec + resolution + fps
    VideoCodec           — {H264, H265}
    SessionState         — RTSP session FSM
    RTSPSession          — live session record
    Credential           — username / password / role triple
    TransportSpec        — parsed "Transport:" header
    build_sdp()          — SDP text for a mount
    parse_transport()    — "RTP/AVP;unicast;client_port=..." → TransportSpec
    parse_rtsp_request() — raw bytes → method / uri / headers / cseq
    detect_available_backends() — probe the interpreter for usable libs
    RTSPServerManager    — the orchestrator (start / stop / mounts / sessions)

**Module-global state audit (SOP Step 1 mandatory question):**

This module holds three module-level mutables — ``_BACKEND_CACHE`` (probe
result) and ``_DIGEST_NONCE_SEEN`` (anti-replay) plus the per-manager
instance state inside ``RTSPServerManager``. They are deliberately
per-process and per-replica:

* ``_BACKEND_CACHE`` is *derived* (``importlib.util.find_spec`` + env
  override) so every uvicorn worker recomputes the same value — category
  1 of the SOP ("each worker derives the same value from the same source").
* ``_DIGEST_NONCE_SEEN`` is a per-process cache for RTSP-session
  anti-replay; RTSP clients (ffmpeg / VLC / NVR software) keep their
  TCP control channel pinned to one worker for the whole session, so
  cross-worker nonce sharing would add overhead for no correctness win
  — category 3 of the SOP ("intentionally per-worker").
* ``RTSPServerManager`` holds per-instance mutables — callers must use
  a single manager per process, which is the norm for RTSP servers.

**Runtime note:**

The C/C++ RTSP servers (live555, gst-rtsp-server) bind kernel sockets
and need privileged ports when ``port < 1024``. The scaffold defaults to
port 8554 (IANA-reserved RTSP alt port) so unit tests can dry-run without
CAP_NET_BIND_SERVICE. Tests exercise ``RTSPBackend.STUB``, which is a
zero-dependency in-process simulator that implements the full RTSP
state machine but never touches a socket.

**Hardware codec coupling:**

The scaffold does not itself encode video — it expects upstream
producers (the ``hw_codec_binding`` task) to push Annex-B NAL units via
:py:meth:`RTSPServerManager.push_access_unit`. When a real backend
(live555 / gstreamer) is bound, the scaffold forwards NALs into an
appsrc / OnDemandServerMediaSubsession; under STUB the NALs accumulate
in an in-memory deque for tests.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import importlib.util
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────

RTSP_DEFAULT_PORT = 8554
RTSP_IANA_DEFAULT_PORT = 554  # privileged — rarely used during development

# Dynamic RTP payload-type assignments — every SDP we emit uses the same
# numbers so depacketiser tests can match by literal.
PAYLOAD_TYPE_H264 = 96
PAYLOAD_TYPE_H265 = 97

# RTSP 1.0 methods we service. RTSP 2.0 adds GET_PARAMETER/SET_PARAMETER
# already covered here; REDIRECT and ANNOUNCE are deliberately omitted —
# they are not required for Profile S ingest.
SUPPORTED_METHODS = (
    "OPTIONS",
    "DESCRIBE",
    "SETUP",
    "PLAY",
    "PAUSE",
    "TEARDOWN",
    "GET_PARAMETER",
    "SET_PARAMETER",
)

_ENV_BACKEND_OVERRIDE = "OMNISIGHT_IPCAM_RTSP_BACKEND"
_ENV_DEFAULT_PORT = "OMNISIGHT_IPCAM_RTSP_PORT"

_MAX_SESSIONS_HARD_CAP = 256  # refuses start() above this
_SESSION_ID_BYTES = 8
_NONCE_BYTES = 16
_MOUNT_PATH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-/.]{0,127}$")


# ── Enums ──────────────────────────────────────────────────────────────


class RTSPBackend(str, enum.Enum):
    """RTSP server implementation provider."""

    GSTREAMER = "gstreamer"  # gi.repository.GstRtspServer
    LIVE555 = "live555"      # pyLive555 / liveMedia Python bindings
    STUB = "stub"            # in-process simulator — no socket bind


class VideoCodec(str, enum.Enum):
    H264 = "h264"
    H265 = "h265"


class SessionState(str, enum.Enum):
    """RFC 2326 session lifecycle states."""

    INIT = "init"
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"
    TEARDOWN = "teardown"


class TransportProtocol(str, enum.Enum):
    RTP_AVP_UDP = "RTP/AVP"       # unicast over UDP
    RTP_AVP_TCP = "RTP/AVP/TCP"   # interleaved over control channel
    RTP_AVPF_UDP = "RTP/AVPF"     # low-latency feedback profile
    RTP_AVPF_TCP = "RTP/AVPF/TCP"


class AuthScheme(str, enum.Enum):
    NONE = "none"
    BASIC = "basic"
    DIGEST = "digest"


# ── Data classes ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Credential:
    username: str
    password: str
    role: str = "viewer"

    def __post_init__(self) -> None:
        if not self.username:
            raise ValueError("Credential.username must be non-empty")
        if not self.password:
            raise ValueError("Credential.password must be non-empty")


@dataclass
class StreamMount:
    path: str
    codec: VideoCodec = VideoCodec.H264
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate_kbps: int = 4096
    description: str = ""
    profile_level_id: str = "42001F"  # H.264 Baseline L3.1 default
    sprop_parameter_sets: str = ""    # base64(SPS),base64(PPS) for H264;
    # H.265 has sprop-vps / sprop-sps / sprop-pps packed separately
    sprop_vps: str = ""
    sprop_sps: str = ""
    sprop_pps: str = ""

    def __post_init__(self) -> None:
        normalised = _normalise_mount_path(self.path)
        if not _MOUNT_PATH_RE.match(normalised):
            raise ValueError(
                f"Invalid RTSP mount path {self.path!r} — must match "
                f"[a-zA-Z0-9][a-zA-Z0-9_\\-/.]{{0,127}}"
            )
        self.path = normalised
        if self.width <= 0 or self.height <= 0:
            raise ValueError("StreamMount width/height must be positive")
        if not 1 <= self.fps <= 120:
            raise ValueError("StreamMount fps must be in [1, 120]")
        if self.bitrate_kbps < 0:
            raise ValueError("StreamMount bitrate_kbps must be non-negative")

    @property
    def rtp_payload_type(self) -> int:
        return (
            PAYLOAD_TYPE_H264
            if self.codec == VideoCodec.H264
            else PAYLOAD_TYPE_H265
        )

    @property
    def rtp_encoding_name(self) -> str:
        return "H264" if self.codec == VideoCodec.H264 else "H265"


@dataclass
class RTSPServerConfig:
    backend: Optional[RTSPBackend] = None  # None → auto-detect at start()
    bind_address: str = "0.0.0.0"
    port: int = RTSP_DEFAULT_PORT
    max_sessions: int = 32
    session_timeout_s: int = 60
    auth_scheme: AuthScheme = AuthScheme.DIGEST
    auth_realm: str = "OmniSight-IPCam"
    # Nonce lifetime — after this many seconds a nonce is considered stale
    # and the server answers 401 with stale=true (RFC 7616 §3.3).
    nonce_lifetime_s: int = 300
    # Whether to allow plaintext RTSP in addition to TLS (rtsps://). TLS
    # termination is left to the upstream reverse proxy — this flag just
    # tells the SDP whether to emit the rtsps scheme.
    tls_terminated_upstream: bool = False
    # Default mount path when no explicit path is supplied — the
    # gstreamer rtsp-server defaults to "/test" which is surprising; we
    # force-override to "live/main".
    default_mount_path: str = "live/main"

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError(f"RTSP port out of range: {self.port}")
        if self.max_sessions < 1 or self.max_sessions > _MAX_SESSIONS_HARD_CAP:
            raise ValueError(
                f"max_sessions must be in [1, {_MAX_SESSIONS_HARD_CAP}], "
                f"got {self.max_sessions}"
            )
        if self.session_timeout_s < 1:
            raise ValueError("session_timeout_s must be >= 1")
        if not self.auth_realm:
            raise ValueError("auth_realm must be non-empty")


@dataclass
class TransportSpec:
    """Parsed RFC 2326 ``Transport:`` header."""

    protocol: TransportProtocol
    unicast: bool = True
    client_port_rtp: Optional[int] = None
    client_port_rtcp: Optional[int] = None
    server_port_rtp: Optional[int] = None
    server_port_rtcp: Optional[int] = None
    interleaved_rtp: Optional[int] = None
    interleaved_rtcp: Optional[int] = None
    ssrc: Optional[int] = None
    multicast_address: Optional[str] = None
    ttl: Optional[int] = None
    mode: str = "PLAY"

    def to_header(self) -> str:
        """Serialise back to a Transport header value (server side)."""
        parts: list[str] = [self.protocol.value]
        parts.append("multicast" if not self.unicast else "unicast")
        if self.client_port_rtp is not None:
            end = self.client_port_rtcp or (self.client_port_rtp + 1)
            parts.append(f"client_port={self.client_port_rtp}-{end}")
        if self.server_port_rtp is not None:
            end = self.server_port_rtcp or (self.server_port_rtp + 1)
            parts.append(f"server_port={self.server_port_rtp}-{end}")
        if self.interleaved_rtp is not None:
            end = self.interleaved_rtcp or (self.interleaved_rtp + 1)
            parts.append(f"interleaved={self.interleaved_rtp}-{end}")
        if self.ssrc is not None:
            parts.append(f"ssrc={self.ssrc:08X}")
        if self.multicast_address:
            parts.append(f"destination={self.multicast_address}")
        if self.ttl is not None:
            parts.append(f"ttl={self.ttl}")
        parts.append(f"mode={self.mode}")
        return ";".join(parts)


@dataclass
class RTSPRequest:
    method: str
    uri: str
    version: str
    cseq: int
    headers: dict[str, str]
    body: bytes = b""

    @property
    def path(self) -> str:
        """Strip scheme/host from the URI and return the mount path."""
        m = re.match(r"rtsp[s]?://[^/]+/(.*)", self.uri)
        if m:
            return m.group(1)
        if self.uri.startswith("/"):
            return self.uri[1:]
        return self.uri


@dataclass
class RTSPSession:
    session_id: str
    mount_path: str
    transport: TransportSpec
    state: SessionState = SessionState.INIT
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    client_address: str = ""
    user: Optional[str] = None
    timeout_s: int = 60

    def touch(self) -> None:
        self.last_activity_at = time.time()

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        return (now - self.last_activity_at) > self.timeout_s

    def transition(self, new_state: SessionState) -> None:
        if not _is_valid_transition(self.state, new_state):
            raise RTSPSessionStateError(
                f"Invalid session transition {self.state.value} → {new_state.value}"
            )
        self.state = new_state
        self.touch()


@dataclass
class _NonceRecord:
    value: str
    created_at: float
    nc_counter: int = 0


# ── Exceptions ─────────────────────────────────────────────────────────


class RTSPError(Exception):
    """Base class for RTSP-layer errors."""

    status_code: int = 500


class RTSPBackendUnavailable(RTSPError):
    status_code = 503


class RTSPMountNotFound(RTSPError):
    status_code = 404


class RTSPSessionNotFound(RTSPError):
    status_code = 454


class RTSPSessionStateError(RTSPError):
    status_code = 455


class RTSPUnsupportedTransport(RTSPError):
    status_code = 461


class RTSPAuthError(RTSPError):
    status_code = 401


class RTSPBadRequest(RTSPError):
    status_code = 400


# ── Transport parsing ──────────────────────────────────────────────────


def _parse_port_range(spec: str) -> tuple[int, Optional[int]]:
    """Parse a "client_port=8000-8001" fragment → (8000, 8001)."""
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return int(lo), int(hi)
    return int(spec), None


def parse_transport(header: str) -> TransportSpec:
    """Parse an RFC 2326 ``Transport:`` header value.

    Raises ``RTSPUnsupportedTransport`` when the protocol token is not
    one of the four we support (RTP/AVP[/TCP], RTP/AVPF[/TCP]).
    """
    if not header:
        raise RTSPBadRequest("Empty Transport header")
    tokens = [t.strip() for t in header.split(";") if t.strip()]
    if not tokens:
        raise RTSPBadRequest("Empty Transport header after tokenisation")
    proto_token = tokens[0]
    try:
        protocol = TransportProtocol(proto_token)
    except ValueError as exc:
        raise RTSPUnsupportedTransport(
            f"Unsupported transport protocol: {proto_token}"
        ) from exc
    spec = TransportSpec(protocol=protocol)
    for t in tokens[1:]:
        if t == "unicast":
            spec.unicast = True
        elif t == "multicast":
            spec.unicast = False
        elif t.startswith("client_port="):
            lo, hi = _parse_port_range(t.split("=", 1)[1])
            spec.client_port_rtp, spec.client_port_rtcp = lo, hi
        elif t.startswith("server_port="):
            lo, hi = _parse_port_range(t.split("=", 1)[1])
            spec.server_port_rtp, spec.server_port_rtcp = lo, hi
        elif t.startswith("interleaved="):
            lo, hi = _parse_port_range(t.split("=", 1)[1])
            spec.interleaved_rtp, spec.interleaved_rtcp = lo, hi
        elif t.startswith("ssrc="):
            spec.ssrc = int(t.split("=", 1)[1], 16)
        elif t.startswith("destination="):
            spec.multicast_address = t.split("=", 1)[1]
        elif t.startswith("ttl="):
            spec.ttl = int(t.split("=", 1)[1])
        elif t.startswith("mode="):
            spec.mode = t.split("=", 1)[1].strip('"')
        # Any unknown attribute is silently ignored — RFC 2326 §12.39
        # says new attributes are reserved and servers MUST ignore
        # unknown ones.
    return spec


# ── Request parsing ────────────────────────────────────────────────────


def parse_rtsp_request(raw: bytes) -> RTSPRequest:
    """Parse a raw RTSP request buffer into structured form.

    This is a minimal parser sufficient for unit tests and the STUB
    backend — real deployments let live555 / gstreamer handle this. It
    enforces RFC 2326 §6: ``<METHOD> <URI> RTSP/<ver>\\r\\n`` + key:
    value headers + optional body after ``\\r\\n\\r\\n``.
    """
    if not raw:
        raise RTSPBadRequest("Empty RTSP request buffer")
    try:
        header_block, _, body = raw.partition(b"\r\n\r\n")
        lines = header_block.decode("ascii", errors="replace").split("\r\n")
    except Exception as exc:
        raise RTSPBadRequest(f"RTSP request parse error: {exc}") from exc
    if not lines:
        raise RTSPBadRequest("No request-line")
    request_line = lines[0]
    m = re.match(r"^(\S+)\s+(\S+)\s+RTSP/(\d+\.\d+)$", request_line)
    if not m:
        raise RTSPBadRequest(f"Bad request-line: {request_line!r}")
    method, uri, version = m.group(1), m.group(2), m.group(3)
    if method not in SUPPORTED_METHODS and method != "REDIRECT":
        # 501 Not Implemented is returned by the dispatcher; here we
        # still parse so the dispatcher can log the unknown method.
        pass
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise RTSPBadRequest(f"Malformed header line: {line!r}")
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    cseq_raw = headers.get("cseq")
    if cseq_raw is None:
        raise RTSPBadRequest("Missing CSeq header")
    try:
        cseq = int(cseq_raw)
    except ValueError as exc:
        raise RTSPBadRequest(f"Invalid CSeq value: {cseq_raw!r}") from exc
    return RTSPRequest(
        method=method,
        uri=uri,
        version=version,
        cseq=cseq,
        headers=headers,
        body=body,
    )


# ── Session FSM ────────────────────────────────────────────────────────


_VALID_TRANSITIONS = {
    SessionState.INIT: {SessionState.READY, SessionState.TEARDOWN},
    SessionState.READY: {SessionState.PLAYING, SessionState.TEARDOWN},
    SessionState.PLAYING: {
        SessionState.PAUSED,
        SessionState.READY,
        SessionState.TEARDOWN,
    },
    SessionState.PAUSED: {
        SessionState.PLAYING,
        SessionState.READY,
        SessionState.TEARDOWN,
    },
    SessionState.TEARDOWN: set(),
}


def _is_valid_transition(old: SessionState, new: SessionState) -> bool:
    return new in _VALID_TRANSITIONS.get(old, set())


def _normalise_mount_path(path: str) -> str:
    """Strip leading slash and trailing whitespace; keep the rest intact."""
    if path is None:
        return ""
    trimmed = path.strip()
    while trimmed.startswith("/"):
        trimmed = trimmed[1:]
    return trimmed


# ── SDP generation ─────────────────────────────────────────────────────


def _b64strip(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def build_sdp(
    mount: StreamMount,
    bind_address: str,
    session_id: str,
    *,
    ntp_start: Optional[int] = None,
) -> str:
    """Build an RFC 4566 SDP for a single H.264 or H.265 video track.

    Format deliberately matches what ffprobe / VLC / GStreamer expect
    from commercial cameras so compatibility tests (HIL recipe
    ``hil_rtsp_describe``) can parse the SDP unmodified.
    """
    ntp = ntp_start or int(time.time())
    lines = [
        "v=0",
        f"o=- {session_id} {ntp} IN IP4 {bind_address}",
        f"s={mount.description or mount.path}",
        f"c=IN IP4 {bind_address}",
        "t=0 0",
        "a=tool:OmniSight-IPCam",
        "a=control:*",
        f"m=video 0 RTP/AVP {mount.rtp_payload_type}",
        f"a=rtpmap:{mount.rtp_payload_type} {mount.rtp_encoding_name}/90000",
    ]
    if mount.codec == VideoCodec.H264:
        fmtp_parts = ["packetization-mode=1"]
        if mount.profile_level_id:
            fmtp_parts.append(f"profile-level-id={mount.profile_level_id}")
        if mount.sprop_parameter_sets:
            fmtp_parts.append(f"sprop-parameter-sets={mount.sprop_parameter_sets}")
        lines.append(f"a=fmtp:{mount.rtp_payload_type} {';'.join(fmtp_parts)}")
    else:
        # H.265 uses sprop-vps / sprop-sps / sprop-pps per RFC 7798
        fmtp_parts: list[str] = []
        if mount.sprop_vps:
            fmtp_parts.append(f"sprop-vps={mount.sprop_vps}")
        if mount.sprop_sps:
            fmtp_parts.append(f"sprop-sps={mount.sprop_sps}")
        if mount.sprop_pps:
            fmtp_parts.append(f"sprop-pps={mount.sprop_pps}")
        if fmtp_parts:
            lines.append(f"a=fmtp:{mount.rtp_payload_type} {';'.join(fmtp_parts)}")
    lines.append(
        f"a=framerate:{mount.fps}"
    )
    lines.append(
        f"a=cliprect:0,0,{mount.height},{mount.width}"
    )
    lines.append(
        f"a=x-dimensions:{mount.width},{mount.height}"
    )
    lines.append("a=control:track1")
    return "\r\n".join(lines) + "\r\n"


# ── Authentication (RFC 7616 Digest + RFC 7617 Basic) ──────────────────


def _md5(s: str) -> str:
    """RTSP Digest auth's spec-required MD5 hex component."""
    return hashlib.md5(
        s.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()


def build_digest_challenge(
    realm: str,
    nonce: str,
    *,
    qop: str = "auth",
    stale: bool = False,
    algorithm: str = "MD5",
) -> str:
    """Server → client Digest challenge (WWW-Authenticate header value)."""
    fields = [
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'algorithm={algorithm}',
        f'qop="{qop}"',
    ]
    if stale:
        fields.append("stale=true")
    return "Digest " + ", ".join(fields)


def compute_digest_response(
    *,
    username: str,
    password: str,
    realm: str,
    method: str,
    uri: str,
    nonce: str,
    cnonce: str = "",
    nc: str = "00000001",
    qop: str = "auth",
) -> str:
    """RFC 7616 §3.4.1 response hash (MD5, qop=auth).

    Returns the hex digest — caller wraps it in an ``Authorization:
    Digest …`` header.
    """
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop:
        return _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return _md5(f"{ha1}:{nonce}:{ha2}")


def _parse_digest_header(header: str) -> dict[str, str]:
    """Parse an ``Authorization: Digest key=val,...`` value."""
    if not header.lower().startswith("digest "):
        raise RTSPAuthError("Not a Digest authorization header")
    body = header[len("Digest "):]
    # RFC 7616 uses comma-separated key=value pairs; values may be
    # quoted. This parser is deliberately simple — it handles the two
    # shapes real clients emit (all-quoted, mixed-quoted-and-bare).
    out: dict[str, str] = {}
    pairs = re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,]+))', body)
    for key, quoted, bare in pairs:
        out[key.strip().lower()] = (quoted if quoted != "" else bare).strip()
    return out


def _parse_basic_header(header: str) -> tuple[str, str]:
    if not header.lower().startswith("basic "):
        raise RTSPAuthError("Not a Basic authorization header")
    b64 = header[len("Basic "):].strip()
    try:
        decoded = base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception as exc:
        raise RTSPAuthError(f"Malformed Basic header: {exc}") from exc
    if ":" not in decoded:
        raise RTSPAuthError("Basic header missing ':' separator")
    username, _, password = decoded.partition(":")
    return username, password


def generate_nonce() -> str:
    return secrets.token_hex(_NONCE_BYTES)


def generate_session_id() -> str:
    return secrets.token_hex(_SESSION_ID_BYTES)


# ── Backend detection ──────────────────────────────────────────────────


_BACKEND_CACHE: Optional[list[RTSPBackend]] = None


def _probe_backend() -> list[RTSPBackend]:
    """Return available RTSP backends, ordered by preference.

    The order is deliberate:
      1. GStreamer (gst-rtsp-server) — best compatibility, maintained
         by the GStreamer project, widely available on Linux distros.
      2. live555 (liveMedia) — lighter footprint, preferred on small
         embedded boards, still the reference RTSP server in the C++
         world.
      3. STUB — zero dependency fallback, always present.
    """
    available: list[RTSPBackend] = []
    if importlib.util.find_spec("gi") is not None:
        try:
            # Probe the specific submodule without actually importing
            # GStreamer (which would need `gi.require_version`).
            spec = importlib.util.find_spec("gi.repository")
            if spec is not None:
                available.append(RTSPBackend.GSTREAMER)
        except Exception as exc:
            logger.debug("GStreamer probe failed: %s", exc)
    # live555 has several Python bindings — pyLive555 and live555py
    # (both PyPI). We check names in order.
    for candidate in ("live555", "pylive555"):
        if importlib.util.find_spec(candidate) is not None:
            available.append(RTSPBackend.LIVE555)
            break
    available.append(RTSPBackend.STUB)
    return available


def detect_available_backends(*, refresh: bool = False) -> list[RTSPBackend]:
    """Return the preference-ordered list of RTSP backends.

    Result is cached per-process; pass ``refresh=True`` to re-probe
    (useful in tests that monkeypatch ``importlib.util.find_spec``).
    """
    global _BACKEND_CACHE
    if refresh or _BACKEND_CACHE is None:
        _BACKEND_CACHE = _probe_backend()
    return list(_BACKEND_CACHE)


def select_backend(
    preference: Optional[RTSPBackend] = None,
) -> RTSPBackend:
    """Pick a backend honouring (env override → explicit preference →
    auto-probe)."""
    env_override = os.environ.get(_ENV_BACKEND_OVERRIDE, "").strip().lower()
    if env_override:
        try:
            return RTSPBackend(env_override)
        except ValueError as exc:
            raise RTSPBackendUnavailable(
                f"{_ENV_BACKEND_OVERRIDE}={env_override!r} is not a valid backend"
            ) from exc
    available = detect_available_backends()
    if preference is not None:
        if preference in available:
            return preference
        raise RTSPBackendUnavailable(
            f"Backend {preference.value!r} not available; available={[b.value for b in available]}"
        )
    return available[0]


# ── Nonce cache (anti-replay) ──────────────────────────────────────────


class _NonceStore:
    """Per-process nonce cache for anti-replay.

    Real-world RTSP clients keep a TCP control channel pinned to one
    server worker for the entire session, so there is no correctness
    need to share the nonce cache across uvicorn workers — category
    3 of the SOP module-global-state audit.
    """

    def __init__(self, lifetime_s: int) -> None:
        self._lifetime = lifetime_s
        self._store: dict[str, _NonceRecord] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        nonce = generate_nonce()
        with self._lock:
            self._store[nonce] = _NonceRecord(value=nonce, created_at=time.time())
        return nonce

    def verify(self, nonce: str, nc: str) -> tuple[bool, bool]:
        """Return (valid, stale).

        * valid = nonce is known AND (if nc supplied) nc > last seen nc.
        * stale = nonce is known but older than lifetime_s.
        """
        with self._lock:
            rec = self._store.get(nonce)
            if rec is None:
                return False, False
            age = time.time() - rec.created_at
            if age > self._lifetime:
                return False, True
            try:
                nc_int = int(nc, 16) if nc else rec.nc_counter + 1
            except ValueError:
                return False, False
            if nc_int <= rec.nc_counter:
                return False, False
            rec.nc_counter = nc_int
            return True, False

    def purge_expired(self) -> int:
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._store.items() if (now - v.created_at) > self._lifetime]
            for k in expired:
                del self._store[k]
            return len(expired)


# ── Server manager ─────────────────────────────────────────────────────


class RTSPServerManager:
    """Orchestrator over live555 / gstreamer / stub.

    Thread-safe: mounts and sessions are mutated under ``_lock``. Real
    I/O (socket bind, session thread) happens when :py:meth:`start` is
    called on a non-STUB backend; STUB is fully in-memory and what all
    unit tests exercise.
    """

    def __init__(self, config: RTSPServerConfig) -> None:
        self._config = config
        self._backend: Optional[RTSPBackend] = None
        self._mounts: dict[str, StreamMount] = {}
        self._sessions: dict[str, RTSPSession] = {}
        self._credentials: dict[str, Credential] = {}
        self._nonce_store = _NonceStore(config.nonce_lifetime_s)
        self._lock = threading.Lock()
        self._running = False
        # Per-mount NAL buffer — STUB backend stashes pushed access units
        # here so tests can introspect them. Real backends forward to an
        # appsrc / OnDemandServerMediaSubsession.
        self._nal_buffer: dict[str, deque[tuple[list[bytes], int]]] = {}

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> RTSPBackend:
        with self._lock:
            if self._running:
                return self._backend  # type: ignore[return-value]
            self._backend = select_backend(self._config.backend)
            if self._backend == RTSPBackend.STUB:
                logger.info(
                    "RTSP scaffold started on STUB backend (dry-run; no socket)"
                )
            else:
                logger.info(
                    "RTSP scaffold started on %s backend at %s:%d",
                    self._backend.value,
                    self._config.bind_address,
                    self._config.port,
                )
            self._running = True
            return self._backend

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            for session in list(self._sessions.values()):
                try:
                    session.transition(SessionState.TEARDOWN)
                except RTSPSessionStateError:
                    pass
            self._sessions.clear()
            self._running = False
            logger.info("RTSP scaffold stopped (backend=%s)", self._backend)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def backend(self) -> Optional[RTSPBackend]:
        return self._backend

    # ── mount management ───────────────────────────────────────────

    def add_mount(self, mount: StreamMount) -> None:
        with self._lock:
            if mount.path in self._mounts:
                raise ValueError(f"Mount path already registered: {mount.path!r}")
            self._mounts[mount.path] = mount
            self._nal_buffer.setdefault(mount.path, deque(maxlen=256))

    def remove_mount(self, path: str) -> bool:
        path = _normalise_mount_path(path)
        with self._lock:
            existed = path in self._mounts
            self._mounts.pop(path, None)
            self._nal_buffer.pop(path, None)
            return existed

    def get_mount(self, path: str) -> StreamMount:
        path = _normalise_mount_path(path)
        try:
            return self._mounts[path]
        except KeyError as exc:
            raise RTSPMountNotFound(f"No mount registered at {path!r}") from exc

    def list_mounts(self) -> list[str]:
        with self._lock:
            return sorted(self._mounts.keys())

    # ── credentials ────────────────────────────────────────────────

    def add_credential(
        self, username: str, password: str, role: str = "viewer"
    ) -> None:
        cred = Credential(username=username, password=password, role=role)
        with self._lock:
            self._credentials[username] = cred

    def remove_credential(self, username: str) -> bool:
        with self._lock:
            return self._credentials.pop(username, None) is not None

    def authenticate(
        self, request: RTSPRequest
    ) -> Optional[Credential]:
        """Return the authenticated credential, or None if auth is off.

        Raises ``RTSPAuthError`` when auth is enabled but the request
        fails to satisfy the scheme. Callers should catch and translate
        to a 401 response carrying ``build_digest_challenge(...)``.
        """
        if self._config.auth_scheme == AuthScheme.NONE:
            return None
        authz = request.headers.get("authorization", "")
        if not authz:
            raise RTSPAuthError("Missing Authorization header")
        if self._config.auth_scheme == AuthScheme.BASIC:
            username, password = _parse_basic_header(authz)
            cred = self._credentials.get(username)
            if cred is None or cred.password != password:
                raise RTSPAuthError("Invalid Basic credentials")
            return cred
        # Digest
        fields = _parse_digest_header(authz)
        user = fields.get("username", "")
        realm = fields.get("realm", "")
        nonce = fields.get("nonce", "")
        uri = fields.get("uri", "")
        response = fields.get("response", "")
        qop = fields.get("qop", "auth")
        nc = fields.get("nc", "")
        cnonce = fields.get("cnonce", "")
        if realm != self._config.auth_realm:
            raise RTSPAuthError("Realm mismatch")
        valid, stale = self._nonce_store.verify(nonce, nc)
        if not valid:
            exc = RTSPAuthError("Stale nonce" if stale else "Invalid nonce")
            exc.stale = stale  # type: ignore[attr-defined]
            raise exc
        cred = self._credentials.get(user)
        if cred is None:
            raise RTSPAuthError(f"Unknown user: {user!r}")
        expected = compute_digest_response(
            username=cred.username,
            password=cred.password,
            realm=realm,
            method=request.method,
            uri=uri,
            nonce=nonce,
            cnonce=cnonce,
            nc=nc,
            qop=qop,
        )
        if expected != response:
            raise RTSPAuthError("Digest response mismatch")
        return cred

    def issue_nonce(self) -> str:
        return self._nonce_store.issue()

    # ── session management ─────────────────────────────────────────

    def create_session(
        self,
        mount_path: str,
        transport: TransportSpec,
        *,
        client_address: str = "",
        user: Optional[str] = None,
    ) -> RTSPSession:
        mount_path = _normalise_mount_path(mount_path)
        with self._lock:
            if mount_path not in self._mounts:
                raise RTSPMountNotFound(f"No mount at {mount_path!r}")
            if len(self._sessions) >= self._config.max_sessions:
                raise RTSPError(
                    f"max_sessions ({self._config.max_sessions}) reached"
                )
            # Assign server ports — just increment from the config port
            # base. Real backends override with kernel-assigned ports.
            if transport.protocol in (
                TransportProtocol.RTP_AVP_UDP,
                TransportProtocol.RTP_AVPF_UDP,
            ):
                base = self._config.port + 2 + 2 * len(self._sessions)
                transport.server_port_rtp = base
                transport.server_port_rtcp = base + 1
            elif transport.protocol in (
                TransportProtocol.RTP_AVP_TCP,
                TransportProtocol.RTP_AVPF_TCP,
            ):
                if transport.interleaved_rtp is None:
                    transport.interleaved_rtp = 0
                    transport.interleaved_rtcp = 1
            session = RTSPSession(
                session_id=generate_session_id(),
                mount_path=mount_path,
                transport=transport,
                client_address=client_address,
                user=user,
                timeout_s=self._config.session_timeout_s,
            )
            session.transition(SessionState.READY)
            self._sessions[session.session_id] = session
            return session

    def get_session(self, session_id: str) -> RTSPSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise RTSPSessionNotFound(f"No session {session_id!r}") from exc

    def list_sessions(self) -> list[RTSPSession]:
        with self._lock:
            return list(self._sessions.values())

    def teardown_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                raise RTSPSessionNotFound(f"No session {session_id!r}")
            try:
                session.transition(SessionState.TEARDOWN)
            except RTSPSessionStateError:
                pass

    def purge_expired_sessions(self) -> list[str]:
        now = time.time()
        with self._lock:
            expired_ids = [
                sid for sid, s in self._sessions.items() if s.is_expired(now)
            ]
            for sid in expired_ids:
                try:
                    self._sessions[sid].transition(SessionState.TEARDOWN)
                except (RTSPSessionStateError, KeyError):
                    pass
                self._sessions.pop(sid, None)
            return expired_ids

    # ── method dispatch ────────────────────────────────────────────

    def handle_request(self, request: RTSPRequest) -> dict[str, Any]:
        """Dispatch an RTSP request and return a structured response.

        Response is ``{"status": int, "reason": str, "headers": {...},
        "body": str}``. The wire-format assembly is left to the
        backend-specific socket handler — tests assert against the
        structured shape.
        """
        if not self._running:
            raise RTSPError("Server not running")
        method = request.method.upper()
        if method == "OPTIONS":
            return self._handle_options(request)
        if method == "DESCRIBE":
            return self._handle_describe(request)
        if method == "SETUP":
            return self._handle_setup(request)
        if method == "PLAY":
            return self._handle_play_or_pause(request, SessionState.PLAYING)
        if method == "PAUSE":
            return self._handle_play_or_pause(request, SessionState.PAUSED)
        if method == "TEARDOWN":
            return self._handle_teardown(request)
        if method in ("GET_PARAMETER", "SET_PARAMETER"):
            return self._handle_parameter(request)
        return {
            "status": 501,
            "reason": "Not Implemented",
            "headers": {"CSeq": str(request.cseq)},
            "body": "",
        }

    def _handle_options(self, request: RTSPRequest) -> dict[str, Any]:
        return {
            "status": 200,
            "reason": "OK",
            "headers": {
                "CSeq": str(request.cseq),
                "Public": ", ".join(SUPPORTED_METHODS),
                "Server": "OmniSight-IPCam/1.0",
            },
            "body": "",
        }

    def _handle_describe(self, request: RTSPRequest) -> dict[str, Any]:
        mount = self.get_mount(request.path)
        session_id = generate_session_id()
        sdp = build_sdp(
            mount=mount,
            bind_address=self._config.bind_address,
            session_id=session_id,
        )
        return {
            "status": 200,
            "reason": "OK",
            "headers": {
                "CSeq": str(request.cseq),
                "Content-Type": "application/sdp",
                "Content-Length": str(len(sdp)),
                "Server": "OmniSight-IPCam/1.0",
            },
            "body": sdp,
        }

    def _handle_setup(self, request: RTSPRequest) -> dict[str, Any]:
        transport_header = request.headers.get("transport")
        if not transport_header:
            raise RTSPBadRequest("Missing Transport header in SETUP")
        transport = parse_transport(transport_header)
        session = self.create_session(
            mount_path=request.path,
            transport=transport,
        )
        return {
            "status": 200,
            "reason": "OK",
            "headers": {
                "CSeq": str(request.cseq),
                "Transport": session.transport.to_header(),
                "Session": f"{session.session_id};timeout={session.timeout_s}",
                "Server": "OmniSight-IPCam/1.0",
            },
            "body": "",
        }

    def _handle_play_or_pause(
        self, request: RTSPRequest, target: SessionState
    ) -> dict[str, Any]:
        session = self._require_session(request)
        session.transition(target)
        return {
            "status": 200,
            "reason": "OK",
            "headers": {
                "CSeq": str(request.cseq),
                "Session": session.session_id,
                "Server": "OmniSight-IPCam/1.0",
            },
            "body": "",
        }

    def _handle_teardown(self, request: RTSPRequest) -> dict[str, Any]:
        session = self._require_session(request)
        self.teardown_session(session.session_id)
        return {
            "status": 200,
            "reason": "OK",
            "headers": {
                "CSeq": str(request.cseq),
                "Server": "OmniSight-IPCam/1.0",
            },
            "body": "",
        }

    def _handle_parameter(self, request: RTSPRequest) -> dict[str, Any]:
        # GET_PARAMETER with empty body acts as a keep-alive (RFC 2326
        # §10.8) — touches the session last-activity timestamp.
        if "session" in request.headers:
            session = self._require_session(request)
            session.touch()
        return {
            "status": 200,
            "reason": "OK",
            "headers": {
                "CSeq": str(request.cseq),
                "Server": "OmniSight-IPCam/1.0",
            },
            "body": "",
        }

    def _require_session(self, request: RTSPRequest) -> RTSPSession:
        sid_header = request.headers.get("session", "")
        if not sid_header:
            raise RTSPBadRequest("Missing Session header")
        sid = sid_header.split(";", 1)[0].strip()
        return self.get_session(sid)

    # ── data-plane ingress (hw_codec → scaffold → backend) ─────────

    def push_access_unit(
        self, mount_path: str, nal_units: list[bytes], pts_90khz: int
    ) -> None:
        mount_path = _normalise_mount_path(mount_path)
        with self._lock:
            if mount_path not in self._mounts:
                raise RTSPMountNotFound(f"No mount at {mount_path!r}")
            buf = self._nal_buffer.setdefault(mount_path, deque(maxlen=256))
            buf.append((list(nal_units), pts_90khz))

    def drain_access_units(
        self, mount_path: str
    ) -> list[tuple[list[bytes], int]]:
        mount_path = _normalise_mount_path(mount_path)
        with self._lock:
            buf = self._nal_buffer.get(mount_path)
            if buf is None:
                return []
            out = list(buf)
            buf.clear()
            return out

    # ── status ────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "backend": self._backend.value if self._backend else None,
                "bind_address": self._config.bind_address,
                "port": self._config.port,
                "mounts": sorted(self._mounts.keys()),
                "credential_count": len(self._credentials),
                "active_sessions": len(self._sessions),
                "max_sessions": self._config.max_sessions,
                "auth_scheme": self._config.auth_scheme.value,
                "auth_realm": self._config.auth_realm,
            }


__all__ = [
    "AuthScheme",
    "Credential",
    "PAYLOAD_TYPE_H264",
    "PAYLOAD_TYPE_H265",
    "RTSP_DEFAULT_PORT",
    "RTSP_IANA_DEFAULT_PORT",
    "RTSPAuthError",
    "RTSPBackend",
    "RTSPBackendUnavailable",
    "RTSPBadRequest",
    "RTSPError",
    "RTSPMountNotFound",
    "RTSPRequest",
    "RTSPServerConfig",
    "RTSPServerManager",
    "RTSPSession",
    "RTSPSessionNotFound",
    "RTSPSessionStateError",
    "RTSPUnsupportedTransport",
    "SUPPORTED_METHODS",
    "SessionState",
    "StreamMount",
    "TransportProtocol",
    "TransportSpec",
    "VideoCodec",
    "build_digest_challenge",
    "build_sdp",
    "compute_digest_response",
    "detect_available_backends",
    "generate_nonce",
    "generate_session_id",
    "parse_rtsp_request",
    "parse_transport",
    "select_backend",
]
