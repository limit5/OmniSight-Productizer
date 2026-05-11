"""OP-873 JIRA stale-ticket audit tests."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "jira_stale_audit.py"


def _load_script():
    sys.modules.pop("jira_stale_audit", None)
    spec = importlib.util.spec_from_file_location("jira_stale_audit", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["jira_stale_audit"] = module
    spec.loader.exec_module(module)
    return module


class _Client:
    agent_class = "subscription-codex"
    base_url = "https://jira.example/rest/api/3"
    auth_header = "Basic x"
    project_key = "OP"
    bot_account_id = "acct-1"


def _issue(
    key: str,
    created: str,
    labels: list[str],
    *,
    summary: str = "stale ticket",
    fix_versions: list[dict] | None = None,
) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "created": created,
            "labels": labels,
            "fixVersions": fix_versions or [],
        },
    }


def test_query_happy_path_writes_breakdowns_and_dashboard(tmp_path, monkeypatch) -> None:
    mod = _load_script()
    now = datetime(2026, 5, 11, tzinfo=timezone.utc)
    calls: list[tuple[str, str, dict]] = []

    (tmp_path / "jira-stale-tickets-2026-05-04.md").write_text(
        "Total stale tickets: 1\n",
        encoding="utf-8",
    )

    def fake_request(client, method, path, body):
        calls.append((method, path, body))
        return {
            "issues": [
                _issue("OP-1", "2026-03-01T00:00:00.000+0000", ["class:subscription-claude", "tier:M", "sprint:A"]),
                _issue("OP-2", "2026-03-10T00:00:00.000+0000", ["class:subscription-codex", "tier:S"], fix_versions=[{"name": "Sprint B"}]),
                _issue("OP-3", "2026-04-01T00:00:00.000+0000", []),
            ]
        }

    monkeypatch.setattr(mod.jd, "_request", fake_request)
    monkeypatch.setattr(mod.jd, "add_comment", lambda *args, **kwargs: None)

    path, commented = mod.run(client=_Client(), now=now, output_dir=tmp_path, dry_run=True)

    report = path.read_text(encoding="utf-8")
    assert "created < -30d" in calls[0][2]["jql"]
    assert "Dashboard: total stale tickets=3; delta_vs_last_week=+2" in report
    assert "- claude: 1" in report
    assert "- codex: 1" in report
    assert "- unassigned: 1" in report
    assert "- M: 1" in report
    assert "- Sprint B: 1" in report
    assert (tmp_path / "jira-stale-tickets-latest.md").is_symlink()
    assert commented == ()


def test_sixty_day_ticket_gets_auto_comment(tmp_path, monkeypatch) -> None:
    mod = _load_script()
    now = datetime(2026, 5, 11, tzinfo=timezone.utc)
    comments: list[tuple[str, str]] = []
    ticket = mod.StaleTicket(
        key="OP-60",
        summary="old",
        created=datetime(2026, 3, 1, tzinfo=timezone.utc),
        labels=(),
        tier="unassigned",
        sprint="unassigned",
        agent_class="unassigned",
    )

    monkeypatch.setattr(mod, "fetch_stale_tickets", lambda client: (ticket,))
    monkeypatch.setattr(mod.jd, "add_comment", lambda client, key, text: comments.append((key, text)))

    _, commented = mod.run(client=_Client(), now=now, output_dir=tmp_path)

    assert commented == ("OP-60",)
    assert comments == [("OP-60", mod.AUTO_COMMENT_TEXT)]


def test_thirty_day_ticket_under_sixty_days_does_not_comment(tmp_path, monkeypatch) -> None:
    mod = _load_script()
    now = datetime(2026, 5, 11, tzinfo=timezone.utc)
    comments: list[str] = []
    ticket = mod.StaleTicket(
        key="OP-45",
        summary="middle",
        created=datetime(2026, 3, 27, tzinfo=timezone.utc),
        labels=(),
        tier="unassigned",
        sprint="unassigned",
        agent_class="unassigned",
    )

    monkeypatch.setattr(mod, "fetch_stale_tickets", lambda client: (ticket,))
    monkeypatch.setattr(mod.jd, "add_comment", lambda client, key, text: comments.append(key))

    _, commented = mod.run(client=_Client(), now=now, output_dir=tmp_path)

    assert commented == ()
    assert comments == []


def test_rate_limit_backoff_retries_query(monkeypatch) -> None:
    mod = _load_script()
    attempts = {"count": 0}
    sleeps: list[float] = []

    def fake_request(client, method, path, body):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("429 rate limit")
        return {"issues": []}

    monkeypatch.setattr(mod.jd, "_request", fake_request)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert mod.fetch_stale_tickets(_Client()) == ()
    assert attempts["count"] == 3
    assert sleeps == [1.0, 2.0]
