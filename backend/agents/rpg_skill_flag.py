"""OP-2505 — RPG.W12 S3 live-activation helper for skill-XP accrual.

Reads the ``OMNISIGHT_RPG_SKILL_XP_ENABLED`` env flag at call time (not at
import time) so an operator flip is picked up on the next call without a
process restart. Truthy values are ``1``/``true``/``yes``/``on``
(case-insensitive, surrounding whitespace stripped); anything else — including
an unset variable — resolves to ``False``.
"""
from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ENV_NAME = "OMNISIGHT_RPG_SKILL_XP_ENABLED"


def skill_xp_enabled() -> bool:
    """Return True iff the RPG skill-XP accrual gate is flipped on."""
    raw = os.environ.get(_ENV_NAME)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_VALUES


__all__ = ["skill_xp_enabled"]
