"""Contract tests for backend.agents.jira_dispatch.

Per docs/sop/jira-ticket-conventions.md §16. Pins:
- to_snapshot extraction (labels → component / fix_version / mutex)
- parse_prerequisites YAML extraction from description markdown
- _adf_paragraph shape

Network tests (make_client, fetch_pickable_tickets) are skipped when
JIRA credentials absent, so this suite runs offline cleanly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd

JIRA_CREDS_PRESENT = (Path("~/.config/omnisight/jira-claude-token").expanduser()).is_file()


# ── to_snapshot ────────────────────────────────────────────────────


def _fake_issue(
    key: str = "OP-15",
    labels=("class:api-anthropic", "tier:M", "area:backend", "priority:mp"),
    fix_versions=("v0.4.0",),
    summary: str = "MP.W1.2 — quota tracker",
    created: str = "2026-05-06T10:00:00.000+0900",
) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "labels": list(labels),
            "fixVersions": [{"name": v} for v in fix_versions],
            "created": created,
            "components": [],
            "issuetype": {"name": "ストーリー"},
        },
    }


def test_to_snapshot_picks_priority_label_as_component() -> None:
    snap = jd.to_snapshot(_fake_issue())
    assert snap.component == "MP"


def test_to_snapshot_extracts_fix_version() -> None:
    snap = jd.to_snapshot(_fake_issue())
    assert snap.fix_version == "v0.4.0"


def test_to_snapshot_falls_back_to_default_component() -> None:
    snap = jd.to_snapshot(_fake_issue(labels=("tier:S", "area:docs")))
    assert snap.component == "default"


def test_to_snapshot_extracts_mutex_labels() -> None:
    snap = jd.to_snapshot(_fake_issue(labels=("tier:M", "mutex:backend/auth.py", "mutex:alembic-chain-head")))
    assert "mutex:backend/auth.py" in snap.mutex_labels
    assert "mutex:alembic-chain-head" in snap.mutex_labels


def test_to_snapshot_handles_no_fix_version() -> None:
    snap = jd.to_snapshot(_fake_issue(fix_versions=()))
    assert snap.fix_version is None
    assert snap.days_to_fix_version is None


# ── parse_prerequisites ────────────────────────────────────────────


def test_parse_prerequisites_returns_full_schema_when_block_missing() -> None:
    out = jd.parse_prerequisites("## Goal\n\nNo prerequisites here.\n")
    expected_keys = {
        "blocks_on", "soft_prereqs", "mutex_with",
        "schema_locks", "live_state_requires", "external_blockers",
    }
    assert set(out.keys()) == expected_keys
    assert all(out[k] == [] for k in expected_keys)


def test_parse_prerequisites_extracts_blocks_on() -> None:
    desc = """
## Goal
Some goal.

## Prerequisites

```yaml
blocks_on:
  - OP-1234
  - OP-5678
mutex_with:
  - mutex:backend/auth.py
```

## DoD
"""
    out = jd.parse_prerequisites(desc)
    assert out["blocks_on"] == ["OP-1234", "OP-5678"]
    assert out["mutex_with"] == ["mutex:backend/auth.py"]
    # other keys default to []
    assert out["live_state_requires"] == []


def test_parse_prerequisites_extracts_live_state_requires() -> None:
    desc = """
## Prerequisites

```yaml
live_state_requires:
  - alembic_head: "0198"
  - file_exists: "backend/agents/foo.py"
```
"""
    out = jd.parse_prerequisites(desc)
    assert out["live_state_requires"] == [
        {"alembic_head": "0198"},
        {"file_exists": "backend/agents/foo.py"},
    ]


def test_parse_prerequisites_returns_empty_on_malformed_yaml() -> None:
    desc = """
## Prerequisites

```yaml
blocks_on:
  - OP-1234
  invalid: : :
```
"""
    out = jd.parse_prerequisites(desc)
    # Malformed YAML → empty dict (caller treats as fail-safe)
    assert out == {}


# ── ADF helper ─────────────────────────────────────────────────────


def test_adf_paragraph_shape() -> None:
    adf = jd._adf_paragraph("hello")
    assert adf["type"] == "doc"
    assert adf["version"] == 1
    assert adf["content"][0]["type"] == "paragraph"
    assert adf["content"][0]["content"][0]["text"] == "hello"


# ── Network-dependent tests ────────────────────────────────────────


@pytest.mark.skipif(
    not JIRA_CREDS_PRESENT,
    reason="JIRA Claude bot credentials not present (CI runner without secrets)",
)
def test_make_client_authenticates() -> None:
    client = jd.make_client("subscription-claude")
    assert client.bot_account_id
    assert client.base_url.endswith("/rest/api/3")
    assert client.project_key == "OP"


@pytest.mark.skipif(
    not JIRA_CREDS_PRESENT,
    reason="JIRA Claude bot credentials not present (CI runner without secrets)",
)
def test_fetch_pickable_tickets_returns_list() -> None:
    client = jd.make_client("subscription-codex")
    issues = jd.fetch_pickable_tickets(client, max_results=5)
    # May be empty if no class:subscription-codex tickets in TODO state
    assert isinstance(issues, list)
    for i in issues:
        assert "key" in i
        labels = i["fields"]["labels"]
        assert "class:subscription-codex" in labels
