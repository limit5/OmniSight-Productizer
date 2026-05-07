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
from pathlib import Path
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
