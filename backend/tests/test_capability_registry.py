"""OP-1115 capability profile overlay tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.agents import capability_matrix, capability_registry


def _write_minimal_matrix(path: Path) -> None:
    path.write_text(
        """
schema_version: 1
capabilities:
  - code_edit
  - run_tests
  - run_lint
  - gerrit_push
  - jira_update
  - mcp_search
  - memory_recall
  - run_migration
  - deploy_action
  - run_outcomes_grader
read_only_default:
  - mcp_search
  - memory_recall
matrix:
  Story:
    backend:
      S: [code_edit, run_tests, run_lint, jira_update, mcp_search, memory_recall]
      M: [code_edit, run_tests, run_lint, gerrit_push, jira_update, mcp_search, memory_recall]
    db:
      S: [code_edit, run_tests, run_lint, jira_update, mcp_search, memory_recall, run_migration]
      M:
        - code_edit
        - run_tests
        - run_lint
        - gerrit_push
        - jira_update
        - mcp_search
        - memory_recall
        - run_migration
""".strip(),
        encoding="utf-8",
    )


class FakeConn:
    def __init__(self, row: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        self.row = row
        self.fail = fail
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if self.fail:
            raise RuntimeError("db unavailable")
        self.calls.append((sql, args))
        return self.row


def _profile_row(*, tools: list[str], max_tier: str = "L", health: str = "healthy"):
    return {
        "profile_id": "profile-01",
        "provider": "openai-subscription",
        "model": "<unknown>",
        "tools": tools,
        "max_tier": max_tier,
        "cost_mode": "subscription",
        "health_state": health,
        "active": True,
    }


@pytest.fixture()
def matrix(tmp_path: Path) -> capability_matrix.CapabilityMatrix:
    p = tmp_path / "matrix.yaml"
    _write_minimal_matrix(p)
    return capability_matrix.load_capability_matrix(p)


def test_profile_key_from_legacy_class_labels() -> None:
    assert capability_registry.profile_key_from_labels(
        ["area:backend", "class:subscription-codex"]
    ) == ("openai-subscription", "<unknown>")
    assert capability_registry.profile_key_from_labels(
        ["class:subscription-claude"]
    ) == ("anthropic-subscription", "<unknown>")


@pytest.mark.parametrize(
    ("class_label", "expected_provider"),
    [
        ("class:subscription-codex", "openai-subscription"),
        ("class:subscription-claude", "anthropic-subscription"),
    ],
)
@pytest.mark.asyncio
async def test_registry_profile_matching_matrix_returns_same_result_for_subscription_classes(
    matrix: capability_matrix.CapabilityMatrix,
    class_label: str,
    expected_provider: str,
) -> None:
    base = matrix.resolve_for_areas("Story", ["backend", "db"], "S")
    row = _profile_row(tools=sorted(capability_matrix.CAPABILITIES))
    row["provider"] = expected_provider
    conn = FakeConn(row)

    resolved = await capability_registry.resolve(
        matrix,
        conn=conn,
        ticket_type="Story",
        areas=["backend", "db"],
        tier="S",
        labels=[class_label],
    )

    assert resolved == base
    assert conn.calls[0][1] == (expected_provider, "<unknown>")


@pytest.mark.asyncio
async def test_registry_can_only_narrow_matrix_capabilities(
    matrix: capability_matrix.CapabilityMatrix,
) -> None:
    conn = FakeConn(_profile_row(tools=["code_edit", "run_tests", "mcp_search"]))

    resolved = await capability_registry.resolve(
        matrix,
        conn=conn,
        ticket_type="Story",
        areas=["backend"],
        tier="M",
        labels=["class:subscription-codex"],
    )

    assert resolved == frozenset({"code_edit", "run_tests", "mcp_search"})
    assert "gerrit_push" not in resolved


@pytest.mark.asyncio
async def test_legacy_label_overrides_apply_after_profile_overlay(
    matrix: capability_matrix.CapabilityMatrix,
) -> None:
    conn = FakeConn(_profile_row(tools=["code_edit", "run_tests", "mcp_search"]))

    resolved = await capability_registry.resolve(
        matrix,
        conn=conn,
        ticket_type="Story",
        areas=["backend"],
        tier="S",
        labels=[
            "class:subscription-codex",
            "capability:enable=gerrit_push",
            "capability:disable=run_tests",
        ],
    )

    assert "gerrit_push" in resolved
    assert "run_tests" not in resolved
    assert "code_edit" in resolved


@pytest.mark.asyncio
async def test_missing_profile_falls_back_to_read_only_default_with_labels(
    matrix: capability_matrix.CapabilityMatrix,
) -> None:
    conn = FakeConn(None)

    resolved = await capability_registry.resolve(
        matrix,
        conn=conn,
        ticket_type="Story",
        areas=["backend"],
        tier="M",
        labels=["class:subscription-codex", "capability:enable=jira_update"],
    )

    assert resolved == frozenset({"mcp_search", "memory_recall", "jira_update"})
    assert "gerrit_push" not in resolved


@pytest.mark.asyncio
async def test_db_unreachable_falls_back_to_matrix_only_resolution(
    matrix: capability_matrix.CapabilityMatrix,
) -> None:
    conn = FakeConn(fail=True)

    resolved = await capability_registry.resolve(
        matrix,
        conn=conn,
        ticket_type="Story",
        areas=["backend"],
        tier="M",
        labels=["class:subscription-codex"],
    )

    assert resolved == matrix.resolve_for_areas(
        "Story", ["backend"], "M", labels=["class:subscription-codex"]
    )


@pytest.mark.asyncio
async def test_profile_max_tier_and_health_down_deny_pickup(
    matrix: capability_matrix.CapabilityMatrix,
) -> None:
    tier_conn = FakeConn(_profile_row(tools=["code_edit"], max_tier="S"))
    with pytest.raises(capability_registry.CapabilityRegistryDenied) as tier_exc:
        await capability_registry.resolve(
            matrix,
            conn=tier_conn,
            ticket_type="Story",
            areas=["backend"],
            tier="M",
            labels=["class:subscription-codex"],
        )
    assert "exceeds max_tier" in str(tier_exc.value)

    health_conn = FakeConn(_profile_row(tools=["code_edit"], health="down"))
    with pytest.raises(capability_registry.CapabilityRegistryDenied) as health_exc:
        await capability_registry.resolve(
            matrix,
            conn=health_conn,
            ticket_type="Story",
            areas=["backend"],
            tier="S",
            labels=["class:subscription-codex"],
        )
    assert "health_state is down" in str(health_exc.value)
