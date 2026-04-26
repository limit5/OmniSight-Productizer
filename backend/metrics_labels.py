"""Y9 #285 row 4 — Prometheus label cardinality control.

Wraps ``tenant_id`` / ``project_id`` / ``product_line`` values before
they hit Prometheus ``labels(...)`` calls so the ``omnisight_billing_*``
metric family cannot drive the registry's series count past a known
ceiling. The bucket helpers map any value above the per-process cap to
the literal string ``"other"`` so the high-cardinality tail remains
queryable as a single bucket without each unique tenant / project
spawning its own time-series.

Caps (override via env for ops emergencies)
───────────────────────────────────────────
* ``tenant_id``    — 1000 distinct values (env: ``OMNISIGHT_METRICS_TENANT_CAP``)
* ``project_id``   — 10000 distinct values (env: ``OMNISIGHT_METRICS_PROJECT_CAP``)
* ``product_line`` — 50 distinct values (env: ``OMNISIGHT_METRICS_PRODUCT_LINE_CAP``)

The ``product_line`` cap is deliberately generous given the
``projects.product_line`` schema constraint (``length BETWEEN 1 AND 64``):
real deployments use a fixed enum (``embedded`` / ``web`` / ``mobile`` /
``software`` / ``default``) so 50 is comfortably above the realistic
working set while still preventing a runaway operator-defined value
from blowing up the registry.

Sentinel return values
──────────────────────
* ``UNKNOWN_BUCKET = "unknown"`` — caller passed ``None`` or an empty
  string. Used by emit-side fan-outs that don't have a value yet (e.g.
  the LLM callback fires from a ``track_tokens`` site whose request
  scope hasn't loaded a ``product_line`` ContextVar).
* ``OTHER_BUCKET = "other"`` — value is a real string but the
  per-process cap has been hit. Series are still labelled but they all
  collapse into the single ``other`` bucket so PromQL can sum them as
  the long-tail.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
The three ``_seen_*`` sets ARE module-global. Audit answer **#3 —
intentionally per-worker independent**:

* ``uvicorn --workers N`` runs N OS processes; each scrapes its own
  ``/metrics`` endpoint and exposes its own time-series count. The cap
  is per-worker, so the total cardinality across N workers is bounded
  to ``N × cap`` which is the acceptable design ceiling (typical
  prod: ``N=2`` ⇒ ≤ 2000 tenants × 20000 projects × 100 product_lines
  per scrape target — Prometheus federation merges them transparently).
* The cap is a defence against a single misbehaving worker spawning an
  unbounded series count, not a global budget — Prometheus already
  accepts the same label set from multiple replicas without
  multiplying storage cost.
* No PG / Redis coordination needed because each worker's cap is its
  own concern. There is no "shared budget" semantic that would require
  inter-worker arithmetic.

The internal ``threading.Lock`` is for in-process thread safety only
(the ``set.add`` + ``len()`` pair is non-atomic without it). It does
NOT participate in cross-worker coordination.

Read-after-write timing audit
──────────────────────────────
N/A — pure in-memory bookkeeping. The bucket helpers do not read or
write any DB row, file, or external system. They are called from
fire-and-forget metric emission paths (``billing_usage.record_*``) so a
slow lookup here cannot block the upstream LLM call / workflow finish
/ GC sweep.
"""

from __future__ import annotations

import os
import threading
from typing import Optional


# Sentinel labels returned by the bucket helpers. Public constants so
# tests + downstream readers (Grafana queries) can reference them by
# name instead of relying on the string literal.
UNKNOWN_BUCKET: str = "unknown"
OTHER_BUCKET: str = "other"


def _read_env_cap(name: str, default: int) -> int:
    """Parse an env var to int with the supplied default on any failure.

    Negative or zero values are clamped to ``1`` so the helpers always
    have at least one slot — a cap of zero would force every value into
    ``other`` immediately, which is rarely the operator intent and
    impossible to debug later from the env var alone.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, val)


_TENANT_CAP: int = _read_env_cap("OMNISIGHT_METRICS_TENANT_CAP", 1000)
_PROJECT_CAP: int = _read_env_cap("OMNISIGHT_METRICS_PROJECT_CAP", 10_000)
_PRODUCT_LINE_CAP: int = _read_env_cap("OMNISIGHT_METRICS_PRODUCT_LINE_CAP", 50)


_lock = threading.Lock()
_seen_tenants: set[str] = set()
_seen_projects: set[str] = set()
_seen_product_lines: set[str] = set()


def _bucket(value: Optional[str], seen: set[str], cap: int) -> str:
    """Shared bucket logic: keep ``value`` if seen-or-under-cap, else
    fall through to :data:`OTHER_BUCKET`.

    ``None`` / empty inputs short-circuit to :data:`UNKNOWN_BUCKET`
    BEFORE the cap is consulted so a flood of unattributed events
    cannot eat the entire budget on its own.
    """
    if value is None or value == "":
        return UNKNOWN_BUCKET
    with _lock:
        if value in seen:
            return value
        if len(seen) >= cap:
            return OTHER_BUCKET
        seen.add(value)
        return value


def bucket_tenant_id(tenant_id: Optional[str]) -> str:
    """Cap-protected ``tenant_id`` label value. ≤ 1000 distinct per worker."""
    return _bucket(tenant_id, _seen_tenants, _TENANT_CAP)


def bucket_project_id(project_id: Optional[str]) -> str:
    """Cap-protected ``project_id`` label value. ≤ 10000 distinct per worker."""
    return _bucket(project_id, _seen_projects, _PROJECT_CAP)


def bucket_product_line(product_line: Optional[str]) -> str:
    """Cap-protected ``product_line`` label value. ≤ 50 distinct per worker.

    Real deployments use a fixed enum (``embedded`` / ``web`` / ``mobile`` /
    ``software`` / ``default``) so the cap is essentially a defensive
    backstop against operator-defined values; legitimate workloads will
    never hit it.
    """
    return _bucket(product_line, _seen_product_lines, _PRODUCT_LINE_CAP)


def reset_for_tests() -> None:
    """Reset the per-worker bookkeeping sets. Test-only helper.

    Production never calls this — the sets are meant to grow lazily up
    to the cap and stay populated for the lifetime of the process so a
    given tenant_id always lands in the same Prometheus series.
    """
    with _lock:
        _seen_tenants.clear()
        _seen_projects.clear()
        _seen_product_lines.clear()


def cap_status() -> dict[str, dict[str, int]]:
    """Diagnostic snapshot — how full is each cap right now?

    Used by the unit tests to assert overflow behaviour and by the
    optional ``/api/v1/admin/metrics/cap-status`` endpoint (if/when
    operators want to see at a glance whether they're approaching the
    cap and should rotate to a fixed-enum slicing scheme).
    """
    with _lock:
        return {
            "tenant": {"seen": len(_seen_tenants), "cap": _TENANT_CAP},
            "project": {"seen": len(_seen_projects), "cap": _PROJECT_CAP},
            "product_line": {
                "seen": len(_seen_product_lines), "cap": _PRODUCT_LINE_CAP,
            },
        }
