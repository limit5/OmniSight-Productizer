"""OP-948 H3 — ``/api/v1/release-conductor/state`` operator query API.

Read-only HTTP surface over :mod:`backend.release_conductor.state_machine`.
The operator dashboard + the H2 webhook dispatchers use this to answer
"what state is ``vX.Y.Z`` in right now, and how did it get there?" in a
single round-trip.

Per ticket AC #5:

* ``GET /api/v1/release-conductor/state?version=vX.Y.Z`` returns the
  current state row + the full append-only transition history.

The router is mounted from ``backend.main`` via
``_include_versioned_router`` so the canonical URL is
``/api/v1/release-conductor/state``. Writes are deliberately out of
scope — the H2 dispatchers call ``state_machine.transition()``
directly; HTTP-side writes would invite cross-process contention with
the in-process dispatchers and complicate the optimistic-locking
contract.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend import auth
from backend.release_conductor import state_machine


log = logging.getLogger(__name__)


router = APIRouter(prefix="/release-conductor", tags=["release-conductor"])


# Permissive SemVer-ish regex: vMAJOR.MINOR.PATCH with optional
# ``-rcN`` / ``-hotfixN`` / ``-alphaN`` style suffix. Mirrors the
# release-template-engine ticket convention (``vX.Y.Z`` / ``vX.Y.Z-rcN``).
_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(?:-[A-Za-z0-9.+-]+)?$")


def _normalise_version(raw: str | None) -> str:
    candidate = (raw or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="version is required")
    if not _VERSION_RE.match(candidate):
        raise HTTPException(
            status_code=400,
            detail="version must look like vX.Y.Z or vX.Y.Z-<suffix>",
        )
    return candidate


@router.get("/state", response_model=None)
async def get_release_state(
    version: str = Query(..., description="Release version (e.g. v0.5.1-rc1)"),
    _user: auth.User = Depends(auth.current_user),
) -> dict[str, Any]:
    """Return the current state + full transition log for ``version``.

    404 if no ``release_state`` row exists yet (the release was never
    instantiated through G1 + the conductor). Authentication is the
    standard backend chain — any logged-in user can read; writes are
    not exposed on this surface.
    """
    version_clean = _normalise_version(version)
    try:
        row = state_machine.get(version=version_clean)
    except state_machine.ReleaseNotFound:
        log.info("release_state.query.not_found version=%s", version_clean)
        raise HTTPException(
            status_code=404,
            detail=f"no release_state row for version={version_clean!r}",
        )
    return {
        "release_id": row["release_id"],
        "version": row["version"],
        "state": row["state"],
        "row_version": row["row_version"],
        "last_transition_at": row["last_transition_at"],
        "created_at": row["created_at"],
        "history": row["transition_log"],
    }
