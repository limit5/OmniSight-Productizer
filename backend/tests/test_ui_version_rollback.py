"""V3 #5 (issue #319) — ui_version_rollback contract tests.

Pins ``backend/ui_version_rollback.py`` against the V3 row 5 spec:

  * every operator 「回到此版本」 on a V3 #4 iteration timeline node is
    routed to a ``git checkout`` inside the sandbox workspace (V2 #1);
  * the checkout is followed by a HMR signal to the V2 #2 lifecycle
    so the Next.js preview refreshes;
  * event namespace is ``ui_sandbox.rollback.*`` and disjoint from
    every sibling V2 / V3 module;
  * bad operator input (missing commitSha, non-hex ref) produces
    :class:`InvalidCommitRef` rather than shelling out;
  * git subprocess failures produce :class:`GitCommandError` carrying
    the full argv + stderr tail.

All tests are side-effect free — no real git binary is invoked: a
:class:`FakeGitRunner` feeds scripted results keyed on argv.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from backend import ui_version_rollback as uvr
from backend.ui_sandbox import (
    SandboxConfig,
    SandboxInstance,
    SandboxNotFound,
    SandboxStatus,
)
from backend.ui_version_rollback import (
    DEFAULT_GIT_TIMEOUT_S,
    MAX_FILES_CHANGED,
    ROLLBACK_EVENT_CHECKED_OUT,
    ROLLBACK_EVENT_COMPLETED,
    ROLLBACK_EVENT_FAILED,
    ROLLBACK_EVENT_REQUESTED,
    ROLLBACK_EVENT_TYPES,
    UI_VERSION_ROLLBACK_SCHEMA_VERSION,
    GitCommandError,
    GitCommandResult,
    InvalidCommitRef,
    RollbackRequest,
    RollbackResult,
    RollbackSandboxNotFound,
    SubprocessGitRunner,
    VersionRollback,
    VersionRollbackError,
    is_valid_commit_sha,
    normalize_commit_ref,
    rollback_request_from_snapshot,
    short_commit_sha,
)


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════


class EventRecorder:
    """Thread-safe ``(event_type, data)`` sink for the callback
    firehose."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event_type: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            self._events.append((event_type, dict(data)))

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return list(self._events)

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [d for et, d in self._events if et == event_type]

    def types(self) -> list[str]:
        with self._lock:
            return [et for et, _ in self._events]


class FakeGitRunner:
    """Scripted :class:`GitCommandRunner` for deterministic tests.

    Scripts are keyed on the first positional arg (``rev-parse`` /
    ``checkout`` / ``diff``).  Each slot holds either a single
    :class:`GitCommandResult` (returned every call) or a list that is
    drained one entry per call.  Any unmatched call raises.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []
        self._scripts: dict[str, Any] = {}

    def script(self, subcommand: str, result: Any) -> None:
        self._scripts[subcommand] = result

    def __call__(
        self,
        *args: str,
        cwd: str,
        timeout: float = DEFAULT_GIT_TIMEOUT_S,
    ) -> GitCommandResult:
        self.calls.append((args, cwd))
        if not args:
            raise AssertionError("FakeGitRunner called without args")
        subcommand = args[0]
        if subcommand not in self._scripts:
            raise AssertionError(
                f"FakeGitRunner has no script for subcommand={subcommand!r}; "
                f"call {self.calls[-1]!r}"
            )
        entry = self._scripts[subcommand]
        if isinstance(entry, list):
            if not entry:
                raise AssertionError(
                    f"FakeGitRunner script for {subcommand!r} exhausted"
                )
            result = entry.pop(0)
        else:
            result = entry
        if isinstance(result, Exception):
            raise result
        return result


class FakeSandboxManager:
    """Minimal :class:`SandboxManager` twin — only ``.get`` is used by
    :class:`VersionRollback` so we can swap a tiny fixture in."""

    def __init__(self) -> None:
        self._instances: dict[str, SandboxInstance] = {}

    def put(self, instance: SandboxInstance) -> None:
        self._instances[instance.session_id] = instance

    def get(self, session_id: str) -> SandboxInstance | None:
        return self._instances.get(session_id)


class FakeLifecycle:
    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_exc = raise_exc

    def hot_reload(
        self,
        session_id: str,
        *,
        files_changed: tuple[str, ...] = (),
    ) -> None:
        self.calls.append(
            {"session_id": session_id, "files_changed": tuple(files_changed)}
        )
        if self.raise_exc is not None:
            raise self.raise_exc


FULL_SHA_A = "a" * 40
FULL_SHA_B = "b" * 40
FULL_SHA_C = "c" * 40
SHORT_SHA = "a1b2c3d"


def _git_ok(
    argv: tuple[str, ...], stdout: str = "", stderr: str = ""
) -> GitCommandResult:
    return GitCommandResult(
        argv=argv, returncode=0, stdout=stdout, stderr=stderr
    )


def _git_fail(
    argv: tuple[str, ...], stderr: str = "fatal: bad ref"
) -> GitCommandResult:
    return GitCommandResult(
        argv=argv, returncode=128, stdout="", stderr=stderr
    )


def _sample_config(tmp_path: Path, session_id: str = "sess-1") -> SandboxConfig:
    return SandboxConfig(
        session_id=session_id, workspace_path=str(tmp_path)
    )


def _sample_instance(
    tmp_path: Path, session_id: str = "sess-1"
) -> SandboxInstance:
    return SandboxInstance(
        session_id=session_id,
        container_name=f"sbx-{session_id}",
        config=_sample_config(tmp_path, session_id),
        status=SandboxStatus.running,
    )


def _make_rollbacker(
    *,
    manager: FakeSandboxManager,
    git_runner: FakeGitRunner,
    lifecycle: Any = None,
    event_cb: Callable | None = None,
    clock: Callable[[], float] | None = None,
    max_files_changed: int = MAX_FILES_CHANGED,
) -> VersionRollback:
    clock_fn = clock or (lambda: 1000.0)
    return VersionRollback(
        manager=manager,  # type: ignore[arg-type]
        lifecycle=lifecycle,
        git_runner=git_runner,
        event_cb=event_cb,
        clock=clock_fn,
        max_files_changed=max_files_changed,
    )


def _script_resolve(
    runner: FakeGitRunner,
    *,
    head: str | None = FULL_SHA_A,
    target: str = FULL_SHA_B,
) -> None:
    """Set up rev-parse HEAD → ``head`` (or failure when head=None) and
    rev-parse target → ``target``."""

    entries: list[GitCommandResult | Exception] = []
    if head is None:
        entries.append(
            _git_fail(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                stderr="fatal: needed a single revision",
            )
        )
    else:
        entries.append(
            _git_ok(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                stdout=head + "\n",
            )
        )
    entries.append(
        _git_ok(
            ("git", "rev-parse", "--verify", f"{target}^{{commit}}"),
            stdout=target + "\n",
        )
    )
    runner.script("rev-parse", entries)


def _script_checkout_ok(runner: FakeGitRunner, sha: str) -> None:
    runner.script(
        "checkout",
        _git_ok(("git", "checkout", "--detach", "--force", sha)),
    )


def _script_diff(
    runner: FakeGitRunner,
    *,
    previous: str,
    target: str,
    paths: tuple[str, ...],
) -> None:
    stdout = "\n".join(paths) + ("\n" if paths else "")
    runner.script(
        "diff",
        _git_ok(
            ("git", "diff", "--name-only", f"{previous}..{target}"),
            stdout=stdout,
        ),
    )


# ═══════════════════════════════════════════════════════════════════
#  Module invariants
# ═══════════════════════════════════════════════════════════════════


EXPECTED_ALL = {
    "UI_VERSION_ROLLBACK_SCHEMA_VERSION",
    "MAX_FILES_CHANGED",
    "DEFAULT_GIT_TIMEOUT_S",
    "ROLLBACK_EVENT_REQUESTED",
    "ROLLBACK_EVENT_CHECKED_OUT",
    "ROLLBACK_EVENT_COMPLETED",
    "ROLLBACK_EVENT_FAILED",
    "ROLLBACK_EVENT_TYPES",
    "VersionRollbackError",
    "InvalidCommitRef",
    "GitCommandError",
    "RollbackSandboxNotFound",
    "GitCommandResult",
    "GitCommandRunner",
    "SubprocessGitRunner",
    "RollbackRequest",
    "RollbackResult",
    "VersionRollback",
    "is_valid_commit_sha",
    "short_commit_sha",
    "normalize_commit_ref",
    "rollback_request_from_snapshot",
}


def test_all_exports_match():
    assert set(uvr.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(uvr, name)


def test_schema_version_is_semver():
    parts = UI_VERSION_ROLLBACK_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_default_git_timeout_is_positive():
    assert DEFAULT_GIT_TIMEOUT_S > 0
    assert isinstance(DEFAULT_GIT_TIMEOUT_S, (int, float))


def test_max_files_changed_is_positive():
    assert MAX_FILES_CHANGED >= 1


def test_event_types_all_in_rollback_namespace():
    for name in ROLLBACK_EVENT_TYPES:
        assert name.startswith("ui_sandbox.rollback.")


def test_event_types_are_unique():
    assert len(ROLLBACK_EVENT_TYPES) == len(set(ROLLBACK_EVENT_TYPES))


def test_event_types_includes_all_event_constants():
    assert set(ROLLBACK_EVENT_TYPES) == {
        ROLLBACK_EVENT_REQUESTED,
        ROLLBACK_EVENT_CHECKED_OUT,
        ROLLBACK_EVENT_COMPLETED,
        ROLLBACK_EVENT_FAILED,
    }


def test_event_types_order_is_lifecycle_happy_path():
    # requested first; failed last.
    assert ROLLBACK_EVENT_TYPES[0] == ROLLBACK_EVENT_REQUESTED
    assert ROLLBACK_EVENT_TYPES[-1] == ROLLBACK_EVENT_FAILED


def test_event_namespace_disjoint_from_v2_and_v3():
    from backend.ui_agent_visual_context import (
        AGENT_VISUAL_CONTEXT_EVENT_TYPES,
    )
    from backend.ui_annotation_context import ANNOTATION_CONTEXT_EVENT_TYPES
    from backend.ui_preview_error_bridge import ERROR_EVENT_TYPES
    from backend.ui_responsive_viewport import VIEWPORT_BATCH_EVENT_TYPES
    from backend.ui_sandbox_lifecycle import LIFECYCLE_EVENT_TYPES
    from backend.ui_screenshot import SCREENSHOT_EVENT_TYPES

    ours = set(ROLLBACK_EVENT_TYPES)
    for other in (
        LIFECYCLE_EVENT_TYPES,
        SCREENSHOT_EVENT_TYPES,
        VIEWPORT_BATCH_EVENT_TYPES,
        ERROR_EVENT_TYPES,
        AGENT_VISUAL_CONTEXT_EVENT_TYPES,
        ANNOTATION_CONTEXT_EVENT_TYPES,
    ):
        assert ours.isdisjoint(set(other))


def test_error_classes_inherit_from_value_error():
    assert issubclass(VersionRollbackError, ValueError)
    assert issubclass(InvalidCommitRef, VersionRollbackError)
    assert issubclass(GitCommandError, VersionRollbackError)
    assert issubclass(RollbackSandboxNotFound, VersionRollbackError)


# ═══════════════════════════════════════════════════════════════════
#  is_valid_commit_sha
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "value,expected",
    [
        ("a" * 40, True),
        ("0" * 40, True),
        ("abcd", True),  # minimum length 4
        ("abc", False),  # too short
        ("a" * 41, False),  # too long
        ("A" * 40, False),  # uppercase rejected
        ("g" * 40, False),  # non-hex
        ("", False),
        ("a1b2c3d", True),
        ("   a1b2c3d   ", False),  # no implicit strip
    ],
)
def test_is_valid_commit_sha(value: str, expected: bool):
    assert is_valid_commit_sha(value) is expected


def test_is_valid_commit_sha_rejects_non_string():
    assert is_valid_commit_sha(None) is False
    assert is_valid_commit_sha(123) is False
    assert is_valid_commit_sha(b"a" * 40) is False


# ═══════════════════════════════════════════════════════════════════
#  short_commit_sha
# ═══════════════════════════════════════════════════════════════════


def test_short_commit_sha_truncates_long():
    assert short_commit_sha("a" * 40) == "a" * 7


def test_short_commit_sha_passes_through_short():
    assert short_commit_sha("a1b2") == "a1b2"


def test_short_commit_sha_passes_through_exact_length():
    assert short_commit_sha("abcdefg") == "abcdefg"


def test_short_commit_sha_custom_length():
    assert short_commit_sha("a" * 40, length=10) == "a" * 10


def test_short_commit_sha_rejects_bad_length():
    with pytest.raises(ValueError):
        short_commit_sha("abc", length=0)
    with pytest.raises(ValueError):
        short_commit_sha("abc", length=-1)


@pytest.mark.parametrize(
    "value",
    [None, 123, 0.0, b"bytes", [], {}],
)
def test_short_commit_sha_non_string_returns_empty(value: Any):
    assert short_commit_sha(value) == ""


def test_short_commit_sha_blank_returns_empty():
    assert short_commit_sha("") == ""
    assert short_commit_sha("   ") == ""


def test_short_commit_sha_strips_whitespace():
    assert short_commit_sha("  a1b2c3d  ") == "a1b2c3d"


# ═══════════════════════════════════════════════════════════════════
#  normalize_commit_ref
# ═══════════════════════════════════════════════════════════════════


def test_normalize_commit_ref_lowercases_and_trims():
    assert normalize_commit_ref("  A1B2C3D  ") == "a1b2c3d"


def test_normalize_commit_ref_accepts_full_sha():
    sha = "a" * 40
    assert normalize_commit_ref(sha) == sha


def test_normalize_commit_ref_rejects_bad_format():
    with pytest.raises(InvalidCommitRef):
        normalize_commit_ref("not-a-sha")


def test_normalize_commit_ref_rejects_non_string():
    with pytest.raises(InvalidCommitRef):
        normalize_commit_ref(None)  # type: ignore[arg-type]
    with pytest.raises(InvalidCommitRef):
        normalize_commit_ref(12345)  # type: ignore[arg-type]


def test_normalize_commit_ref_rejects_blank():
    with pytest.raises(InvalidCommitRef):
        normalize_commit_ref("")
    with pytest.raises(InvalidCommitRef):
        normalize_commit_ref("   ")


# ═══════════════════════════════════════════════════════════════════
#  rollback_request_from_snapshot
# ═══════════════════════════════════════════════════════════════════


def test_rollback_request_from_snapshot_happy():
    request = rollback_request_from_snapshot(
        session_id="sess-1",
        snapshot={"id": "iter-7", "commitSha": FULL_SHA_A},
    )
    assert request.session_id == "sess-1"
    assert request.iteration_id == "iter-7"
    assert request.commit_sha == FULL_SHA_A


def test_rollback_request_from_snapshot_accepts_short_sha():
    request = rollback_request_from_snapshot(
        session_id="sess-1",
        snapshot={"id": "iter-7", "commitSha": SHORT_SHA},
    )
    assert request.commit_sha == SHORT_SHA


def test_rollback_request_from_snapshot_normalises_case():
    request = rollback_request_from_snapshot(
        session_id="sess-1",
        snapshot={"id": "iter-7", "commitSha": "A1B2C3D"},
    )
    assert request.commit_sha == "a1b2c3d"


def test_rollback_request_from_snapshot_rejects_missing_sha():
    with pytest.raises(InvalidCommitRef):
        rollback_request_from_snapshot(
            session_id="sess-1",
            snapshot={"id": "iter-7", "commitSha": None},
        )


def test_rollback_request_from_snapshot_rejects_blank_sha():
    with pytest.raises(InvalidCommitRef):
        rollback_request_from_snapshot(
            session_id="sess-1",
            snapshot={"id": "iter-7", "commitSha": "   "},
        )


def test_rollback_request_from_snapshot_rejects_bad_format():
    with pytest.raises(InvalidCommitRef):
        rollback_request_from_snapshot(
            session_id="sess-1",
            snapshot={"id": "iter-7", "commitSha": "not-a-sha"},
        )


def test_rollback_request_from_snapshot_accepts_missing_id():
    request = rollback_request_from_snapshot(
        session_id="sess-1",
        snapshot={"commitSha": FULL_SHA_A},
    )
    assert request.iteration_id is None


def test_rollback_request_from_snapshot_rejects_blank_id():
    with pytest.raises(VersionRollbackError):
        rollback_request_from_snapshot(
            session_id="sess-1",
            snapshot={"id": "", "commitSha": FULL_SHA_A},
        )


def test_rollback_request_from_snapshot_rejects_bad_snapshot_shape():
    with pytest.raises(VersionRollbackError):
        rollback_request_from_snapshot(
            session_id="sess-1",
            snapshot="not a mapping",  # type: ignore[arg-type]
        )


def test_rollback_request_from_snapshot_rejects_blank_session():
    with pytest.raises(VersionRollbackError):
        rollback_request_from_snapshot(
            session_id="",
            snapshot={"commitSha": FULL_SHA_A},
        )


def test_rollback_request_from_snapshot_passes_reason():
    request = rollback_request_from_snapshot(
        session_id="sess-1",
        snapshot={"commitSha": FULL_SHA_A},
        reason="operator undid glass morphism",
    )
    assert request.reason == "operator undid glass morphism"


def test_rollback_request_from_snapshot_parses_v3_4_wire_shape():
    # V3 #4 IterationSnapshot wire shape — exact mirror.
    snapshot = {
        "id": "iter-42",
        "commitSha": FULL_SHA_A,
        "screenshotSrc": "data:image/png;base64,AAAA",
        "screenshotAlt": "dashboard",
        "diff": "diff --git a/x b/x\n+line",
        "summary": "agent added header",
        "agentId": "software-beta",
        "createdAt": "2026-04-18T10:00:00Z",
        "diffStats": {"additions": 1, "deletions": 0, "files": 1},
    }
    request = rollback_request_from_snapshot(
        session_id="sess-1", snapshot=snapshot
    )
    assert request.iteration_id == "iter-42"
    assert request.commit_sha == FULL_SHA_A


# ═══════════════════════════════════════════════════════════════════
#  RollbackRequest
# ═══════════════════════════════════════════════════════════════════


def test_rollback_request_happy():
    request = RollbackRequest(
        session_id="sess-1",
        commit_sha=FULL_SHA_A,
        iteration_id="iter-1",
        reason="because",
    )
    assert request.session_id == "sess-1"
    assert request.commit_sha == FULL_SHA_A


def test_rollback_request_is_frozen():
    request = RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_A)
    with pytest.raises(Exception):  # FrozenInstanceError
        request.session_id = "other"  # type: ignore[misc]


def test_rollback_request_rejects_blank_session():
    with pytest.raises(VersionRollbackError):
        RollbackRequest(session_id="", commit_sha=FULL_SHA_A)


def test_rollback_request_rejects_bad_sha():
    with pytest.raises(InvalidCommitRef):
        RollbackRequest(session_id="sess-1", commit_sha="not-a-sha")


def test_rollback_request_rejects_blank_iteration_id():
    with pytest.raises(VersionRollbackError):
        RollbackRequest(
            session_id="sess-1",
            commit_sha=FULL_SHA_A,
            iteration_id="",
        )


def test_rollback_request_rejects_non_string_reason():
    with pytest.raises(VersionRollbackError):
        RollbackRequest(
            session_id="sess-1",
            commit_sha=FULL_SHA_A,
            reason=123,  # type: ignore[arg-type]
        )


def test_rollback_request_to_dict_has_short_sha():
    request = RollbackRequest(
        session_id="sess-1",
        commit_sha=FULL_SHA_A,
        iteration_id="iter-1",
    )
    d = request.to_dict()
    assert d["commit_sha"] == FULL_SHA_A
    assert d["short_sha"] == FULL_SHA_A[:7]
    assert d["iteration_id"] == "iter-1"


def test_rollback_request_to_dict_omits_optional_fields():
    request = RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_A)
    d = request.to_dict()
    assert "iteration_id" not in d
    assert "reason" not in d


# ═══════════════════════════════════════════════════════════════════
#  GitCommandResult
# ═══════════════════════════════════════════════════════════════════


def test_git_command_result_ok_when_zero():
    r = GitCommandResult(argv=("git", "status"), returncode=0, stdout="", stderr="")
    assert r.ok is True


def test_git_command_result_not_ok_when_nonzero():
    r = GitCommandResult(
        argv=("git", "status"), returncode=128, stdout="", stderr="fatal"
    )
    assert r.ok is False


def test_git_command_result_to_dict():
    r = GitCommandResult(
        argv=("git", "rev-parse"),
        returncode=0,
        stdout="abc\n",
        stderr="",
    )
    d = r.to_dict()
    assert d["argv"] == ["git", "rev-parse"]
    assert d["returncode"] == 0
    assert d["stdout"] == "abc\n"


def test_git_command_result_rejects_non_tuple_argv():
    with pytest.raises(ValueError):
        GitCommandResult(argv=["git"], returncode=0, stdout="", stderr="")  # type: ignore[arg-type]


def test_git_command_result_is_frozen():
    r = GitCommandResult(argv=("git",), returncode=0, stdout="", stderr="")
    with pytest.raises(Exception):
        r.returncode = 1  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════
#  SubprocessGitRunner (surface only — no real git call)
# ═══════════════════════════════════════════════════════════════════


def test_subprocess_git_runner_default_binary():
    runner = SubprocessGitRunner()
    assert runner._git_binary == "git"  # noqa: SLF001


def test_subprocess_git_runner_custom_binary():
    runner = SubprocessGitRunner(git_binary="/usr/bin/git")
    assert runner._git_binary == "/usr/bin/git"  # noqa: SLF001


def test_subprocess_git_runner_rejects_blank_binary():
    with pytest.raises(ValueError):
        SubprocessGitRunner(git_binary="")


def test_subprocess_git_runner_rejects_empty_args(tmp_path: Path):
    runner = SubprocessGitRunner()
    with pytest.raises(ValueError):
        runner(cwd=str(tmp_path))


def test_subprocess_git_runner_rejects_non_string_args(tmp_path: Path):
    runner = SubprocessGitRunner()
    with pytest.raises(TypeError):
        runner("rev-parse", 123, cwd=str(tmp_path))  # type: ignore[arg-type]


def test_subprocess_git_runner_missing_binary_raises(tmp_path: Path):
    runner = SubprocessGitRunner(git_binary="/definitely/not/a/git/binary")
    with pytest.raises(GitCommandError) as excinfo:
        runner("status", cwd=str(tmp_path))
    assert excinfo.value.returncode == -1


# ═══════════════════════════════════════════════════════════════════
#  VersionRollback — ctor & accessors
# ═══════════════════════════════════════════════════════════════════


def test_version_rollback_rejects_none_manager():
    with pytest.raises(TypeError):
        VersionRollback(manager=None)  # type: ignore[arg-type]


def test_version_rollback_rejects_non_callable_clock(tmp_path: Path):
    mgr = FakeSandboxManager()
    with pytest.raises(TypeError):
        VersionRollback(
            manager=mgr,  # type: ignore[arg-type]
            git_runner=FakeGitRunner(),
            clock="not-callable",  # type: ignore[arg-type]
        )


def test_version_rollback_rejects_bad_max_files():
    mgr = FakeSandboxManager()
    with pytest.raises(ValueError):
        VersionRollback(
            manager=mgr,  # type: ignore[arg-type]
            git_runner=FakeGitRunner(),
            max_files_changed=0,
        )


def test_version_rollback_rejects_non_positive_timeout():
    mgr = FakeSandboxManager()
    with pytest.raises(ValueError):
        VersionRollback(
            manager=mgr,  # type: ignore[arg-type]
            git_runner=FakeGitRunner(),
            git_timeout_s=0,
        )


def test_version_rollback_default_git_runner_is_subprocess():
    mgr = FakeSandboxManager()
    rb = VersionRollback(manager=mgr)  # type: ignore[arg-type]
    assert isinstance(rb._git_runner, SubprocessGitRunner)  # noqa: SLF001


def test_version_rollback_counters_start_at_zero():
    mgr = FakeSandboxManager()
    rb = VersionRollback(
        manager=mgr,  # type: ignore[arg-type]
        git_runner=FakeGitRunner(),
    )
    assert rb.rollback_count() == 0
    assert rb.failure_count() == 0
    assert rb.noop_count() == 0
    assert rb.last_result() is None
    assert rb.last_error() is None


# ═══════════════════════════════════════════════════════════════════
#  VersionRollback.rollback — happy paths
# ═══════════════════════════════════════════════════════════════════


def test_rollback_happy_path_emits_three_events(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(
        runner,
        previous=FULL_SHA_A,
        target=FULL_SHA_B,
        paths=("app/layout.tsx", "components/Header.tsx"),
    )
    recorder = EventRecorder()
    lifecycle = FakeLifecycle()
    rb = _make_rollbacker(
        manager=mgr,
        git_runner=runner,
        lifecycle=lifecycle,
        event_cb=recorder,
    )

    result = rb.rollback(
        RollbackRequest(
            session_id="sess-1",
            commit_sha=FULL_SHA_B,
            iteration_id="iter-42",
        )
    )

    assert recorder.types() == [
        ROLLBACK_EVENT_REQUESTED,
        ROLLBACK_EVENT_CHECKED_OUT,
        ROLLBACK_EVENT_COMPLETED,
    ]
    assert result.previous_sha == FULL_SHA_A
    assert result.resolved_sha == FULL_SHA_B
    assert result.files_changed == ("app/layout.tsx", "components/Header.tsx")
    assert result.preview_refresh_requested is True
    assert lifecycle.calls == [
        {
            "session_id": "sess-1",
            "files_changed": ("app/layout.tsx", "components/Header.tsx"),
        }
    ]


def test_rollback_result_to_dict_shape(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(
        runner,
        previous=FULL_SHA_A,
        target=FULL_SHA_B,
        paths=("a.tsx",),
    )
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    d = result.to_dict()
    for key in (
        "schema_version",
        "session_id",
        "iteration_id",
        "requested_sha",
        "resolved_sha",
        "previous_sha",
        "short_sha",
        "files_changed",
        "file_count",
        "files_changed_total",
        "truncated",
        "is_noop",
        "preview_refresh_requested",
        "checked_out_at",
        "reason",
        "warnings",
    ):
        assert key in d
    assert d["schema_version"] == UI_VERSION_ROLLBACK_SCHEMA_VERSION
    assert d["short_sha"] == FULL_SHA_B[:7]
    assert d["files_changed"] == ["a.tsx"]
    assert d["file_count"] == 1
    assert d["truncated"] is False


def test_rollback_updates_counters(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    rb.rollback(RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B))
    assert rb.rollback_count() == 1
    assert rb.failure_count() == 0
    assert rb.last_result() is not None


def test_rollback_noop_when_already_on_target(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_A)
    _script_checkout_ok(runner, FULL_SHA_A)
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_A)
    )
    assert result.is_noop is True
    assert result.files_changed == ()
    assert result.previous_sha == FULL_SHA_A
    assert result.resolved_sha == FULL_SHA_A
    assert rb.noop_count() == 1


def test_rollback_without_lifecycle_leaves_refresh_false(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    rb = _make_rollbacker(manager=mgr, git_runner=runner, lifecycle=None)
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    assert result.preview_refresh_requested is False


def test_rollback_records_lifecycle_warning(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    lifecycle = FakeLifecycle(
        raise_exc=SandboxNotFound("stale container")
    )
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, lifecycle=lifecycle
    )
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    assert result.preview_refresh_requested is False
    assert len(result.warnings) == 1
    assert "preview refresh skipped" in result.warnings[0]


def test_rollback_uses_detach_force(tmp_path: Path):
    """Checkout MUST use --detach --force so we don't move HEAD of a
    named branch and we clobber any dirty working tree — rollback is
    destructive by design."""

    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    rb.rollback(RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B))
    checkout_call = next(
        args for args, _cwd in runner.calls if args[0] == "checkout"
    )
    assert "--detach" in checkout_call
    assert "--force" in checkout_call
    assert checkout_call[-1] == FULL_SHA_B


def test_rollback_cwd_is_workspace_path(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    rb.rollback(RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B))
    assert all(cwd == str(tmp_path) for _args, cwd in runner.calls)


def test_rollback_clocks_checked_out_at(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    ticks = iter([1000.0, 1001.0, 1002.0, 1003.0, 1004.0, 1005.0])
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, clock=lambda: next(ticks)
    )
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    assert result.checked_out_at > 1000.0


def test_rollback_fresh_workspace_without_head(tmp_path: Path):
    """HEAD rev-parse failure (fresh sandbox, no commits) should not
    abort rollback — previous_sha becomes None and we skip the diff
    step."""

    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=None, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    assert result.previous_sha is None
    assert result.files_changed == ()
    assert result.is_noop is False


# ═══════════════════════════════════════════════════════════════════
#  VersionRollback.rollback — failure paths
# ═══════════════════════════════════════════════════════════════════


def test_rollback_sandbox_missing(tmp_path: Path):
    mgr = FakeSandboxManager()  # no instance registered
    runner = FakeGitRunner()
    recorder = EventRecorder()
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, event_cb=recorder
    )
    with pytest.raises(RollbackSandboxNotFound):
        rb.rollback(
            RollbackRequest(session_id="ghost", commit_sha=FULL_SHA_B)
        )
    assert recorder.types() == [
        ROLLBACK_EVENT_REQUESTED,
        ROLLBACK_EVENT_FAILED,
    ]
    assert rb.failure_count() == 1
    assert rb.rollback_count() == 0


def test_rollback_git_resolve_fails(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    # HEAD ok, target rev-parse fails.
    runner.script(
        "rev-parse",
        [
            _git_ok(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                stdout=FULL_SHA_A + "\n",
            ),
            _git_fail(
                (
                    "git",
                    "rev-parse",
                    "--verify",
                    f"{FULL_SHA_B}^{{commit}}",
                ),
                stderr="fatal: unknown ref",
            ),
        ],
    )
    recorder = EventRecorder()
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, event_cb=recorder
    )
    with pytest.raises(GitCommandError):
        rb.rollback(
            RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
        )
    assert recorder.types() == [
        ROLLBACK_EVENT_REQUESTED,
        ROLLBACK_EVENT_FAILED,
    ]


def test_rollback_git_checkout_fails(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    runner.script(
        "checkout",
        _git_fail(
            ("git", "checkout", "--detach", "--force", FULL_SHA_B),
            stderr="fatal: working tree dirty",
        ),
    )
    recorder = EventRecorder()
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, event_cb=recorder
    )
    with pytest.raises(GitCommandError) as excinfo:
        rb.rollback(
            RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
        )
    assert "fatal: working tree dirty" in excinfo.value.stderr
    assert recorder.types() == [
        ROLLBACK_EVENT_REQUESTED,
        ROLLBACK_EVENT_FAILED,
    ]


def test_rollback_diff_failure_falls_back_to_warning(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    runner.script(
        "diff",
        _git_fail(
            ("git", "diff", "--name-only", f"{FULL_SHA_A}..{FULL_SHA_B}"),
            stderr="fatal: bad revision",
        ),
    )
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    assert result.files_changed == ()
    assert len(result.warnings) == 1
    assert "diff_name_only failed" in result.warnings[0]


def test_rollback_rev_parse_non_sha_output_raises(tmp_path: Path):
    """When rev-parse succeeds but stdout is not a SHA (eg, git
    returned an arbitrary string for a non-commit ref) we refuse to
    checkout and surface a :class:`GitCommandError`."""

    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    runner.script(
        "rev-parse",
        [
            # HEAD resolves fine.
            _git_ok(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                stdout=FULL_SHA_A + "\n",
            ),
            # Target rev-parse returns non-SHA stdout.
            _git_ok(
                (
                    "git",
                    "rev-parse",
                    "--verify",
                    f"{FULL_SHA_B}^{{commit}}",
                ),
                stdout="not-a-sha\n",
            ),
        ],
    )
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    with pytest.raises(GitCommandError):
        rb.rollback(
            RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
        )


def test_rollback_last_error_set_on_failure(tmp_path: Path):
    mgr = FakeSandboxManager()
    runner = FakeGitRunner()
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    with pytest.raises(RollbackSandboxNotFound):
        rb.rollback(
            RollbackRequest(session_id="ghost", commit_sha=FULL_SHA_B)
        )
    assert rb.last_error() is not None
    assert "RollbackSandboxNotFound" in rb.last_error()  # type: ignore[operator]


def test_rollback_non_request_type(tmp_path: Path):
    mgr = FakeSandboxManager()
    rb = _make_rollbacker(manager=mgr, git_runner=FakeGitRunner())
    with pytest.raises(TypeError):
        rb.rollback({"session_id": "sess-1"})  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  VersionRollback.rollback_from_snapshot
# ═══════════════════════════════════════════════════════════════════


def test_rollback_from_snapshot_happy(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    result = rb.rollback_from_snapshot(
        session_id="sess-1",
        snapshot={"id": "iter-7", "commitSha": FULL_SHA_B},
    )
    assert result.iteration_id == "iter-7"
    assert result.requested_sha == FULL_SHA_B


def test_rollback_from_snapshot_missing_sha_raises_before_git(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    with pytest.raises(InvalidCommitRef):
        rb.rollback_from_snapshot(
            session_id="sess-1",
            snapshot={"id": "iter-7", "commitSha": None},
        )
    assert runner.calls == []


# ═══════════════════════════════════════════════════════════════════
#  Events — envelope shape
# ═══════════════════════════════════════════════════════════════════


def test_requested_event_envelope_keys(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    recorder = EventRecorder()
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, event_cb=recorder
    )
    rb.rollback(
        RollbackRequest(
            session_id="sess-1",
            commit_sha=FULL_SHA_B,
            iteration_id="iter-1",
            reason="operator rolled back",
        )
    )
    payload = recorder.by_type(ROLLBACK_EVENT_REQUESTED)[0]
    for key in (
        "schema_version",
        "session_id",
        "iteration_id",
        "requested_sha",
        "short_sha",
        "reason",
        "at",
    ):
        assert key in payload
    assert payload["short_sha"] == FULL_SHA_B[:7]
    assert payload["reason"] == "operator rolled back"


def test_checked_out_event_envelope_keys(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    recorder = EventRecorder()
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, event_cb=recorder
    )
    rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    payload = recorder.by_type(ROLLBACK_EVENT_CHECKED_OUT)[0]
    for key in (
        "schema_version",
        "session_id",
        "requested_sha",
        "resolved_sha",
        "previous_sha",
        "short_sha",
        "at",
    ):
        assert key in payload
    assert payload["resolved_sha"] == FULL_SHA_B
    assert payload["previous_sha"] == FULL_SHA_A


def test_completed_event_envelope_keys(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(
        runner,
        previous=FULL_SHA_A,
        target=FULL_SHA_B,
        paths=("a.tsx", "b.tsx"),
    )
    recorder = EventRecorder()
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, event_cb=recorder
    )
    rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    payload = recorder.by_type(ROLLBACK_EVENT_COMPLETED)[0]
    for key in (
        "schema_version",
        "session_id",
        "resolved_sha",
        "previous_sha",
        "short_sha",
        "file_count",
        "files_changed_total",
        "files_preview",
        "truncated",
        "is_noop",
        "preview_refresh_requested",
        "warning_count",
        "checked_out_at",
    ):
        assert key in payload
    assert payload["file_count"] == 2
    assert payload["files_preview"] == ["a.tsx", "b.tsx"]


def test_failed_event_envelope_keys(tmp_path: Path):
    mgr = FakeSandboxManager()
    runner = FakeGitRunner()
    recorder = EventRecorder()
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, event_cb=recorder
    )
    with pytest.raises(RollbackSandboxNotFound):
        rb.rollback(
            RollbackRequest(session_id="ghost", commit_sha=FULL_SHA_B)
        )
    payload = recorder.by_type(ROLLBACK_EVENT_FAILED)[0]
    for key in (
        "schema_version",
        "session_id",
        "error",
        "error_type",
        "at",
    ):
        assert key in payload
    assert payload["error_type"] == "RollbackSandboxNotFound"


def test_event_cb_exception_is_swallowed(tmp_path: Path):
    """A broken ``event_cb`` must not break the rollback — we log a
    warning and press on."""

    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())

    def boom(event_type: str, data: Mapping[str, Any]) -> None:
        raise RuntimeError("subscriber melted")

    rb = _make_rollbacker(manager=mgr, git_runner=runner, event_cb=boom)
    # Should NOT raise.
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    assert result.resolved_sha == FULL_SHA_B


# ═══════════════════════════════════════════════════════════════════
#  Truncation
# ═══════════════════════════════════════════════════════════════════


def test_files_changed_truncates_when_above_cap(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    paths = tuple(f"f{i}.tsx" for i in range(50))
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(
        runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=paths
    )
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, max_files_changed=10
    )
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    assert len(result.files_changed) == 10
    assert result.files_changed_total == 50
    assert result.truncated is True


def test_files_changed_not_truncated_when_below_cap(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(
        runner,
        previous=FULL_SHA_A,
        target=FULL_SHA_B,
        paths=("a.tsx", "b.tsx", "c.tsx"),
    )
    rb = _make_rollbacker(
        manager=mgr, git_runner=runner, max_files_changed=10
    )
    result = rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    assert result.truncated is False
    assert result.files_changed_total == 3


def test_completed_event_files_preview_capped_at_20(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    paths = tuple(f"f{i}.tsx" for i in range(40))
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(
        runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=paths
    )
    recorder = EventRecorder()
    rb = _make_rollbacker(
        manager=mgr,
        git_runner=runner,
        event_cb=recorder,
        max_files_changed=100,
    )
    rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    payload = recorder.by_type(ROLLBACK_EVENT_COMPLETED)[0]
    assert len(payload["files_preview"]) == 20
    assert payload["file_count"] == 40
    assert payload["files_changed_total"] == 40


# ═══════════════════════════════════════════════════════════════════
#  Snapshot
# ═══════════════════════════════════════════════════════════════════


def test_snapshot_initial_state():
    mgr = FakeSandboxManager()
    rb = _make_rollbacker(manager=mgr, git_runner=FakeGitRunner())
    snap = rb.snapshot()
    assert snap["schema_version"] == UI_VERSION_ROLLBACK_SCHEMA_VERSION
    assert snap["rollback_count"] == 0
    assert snap["failure_count"] == 0
    assert snap["noop_count"] == 0
    assert snap["last_result"] is None
    assert snap["last_error"] is None


def test_snapshot_after_success(tmp_path: Path):
    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    snap = rb.snapshot()
    assert snap["rollback_count"] == 1
    assert snap["failure_count"] == 0
    assert snap["last_result"]["resolved_sha"] == FULL_SHA_B
    assert snap["last_error"] is None


def test_snapshot_json_safe(tmp_path: Path):
    import json

    mgr = FakeSandboxManager()
    mgr.put(_sample_instance(tmp_path))
    runner = FakeGitRunner()
    _script_resolve(runner, head=FULL_SHA_A, target=FULL_SHA_B)
    _script_checkout_ok(runner, FULL_SHA_B)
    _script_diff(runner, previous=FULL_SHA_A, target=FULL_SHA_B, paths=())
    rb = _make_rollbacker(manager=mgr, git_runner=runner)
    rb.rollback(
        RollbackRequest(session_id="sess-1", commit_sha=FULL_SHA_B)
    )
    round_trip = json.loads(json.dumps(rb.snapshot()))
    assert round_trip["rollback_count"] == 1


# ═══════════════════════════════════════════════════════════════════
#  Thread safety
# ═══════════════════════════════════════════════════════════════════


def test_version_rollback_is_thread_safe(tmp_path: Path):
    """4 threads × 5 rollbacks concurrent should produce exactly 20
    increments of ``rollback_count`` — no lost updates."""

    mgr = FakeSandboxManager()
    # 4 sessions so each thread has its own sandbox.
    tmps: dict[str, Path] = {}
    for sid in ("a", "b", "c", "d"):
        ws = tmp_path / sid
        ws.mkdir()
        tmps[sid] = ws
        mgr.put(_sample_instance(ws, session_id=sid))

    class ThreadSafeGitRunner:
        """Stateless by-design: returns canned results regardless of
        argv.  Safe for concurrent calls."""

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.calls = 0

        def __call__(
            self,
            *args: str,
            cwd: str,
            timeout: float = DEFAULT_GIT_TIMEOUT_S,
        ) -> GitCommandResult:
            with self._lock:
                self.calls += 1
            sub = args[0]
            if sub == "rev-parse":
                # HEAD → A, target → B (whichever target is in argv).
                ref = args[-1]
                if ref.startswith("HEAD"):
                    return _git_ok(
                        ("git",) + args, stdout=FULL_SHA_A + "\n"
                    )
                return _git_ok(("git",) + args, stdout=FULL_SHA_B + "\n")
            if sub == "checkout":
                return _git_ok(("git",) + args)
            if sub == "diff":
                return _git_ok(("git",) + args, stdout="a.tsx\n")
            raise AssertionError(f"unexpected git call: {args}")

    runner = ThreadSafeGitRunner()
    rb = _make_rollbacker(manager=mgr, git_runner=runner)

    errors: list[Exception] = []

    def work(session_id: str) -> None:
        try:
            for _ in range(5):
                rb.rollback(
                    RollbackRequest(
                        session_id=session_id, commit_sha=FULL_SHA_B
                    )
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=work, args=(sid,))
        for sid in ("a", "b", "c", "d")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert rb.rollback_count() == 20
