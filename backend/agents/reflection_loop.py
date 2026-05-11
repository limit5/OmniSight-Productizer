"""B12 — Reflection loop on test/lint failure (OP-850, master plan §2.14).

Bounded retries when the B2 (lint, OP-829) or B5 (test, OP-834) surfaces
report unresolved failures after a clean model exit. The reflection loop
is TASK-LEVEL (whole-turn retry with a structured failure payload),
distinct from B3's CALL-LEVEL loop detection on tool triples — see
``docs/sop/lessons/L-OP-843.md`` (disambiguation discipline).

AC mapping (full description on OP-850):

* AC #1 — ``ReflectionInput`` produces ``{failure_type, file, line,
  expected, actual, traceback}``.
* AC #2 — ``ReflectionInput.to_user_turn()`` formats the payload for
  injection into the next user message.
* AC #3 — ``ReflectionCounter`` caps each failure type at
  ``REFLECTION_LIMIT`` (= 3) separately from the main ``max_iterations``.
* AC #4 — Each reflection counts at ``REFLECTION_HALF_WEIGHT`` (= 0.5)
  against the main ``max_iterations`` via
  ``ReflectionCounter.adjusted_max_iterations()``.
* AC #5 — Reuses B3 (``loop_detector.RESET_LIMIT``) for the cap constant
  and the JSONL append-only persistence pattern from
  ``tom_scratchpad.ToMScratchpad`` for cross-reset survival.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from backend.agents.loop_detector import RESET_LIMIT
from backend.agents.static_analysis_gate import StaticAnalysisResult


FAILURE_TYPE_TEST: str = "test"
FAILURE_TYPE_LINT: str = "lint"
VALID_FAILURE_TYPES: frozenset[str] = frozenset({FAILURE_TYPE_TEST, FAILURE_TYPE_LINT})

# AC #3 / B3 reuse: the sprint charter pins N=3 for both B3 resets and B12
# reflections — keep them coupled so a future re-baseline moves both
# levers together.
REFLECTION_LIMIT: int = RESET_LIMIT

# AC #4: each reflection consumes 0.5 of the main ``max_iterations``
# budget, so 3 reflections cost only 1.5 iterations — that way the
# per-type cap (3 reflections) is the binding limit, not the main cap.
REFLECTION_HALF_WEIGHT: float = 0.5

# JSONL discriminator so the persistence file can be multiplexed with
# other components (mirrors ``tom_scratchpad.ENTRY_TYPE`` discipline).
PERSISTENCE_ENTRY_TYPE: str = "reflection_loop"

# Error catalog (AC subset from spec): used by the orchestrator to log a
# distinct surrender reason when the cap is exhausted vs. the inner B3
# loop terminating.
ERROR_CAP_EXCEEDED: str = "reflection_cap_exceeded"
ERROR_INVALID_FAILURE_TYPE: str = "reflection_invalid_failure_type"


@dataclass(frozen=True)
class ReflectionInput:
    """Structured failure object injected as ``reflection_input`` (AC #1)."""

    failure_type: str
    file: str
    line: int
    expected: str
    actual: str
    traceback: str

    def __post_init__(self) -> None:
        if self.failure_type not in VALID_FAILURE_TYPES:
            raise ValueError(
                f"{ERROR_INVALID_FAILURE_TYPE}: got {self.failure_type!r}, "
                f"expected one of {sorted(VALID_FAILURE_TYPES)}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "failure_type": self.failure_type,
            "file": self.file,
            "line": self.line,
            "expected": self.expected,
            "actual": self.actual,
            "traceback": self.traceback,
        }

    def to_user_turn(self) -> str:
        """Render as the user-turn block per AC #2.

        Wraps the payload in a fenced JSON block so the model parses it
        verbatim rather than treating it as prose — same shape B3 uses
        for its reset paragraph (single canonical record + instruction).
        """
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        return (
            "reflection_input (B12 / OP-850): the previous attempt left an "
            f"unresolved {self.failure_type} failure. Address it now.\n"
            "```json\n"
            f"{payload}\n"
            "```\n"
            "Re-run the verifier (pytest for test failures, the linter for "
            "lint failures) before signing off. Do not re-emit ✅ ITEM_DONE "
            "until the verifier exits clean."
        )


def build_lint_reflection_input(result: StaticAnalysisResult) -> ReflectionInput | None:
    """Adapt a B2 ``StaticAnalysisResult`` into ``ReflectionInput`` per AC #1.

    ``None`` when ``result.is_clean`` so callers can ``if rv:`` cheaply.
    Uses the first diagnostic for the head fields and concatenates all
    diagnostics into ``traceback`` so the model sees the full picture
    (not just one of many).
    """
    if result.is_clean:
        return None
    primary = result.diagnostics[0]
    traceback_lines = [
        f"{d.file}:{d.line}:{d.col}: {d.code}: {d.message}"
        for d in result.diagnostics
    ]
    return ReflectionInput(
        failure_type=FAILURE_TYPE_LINT,
        file=primary.file,
        line=primary.line,
        expected="clean lint (no diagnostics)",
        actual=f"{primary.code}: {primary.message}",
        traceback="\n".join(traceback_lines),
    )


def build_test_reflection_input(
    *,
    file: str,
    line: int,
    expected: str,
    actual: str,
    traceback: str,
) -> ReflectionInput:
    """Adapt B5 test-failure fields into ``ReflectionInput`` per AC #1.

    Caller extracts file / line from the test framework output
    (e.g. pytest's ``FAILED test_x.py::test_y - AssertionError`` plus the
    captured ``test_x.py:42:`` line) and passes the assertion's expected
    vs. actual along with the full traceback.
    """
    return ReflectionInput(
        failure_type=FAILURE_TYPE_TEST,
        file=file,
        line=line,
        expected=expected,
        actual=actual,
        traceback=traceback,
    )


class ReflectionCounter:
    """Per-ticket reflection counter (AC #3, #4, #5).

    Separate counter per ``failure_type`` so a single failing surface
    cannot starve the other (AC #3: "counted separately"). Aggregate
    ``half_weight_consumed`` lets callers compute the adjusted
    ``max_iterations`` budget (AC #4). Optional JSONL persistence path
    mirrors B3's ``ToMScratchpad`` so the counter survives runner
    restart / context reset (AC #5).
    """

    def __init__(
        self,
        *,
        ticket_key: str,
        persistence_path: str | Path | None = None,
    ) -> None:
        self.ticket_key = ticket_key
        self.persistence_path: Path | None = (
            Path(persistence_path) if persistence_path is not None else None
        )
        self._counts: dict[str, int] = {ft: 0 for ft in VALID_FAILURE_TYPES}

    def can_reflect(self, failure_type: str) -> bool:
        self._validate_type(failure_type)
        return self._counts[failure_type] < REFLECTION_LIMIT

    def is_terminal(self, failure_type: str) -> bool:
        self._validate_type(failure_type)
        return self._counts[failure_type] >= REFLECTION_LIMIT

    def record_reflection(self, failure_type: str) -> int:
        """Increment the per-type counter and persist; returns new count.

        Raises ``RuntimeError`` with ``ERROR_CAP_EXCEEDED`` if the cap was
        already reached — the orchestrator is expected to gate via
        ``can_reflect()`` first; this is a defensive trap.
        """
        self._validate_type(failure_type)
        if not self.can_reflect(failure_type):
            raise RuntimeError(
                f"{ERROR_CAP_EXCEEDED}: failure_type={failure_type} already at "
                f"{REFLECTION_LIMIT} reflections (AC #3)"
            )
        self._counts[failure_type] += 1
        self._persist(failure_type)
        return self._counts[failure_type]

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def total_reflections(self) -> int:
        return sum(self._counts.values())

    @property
    def half_weight_consumed(self) -> float:
        """Total reflections × 0.5 — to subtract from main max_iterations."""
        return self.total_reflections * REFLECTION_HALF_WEIGHT

    def adjusted_max_iterations(self, main_max_iterations: int) -> int:
        """Effective ``max_iterations`` after half-weight reflection debt.

        Floors at 1: in practice the per-type cap fires first (AC #3),
        but we never want to hand the SDK a non-positive iteration budget.
        """
        return max(1, int(main_max_iterations - self.half_weight_consumed))

    def _validate_type(self, failure_type: str) -> None:
        if failure_type not in VALID_FAILURE_TYPES:
            raise ValueError(
                f"{ERROR_INVALID_FAILURE_TYPE}: got {failure_type!r}, "
                f"expected one of {sorted(VALID_FAILURE_TYPES)}"
            )

    def _persist(self, failure_type: str) -> None:
        if self.persistence_path is None:
            return
        record = {
            "type": PERSISTENCE_ENTRY_TYPE,
            "ticket_key": self.ticket_key,
            "failure_type": failure_type,
            "count_after": self._counts[failure_type],
        }
        self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
        with self.persistence_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    @classmethod
    def load(
        cls,
        persistence_path: str | Path,
        *,
        ticket_key: str,
    ) -> "ReflectionCounter":
        """Reconstruct a counter from its JSONL persistence file (AC #5).

        Skips malformed lines (matches ``tom_scratchpad`` robustness) and
        records that name another ticket so a shared file can multiplex.
        Missing file → empty counter (cold start is the common case).
        """
        counter = cls(ticket_key=ticket_key, persistence_path=persistence_path)
        path = Path(persistence_path)
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return counter
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("type") != PERSISTENCE_ENTRY_TYPE:
                continue
            if rec.get("ticket_key") != ticket_key:
                continue
            ft = rec.get("failure_type")
            count_after = rec.get("count_after")
            if ft in VALID_FAILURE_TYPES and isinstance(count_after, int):
                # Append-only log: the highest count_after for a type is
                # the post-replay value (later entries supersede earlier).
                counter._counts[ft] = max(counter._counts[ft], count_after)
        return counter


__all__ = [
    "ERROR_CAP_EXCEEDED",
    "ERROR_INVALID_FAILURE_TYPE",
    "FAILURE_TYPE_LINT",
    "FAILURE_TYPE_TEST",
    "PERSISTENCE_ENTRY_TYPE",
    "REFLECTION_HALF_WEIGHT",
    "REFLECTION_LIMIT",
    "ReflectionCounter",
    "ReflectionInput",
    "VALID_FAILURE_TYPES",
    "build_lint_reflection_input",
    "build_test_reflection_input",
]
