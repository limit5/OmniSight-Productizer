"""SC.12.2 -- opt-in PII auto-mask helpers for logs / responses.

These helpers sit one layer above ``pii_presidio.mask_pii``: callers pass a
string, log event dict, or JSON-like response payload and receive a masked copy
plus the PII entity types that fired.  They deliberately do not install a
global logging processor or FastAPI middleware; integration points stay
explicit so audit/forensic paths can opt out when they must retain evidence.

Module-global / cross-worker state audit: defaults are immutable tuple data and
helpers create no singleton analyzer, anonymizer, or cache.  Each uvicorn worker
derives the same traversal policy from source code; Presidio engines are still
built per call unless injected by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .pii_presidio import (
    AnalyzerLike,
    AnonymizerLike,
    PIIMaskResult,
    PresidioConfig,
    mask_pii,
)


DEFAULT_MAX_MASK_DEPTH = 8


@dataclass(frozen=True)
class AutoMaskResult:
    """Masked payload plus normalized PII metadata."""

    value: Any
    entity_types: tuple[str, ...]
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "entity_types": list(self.entity_types),
            "value": self.value,
        }


def _dedupe_entity_types(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


def mask_log_text(
    text: object,
    *,
    config: PresidioConfig | None = None,
    analyzer: AnalyzerLike | None = None,
    anonymizer: AnonymizerLike | None = None,
) -> AutoMaskResult:
    """Mask one log/response string and return changed/entity metadata."""

    if text is None:
        return AutoMaskResult(value="", entity_types=(), changed=False)
    raw = str(text)
    result: PIIMaskResult = mask_pii(
        raw,
        config=config,
        analyzer=analyzer,
        anonymizer=anonymizer,
    )
    return AutoMaskResult(
        value=result.masked_text,
        entity_types=result.analysis.entity_types,
        changed=result.changed,
    )


def mask_log_payload(
    payload: Any,
    *,
    config: PresidioConfig | None = None,
    analyzer: AnalyzerLike | None = None,
    anonymizer: AnonymizerLike | None = None,
    max_depth: int = DEFAULT_MAX_MASK_DEPTH,
) -> AutoMaskResult:
    """Return a masked copy of a JSON-like log event payload.

    Strings are passed through Presidio masking.  Mappings, lists, and tuples
    are traversed recursively.  Non-string scalar values are returned as-is so
    callers can hand the result directly to structured logging sinks.
    """

    return _mask_payload(
        payload,
        config=config,
        analyzer=analyzer,
        anonymizer=anonymizer,
        max_depth=max_depth,
        depth=0,
    )


def mask_response_payload(
    payload: Any,
    *,
    config: PresidioConfig | None = None,
    analyzer: AnalyzerLike | None = None,
    anonymizer: AnonymizerLike | None = None,
    max_depth: int = DEFAULT_MAX_MASK_DEPTH,
) -> AutoMaskResult:
    """Return a masked copy of a JSON-like API response payload."""

    return mask_log_payload(
        payload,
        config=config,
        analyzer=analyzer,
        anonymizer=anonymizer,
        max_depth=max_depth,
    )


def _mask_payload(
    payload: Any,
    *,
    config: PresidioConfig | None,
    analyzer: AnalyzerLike | None,
    anonymizer: AnonymizerLike | None,
    max_depth: int,
    depth: int,
) -> AutoMaskResult:
    if isinstance(payload, str):
        return mask_log_text(
            payload,
            config=config,
            analyzer=analyzer,
            anonymizer=anonymizer,
        )
    if depth >= max_depth:
        return AutoMaskResult(value=payload, entity_types=(), changed=False)
    if isinstance(payload, Mapping):
        changed = False
        entity_types: list[str] = []
        masked: dict[Any, Any] = {}
        for key, value in payload.items():
            item = _mask_payload(
                value,
                config=config,
                analyzer=analyzer,
                anonymizer=anonymizer,
                max_depth=max_depth,
                depth=depth + 1,
            )
            masked[key] = item.value
            changed = changed or item.changed
            entity_types.extend(item.entity_types)
        return AutoMaskResult(
            value=masked,
            entity_types=_dedupe_entity_types(entity_types),
            changed=changed,
        )
    if isinstance(payload, list):
        changed = False
        entity_types = []
        masked_list = []
        for value in payload:
            item = _mask_payload(
                value,
                config=config,
                analyzer=analyzer,
                anonymizer=anonymizer,
                max_depth=max_depth,
                depth=depth + 1,
            )
            masked_list.append(item.value)
            changed = changed or item.changed
            entity_types.extend(item.entity_types)
        return AutoMaskResult(
            value=masked_list,
            entity_types=_dedupe_entity_types(entity_types),
            changed=changed,
        )
    if isinstance(payload, tuple):
        changed = False
        entity_types = []
        masked_items = []
        for value in payload:
            item = _mask_payload(
                value,
                config=config,
                analyzer=analyzer,
                anonymizer=anonymizer,
                max_depth=max_depth,
                depth=depth + 1,
            )
            masked_items.append(item.value)
            changed = changed or item.changed
            entity_types.extend(item.entity_types)
        return AutoMaskResult(
            value=tuple(masked_items),
            entity_types=_dedupe_entity_types(entity_types),
            changed=changed,
        )
    return AutoMaskResult(value=payload, entity_types=(), changed=False)


__all__ = [
    "AutoMaskResult",
    "DEFAULT_MAX_MASK_DEPTH",
    "mask_log_payload",
    "mask_log_text",
    "mask_response_payload",
]
