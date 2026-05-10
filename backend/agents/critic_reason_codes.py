"""B4 (OP-833) — Critic verdict reason-code enum.

Source-of-truth for the closed set of reason codes the pre-commit critic
may emit. Imported by ``critic_agent`` (verdict construction) and the
test suite (parametrised case enumeration).
"""

from __future__ import annotations

from enum import Enum


class CriticReasonCode(str, Enum):
    """Closed enum per master-plan §2.6 AC #3."""

    MISSING_CONTEXT = "missing_context"
    STYLE_VIOLATION = "style_violation"
    LOGIC_BUG = "logic_bug"
    TEST_INADEQUACY = "test_inadequacy"
    AC_DRIFT = "ac_drift"
    GOVERNANCE_VIOLATION = "governance_violation"
    OTHER = "other"

    @classmethod
    def from_string(cls, raw: str) -> "CriticReasonCode | None":
        try:
            return cls(raw)
        except ValueError:
            return None


VALID_REASON_CODES: frozenset[str] = frozenset(c.value for c in CriticReasonCode)
