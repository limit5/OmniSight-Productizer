#!/usr/bin/env python3
"""H4b — Sandbox cost weight calibration.

Reads the past N days of sandbox lifecycle events from ``audit_log``
(``sandbox_launched`` paired with ``sandbox.oom`` / ``sandbox_killed``)
and the freshest ``host_metrics`` ring buffer (best-effort), computes
empirical CPU x time / peak-memory per sandbox class, and produces a
diff report comparing the new weights against the H4a hardcoded
defaults baked into :class:`backend.sandbox_capacity.SandboxCostWeight`.

With ``--apply``, writes the calibrated weights to
``configs/sandbox_cost_weights.yaml`` (replacing the H4a hardcode with
a config-driven source) and records the change in the audit hash chain.

Calibration model
-----------------
* **Class identity** — each launch row stamps ``(tier, tenant_budget)``
  in ``after_json``. We match those against the canonical
  ``COST_WEIGHT_ESTIMATES`` table by ``(tier, closest tokens)`` so a
  drifted class still gets bucketed.
* **Duration** — ``end_ts - start_ts`` where end is the next
  ``sandbox.oom`` / ``sandbox_killed`` row on the same ``entity_id``
  (the container name). Launches with no observed end are dropped.
* **CPU x time** — ``mean(tenant_budget * duration_s)`` per class.
  Calibrating against this normalises a class whose real workload is
  shorter or longer than the H4a estimate baked at design time.
* **Peak memory** — ``sandbox.oom`` rows include ``memory_limit``; if
  no OOM rows are seen for a class we fall back to the launch row's
  ``memory`` field (also stamped at start).
* **Normalisation** — the lightest class (smallest mean CPU x time)
  is pinned to 1.0 token so callers that hardcode "1 token" against
  the lightweight envelope keep working. Heavier classes scale
  proportionally.

Outputs
-------
* Default: markdown diff report on stdout (old vs new tokens, sample
  count, mean duration). Exit 0 on success.
* ``--format json``: single JSON document with the same data.
* ``--apply``: also writes ``configs/sandbox_cost_weights.yaml`` and
  records an ``audit_log`` row keyed
  ``action=sandbox_cost_calibration``.

Exit codes:
    0 — calibration ran (with or without --apply)
    1 — DB read / parse failure
    2 — bad CLI args
    3 — insufficient data (no paired launches in the window)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running from anywhere: prepend repo root so ``import backend.*`` works.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger("calibrate_sandbox_cost")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_DAYS = 7
"""H4b spec: 'H1 上線 1 週後' — calibrate against the first observation
window, then re-run on a rolling cadence operators decide."""

DEFAULT_OUT_PATH = _REPO_ROOT / "configs" / "sandbox_cost_weights.yaml"

LAUNCH_ACTION = "sandbox_launched"
END_ACTIONS = ("sandbox.oom", "sandbox_killed")

MIN_DURATION_S = 0.5
"""Drop launches with end - start < 0.5s — almost certainly a docker
race / immediate failure that doesn't represent real workload cost."""

# ── Drift classification thresholds — used by the diff report only ──
# Calibration math itself doesn't care; these are *operator-review*
# heuristics that bucket each class into a severity tag so a human
# scanning the report can tell at a glance which rows need scrutiny.

MIN_SAMPLES_FOR_CONFIDENCE = 5
"""Below this sample count a class is flagged LOW-DATA in the diff —
even a 'dramatic' Δ on 1-2 samples is statistical noise, not signal.
Operators should NOT --apply a calibration where critical classes have
< MIN_SAMPLES_FOR_CONFIDENCE; review-only."""

MODERATE_DRIFT_PCT = 0.10
"""|new - old| / max(old, ε) ≥ this → REVIEW status. The 10% floor is
an "above-noise" cutoff — token weights round to 2 decimals so anything
below 10% drift on a small token (e.g. 0.5 → 0.55) is well within the
representational granularity and usually means "no real change"."""

LARGE_DRIFT_PCT = 0.50
"""|new - old| / max(old, ε) ≥ this → LARGE status. A 50%+ swing in a
sandbox class's token weight changes admission geometry materially —
operator MUST sanity-check the per-class math (mean dur, mean CPU·s,
sample count) before --apply or a benign-looking calibration could
halve effective concurrency for that workload class overnight."""

# Recommendation verdicts (also embedded in JSON output for CI tooling).
VERDICT_APPLY = "APPLY"
VERDICT_REVIEW = "REVIEW"
VERDICT_SKIP = "SKIP"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Memory-limit parsing (mirrors host_metrics CLI parser)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MEM_UNITS: list[tuple[str, int]] = [
    ("PiB", 1024 ** 5), ("TiB", 1024 ** 4), ("GiB", 1024 ** 3),
    ("MiB", 1024 ** 2), ("KiB", 1024),
    ("PB", 10 ** 15), ("TB", 10 ** 12), ("GB", 10 ** 9),
    ("MB", 10 ** 6), ("kB", 10 ** 3), ("KB", 10 ** 3),
    # Single-letter docker shortcuts (case-sensitive on docker side):
    ("g", 1024 ** 3), ("G", 1024 ** 3),
    ("m", 1024 ** 2), ("M", 1024 ** 2),
    ("k", 1024), ("K", 1024),
    ("B", 1), ("b", 1),
]


def parse_memory_limit_to_mb(raw: str | int | float | None) -> float:
    """Parse a docker-style memory string ('256m', '1g', '1536') -> MB.

    Docker strings use binary units when the suffix is lowercase (m=MiB,
    g=GiB) per its CLI reference; we treat both upper and lowercase
    aliases the same way here because the pre-existing audit rows mix
    casing across releases. Returns 0.0 on any parse failure so a single
    bad row doesn't poison the aggregate.
    """
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        # Already bytes (defensive — older audit rows sometimes recorded
        # int bytes). Convert to MB.
        return float(raw) / (1024 ** 2)
    s = str(raw).strip()
    if not s:
        return 0.0
    for suffix, mult in _MEM_UNITS:
        if s.endswith(suffix):
            num_part = s[: -len(suffix)].strip()
            try:
                return float(num_part) * mult / (1024 ** 2)
            except ValueError:
                return 0.0
    # No unit -> assume raw bytes.
    try:
        return float(s) / (1024 ** 2)
    except ValueError:
        return 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Aggregation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class ClassStats:
    """One sandbox class's calibration aggregate."""

    name: str                       # canonical SandboxCostWeight member name
    tier: str                       # the tier audit rows stamped
    old_tokens: float               # H4a hardcoded value
    sample_count: int = 0
    duration_s_total: float = 0.0
    cpu_token_s_total: float = 0.0  # tenant_budget * duration_s
    peak_mem_mb: float = 0.0        # max observed memory (limit or OOM)
    oom_count: int = 0
    # Δmem_peak — observed host-level mem growth during each sandbox's
    # [launch, end] window, summed across paired launches that had at
    # least one host_metrics ring sample inside their window. Sample
    # count is tracked separately because not every paired launch will
    # have ring coverage (the ring rotates every ~5 minutes; long-tail
    # historical launches in a 7-day window won't).
    delta_mem_peak_mb_total: float = 0.0
    delta_mem_sample_count: int = 0

    @property
    def mean_duration_s(self) -> float:
        return self.duration_s_total / self.sample_count if self.sample_count else 0.0

    @property
    def mean_cpu_token_s(self) -> float:
        return self.cpu_token_s_total / self.sample_count if self.sample_count else 0.0

    @property
    def mean_delta_mem_peak_mb(self) -> float:
        return (
            self.delta_mem_peak_mb_total / self.delta_mem_sample_count
            if self.delta_mem_sample_count else 0.0
        )


@dataclass
class CalibrationResult:
    """Top-level calibration output — drives both diff report + yaml write."""

    generated_at: float
    window_days: int
    window_start_ts: float
    window_end_ts: float
    total_paired: int                  # launches with a matched end event
    total_orphaned: int                # launches with no end (still running / lost)
    host_ring_size: int                # current host_metrics ring depth
    classes: dict[str, ClassStats] = field(default_factory=dict)
    new_weights: dict[str, float] = field(default_factory=dict)
    # ── Diff-report only ─────────────────────────────────────────────
    # When the operator runs the calibrator after at least one prior
    # ``--apply``, ``configs/sandbox_cost_weights.yaml`` is the live
    # source of truth — not the H4a hardcode. The CLI loads that file
    # (when present) into ``baseline_weights`` so the report compares
    # *what's currently running* against *what calibration would
    # produce*. ``baseline_source`` is a human-readable provenance
    # string ("H4a hardcode" or the yaml path + its generated_at).
    baseline_weights: dict[str, float] | None = None
    baseline_source: str = "H4a hardcode (backend.sandbox_capacity)"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Class inference: (tier, tokens) -> canonical name
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ``tier`` stamped on the audit row maps to a subset of canonical
# classes — a t1 launch is never phase64c_local_compile, etc. Built
# lazily inside :func:`canonical_class_table` so the import of
# ``backend.sandbox_capacity`` doesn't fire when this module is merely
# linted. Plain dict literal so changes to the H4a enum surface here
# during review.
_CANONICAL_TIER_HINT: dict[str, str] = {
    "gvisor_lightweight": "t1",
    "docker_t2_networked": "networked",
    "phase64c_local_compile": "t3-local",
    "phase64c_qemu_aarch64": "t1",
    "phase64c_ssh_remote": "t1",
}


def canonical_class_table() -> dict[str, dict[str, Any]]:
    """Snapshot the H4a defaults for diff + class inference.

    Imports ``backend.sandbox_capacity`` lazily so the script works on
    a CI machine without the backend's runtime deps; if the import
    fails (rare), we fall back to the table baked above so the diff
    can still render against well-known values.
    """
    try:
        from backend.sandbox_capacity import (
            COST_WEIGHT_ESTIMATES,
            SandboxCostWeight,
        )
        out: dict[str, dict[str, Any]] = {}
        for member in SandboxCostWeight:
            est = COST_WEIGHT_ESTIMATES[member]
            out[member.name] = {
                "tokens": float(member.value),
                "memory_mb": int(est.memory_mb),
                "cpu_cores": float(est.cpu_cores),
                "burst": bool(est.burst),
                "use_case": est.use_case,
                "tier_hint": _CANONICAL_TIER_HINT.get(member.name, "t1"),
            }
        return out
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("canonical_class_table import fallback: %s", exc)
        # Mirrors COST_WEIGHT_ESTIMATES at the time of writing — used
        # only when backend isn't importable.
        return {
            "gvisor_lightweight": {
                "tokens": 1.0, "memory_mb": 512, "cpu_cores": 1.0,
                "burst": True, "use_case": "unit test / lint",
                "tier_hint": "t1",
            },
            "docker_t2_networked": {
                "tokens": 2.0, "memory_mb": 1536, "cpu_cores": 2.0,
                "burst": False, "use_case": "integration test with network",
                "tier_hint": "networked",
            },
            "phase64c_local_compile": {
                "tokens": 4.0, "memory_mb": 2048, "cpu_cores": 4.0,
                "burst": False, "use_case": "make -j4 local compile (sustained)",
                "tier_hint": "t3-local",
            },
            "phase64c_qemu_aarch64": {
                "tokens": 3.0, "memory_mb": 2048, "cpu_cores": 2.0,
                "burst": False, "use_case": "aarch64 cross-compile under qemu",
                "tier_hint": "t1",
            },
            "phase64c_ssh_remote": {
                "tokens": 0.5, "memory_mb": 256, "cpu_cores": 0.5,
                "burst": True,
                "use_case": "ssh remote (compute on far side, local is just client)",
                "tier_hint": "t1",
            },
        }


def infer_class(
    tier: str | None,
    tokens: float | None,
    canonical: dict[str, dict[str, Any]],
) -> str | None:
    """Map an audit row's ``(tier, tenant_budget)`` to a canonical class.

    Strategy:
        1. Filter canonical classes whose ``tier_hint`` matches the row's
           ``tier`` (an exact-tier preference).
        2. Within that subset pick the class whose ``tokens`` is closest
           to the row's recorded ``tenant_budget``.
        3. If the tier filter empties the set (e.g. an unknown tier),
           fall back to closest tokens across all classes.
        4. Returns ``None`` only when both ``tier`` and ``tokens`` are
           missing.

    Trade-off: a launch that misreports ``tenant_budget`` (e.g. forced
    to 0 by legacy callers) lands on the lightweight class. That bucket
    will look noisier than reality but the calibration still produces
    a sane envelope — no class is silently dropped.
    """
    if tokens is None and not tier:
        return None
    tk = float(tokens) if tokens is not None else 0.0
    candidates = [
        (name, meta) for name, meta in canonical.items()
        if tier and meta["tier_hint"] == tier
    ]
    if not candidates:
        candidates = list(canonical.items())
    candidates.sort(key=lambda kv: abs(kv[1]["tokens"] - tk))
    return candidates[0][0] if candidates else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit log fetch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class AuditRow:
    """Subset of an ``audit_log`` row needed by the calibrator.

    Mirrors the fields the script reads — declared as a dataclass so
    tests can synthesise rows without going through the DB at all.
    """

    id: int
    ts: float
    action: str
    entity_id: str
    after: dict[str, Any]


async def fetch_sandbox_rows(since_ts: float) -> list[AuditRow]:
    """Pull all sandbox lifecycle audit rows since ``since_ts``.

    Uses the asyncpg pool when ``OMNISIGHT_DATABASE_URL`` points at
    Postgres; otherwise falls back to the SQLite dev DB via aiosqlite.
    Either way we read directly so callers don't need a running
    backend.
    """
    pg_dsn = os.environ.get("OMNISIGHT_DATABASE_URL", "").strip()
    if pg_dsn:
        return await _fetch_from_pg(pg_dsn, since_ts)
    return await _fetch_from_sqlite(since_ts)


async def _fetch_from_pg(dsn: str, since_ts: float) -> list[AuditRow]:
    import asyncpg  # type: ignore[import-not-found]
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT id, ts, action, entity_id, after_json "
            "FROM audit_log WHERE ts >= $1 AND action = ANY($2::text[]) "
            "ORDER BY ts ASC, id ASC",
            since_ts, [LAUNCH_ACTION, *END_ACTIONS],
        )
        out: list[AuditRow] = []
        for r in rows:
            try:
                after = json.loads(r["after_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                after = {}
            out.append(AuditRow(
                id=int(r["id"]),
                ts=float(r["ts"]),
                action=str(r["action"]),
                entity_id=str(r["entity_id"] or ""),
                after=after,
            ))
        return out
    finally:
        await conn.close()


async def _fetch_from_sqlite(since_ts: float) -> list[AuditRow]:
    try:
        import aiosqlite  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - dev env always has it
        raise RuntimeError(
            "aiosqlite is required for SQLite mode; install backend deps"
        ) from exc
    from backend.db import _DB_PATH  # type: ignore[attr-defined]
    placeholders = ",".join("?" * (1 + len(END_ACTIONS)))
    sql = (
        "SELECT id, ts, action, entity_id, after_json "
        f"FROM audit_log WHERE ts >= ? AND action IN ({placeholders}) "
        "ORDER BY ts ASC, id ASC"
    )
    params: list[Any] = [since_ts, LAUNCH_ACTION, *END_ACTIONS]
    async with aiosqlite.connect(str(_DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
    out: list[AuditRow] = []
    for r in rows:
        try:
            after = json.loads(r["after_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            after = {}
        out.append(AuditRow(
            id=int(r["id"]),
            ts=float(r["ts"]),
            action=str(r["action"]),
            entity_id=str(r["entity_id"] or ""),
            after=after,
        ))
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core calibration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _delta_mem_peak_mb(window_start: float, window_end: float,
                       host_snapshots: list[Any] | None) -> float | None:
    """Compute Δmem_peak (MB) for one sandbox window from host samples.

    Walks ``host_snapshots`` (a list of ``HostSnapshot`` — duck-typed so
    tests can pass plain objects with ``.host.mem_used_gb`` /
    ``.sampled_at``) and returns ``max(mem_used) - first(mem_used)``
    over snapshots whose ``sampled_at`` falls in
    ``[window_start, window_end]``. Returns ``None`` when no snapshots
    fall in the window (caller treats as "no Δmem signal for this run"
    and skips Δmem accumulation — *not* zero, which would dilute the
    mean of classes that *did* observe growth).

    Negative deltas (other workloads exited during the window, freeing
    host memory) clamp to 0.0: the calibrator measures *upward
    pressure* this sandbox added, not downward swings driven by
    unrelated processes.
    """
    if not host_snapshots:
        return None
    in_window = [
        s for s in host_snapshots
        if window_start <= getattr(s, "sampled_at", 0.0) <= window_end
    ]
    if not in_window:
        return None
    in_window.sort(key=lambda s: s.sampled_at)
    try:
        baseline_gb = float(in_window[0].host.mem_used_gb)
        peak_gb = max(float(s.host.mem_used_gb) for s in in_window)
    except (AttributeError, TypeError, ValueError):
        return None
    delta_gb = peak_gb - baseline_gb
    if delta_gb < 0.0:
        delta_gb = 0.0
    return delta_gb * 1024.0


def calibrate(rows: list[AuditRow], *, window_days: int,
              now: float | None = None,
              host_ring_size: int = 0,
              host_snapshots: list[Any] | None = None) -> CalibrationResult:
    """Produce a :class:`CalibrationResult` from raw audit rows.

    Pure function — host_metrics ring size + the snapshot list itself
    are passed in by the caller so tests can drive deterministically
    without touching the live sampler. The ring size is reported in
    the diff so operators can sanity-check whether the same backend
    was actively sampling during the window. ``host_snapshots`` (when
    provided, typically from ``host_metrics.get_host_history()``)
    enables Δmem_peak accumulation per class — see
    :func:`_delta_mem_peak_mb`.
    """
    canonical = canonical_class_table()
    classes: dict[str, ClassStats] = {
        name: ClassStats(
            name=name,
            tier=meta["tier_hint"],
            old_tokens=float(meta["tokens"]),
        )
        for name, meta in canonical.items()
    }

    # Index ends by entity_id, taking the FIRST end event (the one that
    # actually ended the container — a phantom second event would over-
    # count duration). Launches are walked in time order; for each, we
    # look for an end with ts >= launch.ts on the same entity_id.
    ends_by_entity: dict[str, list[AuditRow]] = {}
    launches: list[AuditRow] = []
    for r in rows:
        if r.action == LAUNCH_ACTION:
            launches.append(r)
        elif r.action in END_ACTIONS:
            ends_by_entity.setdefault(r.entity_id, []).append(r)

    paired = 0
    orphaned = 0
    used_end_ids: set[int] = set()

    for launch in launches:
        ends = ends_by_entity.get(launch.entity_id, [])
        chosen: AuditRow | None = None
        for end in ends:
            if end.id in used_end_ids:
                continue
            if end.ts < launch.ts:
                continue
            chosen = end
            break
        if chosen is None:
            orphaned += 1
            continue
        used_end_ids.add(chosen.id)
        duration_s = chosen.ts - launch.ts
        if duration_s < MIN_DURATION_S:
            # Likely a docker race — drop, but still mark the end as used
            # so it doesn't get paired against a later launch.
            continue

        tier = launch.after.get("tier")
        tokens = launch.after.get("tenant_budget")
        try:
            tokens_f = float(tokens) if tokens is not None else 0.0
        except (TypeError, ValueError):
            tokens_f = 0.0
        klass_name = infer_class(tier, tokens_f, canonical)
        if klass_name is None or klass_name not in classes:
            continue
        stats = classes[klass_name]
        stats.sample_count += 1
        stats.duration_s_total += duration_s
        stats.cpu_token_s_total += tokens_f * duration_s
        # Memory: launch's stamped limit (always present) + OOM's
        # measured peak when applicable.
        launch_mem_mb = parse_memory_limit_to_mb(launch.after.get("memory"))
        if launch_mem_mb > stats.peak_mem_mb:
            stats.peak_mem_mb = launch_mem_mb
        if chosen.action == "sandbox.oom":
            stats.oom_count += 1
            oom_mem_mb = parse_memory_limit_to_mb(
                chosen.after.get("memory_limit"),
            )
            if oom_mem_mb > stats.peak_mem_mb:
                stats.peak_mem_mb = oom_mem_mb
        # Δmem_peak — only accumulate when the host_metrics ring
        # actually covers this sandbox's window; otherwise the mean
        # would be diluted by zero-deltas from launches that the
        # sampler never saw.
        delta_mb = _delta_mem_peak_mb(launch.ts, chosen.ts, host_snapshots)
        if delta_mb is not None:
            stats.delta_mem_peak_mb_total += delta_mb
            stats.delta_mem_sample_count += 1
        paired += 1

    new_weights = _normalise_weights(classes, canonical)

    t = now if now is not None else time.time()
    window_start = t - window_days * 86400.0
    return CalibrationResult(
        generated_at=t,
        window_days=window_days,
        window_start_ts=window_start,
        window_end_ts=t,
        total_paired=paired,
        total_orphaned=orphaned,
        host_ring_size=host_ring_size,
        classes=classes,
        new_weights=new_weights,
    )


def _normalise_weights(classes: dict[str, ClassStats],
                       canonical: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Derive new tokens from BOTH mean CPU×time AND mean Δmem_peak.

    The H4a hardcoded values weight a class by whichever resource
    binds AIMD admission first (CPU cores OR memory). Calibration
    keeps that property by computing two normalised scores per
    sampled class and taking the **max**:

        cpu_score = mean_cpu_token_s    / ref_mean_cpu_token_s
        mem_score = mean_delta_mem_peak / ref_mean_delta_mem_peak
        new_tokens = round(max(cpu_score, mem_score), 2)

    The reference class is the one with the lowest ``mean_cpu_token_s``
    among sampled classes — same as before so the lightest class
    always pins to 1.0 (callers that hardcode "1 token" against the
    lightweight envelope keep working). When the reference class has
    no Δmem signal (no host_metrics ring coverage in any of its
    windows), the mem axis is dropped entirely and we fall back to
    the original CPU-only path: this is the conservative behaviour
    when the sampler wasn't running, not a silent under-weighting.

    Classes with zero samples keep their old token value — calibration
    is silent on classes the operator hasn't exercised in the window
    rather than guessing. Classes with samples but zero
    ``mean_cpu_token_s`` (e.g. ``tenant_budget == 0`` legacy launches)
    also keep the old value so a degenerate aggregate doesn't collapse
    the whole table to 0.
    """
    sampled = [
        (n, s) for n, s in classes.items()
        if s.sample_count > 0 and s.mean_cpu_token_s > 0.0
    ]
    out: dict[str, float] = {n: classes[n].old_tokens for n in classes}
    if not sampled:
        return out
    ref_name, ref_stats = min(sampled, key=lambda kv: kv[1].mean_cpu_token_s)
    cpu_ref = ref_stats.mean_cpu_token_s
    mem_ref = ref_stats.mean_delta_mem_peak_mb
    use_mem_axis = mem_ref > 0.0
    for n, s in sampled:
        cpu_score = s.mean_cpu_token_s / cpu_ref
        if use_mem_axis and s.mean_delta_mem_peak_mb > 0.0:
            mem_score = s.mean_delta_mem_peak_mb / mem_ref
            out[n] = round(max(cpu_score, mem_score), 2)
        else:
            out[n] = round(cpu_score, 2)
    # Pin the ref explicitly — float division might produce 0.999...
    out[ref_name] = 1.0
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Diff-report classification (human-review surface)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Status labels — short ASCII tags so the markdown table renders the
# same in plain text dumps, terminal pagers, and PR comments. Keep them
# aligned to a single column width so the report scans cleanly.
STATUS_OK = "OK"
STATUS_REVIEW = "REVIEW"
STATUS_LARGE = "LARGE"
STATUS_LOW_DATA = "LOW-DATA"
STATUS_NO_DATA = "NO-DATA"


def baseline_for(name: str, result: CalibrationResult) -> float:
    """Return the comparison baseline for ``name``.

    Prefers ``result.baseline_weights[name]`` when the CLI loaded a
    persisted yaml (post-first-``--apply`` state); otherwise falls back
    to the ClassStats' ``old_tokens`` (H4a hardcode). When the loaded
    yaml is missing a class entirely (e.g. operator hand-edited it) we
    also fall back so the diff doesn't silently become "new vs 0".
    """
    if result.baseline_weights is not None:
        val = result.baseline_weights.get(name)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    stats = result.classes.get(name)
    return stats.old_tokens if stats is not None else 0.0


def drift_pct(old: float, new: float) -> float:
    """Signed relative drift, ``(new - old) / max(|old|, ε)``.

    Returns 0.0 for any pair where both are zero (degenerate
    unweighted class). The ε floor prevents infinite blow-up when an
    operator hand-edits a class to 0 in the yaml.
    """
    denom = max(abs(old), 1e-9)
    return (new - old) / denom


def classify_drift(stats: ClassStats, old: float, new: float) -> str:
    """Bucket a class into one of the five status tags.

    Order of precedence is intentional:
        1. ``NO-DATA`` — no paired observations at all → diff is meaningless
        2. ``LOW-DATA`` — under MIN_SAMPLES_FOR_CONFIDENCE → noisy
        3. ``LARGE`` — drift ≥ LARGE_DRIFT_PCT → operator MUST review
        4. ``REVIEW`` — drift ≥ MODERATE_DRIFT_PCT → above-noise change
        5. ``OK`` — within representational granularity (≤ 10%)

    Sample-count gates fire BEFORE drift gates because a 200% swing
    based on 1 sample is statistical noise — labeling it LARGE would
    panic the reviewer over nothing.
    """
    if stats.sample_count == 0:
        return STATUS_NO_DATA
    if stats.sample_count < MIN_SAMPLES_FOR_CONFIDENCE:
        return STATUS_LOW_DATA
    pct = abs(drift_pct(old, new))
    if pct >= LARGE_DRIFT_PCT:
        return STATUS_LARGE
    if pct >= MODERATE_DRIFT_PCT:
        return STATUS_REVIEW
    return STATUS_OK


def summarise_calibration(result: CalibrationResult) -> dict[str, int]:
    """Bucket counts per status — drives the report's Summary section.

    Returns a dict with one int per STATUS_* constant; keys are stable
    so JSON consumers (a future CI gate) can read it positionally.
    """
    counts: dict[str, int] = {
        STATUS_OK: 0, STATUS_REVIEW: 0, STATUS_LARGE: 0,
        STATUS_LOW_DATA: 0, STATUS_NO_DATA: 0,
    }
    for name, stats in result.classes.items():
        old = baseline_for(name, result)
        new = result.new_weights.get(name, stats.old_tokens)
        counts[classify_drift(stats, old, new)] += 1
    return counts


def recommend_action(result: CalibrationResult) -> tuple[str, str]:
    """Top-level operator verdict — APPLY / REVIEW / SKIP + reason.

    Heuristic (intentionally conservative — false-positive REVIEW is
    cheap, false-negative APPLY is expensive):
        * SKIP  → no paired launches OR ALL classes are NO-DATA
                 (calibration ran but contributed nothing actionable)
        * REVIEW → any class is LARGE OR LOW-DATA (≥1 row needs human
                  eyeballs before flipping the live config)
        * APPLY → all sampled classes within MODERATE_DRIFT_PCT and
                 every class meets MIN_SAMPLES_FOR_CONFIDENCE (or is
                 unsampled, which keeps its prior value untouched)

    The verdict is *advisory*; the ``--apply`` flag still works
    regardless. Operators with out-of-band context (e.g. running a
    one-shot calibration on a known stress-test workload) can override.
    """
    if result.total_paired == 0:
        return VERDICT_SKIP, "no paired sandbox launches in window"
    counts = summarise_calibration(result)
    sampled_total = sum(
        1 for s in result.classes.values() if s.sample_count > 0
    )
    if sampled_total == 0:
        return VERDICT_SKIP, "no sampled classes (window contained only orphans)"
    if counts[STATUS_LARGE] > 0:
        return (
            VERDICT_REVIEW,
            f"{counts[STATUS_LARGE]} class(es) drifted ≥ "
            f"{int(LARGE_DRIFT_PCT * 100)}% — verify per-class math first"
        )
    if counts[STATUS_LOW_DATA] > 0:
        return (
            VERDICT_REVIEW,
            f"{counts[STATUS_LOW_DATA]} class(es) below "
            f"{MIN_SAMPLES_FOR_CONFIDENCE}-sample confidence floor"
        )
    if counts[STATUS_REVIEW] > 0:
        return (
            VERDICT_APPLY,
            f"{counts[STATUS_REVIEW]} class(es) drifted ≥ "
            f"{int(MODERATE_DRIFT_PCT * 100)}% but stayed within "
            f"{int(LARGE_DRIFT_PCT * 100)}% — safe to apply"
        )
    return VERDICT_APPLY, "all sampled classes within ±10% of current weights"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Persisted-yaml baseline load (best-effort)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_baseline_weights(path: Path) -> tuple[dict[str, float] | None, str]:
    """Best-effort load of an existing ``sandbox_cost_weights.yaml``.

    Returns ``(weights_dict, source_label)``. When the file doesn't
    exist or can't be parsed, returns ``(None, label)`` so the
    operator still sees in the report header *why* the baseline fell
    back to the H4a hardcode.

    Parser strategy:
        1. Try PyYAML if importable (canonical, handles every edge case
           the writer might emit).
        2. Fall back to a tiny indent-aware scanner that reads only the
           ``weights.<name>.tokens`` paths we control — sufficient for
           any file produced by ``render_yaml`` in this same script.
        3. Either path returning an empty mapping → treat as "no
           baseline" (an empty yaml is operationally identical to a
           missing one for our purposes).
    """
    if not path.exists():
        return None, f"H4a hardcode (no {path.name} found at {path.parent})"
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"H4a hardcode (failed to read {path}: {exc})"
    weights = _parse_weights_via_pyyaml(body)
    if weights is None:
        weights = _parse_weights_via_scanner(body)
    if not weights:
        return None, f"H4a hardcode (no weights parsed from {path})"
    # Prefer the file's stamped generated_at if present, else fall back
    # to the file's mtime. Either way the operator sees freshness.
    stamp = _extract_generated_at(body) or _format_mtime(path)
    return weights, f"{path} (last calibrated {stamp})"


def _parse_weights_via_pyyaml(body: str) -> dict[str, float] | None:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        loaded = yaml.safe_load(body)
    except Exception:
        return None
    if not isinstance(loaded, dict):
        return None
    weights = loaded.get("weights")
    if not isinstance(weights, dict):
        return None
    out: dict[str, float] = {}
    for name, payload in weights.items():
        if not isinstance(payload, dict):
            continue
        tokens = payload.get("tokens")
        try:
            out[str(name)] = float(tokens)
        except (TypeError, ValueError):
            continue
    return out


def _parse_weights_via_scanner(body: str) -> dict[str, float]:
    """Last-resort regex-y scanner — only understands what we wrote.

    Looks for the pattern produced by ``render_yaml``:

        weights:
          <name>:
            tokens: <number>
            ...

    Indent is two-space (we control the writer). Anything that doesn't
    match this exact shape is silently skipped — better to surface as
    "no baseline" in the report than to crash on hand-edits.
    """
    out: dict[str, float] = {}
    in_weights = False
    current_name: str | None = None
    for raw in body.splitlines():
        line = raw.rstrip("\n")
        if not in_weights:
            if line.strip() == "weights:":
                in_weights = True
            continue
        # Top-level key after weights: → exit
        if line and not line.startswith(" ") and not line.startswith("\t"):
            in_weights = False
            current_name = None
            continue
        # Class header at 2-space indent: "  <name>:"
        if line.startswith("  ") and not line.startswith("    "):
            stripped = line[2:].rstrip(":").strip()
            if stripped and not stripped.startswith("#"):
                current_name = stripped
            continue
        # tokens line at 4-space indent under current class
        if current_name and line.startswith("    tokens:"):
            try:
                value = line.split(":", 1)[1].strip()
                out[current_name] = float(value)
            except (IndexError, ValueError):
                continue
    return out


def _extract_generated_at(body: str) -> str | None:
    for raw in body.splitlines():
        if raw.startswith("generated_at:"):
            try:
                return raw.split(":", 1)[1].strip().strip("'\"")
            except IndexError:
                return None
    return None


def _format_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc,
        ).isoformat(timespec="seconds")
    except OSError:
        return "mtime unknown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Reporting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def render_text(result: CalibrationResult) -> str:
    """Markdown-flavoured diff table — the default human view.

    Layout (top-to-bottom):
        1. Window / paired-launch / host_ring header lines
        2. Comparison-baseline provenance line (yaml-or-hardcode)
        3. Summary section — bucket counts per status tag
        4. Per-class table: Class | Tier | Old | New | Δ | Samples |
           Mean dur (s) | Mean CPU·s | Mean Δmem (MB) | Peak mem (MB) |
           OOMs | Status. Sorted by drift magnitude (descending) so
           the rows demanding scrutiny float to the top.
        5. Recommendation footer — APPLY / REVIEW / SKIP + reason
        6. Apply hint pointing at ``--apply``

    The Status column uses short tags (OK / REVIEW / LARGE / LOW-DATA /
    NO-DATA) so the table renders identically in terminals, PR
    comments, and email previews — no glyphs that depend on font
    fallback.
    """
    lines: list[str] = []
    started = datetime.fromtimestamp(result.window_start_ts, timezone.utc)
    ended = datetime.fromtimestamp(result.window_end_ts, timezone.utc)
    lines.append(f"# Sandbox cost calibration — {result.window_days}-day window")
    lines.append(
        f"_Window:_ `{started.isoformat(timespec='seconds')}` → "
        f"`{ended.isoformat(timespec='seconds')}`"
    )
    lines.append(
        f"_Paired launches:_ **{result.total_paired}** "
        f"(orphaned launches with no observed end: {result.total_orphaned})"
    )
    if result.host_ring_size:
        lines.append(
            f"_host_metrics ring size at calibration time:_ "
            f"{result.host_ring_size} snapshots"
        )
    lines.append(f"_Comparison baseline:_ {result.baseline_source}")
    lines.append("")

    # ── Summary section — drift-bucket counts at a glance ──
    counts = summarise_calibration(result)
    verdict, reason = recommend_action(result)
    lines.append("## Summary")
    lines.append(
        f"- **Recommendation:** `{verdict}` — {reason}"
    )
    lines.append(
        f"- Classes by status: "
        f"`{STATUS_OK}={counts[STATUS_OK]}`, "
        f"`{STATUS_REVIEW}={counts[STATUS_REVIEW]}`, "
        f"`{STATUS_LARGE}={counts[STATUS_LARGE]}`, "
        f"`{STATUS_LOW_DATA}={counts[STATUS_LOW_DATA]}`, "
        f"`{STATUS_NO_DATA}={counts[STATUS_NO_DATA]}`"
    )
    lines.append(
        f"- Drift thresholds: REVIEW ≥ {int(MODERATE_DRIFT_PCT * 100)}%, "
        f"LARGE ≥ {int(LARGE_DRIFT_PCT * 100)}%; "
        f"LOW-DATA when samples < {MIN_SAMPLES_FOR_CONFIDENCE}"
    )
    lines.append("")

    if result.total_paired == 0:
        lines.append("> **No paired sandbox launches in window — nothing to calibrate.**")
        return "\n".join(lines) + "\n"

    headers = (
        "Class", "Tier", "Old", "New", "Δ", "Samples",
        "Mean dur (s)", "Mean CPU·s", "Mean Δmem (MB)",
        "Peak mem (MB)", "OOMs", "Status",
    )
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")

    # Pre-compute per-row data so we can sort by drift magnitude.
    # Tiebreak alphabetically so output stays deterministic across
    # identical inputs (important for diff-of-diffs in PR review).
    row_data: list[tuple[float, str, ClassStats, float, float, str]] = []
    for name in result.classes.keys():
        s = result.classes[name]
        old = baseline_for(name, result)
        new = result.new_weights.get(name, s.old_tokens)
        status = classify_drift(s, old, new)
        magnitude = abs(drift_pct(old, new))
        row_data.append((magnitude, name, s, old, new, status))
    row_data.sort(key=lambda r: (-r[0], r[1]))

    for _magnitude, name, s, old, new, status in row_data:
        delta = new - old
        sign = "+" if delta > 0 else ("−" if delta < 0 else " ")
        delta_str = f"{sign}{abs(delta):.2f}" if delta else "—"
        dmem_str = (
            f"{s.mean_delta_mem_peak_mb:.0f}"
            if s.delta_mem_sample_count else "—"
        )
        lines.append(
            "| {name} | {tier} | {old:.2f} | {new:.2f} | {delta} | "
            "{count} | {dur:.2f} | {cs:.2f} | {dmem} | {mem:.0f} | "
            "{oom} | {status} |".format(
                name=name,
                tier=s.tier,
                old=old,
                new=new,
                delta=delta_str,
                count=s.sample_count,
                dur=s.mean_duration_s,
                cs=s.mean_cpu_token_s,
                dmem=dmem_str,
                mem=s.peak_mem_mb,
                oom=s.oom_count,
                status=status,
            )
        )
    lines.append("")
    if verdict == VERDICT_APPLY:
        lines.append("> ✅ Safe to run with `--apply` — persists the new weights to "
                     "`configs/sandbox_cost_weights.yaml` and appends a "
                     "`sandbox_cost_calibration` row to the audit hash chain.")
    elif verdict == VERDICT_REVIEW:
        lines.append("> ⚠ Review the rows tagged `LARGE` / `LOW-DATA` above before "
                     "running with `--apply`. Operators with out-of-band context "
                     "(e.g. known stress-test workload) may override.")
    else:  # VERDICT_SKIP
        lines.append("> ⏸ Calibration produced no actionable signal — re-run after "
                     "more sandbox traffic has accumulated. Do **not** `--apply`.")
    return "\n".join(lines) + "\n"


def render_json(result: CalibrationResult) -> str:
    """JSON form of the diff report — mirrors the text view's data.

    Adds three top-level keys consumers (CI gates, dashboards) read:
        * ``baseline_source`` — provenance of the comparison baseline
        * ``summary`` — bucket counts per status tag
        * ``recommendation`` — ``{verdict, reason}`` advisory

    Each per-class block adds:
        * ``baseline_tokens`` — what the calibrator compared against
          (yaml value if present, H4a hardcode otherwise)
        * ``status`` — one of OK / REVIEW / LARGE / LOW-DATA / NO-DATA
        * ``drift_pct`` — signed (new - baseline) / |baseline|

    ``old_tokens`` is preserved for backwards compatibility with the
    skeleton consumers; new code should prefer ``baseline_tokens``.
    """
    counts = summarise_calibration(result)
    verdict, reason = recommend_action(result)
    classes_payload: dict[str, Any] = {}
    for name, s in result.classes.items():
        baseline_t = baseline_for(name, result)
        new_t = result.new_weights.get(name, s.old_tokens)
        classes_payload[name] = {
            "tier": s.tier,
            "old_tokens": s.old_tokens,
            "baseline_tokens": baseline_t,
            "new_tokens": new_t,
            "drift_pct": drift_pct(baseline_t, new_t),
            "status": classify_drift(s, baseline_t, new_t),
            "sample_count": s.sample_count,
            "mean_duration_s": s.mean_duration_s,
            "mean_cpu_token_s": s.mean_cpu_token_s,
            "mean_delta_mem_peak_mb": s.mean_delta_mem_peak_mb,
            "delta_mem_sample_count": s.delta_mem_sample_count,
            "peak_mem_mb": s.peak_mem_mb,
            "oom_count": s.oom_count,
        }
    payload: dict[str, Any] = {
        "generated_at": result.generated_at,
        "window_days": result.window_days,
        "window_start_ts": result.window_start_ts,
        "window_end_ts": result.window_end_ts,
        "total_paired": result.total_paired,
        "total_orphaned": result.total_orphaned,
        "host_ring_size": result.host_ring_size,
        "baseline_source": result.baseline_source,
        "summary": counts,
        "recommendation": {"verdict": verdict, "reason": reason},
        "classes": classes_payload,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  YAML write + audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def render_yaml(result: CalibrationResult) -> str:
    """Render the calibrated weights as YAML.

    Stdlib-only — we hand-write the small structure to avoid pulling
    PyYAML into the script's runtime dependency surface (PyYAML is in
    the backend image but a fresh dev box running this tool may not
    have it). The structure stays trivial enough that this is safe.
    """
    canonical = canonical_class_table()
    iso = datetime.fromtimestamp(result.generated_at, timezone.utc).isoformat(
        timespec="seconds",
    )
    lines: list[str] = []
    lines.append("# H4b — Auto-generated sandbox cost weights.")
    lines.append("# Source: scripts/calibrate_sandbox_cost.py")
    lines.append("# Replaces the H4a hardcoded values in")
    lines.append("# backend.sandbox_capacity.SandboxCostWeight when")
    lines.append("# loaded by the runtime (see consumer wiring in I6).")
    lines.append(f"generated_at: '{iso}'")
    lines.append(f"calibration_window_days: {result.window_days}")
    lines.append(f"sample_count: {result.total_paired}")
    lines.append(f"orphaned_count: {result.total_orphaned}")
    lines.append("weights:")
    for name in sorted(result.classes.keys()):
        s = result.classes[name]
        meta = canonical.get(name, {})
        new = float(result.new_weights.get(name, s.old_tokens))
        # Memory floor — keep the H4a memory envelope intact when we
        # didn't observe a peak (no OOMs, no parseable launch limit).
        peak_mem = int(round(s.peak_mem_mb)) if s.peak_mem_mb > 0 \
            else int(meta.get("memory_mb", 0))
        cpu_cores = float(meta.get("cpu_cores", 1.0))
        burst = bool(meta.get("burst", False))
        use_case = str(meta.get("use_case", "")).replace("'", "''")
        lines.append(f"  {name}:")
        lines.append(f"    tokens: {new}")
        lines.append(f"    memory_mb: {peak_mem}")
        lines.append(f"    cpu_cores: {cpu_cores}")
        lines.append(f"    burst: {'true' if burst else 'false'}")
        lines.append(f"    use_case: '{use_case}'")
        lines.append(f"    sample_count: {s.sample_count}")
        lines.append(f"    mean_duration_s: {round(s.mean_duration_s, 3)}")
        lines.append(
            f"    mean_delta_mem_peak_mb: "
            f"{round(s.mean_delta_mem_peak_mb, 1)}"
        )
        lines.append(
            f"    delta_mem_sample_count: {s.delta_mem_sample_count}"
        )
    return "\n".join(lines) + "\n"


def write_yaml(result: CalibrationResult, path: Path) -> None:
    """Write the calibrated weights atomically (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = render_yaml(result)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


async def emit_audit_row(result: CalibrationResult, out_path: Path) -> bool:
    """Append a ``sandbox_cost_calibration`` row to the audit hash chain.

    Best-effort: if the audit module isn't reachable (no DB pool, fresh
    install, dev box without PG running) we log + return False rather
    than refusing the apply — the yaml is the operator-visible truth,
    audit is the secondary integrity record.
    """
    try:
        from backend import audit
    except Exception as exc:
        logger.warning("audit module import failed; skipping chain row: %s", exc)
        return False
    diff: dict[str, Any] = {}
    for name, s in result.classes.items():
        new = result.new_weights.get(name, s.old_tokens)
        if abs(new - s.old_tokens) > 1e-6 or s.sample_count > 0:
            diff[name] = {
                "old_tokens": s.old_tokens,
                "new_tokens": new,
                "sample_count": s.sample_count,
                "mean_duration_s": round(s.mean_duration_s, 3),
                "peak_mem_mb": round(s.peak_mem_mb, 1),
            }
    after = {
        "config_path": str(out_path),
        "window_days": result.window_days,
        "total_paired": result.total_paired,
        "total_orphaned": result.total_orphaned,
        "weights": diff,
    }
    try:
        # Audit module needs a PG pool; init one from the env DSN if the
        # caller hasn't already.
        await _ensure_pg_pool()
        await audit.log(
            action="sandbox_cost_calibration",
            entity_kind="config",
            entity_id="sandbox_cost_weights.yaml",
            before=None,
            after=after,
            actor="system:calibrate_sandbox_cost",
        )
        return True
    except Exception as exc:
        logger.warning("audit.log failed; yaml still written: %s", exc)
        return False


async def _ensure_pg_pool() -> None:
    """Open the asyncpg pool if a DSN is set and no pool exists yet."""
    dsn = os.environ.get("OMNISIGHT_DATABASE_URL", "").strip()
    if not dsn:
        return
    from backend import db_pool
    try:
        db_pool.get_pool()
        return  # already initialised in this process
    except RuntimeError:
        await db_pool.init_pool(dsn, min_size=1, max_size=2)


async def _close_pg_pool_if_open() -> None:
    try:
        from backend import db_pool
        await db_pool.close_pool()
    except Exception:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Best-effort host_metrics probe (live ring buffer)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def host_ring_depth() -> int:
    """Return the live ``host_metrics`` ring buffer size, or 0 on failure.

    Read from this process's view (``host_metrics.get_host_history()``)
    — useful when calibration runs in the same process as the backend.
    Out-of-process operators get 0; the diff just won't show the line.
    """
    try:
        from backend import host_metrics  # type: ignore[no-redef]
        return len(host_metrics.get_host_history())
    except Exception as exc:
        logger.debug("host_metrics ring probe failed: %s", exc)
        return 0


def host_ring_snapshots() -> list[Any]:
    """Return a copy of the live ``host_metrics`` ring (oldest first).

    Used by the CLI to feed Δmem_peak computation. Returns an empty
    list when the backend module isn't importable or the sampler
    hasn't produced anything yet — calibrate() then skips Δmem
    accumulation rather than feeding it zeros.
    """
    try:
        from backend import host_metrics  # type: ignore[no-redef]
        return list(host_metrics.get_host_history())
    except Exception as exc:
        logger.debug("host_metrics ring fetch failed: %s", exc)
        return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="H4b — Sandbox cost weight calibration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/calibrate_sandbox_cost.py\n"
            "  python scripts/calibrate_sandbox_cost.py --days 14 --format json\n"
            "  python scripts/calibrate_sandbox_cost.py --apply\n"
        ),
    )
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"calibration window in days (default: {DEFAULT_DAYS})")
    p.add_argument("--format", choices=("text", "json"), default="text",
                   help="diff report format (default: text/markdown)")
    p.add_argument("--apply", action="store_true",
                   help="write the new weights to "
                        "configs/sandbox_cost_weights.yaml + audit row")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH,
                   help=f"yaml output path (default: {DEFAULT_OUT_PATH})")
    p.add_argument("--baseline", type=Path, default=None,
                   help="compare new weights against this yaml instead of "
                        "the H4a hardcode (defaults to --out path when it "
                        "exists, so re-runs after the first --apply diff "
                        "against what's actually live)")
    p.add_argument("--no-baseline", action="store_true",
                   help="force comparison against the H4a hardcode even "
                        "when a persisted yaml exists (rare; useful for "
                        "auditing how far live weights have drifted from "
                        "the original code defaults)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="enable debug logging")
    return p


async def _async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.days < 1:
        print("--days must be >= 1", file=sys.stderr)
        return 2

    now = time.time()
    since_ts = now - args.days * 86400.0

    try:
        rows = await fetch_sandbox_rows(since_ts)
    except Exception as exc:
        logger.error("audit_log fetch failed: %s", exc)
        return 1
    finally:
        await _close_pg_pool_if_open()

    snaps = host_ring_snapshots()
    result = calibrate(
        rows,
        window_days=args.days,
        now=now,
        host_ring_size=len(snaps),
        host_snapshots=snaps,
    )

    # Resolve comparison baseline. Precedence:
    #   --no-baseline     → force H4a hardcode
    #   --baseline PATH   → load from explicit path (warns if missing)
    #   default           → load from --out if it exists, else hardcode
    if not args.no_baseline:
        baseline_path: Path | None = None
        if args.baseline is not None:
            baseline_path = args.baseline
        elif args.out.exists():
            baseline_path = args.out
        if baseline_path is not None:
            weights, source = load_baseline_weights(baseline_path)
            result.baseline_weights = weights
            result.baseline_source = source

    if args.format == "json":
        sys.stdout.write(render_json(result) + "\n")
    else:
        sys.stdout.write(render_text(result))

    if result.total_paired == 0:
        # Diff was rendered (so the operator sees the empty notice);
        # exit non-zero so a CI cron treats "no data" as a soft alert.
        return 3

    if args.apply:
        write_yaml(result, args.out)
        ok = await emit_audit_row(result, args.out)
        await _close_pg_pool_if_open()
        sys.stdout.write(
            f"\nWrote {args.out}"
            + (" (audit chain row appended)\n" if ok
               else " (audit chain row SKIPPED — see warnings above)\n")
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
