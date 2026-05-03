"""BP.A.2 — Contract tests for ``backend/templates/task.py``.

Pins the TaskTemplate validation surface so downstream work — the
Cognitive Load Scanner (BP.A.5) consuming ``max_cognitive_load_tokens``,
the FastAPI middleware (BP.A.6) translating ValidationError into the
cognitive-penalty prompt, the BP.B Guild registry validating
``guild_id`` against the 21-Guild enum — can rely on the boundaries
being enforced *at the model layer*, not in ad-hoc if-checks inside
FastAPI handlers.

BP.A.7 will fold a superset of these checks into the unified
~150-test ``test_templates.py`` suite. Until then this file is the
authoritative regression for TaskTemplate alone — keep it green.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.templates.task import SCHEMA_VERSION, TaskTemplate


def _valid_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        target_triple="x86_64-pc-linux-gnu",
        allowed_dependencies=[
            "backend/templates/spec.py",
            "backend/cognitive_load.py",
        ],
        max_cognitive_load_tokens=4096,
        guild_id="backend",
        size="M",
    )
    base.update(overrides)
    return base


class TestTaskTemplateHappyPath:
    def test_constructs_with_default_schema_version(self) -> None:
        t = TaskTemplate(**_valid_payload())
        assert t.schema_version == SCHEMA_VERSION == "1.0.0"

    def test_is_frozen(self) -> None:
        t = TaskTemplate(**_valid_payload())
        with pytest.raises(ValidationError):
            t.size = "XL"  # type: ignore[misc]

    def test_strips_whitespace_on_string_fields(self) -> None:
        payload = _valid_payload()
        payload["guild_id"] = "  backend  "
        t = TaskTemplate(**payload)
        assert t.guild_id == "backend"

    def test_strips_whitespace_on_list_entries(self) -> None:
        payload = _valid_payload(
            allowed_dependencies=["  backend/templates/spec.py  "]
        )
        t = TaskTemplate(**payload)
        assert t.allowed_dependencies == ["backend/templates/spec.py"]

    def test_json_round_trip(self) -> None:
        original = TaskTemplate(**_valid_payload())
        rebuilt = TaskTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt == original

    def test_empty_allowed_dependencies_accepted(self) -> None:
        # A truly trivial task may legitimately need zero contract files;
        # the schema must not force-fit a min_length.
        t = TaskTemplate(**_valid_payload(allowed_dependencies=[]))
        assert t.allowed_dependencies == []


class TestTaskTemplateTargetTriple:
    @pytest.mark.parametrize(
        "triple",
        [
            "x86_64-pc-linux-gnu",
            "aarch64-vendor-linux",
            "aarch64-unknown-linux-gnu",
            "armv7-unknown-linux-gnueabihf",
            "x86_64-apple-darwin",
        ],
    )
    def test_accepts_canonical_triples(self, triple: str) -> None:
        t = TaskTemplate(**_valid_payload(target_triple=triple))
        assert t.target_triple == triple

    @pytest.mark.parametrize(
        "bad",
        [
            "",                       # empty
            "x86_64",                 # 1 segment
            "x86_64-pc",              # 2 segments
            "x86_64--pc-linux",       # empty middle segment
            "x86_64-pc-linux-gnu-x",  # 5 segments (>4)
            "x86 64-pc-linux-gnu",    # space inside segment
            "x86_64/pc/linux",        # wrong separator
        ],
    )
    def test_rejects_malformed_triples(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**_valid_payload(target_triple=bad))
        # min_length=1 fires for empty; pattern fires for the rest. Either
        # is an acceptable rejection — what matters is "did NOT accept".
        codes = {e["type"] for e in exc.value.errors()}
        assert codes & {"string_pattern_mismatch", "string_too_short"}, codes


class TestTaskTemplateAllowedDependencies:
    def test_inner_empty_string_rejected(self) -> None:
        payload = _valid_payload(
            allowed_dependencies=["backend/templates/spec.py", ""]
        )
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**payload)
        errs = exc.value.errors()
        assert any(
            e["type"] == "string_too_short"
            and e["loc"][:2] == ("allowed_dependencies", 1)
            for e in errs
        ), errs

    def test_inner_whitespace_only_rejected(self) -> None:
        # ``str_strip_whitespace`` strips first; result is "" and trips
        # the inner ``min_length=1`` constraint.
        payload = _valid_payload(allowed_dependencies=["   "])
        with pytest.raises(ValidationError):
            TaskTemplate(**payload)

    def test_missing_field_reports_clear_loc(self) -> None:
        payload = _valid_payload()
        del payload["allowed_dependencies"]
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("allowed_dependencies",) in locs


class TestTaskTemplateCognitiveLoadCeiling:
    @pytest.mark.parametrize("bad_value", [0, -1, -1024])
    def test_rejects_non_positive(self, bad_value: int) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**_valid_payload(max_cognitive_load_tokens=bad_value))
        assert any(
            e["type"] == "greater_than" for e in exc.value.errors()
        ), exc.value.errors()

    def test_accepts_one(self) -> None:
        # The ceiling is "must be > 0"; ``1`` is the minimum legal value.
        t = TaskTemplate(**_valid_payload(max_cognitive_load_tokens=1))
        assert t.max_cognitive_load_tokens == 1

    def test_rejects_non_numeric_string(self) -> None:
        # Pydantic v2 coerces "4096" -> 4096 by default; that's intended.
        # What we *do* want rejected is an outright non-numeric value.
        with pytest.raises(ValidationError):
            TaskTemplate(**_valid_payload(max_cognitive_load_tokens="huge"))  # type: ignore[arg-type]


class TestTaskTemplateGuildId:
    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**_valid_payload(guild_id=""))
        assert any(
            e["type"] == "string_too_short" for e in exc.value.errors()
        )

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplate(**_valid_payload(guild_id="   "))


class TestTaskTemplateSize:
    @pytest.mark.parametrize("size", ["S", "M", "XL"])
    def test_accepts_canonical_sizes(self, size: str) -> None:
        t = TaskTemplate(**_valid_payload(size=size))
        assert t.size == size

    @pytest.mark.parametrize("bad", ["s", "L", "XXL", "", "MEDIUM", "1"])
    def test_rejects_other_values(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**_valid_payload(size=bad))
        assert any(
            e["type"] == "literal_error" for e in exc.value.errors()
        )


class TestTaskTemplateStrictness:
    def test_extra_fields_rejected(self) -> None:
        payload = _valid_payload()
        payload["rogue"] = "nope"
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**payload)
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_schema_version_pinned_to_one_zero_zero(self) -> None:
        payload = _valid_payload()
        payload["schema_version"] = "2.0.0"
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**payload)
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_missing_required_field_reports_clear_error(self) -> None:
        payload = _valid_payload()
        del payload["max_cognitive_load_tokens"]
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("max_cognitive_load_tokens",) in locs
