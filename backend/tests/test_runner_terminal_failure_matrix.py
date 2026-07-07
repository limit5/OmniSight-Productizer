"""OP-2542 (R3.2) — runner terminal-failure matrix: every terminal writes an incident.

Two representative terminal tests proving the pinned OP-2537 contract is
threaded at the ``transition_back_to_todo(..., failure_class=, claim_token=)``
seam:

  1. CLI-failure revert (``_revert_cli_failure_to_todo``) — passes
     ``failure_class`` (rc=124 → RUNNER_TIMEOUT, otherwise OTHER) AND the
     OP-977 fencing token minted at claim time.
  2. Gerrit push-failure revert (``_handle_gerrit_push_failure``) — the
     merge-conflict category maps to MERGE_CONFLICT and the token is
     threaded; pre-claim shape (claim=None) degrades to claim_token=None.

Network-free: the runner is loaded via importlib.spec_from_file_location and
``jira_dispatch`` is patched at the module-attribute level (same harness as
test_auto_runner_jira.py).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_terminal_matrix_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_terminal_matrix_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    agent_class = "subscription-claude"
    bot_email = "rt3628+claude-bot@gmail.com"
    bot_account_id = "acc-claude-bot"


def _won_claim(mod: Any, token: str = "tok-r32") -> Any:
    return SimpleNamespace(
        ok=True,
        lost_to=None,
        claim_token=f"{mod.INSTANCE_ID}:{token}",
        coordination_lease_id=None,
        coordination_fencing_token=None,
    )


def _patch_terminal_seam(
    monkeypatch: pytest.MonkeyPatch, mod: Any
) -> dict[str, list]:
    calls: dict[str, list] = {
        "transition_back_to_todo": [],
        "release_ticket_claim": [],
        "add_comment": [],
    }

    def recorder(name: str):
        def fn(*args, **kwargs):
            calls[name].append((args, kwargs))
        return fn

    for name in calls:
        monkeypatch.setattr(mod.jira_dispatch, name, recorder(name))
    return calls


# ── terminal 1: CLI-failure revert ─────────────────────────────────


@pytest.mark.parametrize(
    ("rc", "expected_class"),
    [(1, "OTHER"), (124, "RUNNER_TIMEOUT")],
)
def test_cli_failure_revert_threads_failure_class_and_claim_token(
    monkeypatch: pytest.MonkeyPatch, rc: int, expected_class: str
) -> None:
    mod = _load_jira_runner()
    calls = _patch_terminal_seam(monkeypatch, mod)
    claim = _won_claim(mod)

    mod._revert_cli_failure_to_todo(_StubClient(), "OP-2542", rc, claim)

    assert len(calls["transition_back_to_todo"]) == 1
    _, kwargs = calls["transition_back_to_todo"][0]
    assert kwargs["failure_class"] == expected_class
    assert kwargs["claim_token"] == claim.claim_token
    # OP-1524 ordering: claim release fired (before the revert comment).
    assert len(calls["release_ticket_claim"]) == 1


# ── terminal 2: Gerrit push-failure revert ─────────────────────────


def test_push_failure_revert_maps_merge_conflict_and_threads_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    calls = _patch_terminal_seam(monkeypatch, mod)
    claim = _won_claim(mod, token="tok-push")

    category, action = mod._handle_gerrit_push_failure(
        _StubClient(),
        "OP-2542",
        "error: merge conflict while rebasing onto develop",
        claim=claim,
    )

    assert (category, action) == ("merge_conflict", "revert")
    assert len(calls["transition_back_to_todo"]) == 1
    _, kwargs = calls["transition_back_to_todo"][0]
    assert kwargs["failure_class"] == "MERGE_CONFLICT"
    assert kwargs["claim_token"] == claim.claim_token


def test_push_failure_revert_without_claim_passes_none_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-claim / lost-claim shape: claim_token=None per the R3.2 matrix."""
    mod = _load_jira_runner()
    calls = _patch_terminal_seam(monkeypatch, mod)

    mod._handle_gerrit_push_failure(
        _StubClient(),
        "OP-2542",
        "remote rejected: invalid author",
        claim=None,
    )

    assert len(calls["transition_back_to_todo"]) == 1
    _, kwargs = calls["transition_back_to_todo"][0]
    assert kwargs["failure_class"] == "OTHER"
    assert kwargs["claim_token"] is None
