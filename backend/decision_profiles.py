"""Phase 58 — Decision Profiles.

Layer between MODE (parallelism budget) and individual decisions: a
profile decides *how strict* the auto-resolution is. Four built-in
presets:

  STRICT      every risky+ decision queues for the operator
              (≈ legacy SUPERVISED behaviour)
  BALANCED    risky auto-resolves at confidence ≥ 0.7;
              destructive still queues; suitable for daily use
  AUTONOMOUS  destructive auto-resolves at confidence ≥ 0.85; only
              the irreducible critical_kinds list still queues;
              24h bulk-undo safety net
  GHOST       even critical_kinds auto-resolve with a 5s notice
              countdown; double-gated by env vars
              OMNISIGHT_ALLOW_GHOST_PROFILE=true and
              OMNISIGHT_ENV=staging — otherwise PUT /profile rejects
              the switch.

Persistence: `decision_profiles` table with `enabled=1` on the
current row.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

ProfileId = str  # "STRICT" | "BALANCED" | "AUTONOMOUS" | "GHOST"


@dataclass(frozen=True)
class Profile:
    id: ProfileId
    threshold_risky: float
    threshold_destructive: float
    auto_critical: bool
    description: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "threshold_risky": self.threshold_risky,
            "threshold_destructive": self.threshold_destructive,
            "auto_critical": self.auto_critical,
            "description": self.description,
        }


_BUILTIN: dict[ProfileId, Profile] = {
    "STRICT": Profile(
        id="STRICT",
        threshold_risky=2.0,           # >1.0 → impossible → always queue
        threshold_destructive=2.0,
        auto_critical=False,
        description="Every risky+ decision queues for operator approval (legacy SUPERVISED behaviour).",
    ),
    "BALANCED": Profile(
        id="BALANCED",
        threshold_risky=0.7,
        threshold_destructive=2.0,     # destructive still queues
        auto_critical=False,
        description="Risky auto-resolves at confidence ≥ 0.7; destructive still queues. Daily default.",
    ),
    "AUTONOMOUS": Profile(
        id="AUTONOMOUS",
        threshold_risky=0.5,
        threshold_destructive=0.85,
        auto_critical=False,
        description="Destructive auto-resolves at confidence ≥ 0.85; critical kinds still queue. 24h bulk-undo.",
    ),
    "GHOST": Profile(
        id="GHOST",
        threshold_risky=0.0,
        threshold_destructive=0.0,
        auto_critical=True,            # everything auto except gating
        description="Even critical kinds auto-resolve. Double-gated by env. STAGING ONLY.",
    ),
}


# Critical kinds — always queue regardless of profile, unless
# auto_critical=True (currently only GHOST). Treated as a separate
# allow-list so adding a new critical kind is one line.
CRITICAL_KINDS = {
    "git_push/main",
    "deploy/prod",
    "release/ship",
    "workspace/delete",
    "user/grant_admin",
}


# Fix-B B7: sync-only lock; awaits happen outside. See decision_engine.py.
_state_lock = threading.Lock()
_current: ProfileId = os.environ.get("OMNISIGHT_DEFAULT_PROFILE", "STRICT").strip().upper() or "STRICT"
if _current not in _BUILTIN:
    _current = "STRICT"


def list_profiles() -> list[dict]:
    return [p.to_dict() for p in _BUILTIN.values()]


def get_profile(pid: ProfileId | None = None) -> Profile:
    """Return the named profile or the current one if pid is None."""
    if pid is None:
        with _state_lock:
            pid = _current
    return _BUILTIN.get(pid, _BUILTIN["STRICT"])


def get_current_id() -> ProfileId:
    with _state_lock:
        return _current


class GhostNotAllowed(Exception):
    """Raised when a caller tries to switch to GHOST without the
    double env gate."""


def set_profile(pid: ProfileId) -> Profile:
    """Switch the active profile. Raises ValueError on unknown id;
    GhostNotAllowed when GHOST is requested without the gate."""
    global _current
    pid = pid.strip().upper()
    if pid not in _BUILTIN:
        raise ValueError(f"unknown profile: {pid}")
    if pid == "GHOST":
        if (os.environ.get("OMNISIGHT_ALLOW_GHOST_PROFILE", "").strip().lower() != "true"
                or os.environ.get("OMNISIGHT_ENV", "").strip().lower() != "staging"):
            raise GhostNotAllowed(
                "GHOST profile requires OMNISIGHT_ALLOW_GHOST_PROFILE=true "
                "AND OMNISIGHT_ENV=staging — refused"
            )
    with _state_lock:
        prev = _current
        _current = pid
    logger.info("DecisionProfile: %s → %s", prev, pid)
    # Persist + audit (best effort)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist(pid))
    except RuntimeError:
        pass
    try:
        from backend import audit as _audit
        _audit.log_sync(
            action="profile_change", entity_kind="decision_profile", entity_id="global",
            before={"profile": prev}, after={"profile": pid},
        )
    except Exception:
        pass
    # SSE event
    try:
        from backend.events import bus as _bus
        _bus.publish("profile_changed", {"profile": pid, "previous": prev})
    except Exception as exc:
        logger.warning("profile_changed publish failed: %s", exc)
    return get_profile(pid)


async def _persist(pid: ProfileId) -> None:
    """Persist the newly-active profile AND disable all others.

    Phase-3 Step A.1 (2026-04-21): ported to pool + tx-wrapped. The
    "disable everyone, enable one" pair is a read-modify-write on
    the profile set — under compat a crash between the UPDATE and
    INSERT would leave ALL profiles disabled (0 active), which
    ``load_from_db`` would then treat as "no profile configured"
    on next lifespan restart. The tx makes this all-or-nothing.
    """
    try:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE decision_profiles SET enabled = 0"
                )
                await conn.execute(
                    "INSERT INTO decision_profiles "
                    "(id, threshold_risky, threshold_destructive, "
                    " auto_critical, enabled, description, updated_at) "
                    "VALUES ($1, $2, $3, $4, 1, $5, "
                    "        to_char(clock_timestamp(), "
                    "                 'YYYY-MM-DD HH24:MI:SS')) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  threshold_risky = EXCLUDED.threshold_risky, "
                    "  threshold_destructive = EXCLUDED.threshold_destructive, "
                    "  auto_critical = EXCLUDED.auto_critical, "
                    "  enabled = 1, "
                    "  description = EXCLUDED.description, "
                    "  updated_at = EXCLUDED.updated_at",
                    pid,
                    _BUILTIN[pid].threshold_risky,
                    _BUILTIN[pid].threshold_destructive,
                    1 if _BUILTIN[pid].auto_critical else 0,
                    _BUILTIN[pid].description,
                )
    except Exception as exc:
        logger.warning("profile persist failed: %s", exc)


async def load_from_db() -> Optional[ProfileId]:
    """Restore the current profile from DB at lifespan startup."""
    global _current
    try:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            pid = await conn.fetchval(
                "SELECT id FROM decision_profiles "
                "WHERE enabled = 1 LIMIT 1"
            )
        if pid and pid in _BUILTIN:
            with _state_lock:
                _current = pid
            return pid
    except Exception as exc:
        logger.warning("decision_profiles load failed: %s", exc)
    return None


def _reset_for_tests() -> None:
    global _current
    with _state_lock:
        _current = "STRICT"
