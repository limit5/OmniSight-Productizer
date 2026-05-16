"""[OP-1132] Post-reboot production smoke-test cron contract."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "smoke-test-prod.sh"
SERVICE = REPO_ROOT / "deploy" / "systemd" / "omnisight-smoke-test.service"
TIMER = REPO_ROOT / "deploy" / "systemd" / "omnisight-smoke-test.timer"


def _directive(path: Path, section: str, key: str) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{key}="
    for line in lines[lines.index(f"[{section}]") + 1:]:
        if line.startswith("["):
            return None
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _run_smoke(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    log_path = tmp_path / "smoke.jsonl"
    notify_path = tmp_path / "notify.jsonl"
    env.update(
        {
            "OMNISIGHT_SMOKE_LOG": str(log_path),
            "OMNISIGHT_SMOKE_NOTIFY_PATH": str(notify_path),
            "OMNISIGHT_SMOKE_LIVEZ_CMD": "printf livez-ok",
            "OMNISIGHT_SMOKE_READYZ_CMD": "printf readyz-ok",
            "OMNISIGHT_SMOKE_DB_CMD": "printf db-ok",
            "OMNISIGHT_SMOKE_JIRA_CMD": "printf jira-ok",
            "OMNISIGHT_SMOKE_GERRIT_CMD": "printf gerrit-ok",
            "OMNISIGHT_SMOKE_RUNNER_ACTIVITY_CMD": "printf runner-ok",
            "OMNISIGHT_SMOKE_NOTIFY_CMD": (
                "printf '%s\\n' \"$OMNISIGHT_SMOKE_FAILURE_JSON\" "
                ">> \"$OMNISIGHT_SMOKE_NOTIFY_PATH\""
            ),
        }
    )
    env.update(overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_smoke_script_records_success_row_for_all_checks(tmp_path: Path) -> None:
    result = _run_smoke(tmp_path)

    assert result.returncode == 0, result.stderr
    rows = _jsonl(tmp_path / "smoke.jsonl")
    assert rows[0]["status"] == "pass"
    assert rows[0]["failed_checks"] == []
    assert set(rows[0]["checks"]) == {
        "livez",
        "readyz",
        "db_alembic_version",
        "jira_reachability",
        "gerrit_ssh",
        "runner_activity",
    }
    assert not (tmp_path / "notify.jsonl").exists()


def test_smoke_script_records_failure_row_and_notifies_operator(tmp_path: Path) -> None:
    result = _run_smoke(tmp_path, OMNISIGHT_SMOKE_READYZ_CMD="echo down >&2; exit 7")

    assert result.returncode == 1
    rows = _jsonl(tmp_path / "smoke.jsonl")
    assert rows[0]["status"] == "fail"
    assert rows[0]["failed_checks"] == ["readyz"]
    assert rows[0]["checks"]["readyz"]["status"] == "fail"
    notifications = _jsonl(tmp_path / "notify.jsonl")
    assert notifications[0]["failed_checks"] == ["readyz"]
    assert notifications[0]["log_path"] == str(tmp_path / "smoke.jsonl")


def test_systemd_timer_fires_ten_minutes_after_boot_then_every_thirty_minutes() -> None:
    assert _directive(TIMER, "Timer", "Unit") == "omnisight-smoke-test.service"
    assert _directive(TIMER, "Timer", "OnBootSec") == "10min"
    assert _directive(TIMER, "Timer", "OnUnitActiveSec") == "30min"
    assert _directive(TIMER, "Timer", "Persistent") == "true"
    assert _directive(TIMER, "Install", "WantedBy") == "timers.target"


def test_systemd_service_runs_prod_smoke_script_and_wires_prod_env() -> None:
    assert _directive(SERVICE, "Service", "Type") == "oneshot"
    assert _directive(SERVICE, "Service", "WorkingDirectory") == (
        "/home/user/work/sora/OmniSight-Productizer"
    )
    assert _directive(SERVICE, "Service", "ExecStart") == (
        "/bin/bash /home/user/work/sora/OmniSight-Productizer/scripts/smoke-test-prod.sh"
    )
    text = SERVICE.read_text(encoding="utf-8")
    assert "EnvironmentFile=-/home/user/.config/omnisight/prod-smoke-test.env" in text
    assert "OMNISIGHT_SMOKE_LOG=/var/log/omnisight-smoke-test.log" in text


def test_script_contains_required_probe_contracts() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "$BASE_URL/livez" in text
    assert "$BASE_URL/readyz" in text
    assert "SELECT version_num FROM alembic_version LIMIT 1;" in text
    assert "/rest/api/3/myself" in text
    assert "gerrit version" in text
    assert "RUNNER_MAX_AGE_SECONDS" in text
    assert "backend.agents.operator_notifier" in text
