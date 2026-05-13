"""SP-B-X-011 / OP-1069 — human-operator action race detection."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_authority_check, jira_dispatch, live_state_check  # noqa: E402


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_under_test_human_authority", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_under_test_human_authority", REPO_ROOT / "auto-runner-jira.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_classify_author_bot_l1_l2_and_unknown(tmp_path: Path) -> None:
    roster = tmp_path / "operator-deputy-roster.yaml"
    roster.write_text(
        yaml.safe_dump({
            "top_operator": {"accountId": "acc-sora"},
            "deputies": [{"accountId": "acc-deputy"}],
        }),
        encoding="utf-8",
    )
    bot = jira_authority_check.classify_author(
        {"accountId": "abc-codex-bot-123", "displayName": "Codex Bot"},
        roster_path=roster,
    )
    assert (bot.kind, bot.level) == ("automation", "L3")
    assert jira_authority_check.classify_author(
        {"accountId": "acc-sora"}, roster_path=roster,
    ).level == "L1"
    assert jira_authority_check.classify_author(
        {"accountId": "acc-deputy"}, roster_path=roster,
    ).level == "L2"
    unknown = jira_authority_check.classify_author(
        {"accountId": "acc-someone"}, roster_path=roster,
    )
    assert (unknown.kind, unknown.level) == ("human", "unknown")


def test_latest_authority_change_parses_jira_changelog(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(client: Any, method: str, path: str, body: Any = None,
                     idem_key: Any = None) -> dict[str, Any]:
        assert (method, path) == ("GET", "/issue/OP-1069/changelog")
        return {
            "values": [
                {
                    "created": "2026-05-14T01:00:00.000+0000",
                    "author": {"accountId": "acc-codex-bot", "displayName": "codex-bot"},
                    "items": [{"field": "labels", "fromString": "", "toString": "x"}],
                },
                {
                    "created": "2026-05-14T02:00:00.000+0000",
                    "author": {"accountId": "acc-human", "displayName": "Sora"},
                    "items": [{"field": "status", "fromString": "進行中", "toString": "To Do"}],
                },
            ]
        }

    monkeypatch.setattr(jira_dispatch, "_request", fake_request)
    change = jira_authority_check.latest_authority_change(object(), "OP-1069")
    assert change is not None
    assert (change.field, change.old, change.new) == ("status", "進行中", "To Do")
    assert change.author.kind == "human"


def test_runner_yields_to_human_status_change_and_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    comments: list[str] = []
    releases: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        mod.jira_dispatch, "add_comment",
        lambda client, key, text, idem_key=None: comments.append(text),
    )
    monkeypatch.setattr(
        mod.jira_dispatch, "transition_back_to_todo",
        lambda *a, **kw: pytest.fail("human yield must not transition"),
    )
    monkeypatch.setattr(
        mod.jira_dispatch, "release_ticket_claim",
        lambda client, key, instance, token=None: releases.append((key, instance, token)),
    )
    monkeypatch.setattr(
        mod.jira_authority_check, "latest_authority_change",
        lambda client, key: jira_authority_check.AuthorityChange(
            "status", "進行中", "To Do",
            jira_authority_check.AuthorityAuthor("acc-human", "Sora", "human", "L1"),
            "2026-05-14T02:00:00.000+0000",
        ),
    )
    claim = jira_dispatch.ClaimResult(
        ok=True, lost_to=None, claim_token="default:tok-a",
    )
    rc = mod._handle_toctou_abort(
        object(),
        "OP-1069",
        live_state_check.BoundaryRecheckResult(False, "abort_reverted", "status changed"),
        phase="pre-submit",
        claim=claim,
    )
    assert rc == 0
    assert comments == [
        "[runner-yielded-to-human-authority] author=acc-human "
        "change=status 進行中→To Do; pausing."
    ]
    assert releases == [("OP-1069", "default", "tok-a")]


def test_runner_keeps_existing_toctou_path_for_bot_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    comments: list[str] = []
    reverts: list[str] = []
    monkeypatch.setattr(
        mod.jira_dispatch, "add_comment",
        lambda client, key, text, idem_key=None: comments.append(text),
    )
    monkeypatch.setattr(
        mod.jira_dispatch, "transition_back_to_todo",
        lambda client, key, reason, **kw: reverts.append(reason),
    )
    monkeypatch.setattr(
        mod.jira_authority_check, "latest_authority_change",
        lambda client, key: jira_authority_check.AuthorityChange(
            "assignee", "claude-bot", "codex-bot",
            jira_authority_check.AuthorityAuthor(
                "acc-codex-bot", "codex-bot", "automation", "L3",
            ),
            "2026-05-14T02:00:00.000+0000",
        ),
    )
    rc = mod._handle_toctou_abort(
        object(),
        "OP-1069",
        live_state_check.BoundaryRecheckResult(
            False, "abort_assignee_changed", "assignee diverged",
        ),
        phase="pre-push",
    )
    assert rc == 1
    assert comments[0].startswith("[runner-toctou:pre-push:abort_assignee_changed]")
    assert reverts
    assert mod._runner_active_marker("OP-1069", "selected") == (
        "[runner-active: OP-1069 selected 1]"
    )
    assert mod._runner_active_marker("OP-1069", "working") == (
        "[runner-active: OP-1069 working 2]"
    )
