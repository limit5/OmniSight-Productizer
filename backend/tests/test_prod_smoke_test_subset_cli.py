"""Unit test: ``scripts/prod_smoke_test.py`` --subset CLI flag.

The bootstrap wizard's L6 Step 5 invokes the DAG #1 smoke subset.
This test guards the CLI contract the wizard relies on — no live
server required.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "prod_smoke_test.py"
)


def _load(argv: list[str]):
    """Import the script with *argv* so module-level parsing runs."""
    sys.argv = ["prod_smoke_test.py", *argv]
    spec = importlib.util.spec_from_file_location("prod_smoke_test", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(m)
    return m


def test_default_runs_both_dags():
    m = _load([])
    assert m.SUBSET == "both"
    assert len(m.DAGS) == 2


def test_subset_dag1_picks_only_compile_flash():
    m = _load(["--subset", "dag1"])
    assert m.SUBSET == "dag1"
    assert len(m.DAGS) == 1
    label, payload = m.DAGS[0]
    assert "compile-flash" in label
    assert payload["target_platform"] == "host_native"
    assert payload["dag"]["dag_id"] == "smoke-compile-flash-host-native"


def test_subset_dag2_picks_only_cross_compile():
    m = _load(["--subset", "dag2"])
    assert m.SUBSET == "dag2"
    assert len(m.DAGS) == 1
    label, payload = m.DAGS[0]
    assert "cross-compile" in label
    assert payload["target_platform"] == "aarch64"


def test_base_url_positional_preserved_with_subset():
    m = _load(["https://omnisight.example.com", "--subset", "dag1"])
    assert m.BASE_URL == "https://omnisight.example.com"
    assert m.SUBSET == "dag1"
    assert m.API == "https://omnisight.example.com/api/v1"


def test_trailing_slash_stripped():
    m = _load(["http://localhost:9000/"])
    assert m.BASE_URL == "http://localhost:9000"
    assert m.API == "http://localhost:9000/api/v1"


def test_select_dags_helper_accepts_both():
    m = _load([])
    assert [lbl for lbl, _ in m._select_dags("both")] == [
        "DAG #1: compile-flash (host_native)",
        "DAG #2: cross-compile (aarch64)",
    ]
    assert [lbl for lbl, _ in m._select_dags("dag1")] == [
        "DAG #1: compile-flash (host_native)",
    ]
