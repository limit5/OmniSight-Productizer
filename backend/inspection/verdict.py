"""Shared inspection verdict contract for C8 production-line vision."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


VerdictValue = Literal["pass", "fail"]


class Defect(BaseModel):
    """One detected defect in an inspection verdict."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    class_: str = Field(..., alias="class", min_length=1)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    loc: list[float] | None = Field(default=None, min_length=2, max_length=2)
    score: float = Field(..., ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _has_location(self) -> "Defect":
        if self.bbox is None and self.loc is None:
            raise ValueError("defect must include bbox or loc")
        return self


class InspectionVerdict(BaseModel):
    """Canonical verdict consumed by inference and line-logic components."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    part_id: str = Field(..., min_length=1)
    station_id: str = Field(..., min_length=1)
    verdict: VerdictValue
    defects: list[Defect] = Field(default_factory=list)
    model_version: str = Field(..., min_length=1)
    recipe_version: str = Field(..., min_length=1)
    ts_capture: datetime
    ts_verdict: datetime

    @model_validator(mode="after")
    def _pass_verdict_has_no_defects(self) -> "InspectionVerdict":
        if self.verdict == "pass" and self.defects:
            raise ValueError("pass verdict must not include defects")
        return self

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-facing payload with stable field aliases."""

        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InspectionVerdict":
        """Validate a JSON-facing payload into an inspection verdict."""

        return cls.model_validate(payload)


def verdict_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for an inspection verdict payload."""

    return InspectionVerdict.model_json_schema()
