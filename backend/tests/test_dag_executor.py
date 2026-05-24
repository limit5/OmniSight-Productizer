"""OP-1658 — local task handlers (cmake / make / python3) in scratch.

Covers the acceptance criteria for the toolchain → handler dispatch slice
(design doc §7, codex Q7):

  * Code — three local handlers (cmake / make / python3); per-plan workspace
    isolation; an unknown / non-local toolchain (and a non-``t1`` tier)
    FAILS the task instead of crashing; an ``expected_output`` escaping the
    workspace FAILS;
  * Integration — a real ``cmake`` compile and a real ``python3`` step run in
    a scratch workspace and produce the declared artifact (local);
  * Exercised — the ``compile`` → ``run-test`` smoke runs end-to-end locally,
    in dependency order, each task in its own per-plan scratch.

Everything is local-only: no DB, no Docker, no Gerrit, no host state outside
the test's ``tmp_path``. Fixtures live in a test-local tmp dir — NEVER
``test_assets/`` (read-only ground truth per CLAUDE.md).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from backend import dag_executor as dx
from backend.dag_schema import DAG, Task


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers / fixtures (all under tmp_path — never test_assets/)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _task(
    task_id: str, *, toolchain: str = "cmake", tier: str = "t1",
    inputs=None, output: str | None = None, depends_on=None,
) -> Task:
    return Task(
        task_id=task_id,
        description=f"task {task_id}",
        required_tier=tier,
        toolchain=toolchain,
        expected_output=output or f"build/{task_id}",
        inputs=inputs or [],
        depends_on=depends_on or [],
    )


def _handler(tmp_path: Path, *, project: Path | None = None, **kw) -> dx.LocalTaskHandler:
    builder = dx.PlanWorkspaceBuilder(
        workdir_root=tmp_path / "wd", project_root=project,
    )
    return dx.LocalTaskHandler(workspace_builder=builder, **kw)


def _ok_runner(argv, cwd, timeout_s):
    """A fake runner that 'succeeds' without spawning a real process."""
    return 0, f"ran {argv}\n", ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: unknown / non-local toolchain -> FAIL (not a crash)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_unknown_toolchain_fails_not_crash(tmp_path):
    handler = _handler(tmp_path)
    res = await handler.run(1, _task("t", toolchain="flash_board"))
    assert res.status == "failed" and not res.ok
    assert "flash_board" in res.reason
    # gate fired before any workspace was built — nothing for the caller to clean
    assert res.workspace is None
    assert res.rc is None


@pytest.mark.parametrize("tier", ["networked", "t3"])
async def test_nonlocal_tier_fails(tmp_path, tier):
    handler = _handler(tmp_path)
    res = await handler.run(1, _task("t", toolchain="cmake", tier=tier))
    assert res.status == "failed"
    assert tier in res.reason and "t1" in res.reason
    assert res.workspace is None


async def test_known_local_toolchains_are_exactly_three():
    assert set(dx.LOCAL_TOOLCHAINS) == {"cmake", "make", "python3"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: expected_output escaping the workspace -> FAIL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("bad", ["../escape.bin", "/etc/passwd", "a/../../x"])
async def test_expected_output_escape_fails(tmp_path, bad):
    # Injected runner 'succeeds' so we reach the artifact-resolution gate.
    handler = _handler(tmp_path, runner=_ok_runner)
    res = await handler.run(1, _task("t", toolchain="make", output=bad))
    assert res.status == "failed"
    assert "escape" in res.reason.lower() or "workspace" in res.reason.lower()
    # the workspace WAS created (gate is post-prepare) so the caller can clean it
    assert res.workspace is not None and res.workspace.is_dir()


def test_resolve_expected_output_jail(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert dx._resolve_expected_output(ws, "build/app") == ws / "build" / "app"
    for bad in ["../x", "/abs", "a/../../b", ""]:
        with pytest.raises(ValueError):
            dx._resolve_expected_output(ws, bad)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: per-plan workspace isolation + lifecycle (handler creates, no cleanup)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_per_plan_workspace_isolation(tmp_path):
    # Same task_id in two different plans must land in distinct scratch dirs.
    seen: list[Path] = []

    def capture(argv, cwd, timeout_s):
        seen.append(Path(cwd))
        # produce the declared artifact so the task is "ok"
        (Path(cwd) / "build").mkdir(parents=True, exist_ok=True)
        (Path(cwd) / "build" / "shared").write_text("x")
        return 0, "", ""

    handler = _handler(tmp_path, runner=capture)
    r1 = await handler.run(11, _task("shared", toolchain="make", output="build/shared"))
    r2 = await handler.run(22, _task("shared", toolchain="make", output="build/shared"))

    assert r1.ok and r2.ok
    assert r1.workspace == tmp_path / "wd" / "11-shared"
    assert r2.workspace == tmp_path / "wd" / "22-shared"
    assert r1.workspace != r2.workspace
    assert seen == [r1.workspace, r2.workspace]


async def test_handler_does_not_clean_up_workspace(tmp_path):
    # Lifecycle: the handler returns the workspace; the CALLER owns cleanup,
    # so the scratch (and its artifact) must survive after run() returns.
    def produce(argv, cwd, timeout_s):
        (Path(cwd) / "out.bin").write_text("artifact")
        return 0, "", ""

    handler = _handler(tmp_path, runner=produce)
    res = await handler.run(7, _task("t", toolchain="make", output="out.bin"))
    assert res.ok
    assert res.workspace.is_dir()
    assert res.artifact == res.workspace / "out.bin"
    assert res.artifact.read_text() == "artifact"


async def test_nonzero_rc_fails_and_captures_output(tmp_path):
    def boom(argv, cwd, timeout_s):
        return 2, "partial stdout\n", "boom stderr\n"

    handler = _handler(tmp_path, runner=boom)
    res = await handler.run(1, _task("t", toolchain="make", output="x"))
    assert res.status == "failed"
    assert res.rc == 2
    assert "partial stdout" in res.stdout
    assert "boom stderr" in res.stderr
    assert "rc=2" in res.reason


async def test_rc_zero_but_artifact_missing_fails(tmp_path):
    handler = _handler(tmp_path, runner=_ok_runner)  # succeeds, writes nothing
    res = await handler.run(1, _task("t", toolchain="make", output="never_made"))
    assert res.status == "failed"
    assert res.rc == 0
    assert "not produced" in res.reason


async def test_cmake_runs_configure_then_build_in_order(tmp_path):
    calls: list[list[str]] = []

    def record(argv, cwd, timeout_s):
        calls.append(argv)
        # materialise the artifact on the build step
        (Path(cwd) / dx.CMAKE_BUILD_DIR).mkdir(parents=True, exist_ok=True)
        (Path(cwd) / "build" / "app").write_text("bin")
        return 0, "", ""

    handler = _handler(tmp_path, runner=record)
    res = await handler.run(1, _task("c", toolchain="cmake", output="build/app"))
    assert res.ok
    assert calls == [
        ["cmake", "-S", ".", "-B", dx.CMAKE_BUILD_DIR],
        ["cmake", "--build", dx.CMAKE_BUILD_DIR],
    ]


async def test_python3_requires_py_input(tmp_path):
    handler = _handler(tmp_path, runner=_ok_runner)
    res = await handler.run(1, _task("t", toolchain="python3", inputs=[], output="x"))
    assert res.status == "failed"
    assert ".py" in res.reason
    # the prepare-time failure still built (and returned) the workspace
    assert res.workspace is not None


async def test_python3_uses_first_py_input(tmp_path):
    captured: list[list[str]] = []

    def record(argv, cwd, timeout_s):
        captured.append(argv)
        (Path(cwd) / "r.txt").write_text("ok")
        return 0, "", ""

    handler = _handler(tmp_path, runner=record)
    res = await handler.run(
        1, _task("t", toolchain="python3",
                 inputs=["data.bin", "run.py", "other.py"], output="r.txt"),
    )
    assert res.ok
    assert captured == [[sys.executable, "run.py"]]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Integration — a real cmake compile produces the declared artifact
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_HAS_CMAKE = shutil.which("cmake") is not None
_HAS_CC = any(shutil.which(c) for c in ("cc", "gcc", "clang"))
_HAS_MAKE = shutil.which("make") is not None


def _cmake_project(root: Path) -> None:
    """A minimal C project cmake can configure + build into ``build/hello``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\n"
        "project(hello C)\n"
        "add_executable(hello hello.c)\n"
        "set_target_properties(hello PROPERTIES\n"
        "  RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})\n"
    )
    (root / "hello.c").write_text(
        '#include <stdio.h>\nint main(void){printf("hi\\n");return 0;}\n'
    )


@pytest.mark.skipif(not (_HAS_CMAKE and _HAS_CC), reason="cmake + C compiler required")
async def test_integration_real_cmake_compile(tmp_path):
    project = tmp_path / "proj"
    _cmake_project(project)
    handler = _handler(tmp_path, project=project)
    task = _task(
        "compile", toolchain="cmake",
        inputs=["CMakeLists.txt", "hello.c"], output="build/hello",
    )
    res = await handler.run(100, task)
    assert res.ok, f"cmake failed: rc={res.rc} reason={res.reason}\n{res.stderr}"
    assert res.rc == 0
    assert res.artifact == res.workspace / "build" / "hello"
    assert res.artifact.exists()


@pytest.mark.skipif(not _HAS_MAKE, reason="make required")
async def test_integration_real_make(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    # A Makefile whose default target writes the declared artifact.
    (project / "Makefile").write_text(
        "all:\n\tmkdir -p out\n\techo built > out/lib.txt\n"
    )
    handler = _handler(tmp_path, project=project)
    res = await handler.run(
        101, _task("build", toolchain="make", inputs=["Makefile"], output="out/lib.txt"),
    )
    assert res.ok, f"make failed: rc={res.rc} reason={res.reason}\n{res.stderr}"
    assert res.artifact.read_text().strip() == "built"


@pytest.mark.skipif(not _HAS_CMAKE or not _HAS_CC, reason="cmake + C compiler required")
async def test_integration_python3_test_step(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "run_tests.py").write_text(
        "from pathlib import Path\n"
        "Path('report.txt').write_text('PASS')\n"
    )
    handler = _handler(tmp_path, project=project)
    res = await handler.run(
        102, _task("test", toolchain="python3",
                   inputs=["run_tests.py"], output="report.txt"),
    )
    assert res.ok, f"python3 step failed: rc={res.rc} reason={res.reason}\n{res.stderr}"
    assert res.artifact.read_text() == "PASS"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AC: Exercised — compile -> run-test smoke, end-to-end, in dep order
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.skipif(not (_HAS_CMAKE and _HAS_CC), reason="cmake + C compiler required")
async def test_smoke_compile_then_test_end_to_end(tmp_path):
    project = tmp_path / "proj"
    _cmake_project(project)
    (project / "run_tests.py").write_text(
        "from pathlib import Path\n"
        "Path('test-report.txt').write_text('1 passed')\n"
    )
    dag = DAG(dag_id="smoke", tasks=[
        _task("compile", toolchain="cmake",
              inputs=["CMakeLists.txt", "hello.c"], output="build/hello"),
        _task("run-test", toolchain="python3", depends_on=["compile"],
              inputs=["run_tests.py"], output="test-report.txt"),
    ])
    handler = _handler(tmp_path, project=project)

    # Drive the two tasks in dependency order (the scheduler's contract),
    # each in its own per-plan scratch workspace.
    results: dict[str, dx.LocalTaskResult] = {}
    for task in dx.topological_order(dag):
        results[task.task_id] = await handler.run(plan_id=500, task=task)

    assert [tid for tid in results] == ["compile", "run-test"]
    assert results["compile"].ok
    assert results["compile"].artifact.exists()
    assert results["run-test"].ok
    assert results["run-test"].artifact.read_text() == "1 passed"
    # distinct scratch per task under the {plan_id}-{task_id} convention
    assert results["compile"].workspace == tmp_path / "wd" / "500-compile"
    assert results["run-test"].workspace == tmp_path / "wd" / "500-run-test"
