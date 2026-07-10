"""Legacy learned-item promotion is RETIRED (Phase U4 step-0b).

HISTORY: the step-0 interlock (OP-2564) made the two legacy FS-writing promote
endpoints — ``routers/auto_skills.py::promote_auto_skill`` and
``routers/skills.py`` ``/pending/{name}/promote`` — human-only + deny-by-default,
closing the live hole where any api-key principal (``role="admin"`` per auth.py)
could promote arbitrary Markdown into the injected ``configs/skills/`` prompt set
with no eval. But that gate was human-OVERRIDEABLE: setting
``OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED=true`` reactivated the legacy UNGATED
writers (audit finding, 2026-07-10).

U4-0b hard-closes that: the legacy promote path is now **UNCONDITIONALLY denied
(HTTP 410 Gone), independent of any flag or principal**. Promotion of a learned
item into the injected set happens ONLY via the U4 canonical gated publisher
(eval-passed + human-approved, materialized DB-view live set) — a NEW path that
does not reuse these endpoints. Only that publisher may ever replace this denial.
See docs/design/2026-07-10-phase-u4-a0-contract-freeze.md (§G1, §G7).
"""
from __future__ import annotations

import os

from fastapi import HTTPException

from backend import auth

# The kill-switch flag for the FUTURE U4 canonical publisher / snapshot delivery
# (freeze §G7). It NO LONGER gates the retired legacy endpoints — those are
# unconditionally denied by ``assert_promotion_allowed`` regardless of this flag.
_ENABLE_ENV = "OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}


def promotion_enabled() -> bool:
    """Kill-switch for the FUTURE U4 canonical publisher / snapshot delivery
    (freeze §G7). It does NOT gate the retired legacy promote endpoints."""
    return (os.environ.get(_ENABLE_ENV) or "").strip().lower() in _TRUTHY


def assert_promotion_allowed(user: auth.User) -> None:
    """RETIRED (U4-0b): the legacy FS-writing promote path is unconditionally
    unavailable — always raises HTTP 410, independent of flag or principal.

    Promotion is only via the U4 canonical eval-gated, human-approved publisher.
    The ``user`` argument is retained for call-site compatibility (both legacy
    endpoints already call this chokepoint) but is not consulted: retirement is
    unconditional, so no flag or role can re-open the ungated legacy writers.
    """
    del user  # unconditional retirement — principal is irrelevant
    raise HTTPException(
        status_code=410,
        detail={
            "error": "legacy_promotion_retired",
            "reason": (
                "the legacy learned-item promote endpoints are retired (U4-0b); "
                "promotion is only via the U4 canonical eval-gated, human-approved "
                "publisher. This denial is unconditional — no flag re-opens it."
            ),
        },
    )
