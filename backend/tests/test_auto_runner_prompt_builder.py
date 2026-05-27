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
        "ci", "gerrit",
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
    # Forbidden block must list the other recognised areas — never
    # the declared one, never the unknown literal.
    for forbidden in (
        "frontend", "devops", "tests", "db", "docs", "security",
        "embedded", "tooling", "ci", "gerrit",
    ):
        assert f"- {forbidden}" in prompt
    assert "- backend" not in prompt.split("Stay strictly within")[1].split("If you find")[0]


def test_ci_db_ticket_builds_runner_prompt(runner, fake_client, monkeypatch):
    """OP-1559: area:ci + area:db labels are runner-recognised together."""
    _patch_issue(monkeypatch, labels=["area:ci", "area:db", "tier:M"])

    prompt = runner._build_prompt(fake_client, "OP-1559-repro", "test ticket body")

    assert "OP-1559-repro" in prompt
    assert "Areas: ci, db" in prompt
    assert "- ci" not in prompt.split("Stay strictly within")[1].split("If you find")[0]
    assert "- db" not in prompt.split("Stay strictly within")[1].split("If you find")[0]


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


# ── OP-1680 (F7): Bearer auth on the project-state fetch ──
#
# The fetch hits an auth-gated endpoint. These pin AC #1 + #3: when
# OMNISIGHT_RUNNER_API_TOKEN is set the urllib Request carries
# `Authorization: Bearer <token>`; when unset no auth header is added and
# the call still issues; and the token value is never written to the log.


def _capture_fetch_request(monkeypatch):
    """Patch urlopen to capture the urllib Request and return a stub 200.

    Returns the mutable list the captured Request lands in so a test can
    assert on its headers after driving the real `_fetch_project_state`.
    """
    import urllib.request

    captured: list = []

    class _FakeResp:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, *args, **kwargs):  # noqa: ARG001
        captured.append(req)
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return captured


def test_fetch_project_state_sends_bearer_when_token_set(runner, monkeypatch):
    """AC #1 — token in env → request carries `Authorization: Bearer <token>`."""
    monkeypatch.setenv(runner.PROJECT_STATE_API_TOKEN_ENV, "s3cret-key")
    captured = _capture_fetch_request(monkeypatch)

    result = runner._fetch_project_state("OP-1234")

    assert result == {}, "stub 200 payload must parse"
    assert len(captured) == 1, "call must still issue exactly once"
    # urllib capitalises header keys via add_header(); "Authorization" is
    # already in that form so get_header round-trips.
    assert captured[0].get_header("Authorization") == "Bearer s3cret-key"


def test_fetch_project_state_omits_auth_when_token_unset(runner, monkeypatch):
    """AC #1 — no token → no auth header, but the call still issues."""
    monkeypatch.delenv(runner.PROJECT_STATE_API_TOKEN_ENV, raising=False)
    captured = _capture_fetch_request(monkeypatch)

    result = runner._fetch_project_state("OP-1234")

    assert result == {}
    assert len(captured) == 1, "behaviour unchanged: call still issues when unset"
    assert captured[0].get_header("Authorization") is None
    # Accept header is preserved either way.
    assert captured[0].get_header("Accept") == "application/json"


def test_fetch_project_state_never_logs_token_value(runner, monkeypatch, capsys):
    """AC #3 — fetch_failed log must print key + exc only, never the token."""
    monkeypatch.setenv(runner.PROJECT_STATE_API_TOKEN_ENV, "super-secret-token")

    import urllib.request

    def _urlopen_boom(req, *args, **kwargs):  # noqa: ARG001
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_boom)
    result = runner._fetch_project_state("OP-1234")

    assert result is None
    captured = capsys.readouterr()
    assert "fetch_failed" in captured.err
    assert "super-secret-token" not in captured.err
    assert "super-secret-token" not in captured.out


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


# ── AUDIT-29b-6 (OP-1024): lesson-surface meta-mechanism in _build_prompt ──
#
# _build_prompt grows two flag-gated blocks: "Relevant lessons" (cognee_recall)
# and "Anti-patterns matching this ticket" (antipattern_inject). Both must:
#   - appear only when the flag is on,
#   - sit before the AC-verification section,
#   - degrade to nothing (never raise) when retrieval fails,
#   - emit a `[runner] *.surfaced` debug log line when they fire.

from backend.agents import cognee_integration as _ci  # noqa: E402
from backend.agents.lesson_retrieval import LessonSearchResult  # noqa: E402

_AC_MARKER = "# Acceptance Criteria verification"
_LESSON_HEADER = "# Relevant lessons (AUDIT-29b lesson-surface)"
_ANTIPATTERN_HEADER = "# Anti-patterns matching this ticket (AUDIT-29b lesson-surface)"


def test_lesson_recall_block_present_when_flag_on(
    runner, fake_client, monkeypatch, capsys
):
    """cognee_recall on → "Relevant lessons" block, positioned before the AC marker."""
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M"], summary="runner identity")
    monkeypatch.setenv("OMNISIGHT_COGNEE_RECALL", "1")
    fake_lessons = (
        LessonSearchResult(
            path=Path("docs/sop/lessons/L-OP-729-runner-bot-identity-must-be-worktree-local.md"),
            text="**Situation**: shared identity. **Fix**: worktree-local bot identity.",
            score=2.0,
        ),
    )
    monkeypatch.setattr(_ci, "retrieve_lessons_via_cognee", lambda *a, **kw: fake_lessons)
    prompt = runner._build_prompt(fake_client, "OP-1024-lr", "synthetic ticket about runner identity")
    assert _LESSON_HEADER in prompt
    assert "L-OP-729-runner-bot-identity-must-be-worktree-local.md" in prompt
    assert "worktree-local bot identity" in prompt
    assert _AC_MARKER in prompt
    assert prompt.index(_LESSON_HEADER) < prompt.index(_AC_MARKER)
    assert "lesson_recall.surfaced" in capsys.readouterr().err


def test_lesson_recall_block_absent_when_flag_off(runner, fake_client, monkeypatch):
    """cognee_recall default-off → no block, retriever never invoked."""
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M"])
    monkeypatch.delenv("OMNISIGHT_COGNEE_RECALL", raising=False)

    calls: list[tuple] = []

    def _should_not_run(*a, **kw):  # noqa: ANN002, ANN003
        calls.append((a, kw))
        return ()

    monkeypatch.setattr(_ci, "retrieve_lessons_via_cognee", _should_not_run)
    prompt = runner._build_prompt(fake_client, "OP-1024-off", "body")
    assert _LESSON_HEADER not in prompt
    assert calls == [], "retriever must short-circuit on flag-off"


def test_lesson_recall_retrieval_error_degrades_to_empty(runner, fake_client, monkeypatch):
    """A retriever exception degrades to an empty block — the pickup still builds."""
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M"])
    monkeypatch.setenv("OMNISIGHT_COGNEE_RECALL", "1")

    def _boom(*a, **kw):  # noqa: ANN002, ANN003
        raise RuntimeError("kg exploded")

    monkeypatch.setattr(_ci, "retrieve_lessons_via_cognee", _boom)
    prompt = runner._build_prompt(fake_client, "OP-1024-err", "body still here")
    assert _LESSON_HEADER not in prompt
    assert "OP-1024-err" in prompt
    assert "body still here" in prompt


def test_antipattern_block_auto_injected_on_area_match(
    runner, fake_client, monkeypatch, capsys
):
    """antipattern_inject on + area:db → cookbook pattern surfaced with an "area match" tag.

    Cognee is not installed in the test env, so this exercises the
    deterministic keyword-overlap fallback path + the area-domain bias over
    the real docs/sop/architecture-anti-patterns.md.
    """
    _patch_issue(monkeypatch, labels=["area:db", "tier:M"], summary="migration ticket scope")
    monkeypatch.setenv("OMNISIGHT_ANTIPATTERN_INJECT", "1")
    prompt = runner._build_prompt(
        fake_client, "OP-1024-ap", "synthetic migration ticket touching alembic schema"
    )
    assert _ANTIPATTERN_HEADER in prompt
    assert "area match" in prompt
    assert prompt.index(_ANTIPATTERN_HEADER) < prompt.index(_AC_MARKER)
    err = capsys.readouterr().err
    assert "antipattern_inject.surfaced" in err
    # The surfaced pattern must be one tagged with the area:db domain.
    assert "area match: " in prompt


def test_antipattern_block_absent_when_flag_off(runner, fake_client, monkeypatch):
    _patch_issue(monkeypatch, labels=["area:db", "tier:M"])
    monkeypatch.delenv("OMNISIGHT_ANTIPATTERN_INJECT", raising=False)
    prompt = runner._build_prompt(fake_client, "OP-1024-apoff", "body")
    assert _ANTIPATTERN_HEADER not in prompt


def test_both_lesson_blocks_off_by_default(runner, fake_client, monkeypatch):
    """Default config: neither block appears (zero-impact rollout)."""
    _patch_issue(monkeypatch, labels=["area:backend", "tier:M"])
    monkeypatch.delenv("OMNISIGHT_COGNEE_RECALL", raising=False)
    monkeypatch.delenv("OMNISIGHT_ANTIPATTERN_INJECT", raising=False)
    prompt = runner._build_prompt(fake_client, "OP-1024-default", "body")
    assert _LESSON_HEADER not in prompt
    assert _ANTIPATTERN_HEADER not in prompt


# ── OP-1780 (1A.3): strip OmniSight context from customer-tenant prompts ──
#
# Lessons / anti-patterns / CLAUDE.md L1 doc-rules read OmniSight's own
# docs/sop/* SOP corpus regardless of tenant. For a non-`omnisight-self`
# tenant the assembled prompt must contain NONE of them (closes L8-prompt +
# the Wire-1 info-disclosure); for the internal tenant the prompt is
# unchanged. The bound tenant comes from db_context (OP-1778 set_tenant_id)
# with a fallback to the ticket's own `tenant:<tid>` label.

from backend import db_context  # noqa: E402
from backend.agents import runner_tenant  # noqa: E402

_DOCRULES_HEADER = "# Documentation rules (per CLAUDE.md L1"
# Strings that only ever appear in OmniSight's own SOP / process context.
_OMNISIGHT_SOP_STRINGS = (
    _LESSON_HEADER,
    _ANTIPATTERN_HEADER,
    _DOCRULES_HEADER,
    "HANDOFF.md",
    "docs/sop/lessons-learned.md",
)


def _enable_lesson_and_antipattern_flags(monkeypatch):
    """Turn ON both OmniSight-SOP injection flags so the strip is observable.

    With the flags off the blocks are absent anyway; the customer-strip
    assertion is only meaningful when the blocks WOULD otherwise be emitted.
    A fake lesson retriever supplies a deterministic, OmniSight-flavoured
    lesson so the self-tenant control case actually renders the block.
    """
    monkeypatch.setenv("OMNISIGHT_COGNEE_RECALL", "1")
    monkeypatch.setenv("OMNISIGHT_ANTIPATTERN_INJECT", "1")
    fake_lessons = (
        LessonSearchResult(
            path=Path("docs/sop/lessons/L-OP-729-runner-bot-identity.md"),
            text="**Situation**: OmniSight SOP lesson body. **Fix**: do the thing.",
            score=2.0,
        ),
    )
    monkeypatch.setattr(_ci, "retrieve_lessons_via_cognee", lambda *a, **kw: fake_lessons)


def test_customer_tenant_prompt_is_free_of_omnisight_sop(
    runner, fake_client, monkeypatch
):
    """A `tenant:<tid>` ticket gets NONE of OmniSight's lessons / anti-patterns
    / CLAUDE.md doc-rules — even with both injection flags ON."""
    _patch_issue(
        monkeypatch,
        labels=["area:db", "tier:M", "tenant:t-acme"],
        summary="customer migration ticket scope",
    )
    _enable_lesson_and_antipattern_flags(monkeypatch)
    # No tenant bound in context → resolution falls back to the label.
    monkeypatch.setattr(db_context, "current_tenant_id", lambda: None)

    prompt = runner._build_prompt(
        fake_client, "OP-1780-cust", "synthetic migration ticket touching alembic schema"
    )

    for needle in _OMNISIGHT_SOP_STRINGS:
        assert needle not in prompt, f"OmniSight SOP leaked to customer prompt: {needle!r}"
    # The prompt must still build with the runner scaffolding intact.
    assert "OP-1780-cust" in prompt
    assert "# Acceptance Criteria verification" in prompt


def test_customer_tenant_via_bound_context_strips_sop(
    runner, fake_client, monkeypatch
):
    """The strip is driven by the bound DB/FS tenant context (OP-1778),
    not only by the label — a context-bound customer with no label still
    strips."""
    _patch_issue(monkeypatch, labels=["area:db", "tier:M"], summary="ctx-bound customer")
    _enable_lesson_and_antipattern_flags(monkeypatch)
    monkeypatch.setattr(db_context, "current_tenant_id", lambda: "t-bravo")

    prompt = runner._build_prompt(fake_client, "OP-1780-ctx", "body")

    for needle in _OMNISIGHT_SOP_STRINGS:
        assert needle not in prompt, f"context-bound customer leaked: {needle!r}"


def test_self_tenant_prompt_retains_omnisight_sop(
    runner, fake_client, monkeypatch, capsys
):
    """Back-compat: an `omnisight-self` (label-less / internal) ticket is
    UNCHANGED — it still receives lessons + anti-patterns + CLAUDE.md
    doc-rules."""
    _patch_issue(
        monkeypatch,
        labels=["area:db", "tier:M"],
        summary="internal migration ticket scope",
    )
    _enable_lesson_and_antipattern_flags(monkeypatch)
    monkeypatch.setattr(db_context, "current_tenant_id", lambda: None)

    prompt = runner._build_prompt(
        fake_client, "OP-1780-self", "synthetic migration ticket touching alembic schema"
    )

    # All three OmniSight context blocks present for the internal tenant.
    assert _LESSON_HEADER in prompt
    assert _ANTIPATTERN_HEADER in prompt
    assert _DOCRULES_HEADER in prompt
    assert "docs/sop/lessons-learned.md" in prompt
    # No strip log line on the self path.
    assert "tenant_context_strip" not in capsys.readouterr().err


def test_explicit_self_tenant_label_retains_sop(runner, fake_client, monkeypatch):
    """An explicit `tenant:omnisight-self` label resolves to the internal
    tenant and keeps the OmniSight context (mirrors runner_tenant)."""
    _patch_issue(
        monkeypatch,
        labels=["area:db", "tier:M", f"tenant:{runner_tenant.OMNISIGHT_SELF_TENANT}"],
        summary="explicit self ticket",
    )
    _enable_lesson_and_antipattern_flags(monkeypatch)
    monkeypatch.setattr(db_context, "current_tenant_id", lambda: None)

    prompt = runner._build_prompt(fake_client, "OP-1780-self2", "body")

    assert _DOCRULES_HEADER in prompt
    assert _LESSON_HEADER in prompt
