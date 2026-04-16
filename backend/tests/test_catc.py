"""O0 (#263) — Tests for CATC payload schema + validator + glob + codeowners check."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.catc import (
    CatcCodeownersCheck,
    ImpactScope,
    Navigation,
    TaskCard,
    check_catc_against_codeowners,
    globs_overlap,
    match_path_against_glob,
    task_card_json_schema,
)


# Reference payload from docs/design/enterprise-multi-agent-event-driven-architecture.md §二
_REFERENCE_PAYLOAD = {
    "jira_ticket": "PROJ-402",
    "acceptance_criteria": (
        "系統能根據設定檔動態切換 UVC 或 RTSP 來源，且記憶體無洩漏。"
    ),
    "navigation": {
        "entry_point": "./src/camera/stream_manager.cpp",
        "impact_scope": {
            "allowed": ["src/camera/*", "include/camera_api.h"],
            "forbidden": ["src/core/npu_pipeline.cpp"],
        },
    },
    "domain_context": (
        "ARM32 架構下切換影像串流時，必須先確保 V4L2 緩衝區已完全 munmap。"
    ),
    "handoff_protocol": [
        "執行 make test 通過沙盒驗證",
        "Commit 代碼並發起 Pull Request",
    ],
}


class TestTaskCardValidation:

    def test_reference_payload_parses(self):
        card = TaskCard.from_dict(_REFERENCE_PAYLOAD)
        assert card.jira_ticket == "PROJ-402"
        assert card.navigation.entry_point.endswith("stream_manager.cpp")
        assert card.navigation.impact_scope.allowed == [
            "src/camera/*",
            "include/camera_api.h",
        ]

    def test_rejects_missing_impact_scope(self):
        """The validator MUST reject payloads that omit impact_scope."""
        bad = {
            "jira_ticket": "PROJ-1",
            "acceptance_criteria": "x",
            "navigation": {"entry_point": "src/x.cpp"},
        }
        with pytest.raises(ValidationError):
            TaskCard.from_dict(bad)

    def test_rejects_empty_allowed_list(self):
        bad = dict(_REFERENCE_PAYLOAD)
        bad["navigation"] = {
            "entry_point": "src/x.cpp",
            "impact_scope": {"allowed": [], "forbidden": []},
        }
        with pytest.raises(ValidationError):
            TaskCard.from_dict(bad)

    def test_rejects_missing_navigation(self):
        bad = {"jira_ticket": "PROJ-1", "acceptance_criteria": "x"}
        with pytest.raises(ValidationError):
            TaskCard.from_dict(bad)

    def test_rejects_bad_jira_ticket(self):
        bad = dict(_REFERENCE_PAYLOAD)
        bad["jira_ticket"] = "lowercase-1"
        with pytest.raises(ValidationError):
            TaskCard.from_dict(bad)

    def test_rejects_unknown_fields(self):
        """extra=forbid — worker contract must match schema exactly."""
        bad = dict(_REFERENCE_PAYLOAD)
        bad["mystery_field"] = "oops"
        with pytest.raises(ValidationError):
            TaskCard.from_dict(bad)

    def test_rejects_empty_glob_string(self):
        bad = dict(_REFERENCE_PAYLOAD)
        bad["navigation"] = {
            "entry_point": "src/x.cpp",
            "impact_scope": {"allowed": ["  "], "forbidden": []},
        }
        with pytest.raises(ValidationError):
            TaskCard.from_dict(bad)

    def test_defaults_for_optional_fields(self):
        minimal = {
            "jira_ticket": "PROJ-1",
            "acceptance_criteria": "x",
            "navigation": {
                "entry_point": "src/x.cpp",
                "impact_scope": {"allowed": ["src/x.cpp"]},
            },
        }
        card = TaskCard.from_dict(minimal)
        assert card.domain_context == ""
        assert card.handoff_protocol == []
        assert card.navigation.impact_scope.forbidden == []


class TestRoundTrip:

    def test_dict_roundtrip(self):
        card = TaskCard.from_dict(_REFERENCE_PAYLOAD)
        d = card.to_dict()
        card2 = TaskCard.from_dict(d)
        assert card == card2

    def test_json_roundtrip(self):
        card = TaskCard.from_dict(_REFERENCE_PAYLOAD)
        raw = card.to_json()
        card2 = TaskCard.from_json(raw)
        assert card == card2

    def test_json_payload_unchanged(self):
        card = TaskCard.from_dict(_REFERENCE_PAYLOAD)
        reparsed = json.loads(card.to_json())
        # Normalize reference payload (pydantic strips the './' from entry_point? no, it shouldn't)
        assert reparsed["jira_ticket"] == _REFERENCE_PAYLOAD["jira_ticket"]
        assert (
            reparsed["navigation"]["impact_scope"]["allowed"]
            == _REFERENCE_PAYLOAD["navigation"]["impact_scope"]["allowed"]
        )

    def test_nested_dataclass_models_equal(self):
        card = TaskCard.from_dict(_REFERENCE_PAYLOAD)
        assert isinstance(card.navigation, Navigation)
        assert isinstance(card.navigation.impact_scope, ImpactScope)

    def test_json_schema_has_required_fields(self):
        schema = task_card_json_schema()
        required = set(schema.get("required", []))
        assert {
            "jira_ticket",
            "acceptance_criteria",
            "navigation",
        }.issubset(required)


class TestGlobParser:

    @pytest.mark.parametrize(
        "path,pattern,expected",
        [
            # single-segment wildcard
            ("src/camera/foo.cpp", "src/camera/*", True),
            ("src/camera/nested/foo.cpp", "src/camera/*", False),
            # double-star any-depth
            ("src/camera/foo.cpp", "src/camera/**", True),
            ("src/camera/nested/foo.cpp", "src/camera/**", True),
            ("src/camera", "src/camera/**", True),
            # literal file
            ("include/camera_api.h", "include/camera_api.h", True),
            ("include/other.h", "include/camera_api.h", False),
            # extension glob
            ("foo.dts", "*.dts", True),
            ("foo.cpp", "*.dts", False),
            # question mark
            ("a.c", "?.c", True),
            ("ab.c", "?.c", False),
            # star does NOT cross slashes
            ("src/camera/foo.cpp", "src/*.cpp", False),
            ("src/foo.cpp", "src/*.cpp", True),
        ],
    )
    def test_match_path_against_glob(self, path, pattern, expected):
        assert match_path_against_glob(path, pattern) is expected

    def test_overlap_concrete_paths(self):
        assert globs_overlap("a/b.c", "a/b.c") is True
        assert globs_overlap("a/b.c", "a/c.c") is False

    def test_overlap_concrete_vs_glob(self):
        assert globs_overlap("src/camera/foo.cpp", "src/camera/*") is True
        assert globs_overlap("src/core/foo.cpp", "src/camera/*") is False

    def test_overlap_glob_vs_glob(self):
        assert globs_overlap("src/camera/*", "src/camera/**") is True
        assert globs_overlap("src/camera/*", "src/core/*") is False
        assert globs_overlap("include/camera_api.h", "include/**") is True

    def test_overlap_bare_wildcard_matches_anything(self):
        assert globs_overlap("*", "src/camera/*") is True


class TestCheckCatcAgainstCodeowners:
    """The intersection helper is the pre-dispatch gate — verify it is sound."""

    def _make_card(self, allowed, forbidden=None) -> TaskCard:
        return TaskCard.from_dict(
            {
                "jira_ticket": "PROJ-1",
                "acceptance_criteria": "x",
                "navigation": {
                    "entry_point": allowed[0],
                    "impact_scope": {
                        "allowed": allowed,
                        "forbidden": forbidden or [],
                    },
                },
            },
        )

    def test_agent_owns_allowed_paths(self):
        """firmware/hal owns src/hal/** per CODEOWNERS, so src/hal/* card is OK."""
        card = self._make_card(["src/hal/gpio.h"])
        result = check_catc_against_codeowners(card, "firmware", "hal")
        assert isinstance(result, CatcCodeownersCheck)
        assert result.ok is True
        assert "src/hal/gpio.h" in result.allowed_owned
        assert result.allowed_foreign == []
        assert result.forbidden_in_scope == []

    def test_agent_does_not_own_allowed_paths(self):
        """software/algorithm asked to touch src/hal/** — foreign owner."""
        card = self._make_card(["src/hal/gpio.h"])
        result = check_catc_against_codeowners(card, "software", "algorithm")
        assert result.ok is False
        # The glob src/hal/gpio.h should be flagged as foreign (firmware-owned).
        foreign_globs = [g for g, _ in result.allowed_foreign]
        assert "src/hal/gpio.h" in foreign_globs

    def test_forbidden_overlaps_scope_is_blocked(self):
        """firmware/hal has src/hal/** in scope; a card forbidding src/hal/secret.h
        while asking firmware/hal to run it is a contradiction and must fail.
        """
        card = self._make_card(
            allowed=["src/other/generic.c"],
            forbidden=["src/hal/secret.h"],
        )
        result = check_catc_against_codeowners(card, "firmware", "hal")
        assert result.ok is False
        assert "src/hal/secret.h" in result.forbidden_in_scope

    def test_unowned_path_is_soft_allowed(self):
        """Paths with no CODEOWNERS entry do not block dispatch."""
        card = self._make_card(["random/unowned/file.txt"])
        result = check_catc_against_codeowners(card, "firmware", "hal")
        assert result.ok is True
        assert "random/unowned/file.txt" in result.allowed_unowned
        assert "unowned" in result.reason

    def test_reason_mentions_agent_when_ownership_complete(self):
        card = self._make_card(["src/hal/gpio.h", "src/hal/i2c.c"])
        result = check_catc_against_codeowners(card, "firmware", "hal")
        assert result.ok is True
        assert "firmware" in result.reason
