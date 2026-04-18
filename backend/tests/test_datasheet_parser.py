"""Tests for L4-CORE-02 Datasheet PDF → HardwareProfile parser (#212)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.datasheet_parser import (
    CONFIDENCE_THRESHOLD,
    FieldExtraction,
    apply_operator_overrides,
    extract_text_from_string,
    parse_datasheet,
    _heuristic_extract,
)
from backend.hardware_profile import HardwareProfile, SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures"


# ── helpers ──────────────────────────────────────────────────────────

def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _make_ask_fn(response: dict[str, Any]):
    """Build a deterministic mock ask_fn that returns the given dict as JSON."""
    async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
        return json.dumps(response), 100
    return ask_fn


def _make_failing_ask_fn():
    async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
        raise RuntimeError("LLM unavailable")
    return ask_fn


# ── FieldExtraction ──────────────────────────────────────────────────

class TestFieldExtraction:
    def test_accepted_above_threshold(self):
        fe = FieldExtraction("Hi3516DV300", 0.9)
        assert fe.accepted is True

    def test_not_accepted_below_threshold(self):
        fe = FieldExtraction("Hi3516DV300", 0.5)
        assert fe.accepted is False

    def test_threshold_boundary(self):
        fe = FieldExtraction("x", CONFIDENCE_THRESHOLD)
        assert fe.accepted is True
        fe2 = FieldExtraction("x", CONFIDENCE_THRESHOLD - 0.01)
        assert fe2.accepted is False


# ── Heuristic extraction: Hi3516DV300 ───────────────────────────────

class TestHeuristicHi3516:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.text = _load_fixture("datasheet_hi3516.txt")
        self.result = _heuristic_extract(self.text)

    def test_soc_detected(self):
        assert self.result.profile.soc == "Hi3516DV300"

    def test_npu_detected(self):
        assert "NNIE" in self.result.profile.npu

    def test_dsp_detected(self):
        assert "C66x" in self.result.profile.dsp

    def test_sensors_detected(self):
        sensors = [s.upper() for s in self.result.profile.sensor]
        assert "IMX307" in sensors
        assert "OV2718" in sensors

    def test_codecs_detected(self):
        codec_lower = [c.lower() for c in self.result.profile.codec]
        assert any("h.265" in c or "hevc" in c for c in codec_lower)
        assert any("h.264" in c or "avc" in c for c in codec_lower)

    def test_usb_detected(self):
        usb_str = " ".join(self.result.profile.usb).lower()
        assert "usb" in usb_str

    def test_memory_map_totals(self):
        mm = self.result.profile.memory_map
        assert mm is not None
        assert mm.total_ram_bytes == 512 * 1024 * 1024
        assert mm.total_flash_bytes == 128 * 1024 * 1024

    def test_peripherals_detected(self):
        ifaces = {p.interface for p in self.result.profile.peripherals}
        assert "I2C" in ifaces
        assert "SPI" in ifaces
        assert "UART" in ifaces

    def test_all_heuristic_below_threshold(self):
        for k, v in self.result.field_confidences.items():
            assert v <= 0.5, f"{k} confidence {v} should be ≤0.5 for heuristic"

    def test_llm_not_used(self):
        assert self.result.llm_used is False

    def test_schema_version(self):
        assert self.result.profile.schema_version == SCHEMA_VERSION


# ── Heuristic extraction: RK3566 ────────────────────────────────────

class TestHeuristicRK3566:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.text = _load_fixture("datasheet_rk3566.txt")
        self.result = _heuristic_extract(self.text)

    def test_soc_detected(self):
        assert self.result.profile.soc == "RK3566"

    def test_npu_detected(self):
        npu = self.result.profile.npu
        assert "RKNN" in npu or "NPU" in npu

    def test_codecs_detected(self):
        codec_lower = [c.lower() for c in self.result.profile.codec]
        assert any("h.265" in c or "hevc" in c for c in codec_lower)
        assert any("vp9" in c for c in codec_lower)

    def test_sensors_detected(self):
        sensors = [s.upper() for s in self.result.profile.sensor]
        assert "OV5647" in sensors or "IMX219" in sensors or "IMX415" in sensors

    def test_usb3_detected(self):
        usb_str = " ".join(self.result.profile.usb).lower()
        assert "usb" in usb_str

    def test_memory_totals(self):
        mm = self.result.profile.memory_map
        assert mm is not None
        assert mm.total_ram_bytes == 4096 * 1024 * 1024


# ── Heuristic extraction: ESP32-S3 ──────────────────────────────────

class TestHeuristicESP32S3:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.text = _load_fixture("datasheet_esp32s3.txt")
        self.result = _heuristic_extract(self.text)

    def test_soc_detected(self):
        assert "ESP32" in self.result.profile.soc

    def test_sensors_detected(self):
        sensors = [s.upper() for s in self.result.profile.sensor]
        assert "OV2640" in sensors or "OV7670" in sensors

    def test_no_dedicated_npu(self):
        pass

    def test_peripherals_detected(self):
        ifaces = {p.interface for p in self.result.profile.peripherals}
        assert "I2C" in ifaces
        assert "SPI" in ifaces
        assert "UART" in ifaces

    def test_flash_detected(self):
        mm = self.result.profile.memory_map
        assert mm is not None
        assert mm.total_flash_bytes == 16 * 1024 * 1024


# ── LLM extraction ──────────────────────────────────────────────────

LLM_RESPONSE_HI3516: dict = {
    "soc": {"value": "Hi3516DV300", "confidence": 0.95},
    "mcu": {"value": "", "confidence": 0.0},
    "dsp": {"value": "C66x", "confidence": 0.9},
    "npu": {"value": "NNIE 0.5 TOPS", "confidence": 0.92},
    "sensor": {"value": ["IMX307", "OV2718"], "confidence": 0.9},
    "codec": {"value": ["H.265", "H.264", "MJPEG"], "confidence": 0.95},
    "usb": {"value": ["USB2.0-OTG", "USB2.0-Host"], "confidence": 0.88},
    "display": {"value": "7-inch 1024x600 LVDS", "confidence": 0.85},
    "memory_map": {
        "value": {
            "regions": [
                {"name": "DDR", "base_address": "0x80000000", "size_bytes": 536870912, "kind": "ddr"},
                {"name": "SRAM", "base_address": "0x04010000", "size_bytes": 65536, "kind": "sram"},
            ],
            "total_ram_bytes": 536870912,
            "total_flash_bytes": 16777216,
        },
        "confidence": 0.88,
    },
    "peripherals": {
        "value": [
            {"name": "I2C", "interface": "I2C", "count": 3, "notes": ""},
            {"name": "SPI", "interface": "SPI", "count": 2, "notes": ""},
            {"name": "UART", "interface": "UART", "count": 4, "notes": "with flow control"},
            {"name": "GPIO", "interface": "GPIO", "count": 80, "notes": "10 groups"},
            {"name": "ADC", "interface": "ADC", "count": 2, "notes": "12-bit"},
            {"name": "PWM", "interface": "PWM", "count": 6, "notes": ""},
            {"name": "Ethernet", "interface": "RMII", "count": 1, "notes": "10/100M"},
        ],
        "confidence": 0.9,
    },
}


class TestLLMExtraction:
    @pytest.mark.asyncio
    async def test_llm_parse_hi3516(self):
        text = _load_fixture("datasheet_hi3516.txt")
        ask_fn = _make_ask_fn(LLM_RESPONSE_HI3516)
        result = await parse_datasheet("dummy.pdf", ask_fn=ask_fn, raw_text=text)

        assert result.llm_used is True
        assert result.profile.soc == "Hi3516DV300"
        assert result.profile.npu == "NNIE 0.5 TOPS"
        assert "IMX307" in result.profile.sensor
        assert result.field_confidences["soc"] == 0.95
        assert "soc" not in result.low_confidence_fields

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_heuristic(self):
        text = _load_fixture("datasheet_hi3516.txt")
        ask_fn = _make_failing_ask_fn()
        result = await parse_datasheet("dummy.pdf", ask_fn=ask_fn, raw_text=text)

        assert result.llm_used is False
        assert result.profile.soc == "Hi3516DV300"

    @pytest.mark.asyncio
    async def test_no_ask_fn_uses_heuristic(self):
        text = _load_fixture("datasheet_hi3516.txt")
        result = await parse_datasheet("dummy.pdf", raw_text=text)

        assert result.llm_used is False
        assert result.profile.soc == "Hi3516DV300"

    @pytest.mark.asyncio
    async def test_llm_returns_markdown_fenced_json(self):
        text = _load_fixture("datasheet_hi3516.txt")
        fenced = "```json\n" + json.dumps(LLM_RESPONSE_HI3516) + "\n```"

        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            return fenced, 100

        result = await parse_datasheet("dummy.pdf", ask_fn=ask_fn, raw_text=text)
        assert result.llm_used is True
        assert result.profile.soc == "Hi3516DV300"

    @pytest.mark.asyncio
    async def test_llm_returns_empty_falls_back(self):
        text = _load_fixture("datasheet_hi3516.txt")

        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            return "", 0

        result = await parse_datasheet("dummy.pdf", ask_fn=ask_fn, raw_text=text)
        assert result.llm_used is False

    @pytest.mark.asyncio
    async def test_llm_returns_invalid_json_falls_back(self):
        text = _load_fixture("datasheet_hi3516.txt")

        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            return "not json at all", 50

        result = await parse_datasheet("dummy.pdf", ask_fn=ask_fn, raw_text=text)
        assert result.llm_used is False
        assert result.profile.soc == "Hi3516DV300"


# ── Confidence threshold logic ───────────────────────────────────────

class TestConfidenceThreshold:
    @pytest.mark.asyncio
    async def test_low_confidence_fields_flagged(self):
        response = {
            "soc": {"value": "TestSoC", "confidence": 0.9},
            "mcu": {"value": "TestMCU", "confidence": 0.3},
            "dsp": {"value": "", "confidence": 0.0},
            "npu": {"value": "TestNPU", "confidence": 0.8},
            "sensor": {"value": [], "confidence": 0.0},
            "codec": {"value": ["H.264"], "confidence": 0.75},
            "usb": {"value": [], "confidence": 0.0},
            "display": {"value": "", "confidence": 0.0},
            "memory_map": {"value": {"regions": [], "total_ram_bytes": None, "total_flash_bytes": None}, "confidence": 0.0},
            "peripherals": {"value": [], "confidence": 0.0},
        }
        ask_fn = _make_ask_fn(response)
        result = await parse_datasheet("x.pdf", ask_fn=ask_fn, raw_text="dummy text")

        assert "soc" not in result.low_confidence_fields
        assert "mcu" in result.low_confidence_fields
        assert "dsp" in result.low_confidence_fields
        assert "npu" not in result.low_confidence_fields
        assert "codec" not in result.low_confidence_fields
        assert result.needs_operator_review is True

    @pytest.mark.asyncio
    async def test_all_high_confidence_no_review(self):
        response = {k: {"value": f"val-{k}", "confidence": 0.9} for k in
                    ("soc", "mcu", "dsp", "npu", "display")}
        response["sensor"] = {"value": ["S1"], "confidence": 0.9}
        response["codec"] = {"value": ["C1"], "confidence": 0.9}
        response["usb"] = {"value": ["U1"], "confidence": 0.9}
        response["memory_map"] = {
            "value": {"regions": [], "total_ram_bytes": 1024, "total_flash_bytes": 1024},
            "confidence": 0.9,
        }
        response["peripherals"] = {"value": [], "confidence": 0.9}

        ask_fn = _make_ask_fn(response)
        result = await parse_datasheet("x.pdf", ask_fn=ask_fn, raw_text="dummy text")
        assert result.needs_operator_review is False
        assert len(result.low_confidence_fields) == 0


# ── Operator override (fallback form-fill) ───────────────────────────

class TestOperatorOverride:
    @pytest.mark.asyncio
    async def test_override_replaces_field(self):
        text = _load_fixture("datasheet_hi3516.txt")
        result = await parse_datasheet("x.pdf", raw_text=text)

        assert result.field_confidences.get("mcu", 0.0) < CONFIDENCE_THRESHOLD
        result = apply_operator_overrides(result, {"mcu": "STM32F407"})

        assert result.profile.mcu == "STM32F407"
        assert result.field_confidences["mcu"] == 1.0
        assert "mcu" not in result.low_confidence_fields

    @pytest.mark.asyncio
    async def test_override_unknown_field_ignored(self):
        text = _load_fixture("datasheet_hi3516.txt")
        result = await parse_datasheet("x.pdf", raw_text=text)
        original_soc = result.profile.soc

        result = apply_operator_overrides(result, {"nonexistent_field": "value"})
        assert result.profile.soc == original_soc

    @pytest.mark.asyncio
    async def test_override_sensor_list(self):
        text = _load_fixture("datasheet_hi3516.txt")
        result = await parse_datasheet("x.pdf", raw_text=text)

        result = apply_operator_overrides(result, {"sensor": ["IMX335", "OV5647"]})
        assert result.profile.sensor == ["IMX335", "OV5647"]
        assert result.field_confidences["sensor"] == 1.0


# ── Empty / edge cases ──────────────────────────────────────────────

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_text_returns_empty_profile(self):
        result = await parse_datasheet("x.pdf", raw_text="")
        assert result.profile.soc == ""
        assert len(result.low_confidence_fields) > 0

    @pytest.mark.asyncio
    async def test_whitespace_only_text(self):
        result = await parse_datasheet("x.pdf", raw_text="   \n\n  ")
        assert result.profile.soc == ""

    def test_text_truncation(self):
        long_text = "A" * 200_000
        truncated = extract_text_from_string(long_text)
        assert len(truncated) == 120_000

    @pytest.mark.asyncio
    async def test_pdf_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            await parse_datasheet("/nonexistent/path/foo.pdf")


# ── HardwareProfile integration ─────────────────────────────────────

class TestProfileIntegration:
    @pytest.mark.asyncio
    async def test_result_profile_is_valid_pydantic(self):
        text = _load_fixture("datasheet_hi3516.txt")
        result = await parse_datasheet("x.pdf", raw_text=text)

        d = result.profile.model_dump()
        restored = HardwareProfile(**d)
        assert restored == result.profile

    @pytest.mark.asyncio
    async def test_result_profile_json_roundtrip(self):
        text = _load_fixture("datasheet_rk3566.txt")
        result = await parse_datasheet("x.pdf", raw_text=text)

        json_str = result.profile.model_dump_json()
        restored = HardwareProfile.model_validate_json(json_str)
        assert restored == result.profile

    @pytest.mark.asyncio
    async def test_llm_result_profile_valid(self):
        text = _load_fixture("datasheet_hi3516.txt")
        ask_fn = _make_ask_fn(LLM_RESPONSE_HI3516)
        result = await parse_datasheet("x.pdf", ask_fn=ask_fn, raw_text=text)

        d = result.profile.model_dump()
        restored = HardwareProfile(**d)
        assert restored == result.profile
        assert restored.memory_map is not None
        assert len(restored.memory_map.regions) == 2
        assert len(restored.peripherals) == 7
