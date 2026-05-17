"""OP-1442 — JIRA dispatch integration for feature-dup warnings."""

from __future__ import annotations

from backend.agents import feature_dup_detector as fdd
from backend.agents import jira_dispatch as jd
from backend.agents.scheduler import TicketSnapshot


def _client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


def _snapshot(key: str, labels: tuple[str, ...] = ()) -> TicketSnapshot:
    return TicketSnapshot(
        key=key,
        component="default",
        fix_version=None,
        created_at="2026-05-08T00:00:00.000+0000",
        days_since_created=0.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=labels,
    )


def test_post_feature_dup_warnings_cross_links_both_tickets(monkeypatch) -> None:
    comments: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        jd,
        "add_comment",
        lambda _client, key, text, idem_key=None: comments.append(
            (key, text, idem_key)
        ),
    )
    open_signals = [
        fdd.PatchSignal(
            ticket_key="OP-1442",
            files_changed=frozenset({"backend/agents/feature_dup_detector.py"}),
            added_symbols=frozenset({"detect_duplicate_feature"}),
        ),
        fdd.PatchSignal(
            ticket_key="OP-1439",
            change_number="912",
            files_changed=frozenset({"backend/agents/ops_audit.py"}),
            added_symbols=frozenset({"detect_duplicate_feature"}),
        ),
    ]

    hits = jd.post_feature_dup_warnings(
        _client(),
        _snapshot("OP-1442", labels=("area:backend",)),
        description="feature duplication detector",
        open_signals=open_signals,
    )

    assert len(hits) == 1
    assert [key for key, _text, _idem in comments] == ["OP-1442", "OP-1439"]
    assert all("[feature-dup-warning]" in text for _key, text, _idem in comments)
    assert "possible overlap with OP-1439" in comments[0][1]
    assert "possible overlap with OP-1442" in comments[1][1]
    assert comments[0][2] == comments[1][2].replace("-other", "-current")
