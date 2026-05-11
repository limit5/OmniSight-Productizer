#!/usr/bin/env python3
"""OP-879 D7 -- post-deploy smoke + metric baseline comparator.

Walks a fixed list of 10 routes (``/health``, ``/api/v1/changes`` plus
8 key MCP endpoints), measures per-route latency p50/p95/p99, compares
each route's p95 to the ``metric_baselines`` row, and decides:

* within ``WARN_PCT`` (default 20%) of baseline       -> ``pass``
* within ``ABORT_PCT`` (default 50%) of baseline      -> ``warn``
* beyond ``ABORT_PCT`` of baseline                    -> ``abort``

On ``abort`` the comparator emits an SSE ``staging.smoke.failed`` event
(via the injected ``event_sink``) and invokes the D6 rollback path
(``scripts/bluegreen_switch.sh rollback`` -- injected as ``rollback_fn``
so tests can run without touching the deploy state directory).

Error catalog (OP-879)
----------------------
* ``BaselineMissing``        -- first deploy; bootstraps baseline from
                                the current measurement and emits
                                ``staging.smoke.bootstrap`` warn event.
* ``MetricFetchTimeout``     -- partial result; emits a
                                ``staging.smoke.partial`` warn event so
                                operator can review.
* ``SmokeAbortTriggered``    -- p95 regression beyond ``ABORT_PCT``;
                                triggers SSE + rollback.

Sub-commands
------------
``compare``    run measurement against ``--base-url`` and decide.
               (Default. Used by the staging deploy post-cut hook.)
``snapshot``   operator-approved re-snapshot of baselines from a fresh
               measurement (AC #5). Always overwrites the existing rows.

Idempotency
-----------
* ``compare`` is read-only against ``metric_baselines`` -- repeated
  runs produce the same decision for the same measurement.
* ``snapshot`` is an UPSERT keyed on ``endpoint`` -- repeated runs
  produce one row per endpoint regardless of invocation count.

Module-global / cross-worker state audit
----------------------------------------
No module-level singletons. ``DEFAULT_ROUTES`` is a frozen tuple of
``Endpoint``; mutation by tests is forbidden by tuple semantics. The
sample / fetch / event-sink / rollback callables are all injected so
the production wiring (HTTP fetch, subprocess rollback, JSON SSE) can
be swapped out for in-process fakes under pytest.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib import error as urlerror, request


# ── Constants ───────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_WARN_PCT = 0.20
DEFAULT_ABORT_PCT = 0.50
DEFAULT_SAMPLE_COUNT = 25
DEFAULT_REQUEST_TIMEOUT_S = 5.0
DEFAULT_TOTAL_BUDGET_S = 120.0

EVENT_SMOKE_PASS = "staging.smoke.pass"
EVENT_SMOKE_WARN = "staging.smoke.warn"
EVENT_SMOKE_FAILED = "staging.smoke.failed"
EVENT_SMOKE_BOOTSTRAP = "staging.smoke.bootstrap"
EVENT_SMOKE_PARTIAL = "staging.smoke.partial"

DECISION_PASS = "pass"
DECISION_WARN = "warn"
DECISION_ABORT = "abort"


@dataclass(frozen=True)
class Endpoint:
    """A single route under comparator surveillance."""
    name: str
    path: str


# 10 fixed routes per AC #2: /health, /api/v1/changes, 8 MCP endpoints.
DEFAULT_ROUTES: tuple[Endpoint, ...] = (
    Endpoint("health", "/health"),
    Endpoint("changes", "/api/v1/changes"),
    Endpoint("mcp_list_tools", "/api/v1/mcp/tools"),
    Endpoint("mcp_list_resources", "/api/v1/mcp/resources"),
    Endpoint("mcp_invoke_tool", "/api/v1/mcp/tools/invoke"),
    Endpoint("mcp_read_resource", "/api/v1/mcp/resources/read"),
    Endpoint("mcp_list_prompts", "/api/v1/mcp/prompts"),
    Endpoint("mcp_get_prompt", "/api/v1/mcp/prompts/get"),
    Endpoint("mcp_jira_search", "/api/v1/mcp/jira/search"),
    Endpoint("mcp_graphiti_query", "/api/v1/mcp/graphiti/query"),
)


# ── Error catalog ────────────────────────────────────────────────


class BaselineMissing(Exception):
    """First deploy; no baseline rows exist. Bootstrap + warn."""


class MetricFetchTimeout(Exception):
    """One or more endpoints exceeded ``--request-timeout``.

    The comparator records the partial result and emits
    ``staging.smoke.partial`` so the operator can review. Does NOT
    trigger rollback -- a fetch timeout is ambiguous (could be a
    transient network blip, not a regression).
    """


class SmokeAbortTriggered(Exception):
    """p95 regression > ``ABORT_PCT`` on at least one route.

    Carries the structured abort payload so the caller can inspect /
    re-emit / forward to alerting if the in-process SSE sink already
    fired.
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        super().__init__(payload.get("summary", "smoke abort triggered"))
        self.payload = dict(payload)


# ── Data carriers ────────────────────────────────────────────────


@dataclass(frozen=True)
class Measurement:
    """Per-endpoint percentile snapshot."""
    endpoint: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    sample_count: int


@dataclass(frozen=True)
class Baseline:
    """Per-endpoint baseline row loaded from ``metric_baselines``."""
    endpoint: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    sample_count: int
    deployment_tag: str = ""


@dataclass
class CompareReport:
    """Final compare outcome, returned by :func:`run_smoke_compare`."""
    decision: str  # "pass" | "warn" | "abort"
    measurements: list[Measurement]
    baselines_used: dict[str, Baseline]
    per_endpoint: list[dict[str, Any]] = field(default_factory=list)
    timed_out_endpoints: list[str] = field(default_factory=list)
    bootstrap: bool = False
    rollback_invoked: bool = False
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "summary": self.summary,
            "bootstrap": self.bootstrap,
            "rollback_invoked": self.rollback_invoked,
            "timed_out_endpoints": list(self.timed_out_endpoints),
            "per_endpoint": list(self.per_endpoint),
            "measurements": [
                {
                    "endpoint": m.endpoint,
                    "p50_ms": m.p50_ms,
                    "p95_ms": m.p95_ms,
                    "p99_ms": m.p99_ms,
                    "sample_count": m.sample_count,
                }
                for m in self.measurements
            ],
        }


# ── Percentile math ──────────────────────────────────────────────


def percentile(samples_ms: Sequence[float], pct: float) -> float:
    """Return the ``pct``-percentile (0..1) of ``samples_ms`` in ms.

    Uses linear interpolation between the two surrounding samples
    (NIST style). Returns 0.0 for an empty sequence so the caller can
    surface "no data" via ``sample_count`` rather than raise here --
    raising would force every caller to wrap, and the
    ``sample_count==0`` signal is already present in the row.
    """
    if not samples_ms:
        return 0.0
    if not 0.0 <= pct <= 1.0:
        raise ValueError(f"pct must be in [0, 1], got {pct!r}")
    ordered = sorted(samples_ms)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = pct * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


# ── Sampler / fetcher ────────────────────────────────────────────


Sampler = Callable[[Endpoint, int, float], list[float]]
"""``(endpoint, sample_count, request_timeout_s) -> list[latency_ms]``."""


def http_sampler(
    base_url: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Sampler:
    """Return a :data:`Sampler` that times real HTTP GETs.

    Each sample is a fresh request. Connection reuse is intentionally
    not done -- staging smoke is a tiny burst (10 routes x ~25 samples)
    where per-request overhead matters less than measurement
    isolation. A timed-out request raises :class:`MetricFetchTimeout`
    so the orchestrator can record the partial-result path; other HTTP
    error codes (4xx/5xx) are still timed and returned because
    end-to-end latency on an error response is itself a valid signal.
    """
    static_headers = dict(headers or {})
    base = base_url.rstrip("/")

    def _sample(endpoint: Endpoint, n: int, timeout_s: float) -> list[float]:
        url = f"{base}{endpoint.path}"
        out: list[float] = []
        for _ in range(n):
            req = request.Request(url, headers=static_headers, method="GET")
            start = time.perf_counter()
            try:
                with request.urlopen(req, timeout=timeout_s) as resp:
                    resp.read()
            except urlerror.HTTPError:
                pass  # error responses still count toward latency
            except (TimeoutError, urlerror.URLError) as exc:
                if "timed out" in str(exc).lower() or isinstance(
                    getattr(exc, "reason", None), TimeoutError
                ):
                    raise MetricFetchTimeout(
                        f"{endpoint.path}: request timed out after {timeout_s}s"
                    ) from exc
                raise
            out.append((time.perf_counter() - start) * 1000.0)
        return out

    return _sample


def measure_endpoint(
    endpoint: Endpoint,
    sampler: Sampler,
    *,
    sample_count: int,
    request_timeout_s: float,
) -> Measurement:
    """Drive the ``sampler`` and reduce to a :class:`Measurement`."""
    samples = sampler(endpoint, sample_count, request_timeout_s)
    return Measurement(
        endpoint=endpoint.path,
        p50_ms=percentile(samples, 0.50),
        p95_ms=percentile(samples, 0.95),
        p99_ms=percentile(samples, 0.99),
        sample_count=len(samples),
    )


# ── DB access ────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_baselines(conn: sqlite3.Connection) -> dict[str, Baseline]:
    """Return ``{endpoint_path: Baseline}`` from ``metric_baselines``.

    Empty dict means "first deploy" -- the caller bootstraps.
    """
    cur = conn.execute(
        "SELECT endpoint, p50_ms, p95_ms, p99_ms, sample_count, deployment_tag "
        "FROM metric_baselines"
    )
    out: dict[str, Baseline] = {}
    for row in cur.fetchall():
        out[row[0]] = Baseline(
            endpoint=row[0],
            p50_ms=float(row[1]),
            p95_ms=float(row[2]),
            p99_ms=float(row[3]),
            sample_count=int(row[4]),
            deployment_tag=row[5] or "",
        )
    return out


def upsert_baselines(
    conn: sqlite3.Connection,
    measurements: Iterable[Measurement],
    *,
    deployment_tag: str = "",
) -> int:
    """UPSERT each ``measurement`` into ``metric_baselines``.

    Returns the number of rows written. Uses the SQLite-compatible
    ``INSERT ... ON CONFLICT(endpoint) DO UPDATE`` form so a second
    invocation (e.g. operator re-snapshot) replaces rather than
    duplicates -- the alembic schema declares ``endpoint`` as PK.
    """
    n = 0
    now = _utc_now_iso()
    for m in measurements:
        conn.execute(
            """
            INSERT INTO metric_baselines
                (endpoint, p50_ms, p95_ms, p99_ms, sample_count,
                 deployment_tag, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p50_ms = excluded.p50_ms,
                p95_ms = excluded.p95_ms,
                p99_ms = excluded.p99_ms,
                sample_count = excluded.sample_count,
                deployment_tag = excluded.deployment_tag,
                captured_at = excluded.captured_at
            """,
            (
                m.endpoint,
                m.p50_ms,
                m.p95_ms,
                m.p99_ms,
                m.sample_count,
                deployment_tag,
                now,
            ),
        )
        n += 1
    conn.commit()
    return n


# ── Decision ─────────────────────────────────────────────────────


def classify_delta(
    *,
    baseline_p95: float,
    measured_p95: float,
    warn_pct: float,
    abort_pct: float,
) -> tuple[str, float]:
    """Return ``(decision, delta_ratio)`` for a single endpoint.

    ``delta_ratio`` is signed (negative = faster than baseline). The
    decision uses the absolute value -- AC says "+/-20% / +/-50%" so
    a faster-than-baseline result also trips the threshold (the
    operator should know if traffic shape suddenly inverted).

    A zero baseline (which only happens for legitimate ~0ms endpoints
    or a corrupted row) maps any non-zero measurement to ``abort``
    because the ratio is undefined. The
    ``BaselineMissing`` path handles the genuine no-baseline case
    upstream so this branch only catches the corrupted-row edge.
    """
    if baseline_p95 <= 0.0:
        if measured_p95 <= 0.0:
            return DECISION_PASS, 0.0
        return DECISION_ABORT, float("inf")
    delta = (measured_p95 - baseline_p95) / baseline_p95
    mag = abs(delta)
    if mag > abort_pct:
        return DECISION_ABORT, delta
    if mag > warn_pct:
        return DECISION_WARN, delta
    return DECISION_PASS, delta


def _aggregate_decision(per_endpoint: Sequence[Mapping[str, Any]]) -> str:
    """Roll up per-endpoint decisions to a single overall decision."""
    seen = {row["decision"] for row in per_endpoint}
    if DECISION_ABORT in seen:
        return DECISION_ABORT
    if DECISION_WARN in seen:
        return DECISION_WARN
    return DECISION_PASS


# ── Event sink + rollback adapters ───────────────────────────────


EventSink = Callable[[str, Mapping[str, Any]], None]
RollbackFn = Callable[[], bool]
"""Returns True on rollback success, False otherwise."""


def stdout_event_sink(event: str, payload: Mapping[str, Any]) -> None:
    """Emit one JSON line per event. Used as the production sink."""
    record = {
        "timestamp": _utc_now_iso(),
        "level": "INFO" if event != EVENT_SMOKE_FAILED else "ERROR",
        "event": event,
        **payload,
    }
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


def bluegreen_rollback() -> bool:
    """Invoke ``scripts/bluegreen_switch.sh rollback`` and return ok.

    Separated from the orchestrator so tests can pass an in-process
    spy. Production wiring shells out with a 60s timeout (the rollback
    primitive itself is ~instant; the budget covers caddy reload).
    """
    script = REPO_ROOT / "scripts" / "bluegreen_switch.sh"
    try:
        subprocess.run(
            [str(script), "rollback"],
            check=True,
            timeout=60,
            capture_output=True,
            text=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


# ── Orchestrator ────────────────────────────────────────────────


def _measure_all(
    routes: Sequence[Endpoint],
    sampler: Sampler,
    *,
    sample_count: int,
    request_timeout_s: float,
    total_budget_s: float,
) -> tuple[list[Measurement], list[str]]:
    """Measure every route, returning (measurements, timed_out_paths).

    Honours ``total_budget_s`` as a soft cap; once exceeded the
    remaining endpoints are recorded as timeouts. Per-endpoint
    timeouts (raised by :class:`MetricFetchTimeout` from the sampler)
    are caught and recorded so the caller can decide whether to
    surface ``MetricFetchTimeout`` once at the end (partial result).
    """
    out: list[Measurement] = []
    timed_out: list[str] = []
    deadline = time.monotonic() + total_budget_s
    for endpoint in routes:
        if time.monotonic() >= deadline:
            timed_out.append(endpoint.path)
            continue
        try:
            out.append(
                measure_endpoint(
                    endpoint,
                    sampler,
                    sample_count=sample_count,
                    request_timeout_s=request_timeout_s,
                )
            )
        except MetricFetchTimeout:
            timed_out.append(endpoint.path)
    return out, timed_out


def run_smoke_compare(
    conn: sqlite3.Connection,
    sampler: Sampler,
    *,
    routes: Sequence[Endpoint] = DEFAULT_ROUTES,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    total_budget_s: float = DEFAULT_TOTAL_BUDGET_S,
    warn_pct: float = DEFAULT_WARN_PCT,
    abort_pct: float = DEFAULT_ABORT_PCT,
    deployment_tag: str = "",
    event_sink: EventSink = stdout_event_sink,
    rollback_fn: RollbackFn = bluegreen_rollback,
) -> CompareReport:
    """Run one smoke compare cycle (AC #1-4).

    Returns a :class:`CompareReport`. Does NOT raise on
    :class:`SmokeAbortTriggered` -- the rollback + SSE side-effects
    are performed in-band so the caller can inspect ``report.decision``
    and ``report.rollback_invoked`` for the full picture. The CLI
    main translates ``decision == 'abort'`` to a non-zero exit code.
    """
    baselines = load_baselines(conn)
    bootstrap = not baselines

    measurements, timed_out = _measure_all(
        routes,
        sampler,
        sample_count=sample_count,
        request_timeout_s=request_timeout_s,
        total_budget_s=total_budget_s,
    )

    # ── Bootstrap path ────────────────────────────────────────
    # AC: "BaselineMissing -- first deploy; bootstrap baseline + warn."
    if bootstrap:
        upsert_baselines(conn, measurements, deployment_tag=deployment_tag)
        report = CompareReport(
            decision=DECISION_WARN,
            measurements=measurements,
            baselines_used={},
            per_endpoint=[
                {
                    "endpoint": m.endpoint,
                    "decision": DECISION_WARN,
                    "delta_ratio": None,
                    "measured_p95_ms": m.p95_ms,
                    "baseline_p95_ms": None,
                    "reason": "bootstrap",
                }
                for m in measurements
            ],
            timed_out_endpoints=timed_out,
            bootstrap=True,
            rollback_invoked=False,
            summary=(
                f"BaselineMissing: bootstrapped {len(measurements)} baselines "
                f"from current run (deployment_tag={deployment_tag or '<none>'})"
            ),
        )
        event_sink(EVENT_SMOKE_BOOTSTRAP, report.as_dict())
        if timed_out:
            event_sink(EVENT_SMOKE_PARTIAL, {
                "timed_out_endpoints": list(timed_out),
                "deployment_tag": deployment_tag,
            })
        return report

    # ── Compare path ──────────────────────────────────────────
    per_endpoint: list[dict[str, Any]] = []
    baselines_used: dict[str, Baseline] = {}
    for m in measurements:
        b = baselines.get(m.endpoint)
        if b is None:
            # New endpoint added to DEFAULT_ROUTES after baseline was
            # captured. Treat as bootstrap-equivalent for this row --
            # warn + upsert so the next compare has a baseline.
            upsert_baselines(conn, [m], deployment_tag=deployment_tag)
            per_endpoint.append({
                "endpoint": m.endpoint,
                "decision": DECISION_WARN,
                "delta_ratio": None,
                "measured_p95_ms": m.p95_ms,
                "baseline_p95_ms": None,
                "reason": "endpoint_missing_in_baseline",
            })
            continue
        baselines_used[m.endpoint] = b
        decision, delta = classify_delta(
            baseline_p95=b.p95_ms,
            measured_p95=m.p95_ms,
            warn_pct=warn_pct,
            abort_pct=abort_pct,
        )
        per_endpoint.append({
            "endpoint": m.endpoint,
            "decision": decision,
            "delta_ratio": delta,
            "measured_p95_ms": m.p95_ms,
            "baseline_p95_ms": b.p95_ms,
            "reason": "",
        })

    overall = _aggregate_decision(per_endpoint)
    bad = [row for row in per_endpoint if row["decision"] == DECISION_ABORT]

    report = CompareReport(
        decision=overall,
        measurements=measurements,
        baselines_used=baselines_used,
        per_endpoint=per_endpoint,
        timed_out_endpoints=timed_out,
        bootstrap=False,
        rollback_invoked=False,
    )
    if overall == DECISION_ABORT:
        report.summary = (
            f"SmokeAbortTriggered: {len(bad)} endpoint(s) regressed "
            f"beyond +/-{int(abort_pct * 100)}% p95 "
            f"(deployment_tag={deployment_tag or '<none>'})"
        )
        rollback_ok = rollback_fn()
        report.rollback_invoked = rollback_ok
        payload = report.as_dict()
        payload["deployment_tag"] = deployment_tag
        event_sink(EVENT_SMOKE_FAILED, payload)
    elif overall == DECISION_WARN:
        report.summary = (
            f"Smoke warn: {sum(1 for r in per_endpoint if r['decision'] == DECISION_WARN)} "
            f"endpoint(s) drifted beyond +/-{int(warn_pct * 100)}% p95"
        )
        event_sink(EVENT_SMOKE_WARN, report.as_dict())
    else:
        report.summary = (
            f"Smoke pass: all {len(per_endpoint)} endpoints within "
            f"+/-{int(warn_pct * 100)}% p95 of baseline"
        )
        event_sink(EVENT_SMOKE_PASS, report.as_dict())

    if timed_out:
        # AC: MetricFetchTimeout -- partial result; require manual review
        event_sink(EVENT_SMOKE_PARTIAL, {
            "timed_out_endpoints": list(timed_out),
            "deployment_tag": deployment_tag,
            "overall_decision": overall,
        })

    return report


def run_snapshot(
    conn: sqlite3.Connection,
    sampler: Sampler,
    *,
    routes: Sequence[Endpoint] = DEFAULT_ROUTES,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    total_budget_s: float = DEFAULT_TOTAL_BUDGET_S,
    deployment_tag: str = "",
) -> CompareReport:
    """Operator-approved baseline re-snapshot (AC #5).

    Always overwrites every row from the fresh measurement. Skips the
    comparator entirely -- the operator has explicitly opted in to
    "this is the new normal".
    """
    measurements, timed_out = _measure_all(
        routes,
        sampler,
        sample_count=sample_count,
        request_timeout_s=request_timeout_s,
        total_budget_s=total_budget_s,
    )
    if timed_out:
        raise MetricFetchTimeout(
            "snapshot refuses to commit a partial baseline; "
            f"timed out on: {', '.join(timed_out)}"
        )
    upsert_baselines(conn, measurements, deployment_tag=deployment_tag)
    return CompareReport(
        decision=DECISION_PASS,
        measurements=measurements,
        baselines_used={},
        per_endpoint=[],
        timed_out_endpoints=[],
        bootstrap=False,
        rollback_invoked=False,
        summary=(
            f"snapshot committed {len(measurements)} baselines "
            f"(deployment_tag={deployment_tag or '<none>'})"
        ),
    )


# ── CLI ─────────────────────────────────────────────────────────


def _resolve_db_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("OMNISIGHT_DATABASE_PATH")
    if env:
        return Path(env)
    return REPO_ROOT / "data" / "omnisight.db"


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metric_baselines (
            endpoint        TEXT PRIMARY KEY,
            p50_ms          REAL NOT NULL CHECK (p50_ms >= 0),
            p95_ms          REAL NOT NULL CHECK (p95_ms >= 0),
            p99_ms          REAL NOT NULL CHECK (p99_ms >= 0),
            sample_count    INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
            deployment_tag  TEXT NOT NULL DEFAULT '',
            captured_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_baseline_compare",
        description=(
            "OP-879 D7 post-deploy smoke + metric baseline comparator. "
            "Default sub-command is `compare`."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "OMNISIGHT_STAGING_URL", "http://localhost:8000"
        ),
        help="Base URL to measure (default: $OMNISIGHT_STAGING_URL or localhost:8000)",
    )
    parser.add_argument("--db-path", default=None, help="SQLite DB path")
    parser.add_argument(
        "--sample-count", type=int, default=DEFAULT_SAMPLE_COUNT,
        help=f"requests per endpoint (default: {DEFAULT_SAMPLE_COUNT})",
    )
    parser.add_argument(
        "--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT_S,
        help="per-request timeout in seconds",
    )
    parser.add_argument(
        "--total-budget", type=float, default=DEFAULT_TOTAL_BUDGET_S,
        help="overall measurement budget in seconds",
    )
    parser.add_argument(
        "--warn-pct", type=float, default=DEFAULT_WARN_PCT,
        help="p95 drift fraction that emits warn (default: 0.20 = +/-20%%)",
    )
    parser.add_argument(
        "--abort-pct", type=float, default=DEFAULT_ABORT_PCT,
        help="p95 drift fraction that triggers abort (default: 0.50 = +/-50%%)",
    )
    parser.add_argument(
        "--deployment-tag", default="",
        help="image tag / SHA recorded with the baseline / abort event",
    )
    parser.add_argument(
        "--snapshot", action="store_true",
        help="re-snapshot baselines instead of comparing (AC #5)",
    )
    args = parser.parse_args(argv)

    db_path = _resolve_db_path(args.db_path)
    conn = _open_db(db_path)
    sampler = http_sampler(args.base_url)

    if args.snapshot:
        report = run_snapshot(
            conn,
            sampler,
            sample_count=args.sample_count,
            request_timeout_s=args.request_timeout,
            total_budget_s=args.total_budget,
            deployment_tag=args.deployment_tag,
        )
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return 0

    report = run_smoke_compare(
        conn,
        sampler,
        sample_count=args.sample_count,
        request_timeout_s=args.request_timeout,
        total_budget_s=args.total_budget,
        warn_pct=args.warn_pct,
        abort_pct=args.abort_pct,
        deployment_tag=args.deployment_tag,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    if report.decision == DECISION_ABORT:
        return 2
    if report.decision == DECISION_WARN:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
