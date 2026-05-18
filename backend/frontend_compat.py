"""OP-1483 — FE/BE bundle compatibility observer.

Codex's 2026-05-18 incident showed prod serving backend=hotfix8 +
frontend=hotfix5 for several hours without anyone noticing. The hotfixes
were internal-only so the skew did not corrupt user data, but the same
shape would silently break a contract-changing release.

This module is the runtime detector: every SSR / browser-originated API
request carries an ``X-OmniSight-Frontend-Bundle`` header naming the
bundle id the frontend was built from, and an
``X-OmniSight-Frontend-Api-Contract`` header naming the API version it
was built against. A FastAPI middleware (see
``backend.main._frontend_compat_observer``) drops each observation into
the in-process ring buffer maintained here; ``/readyz`` reads
``summary()`` to surface ``frontend_compat_check`` to operators.

Design notes:

* In-process state only. The ring buffer is per-worker and resets on
  restart. A real fleet-wide view would aggregate via Prometheus —
  this module exists so an operator on a single replica can already
  tell "is my FE skewed against my BE?" without waiting for the
  metrics scrape cycle.
* Observation, not gating. ``/readyz`` reports the verdict but the
  ``ready`` gate is intentionally NOT downgraded on mismatch — the
  ticket's non-goal explicitly bans degrading UX further on a skew
  that the operator can't fix from the request side.
* Counter increment is fire-and-forget. The Prometheus metric is the
  authoritative cross-replica view; ``record_observation`` increments
  the matching counter so ``rate()`` queries fire the
  ``FEBEBundleMismatch`` alert (see ``prometheus/rules/image-compat.yml``).
"""

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Deque

#: Maximum number of frontend observations retained in the ring buffer.
#: The ticket calls out "last N (e.g. 100)"; 100 is enough to absorb a
#: brief Prometheus scrape gap without growing memory unboundedly on a
#: replica that handles 100+ rps.
_RING_BUFFER_MAX = 100

#: Ring buffer of recent frontend observations.  Each entry is a
#: ``(bundle_id, api_contract)`` tuple.  ``None`` markers preserve the
#: "we saw a non-SSR request that carried no headers" signal without
#: polluting the unique-bundles set surfaced to operators.
_observations: Deque[tuple[str | None, str | None]] = deque(maxlen=_RING_BUFFER_MAX)

#: Cumulative mismatch counter since process start.  Survives ring-
#: buffer eviction so /readyz can still report a non-zero count even
#: after the offending observations have rolled out of the window.
_mismatch_total: int = 0

#: Guards both the deque and the cumulative counter so concurrent
#: middleware invocations (the FastAPI event loop multiplexes many
#: requests) cannot observe a torn write.
_lock = RLock()


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def record_observation(
    fe_bundle: str | None,
    fe_api_contract: str | None,
    *,
    be_bundle: str | None = None,
) -> None:
    """Record one frontend-originated observation.

    ``be_bundle`` is optional and only used to drive the Prometheus
    counter increment.  Operators wire that in from
    ``backend.api_versioning.build_version_payload``'s ``bundle_id``
    field via the request middleware so all comparisons reference the
    same source of truth.
    """
    global _mismatch_total

    fe = _normalize(fe_bundle)
    contract = _normalize(fe_api_contract)
    if fe is None and contract is None:
        return  # nothing useful to record

    with _lock:
        _observations.append((fe, contract))
        if fe is not None and be_bundle is not None and fe != be_bundle:
            _mismatch_total += 1
            _emit_mismatch_counter(fe, be_bundle)


def _emit_mismatch_counter(fe_bundle: str, be_bundle: str) -> None:
    """Bump the Prometheus counter for the (fe, be) pair.

    Wrapped in a defensive try/except: a misconfigured metrics registry
    must not crash the request hot path that feeds this module.
    """
    try:
        from backend import metrics as _metrics

        _metrics.fe_be_bundle_mismatch_total.labels(
            fe_bundle=fe_bundle,
            be_bundle=be_bundle,
        ).inc()
    except Exception:
        # Metrics are best-effort — never propagate failures back to
        # the middleware.  The in-process counter is still authoritative
        # for /readyz.
        pass


def summary(be_bundle: str | None, be_api_required: str | None) -> dict:
    """Return the dict shape /readyz surfaces under ``frontend_compat_check``.

    The contract pinned by OP-1483 AC#1:

    * empty buffer → ``ok=True`` (no incoming FE requests yet, nothing
      to compare against — silent is healthy on a freshly-booted
      replica that has not seen browser traffic).
    * every observation matches backend's bundle_id → ``ok=True``.
    * any observation disagrees → ``ok=False`` with the offending
      bundles + ``mismatch_count`` so operators can spot which version
      pair is in play.
    """
    with _lock:
        snapshot = list(_observations)
        cumulative = _mismatch_total

    observed_bundles: list[str] = []
    seen: set[str] = set()
    mismatch_observations = 0
    for fe, _contract in snapshot:
        if fe is None:
            continue
        if fe not in seen:
            seen.add(fe)
            observed_bundles.append(fe)
        if be_bundle is not None and fe != be_bundle:
            mismatch_observations += 1

    ok = mismatch_observations == 0

    detail_parts: list[str] = []
    if not snapshot:
        detail_parts.append("no_frontend_observations")
    elif be_bundle is None:
        # Backend itself doesn't know its own bundle id (e.g. local
        # dev with no baked bundle.json).  Surface the observations
        # but stay ok=True — we can't decide mismatch without a baseline.
        detail_parts.append("backend_bundle_unknown")
        ok = True
    else:
        detail_parts.append(f"be_bundle={be_bundle}")
        detail_parts.append(f"unique_fe_bundles={len(observed_bundles)}")
        if mismatch_observations:
            detail_parts.append(f"recent_mismatches={mismatch_observations}")
    if be_api_required is not None:
        detail_parts.append(f"api_required={be_api_required}")

    return {
        "ok": ok,
        "detail": ",".join(detail_parts),
        "observed_fe_bundles": observed_bundles,
        "mismatch_count": cumulative,
        "samples_in_window": len(snapshot),
        "window_capacity": _RING_BUFFER_MAX,
    }


def reset_for_tests() -> None:
    """Test-only — clear the ring buffer and reset the counter."""
    global _mismatch_total
    with _lock:
        _observations.clear()
        _mismatch_total = 0
