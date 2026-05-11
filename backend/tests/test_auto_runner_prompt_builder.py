"""OP-832: prompt-builder must reject unknown `area:<X>` labels.

Pre-OP-832 the runner accepted any `area:<X>` label and computed
`forbidden = all_areas - declared`, which silently produced a forbid-all
prompt when `<X>` wasn't in `all_areas` (the trigger for OP-829's
5-iteration self-revert loop). These tests pin the new strict-validation
behaviour and the typed exception so a future regression breaks loudly.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from backend.agents import jira_dispatch


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_MODULE_NAME = "auto_runner_jira"  # must match scripts/jira_seed_example_tickets.py


def _load_runner():
    """Load the hyphen-named `auto-runner-jira.py` once, cached in sys.modules.

    Cache so the seed-script's loader (which uses the same name) reuses this
    instance — keeps `UnknownAreaLabelError` identity consistent across both
    callers.
    """
    cached = sys.modules.get(RUNNER_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        RUNNER_MODULE_NAME, REPO_ROOT / "auto-runner-jira.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[RUNNER_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


@pytest.fixture
def fake_client():
    return jira_dispatch.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


def _patch_issue(monkeypatch, labels: list[str], summary: str = "stub", components=None):
    """Stub jira_dispatch._request so _build_prompt sees the given labels."""
    payload = {
        "fields": {
            "summary": summary,
            "labels": labels,
            "components": components or [],
        }
    }
    monkeypatch.setattr(jira_dispatch, "_request", lambda *a, **kw: payload)


# ── AC #4: exported constant matches the all_areas set used internally ──


def test_recognised_areas_constant_matches_legacy_all_areas(runner):
    """The exported RECOGNISED_AREAS constant is the single source of truth.

    Pre-OP-832 the set was a hardcoded list literal. Drift between the new
    constant and any future re-introduction of a literal would resurrect the
    silent forbid-all bug.
    """
    legacy_all_areas = {
        "backend", "frontend", "devops", "tests", "db",
        "docs", "security", "embedded", "tooling",
    }
    assert runner.RECOGNISED_AREAS == frozenset(legacy_all_areas)
    assert isinstance(runner.RECOGNISED_AREAS, frozenset)


# ── AC #1 happy path: recognised area builds prompt cleanly ──


def test_recognised_area_builds_prompt_without_raising(runner, fake_client, monkeypatch):
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M", "class:subscription-codex"])
    prompt = runner._build_prompt(fake_client, "OP-1000", "stub description body")
    assert "OP-1000" in prompt
    assert "Areas: backend" in prompt
    assert "stub description body" in prompt
    # Forbidden block must list the eight other recognised areas — never
    # the declared one, never the unknown literal.
    for forbidden in ("frontend", "devops", "tests", "db", "docs", "security", "embedded", "tooling"):
        assert f"- {forbidden}" in prompt
    assert "- backend" not in prompt.split("Stay strictly within")[1].split("If you find")[0]


# ── AC #1: unknown area raises typed exception ──


def test_unknown_area_label_raises_typed_exception(runner, fake_client, monkeypatch):
    """`area:runner` (the OP-829 trigger) must raise UnknownAreaLabelError."""
    _patch_issue(monkeypatch, labels=["area:runner", "tier:M"])
    with pytest.raises(runner.UnknownAreaLabelError) as exc_info:
        runner._build_prompt(fake_client, "OP-829-repro", "doesn't matter")
    err = exc_info.value
    assert err.unknown == ["runner"]
    assert set(err.recognised) == set(runner.RECOGNISED_AREAS)
    # Message must list the full recognised set so the operator sees the
    # valid options without grepping the source.
    msg = str(err)
    for area in runner.RECOGNISED_AREAS:
        assert area in msg


# ── AC #1: mixed (one good + one bad) raises with both listed ──


def test_mixed_labels_raise_listing_only_unknowns(runner, fake_client, monkeypatch):
    _patch_issue(
        monkeypatch,
        labels=["area:backend", "area:bogus", "area:also-fake", "tier:S"],
    )
    with pytest.raises(runner.UnknownAreaLabelError) as exc_info:
        runner._build_prompt(fake_client, "OP-mixed", "")
    err = exc_info.value
    assert err.unknown == ["also-fake", "bogus"]
    # The legitimate `backend` declaration must NOT be flagged as unknown.
    assert "backend" not in err.unknown


# ── AC #1 corner: no area declared falls back to legacy <none declared> ──


def test_no_area_label_falls_back_to_none_declared(runner, fake_client, monkeypatch):
    _patch_issue(monkeypatch, labels=["tier:S", "class:subscription-codex"])
    prompt = runner._build_prompt(fake_client, "OP-no-area", "body")
    assert "Areas: <none declared>" in prompt
    # With no declared areas, every recognised area is forbidden.
    for forbidden in runner.RECOGNISED_AREAS:
        assert f"- {forbidden}" in prompt


# ── AC #2: seed-script validation fails fast on unknown labels ──


def test_seed_script_create_issue_rejects_unknown_area(runner, monkeypatch):
    """`scripts/jira_seed_example_tickets.py` must validate before POSTing.

    Operator gets feedback at filing time so the runner never sees a wedge
    ticket. Importing the script via importlib because it lives outside the
    backend package.
    """
    spec = importlib.util.spec_from_file_location(
        "jira_seed_example_tickets",
        REPO_ROOT / "scripts" / "jira_seed_example_tickets.py",
    )
    seed_module = importlib.util.module_from_spec(spec)
    # Loading the seed module reads ~/.config/omnisight/jira-claude.env at
    # import time only via _load_env() which is called lazily — exec is safe.
    spec.loader.exec_module(seed_module)

    # Sanity: validator accepts a recognised label.
    seed_module._validate_area_labels(["area:backend", "tier:M"])

    # Unknown label must raise the same typed exception the runner uses.
    with pytest.raises(runner.UnknownAreaLabelError) as exc_info:
        seed_module._validate_area_labels(["area:bogus", "tier:M"])
    assert exc_info.value.unknown == ["bogus"]
