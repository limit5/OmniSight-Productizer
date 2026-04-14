"""Phase 65 S1 — Training set exporter.

Walks `workflow_runs` × `workflow_steps` (× audit_log when needed)
and produces JSONL training examples for fine-tuning. The double
gate keeps the training set clean:

  * `workflow_runs.status == "completed"`           — only successes
  * `metadata.hvt_passed == true`                   — actual hardware
                                                       validation OK
  * Resolver was {user, auto+approved} for every
    decision in the run                              — no auto-only
                                                       feedback loop
  * PII scrub passed (no fragments above safety floor)

Then we pick the **shortest path**: walk the step list, skip every
step whose error is set and replaced by a later successful retry of
the same idempotency_key root. The result is the canonical
"how-it-actually-worked-end-to-end" trace, NOT the meandering
debugging history.

Output one line per example, OpenAI-style ChatML:
    {"messages": [{"role":..,"content":..}, ...],
     "metadata": {"workflow_run_id":..., "kind":..., "tokens":...}}

This module is pure: read DB → filter → format → write file. No LLM
calls. Caller (Phase 65 S4 nightly) handles scheduling + the
Tier-2 sandbox boundary for the actual fine-tune submission.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Result types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class TrainingExample:
    """One JSONL row."""
    messages: list[dict]
    metadata: dict


@dataclass
class GateReason:
    """Why a candidate run was kept or skipped — emitted to the
    operator log so the export pipeline is auditable."""
    run_id: str
    kept: bool
    reason: str


@dataclass
class ExportStats:
    written: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    output_path: str = ""

    def bump_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_KEY_ROOT_RE = re.compile(r"^([^/#]+)")


def _key_root(idempotency_key: str) -> str:
    """Strip ``/retry-N`` or ``#hash`` suffixes so we can identify
    "the same step that was retried"."""
    m = _KEY_ROOT_RE.match(idempotency_key)
    return m.group(1) if m else idempotency_key


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shortest path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def shortest_path(steps: list[Any]) -> list[Any]:
    """Given the full step list (chronological), return only the
    steps that contributed to the FINAL successful outcome.

    Rule:
      * Group by `_key_root(idempotency_key)`.
      * Within a group, the LAST successful (no error) wins.
      * Earlier steps with the same root and `error` set are dropped.
      * Steps with no successful sibling are kept as-is (they're
        either standalone failures or work-in-progress steps).
    """
    if not steps:
        return []
    by_root: dict[str, list[Any]] = {}
    for s in steps:
        by_root.setdefault(_key_root(s.idempotency_key), []).append(s)

    keepers: set[int] = set()  # step.id values to retain
    for group in by_root.values():
        # Find the LAST successful one (group is in chronological order
        # because we append in scan order).
        last_success = None
        for s in group:
            if not getattr(s, "error", None):
                last_success = s
        if last_success is not None:
            keepers.add(last_success.id)
        else:
            # No success — keep the very last attempt only (the
            # "final word" failure) so the exporter can decide.
            keepers.add(group[-1].id)

    return [s for s in steps if s.id in keepers]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _decisions_for_run(run_id: str) -> list[dict]:
    """Pull audit_log rows describing decisions resolved during this
    run. We use audit (not decision_engine in-memory state) so the
    export survives backend restarts."""
    from backend import db
    async with db._conn().execute(
        "SELECT * FROM audit_log WHERE entity_kind='decision' "
        "ORDER BY id ASC"
    ) as cur:
        rows = await cur.fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            after = json.loads(r["after_json"] or "{}")
        except Exception:
            after = {}
        # Best-effort filter: decision rows whose `source.run_id` matches.
        src = (after.get("source") or {}) if isinstance(after, dict) else {}
        if src.get("run_id") == run_id or after.get("run_id") == run_id:
            out.append({"actor": r["actor"], "after": after})
    return out


def _resolver_clean(decisions: list[dict]) -> bool:
    """All decisions were either user-resolved OR auto + later approved
    by user. Reject auto-only chains — that's the feedback-loop
    poisoning vector the design explicitly calls out."""
    if not decisions:
        return True  # no decisions → trivially clean
    for d in decisions:
        actor = (d.get("actor") or "").lower()
        after = d.get("after") or {}
        resolver = (after.get("resolver") or "").lower()
        # User resolution always passes.
        if actor.startswith("user:") or resolver in {"user", "operator", "admin"}:
            continue
        # Auto resolution must have been ratified.
        if (after.get("auto_executed") and after.get("user_approved")):
            continue
        return False
    return True


def gate(run: Any, decisions: list[dict],
         scrub_safe: bool) -> tuple[bool, str]:
    """Single decision-point: keep or skip."""
    status = getattr(run, "status", "")
    if status != "completed":
        return False, f"status={status}"
    md = getattr(run, "metadata", {}) or {}
    if not bool(md.get("hvt_passed")):
        return False, "hvt_passed_false"
    if not _resolver_clean(decisions):
        return False, "resolver_auto_only"
    if not scrub_safe:
        return False, "pii_scrub_unsafe"
    return True, "ok"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ChatML formatter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _format_messages(run: Any, kept_steps: list[Any]) -> list[dict]:
    """Render kept steps as a synthetic conversation. v1 stays
    deterministic — one user turn per step kind, one assistant turn
    per step output. Caller is responsible for any further reshape
    needed by their fine-tune backend."""
    msgs: list[dict] = [
        {"role": "system",
         "content": (
             f"You are completing a workflow of kind '{getattr(run, 'kind', '?')}'. "
             "Each user turn poses a step; respond with the step's outcome."
         )},
    ]
    for s in kept_steps:
        msgs.append({"role": "user", "content": f"step: {s.idempotency_key}"})
        body = s.output if s.output is not None else (s.error or "")
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False, sort_keys=True)
        msgs.append({"role": "assistant", "content": str(body)[:4000]})
    return msgs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-run extract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def extract_for_run(run_id: str) -> tuple[GateReason, TrainingExample | None]:
    """Pull one workflow_run, decide if it's eligible, format if so."""
    from backend import workflow as wf
    run = await wf.get_run(run_id)
    if run is None:
        return GateReason(run_id, False, "run_not_found"), None
    steps = await wf.list_steps(run_id)
    kept = shortest_path(steps)
    decisions = await _decisions_for_run(run_id)

    # Scrub the would-be body BEFORE the gate so a poisoned trace
    # short-circuits without producing a half-redacted artefact.
    from backend.skills_scrubber import scrub, is_safe_to_promote
    payload_text = json.dumps([
        {"key": s.idempotency_key,
         "out": s.output if s.output is not None else (s.error or "")}
        for s in kept
    ], default=str, ensure_ascii=False)
    _, scrub_hits = scrub(payload_text)
    scrub_safe = is_safe_to_promote(scrub_hits)

    keep, reason = gate(run, decisions, scrub_safe)
    if not keep:
        return GateReason(run_id, False, reason), None

    # Re-build with already-scrubbed payloads embedded back into the
    # step bodies for ChatML emission.
    scrubbed_steps: list[Any] = []
    from copy import copy as _copy
    for s in kept:
        body = s.output if s.output is not None else (s.error or "")
        if isinstance(body, str):
            body, _ = scrub(body)
        elif isinstance(body, dict):
            body = json.loads(scrub(json.dumps(body, ensure_ascii=False))[0])
        new_s = _copy(s)
        if getattr(new_s, "error", None):
            new_s.error = body if isinstance(body, str) else json.dumps(body)
        else:
            new_s.output = body
        scrubbed_steps.append(new_s)

    msgs = _format_messages(run, scrubbed_steps)
    example = TrainingExample(
        messages=msgs,
        metadata={
            "workflow_run_id": run.id,
            "kind": run.kind,
            "step_count": len(scrubbed_steps),
            "hvt_passed": True,
            "pii_scrub_hits": dict(scrub_hits),
            "exported_at": time.time(),
        },
    )
    return GateReason(run_id, True, "ok"), example


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bulk export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def export_jsonl(
    output_path: str | Path,
    *,
    since: float | None = None,
    limit: int = 1000,
) -> ExportStats:
    """Walk recent workflow_runs (descending), apply gate, write
    accepted examples to a JSONL file. Returns counters per skip
    reason so the caller can audit the funnel."""
    from backend import workflow as wf

    out_path = Path(output_path)
    stats = ExportStats(output_path=str(out_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    runs = await wf.list_runs(status="completed", limit=limit)
    if since is not None:
        runs = [r for r in runs if (r.completed_at or 0) >= since]

    # Open and write line-by-line so a crash leaves a partial-but-
    # still-valid JSONL file.
    with out_path.open("w", encoding="utf-8") as f:
        for r in runs:
            reason, example = await extract_for_run(r.id)
            if not reason.kept:
                stats.bump_skip(reason.reason)
                continue
            f.write(json.dumps(asdict(example), ensure_ascii=False) + "\n")
            stats.written += 1

    try:
        from backend import metrics as _m
        _m.training_set_rows.labels(result="written").inc(stats.written)
        for k, v in stats.skip_reasons.items():
            _m.training_set_rows.labels(result=f"skip:{k}").inc(v)
    except Exception:
        pass

    logger.info(
        "training set export: wrote=%d skipped=%d (%s) → %s",
        stats.written, stats.skipped, dict(stats.skip_reasons), out_path,
    )
    return stats


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI (`python -m backend.finetune_export`)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":  # pragma: no cover — CLI entry point
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Export workflow_runs to JSONL")
    parser.add_argument("--out", required=True, help="Output JSONL path")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    async def main():
        from backend import db
        await db.init()
        try:
            stats = await export_jsonl(args.out, limit=args.limit)
            print(json.dumps(asdict(stats), default=str, indent=2))
        finally:
            await db.close()

    asyncio.run(main())
