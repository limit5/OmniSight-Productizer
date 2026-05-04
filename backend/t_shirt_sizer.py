"""BP.C.1 - T-shirt Gateway sizing (S / M / XL).

The sizer is the first Blueprint Phase C step: it turns an operator's
free-form project request into a closed T-shirt size that downstream
topology work can thread into ``GraphState.size``.

This module deliberately stops at classification. It does not build
topologies, mutate ``GraphState``, or touch router wiring; BP.C.2-BP.C.4
own those surfaces.

Module-global audit (SOP Step 1): module constants are immutable tuples
and compiled regex objects; there is no singleton, cache, or mutable
cross-request state. Every worker derives the same default model list
from its own environment, so no cross-worker coordination is required.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Awaitable, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

TShirtSize = Literal["S", "M", "XL"]
SizerSource = Literal["llm", "heuristic"]
SizerAskFn = Callable[[str, str], Awaitable[tuple[str, int]]]

DEFAULT_SIZER_MODELS: tuple[str, ...] = tuple(
    m.strip()
    for m in os.environ.get(
        "OMNISIGHT_T_SHIRT_SIZER_MODELS",
        "anthropic/claude-haiku-4-5-20251001,ollama/gemma4:e4b",
    ).split(",")
    if m.strip()
)

_VALID_SIZES: tuple[str, ...] = ("S", "M", "XL")
_LLM_CONFIDENCE_FLOOR = 0.55

_S_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:typo|copy|text|docs?|readme|comment|rename|format|style|"
        r"single[- ]file|one[- ]file|small fix|simple)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:小修|文案|註解|單一檔案|簡單修正|改字|改名)"),
)

_XL_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:multi[- ]tenant|distributed|microservice|migration|"
        r"compliance|security audit|architecture|cross[- ]platform|"
        r"firmware|driver|rtos|kernel|hardware|npu|pipeline|"
        r"end[- ]to[- ]end|full[- ]stack|ha|failover)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:多租戶|分散式|微服務|遷移|合規|資安|架構|韌體|驅動|硬體|端到端)"),
)

_LLM_SYSTEM_PROMPT = """You are the OmniSight Gateway sizing agent.
Classify the user's project request into exactly one T-shirt size:

S  = Fast-track: simple, local, one specialist lane, narrow file or doc work.
M  = Standard DAG: normal product work, several tasks, one product surface.
XL = Fractal matrix: cross-subsystem, multi-service, hardware/firmware,
     compliance/security-sensitive, migration, HA, or broad integration work.

Return ONLY one JSON object, no markdown and no prose:
{
  "size": "S|M|XL",
  "confidence": 0.0,
  "rationale": "short reason"
}

Rules:
- Prefer M when the request is underspecified or borderline.
- Use XL only when the work clearly spans subsystems, platforms, or risk domains.
- Use S only when the request is obviously narrow and low-risk.
- Never emit a size outside S, M, XL."""


class TShirtSizingReport(BaseModel):
    """Immutable result of one BP.C.1 sizing pass."""

    model_config = ConfigDict(frozen=True)

    size: TShirtSize = Field(
        ...,
        description="Closed S/M/XL topology size selected for the request.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Classifier confidence, clamped into 0.0..1.0.",
    )
    rationale: str = Field(
        "",
        description="Short operator-facing reason for the selected size.",
    )
    model: str = Field(
        "",
        description="Model that produced the decision; empty for heuristic fallback.",
    )
    tokens_used: int = Field(
        0,
        ge=0,
        description="Best-effort LLM token count returned by the ask function.",
    )
    source: SizerSource = Field(
        ...,
        description="Decision source: LLM response or local heuristic fallback.",
    )


class TShirtSizerParseError(RuntimeError):
    """Raised when an LLM response cannot be parsed into a valid report."""


def build_sizing_prompt(request_text: str) -> str:
    """Build the deterministic Haiku/Gemma sizing prompt."""
    return f"{_LLM_SYSTEM_PROMPT}\n\n---\n\nUSER REQUEST:\n{request_text}"


async def size_project(
    request_text: str,
    *,
    ask_fn: SizerAskFn | None = None,
    models: Sequence[str] | None = None,
) -> TShirtSizingReport:
    """Classify *request_text* as ``S``, ``M``, or ``XL``.

    Production wiring uses ``iq_runner.live_ask_fn`` lazily. Tests pass a
    deterministic ``ask_fn`` and model list, mirroring the existing DAG
    splitter pattern in ``backend.orchestrator_gateway``.
    """
    text = (request_text or "").strip()
    selected_models = tuple(models) if models is not None else DEFAULT_SIZER_MODELS
    if ask_fn is None:
        ask_fn = _default_ask_fn

    if text and selected_models:
        prompt = build_sizing_prompt(text)
        for model in selected_models:
            try:
                raw, tokens = await ask_fn(model, prompt)
                if not raw:
                    continue
                report = parse_sizer_response(raw, model=model, tokens_used=tokens)
                if report.confidence >= _LLM_CONFIDENCE_FLOOR:
                    return report
                logger.debug(
                    "t_shirt_sizer: low-confidence response from %s: %.2f",
                    model,
                    report.confidence,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("t_shirt_sizer: model %s failed: %s", model, exc)

    return heuristic_size_project(text)


async def _default_ask_fn(model: str, prompt: str) -> tuple[str, int]:
    """Live LLM hook, lazy-imported so imports stay network-free."""
    try:
        from backend.iq_runner import live_ask_fn
    except Exception as exc:  # pragma: no cover - environment-specific
        logger.warning("t_shirt_sizer: live_ask_fn unavailable: %s", exc)
        return ("", 0)
    return await live_ask_fn(model, prompt)


def parse_sizer_response(
    raw: str,
    *,
    model: str = "",
    tokens_used: int = 0,
) -> TShirtSizingReport:
    """Parse a strict or lightly wrapped JSON sizer response."""
    data = _parse_json_object(raw)
    size = str(data.get("size") or "").strip().upper()
    if size not in _VALID_SIZES:
        raise TShirtSizerParseError(f"invalid size: {size!r}")

    confidence = _clamp_confidence(data.get("confidence"), default=0.0)
    rationale = str(data.get("rationale") or "").strip()
    return TShirtSizingReport(
        size=size,  # type: ignore[arg-type]
        confidence=confidence,
        rationale=rationale,
        model=model,
        tokens_used=max(0, int(tokens_used or 0)),
        source="llm",
    )


def heuristic_size_project(request_text: str) -> TShirtSizingReport:
    """Conservative LLM-free fallback.

    The fallback intentionally defaults to M for empty or mixed signals,
    preserving the legacy standard-DAG behaviour until BP.C.4 wires the
    feature flag into production traffic.
    """
    text = request_text or ""
    s_hits = sum(1 for pat in _S_SIGNALS if pat.search(text))
    xl_hits = sum(1 for pat in _XL_SIGNALS if pat.search(text))

    if xl_hits > s_hits:
        return TShirtSizingReport(
            size="XL",
            confidence=0.62,
            rationale="Broad or risk-sensitive project signals matched.",
            source="heuristic",
        )
    if s_hits > 0 and xl_hits == 0:
        return TShirtSizingReport(
            size="S",
            confidence=0.62,
            rationale="Narrow low-risk change signals matched.",
            source="heuristic",
        )
    return TShirtSizingReport(
        size="M",
        confidence=0.5 if text.strip() else 0.0,
        rationale="Defaulting to standard DAG for underspecified or mixed signals.",
        source="heuristic",
    )


def _parse_json_object(raw: str) -> dict:
    """Tolerate fences/prose but require the enclosed value to be an object."""
    if not raw or not raw.strip():
        raise TShirtSizerParseError("empty response")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise TShirtSizerParseError(f"no JSON object in response: {text[:120]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise TShirtSizerParseError(f"json decode failed: {exc}") from exc
    if not isinstance(data, dict):
        raise TShirtSizerParseError("response JSON is not an object")
    return data


def _clamp_confidence(value: object, *, default: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, confidence))


__all__ = [
    "DEFAULT_SIZER_MODELS",
    "TShirtSize",
    "TShirtSizingReport",
    "TShirtSizerParseError",
    "SizerAskFn",
    "build_sizing_prompt",
    "heuristic_size_project",
    "parse_sizer_response",
    "size_project",
]
