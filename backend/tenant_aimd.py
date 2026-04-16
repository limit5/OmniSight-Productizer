"""M4 — Per-tenant AIMD (Additive-Increase / Multiplicative-Decrease).

Decides whose DRF budget to shrink when the host is hot, instead of the
old "flat host-wide derate" that punished innocent tenants. Uses
``host_metrics.get_culprit_tenant`` to find the offender (if any):

    host CPU hot + one outlier tenant  →  derate *only* that tenant
    host CPU hot + no outlier          →  flat derate everybody (old behaviour)
    host CPU cool                      →  gradual per-tenant recover

This module is *stateless about H2 coordinator internals* — it just
provides the decision function. H2 (future) calls ``plan_derate(...)``
each control cycle, applies the resulting multipliers to whatever
budget store it owns (DRF / quota.daily_cap / whatever), and feeds the
measured result back. Until H2 ships, the M4 tests exercise the helper
directly so the logic is locked in before the wiring lands.

Knobs live on ``AimdConfig`` so tests can override them without
monkeypatching module-level constants. Defaults mirror the classic AIMD
parameters: decrease by 50% on hot, additive +5% per cool cycle, floor
at 10% of baseline so a tenant can never be starved to death.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class DerateReason(str, Enum):
    CULPRIT = "culprit"   # single-tenant derate
    FLAT = "flat"         # host-hot, no outlier → cut everybody
    RECOVER = "recover"   # additive-increase back toward baseline
    HOLD = "hold"         # no change (cool + already at baseline)


@dataclass(frozen=True)
class AimdConfig:
    host_hot_cpu_pct: float = 85.0        # host-wide threshold to trigger MD
    host_cool_cpu_pct: float = 60.0       # below this we slowly recover
    md_factor: float = 0.5                # multiplicative decrease
    ai_step: float = 0.05                 # additive increase per cool cycle
    min_multiplier: float = 0.1           # floor — never starve a tenant
    max_multiplier: float = 1.0           # ceiling = baseline budget
    # Reuse host_metrics.get_culprit_tenant's own knobs here — keeping
    # them in one place so tests can override consistently.
    culprit_min_cpu_pct: float = 80.0
    culprit_margin_pct: float = 150.0


@dataclass
class TenantDerateState:
    tenant_id: str
    multiplier: float = 1.0               # 1.0 = full baseline budget
    last_changed: float = 0.0
    last_reason: DerateReason = DerateReason.HOLD


@dataclass
class DeratePlan:
    """One control-cycle's decision. ``affected`` maps tenant_id → new
    multiplier; tenants not in the map are unchanged. ``reason`` is the
    highest-level driver so the audit trail + Prom counter can label
    the event."""
    reason: DerateReason
    culprit_tenant_id: str | None
    affected: dict[str, float] = field(default_factory=dict)


_lock = threading.RLock()
_state: dict[str, TenantDerateState] = {}


def _reset_for_tests() -> None:
    with _lock:
        _state.clear()


def _get_state(tenant_id: str) -> TenantDerateState:
    st = _state.get(tenant_id)
    if st is None:
        st = TenantDerateState(tenant_id=tenant_id)
        _state[tenant_id] = st
    return st


def current_multiplier(tenant_id: str) -> float:
    """The most recent AIMD multiplier for ``tenant_id``. 1.0 when no
    derate has ever been applied (first-seen tenants default to
    baseline). Used by H2 + DRF to scale the raw token budget before
    handing it to ``start_container(tenant_budget=...)``."""
    with _lock:
        st = _state.get(tenant_id)
        return st.multiplier if st else 1.0


def snapshot() -> list[TenantDerateState]:
    with _lock:
        return [TenantDerateState(
            tenant_id=s.tenant_id,
            multiplier=s.multiplier,
            last_changed=s.last_changed,
            last_reason=s.last_reason,
        ) for s in _state.values()]


def plan_derate(host_cpu_pct: float,
                usage_by_tenant: dict,
                config: AimdConfig | None = None) -> DeratePlan:
    """Compute the control-cycle decision.

    ``usage_by_tenant`` maps tenant_id → ``host_metrics.TenantUsage``.
    Callers pass the aggregated snapshot from the sampler so this
    function stays pure (easier to test).
    """
    cfg = config or AimdConfig()
    now = time.time()

    # Lazy import so cyclic with host_metrics.py is fine (we only need
    # the dataclass shape here, but this keeps the type visible for
    # type-checkers without importing at module load).
    from backend.host_metrics import get_culprit_tenant

    # ── HOT path ─────────────────────────────────────────────────
    if host_cpu_pct >= cfg.host_hot_cpu_pct:
        culprit = get_culprit_tenant(
            usage_by_tenant,
            min_cpu_pct=cfg.culprit_min_cpu_pct,
            margin_pct=cfg.culprit_margin_pct,
        )
        affected: dict[str, float] = {}
        if culprit is not None:
            # Single-tenant derate — exactly what M4 asked for.
            with _lock:
                st = _get_state(culprit)
                new_mult = max(cfg.min_multiplier, st.multiplier * cfg.md_factor)
                if new_mult < st.multiplier:
                    st.multiplier = new_mult
                    st.last_changed = now
                    st.last_reason = DerateReason.CULPRIT
                    affected[culprit] = new_mult
            _emit_metric(culprit, DerateReason.CULPRIT)
            return DeratePlan(reason=DerateReason.CULPRIT,
                              culprit_tenant_id=culprit, affected=affected)

        # Fallback — host hot but no clear outlier: flat derate every
        # tenant that currently has running sandboxes.
        with _lock:
            for tid in usage_by_tenant:
                st = _get_state(tid)
                new_mult = max(cfg.min_multiplier, st.multiplier * cfg.md_factor)
                if new_mult < st.multiplier:
                    st.multiplier = new_mult
                    st.last_changed = now
                    st.last_reason = DerateReason.FLAT
                    affected[tid] = new_mult
        for tid in affected:
            _emit_metric(tid, DerateReason.FLAT)
        return DeratePlan(reason=DerateReason.FLAT,
                          culprit_tenant_id=None, affected=affected)

    # ── COOL path ────────────────────────────────────────────────
    if host_cpu_pct <= cfg.host_cool_cpu_pct:
        affected = {}
        with _lock:
            # Recover every tenant currently below baseline, whether or
            # not it's in today's usage snapshot — an idle tenant that
            # was previously derated still deserves to climb back.
            for tid, st in _state.items():
                if st.multiplier >= cfg.max_multiplier:
                    continue
                new_mult = min(cfg.max_multiplier, st.multiplier + cfg.ai_step)
                st.multiplier = new_mult
                st.last_changed = now
                st.last_reason = DerateReason.RECOVER
                affected[tid] = new_mult
        for tid in affected:
            _emit_metric(tid, DerateReason.RECOVER)
        if affected:
            return DeratePlan(reason=DerateReason.RECOVER,
                              culprit_tenant_id=None, affected=affected)

    # ── HOLD — nothing to do ─────────────────────────────────────
    return DeratePlan(reason=DerateReason.HOLD, culprit_tenant_id=None)


def _emit_metric(tenant_id: str, reason: DerateReason) -> None:
    try:
        from backend import metrics as _m
        _m.tenant_derate_total.labels(
            tenant_id=tenant_id, reason=reason.value,
        ).inc()
    except Exception as exc:
        logger.debug("tenant_derate_total metric bump failed: %s", exc)
