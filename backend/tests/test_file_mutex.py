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
        {"docs/sop/lessons/L-OP-731-*.md", "backend/agents/provider_orchestrator.py", "backend/tests/test_provider_orchestrator.py"},
        {"docs/sop/lessons/L-OP-731-*.md", "backend/alembic/versions/XXXX_agent_character_card.py", "backend/tests/test_alembic_XXXX_agent_character_card.py"},
        {"docs/sop/lessons/L-OP-731-*.md", "auto-runner-jira.py", "backend/agents/jira_dispatch.py", "backend/tests/test_file_mutex.py"},
        {"docs/sop/lessons/L-OP-731-*.md", "docs/sop/jira-ticket-conventions.md"},
        {"docs/sop/lessons/L-OP-731-*.md", "backend/agents/scope_to_paths.py", "backend/tests/test_file_mutex.py"},
    ]

    for description, paths in zip(descriptions, expected, strict=True):
        assert jd.predict_target_files(_snapshot(), description=description) == paths


def test_predict_target_files_uses_scope_label_when_files_section_missing() -> None:
    paths = jd.predict_target_files(_snapshot(labels=("scope:runner-pipeline",)), description="## Goal\n")
    # OP-795 Bug 1: per-ticket lesson path, not a shared wildcard
    assert "docs/sop/lessons/L-OP-731-*.md" in paths
    assert "docs/sop/lessons/*.md" not in paths
    assert "auto-runner-jira.py" in paths
    assert "backend/agents/jira_*.py" in paths


def test_predict_target_files_includes_lessons_for_unknown_scope() -> None:
    # OP-795 Bug 1: predicted lesson path is per-ticket, never the shared wildcard
    assert jd.predict_target_files(_snapshot(labels=("scope:unknown",)), description="## Goal\n") == {
        "docs/sop/lessons/L-OP-731-*.md"
    }


def test_op_795_two_tickets_writing_different_lessons_do_not_collide() -> None:
    """OP-795 Bug 1 regression: previously ALWAYS_TOUCHED = {"docs/sop/lessons/*.md"}
    matched any lesson PS via fnmatch and blocked every other ticket. With the
    per-ticket template, two tickets adding distinct lessons predict disjoint
    paths and `_paths_overlap` reports no collision.
    """
    a = jd.predict_target_files(_snapshot(key="OP-720", labels=("scope:unknown",)), description="## Goal\n")
    b = jd.predict_target_files(_snapshot(key="OP-748", labels=("scope:unknown",)), description="## Goal\n")
    in_flight_a_lesson = "docs/sop/lessons/L-OP-720-sibling-ps-race.md"
    # OP-748's prediction must NOT match an in-flight OP-720 lesson path
    assert jd._paths_overlap(b, {in_flight_a_lesson}) == set()
    # But OP-720's own prediction DOES match its own lesson (sanity check)
    assert jd._paths_overlap(a, {in_flight_a_lesson}) == {in_flight_a_lesson}


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


def test_file_mutex_check_allows_ticket_with_only_lessons_path_when_unowned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jd, "_open_bot_owned_file_owners", lambda: {})
    ok, reason = jd.file_mutex_check(_snapshot(labels=()), description="## Goal\nNo files listed.\n")
    assert ok is True
    assert "no collision" in reason


# ── OP-800: Pattern 12 cure (3) — runtime layer ────────────────────


_SCAFFOLD_DESCRIPTION_E1 = (
    "## Goal\nE1 spike + ADR.\n\n"
    "## Files / Paths\n"
    "- docs-site/mkdocs.yml (NEW)\n"
    "- docs-site/requirements.txt (NEW)\n"
    "- docs-site/docs/index.md (NEW)\n"
)
_SCAFFOLD_DESCRIPTION_E2 = (
    "## Goal\nE2 canonical scaffold.\n\n"
    "## Files / Paths\n"
    "- docs-site/mkdocs.yml (NEW)\n"
    "- docs-site/Makefile (NEW)\n"
)


def test_op_800_second_ticket_with_overlapping_paths_is_blocked_until_first_ps_merges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #1: synthetic Sprint E replay — E1 has open PS, E2 must mutex-block.

    Once E1's PS merges (i.e. it leaves the open-bot-owned set), E2 unblocks.
    """
    e1_open: dict[str, list[jd.GerritFileOwner]] = {
        "docs-site/mkdocs.yml": [jd.GerritFileOwner("264", "claude-bot")],
        "docs-site/requirements.txt": [jd.GerritFileOwner("264", "claude-bot")],
        "docs-site/docs/index.md": [jd.GerritFileOwner("264", "claude-bot")],
    }
    monkeypatch.setattr(jd, "_open_bot_owned_file_owners", lambda: e1_open)

    e2 = _snapshot(key="OP-786", labels=())
    ok, reason = jd.file_mutex_check(e2, description=_SCAFFOLD_DESCRIPTION_E2)
    assert ok is False
    assert "docs-site/mkdocs.yml" in reason
    assert "#264" in reason

    # E1 merges → empty open-bot set → E2 unblocks.
    monkeypatch.setattr(jd, "_open_bot_owned_file_owners", lambda: {})
    ok, reason = jd.file_mutex_check(e2, description=_SCAFFOLD_DESCRIPTION_E2)
    assert ok is True
    assert "no collision" in reason


def test_op_800_non_overlapping_paths_do_not_false_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #2: two tickets with disjoint Files / Paths sections both stay pickable."""
    other_open: dict[str, list[jd.GerritFileOwner]] = {
        "backend/agents/scheduler.py": [jd.GerritFileOwner("301", "codex-bot")],
        "backend/tests/test_scheduler.py": [jd.GerritFileOwner("301", "codex-bot")],
    }
    monkeypatch.setattr(jd, "_open_bot_owned_file_owners", lambda: other_open)

    candidate = _snapshot(key="OP-810", labels=())
    description = (
        "## Goal\nUnrelated work.\n\n"
        "## Files / Paths\n"
        "- backend/agents/cost_estimator.py\n"
        "- backend/tests/test_cost_estimator.py\n"
    )
    ok, reason = jd.file_mutex_check(candidate, description=description)
    assert ok is True
    assert "no collision" in reason


def test_op_800_collision_reason_cites_change_number_and_pattern_12_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #3: collision reason carries the blocker's Gerrit change # and Pattern 12 link.

    The runner copies the reason into a ``[runner-file-mutex]`` JIRA comment,
    so this also covers the operator-facing resolution hint requirement.
    """
    monkeypatch.setattr(
        jd,
        "_open_bot_owned_file_owners",
        lambda: {"docs-site/mkdocs.yml": [jd.GerritFileOwner("264", "claude-bot")]},
    )
    ok, reason = jd.file_mutex_check(
        _snapshot(key="OP-786", labels=()),
        description=_SCAFFOLD_DESCRIPTION_E2,
    )
    assert ok is False
    # Gerrit change # (acceptance criterion: cite blocker's change number).
    assert "#264" in reason
    # Pattern 12 cookbook link (acceptance criterion: cite Pattern 12 link).
    assert jd.PATTERN_12_COOKBOOK_LINK in reason
    assert "Pattern 12" in reason
    # Three resolution paths from the spec, in order: merge / rebase / re-scope.
    assert "merge PS #264 first" in reason
    assert "rebase the conflicting PS" in reason
    assert "re-scope" in reason
    # Operator override hint is mentioned so the operator knows the escape hatch.
    assert jd.FILE_OVERLAP_OVERRIDE_LABEL in reason


def test_op_800_override_label_bypasses_file_overlap_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #5: ``runner-mutex-override:file-overlap`` label unblocks pickup.

    The hand-merge plan is the operator's responsibility once they apply the
    label, so the gate yields and the pickup proceeds — but the reason still
    names the bypassed PS so the operator-facing log captures the override.
    """
    monkeypatch.setattr(
        jd,
        "_open_bot_owned_file_owners",
        lambda: {"docs-site/mkdocs.yml": [jd.GerritFileOwner("264", "claude-bot")]},
    )
    overridden = _snapshot(key="OP-786", labels=(jd.FILE_OVERLAP_OVERRIDE_LABEL,))
    ok, reason = jd.file_mutex_check(overridden, description=_SCAFFOLD_DESCRIPTION_E2)
    assert ok is True
    assert "file-overlap override" in reason
    assert "#264" in reason
    assert "docs-site/mkdocs.yml" in reason

    # Without the label, identical inputs must block — sanity check that the
    # override is the *only* thing letting this through.
    not_overridden = _snapshot(key="OP-786", labels=())
    ok2, _ = jd.file_mutex_check(not_overridden, description=_SCAFFOLD_DESCRIPTION_E2)
    assert ok2 is False


def test_op_800_files_section_parser_is_re_exported_from_scope_to_paths() -> None:
    """The Files / Paths parser is owned by ``scope_to_paths`` per OP-800 spec."""
    from backend.agents import scope_to_paths

    parsed = scope_to_paths.parse_files_section(
        "## Files / Paths\n- backend/agents/jira_dispatch.py (MODIFY)\n"
        "- backend/tests/test_file_mutex.py (MODIFY)\n"
    )
    assert parsed == {
        "backend/agents/jira_dispatch.py",
        "backend/tests/test_file_mutex.py",
    }
    # Risk mitigation: bare globs without a literal `.` in the filename are
    # rejected, so a sloppy ``backend/**/*.py`` line cannot accidentally
    # mutex-block every other ticket.
    assert scope_to_paths.parse_files_section(
        "## Files / Paths\n- backend/**\n- backend/agents/**\n"
    ) == set()


def test_synthetic_pickup_skips_colliding_ticket_and_picks_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    candidates = [_snapshot("OP-A"), _snapshot("OP-B")]
    calls: dict[str, list] = {"labels": [], "comments": []}

    monkeypatch.setenv(mod.PRE_PICKUP_CAP_GATE_ENV, "off")
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


def test_dependency_blocked_candidate_gets_waiting_label_and_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls: dict[str, list] = {"labels": [], "comments": []}
    snapshot = _snapshot("OP-B")

    monkeypatch.setenv(mod.PRE_PICKUP_CAP_GATE_ENV, "off")
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "pre_pickup_ok",
        lambda c, s: (False, "blocked-by:OP-A blocked by OP-A (state=Under Review)"),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_label",
        lambda c, k, label: calls["labels"].append((k, label)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda c, k, text, idem_key=None: calls["comments"].append((k, text)),
    )

    assert mod._check_pre_pickup_candidate(_client(), snapshot) is False

    assert calls["labels"] == [("OP-B", "runner-blocked:waiting-OP-A")]
    assert len(calls["comments"]) == 1
    assert calls["comments"][0][0] == "OP-B"
    assert "[runner-dependency-blocked]" in calls["comments"][0][1]
    assert "blocked-by:OP-A" in calls["comments"][0][1]


def test_dependency_waiting_label_removed_after_blocker_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    removed: list[tuple[str, str]] = []
    comments: list[tuple[str, str]] = []
    snapshot = _snapshot("OP-B", labels=("runner-blocked:waiting-OP-A", "scope:runner-pipeline"))

    monkeypatch.setenv(mod.PRE_PICKUP_CAP_GATE_ENV, "off")
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(mod.jira_dispatch, "pre_pickup_ok", lambda c, s: (True, "pre-pickup checks passed"))
    monkeypatch.setattr(mod.jira_dispatch, "fetch_description", lambda c, k: "## Goal\n")
    monkeypatch.setattr(mod.jira_dispatch, "file_mutex_check", lambda s, description=None: (True, "no collision"))
    monkeypatch.setattr(
        mod.jira_dispatch,
        "remove_label",
        lambda c, k, label: removed.append((k, label)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda c, k, text, idem_key=None: comments.append((k, text)),
    )

    assert mod._check_pre_pickup_candidate(_client(), snapshot) is True

    assert removed == [
        ("OP-B", "runner-blocked:waiting-OP-A"),
        ("OP-B", jd.FILE_COLLISION_SKIP_LABEL),
    ]
    # OP-955 AC#3: clearing markers must emit exactly one "unblocked" comment.
    assert len(comments) == 1
    assert comments[0][0] == "OP-B"
    assert "[runner-dependency-unblocked]" in comments[0][1]


def test_runner_main_all_candidates_blocked_by_file_mutex_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_jira_runner()
    snapshots = [_snapshot("OP-A"), _snapshot("OP-B")]

    monkeypatch.setenv(mod.PRE_PICKUP_CAP_GATE_ENV, "off")
    monkeypatch.setattr(mod, "TARGET_OVERRIDE", "")
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "make_client",
        lambda cls, instance_id=None: _client(),
    )
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
