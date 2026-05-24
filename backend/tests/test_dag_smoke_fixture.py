"""OP-1673 — buildable smoke DAG fixture + validator-legal inputs.

The smoke DAG (scripts/prod_smoke_test.py DAG #1) is now backed by a tiny
committed CMake fixture at ``backend/dag_smoke_fixture/`` (CMakeLists.txt +
main.c -> build/firmware.bin; run_test.py -> logs/test.log) and declares
validator-legal inputs:

  * compile:  inputs=["external:CMakeLists.txt", "external:main.c"]
  * run-test: inputs=["build/firmware.bin", "external:run_test.py"]

This module verifies, against the REAL prod DAG payload + the REAL
LocalTaskHandler (project_root = the fixture):

  * the DAG passes :func:`backend.dag_validator.validate` (dep_closure ok);
  * the fixture compiles: the cmake handler produces build/firmware.bin from
    an otherwise-empty scratch (the OP-1673 "empty scratch -> rc=1" cure);
  * the executor materialises BOTH input kinds into the run-test scratch —
    the staged upstream output (build/firmware.bin) and the external: source
    seed (run_test.py).

Local-only: no DB / Docker / network; scratch lives under ``tmp_path``.

KNOWN BLOCKER (F4 / OP-1676 gap) — the full run-test EXECUTION is xfail:
``LocalTaskHandler._python_entry`` returns the raw ``external:run_test.py``
literal instead of the materialised ``run_test.py``, so the python3 task
launches ``python3 external:run_test.py`` -> rc=2 (file not found). Fixing it
is a one-liner in a HANDLER INTERNAL, which this ticket MUST NOT touch ("that
is F4"). The xfail test encodes the intended end state and flips to xpass once
that prefix-stripping lands.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from backend import dag_executor as dx
from backend.dag_schema import DAG
from backend.dag_validator import validate


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "prod_smoke_test.py"
_FIXTURE = _REPO_ROOT / "backend" / "dag_smoke_fixture"

_HAS_CMAKE = shutil.which("cmake") is not None
_HAS_CC = shutil.which("cc") is not None or shutil.which("gcc") is not None


def _load_smoke_module():
    """Import scripts/prod_smoke_test.py with a clean argv (it parses argv at
    import time). Mirrors test_prod_smoke_test_subset_cli._load."""
    sys.argv = ["prod_smoke_test.py", "--subset", "dag1"]
    spec = importlib.util.spec_from_file_location("prod_smoke_test", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(m)
    return m


def _smoke_dag() -> DAG:
    m = _load_smoke_module()
    return DAG.model_validate(m.DAG_1_COMPILE_FLASH_HOST_NATIVE["dag"])


def _task(dag: DAG, task_id: str):
    return next(t for t in dag.tasks if t.task_id == task_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  The committed fixture exists and has the three expected files
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_fixture_files_committed():
    assert (_FIXTURE / "CMakeLists.txt").is_file()
    assert (_FIXTURE / "main.c").is_file()
    assert (_FIXTURE / "run_test.py").is_file()
    # NEVER under test_assets/ (CLAUDE.md read-only ground truth rule).
    assert "test_assets" not in _FIXTURE.parts


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Integration — DAG passes the validator (dep_closure ok)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_smoke_dag_passes_validator():
    result = validate(_smoke_dag())
    assert result.ok, result.summary() + " :: " + repr(
        [e.to_dict() for e in result.errors]
    )


def test_smoke_dag_inputs_are_validator_legal_shape():
    dag = _smoke_dag()
    compile_t = _task(dag, "compile")
    run_test = _task(dag, "run-test")
    # source seeds carry external: so dep_closure accepts them
    assert compile_t.inputs == ["external:CMakeLists.txt", "external:main.c"]
    # build/firmware.bin is compile's upstream output (no prefix needed);
    # run_test.py is an external: seed.
    assert run_test.inputs == ["build/firmware.bin", "external:run_test.py"]
    assert run_test.depends_on == ["compile"]
    # no dep_closure error specifically
    errs = [e for e in validate(dag).errors if e.rule == "dep_closure"]
    assert errs == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Exercised — compile no longer fails on an empty workspace
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.skipif(not (_HAS_CMAKE and _HAS_CC), reason="cmake + C compiler required")
async def test_fixture_compile_builds_firmware(tmp_path):
    dag = _smoke_dag()
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=_FIXTURE,
    )
    handler = dx.LocalTaskHandler(workspace_builder=builder)
    res = await handler.run(700, _task(dag, "compile"))
    assert res.ok, f"compile failed: rc={res.rc} reason={res.reason}\n{res.stderr}"
    assert res.artifact == res.workspace / "build" / "firmware.bin"
    assert res.artifact.exists()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Exercised — run-test scratch receives build/firmware.bin (+ the seed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_run_test_scratch_materialises_both_input_kinds(tmp_path):
    """prepare() must land BOTH the staged upstream output (build/firmware.bin)
    AND the external: source seed (run_test.py) in the run-test scratch."""
    dag = _smoke_dag()
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=_FIXTURE,
    )
    # simulate compile having produced build/firmware.bin, then staged
    upstream = tmp_path / "compiled" / "build" / "firmware.bin"
    upstream.parent.mkdir(parents=True)
    upstream.write_bytes(b"\x7fELF-stub")
    builder.stage_output(701, "build/firmware.bin", upstream)

    ws = builder.prepare(701, _task(dag, "run-test"))
    assert (ws.path / "build" / "firmware.bin").read_bytes() == b"\x7fELF-stub"
    # external:run_test.py -> materialised at its bare path from the fixture
    assert (ws.path / "run_test.py").is_file()
    # the literal prefixed name must NOT be created
    assert not (ws.path / "external:run_test.py").exists()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Integration — both tasks run through the real handler -> both ok
#
#  BLOCKED on the F4 (OP-1676) _python_entry gap: it returns the raw
#  "external:run_test.py" literal, so the python3 task launches a file that
#  prepare() materialised under the bare name "run_test.py" -> rc=2. The fix
#  is a HANDLER INTERNAL (out of this ticket's scope). xfail(strict=False) so
#  this flips to a visible xpass the moment that prefix-stripping lands.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.skipif(not (_HAS_CMAKE and _HAS_CC), reason="cmake + C compiler required")
@pytest.mark.xfail(
    reason="F4/OP-1676 gap: LocalTaskHandler._python_entry does not strip the "
    "external:/user: prefix, so run-test launches 'external:run_test.py' "
    "(rc=2). Handler-internal fix is out of OP-1673 scope.",
    strict=False,
)
async def test_smoke_dag_both_tasks_ok_end_to_end(tmp_path):
    dag = _smoke_dag()
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=_FIXTURE,
    )
    handler = dx.LocalTaskHandler(workspace_builder=builder)

    compile_t = _task(dag, "compile")
    run_test = _task(dag, "run-test")

    r1 = await handler.run(702, compile_t)
    assert r1.ok, f"compile: rc={r1.rc} {r1.reason}\n{r1.stderr}"
    builder.stage_output(702, compile_t.expected_output, r1.artifact)

    r2 = await handler.run(702, run_test)
    assert r2.ok, f"run-test: rc={r2.rc} {r2.reason}\n{r2.stderr}"
    assert r2.artifact == r2.workspace / "logs" / "test.log"
    assert r2.artifact.read_text().startswith("PASS")
