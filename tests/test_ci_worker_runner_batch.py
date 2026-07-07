"""OP-2548 — runner CI full batch includes root tests/."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load_ci_worker():
    path = SCRIPT_DIR / "ci_worker.py"
    spec = importlib.util.spec_from_file_location("ci_worker_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_full_pytest_batch_includes_root_tests() -> None:
    worker = _load_ci_worker()
    cfg = worker.WorkerConfig(repo_root=REPO_ROOT)

    argv = worker._build_pytest_argv("full", [], cfg)

    assert argv == [
        "python", "-m", "pytest", "--no-cov", "-q", "backend/tests", "tests",
    ]
