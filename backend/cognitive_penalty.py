"""BP.H.1 — CI report to cognitive-penalty warning prompt.

This row is the agent-facing middle tier in Blueprint Phase H:
CI hard rejection already marks a bad patch ``Verified -1``; this
module turns the same CI report into a compact warning block that the
next agent turn can append to its prompt before retrying.

The module mirrors the pure projection shape used by
``backend.web.vite_error_prompt``: parse a structured history/report
input, render one deterministic prompt block, and return ``""`` when
there is no warning to surface.

Module-global state audit (SOP Step 1)
--------------------------------------
Only immutable constants live at module scope. There is no singleton,
cache, or mutable in-memory state; every worker derives the same prompt
from the same CI report input (qualified answer #1).

Read-after-write timing audit
-----------------------------
N/A — pure projection from caller-provided report data to strings. This
module does not read or write DB / Redis / files and has no async timing
surface.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field


__all__ = [
    "COGNITIVE_PENALTY_SECTION_HEADER",
    "COGNITIVE_PENALTY_WARNING_PREFIX",
    "MAX_COGNITIVE_PENALTY_FAILURES",
    "MAX_COGNITIVE_PENALTY_PROMPT_BYTES",
    "CognitivePenaltyFailure",
    "CognitivePenaltyReport",
    "apply_cognitive_penalty_prompt",
    "build_cognitive_penalty_prompt",
    "parse_ci_report",
    "parse_ci_report_json",
]


COGNITIVE_PENALTY_SECTION_HEADER: str = "# Cognitive Penalty (CI)"
COGNITIVE_PENALTY_WARNING_PREFIX: str = (
    "CI 驗證失敗，上一個 patch 已被標記 Verified -1。"
)
MAX_COGNITIVE_PENALTY_FAILURES: int = 5
MAX_COGNITIVE_PENALTY_PROMPT_BYTES: int = 4096
_UNKNOWN_TEST: str = "<unknown-test>"
_UNKNOWN_MESSAGE: str = "<unknown failure>"


class CognitivePenaltyFailure(BaseModel):
    """Single CI failure distilled for prompt feedback."""

    model_config = ConfigDict(frozen=True)

    nodeid: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)


class CognitivePenaltyReport(BaseModel):
    """Frozen prompt-ready view of a CI report."""

    model_config = ConfigDict(frozen=True)

    status: Literal["pass", "fail", "unknown"]
    verified_label: Literal["Verified +1", "Verified -1", "Verified 0"]
    tests_run: int = Field(..., ge=0)
    tests_failed: int = Field(..., ge=0)
    tests_passed: int = Field(..., ge=0)
    tests_skipped: int = Field(..., ge=0)
    failures: tuple[CognitivePenaltyFailure, ...] = ()

    @property
    def should_warn(self) -> bool:
        return self.verified_label == "Verified -1"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    cut = max_bytes
    while cut > 0 and (encoded[cut] & 0b1100_0000) == 0b1000_0000:
        cut -= 1
    return encoded[:cut].decode("utf-8", errors="ignore")


def _clean_text(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = " ".join(str(value).split())
    return text or fallback


def _int_from_summary(summary: Mapping[str, Any], key: str) -> int:
    try:
        value = int(summary.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _failure_message(test: Mapping[str, Any]) -> str:
    call = test.get("call")
    if isinstance(call, Mapping):
        crash = call.get("crash")
        if isinstance(crash, Mapping):
            message = _clean_text(crash.get("message"), "")
            if message:
                return message
        longrepr = _clean_text(call.get("longrepr"), "")
        if longrepr:
            return longrepr
    return _clean_text(test.get("message") or test.get("longrepr"), _UNKNOWN_MESSAGE)


def _extract_failures(tests: Any) -> tuple[CognitivePenaltyFailure, ...]:
    if not isinstance(tests, Sequence) or isinstance(tests, (str, bytes, bytearray)):
        return ()
    failures: list[CognitivePenaltyFailure] = []
    for item in tests:
        if not isinstance(item, Mapping):
            continue
        outcome = _clean_text(item.get("outcome"), "").lower()
        if outcome not in {"failed", "error"}:
            continue
        failures.append(
            CognitivePenaltyFailure(
                nodeid=_clean_text(item.get("nodeid"), _UNKNOWN_TEST),
                message=_failure_message(item),
            )
        )
        if len(failures) >= MAX_COGNITIVE_PENALTY_FAILURES:
            break
    return tuple(failures)


def parse_ci_report(report: Mapping[str, Any]) -> CognitivePenaltyReport:
    """Parse a pytest-json-report/GitHub summary-shaped CI report.

    The accepted input intentionally matches the local reporter shape in
    ``scripts/report_live_test_status.py``: ``summary`` carries counts
    and ``tests`` carries pytest node failures. Unknown or degraded
    counts are normalised to zero so prompt assembly never crashes.
    """

    summary_obj = report.get("summary", {})
    summary: Mapping[str, Any] = summary_obj if isinstance(summary_obj, Mapping) else {}
    passed = _int_from_summary(summary, "passed")
    failed = _int_from_summary(summary, "failed")
    skipped = _int_from_summary(summary, "skipped")
    total = _int_from_summary(summary, "total")
    tests_run = max(total, passed + failed + skipped)
    failures = _extract_failures(report.get("tests", ()))

    raw_status = _clean_text(report.get("status"), "").lower()
    if failed > 0 or failures or raw_status == "fail":
        status: Literal["pass", "fail", "unknown"] = "fail"
        verified_label: Literal["Verified +1", "Verified -1", "Verified 0"] = (
            "Verified -1"
        )
    elif raw_status == "pass" or tests_run > 0:
        status = "pass"
        verified_label = "Verified +1"
    else:
        status = "unknown"
        verified_label = "Verified 0"

    return CognitivePenaltyReport(
        status=status,
        verified_label=verified_label,
        tests_run=tests_run,
        tests_failed=max(failed, len(failures)),
        tests_passed=passed,
        tests_skipped=skipped,
        failures=failures,
    )


def parse_ci_report_json(raw: str) -> CognitivePenaltyReport:
    """Parse a CI report JSON string, degrading malformed JSON to unknown."""

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, Mapping):
        data = {}
    return parse_ci_report(data)


def build_cognitive_penalty_prompt(report: CognitivePenaltyReport) -> str:
    """Render the warning prompt block for a failed CI report.

    Returns ``""`` for passing or unknown reports so callers can
    unconditionally append the result.
    """

    if not report.should_warn:
        return ""

    lines = [
        COGNITIVE_PENALTY_SECTION_HEADER,
        "",
        COGNITIVE_PENALTY_WARNING_PREFIX,
        (
            f"測試摘要: run={report.tests_run}, failed={report.tests_failed}, "
            f"passed={report.tests_passed}, skipped={report.tests_skipped}."
        ),
        "請先修正下列 CI failure，再繼續產出新的 patch。",
    ]
    if report.failures:
        lines.append("")
        for idx, failure in enumerate(report.failures, start=1):
            lines.append(f"{idx}. {failure.nodeid}: {failure.message}")
    return _truncate_utf8("\n".join(lines), MAX_COGNITIVE_PENALTY_PROMPT_BYTES)


def apply_cognitive_penalty_prompt(base_prompt: str, report: CognitivePenaltyReport) -> str:
    """Append the cognitive-penalty block to ``base_prompt`` when needed."""

    block = build_cognitive_penalty_prompt(report)
    if not block:
        return base_prompt
    if not base_prompt:
        return block
    return f"{base_prompt.rstrip()}\n\n---\n\n{block}"
