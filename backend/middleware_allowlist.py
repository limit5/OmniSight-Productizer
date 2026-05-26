"""Shared public path allowlist for middleware auth bypass decisions."""

from __future__ import annotations

from typing import Final

# Initial seed: paths confirmed public per current behavior of the 4-of-5
# whitelist agreement (i.e., the 4 middlewares that allow these are correct;
# the 5th is the drift bug that v2-7-2bc will fix). See parent spec doc
# section 4.3 for the migration rationale.
PUBLIC_PATH_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "/livez",
        "/readyz",
        "/healthz",
        "/api/v1/livez",
        "/api/v1/readyz",
        "/api/v1/healthz",
        # /health membership decided in v2-7-FixHealth, NOT here per
        # section 6 of contract spec.
        "/metrics",
        # Image-surfacing /version (Family ⑤ §3, OP-1745). Public,
        # root-mounted, no-auth per §3.1; the live enforcement gate is
        # backend/auth_baseline.py's AUTH_BASELINE_ALLOWLIST, but this
        # seed list is kept in sync per the Family ⑦ allowlist contract
        # (2026-05-16-v2-family7-allowlist-contract.md §4) so the future
        # unified allowlist already carries /version.
        "/version",
        "/openapi.json",
        "/docs",
        "/redoc",
        # WebSocket: see section 5.1 -- not added to allowlist; auth happens
        # via WebSocket subprotocol.
    }
)


def is_public(path: str) -> bool:
    """Return True if ``path`` is in PUBLIC_PATH_ALLOWLIST.

    Match semantics are locked in parent spec section 4.4:
    - Exact match (default)
    - No prefix/glob expansion in v1; future v2 may add prefix support
    - Case-sensitive
    - Trailing slash NOT normalized (caller responsibility)
    """
    return path in PUBLIC_PATH_ALLOWLIST
