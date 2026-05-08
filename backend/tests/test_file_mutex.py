"""OP-731 file-level pickup mutex tests."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd
from backend.agents import scheduler

_RUNNER_PATH = REPO_ROOT / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_file_mutex_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_file_mutex_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snapshot(
    key: str = "OP-731",
    labels: tuple[str, ...] = ("class:subscription-codex", "scope:runner-pipeline"),
) -> scheduler.TicketSnapshot:
    return scheduler.TicketSnapshot(
        key=key,
        component="META",
        fix_version=None,
        created_at="2026-05-08T00:00:00.000+0000",
        days_since_created=0.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=labels,
    )


def _client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic test",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


def test_predict_target_files_extracts_paths_from_five_real_op_descriptions() -> None:
    """Explicit Files / Paths wins for OP descriptions copied from repo seed/migration data."""
    descriptions = [
        """
## Goal
Central registry.

## Files / Paths
- backend/agents/provider_orchestrator.py (new, ~250 LOC)
- backend/tests/test_provider_orchestrator.py (new, ~30 test)

## Spec references
""",
        """
## Files / Paths
- backend/alembic/versions/XXXX_agent_character_card.py (new)
- backend/tests/test_alembic_XXXX_agent_character_card.py (new)

## Prerequisites
""",
        """
## Files / Paths
- `auto-runner-jira.py`
- `backend/agents/jira_dispatch.py`
- `backend/tests/test_file_mutex.py`

## Spec references
""",
        """
## Files / Paths
_(refine on pickup)_
- docs/sop/jira-ticket-conventions.md

## Acceptance Criteria
""",
        """
## Files / Paths
- backend/agents/scope_to_paths.py (NEW) -- scope label map
- backend/tests/test_file_mutex.py (NEW)

## Spec references
""",
    ]
    expected = [
        {"backend/agents/provider_orchestrator.py", "backend/tests/test_provider_orchestrator.py"},
        {"backend/alembic/versions/XXXX_agent_character_card.py", "backend/tests/test_alembic_XXXX_agent_character_card.py"},
        {"auto-runner-jira.py", "backend/agents/jira_dispatch.py", "backend/tests/test_file_mutex.py"},
        {"docs/sop/jira-ticket-conventions.md"},
        {"backend/agents/scope_to_paths.py", "backend/tests/test_file_mutex.py"},
    ]

    for description, paths in zip(descriptions, expected, strict=True):
        assert jd.predict_target_files(_snapshot(), description=description) == paths


def test_predict_target_files_uses_scope_label_when_files_section_missing() -> None:
    paths = jd.predict_target_files(_snapshot(labels=("scope:runner-pipeline",)), description="## Goal\n")
    assert "auto-runner-jira.py" in paths
    assert "backend/agents/jira_*.py" in paths


def test_open_bot_owned_files_parses_three_open_patch_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    changes = [
        {
            "number": "160",
            "owner": {"username": "claude-bot"},
            "currentPatchSet": {"files": [{"file": "docs/operations/a.md"}, {"file": "/COMMIT_MSG"}]},
        },
        {
            "number": "161",
            "owner": {"username": "codex-bot"},
            "currentPatchSet": {"files": [{"file": "backend/agents/jira_dispatch.py"}]},
        },
        {
            "number": "162",
            "owner": {"username": "claude-bot"},
            "currentPatchSet": {"files": [{"file": "auto-runner-jira.py"}]},
        },
        {"type": "stats", "rowCount": 3},
    ]
    stdout = "\n".join(json.dumps(c) for c in changes)

    class FakeResult:
        def __init__(self) -> None:
            self.stdout = stdout

        def check_returncode(self) -> None:
            return None

    monkeypatch.setattr(jd.subprocess, "run", lambda *a, **kw: FakeResult())

    assert jd.open_bot_owned_files() == {
        "docs/operations/a.md",
        "backend/agents/jira_dispatch.py",
        "auto-runner-jira.py",
    }


def test_file_mutex_check_blocks_when_target_overlaps_open_patch_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        jd,
        "_open_bot_owned_file_owners",
        lambda: {"backend/agents/jira_dispatch.py": [jd.GerritFileOwner("161", "codex-bot")]},
    )
    ok, reason = jd.file_mutex_check(
        _snapshot(),
        description="## Files / Paths\n- backend/agents/jira_dispatch.py\n",
    )
    assert ok is False
    assert "backend/agents/jira_dispatch.py" in reason
    assert "#161" in reason
    assert "codex-bot" in reason


def test_file_mutex_check_allows_ticket_with_no_predictable_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_query_gerrit():
        raise AssertionError("Gerrit query should be skipped without predicted paths")

    monkeypatch.setattr(jd, "_open_bot_owned_file_owners", must_not_query_gerrit)
    ok, reason = jd.file_mutex_check(_snapshot(labels=()), description="## Goal\nNo files listed.\n")
    assert ok is True
    assert "skipped" in reason


def test_synthetic_pickup_skips_colliding_ticket_and_picks_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    candidates = [_snapshot("OP-A"), _snapshot("OP-B")]
    calls: dict[str, list] = {"labels": [], "comments": []}

    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(mod.jira_dispatch, "pre_pickup_ok", lambda c, s: (True, "ok"))
    monkeypatch.setattr(mod.jira_dispatch, "fetch_description", lambda c, k: f"desc for {k}")
    monkeypatch.setattr(
        mod.jira_dispatch,
        "file_mutex_check",
        lambda s, description=None: (
            (False, "file collision: backend/agents/jira_dispatch.py already in open PS #161 (owner: codex-bot)")
            if s.key == "OP-A"
            else (True, "no collision")
        ),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_label",
        lambda c, k, label: calls["labels"].append((k, label)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda c, k, text: calls["comments"].append((k, text)),
    )
    monkeypatch.setattr(mod.jira_dispatch, "remove_label", lambda c, k, label: None)

    weights = scheduler.SchedulerWeights(
        schema_version=1,
        phase=0,
        priority_weights={"META": 90, "default": 50},
        per_downstream_unblock=5,
        max_unblock_bonus=30,
        deadline_pressure_coefficient=10,
        age_bonus_coefficient=3,
        mutex_in_progress_penalty=50,
    )
    stats = {"file_mutex_blocked": 0, "other_blocked": 0}
    winner = scheduler.dispatch(
        candidates,
        weights,
        pre_pickup_check=lambda t: mod._check_pre_pickup_candidate(_client(), t, stats),
    )

    assert winner is not None
    assert winner.key == "OP-B"
    assert calls["labels"] == [("OP-A", jd.FILE_COLLISION_SKIP_LABEL)]
    assert len(calls["comments"]) == 1
    assert calls["comments"][0][0] == "OP-A"
    assert "[runner-file-mutex]" in calls["comments"][0][1]


def test_runner_main_all_candidates_blocked_by_file_mutex_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_jira_runner()
    snapshots = [_snapshot("OP-A"), _snapshot("OP-B")]

    monkeypatch.setattr(mod, "TARGET_OVERRIDE", "")
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(mod.jira_dispatch, "make_client", lambda cls: _client())
    monkeypatch.setattr(mod.jira_dispatch, "fetch_pickable_tickets", lambda c: [{"key": s.key, "fields": {}} for s in snapshots])
    monkeypatch.setattr(mod.jira_dispatch, "to_snapshot", lambda issue: next(s for s in snapshots if s.key == issue["key"]))
    monkeypatch.setattr(mod.scheduler, "load_weights", lambda: scheduler.SchedulerWeights(
        schema_version=1,
        phase=0,
        priority_weights={"META": 90, "default": 50},
        per_downstream_unblock=5,
        max_unblock_bonus=30,
        deadline_pressure_coefficient=10,
        age_bonus_coefficient=3,
        mutex_in_progress_penalty=50,
    ))
    monkeypatch.setattr(mod.jira_dispatch, "pre_pickup_ok", lambda c, s: (True, "ok"))
    monkeypatch.setattr(mod.jira_dispatch, "fetch_description", lambda c, k: f"desc for {k}")
    monkeypatch.setattr(
        mod.jira_dispatch,
        "file_mutex_check",
        lambda s, description=None: (False, "file collision: auto-runner-jira.py already in open PS #160 (owner: claude-bot)"),
    )
    monkeypatch.setattr(mod.jira_dispatch, "add_label", lambda c, k, label: None)
    monkeypatch.setattr(mod.jira_dispatch, "add_comment", lambda c, k, text: None)

    rc = mod.main()

    assert rc == 0
    assert "[runner] all candidates blocked by file-mutex" in capsys.readouterr().out
