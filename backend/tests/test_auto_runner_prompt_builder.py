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


# ── OP-986: localized JIRA issuetype name still resolves the full matrix ──


def test_japanese_issuetype_resolves_full_capabilities(runner, fake_client, monkeypatch):
    """OP-986 regression: when JIRA is in the Japanese display locale it
    returns ``issuetype.name='ストーリー'``. The runner must still resolve the
    full tier-M capability set for the ticket — including ``gerrit_push`` —
    not the read-only safe-default (which silently blocked auto-push on every
    Story-typed pickup, OP-980/981/985 incident 2026-05-12).
    """
    payload = {
        "fields": {
            "summary": "AUDIT-26 child",
            "labels": ["area:backend", "tier:M"],
            "components": [{"name": "HIGH"}],
            "issuetype": {"name": "ストーリー", "id": "10001"},
        }
    }
    monkeypatch.setattr(jira_dispatch, "_request", lambda *a, **kw: payload)

    prompt = runner._build_prompt(fake_client, "OP-986-repro", "stub body")

    caps = runner._LAST_RESOLVED_CAPABILITIES["OP-986-repro"]
    assert "gerrit_push" in caps
    assert {
        "code_edit", "run_tests", "run_lint",
        "jira_update", "mcp_search", "memory_recall",
    }.issubset(caps)
    # Crucially NOT the read-only-only fallback.
    assert caps != runner._load_capability_matrix().read_only_default
    # The prompt's "Enabled capabilities" block must reflect it.
    caps_block = prompt.split("Enabled capabilities", 1)[1].split("# Documentation rules", 1)[0]
    assert "gerrit_push" in caps_block
    # The ticket-type echoed into the metadata side channel is the raw
    # localized name (the runner doesn't rewrite it — only the matrix lookup
    # normalizes), so downstream metrics see what JIRA actually returned.
    assert runner._LAST_TICKET_METADATA["OP-986-repro"]["ticket_type"] == "ストーリー"


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


# ── OP-905 (F7): project-state injection at pickup ──
#
# Four cases pinned by the AC test-plan: flag-on injects, flag-off omits,
# API timeout proceeds without injection, malformed response proceeds
# without injection. A fifth case covers the operator override label.


_PROJECT_CONTEXT_HEADER = "# Project context"


@pytest.fixture
def fake_project_state_payload():
    """Realistic-shaped aggregator payload used by injection tests."""
    return {
        "ticket": "OP-1234",
        "develop_sha": "deadbeef",
        "structural": {"blockers": ["OP-1"], "parent": "OP-META"},
        "temporal": {"recent_failures": []},
        "causal": None,
        "generated_at": "2026-05-11T00:00:00+00:00",
    }


def test_project_state_inject_flag_on_emits_block(
    runner, fake_client, monkeypatch, fake_project_state_payload
):
    """AC #1 + #2 + #6 — flag enabled → prompt contains the `# Project context` block."""
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M"])
    monkeypatch.setenv("OMNISIGHT_PROJECT_STATE_INJECT", "1")
    monkeypatch.setattr(
        runner, "_fetch_project_state", lambda key: fake_project_state_payload,
    )
    prompt = runner._build_prompt(fake_client, "OP-1234", "stub body")
    assert _PROJECT_CONTEXT_HEADER in prompt
    # AC #2 — block must appear before the AC verification section so the
    # downstream CLI sees context before it is told to satisfy the AC.
    ac_marker = "# Acceptance Criteria verification"
    assert ac_marker in prompt
    assert prompt.index(_PROJECT_CONTEXT_HEADER) < prompt.index(ac_marker)
    # The payload itself must be embedded so the CLI can act on it.
    assert "OP-META" in prompt
    assert "deadbeef" in prompt


def test_project_state_inject_flag_off_omits_block(
    runner, fake_client, monkeypatch, fake_project_state_payload
):
    """AC #4 — flag default-off → block absent, fetcher never invoked."""
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M"])
    monkeypatch.delenv("OMNISIGHT_PROJECT_STATE_INJECT", raising=False)

    calls: list[str] = []

    def _should_not_be_called(key: str):  # noqa: ARG001
        calls.append(key)
        return fake_project_state_payload

    monkeypatch.setattr(runner, "_fetch_project_state", _should_not_be_called)
    prompt = runner._build_prompt(fake_client, "OP-1234", "stub body")
    assert _PROJECT_CONTEXT_HEADER not in prompt
    assert calls == [], "fetcher must short-circuit on flag-off"


def test_project_state_inject_api_timeout_proceeds_without_block(
    runner, fake_client, monkeypatch
):
    """AC #3 + error catalog — timeout degrades to empty block + log, prompt still builds."""
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M"])
    monkeypatch.setenv("OMNISIGHT_PROJECT_STATE_INJECT", "1")

    def _raise_timeout(key: str):  # noqa: ARG001
        raise TimeoutError("simulated 3s budget exceeded")

    # Drive the real _fetch_project_state path so the except branch runs.
    import urllib.request

    def _urlopen_timeout(*args, **kwargs):  # noqa: ARG001
        raise TimeoutError("simulated 3s budget exceeded")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_timeout)
    prompt = runner._build_prompt(fake_client, "OP-1234", "stub body")
    assert _PROJECT_CONTEXT_HEADER not in prompt
    # Prompt must still contain the rest of the structure — the degrade
    # path is "proceed without injection", not "fail the pickup".
    assert "OP-1234" in prompt
    assert "stub body" in prompt


def test_project_state_inject_malformed_response_proceeds_without_block(
    runner, fake_client, monkeypatch
):
    """Error catalog — malformed JSON degrades to empty block + log."""
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M"])
    monkeypatch.setenv("OMNISIGHT_PROJECT_STATE_INJECT", "1")

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen_garbage(*args, **kwargs):  # noqa: ARG001
        return _FakeResp(b"not json at all <<<")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_garbage)
    prompt = runner._build_prompt(fake_client, "OP-1234", "stub body")
    assert _PROJECT_CONTEXT_HEADER not in prompt
    assert "OP-1234" in prompt


def test_project_state_skip_label_disables_injection_per_ticket(
    runner, fake_client, monkeypatch, fake_project_state_payload
):
    """AC #5 — `project-state:skip` label disables injection for this pickup only."""
    _patch_issue(
        monkeypatch,
        labels=["area:backend", "tier:M", runner.PROJECT_STATE_SKIP_LABEL],
    )
    monkeypatch.setenv("OMNISIGHT_PROJECT_STATE_INJECT", "1")

    calls: list[str] = []

    def _should_not_be_called(key: str):  # noqa: ARG001
        calls.append(key)
        return fake_project_state_payload

    monkeypatch.setattr(runner, "_fetch_project_state", _should_not_be_called)
    prompt = runner._build_prompt(fake_client, "OP-1234", "stub body")
    assert _PROJECT_CONTEXT_HEADER not in prompt
    assert calls == [], "label override must short-circuit before any fetch"


def test_build_prompt_reads_cognee_recall_flag_from_env(
    runner, fake_client, monkeypatch
):
    """AUDIT-29b-5 — _build_prompt resolves cognee_recall on every pickup."""
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M"])
    monkeypatch.setenv("OMNISIGHT_COGNEE_RECALL", "1")

    runner._LAST_AGENT_FEATURE_FLAGS.clear()
    runner._build_prompt(fake_client, "OP-1023", "stub body")

    assert runner._LAST_AGENT_FEATURE_FLAGS["OP-1023"] == {
        "cognee_recall": True,
    }
