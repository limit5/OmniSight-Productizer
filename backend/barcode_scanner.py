"""C22 — L4-CORE-22 Barcode/scanning SDK abstraction (#243).

Unified BarcodeScanner interface with vendor adapter pattern.
Supports Zebra SNAPI, Honeywell SDK, Datalogic SDK, Newland SDK.
Symbologies: UPC/EAN/Code128/QR/DataMatrix/PDF417/Aztec + more.
Decode modes: HID wedge / SPP / API.

Public API:
    vendors      = list_vendors()
    symbologies  = list_symbologies(category=None)
    modes        = list_decode_modes()
    scanner      = create_scanner(vendor_id, config)
    result       = scanner.scan(frame_data)
    result       = decode_frame(vendor_id, frame_data, symbology_filter)
    samples      = list_frame_samples()
    validated    = validate_frame_sample(sample_id, vendor_id)
    recipes      = list_test_recipes()
    report       = run_test_recipe(recipe_id)
    artifacts    = list_artifacts()
    verdict      = validate_gate()
"""

from __future__ import annotations

import hashlib
import logging
import struct
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "configs" / "barcode_scanner.yaml"


# ── Enums ──────────────────────────────────────────────────────────────

class BarcodeDomain(str, Enum):
    vendor_adapters = "vendor_adapters"
    symbology = "symbology"
    decode_modes = "decode_modes"
    frame_samples = "frame_samples"
    error_handling = "error_handling"
    integration = "integration"


class VendorId(str, Enum):
    zebra_snapi = "zebra_snapi"
    honeywell = "honeywell"
    datalogic = "datalogic"
    newland = "newland"


class SymbologyCategory(str, Enum):
    one_d = "one_d"
    two_d = "two_d"


class SymbologyId(str, Enum):
    upc_a = "upc_a"
    upc_e = "upc_e"
    ean_8 = "ean_8"
    ean_13 = "ean_13"
    code_128 = "code_128"
    code_39 = "code_39"
    code_93 = "code_93"
    codabar = "codabar"
    interleaved_2of5 = "interleaved_2of5"
    gs1_databar = "gs1_databar"
    qr_code = "qr_code"
    data_matrix = "data_matrix"
    pdf417 = "pdf417"
    aztec = "aztec"
    maxi_code = "maxi_code"
    han_xin = "han_xin"


class DecodeMode(str, Enum):
    hid_wedge = "hid_wedge"
    spp = "spp"
    api = "api"


class ScannerState(str, Enum):
    disconnected = "disconnected"
    connected = "connected"
    configured = "configured"
    scanning = "scanning"
    error = "error"


class ScanResultStatus(str, Enum):
    success = "success"
    no_decode = "no_decode"
    partial_decode = "partial_decode"
    unsupported_symbology = "unsupported_symbology"
    error = "error"


class TestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


class GateVerdict(str, Enum):
    passed = "passed"
    failed = "failed"
    error = "error"


# ── Data models ────────────────────────────────────────────────────────

@dataclass
class VendorDef:
    vendor_id: str
    name: str
    description: str = ""
    sdk_name: str = ""
    transport: list[str] = field(default_factory=list)
    supported_symbologies: list[str] = field(default_factory=list)
    decode_modes: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)


@dataclass
class SymbologyDef:
    symbology_id: str
    name: str
    category: str
    description: str = ""
    digit_count: Optional[int] = None
    charset: str = ""
    check_digit: bool = False
    max_data_chars: Optional[int] = None
    error_correction_levels: list[str] = field(default_factory=list)


@dataclass
class DecodeModeDef:
    mode_id: str
    name: str
    description: str = ""
    transport: str = ""
    requires_driver: bool = False
    latency: str = "medium"
    features: list[str] = field(default_factory=list)


@dataclass
class FrameSample:
    sample_id: str
    symbology: str
    description: str = ""
    expected_data: str = ""
    format: str = "grayscale_8bit"
    width: int = 200
    height: int = 100


@dataclass
class ScanResult:
    status: str
    symbology: Optional[str] = None
    data: Optional[str] = None
    raw_bytes: Optional[bytes] = None
    confidence: float = 0.0
    decode_time_ms: float = 0.0
    vendor_id: Optional[str] = None
    frame_hash: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScannerConfig:
    vendor_id: str
    decode_mode: str = "api"
    enabled_symbologies: list[str] = field(default_factory=list)
    illumination: bool = True
    aim_pattern: bool = True
    trigger_mode: str = "manual"
    scan_timeout_ms: int = 5000
    beeper_enabled: bool = True
    led_enabled: bool = True
    prefix: str = ""
    suffix: str = ""
    inter_char_delay_ms: int = 0


@dataclass
class TestRecipeDef:
    recipe_id: str
    name: str
    description: str = ""
    domains: list[str] = field(default_factory=list)


@dataclass
class TestResult:
    recipe_id: str
    status: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ArtifactDef:
    artifact_id: str
    kind: str
    description: str = ""


# ── Config loader ──────────────────────────────────────────────────────

_cfg: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    global _cfg
    if _cfg is not None:
        return _cfg
    with open(_CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    _cfg = raw.get("barcode_scanner", raw)
    return _cfg


def _get_cfg() -> dict[str, Any]:
    return _load_config()


# ── Checksum helpers ───────────────────────────────────────────────────

def _frame_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _generate_synthetic_frame(symbology: str, data: str, width: int, height: int) -> bytes:
    header = struct.pack(">HH", width, height)
    payload = data.encode("utf-8")
    sym_tag = symbology.encode("utf-8")
    padding = bytes(max(0, width * height - len(header) - len(payload) - len(sym_tag) - 4))
    marker = b"\xba\x5c"
    return header + marker + sym_tag + b"\x00" + payload + b"\x00" + padding


# ── Symbology validation ──────────────────────────────────────────────

_UPC_A_LEN = 12
_UPC_E_LEN = 8
_EAN_8_LEN = 8
_EAN_13_LEN = 13


def _validate_check_digit_ean(digits: str) -> bool:
    if not digits.isdigit():
        return False
    nums = [int(d) for d in digits]
    total = 0
    for i, n in enumerate(nums[:-1]):
        total += n * (1 if (len(nums) - 1 - i) % 2 == 0 else 3)
    check = (10 - (total % 10)) % 10
    return check == nums[-1]


def validate_symbology_data(symbology: str, data: str) -> tuple[bool, str]:
    sym = symbology.lower()
    if sym == "upc_a":
        if len(data) != _UPC_A_LEN or not data.isdigit():
            return False, f"UPC-A requires exactly {_UPC_A_LEN} digits"
        if not _validate_check_digit_ean(data):
            return False, "UPC-A check digit invalid"
        return True, "valid"

    if sym == "upc_e":
        if len(data) != _UPC_E_LEN or not data.isdigit():
            return False, f"UPC-E requires exactly {_UPC_E_LEN} digits"
        return True, "valid"

    if sym == "ean_8":
        if len(data) != _EAN_8_LEN or not data.isdigit():
            return False, f"EAN-8 requires exactly {_EAN_8_LEN} digits"
        if not _validate_check_digit_ean(data):
            return False, "EAN-8 check digit invalid"
        return True, "valid"

    if sym == "ean_13":
        if len(data) != _EAN_13_LEN or not data.isdigit():
            return False, f"EAN-13 requires exactly {_EAN_13_LEN} digits"
        if not _validate_check_digit_ean(data):
            return False, "EAN-13 check digit invalid"
        return True, "valid"

    if sym in ("code_128", "code_93"):
        if not all(0 <= ord(c) < 128 for c in data):
            return False, f"{symbology} requires ASCII data"
        return True, "valid"

    if sym == "code_39":
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -.$/+%")
        if not set(data.upper()).issubset(allowed):
            return False, "Code 39 allows uppercase alphanumeric + special"
        return True, "valid"

    if sym == "codabar":
        allowed = set("0123456789-$:/.+ABCD")
        if not set(data.upper()).issubset(allowed):
            return False, "Codabar allows numeric + 6 special + start/stop"
        return True, "valid"

    if sym == "interleaved_2of5":
        if not data.isdigit():
            return False, "Interleaved 2 of 5 requires numeric data"
        if len(data) % 2 != 0:
            return False, "Interleaved 2 of 5 requires even digit count"
        return True, "valid"

    if sym in ("qr_code", "data_matrix", "pdf417", "aztec", "maxi_code", "han_xin", "gs1_databar"):
        return True, "valid"

    return False, f"Unknown symbology: {symbology}"


# ── BarcodeScanner interface (abstract) ────────────────────────────────

class BarcodeScanner(ABC):
    """Unified barcode scanner interface — all vendor adapters implement this."""

    def __init__(self, config: ScannerConfig):
        self._config = config
        self._state = ScannerState.disconnected
        self._scan_count = 0
        self._last_result: Optional[ScanResult] = None

    @property
    def vendor_id(self) -> str:
        return self._config.vendor_id

    @property
    def state(self) -> ScannerState:
        return self._state

    @property
    def config(self) -> ScannerConfig:
        return self._config

    @property
    def scan_count(self) -> int:
        return self._scan_count

    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def disconnect(self) -> bool:
        ...

    @abstractmethod
    def configure(self, config: ScannerConfig) -> bool:
        ...

    @abstractmethod
    def scan(self, frame_data: bytes) -> ScanResult:
        ...

    @abstractmethod
    def get_capabilities(self) -> dict[str, Any]:
        ...

    def set_decode_mode(self, mode: str) -> bool:
        if mode not in (m.value for m in DecodeMode):
            return False
        self._config.decode_mode = mode
        return True

    def enable_symbology(self, symbology: str) -> bool:
        if symbology not in (s.value for s in SymbologyId):
            return False
        if symbology not in self._config.enabled_symbologies:
            self._config.enabled_symbologies.append(symbology)
        return True

    def disable_symbology(self, symbology: str) -> bool:
        if symbology in self._config.enabled_symbologies:
            self._config.enabled_symbologies.remove(symbology)
            return True
        return False

    def get_status(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "state": self._state.value,
            "decode_mode": self._config.decode_mode,
            "enabled_symbologies": self._config.enabled_symbologies,
            "scan_count": self._scan_count,
        }


# ── Vendor adapters ───────────────────────────────────────────────────

class _BaseAdapter(BarcodeScanner):
    """Shared decode logic for all vendor adapters."""

    _VENDOR_FEATURES: dict[str, Any] = {}

    def connect(self) -> bool:
        if self._state != ScannerState.disconnected:
            return False
        self._state = ScannerState.connected
        logger.info("Scanner connected: vendor=%s", self.vendor_id)
        return True

    def disconnect(self) -> bool:
        if self._state == ScannerState.disconnected:
            return False
        self._state = ScannerState.disconnected
        logger.info("Scanner disconnected: vendor=%s", self.vendor_id)
        return True

    def configure(self, config: ScannerConfig) -> bool:
        if self._state not in (ScannerState.connected, ScannerState.configured):
            return False
        self._config = config
        self._state = ScannerState.configured
        return True

    def scan(self, frame_data: bytes) -> ScanResult:
        if self._state not in (ScannerState.configured, ScannerState.connected):
            return ScanResult(
                status=ScanResultStatus.error.value,
                vendor_id=self.vendor_id,
                metadata={"error": "scanner not ready"},
            )

        self._state = ScannerState.scanning
        t0 = time.monotonic()

        result = self._decode_frame(frame_data)
        elapsed = (time.monotonic() - t0) * 1000

        result.decode_time_ms = round(elapsed, 2)
        result.vendor_id = self.vendor_id
        result.frame_hash = _frame_hash(frame_data)

        self._scan_count += 1
        self._last_result = result
        self._state = ScannerState.configured

        if result.status == ScanResultStatus.success.value:
            result = self._apply_decode_mode(result)

        return result

    def _decode_frame(self, frame_data: bytes) -> ScanResult:
        if len(frame_data) < 8:
            return ScanResult(status=ScanResultStatus.no_decode.value)

        marker_pos = frame_data.find(b"\xba\x5c")
        if marker_pos < 0:
            return ScanResult(status=ScanResultStatus.no_decode.value)

        rest = frame_data[marker_pos + 2:]
        null1 = rest.find(b"\x00")
        if null1 < 0:
            return ScanResult(status=ScanResultStatus.no_decode.value)

        sym_tag = rest[:null1].decode("utf-8", errors="replace")
        payload_rest = rest[null1 + 1:]
        null2 = payload_rest.find(b"\x00")
        if null2 < 0:
            decoded_data = payload_rest.decode("utf-8", errors="replace")
        else:
            decoded_data = payload_rest[:null2].decode("utf-8", errors="replace")

        if not sym_tag or not decoded_data:
            return ScanResult(status=ScanResultStatus.no_decode.value)

        if self._config.enabled_symbologies and sym_tag not in self._config.enabled_symbologies:
            return ScanResult(
                status=ScanResultStatus.unsupported_symbology.value,
                symbology=sym_tag,
                metadata={"reason": "symbology not in enabled list"},
            )

        valid, msg = validate_symbology_data(sym_tag, decoded_data)
        confidence = 0.95 if valid else 0.5

        return ScanResult(
            status=ScanResultStatus.success.value,
            symbology=sym_tag,
            data=decoded_data,
            raw_bytes=payload_rest[:null2] if null2 >= 0 else payload_rest,
            confidence=confidence,
        )

    def _apply_decode_mode(self, result: ScanResult) -> ScanResult:
        mode = self._config.decode_mode

        if mode == DecodeMode.hid_wedge.value:
            output = self._config.prefix + (result.data or "") + self._config.suffix
            result.metadata["hid_output"] = output
            result.metadata["inter_char_delay_ms"] = self._config.inter_char_delay_ms

        elif mode == DecodeMode.spp.value:
            output = self._config.prefix + (result.data or "") + self._config.suffix + "\r\n"
            result.metadata["spp_output"] = output

        elif mode == DecodeMode.api.value:
            result.metadata["api_decode_event"] = {
                "symbology": result.symbology,
                "data": result.data,
                "confidence": result.confidence,
            }

        return result

    def get_capabilities(self) -> dict[str, Any]:
        return dict(self._VENDOR_FEATURES)


class ZebraSNAPIAdapter(_BaseAdapter):
    _VENDOR_FEATURES = {
        "vendor": "zebra_snapi",
        "sdk": "CoreScanner",
        "transports": ["usb_hid", "usb_cdc", "ssi"],
        "image_capture": True,
        "firmware_update": True,
        "parameter_programming": True,
        "beeper_control": True,
        "led_control": True,
    }


class HoneywellAdapter(_BaseAdapter):
    _VENDOR_FEATURES = {
        "vendor": "honeywell",
        "sdk": "FreeScan",
        "transports": ["usb_hid", "rs232", "bluetooth"],
        "image_capture": True,
        "firmware_update": True,
        "parameter_programming": True,
        "aim_control": True,
    }


class DatalogicAdapter(_BaseAdapter):
    _VENDOR_FEATURES = {
        "vendor": "datalogic",
        "sdk": "Aladdin",
        "transports": ["usb_hid", "rs232", "usb_cdc"],
        "image_capture": True,
        "parameter_programming": True,
        "green_spot_aim": True,
    }


class NewlandAdapter(_BaseAdapter):
    _VENDOR_FEATURES = {
        "vendor": "newland",
        "sdk": "NLS",
        "transports": ["uart", "usb_hid", "usb_cdc"],
        "image_capture": True,
        "firmware_update": True,
        "illumination_control": True,
    }


_ADAPTER_MAP: dict[str, type[BarcodeScanner]] = {
    VendorId.zebra_snapi.value: ZebraSNAPIAdapter,
    VendorId.honeywell.value: HoneywellAdapter,
    VendorId.datalogic.value: DatalogicAdapter,
    VendorId.newland.value: NewlandAdapter,
}


# ── Factory ────────────────────────────────────────────────────────────

def create_scanner(vendor_id: str, config: Optional[ScannerConfig] = None) -> BarcodeScanner:
    cls = _ADAPTER_MAP.get(vendor_id)
    if cls is None:
        raise ValueError(f"Unknown vendor: {vendor_id}")
    if config is None:
        config = ScannerConfig(vendor_id=vendor_id)
    return cls(config)


# ── Public query functions ─────────────────────────────────────────────

def list_vendors() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    vendors = cfg.get("vendors", [])
    return [
        VendorDef(
            vendor_id=v["id"],
            name=v["name"],
            description=v.get("description", ""),
            sdk_name=v.get("sdk_name", ""),
            transport=v.get("transport", []),
            supported_symbologies=v.get("supported_symbologies", []),
            decode_modes=v.get("decode_modes", []),
            features=v.get("features", []),
        )
        for v in vendors
    ]


def list_symbologies(category: Optional[str] = None) -> list[dict[str, Any]]:
    cfg = _get_cfg()
    sym_cfg = cfg.get("symbologies", {})
    results: list[dict[str, Any]] = []

    for cat_key in ("one_d", "two_d"):
        if category and cat_key != category:
            continue
        for s in sym_cfg.get(cat_key, []):
            sd = SymbologyDef(
                symbology_id=s["id"],
                name=s["name"],
                category=cat_key,
                description=s.get("description", ""),
                digit_count=s.get("digit_count"),
                charset=s.get("charset", ""),
                check_digit=s.get("check_digit", False),
                max_data_chars=s.get("max_data_chars"),
                error_correction_levels=[str(x) for x in s.get("error_correction_levels", [])],
            )
            results.append(asdict(sd))

    return results


def list_decode_modes() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    modes = cfg.get("decode_modes", [])
    return [
        asdict(DecodeModeDef(
            mode_id=m["id"],
            name=m["name"],
            description=m.get("description", ""),
            transport=m.get("transport", ""),
            requires_driver=m.get("requires_driver", False),
            latency=m.get("latency", "medium"),
            features=m.get("features", []),
        ))
        for m in modes
    ]


def list_frame_samples() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    samples = cfg.get("frame_samples", [])
    return [
        asdict(FrameSample(
            sample_id=s["id"],
            symbology=s["symbology"],
            description=s.get("description", ""),
            expected_data=s.get("expected_data", ""),
            format=s.get("format", "grayscale_8bit"),
            width=s.get("width", 200),
            height=s.get("height", 100),
        ))
        for s in samples
    ]


def generate_frame_sample(sample_id: str) -> tuple[bytes, dict[str, Any]]:
    cfg = _get_cfg()
    samples = cfg.get("frame_samples", [])
    sample = None
    for s in samples:
        if s["id"] == sample_id:
            sample = s
            break
    if sample is None:
        raise ValueError(f"Unknown frame sample: {sample_id}")

    frame = _generate_synthetic_frame(
        sample["symbology"],
        sample["expected_data"],
        sample.get("width", 200),
        sample.get("height", 100),
    )
    meta = {
        "sample_id": sample_id,
        "symbology": sample["symbology"],
        "expected_data": sample["expected_data"],
        "frame_size": len(frame),
        "frame_hash": _frame_hash(frame),
    }
    return frame, meta


def decode_frame(vendor_id: str, frame_data: bytes,
                 symbology_filter: Optional[list[str]] = None) -> ScanResult:
    config = ScannerConfig(
        vendor_id=vendor_id,
        enabled_symbologies=symbology_filter or [],
    )
    scanner = create_scanner(vendor_id, config)
    scanner.connect()
    scanner.configure(config)
    result = scanner.scan(frame_data)
    scanner.disconnect()
    return result


def validate_frame_sample(sample_id: str, vendor_id: str) -> dict[str, Any]:
    frame, meta = generate_frame_sample(sample_id)
    result = decode_frame(vendor_id, frame)
    expected = meta["expected_data"]
    match = result.data == expected
    return {
        "sample_id": sample_id,
        "vendor_id": vendor_id,
        "expected": expected,
        "decoded": result.data,
        "match": match,
        "status": "passed" if match else "failed",
        "confidence": result.confidence,
        "decode_time_ms": result.decode_time_ms,
    }


# ── Test recipes ───────────────────────────────────────────────────────

def list_test_recipes() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    recipes = cfg.get("test_recipes", [])
    return [
        asdict(TestRecipeDef(
            recipe_id=r["id"],
            name=r["name"],
            description=r.get("description", ""),
            domains=r.get("domains", []),
        ))
        for r in recipes
    ]


def run_test_recipe(recipe_id: str) -> TestResult:
    recipes = {r["recipe_id"]: r for r in list_test_recipes()}
    if recipe_id not in recipes:
        return TestResult(
            recipe_id=recipe_id,
            status=TestStatus.error.value,
            details=[{"error": f"Unknown recipe: {recipe_id}"}],
        )

    recipes[recipe_id]
    t0 = time.monotonic()

    if recipe_id == "vendor_adapter_lifecycle":
        return _run_vendor_lifecycle_recipe(recipe_id, t0)
    elif recipe_id == "symbology_decode":
        return _run_symbology_decode_recipe(recipe_id, t0)
    elif recipe_id == "decode_mode_switch":
        return _run_decode_mode_recipe(recipe_id, t0)
    elif recipe_id == "frame_sample_validation":
        return _run_frame_sample_recipe(recipe_id, t0)
    elif recipe_id == "multi_vendor_roundtrip":
        return _run_multi_vendor_recipe(recipe_id, t0)
    elif recipe_id == "error_handling":
        return _run_error_handling_recipe(recipe_id, t0)

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.skipped.value,
        details=[{"note": "No runner for recipe"}],
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
    )


def _run_vendor_lifecycle_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    for vid in VendorId:
        try:
            scanner = create_scanner(vid.value)
            assert scanner.connect(), "connect failed"
            cfg = ScannerConfig(vendor_id=vid.value, decode_mode="api")
            assert scanner.configure(cfg), "configure failed"
            assert scanner.state == ScannerState.configured
            caps = scanner.get_capabilities()
            assert "vendor" in caps
            assert scanner.disconnect(), "disconnect failed"
            assert scanner.state == ScannerState.disconnected
            details.append({"vendor": vid.value, "status": "passed"})
            passed += 1
        except Exception as e:
            details.append({"vendor": vid.value, "status": "failed", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_symbology_decode_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0
    samples = list_frame_samples()

    for sample in samples:
        try:
            frame, meta = generate_frame_sample(sample["sample_id"])
            result = decode_frame("zebra_snapi", frame)
            if result.data == meta["expected_data"]:
                details.append({"sample": sample["sample_id"], "status": "passed"})
                passed += 1
            else:
                details.append({
                    "sample": sample["sample_id"],
                    "status": "failed",
                    "expected": meta["expected_data"],
                    "got": result.data,
                })
                failed += 1
        except Exception as e:
            details.append({"sample": sample["sample_id"], "status": "error", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_decode_mode_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    frame, meta = generate_frame_sample("qr_code_sample")

    for mode in DecodeMode:
        try:
            config = ScannerConfig(vendor_id="zebra_snapi", decode_mode=mode.value)
            scanner = create_scanner("zebra_snapi", config)
            scanner.connect()
            scanner.configure(config)
            result = scanner.scan(frame)
            assert result.status == ScanResultStatus.success.value

            if mode == DecodeMode.hid_wedge:
                assert "hid_output" in result.metadata
            elif mode == DecodeMode.spp:
                assert "spp_output" in result.metadata
            elif mode == DecodeMode.api:
                assert "api_decode_event" in result.metadata

            scanner.disconnect()
            details.append({"mode": mode.value, "status": "passed"})
            passed += 1
        except Exception as e:
            details.append({"mode": mode.value, "status": "failed", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_frame_sample_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0
    samples = list_frame_samples()

    for sample in samples:
        for vid in VendorId:
            try:
                vr = validate_frame_sample(sample["sample_id"], vid.value)
                if vr["match"]:
                    passed += 1
                else:
                    failed += 1
                details.append({
                    "sample": sample["sample_id"],
                    "vendor": vid.value,
                    "status": vr["status"],
                })
            except Exception as e:
                details.append({
                    "sample": sample["sample_id"],
                    "vendor": vid.value,
                    "status": "error",
                    "error": str(e),
                })
                failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_multi_vendor_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0
    samples = list_frame_samples()

    for sample in samples:
        frame, meta = generate_frame_sample(sample["sample_id"])
        results_by_vendor = {}
        for vid in VendorId:
            r = decode_frame(vid.value, frame)
            results_by_vendor[vid.value] = r.data

        data_values = list(results_by_vendor.values())
        all_same = all(d == data_values[0] for d in data_values) and data_values[0] == meta["expected_data"]

        if all_same:
            passed += 1
            details.append({"sample": sample["sample_id"], "status": "passed"})
        else:
            failed += 1
            details.append({
                "sample": sample["sample_id"],
                "status": "failed",
                "results": results_by_vendor,
                "expected": meta["expected_data"],
            })

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_error_handling_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    # Test 1: corrupt/empty frame
    try:
        result = decode_frame("zebra_snapi", b"")
        assert result.status == ScanResultStatus.no_decode.value
        details.append({"test": "empty_frame", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "empty_frame", "status": "failed", "error": str(e)})
        failed += 1

    # Test 2: random bytes
    try:
        result = decode_frame("zebra_snapi", b"\xff" * 100)
        assert result.status == ScanResultStatus.no_decode.value
        details.append({"test": "random_bytes", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "random_bytes", "status": "failed", "error": str(e)})
        failed += 1

    # Test 3: scan when disconnected
    try:
        scanner = create_scanner("zebra_snapi")
        result = scanner.scan(b"test")
        assert result.status == ScanResultStatus.error.value
        details.append({"test": "scan_disconnected", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "scan_disconnected", "status": "failed", "error": str(e)})
        failed += 1

    # Test 4: unsupported symbology filter
    try:
        frame, _ = generate_frame_sample("qr_code_sample")
        result = decode_frame("zebra_snapi", frame, symbology_filter=["upc_a"])
        assert result.status == ScanResultStatus.unsupported_symbology.value
        details.append({"test": "symbology_filter", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "symbology_filter", "status": "failed", "error": str(e)})
        failed += 1

    # Test 5: unknown vendor
    try:
        create_scanner("unknown_vendor")
        details.append({"test": "unknown_vendor", "status": "failed", "error": "no exception"})
        failed += 1
    except ValueError:
        details.append({"test": "unknown_vendor", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "unknown_vendor", "status": "failed", "error": str(e)})
        failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


# ── Artifacts & gate ───────────────────────────────────────────────────

def list_artifacts() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    arts = cfg.get("artifacts", [])
    return [
        asdict(ArtifactDef(
            artifact_id=a["id"],
            kind=a["kind"],
            description=a.get("description", ""),
        ))
        for a in arts
    ]


def validate_gate() -> dict[str, Any]:
    t0 = time.monotonic()
    recipe_results = []
    all_passed = True

    for recipe in list_test_recipes():
        result = run_test_recipe(recipe["recipe_id"])
        recipe_results.append(asdict(result))
        if result.status != TestStatus.passed.value:
            all_passed = False

    total_tests = sum(r["total"] for r in recipe_results)
    total_passed = sum(r["passed"] for r in recipe_results)
    total_failed = sum(r["failed"] for r in recipe_results)

    return {
        "verdict": GateVerdict.passed.value if all_passed else GateVerdict.failed.value,
        "total_recipes": len(recipe_results),
        "total_tests": total_tests,
        "total_passed": total_passed,
        "total_failed": total_failed,
        "duration_ms": round((time.monotonic() - t0) * 1000, 2),
        "recipes": recipe_results,
    }
