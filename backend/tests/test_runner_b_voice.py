"""B-Voice (2026-06-29): the jailed agent writes its JIRA report to a file and
the wrapper relays it out-of-jail — because the sandbox scrubs JIRA creds from
the agent CLI (OP-1777), so the agent cannot post to JIRA itself.

Pins: (1) ``runner_report_path`` lives in the per-ticket scratch ROOT — a SIBLING
of cli-home — so it survives ``cleanup_cli_home`` (which wipes only cli-home);
(2) ``_relay_runner_report`` posts the file body via ``add_comment`` + deletes
it, and is a NON-FATAL no-op when the file is missing or ``add_comment`` raises.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from backend.agents import runner_sandbox


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_MODULE_NAME = "auto_runner_jira"
KEY = "OP-BVOICE-TEST"


def _load_runner():
    cached = sys.modules.get(RUNNER_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        RUNNER_MODULE_NAME, REPO_ROOT / "auto-runner-jira.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[RUNNER_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


@pytest.fixture(autouse=True)
def _clean_scratch():
    scratch = Path("/tmp") / f"runner-{KEY.replace('/', '_')}"
    shutil.rmtree(scratch, ignore_errors=True)
    yield
    shutil.rmtree(scratch, ignore_errors=True)


def test_report_path_is_sibling_of_cli_home_and_created():
    p = runner_sandbox.runner_report_path(KEY)
    assert p.name == "runner-report.md"
    # SIBLING of cli-home (both under the per-ticket scratch root) so
    # cleanup_cli_home (which wipes only cli-home) does NOT delete the report.
    assert p.parent == runner_sandbox.cli_home_for(KEY).parent
    assert p.parent.is_dir()  # _tmp_dir_for created the scratch root


def test_report_survives_cli_home_cleanup():
    p = runner_sandbox.runner_report_path(KEY)
    runner_sandbox.cli_home_for(KEY).mkdir(parents=True, exist_ok=True)
    p.write_text("AC verification for OP-X:\n✓ done — test_foo\n")
    runner_sandbox.cleanup_cli_home(KEY)
    assert p.exists(), "report must survive cleanup_cli_home (it only wipes cli-home)"


def test_relay_posts_body_and_deletes(runner, monkeypatch):
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner.jira_dispatch, "add_comment",
        lambda client, key, text: posted.append((key, text)),
    )
    p = runner_sandbox.runner_report_path(KEY)
    p.write_text("AC verification for OP-X:\n✓ wired ONVIF — test_onvif:L10-40\n")
    runner._relay_runner_report(object(), KEY)
    assert len(posted) == 1
    key, text = posted[0]
    assert key == KEY
    assert "[runner-report]" in text
    assert "AC verification" in text
    assert not p.exists(), "relay must delete the report after posting"


def test_relay_noop_when_no_report(runner, monkeypatch):
    posted: list = []
    monkeypatch.setattr(
        runner.jira_dispatch, "add_comment", lambda *a, **k: posted.append(a)
    )
    runner._relay_runner_report(object(), KEY)  # no file written
    assert posted == []  # nothing posted, no error


def test_relay_is_nonfatal_on_error(runner, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("jira down")

    monkeypatch.setattr(runner.jira_dispatch, "add_comment", _boom)
    runner_sandbox.runner_report_path(KEY).write_text("something")
    runner._relay_runner_report(object(), KEY)  # must NOT raise
