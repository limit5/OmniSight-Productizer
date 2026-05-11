"""C1 + C6 — Anthropic Memory Tool tier-aware recall policy.

This module is the policy-decision layer for Anthropic Memory Tool
recall calls. C1 (OP-851) introduced the basic tier filter that
gates ``tier:L`` recalls behind ``OMNISIGHT_MEMORY_TIER_L_OPTIN``.
C6 (this row, OP-856) extends that filter with the finer-grained
S/M/L/X policy described in
``docs/audit/2026-05-11-sprint-abc-master-plan.md`` §3.7 and the
companion runbook at ``docs/operations/memory-tier-policy.md``.

Scope discipline
----------------
This file is the **policy decision** + **audit emission** seam only.
Memory storage (filesystem at ``/var/omnisight/memory/<fleet-id>/``),
Anthropic API wiring (``tools=[..., {"type": "memory_20260120"}]``)
and lessons-learned seeding live in C1's broader scope. C6 does not
own those moving parts; it only owns the recall-policy gate that C1
calls before returning results to the model.

Policy summary (see runbook for the full table):

* ``tier:S`` — recall always allowed; cross-fleet allowed.
* ``tier:M`` — recall allowed within fleet; cross-fleet requires
  explicit federation opt-in.
* ``tier:L`` — recall requires ``OMNISIGHT_MEMORY_TIER_L_OPTIN=1``
  (existing C1 gate); permitted recalls additionally emit a
  per-recall audit event.
* ``tier:X`` — refused; emits ``TierViolationUnauthorizedRecall``
  with ``escalate=True`` so the operator gets paged.

Cross-fleet sharing
-------------------
Different runner fleets (e.g. dev fleet vs prod fleet) own separate
memory namespaces under ``/var/omnisight/memory/<fleet-id>/``.
Cross-namespace recall requires ``OMNISIGHT_MEMORY_FEDERATION``
listing the federated fleets, comma-separated. The query fleet (the
caller's own fleet) does **not** need to appear in the federation
list — federation governs *foreign* fleet access only.

Audit
-----
Every permitted recall and every refusal posts an audit row via
``backend.agents.incident_recorder.record_memory_recall_audit`` with
``failure_class=MEMORY_RECALL_AUDIT``. Audit-write failures are
fail-open (the recall result still flows back to the caller; only the
audit row is dropped, with a logged warning so operators can spot
sustained outages).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Mapping

log = logging.getLogger(__name__)


class MemoryTier(str, Enum):
    """JIRA-style tier label attached to a memory record."""

    S = "S"
    M = "M"
    L = "L"
    X = "X"

    @classmethod
    def parse(cls, raw: str | "MemoryTier") -> "MemoryTier":
        if isinstance(raw, cls):
            return raw
        token = (raw or "").strip().upper().removeprefix("TIER:")
        try:
            return cls(token)
        except ValueError as exc:
            raise UnknownMemoryTier(token) from exc


class UnknownMemoryTier(ValueError):
    """Raised when a recall request carries a tier label outside S/M/L/X."""


class TierViolationUnauthorizedRecall(PermissionError):
    """Raised when a recall is refused by the tier-policy gate.

    Carries an ``escalate`` flag so the caller can route ``tier:X``
    refusals to the operator pager while ``tier:L`` / ``tier:M``
    refusals are merely logged. The runner promotes ``escalate=True``
    refusals to a notify-operator hook in
    ``scripts/run_s1_via_anthropic_sdk.py``.
    """

    def __init__(self, tier: MemoryTier, reason: str, *, escalate: bool):
        super().__init__(f"recall refused for tier:{tier.value}: {reason}")
        self.tier = tier
        self.reason = reason
        self.escalate = escalate


class CrossFleetRecallRefused(TierViolationUnauthorizedRecall):
    """Raised specifically when cross-fleet federation env is missing."""

    def __init__(self, tier: MemoryTier, query_fleet: str, target_fleet: str):
        super().__init__(
            tier,
            f"cross-fleet recall {query_fleet!r}->{target_fleet!r} refused; "
            "set OMNISIGHT_MEMORY_FEDERATION to opt in",
            escalate=False,
        )
        self.query_fleet = query_fleet
        self.target_fleet = target_fleet


class MemoryAuditWriteFailed(RuntimeError):
    """Raised internally when the audit-write side-effect fails.

    Caught at the policy boundary so the recall remains fail-open;
    the caller never sees this exception.
    """


# ─── Env-driven runtime knobs ──────────────────────────────────────


TIER_L_OPTIN_ENV = "OMNISIGHT_MEMORY_TIER_L_OPTIN"
"""Existing C1 gate. ``"1"`` (case-insensitive ``true``/``yes``) opts
in to tier:L recalls; anything else refuses them."""


FEDERATION_ENV = "OMNISIGHT_MEMORY_FEDERATION"
"""Comma-separated list of fleet ids whose memory namespaces this
runner is permitted to recall *across*. Order does not matter; empty
or unset disables federation entirely."""


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_truthy(env_value: str | None) -> bool:
    if env_value is None:
        return False
    return env_value.strip().lower() in _TRUTHY


def parse_federation(env_value: str | None) -> frozenset[str]:
    """Parse ``OMNISIGHT_MEMORY_FEDERATION`` into a fleet-id set.

    Whitespace is stripped, blanks are dropped. Returned set is frozen
    so downstream consumers cannot mutate the runtime federation view.
    """
    if not env_value:
        return frozenset()
    parts = (chunk.strip() for chunk in env_value.split(","))
    return frozenset(p for p in parts if p)


# ─── Policy decision ───────────────────────────────────────────────


@dataclass(frozen=True)
class RecallRequest:
    """One memory.recall(query, tier, fleet) invocation.

    ``query_fleet`` is the caller's own fleet (fleet of the runner
    issuing the recall). ``target_fleet`` is the namespace being
    queried; when they differ this is a cross-fleet recall and falls
    under the federation rule.
    """

    query: str
    tier: MemoryTier
    query_fleet: str
    target_fleet: str

    @property
    def is_cross_fleet(self) -> bool:
        return self.query_fleet != self.target_fleet


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of ``evaluate_recall``.

    * ``permitted`` — whether the recall may execute.
    * ``escalate`` — true when the operator must be paged (tier:X
      refusal). Refusals at tier:M / tier:L do not escalate because
      they reflect a missing opt-in, not a policy violation.
    * ``refusal_reason`` — populated only when permitted is false;
      mirrors the message attached to the raised exception. Useful
      for audit rows on refusals.
    * ``audit_summary`` — pre-formatted summary string for the
      ``runner_incidents.summary`` column.
    """

    permitted: bool
    tier: MemoryTier
    escalate: bool
    refusal_reason: str | None
    audit_summary: str


# Audit emission seam. Default points at ``incident_recorder``; tests
# inject an in-memory recorder to keep the policy module dependency-
# free at import time. The signature must match
# ``incident_recorder.record_memory_recall_audit``.
AuditEmitter = Callable[[RecallRequest, PolicyDecision], None]


def _default_audit_emitter(request: RecallRequest, decision: PolicyDecision) -> None:
    # Local import keeps the policy module importable even if the
    # incident_recorder shim is unavailable (e.g. early-boot or a
    # narrowly-scoped unit test).
    from backend.agents import incident_recorder

    incident_recorder.record_memory_recall_audit(request, decision)


def evaluate_recall(
    request: RecallRequest,
    *,
    env: Mapping[str, str] | None = None,
) -> PolicyDecision:
    """Resolve a recall request to a permit/refuse decision.

    Pure function — does not raise on policy refusal (caller does that
    via :func:`enforce_recall`) and does not perform the audit-write
    side-effect. The split lets tests assert the decision shape
    without coupling to the audit recorder.
    """
    env = env if env is not None else os.environ

    tier = request.tier
    cross = request.is_cross_fleet

    if tier is MemoryTier.S:
        return _allow(request, escalate=False, note="tier:S unrestricted")

    if tier is MemoryTier.M:
        if not cross:
            return _allow(request, escalate=False, note="tier:M same-fleet")
        federation = parse_federation(env.get(FEDERATION_ENV))
        if request.target_fleet in federation:
            return _allow(
                request,
                escalate=False,
                note=f"tier:M cross-fleet via federation={sorted(federation)}",
            )
        return _refuse(
            request,
            escalate=False,
            reason=(
                f"cross-fleet recall {request.query_fleet!r}->"
                f"{request.target_fleet!r} refused; "
                f"set {FEDERATION_ENV} to opt in"
            ),
        )

    if tier is MemoryTier.L:
        if not _is_truthy(env.get(TIER_L_OPTIN_ENV)):
            return _refuse(
                request,
                escalate=False,
                reason=f"tier:L recall refused; set {TIER_L_OPTIN_ENV}=1 to opt in",
            )
        if cross:
            federation = parse_federation(env.get(FEDERATION_ENV))
            if request.target_fleet not in federation:
                return _refuse(
                    request,
                    escalate=False,
                    reason=(
                        f"tier:L cross-fleet recall refused; "
                        f"set {FEDERATION_ENV} to opt in"
                    ),
                )
        # tier:L permitted recalls always escalate to operator-visible
        # audit (per AC #1 escalation level distinguished from C1's
        # basic refusal). Escalation here = "operator review", not
        # "page", since the recall itself was opted in.
        return _allow(
            request,
            escalate=True,
            note="tier:L opt-in honoured; per-recall audit",
        )

    # tier:X — always refused, always escalate (operator pager).
    if tier is MemoryTier.X:
        return _refuse(
            request,
            escalate=True,
            reason="tier:X recall refused; operator authorization required",
        )

    raise UnknownMemoryTier(tier)  # type: ignore[arg-type]


def enforce_recall(
    request: RecallRequest,
    *,
    env: Mapping[str, str] | None = None,
    audit_emitter: AuditEmitter | None = None,
) -> PolicyDecision:
    """Evaluate, audit, and raise on refusal.

    Audit-write happens **before** raising / returning — the C6 spec
    requires audit-write happen-before semantics so refusals leave a
    paper trail even when the caller short-circuits. Audit failures
    are caught and logged (fail-open per ``MemoryAuditWriteFailed``);
    the original decision still drives the return path.
    """
    decision = evaluate_recall(request, env=env)
    emit = audit_emitter or _default_audit_emitter
    try:
        emit(request, decision)
    except MemoryAuditWriteFailed as exc:
        log.warning(
            "memory_recall_audit_write_failed: tier=%s permitted=%s err=%s",
            decision.tier.value,
            decision.permitted,
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        log.warning(
            "memory_recall_audit_unexpected_error: tier=%s permitted=%s err=%s",
            decision.tier.value,
            decision.permitted,
            exc,
        )

    if not decision.permitted:
        if (
            request.is_cross_fleet
            and request.tier is MemoryTier.M
            and decision.refusal_reason
            and FEDERATION_ENV in decision.refusal_reason
        ):
            raise CrossFleetRecallRefused(
                request.tier, request.query_fleet, request.target_fleet
            )
        raise TierViolationUnauthorizedRecall(
            request.tier,
            decision.refusal_reason or "policy refused",
            escalate=decision.escalate,
        )
    return decision


def tier_filter(
    records: Iterable[Mapping[str, object]],
    *,
    request: RecallRequest,
    env: Mapping[str, str] | None = None,
    audit_emitter: AuditEmitter | None = None,
) -> list[Mapping[str, object]]:
    """C1's recall-time filter, extended with the C6 policy.

    ``records`` is the raw set of memory rows the storage layer would
    return. The function consults :func:`enforce_recall` to decide
    permit/refuse; on refuse it raises (matches C1's existing
    contract). On permit, the records pass through unchanged — the
    tier policy is *gate*, not *re-rank*.

    Forward compatibility with C1: when C1's broader memory_tool
    handler lands, it should call ``tier_filter(records, request=...)``
    immediately after retrieval and before returning to the model.
    """
    enforce_recall(request, env=env, audit_emitter=audit_emitter)
    return list(records)


def _allow(request: RecallRequest, *, escalate: bool, note: str) -> PolicyDecision:
    return PolicyDecision(
        permitted=True,
        tier=request.tier,
        escalate=escalate,
        refusal_reason=None,
        audit_summary=_format_audit_summary(request, "permitted", note),
    )


def _refuse(request: RecallRequest, *, escalate: bool, reason: str) -> PolicyDecision:
    return PolicyDecision(
        permitted=False,
        tier=request.tier,
        escalate=escalate,
        refusal_reason=reason,
        audit_summary=_format_audit_summary(request, "refused", reason),
    )


def _format_audit_summary(request: RecallRequest, outcome: str, note: str) -> str:
    return (
        f"recall query={request.query!r} tier={request.tier.value} "
        f"fleet={request.query_fleet}->{request.target_fleet} "
        f"outcome={outcome} note={note}"
    )


__all__ = [
    "AuditEmitter",
    "CrossFleetRecallRefused",
    "FEDERATION_ENV",
    "MemoryAuditWriteFailed",
    "MemoryTier",
    "PolicyDecision",
    "RecallRequest",
    "TIER_L_OPTIN_ENV",
    "TierViolationUnauthorizedRecall",
    "UnknownMemoryTier",
    "enforce_recall",
    "evaluate_recall",
    "parse_federation",
    "tier_filter",
]
