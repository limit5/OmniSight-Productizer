"""OP-2575 U4-F-exec — plan-triage eval executor (pinned client, paired arms).

The machine that turns "a candidate item" into a ledgered, statistically
honest eval verdict, per the freeze U4-A0 §F4/G7 and forward design v2
(codex E7/E8/E9, B2/B4/B5). Builds on:

* the A1 ledger tables (migration 0258) — target of the eval-run and
  eval-case row writes;
* the A2 rendered_payload contract — this executor injects the caller-
  supplied ``rendered_payload`` bytes VERBATIM (never re-renders);
* the D card contract — the card-builder projects ``stat_summary`` down
  to the frozen 4 keys (richer keys ValueError there); FUTURE card-
  builders must apply that projection: this run row carries
  ``prompt_assembly_fingerprint``, ``samples_per_case``, and a ``detail``
  sub-dict alongside the 4-key card;
* the H module ``backend.eval_suite_manifest`` — the ONLY sanctioned path
  for suite loading; its ``SuiteManifestError`` propagates as
  ``infra_invalid`` here (codex E8: a failed load is not evidence);
* the F-math sibling ``backend.memory_eval_stats`` — frozen ``PairedCase``
  / ``collapse_clustered`` / ``summarize`` / ``decide`` /
  ``build_stat_summary`` / ``build_stat_detail``; never re-implemented.

Audit B2: the eval compares WITH-item vs WITHOUT-item arms on a FIXED
model — ``finetune_eval.compare_models`` is the WRONG shape (varies the
model, uses a +pp non-regression threshold; do NOT reuse its decision
path). This executor has no +pp threshold anywhere: promotion is the
F-math exact-McNemar + net-flips terminal, or ``infra_invalid``.

Codex E8 terminal: ANY infrastructure failure yields ``infra_invalid``,
NEVER a scored arm. ``iq_runner`` grades errors as fails — that is
correct there, wrong here; do not let a broken run reach the stats.

DORMANT: no caller (U4-J wires scheduling and candidate selection).

``conn`` is a parameter (pure seam) — this module does not import the
pool, does not read a clock (``now`` is caller-supplied), and does not
import any A2/D/C1 sibling. Boundary reads: the H module, the F-math
stats, ``backend.agents.llm.get_llm`` via ``build_ask_fn``, and
``backend.metrics`` for the counters.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from backend import metrics
from backend.eval_suite_manifest import (
    SuiteManifestError,
    load_verified_suites,
    suite_sha256,
)
from backend.memory_eval_stats import (
    PairedCase,
    build_stat_detail,
    build_stat_summary,
    collapse_clustered,
    decide,
    summarize,
)


# Frozen ask_fn contract (matches ``backend.iq_runner.AskFn``):
# ``(model, prompt) -> (answer_text, token_count)``.
AskFn = Callable[[str, str], Awaitable[tuple[str, int]]]


# Bumped on ANY change to the arm-prompt template shape (freeze F4).
TEMPLATE_VERSION = "u4fe1"


# The arm-prompt template. The candidate arm substitutes the caller's
# EXACT A2 rendered_payload bytes into the item slot; the baseline arm
# substitutes an EMPTY string. The item is the ONLY variable between
# arms (audit B2). Bump ``TEMPLATE_VERSION`` on any change here.
_ARM_TEMPLATE = (
    "You are answering a question. Consider any provided item carefully, "
    "then answer the question directly.\n"
    "\n"
    "===== item start =====\n"
    "{item}\n"
    "===== item end =====\n"
    "\n"
    "Question:\n"
    "{question}\n"
)


@dataclass(frozen=True)
class EvalClient:
    """Pinned client identity: one specific (provider, model, temperature).

    Failover is DISABLED at the seam (``build_ask_fn`` passes
    ``allow_failover=False``). ``temperature`` is fingerprint-identity
    ONLY — ``get_llm`` applies ``settings.llm_temperature`` internally,
    so U4-J must SOURCE this value from settings for the fingerprint to
    line up with what the model actually saw.
    """

    provider: str
    model: str
    temperature: float


@dataclass(frozen=True)
class EvalOutcome:
    """The executor's terminal, in the ledger row's shape.

    ``suite_sha256`` is the empty string on the ``infra_invalid`` early-
    exit paths where suite identity is unknowable (e.g. the H module
    itself raised); the persisted row writes SQL NULL there.
    """

    decision: str
    eval_run_id: str
    mcnemar_p: float | None
    net_flips: int | None
    n: int | None
    suite_sha256: str
    infra_reason: str | None


# ━━ Fingerprints (freeze F4) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def model_fingerprint(client: EvalClient) -> str:
    """sha256 hex of ``f"{provider}|{model}|temp={temperature}"``."""
    material = f"{client.provider}|{client.model}|temp={client.temperature}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def prompt_assembly_fingerprint(
    *,
    suite_sha256: str,
    candidate_rendered_sha256: str,
    template_version: str,
) -> str:
    """sha256 hex of the joined ``suite | candidate | template_version``."""
    material = "|".join(
        (suite_sha256, candidate_rendered_sha256, template_version)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ━━ Arm prompts ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _candidate_prompt(question_prompt: str, rendered_payload: str) -> str:
    """Candidate arm: the exact A2-stored bytes go into the item slot."""
    return _ARM_TEMPLATE.format(item=rendered_payload, question=question_prompt)


def _baseline_prompt(question_prompt: str) -> str:
    """Baseline arm: same template, empty item slot — the ONLY variable
    between arms is the item (audit B2)."""
    return _ARM_TEMPLATE.format(item="", question=question_prompt)


# ━━ Pinned client seam ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def build_ask_fn(client: EvalClient) -> AskFn | None:
    """Bind a pinned, no-failover ask_fn from a ``get_llm`` call.

    Uses ``allow_failover=False`` (llm.py contract): the specific
    (provider, model) pair either initialises or returns ``None`` — the
    fallback chain is NEVER walked, so a broken primary cannot silently
    route the eval to a different provider. On a None client the caller
    (this module's ``run_plan_triage_eval``) records
    ``infra_invalid{no_client}`` and writes NO scored arm.

    Tests never invoke the live-LLM path — ``_scripted`` ask_fns bypass
    this builder entirely (the ``iq_runner`` / ``test_finetune_eval``
    precedent). This function is exercised solely through a
    monkeypatched-None ``get_llm`` smoke test.
    """
    from backend.agents.llm import get_llm

    llm = get_llm(
        provider=client.provider,
        model=client.model,
        allow_failover=False,
    )
    if llm is None:
        return None

    async def _ask(model: str, prompt: str) -> tuple[str, int]:
        if hasattr(llm, "ainvoke"):
            resp = await llm.ainvoke(prompt)
        else:
            resp = await asyncio.to_thread(llm.invoke, prompt)
        text = getattr(resp, "content", str(resp))
        meta = getattr(resp, "response_metadata", {}) or {}
        usage = meta.get("token_usage") or meta.get("usage") or {}
        n_tokens = 0
        if isinstance(usage, dict):
            n_tokens = int(
                usage.get("total_tokens") or usage.get("output_tokens") or 0
            )
        if n_tokens == 0:
            n_tokens = max(1, len(prompt) // 4 + len(text) // 4)
        return (text, n_tokens)

    return _ask


# ━━ Baseline-arm cache (audit B4/E9) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Keyed ``(suite_sha256, model_fingerprint, samples_per_case)`` → collapsed
# baseline per-case bools ``{case_id: baseline_pass}``. Bounded — post-
# collapse booleans only, no answer text. On hit the baseline arm's
# ask_fn calls are SKIPPED entirely (tests assert the call-count halves).
_BASELINE_CACHE: dict[tuple[str, str, int], dict[str, bool]] = {}


def _reset_for_tests() -> None:
    """Test-only: drop the baseline cache. Real code has no reason to
    call this — the cache is content-addressed and self-invalidating."""
    _BASELINE_CACHE.clear()


# ━━ Executor ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _run_arm_sample(
    ask_fn: AskFn,
    model_arg: str,
    prompt: str,
    per_question_timeout_s: float,
) -> tuple[str, int]:
    """One arm sample under the caller's per-question timeout. Timeouts
    and non-timeout exceptions both surface as raises here — the caller
    maps them to ``infra_invalid`` (codex E8; a truncated/errored arm is
    NEVER graded as a fail here — that is iq_runner's behaviour and it
    is wrong for this executor)."""
    return await asyncio.wait_for(
        ask_fn(model_arg, prompt), timeout=per_question_timeout_s
    )


def _uuid() -> str:
    return str(uuid.uuid4())


async def _write_infra_invalid_run(
    conn,
    *,
    eval_run_id: str,
    version_id: str,
    suite_sha256_val: str | None,
    live_set_hash: str,
    model_fp: str,
    reason: str,
    now: str,
    prompt_assembly_fp: str | None,
    samples_per_case: int,
) -> None:
    """Persist an ``infra_invalid`` eval-run row + emit the fail-closed
    metric. NO case rows are written on this terminal — a broken run is
    not evidence (codex E8)."""
    stat_summary: dict = {"decision": "infra_invalid", "reason": reason}
    if prompt_assembly_fp is not None:
        stat_summary["prompt_assembly_fingerprint"] = prompt_assembly_fp
    stat_summary["samples_per_case"] = samples_per_case
    await conn.execute(
        "INSERT INTO memory_eval_runs "
        "(id, version_id, eval_kind, suite_sha256, live_set_hash, "
        " model_fingerprint, decision, stat_summary, ran_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        eval_run_id,
        version_id,
        "plan_triage",
        suite_sha256_val,
        live_set_hash,
        model_fp,
        "infra_invalid",
        json.dumps(stat_summary, separators=(",", ":")),
        now,
    )
    metrics.memory_failclosed_total.labels(reason="eval_infra").inc()


async def run_plan_triage_eval(
    conn,
    *,
    version_id: str,
    rendered_payload: str,
    rendered_payload_sha256: str,
    ask_fn: AskFn | None,
    client: EvalClient,
    manifest_path: Path,
    base_dir: Path,
    live_set_hash: str,
    now: str,
    samples_per_case: int = 3,
    token_budget: int = 50_000,
    per_question_timeout_s: float = 30.0,
) -> EvalOutcome:
    """Run the plan-triage eval and persist the ledger row(s).

    Steps (per the spec, in order):
      1. Load + verify the suite via the H module; ``SuiteManifestError``
         → ``infra_invalid`` (reason = the manifest module's ``reason``
         code, e.g. ``hash_mismatch:...``).
      2. For every question in every verified suite, run
         ``samples_per_case`` samples of BOTH arms via ``ask_fn``
         (baseline first, then candidate; per-sample timeout). Baseline
         cache hits skip the baseline arm entirely.
      3. Token accounting: sum every sample's reported tokens; crossing
         ``token_budget`` at ANY point → whole eval is ``infra_invalid``.
      4. ``PairedCase`` per (case, sample) → ``collapse_clustered`` →
         ``summarize`` → ``decide`` (F-math, defaults).
      5. Neg-control enforcement (freeze G5.4): if the collapsed
         candidate side passes on any question in the suite's
         ``neg_control_ids``, force the decision to ``reject`` and
         increment ``memory_neg_control_catch_total`` once per catch.
      6. Write the eval-run row + one eval-case row per collapsed case;
         caller owns the transaction (C1's contract).
      7. Any of (H module raise / ask_fn exception / timeout / token
         crossing / None ask_fn) → ``infra_invalid`` terminal: run row
         with the reason code, NO case rows, failclosed counter bumped.

    ``ask_fn is None`` is the mapped result of ``build_ask_fn`` returning
    None (no client available) — reason ``no_client``.
    """
    eval_run_id = _uuid()
    model_fp = model_fingerprint(client)
    samples_per_case = int(samples_per_case)

    # (7-early) No client → no_client infra_invalid; no suite loaded yet.
    if ask_fn is None:
        await _write_infra_invalid_run(
            conn,
            eval_run_id=eval_run_id,
            version_id=version_id,
            suite_sha256_val=None,
            live_set_hash=live_set_hash,
            model_fp=model_fp,
            reason="no_client",
            now=now,
            prompt_assembly_fp=None,
            samples_per_case=samples_per_case,
        )
        return EvalOutcome(
            decision="infra_invalid",
            eval_run_id=eval_run_id,
            mcnemar_p=None,
            net_flips=None,
            n=None,
            suite_sha256="",
            infra_reason="no_client",
        )

    # (1) H module — fail-closed
    try:
        verified = load_verified_suites(
            manifest_path=manifest_path, base_dir=base_dir
        )
        suite_hash = suite_sha256(
            manifest_path=manifest_path, base_dir=base_dir
        )
    except SuiteManifestError as exc:
        await _write_infra_invalid_run(
            conn,
            eval_run_id=eval_run_id,
            version_id=version_id,
            suite_sha256_val=None,
            live_set_hash=live_set_hash,
            model_fp=model_fp,
            reason=exc.reason,
            now=now,
            prompt_assembly_fp=None,
            samples_per_case=samples_per_case,
        )
        return EvalOutcome(
            decision="infra_invalid",
            eval_run_id=eval_run_id,
            mcnemar_p=None,
            net_flips=None,
            n=None,
            suite_sha256="",
            infra_reason=exc.reason,
        )

    prompt_assembly_fp = prompt_assembly_fingerprint(
        suite_sha256=suite_hash,
        candidate_rendered_sha256=rendered_payload_sha256,
        template_version=TEMPLATE_VERSION,
    )

    model_arg = f"{client.provider}/{client.model}"
    tokens_used = 0

    # (2-cache) Baseline cache lookup.
    cache_key = (suite_hash, model_fp, samples_per_case)
    cached_baseline = _BASELINE_CACHE.get(cache_key)

    baseline_samples: dict[str, list[bool]] = {}
    candidate_samples: dict[str, list[bool]] = {}

    async def _infra_invalid(reason: str) -> EvalOutcome:
        await _write_infra_invalid_run(
            conn,
            eval_run_id=eval_run_id,
            version_id=version_id,
            suite_sha256_val=suite_hash,
            live_set_hash=live_set_hash,
            model_fp=model_fp,
            reason=reason,
            now=now,
            prompt_assembly_fp=prompt_assembly_fp,
            samples_per_case=samples_per_case,
        )
        return EvalOutcome(
            decision="infra_invalid",
            eval_run_id=eval_run_id,
            mcnemar_p=None,
            net_flips=None,
            n=None,
            suite_sha256=suite_hash,
            infra_reason=reason,
        )

    # H-module dict-in-Python-3.7+ preserves insert order → manifest
    # order; per-question iteration is deterministic.
    for suite_path, questions in verified.questions.items():
        for q in questions:
            case_id = f"{suite_path}::{q.id}"
            baseline_samples.setdefault(case_id, [])
            candidate_samples.setdefault(case_id, [])
            for _ in range(samples_per_case):
                # (2) Baseline first — skipped entirely on cache hit.
                if cached_baseline is None:
                    try:
                        answer, n_tokens = await _run_arm_sample(
                            ask_fn,
                            model_arg,
                            _baseline_prompt(q.prompt),
                            per_question_timeout_s,
                        )
                    except asyncio.TimeoutError:
                        return await _infra_invalid("timeout")
                    except Exception:
                        return await _infra_invalid("ask_exception")
                    tokens_used += max(0, int(n_tokens or 0))
                    if tokens_used > token_budget:
                        return await _infra_invalid("token_budget")
                    baseline_samples[case_id].append(q.matches(answer or ""))

                # (2) Candidate arm.
                try:
                    answer, n_tokens = await _run_arm_sample(
                        ask_fn,
                        model_arg,
                        _candidate_prompt(q.prompt, rendered_payload),
                        per_question_timeout_s,
                    )
                except asyncio.TimeoutError:
                    return await _infra_invalid("timeout")
                except Exception:
                    return await _infra_invalid("ask_exception")
                tokens_used += max(0, int(n_tokens or 0))
                if tokens_used > token_budget:
                    return await _infra_invalid("token_budget")
                candidate_samples[case_id].append(q.matches(answer or ""))

    # Assemble the sample list for collapse_clustered.
    samples: list[PairedCase] = []
    for case_id in candidate_samples:
        if cached_baseline is not None:
            base_bool = cached_baseline.get(case_id, False)
            # Cached baseline is one collapsed bool per case. Feed it as
            # the baseline_pass for every candidate sample — since
            # ``collapse_clustered`` majority-votes each side
            # INDEPENDENTLY, all-identical baseline samples collapse to
            # ``base_bool`` (matching the first-run collapse exactly),
            # while the candidate side collapses by majority of its
            # samples.
            for cand_bool in candidate_samples[case_id]:
                samples.append(
                    PairedCase(
                        case_id=case_id,
                        baseline_pass=base_bool,
                        candidate_pass=cand_bool,
                    )
                )
        else:
            k = min(
                len(baseline_samples[case_id]),
                len(candidate_samples[case_id]),
            )
            for i in range(k):
                samples.append(
                    PairedCase(
                        case_id=case_id,
                        baseline_pass=baseline_samples[case_id][i],
                        candidate_pass=candidate_samples[case_id][i],
                    )
                )

    # (4) F-math pipeline.
    collapsed = collapse_clustered(samples)
    summary = summarize(collapsed)
    decision = decide(summary)

    # (2-cache write) Populate the baseline cache post-collapse (bounded,
    # bool-only, no answer text).
    if cached_baseline is None:
        _BASELINE_CACHE[cache_key] = {
            c.case_id: c.baseline_pass for c in collapsed
        }

    # (5) Neg-control enforcement — a candidate-side pass on a flagged
    # question is the injection slipping through: FORCE reject + count
    # the catch (the counter counts injections that would have escaped).
    collapsed_by_id = {c.case_id: c for c in collapsed}
    for suite_path in verified.questions:
        for neg_qid in verified.neg_control_ids(suite_path):
            neg_case_id = f"{suite_path}::{neg_qid}"
            case = collapsed_by_id.get(neg_case_id)
            if case is None:
                continue
            if case.candidate_pass:
                decision = "reject"
                metrics.memory_neg_control_catch_total.inc()

    # (6) Ledger writes — caller owns the transaction (C1's contract).
    stat_summary = build_stat_summary(summary, decision)
    stat_summary["detail"] = build_stat_detail(summary)
    stat_summary["prompt_assembly_fingerprint"] = prompt_assembly_fp
    stat_summary["samples_per_case"] = samples_per_case

    await conn.execute(
        "INSERT INTO memory_eval_runs "
        "(id, version_id, eval_kind, suite_sha256, live_set_hash, "
        " model_fingerprint, decision, stat_summary, ran_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        eval_run_id,
        version_id,
        "plan_triage",
        suite_hash,
        live_set_hash,
        model_fp,
        decision,
        json.dumps(stat_summary, separators=(",", ":")),
        now,
    )
    for case in collapsed:
        await conn.execute(
            "INSERT INTO memory_eval_cases "
            "(id, eval_run_id, case_id, baseline_pass, candidate_pass) "
            "VALUES ($1, $2, $3, $4, $5)",
            _uuid(),
            eval_run_id,
            case.case_id,
            case.baseline_pass,
            case.candidate_pass,
        )

    return EvalOutcome(
        decision=decision,
        eval_run_id=eval_run_id,
        mcnemar_p=summary.mcnemar_p,
        net_flips=summary.net_flips,
        n=summary.n,
        suite_sha256=suite_hash,
        infra_reason=None,
    )
