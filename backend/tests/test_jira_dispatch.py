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


# ── Gerrit push helpers (OP-247 Phase 1) ─────────────────────────


def test_gerrit_auth_mapping_covers_4_classes() -> None:
    """_GERRIT_AUTH_BY_CLASS must include all 4 in-flight agent classes."""
    expected = {"subscription-codex", "subscription-claude", "api-anthropic", "api-openai"}
    assert expected.issubset(set(jd._GERRIT_AUTH_BY_CLASS.keys()))


def test_gerrit_auth_codex_classes_share_codex_bot_key() -> None:
    """subscription-codex + api-openai both use codex-bot key (per memory convention)."""
    sub_user, sub_key = jd._GERRIT_AUTH_BY_CLASS["subscription-codex"]
    api_user, api_key = jd._GERRIT_AUTH_BY_CLASS["api-openai"]
    assert sub_user == api_user == "codex-bot"
    assert sub_key == api_key


def test_gerrit_auth_claude_classes_share_claude_bot_key() -> None:
    """subscription-claude + api-anthropic both use claude-bot key."""
    sub_user, sub_key = jd._GERRIT_AUTH_BY_CLASS["subscription-claude"]
    api_user, api_key = jd._GERRIT_AUTH_BY_CLASS["api-anthropic"]
    assert sub_user == api_user == "claude-bot"
    assert sub_key == api_key


def test_gerrit_ssh_url_for_codex() -> None:
    url = jd._gerrit_ssh_url("subscription-codex")
    assert url == "ssh://codex-bot@sora.services:29418/omnisight/OmniSight-Productizer"


def test_gerrit_ssh_url_for_claude() -> None:
    url = jd._gerrit_ssh_url("subscription-claude")
    assert url == "ssh://claude-bot@sora.services:29418/omnisight/OmniSight-Productizer"


def test_gerrit_ssh_url_unknown_class_falls_back_to_claude() -> None:
    """Unknown agent_class defaults to claude-bot (safer default — less write power)."""
    url = jd._gerrit_ssh_url("local-llm-qwen")
    assert "claude-bot" in url


def test_gerrit_change_url_regex_parses_sample_output() -> None:
    """Real Gerrit push response format from 2026-05-06 cycle 2:
    'remote:   https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/24 docs: ...'
    """
    sample = (
        "remote: Processing changes: refs: 1, new: 1, done\n"
        "remote: \n"
        "remote: SUCCESS\n"
        "remote: \n"
        "remote:   https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/24 "
        "docs: example commit subject [NEW]\n"
        "To ssh://sora.services:29418/omnisight/OmniSight-Productizer\n"
        " * [new reference]     HEAD -> refs/for/develop\n"
    )
    m = jd._GERRIT_CHANGE_URL_RE.search(sample)
    assert m is not None
    assert m.group(1) == "https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/24"
    assert m.group(2) == "24"


def test_gerrit_change_url_regex_no_match_when_push_failed() -> None:
    """Gerrit rejection responses don't contain Change URL."""
    sample = "remote: ERROR: missing Change-Id\n[remote rejected]\n"
    assert jd._GERRIT_CHANGE_URL_RE.search(sample) is None


def test_gerrit_push_result_failure_shape() -> None:
    """GerritPushResult dataclass — failure case has None for change fields."""
    result = jd.GerritPushResult(success=False, change_number=None, change_url=None, detail="error")
    assert not result.success
    assert result.change_number is None


def test_gerrit_push_result_success_shape() -> None:
    """GerritPushResult dataclass — success case has populated fields."""
    result = jd.GerritPushResult(
        success=True,
        change_number=42,
        change_url="https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/42",
        detail="SUCCESS",
    )
    assert result.success
    assert result.change_number == 42
    assert "/+/42" in result.change_url


def test_transition_ids_includes_under_review() -> None:
    """OP-247 Phase 1 added to_under_review = '3' per §10 mapping."""
    assert jd.TRANSITION_IDS["to_under_review"] == "3"


def test_gerrit_constants_match_memory_reference() -> None:
    """Endpoints must match reference_gerrit_self_hosted memory (Track C 2026-05-05)."""
    assert jd.GERRIT_SSH_HOST == "sora.services"
    assert jd.GERRIT_SSH_PORT == 29418
    assert jd.GERRIT_PROJECT_PATH == "omnisight/OmniSight-Productizer"
    assert "29420" in jd.GERRIT_HOOK_URL
    assert jd.GERRIT_HOOK_URL.endswith("/tools/hooks/commit-msg")


# ── Phase 1.5: bot identity + worktree sync (OP-247 follow-up) ──


def test_bot_email_for_codex_class() -> None:
    """Per memory: bot emails are rt3628+<bot-username>@gmail.com."""
    assert jd._bot_email_for("subscription-codex") == "rt3628+codex-bot@gmail.com"
    assert jd._bot_email_for("api-openai") == "rt3628+codex-bot@gmail.com"


def test_bot_email_for_claude_class() -> None:
    assert jd._bot_email_for("subscription-claude") == "rt3628+claude-bot@gmail.com"
    assert jd._bot_email_for("api-anthropic") == "rt3628+claude-bot@gmail.com"


def test_bot_email_for_unknown_falls_back_to_claude() -> None:
    """Unknown class → safe default = claude-bot."""
    assert jd._bot_email_for("local-llm-qwen") == "rt3628+claude-bot@gmail.com"


def test_worktree_sync_result_dataclass_shape() -> None:
    r = jd.WorktreeSyncResult(
        branch_name="feature/OP-99-runner-fresh",
        develop_sha="abc123def456abc123def456abc123def456abc1",
        detail="fresh branch feature/OP-99-runner-fresh at abc123def456",
    )
    assert r.branch_name.startswith("feature/")
    assert len(r.develop_sha) >= 12
    assert "abc123def456" in r.detail


def test_set_bot_identity_calls_git_config_with_bot_email(tmp_path, monkeypatch):
    """set_bot_identity_in_worktree shells out to `git config user.email` + `user.name`."""
    calls = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    jd.set_bot_identity_in_worktree(tmp_path, "subscription-codex")

    # Two calls: user.email + user.name
    assert len(calls) == 2
    email_call = next(c for c in calls if c[2] == "user.email")
    name_call = next(c for c in calls if c[2] == "user.name")
    assert email_call[3] == "rt3628+codex-bot@gmail.com"
    assert name_call[3] == "codex-bot"


def test_ensure_change_ids_rebase_command_shape(tmp_path, monkeypatch):
    """ensure_change_ids invokes `git rebase <base_ref> --exec amend`."""
    calls = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    jd.ensure_change_ids(tmp_path, base_ref="abcdef1234")

    assert len(calls) == 1
    assert calls[0][:3] == ["git", "rebase", "abcdef1234"]
    assert "--exec" in calls[0]
    # The exec command must run `git commit --amend --no-edit` to trigger commit-msg hook
    exec_idx = calls[0].index("--exec") + 1
    assert "commit --amend --no-edit" in calls[0][exec_idx]


def test_sync_to_gerrit_develop_returns_branch_name_with_ticket_key(tmp_path, monkeypatch):
    """sync_to_gerrit_develop returns branch_name = feature/<TICKET-KEY>-runner-fresh."""
    fake_sha = "deadbeef1234deadbeef1234deadbeef12345678"
    call_log = []

    class FakeResult:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        call_log.append(cmd)
        # rev-parse FETCH_HEAD returns fake_sha
        if cmd[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
            return FakeResult(stdout=fake_sha + "\n")
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    result = jd.sync_to_gerrit_develop(tmp_path, "subscription-codex", "OP-42")

    assert result.branch_name == "feature/OP-42-runner-fresh"
    assert result.develop_sha == fake_sha
    assert fake_sha[:12] in result.detail

    # Verify call sequence: fetch → rev-parse → switch → clean
    fetch_calls = [c for c in call_log if c[:2] == ["git", "fetch"]]
    switch_calls = [c for c in call_log if c[:2] == ["git", "switch"]]
    clean_calls = [c for c in call_log if c[:2] == ["git", "clean"]]
    assert len(fetch_calls) == 1
    assert "develop" in fetch_calls[0]
    assert len(switch_calls) == 1
    assert "-C" in switch_calls[0]
    assert f"feature/OP-42-runner-fresh" in switch_calls[0]
    assert fake_sha in switch_calls[0]
    assert len(clean_calls) == 1


def test_sync_to_gerrit_develop_unknown_class_raises() -> None:
    """Unknown agent_class for SSH auth raises ValueError before any git op."""
    with pytest.raises(ValueError, match="unknown agent_class"):
        jd.sync_to_gerrit_develop(Path("/tmp"), "no-such-class", "OP-1")
