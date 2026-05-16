"""OP-1124 — unit tests for the hot-file ``mutex_with`` auto-injector.

Exercises the four scenarios called out in the Code AC:

(a) ticket with no hot files          → no mutation
(b) ticket with one hot file          → one ``mutex:<file>`` added
(c) ticket with multiple hot files    → all added, deterministic order
(d) ticket re-filed                   → idempotent, second pass adds nothing
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "jira_mutex_auto_inject.py"


def _load_module():
    import sys

    name = "jira_mutex_auto_inject"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HOT_FILES = {
    "backend/agents/jira_dispatch.py",
    "auto-runner-jira.py",
    "auto-runner-multi.py",
    "auto-runner-codex.py",
    "backend/agents/runner_coordination.py",
    "backend/agents/runner_progress.py",
    "backend/agents/scheduler.py",
    "backend/agents/capability_matrix.py",
}


def test_a_no_hot_files_no_mutation() -> None:
    """Scenario (a): ticket touches only cold files; description unchanged."""
    mod = _load_module()
    description = (
        "## Goal\n"
        "Tweak a backend helper.\n"
        "## Files / Paths\n"
        "- backend/agents/notifier.py\n"
        "- backend/tests/test_notifier.py\n"
    )
    result = mod.auto_inject_for_filing(description, hot_files=HOT_FILES)
    assert result.added_mutexes == ()
    assert result.already_present == ()
    assert result.hot_files_touched == ()
    assert result.description == description


def test_b_single_hot_file_injects_one_mutex() -> None:
    """Scenario (b): one hot file is touched → one mutex label added."""
    mod = _load_module()
    description = (
        "## Goal\n"
        "Patch jira_dispatch import block.\n"
        "## Files / Paths\n"
        "- backend/agents/jira_dispatch.py\n"
        "- backend/tests/test_jira_dispatch.py (existing)\n"
        "## Prerequisites\n"
        "\n"
        "```yaml\n"
        "blocks_on: []\n"
        "soft_prereqs: []\n"
        "mutex_with: []\n"
        "schema_locks: []\n"
        "live_state_requires: []\n"
        "external_blockers: []\n"
        "```\n"
    )
    result = mod.auto_inject_for_filing(description, hot_files=HOT_FILES)
    assert result.added_mutexes == ("mutex:backend/agents/jira_dispatch.py",)
    assert result.already_present == ()
    assert result.hot_files_touched == ("backend/agents/jira_dispatch.py",)
    assert "mutex:backend/agents/jira_dispatch.py" in result.description
    # Round-trip through the same parser the runner uses to make sure
    # the rewrite remains machine-readable.
    import yaml

    body = result.description.split("```yaml", 1)[1].split("```", 1)[0]
    data = yaml.safe_load(body)
    assert data["mutex_with"] == ["mutex:backend/agents/jira_dispatch.py"]


def test_c_multiple_hot_files_injects_sorted() -> None:
    """Scenario (c): multiple hot files → all added in deterministic order."""
    mod = _load_module()
    description = (
        "## Scope\n"
        "Refactor scheduler + capability_matrix + jira_dispatch in lockstep.\n"
        "## Files / Paths\n"
        "- backend/agents/scheduler.py\n"
        "- backend/agents/capability_matrix.py\n"
        "- backend/agents/jira_dispatch.py\n"
        "- auto-runner-multi.py\n"
    )
    result = mod.auto_inject_for_filing(description, hot_files=HOT_FILES)
    assert result.added_mutexes == (
        "mutex:auto-runner-multi.py",
        "mutex:backend/agents/capability_matrix.py",
        "mutex:backend/agents/jira_dispatch.py",
        "mutex:backend/agents/scheduler.py",
    )
    # A Prerequisites block was newly appended (no prior block).
    assert "## Prerequisites" in result.description
    assert result.description.count("## Prerequisites") == 1
    for label in result.added_mutexes:
        assert label in result.description


def test_d_re_filing_is_idempotent() -> None:
    """Scenario (d): re-running on already-injected ticket adds nothing."""
    mod = _load_module()
    description = (
        "## Goal\n"
        "Patch jira_dispatch.\n"
        "## Files / Paths\n"
        "- backend/agents/jira_dispatch.py\n"
    )
    first = mod.auto_inject_for_filing(description, hot_files=HOT_FILES)
    assert first.added_mutexes == ("mutex:backend/agents/jira_dispatch.py",)

    second = mod.auto_inject_for_filing(first.description, hot_files=HOT_FILES)
    assert second.added_mutexes == ()
    assert second.already_present == ("mutex:backend/agents/jira_dispatch.py",)
    # Exactly one prerequisites fence — no duplicate block appended.
    assert second.description.count("## Prerequisites") == 1
    assert second.description.count("```yaml") == 1
    # Idempotent on content too — re-running returns the same bytes.
    assert second.description == first.description


# ── Auxiliary coverage ─────────────────────────────────────────────


def test_extract_files_touched_picks_up_bare_runner_paths() -> None:
    mod = _load_module()
    touched = mod.extract_files_touched(
        "Edits both auto-runner-jira.py and backend/agents/scheduler.py."
    )
    assert "auto-runner-jira.py" in touched
    assert "backend/agents/scheduler.py" in touched


def test_allowlist_file_loads_eight_entries() -> None:
    """Pin the initial 8-file allow-list shape — guards against accidental edits."""
    mod = _load_module()
    loaded = mod.load_hot_files()
    assert loaded == HOT_FILES


def test_inject_preserves_existing_unrelated_mutex() -> None:
    """Pre-existing mutex_with entries (e.g. alembic-chain-head) must survive."""
    mod = _load_module()
    description = (
        "## Files / Paths\n"
        "- backend/agents/jira_dispatch.py\n"
        "## Prerequisites\n"
        "\n"
        "```yaml\n"
        "blocks_on: []\n"
        "soft_prereqs: []\n"
        "mutex_with:\n"
        "  - mutex:alembic-chain-head\n"
        "schema_locks: []\n"
        "live_state_requires: []\n"
        "external_blockers: []\n"
        "```\n"
    )
    result = mod.auto_inject_for_filing(description, hot_files=HOT_FILES)
    assert "mutex:alembic-chain-head" in result.description
    assert "mutex:backend/agents/jira_dispatch.py" in result.description
    assert result.added_mutexes == ("mutex:backend/agents/jira_dispatch.py",)


def test_no_prereqs_block_creates_one() -> None:
    """If a description has no Prereqs YAML, the injector appends a minimal block."""
    mod = _load_module()
    description = (
        "## Goal\nEdit hot file.\n"
        "## Files / Paths\n- backend/agents/scheduler.py\n"
    )
    result = mod.auto_inject_for_filing(description, hot_files=HOT_FILES)
    assert "## Prerequisites" in result.description
    assert "mutex:backend/agents/scheduler.py" in result.description
    # The shape matches what backend.agents.jira_dispatch.parse_prerequisites
    # expects (six canonical keys present).
    body = result.description.split("```yaml", 1)[1].split("```", 1)[0]
    import yaml

    data = yaml.safe_load(body)
    for key in (
        "blocks_on",
        "soft_prereqs",
        "mutex_with",
        "schema_locks",
        "live_state_requires",
        "external_blockers",
    ):
        assert key in data
    assert data["mutex_with"] == ["mutex:backend/agents/scheduler.py"]


def test_parse_prerequisites_round_trip() -> None:
    """The injected block must round-trip through the runner's parser."""
    mod = _load_module()
    description = (
        "## Files / Paths\n- backend/agents/jira_dispatch.py\n"
        "- backend/agents/runner_progress.py\n"
    )
    result = mod.auto_inject_for_filing(description, hot_files=HOT_FILES)

    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from backend.agents import jira_dispatch as jd  # noqa: WPS433

    parsed = jd.parse_prerequisites(result.description)
    assert set(parsed["mutex_with"]) == {
        "mutex:backend/agents/jira_dispatch.py",
        "mutex:backend/agents/runner_progress.py",
    }


# ── Filing-flow integration smoke test ─────────────────────────────


def test_file_jira_ticket_invokes_auto_inject(monkeypatch) -> None:
    """file_ticket() must POST the injector-rewritten description, not the raw one."""
    import argparse
    import json

    fjt_spec = importlib.util.spec_from_file_location(
        "file_jira_ticket", REPO_ROOT / "scripts" / "file_jira_ticket.py"
    )
    fjt = importlib.util.module_from_spec(fjt_spec)
    assert fjt_spec.loader is not None
    fjt_spec.loader.exec_module(fjt)

    captured = {}

    monkeypatch.setattr(
        fjt,
        "_jira_config",
        lambda cls: ("https://jira.example.test", "OP", "Basic test-token"),
    )

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps({"key": "OP-1124-test"}).encode()

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode())
        return Resp()

    monkeypatch.setattr(fjt.urllib.request, "urlopen", fake_urlopen)

    args = argparse.Namespace(
        summary="hot file edit",
        description_file="",
        priority="High",
        tier="S",
        cls="subscription-codex",
        type="bug",
        areas=["backend", "tests"],
        scope=None,
        check=False,
        force=False,
    )
    description = (
        "## Goal\nUpdate jira_dispatch import block.\n"
        "## Files / Paths\n- backend/agents/jira_dispatch.py\n"
        "- backend/tests/test_jira_dispatch.py\n"
    )
    key = fjt.file_ticket(args, description)
    assert key == "OP-1124-test"

    posted_description_block = captured["body"]["fields"]["description"]
    posted_text = posted_description_block["content"][0]["content"][0]["text"]
    assert "mutex:backend/agents/jira_dispatch.py" in posted_text
    assert "## Prerequisites" in posted_text


def test_atlas_retrofit_audit_meets_quorum(tmp_path) -> None:
    """Integration AC — replay Atlas tickets through injector via --from-file.

    Models the 7 hot-file-bearing Atlas tickets called out in the OP-1124
    description (OP-1107, OP-1108, OP-1111, OP-1116, OP-1117 + nearby
    siblings). The injector must flag >=3 of them as needing
    mutex_with — the AC quorum.
    """
    import json
    import subprocess

    fixtures = []
    for key, paths in [
        ("OP-1107", ["backend/agents/runner_coordination.py", "backend/agents/jira_dispatch.py"]),
        ("OP-1108", ["backend/agents/jira_dispatch.py", "backend/agents/runner_coordination.py"]),
        ("OP-1111", ["backend/agents/jira_dispatch.py"]),
        ("OP-1116", ["backend/agents/jira_dispatch.py", "backend/agents/scheduler.py"]),
        ("OP-1117", ["backend/agents/capability_matrix.py", "backend/agents/jira_dispatch.py"]),
        ("OP-1118", ["scripts/omnisight-rescue/runner_rescue.py"]),
        ("OP-1120", ["backend/agents/scheduler.py"]),
    ]:
        files_block = "## Files / Paths\n" + "\n".join(f"- {p}" for p in paths) + "\n"
        fixtures.append({
            "key": key,
            "fields": {
                "summary": f"{key} sample",
                "status": {"name": "Done"},
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [{
                        "type": "codeBlock",
                        "attrs": {"language": "markdown"},
                        "content": [{"type": "text", "text": files_block}],
                    }],
                },
            },
        })
    fixture_path = tmp_path / "atlas.json"
    fixture_path.write_text(json.dumps(fixtures))

    script_path = REPO_ROOT / "scripts" / "audit_atlas_mutex_retrofit.py"
    result = subprocess.run(
        [
            "python3",
            str(script_path),
            "--from-file",
            str(fixture_path),
            "--json",
            "--require-quorum",
            "3",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["scanned"] == 7
    assert summary["would_add_count"] >= 3
    # OP-1117's combo (jira_dispatch + capability_matrix) is the
    # canonical exemplar — pin it.
    op_1117 = next(r for r in summary["rows"] if r["key"] == "OP-1117")
    assert sorted(op_1117["would_add_mutexes"]) == [
        "mutex:backend/agents/capability_matrix.py",
        "mutex:backend/agents/jira_dispatch.py",
    ]
