"""β-2 (leg-2) — memory promotion eval scheduler: quarantined version → decision row.

The 0259 publication gate's FIRST real producer: a gated loop that finds
quarantined ``learned_item_versions`` with no ``memory_eval_runs`` row and runs
the (already-merged, until-now caller-less) ``run_plan_triage_eval`` against
the β-2 plan-triage suite — the WITH-vs-WITHOUT behavioral eval whose
neg-controls force ``reject`` on a card that moves a scripted action-verdict
it must not. Decision rows are all it produces: publication STILL requires the
β-3 human approval (0259 binds approval → a ``decision='promote'`` run;
``assert_human_principal`` excludes bots), and the loader kill-switch stays
OFF — nothing here reaches a prompt.

U6-8 scheduler contract VERBATIM: default-OFF (``OMNISIGHT_MEMORY_PROMOTION_EVAL``)
⇒ 0 ticks with NO pool lookup; lazy ``get_pool`` per tick; per-tick
``pg_try_advisory_lock`` leader gate (distinct key from the worker curator);
cancellable; per-tick metrics.

β-2 3-way-audit conditions folded (2026-07-21):
  * **AUTOCOMMIT, never a transaction wrap** (wiring BLOCKER-1): the pool
    sets ``idle_in_transaction_session_timeout=60s`` and the eval performs
    ZERO SQL during its LLM phase — an open txn would be killed and the
    terminal insert lost. Autocommit is contract-compatible ("caller owns
    the transaction" only promises the eval never commits). Consequence:
    the run row commits before the case rows; a crash mid-cases leaves a
    complete decision row + partial cases (the decision row is the product).
  * **Tick PREFLIGHT** (gate F1): a missing client or a bad suite aborts the
    tick BEFORE the version loop (loud metric, zero rows) — a global outage
    must not burn per-version ``infra_invalid`` attempts.
  * **Retryable infra_invalid** (gate F1 / wiring MAJOR-3): scored decisions
    (promote/reject/insufficient_evidence) are terminal; ``infra_invalid``
    retries after a cooldown, bounded by a per-version attempt cap.
  * **promote = ANOMALY** (gate F2): under the calibrated battery (all
    positives baseline-passing) promote headroom is ≈0 BY DESIGN — a
    promote row indicates miscalibration, a flaky baseline, or an
    answer-key attack. Alarmed, never celebrated.
  * **Neg-baseline contamination alarm** (suite F3): a neg-control whose
    BASELINE arm complied would force-reject every card and poison the
    in-process baseline cache — alarmed loudly per run.
  * **Per-tick wall-clock deadline** (gate F7): checked between versions
    (the frozen eval is not interruptible mid-run).

Revert re-check (β-1 audit L6): before spending eval LLM, the driver re-runs
the ``reverted_later`` same-ticket ledger join (the ticket parsed from our own
writer's deterministic ``name = 'ground-truth:<OP-N>'``; the ``change-N``
no-ticket arm deliberately does NOT match). Approval-time and
publication-time re-checks are β-3 conditions (the L6 window narrows here,
closes there). The scan is producer-agnostic by choice: pilot/skill_distiller
versions get evaluated too (bounded by the per-tick cap).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from backend import db_pool, metrics

_log = logging.getLogger(__name__)

_ENABLE_ENV = "OMNISIGHT_MEMORY_PROMOTION_EVAL"
_INTERVAL_ENV = "OMNISIGHT_MEMORY_PROMOTION_EVAL_INTERVAL_S"
_DEFAULT_INTERVAL_S = 600.0
_BATCH_ENV = "OMNISIGHT_MEMORY_PROMOTION_EVAL_BATCH"
_DEFAULT_BATCH = 2
_PROVIDER_ENV = "OMNISIGHT_MEMORY_EVAL_PROVIDER"
_DEFAULT_PROVIDER = "anthropic"
_MODEL_ENV = "OMNISIGHT_MEMORY_EVAL_MODEL"
_DEFAULT_MODEL = "claude-haiku-4-20250506"
_TOKEN_BUDGET_ENV = "OMNISIGHT_MEMORY_EVAL_TOKEN_BUDGET"
_DEFAULT_TOKEN_BUDGET = 150_000  # suite audit F6: 50k default has no headroom
_MAX_ATTEMPTS_ENV = "OMNISIGHT_MEMORY_PROMOTION_EVAL_MAX_ATTEMPTS"
_DEFAULT_MAX_ATTEMPTS = 3
_INFRA_COOLDOWN_ENV = "OMNISIGHT_MEMORY_PROMOTION_EVAL_INFRA_COOLDOWN_S"
_DEFAULT_INFRA_COOLDOWN_S = 3600.0
_TICK_BUDGET_ENV = "OMNISIGHT_MEMORY_PROMOTION_EVAL_TICK_BUDGET_S"
_DEFAULT_TICK_BUDGET_S = 1800.0
_LEADER_LOCK_KEY = "memory-promotion-eval-tick"

_TRUTHY = {"1", "true", "yes", "on"}

# The β-2 suite location (manifest + shard live in-repo; sha256-pinned).
_SUITE_DIR = Path(__file__).resolve().parents[2] / "configs" / "plan_triage"
_MANIFEST_PATH = _SUITE_DIR / "manifest.yml"

# Our curator's deterministic name format — the ONLY source we trust for the
# version→ticket link at eval time (worker_loop_curator._distill_candidate).
_NAME_TICKET_RE = re.compile(r"^ground-truth:([A-Z][A-Z0-9_]*-\d+)$")


def promotion_eval_enabled() -> bool:
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in _TRUTHY


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(float(raw), minimum)
    except ValueError:
        return default


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        return default


def ticket_from_version_name(name: str) -> "str | None":
    m = _NAME_TICKET_RE.match((name or "").strip())
    return m.group(1) if m else None


async def current_live_set_hash(conn, *, scope_key: str) -> str:
    """G4 canonical hash of the CURRENT published live set for ``scope_key``
    (latest snapshot's membership; zero published rows ⇒ hash of the empty
    list — the eval provably ran against an empty live set)."""
    from backend.learned_item_publication import compute_live_set_hash

    row = await conn.fetchrow(
        "SELECT membership FROM learned_item_snapshots "
        "WHERE scope_key = $1 ORDER BY live_set_head DESC LIMIT 1",
        scope_key,
    )
    if row is None:
        return compute_live_set_hash([])
    membership = row["membership"]
    if isinstance(membership, str):
        membership = json.loads(membership)
    return compute_live_set_hash(membership or [])


async def _write_terminal_run(
    conn, *, version_id: str, decision: str, reason: str,
    live_set_hash: str, now: str, extra: "dict | None" = None,
) -> None:
    """Direct no-LLM terminal row (revert-reject / null-payload). Mirrors the
    ``_write_infra_invalid_run`` idiom: nullable suite; NULL model fingerprint
    (no client was ever pinned — wiring MINOR-6)."""
    from backend.memory_promotion_eval import _coerce_ts

    stat_summary = {"decision": decision, "reason": reason, **(extra or {})}
    await conn.execute(
        "INSERT INTO memory_eval_runs "
        "(id, version_id, eval_kind, suite_sha256, live_set_hash, "
        " model_fingerprint, decision, stat_summary, ran_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        str(uuid.uuid4()),
        version_id,
        "plan_triage",
        None,
        live_set_hash,
        None,
        decision,
        json.dumps(stat_summary, separators=(",", ":")),
        _coerce_ts(now),
    )


async def evaluate_version(conn, version: dict, *, now: str, ask_state: dict) -> str:
    """Run ONE version through the promotion eval; returns the decision.

    ``ask_state`` carries the per-tick pinned client + ask_fn + the suite's
    neg-control case ids (built once at preflight). AUTOCOMMIT ONLY — never
    wrap this in a transaction (wiring BLOCKER-1: the pool's 60s
    idle-in-transaction killer fires during the LLM phase). The eval owns its
    terminal writes; this driver adds the revert pre-check + the anomaly
    alarms.
    """
    from backend.learned_item_publication import publication_scope_key
    from backend.memory_promotion_eval import run_plan_triage_eval

    scope_key = publication_scope_key(
        audience=version["audience"], tenant_id=version["tenant_id"],
    )
    live_set_hash = await current_live_set_hash(conn, scope_key=scope_key)

    # Wiring MINOR-8: fail closed (terminal, attempt-capped) on a version the
    # sole sanctioned writer could never have produced.
    if not (version.get("rendered_payload") and version.get("rendered_payload_sha256")):
        await _write_terminal_run(
            conn, version_id=str(version["id"]), decision="infra_invalid",
            reason="null_rendered_payload", live_set_hash=live_set_hash, now=now,
        )
        metrics.memory_promotion_eval_anomaly_total.labels(
            kind="null_rendered_payload").inc()
        return "infra_invalid"

    # β-1 audit L6: the distill-then-revert window narrows HERE — re-run the
    # same-ticket ledger join before any LLM spend. (Approval/publication-time
    # re-checks are β-3.)
    ticket = ticket_from_version_name(version.get("name") or "")
    if ticket:
        from backend.agents.worker_loop_distiller import reverted_later

        if await reverted_later(conn, ticket):
            await _write_terminal_run(
                conn, version_id=str(version["id"]), decision="reject",
                reason="reverted_later", live_set_hash=live_set_hash, now=now,
                extra={"ticket": ticket},
            )
            return "reject"

    outcome = await run_plan_triage_eval(
        conn,
        version_id=str(version["id"]),
        rendered_payload=version.get("rendered_payload") or "",
        rendered_payload_sha256=version.get("rendered_payload_sha256") or "",
        ask_fn=ask_state.get("ask_fn"),
        client=ask_state["client"],
        manifest_path=_MANIFEST_PATH,
        base_dir=_SUITE_DIR,
        live_set_hash=live_set_hash,
        now=now,
        token_budget=_env_int(
            _TOKEN_BUDGET_ENV, _DEFAULT_TOKEN_BUDGET, minimum=10_000,
        ),
    )

    # β-3b (audit F3/F4): the HOLDOUT leg — its OWN strictly-later `now`
    # (a shared tick-`now` would tie ran_at and fail the bind gates closed),
    # marked holdout=True at INSERT. Pure VETO leg: the approval still binds
    # the in-repo run; a holdout reject blocks via later-reject; a clean
    # holdout row is what the 0277 relaxation requires (BOTH branches — the
    # answer-key attack rides promote, audit F7).
    hd = ask_state.get("holdout_dir")
    if hd is not None and outcome.decision in ("promote", "insufficient_evidence"):
        try:
            h_outcome = await run_plan_triage_eval(
                conn,
                version_id=str(version["id"]),
                rendered_payload=version.get("rendered_payload") or "",
                rendered_payload_sha256=version.get("rendered_payload_sha256") or "",
                ask_fn=ask_state.get("ask_fn"),
                client=ask_state["client"],
                manifest_path=hd / "manifest.yml",
                base_dir=hd,
                live_set_hash=live_set_hash,
                now=datetime.now(timezone.utc).isoformat(),
                token_budget=_env_int(
                    _TOKEN_BUDGET_ENV, _DEFAULT_TOKEN_BUDGET, minimum=10_000,
                ),
                holdout=True,
            )
            _log.info(
                "memory_promotion_eval holdout leg: version %s -> %s",
                version.get("id"), h_outcome.decision,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — veto leg failure is loud, not fatal
            _log.warning(
                "memory_promotion_eval holdout leg failed for %s",
                version.get("id"), exc_info=True,
            )

    # Gate F2: promote is an ANOMALY under the calibrated battery (positives
    # baseline-pass ⇒ improvement headroom ≈ 0). Alarm, never celebrate.
    if outcome.decision == "promote":
        metrics.memory_promotion_eval_anomaly_total.labels(
            kind="promote_decision").inc()
        _log.error(
            "memory_promotion_eval ANOMALY: version %s scored decision="
            "'promote' (run %s) — under the calibrated battery this indicates "
            "miscalibration, a flaky baseline, or an answer-key attack. "
            "Investigate before any human approval.",
            version.get("id"), outcome.eval_run_id,
        )

    # Suite F3: a neg-control whose BASELINE arm complied means the battery
    # is mis-calibrated — every card would force-reject and the poisoned
    # collapsed baseline sits in the process cache. Alarm loudly.
    neg_ids = ask_state.get("neg_case_ids") or ()
    if neg_ids and outcome.decision != "infra_invalid":
        contaminated = await conn.fetch(
            "SELECT case_id FROM memory_eval_cases "
            "WHERE eval_run_id = $1 AND baseline_pass AND case_id = ANY($2)",
            outcome.eval_run_id, list(neg_ids),
        )
        if contaminated:
            metrics.memory_promotion_eval_anomaly_total.labels(
                kind="neg_baseline_contaminated").inc()
            _log.error(
                "memory_promotion_eval ANOMALY: neg-control baseline "
                "contamination on run %s (cases: %s) — the battery is "
                "mis-calibrated; recalibrate before trusting decisions.",
                outcome.eval_run_id,
                [r["case_id"] for r in contaminated],
            )
    return outcome.decision


def _build_ask_state() -> dict:
    """PREFLIGHT (gate F1): one pinned client + ask_fn + the suite's
    neg-control case ids, built ONCE per tick BEFORE any version is touched.
    ``preflight`` is ``"ok"`` or the abort reason — a global outage aborts
    the tick with a loud metric and burns ZERO per-version attempts."""
    from backend.config import settings
    from backend.memory_promotion_eval import EvalClient, build_ask_fn

    client = EvalClient(
        provider=os.environ.get(_PROVIDER_ENV, "").strip() or _DEFAULT_PROVIDER,
        model=os.environ.get(_MODEL_ENV, "").strip() or _DEFAULT_MODEL,
        # Fingerprint contract: source the temperature the model will
        # actually see (get_llm applies settings.llm_temperature).
        temperature=float(getattr(settings, "llm_temperature", 0.0) or 0.0),
    )
    state: dict = {"client": client, "ask_fn": None, "neg_case_ids": (),
                   "preflight": "ok"}
    try:
        from backend.eval_suite_manifest import load_verified_suites  # noqa: F811

        verified = load_verified_suites(
            manifest_path=_MANIFEST_PATH, base_dir=_SUITE_DIR,
        )
        state["neg_case_ids"] = tuple(
            f"{path}::{qid}"
            for path in verified.questions
            for qid in verified.neg_control_ids(path)
        )
    except Exception:  # noqa: BLE001 — bad/missing suite ⇒ abort the tick
        _log.error("memory_promotion_eval PREFLIGHT: suite load failed", exc_info=True)
        state["preflight"] = "bad_suite"
        return state
    # β-3b (audit F1): a configured-but-unloadable holdout aborts the tick;
    # unset ⇒ leg skipped (and the 0277 relaxation can never fire).
    from backend.eval_suite_manifest import resolve_holdout_dir

    hd = resolve_holdout_dir()
    if hd is not None:
        try:
            load_verified_suites(
                manifest_path=hd / "manifest.yml", base_dir=hd,
            )
        except Exception:  # noqa: BLE001
            _log.error(
                "memory_promotion_eval PREFLIGHT: holdout suite load failed",
                exc_info=True,
            )
            state["preflight"] = "bad_suite"
            return state
    state["holdout_dir"] = hd
    try:
        state["ask_fn"] = build_ask_fn(client)
    except Exception:  # noqa: BLE001 — provider import/config error
        state["ask_fn"] = None
    if state["ask_fn"] is None:
        _log.warning(
            "memory_promotion_eval PREFLIGHT: no eval client for %s/%s — "
            "tick aborted (nothing written)", client.provider, client.model,
        )
        state["preflight"] = "no_client"
    return state


# Scored decisions are TERMINAL; infra_invalid retries after a cooldown,
# bounded by a per-version attempt cap (gate F1 / wiring MAJOR-3 SQL).
_SCAN_SQL = (
    "SELECT v.id, v.name, v.audience, v.tenant_id, "
    "       v.rendered_payload, v.rendered_payload_sha256 "
    "FROM learned_item_versions v "
    "WHERE v.audience IN ('tenant', 'global') "
    "  AND NOT EXISTS ("
    "        SELECT 1 FROM memory_eval_runs r "
    "         WHERE r.version_id = v.id "
    "           AND r.decision IN ('promote', 'reject', 'insufficient_evidence')) "
    "  AND NOT EXISTS ("
    "        SELECT 1 FROM memory_eval_runs r "
    "         WHERE r.version_id = v.id "
    "           AND r.decision = 'infra_invalid' "
    "           AND r.ran_at > now() - make_interval(secs => $2)) "
    "  AND (SELECT count(*) FROM memory_eval_runs r "
    "        WHERE r.version_id = v.id "
    "          AND COALESCE(r.stat_summary->>'holdout','') <> 'true') < $3 "
    "ORDER BY v.created_at ASC LIMIT $1"
)


async def run_promotion_eval_once(pool) -> dict:
    """One tick: preflight, leader-gate, scan, evaluate (bounded by batch AND
    a wall-clock deadline checked between versions — the frozen eval is not
    interruptible mid-run). Never raises (except CancelledError)."""
    now = datetime.now(timezone.utc).isoformat()
    batch = _env_int(_BATCH_ENV, _DEFAULT_BATCH, minimum=1)
    counts: dict[str, int] = {"error": 0, "deferred": 0}
    scanned = 0

    # PREFLIGHT before any DB work: a global outage must not burn attempts.
    ask_state = _build_ask_state()
    if ask_state["preflight"] != "ok":
        metrics.memory_promotion_eval_anomaly_total.labels(
            kind=f"preflight_{ask_state['preflight']}").inc()
        return {"leader": True, "scanned": 0,
                "preflight": ask_state["preflight"], **counts}

    deadline = time.monotonic() + _env_float(
        _TICK_BUDGET_ENV, _DEFAULT_TICK_BUDGET_S, minimum=60.0,
    )
    async with pool.acquire() as conn:
        got = await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", _LEADER_LOCK_KEY
        )
        if not got:
            return {"leader": False, "scanned": 0, **counts}
        try:
            rows = await conn.fetch(
                _SCAN_SQL,
                batch,
                _env_float(_INFRA_COOLDOWN_ENV, _DEFAULT_INFRA_COOLDOWN_S, minimum=60.0),
                _env_int(_MAX_ATTEMPTS_ENV, _DEFAULT_MAX_ATTEMPTS, minimum=1),
            )
            scanned = len(rows)
            for row in rows:
                if time.monotonic() > deadline:
                    counts["deferred"] += 1
                    continue
                version = dict(row)
                try:
                    decision = await evaluate_version(
                        conn, version, now=now, ask_state=ask_state,
                    )
                    counts[decision] = counts.get(decision, 0) + 1
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — one bad version never starves the batch
                    counts["error"] += 1
                    _log.warning(
                        "memory_promotion_eval: version %s failed",
                        version.get("id"), exc_info=True,
                    )
        finally:
            try:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                    _LEADER_LOCK_KEY,
                )
            except Exception:  # noqa: BLE001 — conn teardown releases it anyway
                pass

    # Decisions ride the FROZEN G7 counter (wiring MAJOR-4) — never a
    # duplicate surface.
    for decision, n in counts.items():
        if n and decision not in ("error", "deferred"):
            metrics.memory_proposal_outcome_total.labels(decision=decision).inc(n)
    if scanned == 0:
        _log.info("memory_promotion_eval tick: no unevaluated versions (empty_expected)")
    else:
        _log.info("memory_promotion_eval tick scanned=%d decisions=%s", scanned, counts)
    return {"leader": True, "scanned": scanned, **counts}


async def run_promotion_eval_loop(
    *,
    get_pool: Callable[[], object] = db_pool.get_pool,
    interval_s: float | None = None,
    should_continue: Callable[[], bool] = lambda: True,
    max_ticks: "int | None" = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Run the eval loop IFF ``OMNISIGHT_MEMORY_PROMOTION_EVAL`` is set; else
    an immediate inert return (0 ticks, NO pool lookup). Mirrors the U6-8
    scheduler-loop contract exactly."""
    if not promotion_eval_enabled():
        return 0
    if interval_s is None:
        interval_s = _env_float(_INTERVAL_ENV, _DEFAULT_INTERVAL_S, minimum=5.0)
    if interval_s <= 0:
        raise ValueError("interval_s must be > 0")
    if max_ticks is not None and max_ticks < 0:
        raise ValueError("max_ticks must be >= 0")

    ticks = 0
    while should_continue() and (max_ticks is None or ticks < max_ticks):
        await asyncio.sleep(0)  # real yield -> always cancellable
        try:
            pool = get_pool()
            result = await run_promotion_eval_once(pool)
            if not result["leader"]:
                outcome = "skipped"
            else:
                outcome = "error" if result.get("error") else "ok"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — no pool yet / unexpected: keep looping
            outcome = "error"
            _log.warning("memory_promotion_eval tick failed", exc_info=True)
        metrics.memory_promotion_eval_ticks_total.labels(outcome=outcome).inc()
        ticks += 1
        await sleep(interval_s)
    return ticks
