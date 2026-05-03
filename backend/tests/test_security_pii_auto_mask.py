"""SC.12.3 -- PII auto-mask helper contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from backend.security.pii_auto_mask import (
    mask_log_payload,
    mask_log_text,
    mask_response_payload,
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


class TokenAnalyzer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def analyze(
        self,
        *,
        text: str,
        language: str,
        entities: list[str],
        score_threshold: float,
    ) -> list[FakeRecognizerResult]:
        self.calls.append(text)
        results: list[FakeRecognizerResult] = []
        for token, entity_type in (
            ("alice@example.com", "EMAIL_ADDRESS"),
            ("bob@example.com", "EMAIL_ADDRESS"),
            ("555-0100", "PHONE_NUMBER"),
        ):
            start = text.find(token)
            if start >= 0:
                results.append(
                    FakeRecognizerResult(
                        entity_type=entity_type,
                        start=start,
                        end=start + len(token),
                        score=0.91,
                    )
                )
        return results


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


def test_mask_log_text_masks_string_and_returns_metadata() -> None:
    text = "Email alice@example.com for support."
    start = text.index("alice@example.com")
    analyzer = FakeAnalyzer(
        [FakeRecognizerResult("EMAIL_ADDRESS", start, start + 17, 0.94)]
    )
    anonymizer = FakeAnonymizer()

    result = mask_log_text(text, analyzer=analyzer, anonymizer=anonymizer)

    assert result.changed is True
    assert result.value == "Email <EMAIL_ADDRESS> for support."
    assert result.entity_types == ("EMAIL_ADDRESS",)
    assert result.to_dict() == {
        "changed": True,
        "entity_types": ["EMAIL_ADDRESS"],
        "value": "Email <EMAIL_ADDRESS> for support.",
    }


def test_mask_log_text_none_returns_empty_without_engine_call() -> None:
    analyzer = FakeAnalyzer(
        [FakeRecognizerResult("EMAIL_ADDRESS", 0, 5, 0.9)]
    )

    result = mask_log_text(None, analyzer=analyzer, anonymizer=FakeAnonymizer())

    assert result.changed is False
    assert result.value == ""
    assert result.entity_types == ()
    assert analyzer.calls == []


def test_mask_log_payload_masks_nested_json_like_payload_without_mutating_source() -> None:
    payload = {
        "message": "Email alice@example.com",
        "context": {
            "owner": "bob@example.com",
            "phones": ["555-0100", 7],
            "tags": ("public", "alice@example.com"),
        },
        "count": 2,
    }
    analyzer = TokenAnalyzer()

    result = mask_log_payload(payload, analyzer=analyzer, anonymizer=FakeAnonymizer())

    assert result.changed is True
    assert result.entity_types == ("EMAIL_ADDRESS", "PHONE_NUMBER")
    assert result.value == {
        "message": "Email <EMAIL_ADDRESS>",
        "context": {
            "owner": "<EMAIL_ADDRESS>",
            "phones": ["<PHONE_NUMBER>", 7],
            "tags": ("public", "<EMAIL_ADDRESS>"),
        },
        "count": 2,
    }
    assert payload["message"] == "Email alice@example.com"
    assert payload["context"]["tags"] == ("public", "alice@example.com")


def test_mask_log_payload_respects_max_depth_for_nested_containers() -> None:
    payload = {
        "top": "alice@example.com",
        "nested": {"email": "bob@example.com"},
    }

    result = mask_log_payload(
        payload,
        analyzer=TokenAnalyzer(),
        anonymizer=FakeAnonymizer(),
        max_depth=1,
    )

    assert result.changed is True
    assert result.value == {
        "top": "<EMAIL_ADDRESS>",
        "nested": {"email": "bob@example.com"},
    }
    assert result.entity_types == ("EMAIL_ADDRESS",)


def test_mask_response_payload_uses_same_contract_as_log_payload() -> None:
    payload = {"email": "alice@example.com"}

    result = mask_response_payload(
        payload,
        analyzer=TokenAnalyzer(),
        anonymizer=FakeAnonymizer(),
    )

    assert result.changed is True
    assert result.value == {"email": "<EMAIL_ADDRESS>"}
    assert result.entity_types == ("EMAIL_ADDRESS",)
