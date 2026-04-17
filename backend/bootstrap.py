"""L1 — Bootstrap status detection.

First-install wizard backend. Exposes :func:`get_bootstrap_status`, which
reports the four gates the `/bootstrap` wizard drives to green before the
app is considered "finalized":

  * ``admin_password_default``  — admin still using the well-known default
  * ``llm_provider_configured`` — selected provider has a usable key
  * ``cf_tunnel_configured``    — Cloudflare Tunnel provisioned (or skipped)
  * ``smoke_passed``            — end-to-end smoke test has run green

Signals are derived from already-authoritative sources (users table,
``settings``, CF tunnel router state) so the wizard never has to keep a
parallel truth. The one piece that needs its own persistence is
``smoke_passed``: a successful smoke run writes a marker into
``data/.bootstrap_state.json`` that :func:`get_bootstrap_status` reads.
Subsequent L1 checkboxes migrate this marker into a proper
``bootstrap_state`` table.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BOOTSTRAP_MARKER = _PROJECT_ROOT / "data" / ".bootstrap_state.json"

# Logical wizard step identifiers written into ``bootstrap_state.step``.
# Keep these stable — the finalize API compares the set of recorded steps
# against ``REQUIRED_STEPS`` before allowing the wizard to close out.
STEP_ADMIN_PASSWORD = "admin_password_set"
STEP_LLM_PROVIDER = "llm_provider_configured"
STEP_CF_TUNNEL = "cf_tunnel_configured"
STEP_SMOKE = "smoke_passed"
STEP_FINALIZED = "finalized"

REQUIRED_STEPS: tuple[str, ...] = (
    STEP_ADMIN_PASSWORD,
    STEP_LLM_PROVIDER,
    STEP_CF_TUNNEL,
    STEP_SMOKE,
)


@dataclass(frozen=True)
class BootstrapStatus:
    admin_password_default: bool
    llm_provider_configured: bool
    cf_tunnel_configured: bool
    smoke_passed: bool

    @property
    def all_green(self) -> bool:
        return (
            not self.admin_password_default
            and self.llm_provider_configured
            and self.cf_tunnel_configured
            and self.smoke_passed
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Signal probes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _admin_password_is_default() -> bool:
    """True if any admin row is still flagged ``must_change_password``.

    The default admin created by :func:`auth.ensure_default_admin` sets
    ``must_change_password=1`` whenever the password matches the bundled
    fallback (``omnisight-admin``). Clearing the flag via
    ``POST /auth/change-password`` is the exit path — so this flag IS the
    ground truth for "operator hasn't rotated the shipping credentials
    yet".
    """
    try:
        from backend import db

        conn = db._conn()
    except Exception as exc:
        logger.debug("bootstrap: db not initialised (%s) — treating admin password as default", exc)
        return True

    try:
        async with conn.execute(
            "SELECT COUNT(*) AS n FROM users "
            "WHERE role='admin' AND enabled=1 AND must_change_password=1"
        ) as cur:
            row = await cur.fetchone()
    except Exception as exc:
        logger.warning("bootstrap: admin password probe failed: %s", exc)
        return True

    return bool(row and (row["n"] or 0) > 0)


def _llm_provider_is_configured() -> bool:
    """True if the selected LLM provider has a non-empty credential.

    Mirrors the sanity check in :mod:`backend.config` (``ollama`` is local,
    everyone else needs ``{provider}_api_key``). The provider is considered
    *configured* the moment a key is present; we don't ping here — wizard
    step L3 does the live ``provider.ping()`` and will surface a distinct
    ``key_invalid`` error if the credential is bogus.
    """
    try:
        from backend.config import settings
    except Exception as exc:
        logger.warning("bootstrap: cannot import settings (%s)", exc)
        return False

    provider = (settings.llm_provider or "").strip().lower()
    if not provider:
        return False
    if provider == "ollama":
        return True
    key_field = f"{provider}_api_key"
    key = getattr(settings, key_field, "") or ""
    return bool(key.strip())


def _cf_tunnel_is_configured() -> bool:
    """True if a CF tunnel has been provisioned OR explicitly skipped.

    Two sources are consulted:

    1. Explicit skip / provisioned marker in ``data/.bootstrap_state.json``
       (set by L4 "cf_tunnel_configured"/"cf_tunnel_skipped").
    2. Live router state — ``tunnel_id`` present in the CF tunnel router
       means a provision call has landed in this process.

    The marker takes precedence so the signal survives process restarts.
    """
    marker = _read_marker()
    if marker.get("cf_tunnel_configured") is True:
        return True
    if marker.get("cf_tunnel_skipped") is True:
        return True

    try:
        from backend.routers import cloudflare_tunnel as _cft

        state = _cft._get_state()
        return bool(state.get("tunnel_id"))
    except Exception as exc:
        logger.debug("bootstrap: CF tunnel router probe failed (%s)", exc)
        return False


def _smoke_has_passed() -> bool:
    """True if a smoke run has been marked green in the bootstrap marker."""
    return bool(_read_marker().get("smoke_passed"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Marker file helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _read_marker() -> dict:
    path = _BOOTSTRAP_MARKER
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("bootstrap: marker %s unreadable (%s) — treating as empty", path, exc)
        return {}


def _write_marker(data: dict) -> None:
    path = _BOOTSTRAP_MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True, indent=2), encoding="utf-8")


def mark_smoke_passed(passed: bool = True) -> None:
    """Persist the smoke test outcome into the bootstrap marker.

    Kept in :mod:`bootstrap` (not the smoke runner) so the storage layout
    stays colocated with :func:`get_bootstrap_status` that reads it.
    """
    data = _read_marker()
    data["smoke_passed"] = bool(passed)
    _write_marker(data)


def mark_cf_tunnel(*, configured: bool = False, skipped: bool = False) -> None:
    """Persist CF tunnel outcome (provisioned / deliberately skipped)."""
    data = _read_marker()
    if configured:
        data["cf_tunnel_configured"] = True
        data.pop("cf_tunnel_skipped", None)
    elif skipped:
        data["cf_tunnel_skipped"] = True
        data.pop("cf_tunnel_configured", None)
    else:
        data.pop("cf_tunnel_configured", None)
        data.pop("cf_tunnel_skipped", None)
    _write_marker(data)


def _reset_for_tests(marker_path: Optional[Path] = None) -> None:
    """Point the marker at a fresh path (or wipe the default one)."""
    global _BOOTSTRAP_MARKER
    if marker_path is not None:
        _BOOTSTRAP_MARKER = marker_path
    elif _BOOTSTRAP_MARKER.exists():
        try:
            _BOOTSTRAP_MARKER.unlink()
        except OSError:
            pass
    _gate_cache_reset()


def is_bootstrap_finalized_flag() -> bool:
    """Return the persisted ``bootstrap_finalized`` app-setting flag.

    The flag lives in the bootstrap marker alongside ``smoke_passed`` /
    ``cf_tunnel_configured``. It is written once by
    :func:`mark_bootstrap_finalized` the moment every required step has
    been recorded, and is what the gate middleware ultimately trusts so
    the wizard cannot be re-entered after a restart.
    """
    return bool(_read_marker().get("bootstrap_finalized"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  bootstrap_state table — step audit trail
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _serialise_metadata(metadata: Optional[dict[str, Any]]) -> str:
    if not metadata:
        return "{}"
    try:
        return json.dumps(metadata, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("bootstrap: metadata not JSON-serialisable (%s) — storing {}", exc)
        return "{}"


def _deserialise_metadata(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def record_bootstrap_step(
    step: str,
    *,
    actor_user_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Upsert a wizard step into ``bootstrap_state``.

    Re-running a step overwrites ``completed_at`` / ``actor_user_id`` /
    ``metadata`` so the row reflects the most recent completion (the
    wizard can legitimately redo a step, e.g. operator picks a different
    LLM provider). Call this from each wizard sub-step handler the moment
    it finishes its side-effects.
    """
    if not step:
        raise ValueError("bootstrap step name must be non-empty")

    from backend import db

    try:
        conn = db._conn()
    except Exception as exc:
        logger.warning("bootstrap: cannot record step %s — db not ready (%s)", step, exc)
        return

    payload = _serialise_metadata(metadata)
    try:
        await conn.execute(
            "INSERT INTO bootstrap_state (step, completed_at, actor_user_id, metadata) "
            "VALUES (?, datetime('now'), ?, ?) "
            "ON CONFLICT(step) DO UPDATE SET "
            "  completed_at=excluded.completed_at, "
            "  actor_user_id=excluded.actor_user_id, "
            "  metadata=excluded.metadata",
            (step, actor_user_id, payload),
        )
        await conn.commit()
    except Exception as exc:
        logger.warning("bootstrap: record_bootstrap_step(%s) failed: %s", step, exc)


async def get_bootstrap_step(step: str) -> Optional[dict[str, Any]]:
    """Return the most recent record for *step*, or ``None`` if absent."""
    from backend import db

    try:
        conn = db._conn()
    except Exception as exc:
        logger.debug("bootstrap: get_bootstrap_step db not ready (%s)", exc)
        return None

    try:
        async with conn.execute(
            "SELECT step, completed_at, actor_user_id, metadata "
            "FROM bootstrap_state WHERE step=?",
            (step,),
        ) as cur:
            row = await cur.fetchone()
    except Exception as exc:
        logger.warning("bootstrap: get_bootstrap_step(%s) failed: %s", step, exc)
        return None
    if row is None:
        return None
    return {
        "step": row["step"],
        "completed_at": row["completed_at"],
        "actor_user_id": row["actor_user_id"],
        "metadata": _deserialise_metadata(row["metadata"]),
    }


async def list_bootstrap_steps() -> list[dict[str, Any]]:
    """Return all recorded wizard steps ordered by ``completed_at`` asc."""
    from backend import db

    try:
        conn = db._conn()
    except Exception as exc:
        logger.debug("bootstrap: list_bootstrap_steps db not ready (%s)", exc)
        return []

    try:
        async with conn.execute(
            "SELECT step, completed_at, actor_user_id, metadata "
            "FROM bootstrap_state ORDER BY completed_at ASC, step ASC"
        ) as cur:
            rows = await cur.fetchall()
    except Exception as exc:
        logger.warning("bootstrap: list_bootstrap_steps failed: %s", exc)
        return []
    return [
        {
            "step": r["step"],
            "completed_at": r["completed_at"],
            "actor_user_id": r["actor_user_id"],
            "metadata": _deserialise_metadata(r["metadata"]),
        }
        for r in rows
    ]


async def _recorded_step_names() -> set[str]:
    return {s["step"] for s in await list_bootstrap_steps()}


async def missing_required_steps() -> list[str]:
    """Return required steps that have no row in ``bootstrap_state`` yet.

    Used by ``POST /api/v1/bootstrap/finalize`` to gate the transition —
    finalize must refuse until every required step is on record.
    """
    recorded = await _recorded_step_names()
    return [s for s in REQUIRED_STEPS if s not in recorded]


async def mark_bootstrap_finalized(
    *,
    actor_user_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    required: Iterable[str] = REQUIRED_STEPS,
) -> BootstrapStatus:
    """Close the wizard: verify live gates + required rows, then persist.

    Raises ``RuntimeError`` if any gate is still red, or if any required
    step is missing from ``bootstrap_state`` (caller — the finalize API
    route — should surface this as HTTP 409). On success:

      * writes a ``finalized`` row into ``bootstrap_state``
      * sets ``bootstrap_finalized=true`` in the app-settings marker
      * flips the in-process gate cache to sticky-green so subsequent
        requests are no longer redirected to ``/bootstrap``
    """
    status = await get_bootstrap_status()
    if not status.all_green:
        raise RuntimeError(
            f"bootstrap not green: {status.to_dict()}"
        )
    missing = [s for s in required if s not in await _recorded_step_names()]
    if missing:
        raise RuntimeError(
            f"bootstrap_state missing required steps: {missing}"
        )

    await record_bootstrap_step(
        STEP_FINALIZED,
        actor_user_id=actor_user_id,
        metadata=metadata or {},
    )

    data = _read_marker()
    data["bootstrap_finalized"] = True
    _write_marker(data)

    _gate_cache["finalized"] = True
    _gate_cache["ts"] = time.monotonic()
    logger.info("bootstrap: finalized by actor=%s", actor_user_id or "<anonymous>")
    return status


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def get_bootstrap_status() -> BootstrapStatus:
    """Compute the four bootstrap gates.

    The returned object is a dataclass; callers that need a plain dict
    (e.g. JSON responses) should call :meth:`BootstrapStatus.to_dict`.
    """
    return BootstrapStatus(
        admin_password_default=await _admin_password_is_default(),
        llm_provider_configured=_llm_provider_is_configured(),
        cf_tunnel_configured=_cf_tunnel_is_configured(),
        smoke_passed=_smoke_has_passed(),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L1 #2 — Gate middleware helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_GATE_CACHE_TTL = 2.0  # seconds — keep low so wizard progress is visible
_gate_cache: dict = {"finalized": None, "ts": 0.0}


def _gate_cache_reset() -> None:
    _gate_cache["finalized"] = None
    _gate_cache["ts"] = 0.0


async def is_bootstrap_finalized() -> bool:
    """Cheap, cached check used by the global redirect middleware.

    Semantics:
      * Returns ``True`` once every gate in :func:`get_bootstrap_status`
        is green OR the persisted ``bootstrap_finalized`` app-setting flag
        is set. Once ``True`` in this process it sticks (the wizard
        can't un-finalize itself).
      * ``False`` result is re-checked every ``_GATE_CACHE_TTL`` seconds
        so wizard progress reflects into the middleware promptly.
      * Probe errors fail-open (``True``) so a broken DB never locks
        operators out of the app.
    """
    now = time.monotonic()
    cache = _gate_cache
    if cache["finalized"] is True:
        return True
    # Persisted finalize flag short-circuits the live probe — this is
    # what makes the wizard not re-open after a restart even if, say,
    # the smoke marker was wiped by a sysadmin.
    if is_bootstrap_finalized_flag():
        cache["finalized"] = True
        cache["ts"] = now
        return True
    if cache["finalized"] is not None and (now - cache["ts"]) < _GATE_CACHE_TTL:
        return cache["finalized"]
    try:
        status = await get_bootstrap_status()
        finalized = bool(status.all_green)
    except Exception as exc:
        logger.warning("bootstrap gate probe failed (%s) — failing open", exc)
        finalized = True
    cache["finalized"] = finalized
    cache["ts"] = now
    return finalized
