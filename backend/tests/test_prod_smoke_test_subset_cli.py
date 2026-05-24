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


# ── OP-1662: DAG #1 redefined compile -> run-test (was compile -> flash) ──
#
# The dag_id / subset label keep the historical "compile-flash" name so
# the cross-surface contract (bootstrap _SMOKE_DAG_ID, openapi, the wizard
# UI, e2e) does not drift — a rename is a separate cross-area ticket. What
# changed is the *body* of step 2: a hollow t3 "flash" (you can't flash a
# board over localhost — docs/operations/sandbox.md:320) became a genuine
# t1 on-host self-test that consumes build/firmware.bin and writes
# logs/test.log.


def _dag1_tasks(m) -> list[dict]:
    return m.DAG_1_COMPILE_FLASH_HOST_NATIVE["dag"]["tasks"]


def test_dag1_is_compile_then_run_test():
    m = _load(["--subset", "dag1"])
    tasks = _dag1_tasks(m)
    assert [t["task_id"] for t in tasks] == ["compile", "run-test"]
    # The hollow symbolic flash step is gone for good.
    assert all(t["task_id"] != "flash" for t in tasks)
    assert all(t["expected_output"] != "logs/flash.log" for t in tasks)


def test_dag1_run_test_consumes_firmware_and_writes_test_log():
    m = _load(["--subset", "dag1"])
    compile_t, run_test = _dag1_tasks(m)

    # Step 1 (compile) builds the firmware image from the committed fixture
    # source seeds (external: prefix so dep_closure accepts them — OP-1673).
    assert compile_t["task_id"] == "compile"
    assert compile_t["inputs"] == ["external:CMakeLists.txt", "external:main.c"]
    assert compile_t["expected_output"] == "build/firmware.bin"

    # Step 2 (run-test) is an honest LOCAL self-test: t1 tier (not the old
    # t3-that-swapped-to-t1), runs python3, consumes the compiled image,
    # writes the test log, and depends on compile. Its inputs are the
    # upstream output (build/firmware.bin) + an external: source seed
    # (run_test.py from the fixture) — OP-1673.
    assert run_test["task_id"] == "run-test"
    assert run_test["required_tier"] == "t1"
    assert run_test["toolchain"] == "python3"
    assert run_test["inputs"] == ["build/firmware.bin", "external:run_test.py"]
    assert run_test["expected_output"] == "logs/test.log"
    assert run_test["depends_on"] == ["compile"]


def test_dag1_opt_in_metadata_present():
    """The per-run half of the executor dual opt-in gate (OP-1673).

    Without metadata.dag_executor_opt_in the executor leaves this run on the
    legacy path; the smoke DAG must opt in so it actually exercises the
    dag-executor.
    """
    m = _load(["--subset", "dag1"])
    meta = m.DAG_1_COMPILE_FLASH_HOST_NATIVE["metadata"]
    assert meta.get("dag_executor_opt_in") is True
    # test_run kept so the run is excluded from the finetune corpus.
    assert meta.get("test_run") is True


def test_dag1_validates_clean():
    """The redefined DAG must pass the semantic validator (AC: validates).

    No target_profile is needed: run-test is a real t1 task, so it does not
    rely on the t3 -> LOCAL tier swap the old flash step leaned on.
    """
    from backend.dag_schema import DAG
    from backend.dag_validator import validate

    m = _load(["--subset", "dag1"])
    dag = DAG.model_validate(m.DAG_1_COMPILE_FLASH_HOST_NATIVE["dag"])
    result = validate(dag)
    assert result.ok, result.summary() + " :: " + repr(
        [e.to_dict() for e in result.errors]
    )
