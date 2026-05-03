"""BP.A.3 — Contract tests for ``backend/templates/impl.py``.

Pins the ImplTemplate validation surface so downstream work — the
Auditor Guild consuming the template (BP.A.4 ReviewTemplate), the
FastAPI middleware (BP.A.6) translating ValidationError into the
cognitive-penalty prompt and cross-checking ``target_triple`` against
the sibling TaskTemplate — can rely on the boundaries being enforced
*at the model layer*, not in ad-hoc if-checks inside FastAPI handlers.

BP.A.7 will fold a superset of these checks into the unified
~150-test ``test_templates.py`` suite. Until then this file is the
authoritative regression for ImplTemplate alone — keep it green.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.templates.impl import SCHEMA_VERSION, ImplTemplate


def _valid_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        source_code_payload="int main(void) { return 0; }\n",
        compiled_exit_code=0,
        time_complexity="O(n log n)",
        target_triple="x86_64-pc-linux-gnu",
    )
    base.update(overrides)
    return base


class TestImplTemplateHappyPath:
    def test_constructs_with_default_schema_version(self) -> None:
        t = ImplTemplate(**_valid_payload())
        assert t.schema_version == SCHEMA_VERSION == "1.0.0"

    def test_is_frozen(self) -> None:
        t = ImplTemplate(**_valid_payload())
        with pytest.raises(ValidationError):
            t.time_complexity = "O(1)"  # type: ignore[misc]

    def test_strips_whitespace_on_string_fields(self) -> None:
        payload = _valid_payload(
            source_code_payload="   payload\n",
            time_complexity="  O(n)  ",
            target_triple="  x86_64-pc-linux-gnu  ",
        )
        t = ImplTemplate(**payload)
        assert t.source_code_payload == "payload"
        assert t.time_complexity == "O(n)"
        assert t.target_triple == "x86_64-pc-linux-gnu"

    def test_json_round_trip(self) -> None:
        original = ImplTemplate(**_valid_payload())
        rebuilt = ImplTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt == original

    def test_minimal_one_byte_source_payload_accepted(self) -> None:
        t = ImplTemplate(**_valid_payload(source_code_payload="x"))
        assert t.source_code_payload == "x"


class TestImplTemplateSourceCodePayload:
    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**_valid_payload(source_code_payload=""))
        assert any(
            e["type"] == "string_too_short"
            and tuple(e["loc"]) == ("source_code_payload",)
            for e in exc.value.errors()
        )

    def test_whitespace_only_rejected(self) -> None:
        # ``str_strip_whitespace`` strips first; result is "" and trips
        # the field's ``min_length=1`` constraint.
        with pytest.raises(ValidationError):
            ImplTemplate(**_valid_payload(source_code_payload="   \n\t "))

    def test_missing_field_reports_clear_loc(self) -> None:
        payload = _valid_payload()
        del payload["source_code_payload"]
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("source_code_payload",) in locs


class TestImplTemplateCompiledExitCode:
    def test_accepts_zero(self) -> None:
        t = ImplTemplate(**_valid_payload(compiled_exit_code=0))
        assert t.compiled_exit_code == 0

    @pytest.mark.parametrize("bad", [1, 2, 127, -1, 255])
    def test_rejects_non_zero(self, bad: int) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**_valid_payload(compiled_exit_code=bad))
        assert any(
            e["type"] == "literal_error" for e in exc.value.errors()
        ), exc.value.errors()

    def test_rejects_string(self) -> None:
        # Pydantic v2 may coerce numeric strings; the Literal[0]
        # constraint is what we rely on for hard rejection.
        with pytest.raises(ValidationError):
            ImplTemplate(**_valid_payload(compiled_exit_code="0"))  # type: ignore[arg-type]


class TestImplTemplateTimeComplexity:
    @pytest.mark.parametrize(
        "good",
        [
            "O(1)",
            "O(n)",
            "O(n log n)",
            "O(n^2)",
            "O(2^n)",
            "O(n!)",
            "O(n*log(n))",   # nested parens
            "O(m + n)",
            "Θ(n)",
            "Ω(log n)",
            "o(n)",
            "θ(n^2)",
            "ω(1)",
        ],
    )
    def test_accepts_canonical_big_o(self, good: str) -> None:
        t = ImplTemplate(**_valid_payload(time_complexity=good))
        assert t.time_complexity == good

    @pytest.mark.parametrize(
        "bad",
        [
            "",                # empty
            "fast",            # no Bachmann–Landau prefix, no parens
            "n^2",             # missing leading O/Θ/Ω
            "O",               # no parens
            "O()",             # empty body — schema requires .+
            "O(",              # missing closing paren
            "O n)",            # missing opening paren
            "Big-O(n)",        # leading char not in {O,o,Θ,θ,Ω,ω}
            "1",               # bare scalar
        ],
    )
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**_valid_payload(time_complexity=bad))
        codes = {e["type"] for e in exc.value.errors()}
        # ``""`` trips ``string_too_short`` (min_length=1); the rest trip
        # the regex pattern. Either flavour is acceptable rejection.
        assert codes & {"string_pattern_mismatch", "string_too_short"}, codes

    def test_missing_field_reports_clear_loc(self) -> None:
        payload = _valid_payload()
        del payload["time_complexity"]
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("time_complexity",) in locs


class TestImplTemplateTargetTriple:
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
        t = ImplTemplate(**_valid_payload(target_triple=triple))
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
            ImplTemplate(**_valid_payload(target_triple=bad))
        codes = {e["type"] for e in exc.value.errors()}
        assert codes & {"string_pattern_mismatch", "string_too_short"}, codes


class TestImplTemplateStrictness:
    def test_extra_fields_rejected(self) -> None:
        payload = _valid_payload()
        payload["rogue"] = "nope"
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**payload)
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_schema_version_pinned_to_one_zero_zero(self) -> None:
        payload = _valid_payload()
        payload["schema_version"] = "2.0.0"
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**payload)
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_missing_required_field_reports_clear_error(self) -> None:
        payload = _valid_payload()
        del payload["compiled_exit_code"]
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("compiled_exit_code",) in locs


class TestImplTemplateCrossTemplateAlignment:
    """``ImplTemplate.target_triple`` shares grammar with TaskTemplate.

    The semantic equality check (Coder produced what PM asked for) is
    BP.A.6 middleware territory, but the *grammar* must match exactly
    so a triple that round-trips through PM never bounces at Coder.
    """

    def test_target_triple_grammar_matches_task_template(self) -> None:
        from backend.templates.task import TaskTemplate

        triples = [
            "x86_64-pc-linux-gnu",
            "aarch64-unknown-linux-gnu",
            "armv7-unknown-linux-gnueabihf",
        ]
        for triple in triples:
            i = ImplTemplate(**_valid_payload(target_triple=triple))
            t = TaskTemplate(
                target_triple=triple,
                allowed_dependencies=[],
                max_cognitive_load_tokens=4096,
                guild_id="backend",
                size="M",
            )
            assert i.target_triple == t.target_triple == triple
