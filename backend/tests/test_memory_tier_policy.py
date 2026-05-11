"""C6 (OP-856) — Tier-aware memory recall policy contract tests.

Eight cases — one per state-transition branch in master-plan §3.7,
plus the cross-fleet env parser and the audit happen-before contract.
The policy module is pure-Python with an injectable audit emitter, so
no DB, no filesystem, no network is required.
"""

from __future__ import annotations

import pytest

from backend.agents import incident_recorder
from backend.agents.incident_recorder import FailureClass
from backend.agents.memory_tool_handler import (
    FEDERATION_ENV,
    TIER_L_OPTIN_ENV,
    CrossFleetRecallRefused,
    MemoryAuditWriteFailed,
    MemoryTier,
    PolicyDecision,
    RecallRequest,
    TierViolationUnauthorizedRecall,
    enforce_recall,
    evaluate_recall,
    parse_federation,
    tier_filter,
)


@pytest.fixture(autouse=True)
def _reset_audit_buffer():
    incident_recorder.reset_for_tests()
    yield
    incident_recorder.reset_for_tests()


def _request(
    tier: MemoryTier,
    *,
    query_fleet: str = "fleet-prod",
    target_fleet: str | None = None,
    query: str = "how to retry on stale bridge",
) -> RecallRequest:
    return RecallRequest(
        query=query,
        tier=tier,
        query_fleet=query_fleet,
        target_fleet=target_fleet or query_fleet,
    )


def _audit_rows() -> list[incident_recorder.IncidentRecord]:
    return incident_recorder.get_recorded_events(FailureClass.MEMORY_RECALL_AUDIT)


# ── Case 1: tier:S — recall always allowed; cross-fleet allowed ────


def test_tier_s_allows_cross_fleet_without_federation():
    request = _request(MemoryTier.S, query_fleet="fleet-dev", target_fleet="fleet-prod")

    decision = enforce_recall(request, env={})

    assert decision.permitted is True
    assert decision.escalate is False
    assert decision.refusal_reason is None
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0].permitted is True
    assert rows[0].tier == "S"
    assert "tier:S unrestricted" in rows[0].summary


# ── Case 2: tier:M same-fleet — allow + audit ─────────────────────


def test_tier_m_same_fleet_allows():
    request = _request(MemoryTier.M)

    decision = enforce_recall(request, env={})

    assert decision.permitted is True
    assert decision.escalate is False
    assert _audit_rows()[0].summary.endswith("note=tier:M same-fleet")


# ── Case 3: tier:M cross-fleet + OPTIN — allow + audit ────────────


def test_tier_m_cross_fleet_with_federation_allows():
    request = _request(
        MemoryTier.M, query_fleet="fleet-dev", target_fleet="fleet-prod"
    )

    decision = enforce_recall(
        request, env={FEDERATION_ENV: "fleet-prod, fleet-staging"}
    )

    assert decision.permitted is True
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0].target_fleet == "fleet-prod"
    assert "federation" in rows[0].summary


# ── Case 4: tier:M cross-fleet no OPTIN — refuse with CrossFleet ──


def test_tier_m_cross_fleet_without_federation_refuses():
    request = _request(
        MemoryTier.M, query_fleet="fleet-dev", target_fleet="fleet-prod"
    )

    with pytest.raises(CrossFleetRecallRefused) as excinfo:
        enforce_recall(request, env={})

    assert excinfo.value.escalate is False
    assert excinfo.value.query_fleet == "fleet-dev"
    assert excinfo.value.target_fleet == "fleet-prod"
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0].permitted is False
    assert FEDERATION_ENV in rows[0].summary


# ── Case 5: tier:L + OPTIN — allow + audit + escalate ─────────────


def test_tier_l_with_optin_allows_and_escalates():
    request = _request(MemoryTier.L)

    decision = enforce_recall(request, env={TIER_L_OPTIN_ENV: "1"})

    assert decision.permitted is True
    assert decision.escalate is True, "tier:L permitted recalls must surface for review"
    row = _audit_rows()[0]
    assert row.escalate is True
    assert "per-recall audit" in row.summary


# ── Case 6: tier:L no OPTIN — refuse ──────────────────────────────


def test_tier_l_without_optin_refuses():
    request = _request(MemoryTier.L)

    with pytest.raises(TierViolationUnauthorizedRecall) as excinfo:
        enforce_recall(request, env={})

    assert excinfo.value.tier is MemoryTier.L
    assert excinfo.value.escalate is False
    assert TIER_L_OPTIN_ENV in str(excinfo.value)
    assert _audit_rows()[0].permitted is False


# ── Case 7: tier:X — refuse + escalate ────────────────────────────


def test_tier_x_refused_and_escalated():
    request = _request(MemoryTier.X)

    with pytest.raises(TierViolationUnauthorizedRecall) as excinfo:
        enforce_recall(request, env={TIER_L_OPTIN_ENV: "1"})

    assert excinfo.value.tier is MemoryTier.X
    assert excinfo.value.escalate is True, "tier:X refusal must page operator"
    row = _audit_rows()[0]
    assert row.permitted is False
    assert row.escalate is True


# ── Case 8a: cross-fleet env parser ───────────────────────────────


def test_parse_federation_handles_whitespace_and_blanks():
    assert parse_federation(None) == frozenset()
    assert parse_federation("") == frozenset()
    assert parse_federation("fleet-a, fleet-b ,, ,fleet-c") == frozenset(
        {"fleet-a", "fleet-b", "fleet-c"}
    )
    assert parse_federation("fleet-a") == frozenset({"fleet-a"})


# ── Case 8b: audit-write happen-before — fail-open contract ───────


def test_audit_write_failure_does_not_block_permit():
    request = _request(MemoryTier.S)

    def boom(_request, _decision):
        raise MemoryAuditWriteFailed("simulated DB outage")

    decision = enforce_recall(request, env={}, audit_emitter=boom)

    assert decision.permitted is True
    # Audit failed, so the in-memory buffer stays empty — but the
    # recall result still reaches the caller.
    assert _audit_rows() == []


def test_audit_write_failure_does_not_swallow_refusal():
    request = _request(MemoryTier.X)

    def boom(_request, _decision):
        raise MemoryAuditWriteFailed("simulated DB outage")

    with pytest.raises(TierViolationUnauthorizedRecall):
        enforce_recall(request, env={}, audit_emitter=boom)


# ── Extra: tier_filter passes records through on permit ───────────


def test_tier_filter_passes_records_through_on_permit():
    records = [{"id": "L-OP-1"}, {"id": "L-OP-2"}]
    request = _request(MemoryTier.S)

    out = tier_filter(records, request=request, env={})

    assert out == records


def test_tier_filter_raises_on_refusal_and_audits():
    records = [{"id": "L-OP-1"}]
    request = _request(MemoryTier.X)

    with pytest.raises(TierViolationUnauthorizedRecall):
        tier_filter(records, request=request, env={})

    assert len(_audit_rows()) == 1


# ── Extra: evaluate_recall is pure (no side effects) ──────────────


def test_evaluate_recall_does_not_emit_audit():
    request = _request(MemoryTier.S)

    decision = evaluate_recall(request, env={})

    assert isinstance(decision, PolicyDecision)
    assert decision.permitted is True
    assert _audit_rows() == [], "evaluate_recall must not write audit rows"
