from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SHUTDOWN_SH = REPO_ROOT / "scripts" / "shutdown.sh"


def _write_fake_docker(
    tmp_path: Path,
    *,
    services: list[str],
    hung: set[str] | None = None,
) -> Path:
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "services").write_text("\n".join(services) + "\n", encoding="utf-8")
    for service in services:
        (state_dir / f"cid_{service}.running").write_text("true\n", encoding="utf-8")
    (state_dir / "hung").write_text("\n".join(sorted(hung or set())) + "\n", encoding="utf-8")

    docker = bin_dir / "docker"
    docker.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            state="${FAKE_DOCKER_STATE:?}"
            printf '%s\\n' "$*" >> "$state/calls"

            if [[ "${1:-}" == "compose" ]]; then
              shift
              if [[ "${1:-}" == "version" ]]; then
                echo "Docker Compose version v2.fake"
                exit 0
              fi
              while [[ $# -gt 0 ]]; do
                case "$1" in
                  -f) shift 2 ;;
                  --profile) shift 2 ;;
                  exec) shift; break ;;
                  ps) shift; break ;;
                  *) shift ;;
                esac
              done
              if [[ "${1:-}" == "-T" ]]; then
                shift
                service="${1:-}"
                shift
                printf '%s %s\\n' "$service" "$*" >> "$state/execs"
                exit 0
              fi
              if [[ "${1:-}" == "--services" ]]; then
                cat "$state/services"
                exit 0
              fi
              if [[ "${1:-}" == "--all" && "${2:-}" == "--services" ]]; then
                while IFS= read -r service; do
                  [[ -z "$service" ]] && continue
                  if [[ "$(cat "$state/cid_${service}.running" 2>/dev/null || echo false)" == "true" ]]; then
                    echo "$service"
                  fi
                done < "$state/services"
                exit 0
              fi
              if [[ "${1:-}" == "-q" ]]; then
                echo "cid_$2"
                exit 0
              fi
              exit 0
            fi

            if [[ "${1:-}" == "inspect" ]]; then
              cid="${@: -1}"
              cat "$state/${cid}.running" 2>/dev/null || echo false
              exit 0
            fi

            if [[ "${1:-}" == "kill" ]]; then
              signal=""
              cid=""
              for arg in "$@"; do
                case "$arg" in
                  --signal=*) signal="${arg#--signal=}" ;;
                  cid_*) cid="$arg" ;;
                esac
              done
              service="${cid#cid_}"
              printf '%s %s\\n' "$signal" "$service" >> "$state/signals"
              if [[ "$signal" == "KILL" ]] || ! grep -qx "$service" "$state/hung"; then
                echo false > "$state/${cid}.running"
              fi
              echo "$cid"
              exit 0
            fi

            echo "unexpected docker args: $*" >&2
            exit 99
            """
        ),
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir


def _write_fast_date(tmp_path: Path) -> None:
    date = tmp_path / "bin" / "date"
    date.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            counter="${FAKE_DOCKER_STATE:?}/date_counter"
            current=0
            if [[ -f "$counter" ]]; then
              current=$(cat "$counter")
            fi
            current=$((current + 100))
            echo "$current" > "$counter"
            echo "$current"
            """
        ),
        encoding="utf-8",
    )
    date.chmod(0o755)


def _run_shutdown(tmp_path: Path, *, services: list[str], hung: set[str] | None = None):
    bin_dir = _write_fake_docker(tmp_path, services=services, hung=hung)
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env = os.environ.copy()
    env["FAKE_DOCKER_STATE"] = str(tmp_path / "state")
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    proc = subprocess.run(
        [
            "bash",
            str(SHUTDOWN_SH),
            "--mode",
            "compose",
            "--compose-file",
            str(compose_file),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return proc, tmp_path / "state"


def _json_lines(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def test_shutdown_signals_sigterm_then_sigkill_after_grace(tmp_path: Path) -> None:
    _write_fake_docker(tmp_path, services=["worker"], hung={"worker"})
    _write_fast_date(tmp_path)
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env = os.environ.copy()
    env["FAKE_DOCKER_STATE"] = str(tmp_path / "state")
    env["PATH"] = f"{tmp_path / 'bin'}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(SHUTDOWN_SH), "--mode", "compose", "--compose-file", str(compose_file)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "state" / "signals").read_text(encoding="utf-8").splitlines() == [
        "TERM worker",
        "KILL worker",
    ]
    assert _json_lines(proc.stdout) == [
        {"service": "worker", "grace_used": 10, "method": "SIGKILL", "result": "stopped"}
    ]


def test_shutdown_uses_service_specific_grace_period(tmp_path: Path) -> None:
    proc, _state = _run_shutdown(tmp_path, services=["frontend", "backend", "postgres"])

    assert proc.returncode == 0, proc.stderr
    lines = _json_lines(proc.stdout)
    assert {"service": "frontend", "grace_used": 15, "method": "SIGTERM", "result": "stopped"} in lines
    assert {"service": "backend", "grace_used": 40, "method": "SIGTERM", "result": "stopped"} in lines
    assert {"service": "postgres", "grace_used": 30, "method": "SIGTERM", "result": "stopped"} in lines


def test_shutdown_idempotent_on_already_stopped(tmp_path: Path) -> None:
    proc, state = _run_shutdown(tmp_path, services=["frontend"])
    assert proc.returncode == 0, proc.stderr

    proc = subprocess.run(
        [
            "bash",
            str(SHUTDOWN_SH),
            "--mode",
            "compose",
            "--compose-file",
            str(tmp_path / "compose.yml"),
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "FAKE_DOCKER_STATE": str(state),
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        },
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert _json_lines(proc.stdout) == [
        {"service": "frontend", "grace_used": 15, "method": "SIGTERM", "result": "already_exited"}
    ]


def test_shutdown_outputs_jsonl_parseable(tmp_path: Path) -> None:
    proc, _state = _run_shutdown(tmp_path, services=["caddy", "frontend", "backend-a"])

    assert proc.returncode == 0, proc.stderr
    lines = _json_lines(proc.stdout)
    assert [line["service"] for line in lines] == ["caddy", "frontend", "backend-a"]
    assert all(set(line) == {"service", "grace_used", "method", "result"} for line in lines)


def test_shutdown_preserves_existing_behavior(tmp_path: Path) -> None:
    proc, state = _run_shutdown(tmp_path, services=["caddy", "frontend", "backend-a", "prometheus"])

    assert proc.returncode == 0, proc.stderr
    calls = (state / "calls").read_text(encoding="utf-8")
    assert "compose version" in calls
    assert f"compose -f {tmp_path / 'compose.yml'} ps --services" in calls
    assert "compose -f " in calls and " --profile observability ps --services" in calls

    signals = (state / "signals").read_text(encoding="utf-8").splitlines()
    assert signals == [
        "TERM caddy",
        "TERM frontend",
        "TERM backend-a",
        "TERM prometheus",
    ]
