"""SC.12.1 -- Microsoft Presidio adapter contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from backend.security.pii_presidio import (
    DEFAULT_PII_ENTITIES,
    PresidioConfig,
    analyze_pii,
    mask_pii,
)


@dataclass(frozen=True)
class FakeRecognizerResult:
    entity_type: str
    start: int
    end: int
    score: float


class FakeAnalyzer:
    def __init__(self, results: list[FakeRecognizerResult]):
        self.results = results
        self.calls: list[dict[str, object]] = []

    def analyze(
        self,
        *,
        text: str,
        language: str,
        entities: list[str],
        score_threshold: float,
    ) -> list[FakeRecognizerResult]:
        self.calls.append(
            {
                "text": text,
                "language": language,
                "entities": entities,
                "score_threshold": score_threshold,
            }
        )
        return list(self.results)


class FakeAnonymizer:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def anonymize(
        self,
        *,
        text: str,
        analyzer_results: list[FakeRecognizerResult],
    ) -> SimpleNamespace:
        self.calls.append({"text": text, "analyzer_results": analyzer_results})
        out = text
        for item in sorted(analyzer_results, key=lambda result: result.start, reverse=True):
            out = out[: item.start] + f"<{item.entity_type}>" + out[item.end :]
        return SimpleNamespace(text=out)


def test_analyze_pii_normalizes_presidio_results_in_source_order() -> None:
    text = "Email alice@example.com or call 555-0100."
    email_start = text.index("alice@example.com")
    phone_start = text.index("555-0100")
    analyzer = FakeAnalyzer(
        [
            FakeRecognizerResult("PHONE_NUMBER", phone_start, phone_start + 8, 0.71),
            FakeRecognizerResult("EMAIL_ADDRESS", email_start, email_start + 17, 0.93),
        ]
    )

    result = analyze_pii(text, analyzer=analyzer)

    assert result.has_pii is True
    assert result.entity_types == ("EMAIL_ADDRESS", "PHONE_NUMBER")
    assert [item.text for item in result.findings] == ["alice@example.com", "555-0100"]
    assert result.to_dict()["findings"][0]["score"] == pytest.approx(0.93)


def test_analyze_pii_passes_config_to_presidio() -> None:
    analyzer = FakeAnalyzer([])
    cfg = PresidioConfig(
        language="en",
        score_threshold=0.7,
        entities=(" email_address ", "PERSON", "EMAIL_ADDRESS"),
    )

    result = analyze_pii("Contact Alice", config=cfg, analyzer=analyzer)

    assert result.has_pii is False
    assert analyzer.calls == [
        {
            "text": "Contact Alice",
            "language": "en",
            "entities": ["EMAIL_ADDRESS", "PERSON"],
            "score_threshold": 0.7,
        }
    ]


def test_mask_pii_uses_presidio_anonymizer_and_preserves_analysis() -> None:
    text = "alice@example.com works here"
    start = text.index("alice@example.com")
    analyzer = FakeAnalyzer(
        [FakeRecognizerResult("EMAIL_ADDRESS", start, start + 17, 0.96)]
    )
    anonymizer = FakeAnonymizer()

    result = mask_pii(text, analyzer=analyzer, anonymizer=anonymizer)

    assert result.changed is True
    assert result.masked_text == "<EMAIL_ADDRESS> works here"
    assert result.analysis.entity_types == ("EMAIL_ADDRESS",)
    assert anonymizer.calls[0]["analyzer_results"][0].entity_type == "EMAIL_ADDRESS"


def test_mask_pii_does_not_call_anonymizer_when_no_findings() -> None:
    anonymizer = FakeAnonymizer()

    result = mask_pii("no pii here", analyzer=FakeAnalyzer([]), anonymizer=anonymizer)

    assert result.changed is False
    assert result.masked_text == "no pii here"
    assert anonymizer.calls == []


def test_empty_text_short_circuits_without_engine_call() -> None:
    analyzer = FakeAnalyzer(
        [FakeRecognizerResult("EMAIL_ADDRESS", 0, 5, 0.9)]
    )

    result = analyze_pii("", analyzer=analyzer)

    assert result.has_pii is False
    assert analyzer.calls == []


def test_default_entity_catalog_includes_expected_presidio_types() -> None:
    assert "EMAIL_ADDRESS" in DEFAULT_PII_ENTITIES
    assert "PHONE_NUMBER" in DEFAULT_PII_ENTITIES
    assert "US_SSN" in DEFAULT_PII_ENTITIES
    assert "PERSON" not in DEFAULT_PII_ENTITIES
    assert "LOCATION" not in DEFAULT_PII_ENTITIES
    assert "URL" not in DEFAULT_PII_ENTITIES


def test_missing_presidio_dependency_raises_clear_runtime_error(monkeypatch) -> None:
    def fail_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "presidio_analyzer":
            raise ImportError("missing presidio")
        return real_import(name, *args, **kwargs)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__", fail_import)

    with pytest.raises(RuntimeError, match="Microsoft Presidio analyzer"):
        analyze_pii("alice@example.com")
