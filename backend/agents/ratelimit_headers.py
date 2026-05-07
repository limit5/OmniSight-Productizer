"""Z.1 provider rate-limit header registry (legacy thin re-export).

The canonical source moved to ``backend.agents.ratelimit_contract`` in
OP-80 (2026-05-07) so Z.1 + MP cannot drift independently. This module
remains as a backwards-compat shim — existing imports of
``_PROVIDER_RATELIMIT_HEADERS`` keep working unchanged.
"""

from __future__ import annotations

# OP-80: re-export the canonical mapping so existing Z.1 callers don't
# break. New code should import from ``ratelimit_contract`` directly.
from backend.agents.ratelimit_contract import (
    PROVIDER_RATELIMIT_HEADER_KEYS as _PROVIDER_RATELIMIT_HEADERS,
)


def provider_ratelimit_family(provider_id: str | None) -> str | None:
    """Return the Z.1 mapping key for an MP provider id, if known."""
    if not provider_id:
        return None
    provider = provider_id.strip().lower()
    if provider in _PROVIDER_RATELIMIT_HEADERS:
        return provider
    if provider.endswith("-subscription"):
        provider = provider[: -len("-subscription")]
        if provider in _PROVIDER_RATELIMIT_HEADERS:
            return provider
    return None


__all__ = [
    "_PROVIDER_RATELIMIT_HEADERS",
    "provider_ratelimit_family",
]
