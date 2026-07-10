"""Emergency promotion interlock (Phase U4 step 0).

Deny-by-default + human-only gate on EVERY runtime path that promotes a
"learned item" (an auto-distilled skill or a pending skill candidate) into
the injected ``configs/skills/`` prompt set (and its vector-memory sink).

WHY THIS EXISTS (verified live hole, 2026-07-10):
  * ``auth.py`` assigns ``role="admin"`` to any valid API-key bearer, so the
    ``require_admin`` dependency on the promote endpoints does NOT stop a
    bot / service principal — an api-key (incl. a prompt-injected runner)
    could promote arbitrary Markdown into the GLOBAL prompt set with no eval.
  * There are two such write endpoints (``routers/auto_skills.py`` DB-row
    promote and ``routers/skills.py`` ``/pending/{name}/promote``), and the
    Decision-Engine ``skill/promote`` proposal is ``severity=routine`` (auto-
    resolves in supervised+).
  * Promotion of self-distilled guidance into future agent prompts is exactly
    the memory-poisoning surface Phase U's eval gate is meant to govern. Until
    that gate exists, promotion must NOT happen automatically and must NEVER be
    reachable by a non-human principal.

This module is the single chokepoint both endpoints call. The eventual U4
``publish_learned_item_version()`` supersedes it; until then this closes the
hole deny-by-default. See docs/design/2026-07-10-phase-u4-eval-gated-promotion-design.md §F.
"""
from __future__ import annotations

import os

from fastapi import HTTPException

from backend import auth
from backend.api.release_approval import _assert_human_operator

# Operators flip this ON deliberately (a human, after weighing the item) once
# they accept the pre-U4 risk; default-OFF means the injected set cannot grow
# by default. Once U4's eval gate lands, promotion is gated on eval-PASS +
# an approved human-only proposal instead of this coarse flag.
_ENABLE_ENV = "OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}


def promotion_enabled() -> bool:
    """True only when an operator has explicitly enabled promotion."""
    return (os.environ.get(_ENABLE_ENV) or "").strip().lower() in _TRUTHY


def assert_promotion_allowed(user: auth.User) -> None:
    """Gate a learned-item promotion. Raises HTTPException(403) if refused.

    Two independent checks (defense in depth):
      1. HUMAN-ONLY — rejects api-key / bot / ai-* principals even though they
         hold ``role="admin"`` (``require_admin`` is not enough on its own).
      2. DENY-BY-DEFAULT — refuses unless an operator has explicitly enabled
         promotion, so the injected prompt set cannot grow without a
         deliberate human act while the U4 eval gate is being built.
    """
    _assert_human_operator(user)
    if not promotion_enabled():
        raise HTTPException(
            status_code=403,
            detail={
                "error": "promotion_disabled",
                "reason": (
                    "learned-item promotion is deny-by-default until the U4 "
                    "eval-gated promotion substrate exists; set "
                    f"{_ENABLE_ENV}=true to override (human-only, deliberate)."
                ),
            },
        )
