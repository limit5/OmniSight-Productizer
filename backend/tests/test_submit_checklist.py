"""B6 submit-review checklist tests (OP-835).

Nine cases per the test plan in
``docs/audit/2026-05-11-sprint-abc-master-plan.md`` §2.8:

1. feature-list JSON parses cleanly
2. malformed JSON → ``FeatureListParseError`` (caller maps to legacy)
3. legacy freeform up-converts each bullet to FL<n>
4. legacy with no bullets → ``LegacyUpConvertError``
5. all-passed responses → submit unlocked in one cycle
6. one fail then retry-pass → submit unlocked on cycle 2
7. three failing cycles → ``ChecklistThreeCycleFail``
8. ambiguous ``passed: "mostly"`` → ``ChecklistAmbiguousResponse``
9. staging guard: untracked file → ``FeatureListNotStaged`` /
   staged file passes
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.agents.feature_list_parser import (
    FeatureListParseError,
    LegacyUpConvertError,
    detect_and_parse,
)
from backend.agents.submit_checklist import (
    ChecklistAmbiguousResponse,
    ChecklistThreeCycleFail,
    FeatureListNotStaged,
    MAX_CYCLES,
    assert_feature_list_staged,
    build_checklist,
    evaluate_responses,
    run_checklist_cycle,
)


# ── Fixtures ─────────────────────────────────────────────────────────


FEATURE_LIST_DESCRIPTION = """## Goal
Wire X to Y.

## Feature list (JSON)

```json
[
  {"id": "FL1", "description": "X registered", "verify": "test_x_registered passes"},
  {"id": "FL2", "description": "Y routed", "verify": "manual:operator-check"}
]
```

## Error catalog
- `x_register_conflict`
"""


LEGACY_DESCRIPTION = """## Goal
Do the thing.

## Acceptance criteria
- [ ] First bullet
- Second bullet without checkbox
* Third bullet with asterisk
1. Fourth bullet numbered

## Notes
Trailing section should not absorb bullets.
"""


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Initialise a throwaway git repo so the staging guard can shell out."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "test"],
        check=True,
    )
    return tmp_path


# ── 1. feature-list valid ────────────────────────────────────────────


def test_feature_list_json_parses_cleanly():
    detection = detect_and_parse(FEATURE_LIST_DESCRIPTION)
    assert detection.mode == "feature_list_json"
    assert [it.id for it in detection.items] == ["FL1", "FL2"]
    assert detection.items[0].description == "X registered"
    assert detection.items[0].verify == "test_x_registered passes"
    assert detection.items[1].verify == "manual:operator-check"

    payload = build_checklist(FEATURE_LIST_DESCRIPTION)
    assert payload.mode == "feature_list_json"
    assert "FL1" in payload.message and "FL2" in payload.message
    assert "strict JSON boolean" in payload.message  # rejection hint visible


# ── 2. malformed JSON ────────────────────────────────────────────────


def test_malformed_feature_list_json_raises_format_unparseable():
    malformed = """## Feature list (JSON)

```json
[ {"id": "FL1", "description": "x"  // trailing comment, invalid JSON
```
"""
    with pytest.raises(FeatureListParseError):
        detect_and_parse(malformed)


def test_malformed_json_falls_back_to_legacy_via_build_checklist():
    """B6 AC #1: malformed JSON falls back to legacy mode (no raise)."""
    mixed = """## Feature list (JSON)

```json
not really json {{{
```

## Acceptance criteria
- Real legacy bullet survives
"""
    payload = build_checklist(mixed)
    assert payload.mode == "legacy_freeform"
    assert [it.id for it in payload.items] == ["FL1"]
    assert payload.items[0].description == "Real legacy bullet survives"


# ── 3. legacy freeform conversion ────────────────────────────────────


def test_legacy_freeform_up_converts_each_bullet():
    detection = detect_and_parse(LEGACY_DESCRIPTION)
    assert detection.mode == "legacy_freeform"
    assert [it.id for it in detection.items] == ["FL1", "FL2", "FL3", "FL4"]
    assert detection.items[0].description == "First bullet"
    # Checkbox marker stripped from the first bullet's body.
    assert "[" not in detection.items[0].description
    # All up-converted items default verify to manual:operator-check.
    assert all(it.verify == "manual:operator-check" for it in detection.items)


# ── 4. empty legacy conversion escalates ─────────────────────────────


def test_empty_legacy_up_convert_raises_escalation():
    no_bullets = """## Goal
nothing.

## Acceptance criteria

Just a paragraph with no list items.

## Notes
later section
"""
    with pytest.raises(LegacyUpConvertError):
        detect_and_parse(no_bullets)


# ── 5. all passed → submit ───────────────────────────────────────────


def test_all_passed_flow_unlocks_submit_in_one_cycle():
    payload = build_checklist(FEATURE_LIST_DESCRIPTION)

    def responder(items, cycle):
        assert cycle == 0  # only one cycle should run
        return [
            {"id": it.id, "passed": True, "evidence": "covered by suite"}
            for it in items
        ]

    decision = run_checklist_cycle("OP-TEST", payload.items, model_responder=responder)
    assert decision.all_passed
    assert decision.failures == ()
    assert [r.evidence for r in decision.responses] == ["covered by suite"] * 2


# ── 6. one-fail then retry-pass ──────────────────────────────────────


def test_one_fail_then_retry_passes():
    payload = build_checklist(FEATURE_LIST_DESCRIPTION)
    calls: list[int] = []
    re_edits: list[int] = []

    def responder(items, cycle):
        calls.append(cycle)
        if cycle == 0:
            return [
                {"id": "FL1", "passed": True, "evidence": "ok"},
                {"id": "FL2", "passed": False, "evidence": "UI missing"},
            ]
        return [
            {"id": "FL1", "passed": True, "evidence": "ok"},
            {"id": "FL2", "passed": True, "evidence": "added affordance"},
        ]

    def re_edit_hook(failures, cycle):
        re_edits.append(cycle)
        assert [f.id for f in failures] == ["FL2"]

    decision = run_checklist_cycle(
        "OP-TEST",
        payload.items,
        model_responder=responder,
        re_edit_hook=re_edit_hook,
    )
    assert decision.all_passed
    assert calls == [0, 1]
    # Hook fires between cycle 0 and cycle 1 — exactly once.
    assert re_edits == [0]


# ── 7. 3-cycle escalate ──────────────────────────────────────────────


def test_three_cycle_escalate_raises():
    payload = build_checklist(FEATURE_LIST_DESCRIPTION)
    cycles_seen: list[int] = []

    def responder(items, cycle):
        cycles_seen.append(cycle)
        return [
            {"id": "FL1", "passed": False, "evidence": "still broken"},
            {"id": "FL2", "passed": False, "evidence": "still broken"},
        ]

    with pytest.raises(ChecklistThreeCycleFail) as exc_info:
        run_checklist_cycle(
            "OP-TEST",
            payload.items,
            model_responder=responder,
        )
    assert cycles_seen == list(range(MAX_CYCLES))
    assert {f.id for f in exc_info.value.last_failures} == {"FL1", "FL2"}


# ── 8. ambiguous response ────────────────────────────────────────────


def test_ambiguous_response_rejected_as_non_binary():
    payload = build_checklist(FEATURE_LIST_DESCRIPTION)
    ambiguous = [
        {"id": "FL1", "passed": "mostly", "evidence": "hand-wavy"},
        {"id": "FL2", "passed": True, "evidence": "real"},
    ]
    with pytest.raises(ChecklistAmbiguousResponse) as exc_info:
        evaluate_responses(payload.items, ambiguous)
    assert exc_info.value.item_id == "FL1"
    assert exc_info.value.raw_value == "mostly"


def test_ambiguous_response_via_run_checklist_propagates():
    """The cycle runner does not swallow ambiguity — the model must retry."""
    payload = build_checklist(FEATURE_LIST_DESCRIPTION)

    def responder(items, cycle):
        return [
            {"id": "FL1", "passed": 1, "evidence": "truthy-int not bool"},
            {"id": "FL2", "passed": True, "evidence": "ok"},
        ]

    with pytest.raises(ChecklistAmbiguousResponse):
        run_checklist_cycle("OP-TEST", payload.items, model_responder=responder)


# ── 9. staging guard ────────────────────────────────────────────────


def test_staging_guard_rejects_untracked_feature_list(tmp_git_repo: Path):
    artifact = tmp_git_repo / "feature_list.json"
    artifact.write_text('[{"id": "FL1", "description": "x", "verify": "y"}]\n')
    # Not staged — porcelain should report ??.
    with pytest.raises(FeatureListNotStaged) as exc_info:
        assert_feature_list_staged(tmp_git_repo, "feature_list.json")
    assert exc_info.value.status_line.startswith("??")


def test_staging_guard_passes_when_staged(tmp_git_repo: Path):
    artifact = tmp_git_repo / "feature_list.json"
    artifact.write_text('[{"id": "FL1", "description": "x", "verify": "y"}]\n')
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "add", "feature_list.json"],
        check=True,
    )
    # No exception → staging contract satisfied.
    assert_feature_list_staged(tmp_git_repo, "feature_list.json")


def test_staging_guard_rejects_unstaged_modification(tmp_git_repo: Path):
    """Staging an old version and then editing breaks the contract.

    Porcelain ``AM`` means "added to index, modified in worktree" — the
    commit will use the indexed (stale) blob, so this MUST raise.
    """
    artifact = tmp_git_repo / "feature_list.json"
    artifact.write_text('[{"id": "FL1", "description": "x", "verify": "y"}]\n')
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "add", "feature_list.json"],
        check=True,
    )
    artifact.write_text('[{"id": "FL1", "description": "x-edited", "verify": "y"}]\n')
    with pytest.raises(FeatureListNotStaged) as exc_info:
        assert_feature_list_staged(tmp_git_repo, "feature_list.json")
    assert exc_info.value.status_line[1] != " "
