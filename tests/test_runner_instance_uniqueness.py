import importlib.util, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runner-instance-uniqueness-check.py"
SERVICE = ROOT / "deploy/systemd/runner-instance-uniqueness-check.service"
TIMER = ROOT / "deploy/systemd/runner-instance-uniqueness-check.timer"

def _load():
    spec = importlib.util.spec_from_file_location("runner_instance_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

checker = _load()

def _ok(stdout: str):
    return subprocess.CompletedProcess(["systemctl"], 0, stdout, "")

def _directive(path: Path, section: str, key: str) -> str | None:
    lines = path.read_text().splitlines()
    prefix = f"{key}="
    for line in lines[lines.index(f"[{section}]") + 1:]:
        if line.startswith("["):
            return None
        if line.startswith(prefix):
            return line[len(prefix):]
    return None

def test_parse_units_and_environment() -> None:
    assert checker.parse_runner_units(
        "omnisight-runner-claude-default.service loaded active running\n"
        "omnisight-runner-codex-2.service loaded active running\n"
        "runner-codex-bot@worker2.service loaded active running\n"
    ) == ["omnisight-runner-claude-default.service", "omnisight-runner-codex-2.service"]
    assert checker.parse_environment_value(
        'PYTHONUNBUFFERED=1 OMNISIGHT_RUNNER_INSTANCE_ID=2 FOO="bar baz"'
    ) == "2"
    assert checker.parse_environment_value("OMNISIGHT_RUNNER_INSTANCE_ID=") == "default"

def test_enumerate_runner_processes_with_mocked_systemctl() -> None:
    def fake_run(cmd, **kwargs):
        if cmd[1] == "list-units":
            return _ok("omnisight-runner-claude-default.service loaded active running\n")
        return _ok("Environment=OMNISIGHT_RUNNER_INSTANCE_ID=default\nMainPID=111\n")

    proc = checker.enumerate_runner_processes(runner=fake_run)[0]
    assert proc == checker.RunnerProcess(
        "omnisight-runner-claude-default.service", "default", 111
    )

def test_collision_notifies_critical(monkeypatch) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        if cmd[1] == "list-units":
            return _ok(
                "omnisight-runner-claude-default.service loaded active running\n"
                "omnisight-runner-codex-default.service loaded active running\n"
            )
        pid = "111" if "claude" in cmd[2] else "222"
        return _ok(f"Environment=OMNISIGHT_RUNNER_INSTANCE_ID=default\nMainPID={pid}\n")

    monkeypatch.setattr(
        checker,
        "notify",
        lambda severity, code, message="", context=None: calls.append((severity, code, context)),
    )

    assert checker.check_uniqueness(runner=fake_run) == 1
    assert calls[0][:2] == (checker.Severity.CRITICAL, "runner_instance_collision")
    assert calls[0][2]["instance_id"] == "default"
    assert calls[0][2]["units"] == ["omnisight-runner-claude-default.service", "omnisight-runner-codex-default.service"]
    assert calls[0][2]["pids"] == [111, 222]

def test_systemd_units_wire_five_minute_timer_to_checker() -> None:
    assert _directive(TIMER, "Timer", "OnUnitActiveSec") == "5min"
    assert _directive(TIMER, "Timer", "Unit") == "runner-instance-uniqueness-check.service"
    assert _directive(TIMER, "Install", "WantedBy") == "timers.target"
    assert "scripts/runner-instance-uniqueness-check.py" in (_directive(SERVICE, "Service", "ExecStart") or "")
