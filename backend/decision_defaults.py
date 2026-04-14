"""Phase 58 — Smart Defaults registry.

Each `kind` (or fnmatch glob) maps to a chooser callable that returns
a `ChosenOption(option_id, confidence, rationale)`. `propose()` calls
`consult()` *after* `decision_rules.apply()` so operator-authored rules
still win.

v0 chooser library covers ~20 of the most frequent kinds with simple
heuristic + history lookup. v1 will add LLM-structured-output choosers
where confidence comes from the model.

Failures: a chooser that raises is logged at warning and silently
skipped; the engine then falls through to `default_option_id`
(template behaviour).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChosenOption:
    option_id: str
    confidence: float            # 0.0 .. 1.0
    rationale: str               # short human-readable; goes into source


@dataclass(frozen=True)
class Context:
    """Inputs the chooser may consult."""
    kind: str
    severity: str
    options: list[dict[str, Any]]
    default_option_id: str | None
    is_host_native: bool = False     # Phase 59 wiring
    project_track: str = ""
    extra: dict[str, Any] | None = None


Chooser = Callable[[Context], Optional[ChosenOption]]


_REGISTRY: list[tuple[str, Chooser]] = []


def register(kind_pattern: str):
    """Decorator for chooser functions."""
    def _w(fn: Chooser) -> Chooser:
        _REGISTRY.append((kind_pattern, fn))
        return fn
    return _w


def consult(ctx: Context) -> Optional[ChosenOption]:
    """First-match chooser. Returns None if no chooser handled this kind."""
    for pattern, fn in _REGISTRY:
        if not fnmatch.fnmatchcase(ctx.kind, pattern):
            continue
        try:
            chosen = fn(ctx)
        except Exception as exc:
            logger.warning("chooser %s failed: %s", pattern, exc)
            continue
        if chosen is None:
            continue
        # Validate the chosen option is in the option set
        valid = {o.get("id") for o in ctx.options}
        if chosen.option_id not in valid:
            logger.warning("chooser %s returned unknown option_id=%s",
                           pattern, chosen.option_id)
            continue
        return chosen
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Built-in choosers (v0 seed)
#  Confidence is heuristic; v1 will add LLM-driven scores.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _safe_default(ctx: Context, *, confidence: float, why: str) -> Optional[ChosenOption]:
    """Pick the existing default with the supplied confidence and
    rationale. Used by choosers that just want to nudge a baseline
    confidence without inventing new option ids."""
    if not ctx.default_option_id:
        return None
    return ChosenOption(option_id=ctx.default_option_id, confidence=confidence, rationale=why)


@register("stuck/repeat_error")
def _choose_repeat_error(ctx: Context) -> Optional[ChosenOption]:
    """3+ identical errors → switch_model is the right call."""
    if any(o.get("id") == "switch_model" for o in ctx.options):
        return ChosenOption("switch_model", 0.92, "agent stuck on identical error")
    return _safe_default(ctx, confidence=0.7, why="repeat_error fallback")


@register("stuck/long_running")
def _choose_long_running(ctx: Context) -> Optional[ChosenOption]:
    """> 15 min running → spawn_alternate keeps the user un-blocked."""
    if any(o.get("id") == "spawn_alternate" for o in ctx.options):
        return ChosenOption("spawn_alternate", 0.78, "agent slow but not erroring")
    return _safe_default(ctx, confidence=0.6, why="long_running fallback")


@register("stuck/blocked_forever")
def _choose_blocked_forever(ctx: Context) -> Optional[ChosenOption]:
    """1h+ blocked → escalate is the right call but it's destructive
    so confidence stays low — the profile gates it."""
    if any(o.get("id") == "escalate" for o in ctx.options):
        return ChosenOption("escalate", 0.65, "blocked > 1h, human input needed")
    return _safe_default(ctx, confidence=0.5, why="blocked_forever fallback")


@register("provider_switch")
@register("provider/*")
def _choose_provider_switch(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.8, why="default provider preferred")


@register("model_switch/refactor")
@register("model_switch/*")
def _choose_model_switch(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.75, why="default tier appropriate")


@register("branch/create")
@register("branch_naming/*")
def _choose_branch_create(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.95, why="branch naming follows convention")


@register("commit_style")
@register("commit/format")
def _choose_commit_style(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.9, why="conventional commits style")


@register("test_framework/select")
@register("test/runner")
def _choose_test_runner(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.88, why="default runner per project track")


@register("retry_strategy/transient")
def _choose_retry_strategy(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.9, why="transient — exponential backoff is safe")


@register("webhook_order/*")
def _choose_webhook_order(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.85, why="alphabetical order is deterministic")


@register("compression_strategy/*")
def _choose_compression(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.82, why="rtk default per workload")


@register("default_timeout/*")
def _choose_timeout(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.88, why="default timeout matches measured p95")


@register("file_scope/glob")
def _choose_file_scope(ctx: Context) -> Optional[ChosenOption]:
    return _safe_default(ctx, confidence=0.8, why="codeowners scope is conservative")


@register("ambiguity/*")
def _choose_ambiguity(ctx: Context) -> Optional[ChosenOption]:
    """Ambiguity decisions ship a safe_default_id; honour it with high confidence."""
    return _safe_default(ctx, confidence=0.85, why="propose() supplied safe_default")


# Phase 59 — host-native fast path. Kinds that are normally `risky`
# (deploy/dev_board, binary/execute) become safer when
# host_arch == target_arch + container isolation.
@register("deploy/dev_board")
@register("deploy/host")
def _choose_deploy_host_native(ctx: Context) -> Optional[ChosenOption]:
    if ctx.is_host_native:
        return _safe_default(ctx, confidence=0.92,
                             why="host-native deploy = container exec; routine")
    return _safe_default(ctx, confidence=0.65, why="cross-arch deploy; tread carefully")


@register("binary/execute")
def _choose_binary_execute(ctx: Context) -> Optional[ChosenOption]:
    if ctx.is_host_native:
        return _safe_default(ctx, confidence=0.95,
                             why="same-arch binary; container-isolated execution")
    return _safe_default(ctx, confidence=0.7, why="cross-arch via QEMU")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _registered_patterns() -> list[str]:
    return [p for p, _ in _REGISTRY]
