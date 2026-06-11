"""Tests for the C8 golden-sample diff scorer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.inspection.golden import (
    GoldenSample,
    GoldenSampleRegistry,
    PixelFeatureDiffScorer,
)
from backend.inspection.verdict import InspectionVerdict


NOW = datetime(2026, 6, 12, 8, 15, 30, tzinfo=timezone.utc)


def _sample() -> GoldenSample:
    return GoldenSample(
        recipe_id="recipe-aluminum",
        recipe_version="recipe-aluminum-v3",
        sample_id="golden-front",
        part_id="golden-part",
        station_id="station-aoi-1",
        model_version="rknn-scratch-detector@2026.06.12",
        reference_image=[
            [0.10, 0.20],
            [0.30, 0.40],
        ],
        features={"edge_density": 0.50, "hole_roundness": 0.90},
        metadata={"camera_id": "cam-left-1"},
    )


def test_registry_stores_versioned_golden_samples() -> None:
    sample = _sample()
    newer = sample.model_copy(
        update={"recipe_version": "recipe-aluminum-v4", "sample_id": "golden-front"}
    )
    registry = GoldenSampleRegistry([sample, newer])

    assert registry.get("recipe-aluminum", "recipe-aluminum-v3", "golden-front") == sample
    assert registry.get("recipe-aluminum", "recipe-aluminum-v4", "golden-front") == newer
    assert registry.list_versions("recipe-aluminum") == [
        "recipe-aluminum-v3",
        "recipe-aluminum-v4",
    ]


def test_registry_rejects_duplicate_recipe_version_sample() -> None:
    registry = GoldenSampleRegistry([_sample()])

    with pytest.raises(ValueError, match="golden sample already registered"):
        registry.register(_sample())


def test_sample_requires_reference_image_or_features() -> None:
    payload = _sample().model_dump()
    payload["reference_image"] = None
    payload["features"] = {}

    with pytest.raises(ValueError, match="golden sample must include"):
        GoldenSample.model_validate(payload)


def test_pixel_feature_diff_scorer_returns_pass_verdict_contract() -> None:
    scorer = PixelFeatureDiffScorer(clock=lambda: NOW)

    verdict = scorer.score(
        _sample(),
        part_id="part-00042",
        station_id="station-aoi-1",
        image=[
            [0.10, 0.21],
            [0.29, 0.40],
        ],
        features={"edge_density": 0.51, "hole_roundness": 0.91},
        ts_capture=NOW,
    )

    assert isinstance(verdict, InspectionVerdict)
    assert verdict.to_payload() == {
        "part_id": "part-00042",
        "station_id": "station-aoi-1",
        "verdict": "pass",
        "defects": [],
        "model_version": "pixel-feature-diff@host",
        "recipe_version": "recipe-aluminum-v3",
        "ts_capture": "2026-06-12T08:15:30Z",
        "ts_verdict": "2026-06-12T08:15:30Z",
        "golden_recipe_id": "recipe-aluminum",
        "golden_sample_id": "golden-front",
        "golden_model_version": "rknn-scratch-detector@2026.06.12",
    }


def test_pixel_feature_diff_scorer_reports_defects_as_verdict_contract() -> None:
    scorer = PixelFeatureDiffScorer(
        pixel_threshold=0.04,
        feature_threshold=0.10,
        clock=lambda: NOW,
    )

    verdict = scorer.score(
        _sample(),
        part_id="part-00043",
        station_id="station-aoi-1",
        image=[
            [0.30, 0.40],
            [0.50, 0.60],
        ],
        features={"edge_density": 0.80, "hole_roundness": 0.90},
        ts_capture=NOW,
    )
    payload = verdict.to_payload()

    assert payload["verdict"] == "fail"
    assert [defect["class"] for defect in payload["defects"]] == [
        "pixel_diff",
        "feature_diff",
    ]
    assert payload["defects"][0]["raw_score"] == pytest.approx(0.20)
    assert payload["defects"][0]["threshold"] == 0.04
    assert payload["defects"][1]["feature"] == "edge_density"
    assert payload["defects"][1]["expected"] == 0.50
    assert payload["defects"][1]["observed"] == 0.80


def test_pixel_feature_diff_scorer_validates_required_observation_inputs() -> None:
    scorer = PixelFeatureDiffScorer(clock=lambda: NOW)

    with pytest.raises(ValueError, match="image is required"):
        scorer.score(
            _sample(),
            part_id="part-00044",
            station_id="station-aoi-1",
            features={"edge_density": 0.50, "hole_roundness": 0.90},
        )

    with pytest.raises(ValueError, match="missing observed features: hole_roundness"):
        scorer.score(
            _sample(),
            part_id="part-00044",
            station_id="station-aoi-1",
            image=[
                [0.10, 0.20],
                [0.30, 0.40],
            ],
            features={"edge_density": 0.50},
        )


def test_pixel_feature_diff_scorer_rejects_shape_mismatch() -> None:
    scorer = PixelFeatureDiffScorer(clock=lambda: NOW)

    with pytest.raises(ValueError, match="image width does not match"):
        scorer.score(
            _sample(),
            part_id="part-00045",
            station_id="station-aoi-1",
            image=[
                [0.10, 0.20, 0.30],
                [0.30, 0.40, 0.50],
            ],
            features={"edge_density": 0.50, "hole_roundness": 0.90},
        )
