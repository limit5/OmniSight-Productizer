"""Tests for the C8 shared inspection verdict contract."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from backend.inspection.verdict import Defect, InspectionVerdict, verdict_json_schema


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "inspection" / "verdict.schema.json"


def _failed_payload() -> dict[str, object]:
    return {
        "part_id": "part-00042",
        "station_id": "station-aoi-1",
        "verdict": "fail",
        "defects": [
            {
                "class": "scratch",
                "bbox": [12.5, 17.0, 24.25, 31.5],
                "score": 0.97,
            },
            {
                "class": "dent",
                "loc": [44.0, 52.5],
                "score": 0.86,
            },
        ],
        "model_version": "rknn-scratch-detector@2026.06.12",
        "recipe_version": "recipe-aluminum-v3",
        "ts_capture": "2026-06-12T08:15:30.123456Z",
        "ts_verdict": "2026-06-12T08:15:30.223456Z",
    }


def _loaded_schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_json_schema_matches_pydantic_contract() -> None:
    assert _loaded_schema() == verdict_json_schema()


def test_round_trip_preserves_canonical_payload() -> None:
    payload = _failed_payload()

    verdict = InspectionVerdict.from_payload(payload)
    round_tripped = InspectionVerdict.from_payload(verdict.to_payload())

    assert round_tripped == verdict
    assert round_tripped.to_payload() == payload


def test_schema_validates_verdict_payload() -> None:
    jsonschema.validate(instance=_failed_payload(), schema=_loaded_schema())


def test_forward_compatible_unknown_fields_round_trip() -> None:
    payload = _failed_payload()
    payload["camera_id"] = "cam-left-1"
    defects = payload["defects"]
    assert isinstance(defects, list)
    defect = defects[0]
    assert isinstance(defect, dict)
    defect["segment_id"] = "roi-upper-left"

    verdict = InspectionVerdict.from_payload(payload)

    assert verdict.to_payload()["camera_id"] == "cam-left-1"
    assert verdict.to_payload()["defects"][0]["segment_id"] == "roi-upper-left"
    jsonschema.validate(instance=verdict.to_payload(), schema=_loaded_schema())


def test_pass_verdict_rejects_defects() -> None:
    payload = _failed_payload()
    payload["verdict"] = "pass"

    with pytest.raises(ValueError, match="pass verdict must not include defects"):
        InspectionVerdict.from_payload(payload)


def test_defect_requires_bbox_or_loc() -> None:
    with pytest.raises(ValueError, match="defect must include bbox or loc"):
        Defect.model_validate({"class": "scratch", "score": 0.9})
