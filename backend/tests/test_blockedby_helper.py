"""OP-874 — tests for the intent-named blockedBy helper + audit + lint.

This file contains literal `POST /issueLink` fixture text (inside string
literals) that exercises the lint rule. The marker ``op-874-allow-raw-issuelink-post``
below is the documented suppression token from
``scripts/check_issuelink_post_callers.py``; it tells the lint rule the
fixture is intentional and not a real raw caller.

Seven cases cover:

1. ``add_blocked_by`` happy path emits the correct API direction.
2. ``add_blocked_by`` is idempotent on second call (no duplicate link).
3. ``add_blocked_by`` refuses self-reference with the typed error.
4. The audit detects an inverted Blocks link from the comment trail.
5. ``--fix`` corrects the link and emits a rollback record.
6. ``OMNISIGHT_BLOCKEDBY_FAIL_CLOSED`` flips the exception path
   from fail-open ``(False, "skipped")`` to fail-closed
   ``(True, "skipped")``.
7. The raw-``POST /issueLink`` lint rule fires when a non-allowlisted
   caller appears.
"""
from __future__ import annotations

import sys
import textwrap
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import file_coordinator as fc
from backend.agents import jira_dispatch as jd
from backend.agents import scheduler


def _client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic test",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


def _snapshot(key: str) -> scheduler.TicketSnapshot:
    return scheduler.TicketSnapshot(
        key=key,
        component="default",
        fix_version=None,
        created_at="2026-05-11T00:00:00.000+0000",
        days_since_created=0.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=(),
    )


# ─── AC1 happy path ───────────────────────────────────────────────


def test_add_blocked_by_happy_path_emits_blocker_as_inward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_blocked_by(blocked=X, blocker=Y) must POST inward=Y, outward=X."""
    created: list[dict] = []
    monkeypatch.setattr(
        fc, "jira_link_exists",
        lambda client, *, blocked, blocker, link_type="Blocks": False,
    )

    def fake_create(client, *, inward, outward, link_type="Blocks"):
        created.append({"inward": inward, "outward": outward, "type": link_type})

    monkeypatch.setattr(fc, "jira_create_issue_link", fake_create)
    comments: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jd, "add_comment",
        lambda client, key, text, idem_key=None: comments.append((key, text)),
    )

    result = fc.add_blocked_by(_client(), "OP-866", "OP-858", reason="C7 lands C8")

    assert result is True
    assert created == [{"inward": "OP-858", "outward": "OP-866", "type": "Blocks"}]
    assert comments == [(
        "OP-866",
        f"{fc.ADD_BLOCKED_BY_COMMENT_PREFIX} blockedBy OP-858: C7 lands C8",
    )]


# ─── AC1 idempotent ───────────────────────────────────────────────


def test_add_blocked_by_idempotent_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call returns False without re-posting the link."""
    state = {"exists": False}
    created: list[tuple[str, str]] = []
    monkeypatch.setattr(
        fc, "jira_link_exists",
        lambda client, *, blocked, blocker, link_type="Blocks": state["exists"],
    )

    def fake_create(client, *, inward, outward, link_type="Blocks"):
        state["exists"] = True
        created.append((inward, outward))

    monkeypatch.setattr(fc, "jira_create_issue_link", fake_create)
    monkeypatch.setattr(jd, "add_comment", lambda *a, **kw: None)

    assert fc.add_blocked_by(_client(), "OP-866", "OP-858") is True
    assert fc.add_blocked_by(_client(), "OP-866", "OP-858") is False
    assert created == [("OP-858", "OP-866")]


# ─── AC1 self-reference defensive guard ───────────────────────────


def test_add_blocked_by_self_reference_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fc, "jira_link_exists", lambda *a, **kw: False)
    monkeypatch.setattr(
        fc, "jira_create_issue_link",
        lambda *a, **kw: pytest.fail("create must not be called on self-link"),
    )

    with pytest.raises(fc.BlockedByLinkSelfReference):
        fc.add_blocked_by(_client(), "OP-866", "OP-866")


# ─── AC2 audit detects wrong direction ───────────────────────────


def _wrong_direction_audit_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the audit to see ONE blocked ticket with ONE inverted Blocks link.

    Operator intent (recovered from comment): OP-866 blockedBy OP-858.
    Actual API payload: inwardIssue=OP-866, outwardIssue=OP-858 — INVERTED
    (since inward should be the blocker, not the blocked).
    """
    import scripts.audit_blockedby_directions as audit  # local import

    monkeypatch.setattr(
        audit, "_fetch_open_tickets_with_links",
        lambda client: [
            {
                "key": "OP-866",
                "fields": {
                    "issuelinks": [
                        {
                            "id": "link-1",
                            "type": {"name": "Blocks"},
                            "inwardIssue": {"key": "OP-866"},   # WRONG
                            "outwardIssue": {"key": "OP-858"},  # WRONG
                        }
                    ],
                    "status": {"name": "To Do"},
                },
            }
        ],
    )
    monkeypatch.setattr(
        audit, "_fetch_blocked_ticket_comments",
        lambda client, key: [
            f"{fc.ADD_BLOCKED_BY_COMMENT_PREFIX} blockedBy OP-858: depends on C8"
        ],
    )


def test_audit_detects_wrong_direction_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    import scripts.audit_blockedby_directions as audit

    _wrong_direction_audit_fixture(monkeypatch)
    result = audit.run_audit(_client(), fix=False, dry_run=False)

    assert result.scanned_tickets == 1
    assert result.scanned_links == 1
    assert result.intent_recovered == 1
    assert result.matches == 0
    assert len(result.mismatches) == 1
    mismatch = result.mismatches[0]
    assert mismatch.intent_blocked == "OP-866"
    assert mismatch.intent_blocker == "OP-858"
    assert mismatch.actual_inward == "OP-866"
    assert mismatch.actual_outward == "OP-858"


# ─── AC2 --fix corrects + writes rollback ────────────────────────


def test_audit_fix_corrects_and_writes_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    import scripts.audit_blockedby_directions as audit

    _wrong_direction_audit_fixture(monkeypatch)
    deleted: list[str] = []
    monkeypatch.setattr(audit, "_delete_link", lambda client, link_id: deleted.append(link_id))
    created: list[dict] = []
    monkeypatch.setattr(
        fc, "jira_create_issue_link",
        lambda client, *, inward, outward, link_type="Blocks":
            created.append({"inward": inward, "outward": outward}),
    )

    result = audit.run_audit(_client(), fix=True, dry_run=False)
    report_path, rollback_path = audit.write_artifacts(
        result, fix_applied=True, output_dir=tmp_path,
    )

    assert deleted == ["link-1"]
    assert created == [{"inward": "OP-858", "outward": "OP-866"}]
    assert len(result.fixed) == 1
    assert result.fixed[0]["deleted_link_id"] == "link-1"
    assert result.fixed[0]["restored_blocker_key"] == "OP-858"
    assert result.fixed[0]["restored_blocked_key"] == "OP-866"

    assert report_path.exists()
    assert "Repaired by --fix" in report_path.read_text()
    assert rollback_path is not None
    assert rollback_path.exists()
    assert "deleted_link_id" in rollback_path.read_text()


# ─── AC4 fail-closed env knob ────────────────────────────────────


def test_has_unresolved_blockedby_fail_closed_blocks_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMNISIGHT_BLOCKEDBY_FAIL_CLOSED=1 → exception path returns (True, …)."""
    def raise_oserror(*a, **kw):
        raise OSError("simulated JIRA outage")

    monkeypatch.setattr(fc, "jira_get_blocked_by", raise_oserror)

    monkeypatch.delenv(fc.BLOCKEDBY_FAIL_CLOSED_ENV, raising=False)
    blocked_open, reason_open = fc.has_unresolved_blockedby(_client(), _snapshot("OP-1"))
    assert blocked_open is False
    assert "skipped" in reason_open
    assert "fail-closed" not in reason_open

    monkeypatch.setenv(fc.BLOCKEDBY_FAIL_CLOSED_ENV, "1")
    blocked_closed, reason_closed = fc.has_unresolved_blockedby(_client(), _snapshot("OP-1"))
    assert blocked_closed is True
    assert "fail-closed" in reason_closed


def test_has_unresolved_blockedby_fail_closed_state_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state-lookup exception branch ALSO honours the fail-closed env."""
    monkeypatch.setattr(fc, "jira_get_blocked_by", lambda c, k: ["OP-99"] if k == "OP-1" else [])

    call_count = {"n": 0}

    def maybe_raise(client, key):
        # First call returns blockers; second call (reverse cycle check) raises.
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ["OP-99"]
        raise urllib.error.URLError("simulated")

    # We need the first jira_get_blocked_by call to succeed (return ["OP-99"])
    # and the second to raise. Use a small stateful stub.
    monkeypatch.setattr(fc, "jira_get_blocked_by", maybe_raise)

    monkeypatch.setenv(fc.BLOCKEDBY_FAIL_CLOSED_ENV, "yes")
    blocked, reason = fc.has_unresolved_blockedby(_client(), _snapshot("OP-1"))
    assert blocked is True
    assert "fail-closed" in reason


# ─── AC3 raw /issueLink POST lint rule ───────────────────────────


def test_check_issuelink_post_lint_flags_new_caller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fresh raw POST /issueLink in a non-allowlisted file must fail the lint."""
    sys.path.insert(0, str(REPO_ROOT))
    import scripts.check_issuelink_post_callers as lint

    # Synthesise a fake repo with one offending file.
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    bad_dir = fake_repo / "backend" / "agents"
    bad_dir.mkdir(parents=True)
    (bad_dir / "rogue.py").write_text(
        textwrap.dedent(
            '''
            def wire_link(client, a, b):
                return _request_idempotent(client, "POST", "/issueLink", {"a": a, "b": b}, "idem")
            '''
        ).lstrip()
    )

    monkeypatch.setattr(lint, "REPO_ROOT", fake_repo)
    hits = lint.scan()
    assert len(hits) == 1
    rel, _lineno, line = hits[0]
    assert rel.as_posix() == "backend/agents/rogue.py"
    assert '"POST"' in line and "/issueLink" in line


def test_check_issuelink_post_lint_allows_file_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The allowlisted file_coordinator.py path must NOT trigger the lint."""
    sys.path.insert(0, str(REPO_ROOT))
    import scripts.check_issuelink_post_callers as lint

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    allowed_dir = fake_repo / "backend" / "agents"
    allowed_dir.mkdir(parents=True)
    (allowed_dir / "file_coordinator.py").write_text(
        '_request_idempotent(client, "POST", "/issueLink", {}, "k")\n'
    )

    monkeypatch.setattr(lint, "REPO_ROOT", fake_repo)
    hits = lint.scan()
    assert hits == []


# ─── Bonus: jira_create_issue_link docstring documents the deprecation ─


def test_jira_create_issue_link_docstring_points_to_add_blocked_by() -> None:
    """OP-874 deprecation note must be discoverable from the helper's docstring."""
    doc = (fc.jira_create_issue_link.__doc__ or "").lower()
    assert "deprecated" in doc
    assert "add_blocked_by" in doc
