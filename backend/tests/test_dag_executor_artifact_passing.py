"""OP-1676 — inter-task artifact passing (upstream output -> downstream inputs).

Covers the two coupled materialisation gaps the dag_validator already allows
but the executor previously mishandled (design doc gap D+E):

  * UPSTREAM-OUTPUT inputs — an input equal to an upstream task's
    ``expected_output`` is staged by :func:`record_and_finalize_plan` into the
    per-plan staging area, then materialised by EXACT path into the downstream
    task's scratch by :meth:`PlanWorkspaceBuilder.prepare`. Staging WINS over
    ``project_root`` on a path collision.
  * ``external:`` / ``user:`` inputs — the prefix is stripped and the path
    copied from ``project_root`` (the raw literal matched no glob before).

Local harness only — no DB, no Docker, no host state outside ``tmp_path``. The
end-to-end test reuses the in-memory ``backend.workflow`` / ``dag_storage``
doubles from :mod:`backend.tests.test_dag_executor_terminal`.
"""

from __future__ import annotations

from pathlib import Path

from backend import dag_executor as dx
from backend.dag_schema import DAG, Task
from backend.tests.test_dag_executor_terminal import (
    FakeStorage,
    FakeWorkflow,
    _stored_plan,
)


def _task(task_id, *, toolchain="make", tier="t1", inputs=None,
          output=None, depends_on=None) -> Task:
    return Task(
        task_id=task_id, description=f"task {task_id}",
        required_tier=tier, toolchain=toolchain,
        expected_output=output or f"out/{task_id}",
        inputs=inputs or [], depends_on=depends_on or [],
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  prepare(): external:/user: inputs (gap E)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_external_prefixed_input_copied_from_project_root(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)")
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=project,
    )
    ws = builder.prepare(7, _task("compile", inputs=["external:CMakeLists.txt"]))
    # prefix stripped → the real file lands under its bare path
    assert ws.copied == ["CMakeLists.txt"]
    assert (ws.path / "CMakeLists.txt").read_text().startswith("cmake")
    # the literal "external:CMakeLists.txt" must NOT be created
    assert not (ws.path / "external:CMakeLists.txt").exists()


def test_user_prefixed_input_copied_from_project_root(tmp_path):
    project = tmp_path / "proj"
    (project / "cfg").mkdir(parents=True)
    (project / "cfg" / "seed.toml").write_text("k = 1")
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=project,
    )
    ws = builder.prepare(8, _task("t", inputs=["user:cfg/seed.toml"]))
    assert ws.copied == ["cfg/seed.toml"]
    assert (ws.path / "cfg" / "seed.toml").read_text() == "k = 1"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  stage_output + prepare(): upstream-output inputs (gap D)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_staged_upstream_output_materialised_by_exact_path(tmp_path):
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    # an upstream task produced this artifact in its (now-irrelevant) scratch
    upstream = tmp_path / "up" / "build" / "firmware.bin"
    upstream.parent.mkdir(parents=True)
    upstream.write_text("ELF")

    staged = builder.stage_output(3, "build/firmware.bin", upstream)
    assert staged == tmp_path / "wd" / "_plan_outputs" / "3" / "build" / "firmware.bin"

    ws = builder.prepare(3, _task("run-test", inputs=["build/firmware.bin"]))
    assert ws.copied == ["build/firmware.bin"]
    assert (ws.path / "build" / "firmware.bin").read_text() == "ELF"


def test_staging_wins_over_project_root_on_collision(tmp_path):
    project = tmp_path / "proj"
    (project / "build").mkdir(parents=True)
    (project / "build" / "firmware.bin").write_text("STALE-PROJECT")
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=project,
    )
    upstream = tmp_path / "up" / "firmware.bin"
    upstream.parent.mkdir(parents=True)
    upstream.write_text("FRESH-UPSTREAM")
    builder.stage_output(3, "build/firmware.bin", upstream)

    ws = builder.prepare(3, _task("run-test", inputs=["build/firmware.bin"]))
    # the fresh upstream copy wins; the path is not duplicated in `copied`
    assert ws.copied == ["build/firmware.bin"]
    assert (ws.path / "build" / "firmware.bin").read_text() == "FRESH-UPSTREAM"


def test_staged_directory_output_materialised(tmp_path):
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    upstream = tmp_path / "up" / "out"
    upstream.mkdir(parents=True)
    (upstream / "a.txt").write_text("a")
    (upstream / "b.txt").write_text("b")
    builder.stage_output(4, "out", upstream)

    ws = builder.prepare(4, _task("t", inputs=["out"]))
    assert ws.copied == ["out"]
    assert (ws.path / "out" / "a.txt").read_text() == "a"
    assert (ws.path / "out" / "b.txt").read_text() == "b"


def test_missing_staged_output_is_not_materialised(tmp_path):
    # First-run-only limitation: nothing staged (e.g. a re-claim where the
    # producing task's output was already cleaned) → the upstream-output input
    # is simply not materialised, no crash.
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    ws = builder.prepare(9, _task("run-test", inputs=["build/firmware.bin"]))
    assert ws.copied == []
    assert not (ws.path / "build" / "firmware.bin").exists()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  stage_output: non-file io entities + jail
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_stage_output_ignores_non_file_io_entities(tmp_path):
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    art = tmp_path / "x"
    art.write_text("z")
    assert builder.stage_output(1, "git:abcdef0", art) is None
    assert builder.stage_output(1, "issue:OP-1", art) is None
    # nothing was written under the staging root
    assert not (tmp_path / "wd" / "_plan_outputs").exists()


def test_stage_output_missing_artifact_is_noop(tmp_path):
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    assert builder.stage_output(1, "build/x.bin", None) is None
    assert builder.stage_output(1, "build/x.bin", tmp_path / "absent") is None


def test_upstream_output_input_with_dotdot_is_not_materialised(tmp_path):
    # A `..` upstream-output input never escapes the staging jail (and falls
    # through to the project-root glob, which raises as before — covered
    # elsewhere). With no project root bound, it is simply ignored.
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    ws = builder.prepare(2, _task("t", inputs=["../escape.bin"]))
    assert ws.copied == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration — a 2-task DAG: compile -> build/firmware.bin -> run-test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _artifact_runner(seen: dict):
    """Fake runner: records whether each task's scratch already holds the
    upstream artifact, then produces that task's own expected_output."""
    def run(argv, cwd, timeout_s):
        cwd = Path(cwd)
        name = cwd.name  # "{plan_id}-{task_id}"
        seen[name] = (cwd / "build" / "firmware.bin").exists()
        if name.endswith("-compile"):
            (cwd / "build").mkdir(parents=True, exist_ok=True)
            (cwd / "build" / "firmware.bin").write_text("ELF")
        else:  # run-test
            (cwd / "out").mkdir(parents=True, exist_ok=True)
            (cwd / "out" / "test.log").write_text("PASS")
        return 0, "ok\n", ""
    return run


async def test_two_task_dag_passes_artifact_to_downstream_scratch(tmp_path):
    dag = DAG(dag_id="art", tasks=[
        _task("compile", output="build/firmware.bin"),
        _task("run-test", output="out/test.log",
              inputs=["build/firmware.bin"], depends_on=["compile"]),
    ])
    plan = _stored_plan(dag, plan_id=500, run_id="wf-art")
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    seen: dict[str, bool] = {}
    handler = dx.LocalTaskHandler(
        workspace_builder=builder, runner=_artifact_runner(seen),
    )

    res = await dx.record_and_finalize_plan(
        plan, handler=handler, workflow=wf, storage=storage, cleanup=False,
    )

    assert res.status == "completed"
    assert res.recorded == ["compile", "run-test"]
    # compile had no upstream input; run-test's scratch DID receive firmware.bin
    assert seen["500-compile"] is False
    assert seen["500-run-test"] is True
    # and the file is really there on disk (cleanup=False keeps the scratch)
    rt_ws = tmp_path / "wd" / "500-run-test"
    assert (rt_ws / "build" / "firmware.bin").read_text() == "ELF"


async def test_plan_staging_cleaned_after_completed_run(tmp_path):
    dag = DAG(dag_id="art", tasks=[
        _task("compile", output="build/firmware.bin"),
        _task("run-test", output="out/test.log",
              inputs=["build/firmware.bin"], depends_on=["compile"]),
    ])
    plan = _stored_plan(dag, plan_id=501, run_id="wf-art2")
    wf = FakeWorkflow()
    storage = FakeStorage({plan.id: "executing"})
    builder = dx.PlanWorkspaceBuilder(workdir_root=tmp_path / "wd")
    handler = dx.LocalTaskHandler(
        workspace_builder=builder, runner=_artifact_runner({}),
    )

    res = await dx.record_and_finalize_plan(  # cleanup defaults on
        plan, handler=handler, workflow=wf, storage=storage,
    )
    assert res.status == "completed"
    # the executor-owned staging area is removed once the walk is done
    assert not (tmp_path / "wd" / "_plan_outputs" / "501").exists()
