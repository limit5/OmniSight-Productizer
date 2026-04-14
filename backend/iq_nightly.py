"""Phase 63-D D3 — nightly IQ benchmark orchestrator.

Wires D1 (schema + curated YAML) + D2 (runner) into:
  1. persist_run(reports)    — INSERT each benchmark × model row
  2. recent_scores(model, n) — trailing-days lookup
  3. detect_regression(model, threshold_pp=10):
         — True iff the latest TWO runs (≥2 consecutive days) both fell
           more than `threshold_pp` below the 7-day baseline median
  4. nightly(models, *, ask_fn=...) — the actual loop:
         load_all benchmarks → run_all → persist → publish Gauge
         → detect regression → notify (level=action) once per model/day

Opt-in: disabled unless OMNISIGHT_SELF_IMPROVE_LEVEL includes L3
(same pattern as Phase 62 skills extraction).

The notify call is awaited but any failure is swallowed — a down
Jira / Slack must never take down the benchmark record.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from backend import iq_benchmark as ib
from backend import iq_runner as ir

logger = logging.getLogger(__name__)

DEFAULT_REGRESSION_PP = 10.0   # 10 percentage points below baseline
DEFAULT_BASELINE_DAYS = 7
SECONDS_PER_DAY = 86400


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Opt-in
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def is_enabled() -> bool:
    """IQ benchmark gated on L3 (prompt / intelligence track) since it
    feeds the same signal domain. off | l1 | l1+l3 | all — active iff
    L3 is present (or `all`)."""
    level = (os.environ.get("OMNISIGHT_SELF_IMPROVE_LEVEL") or "off").strip().lower()
    if level in {"off", ""}:
        return False
    return "l3" in level or level == "all"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Persistence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class IQRow:
    id: int
    ts: float
    model: str
    benchmark: str
    weighted_score: float
    pass_count: int
    total_count: int
    tokens_used: int
    truncated_at_question: Optional[str]


async def persist_run(reports: Sequence[ir.RunReport],
                      *, ts: float | None = None) -> int:
    """Insert one row per report. Returns the number of rows written."""
    if not reports:
        return 0
    from backend import db
    now = ts if ts is not None else time.time()
    conn = db._conn()
    for r in reports:
        await conn.execute(
            "INSERT INTO iq_runs (ts, model, benchmark, weighted_score, "
            "pass_count, total_count, tokens_used, truncated_at_question) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, r.score.model, r.score.benchmark, r.score.weighted_score,
             r.score.pass_count, r.score.total_count,
             r.tokens_used, r.truncated_at_question),
        )
    await conn.commit()
    return len(reports)


async def recent_scores(model: str, *, days: int = DEFAULT_BASELINE_DAYS,
                        now: float | None = None) -> list[IQRow]:
    """Return rows for `model` within the last `days` days, newest first."""
    from backend import db
    cutoff = (now if now is not None else time.time()) - days * SECONDS_PER_DAY
    async with db._conn().execute(
        "SELECT * FROM iq_runs WHERE model=? AND ts >= ? ORDER BY ts DESC",
        (model, cutoff),
    ) as cur:
        rows = await cur.fetchall()
    return [
        IQRow(
            id=r["id"], ts=r["ts"], model=r["model"], benchmark=r["benchmark"],
            weighted_score=r["weighted_score"],
            pass_count=r["pass_count"], total_count=r["total_count"],
            tokens_used=r["tokens_used"],
            truncated_at_question=r["truncated_at_question"],
        )
        for r in rows
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Regression detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _daily_aggregate(rows: list[IQRow]) -> list[tuple[float, float]]:
    """Reduce multiple (ts, score) rows on the same calendar day to
    one mean. Returns [(day_key, avg_score), ...] sorted newest-first.
    Day key is int(ts // SECONDS_PER_DAY)."""
    by_day: dict[int, list[float]] = {}
    for r in rows:
        day = int(r.ts // SECONDS_PER_DAY)
        by_day.setdefault(day, []).append(r.weighted_score)
    out = [(day, sum(vs) / len(vs)) for day, vs in by_day.items()]
    out.sort(key=lambda x: -x[0])
    return out


async def detect_regression(model: str, *,
                            threshold_pp: float = DEFAULT_REGRESSION_PP,
                            baseline_days: int = DEFAULT_BASELINE_DAYS,
                            now: float | None = None,
                            ) -> tuple[bool, str]:
    """True iff the latest TWO distinct days both scored more than
    `threshold_pp` points below the preceding-window median baseline.

    Returns (is_regressed, reason_string) — the string goes into the
    notification + audit log.
    """
    rows = await recent_scores(model, days=baseline_days + 2, now=now)
    daily = _daily_aggregate(rows)
    if len(daily) < 2:
        return False, f"insufficient samples ({len(daily)} days)"

    # Latest two days.
    (d0, s0), (d1, s1) = daily[0], daily[1]
    if d0 == d1:
        return False, "samples span only 1 calendar day"

    # Baseline = median of scores in [d1-baseline_days, d1-1].
    baseline_pool = [s for (d, s) in daily[2:2 + baseline_days]]
    if len(baseline_pool) < 3:
        return False, f"baseline pool too small ({len(baseline_pool)})"
    baseline = _median(baseline_pool)

    cutoff = baseline - threshold_pp / 100.0
    if s0 < cutoff and s1 < cutoff:
        return True, (f"score {s0:.2%} (d0) + {s1:.2%} (d1) both < "
                      f"{cutoff:.2%} (baseline {baseline:.2%} − "
                      f"{threshold_pp:.0f}pp)")
    return False, (f"latest {s0:.2%}/{s1:.2%} vs baseline "
                   f"{baseline:.2%} (cutoff {cutoff:.2%})")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  nightly() — orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def nightly(models: Sequence[str], *,
                  ask_fn: ir.AskFn | None = None,
                  token_budget_per_model: int = 50_000) -> list[ir.RunReport]:
    """Run all benchmarks × models, persist, publish Gauge, check
    regression. Returns reports for test inspection.

    Safe to invoke when opt-in is off (returns []). `ask_fn` defaults
    to `iq_runner.live_ask_fn` at call time.
    """
    if not is_enabled():
        logger.info("iq_nightly skipped: OMNISIGHT_SELF_IMPROVE_LEVEL != l3/all")
        return []
    benchmarks = ib.load_all()
    if not benchmarks:
        logger.warning("iq_nightly: no benchmarks loaded from configs/iq_benchmark/")
        return []
    ask = ask_fn or ir.live_ask_fn
    reports = await ir.run_all(
        benchmarks, models, ask_fn=ask,
        token_budget_per_model=token_budget_per_model,
    )
    await persist_run(reports)
    await _publish_gauge(reports)
    await _check_and_notify(models)
    return reports


async def _publish_gauge(reports: Sequence[ir.RunReport]) -> None:
    """Average weighted_score per model across all benchmarks for this
    run, push as Gauge."""
    try:
        from backend import metrics as _m
        by_model: dict[str, list[float]] = {}
        for r in reports:
            by_model.setdefault(r.score.model, []).append(r.score.weighted_score)
        for model, vs in by_model.items():
            if not vs:
                continue
            _m.intelligence_iq_score.labels(model=model).set(sum(vs) / len(vs))
    except Exception:
        pass


async def _check_and_notify(models: Sequence[str]) -> None:
    """Detect regression per model; send Notification level=action once."""
    from backend import notifications as _n
    for model in models:
        try:
            regressed, reason = await detect_regression(model)
        except Exception as exc:
            logger.warning("iq regression check failed for %s: %s", model, exc)
            continue
        if not regressed:
            continue
        try:
            from backend import metrics as _m
            _m.intelligence_iq_regression_total.labels(model=model).inc()
        except Exception:
            pass
        try:
            await _n.notify(
                level="action",
                title=f"[IIS-IQ] {model} regression",
                message=f"Intelligence benchmark drop: {reason}",
                source=f"iq:{model}",
            )
        except Exception as exc:
            logger.warning("iq notify failed for %s: %s", model, exc)
