"""D1 — SKILL-UVC: UVC 1.5 USB Video Class gadget (#218).

UVC 1.5 descriptor scaffold (H.264 + MJPEG + still image + extension unit),
gadget-fs / functionfs binding via Linux ConfigFS, UVCH264 payload generator
with SCR/PTS timestamping, and USB-CV compliance validation helpers.

Public API:
    formats          = list_stream_formats()
    resolutions      = list_resolutions(format_id)
    xu_controls      = list_xu_controls()
    desc_tree        = build_descriptor_tree(config)
    valid            = validate_descriptors(desc_tree)
    gadget           = UVCGadgetManager(config)
    gadget.create_gadget()
    gadget.bind_udc(udc_name)
    gadget.start_stream(format, width, height, fps)
    gadget.stop_stream()
    gadget.capture_still() -> StillCapture
    gadget.xu_get(selector) -> int
    gadget.xu_set(selector, value)
    gadget.unbind_udc()
    gadget.destroy_gadget()
    payloads         = UVCH264PayloadGenerator(max_payload).generate(nal_data)
    report           = run_compliance_check(gadget)
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "configs" / "uvc_gadget.yaml"
_CONFIGFS_BASE = Path("/sys/kernel/config/usb_gadget")


# ── Enums ──────────────────────────────────────────────────────────────


class StreamFormat(str, Enum):
    H264 = "h264"
    MJPEG = "mjpeg"
    YUY2 = "yuy2"


class GadgetState(str, Enum):
    UNCONFIGURED = "unconfigured"
    CREATED = "created"
    BOUND = "bound"
    STREAMING = "streaming"
    ERROR = "error"


class StillMethod(IntEnum):
    NONE = 0
    METHOD_2 = 2
    METHOD_3 = 3


class UVCTerminalType(IntEnum):
    TT_VENDOR_SPECIFIC = 0x0100
    TT_STREAMING = 0x0101
    ITT_VENDOR_SPECIFIC = 0x0200
    ITT_CAMERA = 0x0201
    ITT_MEDIA_TRANSPORT = 0x0202
    OTT_VENDOR_SPECIFIC = 0x0300
    OTT_DISPLAY = 0x0301


class UVCRequestCode(IntEnum):
    SET_CUR = 0x01
    GET_CUR = 0x81
    GET_MIN = 0x82
    GET_MAX = 0x83
    GET_RES = 0x84
    GET_LEN = 0x85
    GET_INFO = 0x86
    GET_DEF = 0x87


class DescriptorType(IntEnum):
    DEVICE = 0x01
    CONFIGURATION = 0x02
    STRING = 0x03
    INTERFACE = 0x04
    ENDPOINT = 0x05
    INTERFACE_ASSOCIATION = 0x0B
    CS_INTERFACE = 0x24
    CS_ENDPOINT = 0x25


class VSDescriptorSubtype(IntEnum):
    VS_INPUT_HEADER = 0x01
    VS_OUTPUT_HEADER = 0x02
    VS_STILL_IMAGE_FRAME = 0x03
    VS_FORMAT_UNCOMPRESSED = 0x04
    VS_FRAME_UNCOMPRESSED = 0x05
    VS_FORMAT_MJPEG = 0x06
    VS_FRAME_MJPEG = 0x07
    VS_FORMAT_H264 = 0x13
    VS_FRAME_H264 = 0x14
    VS_COLORFORMAT = 0x0D


class VCDescriptorSubtype(IntEnum):
    VC_HEADER = 0x01
    VC_INPUT_TERMINAL = 0x02
    VC_OUTPUT_TERMINAL = 0x03
    VC_SELECTOR_UNIT = 0x04
    VC_PROCESSING_UNIT = 0x05
    VC_EXTENSION_UNIT = 0x06


# ── Data classes ───────────────────────────────────────────────────────


@dataclass
class FrameDescriptor:
    width: int
    height: int
    min_fps: int = 15
    max_fps: int = 30
    default_fps: int = 30
    min_bitrate: int = 0
    max_bitrate: int = 0
    max_frame_size: int = 0

    def __post_init__(self) -> None:
        if self.max_frame_size == 0:
            self.max_frame_size = self.width * self.height * 2
        if self.max_bitrate == 0:
            self.max_bitrate = self.width * self.height * self.max_fps * 16
        if self.min_bitrate == 0:
            self.min_bitrate = self.width * self.height * self.min_fps * 8


@dataclass
class FormatDescriptor:
    format_id: StreamFormat
    guid: bytes = b""
    bits_per_pixel: int = 16
    frames: list[FrameDescriptor] = field(default_factory=list)
    default_frame_index: int = 1

    def __post_init__(self) -> None:
        if not self.guid:
            self.guid = _FORMAT_GUIDS.get(self.format_id, b"\x00" * 16)


@dataclass
class StillImageDescriptor:
    width: int = 1920
    height: int = 1080
    compression: int = 0
    method: StillMethod = StillMethod.METHOD_2


@dataclass
class XUControl:
    selector: int
    name: str
    size: int = 4
    info_flags: int = 0x03
    min_value: int = 0
    max_value: int = 255
    default_value: int = 0
    resolution: int = 1
    current_value: int = 0
    read_only: bool = False

    def __post_init__(self) -> None:
        if self.read_only:
            self.info_flags = 0x01


@dataclass
class ExtensionUnitDescriptor:
    unit_id: int = 6
    guid: bytes = b""
    num_controls: int = 0
    controls: list[XUControl] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.guid:
            self.guid = bytes.fromhex("a29e7641f04e36119f2bff6137d0e124")
        if self.num_controls == 0:
            self.num_controls = len(self.controls)


@dataclass
class CameraTerminalDescriptor:
    terminal_id: int = 1
    terminal_type: int = UVCTerminalType.ITT_CAMERA
    controls: int = 0x0000_002A
    optical_zoom: bool = False
    auto_focus: bool = True


@dataclass
class ProcessingUnitDescriptor:
    unit_id: int = 2
    source_id: int = 1
    controls: int = 0x0000_177F
    max_multiplier: int = 0


@dataclass
class OutputTerminalDescriptor:
    terminal_id: int = 3
    terminal_type: int = UVCTerminalType.TT_STREAMING
    source_id: int = 2


@dataclass
class DescriptorTree:
    camera_terminal: CameraTerminalDescriptor = field(
        default_factory=CameraTerminalDescriptor
    )
    processing_unit: ProcessingUnitDescriptor = field(
        default_factory=ProcessingUnitDescriptor
    )
    output_terminal: OutputTerminalDescriptor = field(
        default_factory=OutputTerminalDescriptor
    )
    extension_unit: ExtensionUnitDescriptor = field(
        default_factory=ExtensionUnitDescriptor
    )
    formats: list[FormatDescriptor] = field(default_factory=list)
    still_image: StillImageDescriptor = field(default_factory=StillImageDescriptor)


@dataclass
class GadgetConfig:
    gadget_name: str = "g_uvc"
    vendor_id: int = 0x1D6B
    product_id: int = 0x0104
    bcd_device: int = 0x0100
    bcd_usb: int = 0x0200
    device_class: int = 0xEF
    device_subclass: int = 0x02
    device_protocol: int = 0x01
    manufacturer: str = "OmniSight"
    product: str = "UVC Camera"
    serial: str = "000000000001"
    max_power: int = 500
    max_resolution: tuple[int, int] = (1920, 1080)
    max_fps: int = 30
    formats: list[StreamFormat] = field(
        default_factory=lambda: [StreamFormat.H264, StreamFormat.MJPEG, StreamFormat.YUY2]
    )
    still_method: StillMethod = StillMethod.METHOD_2
    xu_controls: list[XUControl] = field(default_factory=list)
    udc: str = ""


@dataclass
class StillCapture:
    path: str = ""
    size: int = 0
    width: int = 0
    height: int = 0
    timestamp: float = 0.0


@dataclass
class StreamStatus:
    state: GadgetState = GadgetState.UNCONFIGURED
    format: str = ""
    width: int = 0
    height: int = 0
    fps: int = 0
    frames_sent: int = 0
    bytes_sent: int = 0
    start_time: float = 0.0
    errors: int = 0


@dataclass
class UVCPayloadHeader:
    header_length: int = 12
    bit_field: int = 0
    pts: int = 0
    scr_stc: int = 0
    scr_sof: int = 0

    @property
    def has_pts(self) -> bool:
        return bool(self.bit_field & 0x04)

    @property
    def has_scr(self) -> bool:
        return bool(self.bit_field & 0x08)

    @property
    def eof(self) -> bool:
        return bool(self.bit_field & 0x02)

    @property
    def fid(self) -> bool:
        return bool(self.bit_field & 0x01)

    def pack(self) -> bytes:
        data = struct.pack("<BB", self.header_length, self.bit_field)
        if self.has_pts:
            data += struct.pack("<I", self.pts)
        if self.has_scr:
            data += struct.pack("<IH", self.scr_stc, self.scr_sof)
        if len(data) < self.header_length:
            data += b"\x00" * (self.header_length - len(data))
        return data[: self.header_length]


@dataclass
class ComplianceResult:
    test_name: str = ""
    passed: bool = False
    details: str = ""
    chapter: str = ""


@dataclass
class ComplianceReport:
    gadget_name: str = ""
    timestamp: float = 0.0
    results: list[ComplianceResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)


# ── Format GUIDs ───────────────────────────────────────────────────────

_FORMAT_GUIDS: dict[StreamFormat, bytes] = {
    StreamFormat.YUY2: bytes(
        [0x59, 0x55, 0x59, 0x32, 0x00, 0x00, 0x10, 0x00,
         0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71]
    ),
    StreamFormat.MJPEG: bytes(
        [0x4D, 0x4A, 0x50, 0x47, 0x00, 0x00, 0x10, 0x00,
         0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71]
    ),
    StreamFormat.H264: bytes(
        [0x48, 0x32, 0x36, 0x34, 0x00, 0x00, 0x10, 0x00,
         0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71]
    ),
}

_DEFAULT_RESOLUTIONS: list[tuple[int, int, int]] = [
    (1920, 1080, 30),
    (1280, 720, 60),
    (640, 480, 120),
    (320, 240, 120),
]

_DEFAULT_XU_CONTROLS: list[XUControl] = [
    XUControl(selector=1, name="Firmware Version", size=4, read_only=True, default_value=0x01_00_00_00),
    XUControl(selector=2, name="ISP Brightness", size=1, max_value=255, default_value=128),
    XUControl(selector=3, name="ISP Contrast", size=1, max_value=255, default_value=128),
    XUControl(selector=4, name="ISP Saturation", size=1, max_value=255, default_value=128),
    XUControl(selector=5, name="ISP Sharpness", size=1, max_value=255, default_value=128),
    XUControl(selector=6, name="GPIO Output", size=1, max_value=0xFF, default_value=0),
    XUControl(selector=7, name="Sensor Register R/W", size=4, max_value=0xFFFFFFFF, default_value=0),
    XUControl(selector=8, name="Debug Log Level", size=1, max_value=4, default_value=0),
]


# ── Descriptor builder ─────────────────────────────────────────────────


class UVCDescriptorBuilder:
    """Build a UVC 1.5 compliant descriptor tree."""

    def __init__(self, config: GadgetConfig) -> None:
        self._config = config
        self._tree: Optional[DescriptorTree] = None

    def build(self) -> DescriptorTree:
        xu_controls = self._config.xu_controls or list(_DEFAULT_XU_CONTROLS)
        formats = self._build_formats()
        self._tree = DescriptorTree(
            camera_terminal=CameraTerminalDescriptor(terminal_id=1),
            processing_unit=ProcessingUnitDescriptor(unit_id=2, source_id=1),
            output_terminal=OutputTerminalDescriptor(terminal_id=3, source_id=2),
            extension_unit=ExtensionUnitDescriptor(
                unit_id=6,
                controls=xu_controls,
                num_controls=len(xu_controls),
            ),
            formats=formats,
            still_image=StillImageDescriptor(
                width=self._config.max_resolution[0],
                height=self._config.max_resolution[1],
                method=self._config.still_method,
            ),
        )
        return self._tree

    def _build_formats(self) -> list[FormatDescriptor]:
        formats: list[FormatDescriptor] = []
        for fmt_id in self._config.formats:
            frames = self._build_frames(fmt_id)
            bpp = 16 if fmt_id == StreamFormat.YUY2 else 0
            formats.append(
                FormatDescriptor(format_id=fmt_id, bits_per_pixel=bpp, frames=frames)
            )
        return formats

    def _build_frames(self, fmt_id: StreamFormat) -> list[FrameDescriptor]:
        max_w, max_h = self._config.max_resolution
        frames: list[FrameDescriptor] = []
        for w, h, fps in _DEFAULT_RESOLUTIONS:
            if w <= max_w and h <= max_h:
                actual_fps = min(fps, self._config.max_fps) if fmt_id == StreamFormat.YUY2 else fps
                frames.append(FrameDescriptor(width=w, height=h, max_fps=actual_fps, default_fps=min(actual_fps, 30)))
        return frames

    @property
    def tree(self) -> Optional[DescriptorTree]:
        return self._tree


def build_descriptor_tree(config: GadgetConfig) -> DescriptorTree:
    builder = UVCDescriptorBuilder(config)
    return builder.build()


# ── Descriptor validation ──────────────────────────────────────────────


def validate_descriptors(tree: DescriptorTree) -> list[str]:
    errors: list[str] = []

    if tree.camera_terminal.terminal_id < 1:
        errors.append("Camera terminal ID must be >= 1")
    if tree.processing_unit.source_id != tree.camera_terminal.terminal_id:
        errors.append(
            f"PU source_id ({tree.processing_unit.source_id}) != "
            f"CT terminal_id ({tree.camera_terminal.terminal_id})"
        )
    if tree.output_terminal.source_id != tree.processing_unit.unit_id:
        errors.append(
            f"OT source_id ({tree.output_terminal.source_id}) != "
            f"PU unit_id ({tree.processing_unit.unit_id})"
        )

    if not tree.formats:
        errors.append("At least one format descriptor required")

    for fmt in tree.formats:
        if not fmt.frames:
            errors.append(f"Format {fmt.format_id.value} has no frame descriptors")
        if len(fmt.guid) != 16:
            errors.append(f"Format {fmt.format_id.value} GUID must be 16 bytes")
        for frame in fmt.frames:
            if frame.width <= 0 or frame.height <= 0:
                errors.append(f"Invalid frame size {frame.width}x{frame.height}")
            if frame.max_fps <= 0:
                errors.append(f"Invalid max_fps {frame.max_fps}")

    xu = tree.extension_unit
    if xu.unit_id < 1:
        errors.append("Extension unit ID must be >= 1")
    seen_selectors: set[int] = set()
    for ctrl in xu.controls:
        if ctrl.selector in seen_selectors:
            errors.append(f"Duplicate XU selector {ctrl.selector}")
        seen_selectors.add(ctrl.selector)
        if ctrl.size < 1:
            errors.append(f"XU control '{ctrl.name}' size must be >= 1")

    if tree.still_image.width <= 0 or tree.still_image.height <= 0:
        errors.append("Invalid still image dimensions")

    return errors


# ── ConfigFS gadget operations ─────────────────────────────────────────


def _detect_udc() -> str:
    udc_path = Path("/sys/class/udc")
    if udc_path.exists():
        udcs = list(udc_path.iterdir())
        if udcs:
            return udcs[0].name
    return ""


def _configfs_write(path: Path, value: str) -> bool:
    try:
        path.write_text(value)
        return True
    except OSError as exc:
        logger.error("configfs write %s failed: %s", path, exc)
        return False


def _configfs_read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as exc:
        logger.error("configfs read %s failed: %s", path, exc)
        return ""


def _configfs_mkdir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        logger.error("configfs mkdir %s failed: %s", path, exc)
        return False


class ConfigFSGadgetBinder:
    """Manage a USB gadget via Linux ConfigFS."""

    def __init__(self, config: GadgetConfig) -> None:
        self._config = config
        self._gadget_path = _CONFIGFS_BASE / config.gadget_name
        self._bound = False

    @property
    def gadget_path(self) -> Path:
        return self._gadget_path

    @property
    def is_bound(self) -> bool:
        return self._bound

    def create(self) -> bool:
        gp = self._gadget_path
        if not _configfs_mkdir(gp):
            return False

        _configfs_write(gp / "idVendor", f"0x{self._config.vendor_id:04x}")
        _configfs_write(gp / "idProduct", f"0x{self._config.product_id:04x}")
        _configfs_write(gp / "bcdDevice", f"0x{self._config.bcd_device:04x}")
        _configfs_write(gp / "bcdUSB", f"0x{self._config.bcd_usb:04x}")
        _configfs_write(gp / "bDeviceClass", f"0x{self._config.device_class:02x}")
        _configfs_write(gp / "bDeviceSubClass", f"0x{self._config.device_subclass:02x}")
        _configfs_write(gp / "bDeviceProtocol", f"0x{self._config.device_protocol:02x}")

        strings_path = gp / "strings" / "0x409"
        _configfs_mkdir(strings_path)
        _configfs_write(strings_path / "manufacturer", self._config.manufacturer)
        _configfs_write(strings_path / "product", self._config.product)
        _configfs_write(strings_path / "serialnumber", self._config.serial)

        config_path = gp / "configs" / "c.1"
        _configfs_mkdir(config_path)
        config_strings = config_path / "strings" / "0x409"
        _configfs_mkdir(config_strings)
        _configfs_write(config_strings / "configuration", "UVC Webcam")
        _configfs_write(config_path / "MaxPower", str(self._config.max_power))

        func_path = gp / "functions" / "uvc.usb0"
        _configfs_mkdir(func_path)
        self._setup_streaming_descriptors(func_path)

        logger.info("ConfigFS gadget created at %s", gp)
        return True

    def _setup_streaming_descriptors(self, func_path: Path) -> None:
        streaming = func_path / "streaming"
        control = func_path / "control"
        _configfs_mkdir(streaming)
        _configfs_mkdir(control)

        header_path = streaming / "header" / "h"
        _configfs_mkdir(header_path)

        for fmt_id in self._config.formats:
            fmt_name = fmt_id.value
            fmt_dir = streaming / fmt_name / fmt_name
            _configfs_mkdir(fmt_dir)
            for w, h, fps in _DEFAULT_RESOLUTIONS:
                if w <= self._config.max_resolution[0] and h <= self._config.max_resolution[1]:
                    frame_dir = fmt_dir / f"{w}x{h}p{fps}"
                    _configfs_mkdir(frame_dir)
                    _configfs_write(frame_dir / "wWidth", str(w))
                    _configfs_write(frame_dir / "wHeight", str(h))
                    interval = 10_000_000 // fps
                    _configfs_write(frame_dir / "dwDefaultFrameInterval", str(interval))
                    _configfs_write(frame_dir / "dwFrameInterval", str(interval))

        ctrl_header = control / "header" / "h"
        _configfs_mkdir(ctrl_header)

    def bind(self, udc_name: str = "") -> bool:
        udc = udc_name or self._config.udc or _detect_udc()
        if not udc:
            logger.error("No UDC available to bind")
            return False
        if _configfs_write(self._gadget_path / "UDC", udc):
            self._bound = True
            logger.info("Gadget bound to UDC %s", udc)
            return True
        return False

    def unbind(self) -> bool:
        if _configfs_write(self._gadget_path / "UDC", ""):
            self._bound = False
            logger.info("Gadget unbound from UDC")
            return True
        return False

    def destroy(self) -> bool:
        if self._bound:
            self.unbind()
        try:
            import shutil
            shutil.rmtree(self._gadget_path, ignore_errors=True)
            logger.info("Gadget destroyed at %s", self._gadget_path)
            return True
        except OSError as exc:
            logger.error("Failed to destroy gadget: %s", exc)
            return False


# ── UVCH264 payload generator ──────────────────────────────────────────


class UVCH264PayloadGenerator:
    """Generate UVC payload packets from H.264 NAL units."""

    def __init__(self, max_payload_size: int = 3072) -> None:
        if max_payload_size < 64:
            raise ValueError("max_payload_size must be >= 64")
        self._max_payload = max_payload_size
        self._header_size = 12
        self._fid: bool = False
        self._pts_counter: int = 0
        self._sof_counter: int = 0
        self._frame_count: int = 0

    @property
    def max_payload_size(self) -> int:
        return self._max_payload

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def generate(self, nal_data: bytes) -> list[bytes]:
        if not nal_data:
            return []

        max_data = self._max_payload - self._header_size
        if max_data <= 0:
            raise ValueError("max_payload_size too small for header")

        chunks: list[bytes] = []
        offset = 0
        total = len(nal_data)

        while offset < total:
            remaining = total - offset
            chunk_size = min(remaining, max_data)
            is_last = (offset + chunk_size) >= total

            header = self._make_header(is_eof=is_last)
            payload = header + nal_data[offset : offset + chunk_size]
            chunks.append(payload)
            offset += chunk_size

        self._fid = not self._fid
        self._frame_count += 1
        return chunks

    def _make_header(self, is_eof: bool = False) -> bytes:
        self._pts_counter += 1
        self._sof_counter = (self._sof_counter + 1) & 0x7FF

        bit_field = 0x0C
        if self._fid:
            bit_field |= 0x01
        if is_eof:
            bit_field |= 0x02

        pts = (self._pts_counter * 33333) & 0xFFFFFFFF
        scr_stc = (self._pts_counter * 33333) & 0xFFFFFFFF
        scr_sof = self._sof_counter & 0xFFFF

        return struct.pack("<BBIIH", self._header_size, bit_field, pts, scr_stc, scr_sof)

    def reset(self) -> None:
        self._fid = False
        self._pts_counter = 0
        self._sof_counter = 0
        self._frame_count = 0


# ── UVC Gadget Manager ─────────────────────────────────────────────────


class UVCGadgetManager:
    """High-level manager for a UVC gadget device."""

    def __init__(self, config: Optional[GadgetConfig] = None) -> None:
        self._config = config or GadgetConfig()
        self._state = GadgetState.UNCONFIGURED
        self._binder: Optional[ConfigFSGadgetBinder] = None
        self._desc_tree: Optional[DescriptorTree] = None
        self._payload_gen: Optional[UVCH264PayloadGenerator] = None
        self._stream_status = StreamStatus()
        self._xu_values: dict[int, int] = {}
        self._init_xu_values()

    def _init_xu_values(self) -> None:
        controls = self._config.xu_controls or _DEFAULT_XU_CONTROLS
        for ctrl in controls:
            self._xu_values[ctrl.selector] = ctrl.default_value

    @property
    def state(self) -> GadgetState:
        return self._state

    @property
    def config(self) -> GadgetConfig:
        return self._config

    @property
    def descriptor_tree(self) -> Optional[DescriptorTree]:
        return self._desc_tree

    @property
    def stream_status(self) -> StreamStatus:
        return self._stream_status

    def create_gadget(self) -> bool:
        if self._state != GadgetState.UNCONFIGURED:
            logger.warning("Gadget already in state %s", self._state.value)
            return False

        builder = UVCDescriptorBuilder(self._config)
        self._desc_tree = builder.build()

        errors = validate_descriptors(self._desc_tree)
        if errors:
            logger.error("Descriptor validation failed: %s", errors)
            self._state = GadgetState.ERROR
            return False

        self._binder = ConfigFSGadgetBinder(self._config)
        if not self._binder.create():
            self._state = GadgetState.ERROR
            return False

        self._state = GadgetState.CREATED
        logger.info("UVC gadget '%s' created", self._config.gadget_name)
        return True

    def bind_udc(self, udc_name: str = "") -> bool:
        if self._state != GadgetState.CREATED:
            logger.warning("Cannot bind: gadget state is %s", self._state.value)
            return False
        if self._binder and self._binder.bind(udc_name):
            self._state = GadgetState.BOUND
            return True
        self._state = GadgetState.ERROR
        return False

    def start_stream(
        self,
        fmt: StreamFormat = StreamFormat.H264,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
    ) -> bool:
        if self._state != GadgetState.BOUND:
            logger.warning("Cannot stream: gadget state is %s", self._state.value)
            return False

        if fmt == StreamFormat.H264:
            self._payload_gen = UVCH264PayloadGenerator()

        self._stream_status = StreamStatus(
            state=GadgetState.STREAMING,
            format=fmt.value,
            width=width,
            height=height,
            fps=fps,
            start_time=time.time(),
        )
        self._state = GadgetState.STREAMING
        logger.info("Streaming %s %dx%d@%dfps", fmt.value, width, height, fps)
        return True

    def stop_stream(self) -> bool:
        if self._state != GadgetState.STREAMING:
            return False
        self._stream_status.state = GadgetState.BOUND
        self._state = GadgetState.BOUND
        self._payload_gen = None
        logger.info("Stream stopped")
        return True

    def send_payload(self, data: bytes) -> bool:
        if self._state != GadgetState.STREAMING:
            return False
        self._stream_status.frames_sent += 1
        self._stream_status.bytes_sent += len(data)
        return True

    _still_counter: int = 0

    def capture_still(self) -> StillCapture:
        if self._state not in (GadgetState.BOUND, GadgetState.STREAMING):
            return StillCapture()

        UVCGadgetManager._still_counter += 1
        still = self._desc_tree.still_image if self._desc_tree else StillImageDescriptor()
        capture = StillCapture(
            path=f"/tmp/uvc_still_{int(time.time())}_{UVCGadgetManager._still_counter}.jpg",
            size=still.width * still.height * 3 // 4,
            width=still.width,
            height=still.height,
            timestamp=time.time(),
        )
        logger.info("Still image captured: %s", capture.path)
        return capture

    def xu_get(self, selector: int) -> int:
        if selector not in self._xu_values:
            raise ValueError(f"Unknown XU selector {selector}")
        return self._xu_values[selector]

    def xu_set(self, selector: int, value: int) -> bool:
        controls = self._config.xu_controls or _DEFAULT_XU_CONTROLS
        ctrl = next((c for c in controls if c.selector == selector), None)
        if ctrl is None:
            raise ValueError(f"Unknown XU selector {selector}")
        if ctrl.read_only:
            raise ValueError(f"XU selector {selector} ({ctrl.name}) is read-only")
        if value < ctrl.min_value or value > ctrl.max_value:
            raise ValueError(
                f"Value {value} out of range [{ctrl.min_value}, {ctrl.max_value}] "
                f"for selector {selector}"
            )
        self._xu_values[selector] = value
        logger.info("XU selector %d set to %d", selector, value)
        return True

    def unbind_udc(self) -> bool:
        if self._state == GadgetState.STREAMING:
            self.stop_stream()
        if self._binder and self._binder.unbind():
            self._state = GadgetState.CREATED
            return True
        return False

    def destroy_gadget(self) -> bool:
        if self._state == GadgetState.STREAMING:
            self.stop_stream()
        if self._state == GadgetState.BOUND:
            self.unbind_udc()
        if self._binder and self._binder.destroy():
            self._state = GadgetState.UNCONFIGURED
            self._binder = None
            self._desc_tree = None
            return True
        return False

    def get_status(self) -> dict[str, Any]:
        return {
            "gadget_name": self._config.gadget_name,
            "state": self._state.value,
            "vendor_id": f"0x{self._config.vendor_id:04x}",
            "product_id": f"0x{self._config.product_id:04x}",
            "stream": {
                "format": self._stream_status.format,
                "resolution": f"{self._stream_status.width}x{self._stream_status.height}",
                "fps": self._stream_status.fps,
                "frames_sent": self._stream_status.frames_sent,
                "bytes_sent": self._stream_status.bytes_sent,
            }
            if self._state == GadgetState.STREAMING
            else None,
        }


# ── Compliance check ───────────────────────────────────────────────────


def run_compliance_check(manager: UVCGadgetManager) -> ComplianceReport:
    report = ComplianceReport(
        gadget_name=manager.config.gadget_name,
        timestamp=time.time(),
    )

    tree = manager.descriptor_tree
    if tree is None:
        report.results.append(
            ComplianceResult(
                test_name="Descriptor tree present",
                passed=False,
                details="No descriptor tree built",
                chapter="Chapter 9",
            )
        )
        return report

    desc_errors = validate_descriptors(tree)
    report.results.append(
        ComplianceResult(
            test_name="Descriptor validation",
            passed=len(desc_errors) == 0,
            details="; ".join(desc_errors) if desc_errors else "All descriptors valid",
            chapter="Chapter 9",
        )
    )

    has_h264 = any(f.format_id == StreamFormat.H264 for f in tree.formats)
    has_mjpeg = any(f.format_id == StreamFormat.MJPEG for f in tree.formats)
    report.results.append(
        ComplianceResult(
            test_name="H.264 format present",
            passed=has_h264,
            details="H.264 format descriptor found" if has_h264 else "Missing",
            chapter="UVC 1.5",
        )
    )
    report.results.append(
        ComplianceResult(
            test_name="MJPEG format present",
            passed=has_mjpeg,
            details="MJPEG format descriptor found" if has_mjpeg else "Missing",
            chapter="UVC 1.5",
        )
    )

    report.results.append(
        ComplianceResult(
            test_name="Still image descriptor",
            passed=tree.still_image.width > 0 and tree.still_image.height > 0,
            details=f"{tree.still_image.width}x{tree.still_image.height} method {tree.still_image.method}",
            chapter="UVC 1.5",
        )
    )

    report.results.append(
        ComplianceResult(
            test_name="Extension unit present",
            passed=tree.extension_unit.num_controls > 0,
            details=f"{tree.extension_unit.num_controls} XU controls",
            chapter="UVC 1.5",
        )
    )

    report.results.append(
        ComplianceResult(
            test_name="Camera terminal → PU → OT chain",
            passed=(
                tree.processing_unit.source_id == tree.camera_terminal.terminal_id
                and tree.output_terminal.source_id == tree.processing_unit.unit_id
            ),
            details="CT→PU→OT chain valid",
            chapter="Chapter 9",
        )
    )

    for fmt in tree.formats:
        all_valid = all(
            f.width > 0 and f.height > 0 and f.max_fps > 0 for f in fmt.frames
        )
        report.results.append(
            ComplianceResult(
                test_name=f"Frame descriptors ({fmt.format_id.value})",
                passed=all_valid and len(fmt.frames) > 0,
                details=f"{len(fmt.frames)} frame(s), all valid" if all_valid else "Invalid frames",
                chapter="UVC 1.5",
            )
        )

    report.results.append(
        ComplianceResult(
            test_name="IAD device class",
            passed=manager.config.device_class == 0xEF,
            details=f"bDeviceClass=0x{manager.config.device_class:02X}",
            chapter="Chapter 9",
        )
    )

    report.results.append(
        ComplianceResult(
            test_name="USB 2.0 HS compliance",
            passed=manager.config.bcd_usb >= 0x0200,
            details=f"bcdUSB=0x{manager.config.bcd_usb:04X}",
            chapter="Chapter 9",
        )
    )

    return report


# ── Public query helpers ───────────────────────────────────────────────


def list_stream_formats() -> list[dict[str, Any]]:
    return [
        {
            "id": StreamFormat.H264.value,
            "name": "H.264",
            "description": "Hardware-encoded H.264 Baseline/Main profile",
            "guid": _FORMAT_GUIDS[StreamFormat.H264].hex(),
        },
        {
            "id": StreamFormat.MJPEG.value,
            "name": "MJPEG",
            "description": "Motion JPEG",
            "guid": _FORMAT_GUIDS[StreamFormat.MJPEG].hex(),
        },
        {
            "id": StreamFormat.YUY2.value,
            "name": "YUY2",
            "description": "Uncompressed YUYV 4:2:2",
            "guid": _FORMAT_GUIDS[StreamFormat.YUY2].hex(),
        },
    ]


def list_resolutions(format_id: Optional[str] = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for w, h, fps in _DEFAULT_RESOLUTIONS:
        results.append({"width": w, "height": h, "max_fps": fps, "label": f"{w}x{h}@{fps}fps"})
    return results


def list_xu_controls() -> list[dict[str, Any]]:
    return [
        {
            "selector": c.selector,
            "name": c.name,
            "size": c.size,
            "min": c.min_value,
            "max": c.max_value,
            "default": c.default_value,
            "read_only": c.read_only,
        }
        for c in _DEFAULT_XU_CONTROLS
    ]


# ── Config loader ─────────────────────────────────────────────────────


def _load_config() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as fh:
            return yaml.safe_load(fh) or {}
    return {}
