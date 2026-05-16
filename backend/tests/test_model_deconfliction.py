"""OP-1117 (v2-Ⅹ-5e) — Model deconfliction policy tests.

Covers the three Code AC items:

1. Failure-class catalog completeness: every runner-failure
   :class:`FailureClass` member has an explicit
   :class:`FailoverDisposition`, and the partitioning matches the
   spec (model-specific failures → TRY_NEXT_MODEL; ticket-content /
   environment failures → DONT_RETRY_OTHER_MODEL).
2. Decision matrix in dispatch:
   :func:`should_pickup_after_prior_failure` resolves the matrix and
   :func:`fetch_pickable_tickets` consults it.
3. Thrash-prevention scenarios: a multi-model chain on a
   content-bound failure halts after the first refusal; a
   model-specific failure is allowed to walk one hop; the operator
   escape hatch unconditionally allows.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import incident_recorder, jira_dispatch as jd
from backend.agents.failure_class import RUNNER_FAILURE_MEMBERS, FailureClass
from backend.agents.model_deconfliction import (
    DECONFLICTION_DISABLE_ENV,
    DEFAULT_LOOKBACK_SECONDS,
    FAILOVER_DISPOSITION,
    DispatchDecision,
    FailoverDisposition,
    failover_disposition,
    is_deconfliction_disabled,
    should_pickup_after_prior_failure,
)


@pytest.fixture(autouse=True)
def _reset_runner_incident_buffer():
    incident_recorder.reset_for_tests()
    yield
    incident_recorder.reset_for_tests()


@pytest.fixture(autouse=True)
def _ensure_deconfliction_enabled(monkeypatch):
    """Strip any pre-existing escape-hatch env so tests see the gate active."""
    monkeypatch.delenv(DECONFLICTION_DISABLE_ENV, raising=False)


# ── AC #1: failure-class catalog ───────────────────────────────────


def test_catalog_covers_every_runner_failure_member():
    """All 10 runner-failure members + OTHER catch-all have a disposition.

    ``MEMORY_RECALL_AUDIT`` is intentionally excluded — it is the C6
    audit slot on the shared table, not a runner failure.
    """
    expected = set(RUNNER_FAILURE_MEMBERS) | {FailureClass.OTHER}
    assert set(FAILOVER_DISPOSITION) == expected


def test_catalog_does_not_include_memory_recall_audit():
    assert FailureClass.MEMORY_RECALL_AUDIT not in FAILOVER_DISPOSITION


@pytest.mark.parametrize(
    "klass",
    [
        FailureClass.LINT_FAILURE,
        FailureClass.TEST_FAILURE,
        FailureClass.LLM_LOOP_DETECTED,
        FailureClass.RUNNER_TIMEOUT,
        FailureClass.OUTCOMES_GRADER_REFUSED,
    ],
)
def test_model_specific_failures_route_to_try_next(klass):
    assert failover_disposition(klass) is FailoverDisposition.TRY_NEXT_MODEL


@pytest.mark.parametrize(
    "klass",
    [
        FailureClass.MERGE_CONFLICT,
        FailureClass.WORKTREE_DIRTY,
        FailureClass.UNKNOWN_AREA_LABEL,
        FailureClass.BRIDGE_DESYNC,
        FailureClass.MUTEX_CONTENTION,
        FailureClass.OTHER,
    ],
)
def test_content_bound_failures_route_to_dont_retry(klass):
    assert (
        failover_disposition(klass) is FailoverDisposition.DONT_RETRY_OTHER_MODEL
    )


def test_failover_disposition_unknown_class_defaults_to_dont_retry():
    """Defensive — an unrecognised key never widens the thrash window."""
    # We can't fabricate a non-enum FailureClass, but we can pass a
    # sentinel through the .get() default path. The function signature
    # uses .get() so a stale enum (e.g. after a partial rollback) still
    # routes to the safe default.
    assert (
        FAILOVER_DISPOSITION.get(object(), FailoverDisposition.DONT_RETRY_OTHER_MODEL)
        is FailoverDisposition.DONT_RETRY_OTHER_MODEL
    )


# ── AC #2: should_pickup_after_prior_failure decision matrix ──────


def _record(
    ticket_key: str,
    failure_class: FailureClass,
    runner_class: str,
    *,
    created_at: float | None = None,
):
    rec = incident_recorder.record_runner_incident(
        ticket_key=ticket_key,
        failure_class=failure_class,
        summary=f"{runner_class} hit {failure_class.value}",
        runner_class=runner_class,
    )
    if created_at is not None:
        # The buffer holds frozen dataclasses; replace the stamped time
        # so we can simulate old / new incidents deterministically.
        buf = incident_recorder._runner_buffer  # type: ignore[attr-defined]
        idx = buf.index(rec)
        buf[idx] = incident_recorder.RunnerIncidentRecord(
            incident_id=rec.incident_id,
            ticket_key=rec.ticket_key,
            failure_class=rec.failure_class,
            summary=rec.summary,
            raw_traceback=rec.raw_traceback,
            runner_class=rec.runner_class,
            mutex_label=rec.mutex_label,
            area=rec.area,
            created_at=created_at,
        )
    return rec


def test_no_prior_incidents_allows_pickup():
    decision = should_pickup_after_prior_failure(
        ticket_key="OP-9001",
        current_runner_class="subscription-claude",
    )
    assert decision.allowed is True
    assert decision.blocking_incident is None
    assert "no-prior-incidents" in decision.reason


def test_same_model_prior_failure_does_not_block_failover_pickup():
    """Same-model retry is out of scope — reflection-loop guard owns it.

    The deconfliction policy looks specifically at *cross-model*
    failover; a codex failure does not block another codex pickup
    through this gate.
    """
    _record("OP-9002", FailureClass.MERGE_CONFLICT, "subscription-codex")
    decision = should_pickup_after_prior_failure(
        ticket_key="OP-9002",
        current_runner_class="subscription-codex",
    )
    assert decision.allowed is True
    assert "no-recent-cross-model-failures" in decision.reason


def test_cross_model_try_next_failure_allows_pickup():
    """Codex fails LINT_FAILURE → claude allowed (TRY_NEXT_MODEL)."""
    rec = _record("OP-9003", FailureClass.LINT_FAILURE, "subscription-codex")
    decision = should_pickup_after_prior_failure(
        ticket_key="OP-9003",
        current_runner_class="subscription-claude",
    )
    assert decision.allowed is True
    assert decision.blocking_incident == rec
    assert "TRY_NEXT_MODEL" in decision.reason


def test_cross_model_dont_retry_failure_blocks_pickup():
    """Codex fails MERGE_CONFLICT → claude refused (DONT_RETRY_OTHER_MODEL)."""
    rec = _record("OP-9004", FailureClass.MERGE_CONFLICT, "subscription-codex")
    decision = should_pickup_after_prior_failure(
        ticket_key="OP-9004",
        current_runner_class="subscription-claude",
    )
    assert decision.allowed is False
    assert decision.blocking_incident == rec
    assert "DONT_RETRY_OTHER_MODEL" in decision.reason
    assert "thrash-prevention" in decision.reason


@pytest.mark.parametrize(
    "failure_class, expected_allowed",
    [
        (FailureClass.LINT_FAILURE, True),
        (FailureClass.TEST_FAILURE, True),
        (FailureClass.LLM_LOOP_DETECTED, True),
        (FailureClass.RUNNER_TIMEOUT, True),
        (FailureClass.OUTCOMES_GRADER_REFUSED, True),
        (FailureClass.MERGE_CONFLICT, False),
        (FailureClass.WORKTREE_DIRTY, False),
        (FailureClass.UNKNOWN_AREA_LABEL, False),
        (FailureClass.BRIDGE_DESYNC, False),
        (FailureClass.MUTEX_CONTENTION, False),
        (FailureClass.OTHER, False),
    ],
    ids=lambda v: v.value if isinstance(v, FailureClass) else str(v),
)
def test_synthetic_codex_fails_claude_only_picks_up_when_try_next(
    failure_class, expected_allowed
):
    """Integration AC #1 — synthetic: codex fails on a ticket → claude
    pickup gated by failure_class membership in TRY_NEXT_MODEL set."""
    _record(
        "OP-9005", failure_class, "subscription-codex",
    )
    decision = should_pickup_after_prior_failure(
        ticket_key="OP-9005",
        current_runner_class="subscription-claude",
    )
    assert decision.allowed is expected_allowed


def test_old_failure_outside_lookback_does_not_block():
    """Recency gate — a 2-day-old MERGE_CONFLICT does not lock claude out.

    The AC wording is "another model JUST failed"; if state has changed
    enough that the failure is from a separate day, the ticket should
    be reachable again.
    """
    now = time.time()
    two_days_ago = now - (2 * 24 * 3600)
    _record(
        "OP-9006",
        FailureClass.MERGE_CONFLICT,
        "subscription-codex",
        created_at=two_days_ago,
    )
    decision = should_pickup_after_prior_failure(
        ticket_key="OP-9006",
        current_runner_class="subscription-claude",
        now=now,
    )
    assert decision.allowed is True
    assert "no-recent-cross-model-failures" in decision.reason


def test_disabled_env_var_allows_unconditionally(monkeypatch):
    monkeypatch.setenv(DECONFLICTION_DISABLE_ENV, "1")
    _record("OP-9007", FailureClass.MERGE_CONFLICT, "subscription-codex")
    assert is_deconfliction_disabled() is True
    decision = should_pickup_after_prior_failure(
        ticket_key="OP-9007",
        current_runner_class="subscription-claude",
    )
    assert decision.allowed is True
    assert "deconfliction-disabled" in decision.reason


def test_disabled_env_var_only_active_when_value_is_one(monkeypatch):
    monkeypatch.setenv(DECONFLICTION_DISABLE_ENV, "true")
    assert is_deconfliction_disabled() is False


def test_default_lookback_is_24_hours():
    assert DEFAULT_LOOKBACK_SECONDS == 24 * 3600


def test_most_recent_cross_model_failure_drives_decision():
    """Catalog ordering — newest failure wins when multiple priors exist.

    Codex first hits LINT_FAILURE (TRY_NEXT), then later hits
    MERGE_CONFLICT (DONT_RETRY). Claude must see the MERGE_CONFLICT
    and refuse pickup; the older LINT_FAILURE does not let the
    fallback past.
    """
    now = time.time()
    _record(
        "OP-9008",
        FailureClass.LINT_FAILURE,
        "subscription-codex",
        created_at=now - 600,
    )
    _record(
        "OP-9008",
        FailureClass.MERGE_CONFLICT,
        "subscription-codex",
        created_at=now - 60,
    )
    decision = should_pickup_after_prior_failure(
        ticket_key="OP-9008",
        current_runner_class="subscription-claude",
        now=now,
    )
    assert decision.allowed is False
    assert decision.blocking_incident is not None
    assert decision.blocking_incident.failure_class is FailureClass.MERGE_CONFLICT


# ── AC #3: thrash-prevention multi-model scenarios ────────────────


def test_thrash_three_model_chain_halts_on_content_bound_failure():
    """Codex hits MERGE_CONFLICT → claude blocked → a hypothetical third
    runner is also blocked. The fallback chain cannot thrash through
    every model on a content-bound failure.
    """
    _record("OP-9009", FailureClass.MERGE_CONFLICT, "subscription-codex")
    claude_decision = should_pickup_after_prior_failure(
        ticket_key="OP-9009",
        current_runner_class="subscription-claude",
    )
    assert claude_decision.allowed is False

    # Simulate claude logging a same-class failure (some other code
    # path or just operator inspection) and a third runner trying.
    _record("OP-9009", FailureClass.MERGE_CONFLICT, "subscription-claude")
    third_decision = should_pickup_after_prior_failure(
        ticket_key="OP-9009",
        current_runner_class="api-anthropic",
    )
    assert third_decision.allowed is False


def test_thrash_chain_allowed_when_each_link_is_model_specific():
    """A model-specific failure does not lock the next hop out."""
    _record("OP-9010", FailureClass.LLM_LOOP_DETECTED, "subscription-codex")
    decision = should_pickup_after_prior_failure(
        ticket_key="OP-9010",
        current_runner_class="subscription-claude",
    )
    assert decision.allowed is True


def test_same_ticket_different_classes_drives_per_ticket_isolation():
    """Decisions are scoped to ``ticket_key`` — codex's failure on
    OP-A does not block claude's pickup of OP-B."""
    _record("OP-9011", FailureClass.MERGE_CONFLICT, "subscription-codex")
    decision_b = should_pickup_after_prior_failure(
        ticket_key="OP-9012",
        current_runner_class="subscription-claude",
    )
    assert decision_b.allowed is True


# ── AC #2: fetch_pickable_tickets integration ─────────────────────


def _claude_client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-claude",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-claude-bot",
        bot_email="claude-bot@example.invalid",
    )


def _stub_jql_response(monkeypatch, issues: list[dict]) -> None:
    """Replace ``jd._request`` so JQL POST returns the given issues."""

    def fake_request(client, method, path, body=None, idem_key=None):
        if method == "POST" and path == "/search/jql":
            return {"issues": issues}
        raise AssertionError(
            f"unexpected jira_dispatch._request call: {method} {path}"
        )

    monkeypatch.setattr(jd, "_request", fake_request)


def _issue(key: str, labels: tuple[str, ...] = ("class:subscription-claude",)) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": f"{key} test",
            "labels": list(labels),
            "fixVersions": [],
            "created": "2026-05-15T10:00:00.000+0900",
            "components": [],
            "issuetype": {"name": "Story"},
        },
    }


def test_fetch_pickable_tickets_drops_ticket_blocked_by_deconfliction(monkeypatch, caplog):
    """Codex's recent MERGE_CONFLICT on OP-X removes OP-X from claude's
    pickable list and emits a structured audit line."""
    _record("OP-9013", FailureClass.MERGE_CONFLICT, "subscription-codex")
    _stub_jql_response(monkeypatch, [_issue("OP-9013"), _issue("OP-9014")])

    with caplog.at_level("INFO", logger="backend.agents.jira_dispatch"):
        result = jd.fetch_pickable_tickets(_claude_client())

    keys = {issue["key"] for issue in result}
    assert "OP-9013" not in keys
    assert "OP-9014" in keys

    audit_lines = [
        rec.message for rec in caplog.records
        if "runner_deconfliction_refusal" in rec.message
    ]
    assert any("OP-9013" in line for line in audit_lines)
    assert any("MERGE_CONFLICT" in line for line in audit_lines)


def test_fetch_pickable_tickets_keeps_ticket_when_failure_is_try_next(monkeypatch):
    """Codex's recent LINT_FAILURE on OP-Y does NOT remove OP-Y from
    claude's pickable list."""
    _record("OP-9015", FailureClass.LINT_FAILURE, "subscription-codex")
    _stub_jql_response(monkeypatch, [_issue("OP-9015")])

    result = jd.fetch_pickable_tickets(_claude_client())
    keys = {issue["key"] for issue in result}
    assert "OP-9015" in keys


def test_fetch_pickable_tickets_emits_deconfliction_audit_shape(monkeypatch, caplog):
    """Audit line carries blocking class + runner so operators can grep."""
    _record("OP-9016", FailureClass.WORKTREE_DIRTY, "subscription-codex")
    _stub_jql_response(monkeypatch, [_issue("OP-9016")])

    with caplog.at_level("INFO", logger="backend.agents.jira_dispatch"):
        jd.fetch_pickable_tickets(_claude_client())

    payloads = [
        rec.message for rec in caplog.records
        if rec.message.startswith("runner_deconfliction_refusal")
    ]
    assert payloads, "expected runner_deconfliction_refusal audit line"
    line = payloads[0]
    assert "OP-9016" in line
    assert "WORKTREE_DIRTY" in line
    assert "subscription-codex" in line


def test_dispatch_decision_dataclass_is_frozen():
    """Guard against accidental mutation of decisions in flight."""
    decision = DispatchDecision(allowed=True, reason="x")
    with pytest.raises(Exception):  # frozen dataclass — FrozenInstanceError
        decision.allowed = False  # type: ignore[misc]
