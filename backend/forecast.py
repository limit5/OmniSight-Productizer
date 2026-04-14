"""Phase 60 — Project Forecast (v0 prototype).

Reads `configs/hardware_manifest.yaml` and produces a coarse forecast:
tasks / agents / hours / tokens / USD / confidence. The current
implementation is purely template-based; v1 will overlay history from
`token_usage` and `simulations`, v2 will fit a regression. Keeping
the API stable so the v0/v1/v2 swap is internal.

The dataclasses are intentionally JSON-friendly (asdict round-trips
cleanly) so the FastAPI router can return them via dict() without a
Pydantic shim.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Templates — track × phase task count (v0 hard-coded baselines)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# NPI phases per backend/pipeline.py
_NPI_PHASES = ["concept", "spec", "sample", "ev", "dvt", "pvt", "mp", "sustaining"]

# Tasks per phase × project_track. Numbers are the median observed in
# OmniSight-driven embedded camera projects 2025-2026 (small sample,
# refine in v1 once token_usage history is rich enough).
_TASKS_PER_PHASE: dict[str, dict[str, int]] = {
    "firmware": {
        "concept": 4, "spec": 6, "sample": 12, "ev": 14, "dvt": 16, "pvt": 12, "mp": 6, "sustaining": 4,
    },
    "driver": {
        "concept": 3, "spec": 5, "sample": 10, "ev": 12, "dvt": 14, "pvt": 10, "mp": 4, "sustaining": 3,
    },
    "algo": {
        "concept": 5, "spec": 7, "sample": 14, "ev": 18, "dvt": 20, "pvt": 14, "mp": 6, "sustaining": 5,
    },
    "app_only": {
        "concept": 2, "spec": 3, "sample": 6, "ev": 8, "dvt": 8, "pvt": 6, "mp": 3, "sustaining": 2,
    },
    "full_stack": {
        "concept": 8, "spec": 12, "sample": 20, "ev": 24, "dvt": 28, "pvt": 20, "mp": 10, "sustaining": 8,
    },
}

# Roles required per project_track (which agent types the orchestrator
# will spin up). Agent count is the cardinality of this set.
_ROLES_BY_TRACK: dict[str, list[str]] = {
    "firmware":   ["firmware", "validator", "reviewer", "reporter"],
    "driver":     ["firmware", "software", "validator", "reviewer", "reporter"],
    "algo":       ["software", "validator", "reviewer", "reporter"],
    "app_only":   ["software", "validator", "reporter"],
    "full_stack": ["firmware", "software", "validator", "reviewer", "reporter", "general", "devops"],
}

# Average resources per single task (template defaults).
_AVG_MIN_PER_TASK = 18           # 18 minutes wall-clock
_AVG_TOKENS_PER_TASK = 7_500     # input+output blended

# Cross-compile burdens add multiplier on top of host-native baseline.
_CROSS_COMPILE_PENALTY = {
    "host_native": 1.00,
    "armv7": 1.30,
    "aarch64": 1.20,
    "riscv64": 1.45,
    "vendor-example": 1.25,
    "": 1.10,
}

# Profile sensitivity multipliers (Phase 58 will set the real values).
_PROFILE_MULT = {
    "STRICT":     1.30,           # +30% time waiting for approvals
    "BALANCED":   1.00,           # baseline
    "AUTONOMOUS": 0.78,           # -22% via fewer interventions
    "GHOST":      0.65,           # -35% all-auto, staging only
}

_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_TIER_MIX = {"premium": 0.10, "default": 0.70, "budget": 0.20}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass(frozen=True)
class TaskBreakdown:
    total: int
    by_phase: dict[str, int]
    by_track: str

@dataclass(frozen=True)
class AgentBreakdown:
    total: int
    by_type: list[str]

@dataclass(frozen=True)
class DurationBreakdown:
    total_hours: float
    optimistic_hours: float          # if AUTONOMOUS profile + host-native
    pessimistic_hours: float         # if STRICT profile + cross-compile

@dataclass(frozen=True)
class TokenBreakdown:
    total: int
    by_tier: dict[str, int]

@dataclass(frozen=True)
class CostBreakdown:
    total_usd: float
    provider: str
    by_tier_usd: dict[str, float]

@dataclass(frozen=True)
class ProfileSensitivity:
    profile: str                     # STRICT / BALANCED / AUTONOMOUS / GHOST
    hours: float
    multiplier: float

@dataclass(frozen=True)
class ProjectForecast:
    project_name: str
    target_platform: str
    project_track: str
    tasks: TaskBreakdown
    agents: AgentBreakdown
    duration: DurationBreakdown
    tokens: TokenBreakdown
    cost: CostBreakdown
    confidence: float                # 0..1
    method: Literal["fresh", "template", "template+regression"]
    profile_sensitivity: list[ProfileSensitivity]
    generated_at: float

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pricing loader
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_PRICING_CACHE: dict[str, dict[str, float]] | None = None
_PRICING_PATH = _PROJECT_ROOT / "configs" / "provider_pricing.yaml"


def _load_pricing() -> dict[str, dict[str, float]]:
    global _PRICING_CACHE
    if _PRICING_CACHE is not None:
        return _PRICING_CACHE
    try:
        if _PRICING_PATH.exists():
            data = yaml.safe_load(_PRICING_PATH.read_text(encoding="utf-8")) or {}
            _PRICING_CACHE = {p: {t: float(v) for t, v in tiers.items()} for p, tiers in data.items()}
            return _PRICING_CACHE
    except Exception as exc:
        logger.warning("provider_pricing.yaml load failed: %s", exc)
    # Fallback: zero-cost (ollama-like)
    _PRICING_CACHE = {_DEFAULT_PROVIDER: {"premium": 0.0, "default": 0.0, "budget": 0.0}}
    return _PRICING_CACHE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public: from_manifest
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def from_manifest(manifest_path: Path | str | None = None,
                  provider: str | None = None) -> ProjectForecast:
    """Compute a fresh forecast from the active hardware_manifest.yaml.

    `provider` overrides the default pricing source (e.g. "openai");
    if not given, uses OMNISIGHT_LLM_PROVIDER env or anthropic.
    """
    mp = Path(manifest_path) if manifest_path else _PROJECT_ROOT / "configs" / "hardware_manifest.yaml"
    data: dict = {}
    if mp.exists():
        try:
            data = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("manifest parse failed: %s", exc)

    project = data.get("project") or {}
    project_name = project.get("name") or "(unnamed)"
    target_platform = project.get("target_platform") or ""
    project_track = (project.get("project_track") or _infer_track_from_manifest(data) or "full_stack").lower()

    if project_track not in _TASKS_PER_PHASE:
        project_track = "full_stack"
    phase_table = _TASKS_PER_PHASE[project_track]

    # ---- Tasks
    by_phase = dict(phase_table)
    total_tasks = sum(by_phase.values())

    # ---- Agents
    role_list = list(_ROLES_BY_TRACK.get(project_track, _ROLES_BY_TRACK["full_stack"]))
    agents = AgentBreakdown(total=len(role_list), by_type=role_list)

    # ---- Duration (BALANCED baseline)
    cross_mult = _CROSS_COMPILE_PENALTY.get(target_platform, _CROSS_COMPILE_PENALTY[""])
    base_minutes = total_tasks * _AVG_MIN_PER_TASK * cross_mult
    base_hours = round(base_minutes / 60.0, 1)

    duration = DurationBreakdown(
        total_hours=base_hours,
        optimistic_hours=round(base_hours * _PROFILE_MULT["AUTONOMOUS"] * (1.0 if target_platform == "host_native" else 0.95), 1),
        pessimistic_hours=round(base_hours * _PROFILE_MULT["STRICT"] * 1.05, 1),
    )

    # ---- Tokens
    total_tokens = total_tasks * _AVG_TOKENS_PER_TASK
    by_tier_tokens = {tier: int(total_tokens * pct) for tier, pct in _DEFAULT_TIER_MIX.items()}
    tokens = TokenBreakdown(total=total_tokens, by_tier=by_tier_tokens)

    # ---- Cost
    chosen_provider = provider or os.environ.get("OMNISIGHT_LLM_PROVIDER") or _DEFAULT_PROVIDER
    pricing = _load_pricing().get(chosen_provider) or _load_pricing()[_DEFAULT_PROVIDER]
    by_tier_usd = {
        tier: round(by_tier_tokens[tier] / 1_000_000 * pricing.get(tier, 0.0), 4)
        for tier in by_tier_tokens
    }
    cost = CostBreakdown(
        total_usd=round(sum(by_tier_usd.values()), 4),
        provider=chosen_provider,
        by_tier_usd=by_tier_usd,
    )

    # ---- Profile sensitivity
    sensitivity = [
        ProfileSensitivity(profile=p, hours=round(base_hours * m, 1), multiplier=m)
        for p, m in _PROFILE_MULT.items()
    ]

    # ---- Confidence (v0 always 0.5 because purely template; v1 will
    # raise for projects whose track + arch combo has historical samples)
    confidence = 0.5

    return ProjectForecast(
        project_name=project_name,
        target_platform=target_platform or "(unset)",
        project_track=project_track,
        tasks=TaskBreakdown(total=total_tasks, by_phase=by_phase, by_track=project_track),
        agents=agents,
        duration=duration,
        tokens=tokens,
        cost=cost,
        confidence=confidence,
        method="template",
        profile_sensitivity=sensitivity,
        generated_at=time.time(),
    )


def _infer_track_from_manifest(data: dict) -> str | None:
    """Best-effort guess when manifest doesn't carry an explicit
    project_track (legacy manifests). Conservative: full_stack."""
    if not data:
        return None
    sensor = data.get("sensor") or {}
    if data.get("algorithm"):
        return "algo"
    if data.get("driver_required") or sensor:
        return "driver"
    return None
