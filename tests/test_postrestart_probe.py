"""OP-1768 regression coverage for scripts/postrestart-probe.sh."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "postrestart-probe.sh"


def _json_record(stdout: str) -> dict[str, object]:
    for line in stdout.splitlines():
        if line.startswith('{"event":"postrestart_probe"'):
            return json.loads(line)
    raise AssertionError(f"postrestart probe JSON record missing:\n{stdout}")


def test_default_version_path_populates_image_head_and_surfaces_drift(
    tmp_path: Path,
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    docker = fakebin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

if [ "${1:-}" = "inspect" ]; then
  exit 0
fi

if [ "${1:-}" = "exec" ] && [ "${3:-}" = "pg_isready" ]; then
  exit 0
fi

if [ "${1:-}" = "exec" ] && [ "${3:-}" = "psql" ]; then
  printf '%s\\n' 'db_head'
  exit 0
fi

if [ "${1:-}" = "exec" ] && [ "${3:-}" = "sh" ] && [ "${4:-}" = "-c" ]; then
  case "${5:-}" in
    *"curl -fsS -o /dev/null -w '%{http_code}' 'http://localhost:8000/readyz'"*)
      printf '200'
      exit 0
      ;;
    *"curl -fsS 'http://localhost:8000/version'"*)
      printf '%s\\n' '{"alembic_head_in_image":"image_head"}'
      exit 0
      ;;
    *"curl -fsS 'http://localhost:8000/api/v1/version'"*)
      exit 0
      ;;
  esac
fi

printf 'unexpected docker invocation: %s\\n' "$*" >&2
exit 1
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        "OMNISIGHT_POSTRESTART_RTO": "5",
    }
    env.pop("OMNISIGHT_VERSION_PATH", None)

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        env=env,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    record = _json_record(proc.stdout)
    assert record["healthy"] is True
    assert record["db_head"] == "db_head"
    assert record["image_head"] == "image_head"
    assert record["drift"] is True
    assert "DRIFT: image expects alembic head 'image_head'" in proc.stderr
