"""Fix-B B1 — shared pagination bounds for list endpoints.

Before: every list endpoint had `limit: int = N` with no upper cap, so
a caller could request `limit=1_000_000` and trigger a memory blow-up
or trip sqlite's quirks. This module offers a ready-made FastAPI
`Query(...)` with sane defaults.

Usage:
    from backend.routers._pagination import Limit
    @router.get("/foo")
    async def list_foo(limit: int = Limit()):
        ...

Customise the default per endpoint if needed:
    limit: int = Limit(default=50, max_cap=200)
"""

from __future__ import annotations

from fastapi import Query

# Global hard ceiling. No list endpoint should ever return more than this
# in one call — the few callers that truly need more must paginate.
HARD_MAX = 500


def Limit(default: int = 100, max_cap: int = HARD_MAX):  # noqa: N802
    return Query(default=default, ge=1, le=min(max_cap, HARD_MAX),
                 description=f"Max rows per page (1..{min(max_cap, HARD_MAX)})")
