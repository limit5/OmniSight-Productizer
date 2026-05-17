"""OP-1061 — runner releases fenced JIRA claims after post-CLI exits."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch  # noqa: E402


def _load_runner():
    path = REPO_ROOT / "auto-runner-jira.py"
    spec = importlib.util.spec_from_file_location("auto_runner_jira", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_wrapper_passes_fencing_token(monkeypatch) -> None:
    runner = _load_runner()
    calls: list[tuple[str, str, str, str | None, dict[str, Any]]] = []
    claim = jira_dispatch.ClaimResult(
        ok=True,
        lost_to=None,
        claim_token="default:0001715518859123456-3af9c1d0",
    )

    monkeypatch.setattr(runner, "INSTANCE_ID", "default")

    def fake_release(client, key, instance_id, token=None, **kwargs):
        calls.append((client, key, instance_id, token, kwargs))

    monkeypatch.setattr(
        runner.jira_dispatch,
        "release_ticket_claim",
        fake_release,
    )

    runner._release_ticket_claim_if_acquired("client", "OP-1061", claim)

    assert calls == [
        (
            "client",
            "OP-1061",
            "default",
            "0001715518859123456-3af9c1d0",
            {"coordination_lease_id": None, "coordination_fencing_token": None},
        )
    ]


def test_pre_claim_exit_does_not_release(monkeypatch) -> None:
    runner = _load_runner()
    calls: list[Any] = []
    monkeypatch.setattr(
        runner.jira_dispatch,
        "release_ticket_claim",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    runner._release_ticket_claim_if_acquired("client", "OP-1061", None)
    runner._release_ticket_claim_if_acquired(
        "client",
        "OP-1061",
        jira_dispatch.ClaimResult(ok=False, lost_to="claim:other:tok"),
    )

    assert calls == []


def test_double_release_is_noop_after_first_put(monkeypatch) -> None:
    labels = ["claim:default:tok-a", "claim:default:tok-b", "area:backend"]
    puts: list[list[str]] = []

    def fake_request(client, method, path, body=None):
        nonlocal labels
        if method == "GET":
            return {"fields": {"labels": list(labels)}}
        removed = [item["remove"] for item in body["update"]["labels"]]
        puts.append(removed)
        labels = [label for label in labels if label not in removed]
        return {}

    monkeypatch.setattr(jira_dispatch, "_request", fake_request)

    jira_dispatch.release_ticket_claim("client", "OP-1061", "default", "tok-a")
    jira_dispatch.release_ticket_claim("client", "OP-1061", "default", "tok-a")

    assert puts == [["claim:default:tok-a", "claim:default:tok-b"]]
    assert labels == ["area:backend"]


def test_cli_rc_failure_revert_clears_assignee_and_releases_claim(monkeypatch) -> None:
    runner = _load_runner()
    labels = ["claim:default:tok-a", "area:tooling"]
    assignee: str | None = "acc-codex-bot"
    transitions: list[str] = []
    claim = jira_dispatch.ClaimResult(
        ok=True,
        lost_to=None,
        claim_token="default:tok-a",
    )

    def fake_transition(client, key, reason):
        transitions.append(reason)

    def fake_clear_assignee(client, key):
        nonlocal assignee
        assignee = None

    def fake_release(client, key, instance_id, token=None, **kwargs):
        nonlocal labels
        labels = [label for label in labels if not label.startswith("claim:")]

    monkeypatch.setattr(runner, "INSTANCE_ID", "default")
    monkeypatch.setattr(
        runner.jira_dispatch,
        "transition_back_to_todo",
        fake_transition,
    )
    monkeypatch.setattr(runner.jira_dispatch, "clear_assignee", fake_clear_assignee)
    monkeypatch.setattr(runner.jira_dispatch, "release_ticket_claim", fake_release)

    runner._revert_cli_failure_to_todo("client", "OP-1410", 1, claim)

    assert transitions == ["CLI exited 1; needs operator review."]
    assert assignee is None
    assert labels == ["area:tooling"]


def test_cli_rc_failure_clear_assignee_failure_still_releases_claim(monkeypatch) -> None:
    runner = _load_runner()
    releases: list[tuple[str, str | None]] = []
    claim = jira_dispatch.ClaimResult(
        ok=True,
        lost_to=None,
        claim_token="default:tok-a",
    )

    monkeypatch.setattr(runner, "INSTANCE_ID", "default")
    monkeypatch.setattr(
        runner.jira_dispatch,
        "transition_back_to_todo",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        runner.jira_dispatch,
        "clear_assignee",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("jira down")),
    )
    monkeypatch.setattr(
        runner.jira_dispatch,
        "release_ticket_claim",
        lambda client, key, instance_id, token=None, **kwargs: releases.append(
            (key, token)
        ),
    )

    runner._revert_cli_failure_to_todo("client", "OP-1410", 1, claim)

    assert releases == [("OP-1410", "tok-a")]


def test_enumerated_exit_paths_release_after_claim() -> None:
    source = (REPO_ROOT / "auto-runner-jira.py").read_text()
    required = [
        "_finalize_under_review(\n"
        "        client,\n"
        "        key,\n"
        "        push_result.change_url,",
        "_release_ticket_claim_if_acquired(client, key, claim)",
        "except jira_dispatch.NoCommitsOnBranchError as e:",
        "except jira_dispatch.WorktreeDirtyError as e:",
        "except Exception as e:\n"
        "            # Empty-tree / rebase --keep-empty failure path:",
        "except outcomes_consumer.OutcomesGraderRefused:\n"
        "                _release_ticket_claim_if_acquired(client, snapshot.key, claim)",
        "if outcomes_status == \"fail\":",
        "_handle_gerrit_push_failure(\n"
        "                client,\n"
        "                snapshot.key,",
        "_revert_cli_failure_to_todo(client, snapshot.key, rc, claim)",
    ]
    for needle in required:
        assert needle in source

    assert source.count("_release_ticket_claim_if_acquired") >= 9


def test_audited_revert_paths_clear_assignee_before_release() -> None:
    source = (REPO_ROOT / "auto-runner-jira.py").read_text().split(
        "# OP-836 post-CLI verify", 1
    )[1]
    markers = [
        "[runner-workspace-tampered]",
        'print(f"[runner] CLI produced no commits:',
        'print(f"[runner] CLI left worktree dirty:',
        'print(f"[runner] Gerrit push setup failed:',
    ]
    for marker in markers:
        block = source.split(marker, 1)[1].split("return 1", 1)[0]
        assert "_clear_assignee_after_revert(client, snapshot.key)" in block
        assert "_release_ticket_claim_if_acquired(client, snapshot.key, claim)" in block
        assert block.index("_clear_assignee_after_revert") < block.index(
            "_release_ticket_claim_if_acquired"
        )


def test_area_label_rejection_remains_pre_claim_noop() -> None:
    source = (REPO_ROOT / "auto-runner-jira.py").read_text()
    area_block = source.split("except UnknownAreaLabelError as e:", 1)[1].split(
        "enabled_caps =", 1
    )[0]
    assert "claim_ticket_atomic" not in area_block
    assert "_release_ticket_claim_if_acquired" not in area_block
