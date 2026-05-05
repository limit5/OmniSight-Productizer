"""R0 (#306) — PEP Gateway router.

Read-side endpoints for the PEP Live Feed panel + status snapshot
for operators. Approve / Reject of HELD tool calls rides the
existing ``/decisions/{id}/approve`` and ``/decisions/{id}/reject``
surface from :mod:`backend.routers.decisions` — the ``decision_id``
returned alongside each HELD entry is exactly what the frontend
should POST to.

Endpoints
=========

* ``GET /pep/live``        — recent decisions (ring) + HELD queue + stats.
* ``GET /pep/decisions``   — paginated recent decisions.
* ``GET /pep/held``        — HELD queue only (for the header chip).
* ``GET /pep/policy``      — active tier whitelists + rule counts.
* ``GET /pep/status``      — circuit breaker snapshot.
* ``POST /pep/breaker/reset`` — operator reset for the breaker (admin).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend import auth as _au
from backend import pep_gateway as _pep

router = APIRouter(prefix="/pep", tags=["pep"])


@router.get("/live")
async def pep_live(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    return {
        "recent": _pep.recent_decisions(limit=limit),
        "held": _pep.held_snapshot(),
        "stats": _pep.stats(),
        "breaker": _pep.breaker_status(),
    }


@router.get("/decisions")
async def pep_decisions(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    items = _pep.recent_decisions(limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/held")
async def pep_held() -> dict[str, Any]:
    items = _pep.held_snapshot()
    return {"items": items, "count": len(items)}


@router.get("/policy")
async def pep_policy() -> dict[str, Any]:
    return {
        "tiers": {
            "t1": sorted(_pep.tier_whitelist("t1")),
            "t2": sorted(_pep.tier_whitelist("t2")),
            "t3": sorted(_pep.tier_whitelist("t3")),
        },
        "destructive_rule_count": len(_pep._DESTRUCTIVE_RULES),
        "prod_hold_rule_count": len(_pep._PROD_HOLD_RULES),
        "destructive_rules": [name for name, _ in _pep._DESTRUCTIVE_RULES],
        "prod_hold_rules": [name for name, _ in _pep._PROD_HOLD_RULES],
        "guild_policy_matrix": _pep.guild_policy_matrix(),
    }


@router.get("/status")
async def pep_status() -> dict[str, Any]:
    return {
        "breaker": _pep.breaker_status(),
        "stats": _pep.stats(),
        "held_count": len(_pep.held_snapshot()),
    }


@router.post("/breaker/reset")
async def pep_breaker_reset(_user=Depends(_au.require_operator)) -> dict[str, Any]:
    _pep.reset_breaker()
    return {"ok": True, "breaker": _pep.breaker_status()}
