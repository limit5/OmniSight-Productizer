"""Golden-sample registry and diff scorer for C8 inspection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.inspection.verdict import Defect, InspectionVerdict


Number = int | float
PixelGrid = Sequence[Sequence[Number]]


class GoldenSample(BaseModel):
    """Versioned reference sample for one recipe and station."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    recipe_id: str = Field(..., min_length=1)
    recipe_version: str = Field(..., min_length=1)
    sample_id: str = Field(..., min_length=1)
    part_id: str = Field(..., min_length=1)
    station_id: str = Field(..., min_length=1)
    model_version: str = Field(..., min_length=1)
    reference_image: list[list[float]] | None = None
    features: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_reference_signal(self) -> "GoldenSample":
        if self.reference_image is None and not self.features:
            raise ValueError("golden sample must include reference_image or features")
        return self


class GoldenSampleRegistry:
    """In-memory golden-sample registry keyed by recipe and version."""

    def __init__(self, samples: Iterable[GoldenSample] = ()) -> None:
        self._samples: dict[tuple[str, str, str], GoldenSample] = {}
        for sample in samples:
            self.register(sample)

    def register(self, sample: GoldenSample) -> None:
        key = self._key(sample.recipe_id, sample.recipe_version, sample.sample_id)
        if key in self._samples:
            raise ValueError(
                "golden sample already registered: "
                f"{sample.recipe_id}@{sample.recipe_version}/{sample.sample_id}"
            )
        self._samples[key] = sample

    def get(self, recipe_id: str, recipe_version: str, sample_id: str) -> GoldenSample:
        key = self._key(recipe_id, recipe_version, sample_id)
        try:
            return self._samples[key]
        except KeyError as exc:
            raise KeyError(
                f"unknown golden sample: {recipe_id}@{recipe_version}/{sample_id}"
            ) from exc

    def list_versions(self, recipe_id: str) -> list[str]:
        versions = {
            recipe_version
            for registered_recipe_id, recipe_version, _sample_id in self._samples
            if registered_recipe_id == recipe_id
        }
        return sorted(versions)

    @staticmethod
    def _key(recipe_id: str, recipe_version: str, sample_id: str) -> tuple[str, str, str]:
        return (recipe_id, recipe_version, sample_id)


class DiffScorer(Protocol):
    """Pluggable scorer interface for host diff and future RKNN scorers."""

    def score(
        self,
        sample: GoldenSample,
        *,
        part_id: str,
        station_id: str,
        image: PixelGrid | None = None,
        features: Mapping[str, Number] | None = None,
        ts_capture: datetime | None = None,
    ) -> InspectionVerdict:
        """Score an observed part against a golden sample."""


class PixelFeatureDiffScorer:
    """Score numeric pixel grids and scalar features against a golden sample."""

    def __init__(
        self,
        *,
        pixel_threshold: float = 0.05,
        feature_threshold: float = 0.05,
        model_version: str = "pixel-feature-diff@host",
        clock: Any | None = None,
    ) -> None:
        if pixel_threshold < 0.0:
            raise ValueError("pixel_threshold must be non-negative")
        if feature_threshold < 0.0:
            raise ValueError("feature_threshold must be non-negative")
        self.pixel_threshold = pixel_threshold
        self.feature_threshold = feature_threshold
        self.model_version = model_version
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def score(
        self,
        sample: GoldenSample,
        *,
        part_id: str,
        station_id: str,
        image: PixelGrid | None = None,
        features: Mapping[str, Number] | None = None,
        ts_capture: datetime | None = None,
    ) -> InspectionVerdict:
        defects: list[Defect] = []
        capture_time = ts_capture or self._clock()
        verdict_time = self._clock()

        if sample.reference_image is not None:
            if image is None:
                raise ValueError("image is required for pixel diff scoring")
            pixel_score = _mean_abs_pixel_diff(sample.reference_image, image)
            if pixel_score > self.pixel_threshold:
                defects.append(
                    Defect(
                        class_="pixel_diff",
                        loc=[0.0, 0.0],
                        score=_bounded_score(pixel_score, self.pixel_threshold),
                        raw_score=pixel_score,
                        threshold=self.pixel_threshold,
                    )
                )

        if sample.features:
            if features is None:
                raise ValueError("features are required for feature diff scoring")
            missing = sorted(set(sample.features) - set(features))
            if missing:
                raise ValueError(f"missing observed features: {', '.join(missing)}")
            for name, expected in sorted(sample.features.items()):
                observed = float(features[name])
                feature_score = _relative_diff(float(expected), observed)
                if feature_score > self.feature_threshold:
                    defects.append(
                        Defect(
                            class_="feature_diff",
                            loc=[0.0, 0.0],
                            score=_bounded_score(feature_score, self.feature_threshold),
                            feature=name,
                            expected=float(expected),
                            observed=observed,
                            raw_score=feature_score,
                            threshold=self.feature_threshold,
                        )
                    )

        return InspectionVerdict(
            part_id=part_id,
            station_id=station_id,
            verdict="fail" if defects else "pass",
            defects=defects,
            model_version=self.model_version,
            recipe_version=sample.recipe_version,
            ts_capture=capture_time,
            ts_verdict=verdict_time,
            golden_recipe_id=sample.recipe_id,
            golden_sample_id=sample.sample_id,
            golden_model_version=sample.model_version,
        )


def _mean_abs_pixel_diff(reference: PixelGrid, observed: PixelGrid) -> float:
    if len(reference) != len(observed):
        raise ValueError("image height does not match golden sample")

    total = 0.0
    count = 0
    for row_index, reference_row in enumerate(reference):
        observed_row = observed[row_index]
        if len(reference_row) != len(observed_row):
            raise ValueError("image width does not match golden sample")
        for column_index, expected in enumerate(reference_row):
            total += abs(float(expected) - float(observed_row[column_index]))
            count += 1

    if count == 0:
        raise ValueError("image must include at least one pixel")
    return total / count


def _relative_diff(expected: float, observed: float) -> float:
    denominator = max(abs(expected), 1.0)
    return abs(expected - observed) / denominator


def _bounded_score(raw_score: float, threshold: float) -> float:
    if threshold == 0.0:
        return 1.0
    return max(0.0, min(1.0, raw_score / threshold))
