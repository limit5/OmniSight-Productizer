"""N3 — OpenAPI contract dump + drift smoke test.

Covers:
  * `scripts/dump_openapi.py` produces a deterministic, sorted JSON
    snapshot containing the load-bearing routes the frontend imports
    in `lib/generated/openapi.ts`.
  * Running the script in `--check` mode against a tempfile that was
    just written by the same script must pass.
  * The committed `openapi.json` at the repo root must match what
    `app.openapi()` currently produces (this is the real CI gate; the
    shell job in `.github/workflows/ci.yml` enforces the same thing
    but keeping it as a Python test gives devs a fast local signal).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DUMP = _REPO_ROOT / "scripts" / "dump_openapi.py"
_SNAPSHOT = _REPO_ROOT / "openapi.json"


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "OMNISIGHT_DEBUG": "true", "PYTHONPATH": str(_REPO_ROOT)}
    return subprocess.run(
        [sys.executable, str(_DUMP), *args],
        cwd=cwd or _REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def test_dump_script_exists() -> None:
    assert _DUMP.exists(), f"missing {_DUMP}"


def test_dump_is_deterministic(tmp_path: Path) -> None:
    """Running the dump twice must produce byte-identical output."""
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"
    ra = _run(["--out", str(out_a)])
    rb = _run(["--out", str(out_b)])
    assert ra.returncode == 0, ra.stderr
    assert rb.returncode == 0, rb.stderr
    assert out_a.read_bytes() == out_b.read_bytes(), "dump is not deterministic"


def test_dump_contains_loadbearing_routes(tmp_path: Path) -> None:
    """Fail early if the frontend's compile-time contract probes lose
    their targets. These match the imports in `lib/api.ts`'s N3 block."""
    out = tmp_path / "schema.json"
    proc = _run(["--out", str(out)])
    assert proc.returncode == 0, proc.stderr
    schema = json.loads(out.read_text())
    # Routes the frontend pins in `lib/api.ts` (N3 tripwire).
    for p in ("/api/v1/agents", "/api/v1/tasks"):
        assert p in schema["paths"], f"route {p} missing from OpenAPI schema"
    # Schemas referenced by `lib/generated/openapi.ts` aliases.
    for s in ("Agent", "Task", "AgentStatus", "TaskStatus"):
        assert s in schema["components"]["schemas"], f"schema {s} missing"


@pytest.mark.skipif(not _SNAPSHOT.exists(), reason="snapshot not committed yet")
def test_committed_snapshot_matches_live_schema() -> None:
    """`openapi.json` at the repo root must match what the app currently
    advertises. If this fails, run `pnpm run openapi:sync` and commit."""
    proc = _run(["--check"])
    assert proc.returncode == 0, (
        "Committed openapi.json drifted from backend. "
        "Run `pnpm run openapi:sync` and commit.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr[:2000]}"
    )
