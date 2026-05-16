"""OP-1028 tests for the systemd service directive pre-commit hook."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_systemd_service_directives.py"
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


CONFORMANT_UNIT = """\
[Unit]
Description=Synthetic conformant unit

[Service]
Type=simple
Restart=on-failure
StandardOutput=journal
ExecStart=/usr/bin/true
"""

MISSING_DIRECTIVE_UNIT = """\
[Unit]
Description=Synthetic non-conformant unit

[Service]
Type=simple
Restart=on-failure
ExecStart=/usr/bin/true
"""


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "check_systemd_service_directives",
        SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_conformant_service_unit_passes(tmp_path) -> None:
    mod = _load_script()
    unit = tmp_path / "conformant.service"
    unit.write_text(CONFORMANT_UNIT, encoding="utf-8")

    assert mod.check_file(unit) == []
    assert mod.main(["--all", str(unit)]) == 0


def test_missing_directive_service_unit_fails(tmp_path, capsys) -> None:
    mod = _load_script()
    unit = tmp_path / "missing-standard-output.service"
    unit.write_text(MISSING_DIRECTIVE_UNIT, encoding="utf-8")

    assert mod.check_file(unit) == [
        f"{unit}: [Service] missing required directive(s): StandardOutput="
    ]
    assert mod.main(["--all", str(unit)]) == 1
    assert "StandardOutput=" in capsys.readouterr().err


def test_pre_commit_config_registers_systemd_service_hook() -> None:
    config = PRE_COMMIT_CONFIG.read_text(encoding="utf-8")

    assert "id: omnisight-systemd-service-directives" in config
    assert "entry: python3 scripts/check_systemd_service_directives.py" in config
    assert "files: \\.service$" in config


def test_ci_runs_pre_commit_hooks() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert 'pip install "pre-commit==4.0.1"' in workflow
    assert "pre-commit run --all-files --show-diff-on-failure" in workflow


def test_staged_new_non_conformant_service_is_rejected(tmp_path) -> None:
    script = tmp_path / "scripts" / "check_systemd_service_directives.py"
    script.parent.mkdir()
    script.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    subprocess.run(
        ["git", "config", "user.email", "op-1028@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "OP-1028 Test"],
        cwd=tmp_path,
        check=True,
    )

    unit = tmp_path / "synthetic.service"
    unit.write_text(MISSING_DIRECTIVE_UNIT, encoding="utf-8")
    subprocess.run(["git", "add", "synthetic.service"], cwd=tmp_path, check=True)

    result = subprocess.run(
        [sys.executable, str(script), "synthetic.service"],
        cwd=tmp_path,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 1
    assert (
        "synthetic.service: [Service] missing required directive(s): StandardOutput="
        in result.stderr
    )
