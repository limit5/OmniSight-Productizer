"""Tests for OP-2193 / R.1 — fail-closed routed-repo dispatch pre-gate.

Wires resolve_routed_repo into _check_pre_pickup_candidate so a ticket that
asserts an unresolvable ``repo:<name>`` ABSTAINS (label + comment) instead of
falling through to the productizer workspace. A no-``repo:`` ticket is inert.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from backend.agents import routed_repo, scheduler

_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner():
    """Load auto-runner-jira.py despite the hyphen in its filename."""
    sys.modules.pop("jira_runner_under_test", None)
    spec = importlib.util.spec_from_file_location("jira_runner_under_test", _RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


arj = _load_jira_runner()


def _snap(labels):
    return scheduler.TicketSnapshot(
        key="OP-9999",
        component="META",
        fix_version=None,
        created_at="2026-06-14T00:00:00+00:00",
        days_since_created=0.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=tuple(labels),
    )


class _FakeClient:
    project_key = "OP"
    agent_class = "subscription-claude"


@pytest.fixture(autouse=True)
def _dry_run(monkeypatch):
    # DRY_RUN True → the gate computes the verdict but performs no JIRA writes,
    # so these tests don't need a live client.
    monkeypatch.setattr(arj, "DRY_RUN", True, raising=False)


@pytest.fixture
def set_routed(monkeypatch):
    def _set(raw: str) -> None:
        monkeypatch.setattr(routed_repo.settings, "routed_repos", raw, raising=False)
    return _set


def test_no_repo_label_passes_gate(set_routed):
    set_routed("")
    assert arj._routed_repo_pre_gate_ok(_FakeClient(), _snap(["area:backend", "tier:M"])) is True


def test_resolvable_repo_label_passes_gate(set_routed):
    set_routed(
        '{"conference-appliance": {"gerrit_url": "ssh://h/omnisight/conference-appliance"}}'
    )
    assert arj._routed_repo_pre_gate_ok(_FakeClient(), _snap(["repo:conference-appliance"])) is True


def test_unresolvable_repo_label_abstains(set_routed):
    set_routed("")  # no config → repo: cannot resolve
    stats = {}
    assert arj._routed_repo_pre_gate_ok(_FakeClient(), _snap(["repo:conference-appliance"]), stats) is False
    assert stats.get("other_blocked") == 1


def test_malformed_config_with_repo_label_abstains(set_routed):
    set_routed("{not json")
    assert arj._routed_repo_pre_gate_ok(_FakeClient(), _snap(["repo:x"])) is False


def test_conflicting_repo_labels_abstain(set_routed):
    set_routed("{}")
    assert arj._routed_repo_pre_gate_ok(_FakeClient(), _snap(["repo:a", "repo:b"])) is False


def test_pre_pickup_candidate_short_circuits_on_unresolved(monkeypatch, set_routed):
    # _check_pre_pickup_candidate must return False (and NOT call the downstream
    # pre_pickup_ok) when the routed pre-gate fails.
    set_routed("")
    called = {"pre_pickup_ok": False}

    def _boom(*a, **k):
        called["pre_pickup_ok"] = True
        return True, ""

    monkeypatch.setattr(arj.jira_dispatch, "pre_pickup_ok", _boom)
    assert arj._check_pre_pickup_candidate(_FakeClient(), _snap(["repo:conference-appliance"])) is False
    assert called["pre_pickup_ok"] is False  # short-circuited before the normal gate
