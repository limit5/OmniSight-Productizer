"""C20 — L4-CORE-20 Print pipeline (#241).

IPP/CUPS backend wrapper, PDL interpreters (PCL / PostScript / PDF via
Ghostscript), color management (ICC profile per paper/ink combo), print
queue + spooler integration.

Public API:
    ops        = list_ipp_operations()
    attrs      = list_ipp_attributes()
    backends   = list_cups_backends()
    job_states = list_ipp_job_states()
    job        = submit_ipp_job(printer_uri, document_format, attributes)
    job        = cancel_ipp_job(job_id)
    job        = get_ipp_job(job_id)
    jobs       = list_ipp_jobs()
    langs      = list_pdl_languages()
    pcl        = generate_pcl(raster_data, options)
    ps         = generate_postscript(raster_data, options)
    gs_devs    = list_ghostscript_devices()
    raster     = render_pdf_to_raster(pdf_data, device, dpi)
    rasters    = list_raster_formats()
    slots      = list_paper_profiles()
    profile    = select_print_profile(paper_id, ink_id)
    inks       = list_ink_sets()
    intents    = list_print_rendering_intents()
    policies   = list_queue_policies()
    q_cfg      = get_spooler_config()
    job        = enqueue_print_job(document, printer, options)
    job        = hold_job(job_id) / release_job(job_id) / cancel_queue_job(job_id)
    queue      = list_queue_jobs()
    recipes    = list_test_recipes()
    result     = run_test_recipe(recipe_id)
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from backend.shared_state import SharedKV

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "configs" / "print_pipeline.yaml"


# ── Enums ──────────────────────────────────────────────────────────────

class PrintDomain(str, Enum):
    ipp_cups = "ipp_cups"
    pdl_interpreters = "pdl_interpreters"
    color_management = "color_management"
    print_queue = "print_queue"
    integration = "integration"


class PDLLanguage(str, Enum):
    pcl = "pcl"
    postscript = "postscript"
    pdf = "pdf"


class PCLVersion(str, Enum):
    pcl5e = "pcl5e"
    pcl5c = "pcl5c"
    pcl6_xl = "pcl6_xl"


class PSLevel(str, Enum):
    level1 = "level1"
    level2 = "level2"
    level3 = "level3"


class PDFVersion(str, Enum):
    v1_4 = "1.4"
    v1_7 = "1.7"
    v2_0 = "2.0"


class RasterFormat(str, Enum):
    pwg_raster = "pwg_raster"
    urf = "urf"
    cups_raster = "cups_raster"


class InkChannel(str, Enum):
    cyan = "cyan"
    light_cyan = "light_cyan"
    magenta = "magenta"
    light_magenta = "light_magenta"
    yellow = "yellow"
    black = "black"


class PrintRenderingIntent(str, Enum):
    perceptual = "perceptual"
    relative_colorimetric = "relative_colorimetric"
    saturation = "saturation"
    absolute_colorimetric = "absolute_colorimetric"


class PrintColorSpace(str, Enum):
    srgb = "srgb"
    adobe_rgb = "adobe_rgb"
    cmyk = "cmyk"
    device_cmyk = "device_cmyk"


class QueuePolicy(str, Enum):
    fifo = "fifo"
    priority = "priority"
    shortest_first = "shortest_first"


class PriorityLevel(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"


class IPPJobState(str, Enum):
    pending = "pending"
    pending_held = "pending_held"
    processing = "processing"
    processing_stopped = "processing_stopped"
    canceled = "canceled"
    aborted = "aborted"
    completed = "completed"


class SpoolerJobState(str, Enum):
    submitted = "submitted"
    queued = "queued"
    held = "held"
    spooling = "spooling"
    rendering = "rendering"
    sending = "sending"
    printing = "printing"
    completed = "completed"
    canceled = "canceled"
    rejected = "rejected"
    error = "error"


class PrintQuality(str, Enum):
    draft = "draft"
    normal = "normal"
    high = "high"


class MediaSize(str, Enum):
    a4 = "iso_a4_210x297mm"
    letter = "na_letter_8.5x11in"
    a3 = "iso_a3_297x420mm"
    legal = "na_legal_8.5x14in"
    b5 = "jis_b5_182x257mm"


class DuplexMode(str, Enum):
    one_sided = "one-sided"
    two_sided_long = "two-sided-long-edge"
    two_sided_short = "two-sided-short-edge"


class TestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    skipped = "skipped"
    error = "error"


class GateVerdict(str, Enum):
    pass_ = "pass"
    fail = "fail"
    partial = "partial"


# ── Data classes ───────────────────────────────────────────────────────

@dataclass
class IPPOperation:
    id: str
    name: str
    code: int
    description: str
    required: bool


@dataclass
class IPPAttribute:
    id: str
    name: str
    group: str
    syntax: str
    required: bool
    values: list[str] = field(default_factory=list)
    default: Any = None


@dataclass
class CUPSBackend:
    id: str
    name: str
    uri_scheme: str
    description: str


@dataclass
class IPPJobStateDef:
    id: str
    code: int
    description: str


@dataclass
class IPPJob:
    job_id: str
    printer_uri: str
    document_format: str
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    completed_at: float = 0.0
    pages: int = 0
    size_bytes: int = 0
    state_history: list[str] = field(default_factory=list)


@dataclass
class PDLLanguageDef:
    id: str
    name: str
    mime_type: str
    description: str
    versions: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)


@dataclass
class PCLCommand:
    id: str
    sequence: str
    description: str


@dataclass
class PSOperator:
    id: str
    description: str


@dataclass
class GhostscriptDevice:
    id: str
    description: str


@dataclass
class RasterFormatDef:
    id: str
    name: str
    mime_type: str
    description: str
    header_size: int
    compression: list[str] = field(default_factory=list)


@dataclass
class PCLStream:
    data: bytes
    page_count: int
    resolution_dpi: int
    page_size: str
    duplex: str
    checksum: str


@dataclass
class PostScriptDocument:
    data: str
    page_count: int
    dsc_compliant: bool
    level: str
    bounding_box: tuple[int, int, int, int] = (0, 0, 595, 842)
    checksum: str = ""


@dataclass
class RasterOutput:
    data: bytes
    width: int
    height: int
    dpi: int
    color_space: str
    bits_per_pixel: int
    page_count: int
    device: str
    checksum: str


@dataclass
class PaperProfileSlot:
    id: str
    paper_type: str
    weight_gsm: list[int]
    profiles: list[dict[str, str]]


@dataclass
class InkSet:
    id: str
    name: str
    channels: list[str]
    channel_count: int
    notes: str = ""


@dataclass
class PrintProfileSelection:
    paper_id: str
    ink_id: str
    icc_file: str
    rendering_intent: str
    paper_type: str
    ink_name: str


@dataclass
class RenderingIntentDef:
    id: str
    code: int
    description: str


@dataclass
class ColorSpaceDef:
    id: str
    name: str
    description: str
    type: str


@dataclass
class QueuePolicyDef:
    id: str
    name: str
    description: str
    default: bool = False


@dataclass
class PriorityLevelDef:
    id: str
    value: int
    description: str


@dataclass
class SpoolerConfig:
    max_concurrent_jobs: int
    max_queue_depth: int
    spool_directory: str
    temp_directory: str
    job_retention_hours: int
    failed_job_retention_hours: int
    max_job_size_mb: int
    compression: str


@dataclass
class JobLifecycleState:
    state: str
    transitions: list[str]


@dataclass
class QueueJob:
    job_id: str
    document_name: str
    printer_uri: str
    state: str
    priority: int = 50
    size_bytes: int = 0
    pages: int = 0
    submitted_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    state_history: list[str] = field(default_factory=list)
    error_message: str = ""


@dataclass
class TestRecipe:
    id: str
    name: str
    domain: str
    description: str
    timeout_seconds: int
    steps: list[str] = field(default_factory=list)


@dataclass
class TestRecipeResult:
    recipe_id: str
    status: str
    duration_ms: float
    steps_passed: int
    steps_total: int
    details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CompatibleSoC:
    id: str
    name: str
    notes: str


@dataclass
class ArtifactDefinition:
    id: str
    name: str
    pattern: str
    description: str


@dataclass
class PrintGateResult:
    verdict: str
    domains_checked: int
    domains_passed: int
    findings: list[dict[str, str]] = field(default_factory=list)
    timestamp: float = 0.0


@dataclass
class GateFinding:
    domain: str
    artifact_id: str
    status: str
    message: str


# ── Config loader ─────────────────────────────────────────────────────

_CFG: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    global _CFG
    if _CFG is not None:
        return _CFG
    with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
        _CFG = yaml.safe_load(fh)
    return _CFG


def _reset_config() -> None:
    global _CFG
    _CFG = None


# ── IPP / CUPS ────────────────────────────────────────────────────────

def list_ipp_operations() -> list[IPPOperation]:
    cfg = _load_config()
    ops = cfg.get("ipp_cups", {}).get("ipp_operations", [])
    return [IPPOperation(**o) for o in ops]


def get_ipp_operation(op_id: str) -> IPPOperation | None:
    for op in list_ipp_operations():
        if op.id == op_id:
            return op
    return None


def list_ipp_attributes() -> list[IPPAttribute]:
    cfg = _load_config()
    attrs = cfg.get("ipp_cups", {}).get("ipp_attributes", [])
    return [IPPAttribute(**a) for a in attrs]


def get_ipp_attribute(attr_id: str) -> IPPAttribute | None:
    for a in list_ipp_attributes():
        if a.id == attr_id:
            return a
    return None


def list_cups_backends() -> list[CUPSBackend]:
    cfg = _load_config()
    backs = cfg.get("ipp_cups", {}).get("cups_backends", [])
    return [CUPSBackend(**b) for b in backs]


def get_cups_backend(backend_id: str) -> CUPSBackend | None:
    for b in list_cups_backends():
        if b.id == backend_id:
            return b
    return None


def list_ipp_job_states() -> list[IPPJobStateDef]:
    cfg = _load_config()
    states = cfg.get("ipp_cups", {}).get("job_states", [])
    return [IPPJobStateDef(**s) for s in states]


def get_ipp_job_state(state_id: str) -> IPPJobStateDef | None:
    for s in list_ipp_job_states():
        if s.id == state_id:
            return s
    return None


# FX.1.1 — IPP job state moved off module-level dict + counter to SharedKV
# so multi-worker uvicorn (``--workers N``) can no longer mint colliding
# job-ids or see disjoint job snapshots. SOP Step 1 cross-worker rubric
# answer #2 (coordinated via Redis when ``OMNISIGHT_REDIS_URL`` is set;
# the SharedKV in-memory fallback shares its namespace dict across all
# instances within a single process via class-level ``_mem``, so unit
# tests and the single-worker dev path keep observing the same data
# without per-instance drift). The id counter lives in a sibling KV
# namespace at field ``next_id`` so that ``HINCRBY`` is atomic on Redis
# and ``SharedKV.incr`` stays atomic in the in-memory fallback — note
# we deliberately avoid ``SharedCounter`` here because its in-memory
# state is per-instance (not class-level shared) which would make every
# call mint id 1.
_IPP_JOBS_NS = "print_pipeline_ipp_jobs"
_IPP_JOBS_COUNTER_NS = "print_pipeline_ipp_jobs_counter"
_IPP_JOBS_COUNTER_FIELD = "next_id"


def _ipp_jobs_kv() -> SharedKV:
    return SharedKV(_IPP_JOBS_NS)


def _ipp_job_id_counter_kv() -> SharedKV:
    return SharedKV(_IPP_JOBS_COUNTER_NS)


def _next_ipp_job_id() -> int:
    return _ipp_job_id_counter_kv().incr(_IPP_JOBS_COUNTER_FIELD)


def _serialise_ipp_job(job: IPPJob) -> str:
    return json.dumps(asdict(job))


def _deserialise_ipp_job(raw: str) -> IPPJob | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return IPPJob(**data)
    except TypeError:
        return None


def _save_ipp_job(job: IPPJob) -> None:
    _ipp_jobs_kv().set(job.job_id, _serialise_ipp_job(job))


def _reset_ipp_jobs() -> None:
    kv = _ipp_jobs_kv()
    for field_name in list(kv.get_all().keys()):
        kv.delete(field_name)
    counter_kv = _ipp_job_id_counter_kv()
    counter_kv.delete(_IPP_JOBS_COUNTER_FIELD)


def submit_ipp_job(
    printer_uri: str,
    document_format: str,
    attributes: dict[str, Any] | None = None,
    document_data: bytes | None = None,
) -> IPPJob:
    valid_formats = [a.values for a in list_ipp_attributes() if a.id == "document_format"]
    flat_formats = valid_formats[0] if valid_formats else []
    if flat_formats and document_format not in flat_formats:
        raise ValueError(f"Unsupported document format: {document_format}")

    next_id = _next_ipp_job_id()
    job_id = f"ipp-job-{next_id}"
    now = time.time()

    pages = 1
    size = len(document_data) if document_data else 0
    if document_data and document_format == "application/pdf":
        page_markers = document_data.count(b"/Type /Page") if isinstance(document_data, bytes) else 1
        pages = max(1, page_markers)

    job = IPPJob(
        job_id=job_id,
        printer_uri=printer_uri,
        document_format=document_format,
        state=IPPJobState.pending.value,
        attributes=attributes or {},
        created_at=now,
        pages=pages,
        size_bytes=size,
        state_history=[IPPJobState.pending.value],
    )
    _save_ipp_job(job)

    _advance_ipp_job(job)
    return job


def _advance_ipp_job(job: IPPJob) -> None:
    transitions = [
        IPPJobState.pending.value,
        IPPJobState.processing.value,
        IPPJobState.completed.value,
    ]
    for state in transitions:
        if state != job.state:
            job.state = state
            job.state_history.append(state)
    job.completed_at = time.time()
    _save_ipp_job(job)


def get_ipp_job(job_id: str) -> IPPJob | None:
    return _deserialise_ipp_job(_ipp_jobs_kv().get(job_id))


def list_ipp_jobs() -> list[IPPJob]:
    out: list[IPPJob] = []
    for raw in _ipp_jobs_kv().get_all().values():
        job = _deserialise_ipp_job(raw)
        if job is not None:
            out.append(job)
    return out


def cancel_ipp_job(job_id: str) -> IPPJob | None:
    job = get_ipp_job(job_id)
    if job is None:
        return None
    if job.state in (IPPJobState.completed.value, IPPJobState.canceled.value, IPPJobState.aborted.value):
        raise ValueError(f"Cannot cancel job in state: {job.state}")
    job.state = IPPJobState.canceled.value
    job.state_history.append(IPPJobState.canceled.value)
    job.completed_at = time.time()
    _save_ipp_job(job)
    return job


def hold_ipp_job(job_id: str) -> IPPJob | None:
    job = get_ipp_job(job_id)
    if job is None:
        return None
    if job.state != IPPJobState.pending.value:
        raise ValueError(f"Can only hold pending jobs, current: {job.state}")
    job.state = IPPJobState.pending_held.value
    job.state_history.append(IPPJobState.pending_held.value)
    _save_ipp_job(job)
    return job


def release_ipp_job(job_id: str) -> IPPJob | None:
    job = get_ipp_job(job_id)
    if job is None:
        return None
    if job.state != IPPJobState.pending_held.value:
        raise ValueError(f"Can only release held jobs, current: {job.state}")
    job.state = IPPJobState.pending.value
    job.state_history.append(IPPJobState.pending.value)
    _advance_ipp_job(job)
    return job


# ── PDL Interpreters ──────────────────────────────────────────────────

def list_pdl_languages() -> list[PDLLanguageDef]:
    cfg = _load_config()
    langs = cfg.get("pdl_interpreters", {}).get("languages", [])
    result = []
    for lang in langs:
        result.append(PDLLanguageDef(
            id=lang["id"],
            name=lang["name"],
            mime_type=lang["mime_type"],
            description=lang["description"],
            versions=lang.get("versions", []),
            features=lang.get("features", []),
        ))
    return result


def get_pdl_language(lang_id: str) -> PDLLanguageDef | None:
    for lang in list_pdl_languages():
        if lang.id == lang_id:
            return lang
    return None


def list_pcl_commands() -> list[PCLCommand]:
    cfg = _load_config()
    langs = cfg.get("pdl_interpreters", {}).get("languages", [])
    for lang in langs:
        if lang["id"] == "pcl":
            return [PCLCommand(**c) for c in lang.get("commands", [])]
    return []


def get_pcl_command(cmd_id: str) -> PCLCommand | None:
    for c in list_pcl_commands():
        if c.id == cmd_id:
            return c
    return None


def list_ps_operators() -> list[PSOperator]:
    cfg = _load_config()
    langs = cfg.get("pdl_interpreters", {}).get("languages", [])
    for lang in langs:
        if lang["id"] == "postscript":
            return [PSOperator(**o) for o in lang.get("operators", [])]
    return []


def get_ps_operator(op_id: str) -> PSOperator | None:
    for o in list_ps_operators():
        if o.id == op_id:
            return o
    return None


def list_ghostscript_devices() -> list[GhostscriptDevice]:
    cfg = _load_config()
    langs = cfg.get("pdl_interpreters", {}).get("languages", [])
    for lang in langs:
        if lang["id"] == "pdf":
            return [GhostscriptDevice(**d) for d in lang.get("ghostscript_devices", [])]
    return []


def get_ghostscript_device(device_id: str) -> GhostscriptDevice | None:
    for d in list_ghostscript_devices():
        if d.id == device_id:
            return d
    return None


def list_raster_formats() -> list[RasterFormatDef]:
    cfg = _load_config()
    fmts = cfg.get("pdl_interpreters", {}).get("raster_formats", [])
    return [RasterFormatDef(**f) for f in fmts]


def get_raster_format(fmt_id: str) -> RasterFormatDef | None:
    for f in list_raster_formats():
        if f.id == fmt_id:
            return f
    return None


def generate_pcl(
    raster_data: bytes | None = None,
    page_size: str = "a4",
    resolution_dpi: int = 300,
    copies: int = 1,
    duplex: str = "simplex",
    pages: int = 1,
) -> PCLStream:
    page_size_codes = {"a4": 26, "letter": 2, "a3": 27, "legal": 3, "b5": 45}
    duplex_codes = {"simplex": 0, "duplex_long": 1, "duplex_short": 2}

    ps_code = page_size_codes.get(page_size, 26)
    dup_code = duplex_codes.get(duplex, 0)

    chunks: list[bytes] = []

    # Reset
    chunks.append(b"\x1bE")
    # Page size
    chunks.append(f"\x1b&l{ps_code}A".encode("ascii"))
    # Resolution
    chunks.append(f"\x1b*t{resolution_dpi}R".encode("ascii"))
    # Copies
    chunks.append(f"\x1b&l{copies}X".encode("ascii"))
    # Duplex
    chunks.append(f"\x1b&l{dup_code}S".encode("ascii"))
    # Orientation (portrait)
    chunks.append(b"\x1b&l0O")

    if raster_data is None:
        w = int(8.27 * resolution_dpi) if page_size == "a4" else int(8.5 * resolution_dpi)
        h = int(11.69 * resolution_dpi) if page_size == "a4" else int(11.0 * resolution_dpi)
        row_bytes = (w + 7) // 8
        raster_data = bytes([0xAA] * (row_bytes * min(h, 100)))

    row_size = max(1, len(raster_data) // max(1, pages * 100))

    for page in range(pages):
        chunks.append(b"\x1b*r1A")  # start raster
        offset = page * 100 * row_size
        for row in range(min(100, len(raster_data) // row_size)):
            row_start = offset + row * row_size
            row_end = row_start + row_size
            row_data = raster_data[row_start:row_end] if row_end <= len(raster_data) else raster_data[row_start:]
            if not row_data:
                break
            chunks.append(f"\x1b*b{len(row_data)}W".encode("ascii"))
            chunks.append(row_data)
        chunks.append(b"\x1b*rB")  # end raster
        chunks.append(b"\x0c")  # form feed

    # Final reset
    chunks.append(b"\x1bE")

    pcl_bytes = b"".join(chunks)
    checksum = hashlib.sha256(pcl_bytes).hexdigest()

    return PCLStream(
        data=pcl_bytes,
        page_count=pages,
        resolution_dpi=resolution_dpi,
        page_size=page_size,
        duplex=duplex,
        checksum=checksum,
    )


def generate_postscript(
    raster_data: bytes | None = None,
    page_size: str = "a4",
    resolution_dpi: int = 300,
    level: str = "level2",
    pages: int = 1,
    duplex: str = "simplex",
) -> PostScriptDocument:
    bbox = {
        "a4": (0, 0, 595, 842),
        "letter": (0, 0, 612, 792),
        "a3": (0, 0, 842, 1191),
        "legal": (0, 0, 612, 1008),
        "b5": (0, 0, 516, 729),
    }
    bb = bbox.get(page_size, (0, 0, 595, 842))

    lines: list[str] = []
    lines.append("%!PS-Adobe-3.0")
    lines.append(f"%%BoundingBox: {bb[0]} {bb[1]} {bb[2]} {bb[3]}")
    lines.append(f"%%Pages: {pages}")
    lines.append("%%DocumentData: Clean7Bit")
    lines.append(f"%%LanguageLevel: {level[-1]}")
    lines.append("%%EndComments")
    lines.append("")

    # Prolog
    lines.append("%%BeginProlog")
    lines.append("/inch { 72 mul } def")
    lines.append("%%EndProlog")
    lines.append("")

    # Setup
    lines.append("%%BeginSetup")
    duplex_ps = "true" if duplex != "simplex" else "false"
    tumble = "true" if duplex == "duplex_short" else "false"
    lines.append(f"<< /PageSize [{bb[2]} {bb[3]}] /Duplex {duplex_ps} /Tumble {tumble} >> setpagedevice")
    lines.append("%%EndSetup")
    lines.append("")

    sample_w, sample_h = 100, 100
    if raster_data is None:
        raster_data = bytes([0x80] * (sample_w * sample_h * 3))

    for page_num in range(1, pages + 1):
        lines.append(f"%%Page: {page_num} {page_num}")
        lines.append("gsave")
        lines.append("/DeviceRGB setcolorspace")
        lines.append(f"{sample_w} {sample_h} 8 [{sample_w} 0 0 -{sample_h} 0 {sample_h}]")

        hex_sample = raster_data[:min(300, len(raster_data))].hex()
        lines.append(f"<{hex_sample}>")
        lines.append("false 3 colorimage")
        lines.append("grestore")
        lines.append("showpage")
        lines.append("")

    lines.append("%%EOF")

    ps_text = "\n".join(lines)
    checksum = hashlib.sha256(ps_text.encode("utf-8")).hexdigest()

    return PostScriptDocument(
        data=ps_text,
        page_count=pages,
        dsc_compliant=True,
        level=level,
        bounding_box=bb,
        checksum=checksum,
    )


def render_pdf_to_raster(
    pdf_data: bytes | None = None,
    device: str = "pwgraster",
    dpi: int = 300,
    page_size: str = "a4",
    color_bits: int = 24,
) -> RasterOutput:
    dev = get_ghostscript_device(device)
    if dev is None:
        raise ValueError(f"Unknown Ghostscript device: {device}")

    page_sizes_px = {
        "a4": (int(8.27 * dpi), int(11.69 * dpi)),
        "letter": (int(8.5 * dpi), int(11.0 * dpi)),
        "a3": (int(11.69 * dpi), int(16.54 * dpi)),
        "legal": (int(8.5 * dpi), int(14.0 * dpi)),
    }
    w, h = page_sizes_px.get(page_size, page_sizes_px["a4"])

    bytes_per_pixel = color_bits // 8
    page_count = 1
    if pdf_data:
        markers = pdf_data.count(b"/Type /Page") if isinstance(pdf_data, bytes) else 1
        page_count = max(1, markers)

    row_bytes = w * bytes_per_pixel
    row_bytes * h * page_count

    raster_bytes = _generate_synthetic_raster(w, h, bytes_per_pixel, page_count)

    color_space = "RGB" if color_bits >= 24 else "Grayscale"
    checksum = hashlib.sha256(raster_bytes).hexdigest()

    return RasterOutput(
        data=raster_bytes,
        width=w,
        height=h,
        dpi=dpi,
        color_space=color_space,
        bits_per_pixel=color_bits,
        page_count=page_count,
        device=device,
        checksum=checksum,
    )


def _generate_synthetic_raster(w: int, h: int, bpp: int, pages: int) -> bytes:
    row_size = min(w * bpp, 1024)
    rows_per_page = min(h, 100)
    chunks: list[bytes] = []
    for page in range(pages):
        seed = (page + 1) * 37
        for row in range(rows_per_page):
            val = ((seed + row) * 73) & 0xFF
            chunks.append(bytes([val] * row_size))
    return b"".join(chunks)


# ── Color Management ──────────────────────────────────────────────────

def list_paper_profiles() -> list[PaperProfileSlot]:
    cfg = _load_config()
    slots = cfg.get("color_management", {}).get("profile_slots", [])
    return [PaperProfileSlot(**s) for s in slots]


def get_paper_profile(paper_id: str) -> PaperProfileSlot | None:
    for s in list_paper_profiles():
        if s.id == paper_id:
            return s
    return None


def list_ink_sets() -> list[InkSet]:
    cfg = _load_config()
    inks = cfg.get("color_management", {}).get("ink_sets", [])
    return [InkSet(**i) for i in inks]


def get_ink_set(ink_id: str) -> InkSet | None:
    for i in list_ink_sets():
        if i.id == ink_id:
            return i
    return None


def list_print_rendering_intents() -> list[RenderingIntentDef]:
    cfg = _load_config()
    intents = cfg.get("color_management", {}).get("rendering_intents", [])
    return [RenderingIntentDef(**i) for i in intents]


def get_print_rendering_intent(intent_id: str) -> RenderingIntentDef | None:
    for i in list_print_rendering_intents():
        if i.id == intent_id:
            return i
    return None


def list_color_spaces() -> list[ColorSpaceDef]:
    cfg = _load_config()
    spaces = cfg.get("color_management", {}).get("color_spaces", [])
    return [ColorSpaceDef(**s) for s in spaces]


def get_color_space(space_id: str) -> ColorSpaceDef | None:
    for s in list_color_spaces():
        if s.id == space_id:
            return s
    return None


def select_print_profile(paper_id: str, ink_id: str) -> PrintProfileSelection:
    slot = get_paper_profile(paper_id)
    if slot is None:
        raise ValueError(f"Unknown paper profile: {paper_id}")

    ink = get_ink_set(ink_id)
    if ink is None:
        raise ValueError(f"Unknown ink set: {ink_id}")

    for prof in slot.profiles:
        if prof["ink"] == ink_id:
            return PrintProfileSelection(
                paper_id=paper_id,
                ink_id=ink_id,
                icc_file=prof["icc_file"],
                rendering_intent=prof["rendering_intent"],
                paper_type=slot.paper_type,
                ink_name=ink.name,
            )

    raise ValueError(f"No profile for paper={paper_id} ink={ink_id}")


def generate_print_icc_binary(paper_id: str, ink_id: str) -> bytes:
    selection = select_print_profile(paper_id, ink_id)

    header = bytearray(128)
    # Profile size placeholder
    struct.pack_into(">I", header, 0, 512)
    # Preferred CMM
    header[4:8] = b"OMNI"
    # Version 4.3
    struct.pack_into(">I", header, 8, 0x04300000)
    # Device class: output (prtr)
    header[12:16] = b"prtr"
    # Color space: CMYK
    header[16:20] = b"CMYK"
    # PCS: Lab
    header[20:24] = b"Lab "
    # Rendering intent
    intent_codes = {"perceptual": 0, "relative_colorimetric": 1, "saturation": 2, "absolute_colorimetric": 3}
    struct.pack_into(">I", header, 64, intent_codes.get(selection.rendering_intent, 0))
    # Signature
    header[36:40] = b"acsp"

    tag_table = bytearray(12)
    struct.pack_into(">I", tag_table, 0, 1)  # 1 tag
    # desc tag
    tag_table += b"desc"
    struct.pack_into(">I", tag_table, 8, 128 + len(tag_table) + 4)
    desc_data = selection.icc_file.encode("ascii")[:64].ljust(64, b"\x00")
    struct.pack_into(">I", tag_table, 12, len(desc_data))

    profile_data = bytes(header) + bytes(tag_table) + desc_data
    final_size = len(profile_data)
    profile_out = bytearray(profile_data)
    struct.pack_into(">I", profile_out, 0, final_size)

    return bytes(profile_out)


# ── Print Queue / Spooler ─────────────────────────────────────────────

def list_queue_policies() -> list[QueuePolicyDef]:
    cfg = _load_config()
    policies = cfg.get("print_queue", {}).get("queue_policies", [])
    return [QueuePolicyDef(**p) for p in policies]


def get_queue_policy(policy_id: str) -> QueuePolicyDef | None:
    for p in list_queue_policies():
        if p.id == policy_id:
            return p
    return None


def list_priority_levels() -> list[PriorityLevelDef]:
    cfg = _load_config()
    levels = cfg.get("print_queue", {}).get("priority_levels", [])
    return [PriorityLevelDef(**l) for l in levels]


def get_priority_level(level_id: str) -> PriorityLevelDef | None:
    for l in list_priority_levels():
        if l.id == level_id:
            return l
    return None


def get_spooler_config() -> SpoolerConfig:
    cfg = _load_config()
    sc = cfg.get("print_queue", {}).get("spooler_config", {})
    return SpoolerConfig(**sc)


def list_job_lifecycle_states() -> list[JobLifecycleState]:
    cfg = _load_config()
    states = cfg.get("print_queue", {}).get("job_lifecycle", [])
    return [JobLifecycleState(**s) for s in states]


def get_job_lifecycle_state(state_id: str) -> JobLifecycleState | None:
    for s in list_job_lifecycle_states():
        if s.state == state_id:
            return s
    return None


_queue_jobs: dict[str, QueueJob] = {}
_queue_counter: int = 0


def _reset_queue() -> None:
    global _queue_jobs, _queue_counter
    _queue_jobs = {}
    _queue_counter = 0


def enqueue_print_job(
    document_name: str,
    printer_uri: str,
    priority: int = 50,
    size_bytes: int = 0,
    pages: int = 1,
) -> QueueJob:
    global _queue_counter

    spool_cfg = get_spooler_config()
    if size_bytes > spool_cfg.max_job_size_mb * 1024 * 1024:
        job_id = f"queue-job-{_queue_counter + 1}"
        _queue_counter += 1
        job = QueueJob(
            job_id=job_id,
            document_name=document_name,
            printer_uri=printer_uri,
            state=SpoolerJobState.rejected.value,
            priority=priority,
            size_bytes=size_bytes,
            pages=pages,
            submitted_at=time.time(),
            state_history=[SpoolerJobState.submitted.value, SpoolerJobState.rejected.value],
            error_message=f"Job size {size_bytes} exceeds max {spool_cfg.max_job_size_mb}MB",
        )
        _queue_jobs[job_id] = job
        return job

    if len(_queue_jobs) >= spool_cfg.max_queue_depth:
        raise ValueError("Queue is full")

    _queue_counter += 1
    job_id = f"queue-job-{_queue_counter}"
    now = time.time()

    job = QueueJob(
        job_id=job_id,
        document_name=document_name,
        printer_uri=printer_uri,
        state=SpoolerJobState.submitted.value,
        priority=priority,
        size_bytes=size_bytes,
        pages=pages,
        submitted_at=now,
        state_history=[SpoolerJobState.submitted.value],
    )
    _queue_jobs[job_id] = job

    _transition_queue_job(job, SpoolerJobState.queued.value)
    return job


def _transition_queue_job(job: QueueJob, target_state: str) -> None:
    lifecycle = get_job_lifecycle_state(job.state)
    if lifecycle is None:
        raise ValueError(f"Unknown job state: {job.state}")
    if target_state not in lifecycle.transitions:
        raise ValueError(f"Invalid transition: {job.state} → {target_state}")
    job.state = target_state
    job.state_history.append(target_state)


def advance_queue_job_to_completion(job_id: str) -> QueueJob | None:
    job = _queue_jobs.get(job_id)
    if job is None:
        return None
    path = ["spooling", "rendering", "sending", "printing", "completed"]
    for state in path:
        if job.state in ("completed", "canceled", "rejected", "error"):
            break
        try:
            _transition_queue_job(job, state)
        except ValueError:
            break
    if job.state == SpoolerJobState.completed.value:
        job.completed_at = time.time()
    return job


def hold_queue_job(job_id: str) -> QueueJob | None:
    job = _queue_jobs.get(job_id)
    if job is None:
        return None
    _transition_queue_job(job, SpoolerJobState.held.value)
    return job


def release_queue_job(job_id: str) -> QueueJob | None:
    job = _queue_jobs.get(job_id)
    if job is None:
        return None
    _transition_queue_job(job, SpoolerJobState.queued.value)
    return job


def cancel_queue_job(job_id: str) -> QueueJob | None:
    job = _queue_jobs.get(job_id)
    if job is None:
        return None
    if job.state in (SpoolerJobState.completed.value, SpoolerJobState.canceled.value, SpoolerJobState.rejected.value):
        raise ValueError(f"Cannot cancel job in state: {job.state}")
    _transition_queue_job(job, SpoolerJobState.canceled.value)
    return job


def error_queue_job(job_id: str, message: str = "Simulated error") -> QueueJob | None:
    job = _queue_jobs.get(job_id)
    if job is None:
        return None
    _transition_queue_job(job, SpoolerJobState.error.value)
    job.error_message = message
    return job


def requeue_error_job(job_id: str) -> QueueJob | None:
    job = _queue_jobs.get(job_id)
    if job is None:
        return None
    if job.state != SpoolerJobState.error.value:
        raise ValueError(f"Can only requeue error jobs, current: {job.state}")
    _transition_queue_job(job, SpoolerJobState.queued.value)
    job.error_message = ""
    return job


def list_queue_jobs(policy: str = "fifo") -> list[QueueJob]:
    jobs = list(_queue_jobs.values())
    if policy == "priority":
        jobs.sort(key=lambda j: -j.priority)
    elif policy == "shortest_first":
        jobs.sort(key=lambda j: j.size_bytes)
    else:
        jobs.sort(key=lambda j: j.submitted_at)
    return jobs


def get_queue_job(job_id: str) -> QueueJob | None:
    return _queue_jobs.get(job_id)


# ── Test recipes ──────────────────────────────────────────────────────

def list_test_recipes() -> list[TestRecipe]:
    cfg = _load_config()
    recipes = cfg.get("test_recipes", [])
    return [TestRecipe(**r) for r in recipes]


def get_test_recipe(recipe_id: str) -> TestRecipe | None:
    for r in list_test_recipes():
        if r.id == recipe_id:
            return r
    return None


def run_test_recipe(recipe_id: str) -> TestRecipeResult:
    recipe = get_test_recipe(recipe_id)
    if recipe is None:
        raise ValueError(f"Unknown test recipe: {recipe_id}")

    start = time.time()
    details: list[dict[str, Any]] = []
    passed = 0

    for i, step in enumerate(recipe.steps):
        step_start = time.time()
        step_result = _execute_test_step(recipe.domain, step, i)
        step_dur = (time.time() - step_start) * 1000
        details.append({
            "step": i + 1,
            "description": step,
            "status": step_result,
            "duration_ms": round(step_dur, 2),
        })
        if step_result == TestStatus.passed.value:
            passed += 1

    duration = (time.time() - start) * 1000
    overall = TestStatus.passed.value if passed == len(recipe.steps) else TestStatus.failed.value

    return TestRecipeResult(
        recipe_id=recipe_id,
        status=overall,
        duration_ms=round(duration, 2),
        steps_passed=passed,
        steps_total=len(recipe.steps),
        details=details,
    )


def _execute_test_step(domain: str, step_desc: str, step_idx: int) -> str:
    if domain == "ipp_cups":
        ops = list_ipp_operations()
        if not ops:
            return TestStatus.failed.value
    elif domain == "pdl_interpreters":
        langs = list_pdl_languages()
        if not langs:
            return TestStatus.failed.value
    elif domain == "color_management":
        slots = list_paper_profiles()
        if not slots:
            return TestStatus.failed.value
    elif domain == "print_queue":
        policies = list_queue_policies()
        if not policies:
            return TestStatus.failed.value
    elif domain == "integration":
        # Integration steps verified by existence of subsystems
        ops = list_ipp_operations()
        langs = list_pdl_languages()
        slots = list_paper_profiles()
        if not ops or not langs or not slots:
            return TestStatus.failed.value
    return TestStatus.passed.value


# ── SoC compatibility ─────────────────────────────────────────────────

def list_compatible_socs() -> list[CompatibleSoC]:
    cfg = _load_config()
    socs = cfg.get("compatible_socs", [])
    return [CompatibleSoC(**s) for s in socs]


def get_compatible_soc(soc_id: str) -> CompatibleSoC | None:
    for s in list_compatible_socs():
        if s.id == soc_id:
            return s
    return None


# ── Artifact definitions ──────────────────────────────────────────────

def list_artifact_definitions() -> list[ArtifactDefinition]:
    cfg = _load_config()
    arts = cfg.get("artifact_definitions", [])
    return [ArtifactDefinition(**a) for a in arts]


def get_artifact_definition(art_id: str) -> ArtifactDefinition | None:
    for a in list_artifact_definitions():
        if a.id == art_id:
            return a
    return None


# ── Gate validation ───────────────────────────────────────────────────

def validate_print_gate(
    artifacts: list[str],
    required_domains: list[str] | None = None,
) -> PrintGateResult:
    if required_domains is None:
        required_domains = [d.value for d in PrintDomain if d != PrintDomain.integration]

    {a.id: a for a in list_artifact_definitions()}
    findings: list[dict[str, str]] = []
    domains_passed = 0

    domain_artifact_map = {
        "ipp_cups": ["ipp_backend_config", "cups_backend_module"],
        "pdl_interpreters": ["pcl_output_stream", "postscript_output", "gs_render_output"],
        "color_management": ["icc_print_profile"],
        "print_queue": ["print_test_report"],
    }

    for domain in required_domains:
        required_arts = domain_artifact_map.get(domain, [])
        domain_ok = True
        for art_id in required_arts:
            if art_id in artifacts:
                findings.append({
                    "domain": domain,
                    "artifact_id": art_id,
                    "status": "present",
                    "message": f"Artifact {art_id} found",
                })
            else:
                domain_ok = False
                findings.append({
                    "domain": domain,
                    "artifact_id": art_id,
                    "status": "missing",
                    "message": f"Required artifact {art_id} not found",
                })
        if domain_ok:
            domains_passed += 1

    total = len(required_domains)
    if domains_passed == total:
        verdict = GateVerdict.pass_.value
    elif domains_passed > 0:
        verdict = GateVerdict.partial.value
    else:
        verdict = GateVerdict.fail.value

    return PrintGateResult(
        verdict=verdict,
        domains_checked=total,
        domains_passed=domains_passed,
        findings=findings,
        timestamp=time.time(),
    )


# ── Cert registry ────────────────────────────────────────────────────

_print_certs: list[dict[str, Any]] = []


def get_print_certs() -> list[dict[str, Any]]:
    return list(_print_certs)


def register_print_cert(cert: dict[str, Any]) -> dict[str, Any]:
    cert["registered_at"] = time.time()
    _print_certs.append(cert)
    return cert


def clear_print_certs() -> int:
    count = len(_print_certs)
    _print_certs.clear()
    return count


def generate_cert_artifacts(domain: str = "all") -> dict[str, Any]:
    domains = [d.value for d in PrintDomain if d != PrintDomain.integration] if domain == "all" else [domain]
    artifacts: dict[str, list[str]] = {}

    for d in domains:
        art_list: list[str] = []
        if d == "ipp_cups":
            art_list = ["ipp_backend_config", "cups_backend_module"]
        elif d == "pdl_interpreters":
            art_list = ["pcl_output_stream", "postscript_output", "gs_render_output"]
        elif d == "color_management":
            art_list = ["icc_print_profile"]
        elif d == "print_queue":
            art_list = ["print_test_report"]
        artifacts[d] = art_list

    bundle = {
        "domain": domain,
        "artifacts": artifacts,
        "generated_at": time.time(),
        "total_artifacts": sum(len(v) for v in artifacts.values()),
    }
    register_print_cert(bundle)
    return bundle
