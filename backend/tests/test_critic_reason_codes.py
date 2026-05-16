"""OP-1283 - unit tests for critic reason-code helpers."""

from __future__ import annotations

import pytest

from backend.agents.critic_reason_codes import VALID_REASON_CODES, CriticReasonCode


@pytest.mark.parametrize("reason_code", tuple(CriticReasonCode))
def test_from_string_accepts_every_reason_code(reason_code: CriticReasonCode) -> None:
    assert CriticReasonCode.from_string(reason_code.value) is reason_code


@pytest.mark.parametrize("raw", ["", "logic-bug", "LOGIC_BUG", "unknown"])
def test_from_string_returns_none_for_unknown_reason_codes(raw: str) -> None:
    assert CriticReasonCode.from_string(raw) is None


def test_reason_codes_compare_as_strings() -> None:
    assert CriticReasonCode.LOGIC_BUG == "logic_bug"


def test_valid_reason_codes_matches_enum_values() -> None:
    assert VALID_REASON_CODES == frozenset(
        reason_code.value for reason_code in CriticReasonCode
    )
