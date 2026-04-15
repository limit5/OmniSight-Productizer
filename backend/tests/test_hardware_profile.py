"""Tests for L4-CORE-01 HardwareProfile schema (#211)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.hardware_profile import (
    SCHEMA_VERSION,
    HardwareProfile,
    MemoryMap,
    MemoryRegion,
    Peripheral,
)
from backend.intent_parser import ParsedSpec


# ── fixtures ──────────────────────────────────────────────────────

FULL_PROFILE_DICT: dict = {
    "schema_version": SCHEMA_VERSION,
    "soc": "Hi3516DV300",
    "mcu": "STM32F407",
    "dsp": "C66x",
    "npu": "NNIE",
    "sensor": ["IMX307", "OV2718"],
    "codec": ["H.264", "H.265"],
    "usb": ["USB2.0-OTG", "USB3.0-Host"],
    "display": "7-inch 1024x600 LVDS",
    "memory_map": {
        "regions": [
            {"name": "DDR", "base_address": "0x80000000", "size_bytes": 536870912, "kind": "ddr"},
            {"name": "SRAM", "base_address": "0x04010000", "size_bytes": 65536, "kind": "sram"},
        ],
        "total_ram_bytes": 536870912,
        "total_flash_bytes": 16777216,
    },
    "peripherals": [
        {"name": "SPI NOR Flash", "interface": "SPI", "count": 1, "notes": "W25Q128"},
        {"name": "I2C EEPROM", "interface": "I2C", "count": 2, "notes": "AT24C256"},
    ],
}


MINIMAL_PROFILE_DICT: dict = {}


# ── round-trip tests ──────────────────────────────────────────────


def test_full_profile_roundtrip():
    hp = HardwareProfile(**FULL_PROFILE_DICT)
    dumped = hp.model_dump()
    restored = HardwareProfile(**dumped)
    assert restored == hp


def test_full_profile_json_roundtrip():
    hp = HardwareProfile(**FULL_PROFILE_DICT)
    json_str = hp.model_dump_json()
    restored = HardwareProfile.model_validate_json(json_str)
    assert restored == hp


def test_minimal_profile_roundtrip():
    hp = HardwareProfile(**MINIMAL_PROFILE_DICT)
    dumped = hp.model_dump()
    restored = HardwareProfile(**dumped)
    assert restored == hp
    assert hp.soc == ""
    assert hp.memory_map is None
    assert hp.peripherals == []


def test_json_schema_generated():
    schema = HardwareProfile.model_json_schema()
    assert schema["type"] == "object"
    assert "soc" in schema["properties"]
    assert "memory_map" in schema["properties"]
    assert "$defs" in schema
    assert "MemoryRegion" in schema["$defs"]


# ── validation tests ──────────────────────────────────────────────


def test_bad_schema_version_rejected():
    with pytest.raises(ValidationError, match="unsupported schema_version"):
        HardwareProfile(schema_version=99)


def test_memory_region_invalid_address():
    with pytest.raises(ValidationError):
        MemoryRegion(name="bad", base_address="not_hex", size_bytes=1024)


def test_memory_region_zero_size():
    with pytest.raises(ValidationError):
        MemoryRegion(name="bad", base_address="0x00", size_bytes=0)


def test_peripheral_requires_name():
    with pytest.raises(ValidationError):
        Peripheral(name="")


def test_memory_region_valid_kinds():
    for kind in ("ram", "rom", "flash", "sram", "dram", "ddr", "mmio", "other"):
        mr = MemoryRegion(name="test", base_address="0x1000", size_bytes=4096, kind=kind)
        assert mr.kind == kind


def test_memory_region_invalid_kind():
    with pytest.raises(ValidationError):
        MemoryRegion(name="test", base_address="0x1000", size_bytes=4096, kind="bogus")


# ── summary ───────────────────────────────────────────────────────


def test_summary_full():
    hp = HardwareProfile(**FULL_PROFILE_DICT)
    s = hp.summary()
    assert "Hi3516DV300" in s
    assert "NNIE" in s
    assert "IMX307" in s


def test_summary_empty():
    hp = HardwareProfile()
    assert hp.summary() == "(empty profile)"


# ── ParsedSpec integration ────────────────────────────────────────


def test_parsed_spec_with_hardware_profile_to_dict():
    hp = HardwareProfile(soc="RK3566", sensor=["OV5647"])
    ps = ParsedSpec(hardware_profile=hp)
    d = ps.to_dict()
    assert d["hardware_profile"] is not None
    assert d["hardware_profile"]["soc"] == "RK3566"
    assert d["hardware_profile"]["sensor"] == ["OV5647"]


def test_parsed_spec_without_hardware_profile():
    ps = ParsedSpec()
    d = ps.to_dict()
    assert d["hardware_profile"] is None


def test_parsed_spec_hardware_profile_json_roundtrip():
    hp = HardwareProfile(**FULL_PROFILE_DICT)
    ps = ParsedSpec(hardware_profile=hp)
    d = ps.to_dict()
    json_str = json.dumps(d)
    loaded = json.loads(json_str)
    restored_hp = HardwareProfile(**loaded["hardware_profile"])
    assert restored_hp == hp
