"""L4-CORE-02 — Datasheet PDF → HardwareProfile parser (#212).

Extracts structured hardware specifications from datasheet PDFs and
maps them into ``HardwareProfile`` (from L4-CORE-01).

Pipeline:
  1. PDF → raw text  (pdfplumber, with table-aware extraction)
  2. Raw text → structured JSON via LLM prompt
  3. Per-field confidence scoring (≥0.7 auto-accept, else clarify)
  4. Fallback: returns partial profile + low-confidence fields list
     for operator form-fill

The module follows the same ask_fn pattern as ``intent_parser.py``:
callers inject their LLM call function, enabling deterministic tests
with a mock ask_fn.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from backend.hardware_profile import (
    SCHEMA_VERSION,
    HardwareProfile,
    MemoryMap,
    MemoryRegion,
    Peripheral,
)

logger = logging.getLogger(__name__)

AskFn = Callable[[str, str], Awaitable[tuple[str, int]]]

CONFIDENCE_THRESHOLD = 0.7

_MAX_PDF_CHARS = 120_000


@dataclass
class FieldExtraction:
    value: object
    confidence: float = 0.0

    @property
    def accepted(self) -> bool:
        return self.confidence >= CONFIDENCE_THRESHOLD


@dataclass
class DatasheetResult:
    profile: HardwareProfile
    field_confidences: dict[str, float] = field(default_factory=dict)
    low_confidence_fields: list[str] = field(default_factory=list)
    source_text_chars: int = 0
    llm_used: bool = False

    @property
    def needs_operator_review(self) -> bool:
        return len(self.low_confidence_fields) > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PDF text extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_text_from_pdf(pdf_path: str | Path) -> str:
    """Extract text from a PDF file using pdfplumber.

    Returns concatenated page text. Tables are extracted separately
    and appended as tab-separated rows for better structure.
    """
    import pdfplumber

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    pages_text: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            tables = page.extract_tables() or []
            table_text = ""
            for table in tables:
                for row in table:
                    if row:
                        cells = [str(c or "").strip() for c in row]
                        table_text += "\t".join(cells) + "\n"
            combined = text
            if table_text:
                combined += f"\n[TABLE on page {i + 1}]\n{table_text}"
            pages_text.append(combined)

    full = "\n\n".join(pages_text)
    if len(full) > _MAX_PDF_CHARS:
        full = full[:_MAX_PDF_CHARS]
    return full


def extract_text_from_string(raw_text: str) -> str:
    """Pass-through for pre-extracted text (testing / pipeline reuse)."""
    if len(raw_text) > _MAX_PDF_CHARS:
        return raw_text[:_MAX_PDF_CHARS]
    return raw_text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LLM extraction prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_EXTRACTION_PROMPT = """\
You are a hardware datasheet extraction engine. Given raw text from \
a semiconductor/module datasheet, extract the following fields into \
a JSON object. For each field, provide the extracted value and a \
confidence score (0.0–1.0).

Return ONLY a JSON object (no markdown, no prose) matching this schema:

{
  "soc":         { "value": "<SoC/main chip part number>", "confidence": 0.0..1.0 },
  "mcu":         { "value": "<MCU part number if separate from SoC>", "confidence": 0.0..1.0 },
  "dsp":         { "value": "<DSP core identifier>", "confidence": 0.0..1.0 },
  "npu":         { "value": "<Neural Processing Unit name/spec>", "confidence": 0.0..1.0 },
  "sensor":      { "value": ["<sensor1>", "<sensor2>"], "confidence": 0.0..1.0 },
  "codec":       { "value": ["<codec1>", "<codec2>"], "confidence": 0.0..1.0 },
  "usb":         { "value": ["<USB descriptor1>"], "confidence": 0.0..1.0 },
  "display":     { "value": "<display spec string>", "confidence": 0.0..1.0 },
  "memory_map":  {
    "value": {
      "regions": [
        { "name": "<region>", "base_address": "0x...", "size_bytes": 12345, "kind": "ram|rom|flash|sram|dram|ddr|mmio|other" }
      ],
      "total_ram_bytes": null,
      "total_flash_bytes": null
    },
    "confidence": 0.0..1.0
  },
  "peripherals": {
    "value": [
      { "name": "<peripheral>", "interface": "<bus>", "count": 1, "notes": "" }
    ],
    "confidence": 0.0..1.0
  }
}

Rules:
- Use "" or [] for fields not found in the datasheet.
- Confidence ≥ 0.85 when the value is stated verbatim in the text.
- Confidence 0.5–0.7 when you are interpreting or inferring.
- Confidence < 0.3 when the text is ambiguous or contradictory.
- For memory_map, only include regions explicitly mentioned with \
  addresses. If only total RAM/Flash sizes are mentioned, use the \
  totals fields and leave regions empty.
- SoC field: the primary chip name (e.g. "Hi3516DV300", "RK3566", \
  "ESP32-S3"). This is the most important field.
- sensor: image sensors, environmental sensors, etc.
- codec: H.264, H.265, AAC, etc.
- peripherals: I2C, SPI, UART, GPIO, ADC, etc. with counts."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Heuristic fallback (regex-based, no LLM)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SOC_PATTERNS = [
    re.compile(r"\b(Hi35\d{2}[A-Z]*\d*)\b", re.IGNORECASE),
    re.compile(r"\b(RK3\d{3}[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(ESP32[A-Z0-9-]*)\b", re.IGNORECASE),
    re.compile(r"\b(MT\d{4}[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(AM\d{4}[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(i\.MX\d+[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(STM32[A-Z]\d+[A-Z]*\d*)\b", re.IGNORECASE),
    re.compile(r"\b(nRF\d{4,5}[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(BCM\d{4,5}[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(Ambarella\s+[A-Z]\d+[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(Qualcomm\s+QCS\d+)\b", re.IGNORECASE),
]

_SENSOR_PATTERNS = [
    re.compile(r"\b(IMX\d{3}[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(OV\d{4,5}[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(AR\d{4}[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(GC\d{4}[A-Z]*)\b", re.IGNORECASE),
    re.compile(r"\b(SC\d{4}[A-Z]*)\b", re.IGNORECASE),
]

_CODEC_PATTERNS = [
    re.compile(r"\b(H\.264|AVC)\b", re.IGNORECASE),
    re.compile(r"\b(H\.265|HEVC)\b", re.IGNORECASE),
    re.compile(r"\b(H\.266|VVC)\b", re.IGNORECASE),
    re.compile(r"\b(MJPEG)\b", re.IGNORECASE),
    re.compile(r"\b(VP[89])\b", re.IGNORECASE),
    re.compile(r"\b(AV1)\b", re.IGNORECASE),
    re.compile(r"\b(AAC)\b", re.IGNORECASE),
    re.compile(r"\b(G\.711|G\.726)\b", re.IGNORECASE),
]

_USB_PATTERNS = [
    re.compile(r"\b(USB\s*3\.\d[\s\-]*(?:Host|Device|OTG)?)\b", re.IGNORECASE),
    re.compile(r"\b(USB\s*2\.\d[\s\-]*(?:Host|Device|OTG)?)\b", re.IGNORECASE),
    re.compile(r"\b(USB\s*Type-?C)\b", re.IGNORECASE),
]

_NPU_PATTERNS = [
    re.compile(r"\b(NNIE)\b"),
    re.compile(r"\b(NPU[:\s]*[\d.]+\s*T?OPS)\b", re.IGNORECASE),
    re.compile(r"\b(Neural\s+Network\s+(?:Processing\s+)?(?:Engine|Unit|Accelerator))\b", re.IGNORECASE),
    re.compile(r"\b(Xtensa\s+[A-Z0-9]+)\b", re.IGNORECASE),
    re.compile(r"\b(RKNN)\b", re.IGNORECASE),
]

_DSP_PATTERNS = [
    re.compile(r"\b(C[56]\dx)\b", re.IGNORECASE),
    re.compile(r"\b(HiFi\s*\d)\b", re.IGNORECASE),
    re.compile(r"\b(Tensilica)\b", re.IGNORECASE),
    re.compile(r"\b(CEVA[- ]?[A-Z0-9]+)\b", re.IGNORECASE),
    re.compile(r"\b(Cadence\s+DSP)\b", re.IGNORECASE),
]

_PERIPHERAL_PATTERNS = [
    (re.compile(r"\b(\d+)\s*[×x]\s*(?:I2C|I²C)\b", re.IGNORECASE), "I2C"),
    (re.compile(r"\b(?:I2C|I²C)\s*[×x:]\s*(\d+)\b", re.IGNORECASE), "I2C"),
    (re.compile(r"\b(\d+)\s*[×x]\s*SPI\b", re.IGNORECASE), "SPI"),
    (re.compile(r"\bSPI\s*[×x:]\s*(\d+)\b", re.IGNORECASE), "SPI"),
    (re.compile(r"\b(\d+)\s*[×x]\s*UART\b", re.IGNORECASE), "UART"),
    (re.compile(r"\bUART\s*[×x:]\s*(\d+)\b", re.IGNORECASE), "UART"),
    (re.compile(r"\b(\d+)\s*[×x]\s*GPIO\b", re.IGNORECASE), "GPIO"),
    (re.compile(r"\bGPIO\s*[×x:]\s*(\d+)\b", re.IGNORECASE), "GPIO"),
    (re.compile(r"\b(\d+)[- ]?bit\s*ADC\b", re.IGNORECASE), "ADC"),
    (re.compile(r"\bADC\b", re.IGNORECASE), "ADC"),
    (re.compile(r"\bPWM\b", re.IGNORECASE), "PWM"),
    (re.compile(r"\bSDIO\b", re.IGNORECASE), "SDIO"),
    (re.compile(r"\bEthernet\b", re.IGNORECASE), "Ethernet"),
]

_RAM_PATTERNS = [
    re.compile(r"\b(?:DDR\d?(?:L|X)?|DRAM|LPDDR\d(?:X)?)\s*[:=]?\s*(\d+)\s*(KB|MB|GB)\b", re.IGNORECASE),
    re.compile(r"\bTotal\s+RAM\s*[:=]?\s*(\d+)\s*(KB|MB|GB)\b", re.IGNORECASE),
    re.compile(r"\bRAM\s*[:=]?\s*(\d+)\s*(KB|MB|GB)\b", re.IGNORECASE),
    re.compile(r"\b(?:SRAM|memory)\s*[:=]?\s*(\d+)\s*(KB|MB|GB)\b", re.IGNORECASE),
]

_FLASH_PATTERNS = [
    re.compile(r"\b(?:SPI\s+Flash|NOR\s+Flash|NAND\s+Flash)\s*[:=]?\s*(?:up\s+to\s+)?(\d+)\s*(KB|MB|GB)\b", re.IGNORECASE),
    re.compile(r"\b(?:eMMC|Flash|ROM)\s*[\d.]*\s*[:=]?\s*(?:up\s+to\s+)?(\d+)\s*(KB|MB|GB)\b", re.IGNORECASE),
    re.compile(r"\bTotal\s+Flash\s*[:=]?\s*(\d+)\s*(KB|MB|GB)\b", re.IGNORECASE),
]

_DISPLAY_PATTERN = re.compile(
    r"\b(\d+(?:\.\d+)?[\s-]*inch[^.\n]{0,80})\b",
    re.IGNORECASE,
)


def _size_to_bytes(amount: int, unit: str) -> int:
    unit = unit.upper()
    if unit == "KB":
        return amount * 1024
    if unit == "MB":
        return amount * 1024 * 1024
    if unit == "GB":
        return amount * 1024 * 1024 * 1024
    return amount


def _find_all_unique(patterns: list[re.Pattern], text: str) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for pat in patterns:
        for m in pat.finditer(text):
            val = m.group(1).strip()
            key = val.lower().replace(" ", "")
            if key not in seen:
                seen.add(key)
                results.append(val)
    return results


def _heuristic_extract(text: str) -> DatasheetResult:
    """Regex-based extraction. Returns low confidence (0.5) for all
    fields since regex can't reason about context."""

    socs = _find_all_unique(_SOC_PATTERNS, text)
    soc = socs[0] if socs else ""

    sensors = _find_all_unique(_SENSOR_PATTERNS, text)
    codecs = _find_all_unique(_CODEC_PATTERNS, text)
    usb_list = _find_all_unique(_USB_PATTERNS, text)
    npus = _find_all_unique(_NPU_PATTERNS, text)
    dsps = _find_all_unique(_DSP_PATTERNS, text)

    npu = npus[0] if npus else ""
    dsp = dsps[0] if dsps else ""

    peripherals: list[Peripheral] = []
    seen_ifaces: set[str] = set()
    for pat, iface in _PERIPHERAL_PATTERNS:
        m = pat.search(text)
        if m and iface not in seen_ifaces:
            seen_ifaces.add(iface)
            try:
                count = int(m.group(1))
            except (ValueError, IndexError):
                count = 1
            peripherals.append(Peripheral(name=iface, interface=iface, count=count))

    total_ram: int | None = None
    for pat in _RAM_PATTERNS:
        for m in pat.finditer(text):
            val = _size_to_bytes(int(m.group(1)), m.group(2))
            if total_ram is None or val > total_ram:
                total_ram = val

    total_flash: int | None = None
    for pat in _FLASH_PATTERNS:
        for m in pat.finditer(text):
            val = _size_to_bytes(int(m.group(1)), m.group(2))
            if total_flash is None or val > total_flash:
                total_flash = val

    memory_map: MemoryMap | None = None
    if total_ram is not None or total_flash is not None:
        memory_map = MemoryMap(
            regions=[],
            total_ram_bytes=total_ram,
            total_flash_bytes=total_flash,
        )

    display = ""
    disp_match = _DISPLAY_PATTERN.search(text)
    if disp_match:
        display = disp_match.group(1).strip()

    base_conf = 0.5
    confidences: dict[str, float] = {
        "soc": base_conf if soc else 0.0,
        "mcu": 0.0,
        "dsp": base_conf if dsp else 0.0,
        "npu": base_conf if npu else 0.0,
        "sensor": base_conf if sensors else 0.0,
        "codec": base_conf if codecs else 0.0,
        "usb": base_conf if usb_list else 0.0,
        "display": base_conf if display else 0.0,
        "memory_map": base_conf if memory_map else 0.0,
        "peripherals": base_conf if peripherals else 0.0,
    }

    low_conf = [k for k, v in confidences.items() if v < CONFIDENCE_THRESHOLD]

    profile = HardwareProfile(
        soc=soc,
        dsp=dsp,
        npu=npu,
        sensor=sensors,
        codec=codecs,
        usb=usb_list,
        display=display,
        memory_map=memory_map,
        peripherals=peripherals,
    )

    return DatasheetResult(
        profile=profile,
        field_confidences=confidences,
        low_confidence_fields=low_conf,
        source_text_chars=len(text),
        llm_used=False,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LLM-backed extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _llm_extract(
    text: str,
    ask_fn: AskFn,
    model: str,
) -> DatasheetResult | None:
    """Use LLM to extract structured fields from datasheet text."""
    try:
        combined = f"{_EXTRACTION_PROMPT}\n\n---\n\nDATASHEET TEXT:\n{text}"
        raw, _tokens = await ask_fn(model, combined)
        if not raw:
            return None

        s = raw.strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
        data = json.loads(s)
        if not isinstance(data, dict):
            return None
    except Exception as exc:
        logger.debug("_llm_extract failed: %s", exc)
        return None

    def pick_str(name: str) -> FieldExtraction:
        entry = data.get(name)
        if not isinstance(entry, dict):
            return FieldExtraction("", 0.0)
        v = str(entry.get("value") or "").strip()
        try:
            c = float(entry.get("confidence") or 0.0)
            c = max(0.0, min(1.0, c))
        except (TypeError, ValueError):
            c = 0.0
        return FieldExtraction(v, c)

    def pick_list(name: str) -> FieldExtraction:
        entry = data.get(name)
        if not isinstance(entry, dict):
            return FieldExtraction([], 0.0)
        v = entry.get("value")
        if not isinstance(v, list):
            v = [str(v)] if v else []
        v = [str(x).strip() for x in v if x]
        try:
            c = float(entry.get("confidence") or 0.0)
            c = max(0.0, min(1.0, c))
        except (TypeError, ValueError):
            c = 0.0
        return FieldExtraction(v, c)

    def pick_memory_map() -> FieldExtraction:
        entry = data.get("memory_map")
        if not isinstance(entry, dict):
            return FieldExtraction(None, 0.0)
        try:
            c = float(entry.get("confidence") or 0.0)
            c = max(0.0, min(1.0, c))
        except (TypeError, ValueError):
            c = 0.0
        v = entry.get("value")
        if not isinstance(v, dict):
            return FieldExtraction(None, 0.0)
        try:
            regions_raw = v.get("regions") or []
            regions: list[MemoryRegion] = []
            for r in regions_raw:
                if isinstance(r, dict) and r.get("name") and r.get("base_address"):
                    regions.append(MemoryRegion(
                        name=r["name"],
                        base_address=r["base_address"],
                        size_bytes=int(r.get("size_bytes") or 0) or 1,
                        kind=r.get("kind", "ram"),
                    ))
            mm = MemoryMap(
                regions=regions,
                total_ram_bytes=v.get("total_ram_bytes"),
                total_flash_bytes=v.get("total_flash_bytes"),
            )
            return FieldExtraction(mm, c)
        except Exception:
            return FieldExtraction(None, 0.0)

    def pick_peripherals() -> FieldExtraction:
        entry = data.get("peripherals")
        if not isinstance(entry, dict):
            return FieldExtraction([], 0.0)
        try:
            c = float(entry.get("confidence") or 0.0)
            c = max(0.0, min(1.0, c))
        except (TypeError, ValueError):
            c = 0.0
        v = entry.get("value")
        if not isinstance(v, list):
            return FieldExtraction([], 0.0)
        peripherals: list[Peripheral] = []
        for p in v:
            if isinstance(p, dict) and p.get("name"):
                peripherals.append(Peripheral(
                    name=str(p["name"]),
                    interface=str(p.get("interface") or ""),
                    count=int(p.get("count") or 1),
                    notes=str(p.get("notes") or ""),
                ))
        return FieldExtraction(peripherals, c)

    soc = pick_str("soc")
    mcu = pick_str("mcu")
    dsp = pick_str("dsp")
    npu = pick_str("npu")
    sensor = pick_list("sensor")
    codec = pick_list("codec")
    usb = pick_list("usb")
    display = pick_str("display")
    mem = pick_memory_map()
    periph = pick_peripherals()

    confidences: dict[str, float] = {
        "soc": soc.confidence,
        "mcu": mcu.confidence,
        "dsp": dsp.confidence,
        "npu": npu.confidence,
        "sensor": sensor.confidence,
        "codec": codec.confidence,
        "usb": usb.confidence,
        "display": display.confidence,
        "memory_map": mem.confidence,
        "peripherals": periph.confidence,
    }
    low_conf = [k for k, v in confidences.items() if v < CONFIDENCE_THRESHOLD]

    profile = HardwareProfile(
        soc=str(soc.value),
        mcu=str(mcu.value),
        dsp=str(dsp.value),
        npu=str(npu.value),
        sensor=sensor.value,
        codec=codec.value,
        usb=usb.value,
        display=str(display.value),
        memory_map=mem.value,
        peripherals=periph.value,
    )

    return DatasheetResult(
        profile=profile,
        field_confidences=confidences,
        low_confidence_fields=low_conf,
        source_text_chars=len(text),
        llm_used=True,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def parse_datasheet(
    source: str | Path,
    *,
    ask_fn: AskFn | None = None,
    model: str = "",
    raw_text: str | None = None,
) -> DatasheetResult:
    """Parse a datasheet into a HardwareProfile.

    Parameters
    ----------
    source : str | Path
        Path to the PDF file, or ignored if ``raw_text`` is provided.
    ask_fn : callable, optional
        LLM ask function ``async (model, prompt) -> (response, tokens)``.
        If None, falls back to heuristic extraction only.
    model : str
        Model identifier passed to ask_fn.
    raw_text : str, optional
        Pre-extracted text (skips PDF extraction). Useful for tests
        and pipelines that already have the text.

    Returns
    -------
    DatasheetResult
        Contains the HardwareProfile, per-field confidences, and the
        list of fields below the 0.7 threshold.
    """
    if raw_text is not None:
        text = extract_text_from_string(raw_text)
    else:
        text = extract_text_from_pdf(source)

    if not text.strip():
        return DatasheetResult(
            profile=HardwareProfile(),
            field_confidences={},
            low_confidence_fields=list(_ALL_FIELDS),
            source_text_chars=0,
        )

    if ask_fn is not None:
        result = await _llm_extract(text, ask_fn, model)
        if result is not None:
            return result
        logger.info("LLM extraction failed, falling back to heuristic")

    return _heuristic_extract(text)


_ALL_FIELDS = (
    "soc", "mcu", "dsp", "npu", "sensor", "codec",
    "usb", "display", "memory_map", "peripherals",
)


def apply_operator_overrides(
    result: DatasheetResult,
    overrides: dict[str, object],
) -> DatasheetResult:
    """Merge operator form-filled values into a DatasheetResult.

    For each key in ``overrides``, sets the corresponding field on the
    profile and raises its confidence to 1.0 (operator-verified).
    """
    data = result.profile.model_dump()

    for key, value in overrides.items():
        if key not in _ALL_FIELDS:
            continue
        if key == "memory_map" and isinstance(value, dict):
            data[key] = value
        elif key == "peripherals" and isinstance(value, list):
            data[key] = value
        else:
            data[key] = value
        result.field_confidences[key] = 1.0

    result.low_confidence_fields = [
        k for k in result.low_confidence_fields
        if k not in overrides
    ]

    result.profile = HardwareProfile(**data)
    return result
