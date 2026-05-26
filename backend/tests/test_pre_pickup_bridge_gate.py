"""OP-1758 graded bridge-health pre-pickup gate tests."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd
from backend.agents import runner_coordination as rc
from backend.agents.provider_quota_tracker import QuotaState
from backend.agents.scheduler import TicketSnapshot


def _client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


def _snapshot(
    *,
    key: str = "OP-1758",
    labels: tuple[str, ...] = ("tier:M", "area:backend"),
) -> TicketSnapshot:
    return TicketSnapshot(
        key=key,
        component="META",
        fix_version=None,
        created_at="2026-05-26T00:00:00.000+0000",
        days_since_created=0.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=labels,
    )


class _FakeJira:
    def __init__(self) -> None:
        self.labels: set[str] = set()
        self.assignee: str | None = None

    def request(
        self,
        client: jd.DispatchClient,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if method == "GET" and path.startswith("/issue/"):
            return {
                "fields": {
                    "assignee": (
                        {"accountId": self.assignee} if self.assignee else None
                    ),
                    "labels": sorted(self.labels),
                }
            }
        if method == "PUT" and path.startswith("/issue/"):
            fields = (body or {}).get("fields") or {}
            if "assignee" in fields:
                assignee = fields["assignee"]
                self.assignee = (assignee or {}).get("accountId") if assignee else None
            for op in ((body or {}).get("update") or {}).get("labels", []):
                if "add" in op:
                    self.labels.add(op["add"])
                if "remove" in op:
                    self.labels.discard(op["remove"])
            return {}
        raise AssertionError(f"unexpected request {method} {path}")


def _bootstrap_runner_claims_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "runner-claims.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db))
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE runner_claims (
            lease_id TEXT PRIMARY KEY, ticket_key TEXT NOT NULL,
            resource_key TEXT NOT NULL, owner_agent_class TEXT NOT NULL,
            owner_instance_id TEXT NOT NULL, fencing_token TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'active', phase TEXT NOT NULL DEFAULT 'pickup',
            heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            released_at TEXT, release_reason TEXT,
            external_refs TEXT NOT NULL DEFAULT '{}',
            CHECK (state IN ('active', 'released'))
        );
        CREATE UNIQUE INDEX uq_runner_claims_resource_active
            ON runner_claims (resource_key) WHERE state = 'active';
        """
    )
    conn.commit()
    conn.close()


def _allow_later_pre_pickup_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.agents import file_coordinator

    monkeypatch.setattr(jd, "fetch_description", lambda c, k: "## Goal\nNo prereqs.\n")
    monkeypatch.setattr(
        jd,
        "migration_freeze_check",
        lambda c, s, description=None: (True, "no freeze"),
    )
    monkeypatch.setattr(
        file_coordinator,
        "has_unresolved_blockedby",
        lambda c, s: (False, "none"),
    )
    monkeypatch.setattr(
        jd.provider_orchestrator,
        "pre_pickup_provider_decision",
        lambda task: jd.provider_orchestrator.PrePickupProviderDecision(
            ok=True,
            reason="ok",
        ),
    )


def _quota_state(
    provider: str,
    *,
    rolling_5h_tokens: int = 0,
    weekly_tokens: int = 0,
    circuit_state: str = "closed",
) -> QuotaState:
    return QuotaState(
        provider=provider,
        rolling_5h_tokens=rolling_5h_tokens,
        weekly_tokens=weekly_tokens,
        last_reset_at=None,
        last_cap_hit_at=datetime.now(timezone.utc),
        circuit_state=circuit_state,
    )


def _stale_bridge(tmp_path: Path):
    def check() -> tuple[bool, float, Path]:
        return False, 1200.0, tmp_path / "bridge-heartbeat.json"

    return check


def test_stale_bridge_code_only_pickup_proceeds_and_records_external_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _allow_later_pre_pickup_gates(monkeypatch)
    _bootstrap_runner_claims_db(tmp_path, monkeypatch)
    monkeypatch.setenv("OMNISIGHT_RUNNER_CLAIM_SHADOW", "on")
    monkeypatch.setattr(jd, "_CLAIM_READBACK_DELAY_S", 0.0)
    monkeypatch.setattr(jd, "_request", _FakeJira().request)

    ok, reason = jd.pre_pickup_ok(
        _client(),
        _snapshot(),
        enabled_capabilities={"code_edit", "run_tests"},
        bridge_health_check=_stale_bridge(tmp_path),
    )
    claim = jd.claim_ticket_atomic(_client(), "OP-1758", "codex-1")

    assert ok is True
    assert reason == "pre-pickup checks passed"
    assert claim.ok is True
    holders = rc.find_active_holders(resource_keys=["ticket:OP-1758"])
    assert holders[0].external_refs["bridge_state"] == "stale-at-acquire"


def test_quota_exhaustion_blocks_before_runner_claim_acquire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _allow_later_pre_pickup_gates(monkeypatch)
    _bootstrap_runner_claims_db(tmp_path, monkeypatch)
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_OPENAI_SUBSCRIPTION_5H", "100")
    monkeypatch.setattr(
        jd.capability_registry.provider_quota_tracker,
        "get_quota_state",
        lambda provider: _quota_state(provider, rolling_5h_tokens=100),
    )
    monkeypatch.setattr(
        jd,
        "fetch_description",
        lambda c, k: pytest.fail("description fetch must not run after quota block"),
    )

    ok, reason = jd.pre_pickup_ok(
        _client(),
        _snapshot(
            key="OP-1762",
            labels=("tier:M", "area:backend", "class:subscription-codex"),
        ),
        enabled_capabilities={"code_edit", "run_tests", "jira_update"},
    )

    assert ok is False
    assert reason == "capability_profile.health:quota-exhausted:openai-subscription"
    assert rc.find_active_holders(resource_keys=["ticket:OP-1762"]) == []


def test_stale_bridge_gerrit_push_pickup_hard_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _allow_later_pre_pickup_gates(monkeypatch)

    ok, reason = jd.pre_pickup_ok(
        _client(),
        _snapshot(key="OP-1758-GP"),
        enabled_capabilities={"code_edit", "run_tests", "gerrit_push"},
        bridge_health_check=_stale_bridge(tmp_path),
    )

    assert ok is False
    assert reason.startswith("bridge_health_stale:")
    assert "age=1200s" in reason


def test_bridge_gate_global_rollback_blocks_non_push_pickup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _allow_later_pre_pickup_gates(monkeypatch)
    monkeypatch.setenv("OMNISIGHT_RUNNER_BRIDGE_GATE_GLOBAL", "1")

    ok, reason = jd.pre_pickup_ok(
        _client(),
        _snapshot(key="OP-1758-ROLLBACK"),
        enabled_capabilities={"code_edit", "run_lint"},
        bridge_health_check=_stale_bridge(tmp_path),
    )

    assert ok is False
    assert reason.startswith("bridge_health_stale:")
