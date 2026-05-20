"""OP-1547 - pipeline coordinator systemd unit contracts.

Locks the deploy-side contract for ADR-0021 section 3.1 / section 9 L2 without
starting the coordinator:

* coordinator + watchdog units live under deploy/systemd;
* both units run from the auto-synced /home/user/sora-bridge checkout;
* coordinator shutdown uses explicit SIGTERM so the OP-1546 L5 drain runs;
* restart policy and watchdog 30s / 90s thresholds stay pinned;
* systemd accepts both user units.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"

COORDINATOR_UNIT = SYSTEMD_DIR / "pipeline-coordinator.service"
WATCHDOG_UNIT = SYSTEMD_DIR / "pipeline-coordinator-watchdog.service"
SORA_BRIDGE = "/home/user/sora-bridge"
CONFIG_DIR = "/home/user/.config/omnisight/coordinator"


def _section(unit: Path, name: str) -> str:
    text = unit.read_text()
    match = re.search(rf"^\[{re.escape(name)}\]\s*\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    assert match, f"section [{name}] not found in {unit.relative_to(REPO_ROOT)}"
    return match.group(1)


def _directive(block: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}\s*=\s*(.+?)\s*$", block, re.M)
    return match.group(1) if match else None


def _directives(block: str, key: str) -> list[str]:
    return re.findall(rf"^{re.escape(key)}\s*=\s*(.+?)\s*$", block, re.M)


def _env_values(block: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _directives(block, "Environment"):
        key, _, value = line.partition("=")
        values[key] = value
    return values


def test_pipeline_coordinator_units_exist() -> None:
    assert COORDINATOR_UNIT.exists()
    assert WATCHDOG_UNIT.exists()


def test_coordinator_unit_runs_from_sora_bridge_checkout() -> None:
    service = _section(COORDINATOR_UNIT, "Service")
    env = _env_values(service)

    assert _directive(service, "Type") == "simple"
    assert _directive(service, "WorkingDirectory") == SORA_BRIDGE
    assert env["PYTHONPATH"] == SORA_BRIDGE
    assert env["OMNISIGHT_COORDINATOR_CONFIG_DIR"] == CONFIG_DIR
    assert (
        env["OMNISIGHT_COORDINATOR_DECISION_LOG_DIR"]
        == f"{CONFIG_DIR}/decision-log"
    )
    assert (
        _directive(service, "ExecStart")
        == "/usr/bin/python3 -m backend.agents.pipeline_coordinator"
    )


def test_coordinator_unit_pins_sigterm_drain_and_restart_policy() -> None:
    service = _section(COORDINATOR_UNIT, "Service")

    assert _directive(service, "KillSignal") == "SIGTERM"
    assert _directive(service, "Restart") == "on-failure"
    assert _directive(service, "RestartSec") == "15"
    assert _directive(service, "TimeoutStopSec") == "90"


def test_units_document_linger_install_step() -> None:
    text = COORDINATOR_UNIT.read_text() + "\n" + WATCHDOG_UNIT.read_text()
    assert "loginctl enable-linger $USER" in text


def test_watchdog_unit_runs_from_sora_bridge_and_restarts_coordinator() -> None:
    service = _section(WATCHDOG_UNIT, "Service")
    env = _env_values(service)
    exec_start = _directive(service, "ExecStart")

    assert _directive(service, "Type") == "simple"
    assert _directive(service, "WorkingDirectory") == SORA_BRIDGE
    assert env["PYTHONPATH"] == SORA_BRIDGE
    assert env["OMNISIGHT_COORDINATOR_HEARTBEAT"] == f"{CONFIG_DIR}/heartbeat"
    assert env["OMNISIGHT_COORDINATOR_WATCHDOG_POLL_SECONDS"] == "30"
    assert env["OMNISIGHT_COORDINATOR_WATCHDOG_STALE_SECONDS"] == "90"
    assert exec_start is not None
    assert "systemctl --user restart pipeline-coordinator.service" in exec_start
    assert _directive(service, "Restart") == "on-failure"


def test_systemd_analyze_user_verify_accepts_pipeline_units() -> None:
    result = subprocess.run(
        [
            "systemd-analyze",
            "--user",
            "verify",
            str(COORDINATOR_UNIT),
            str(WATCHDOG_UNIT),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
