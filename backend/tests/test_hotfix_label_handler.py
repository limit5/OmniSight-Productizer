"""OP-950 hotfix label handler tests."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from backend.release_conductor import event_handlers
from backend.release_conductor.event_handlers import hotfix_label


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "hotfix_cherry_pick.py"


def _event(label: str = "hotfix:cherry-pick-to=release/v1.2.4+1") -> dict:
    return {
        "change": {"number": 95001},
        "approval": {"type": label, "value": "1"},
    }


def _load_script():
    spec = importlib.util.spec_from_file_location("hotfix_cherry_pick_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hotfix_cherry_pick_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_hotfix_label_dispatches_cherry_pick_and_instantiates_meta() -> None:
    comments: list[tuple[str, str]] = []

    def cherry_pick(change_number: str, target: str) -> dict:
        assert change_number == "95001"
        assert target == "release/v1.2.4+1"
        return {
            "cherry_picked_change": "95002",
            "cherry_picked_url": "https://sora.services/c/omnisight/+/95002",
        }

    def instantiate(target: str, picked_change: str) -> dict:
        assert target == "release/v1.2.4+1"
        assert picked_change == "95002"
        return {"meta_key": "HOTFIX-v1.2.4+1"}

    result = hotfix_label.on_hotfix_label_added(
        _event(),
        cherry_pick=cherry_pick,
        gerrit_comment=lambda change, message: comments.append((change, message)),
        meta_instantiator=instantiate,
    )

    assert result["outcome"] == "cherry_picked"
    assert result["cherry_picked_change"] == "95002"
    assert result["meta"] == {"meta_key": "HOTFIX-v1.2.4+1"}
    assert comments == [
        (
            "95001",
            "Hotfix cherry-pick succeeded: created Gerrit change 95002; META HOTFIX-v1.2.4+1",
        )
    ]


def test_gerrit_label_added_routes_hotfix_label_through_h2_dispatch(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        hotfix_label,
        "on_hotfix_label_added",
        lambda event: calls.append(event) or {"outcome": "cherry_picked"},
    )

    result = event_handlers.dispatch("gerrit", "label-added", _event())

    assert result == {"outcome": "cherry_picked"}
    assert calls == [_event()]


def test_conflict_refuses_and_opens_manual_merge_ticket() -> None:
    comments: list[str] = []
    tickets: list[tuple[str, str, str]] = []

    def cherry_pick(_change_number: str, _target: str) -> dict:
        raise hotfix_label.CherryPickConflict("content conflict in backend/app.py")

    result = hotfix_label.on_hotfix_label_added(
        _event(),
        cherry_pick=cherry_pick,
        gerrit_comment=lambda _change, message: comments.append(message),
        manual_merge_ticket=lambda change, target, detail: tickets.append((change, target, detail))
        or {"key": "OP-951"},
    )

    assert result["outcome"] == "refused"
    assert result["error"] == "CherryPickConflict"
    assert result["manual_merge_ticket"] == {"key": "OP-951"}
    assert result["source_unchanged"] is True
    assert tickets == [("95001", "release/v1.2.4+1", "content conflict in backend/app.py")]
    assert "Manual merge ticket opened" in comments[0]


def test_target_branch_missing_refuses_and_alerts() -> None:
    comments: list[str] = []

    def cherry_pick(_change_number: str, _target: str) -> dict:
        raise hotfix_label.TargetBranchMissing("target branch not found: release/v1.2.4+1")

    result = hotfix_label.on_hotfix_label_added(
        _event(),
        cherry_pick=cherry_pick,
        gerrit_comment=lambda _change, message: comments.append(message),
    )

    assert result["outcome"] == "refused"
    assert result["error"] == "TargetBranchMissing"
    assert result["source_unchanged"] is True
    assert "TargetBranchMissing" in comments[0]


def test_malformed_label_refuses_without_cherry_pick() -> None:
    comments: list[str] = []

    def cherry_pick(_change_number: str, _target: str) -> dict:
        raise AssertionError("malformed label must not invoke cherry-pick")

    result = hotfix_label.on_hotfix_label_added(
        _event("hotfix:cherry-pick-to=release/not-semver"),
        cherry_pick=cherry_pick,
        gerrit_comment=lambda _change, message: comments.append(message),
    )

    assert result["outcome"] == "refused"
    assert result["error"] == "LabelMisformed"
    assert result["source_unchanged"] is True
    assert "LabelMisformed" in comments[0]


def test_generic_failure_comments_reverted_partial_state_and_source_unchanged() -> None:
    comments: list[str] = []

    def cherry_pick(_change_number: str, _target: str) -> dict:
        raise RuntimeError("push rejected after local cleanup")

    result = hotfix_label.on_hotfix_label_added(
        _event(),
        cherry_pick=cherry_pick,
        gerrit_comment=lambda _change, message: comments.append(message),
    )

    assert result["outcome"] == "failed"
    assert result["error"] == "RuntimeError"
    assert result["source_unchanged"] is True
    assert "partial state reverted" in comments[0]


def test_script_aborts_cherry_pick_and_restores_original_branch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script = _load_script()
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[list[str]] = []

    def fake_run(args, *, repo, env=None, check=True):
        calls.append(args)
        if args[:4] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "develop\n", "")
        if args[:3] == ["ssh", "-i", "/tmp/key"]:
            payload = {
                "project": "omnisight/OmniSight-Productizer",
                "currentPatchSet": {"ref": "refs/changes/01/95001/1", "revision": "abc123"},
            }
            return subprocess.CompletedProcess(args, 0, f"{payload}\n".replace("'", '"'), "")
        if args[:4] == ["git", "rev-parse", "--verify", "release/v1.2.4+1^{commit}"]:
            return subprocess.CompletedProcess(args, 0, "targetsha\n", "")
        if args == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "targetsha\n", "")
        if args[:2] == ["git", "cherry-pick"] and args[2:] == ["FETCH_HEAD"]:
            return subprocess.CompletedProcess(args, 1, "", "CONFLICT (content): backend/app.py\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(script, "_run", fake_run)
    monkeypatch.setattr(
        script,
        "_gerrit_env",
        lambda _agent_class: {"GIT_SSH_COMMAND": "ssh -i /tmp/key"},
    )
    monkeypatch.setattr(
        script.jira_dispatch,
        "_gerrit_auth_for_instance",
        lambda _agent_class: ("codex-bot", Path("/tmp/key")),
    )
    monkeypatch.setattr(
        script.jira_dispatch,
        "_gerrit_ssh_url",
        lambda _agent_class: "ssh://codex-bot@sora.services:29418/omnisight/OmniSight-Productizer",
    )

    try:
        script.cherry_pick_change(
            repo=repo,
            change_number="95001",
            target="release/v1.2.4+1",
            agent_class="subscription-codex",
        )
    except script.CherryPickConflict:
        pass
    else:
        raise AssertionError("expected CherryPickConflict")

    assert ["git", "cherry-pick", "--abort"] in calls
    assert ["git", "reset", "--hard", "targetsha"] in calls
    assert ["git", "checkout", "develop"] in calls
