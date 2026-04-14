"""Phase 56-DAG-A — DAG semantic validator.

Pure deterministic check pass — no LLM, no DB, no IO beyond the YAML
load of `configs/tier_capabilities.yaml`. Returns ALL errors at once
so the Orchestrator's mutation prompt has the full picture in one
round (vs failing on the first which would force >3 mutation rounds
for a slightly bad DAG).

Validation rule families (each emits its own ``rule`` label so the
metric `dag_validation_error_total{rule}` can break down failures):

  cycle           — DAG must be acyclic (Kahn's algorithm).
  unknown_dep     — depends_on points at non-existent task_id.
  duplicate_id    — two tasks with the same task_id.
  tier_violation  — toolchain not allowed (or explicitly denied) in
                    the task's required_tier per
                    configs/tier_capabilities.yaml.
  io_entity       — expected_output not a recognised entity:
                    file path | git:<sha> | issue:<id>.
  dep_closure     — input not produced by an upstream task and not
                    flagged as `external:<...>` / `user:<...>`.
  mece            — two tasks declare the same expected_output and
                    neither carries `output_overlap_ack=true`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

import yaml

from backend.dag_schema import DAG, Task

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TIER_RULES_PATH = _PROJECT_ROOT / "configs" / "tier_capabilities.yaml"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rule labels + ValidationError
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RuleName = Literal[
    "cycle", "unknown_dep", "duplicate_id",
    "tier_violation", "io_entity", "dep_closure", "mece",
]


@dataclass(frozen=True)
class ValidationError:
    rule: RuleName
    task_id: str | None  # None when the error is graph-level (e.g. cycle)
    message: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "task_id": self.task_id,
                "message": self.message}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[ValidationError]

    @property
    def by_rule(self) -> dict[RuleName, int]:
        out: dict[RuleName, int] = {}
        for e in self.errors:
            out[e.rule] = out.get(e.rule, 0) + 1
        return out

    def summary(self) -> str:
        if self.ok:
            return "DAG validation: OK"
        parts = [f"{r}={c}" for r, c in self.by_rule.items()]
        return f"DAG validation FAILED ({len(self.errors)} error(s)): " + ", ".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tier capability rules — loaded once, cached.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TIER_RULES_CACHE: dict | None = None


def _load_tier_rules() -> dict:
    global _TIER_RULES_CACHE
    if _TIER_RULES_CACHE is None:
        try:
            _TIER_RULES_CACHE = yaml.safe_load(_TIER_RULES_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("tier_capabilities.yaml load failed: %s — using empty rules", exc)
            _TIER_RULES_CACHE = {"tiers": {}}
    return _TIER_RULES_CACHE


def reload_tier_rules_for_tests() -> None:
    """Test hook: forget the cached rules so a monkeypatched yaml is read."""
    global _TIER_RULES_CACHE
    _TIER_RULES_CACHE = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-rule validators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_duplicates(dag: DAG, errors: list[ValidationError]) -> None:
    seen: dict[str, int] = {}
    for t in dag.tasks:
        seen[t.task_id] = seen.get(t.task_id, 0) + 1
    for tid, n in seen.items():
        if n > 1:
            errors.append(ValidationError(
                rule="duplicate_id", task_id=tid,
                message=f"task_id {tid!r} appears {n} times",
            ))


def _check_unknown_deps(dag: DAG, errors: list[ValidationError]) -> None:
    ids = {t.task_id for t in dag.tasks}
    for t in dag.tasks:
        for d in t.depends_on:
            if d not in ids:
                errors.append(ValidationError(
                    rule="unknown_dep", task_id=t.task_id,
                    message=f"depends_on references unknown task {d!r}",
                ))


def _check_cycles(dag: DAG, errors: list[ValidationError]) -> None:
    """Kahn's algorithm. Reports the count of nodes left after a full
    pass so the caller knows the cycle's footprint without us digging
    out the exact cycle members (good enough for a planner mutation
    prompt)."""
    indeg: dict[str, int] = {t.task_id: 0 for t in dag.tasks}
    edges: dict[str, list[str]] = {t.task_id: [] for t in dag.tasks}
    for t in dag.tasks:
        for d in t.depends_on:
            if d in indeg:
                edges[d].append(t.task_id)
                indeg[t.task_id] += 1
    queue = [n for n, k in indeg.items() if k == 0]
    visited = 0
    while queue:
        n = queue.pop()
        visited += 1
        for nxt in edges[n]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if visited != len(dag.tasks):
        unresolved = [n for n, k in indeg.items() if k > 0]
        errors.append(ValidationError(
            rule="cycle", task_id=None,
            message=(f"cyclic dependency detected; "
                     f"{len(unresolved)} task(s) unresolved: {unresolved[:10]}"),
        ))


def _check_tier_capability(dag: DAG, errors: list[ValidationError]) -> None:
    rules = _load_tier_rules().get("tiers", {})
    for t in dag.tasks:
        tier_rules = rules.get(t.required_tier)
        if tier_rules is None:
            errors.append(ValidationError(
                rule="tier_violation", task_id=t.task_id,
                message=(f"tier {t.required_tier!r} has no capability "
                         f"rules in configs/tier_capabilities.yaml"),
            ))
            continue
        denied = set(tier_rules.get("toolchains_denied") or [])
        allowed = set(tier_rules.get("toolchains_allowed") or [])
        if t.toolchain in denied:
            errors.append(ValidationError(
                rule="tier_violation", task_id=t.task_id,
                message=(f"toolchain {t.toolchain!r} is explicitly DENIED "
                         f"in tier {t.required_tier!r}"),
            ))
        elif allowed and t.toolchain not in allowed:
            errors.append(ValidationError(
                rule="tier_violation", task_id=t.task_id,
                message=(f"toolchain {t.toolchain!r} not in allow-list "
                         f"for tier {t.required_tier!r}"),
            ))


# ── I/O entity matchers ──

# A rough-but-strict file path: must contain a '/', end with a sane
# filename, no shell metacharacters. Tightened on purpose; planners
# tend to write paths like "the binary file" otherwise.
_FILE_PATH_RE = re.compile(
    r"^(?!\s)(?:[A-Za-z0-9_.\-/]+/)+[A-Za-z0-9_.\-]+\.[A-Za-z0-9]{1,12}$"
)
_GIT_SHA_RE = re.compile(r"^git:[a-f0-9]{7,40}$")
_ISSUE_RE = re.compile(r"^issue:[A-Za-z0-9_.\-]{1,64}$")


def _is_io_entity(s: str) -> bool:
    return bool(
        _FILE_PATH_RE.match(s) or _GIT_SHA_RE.match(s) or _ISSUE_RE.match(s)
    )


def _check_io_entity(dag: DAG, errors: list[ValidationError]) -> None:
    for t in dag.tasks:
        if not _is_io_entity(t.expected_output):
            errors.append(ValidationError(
                rule="io_entity", task_id=t.task_id,
                message=(f"expected_output {t.expected_output!r} is not a "
                         f"recognised entity (file path / git:<sha> / "
                         f"issue:<id>)"),
            ))


# ── Dep closure ──

# An input may be one of:
#   * the expected_output of an upstream task (most common)
#   * "external:<anything>" or "user:<anything>" (caller-provided)
_INPUT_EXTERNAL_RE = re.compile(r"^(?:external|user):.+$")


def _check_dep_closure(dag: DAG, errors: list[ValidationError]) -> None:
    upstream_outputs: dict[str, set[str]] = {}
    # First pass: build a map from task -> set of outputs available to
    # downstream tasks via its expected_output.
    out_by_id: dict[str, str] = {t.task_id: t.expected_output for t in dag.tasks}
    deps_by_id: dict[str, list[str]] = {t.task_id: list(t.depends_on) for t in dag.tasks}

    # For each task, gather the closure of upstream outputs (BFS).
    for t in dag.tasks:
        seen: set[str] = set()
        stack = list(deps_by_id.get(t.task_id, []))
        while stack:
            d = stack.pop()
            if d in seen or d not in out_by_id:
                continue
            seen.add(d)
            stack.extend(deps_by_id.get(d, []))
        produced = {out_by_id[d] for d in seen}
        upstream_outputs[t.task_id] = produced

    for t in dag.tasks:
        avail = upstream_outputs.get(t.task_id, set())
        for inp in t.inputs:
            if _INPUT_EXTERNAL_RE.match(inp):
                continue  # caller provides
            if inp in avail:
                continue
            errors.append(ValidationError(
                rule="dep_closure", task_id=t.task_id,
                message=(f"input {inp!r} is not produced by any upstream "
                         f"task and is not flagged 'external:' / 'user:'"),
            ))


def _check_mece(dag: DAG, errors: list[ValidationError]) -> None:
    """Two tasks with the same expected_output trigger MECE unless
    BOTH set output_overlap_ack=True (e.g. parallel benchmark merge)."""
    by_output: dict[str, list[Task]] = {}
    for t in dag.tasks:
        by_output.setdefault(t.expected_output, []).append(t)
    for out, ts in by_output.items():
        if len(ts) <= 1:
            continue
        if all(t.output_overlap_ack for t in ts):
            continue
        ids = [t.task_id for t in ts]
        errors.append(ValidationError(
            rule="mece", task_id=None,
            message=(f"output {out!r} produced by {len(ts)} tasks {ids} "
                     f"without unanimous output_overlap_ack=true"),
        ))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def validate(dag: DAG) -> ValidationResult:
    """Run all semantic rules; return ALL errors (not first-fail)."""
    errors: list[ValidationError] = []
    _check_duplicates(dag, errors)
    _check_unknown_deps(dag, errors)
    _check_cycles(dag, errors)
    _check_tier_capability(dag, errors)
    _check_io_entity(dag, errors)
    _check_dep_closure(dag, errors)
    _check_mece(dag, errors)

    # Best-effort metric publish.
    try:
        from backend import metrics as _m
        if errors:
            _m.dag_validation_total.labels(result="failed").inc()
            for rule, count in _result_breakdown(errors).items():
                _m.dag_validation_error_total.labels(rule=rule).inc(count)
        else:
            _m.dag_validation_total.labels(result="passed").inc()
    except Exception:
        pass

    return ValidationResult(ok=not errors, errors=errors)


def _result_breakdown(errors: Iterable[ValidationError]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in errors:
        out[e.rule] = out.get(e.rule, 0) + 1
    return out
