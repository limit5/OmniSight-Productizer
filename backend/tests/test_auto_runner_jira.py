"""Phase 1.5 idempotency tests for auto-runner-jira.py (OP-691).

Targets the ``_finalize_under_review`` helper extracted from the post-CLI
push block. The three contract scenarios (mirroring OP-691 ACs 5/6/7):

  1. Agent already transitioned the ticket itself → no comment, no
     transition POST, no crash (the OP-690 incident pattern).
  2. Happy path — ticket is In Progress when post-CLI block runs →
     comment posted, transition POST fires, runner reports success.
  3. Transition POST fails with a non-Under-Review HTTP error →
     ``[runner-transition-skip]`` comment is posted instead of raising,
     so the runner exits 0 (Gerrit push already succeeded).

Network-free: the runner is loaded via importlib.spec_from_file_location
and ``jira_dispatch`` is patched at the module-attribute level.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    """Load auto-runner-jira.py despite the hyphen in its filename."""
    sys.modules.pop("jira_runner_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    """Minimal stand-in for jira_dispatch.DispatchClient (only attribute
    access for logging; all real network calls go through the patched
    jira_dispatch module attributes)."""
    agent_class = "subscription-codex"
    bot_email = "rt3628+codex-bot@gmail.com"
    bot_account_id = "acc-codex-bot"


def _patch_dispatch(monkeypatch: pytest.MonkeyPatch, mod: Any, **overrides: Any) -> dict[str, list]:
    """Patch the runner-module-bound ``jira_dispatch`` symbol with stubs.
    Returns a calls log dict the test inspects to assert behaviour."""
    calls: dict[str, list] = {
        "get_issue_status": [],
        "post_runner_pushed_comment": [],
        "transition_to_under_review_if_needed": [],
        "add_comment": [],
    }

    def make_recorder(name: str, return_value: Any = None, raise_with: Exception | None = None):
        def fn(*args, **kwargs):
            calls[name].append((args, kwargs))
            if raise_with is not None:
                raise raise_with
            return return_value
        return fn

    # Default recorders — overridable per-test via **overrides
    fns = {
        "get_issue_status": make_recorder("get_issue_status", return_value="In Progress"),
        "post_runner_pushed_comment": make_recorder("post_runner_pushed_comment"),
        "transition_to_under_review_if_needed": make_recorder(
            "transition_to_under_review_if_needed", return_value=True
        ),
        "add_comment": make_recorder("add_comment"),
    }
    for name, fn in overrides.items():
        # Wrap the override so calls still get logged
        original = fn
        def wrap(orig_name: str, orig_fn):
            def wrapped(*args, **kwargs):
                calls[orig_name].append((args, kwargs))
                return orig_fn(*args, **kwargs)
            return wrapped
        fns[name] = wrap(name, original)

    for name, fn in fns.items():
        monkeypatch.setattr(mod.jira_dispatch, name, fn)

    # Pin the constant so the runner's status-comparison stays stable
    monkeypatch.setattr(mod.jira_dispatch, "UNDER_REVIEW_STATUS_NAME", "Under Review")
    return calls


def _snapshot(labels: tuple[str, ...] = ()) -> Any:
    return SimpleNamespace(
        key="OP-1848",
        component="META",
        fix_version=None,
        created_at="2026-05-29T00:00:00.000+0000",
        days_since_created=1.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=labels,
    )


# ── AC5: agent already transitioned the ticket itself ─────────────


def test_finalize_skips_when_ticket_already_under_review(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """OP-690 incident pattern: codex called transition_to_under_review itself,
    so the post-CLI block sees status=Under Review. Must skip duplicate
    comment + transition entirely (no crash, no audit-trail dirt)."""
    mod = _load_jira_runner()

    def already_under_review(*args, **kwargs):
        return "Under Review"

    calls = _patch_dispatch(monkeypatch, mod, get_issue_status=already_under_review)

    mod._finalize_under_review(_StubClient(), "OP-690", "https://x/+/36")

    assert len(calls["get_issue_status"]) == 1
    assert calls["post_runner_pushed_comment"] == []
    assert calls["transition_to_under_review_if_needed"] == []
    assert calls["add_comment"] == []
    out = capsys.readouterr().out
    assert "already in Under Review" in out
    assert "skipping duplicate" in out


# ── AC6: happy path — In Progress at post-CLI time ────────────────


def test_finalize_happy_path_in_progress_posts_comment_and_transitions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Existing flow must continue to work: In Progress → comment + transition
    POST → log "→ Under Review"."""
    mod = _load_jira_runner()
    calls = _patch_dispatch(monkeypatch, mod)  # defaults: status=In Progress, transitioned=True

    mod._finalize_under_review(_StubClient(), "OP-691", "https://x/+/99")

    assert len(calls["get_issue_status"]) == 1
    assert len(calls["post_runner_pushed_comment"]) == 1
    posted_args = calls["post_runner_pushed_comment"][0][0]
    # post_runner_pushed_comment(client, key, gerrit_change_url)
    assert posted_args[1] == "OP-691"
    assert posted_args[2] == "https://x/+/99"
    assert len(calls["transition_to_under_review_if_needed"]) == 1
    assert calls["add_comment"] == []
    assert "→ Under Review" in capsys.readouterr().out


# ── AC7: transition POST fails — log skip comment, don't raise ────


def test_finalize_logs_skip_comment_when_transition_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Transition POST fails with e.g. 400 / 409 / 403 → runner posts a
    [runner-transition-skip] comment and returns normally. Gerrit push
    already succeeded, so a JIRA cosmetic failure should NOT crash the
    runner (rc must remain 0 for the operator)."""
    mod = _load_jira_runner()

    def boom(*args, **kwargs):
        raise RuntimeError("POST /issue/OP-691/transitions → 400: bad transition for state")

    calls = _patch_dispatch(monkeypatch, mod, transition_to_under_review_if_needed=boom)

    # Must NOT raise
    mod._finalize_under_review(_StubClient(), "OP-691", "https://x/+/99")

    assert len(calls["transition_to_under_review_if_needed"]) == 1
    assert len(calls["add_comment"]) == 1
    skip_text = calls["add_comment"][0][0][2]  # add_comment(client, key, text)
    assert "[runner-transition-skip]" in skip_text
    assert "https://x/+/99" in skip_text
    err = capsys.readouterr().err
    assert "transition to Under Review failed" in err


def test_finalize_does_not_crash_when_skip_comment_post_also_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Defensive belt-and-suspenders: if the skip-comment post itself fails
    (e.g. JIRA totally down), the runner still doesn't raise."""
    mod = _load_jira_runner()

    def boom_transition(*args, **kwargs):
        raise RuntimeError("transition 400")

    def boom_comment(*args, **kwargs):
        raise RuntimeError("comment 503")

    _patch_dispatch(
        monkeypatch, mod,
        transition_to_under_review_if_needed=boom_transition,
        add_comment=boom_comment,
    )
    mod._finalize_under_review(_StubClient(), "OP-691", "https://x/+/99")
    err = capsys.readouterr().err
    assert "could not even post skip comment" in err


def test_finalize_continues_to_transition_when_status_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """If get_issue_status raises (network blip), fall back to the legacy path
    of attempting comment+transition rather than silently skipping shipping."""
    mod = _load_jira_runner()

    def boom_status(*args, **kwargs):
        raise RuntimeError("read failed")

    calls = _patch_dispatch(monkeypatch, mod, get_issue_status=boom_status)

    mod._finalize_under_review(_StubClient(), "OP-691", "https://x/+/99")
    assert len(calls["post_runner_pushed_comment"]) == 1
    assert len(calls["transition_to_under_review_if_needed"]) == 1
    err = capsys.readouterr().err
    assert "could not read OP-691 status" in err


def test_finalize_successful_push_does_not_run_revert_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP-1858: claim/assignee strip stays bound to revert paths only."""
    mod = _load_jira_runner()
    calls: dict[str, list] = {"finalize": [], "release": []}

    monkeypatch.setattr(
        mod,
        "_finalize_under_review",
        lambda *args, **kwargs: calls["finalize"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        mod,
        "_medical_readiness_ok_for_closure",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "release_ticket_claim",
        lambda *args, **kwargs: calls["release"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "transition_back_to_todo",
        lambda *a, **kw: pytest.fail("successful push must not revert"),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "cleanup_reverted_ticket_claims_and_assignee",
        lambda *a, **kw: pytest.fail("successful push must not run revert cleanup"),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "clear_assignee",
        lambda *a, **kw: pytest.fail("successful push must not clear assignee"),
    )

    claim = SimpleNamespace(
        ok=True,
        claim_token="default:tok",
        coordination_lease_id=None,
        coordination_fencing_token=None,
    )
    push_result = SimpleNamespace(
        change_url="https://gerrit.example/+/1858",
        change_number=1858,
        post_push_warning=None,
    )

    mod._finalize_successful_push(_StubClient(), "OP-1858", push_result, claim)

    assert len(calls["finalize"]) == 1
    assert len(calls["release"]) == 1


def test_finalize_successful_push_blocks_medical_ticket_without_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls: dict[str, list] = {"comments": [], "finalize": [], "release": []}

    monkeypatch.setattr(
        mod.jira_dispatch,
        "_request",
        lambda c, method, path: {
            "fields": {
                "summary": "[Medical] close regulated ticket",
                "labels": ("regulated-lane",),
            }
        },
    )
    monkeypatch.setattr(
        mod.medical_readiness_check,
        "check_medical_readiness",
        lambda **kwargs: mod.medical_readiness_check.MedicalReadinessResult(
            passed=False,
            required=True,
            reasons=("missing required JIRA label 'regulated:medical'",),
            negative_leak_command=("pytest", "negative-leak"),
        ),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda c, k, text, idem_key=None: calls["comments"].append((k, text)),
    )
    monkeypatch.setattr(
        mod,
        "_finalize_under_review",
        lambda *args, **kwargs: calls["finalize"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "release_ticket_claim",
        lambda *args, **kwargs: calls["release"].append((args, kwargs)),
    )

    claim = SimpleNamespace(
        ok=True,
        claim_token="default:tok",
        coordination_lease_id=None,
        coordination_fencing_token=None,
    )
    push_result = SimpleNamespace(
        change_url="https://gerrit.example/+/2414",
        change_number=2414,
        post_push_warning=None,
    )

    mod._finalize_successful_push(_StubClient(), "OP-2414", push_result, claim)

    assert calls["finalize"] == []
    assert len(calls["release"]) == 1
    assert calls["comments"][0][0] == "OP-2414"
    assert "[medical-readiness-blocked]" in calls["comments"][0][1]
    assert "regulated:medical" in calls["comments"][0][1]


def test_ops_only_forward_blocks_medical_ticket_without_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls: dict[str, list] = {"forward": [], "comments": []}

    monkeypatch.setattr(
        mod,
        "_medical_readiness_ok_for_closure",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "forward_transition_ops_only",
        lambda *args, **kwargs: calls["forward"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda c, k, text, idem_key=None: calls["comments"].append((k, text)),
    )

    rc = mod._handle_ops_only_forward_transition(_StubClient(), "OP-2414")

    assert rc == 1
    assert calls["forward"] == []
    assert calls["comments"] == []


def test_is_camviewpro_contribution_requires_exact_target_label() -> None:
    mod = _load_jira_runner()

    assert mod._is_camviewpro_contribution(("target:camviewpro",))
    assert not mod._is_camviewpro_contribution(("target:other", "camviewpro"))
    assert not mod._is_camviewpro_contribution(())


@pytest.mark.parametrize(
    ("labels", "expected_project_key"),
    [
        (("target:camviewpro", "camviewpro-project:CAMVIEWPRO"), "CAMVIEWPRO"),
        (("target:camviewpro", "customer:case-1"), "case-1"),
        (
            ("target:camviewpro", "customer:case-1", "camviewpro-project:CAMVIEWPRO"),
            "case-1",
        ),
        (("target:camviewpro",), "OP"),
    ],
)
def test_run_camviewpro_contribution_derives_project_key_precedence(
    monkeypatch: pytest.MonkeyPatch,
    labels: tuple[str, ...],
    expected_project_key: str,
) -> None:
    mod = _load_jira_runner()
    snapshot = _snapshot(labels)
    calls: dict[str, list] = {"contribute": [], "labels": []}

    monkeypatch.setattr(
        mod.jira_dispatch,
        "_request",
        lambda c, method, path: {"fields": {"summary": "Route customer source"}},
    )

    async def fake_contribute_to_product(project_key, **kwargs):
        calls["contribute"].append((project_key, kwargs))
        return SimpleNamespace(
            no_changes=False,
            pr=SimpleNamespace(number=42, flagged_medical=False),
        )

    monkeypatch.setattr(mod, "contribute_to_product", fake_contribute_to_product)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_label",
        lambda c, k, label, idem_key=None: calls["labels"].append((k, label)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "transition_to_under_review_if_needed",
        lambda c, k, **kw: True,
    )

    rc = mod._run_camviewpro_contribution(
        _StubClient(), snapshot, "prompt", "subscription-codex", "tenant-a"
    )

    assert rc == 0
    project_key, kwargs = calls["contribute"][0]
    assert project_key == expected_project_key
    assert kwargs["ticket_key"] == "OP-1848"
    assert kwargs["base"] == "main"
    assert kwargs["tenant_id"] == "tenant-a"
    assert calls["labels"] == [("OP-1848", "camviewpro-pr:42")]


def test_run_camviewpro_contribution_derives_inputs_and_sets_pr_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_jira_runner()
    snapshot = _snapshot((
        "target:camviewpro",
        "camviewpro-project:CAM",
        "camviewpro-base:release/2.4",
    ))
    calls: dict[str, list] = {
        "contribute": [],
        "invoke": [],
        "labels": [],
        "transitions": [],
    }

    monkeypatch.setattr(
        mod.jira_dispatch,
        "_request",
        lambda c, method, path: {"fields": {"summary": "Add runner route"}},
    )

    async def fake_contribute_to_product(project_key, **kwargs):
        calls["contribute"].append((project_key, kwargs))
        kwargs["implement"](tmp_path / "camviewpro")
        return SimpleNamespace(
            no_changes=False,
            pr=SimpleNamespace(number=42, flagged_medical=False),
        )

    monkeypatch.setattr(mod, "contribute_to_product", fake_contribute_to_product)
    monkeypatch.setattr(
        mod,
        "_invoke_cli",
        lambda *args, **kwargs: calls["invoke"].append((args, kwargs)) or 0,
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_label",
        lambda c, k, label, idem_key=None: calls["labels"].append((k, label)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "transition_to_under_review_if_needed",
        lambda c, k, **kw: calls["transitions"].append(k) or True,
    )

    rc = mod._run_camviewpro_contribution(
        _StubClient(), snapshot, "prompt", "subscription-codex", "tenant-a"
    )

    assert rc == 0
    project_key, kwargs = calls["contribute"][0]
    assert project_key == "CAM"
    assert kwargs["ticket_key"] == "OP-1848"
    assert kwargs["base"] == "release/2.4"
    assert kwargs["slug"] == "Add runner route"
    assert kwargs["git_account_ref"] == "camviewpro-ro"
    assert kwargs["tenant_id"] == "tenant-a"
    invoke_args, invoke_kwargs = calls["invoke"][0]
    assert invoke_args[:2] == ("subscription-codex", "prompt")
    assert invoke_kwargs["ticket_key"] == "OP-1848"
    assert invoke_kwargs["worktree_path"] == tmp_path / "camviewpro"
    assert invoke_kwargs["tenant_id"] == "tenant-a"
    assert calls["labels"] == [("OP-1848", "camviewpro-pr:42")]
    assert calls["transitions"] == ["OP-1848"]


def test_run_camviewpro_contribution_defaults_base_and_marks_medical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    snapshot = _snapshot(("target:camviewpro",))
    calls: dict[str, list] = {"contribute": [], "labels": []}

    monkeypatch.setattr(
        mod.jira_dispatch,
        "_request",
        lambda c, method, path: {"fields": {"summary": "Medical lane change"}},
    )

    async def fake_contribute_to_product(project_key, **kwargs):
        calls["contribute"].append((project_key, kwargs))
        return SimpleNamespace(
            no_changes=False,
            pr=SimpleNamespace(number=7, flagged_medical=True),
        )

    monkeypatch.setattr(mod, "contribute_to_product", fake_contribute_to_product)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_label",
        lambda c, k, label, idem_key=None: calls["labels"].append((k, label)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "transition_to_under_review_if_needed",
        lambda c, k, **kw: True,
    )

    rc = mod._run_camviewpro_contribution(
        _StubClient(), snapshot, "prompt", "subscription-codex", "tenant-a"
    )

    assert rc == 0
    project_key, kwargs = calls["contribute"][0]
    assert project_key == "OP"
    assert kwargs["base"] == "main"
    assert calls["labels"] == [
        ("OP-1848", "camviewpro-pr:7"),
        ("OP-1848", "regulated-lane"),
    ]


def test_run_camviewpro_contribution_no_changes_comments_without_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    comments: list[str] = []

    monkeypatch.setattr(
        mod.jira_dispatch,
        "_request",
        lambda c, method, path: {"fields": {"summary": "No-op"}},
    )

    async def fake_contribute_to_product(project_key, **kwargs):
        return SimpleNamespace(no_changes=True, pr=None)

    monkeypatch.setattr(mod, "contribute_to_product", fake_contribute_to_product)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda c, k, text, idem_key=None: comments.append(text),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_label",
        lambda *a, **kw: pytest.fail("no_changes must not label a PR"),
    )

    rc = mod._run_camviewpro_contribution(
        _StubClient(), _snapshot(("target:camviewpro",)), "prompt", "subscription-codex", None
    )

    assert rc == 1
    assert "[runner-camviewpro-no-changes]" in comments[0]


def test_run_camviewpro_contribution_nonzero_cli_raises_before_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_jira_runner()

    monkeypatch.setattr(
        mod.jira_dispatch,
        "_request",
        lambda c, method, path: {"fields": {"summary": "Failing change"}},
    )

    async def fake_contribute_to_product(project_key, **kwargs):
        kwargs["implement"](tmp_path)
        pytest.fail("implement failure must stop before PR result")

    monkeypatch.setattr(mod, "contribute_to_product", fake_contribute_to_product)
    monkeypatch.setattr(mod, "_invoke_cli", lambda *a, **kw: 23)

    with pytest.raises(RuntimeError, match="rc=23"):
        mod._run_camviewpro_contribution(
            _StubClient(), _snapshot(("target:camviewpro",)), "prompt",
            "subscription-codex", None,
        )


def _wire_main_to_invoke(
    monkeypatch: pytest.MonkeyPatch,
    mod: Any,
    tmp_path: Path,
    snapshot: Any,
) -> dict[str, list]:
    calls: dict[str, list] = {
        "invoke": [],
        "camviewpro": [],
        "push": [],
        "release": [],
    }
    monkeypatch.setattr(mod, "AGENT_CLASS", "subscription-codex")
    monkeypatch.setattr(mod, "TARGET_OVERRIDE", "")
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(mod, "CODEX_WORKTREE", str(tmp_path))
    monkeypatch.setattr(mod.circuit_breaker, "open_services", lambda: [])
    monkeypatch.setattr(mod, "sweep_stale_runner_branches", lambda p: None)
    monkeypatch.setattr(mod, "configure_orphan_salvage_runtime", lambda: None)
    monkeypatch.setattr(mod.orphan_salvage, "salvage_orphan_commits", lambda p, c: 0)
    monkeypatch.setattr(mod.jira_dispatch, "assert_worktree_config_enabled", lambda r: None)
    monkeypatch.setattr(
        mod.jira_dispatch, "backpressure_decide", lambda *a, **kw: (True, "active")
    )
    monkeypatch.setattr(mod.jira_dispatch, "make_client", lambda *a, **kw: _StubClient())
    monkeypatch.setattr(mod, "_bridge_health_pickup_gate", lambda c: True)
    monkeypatch.setattr(mod.jira_dispatch, "fetch_pickable_tickets", lambda c: [object()])
    monkeypatch.setattr(mod.jira_dispatch, "to_snapshot", lambda issue: snapshot)
    monkeypatch.setattr(mod.scheduler, "load_weights", lambda: object())
    monkeypatch.setattr(
        mod.scheduler,
        "dispatch",
        lambda snapshots, weights, pre_pickup_check=None: snapshots[0],
    )
    monkeypatch.setattr(mod.runner_tenant, "resolve_tenant_id", lambda labels: "tenant-a")
    monkeypatch.setattr(mod.runner_tenant, "is_self_tenant", lambda tenant_id: True)
    monkeypatch.setattr(mod.db_context, "set_tenant_id", lambda tenant_id: None)
    monkeypatch.setattr(
        mod, "already_merged_in_gerrit", lambda key, gerrit_project=None: None
    )
    monkeypatch.setattr(mod.jira_dispatch, "set_bot_identity_in_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(mod.jira_dispatch, "install_commit_msg_hook", lambda p: True)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "sync_to_gerrit_develop",
        lambda *a, **kw: SimpleNamespace(develop_sha="a" * 40, detail="synced"),
    )
    monkeypatch.setattr(mod.runner_progress, "find_recovered_snapshot", lambda p: None)
    monkeypatch.setattr(mod.jira_dispatch, "pre_pickup_ok", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(mod.jira_dispatch, "fetch_description", lambda c, k: "desc")
    monkeypatch.setattr(mod.jira_dispatch, "file_mutex_check", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(mod.jira_dispatch, "remove_label", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_pre_pickup_capability_ok", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(mod, "_build_prompt", lambda c, k, d: "prompt")
    monkeypatch.setattr(
        mod.runner_workspace_safety, "assert_main_repo_unwritable_for_cli", lambda p: None
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "claim_ticket_atomic",
        lambda *a, **kw: SimpleNamespace(
            ok=True,
            lost_to=None,
            claim_token="default:tok",
            coordination_lease_id=None,
            coordination_fencing_token=None,
        ),
    )
    monkeypatch.setattr(mod, "_ensure_runner_character_card", lambda key: None)
    monkeypatch.setattr(
        mod.runner_workspace_safety,
        "write_workspace_sentinel",
        lambda p, k: tmp_path / "sentinel",
    )
    monkeypatch.setattr(mod.jira_dispatch, "transition_to_in_progress", lambda *a, **kw: None)
    monkeypatch.setattr(mod.runner_progress, "record_phase", lambda *a, **kw: None)
    monkeypatch.setattr(mod.live_state_check, "capture_transition_boundary_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(
        mod.runner_metrics_recorder,
        "record_pickup_sync",
        lambda start: (1, datetime.now(timezone.utc)),
    )
    monkeypatch.setattr(mod.runner_metrics_recorder, "record_completion_sync", lambda **kw: None)
    monkeypatch.setattr(
        mod.runner_workspace_safety,
        "verify_workspace_sentinel",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        mod,
        "_invoke_cli",
        lambda *args, **kwargs: calls["invoke"].append((args, kwargs)) or 99,
    )
    monkeypatch.setattr(
        mod,
        "_run_camviewpro_contribution",
        lambda *args, **kwargs: calls["camviewpro"].append((args, kwargs)) or 0,
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "push_to_gerrit_for_review",
        lambda *args, **kwargs: calls["push"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "transition_back_to_todo",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "release_ticket_claim",
        lambda *args, **kwargs: calls["release"].append((args, kwargs)),
    )
    monkeypatch.setattr(mod, "_run_memory_writeback", lambda *a, **kw: None)
    return calls


def test_main_without_camviewpro_label_uses_existing_invoke_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_jira_runner()
    calls = _wire_main_to_invoke(monkeypatch, mod, tmp_path, _snapshot(()))

    rc = mod.main()

    assert rc == 99
    assert calls["camviewpro"] == []
    assert len(calls["invoke"]) == 1
    assert calls["push"] == []


def test_main_with_camviewpro_label_routes_to_contribution_and_skips_invoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_jira_runner()
    snapshot = _snapshot(("target:camviewpro",))
    calls = _wire_main_to_invoke(monkeypatch, mod, tmp_path, snapshot)

    rc = mod.main()

    assert rc == 0
    assert calls["invoke"] == []
    assert calls["push"] == []
    assert len(calls["camviewpro"]) == 1
    args, _kwargs = calls["camviewpro"][0]
    assert isinstance(args[0], _StubClient)
    assert args[1:] == (snapshot, "prompt", "subscription-codex", "tenant-a")
    assert len(calls["release"]) == 1


# ---------------------------------------------------------------------------
# OP-1850 — _invoke_cli must honour the worktree_path override so the CLI's
# --cd (codex) / cwd (claude) point at the JAIL-BOUND worktree, not the default
# OmniSight one. Regression for the canary blocker: P2.4.3b dispatch passes a
# camviewpro clone as worktree_path; pre-fix the codex --cd was hardcoded to
# CODEX_WORKTREE → "No such file or directory" in the jail.
# ---------------------------------------------------------------------------

def _stub_invoke_sandbox(monkeypatch, mod) -> None:
    monkeypatch.setattr(mod.runner_sandbox, "build_allowlisted_env", lambda: {})
    monkeypatch.setattr(mod.runner_sandbox, "sandbox_available", lambda: False)
    monkeypatch.setattr(mod.runner_sandbox, "cli_home_for", lambda key: Path("/tmp/cli-home"))
    monkeypatch.setattr(mod.runner_sandbox, "cleanup_cli_home", lambda key: None)
    monkeypatch.setattr(mod.sandbox_prewarm, "dep_cache_mounts", lambda *a, **kw: None)
    monkeypatch.setattr(mod.db_context, "current_tenant_id", lambda: None)


def test_invoke_cli_codex_cd_uses_worktree_path_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_jira_runner()
    omni = tmp_path / "omnisight-wt"; omni.mkdir()
    override = tmp_path / "camviewpro-clone"; override.mkdir()
    monkeypatch.setattr(mod, "CODEX_WORKTREE", str(omni))
    monkeypatch.setattr(mod, "DRY_RUN", True)  # skip the real Popen; capture the cmd
    _stub_invoke_sandbox(monkeypatch, mod)
    captured: dict[str, Any] = {}

    def _fake_wrap(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["worktree_path"] = kw.get("worktree_path")
        return list(cmd)

    monkeypatch.setattr(mod.runner_sandbox, "wrap_in_bubblewrap", _fake_wrap)

    rc = mod._invoke_cli(
        "subscription-codex", prompt="x", ticket_key="OP-T", worktree_path=override
    )

    assert rc == 0
    assert captured["cmd"][:2] == ["codex", "exec"]
    cd_at = captured["cmd"].index("--cd")
    # the regression: pre-OP-1850 this was str(omni); MUST now be the override.
    assert captured["cmd"][cd_at + 1] == str(override)
    assert captured["worktree_path"] == override


def test_invoke_cli_claude_cwd_uses_worktree_path_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_jira_runner()
    omni = tmp_path / "omnisight-wt"; omni.mkdir()
    override = tmp_path / "camviewpro-clone"; override.mkdir()
    monkeypatch.setattr(mod, "CLAUDE_WORKTREE", str(omni))
    monkeypatch.setattr(mod, "DRY_RUN", False)  # need Popen to capture cwd
    _stub_invoke_sandbox(monkeypatch, mod)
    monkeypatch.setattr(mod.runner_sandbox, "wrap_in_bubblewrap", lambda cmd, **kw: list(cmd))
    captured: dict[str, Any] = {}

    class _FakeProc:
        def __init__(self, *a, **kw):
            captured["cwd"] = kw.get("cwd")
            self.returncode = 0

        def communicate(self, input=None, timeout=None):  # noqa: A002 - shim
            return ("", "")

    monkeypatch.setattr(mod.subprocess, "Popen", _FakeProc)

    rc = mod._invoke_cli(
        "subscription-claude", prompt="x", ticket_key="OP-T", worktree_path=override
    )

    assert rc == 0
    # the regression: pre-OP-1850 this was str(omni); MUST now be the override.
    assert captured["cwd"] == str(override)


# ---------------------------------------------------------------------------
# OP-1858 — _handle_camviewpro_dispatch_outcome must clean up the ticket on
# failure (release claim + revert to To Do + clear assignee) so re-pickup is
# not silently blocked by stale assignee / claim:*. Regression-guards the
# OP-1849 canary #2 finding.
# ---------------------------------------------------------------------------

def _make_claim() -> Any:
    return SimpleNamespace(ok=True, claim_token="codex-1:tok-1858", lost_to=None)


def test_handle_camviewpro_dispatch_outcome_success_releases_claim_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls: dict[str, list] = {"revert": [], "release": []}
    monkeypatch.setattr(
        mod, "_revert_cli_failure_to_todo",
        lambda c, k, rc, claim: calls["revert"].append((k, rc, claim)),
    )
    monkeypatch.setattr(
        mod, "_release_ticket_claim_if_acquired",
        lambda c, k, claim: calls["release"].append((k, claim)),
    )

    claim = _make_claim()
    mod._handle_camviewpro_dispatch_outcome(
        _StubClient(), "OP-X", claim, rc=0,
    )

    assert calls["release"] == [("OP-X", claim)]
    assert calls["revert"] == []  # success → NO revert, only release


def test_handle_camviewpro_dispatch_outcome_nonzero_rc_calls_full_revert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls: dict[str, list] = {"revert": [], "release": []}
    monkeypatch.setattr(
        mod, "_revert_cli_failure_to_todo",
        lambda c, k, rc, claim: calls["revert"].append((k, rc)),
    )
    monkeypatch.setattr(
        mod, "_release_ticket_claim_if_acquired",
        lambda c, k, claim: calls["release"].append(k),
    )

    mod._handle_camviewpro_dispatch_outcome(
        _StubClient(), "OP-X", _make_claim(), rc=1,
    )

    assert calls["revert"] == [("OP-X", 1)]
    assert calls["release"] == []  # full revert subsumes release


def test_handle_camviewpro_dispatch_outcome_exception_comments_and_reverts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls: dict[str, list] = {"revert": [], "comments": []}
    monkeypatch.setattr(
        mod, "_revert_cli_failure_to_todo",
        lambda c, k, rc, claim: calls["revert"].append((k, rc)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch, "add_comment",
        lambda c, k, body: calls["comments"].append((k, body)),
    )

    mod._handle_camviewpro_dispatch_outcome(
        _StubClient(), "OP-X", _make_claim(),
        exc=RuntimeError("camviewpro implement CLI failed rc=1"),
    )

    assert calls["revert"] == [("OP-X", 1)]
    assert len(calls["comments"]) == 1
    assert "RuntimeError" in calls["comments"][0][1]
    assert "camviewpro implement CLI failed" in calls["comments"][0][1]


def test_handle_camviewpro_dispatch_outcome_comment_failure_does_not_block_revert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # add_comment failing must NOT prevent the cleanup — the comment is
    # advisory, the revert is the safety-critical action.
    mod = _load_jira_runner()
    calls: dict[str, list] = {"revert": []}
    monkeypatch.setattr(
        mod, "_revert_cli_failure_to_todo",
        lambda c, k, rc, claim: calls["revert"].append((k, rc)),
    )

    def _boom(*a, **kw):
        raise ConnectionError("JIRA down")

    monkeypatch.setattr(mod.jira_dispatch, "add_comment", _boom)

    mod._handle_camviewpro_dispatch_outcome(
        _StubClient(), "OP-X", _make_claim(), exc=ValueError("bad config"),
    )

    assert calls["revert"] == [("OP-X", 1)]  # revert still ran
