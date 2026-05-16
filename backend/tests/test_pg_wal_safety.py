"""OP-1172 -- PostgreSQL WAL safety shutdown tests."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHUTDOWN_SH = REPO_ROOT / "scripts" / "shutdown.sh"


def _write_fake_docker(
    tmp_path: Path, *, ready_after: int = 1, always_unready: bool = False,
) -> Path:
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "services").write_text("postgres\n", encoding="utf-8")
    (state_dir / "cid_postgres.running").write_text("true\n", encoding="utf-8")
    (state_dir / "ready_after").write_text(str(ready_after), encoding="utf-8")
    (state_dir / "always_unready").write_text(
        "1\n" if always_unready else "0\n", encoding="utf-8",
    )

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
                  exec) shift; break ;;
                  ps) shift; break ;;
                  *) shift ;;
                esac
              done
              if [[ "${1:-}" == "-T" ]]; then
                shift
                service="$1"
                shift
                printf '%s %s\\n' "$service" "$*" >> "$state/execs"
                if [[ "${1:-}" == "pg_isready" ]]; then
                  count=0
                  [[ -f "$state/ready_count" ]] && count=$(cat "$state/ready_count")
                  count=$((count + 1))
                  echo "$count" > "$state/ready_count"
                  if [[ "$(cat "$state/always_unready")" == "1" ]]; then
                    exit 1
                  fi
                  [[ "$count" -ge "$(cat "$state/ready_after")" ]]
                  exit $?
                fi
                exit 0
              fi
              if [[ "${1:-}" == "--services" ]]; then
                cat "$state/services"
                exit 0
              fi
              if [[ "${1:-}" == "--all" && "${2:-}" == "--services" ]]; then
                if [[ "$(cat "$state/cid_postgres.running")" == "true" ]]; then
                  echo "postgres"
                fi
                exit 0
              fi
              if [[ "${1:-}" == "-q" ]]; then
                echo "cid_$2"
                exit 0
              fi
              exit 0
            fi

            if [[ "${1:-}" == "inspect" ]]; then
              cat "$state/${@: -1}.running" 2>/dev/null || echo false
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
              printf '%s %s\\n' "$signal" "${cid#cid_}" >> "$state/signals"
              echo false > "$state/${cid}.running"
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


def _write_fast_time(tmp_path: Path) -> None:
    date = tmp_path / "bin" / "date"
    date.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            counter="${FAKE_DOCKER_STATE:?}/date_counter"
            current=0
            [[ -f "$counter" ]] && current=$(cat "$counter")
            current=$((current + 10))
            echo "$current" > "$counter"
            echo "$current"
            """
        ),
        encoding="utf-8",
    )
    date.chmod(0o755)

    sleep = tmp_path / "bin" / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)


def _run_shutdown(
    tmp_path: Path, *, ready_after: int = 1, always_unready: bool = False,
):
    bin_dir = _write_fake_docker(
        tmp_path,
        ready_after=ready_after,
        always_unready=always_unready,
    )
    _write_fast_time(tmp_path)
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


def test_shutdown_invokes_checkpoint_before_stop(tmp_path: Path) -> None:
    proc, state = _run_shutdown(tmp_path)

    assert proc.returncode == 0, proc.stderr
    execs = (state / "execs").read_text(encoding="utf-8").splitlines()
    signals = (state / "signals").read_text(encoding="utf-8").splitlines()
    assert execs[0] == "postgres psql -c CHECKPOINT;"
    assert execs[1] == "postgres pg_isready"
    assert signals == ["TERM postgres"]


def test_pg_isready_polled_until_ok(tmp_path: Path) -> None:
    proc, state = _run_shutdown(tmp_path, ready_after=3)

    assert proc.returncode == 0, proc.stderr
    execs = (state / "execs").read_text(encoding="utf-8").splitlines()
    assert execs.count("postgres pg_isready") == 3
    assert (state / "signals").read_text(encoding="utf-8").splitlines() == [
        "TERM postgres"
    ]


def test_shutdown_aborts_if_pg_unreachable_within_25s(tmp_path: Path) -> None:
    proc, state = _run_shutdown(tmp_path, always_unready=True)

    assert proc.returncode == 1
    assert "pg_isready did not become OK within 25s" in proc.stderr
    assert not (state / "signals").exists()
