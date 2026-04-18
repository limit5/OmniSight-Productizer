#!/usr/bin/env python3
"""B15 #350 row 263 — A/B test: eager vs lazy skill loading.

Runs the same catalogue of synthetic agent tasks twice: once with
``OMNISIGHT_SKILL_LOADING=eager`` (legacy full-body inlining) and once
with ``lazy`` (Phase-1 metadata catalog + Phase-2 on-demand injection),
then reports per-task and aggregate deltas on:

* **Completion rate**  — did the mode surface the content the task needs?
  In offline mode this is a deterministic proxy: the union of what the
  prompt inlined + what Phase-2 injection pulls must cover the task's
  expected_keywords. In live mode it is "did the LLM produce a non-empty
  response that contains the expected keywords?".
* **Token usage**      — char-count of the system prompt (+ Phase-2
  injection for lazy), divided by 4 (Anthropic rule of thumb).
* **Response quality** — keyword coverage (fraction of task-level
  expected_keywords found in the prompt text / LLM response).

Usage::

    # Offline (no LLM) — pure prompt-assembly comparison. Safe for CI.
    python scripts/b15_ab_test.py --mode offline

    # Live (invokes the configured LLM via backend.llm_adapter):
    OPENAI_API_KEY=... python scripts/b15_ab_test.py --mode live

    # Write report to a custom path:
    python scripts/b15_ab_test.py --output data/my-report.md

Exit codes: 0 = ran to completion (report written). 1 = fatal error
(missing skill file, LLM requested but not configured, etc.). Note:
a regression in lazy mode is NOT a fatal exit — operators grade the
report and decide.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.prompt_loader import (  # noqa: E402
    build_skill_injection,
    build_system_prompt,
    extract_load_skill_requests,
    match_skills_for_context,
)

logger = logging.getLogger("b15_ab_test")


# ──────────────────────────────────────────────────────────────────────
#  Task catalogue — synthetic but representative of real agent work.
#  Each task covers a different (category, sub_type) tuple so the
#  A/B test exercises the catalog across the skill pack.
# ──────────────────────────────────────────────────────────────────────

TASKS: list[dict[str, Any]] = [
    {
        "id": "firmware-bsp-dtb",
        "agent_type": "firmware",
        "sub_type": "bsp",
        "domain_context": "Linux kernel BSP device tree driver I2C",
        "user_prompt": (
            "Add a new I2C sensor to the device tree and enable the "
            "kernel driver via defconfig."
        ),
        "expected_keywords": [
            "device tree", "defconfig", "I2C", "kernel", "driver",
        ],
    },
    {
        "id": "firmware-isp-pipeline",
        "agent_type": "firmware",
        "sub_type": "isp",
        "domain_context": "Camera ISP pipeline tuning AE AWB sensor",
        "user_prompt": "Tune the ISP pipeline AE and AWB for low-light.",
        "expected_keywords": ["ISP", "AE", "AWB", "sensor", "pipeline"],
    },
    {
        "id": "mobile-android-login",
        "agent_type": "mobile",
        "sub_type": "android-kotlin",
        "domain_context": "Android Kotlin Jetpack Compose login screen",
        "user_prompt": "Fix the login screen layout on Android Compose.",
        "expected_keywords": [
            "Android", "Kotlin", "Compose", "layout", "login",
        ],
    },
    {
        "id": "mobile-ios-swiftui",
        "agent_type": "mobile",
        "sub_type": "ios-swift",
        "domain_context": "iOS Swift SwiftUI view state binding",
        "user_prompt": "Add a SwiftUI settings view with state binding.",
        "expected_keywords": ["SwiftUI", "Swift", "iOS", "binding", "view"],
    },
    {
        "id": "web-react-form",
        "agent_type": "web",
        "sub_type": "frontend-react",
        "domain_context": "React TypeScript form validation hooks",
        "user_prompt": "Add client-side form validation with React hooks.",
        "expected_keywords": [
            "React", "hook", "form", "TypeScript", "validation",
        ],
    },
    {
        "id": "software-backend-python",
        "agent_type": "software",
        "sub_type": "backend-python",
        "domain_context": "FastAPI Python backend REST API endpoint pytest",
        "user_prompt": "Add a new REST endpoint with pytest coverage.",
        "expected_keywords": [
            "FastAPI", "Python", "endpoint", "pytest", "REST",
        ],
    },
]


_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Anthropic rule-of-thumb chars/4 tokenizer. Matches the same
    estimate used by ``backend.prompt_registry.get_skill_metadata`` so
    numbers line up with what the catalog advertises."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _keyword_coverage(text: str, keywords: Sequence[str]) -> float:
    """Fraction of ``keywords`` that appear (case-insensitive, whole-word
    or substring) in ``text``. 0.0 if ``keywords`` is empty."""
    if not keywords:
        return 0.0
    lower = text.lower()
    hit = sum(1 for kw in keywords if kw.lower() in lower)
    return hit / len(keywords)


# ──────────────────────────────────────────────────────────────────────
#  Offline run — measures prompt-side deltas without an LLM.
# ──────────────────────────────────────────────────────────────────────


def run_offline(task: dict[str, Any]) -> dict[str, Any]:
    """Build both system prompts and Phase-2 injection for the task and
    return a comparison row. No LLM call — safe in CI.

    For lazy mode we simulate the ReAct handshake by running the
    auto-match once (analogous to the "pre-load hint" the lazy prompt
    puts in front of the agent on turn 1)."""
    agent = task["agent_type"]
    sub = task["sub_type"]
    ctx = task["domain_context"]
    user_prompt = task["user_prompt"]
    kws = task["expected_keywords"]

    t0 = time.perf_counter()
    eager_prompt = build_system_prompt(
        agent_type=agent, sub_type=sub, mode="eager",
    )
    eager_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    lazy_prompt = build_system_prompt(
        agent_type=agent, sub_type=sub, mode="lazy",
        domain_context=ctx,
    )
    lazy_phase1_ms = (time.perf_counter() - t0) * 1000

    # Phase-2 auto-match: mirrors nodes.py's skill-on-demand fallback
    # for when the agent does not emit [LOAD_SKILL:] itself. This is the
    # conservative upper bound on lazy's final token cost.
    t0 = time.perf_counter()
    phase2_injection = build_skill_injection(
        domain_context=ctx, user_prompt=user_prompt,
    )
    phase2_ms = (time.perf_counter() - t0) * 1000

    phase2_matches = [
        m.get("name", "?")
        for m in match_skills_for_context(
            domain_context=ctx, user_prompt=user_prompt, top_k=3,
        )
    ]

    lazy_combined = lazy_prompt + ("\n\n" + phase2_injection if phase2_injection else "")

    eager_tokens = estimate_tokens(eager_prompt)
    lazy_tokens = estimate_tokens(lazy_combined)
    lazy_phase1_tokens = estimate_tokens(lazy_prompt)

    eager_cov = _keyword_coverage(eager_prompt, kws)
    lazy_cov = _keyword_coverage(lazy_combined, kws)

    token_delta = eager_tokens - lazy_tokens
    token_delta_pct = (token_delta / eager_tokens * 100) if eager_tokens else 0.0

    # Offline "completion" proxy — did the mode end up with at least half
    # the expected keywords covered? This keeps the same [0, 1] scale as
    # the live mode so summary stats are comparable.
    eager_complete = eager_cov >= 0.5
    lazy_complete = lazy_cov >= 0.5

    return {
        "task_id": task["id"],
        "agent_type": agent,
        "sub_type": sub,
        "mode": "offline",
        "eager": {
            "prompt_chars": len(eager_prompt),
            "tokens_est": eager_tokens,
            "keyword_coverage": round(eager_cov, 3),
            "completed": eager_complete,
            "build_ms": round(eager_ms, 3),
        },
        "lazy": {
            "phase1_chars": len(lazy_prompt),
            "phase1_tokens_est": lazy_phase1_tokens,
            "phase2_chars": len(phase2_injection),
            "phase2_matches": phase2_matches,
            "phase2_matched": bool(phase2_injection),
            "combined_chars": len(lazy_combined),
            "tokens_est": lazy_tokens,
            "keyword_coverage": round(lazy_cov, 3),
            "completed": lazy_complete,
            "phase1_build_ms": round(lazy_phase1_ms, 3),
            "phase2_build_ms": round(phase2_ms, 3),
        },
        "delta": {
            "tokens_saved": token_delta,
            "tokens_saved_pct": round(token_delta_pct, 1),
            "coverage_delta": round(lazy_cov - eager_cov, 3),
        },
    }


# ──────────────────────────────────────────────────────────────────────
#  Live run — invokes the configured LLM once per mode per task.
# ──────────────────────────────────────────────────────────────────────


def _approx_output_tokens(text: str) -> int:
    """Crude output-token estimate — same rule-of-thumb as input."""
    return estimate_tokens(text)


def run_live(task: dict[str, Any], *, model: str | None = None,
             max_skill_loops: int = 3) -> dict[str, Any]:
    """Run the task against the configured LLM provider in both modes.

    Keeps the lazy path honest by implementing the same ReAct inner loop
    nodes.py uses: the agent may emit ``[LOAD_SKILL: <name>]`` markers
    which we resolve by running ``build_skill_injection`` and feeding
    the body back in an extra system message. The loop is bounded to
    ``max_skill_loops`` iterations (matches nodes.py)."""
    from backend.llm_adapter import invoke_chat

    # Import langchain message types lazily so offline mode stays cheap.
    try:
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
        )
    except Exception as exc:  # pragma: no cover — exercised in live-only envs
        raise RuntimeError(
            "live mode requires langchain-core; install it or use "
            "--mode offline"
        ) from exc

    agent = task["agent_type"]
    sub = task["sub_type"]
    ctx = task["domain_context"]
    user_prompt = task["user_prompt"]
    kws = task["expected_keywords"]

    offline_row = run_offline(task)  # reuse prompt-side measurements

    # ── Eager path — single invocation.
    eager_prompt = build_system_prompt(
        agent_type=agent, sub_type=sub, mode="eager",
    )
    t0 = time.perf_counter()
    try:
        eager_resp = invoke_chat(
            [SystemMessage(content=eager_prompt), HumanMessage(content=user_prompt)],
            model=model,
        )
        eager_err = None
    except Exception as exc:
        eager_resp = ""
        eager_err = f"{type(exc).__name__}: {exc}"
    eager_ms = (time.perf_counter() - t0) * 1000

    # ── Lazy path — Phase 1 prompt + ReAct [LOAD_SKILL:] loop.
    lazy_prompt = build_system_prompt(
        agent_type=agent, sub_type=sub, mode="lazy",
        domain_context=ctx,
    )
    t0 = time.perf_counter()
    extra_msgs: list[Any] = []
    loaded_skills: set[str] = set()
    lazy_resp = ""
    lazy_err: str | None = None
    skill_load_iters = 0
    try:
        for skill_iter in range(max_skill_loops + 1):
            lazy_resp = invoke_chat(
                [
                    SystemMessage(content=lazy_prompt),
                    HumanMessage(content=user_prompt),
                    *extra_msgs,
                ],
                model=model,
            )
            requested = extract_load_skill_requests(lazy_resp)
            new = [r for r in requested if r not in loaded_skills]
            if not new:
                break
            if skill_iter >= max_skill_loops:
                break
            skill_load_iters += 1
            injection = build_skill_injection(
                explicit_skills=new,
                domain_context=ctx,
                user_prompt=user_prompt,
            )
            loaded_skills.update(new)
            if not injection:
                continue
            extra_msgs.append(AIMessage(content=lazy_resp))
            extra_msgs.append(SystemMessage(
                content=("[LOADED_SKILL] The skill body was loaded at your "
                         "request. Continue with this context.\n\n" + injection)
            ))
    except Exception as exc:
        lazy_err = f"{type(exc).__name__}: {exc}"
    lazy_ms = (time.perf_counter() - t0) * 1000

    eager_cov = _keyword_coverage(eager_resp, kws)
    lazy_cov = _keyword_coverage(lazy_resp, kws)

    # Total billed input tokens ≈ prompt-side estimate + injected skill
    # bodies. Output is the LLM response itself.
    eager_input_tokens = offline_row["eager"]["tokens_est"]
    lazy_input_tokens = offline_row["lazy"]["phase1_tokens_est"]
    for m in extra_msgs:
        if isinstance(m, SystemMessage):
            lazy_input_tokens += estimate_tokens(m.content)

    eager_output_tokens = _approx_output_tokens(eager_resp)
    lazy_output_tokens = _approx_output_tokens(lazy_resp)

    return {
        **offline_row,
        "mode": "live",
        "eager_live": {
            "response_chars": len(eager_resp),
            "response_tokens_est": eager_output_tokens,
            "input_tokens_est": eager_input_tokens,
            "keyword_coverage": round(eager_cov, 3),
            "completed": bool(eager_resp and eager_cov >= 0.5),
            "error": eager_err,
            "latency_ms": round(eager_ms, 1),
        },
        "lazy_live": {
            "response_chars": len(lazy_resp),
            "response_tokens_est": lazy_output_tokens,
            "input_tokens_est": lazy_input_tokens,
            "skill_load_iterations": skill_load_iters,
            "skills_loaded": sorted(loaded_skills),
            "keyword_coverage": round(lazy_cov, 3),
            "completed": bool(lazy_resp and lazy_cov >= 0.5),
            "error": lazy_err,
            "latency_ms": round(lazy_ms, 1),
        },
        "delta_live": {
            "input_tokens_saved": eager_input_tokens - lazy_input_tokens,
            "input_tokens_saved_pct": round(
                ((eager_input_tokens - lazy_input_tokens) / eager_input_tokens * 100)
                if eager_input_tokens else 0.0, 1,
            ),
            "coverage_delta": round(lazy_cov - eager_cov, 3),
        },
    }


# ──────────────────────────────────────────────────────────────────────
#  Reporting
# ──────────────────────────────────────────────────────────────────────


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-task rows into headline numbers for the report
    summary block. Keeps the same keys whether rows came from offline
    or live mode so downstream tooling (dashboards, tests) can consume
    one schema.

    Three averages are reported for lazy mode:
      * ``lazy.avg_tokens``             — Phase 1 catalog + Phase-2 auto
                                          injection (top-3). Upper bound.
      * ``lazy.avg_phase1_only_tokens`` — catalog alone (what hits the
                                          prompt *before* the agent asks
                                          for any skill body). Lower
                                          bound / best case.
    """
    if not rows:
        return {}
    eager_tokens = [r["eager"]["tokens_est"] for r in rows]
    lazy_tokens = [r["lazy"]["tokens_est"] for r in rows]
    lazy_phase1_tokens = [r["lazy"]["phase1_tokens_est"] for r in rows]
    eager_done = [int(r["eager"]["completed"]) for r in rows]
    lazy_done = [int(r["lazy"]["completed"]) for r in rows]
    eager_cov = [r["eager"]["keyword_coverage"] for r in rows]
    lazy_cov = [r["lazy"]["keyword_coverage"] for r in rows]

    def mean(xs: list[float]) -> float:
        return float(statistics.fmean(xs)) if xs else 0.0

    avg_eager_tok = mean(eager_tokens)
    avg_lazy_tok = mean(lazy_tokens)
    avg_lazy_phase1 = mean(lazy_phase1_tokens)
    saved_pct = ((avg_eager_tok - avg_lazy_tok) / avg_eager_tok * 100
                 if avg_eager_tok else 0.0)
    phase1_saved_pct = (
        (avg_eager_tok - avg_lazy_phase1) / avg_eager_tok * 100
        if avg_eager_tok else 0.0
    )

    return {
        "tasks": len(rows),
        "eager": {
            "completion_rate": round(mean(eager_done), 3),
            "avg_tokens": round(avg_eager_tok, 1),
            "avg_keyword_coverage": round(mean(eager_cov), 3),
        },
        "lazy": {
            "completion_rate": round(mean(lazy_done), 3),
            "avg_tokens": round(avg_lazy_tok, 1),
            "avg_phase1_only_tokens": round(avg_lazy_phase1, 1),
            "avg_keyword_coverage": round(mean(lazy_cov), 3),
        },
        "delta": {
            "avg_tokens_saved": round(avg_eager_tok - avg_lazy_tok, 1),
            "avg_tokens_saved_pct": round(saved_pct, 1),
            "avg_phase1_only_tokens_saved": round(
                avg_eager_tok - avg_lazy_phase1, 1,
            ),
            "avg_phase1_only_tokens_saved_pct": round(phase1_saved_pct, 1),
            "completion_rate_delta": round(
                mean(lazy_done) - mean(eager_done), 3,
            ),
            "coverage_delta": round(
                mean(lazy_cov) - mean(eager_cov), 3,
            ),
        },
    }


def render_markdown(rows: list[dict[str, Any]], *, mode: str,
                    summary: dict[str, Any]) -> str:
    """Render the final markdown report that gets written to disk.
    Kept separate so tests can feed it synthetic rows and assert on
    formatting without having to re-run the whole harness."""
    now = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())

    lines: list[str] = []
    lines.append("# B15 #350 — Skill Lazy Loading A/B Test Report")
    lines.append("")
    lines.append(f"Generated: {now}")
    lines.append(f"Mode: **{mode}** ({len(rows)} tasks)")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(
        "| Metric | Eager | Lazy (Phase 1 + 2) | Lazy (Phase 1 only) |"
    )
    lines.append("|---|---:|---:|---:|")
    e = summary.get("eager", {})
    l = summary.get("lazy", {})
    d = summary.get("delta", {})
    lines.append(
        f"| Completion rate | {e.get('completion_rate', 0):.1%} | "
        f"{l.get('completion_rate', 0):.1%} | — |"
    )
    lines.append(
        f"| Avg input tokens | {e.get('avg_tokens', 0):.1f} | "
        f"{l.get('avg_tokens', 0):.1f} | "
        f"{l.get('avg_phase1_only_tokens', 0):.1f} |"
    )
    lines.append(
        f"| Avg keyword coverage | {e.get('avg_keyword_coverage', 0):.1%} | "
        f"{l.get('avg_keyword_coverage', 0):.1%} | — |"
    )
    lines.append("")
    lines.append("### Token deltas vs eager")
    lines.append("")
    lines.append("| Variant | Δ tokens | Δ % |")
    lines.append("|---|---:|---:|")
    saved = d.get('avg_tokens_saved', 0)
    saved_p1 = d.get('avg_phase1_only_tokens_saved', 0)
    lines.append(
        f"| Lazy (Phase 1 + 2) | {saved:+.1f} | "
        f"{d.get('avg_tokens_saved_pct', 0):+.1f}% |"
    )
    lines.append(
        f"| Lazy (Phase 1 only, best case) | {saved_p1:+.1f} | "
        f"{d.get('avg_phase1_only_tokens_saved_pct', 0):+.1f}% |"
    )
    lines.append(
        f"| Completion Δ (lazy − eager) | "
        f"— | {d.get('completion_rate_delta', 0):+.1%} |"
    )
    lines.append(
        f"| Coverage Δ (lazy − eager)   | "
        f"— | {d.get('coverage_delta', 0):+.1%} |"
    )
    lines.append("")
    lines.append("## Per-task results")
    lines.append("")
    lines.append(
        "| Task | Eager tok | Lazy P1 tok | Lazy P1+2 tok | Saved % | "
        "Eager cov | Lazy cov | Lazy Phase-2 matches |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for r in rows:
        lazy_matches = r["lazy"].get("phase2_matches", []) or []
        matches_str = ", ".join(lazy_matches) if lazy_matches else "—"
        lines.append(
            f"| `{r['task_id']}` | "
            f"{r['eager']['tokens_est']} | "
            f"{r['lazy']['phase1_tokens_est']} | "
            f"{r['lazy']['tokens_est']} | "
            f"{r['delta']['tokens_saved_pct']:+.1f}% | "
            f"{r['eager']['keyword_coverage']:.1%} | "
            f"{r['lazy']['keyword_coverage']:.1%} | "
            f"{matches_str} |"
        )
    lines.append("")

    # Live mode adds extra columns.
    if mode == "live":
        lines.append("## Live LLM responses")
        lines.append("")
        lines.append(
            "| Task | Eager done | Lazy done | Eager latency (ms) | "
            "Lazy latency (ms) | Skill loops | Skills loaded |"
        )
        lines.append("|---|:-:|:-:|---:|---:|---:|---|")
        for r in rows:
            el = r.get("eager_live", {})
            ll = r.get("lazy_live", {})
            skills = ", ".join(ll.get("skills_loaded", []) or []) or "—"
            lines.append(
                f"| `{r['task_id']}` | "
                f"{'✅' if el.get('completed') else '❌'} | "
                f"{'✅' if ll.get('completed') else '❌'} | "
                f"{el.get('latency_ms', 0):.1f} | "
                f"{ll.get('latency_ms', 0):.1f} | "
                f"{ll.get('skill_load_iterations', 0)} | "
                f"{skills} |"
            )
        lines.append("")

    lines.append("## Key findings")
    lines.append("")
    saved_p2 = d.get("avg_tokens_saved_pct", 0)
    saved_p1 = d.get("avg_phase1_only_tokens_saved_pct", 0)
    cov_delta = d.get("coverage_delta", 0)
    if saved_p2 > 5:
        lines.append(
            f"* **Lazy (P1+2) saves ~{saved_p2:.1f}% input tokens on "
            "average** versus eager full-body inlining."
        )
    elif saved_p2 < -5:
        lines.append(
            f"* ⚠️  **Lazy (P1+2) uses {-saved_p2:.1f}% *more* input "
            "tokens than eager** — the catalog overhead + Phase-2 "
            "top-3 injection beats single-role eager loading. The "
            "optimisation only pays off once an agent legitimately "
            "needs multiple skills per task; for single-role tasks "
            "lazy is a net negative."
        )
    else:
        lines.append(
            f"* Lazy (P1+2) is roughly break-even with eager "
            f"({saved_p2:+.1f}%)."
        )
    if saved_p1 > 0:
        lines.append(
            f"* **Phase-1-only** (catalog without any on-demand "
            f"injection) saves {saved_p1:.1f}% — this is the upper "
            "bound reachable when the agent resolves the task from "
            "catalog metadata alone."
        )
    else:
        lines.append(
            f"* ⚠️  Even Phase-1-only ({saved_p1:+.1f}%) does not "
            "beat eager in this workload — the catalog is larger than "
            "a single bounded role-skill body. Catalog pruning (e.g. "
            "per-agent_type subset) would close this gap."
        )
    if cov_delta >= 0:
        lines.append(
            f"* Keyword coverage held or improved "
            f"({cov_delta:+.1%}) — lazy Phase-2 injected the right "
            "skill bodies for the task."
        )
    else:
        lines.append(
            f"* ⚠️  Keyword coverage regressed by {-cov_delta:.1%} "
            "— lazy's keyword matcher missed content that eager's "
            "full-body inline covered."
        )
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "* **eager** — `build_system_prompt(mode=\"eager\")` inlines the "
        "full role-skill body (legacy behaviour)."
    )
    lines.append(
        "* **lazy**  — `build_system_prompt(mode=\"lazy\")` emits a "
        "compact skill catalog; Phase-2 auto-matches against "
        "`domain_context`+`user_prompt` and injects the top-3 skill "
        "bodies via `build_skill_injection` — matching the code path "
        "in `backend/agents/nodes.py`."
    )
    lines.append(
        "* Token counts use the Anthropic rule of thumb (chars ÷ 4) — "
        "the same estimate `prompt_registry.get_skill_metadata` "
        "advertises on skill cards, so the numbers above align with "
        "the lazy catalog's own self-reporting."
    )
    lines.append(
        "* **Completion rate** (offline) = share of tasks whose "
        "assembled prompt covers ≥50% of the task's `expected_keywords`. "
        "In live mode this also requires a non-empty LLM response."
    )
    lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────────────


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="b15_ab_test",
        description=(
            "B15 #350 row 263 — A/B test eager vs lazy skill loading. "
            "Offline mode compares prompt-side tokens; live mode also "
            "invokes the configured LLM provider."
        ),
    )
    p.add_argument(
        "--mode", choices=("offline", "live"), default="offline",
        help=(
            "offline = prompt-assembly comparison only (safe in CI; "
            "default); live = invoke the configured LLM via "
            "backend.llm_adapter for each mode/task and measure real "
            "completion + latency."
        ),
    )
    p.add_argument(
        "--output", default="data/b15-ab-test-report.md",
        help="Markdown report output path (default: %(default)s)",
    )
    p.add_argument(
        "--json",
        help=(
            "Optional JSON sidecar of the raw per-task rows. Written "
            "alongside the markdown report when set."
        ),
    )
    p.add_argument(
        "--model", default=None,
        help=(
            "Optional model override forwarded to backend.llm_adapter. "
            "Only meaningful with --mode live; otherwise ignored."
        ),
    )
    p.add_argument(
        "--tasks",
        help=(
            "Optional JSON file overriding the built-in task catalogue. "
            "Schema: list of {id, agent_type, sub_type, domain_context, "
            "user_prompt, expected_keywords}."
        ),
    )
    return p.parse_args(argv)


def _load_tasks(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return list(TASKS)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"--tasks file must be a JSON list, got {type(data).__name__}")
    required = {"id", "agent_type", "sub_type", "domain_context",
                "user_prompt", "expected_keywords"}
    for i, t in enumerate(data):
        missing = required - set(t)
        if missing:
            raise SystemExit(
                f"--tasks entry #{i} missing keys: {sorted(missing)}"
            )
    return data


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run the harness. Returns the full result dict (summary + rows)
    so callers (tests, notebooks) can introspect without re-parsing
    the markdown."""
    tasks = _load_tasks(args.tasks)

    rows: list[dict[str, Any]] = []
    for task in tasks:
        if args.mode == "live":
            row = run_live(task, model=args.model)
        else:
            row = run_offline(task)
        rows.append(row)

    summary = _summary(rows)
    report = render_markdown(rows, mode=args.mode, summary=summary)

    out_md = Path(args.output)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(report, encoding="utf-8")

    if args.json:
        out_json = Path(args.json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(
            {"mode": args.mode, "summary": summary, "rows": rows},
            indent=2, ensure_ascii=False,
        ), encoding="utf-8")

    return {"summary": summary, "rows": rows, "report": report,
            "output": str(out_md)}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("B15_AB_LOG_LEVEL", "WARNING"))
    args = _parse_cli(argv if argv is not None else sys.argv[1:])
    result = run(args)
    s = result["summary"]
    e = s.get("eager", {})
    l = s.get("lazy", {})
    d = s.get("delta", {})
    print(
        f"[B15 A/B] tasks={s.get('tasks', 0)} mode={args.mode} "
        f"eager_tok={e.get('avg_tokens', 0):.0f} "
        f"lazy_tok={l.get('avg_tokens', 0):.0f} "
        f"saved={d.get('avg_tokens_saved_pct', 0):+.1f}% "
        f"eager_cov={e.get('avg_keyword_coverage', 0):.1%} "
        f"lazy_cov={l.get('avg_keyword_coverage', 0):.1%} "
        f"report={result['output']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
