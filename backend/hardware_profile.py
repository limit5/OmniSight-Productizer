"""L4-CORE-01 — HardwareProfile schema (#211).

Pydantic model describing the hardware capabilities of a target board.
Used by the embedded product planner (CORE-04) and datasheet parser
(CORE-02) to generate DAGs that match the actual silicon.

JSON schema is exported via ``HardwareProfile.model_json_schema()``.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 1


class MemoryRegion(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    base_address: str = Field(..., pattern=r"^0x[0-9a-fA-F]+$")
    size_bytes: int = Field(..., gt=0)
    kind: str = Field("ram", pattern=r"^(ram|rom|flash|sram|dram|ddr|mmio|other)$")


class MemoryMap(BaseModel):
    regions: list[MemoryRegion] = Field(default_factory=list)
    total_ram_bytes: Optional[int] = Field(None, ge=0)
    total_flash_bytes: Optional[int] = Field(None, ge=0)


class Peripheral(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    interface: str = Field("", max_length=64)
    count: int = Field(1, ge=1)
    notes: str = Field("", max_length=512)


class HardwareProfile(BaseModel):
    schema_version: int = SCHEMA_VERSION
    soc: str = Field("", max_length=128, description="System-on-Chip part number")
    mcu: str = Field("", max_length=128, description="Microcontroller part number")
    dsp: str = Field("", max_length=128, description="DSP core identifier")
    npu: str = Field("", max_length=128, description="Neural processing unit")
    sensor: list[str] = Field(default_factory=list, description="Image/environmental sensors")
    codec: list[str] = Field(default_factory=list, description="Audio/video codec capabilities")
    usb: list[str] = Field(default_factory=list, description="USB interface descriptors")
    display: str = Field("", max_length=256, description="Display panel specification")
    memory_map: Optional[MemoryMap] = None
    peripherals: list[Peripheral] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {v}, expected {SCHEMA_VERSION}")
        return v

    def summary(self) -> str:
        parts: list[str] = []
        if self.soc:
            parts.append(f"SoC={self.soc}")
        if self.mcu:
            parts.append(f"MCU={self.mcu}")
        if self.npu:
            parts.append(f"NPU={self.npu}")
        if self.sensor:
            parts.append(f"sensors={','.join(self.sensor)}")
        return " | ".join(parts) if parts else "(empty profile)"
