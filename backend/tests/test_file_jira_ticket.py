"""OP-737 contract tests for scripts/file_jira_ticket.py."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "file_jira_ticket.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("file_jira_ticket", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _args(**overrides):
    values = {
        "summary": "OP-737 synthetic",
        "description_file": "",
        "priority": "High",
        "tier": "S",
        "cls": "subscription-codex",
        "type": "meta",
        "areas": ["backend", "docs", "tests"],
        "scope": None,
        "check": False,
        "force": False,
        "no_push_capability": False,
        "capability": [],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_invalid_area_rejected_before_network() -> None:
    mod = _load_script()
    with pytest.raises(SystemExit) as exc:
        mod.file_ticket(_args(areas=["backend", "mobile"]), "## Files\nbackend/foo.py\n")
    assert "invalid area: mobile" in str(exc.value)


def test_area_mismatch_warning_requires_force(monkeypatch) -> None:
    mod = _load_script()
    warning = mod.validate_areas_match_description(
        {"backend"},
        "## Acceptance Criteria\n- [ ] Add docs/sop/lessons/L-OP-737-example.md\n",
    )
    assert warning
    assert "['docs']" in warning[0]
    assert "Runner CLI will halt" in warning[0]

    description = "## Acceptance Criteria\n- [ ] Add docs/sop/lessons/L-OP-737-example.md\n"
    with pytest.raises(SystemExit) as exc:
        mod.file_ticket(_args(areas=["backend"]), description)
    assert "pass --force" in str(exc.value)

    monkeypatch.setattr(
        mod,
        "_jira_config",
        lambda cls: ("https://jira.example.test", "OP", "Basic test-token"),
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps({"key": "OP-1000"}).encode()

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    key = mod.file_ticket(_args(areas=["backend"], force=True), description)
    assert key == "OP-1000"


def test_check_mode_validates_without_network(tmp_path, monkeypatch, capsys) -> None:
    mod = _load_script()
    desc = tmp_path / "desc.md"
    desc.write_text("## Files / Paths\n- backend/agents/jira_dispatch.py\n", encoding="utf-8")

    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("--check must not hit network")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fail_urlopen)

    rc = mod.main(
        [
            "--summary",
            "OP-737 check",
            "--description-file",
            str(desc),
            "--priority",
            "High",
            "--tier",
            "S",
            "--class",
            "subscription-codex",
            "--type",
            "feature",
            "--areas",
            "backend",
            "--check",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "AND issuetype = Story" in out
    assert 'labels = "class:subscription-codex"' in out


def test_valid_full_flow_posts_story(monkeypatch) -> None:
    mod = _load_script()
    requests = []

    monkeypatch.setattr(
        mod,
        "_jira_config",
        lambda cls: ("https://jira.example.test", "OP", "Basic test-token"),
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps({"key": "OP-999"}).encode()

    def fake_urlopen(req, timeout):
        requests.append((req, timeout))
        return Response()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    key = mod.file_ticket(
        _args(type="feature", areas=["backend", "docs", "tests"]),
        "## Acceptance Criteria\n- [ ] Update docs/sop/lessons/L-OP-737-example.md\n"
        "## Files / Paths\n- backend/tests/test_file_jira_ticket.py\n",
    )

    assert key == "OP-999"
    assert len(requests) == 1
    req, timeout = requests[0]
    assert timeout == 30
    assert req.full_url == "https://jira.example.test/rest/api/3/issue"
    assert req.get_header("User-agent") == mod.USER_AGENT
    payload = json.loads(req.data.decode())
    fields = payload["fields"]
    assert fields["issuetype"] == {"name": "Story"}
    assert "area:backend" in fields["labels"]
    assert "area:docs" in fields["labels"]
    assert "area:tests" in fields["labels"]
    assert fields["description"]["content"][0]["type"] == "codeBlock"


def test_subscription_class_auto_adds_gerrit_push_capability() -> None:
    """OP-1526 AC1: subscription-* tickets default to capability:enable=gerrit_push."""
    mod = _load_script()
    labels = mod._labels(_args(cls="subscription-claude"))
    assert "capability:enable=gerrit_push" in labels


def test_no_push_capability_flag_omits_default() -> None:
    """OP-1526 AC1: --no-push-capability removes the gerrit_push default."""
    mod = _load_script()
    labels = mod._labels(_args(cls="subscription-codex", no_push_capability=True))
    assert "capability:enable=gerrit_push" not in labels


def test_api_class_does_not_auto_add_gerrit_push_capability() -> None:
    """OP-1526 AC1: api-* classes never auto-add gerrit_push (they don't push)."""
    mod = _load_script()
    for api_cls in ("api-anthropic", "api-openai"):
        labels = mod._labels(_args(cls=api_cls))
        assert "capability:enable=gerrit_push" not in labels, (
            f"{api_cls} should not auto-add gerrit_push capability"
        )


def test_runner_vocab_areas_are_accepted() -> None:
    """OP-1559: filing CLI accepts the runner vocabulary additions."""
    mod = _load_script()
    labels = mod._labels(_args(areas=["db", "ci", "gerrit"]))
    assert "area:db" in labels
    assert "area:ci" in labels
    assert "area:gerrit" in labels


def test_capability_flag_layers_extra_capabilities() -> None:
    """OP-1526 AC1: --capability adds extra capability:enable=<cap> labels."""
    mod = _load_script()
    labels = mod._labels(
        _args(cls="subscription-claude", capability=["run_tests", "jira_update"])
    )
    assert "capability:enable=gerrit_push" in labels
    assert "capability:enable=run_tests" in labels
    assert "capability:enable=jira_update" in labels


def test_capability_flag_parser_splits_comma_list() -> None:
    """OP-1526 AC1: --capability parses a comma-separated list off the CLI."""
    mod = _load_script()
    parser = mod.build_parser()
    args = parser.parse_args(
        [
            "--summary", "x",
            "--description-file", "x.md",
            "--priority", "Medium",
            "--tier", "S",
            "--class", "subscription-claude",
            "--areas", "backend",
            "--capability", "run_tests, jira_update ,mcp_search",
        ]
    )
    assert args.capability == ["run_tests", "jira_update", "mcp_search"]
    assert args.no_push_capability is False
