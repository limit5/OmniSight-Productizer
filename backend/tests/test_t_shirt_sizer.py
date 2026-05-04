"""BP.C.1 - Contract tests for ``backend/t_shirt_sizer.py``.

Pins the S/M/XL sizing surface only. Topology builders, graph rewiring,
and orchestrator pre-stage routing live in later BP.C rows.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from backend import t_shirt_sizer as ts
from backend.t_shirt_sizer import (
    TShirtSizingReport,
    TShirtSizerParseError,
    build_sizing_prompt,
    heuristic_size_project,
    parse_sizer_response,
    size_project,
)


class TestModuleConstants:
    def test_default_models_prefer_haiku_then_gemma(self) -> None:
        assert ts.DEFAULT_SIZER_MODELS[0].startswith("anthropic/claude-haiku")
        assert any("gemma" in model for model in ts.DEFAULT_SIZER_MODELS)

    def test_valid_size_tuple_is_closed(self) -> None:
        assert ts._VALID_SIZES == ("S", "M", "XL")


class TestReportModel:
    def test_accepts_each_size(self) -> None:
        for size in ("S", "M", "XL"):
            report = TShirtSizingReport(
                size=size,  # type: ignore[arg-type]
                confidence=0.9,
                source="llm",
            )
            assert report.size == size

    def test_rejects_invalid_size(self) -> None:
        with pytest.raises(ValidationError):
            TShirtSizingReport(size="L", confidence=0.9, source="llm")  # type: ignore[arg-type]

    def test_rejects_confidence_outside_unit_range(self) -> None:
        with pytest.raises(ValidationError):
            TShirtSizingReport(size="M", confidence=1.1, source="llm")

    def test_report_is_frozen(self) -> None:
        report = TShirtSizingReport(size="M", confidence=0.8, source="llm")
        with pytest.raises(ValidationError):
            report.size = "XL"  # type: ignore[misc]

    def test_json_round_trip(self) -> None:
        report = TShirtSizingReport(
            size="XL",
            confidence=0.91,
            rationale="broad integration",
            model="anthropic/claude-test",
            tokens_used=42,
            source="llm",
        )
        rebuilt = TShirtSizingReport.model_validate_json(report.model_dump_json())
        assert rebuilt == report


class TestPrompt:
    def test_prompt_mentions_all_sizes_and_json_contract(self) -> None:
        prompt = build_sizing_prompt("Build a camera firmware pipeline")
        assert "S  =" in prompt
        assert "M  =" in prompt
        assert "XL =" in prompt
        assert '"size": "S|M|XL"' in prompt
        assert "Build a camera firmware pipeline" in prompt


class TestParseSizerResponse:
    def test_parse_clean_json(self) -> None:
        report = parse_sizer_response(
            '{"size":"S","confidence":0.88,"rationale":"one file"}',
            model="anthropic/claude-test",
            tokens_used=12,
        )
        assert report.size == "S"
        assert report.confidence == 0.88
        assert report.rationale == "one file"
        assert report.model == "anthropic/claude-test"
        assert report.tokens_used == 12
        assert report.source == "llm"

    def test_parse_strips_json_fence(self) -> None:
        report = parse_sizer_response(
            '```json\n{"size":"M","confidence":0.7,"rationale":"standard"}\n```'
        )
        assert report.size == "M"

    def test_parse_extracts_from_prose_wrap(self) -> None:
        report = parse_sizer_response(
            'Here is the JSON: {"size":"XL","confidence":0.9,"rationale":"HA"}'
        )
        assert report.size == "XL"

    def test_parse_uppercases_size(self) -> None:
        report = parse_sizer_response('{"size":"xl","confidence":0.9}')
        assert report.size == "XL"

    @pytest.mark.parametrize("raw", ["", "not json", "[]", '{"size":"L"}'])
    def test_parse_rejects_invalid_response(self, raw: str) -> None:
        with pytest.raises(TShirtSizerParseError):
            parse_sizer_response(raw)

    def test_parse_clamps_confidence(self) -> None:
        high = parse_sizer_response('{"size":"M","confidence":2.5}')
        low = parse_sizer_response('{"size":"M","confidence":-1}')
        bad = parse_sizer_response('{"size":"M","confidence":"bad"}')
        assert high.confidence == 1.0
        assert low.confidence == 0.0
        assert bad.confidence == 0.0

    def test_negative_tokens_clamp_to_zero(self) -> None:
        report = parse_sizer_response('{"size":"M","confidence":0.9}', tokens_used=-5)
        assert report.tokens_used == 0


class TestHeuristicFallback:
    def test_empty_text_defaults_to_m_zero_confidence(self) -> None:
        report = heuristic_size_project("")
        assert report.size == "M"
        assert report.confidence == 0.0
        assert report.source == "heuristic"

    def test_simple_doc_change_is_s(self) -> None:
        report = heuristic_size_project("Fix a README typo in one file")
        assert report.size == "S"
        assert report.source == "heuristic"

    def test_cjk_simple_change_is_s(self) -> None:
        report = heuristic_size_project("單一檔案文案改字")
        assert report.size == "S"

    def test_firmware_pipeline_is_xl(self) -> None:
        report = heuristic_size_project("Build firmware, cloud pipeline, and HA failover")
        assert report.size == "XL"

    def test_cjk_distributed_work_is_xl(self) -> None:
        report = heuristic_size_project("多租戶分散式微服務遷移")
        assert report.size == "XL"

    def test_mixed_signals_default_to_m(self) -> None:
        report = heuristic_size_project("Simple docs for a multi-tenant migration")
        assert report.size == "M"

    def test_normal_feature_defaults_to_m(self) -> None:
        report = heuristic_size_project("Add user profile settings and validation")
        assert report.size == "M"
        assert report.confidence == 0.5


class TestSizeProject:
    @pytest.mark.asyncio
    async def test_uses_first_high_confidence_llm_response(self) -> None:
        calls: list[str] = []

        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            calls.append(model)
            return json.dumps({
                "size": "S",
                "confidence": 0.91,
                "rationale": "single lane",
            }), 17

        report = await size_project(
            "Fix one typo",
            ask_fn=ask_fn,
            models=("anthropic/haiku-test", "ollama/gemma-test"),
        )

        assert report.size == "S"
        assert report.model == "anthropic/haiku-test"
        assert report.tokens_used == 17
        assert calls == ["anthropic/haiku-test"]

    @pytest.mark.asyncio
    async def test_falls_through_to_gemma_when_haiku_empty(self) -> None:
        calls: list[str] = []

        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            calls.append(model)
            if model == "anthropic/haiku-test":
                return "", 0
            return '{"size":"XL","confidence":0.8,"rationale":"broad"}', 33

        report = await size_project(
            "Build firmware and cloud integration",
            ask_fn=ask_fn,
            models=("anthropic/haiku-test", "ollama/gemma-test"),
        )

        assert report.size == "XL"
        assert report.model == "ollama/gemma-test"
        assert calls == ["anthropic/haiku-test", "ollama/gemma-test"]

    @pytest.mark.asyncio
    async def test_low_confidence_llm_response_uses_heuristic(self) -> None:
        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            return '{"size":"S","confidence":0.2,"rationale":"unsure"}', 10

        report = await size_project(
            "Build firmware and HA pipeline",
            ask_fn=ask_fn,
            models=("anthropic/haiku-test",),
        )

        assert report.size == "XL"
        assert report.source == "heuristic"

    @pytest.mark.asyncio
    async def test_malformed_llm_response_uses_heuristic(self) -> None:
        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            return "not json", 10

        report = await size_project(
            "Fix README typo",
            ask_fn=ask_fn,
            models=("anthropic/haiku-test",),
        )

        assert report.size == "S"
        assert report.source == "heuristic"

    @pytest.mark.asyncio
    async def test_ask_fn_exception_uses_next_model(self) -> None:
        calls: list[str] = []

        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            calls.append(model)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return '{"size":"M","confidence":0.8,"rationale":"standard"}', 20

        report = await size_project(
            "Add profile settings",
            ask_fn=ask_fn,
            models=("anthropic/haiku-test", "ollama/gemma-test"),
        )

        assert report.size == "M"
        assert report.model == "ollama/gemma-test"
        assert calls == ["anthropic/haiku-test", "ollama/gemma-test"]

    @pytest.mark.asyncio
    async def test_no_models_uses_heuristic_without_calling_ask_fn(self) -> None:
        called = False

        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            nonlocal called
            called = True
            return '{"size":"M","confidence":1}', 1

        report = await size_project(
            "Fix README typo",
            ask_fn=ask_fn,
            models=(),
        )

        assert report.size == "S"
        assert called is False

    @pytest.mark.asyncio
    async def test_prompt_contains_user_text_for_llm(self) -> None:
        seen: dict[str, Any] = {}

        async def ask_fn(model: str, prompt: str) -> tuple[str, int]:
            seen["prompt"] = prompt
            return '{"size":"M","confidence":0.8,"rationale":"standard"}', 5

        await size_project("Add user profile settings", ask_fn=ask_fn, models=("m",))
        assert "Add user profile settings" in seen["prompt"]
        assert "Return ONLY one JSON object" in seen["prompt"]
