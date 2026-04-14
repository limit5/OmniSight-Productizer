"""Phase 65 S4 — fine-tune nightly orchestrator.

Glue layer that turns S1/S2/S3 into a single end-to-end pass:

  1. Export today's eligible workflow_runs to a JSONL training set
     (Phase 65 S1; gates on completed × hvt_passed × clean resolver
     × scrub-safe).
  2. If row count < `MIN_ROWS_TO_SUBMIT`, log + abort. Tiny training
     sets cause regressions more often than improvements.
  3. submit() to the configured FinetuneBackend (Phase 65 S3; defaults
     to noop). Poll with bounded attempts; on terminal state,
     proceed.
  4. If state=succeeded, run hold-out comparison (Phase 65 S2) baseline
     vs candidate. If reject → file Decision Engine
     `finetune/regression` proposal (severity=destructive,
     default_option=reject) so admin sees the problem.
  5. Audit-log every step (`finetune_exported`, `finetune_submitted`,
     `finetune_evaluated`, `finetune_promoted` | `finetune_rejected`).

This module owns NO promotion side-effect of its own. Promote means
"DE proposal opened with default=accept"; reject means "DE proposal
opened with default=reject". A real swap of the active model is
operator-side, just like Phase 63-C prompt promote_canary.

Opt-in: OMNISIGHT_SELF_IMPROVE_LEVEL must include 'l4' or be 'all'.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend import finetune_backend as _fb
from backend import finetune_eval as _fe
from backend import finetune_export as _fx

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MIN_ROWS_TO_SUBMIT = 50            # below this we don't bother
DEFAULT_POLL_INTERVAL_S = 60.0
DEFAULT_POLL_MAX_ATTEMPTS = 60     # 60 × 60s = 1h cap
DEFAULT_NIGHTLY_INTERVAL_S = 86400.0


def is_enabled() -> bool:
    """Phase 65 hides behind L4 in OMNISIGHT_SELF_IMPROVE_LEVEL —
    same opt-in pattern as Phase 62 / 63-D."""
    level = (os.environ.get("OMNISIGHT_SELF_IMPROVE_LEVEL")
             or "off").strip().lower()
    if level in {"off", ""}:
        return False
    return "l4" in level or level == "all"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Result type
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class NightlyOutcome:
    """Single source of truth for what a nightly pass did."""
    status: str  # see _STATUSES
    rows_written: int = 0
    skipped: dict = field(default_factory=dict)
    submit_handle: Optional[_fb.JobHandle] = None
    job_state: Optional[str] = None
    candidate_model: Optional[str] = None
    eval: Optional[_fe.HoldoutEvaluation] = None
    de_decision_id: Optional[str] = None
    reason: str = ""


_STATUSES = (
    "ok_promoted",       # eval said promote → DE accept proposal filed
    "ok_rejected",       # eval said reject → DE reject proposal filed
    "no_eligible_runs",  # exporter wrote 0 rows
    "below_min_rows",    # too few rows to bother
    "submit_unavailable",  # backend prerequisite missing
    "submit_error",      # backend submitted, runtime error
    "poll_timeout",      # job didn't reach terminal state in time
    "job_failed",        # backend reported failed
    "eval_skipped",      # job succeeded but hold-out missing
    "disabled",          # opt-in not set
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _audit(action: str, after: dict) -> None:
    try:
        from backend import audit as _a
        await _a.log(
            action=action, entity_kind="finetune",
            entity_id=after.get("entity_id") or after.get("handle") or None,
            after=after, actor="system:finetune-nightly",
        )
    except Exception as exc:
        logger.debug("audit %s failed: %s", action, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decision Engine — finetune/regression
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _file_de_proposal(eval_: _fe.HoldoutEvaluation) -> Optional[str]:
    """Open a DE proposal so an operator promotes / rejects manually.
    Severity per outcome:
      reject → destructive (default=reject; we want admin sign-off
               to deploy a regressed model anyway).
      promote → routine (default=accept; safe automation in BALANCED+).
    Returns the decision id, or None on best-effort DE failure.
    """
    try:
        from backend import decision_engine as de
    except Exception as exc:
        logger.warning("DE import failed: %s", exc)
        return None
    try:
        if eval_.decision == "reject":
            kind, severity, default_id = (
                "finetune/regression", de.DecisionSeverity.destructive, "reject",
            )
            options = [
                {"id": "reject", "label": "Reject candidate (recommended)",
                 "description": (
                     f"Discard fine-tuned candidate {eval_.candidate_model!r}. "
                     f"Holdout regressed {-eval_.delta_pp:+.1f}pp vs baseline."
                 )},
                {"id": "accept_anyway",
                 "label": "Accept anyway (override regression gate)",
                 "description": "Promotes despite the regression — operator overrides."},
            ]
        else:
            kind, severity, default_id = (
                "finetune/promote", de.DecisionSeverity.routine, "accept",
            )
            options = [
                {"id": "accept", "label": "Promote candidate",
                 "description": (
                     f"Promote {eval_.candidate_model!r}; "
                     f"holdout Δ={eval_.delta_pp:+.1f}pp."
                 )},
                {"id": "reject", "label": "Discard candidate",
                 "description": "Operator opts out of this fine-tune."},
            ]
        dec = de.propose(
            kind=kind, severity=severity,
            title=(f"Fine-tune {eval_.decision}: "
                   f"{eval_.candidate_model} vs {eval_.baseline_model}"),
            detail=eval_.reason,
            options=options, default_option_id=default_id,
            timeout_s=86400.0,
            source={
                "subsystem": "finetune_nightly",
                "candidate": eval_.candidate_model,
                "baseline": eval_.baseline_model,
                "delta_pp": eval_.delta_pp,
                "threshold_pp": eval_.threshold_pp,
                "benchmark": eval_.benchmark_name,
            },
        )
        return dec.id
    except Exception as exc:
        logger.warning("DE propose finetune outcome failed: %s", exc)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Poll-to-terminal helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TERMINAL = {"succeeded", "failed", "cancelled"}


async def _poll_until_terminal(
    backend: _fb.FinetuneBackend, handle: _fb.JobHandle,
    *, interval_s: float, max_attempts: int,
) -> _fb.JobStatus:
    """Sleep + poll loop. Returns last JobStatus regardless of state.
    `max_attempts` includes the first poll; never sleeps after the
    final terminal observation."""
    last: _fb.JobStatus | None = None
    for i in range(max_attempts):
        last = await backend.poll(handle)
        if last.state in _TERMINAL:
            return last
        if i < max_attempts - 1:
            await asyncio.sleep(interval_s)
    # Exhausted budget; return whatever we last saw.
    if last is None:
        # Defensive: should never happen because max_attempts > 0.
        return _fb.JobStatus(handle=handle, state="failed",
                             error="poll_max_attempts=0")
    return last


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  nightly() — one full pass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def nightly(
    *,
    baseline_model: str,
    base_model_for_finetune: str,
    suffix: str = "omnisight",
    backend: Optional[_fb.FinetuneBackend] = None,
    ask_fn=None,
    output_dir: Optional[Path] = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    poll_max_attempts: int = DEFAULT_POLL_MAX_ATTEMPTS,
    min_rows_to_submit: int = MIN_ROWS_TO_SUBMIT,
) -> NightlyOutcome:
    """Run one end-to-end fine-tune pass. Returns NightlyOutcome —
    the caller (lifespan loop, ops CLI, test) inspects status."""
    if not is_enabled():
        return NightlyOutcome(status="disabled",
                              reason="OMNISIGHT_SELF_IMPROVE_LEVEL missing l4")

    backend = backend or _fb.select_backend()
    out_dir = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(
        prefix="omnisight-finetune-",
    ))
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / f"train-{int(time.time())}.jsonl"

    # 1. Export
    stats = await _fx.export_jsonl(jsonl)
    await _audit("finetune_exported", {
        "rows_written": stats.written,
        "skipped": dict(stats.skip_reasons),
        "output_path": stats.output_path,
    })
    if stats.written == 0:
        return NightlyOutcome(status="no_eligible_runs",
                              rows_written=0, skipped=stats.skip_reasons,
                              reason="no rows passed the export gate")
    if stats.written < min_rows_to_submit:
        return NightlyOutcome(
            status="below_min_rows",
            rows_written=stats.written,
            skipped=stats.skip_reasons,
            reason=(f"only {stats.written} rows < min {min_rows_to_submit}; "
                    f"skipping submit"),
        )

    # 2. Submit
    try:
        handle = await backend.submit(jsonl, base_model=base_model_for_finetune,
                                      suffix=suffix)
    except _fb.BackendUnavailable as exc:
        await _audit("finetune_submit_unavailable", {
            "backend": backend.name, "error": str(exc),
        })
        return NightlyOutcome(status="submit_unavailable", rows_written=stats.written,
                              reason=str(exc))
    except Exception as exc:  # network / SDK error
        await _audit("finetune_submit_error", {
            "backend": backend.name, "error": repr(exc),
        })
        return NightlyOutcome(status="submit_error", rows_written=stats.written,
                              reason=repr(exc))
    await _audit("finetune_submitted", {
        "backend": handle.backend, "external_id": handle.external_id,
        "base_model": base_model_for_finetune, "suffix": suffix,
        "rows": stats.written,
    })

    # 3. Poll
    status = await _poll_until_terminal(
        backend, handle,
        interval_s=poll_interval_s, max_attempts=poll_max_attempts,
    )
    if status.state not in _TERMINAL:
        await _audit("finetune_poll_timeout", {
            "external_id": handle.external_id,
            "last_state": status.state,
        })
        return NightlyOutcome(
            status="poll_timeout", rows_written=stats.written,
            submit_handle=handle, job_state=status.state,
            reason=(f"job {handle.external_id} did not finish in "
                    f"{poll_max_attempts} attempts"),
        )
    if status.state != "succeeded":
        await _audit("finetune_failed", {
            "external_id": handle.external_id,
            "state": status.state,
            "error": status.error,
        })
        return NightlyOutcome(
            status="job_failed", rows_written=stats.written,
            submit_handle=handle, job_state=status.state,
            reason=status.error or status.state,
        )

    candidate = status.fine_tuned_model
    if not candidate:
        await _audit("finetune_eval_skipped", {
            "external_id": handle.external_id,
            "reason": "succeeded but no fine_tuned_model",
        })
        return NightlyOutcome(
            status="eval_skipped", rows_written=stats.written,
            submit_handle=handle, job_state=status.state,
            reason="job succeeded but backend returned no model id",
        )

    # 4. Evaluate
    if ask_fn is None:
        from backend import iq_runner as _ir
        ask_fn = _ir.live_ask_fn
    eval_ = await _fe.compare_models(
        baseline_model, candidate, ask_fn=ask_fn,
    )
    await _audit("finetune_evaluated", {
        "candidate": candidate, "baseline": baseline_model,
        "decision": eval_.decision, "delta_pp": eval_.delta_pp,
        "threshold_pp": eval_.threshold_pp,
    })

    # 5. DE gate
    dec_id = await _file_de_proposal(eval_)
    if eval_.decision == "promote":
        await _audit("finetune_promoted", {
            "candidate": candidate, "decision_id": dec_id,
        })
        outcome_status = "ok_promoted"
    else:
        await _audit("finetune_rejected", {
            "candidate": candidate, "decision_id": dec_id,
            "reason": eval_.reason,
        })
        outcome_status = "ok_rejected"

    return NightlyOutcome(
        status=outcome_status, rows_written=stats.written,
        submit_handle=handle, job_state=status.state,
        candidate_model=candidate, eval=eval_,
        de_decision_id=dec_id, reason=eval_.reason,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Background loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_LOOP_RUNNING = False


async def run_nightly_loop(
    *,
    interval_s: float = DEFAULT_NIGHTLY_INTERVAL_S,
    baseline_model: Optional[str] = None,
    base_model_for_finetune: Optional[str] = None,
    backend: Optional[_fb.FinetuneBackend] = None,
    ask_fn=None,
) -> None:
    """Singleton background coroutine. Skips ticks while opt-in is off
    so ops can disable without restart. Exits cleanly on
    asyncio.CancelledError (Phase 52 convention)."""
    global _LOOP_RUNNING
    if _LOOP_RUNNING:
        return
    _LOOP_RUNNING = True

    def _resolve_models() -> tuple[str, str]:
        try:
            from backend.config import settings as _s
            spec = f"{_s.llm_provider}/{_s.get_model_name()}"
            return spec, spec
        except Exception:
            return "", ""

    try:
        while True:
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                break
            if not is_enabled():
                logger.debug("finetune nightly: opt-in off, skipping tick")
                continue
            base = baseline_model
            ftm = base_model_for_finetune
            if not base or not ftm:
                base, ftm = _resolve_models()
            if not base or not ftm:
                logger.warning("finetune nightly: no models resolved; skipping tick")
                continue
            try:
                await nightly(
                    baseline_model=base, base_model_for_finetune=ftm,
                    backend=backend, ask_fn=ask_fn,
                )
            except Exception as exc:
                logger.warning("finetune nightly tick failed: %s", exc)
    except asyncio.CancelledError:
        pass
    finally:
        _LOOP_RUNNING = False
