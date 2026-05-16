"""OP-1057 Gerrit push result semantics."""

from __future__ import annotations

import subprocess
from pathlib import Path

from backend.agents import jira_dispatch as jd


def _patch_auth(monkeypatch, tmp_path: Path) -> None:
    key = tmp_path / "ssh-key"
    key.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(
        jd,
        "_gerrit_auth_for_instance",
        lambda agent_class, instance_id=None: ("codex-bot", key),
    )
    monkeypatch.setattr(jd, "_head_change_id", lambda worktree_path: "Iabc123")


def _patch_push(monkeypatch, returncode: int = 0, blob: str | None = None) -> None:
    if blob is None:
        blob = (
            "remote:   https://sora.services:29420/c/"
            "omnisight/OmniSight-Productizer/+/42 subject\n"
        )

    def fake_breaker_call(fn, args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout="", stderr=blob)

    monkeypatch.setattr(jd.BREAKERS["gerrit_ssh"], "call", fake_breaker_call)


def test_ssh_success_self_fix_success_returns_clean_success(monkeypatch, tmp_path: Path) -> None:
    from backend.agents import auto_rebase, pre_review_self_fix

    _patch_auth(monkeypatch, tmp_path)
    _patch_push(monkeypatch)
    monkeypatch.setattr(auto_rebase, "load_owner_http_password", lambda user: "secret")
    monkeypatch.setattr(
        pre_review_self_fix,
        "self_fix_mergeability",
        lambda **kwargs: pre_review_self_fix.SelfFixResult(mergeable=True, attempts=1),
    )

    result = jd.push_to_gerrit_for_review(tmp_path, "subscription-codex")

    assert result.success is True
    assert result.change_number == 42
    assert result.post_push_warning is None


def test_ssh_success_self_fix_rest_fail_returns_success_with_warning(
    monkeypatch, tmp_path: Path
) -> None:
    from backend.agents import auto_rebase, pre_review_self_fix

    _patch_auth(monkeypatch, tmp_path)
    _patch_push(monkeypatch)
    monkeypatch.setattr(auto_rebase, "load_owner_http_password", lambda user: "secret")

    def boom(**kwargs):
        raise RuntimeError("REST 503")

    monkeypatch.setattr(pre_review_self_fix, "self_fix_mergeability", boom)

    result = jd.push_to_gerrit_for_review(tmp_path, "subscription-codex")

    assert result.success is True
    assert result.change_number == 42
    assert result.post_push_warning is not None
    assert "pre-review mergeability self-fix failed: RuntimeError: REST 503" in result.post_push_warning


def test_ssh_success_self_fix_cap_exhausted_returns_success_with_warning_and_escalation_marker(
    monkeypatch, tmp_path: Path
) -> None:
    from backend.agents import auto_rebase, pre_review_self_fix

    _patch_auth(monkeypatch, tmp_path)
    _patch_push(monkeypatch)
    monkeypatch.setattr(auto_rebase, "load_owner_http_password", lambda user: "secret")
    monkeypatch.setattr(jd, "_infer_ticket_key_from_worktree", lambda worktree_path: "OP-1057")
    monkeypatch.setattr(jd, "_pre_review_self_fix_diff_context", lambda worktree_path, target: "diff")
    monkeypatch.setattr(jd, "make_client", lambda agent_class, instance_id=None: object())
    monkeypatch.setattr(
        jd,
        "file_pre_review_self_fix_exhaustion_ticket",
        lambda *args, **kwargs: "OP-2000",
    )
    monkeypatch.setattr(
        pre_review_self_fix,
        "self_fix_mergeability",
        lambda **kwargs: pre_review_self_fix.SelfFixResult(
            mergeable=False,
            attempts=3,
            rebased=True,
            force_pushed=True,
            cap_exhausted=True,
            detail="mergeable=false after 3 self-fix attempt(s)",
        ),
    )

    result = jd.push_to_gerrit_for_review(tmp_path, "subscription-codex")

    assert result.success is True
    assert result.change_number == 42
    assert result.post_push_warning is not None
    assert "mergeable=false after 3 self-fix attempt(s)" in result.post_push_warning
    assert "Filed escalation ticket OP-2000" in result.post_push_warning


def test_ssh_nonzero_still_returns_hard_fail(monkeypatch, tmp_path: Path) -> None:
    _patch_auth(monkeypatch, tmp_path)
    _patch_push(monkeypatch, returncode=1, blob="remote rejected")
    monkeypatch.setattr(jd, "_is_transient_gerrit_push_failure", lambda blob: False)

    result = jd.push_to_gerrit_for_review(tmp_path, "subscription-codex")

    assert result.success is False
    assert result.change_number is None
    assert result.post_push_warning is None
    assert "remote rejected" in result.detail


def test_change_url_parse_fail_still_returns_hard_fail(monkeypatch, tmp_path: Path) -> None:
    _patch_auth(monkeypatch, tmp_path)
    _patch_push(monkeypatch, blob="remote: SUCCESS without review URL")

    result = jd.push_to_gerrit_for_review(tmp_path, "subscription-codex")

    assert result.success is False
    assert result.change_number is None
    assert result.post_push_warning is None
    assert "Change URL not parsed" in result.detail
