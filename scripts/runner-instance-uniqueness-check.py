#!/usr/bin/env python3
import re
import shlex
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from backend.agents.operator_notifier import Severity, notify

RUNNER_UNIT_RE = re.compile(r"^omnisight-runner-(?:claude|codex)-([^.@\s]+)\.service$")
INSTANCE_ENV = "OMNISIGHT_RUNNER_INSTANCE_ID"
DEFAULT_INSTANCE_ID = "default"
RunnerProcess = namedtuple("RunnerProcess", "unit instance_id pid")

def parse_runner_units(text: str) -> list[str]:
    return [
        unit
        for unit in (line.strip().split(None, 1)[0] for line in text.splitlines() if line.strip())
        if RUNNER_UNIT_RE.match(unit)
    ]

def _systemctl(args, runner=None):
    return (runner or subprocess.run)(
        ["systemctl", *args], capture_output=True, text=True, timeout=15, check=False
    )

def _show_props(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

def parse_environment_value(text: str) -> str:
    try:
        fields = shlex.split(text)
    except ValueError:
        fields = text.split()
    prefix = f"{INSTANCE_ENV}="
    for field in fields:
        if field.startswith(prefix):
            value = field[len(prefix):].strip()
            return value or DEFAULT_INSTANCE_ID
    return DEFAULT_INSTANCE_ID

def _parse_pid(pid_text: str) -> int | None:
    try:
        pid = int(pid_text.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None

def _fail(cmd: str, result) -> None:
    raise RuntimeError(f"{cmd} failed: {result.stderr.strip() or result.stdout.strip()}")

def enumerate_runner_processes(*, runner=None):
    result = _systemctl(["list-units", "--type=service", "--all", "--no-legend", "--plain"], runner)
    if result.returncode != 0:
        _fail("systemctl list-units --type=service", result)
    processes = []
    for unit in parse_runner_units(result.stdout):
        shown = _systemctl(["show", unit, "-p", "Environment", "-p", "MainPID"], runner)
        if shown.returncode != 0:
            _fail(f"systemctl show {unit}", shown)
        props = _show_props(shown.stdout)
        processes.append(
            RunnerProcess(unit, parse_environment_value(props.get("Environment", "")), _parse_pid(props.get("MainPID", "")))
        )
    return processes

def find_instance_collisions(processes):
    by_instance = {}
    for proc in processes:
        by_instance.setdefault(proc.instance_id, []).append(proc)
    return {instance_id: peers for instance_id, peers in by_instance.items() if len(peers) > 1}

def alert_collisions(collisions) -> None:
    for instance_id, peers in sorted(collisions.items()):
        units = [proc.unit for proc in peers]
        pids = [proc.pid for proc in peers if proc.pid is not None]
        notify(Severity.CRITICAL, "runner_instance_collision", message=f"multiple processes share instance_id={instance_id}: {units}", context={"instance_id": instance_id, "units": units, "pids": pids})

def check_uniqueness(*, runner=None) -> int:
    collisions = find_instance_collisions(enumerate_runner_processes(runner=runner))
    if collisions:
        alert_collisions(collisions)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(check_uniqueness())
