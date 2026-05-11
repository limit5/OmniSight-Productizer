"""Contract tests for backend.agents.jira_dispatch.

Per docs/sop/jira-ticket-conventions.md §16. Pins:
- to_snapshot extraction (labels → component / fix_version / mutex)
- parse_prerequisites YAML extraction from description markdown
- _adf_paragraph shape

Network tests (make_client, fetch_pickable_tickets) are skipped when
JIRA credentials absent, so this suite runs offline cleanly.
"""
from __future__ import annotations

import subprocess
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
    """set_bot_identity_in_worktree shells out to per-worktree git config."""
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
    email_call = next(c for c in calls if c[3] == "user.email")
    name_call = next(c for c in calls if c[3] == "user.name")
    assert email_call[:3] == ["git", "config", "--worktree"]
    assert name_call[:3] == ["git", "config", "--worktree"]
    assert email_call[4] == "rt3628+codex-bot@gmail.com"
    assert name_call[4] == "codex-bot"


def test_assert_worktree_config_enabled_fails_when_disabled(tmp_path) -> None:
    """Startup guard exits with remediation commands when worktree config is disabled."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)

    with pytest.raises(SystemExit) as excinfo:
        jd.assert_worktree_config_enabled(repo)

    message = str(excinfo.value)
    assert "extensions.worktreeConfig is not enabled" in message
    assert f"git -C {repo} config core.repositoryformatversion 1" in message
    assert f"git -C {repo} config extensions.worktreeConfig true" in message


def test_interleaved_worktrees_commit_and_push_with_correct_email(tmp_path) -> None:
    """Regression: shared config writes cannot corrupt commits from sibling worktrees."""
    remote = tmp_path / "origin.git"
    main = tmp_path / "main"
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    main.mkdir()

    subprocess.run(["git", "init"], cwd=main, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "operator@example.test"],
        cwd=main, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "operator"],
        cwd=main, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "seed"],
        cwd=main, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=main, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/main"],
        cwd=main, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "core.repositoryformatversion", "1"],
        cwd=main, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "extensions.worktreeConfig", "true"],
        cwd=main, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "claude-work", str(claude)],
        cwd=main, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "codex-work", str(codex)],
        cwd=main, check=True, capture_output=True, text=True,
    )

    jd.set_bot_identity_in_worktree(claude, "subscription-claude")
    jd.set_bot_identity_in_worktree(codex, "subscription-codex")

    subprocess.run(
        ["git", "config", "user.email", "wrong-shared@example.test"],
        cwd=main, check=True, capture_output=True, text=True,
    )

    claude_email = subprocess.run(
        ["git", "config", "user.email"],
        cwd=claude, check=True, capture_output=True, text=True,
    ).stdout.strip()
    codex_email = subprocess.run(
        ["git", "config", "user.email"],
        cwd=codex, check=True, capture_output=True, text=True,
    ).stdout.strip()

    assert claude_email == "rt3628+claude-bot@gmail.com"
    assert codex_email == "rt3628+codex-bot@gmail.com"

    (claude / "claude.txt").write_text("claude\n")
    subprocess.run(["git", "add", "claude.txt"], cwd=claude, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "claude work"], cwd=claude, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/claude-work"],
        cwd=claude, check=True, capture_output=True, text=True,
    )

    (codex / "codex.txt").write_text("codex\n")
    subprocess.run(["git", "add", "codex.txt"], cwd=codex, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "codex work"], cwd=codex, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/codex-work"],
        cwd=codex, check=True, capture_output=True, text=True,
    )

    claude_commit_email = subprocess.run(
        ["git", "log", "-1", "--format=%ae %ce"],
        cwd=claude, check=True, capture_output=True, text=True,
    ).stdout.strip()
    codex_commit_email = subprocess.run(
        ["git", "log", "-1", "--format=%ae %ce"],
        cwd=codex, check=True, capture_output=True, text=True,
    ).stdout.strip()

    assert claude_commit_email == "rt3628+claude-bot@gmail.com rt3628+claude-bot@gmail.com"
    assert codex_commit_email == "rt3628+codex-bot@gmail.com rt3628+codex-bot@gmail.com"


def _fake_passing_preconditions_run(cmd, **kwargs):
    """OP-827: stub subprocess.run that satisfies ``ensure_change_ids``'s
    two new preconditions (rev-list reports ≥1 commit, status is clean)
    so the unit tests below can focus on rebase-shape / cleanup behaviour
    without spinning up a real git repo.
    """
    class FakeResult:
        returncode = 0
        stderr = ""

    if cmd[:2] == ["git", "rev-list"] and "--count" in cmd:
        result = FakeResult()
        result.stdout = "1\n"  # at least one commit beyond base_ref
        return result
    if cmd[:3] == ["git", "rev-parse", "HEAD"]:
        result = FakeResult()
        result.stdout = "deadbeefcafe\n"
        return result
    if cmd[:2] == ["git", "status"]:
        result = FakeResult()
        result.stdout = ""  # clean tree
        return result
    result = FakeResult()
    result.stdout = ""
    return result


def test_ensure_change_ids_rebase_command_shape(tmp_path, monkeypatch):
    """ensure_change_ids invokes `git rebase <base_ref> --keep-empty --exec amend`."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _fake_passing_preconditions_run(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", fake_run)
    jd.ensure_change_ids(tmp_path, base_ref="abcdef1234")

    # Filter out OP-827 precondition probes; assert only on the rebase invocation.
    rebase_calls = [c for c in calls if c[:2] == ["git", "rebase"]]
    assert len(rebase_calls) == 1
    assert rebase_calls[0][:3] == ["git", "rebase", "abcdef1234"]
    assert "--keep-empty" in rebase_calls[0]
    assert "--exec" in rebase_calls[0]
    # The exec command must run `git commit --amend --no-edit` to trigger commit-msg hook
    exec_idx = rebase_calls[0].index("--exec") + 1
    assert "commit --amend --no-edit" in rebase_calls[0][exec_idx]


def test_ensure_change_ids_quits_rebase_on_failure(tmp_path, monkeypatch):
    """OP-736: failed rebase/amend cleanup must quit half-open rebase state."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rebase"] and "--exec" in cmd:
            raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr="paused")
        return _fake_passing_preconditions_run(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        jd.ensure_change_ids(tmp_path, base_ref="abcdef1234")

    # Locate the rebase invocation among the precondition probes.
    rebase_main = next(
        c for c in calls if c[:2] == ["git", "rebase"] and "--exec" in c
    )
    assert rebase_main[:3] == ["git", "rebase", "abcdef1234"]
    assert "--keep-empty" in rebase_main
    # Cleanup `git rebase --quit` must run after the failed rebase.
    assert ["git", "rebase", "--quit"] in calls


@pytest.mark.parametrize(
    ("artifact", "cleanup_cmd"),
    [
        ("rebase-merge", ["git", "rebase", "--quit"]),
        ("rebase-apply", ["git", "rebase", "--quit"]),
        ("CHERRY_PICK_HEAD", ["git", "cherry-pick", "--abort"]),
        ("MERGE_HEAD", ["git", "merge", "--abort"]),
        ("BISECT_LOG", ["git", "bisect", "reset"]),
        ("REVERT_HEAD", ["git", "revert", "--abort"]),
    ],
)
def test_assert_worktree_clean_recovers_each_state_artifact(
    tmp_path, monkeypatch, artifact, cleanup_cmd,
):
    """OP-736: every known git mid-operation artifact maps to cleanup."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    artifact_path = git_dir / artifact
    if artifact.startswith("rebase-"):
        artifact_path.mkdir()
    else:
        artifact_path.write_text("state\n")
    calls = []

    class FakeResult:
        def __init__(self, stdout="", returncode=0, stderr=""):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["git", "-C", str(tmp_path), "rev-parse"]:
            return FakeResult(stdout=".git\n")
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    jd.assert_worktree_clean(tmp_path)

    assert cleanup_cmd in calls


def test_assert_worktree_clean_clean_baseline_is_noop(tmp_path, monkeypatch, caplog):
    """Clean worktree: no cleanup command and no warning/info log spam."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    calls = []

    class FakeResult:
        def __init__(self, stdout="", returncode=0, stderr=""):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult(stdout=".git\n")

    monkeypatch.setattr("subprocess.run", fake_run)
    jd.assert_worktree_clean(tmp_path)

    assert calls == [["git", "-C", str(tmp_path), "rev-parse", "--git-dir"]]
    assert caplog.records == []


def test_assert_worktree_clean_unrecoverable_failure_raises(tmp_path, monkeypatch):
    """Cleanup failure must be actionable and visible to the caller."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "MERGE_HEAD").write_text("state\n")

    class FakeResult:
        def __init__(self, stdout="", returncode=0, stderr=""):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["git", "-C", str(tmp_path), "rev-parse"]:
            return FakeResult(stdout=".git\n")
        return FakeResult(returncode=2, stderr="merge abort failed")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="MERGE_HEAD cleanup failed.*Manual fix needed"):
        jd.assert_worktree_clean(tmp_path)


def test_assert_worktree_clean_rebase_leak_allows_next_switch(tmp_path):
    """Synthetic OP-736 regression: stale rebase dir is cleared before switch -C."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=tester", "-c", "user.email=tester@example.invalid",
            "commit", "-m", "init",
        ],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    (tmp_path / ".git" / "rebase-merge").mkdir()

    jd.assert_worktree_clean(tmp_path)
    result = subprocess.run(
        ["git", "switch", "-C", "feature/OP-736-runner-fresh", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".git" / "rebase-merge").exists()


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
        if cmd[:4] == ["git", "-C", str(tmp_path), "rev-parse"]:
            return FakeResult(stdout=".git\n")
        # rev-parse FETCH_HEAD returns fake_sha
        if cmd[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
            return FakeResult(stdout=fake_sha + "\n")
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    result = jd.sync_to_gerrit_develop(tmp_path, "subscription-codex", "OP-42")

    assert result.branch_name == "feature/OP-42-runner-fresh"
    assert result.develop_sha == fake_sha
    assert fake_sha[:12] in result.detail

    # Verify pre-sync guard is first, then fetch → rev-parse → switch → reset → clean.
    assert call_log[0] == ["git", "-C", str(tmp_path), "rev-parse", "--git-dir"]
    fetch_calls = [c for c in call_log if c[:2] == ["git", "fetch"]]
    switch_calls = [c for c in call_log if c[:2] == ["git", "switch"]]
    reset_calls = [c for c in call_log if c[:2] == ["git", "reset"]]
    clean_calls = [c for c in call_log if c[:2] == ["git", "clean"]]
    assert len(fetch_calls) == 1
    assert "develop" in fetch_calls[0]
    assert len(switch_calls) == 1
    assert "-C" in switch_calls[0]
    assert f"feature/OP-42-runner-fresh" in switch_calls[0]
    assert fake_sha in switch_calls[0]
    # OP-796: reset --hard must run between switch and clean to discard
    # unstaged tracked-file edits (git switch -C alone preserves them).
    assert len(reset_calls) == 1
    assert reset_calls[0] == ["git", "reset", "--hard", fake_sha]
    assert len(clean_calls) == 1


def test_sync_to_gerrit_develop_unknown_class_raises(monkeypatch) -> None:
    """Unknown agent_class for SSH auth raises ValueError after pre-sync guard."""
    monkeypatch.setattr(jd, "assert_worktree_clean", lambda worktree_path: None)
    with pytest.raises(ValueError, match="unknown agent_class"):
        jd.sync_to_gerrit_develop(Path("/tmp"), "no-such-class", "OP-1")


def test_pre_pickup_ok_signature_accepts_worktree_path() -> None:
    """L17 refactor: pre_pickup_ok accepts optional worktree_path kwarg."""
    import inspect
    sig = inspect.signature(jd.pre_pickup_ok)
    params = list(sig.parameters.keys())
    assert "worktree_path" in params, f"pre_pickup_ok must take worktree_path; got params {params}"


def test_pre_pickup_ok_default_worktree_path_is_none() -> None:
    """Backward-compatible default — pre_pickup_ok(client, snapshot) without worktree still works."""
    import inspect
    sig = inspect.signature(jd.pre_pickup_ok)
    assert sig.parameters["worktree_path"].default is None


# ── OP-687: mutex enforcement at pre-pickup ───────────────────────


def _fake_dispatch_client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


def _snapshot(
    key: str = "OP-555",
    mutex: tuple = ("mutex:backend/foo.py",),
    labels: tuple[str, ...] = (),
):
    from backend.agents.scheduler import TicketSnapshot
    return TicketSnapshot(
        key=key,
        component="default",
        fix_version=None,
        created_at="2026-05-08T00:00:00.000+0000",
        days_since_created=0.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=mutex,
        has_mutex_in_progress_sibling=False,
        labels=labels,
    )


def test_mutex_holding_statuses_include_in_progress_and_under_review() -> None:
    """OP-687: holding window covers both In Progress and Under Review."""
    assert "In Progress" in jd.MUTEX_HOLDING_STATUSES
    assert "Under Review" in jd.MUTEX_HOLDING_STATUSES
    assert "Approved" not in jd.MUTEX_HOLDING_STATUSES
    assert "Archived" not in jd.MUTEX_HOLDING_STATUSES


def test_find_mutex_holders_empty_labels_skips_jql(monkeypatch) -> None:
    """No mutex_with declared → no JQL call (no-op fast path)."""
    called: list = []
    monkeypatch.setattr(jd, "_request", lambda *a, **kw: called.append(1) or {"issues": []})
    holders = jd.find_mutex_holders(_fake_dispatch_client(), [], exclude_key="OP-555")
    assert holders == []
    assert called == []


def test_find_mutex_holders_jql_excludes_self_and_filters_holding_statuses(monkeypatch) -> None:
    """JQL must scope to project, holding statuses, mutex labels (OR'd), exclude self."""
    captured: dict = {}

    def fake_request(client, method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"issues": []}

    monkeypatch.setattr(jd, "_request", fake_request)
    jd.find_mutex_holders(
        _fake_dispatch_client(),
        ["mutex:backend/foo.py", "mutex:alembic-chain-head"],
        exclude_key="OP-555",
    )
    assert captured["method"] == "POST"
    assert captured["path"] == "/search/jql"
    jql = captured["body"]["jql"]
    assert 'project = "OP"' in jql
    assert 'key != "OP-555"' in jql
    assert 'mutex:backend/foo.py' in jql
    assert 'mutex:alembic-chain-head' in jql
    assert ' OR ' in jql
    assert '"In Progress"' in jql and '"Under Review"' in jql


def test_pre_pickup_ok_blocks_when_mutex_held_by_in_progress_sibling(monkeypatch) -> None:
    """Operator concern 2026-05-07: codex picks OP-A (mutex:foo) at 02:00,
    claude picks OP-B (mutex:foo) at 03:00 — claude must NOT pick up.
    """
    desc = (
        "## Goal\nfoo\n\n"
        "## Prerequisites\n\n"
        "```yaml\n"
        "mutex_with:\n"
        "  - mutex:backend/foo.py\n"
        "```\n"
    )
    monkeypatch.setattr(jd, "fetch_description", lambda c, k: desc)
    monkeypatch.setattr(jd, "migration_freeze_check", lambda c, s, description=None: (True, "no freeze"))
    monkeypatch.setattr(
        jd, "find_mutex_holders",
        lambda c, m, exclude_key: [{
            "key": "OP-100",
            "fields": {
                "status": {"name": "In Progress"},
                "labels": ["mutex:backend/foo.py", "class:subscription-codex"],
            },
        }],
    )
    ok, reason = jd.pre_pickup_ok(_fake_dispatch_client(), _snapshot(key="OP-555"))
    assert ok is False
    assert reason.startswith("mutex conflict")
    assert "mutex:backend/foo.py" in reason
    assert "OP-100" in reason
    assert "In Progress" in reason


def test_pre_pickup_ok_releases_pickup_when_holder_transitions_to_approved(monkeypatch) -> None:
    """Once the first ticket transitions Under Review → Approved (a non-holding
    status), the JQL returns no holders and the second ticket can be picked up.
    """
    desc = (
        "## Prerequisites\n\n"
        "```yaml\n"
        "mutex_with:\n"
        "  - mutex:backend/foo.py\n"
        "```\n"
    )
    monkeypatch.setattr(jd, "fetch_description", lambda c, k: desc)
    monkeypatch.setattr(jd, "migration_freeze_check", lambda c, s, description=None: (True, "no freeze"))
    # JQL excludes Approved (not in MUTEX_HOLDING_STATUSES) → empty holder list
    monkeypatch.setattr(jd, "find_mutex_holders", lambda c, m, exclude_key: [])
    ok, reason = jd.pre_pickup_ok(_fake_dispatch_client(), _snapshot(key="OP-555"))
    assert ok is True
    assert "passed" in reason


def test_pre_pickup_ok_skips_mutex_check_when_no_mutex_with_declared(monkeypatch) -> None:
    """No mutex_with in Prerequisites → find_mutex_holders is never called."""
    desc = "## Goal\nNo prereqs section here.\n"
    monkeypatch.setattr(jd, "fetch_description", lambda c, k: desc)
    monkeypatch.setattr(jd, "migration_freeze_check", lambda c, s, description=None: (True, "no freeze"))
    holder_calls: list = []
    monkeypatch.setattr(
        jd, "find_mutex_holders",
        lambda c, m, exclude_key: holder_calls.append((m, exclude_key)) or [],
    )
    ok, _ = jd.pre_pickup_ok(_fake_dispatch_client(), _snapshot(mutex=()))
    assert ok is True
    assert holder_calls == []


# ── OP-752: migration freeze at pre-pickup ────────────────────────


def test_find_active_migrations_queries_in_flight_scope_labels(monkeypatch) -> None:
    captured: dict = {}

    def fake_request(client, method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {
            "issues": [{
                "key": "OP-747",
                "fields": {
                    "labels": [
                        jd.MIGRATION_IN_FLIGHT_LABEL,
                        "migration:scope=docs/sop/lessons/*",
                    ],
                },
            }],
        }

    monkeypatch.setattr(jd, "_request", fake_request)
    migrations = jd.find_active_migrations(_fake_dispatch_client(), exclude_key="OP-752")

    assert migrations == [jd.MigrationFreeze("OP-747", ("docs/sop/lessons/*",))]
    assert captured["method"] == "POST"
    assert captured["path"] == "/search/jql"
    jql = captured["body"]["jql"]
    assert f'labels = "{jd.MIGRATION_IN_FLIGHT_LABEL}"' in jql
    assert 'status not in ("Published", "公開済み", "Archived")' in jql
    assert 'key != "OP-752"' in jql
    assert captured["body"]["fields"] == ["labels", "status"]


def test_pre_pickup_ok_blocks_ticket_touching_active_migration_scope(monkeypatch) -> None:
    desc = (
        "## Goal\nAdd lesson.\n\n"
        "## Files / Paths\n"
        "- docs/sop/lessons/L-OP-752-freeze.md\n"
    )
    comments: list[tuple[str, str, str | None]] = []

    monkeypatch.setattr(jd, "fetch_description", lambda c, k: desc)
    monkeypatch.setattr(
        jd,
        "find_active_migrations",
        lambda c, exclude_key: [jd.MigrationFreeze("OP-747", ("docs/sop/lessons/*",))],
    )
    monkeypatch.setattr(
        jd,
        "add_comment",
        lambda c, k, text, idem_key=None: comments.append((k, text, idem_key)),
    )

    ok, reason = jd.pre_pickup_ok(_fake_dispatch_client(), _snapshot(key="OP-746"))

    assert ok is False
    assert reason == "migration freeze: OP-747 overlaps docs/sop/lessons/*"
    assert comments == [(
        "OP-746",
        "[runner-migration-freeze] paused — waiting on OP-747 migration to complete",
        "migration-freeze-OP-746-OP-747",
    )]


def test_migration_freeze_releases_after_migration_no_longer_active(monkeypatch) -> None:
    desc = (
        "## Files / Paths\n"
        "- docs/sop/lessons/L-OP-752-freeze.md\n"
    )
    comments: list[str] = []
    monkeypatch.setattr(jd, "find_active_migrations", lambda c, exclude_key: [])
    monkeypatch.setattr(jd, "add_comment", lambda c, k, text, idem_key=None: comments.append(text))

    ok, reason = jd.migration_freeze_check(
        _fake_dispatch_client(),
        _snapshot(key="OP-746"),
        description=desc,
    )

    assert ok is True
    assert reason == "no active migration freeze"
    assert comments == []


def test_migration_override_label_bypasses_and_audits(monkeypatch) -> None:
    desc = (
        "## Files / Paths\n"
        "- docs/sop/lessons/L-OP-752-freeze.md\n"
    )
    comments: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        jd,
        "find_active_migrations",
        lambda c, exclude_key: [jd.MigrationFreeze("OP-747", ("docs/sop/lessons/*",))],
    )
    monkeypatch.setattr(
        jd,
        "add_comment",
        lambda c, k, text, idem_key=None: comments.append((k, text, idem_key)),
    )

    ok, reason = jd.migration_freeze_check(
        _fake_dispatch_client(),
        _snapshot(key="OP-746", labels=(jd.MIGRATION_OVERRIDE_LABEL,)),
        description=desc,
    )

    assert ok is True
    assert reason == "migration override: OP-747 overlaps docs/sop/lessons/*"
    assert comments == [(
        "OP-746",
        "[runner-migration-override] migration:override bypassed OP-747 freeze for docs/sop/lessons/*.",
        "migration-override-OP-746-OP-747",
    )]


# ── OP-691: Phase 1.5 transition idempotency ──────────────────────


def test_get_issue_status_extracts_name_field(monkeypatch) -> None:
    """get_issue_status returns issue.fields.status.name as a string."""
    captured: dict = {}

    def fake_request(client, method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        return {"fields": {"status": {"name": "Under Review"}}}

    monkeypatch.setattr(jd, "_request", fake_request)
    name = jd.get_issue_status(_fake_dispatch_client(), "OP-691")
    assert name == "Under Review"
    assert captured["method"] == "GET"
    assert captured["path"] == "/issue/OP-691?fields=status"


def test_get_issue_status_returns_empty_when_status_missing(monkeypatch) -> None:
    """Defensive: malformed JIRA payload (no status) → empty string, no crash."""
    monkeypatch.setattr(jd, "_request", lambda *a, **kw: {"fields": {}})
    assert jd.get_issue_status(_fake_dispatch_client(), "OP-691") == ""


def test_under_review_status_name_constant() -> None:
    """The runner's idempotency check pins on this exact string. Don't drift."""
    assert jd.UNDER_REVIEW_STATUS_NAME == "Under Review"


def test_post_runner_pushed_comment_calls_add_comment_with_url(monkeypatch) -> None:
    """post_runner_pushed_comment is a thin wrapper over add_comment; the
    body must contain the [runner-pushed-to-gerrit] tag and the URL."""
    captured: dict = {}

    def fake_add_comment(client, key, text):
        captured["key"] = key
        captured["text"] = text

    monkeypatch.setattr(jd, "add_comment", fake_add_comment)
    jd.post_runner_pushed_comment(
        _fake_dispatch_client(), "OP-691",
        "https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/42",
    )
    assert captured["key"] == "OP-691"
    assert "[runner-pushed-to-gerrit]" in captured["text"]
    assert "/+/42" in captured["text"]


def test_post_runner_pushed_comment_does_not_call_transitions(monkeypatch) -> None:
    """Splitting from transition_to_under_review (OP-691): the comment
    helper must NOT POST /transitions — that's now a separate concern."""
    transition_calls: list = []

    def fake_request(client, method, path, body=None, idem_key=None):
        if "/transitions" in path:
            transition_calls.append((method, path))
        return {}

    monkeypatch.setattr(jd, "_request", fake_request)
    jd.post_runner_pushed_comment(_fake_dispatch_client(), "OP-691", "https://x/+/1")
    assert transition_calls == []


def test_transition_to_under_review_if_needed_skips_when_already_under_review(monkeypatch) -> None:
    """Idempotency: if status already Under Review, no transition POST is made
    and the function returns False."""
    transition_calls: list = []

    monkeypatch.setattr(jd, "get_issue_status", lambda c, k: "Under Review")

    def fake_request(client, method, path, body=None):
        if "/transitions" in path:
            transition_calls.append((method, path, body))
        return {}

    monkeypatch.setattr(jd, "_request", fake_request)
    transitioned = jd.transition_to_under_review_if_needed(_fake_dispatch_client(), "OP-691")
    assert transitioned is False
    assert transition_calls == []


def test_transition_to_under_review_if_needed_transitions_when_in_progress(monkeypatch) -> None:
    """Happy path: status In Progress → POST /transitions with id 3, returns True."""
    transition_calls: list = []

    monkeypatch.setattr(jd, "get_issue_status", lambda c, k: "In Progress")

    def fake_request(client, method, path, body=None, idem_key=None):
        if "/transitions" in path:
            transition_calls.append((method, path, body))
        return {}

    monkeypatch.setattr(jd, "_request", fake_request)
    transitioned = jd.transition_to_under_review_if_needed(_fake_dispatch_client(), "OP-691")
    assert transitioned is True
    assert len(transition_calls) == 1
    method, path, body = transition_calls[0]
    assert method == "POST"
    assert path == "/issue/OP-691/transitions"
    assert body["transition"]["id"] == jd.TRANSITION_IDS["to_under_review"]


def test_transition_to_under_review_wrapper_idempotent_when_already_under_review(monkeypatch) -> None:
    """Backward-compat wrapper: if ticket is already Under Review, neither
    the comment nor the transition POST fires (OP-691 audit-trail hygiene)."""
    monkeypatch.setattr(jd, "get_issue_status", lambda c, k: "Under Review")
    comment_calls: list = []
    transition_calls: list = []
    monkeypatch.setattr(jd, "post_runner_pushed_comment",
                        lambda c, k, url: comment_calls.append((k, url)))
    monkeypatch.setattr(jd, "transition_to_under_review_if_needed",
                        lambda c, k: transition_calls.append(k) or True)

    jd.transition_to_under_review(_fake_dispatch_client(), "OP-691", "https://x/+/1")
    assert comment_calls == []
    assert transition_calls == []


def test_transition_to_under_review_wrapper_posts_comment_and_transitions_in_progress(monkeypatch) -> None:
    """Backward-compat wrapper happy path: In Progress ticket → comment posted
    AND transition_to_under_review_if_needed called."""
    monkeypatch.setattr(jd, "get_issue_status", lambda c, k: "In Progress")
    calls: list = []
    monkeypatch.setattr(jd, "post_runner_pushed_comment",
                        lambda c, k, url, idem_key=None: calls.append(("comment", k, url)))
    monkeypatch.setattr(jd, "transition_to_under_review_if_needed",
                        lambda c, k, idem_key=None: calls.append(("transition", k)) or True)

    jd.transition_to_under_review(_fake_dispatch_client(), "OP-691",
                                  "https://sora.services:29420/c/x/+/42")
    assert ("comment", "OP-691", "https://sora.services:29420/c/x/+/42") in calls
    assert ("transition", "OP-691") in calls
    # Comment must precede transition (so the transition timestamp matches the URL post)
    assert calls.index(("comment", "OP-691", "https://sora.services:29420/c/x/+/42")) < \
           calls.index(("transition", "OP-691"))


def test_pre_pickup_ok_mutex_reason_lists_each_blocking_sibling(monkeypatch) -> None:
    """When multiple holders share the mutex, reason lists all of them."""
    desc = (
        "## Prerequisites\n\n"
        "```yaml\n"
        "mutex_with:\n"
        "  - mutex:backend/foo.py\n"
        "  - mutex:alembic-chain-head\n"
        "```\n"
    )
    monkeypatch.setattr(jd, "fetch_description", lambda c, k: desc)
    monkeypatch.setattr(jd, "migration_freeze_check", lambda c, s, description=None: (True, "no freeze"))
    monkeypatch.setattr(
        jd, "find_mutex_holders",
        lambda c, m, exclude_key: [
            {"key": "OP-100", "fields": {"status": {"name": "In Progress"},
                                          "labels": ["mutex:backend/foo.py"]}},
            {"key": "OP-101", "fields": {"status": {"name": "Under Review"},
                                          "labels": ["mutex:alembic-chain-head"]}},
        ],
    )
    ok, reason = jd.pre_pickup_ok(_fake_dispatch_client(), _snapshot(key="OP-555"))
    assert ok is False
    assert "OP-100" in reason
    assert "OP-101" in reason
    assert "mutex:backend/foo.py" in reason
    assert "mutex:alembic-chain-head" in reason


# OP-827: ``ensure_change_ids`` precondition tests. Post-mortem of OP-811 +
# OP-813 — claude CLI completed work, posted AC-verification comment, exited
# without commit; runner's ``git rebase`` failed on the empty/dirty branch
# and left the ticket as orphan In Progress for 2.5 days. The fix raises
# typed exceptions so the runner can route to the revert path.


def _init_worktree(tmp_path: Path) -> Path:
    """Build a minimal real git repo under ``tmp_path`` and return the path."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_ensure_change_ids_raises_no_commits_on_empty_branch(tmp_path: Path) -> None:
    """Branch with HEAD == base_ref → NoCommitsOnBranchError, no rebase."""
    repo = _init_worktree(tmp_path)
    base = _head_sha(repo)
    with pytest.raises(jd.NoCommitsOnBranchError) as ei:
        jd.ensure_change_ids(repo, base_ref=base)
    err = ei.value
    assert err.base_ref == base
    assert err.head == base


def test_ensure_change_ids_raises_dirty_worktree(tmp_path: Path) -> None:
    """Uncommitted changes → WorktreeDirtyError listing the dirty paths."""
    repo = _init_worktree(tmp_path)
    base = _head_sha(repo)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "feature.py"], cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "feature"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "uncommitted.py").write_text("y = 2\n", encoding="utf-8")
    with pytest.raises(jd.WorktreeDirtyError) as ei:
        jd.ensure_change_ids(repo, base_ref=base)
    assert "uncommitted.py" in ei.value.dirty_files


def test_ensure_change_ids_succeeds_with_commits_and_clean_tree(tmp_path: Path) -> None:
    """Happy path: commits exist, worktree clean → rebase + amend runs."""
    repo = _init_worktree(tmp_path)
    base = _head_sha(repo)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "feature.py"], cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "feature"],
        cwd=repo, check=True, capture_output=True,
    )
    jd.ensure_change_ids(repo, base_ref=base)


# OP-842: ensure_change_ids must filter the OP-836 workspace-safety
# sentinel from the dirty-paths list. Without these tests the regression
# (which wedged OP-840 in production on 2026-05-11) can silently return.


def test_ensure_change_ids_skips_when_only_dirty_path_is_sentinel(tmp_path: Path) -> None:
    """``.runner-cwd-sentinel`` alone in the dirty list MUST be filtered out
    so ``ensure_change_ids`` proceeds to rebase. The sentinel is OP-836's
    intentionally-untracked tamper-detection marker — its whole purpose is
    to live in the worktree during the CLI session."""
    repo = _init_worktree(tmp_path)
    base = _head_sha(repo)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "feature.py"], cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "feature"],
        cwd=repo, check=True, capture_output=True,
    )
    # Drop the sentinel exactly as runner_workspace_safety would.
    (repo / ".runner-cwd-sentinel").write_text(
        '{"ticket_key": "OP-TEST", "head_sha": "abc"}', encoding="utf-8",
    )
    # Should NOT raise — sentinel is filtered out, no other dirty paths.
    jd.ensure_change_ids(repo, base_ref=base)


def test_ensure_change_ids_raises_when_sentinel_plus_other_dirty(tmp_path: Path) -> None:
    """Sentinel + actual uncommitted user files → still raises but the
    ``dirty_files`` list excludes the sentinel (so the diagnostic message
    points at the real user-side problem, not at our own marker file)."""
    repo = _init_worktree(tmp_path)
    base = _head_sha(repo)
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "feature.py"], cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "feature"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / ".runner-cwd-sentinel").write_text("{}", encoding="utf-8")
    (repo / "uncommitted.py").write_text("y = 2\n", encoding="utf-8")
    with pytest.raises(jd.WorktreeDirtyError) as ei:
        jd.ensure_change_ids(repo, base_ref=base)
    assert "uncommitted.py" in ei.value.dirty_files
    assert ".runner-cwd-sentinel" not in ei.value.dirty_files


def test_ensure_change_ids_no_commits_error_carries_diagnostic_text() -> None:
    """Exception message includes a hint pointing at the CLI as cause."""
    err = jd.NoCommitsOnBranchError(base_ref="abc123def456", head="abc123def456")
    msg = str(err)
    assert "0 commits" in msg
    assert "abc123def456" in msg
    assert "claude CLI" in msg or "without producing a commit" in msg


def test_ensure_change_ids_dirty_error_truncates_long_file_list() -> None:
    """20 dirty files → message shows first 5 only (no log explosion)."""
    err = jd.WorktreeDirtyError(dirty_files=[f"file_{i}.py" for i in range(20)])
    msg = str(err)
    assert "20 uncommitted" in msg
    assert "file_0.py" in msg
    assert "file_19.py" not in msg
