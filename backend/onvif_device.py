"""D2.2 — SKILL-IPCAM: ONVIF Device / Media / Events / PTZ endpoints (#219).

Profile S SOAP 1.2 service surface that sits in front of the RTSP
scaffold (:mod:`backend.ipcam_rtsp_server`) and turns every RTSP mount
into an ONVIF MediaProfile, exposing the four services NVRs actually
probe:

    ┌──────────── Service surface ────────────┐
    Device service  (/onvif/device_service)
      · GetDeviceInformation / GetSystemDateAndTime / GetCapabilities
      · GetNetworkInterfaces / GetServices / GetServiceCapabilities
      · GetScopes / GetUsers / CreateUsers / DeleteUsers / SetUser
      · GetHostname / SetHostname

    Media service   (/onvif/media_service)
      · GetProfiles / GetProfile / GetVideoSources
      · GetVideoSourceConfigurations / GetVideoEncoderConfigurations
      · GetStreamUri / GetSnapshotUri

    Events service  (/onvif/events_service)
      · GetEventProperties / CreatePullPointSubscription
      · PullMessages / Renew / Unsubscribe

    PTZ service     (/onvif/ptz_service)
      · GetConfigurations / GetConfiguration / GetStatus
      · ContinuousMove / Stop / AbsoluteMove / RelativeMove
      · GetPresets / SetPreset / RemovePreset / GotoPreset

The module is deliberately **framework-agnostic** — it takes raw SOAP
bytes in and returns raw SOAP bytes out. A thin FastAPI / WSGI / ASGI
adapter can wrap :py:meth:`ONVIFDevice.dispatch` in a future commit;
unit tests and the WS-Discovery responder (D2.3) drive the bytes-in
interface directly.

**Module-global state audit (SOP Step 1 mandatory question):**

Zero module-level mutable state. All runtime state — users, media
profiles, PTZ statuses, event subscriptions — lives on the
:py:class:`ONVIFDevice` instance. Callers run exactly one device per
process (same contract as :class:`RTSPServerManager`). Cross-worker
coordination is not required because:

1. RTSP control channels are TCP-pinned to a single worker for the
   session — the NVR's ONVIF SOAP calls land on that same worker when
   the upstream proxy uses IP-hash affinity.
2. ONVIF subscriptions are per-client pull endpoints; clients re-open
   them after a worker restart (the SubscriptionReference carries no
   cross-worker guarantee in the spec).
3. PTZ moves are mechanical — the last-writer-wins race is a real
   device race, not a worker race, and cannot be fixed at this layer.

Category 3 of the SOP ("intentionally per-worker"): documented here so
the next reader does not think the missing cross-worker path is a bug.

**Wire-format note:**

SOAP 1.2 envelope (``http://www.w3.org/2003/05/soap-envelope``) is the
only wire format ONVIF requires; SOAP 1.1 is forbidden by the spec. All
responses carry the matching ``Action`` URI in the WS-Addressing
header so NVR software that validates Action headers (Milestone,
Genetec, Synology) accepts them.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import logging
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend.ipcam_rtsp_server import (
    RTSPServerManager,
    StreamMount,
    VideoCodec,
)

logger = logging.getLogger(__name__)


# ── Namespace URIs ────────────────────────────────────────────────────
#
# Every prefix-URI pair here is spec-exact. The ONVIF test tool (ODTT)
# rejects envelopes whose namespace URIs drift even by a trailing slash,
# so they are declared as constants and never built from f-strings.

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_WSA = "http://www.w3.org/2005/08/addressing"
NS_WSSE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
NS_WSSE_UTIL = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
NS_WSSE_PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
NS_WSSE_PASSWORD_TEXT = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordText"
)
NS_WSNT = "http://docs.oasis-open.org/wsn/b-2"
NS_WSTOP = "http://docs.oasis-open.org/wsn/t-1"
NS_TT = "http://www.onvif.org/ver10/schema"
NS_TDS = "http://www.onvif.org/ver10/device/wsdl"
NS_TRT = "http://www.onvif.org/ver10/media/wsdl"
NS_TEV = "http://www.onvif.org/ver10/events/wsdl"
NS_TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"

# WS-Addressing anonymous URI (RFC 3986 form used in Action replies).
WSA_ANON = "http://www.w3.org/2005/08/addressing/anonymous"

# Fixed XPath → namespace map used in every parse; passing it down
# removes 20+ repetitions of the same dict literal.
XML_NS = {
    "s": NS_SOAP,
    "wsa": NS_WSA,
    "wsse": NS_WSSE,
    "wsse_util": NS_WSSE_UTIL,
    "wsnt": NS_WSNT,
    "wstop": NS_WSTOP,
    "tt": NS_TT,
    "tds": NS_TDS,
    "trt": NS_TRT,
    "tev": NS_TEV,
    "tptz": NS_TPTZ,
}

for _prefix, _uri in XML_NS.items():
    ET.register_namespace("" if _prefix == "s" else _prefix, _uri)


# ── Enums and constants ───────────────────────────────────────────────


class UserLevel(str, enum.Enum):
    """ONVIF tt:UserLevel enumeration — matches the WSDL verbatim."""

    ADMINISTRATOR = "Administrator"
    OPERATOR = "Operator"
    USER = "User"
    ANONYMOUS = "Anonymous"
    EXTENDED = "Extended"


class ONVIFService(str, enum.Enum):
    DEVICE = "device"
    MEDIA = "media"
    EVENTS = "events"
    PTZ = "ptz"


class PTZMoveStatus(str, enum.Enum):
    IDLE = "IDLE"
    MOVING = "MOVING"
    UNKNOWN = "UNKNOWN"


# WS-Addressing Action URIs — format is always
# "<wsdl_namespace>/<PortType>/<OperationName>Request" for the wire and
# "<wsdl_namespace>/<PortType>/<OperationName>Response" for the reply.
# ODTT 20.06 enforces this.
_ACTION_DEVICE_PREFIX = f"{NS_TDS}/Device"
_ACTION_MEDIA_PREFIX = f"{NS_TRT}/Media"
_ACTION_EVENTS_PREFIX = f"{NS_TEV}/NotificationProducer"
_ACTION_PULLPOINT_PREFIX = f"{NS_TEV}/PullPointSubscription"
_ACTION_PTZ_PREFIX = f"{NS_TPTZ}/PTZ"


_NOTIFICATION_MAX_QUEUED = 256  # per-subscription circular buffer
_DEFAULT_SUBSCRIPTION_TIMEOUT_S = 600  # ONVIF recommends 10 minutes
_MAX_SUBSCRIPTION_TIMEOUT_S = 3600
_MAX_PULL_MESSAGE_LIMIT = 256  # hard cap on one PullMessages batch

_USER_NAME_RE = re.compile(r"^[a-zA-Z0-9._\-]{1,63}$")
_SCOPE_URI_RE = re.compile(r"^onvif://www\.onvif\.org/[a-zA-Z0-9_\-/.]+$")


# ── Data classes ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeviceInformation:
    """Static metadata returned by GetDeviceInformation."""

    manufacturer: str = "OmniSight"
    model: str = "IPCam-Reference"
    firmware_version: str = "0.1.0"
    serial_number: str = ""
    hardware_id: str = ""

    def __post_init__(self) -> None:
        if not self.manufacturer:
            raise ValueError("DeviceInformation.manufacturer must be non-empty")
        if not self.model:
            raise ValueError("DeviceInformation.model must be non-empty")
        if not self.firmware_version:
            raise ValueError("DeviceInformation.firmware_version must be non-empty")


@dataclass
class NetworkInterface:
    """Subset of tt:NetworkInterface used by GetNetworkInterfaces."""

    token: str
    enabled: bool = True
    mac_address: str = "02:00:00:00:00:01"
    ipv4_address: str = "192.168.1.100"
    ipv4_prefix_length: int = 24
    mtu: int = 1500
    ipv4_dhcp: bool = False

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("NetworkInterface.token must be non-empty")
        if not 0 <= self.ipv4_prefix_length <= 32:
            raise ValueError(
                f"ipv4_prefix_length must be in [0,32], got {self.ipv4_prefix_length}"
            )


@dataclass
class ONVIFUser:
    """WS-UsernameToken credential (stored plaintext to compute PasswordDigest)."""

    username: str
    password: str
    user_level: UserLevel = UserLevel.USER

    def __post_init__(self) -> None:
        if not _USER_NAME_RE.match(self.username):
            raise ValueError(
                f"Invalid ONVIF username {self.username!r} — must match "
                f"[a-zA-Z0-9._-]{{1,63}}"
            )
        if not self.password:
            raise ValueError("ONVIFUser.password must be non-empty")


@dataclass
class VideoSource:
    """tt:VideoSource — physical camera sensor description."""

    token: str = "VideoSource_1"
    framerate: float = 30.0
    resolution_width: int = 1920
    resolution_height: int = 1080

    def __post_init__(self) -> None:
        if self.resolution_width <= 0 or self.resolution_height <= 0:
            raise ValueError("VideoSource resolution must be positive")
        if self.framerate <= 0:
            raise ValueError("VideoSource framerate must be positive")


@dataclass
class MediaProfile:
    """ONVIF media profile — one per RTSP mount.

    The profile is **derived** from a :class:`StreamMount` at construction
    time; callers that add a mount after construction should call
    :py:meth:`ONVIFDevice.refresh_profiles` to pick it up.
    """

    token: str
    name: str
    mount_path: str
    codec: VideoCodec
    width: int
    height: int
    fps: int
    bitrate_kbps: int
    video_source_token: str = "VideoSource_1"
    ptz_config_token: Optional[str] = None
    fixed: bool = False  # ONVIF: profile cannot be deleted

    @classmethod
    def from_stream_mount(
        cls,
        mount: StreamMount,
        *,
        index: int,
        video_source_token: str = "VideoSource_1",
        ptz_config_token: Optional[str] = None,
        fixed: bool = False,
    ) -> "MediaProfile":
        safe = mount.path.replace("/", "_").replace(".", "_")
        return cls(
            token=f"profile_{index}_{safe}",
            name=mount.description or mount.path,
            mount_path=mount.path,
            codec=mount.codec,
            width=mount.width,
            height=mount.height,
            fps=mount.fps,
            bitrate_kbps=mount.bitrate_kbps,
            video_source_token=video_source_token,
            ptz_config_token=ptz_config_token,
            fixed=fixed,
        )


@dataclass
class PTZConfiguration:
    """tt:PTZConfiguration — bounds + defaults for one PTZ node."""

    token: str = "PTZConfig_1"
    name: str = "DefaultPTZConfig"
    node_token: str = "PTZNode_1"
    pan_range: tuple[float, float] = (-1.0, 1.0)
    tilt_range: tuple[float, float] = (-1.0, 1.0)
    zoom_range: tuple[float, float] = (0.0, 1.0)
    default_pan_tilt_speed: float = 1.0
    default_zoom_speed: float = 1.0

    def __post_init__(self) -> None:
        if self.pan_range[0] >= self.pan_range[1]:
            raise ValueError("PTZ pan_range must be (min < max)")
        if self.tilt_range[0] >= self.tilt_range[1]:
            raise ValueError("PTZ tilt_range must be (min < max)")
        if self.zoom_range[0] >= self.zoom_range[1]:
            raise ValueError("PTZ zoom_range must be (min < max)")


@dataclass
class PTZStatus:
    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 0.0
    move_status: PTZMoveStatus = PTZMoveStatus.IDLE
    last_updated: float = field(default_factory=time.time)

    def clamp(self, config: PTZConfiguration) -> None:
        """Clamp to the config-declared ranges."""
        self.pan = max(config.pan_range[0], min(config.pan_range[1], self.pan))
        self.tilt = max(config.tilt_range[0], min(config.tilt_range[1], self.tilt))
        self.zoom = max(config.zoom_range[0], min(config.zoom_range[1], self.zoom))


@dataclass
class PTZPreset:
    token: str
    name: str
    pan: float
    tilt: float
    zoom: float


@dataclass
class NotificationMessage:
    """Single tt:Message payload queued for a subscription."""

    topic: str  # dotted or slash-separated topic path
    produced_at: float
    source: dict[str, str] = field(default_factory=dict)
    data: dict[str, str] = field(default_factory=dict)


@dataclass
class EventSubscription:
    token: str
    topic_filter: str
    created_at: float
    expires_at: float
    queue: deque[NotificationMessage] = field(
        default_factory=lambda: deque(maxlen=_NOTIFICATION_MAX_QUEUED)
    )

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) >= self.expires_at

    def renew(self, seconds: int) -> None:
        self.expires_at = time.time() + seconds

    def matches(self, topic: str) -> bool:
        """Very small topic-filter engine — supports the two shapes
        ODTT actually exercises: the wildcard ``"//."`` and exact
        topic strings. ``""`` also means "match-all"."""
        if not self.topic_filter or self.topic_filter in ("//.", "*"):
            return True
        if self.topic_filter == topic:
            return True
        # Hierarchical prefix match: "tns1:Device//." matches
        # "tns1:Device/Trigger/Foo".
        base = self.topic_filter.rstrip("/.")
        return topic.startswith(base + "/")


@dataclass
class ONVIFServiceConfig:
    """Static wiring between the device and the HTTP front-end."""

    scheme: str = "http"
    xaddr_host: str = "192.168.1.100"
    xaddr_port: int = 80
    device_service_path: str = "/onvif/device_service"
    media_service_path: str = "/onvif/media_service"
    events_service_path: str = "/onvif/events_service"
    ptz_service_path: str = "/onvif/ptz_service"
    snapshot_uri_template: str = (
        "http://{host}:{port}/onvif/snapshot/{profile_token}"
    )
    rtsp_scheme: str = "rtsp"
    rtsp_host: str = ""  # empty → reuse xaddr_host
    rtsp_port: int = 8554
    require_auth: bool = True
    max_clock_skew_s: int = 300  # WS-Security Created window tolerance
    scopes: tuple[str, ...] = (
        "onvif://www.onvif.org/type/video_encoder",
        "onvif://www.onvif.org/Profile/Streaming",
        "onvif://www.onvif.org/hardware/OmniSight-IPCam",
        "onvif://www.onvif.org/name/OmniSight",
        "onvif://www.onvif.org/location/unconfigured",
    )

    def __post_init__(self) -> None:
        if not 1 <= self.xaddr_port <= 65535:
            raise ValueError(f"xaddr_port out of range: {self.xaddr_port}")
        if not 1 <= self.rtsp_port <= 65535:
            raise ValueError(f"rtsp_port out of range: {self.rtsp_port}")
        if self.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported scheme: {self.scheme!r}")
        for scope in self.scopes:
            if not _SCOPE_URI_RE.match(scope):
                raise ValueError(f"Invalid scope URI: {scope!r}")

    @property
    def device_xaddr(self) -> str:
        return self._xaddr(self.device_service_path)

    @property
    def media_xaddr(self) -> str:
        return self._xaddr(self.media_service_path)

    @property
    def events_xaddr(self) -> str:
        return self._xaddr(self.events_service_path)

    @property
    def ptz_xaddr(self) -> str:
        return self._xaddr(self.ptz_service_path)

    def _xaddr(self, path: str) -> str:
        return f"{self.scheme}://{self.xaddr_host}:{self.xaddr_port}{path}"

    def effective_rtsp_host(self) -> str:
        return self.rtsp_host or self.xaddr_host


# ── Exceptions ────────────────────────────────────────────────────────


class ONVIFError(Exception):
    """Base class for all ONVIF-layer errors."""

    # SOAP Fault Code / Subcode pair — ONVIF WSDL uses "ter:" subcodes
    # but we keep them as plain strings. http_status is what the front-
    # end adapter translates to for the outer transport layer.
    fault_code: str = "env:Receiver"
    fault_subcode: str = "ter:Action"
    http_status: int = 500

    def __init__(self, message: str, *, subcode: Optional[str] = None) -> None:
        super().__init__(message)
        if subcode is not None:
            self.fault_subcode = subcode


class ONVIFBadRequest(ONVIFError):
    fault_code = "env:Sender"
    fault_subcode = "ter:InvalidArgs"
    http_status = 400


class ONVIFAuthError(ONVIFError):
    fault_code = "env:Sender"
    fault_subcode = "ter:NotAuthorized"
    http_status = 401


class ONVIFForbidden(ONVIFError):
    fault_code = "env:Sender"
    fault_subcode = "ter:OperationProhibited"
    http_status = 403


class ONVIFNotFound(ONVIFError):
    fault_code = "env:Sender"
    fault_subcode = "ter:NoEntity"
    http_status = 404


class ONVIFActionNotSupported(ONVIFError):
    fault_code = "env:Sender"
    fault_subcode = "ter:ActionNotSupported"
    http_status = 400


class ONVIFInvalidNetworkInterface(ONVIFError):
    fault_code = "env:Sender"
    fault_subcode = "ter:InvalidNetworkInterface"
    http_status = 400


# ── SOAP envelope helpers ─────────────────────────────────────────────


_SOAP_TEMPLATE_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="{ns_soap}" '
    'xmlns:wsa="{ns_wsa}" '
    'xmlns:tt="{ns_tt}" '
    'xmlns:tds="{ns_tds}" '
    'xmlns:trt="{ns_trt}" '
    'xmlns:tev="{ns_tev}" '
    'xmlns:tptz="{ns_tptz}" '
    'xmlns:wsnt="{ns_wsnt}" '
    'xmlns:wstop="{ns_wstop}">'
).format(
    ns_soap=NS_SOAP,
    ns_wsa=NS_WSA,
    ns_tt=NS_TT,
    ns_tds=NS_TDS,
    ns_trt=NS_TRT,
    ns_tev=NS_TEV,
    ns_tptz=NS_TPTZ,
    ns_wsnt=NS_WSNT,
    ns_wstop=NS_WSTOP,
)


def _xml_escape(text: str) -> str:
    """Minimal XML text-content escape — SOAP bodies are UTF-8 text."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _xml_attr_escape(text: str) -> str:
    """Attribute-value escape — also escapes quotes."""
    return _xml_escape(text).replace('"', "&quot;").replace("'", "&apos;")


def build_soap_response(
    body_xml: str,
    *,
    action: str,
    relates_to: Optional[str] = None,
) -> bytes:
    """Wrap a response body element in a SOAP 1.2 envelope.

    ``action`` is the WS-Addressing action URI advertised in the
    Header; ``relates_to`` is the RequestId MessageID — when omitted no
    RelatesTo header is emitted (the spec allows that for one-way
    responses but every real NVR sends a MessageID).
    """
    parts = [_SOAP_TEMPLATE_HEADER, "<s:Header>"]
    parts.append(
        f'<wsa:Action s:mustUnderstand="true">{_xml_escape(action)}</wsa:Action>'
    )
    if relates_to:
        parts.append(
            f"<wsa:RelatesTo>{_xml_escape(relates_to)}</wsa:RelatesTo>"
        )
    parts.append("</s:Header>")
    parts.append("<s:Body>")
    parts.append(body_xml)
    parts.append("</s:Body>")
    parts.append("</s:Envelope>")
    return "".join(parts).encode("utf-8")


def build_soap_fault(err: ONVIFError) -> bytes:
    """Render an :class:`ONVIFError` as a SOAP 1.2 Fault envelope."""
    reason = _xml_escape(str(err))
    parts = [
        _SOAP_TEMPLATE_HEADER,
        "<s:Header>",
        f'<wsa:Action s:mustUnderstand="true">'
        f"{_xml_escape(NS_WSA + '/fault')}</wsa:Action>",
        "</s:Header>",
        "<s:Body>",
        "<s:Fault>",
        "<s:Code>",
        f"<s:Value>{_xml_escape(err.fault_code)}</s:Value>",
        "<s:Subcode>",
        f"<s:Value>{_xml_escape(err.fault_subcode)}</s:Value>",
        "</s:Subcode>",
        "</s:Code>",
        "<s:Reason>",
        f'<s:Text xml:lang="en">{reason}</s:Text>',
        "</s:Reason>",
        "</s:Fault>",
        "</s:Body>",
        "</s:Envelope>",
    ]
    return "".join(parts).encode("utf-8")


def parse_soap_envelope(
    raw: bytes,
) -> tuple[Optional[str], Optional[str], Optional[ET.Element], ET.Element]:
    """Parse raw SOAP bytes → (action, message_id, security, body_child).

    ``body_child`` is the first element inside ``s:Body`` — the
    operation request element. ``security`` is the ``wsse:Security``
    header element if present, else None. Both ``action`` and
    ``message_id`` are extracted from the ``s:Header`` when present.

    Raises :class:`ONVIFBadRequest` for malformed envelopes.
    """
    if not raw:
        raise ONVIFBadRequest("Empty SOAP envelope")
    try:
        # Strip BOM / leading whitespace some NVR SDKs prepend.
        trimmed = raw.lstrip(b"\xef\xbb\xbf").lstrip()
        root = ET.fromstring(trimmed)
    except ET.ParseError as exc:
        raise ONVIFBadRequest(f"Malformed SOAP XML: {exc}") from exc
    if root.tag != f"{{{NS_SOAP}}}Envelope":
        raise ONVIFBadRequest(
            f"Not a SOAP 1.2 envelope (root={root.tag!r}). "
            "ONVIF forbids SOAP 1.1."
        )
    header = root.find(f"{{{NS_SOAP}}}Header")
    body = root.find(f"{{{NS_SOAP}}}Body")
    if body is None:
        raise ONVIFBadRequest("SOAP envelope has no Body")
    action: Optional[str] = None
    message_id: Optional[str] = None
    security: Optional[ET.Element] = None
    if header is not None:
        a = header.find(f"{{{NS_WSA}}}Action")
        if a is not None and a.text:
            action = a.text.strip()
        m = header.find(f"{{{NS_WSA}}}MessageID")
        if m is not None and m.text:
            message_id = m.text.strip()
        security = header.find(f"{{{NS_WSSE}}}Security")
    body_children = list(body)
    if not body_children:
        raise ONVIFBadRequest("SOAP Body is empty")
    return action, message_id, security, body_children[0]


# ── WS-UsernameToken helpers ──────────────────────────────────────────


def _iso_utc(t: float) -> str:
    """RFC 3339 UTC — ONVIF requires the `Z` suffix, never `+00:00`."""
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(value: str) -> float:
    """Parse the handful of RFC3339 shapes real NVRs emit."""
    if not value:
        raise ONVIFBadRequest("Empty timestamp")
    trimmed = value.strip()
    # Drop sub-second precision so strptime is happy; tolerate `Z` and
    # `+HH:MM` offsets.
    trimmed = re.sub(r"\.(\d+)", "", trimmed)
    if trimmed.endswith("Z"):
        dt = datetime.strptime(trimmed, "%Y-%m-%dT%H:%M:%SZ")
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(trimmed)
        except ValueError as exc:
            raise ONVIFBadRequest(
                f"Invalid ISO-8601 timestamp: {value!r}"
            ) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def compute_password_digest(
    password: str, nonce_bytes: bytes, created_iso: str
) -> str:
    """WS-UsernameToken PasswordDigest's spec-required SHA1 component.

    ``base64(SHA1(nonce + created_utf8 + password_utf8))``.
    """
    sha1 = hashlib.sha1(usedforsecurity=False)
    sha1.update(nonce_bytes)
    sha1.update(created_iso.encode("utf-8"))
    sha1.update(password.encode("utf-8"))
    return base64.b64encode(sha1.digest()).decode("ascii")


def build_username_token(
    username: str,
    password: str,
    *,
    nonce_bytes: Optional[bytes] = None,
    created_iso: Optional[str] = None,
) -> str:
    """Build a client-side ``wsse:Security`` header value.

    Included here so ONVIF client tests can build their own auth header
    and exercise :py:meth:`ONVIFDevice._verify_username_token`.
    """
    nonce_bytes = nonce_bytes or secrets.token_bytes(16)
    created_iso = created_iso or _iso_utc(time.time())
    digest = compute_password_digest(password, nonce_bytes, created_iso)
    nonce_b64 = base64.b64encode(nonce_bytes).decode("ascii")
    return (
        f'<wsse:Security xmlns:wsse="{NS_WSSE}" '
        f'xmlns:wsse_util="{NS_WSSE_UTIL}">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{_xml_escape(username)}</wsse:Username>"
        f'<wsse:Password Type="{NS_WSSE_PASSWORD_DIGEST}">'
        f"{digest}</wsse:Password>"
        f'<wsse:Nonce EncodingType="{NS_WSSE}#Base64Binary">'
        f"{nonce_b64}</wsse:Nonce>"
        f"<wsse_util:Created>{created_iso}</wsse_util:Created>"
        "</wsse:UsernameToken>"
        "</wsse:Security>"
    )


# ── The device ────────────────────────────────────────────────────────


class ONVIFDevice:
    """Thread-safe ONVIF Profile S device.

    The device is bound to a :class:`RTSPServerManager` — each RTSP
    mount automatically becomes a media profile whose
    ``GetStreamUri`` points back at the RTSP manager.
    """

    def __init__(
        self,
        config: ONVIFServiceConfig,
        rtsp_manager: RTSPServerManager,
        *,
        device_info: Optional[DeviceInformation] = None,
        network_interfaces: Optional[list[NetworkInterface]] = None,
        video_sources: Optional[list[VideoSource]] = None,
        ptz_configuration: Optional[PTZConfiguration] = None,
        hostname: str = "omnisight-ipcam",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._rtsp = rtsp_manager
        self._device_info = device_info or DeviceInformation()
        self._network_interfaces = list(
            network_interfaces
            or [NetworkInterface(token="eth0")]
        )
        self._video_sources = list(
            video_sources or [VideoSource()]
        )
        self._ptz_config = ptz_configuration or PTZConfiguration()
        self._ptz_status = PTZStatus()
        self._ptz_presets: dict[str, PTZPreset] = {}
        self._users: dict[str, ONVIFUser] = {}
        self._hostname = hostname
        self._clock = clock
        self._lock = threading.RLock()
        self._profiles: dict[str, MediaProfile] = {}
        self._subscriptions: dict[str, EventSubscription] = {}
        self._used_nonces: dict[str, float] = {}
        self.refresh_profiles()

    # ── user administration ──────────────────────────────────────

    def add_user(
        self,
        username: str,
        password: str,
        level: UserLevel = UserLevel.USER,
    ) -> None:
        user = ONVIFUser(username=username, password=password, user_level=level)
        with self._lock:
            self._users[username] = user

    def remove_user(self, username: str) -> bool:
        with self._lock:
            return self._users.pop(username, None) is not None

    def list_users(self) -> list[ONVIFUser]:
        with self._lock:
            return list(self._users.values())

    def get_user(self, username: str) -> Optional[ONVIFUser]:
        with self._lock:
            return self._users.get(username)

    # ── profiles ─────────────────────────────────────────────────

    def refresh_profiles(self) -> None:
        """Sync media profiles with the RTSP manager's mount list."""
        with self._lock:
            self._profiles.clear()
            for i, path in enumerate(self._rtsp.list_mounts()):
                mount = self._rtsp.get_mount(path)
                self._profiles[
                    f"profile_{i}_{path.replace('/', '_').replace('.', '_')}"
                ] = MediaProfile.from_stream_mount(
                    mount,
                    index=i,
                    video_source_token=self._video_sources[0].token
                    if self._video_sources
                    else "VideoSource_1",
                    ptz_config_token=self._ptz_config.token,
                    fixed=(i == 0),
                )

    def list_profiles(self) -> list[MediaProfile]:
        with self._lock:
            return list(self._profiles.values())

    def get_profile(self, token: str) -> MediaProfile:
        with self._lock:
            try:
                return self._profiles[token]
            except KeyError as exc:
                raise ONVIFNotFound(
                    f"No media profile {token!r}",
                    subcode="ter:NoProfile",
                ) from exc

    # ── PTZ ──────────────────────────────────────────────────────

    def get_ptz_status(self) -> PTZStatus:
        with self._lock:
            return PTZStatus(
                pan=self._ptz_status.pan,
                tilt=self._ptz_status.tilt,
                zoom=self._ptz_status.zoom,
                move_status=self._ptz_status.move_status,
                last_updated=self._ptz_status.last_updated,
            )

    def continuous_move(
        self, pan_speed: float, tilt_speed: float, zoom_speed: float
    ) -> None:
        with self._lock:
            self._ptz_status.move_status = PTZMoveStatus.MOVING
            # A real driver would dispatch to the motor controller; the
            # in-memory model simulates the integration over a 100 ms
            # tick. Tests drive the clock deterministically via
            # :py:meth:`tick_ptz`.
            self._ptz_last_velocity = (pan_speed, tilt_speed, zoom_speed)
            self._ptz_status.last_updated = self._clock()

    def stop_ptz(self, stop_pan_tilt: bool = True, stop_zoom: bool = True) -> None:
        with self._lock:
            if stop_pan_tilt and stop_zoom:
                self._ptz_status.move_status = PTZMoveStatus.IDLE
            self._ptz_last_velocity = (0.0, 0.0, 0.0)
            self._ptz_status.last_updated = self._clock()

    def absolute_move(self, pan: float, tilt: float, zoom: float) -> None:
        with self._lock:
            self._ptz_status.pan = pan
            self._ptz_status.tilt = tilt
            self._ptz_status.zoom = zoom
            self._ptz_status.clamp(self._ptz_config)
            self._ptz_status.move_status = PTZMoveStatus.IDLE
            self._ptz_status.last_updated = self._clock()

    def relative_move(
        self, pan_delta: float, tilt_delta: float, zoom_delta: float
    ) -> None:
        with self._lock:
            self.absolute_move(
                self._ptz_status.pan + pan_delta,
                self._ptz_status.tilt + tilt_delta,
                self._ptz_status.zoom + zoom_delta,
            )

    def set_preset(self, name: str, token: Optional[str] = None) -> PTZPreset:
        token = token or f"preset_{len(self._ptz_presets) + 1}"
        with self._lock:
            preset = PTZPreset(
                token=token,
                name=name,
                pan=self._ptz_status.pan,
                tilt=self._ptz_status.tilt,
                zoom=self._ptz_status.zoom,
            )
            self._ptz_presets[token] = preset
            return preset

    def remove_preset(self, token: str) -> bool:
        with self._lock:
            return self._ptz_presets.pop(token, None) is not None

    def list_presets(self) -> list[PTZPreset]:
        with self._lock:
            return list(self._ptz_presets.values())

    def goto_preset(self, token: str) -> None:
        with self._lock:
            preset = self._ptz_presets.get(token)
            if preset is None:
                raise ONVIFNotFound(
                    f"No PTZ preset {token!r}", subcode="ter:NoEntity"
                )
            self.absolute_move(preset.pan, preset.tilt, preset.zoom)

    # ── events ───────────────────────────────────────────────────

    def create_subscription(
        self, topic_filter: str = "", timeout_s: Optional[int] = None
    ) -> EventSubscription:
        if timeout_s is None:
            timeout_s = _DEFAULT_SUBSCRIPTION_TIMEOUT_S
        if not 1 <= timeout_s <= _MAX_SUBSCRIPTION_TIMEOUT_S:
            raise ONVIFBadRequest(
                f"Subscription timeout out of range: {timeout_s}",
                subcode="ter:InvalidArgVal",
            )
        with self._lock:
            token = secrets.token_hex(8)
            now = self._clock()
            sub = EventSubscription(
                token=token,
                topic_filter=topic_filter or "",
                created_at=now,
                expires_at=now + timeout_s,
            )
            self._subscriptions[token] = sub
            return sub

    def publish_event(self, message: NotificationMessage) -> int:
        """Enqueue ``message`` on every matching subscription.

        Returns the number of subscriptions that received it.
        """
        delivered = 0
        with self._lock:
            for sub in list(self._subscriptions.values()):
                if sub.is_expired(self._clock()):
                    continue
                if sub.matches(message.topic):
                    sub.queue.append(message)
                    delivered += 1
            return delivered

    def pull_messages(
        self, token: str, limit: int = 10, timeout_s: int = 0
    ) -> list[NotificationMessage]:
        if limit <= 0 or limit > _MAX_PULL_MESSAGE_LIMIT:
            raise ONVIFBadRequest(
                f"PullMessages MessageLimit out of range: {limit}",
                subcode="ter:InvalidArgVal",
            )
        with self._lock:
            sub = self._subscriptions.get(token)
            if sub is None:
                raise ONVIFNotFound(
                    f"No subscription {token!r}",
                    subcode="ter:InvalidSubscriptionReference",
                )
            if sub.is_expired(self._clock()):
                raise ONVIFNotFound(
                    "Subscription expired", subcode="ter:UnableToGetMessages"
                )
            out: list[NotificationMessage] = []
            for _ in range(limit):
                if not sub.queue:
                    break
                out.append(sub.queue.popleft())
            return out

    def renew_subscription(self, token: str, timeout_s: int) -> EventSubscription:
        if not 1 <= timeout_s <= _MAX_SUBSCRIPTION_TIMEOUT_S:
            raise ONVIFBadRequest(
                f"Renew timeout out of range: {timeout_s}",
                subcode="ter:InvalidArgVal",
            )
        with self._lock:
            sub = self._subscriptions.get(token)
            if sub is None:
                raise ONVIFNotFound(
                    f"No subscription {token!r}",
                    subcode="ter:InvalidSubscriptionReference",
                )
            sub.renew(timeout_s)
            return sub

    def unsubscribe(self, token: str) -> None:
        with self._lock:
            if self._subscriptions.pop(token, None) is None:
                raise ONVIFNotFound(
                    f"No subscription {token!r}",
                    subcode="ter:InvalidSubscriptionReference",
                )

    def purge_expired_subscriptions(self) -> list[str]:
        now = self._clock()
        with self._lock:
            expired = [
                k for k, v in self._subscriptions.items() if v.is_expired(now)
            ]
            for k in expired:
                self._subscriptions.pop(k, None)
            return expired

    def list_subscriptions(self) -> list[EventSubscription]:
        with self._lock:
            return list(self._subscriptions.values())

    # ── auth ─────────────────────────────────────────────────────

    def _verify_username_token(self, security: Optional[ET.Element]) -> Optional[ONVIFUser]:
        """Verify a ``wsse:Security / UsernameToken`` header.

        Returns the matching user or raises :class:`ONVIFAuthError`.
        When auth is disabled in the config the header is ignored and
        None is returned.
        """
        if not self._config.require_auth:
            return None
        if security is None:
            raise ONVIFAuthError(
                "Missing wsse:Security header", subcode="ter:NotAuthorized"
            )
        token = security.find(f"{{{NS_WSSE}}}UsernameToken")
        if token is None:
            raise ONVIFAuthError(
                "Missing UsernameToken", subcode="ter:NotAuthorized"
            )
        username_el = token.find(f"{{{NS_WSSE}}}Username")
        password_el = token.find(f"{{{NS_WSSE}}}Password")
        nonce_el = token.find(f"{{{NS_WSSE}}}Nonce")
        created_el = token.find(f"{{{NS_WSSE_UTIL}}}Created")
        if username_el is None or password_el is None or created_el is None:
            raise ONVIFAuthError(
                "Incomplete UsernameToken", subcode="ter:NotAuthorized"
            )
        username = (username_el.text or "").strip()
        supplied = (password_el.text or "").strip()
        created_iso = (created_el.text or "").strip()
        created_ts = _parse_iso_utc(created_iso)
        now = self._clock()
        if abs(now - created_ts) > self._config.max_clock_skew_s:
            raise ONVIFAuthError(
                f"WSS Created outside clock skew window ({int(now - created_ts)}s)",
                subcode="ter:NotAuthorized",
            )
        user = self._users.get(username)
        if user is None:
            raise ONVIFAuthError(
                f"Unknown user {username!r}", subcode="ter:NotAuthorized"
            )
        p_type = password_el.get("Type") or NS_WSSE_PASSWORD_TEXT
        if p_type == NS_WSSE_PASSWORD_TEXT:
            if supplied != user.password:
                raise ONVIFAuthError(
                    "Password mismatch", subcode="ter:NotAuthorized"
                )
        elif p_type == NS_WSSE_PASSWORD_DIGEST:
            if nonce_el is None or not (nonce_el.text or "").strip():
                raise ONVIFAuthError(
                    "PasswordDigest requires Nonce", subcode="ter:NotAuthorized"
                )
            nonce_b64 = (nonce_el.text or "").strip()
            try:
                nonce_bytes = base64.b64decode(nonce_b64, validate=True)
            except Exception as exc:
                raise ONVIFAuthError(
                    f"Malformed Nonce: {exc}", subcode="ter:NotAuthorized"
                ) from exc
            # Anti-replay: reject a nonce we have already seen within
            # the clock-skew window. Keys are timestamp-prefixed so
            # garbage-collection is trivial.
            self._gc_nonces(now)
            if nonce_b64 in self._used_nonces:
                raise ONVIFAuthError(
                    "Replayed nonce", subcode="ter:NotAuthorized"
                )
            expected = compute_password_digest(
                user.password, nonce_bytes, created_iso
            )
            if expected != supplied:
                raise ONVIFAuthError(
                    "Digest mismatch", subcode="ter:NotAuthorized"
                )
            self._used_nonces[nonce_b64] = created_ts
        else:
            raise ONVIFAuthError(
                f"Unsupported Password Type {p_type!r}",
                subcode="ter:NotAuthorized",
            )
        return user

    def _gc_nonces(self, now: float) -> None:
        cutoff = now - self._config.max_clock_skew_s - 60
        stale = [k for k, ts in self._used_nonces.items() if ts < cutoff]
        for k in stale:
            self._used_nonces.pop(k, None)

    # ── dispatch ─────────────────────────────────────────────────

    def dispatch(
        self,
        service: ONVIFService,
        raw: bytes,
        *,
        remote_address: str = "",
    ) -> tuple[int, bytes]:
        """Handle a single SOAP request and return (http_status, body).

        This is the canonical entry-point. A front-end adapter maps it
        to an HTTP handler on the matching ``/onvif/<service>_service``
        path. Errors are always rendered as SOAP Faults (200 with Fault
        Body is also accepted by ONVIF, but we follow the more-common
        convention of setting an appropriate HTTP status).
        """
        try:
            action, message_id, security, op = parse_soap_envelope(raw)
            self._verify_username_token(security)
            return self._dispatch_operation(
                service=service,
                op=op,
                action=action,
                message_id=message_id,
            )
        except ONVIFError as err:
            logger.info(
                "ONVIF fault: service=%s remote=%s code=%s: %s",
                service.value,
                remote_address,
                err.fault_subcode,
                err,
            )
            return err.http_status, build_soap_fault(err)

    def _dispatch_operation(
        self,
        *,
        service: ONVIFService,
        op: ET.Element,
        action: Optional[str],
        message_id: Optional[str],
    ) -> tuple[int, bytes]:
        tag = op.tag
        # Split "{namespace}Local" → (ns, local)
        if tag.startswith("{"):
            ns_uri, local = tag[1:].split("}", 1)
        else:
            ns_uri, local = "", tag
        handler_key = (service, local)
        handler = _OPERATION_HANDLERS.get(handler_key)
        if handler is None:
            raise ONVIFActionNotSupported(
                f"Unsupported operation {local!r} on {service.value!r} service"
            )
        body_xml, response_action = handler(self, op)
        return (
            200,
            build_soap_response(
                body_xml,
                action=response_action,
                relates_to=message_id,
            ),
        )

    # ── internal: body builders ──────────────────────────────────
    #
    # The builders emit minimal-but-spec-valid XML. Attribute order is
    # stable so unit tests can compare the output byte-for-byte.

    def _xml_device_information(self) -> str:
        d = self._device_info
        return (
            "<tds:GetDeviceInformationResponse>"
            f"<tds:Manufacturer>{_xml_escape(d.manufacturer)}</tds:Manufacturer>"
            f"<tds:Model>{_xml_escape(d.model)}</tds:Model>"
            f"<tds:FirmwareVersion>{_xml_escape(d.firmware_version)}</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{_xml_escape(d.serial_number)}</tds:SerialNumber>"
            f"<tds:HardwareId>{_xml_escape(d.hardware_id)}</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )

    def _xml_system_date_and_time(self) -> str:
        now = self._clock()
        dt = datetime.fromtimestamp(now, tz=timezone.utc)
        return (
            "<tds:GetSystemDateAndTimeResponse>"
            "<tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType>"
            "<tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:TimeZone>"
            "<tt:TZ>UTC0</tt:TZ>"
            "</tt:TimeZone>"
            "<tt:UTCDateTime>"
            "<tt:Time>"
            f"<tt:Hour>{dt.hour}</tt:Hour>"
            f"<tt:Minute>{dt.minute}</tt:Minute>"
            f"<tt:Second>{dt.second}</tt:Second>"
            "</tt:Time>"
            "<tt:Date>"
            f"<tt:Year>{dt.year}</tt:Year>"
            f"<tt:Month>{dt.month}</tt:Month>"
            f"<tt:Day>{dt.day}</tt:Day>"
            "</tt:Date>"
            "</tt:UTCDateTime>"
            "</tds:SystemDateAndTime>"
            "</tds:GetSystemDateAndTimeResponse>"
        )

    def _xml_capabilities(self) -> str:
        c = self._config
        return (
            "<tds:GetCapabilitiesResponse>"
            "<tds:Capabilities>"
            "<tt:Device>"
            f"<tt:XAddr>{_xml_escape(c.device_xaddr)}</tt:XAddr>"
            "<tt:Network><tt:IPFilter>false</tt:IPFilter>"
            "<tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
            "<tt:IPVersion6>false</tt:IPVersion6>"
            "<tt:DynDNS>false</tt:DynDNS></tt:Network>"
            "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve>"
            "<tt:DiscoveryBye>true</tt:DiscoveryBye>"
            "<tt:RemoteDiscovery>false</tt:RemoteDiscovery>"
            "<tt:SystemBackup>false</tt:SystemBackup>"
            "<tt:SystemLogging>false</tt:SystemLogging>"
            "<tt:FirmwareUpgrade>false</tt:FirmwareUpgrade></tt:System>"
            "<tt:Security><tt:TLS1.1>false</tt:TLS1.1>"
            "<tt:TLS1.2>true</tt:TLS1.2>"
            "<tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration>"
            "<tt:AccessPolicyConfig>false</tt:AccessPolicyConfig>"
            "<tt:X.509Token>false</tt:X.509Token>"
            "<tt:SAMLToken>false</tt:SAMLToken>"
            "<tt:KerberosToken>false</tt:KerberosToken>"
            "<tt:UsernameToken>true</tt:UsernameToken>"
            "<tt:HttpDigest>false</tt:HttpDigest>"
            "<tt:RELToken>false</tt:RELToken></tt:Security>"
            "</tt:Device>"
            "<tt:Events>"
            f"<tt:XAddr>{_xml_escape(c.events_xaddr)}</tt:XAddr>"
            "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
            "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
            "<tt:WSPausableSubscriptionManagerInterfaceSupport>false"
            "</tt:WSPausableSubscriptionManagerInterfaceSupport>"
            "</tt:Events>"
            "<tt:Media>"
            f"<tt:XAddr>{_xml_escape(c.media_xaddr)}</tt:XAddr>"
            "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>true</tt:RTP_TCP>"
            "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities>"
            "</tt:Media>"
            "<tt:PTZ>"
            f"<tt:XAddr>{_xml_escape(c.ptz_xaddr)}</tt:XAddr>"
            "</tt:PTZ>"
            "</tds:Capabilities>"
            "</tds:GetCapabilitiesResponse>"
        )

    def _xml_network_interfaces(self) -> str:
        parts = ["<tds:GetNetworkInterfacesResponse>"]
        for nic in self._network_interfaces:
            parts.append(
                f'<tds:NetworkInterfaces token="{_xml_attr_escape(nic.token)}">'
                f"<tt:Enabled>{str(nic.enabled).lower()}</tt:Enabled>"
                f"<tt:Info>"
                f"<tt:Name>{_xml_escape(nic.token)}</tt:Name>"
                f"<tt:HwAddress>{_xml_escape(nic.mac_address)}</tt:HwAddress>"
                f"<tt:MTU>{nic.mtu}</tt:MTU>"
                f"</tt:Info>"
                f"<tt:IPv4>"
                f"<tt:Enabled>true</tt:Enabled>"
                f"<tt:Config>"
                f"<tt:Manual>"
                f"<tt:Address>{_xml_escape(nic.ipv4_address)}</tt:Address>"
                f"<tt:PrefixLength>{nic.ipv4_prefix_length}</tt:PrefixLength>"
                f"</tt:Manual>"
                f"<tt:DHCP>{str(nic.ipv4_dhcp).lower()}</tt:DHCP>"
                f"</tt:Config>"
                f"</tt:IPv4>"
                f"</tds:NetworkInterfaces>"
            )
        parts.append("</tds:GetNetworkInterfacesResponse>")
        return "".join(parts)

    def _xml_services(self, include_capability: bool) -> str:
        c = self._config
        entries = [
            ("tds", c.device_xaddr, NS_TDS, "2.42.2"),
            ("trt", c.media_xaddr, NS_TRT, "2.42.2"),
            ("tev", c.events_xaddr, NS_TEV, "2.42.2"),
            ("tptz", c.ptz_xaddr, NS_TPTZ, "2.42.2"),
        ]
        parts = ["<tds:GetServicesResponse>"]
        for _prefix, xaddr, ns, version in entries:
            parts.append(
                "<tds:Service>"
                f"<tds:Namespace>{_xml_escape(ns)}</tds:Namespace>"
                f"<tds:XAddr>{_xml_escape(xaddr)}</tds:XAddr>"
                "<tds:Version>"
                "<tt:Major>2</tt:Major>"
                "<tt:Minor>42</tt:Minor>"
                "</tds:Version>"
                "</tds:Service>"
            )
        if include_capability:
            # Capability blocks are optional (WSDL docs say "if client
            # sent IncludeCapability=true"). Empty placeholders keep
            # the response well-formed.
            pass
        parts.append("</tds:GetServicesResponse>")
        return "".join(parts)

    def _xml_service_capabilities(self, service: ONVIFService) -> str:
        if service == ONVIFService.DEVICE:
            return (
                "<tds:GetServiceCapabilitiesResponse>"
                "<tds:Capabilities>"
                '<tds:Network IPFilter="false" ZeroConfiguration="false" '
                'IPVersion6="false" DynDNS="false" '
                'Dot11Configuration="false"/>'
                '<tds:Security TLS1.1="false" TLS1.2="true" '
                'OnboardKeyGeneration="false" '
                'AccessPolicyConfig="false" UsernameToken="true" '
                'HttpDigest="false"/>'
                '<tds:System DiscoveryResolve="false" DiscoveryBye="true" '
                'RemoteDiscovery="false" SystemBackup="false" '
                'SystemLogging="false" FirmwareUpgrade="false"/>'
                "</tds:Capabilities>"
                "</tds:GetServiceCapabilitiesResponse>"
            )
        if service == ONVIFService.MEDIA:
            return (
                "<trt:GetServiceCapabilitiesResponse>"
                '<trt:Capabilities SnapshotUri="true" Rotation="false" '
                'VideoSourceMode="false" OSD="false" EXICompression="false">'
                '<trt:ProfileCapabilities MaximumNumberOfProfiles="32"/>'
                '<trt:StreamingCapabilities RTPMulticast="false" '
                'RTP_TCP="true" RTP_RTSP_TCP="true" NonAggregateControl="false" '
                'NoRTSPStreaming="false"/>'
                "</trt:Capabilities>"
                "</trt:GetServiceCapabilitiesResponse>"
            )
        if service == ONVIFService.EVENTS:
            return (
                "<tev:GetServiceCapabilitiesResponse>"
                '<tev:Capabilities WSSubscriptionPolicySupport="false" '
                'WSPullPointSupport="true" '
                'WSPausableSubscriptionManagerInterfaceSupport="false" '
                'MaxNotificationProducers="16" '
                'MaxPullPoints="16" '
                'PersistentNotificationStorage="false"/>'
                "</tev:GetServiceCapabilitiesResponse>"
            )
        if service == ONVIFService.PTZ:
            return (
                "<tptz:GetServiceCapabilitiesResponse>"
                '<tptz:Capabilities EFlip="false" Reverse="false" '
                'GetCompatibleConfigurations="false" '
                'MoveStatus="true" StatusPosition="true"/>'
                "</tptz:GetServiceCapabilitiesResponse>"
            )
        raise ONVIFBadRequest(f"Unknown service {service!r}")

    def _xml_scopes(self) -> str:
        parts = ["<tds:GetScopesResponse>"]
        for scope in self._config.scopes:
            parts.append(
                "<tds:Scopes>"
                "<tt:ScopeDef>Fixed</tt:ScopeDef>"
                f"<tt:ScopeItem>{_xml_escape(scope)}</tt:ScopeItem>"
                "</tds:Scopes>"
            )
        parts.append("</tds:GetScopesResponse>")
        return "".join(parts)

    def _xml_hostname(self) -> str:
        return (
            "<tds:GetHostnameResponse>"
            "<tds:HostnameInformation>"
            "<tt:FromDHCP>false</tt:FromDHCP>"
            f"<tt:Name>{_xml_escape(self._hostname)}</tt:Name>"
            "</tds:HostnameInformation>"
            "</tds:GetHostnameResponse>"
        )

    def _xml_get_users(self) -> str:
        parts = ["<tds:GetUsersResponse>"]
        with self._lock:
            users = list(self._users.values())
        for u in users:
            parts.append(
                "<tds:User>"
                f"<tt:Username>{_xml_escape(u.username)}</tt:Username>"
                f"<tt:UserLevel>{u.user_level.value}</tt:UserLevel>"
                "</tds:User>"
            )
        parts.append("</tds:GetUsersResponse>")
        return "".join(parts)

    def _xml_profile(self, profile: MediaProfile) -> str:
        """Emit one <trt:Profiles> element (shared by GetProfiles/GetProfile)."""
        enc_name = "H264" if profile.codec == VideoCodec.H264 else "H265"
        ptz_block = ""
        if profile.ptz_config_token:
            ptz_block = (
                f'<tt:PTZConfiguration token="{_xml_attr_escape(profile.ptz_config_token)}">'
                f"<tt:Name>{_xml_escape(self._ptz_config.name)}</tt:Name>"
                "<tt:UseCount>1</tt:UseCount>"
                f"<tt:NodeToken>{_xml_escape(self._ptz_config.node_token)}</tt:NodeToken>"
                "</tt:PTZConfiguration>"
            )
        return (
            f'<trt:Profiles fixed="{str(profile.fixed).lower()}" '
            f'token="{_xml_attr_escape(profile.token)}">'
            f"<tt:Name>{_xml_escape(profile.name)}</tt:Name>"
            f'<tt:VideoSourceConfiguration token="VideoSourceConfig_1">'
            "<tt:Name>VideoSourceConfig</tt:Name>"
            "<tt:UseCount>1</tt:UseCount>"
            f"<tt:SourceToken>{_xml_escape(profile.video_source_token)}</tt:SourceToken>"
            '<tt:Bounds x="0" y="0" '
            f'width="{profile.width}" height="{profile.height}"/>'
            "</tt:VideoSourceConfiguration>"
            f'<tt:VideoEncoderConfiguration token="VideoEncoderConfig_{profile.token}">'
            f"<tt:Name>{_xml_escape(profile.name)}-encoder</tt:Name>"
            "<tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>{enc_name}</tt:Encoding>"
            "<tt:Resolution>"
            f"<tt:Width>{profile.width}</tt:Width>"
            f"<tt:Height>{profile.height}</tt:Height>"
            "</tt:Resolution>"
            "<tt:Quality>5</tt:Quality>"
            "<tt:RateControl>"
            f"<tt:FrameRateLimit>{profile.fps}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{profile.bitrate_kbps}</tt:BitrateLimit>"
            "</tt:RateControl>"
            "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
            "</tt:VideoEncoderConfiguration>"
            f"{ptz_block}"
            "</trt:Profiles>"
        )

    def _xml_video_sources(self) -> str:
        parts = ["<trt:GetVideoSourcesResponse>"]
        for src in self._video_sources:
            parts.append(
                f'<trt:VideoSources token="{_xml_attr_escape(src.token)}">'
                f"<tt:Framerate>{src.framerate}</tt:Framerate>"
                "<tt:Resolution>"
                f"<tt:Width>{src.resolution_width}</tt:Width>"
                f"<tt:Height>{src.resolution_height}</tt:Height>"
                "</tt:Resolution>"
                "</trt:VideoSources>"
            )
        parts.append("</trt:GetVideoSourcesResponse>")
        return "".join(parts)

    def _xml_video_source_configurations(self) -> str:
        parts = ["<trt:GetVideoSourceConfigurationsResponse>"]
        for src in self._video_sources:
            parts.append(
                '<trt:Configurations token="VideoSourceConfig_1">'
                "<tt:Name>VideoSourceConfig</tt:Name>"
                "<tt:UseCount>1</tt:UseCount>"
                f"<tt:SourceToken>{_xml_escape(src.token)}</tt:SourceToken>"
                f'<tt:Bounds x="0" y="0" '
                f'width="{src.resolution_width}" height="{src.resolution_height}"/>'
                "</trt:Configurations>"
            )
        parts.append("</trt:GetVideoSourceConfigurationsResponse>")
        return "".join(parts)

    def _xml_video_encoder_configurations(self) -> str:
        parts = ["<trt:GetVideoEncoderConfigurationsResponse>"]
        with self._lock:
            profiles = list(self._profiles.values())
        for profile in profiles:
            enc_name = "H264" if profile.codec == VideoCodec.H264 else "H265"
            parts.append(
                f'<trt:Configurations token="VideoEncoderConfig_{profile.token}">'
                f"<tt:Name>{_xml_escape(profile.name)}-encoder</tt:Name>"
                "<tt:UseCount>1</tt:UseCount>"
                f"<tt:Encoding>{enc_name}</tt:Encoding>"
                "<tt:Resolution>"
                f"<tt:Width>{profile.width}</tt:Width>"
                f"<tt:Height>{profile.height}</tt:Height>"
                "</tt:Resolution>"
                "<tt:Quality>5</tt:Quality>"
                "<tt:RateControl>"
                f"<tt:FrameRateLimit>{profile.fps}</tt:FrameRateLimit>"
                "<tt:EncodingInterval>1</tt:EncodingInterval>"
                f"<tt:BitrateLimit>{profile.bitrate_kbps}</tt:BitrateLimit>"
                "</tt:RateControl>"
                "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
                "</trt:Configurations>"
            )
        parts.append("</trt:GetVideoEncoderConfigurationsResponse>")
        return "".join(parts)

    def _xml_stream_uri(self, profile: MediaProfile) -> str:
        c = self._config
        uri = (
            f"{c.rtsp_scheme}://{c.effective_rtsp_host()}:{c.rtsp_port}/"
            f"{profile.mount_path}"
        )
        return (
            "<trt:GetStreamUriResponse>"
            "<trt:MediaUri>"
            f"<tt:Uri>{_xml_escape(uri)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri>"
            "</trt:GetStreamUriResponse>"
        )

    def _xml_snapshot_uri(self, profile: MediaProfile) -> str:
        c = self._config
        uri = c.snapshot_uri_template.format(
            host=c.xaddr_host,
            port=c.xaddr_port,
            profile_token=profile.token,
        )
        return (
            "<trt:GetSnapshotUriResponse>"
            "<trt:MediaUri>"
            f"<tt:Uri>{_xml_escape(uri)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri>"
            "</trt:GetSnapshotUriResponse>"
        )

    # ── Events body builders ─────────────────────────────────────

    def _xml_event_properties(self) -> str:
        # Minimal TopicSet — declares a single custom topic namespace
        # "tns1" covering motion & tamper alarms (typical IPCam events).
        return (
            "<tev:GetEventPropertiesResponse>"
            "<tev:TopicNamespaceLocation>"
            f"{_xml_escape(NS_TT + '/topicns.xml')}"
            "</tev:TopicNamespaceLocation>"
            '<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>'
            "<wstop:TopicSet>"
            '<tns1:RuleEngine xmlns:tns1="http://www.onvif.org/ver10/topics">'
            "<tns1:MotionRegionDetector/>"
            "<tns1:TamperDetector/>"
            "</tns1:RuleEngine>"
            "</wstop:TopicSet>"
            "<wsnt:TopicExpressionDialect>"
            f"{_xml_escape(NS_WSNT + '/TopicExpression/Concrete')}"
            "</wsnt:TopicExpressionDialect>"
            "<tev:MessageContentFilterDialect>"
            "http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter"
            "</tev:MessageContentFilterDialect>"
            "</tev:GetEventPropertiesResponse>"
        )

    def _xml_subscription_reference(self, sub: EventSubscription) -> str:
        c = self._config
        addr = (
            f"{c.scheme}://{c.xaddr_host}:{c.xaddr_port}"
            f"{c.events_service_path}?SubscriptionId={sub.token}"
        )
        return (
            "<wsa:EndpointReference>"
            f"<wsa:Address>{_xml_escape(addr)}</wsa:Address>"
            "<wsa:ReferenceParameters>"
            f"<tev:SubscriptionId>{_xml_escape(sub.token)}</tev:SubscriptionId>"
            "</wsa:ReferenceParameters>"
            "</wsa:EndpointReference>"
        )

    def _xml_create_pullpoint_response(self, sub: EventSubscription) -> str:
        return (
            "<tev:CreatePullPointSubscriptionResponse>"
            f"<tev:SubscriptionReference>"
            f"{self._xml_subscription_reference(sub)}"
            "</tev:SubscriptionReference>"
            f"<wsnt:CurrentTime>{_iso_utc(sub.created_at)}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{_iso_utc(sub.expires_at)}</wsnt:TerminationTime>"
            "</tev:CreatePullPointSubscriptionResponse>"
        )

    def _xml_pull_messages_response(
        self,
        sub: EventSubscription,
        messages: list[NotificationMessage],
    ) -> str:
        now = self._clock()
        parts = [
            "<tev:PullMessagesResponse>",
            f"<tev:CurrentTime>{_iso_utc(now)}</tev:CurrentTime>",
            f"<tev:TerminationTime>{_iso_utc(sub.expires_at)}</tev:TerminationTime>",
        ]
        for msg in messages:
            source_items = "".join(
                f'<tt:SimpleItem Name="{_xml_attr_escape(k)}" '
                f'Value="{_xml_attr_escape(v)}"/>'
                for k, v in sorted(msg.source.items())
            )
            data_items = "".join(
                f'<tt:SimpleItem Name="{_xml_attr_escape(k)}" '
                f'Value="{_xml_attr_escape(v)}"/>'
                for k, v in sorted(msg.data.items())
            )
            source_block = (
                f"<tt:Source>{source_items}</tt:Source>" if source_items else ""
            )
            data_block = (
                f"<tt:Data>{data_items}</tt:Data>" if data_items else ""
            )
            parts.append(
                "<wsnt:NotificationMessage>"
                f"<wsnt:Topic>{_xml_escape(msg.topic)}</wsnt:Topic>"
                "<wsnt:Message>"
                f'<tt:Message UtcTime="{_iso_utc(msg.produced_at)}" '
                'PropertyOperation="Initialized">'
                f"{source_block}{data_block}"
                "</tt:Message>"
                "</wsnt:Message>"
                "</wsnt:NotificationMessage>"
            )
        parts.append("</tev:PullMessagesResponse>")
        return "".join(parts)

    def _xml_renew_response(self, sub: EventSubscription) -> str:
        now = self._clock()
        return (
            "<wsnt:RenewResponse>"
            f"<wsnt:CurrentTime>{_iso_utc(now)}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{_iso_utc(sub.expires_at)}</wsnt:TerminationTime>"
            "</wsnt:RenewResponse>"
        )

    def _xml_unsubscribe_response(self) -> str:
        return "<wsnt:UnsubscribeResponse/>"

    # ── PTZ body builders ────────────────────────────────────────

    def _xml_ptz_configurations(self) -> str:
        cfg = self._ptz_config
        return (
            "<tptz:GetConfigurationsResponse>"
            f'<tptz:PTZConfiguration token="{_xml_attr_escape(cfg.token)}">'
            f"<tt:Name>{_xml_escape(cfg.name)}</tt:Name>"
            "<tt:UseCount>1</tt:UseCount>"
            f"<tt:NodeToken>{_xml_escape(cfg.node_token)}</tt:NodeToken>"
            f"<tt:DefaultPTZSpeed>"
            f'<tt:PanTilt x="{cfg.default_pan_tilt_speed}" '
            f'y="{cfg.default_pan_tilt_speed}"/>'
            f'<tt:Zoom x="{cfg.default_zoom_speed}"/>'
            "</tt:DefaultPTZSpeed>"
            "<tt:PanTiltLimits>"
            "<tt:Range>"
            "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/"
            "PositionGenericSpace</tt:URI>"
            f'<tt:XRange><tt:Min>{cfg.pan_range[0]}</tt:Min>'
            f'<tt:Max>{cfg.pan_range[1]}</tt:Max></tt:XRange>'
            f'<tt:YRange><tt:Min>{cfg.tilt_range[0]}</tt:Min>'
            f'<tt:Max>{cfg.tilt_range[1]}</tt:Max></tt:YRange>'
            "</tt:Range>"
            "</tt:PanTiltLimits>"
            "<tt:ZoomLimits>"
            "<tt:Range>"
            "<tt:URI>http://www.onvif.org/ver10/tptz/ZoomSpaces/"
            "PositionGenericSpace</tt:URI>"
            f'<tt:XRange><tt:Min>{cfg.zoom_range[0]}</tt:Min>'
            f'<tt:Max>{cfg.zoom_range[1]}</tt:Max></tt:XRange>'
            "</tt:Range>"
            "</tt:ZoomLimits>"
            "</tptz:PTZConfiguration>"
            "</tptz:GetConfigurationsResponse>"
        )

    def _xml_ptz_configuration(self, token: str) -> str:
        if token != self._ptz_config.token:
            raise ONVIFNotFound(
                f"No PTZ configuration {token!r}", subcode="ter:NoConfig"
            )
        cfg = self._ptz_config
        return (
            "<tptz:GetConfigurationResponse>"
            f'<tptz:PTZConfiguration token="{_xml_attr_escape(cfg.token)}">'
            f"<tt:Name>{_xml_escape(cfg.name)}</tt:Name>"
            "<tt:UseCount>1</tt:UseCount>"
            f"<tt:NodeToken>{_xml_escape(cfg.node_token)}</tt:NodeToken>"
            "</tptz:PTZConfiguration>"
            "</tptz:GetConfigurationResponse>"
        )

    def _xml_ptz_status(self) -> str:
        s = self._ptz_status
        return (
            "<tptz:GetStatusResponse>"
            "<tptz:PTZStatus>"
            "<tt:Position>"
            f'<tt:PanTilt x="{s.pan}" y="{s.tilt}"/>'
            f'<tt:Zoom x="{s.zoom}"/>'
            "</tt:Position>"
            "<tt:MoveStatus>"
            f"<tt:PanTilt>{s.move_status.value}</tt:PanTilt>"
            f"<tt:Zoom>{s.move_status.value}</tt:Zoom>"
            "</tt:MoveStatus>"
            f"<tt:UtcTime>{_iso_utc(s.last_updated)}</tt:UtcTime>"
            "</tptz:PTZStatus>"
            "</tptz:GetStatusResponse>"
        )

    def _xml_ptz_presets(self) -> str:
        parts = ["<tptz:GetPresetsResponse>"]
        for preset in self.list_presets():
            parts.append(
                f'<tptz:Preset token="{_xml_attr_escape(preset.token)}">'
                f"<tt:Name>{_xml_escape(preset.name)}</tt:Name>"
                "<tt:PTZPosition>"
                f'<tt:PanTilt x="{preset.pan}" y="{preset.tilt}"/>'
                f'<tt:Zoom x="{preset.zoom}"/>'
                "</tt:PTZPosition>"
                "</tptz:Preset>"
            )
        parts.append("</tptz:GetPresetsResponse>")
        return "".join(parts)


# ── Operation handlers (service + local tag → (body_xml, action_uri)) ─
#
# Using a table avoids a 300-line if/elif chain inside
# :py:meth:`ONVIFDevice._dispatch_operation` and makes the dispatch
# matrix testable in isolation.


_Handler = Callable[[ONVIFDevice, ET.Element], tuple[str, str]]


def _find_text(op: ET.Element, paths: list[str]) -> Optional[str]:
    """Find the first matching XPath and return its text content."""
    for path in paths:
        el = op.find(path, XML_NS)
        if el is not None and el.text is not None:
            return el.text.strip()
    return None


# ── Device operations ────────────────────────────────────────────────


def _op_device_get_information(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_device_information(), f"{_ACTION_DEVICE_PREFIX}/GetDeviceInformationResponse"


def _op_device_get_system_datetime(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_system_date_and_time(), f"{_ACTION_DEVICE_PREFIX}/GetSystemDateAndTimeResponse"


def _op_device_get_capabilities(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_capabilities(), f"{_ACTION_DEVICE_PREFIX}/GetCapabilitiesResponse"


def _op_device_get_services(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    include = _find_text(op, [".//tds:IncludeCapability"])
    include_flag = (include or "false").strip().lower() == "true"
    return (
        dev._xml_services(include_capability=include_flag),
        f"{_ACTION_DEVICE_PREFIX}/GetServicesResponse",
    )


def _op_device_get_service_capabilities(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return (
        dev._xml_service_capabilities(ONVIFService.DEVICE),
        f"{_ACTION_DEVICE_PREFIX}/GetServiceCapabilitiesResponse",
    )


def _op_device_get_network_interfaces(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_network_interfaces(), f"{_ACTION_DEVICE_PREFIX}/GetNetworkInterfacesResponse"


def _op_device_get_scopes(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_scopes(), f"{_ACTION_DEVICE_PREFIX}/GetScopesResponse"


def _op_device_get_hostname(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_hostname(), f"{_ACTION_DEVICE_PREFIX}/GetHostnameResponse"


def _op_device_set_hostname(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    name = _find_text(op, [".//tds:Name"])
    if not name:
        raise ONVIFBadRequest("SetHostname missing Name", subcode="ter:InvalidArgs")
    dev._hostname = name
    return (
        "<tds:SetHostnameResponse/>",
        f"{_ACTION_DEVICE_PREFIX}/SetHostnameResponse",
    )


def _op_device_get_users(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_get_users(), f"{_ACTION_DEVICE_PREFIX}/GetUsersResponse"


def _op_device_create_users(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    users_el = op.findall(".//tds:User", XML_NS)
    if not users_el:
        raise ONVIFBadRequest("CreateUsers needs at least one User",
                              subcode="ter:InvalidArgs")
    for user_el in users_el:
        username = user_el.findtext(".//tt:Username", namespaces=XML_NS) or ""
        password = user_el.findtext(".//tt:Password", namespaces=XML_NS) or ""
        level_text = user_el.findtext(".//tt:UserLevel", namespaces=XML_NS) or "User"
        try:
            level = UserLevel(level_text)
        except ValueError:
            raise ONVIFBadRequest(
                f"Invalid UserLevel {level_text!r}", subcode="ter:InvalidArgs"
            )
        dev.add_user(username=username, password=password, level=level)
    return (
        "<tds:CreateUsersResponse/>",
        f"{_ACTION_DEVICE_PREFIX}/CreateUsersResponse",
    )


def _op_device_delete_users(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    names = [
        el.text.strip() for el in op.findall(".//tt:Username", XML_NS)
        if el.text
    ]
    if not names:
        raise ONVIFBadRequest(
            "DeleteUsers missing Username", subcode="ter:InvalidArgs"
        )
    for name in names:
        if dev.get_user(name) is None:
            raise ONVIFNotFound(
                f"Unknown user {name!r}", subcode="ter:UsernameMissing"
            )
        dev.remove_user(name)
    return (
        "<tds:DeleteUsersResponse/>",
        f"{_ACTION_DEVICE_PREFIX}/DeleteUsersResponse",
    )


def _op_device_set_user(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    user_els = op.findall(".//tds:User", XML_NS)
    for user_el in user_els:
        username = user_el.findtext(".//tt:Username", namespaces=XML_NS) or ""
        password = user_el.findtext(".//tt:Password", namespaces=XML_NS) or ""
        level_text = user_el.findtext(".//tt:UserLevel", namespaces=XML_NS) or "User"
        if dev.get_user(username) is None:
            raise ONVIFNotFound(
                f"Unknown user {username!r}", subcode="ter:UsernameMissing"
            )
        try:
            level = UserLevel(level_text)
        except ValueError:
            raise ONVIFBadRequest(
                f"Invalid UserLevel {level_text!r}", subcode="ter:InvalidArgs"
            )
        dev.remove_user(username)
        dev.add_user(username=username, password=password, level=level)
    return (
        "<tds:SetUserResponse/>",
        f"{_ACTION_DEVICE_PREFIX}/SetUserResponse",
    )


# ── Media operations ─────────────────────────────────────────────────


def _op_media_get_profiles(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    parts = ["<trt:GetProfilesResponse>"]
    for profile in dev.list_profiles():
        parts.append(dev._xml_profile(profile))
    parts.append("</trt:GetProfilesResponse>")
    return "".join(parts), f"{_ACTION_MEDIA_PREFIX}/GetProfilesResponse"


def _op_media_get_profile(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    token = _find_text(op, [".//trt:ProfileToken"])
    if not token:
        raise ONVIFBadRequest("GetProfile missing ProfileToken",
                              subcode="ter:InvalidArgs")
    profile = dev.get_profile(token)
    body = (
        "<trt:GetProfileResponse>"
        f"{dev._xml_profile(profile)}"
        "</trt:GetProfileResponse>"
    )
    return body, f"{_ACTION_MEDIA_PREFIX}/GetProfileResponse"


def _op_media_get_video_sources(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_video_sources(), f"{_ACTION_MEDIA_PREFIX}/GetVideoSourcesResponse"


def _op_media_get_video_source_configurations(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return (
        dev._xml_video_source_configurations(),
        f"{_ACTION_MEDIA_PREFIX}/GetVideoSourceConfigurationsResponse",
    )


def _op_media_get_video_encoder_configurations(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return (
        dev._xml_video_encoder_configurations(),
        f"{_ACTION_MEDIA_PREFIX}/GetVideoEncoderConfigurationsResponse",
    )


def _op_media_get_stream_uri(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    token = _find_text(op, [".//trt:ProfileToken"])
    if not token:
        raise ONVIFBadRequest(
            "GetStreamUri missing ProfileToken", subcode="ter:InvalidArgs"
        )
    profile = dev.get_profile(token)
    return dev._xml_stream_uri(profile), f"{_ACTION_MEDIA_PREFIX}/GetStreamUriResponse"


def _op_media_get_snapshot_uri(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    token = _find_text(op, [".//trt:ProfileToken"])
    if not token:
        raise ONVIFBadRequest(
            "GetSnapshotUri missing ProfileToken", subcode="ter:InvalidArgs"
        )
    profile = dev.get_profile(token)
    return (
        dev._xml_snapshot_uri(profile),
        f"{_ACTION_MEDIA_PREFIX}/GetSnapshotUriResponse",
    )


def _op_media_get_service_capabilities(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return (
        dev._xml_service_capabilities(ONVIFService.MEDIA),
        f"{_ACTION_MEDIA_PREFIX}/GetServiceCapabilitiesResponse",
    )


# ── Events operations ────────────────────────────────────────────────


def _op_events_get_properties(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_event_properties(), f"{_ACTION_EVENTS_PREFIX}/GetEventPropertiesResponse"


def _op_events_get_service_capabilities(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return (
        dev._xml_service_capabilities(ONVIFService.EVENTS),
        f"{_ACTION_EVENTS_PREFIX}/GetServiceCapabilitiesResponse",
    )


def _parse_iso_duration(text: Optional[str], default_s: int) -> int:
    """Parse ISO-8601 duration ``PT...S`` → seconds (minutes + hours also)."""
    if not text:
        return default_s
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", text.strip())
    if not m:
        raise ONVIFBadRequest(
            f"Unsupported duration {text!r}", subcode="ter:InvalidArgVal"
        )
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    total = h * 3600 + mi * 60 + s
    if total == 0:
        raise ONVIFBadRequest(
            "Duration must be > 0", subcode="ter:InvalidArgVal"
        )
    return total


def _op_events_create_pullpoint(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    filter_text = _find_text(
        op, [".//wsnt:TopicExpression", ".//tev:Filter/wsnt:TopicExpression"]
    ) or ""
    timeout_iso = _find_text(
        op, [".//tev:InitialTerminationTime", ".//wsnt:InitialTerminationTime"]
    )
    timeout_s = _parse_iso_duration(timeout_iso, _DEFAULT_SUBSCRIPTION_TIMEOUT_S)
    sub = dev.create_subscription(topic_filter=filter_text, timeout_s=timeout_s)
    return (
        dev._xml_create_pullpoint_response(sub),
        f"{_ACTION_PULLPOINT_PREFIX}/CreatePullPointSubscriptionResponse",
    )


def _extract_subscription_id(op: ET.Element) -> str:
    # Two places the SubscriptionId hides: (1) WS-Addressing reference
    # parameters in the header, already parsed by the caller passing
    # the raw body; (2) the body itself when the NVR sends the shortcut
    # tev:SubscriptionId child. We only accept (2) here — the caller
    # strips the header reference params and mirrors them as a body
    # child before dispatch, matching how live555 & happytimesoft do it.
    sid = _find_text(
        op,
        [".//tev:SubscriptionId", "./tev:SubscriptionId", "./SubscriptionId"],
    )
    if not sid:
        raise ONVIFBadRequest(
            "Missing SubscriptionId reference", subcode="ter:InvalidArgs"
        )
    return sid


def _op_events_pull_messages(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    token = _extract_subscription_id(op)
    timeout_iso = _find_text(op, [".//tev:Timeout", "./Timeout"])
    # Timeout is the max wait in PullMessages — we honour it as metadata
    # but the stub never actually blocks.
    _ = _parse_iso_duration(timeout_iso or "PT1S", default_s=1)
    limit_text = _find_text(op, [".//tev:MessageLimit", "./MessageLimit"])
    try:
        limit = int(limit_text) if limit_text else 10
    except ValueError as exc:
        raise ONVIFBadRequest(
            f"Invalid MessageLimit {limit_text!r}", subcode="ter:InvalidArgVal"
        ) from exc
    messages = dev.pull_messages(token=token, limit=limit)
    sub = dev._subscriptions.get(token)
    if sub is None:
        raise ONVIFNotFound(
            f"Subscription {token!r} disappeared",
            subcode="ter:InvalidSubscriptionReference",
        )
    return (
        dev._xml_pull_messages_response(sub, messages),
        f"{_ACTION_PULLPOINT_PREFIX}/PullMessagesResponse",
    )


def _op_events_renew(dev: ONVIFDevice, op: ET.Element) -> tuple[str, str]:
    token = _extract_subscription_id(op)
    timeout_iso = _find_text(
        op,
        [".//wsnt:TerminationTime", "./TerminationTime", ".//tev:TerminationTime"],
    )
    timeout_s = _parse_iso_duration(timeout_iso, _DEFAULT_SUBSCRIPTION_TIMEOUT_S)
    sub = dev.renew_subscription(token, timeout_s)
    return (
        dev._xml_renew_response(sub),
        f"{NS_WSNT}/SubscriptionManager/RenewResponse",
    )


def _op_events_unsubscribe(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    token = _extract_subscription_id(op)
    dev.unsubscribe(token)
    return (
        dev._xml_unsubscribe_response(),
        f"{NS_WSNT}/SubscriptionManager/UnsubscribeResponse",
    )


# ── PTZ operations ───────────────────────────────────────────────────


def _op_ptz_get_configurations(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_ptz_configurations(), f"{_ACTION_PTZ_PREFIX}/GetConfigurationsResponse"


def _op_ptz_get_configuration(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    token = _find_text(op, [".//tptz:PTZConfigurationToken"])
    if not token:
        raise ONVIFBadRequest(
            "GetConfiguration missing PTZConfigurationToken",
            subcode="ter:InvalidArgs",
        )
    return dev._xml_ptz_configuration(token), f"{_ACTION_PTZ_PREFIX}/GetConfigurationResponse"


def _op_ptz_get_status(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_ptz_status(), f"{_ACTION_PTZ_PREFIX}/GetStatusResponse"


def _op_ptz_get_service_capabilities(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return (
        dev._xml_service_capabilities(ONVIFService.PTZ),
        f"{_ACTION_PTZ_PREFIX}/GetServiceCapabilitiesResponse",
    )


def _parse_pan_tilt_zoom(
    op: ET.Element, *, tag_name: str
) -> tuple[float, float, float]:
    """Pull (x_pan, y_tilt, x_zoom) out of a PTZ vector element.

    ``tag_name`` is ``Velocity``, ``Position``, or ``Translation`` per
    the three move operations. All three have identical xml shape in
    the ONVIF PTZ WSDL. Missing axes default to 0.0 so "pan only"
    moves Just Work.
    """
    vec = op.find(f".//tptz:{tag_name}", XML_NS)
    pan = tilt = zoom = 0.0
    if vec is not None:
        pt = vec.find("tt:PanTilt", XML_NS)
        if pt is not None:
            try:
                pan = float(pt.get("x", "0"))
                tilt = float(pt.get("y", "0"))
            except ValueError as exc:
                raise ONVIFBadRequest(
                    f"Invalid PanTilt values: {exc}",
                    subcode="ter:InvalidArgVal",
                ) from exc
        z = vec.find("tt:Zoom", XML_NS)
        if z is not None:
            try:
                zoom = float(z.get("x", "0"))
            except ValueError as exc:
                raise ONVIFBadRequest(
                    f"Invalid Zoom value: {exc}",
                    subcode="ter:InvalidArgVal",
                ) from exc
    return pan, tilt, zoom


def _op_ptz_continuous_move(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    pan, tilt, zoom = _parse_pan_tilt_zoom(op, tag_name="Velocity")
    dev.continuous_move(pan, tilt, zoom)
    return (
        "<tptz:ContinuousMoveResponse/>",
        f"{_ACTION_PTZ_PREFIX}/ContinuousMoveResponse",
    )


def _op_ptz_absolute_move(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    pan, tilt, zoom = _parse_pan_tilt_zoom(op, tag_name="Position")
    dev.absolute_move(pan, tilt, zoom)
    return (
        "<tptz:AbsoluteMoveResponse/>",
        f"{_ACTION_PTZ_PREFIX}/AbsoluteMoveResponse",
    )


def _op_ptz_relative_move(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    pan, tilt, zoom = _parse_pan_tilt_zoom(op, tag_name="Translation")
    dev.relative_move(pan, tilt, zoom)
    return (
        "<tptz:RelativeMoveResponse/>",
        f"{_ACTION_PTZ_PREFIX}/RelativeMoveResponse",
    )


def _op_ptz_stop(dev: ONVIFDevice, op: ET.Element) -> tuple[str, str]:
    def _flag(name: str, default: bool) -> bool:
        text = _find_text(op, [f".//tptz:{name}", f"./{name}"])
        if text is None:
            return default
        return text.strip().lower() == "true"

    stop_pt = _flag("PanTilt", True)
    stop_z = _flag("Zoom", True)
    dev.stop_ptz(stop_pan_tilt=stop_pt, stop_zoom=stop_z)
    return "<tptz:StopResponse/>", f"{_ACTION_PTZ_PREFIX}/StopResponse"


def _op_ptz_get_presets(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    return dev._xml_ptz_presets(), f"{_ACTION_PTZ_PREFIX}/GetPresetsResponse"


def _op_ptz_set_preset(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    name = _find_text(op, [".//tptz:PresetName"]) or ""
    token = _find_text(op, [".//tptz:PresetToken"])
    if not name:
        raise ONVIFBadRequest(
            "SetPreset missing PresetName", subcode="ter:InvalidArgs"
        )
    preset = dev.set_preset(name=name, token=token)
    return (
        "<tptz:SetPresetResponse>"
        f"<tptz:PresetToken>{_xml_escape(preset.token)}</tptz:PresetToken>"
        "</tptz:SetPresetResponse>",
        f"{_ACTION_PTZ_PREFIX}/SetPresetResponse",
    )


def _op_ptz_remove_preset(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    token = _find_text(op, [".//tptz:PresetToken"])
    if not token:
        raise ONVIFBadRequest(
            "RemovePreset missing PresetToken", subcode="ter:InvalidArgs"
        )
    if not dev.remove_preset(token):
        raise ONVIFNotFound(
            f"No preset {token!r}", subcode="ter:NoEntity"
        )
    return (
        "<tptz:RemovePresetResponse/>",
        f"{_ACTION_PTZ_PREFIX}/RemovePresetResponse",
    )


def _op_ptz_goto_preset(
    dev: ONVIFDevice, op: ET.Element
) -> tuple[str, str]:
    token = _find_text(op, [".//tptz:PresetToken"])
    if not token:
        raise ONVIFBadRequest(
            "GotoPreset missing PresetToken", subcode="ter:InvalidArgs"
        )
    dev.goto_preset(token)
    return (
        "<tptz:GotoPresetResponse/>",
        f"{_ACTION_PTZ_PREFIX}/GotoPresetResponse",
    )


_OPERATION_HANDLERS: dict[tuple[ONVIFService, str], _Handler] = {
    # Device
    (ONVIFService.DEVICE, "GetDeviceInformation"): _op_device_get_information,
    (ONVIFService.DEVICE, "GetSystemDateAndTime"): _op_device_get_system_datetime,
    (ONVIFService.DEVICE, "GetCapabilities"): _op_device_get_capabilities,
    (ONVIFService.DEVICE, "GetServices"): _op_device_get_services,
    (ONVIFService.DEVICE, "GetServiceCapabilities"): _op_device_get_service_capabilities,
    (ONVIFService.DEVICE, "GetNetworkInterfaces"): _op_device_get_network_interfaces,
    (ONVIFService.DEVICE, "GetScopes"): _op_device_get_scopes,
    (ONVIFService.DEVICE, "GetHostname"): _op_device_get_hostname,
    (ONVIFService.DEVICE, "SetHostname"): _op_device_set_hostname,
    (ONVIFService.DEVICE, "GetUsers"): _op_device_get_users,
    (ONVIFService.DEVICE, "CreateUsers"): _op_device_create_users,
    (ONVIFService.DEVICE, "DeleteUsers"): _op_device_delete_users,
    (ONVIFService.DEVICE, "SetUser"): _op_device_set_user,
    # Media
    (ONVIFService.MEDIA, "GetProfiles"): _op_media_get_profiles,
    (ONVIFService.MEDIA, "GetProfile"): _op_media_get_profile,
    (ONVIFService.MEDIA, "GetVideoSources"): _op_media_get_video_sources,
    (ONVIFService.MEDIA, "GetVideoSourceConfigurations"): _op_media_get_video_source_configurations,
    (ONVIFService.MEDIA, "GetVideoEncoderConfigurations"): _op_media_get_video_encoder_configurations,
    (ONVIFService.MEDIA, "GetStreamUri"): _op_media_get_stream_uri,
    (ONVIFService.MEDIA, "GetSnapshotUri"): _op_media_get_snapshot_uri,
    (ONVIFService.MEDIA, "GetServiceCapabilities"): _op_media_get_service_capabilities,
    # Events
    (ONVIFService.EVENTS, "GetEventProperties"): _op_events_get_properties,
    (ONVIFService.EVENTS, "GetServiceCapabilities"): _op_events_get_service_capabilities,
    (ONVIFService.EVENTS, "CreatePullPointSubscription"): _op_events_create_pullpoint,
    (ONVIFService.EVENTS, "PullMessages"): _op_events_pull_messages,
    (ONVIFService.EVENTS, "Renew"): _op_events_renew,
    (ONVIFService.EVENTS, "Unsubscribe"): _op_events_unsubscribe,
    # PTZ
    (ONVIFService.PTZ, "GetConfigurations"): _op_ptz_get_configurations,
    (ONVIFService.PTZ, "GetConfiguration"): _op_ptz_get_configuration,
    (ONVIFService.PTZ, "GetStatus"): _op_ptz_get_status,
    (ONVIFService.PTZ, "GetServiceCapabilities"): _op_ptz_get_service_capabilities,
    (ONVIFService.PTZ, "ContinuousMove"): _op_ptz_continuous_move,
    (ONVIFService.PTZ, "AbsoluteMove"): _op_ptz_absolute_move,
    (ONVIFService.PTZ, "RelativeMove"): _op_ptz_relative_move,
    (ONVIFService.PTZ, "Stop"): _op_ptz_stop,
    (ONVIFService.PTZ, "GetPresets"): _op_ptz_get_presets,
    (ONVIFService.PTZ, "SetPreset"): _op_ptz_set_preset,
    (ONVIFService.PTZ, "RemovePreset"): _op_ptz_remove_preset,
    (ONVIFService.PTZ, "GotoPreset"): _op_ptz_goto_preset,
}


__all__ = [
    # Core
    "ONVIFDevice",
    "ONVIFService",
    "ONVIFServiceConfig",
    # Data classes
    "DeviceInformation",
    "EventSubscription",
    "MediaProfile",
    "NetworkInterface",
    "NotificationMessage",
    "ONVIFUser",
    "PTZConfiguration",
    "PTZMoveStatus",
    "PTZPreset",
    "PTZStatus",
    "UserLevel",
    "VideoSource",
    # Exceptions
    "ONVIFActionNotSupported",
    "ONVIFAuthError",
    "ONVIFBadRequest",
    "ONVIFError",
    "ONVIFForbidden",
    "ONVIFInvalidNetworkInterface",
    "ONVIFNotFound",
    # SOAP helpers
    "build_soap_fault",
    "build_soap_response",
    "build_username_token",
    "compute_password_digest",
    "parse_soap_envelope",
    # Namespaces
    "NS_SOAP",
    "NS_TDS",
    "NS_TEV",
    "NS_TPTZ",
    "NS_TRT",
    "NS_TT",
    "NS_WSA",
    "NS_WSNT",
    "NS_WSSE",
    "NS_WSSE_PASSWORD_DIGEST",
    "NS_WSSE_PASSWORD_TEXT",
    "NS_WSSE_UTIL",
]
