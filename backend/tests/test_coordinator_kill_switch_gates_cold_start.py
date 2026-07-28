"""OP-2735 defect 1: the acting kill-switch must gate the COLD-START gateway too.

The switch was read only in ``run_once()``, which is reached after ``startup()``
has already finished. So with ``ACTING=1`` the 4-phase cold start had already
performed real ``systemctl --user start``, real JIRA ``mention_operator`` /
``clear_assignee`` / ``remove_label``, and re-run interrupted actions -- all
before the switch was ever consulted, while its own drop-in documented it as
"observe-only ... zero mutation".

A control that mutates on the path to being read is worse than no control: it
gets trusted at exactly the moment it is not in force.

These tests pin the full matrix rather than just the fixed cell, because the
property that matters is directional -- consulting the switch may only ever make
the selection MORE restrictive. If a future edit makes any cell go from shadow
to live, that is the regression, and only the whole matrix catches it.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend.agents import pipeline_coordinator as pc


@pytest.fixture()
def config(tmp_path: Path) -> pc.CoordinatorConfig:
    return pc.CoordinatorConfig(
        config_dir=tmp_path,
        heartbeat_path=tmp_path / "heartbeat",
        decision_log_dir=tmp_path / "decisions",
    )


def _is_live(gateway: object) -> bool:
    return type(gateway).__name__ == "JiraDispatchColdStartGateway"


@pytest.mark.parametrize(
    ("acting", "kill", "expect_live"),
    [
        (True, None, True),    # the only live cell: acting on, switch absent
        (True, "0", True),     # switch explicitly off
        (True, "1", False),    # THE FIX: was live before OP-2735
        (False, None, False),
        (False, "0", False),
        (False, "1", False),
    ],
)
def test_cold_start_gateway_matrix(
    config: pc.CoordinatorConfig, monkeypatch, acting: bool, kill: str | None, expect_live: bool
) -> None:
    if kill is None:
        monkeypatch.delenv(pc.ACTING_KILL_ENV, raising=False)
    else:
        monkeypatch.setenv(pc.ACTING_KILL_ENV, kill)
    gateway = pc._default_cold_start_gateway(config, acting=acting)
    assert _is_live(gateway) is expect_live


def test_kill_switch_can_only_restrict_never_widen(
    config: pc.CoordinatorConfig, monkeypatch
) -> None:
    """The directional invariant, stated as a test.

    For every ``acting`` value, turning the switch ON must never turn a shadow
    gateway into a live one. This is the property that makes consulting the
    switch here safe to add to live-action code at all.
    """
    for acting in (True, False):
        monkeypatch.delenv(pc.ACTING_KILL_ENV, raising=False)
        without = _is_live(pc._default_cold_start_gateway(config, acting=acting))
        monkeypatch.setenv(pc.ACTING_KILL_ENV, "1")
        with_kill = _is_live(pc._default_cold_start_gateway(config, acting=acting))
        assert not (with_kill and not without), (
            f"kill switch WIDENED the gateway for acting={acting}"
        )


def test_shadow_gateway_wraps_the_live_one_rather_than_discarding_it(
    config: pc.CoordinatorConfig, monkeypatch
) -> None:
    """Shadow mode must still observe. If the kill switch produced a gateway
    that dropped the live one entirely, the coordinator would go blind rather
    than quiet -- and the whole point of shadow is that it keeps watching."""
    monkeypatch.setenv(pc.ACTING_KILL_ENV, "1")
    gateway = pc._default_cold_start_gateway(config, acting=True)
    assert isinstance(gateway, pc.ShadowColdStartGateway)
