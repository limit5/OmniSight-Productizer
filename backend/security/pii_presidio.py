"""SC.12.1 -- Microsoft Presidio PII detection / masking adapter.

This module is intentionally framework-agnostic: callers pass text in and
receive normalized findings plus an optional masked text result.  It does not
wire itself into logs, responses, or audit rows; those integration points are
owned by later SC.12 rows.

Module-global / cross-worker state audit: default entity names are immutable
tuple data and Presidio engines are built per call unless injected by the
caller.  No mutable singleton or in-memory cache participates in cross-worker
coordination.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Protocol


DEFAULT_PII_ENTITIES: tuple[str, ...] = (
    "CREDIT_CARD",
    "CRYPTO",
    "DATE_TIME",
    "EMAIL_ADDRESS",
    "IBAN_CODE",
    "IP_ADDRESS",
    "PHONE_NUMBER",
    "MEDICAL_LICENSE",
    "US_BANK_NUMBER",
    "US_DRIVER_LICENSE",
    "US_ITIN",
    "US_PASSPORT",
    "US_SSN",
)


@dataclass(frozen=True)
class PresidioConfig:
    language: str = "en"
    score_threshold: float = 0.35
    entities: tuple[str, ...] = DEFAULT_PII_ENTITIES

    def normalized_entities(self) -> list[str]:
        return sorted({item.strip().upper() for item in self.entities if item.strip()})


@dataclass(frozen=True)
class PIIFinding:
    entity_type: str
    start: int
    end: int
    score: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PIIAnalysisResult:
    text: str
    language: str
    score_threshold: float
    findings: tuple[PIIFinding, ...]
    source: str = "presidio"

    @property
    def has_pii(self) -> bool:
        return bool(self.findings)

    @property
    def entity_types(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.entity_type for item in self.findings))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "language": self.language,
            "score_threshold": self.score_threshold,
            "has_pii": self.has_pii,
            "entity_types": list(self.entity_types),
            "findings": [item.to_dict() for item in self.findings],
        }


@dataclass(frozen=True)
class PIIMaskResult:
    original_text: str
    masked_text: str
    analysis: PIIAnalysisResult

    @property
    def changed(self) -> bool:
        return self.original_text != self.masked_text

    def to_dict(self) -> dict[str, Any]:
        data = self.analysis.to_dict()
        data.update(
            {
                "changed": self.changed,
                "masked_text": self.masked_text,
            }
        )
        return data


class AnalyzerLike(Protocol):
    def analyze(
        self,
        *,
        text: str,
        language: str,
        entities: list[str],
        score_threshold: float,
    ) -> list[Any]:
        ...


class AnonymizerLike(Protocol):
    def anonymize(self, *, text: str, analyzer_results: list[Any]) -> Any:
        ...


def _build_default_analyzer() -> AnalyzerLike:
    os.environ.setdefault("TLDEXTRACT_CACHE", "/tmp/omnisight-tldextract")
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpArtifacts, NlpEngine
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            CryptoRecognizer,
            DateRecognizer,
            EmailRecognizer,
            IbanRecognizer,
            IpRecognizer,
            MedicalLicenseRecognizer,
            PhoneRecognizer,
            UsBankRecognizer,
            UsItinRecognizer,
            UsLicenseRecognizer,
            UsPassportRecognizer,
            UsSsnRecognizer,
        )
        from presidio_analyzer.recognizer_registry import RecognizerRegistry
        import spacy
    except ImportError as exc:  # pragma: no cover - exercised via unit test
        raise RuntimeError(
            "Microsoft Presidio analyzer is not installed; install "
            "`presidio-analyzer` from backend/requirements.txt."
        ) from exc

    class _BlankSpacyNlpEngine(NlpEngine):
        """Minimal NLP engine for Presidio's pattern recognizers.

        Presidio's stock ``SpacyNlpEngine`` downloads a model when one is
        missing.  The default OmniSight adapter avoids runtime downloads and
        still exercises Presidio's built-in regex/checksum recognizers; callers
        needing NER-backed PERSON/LOCATION detection can inject a fully
        configured ``AnalyzerEngine``.
        """

        def __init__(self) -> None:
            self._nlp: dict[str, Any] = {}

        def load(self) -> None:
            self._nlp = {"en": spacy.blank("en")}

        def is_loaded(self) -> bool:
            return bool(self._nlp)

        def process_text(self, text: str, language: str) -> NlpArtifacts:
            doc = self._nlp[language](text)
            return NlpArtifacts(
                entities=list(doc.ents),
                tokens=doc,
                tokens_indices=[token.idx for token in doc],
                lemmas=[token.lemma_ or token.text for token in doc],
                nlp_engine=self,
                language=language,
            )

        def process_batch(
            self,
            texts: Iterable[str],
            language: str,
            batch_size: int = 1,
            n_process: int = 1,
            **kwargs: Any,
        ) -> Iterable[tuple[str, NlpArtifacts]]:
            for text in texts:
                yield text, self.process_text(str(text), language)

        def is_stopword(self, word: str, language: str) -> bool:
            return bool(self._nlp[language].vocab[word].is_stop)

        def is_punct(self, word: str, language: str) -> bool:
            return bool(self._nlp[language].vocab[word].is_punct)

        def get_supported_entities(self) -> list[str]:
            return []

        def get_supported_languages(self) -> list[str]:
            return ["en"]

    nlp_engine = _BlankSpacyNlpEngine()
    recognizers = [
        CreditCardRecognizer(),
        CryptoRecognizer(),
        DateRecognizer(),
        EmailRecognizer(),
        IbanRecognizer(),
        IpRecognizer(),
        MedicalLicenseRecognizer(),
        PhoneRecognizer(),
        UsBankRecognizer(),
        UsItinRecognizer(),
        UsLicenseRecognizer(),
        UsPassportRecognizer(),
        UsSsnRecognizer(),
    ]
    registry = RecognizerRegistry(recognizers=recognizers, supported_languages=["en"])
    return AnalyzerEngine(
        registry=registry,
        nlp_engine=nlp_engine,
        supported_languages=["en"],
    )


def _build_default_anonymizer() -> AnonymizerLike:
    try:
        from presidio_anonymizer import AnonymizerEngine
    except ImportError as exc:  # pragma: no cover - exercised via unit test
        raise RuntimeError(
            "Microsoft Presidio anonymizer is not installed; install "
            "`presidio-anonymizer` from backend/requirements.txt."
        ) from exc
    return AnonymizerEngine()


def _finding_from_presidio_result(text: str, result: Any) -> PIIFinding:
    start = int(getattr(result, "start", 0) or 0)
    end = int(getattr(result, "end", start) or start)
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    return PIIFinding(
        entity_type=str(getattr(result, "entity_type", "")),
        start=start,
        end=end,
        score=float(getattr(result, "score", 0.0) or 0.0),
        text=text[start:end],
    )


def _sort_presidio_results(results: Iterable[Any]) -> list[Any]:
    return sorted(
        results,
        key=lambda item: (
            int(getattr(item, "start", 0) or 0),
            int(getattr(item, "end", 0) or 0),
            str(getattr(item, "entity_type", "")),
        ),
    )


def analyze_pii(
    text: str,
    *,
    config: PresidioConfig | None = None,
    analyzer: AnalyzerLike | None = None,
) -> PIIAnalysisResult:
    """Analyze ``text`` with Microsoft Presidio and normalize findings."""

    cfg = config or PresidioConfig()
    if not text:
        return PIIAnalysisResult(
            text=text,
            language=cfg.language,
            score_threshold=cfg.score_threshold,
            findings=(),
        )

    engine = analyzer or _build_default_analyzer()
    raw_results = engine.analyze(
        text=text,
        language=cfg.language,
        entities=cfg.normalized_entities(),
        score_threshold=cfg.score_threshold,
    )
    ordered = _sort_presidio_results(raw_results)
    return PIIAnalysisResult(
        text=text,
        language=cfg.language,
        score_threshold=cfg.score_threshold,
        findings=tuple(_finding_from_presidio_result(text, item) for item in ordered),
    )


def mask_pii(
    text: str,
    *,
    config: PresidioConfig | None = None,
    analyzer: AnalyzerLike | None = None,
    anonymizer: AnonymizerLike | None = None,
) -> PIIMaskResult:
    """Return Presidio's anonymized text plus normalized findings."""

    cfg = config or PresidioConfig()
    if not text:
        analysis = analyze_pii(text, config=cfg, analyzer=analyzer)
        return PIIMaskResult(original_text=text, masked_text=text, analysis=analysis)

    engine = analyzer or _build_default_analyzer()
    raw_results = _sort_presidio_results(
        engine.analyze(
            text=text,
            language=cfg.language,
            entities=cfg.normalized_entities(),
            score_threshold=cfg.score_threshold,
        )
    )
    findings = tuple(_finding_from_presidio_result(text, item) for item in raw_results)
    analysis = PIIAnalysisResult(
        text=text,
        language=cfg.language,
        score_threshold=cfg.score_threshold,
        findings=findings,
    )
    if not raw_results:
        return PIIMaskResult(original_text=text, masked_text=text, analysis=analysis)

    anon = anonymizer or _build_default_anonymizer()
    anonymized = anon.anonymize(text=text, analyzer_results=raw_results)
    masked_text = str(getattr(anonymized, "text", text))
    return PIIMaskResult(
        original_text=text,
        masked_text=masked_text,
        analysis=analysis,
    )
