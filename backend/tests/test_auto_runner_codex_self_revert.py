"""OP-963 — AUDIT-15: runner must not dual-revert when the CLI already
bounced the ticket itself.

Scenario (OP-925 R3, 2026-05-12 11:01-11:05): the codex CLI follows the
§11 discovered-dependency protocol — it posts `[runner-discovered-
dependency]`, calls `transition_back_to_todo`, then exits 0. The runner's
post-CLI Gerrit-push pipeline then hits `NoCommitsOnBranchError` (there
*are* no commits) and used to pile on a second `[runner-no-commits-from-
cli]` revert (or, with the OP-956 ops-only label present, an ops-only
forward-walk that 400s from To Do). Double-comment noise.

The fix re-fetches the live ticket status at the no-commits decision
point: if it is already in To Do the CLI self-reverted, so the runner
emits one log line and exits 0 — no additional JIRA write.

Network-free: the runner is loaded via importlib (hyphenated filename)
and every JIRA/git/metrics dependency is monkeypatched.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.agents import jira_dispatch


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_codex_self_revert_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_codex_self_revert_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    agent_class = "subscription-codex"
    bot_email = "rt3628+codex-bot@gmail.com"
    bot_account_id = "bot-account-id"
    project_key = "OP"


# ── Unit: the new decision primitive ─────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("To Do", True),
        ("進行中", False),
        ("In Progress", False),
        ("Under Review", False),
        ("公開済み", False),
    ],
)
def test_cli_self_reverted_to_todo_reads_live_status(
    monkeypatch: pytest.MonkeyPatch, status: str, expected: bool
) -> None:
    mod = _load_jira_runner()
    monkeypatch.setattr(
        mod.jira_dispatch, "get_issue_status", lambda client, key: status
    )
    assert mod._cli_self_reverted_to_todo(_StubClient(), "OP-925") is expected


def test_cli_self_reverted_to_todo_degrades_to_false_on_read_fault(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live-status read fault must never be *worse* than the old
    always-revert behaviour: return False → the standard no-commits
    handler still runs."""
    mod = _load_jira_runner()

    def boom(client, key):
        raise RuntimeError("jira 503")

    monkeypatch.setattr(mod.jira_dispatch, "get_issue_status", boom)
    assert mod._cli_self_reverted_to_todo(_StubClient(), "OP-925") is False
    assert "could not re-read status" in capsys.readouterr().err


# ── Source-structure: the no-commits handler short-circuits FIRST ────


def test_no_commits_handler_checks_self_revert_before_other_paths() -> None:
    """The `_cli_self_reverted_to_todo` gate must run before the
    OP-956 ops-only branch and before the `[runner-no-commits-from-cli]`
    comment — otherwise an ops-only-labelled ticket that codex already
    bounced would 400 on a forward-walk from To Do, and a non-labelled
    one would get the dual revert."""
    src = _RUNNER_PATH.read_text()
    handler = src.split("except jira_dispatch.NoCommitsOnBranchError as e:", 1)[1]
    handler = handler.split("except jira_dispatch.WorktreeDirtyError", 1)[0]
    i_selfrevert = handler.index("if _cli_self_reverted_to_todo(client, snapshot.key):")
    i_opsonly = handler.index("if _ops_only_active_for(snapshot, client=client):")
    i_nocommits = handler.index(
        'f"[runner-no-commits-from-cli] CLI exited cleanly but produced "'
    )
    assert i_selfrevert < i_opsonly < i_nocommits
    # The self-revert arm exits clean and posts nothing.
    self_revert_arm = handler[i_selfrevert:i_opsonly]
    assert "return 0" in self_revert_arm
    assert "add_comment" not in self_revert_arm
    assert "transition_back_to_todo" not in self_revert_arm


# ── End-to-end: drive main() through the OP-925 11:05 sequence ───────


def _issue(key: str = "OP-925") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": f"{key} hard-dependency ticket",
            "labels": ["class:subscription-codex", "tier:S", "area:backend"],
            "status": {"name": "To Do"},
            "issuetype": {"name": "Story"},
            "fixVersions": [],
            "created": "2026-05-12T00:00:00.000+0000",
            "components": [{"name": "HIGH"}],
            "issuelinks": [],
            "parent": None,
        },
    }


def _wire_main_to_no_commits(
    monkeypatch: pytest.MonkeyPatch, mod: Any, sink: dict[str, list]
) -> None:
    """Monkeypatch every dependency main() touches between startup and the
    `NoCommitsOnBranchError` handler so the only JIRA *writes* that can
    happen are the ones the handler itself issues."""
    monkeypatch.setattr(mod, "TARGET_OVERRIDE", "OP-925")
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(mod.circuit_breaker, "open_services", lambda: [])
    monkeypatch.setattr(mod.orphan_salvage, "salvage_orphan_commits", lambda p, c: 0)
    monkeypatch.setattr(mod.runner_sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(
        mod.jira_dispatch, "assert_worktree_config_enabled", lambda repo: None
    )
    monkeypatch.setattr(
        mod.jira_dispatch, "backpressure_decide", lambda *a, **kw: (True, "active")
    )
    monkeypatch.setattr(mod.jira_dispatch, "make_client", lambda *a, **kw: _StubClient())
    monkeypatch.setattr(
        mod.jira_dispatch, "_request", lambda client, method, path: _issue()
    )
    monkeypatch.setattr(mod.jira_dispatch, "to_snapshot", jira_dispatch.to_snapshot)
    monkeypatch.setattr(mod, "already_merged_in_gerrit", lambda key: None)
    monkeypatch.setattr(
        mod.jira_dispatch, "set_bot_identity_in_worktree", lambda *a, **kw: None
    )
    monkeypatch.setattr(mod.jira_dispatch, "install_commit_msg_hook", lambda p: True)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "sync_to_gerrit_develop",
        lambda *a, **kw: SimpleNamespace(develop_sha="a" * 40, detail="synced"),
    )
    monkeypatch.setattr(mod.jira_dispatch, "pre_pickup_ok", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(mod.jira_dispatch, "fetch_description", lambda c, k: "desc")
    monkeypatch.setattr(mod.jira_dispatch, "file_mutex_check", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(mod.jira_dispatch, "remove_label", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_build_prompt", lambda c, k, d: "prompt")
    monkeypatch.setattr(
        mod.runner_workspace_safety, "assert_main_repo_unwritable_for_cli", lambda p: None
    )
    monkeypatch.setattr(
        mod.runner_workspace_safety,
        "write_workspace_sentinel",
        lambda p, k: Path("/tmp/sentinel"),
    )
    monkeypatch.setattr(
        mod.runner_workspace_safety, "verify_workspace_sentinel", lambda *a, **kw: {}
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "claim_ticket_atomic",
        lambda *a, **kw: SimpleNamespace(ok=True, lost_to=None, claim_token="tok"),
    )
    monkeypatch.setattr(mod.jira_dispatch, "transition_to_in_progress", lambda *a, **kw: None)
    monkeypatch.setattr(
        mod.runner_metrics_recorder,
        "record_pickup_sync",
        lambda start: (1, datetime.now(timezone.utc)),
    )
    monkeypatch.setattr(
        mod.runner_metrics_recorder, "record_completion_sync", lambda **kw: None
    )
    monkeypatch.setattr(mod, "_invoke_cli", lambda *a, **kw: 0)
    monkeypatch.setattr(mod, "_require_runner_capability", lambda *a, **kw: True)

    def fake_ensure_change_ids(worktree_path, base_ref):
        raise jira_dispatch.NoCommitsOnBranchError(base_ref=base_ref, head="b" * 40)

    monkeypatch.setattr(mod.jira_dispatch, "ensure_change_ids", fake_ensure_change_ids)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "push_to_gerrit_for_review",
        lambda *a, **kw: pytest.fail("push must not run with 0 commits"),
    )
    monkeypatch.setattr(mod, "_run_memory_writeback", lambda *a, **kw: None)

    # Record any JIRA write that *would* be a dual-revert.
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda c, k, text, idem_key=None: sink["comments"].append((k, text)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "transition_back_to_todo",
        lambda *a, **kw: sink["reverts"].append((a, kw)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "forward_transition_ops_only",
        lambda *a, **kw: sink["forwards"].append((a, kw)),
    )


def test_main_returns_0_and_posts_nothing_when_cli_already_self_reverted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC #1 + #4: codex exits 0 + 0 commits + ticket already in To Do
    (codex did its own §11 revert) → runner produces NO additional
    comment, no transition, returns rc=0."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()
    sink: dict[str, list] = {"comments": [], "reverts": [], "forwards": []}
    _wire_main_to_no_commits(monkeypatch, mod, sink)
    # Live status: codex already bounced the ticket to To Do.
    monkeypatch.setattr(mod.jira_dispatch, "get_issue_status", lambda c, k: "To Do")

    assert mod.main() == 0
    assert sink["comments"] == []
    assert sink["reverts"] == []
    assert sink["forwards"] == []
    assert "self-reverted" in capsys.readouterr().out


def test_main_still_reverts_when_cli_left_ticket_in_progress(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the OP-827 path: when the CLI did *not*
    self-revert (ticket still In Progress), the no-commits handler keeps
    posting `[runner-no-commits-from-cli]` + reverting to To Do."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()
    sink: dict[str, list] = {"comments": [], "reverts": [], "forwards": []}
    _wire_main_to_no_commits(monkeypatch, mod, sink)
    monkeypatch.setattr(mod.jira_dispatch, "get_issue_status", lambda c, k: "In Progress")

    assert mod.main() == 1
    assert any("[runner-no-commits-from-cli]" in t for _k, t in sink["comments"])
    assert len(sink["reverts"]) == 1
    assert sink["forwards"] == []
