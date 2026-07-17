"""OP-2675 capability-scoped workspace-write executor tests (offline, Linux-only)."""

from __future__ import annotations

import errno
import hashlib
import os
import pathlib
import stat
import uuid
from collections.abc import Mapping

import pytest

from backend.agents import executor_workspace_write as workspace_write
from backend.agents.action_canonicalize import CanonicalizationContext
from backend.agents.canonicalize_code_write_file import _canon_sdk_write
from backend.agents.execution_contract import Applied
from backend.agents.execution_contract import DefinitelyNotApplied
from backend.agents.execution_contract import ExecOutcome
from backend.agents.execution_contract import StoredAction
from backend.agents.execution_contract import TerminalPlan
from backend.agents.execution_contract import Unknown
from backend.agents.execution_contract import resolve_terminal


def _stored(
    *,
    relative_path: str = "result.txt",
    content: str = "written\n",
    adapter_namespace: str = "runner_sdk",
    tool_name: str = "Write",
    schema_version: str = "v1",
    executable_args: Mapping[str, object] | None = None,
) -> StoredAction:
    args = executable_args or {
        "workspace_id": "workspace-1",
        "relative_path": relative_path,
        "resolved_at_prepare": "/untrusted/path/is/not/used",
        "content": content,
    }
    return StoredAction(
        grant_id="grant-1",
        idempotency_key="grant-1:action-1",
        recovery_mode="non_replayable",
        adapter_namespace=adapter_namespace,
        tool_name=tool_name,
        schema_version=schema_version,
        canonical_target=relative_path,
        executable_args=args,
    )


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


async def _execute(
    root: pathlib.Path,
    stored: StoredAction,
    *,
    dispatch: bool = False,
) -> ExecOutcome:
    """Execute with a fresh resolver root fd the EXECUTOR now owns and closes; prove no fd (incl the root) leaks."""
    before = _fd_count()

    def resolve_root_fd(workspace_id: str) -> int:
        assert workspace_id == "workspace-1"
        return os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)

    factory = (
        workspace_write.make_dispatch_executor
        if dispatch
        else workspace_write.make_workspace_write_executor
    )
    outcome = await factory(resolve_root_fd)(stored)
    # The executor now owns the resolver's root fd: after it returns, every fd it opened AND the root fd are closed.
    assert _fd_count() == before
    return outcome


def _assert_applied(
    outcome: ExecOutcome,
    *,
    relative_path: str,
    content: str,
) -> Applied:
    assert isinstance(outcome, Applied)
    data = content.encode("utf-8")
    assert outcome.result == {
        "relative_path": relative_path,
        "bytes_written": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert outcome.evidence == (
        "wrote %d bytes to the granted regular-file capability" % len(data)
    )
    return outcome


def _assert_dna(outcome: ExecOutcome, error: str) -> DefinitelyNotApplied:
    assert outcome == DefinitelyNotApplied(
        error=error,
        evidence="no filesystem mutation",
    )
    return outcome


@pytest.mark.asyncio
async def test_real_write_returns_applied_with_exact_receipt(
    tmp_path: pathlib.Path,
) -> None:
    content = "first real executor\n"

    outcome = await _execute(tmp_path, _stored(content=content))

    _assert_applied(outcome, relative_path="result.txt", content=content)
    assert (tmp_path / "result.txt").read_bytes() == content.encode()


@pytest.mark.asyncio
async def test_empty_content_returns_applied_and_truncates_to_zero_bytes(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "empty.txt"
    target.write_text("old content", encoding="utf-8")

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="empty.txt", content=""),
    )

    _assert_applied(outcome, relative_path="empty.txt", content="")
    assert target.read_bytes() == b""


@pytest.mark.asyncio
async def test_overwrite_preserves_existing_inode_and_mode(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)
    before = target.stat()

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="existing.txt", content="after"),
    )

    _assert_applied(outcome, relative_path="existing.txt", content="after")
    after = target.stat()
    assert after.st_ino == before.st_ino
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o640
    assert target.read_text(encoding="utf-8") == "after"


@pytest.mark.asyncio
async def test_new_file_mode_matches_path_write_text_control_in_same_directory(
    tmp_path: pathlib.Path,
) -> None:
    control = tmp_path / "control.txt"
    control.write_text("control", encoding="utf-8")

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="created.txt", content="created"),
    )

    _assert_applied(outcome, relative_path="created.txt", content="created")
    actual_mode = stat.S_IMODE((tmp_path / "created.txt").stat().st_mode)
    control_mode = stat.S_IMODE(control.stat().st_mode)
    assert actual_mode == control_mode


@pytest.mark.asyncio
async def test_idempotent_rerun_rewrites_same_inode_with_same_receipt(
    tmp_path: pathlib.Path,
) -> None:
    stored = _stored(relative_path="rerun.txt", content="stable")

    first = await _execute(tmp_path, stored)
    inode = (tmp_path / "rerun.txt").stat().st_ino
    second = await _execute(tmp_path, stored)

    _assert_applied(first, relative_path="rerun.txt", content="stable")
    _assert_applied(second, relative_path="rerun.txt", content="stable")
    assert first == second
    assert (tmp_path / "rerun.txt").stat().st_ino == inode
    assert (tmp_path / "rerun.txt").read_text(encoding="utf-8") == "stable"


@pytest.mark.asyncio
async def test_utf8_multibyte_content_reports_encoded_byte_count_and_hash(
    tmp_path: pathlib.Path,
) -> None:
    content = "台灣🙂 café\n"

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="utf8.txt", content=content),
    )

    _assert_applied(outcome, relative_path="utf8.txt", content=content)
    assert (tmp_path / "utf8.txt").read_bytes() == content.encode("utf-8")


@pytest.mark.asyncio
async def test_real_canonicalizer_shape_is_accepted_without_redefaulting(
    tmp_path: pathlib.Path,
) -> None:
    context = CanonicalizationContext(
        workspace_id="workspace-1",
        workspace_root=str(tmp_path),
        adapter_namespace="runner_sdk",
        tool_name="Write",
        schema_version="v1",
    )
    prepared = _canon_sdk_write(
        context,
        {"file_path": "canonical.txt", "content": "canonicalized"},
    )
    stored = _stored(
        relative_path=prepared.canonical_target,
        executable_args=prepared.executable_args,
    )

    outcome = await _execute(tmp_path, stored)

    _assert_applied(
        outcome,
        relative_path="canonical.txt",
        content="canonicalized",
    )
    assert (tmp_path / "canonical.txt").read_text(encoding="utf-8") == (
        "canonicalized"
    )


@pytest.mark.parametrize(
    ("relative_path", "expected_error"),
    [
        (".", "unsafe_relative_path"),
        ("..", "unsafe_relative_path"),
        ("a/..", "unsafe_relative_path"),
        ("a/../../b", "unsafe_relative_path"),
        ("/abs", "unsafe_relative_path"),
        ("", "invalid_args:relative_path"),
        ("a/", "unsafe_relative_path"),
        ("a\0b", "unsafe_relative_path"),
    ],
    ids=["dot", "dot-dot", "nested-dot-dot", "escape", "absolute", "empty", "trailing-slash", "nul"],
)
@pytest.mark.asyncio
async def test_unsafe_path_is_dna_and_creates_nothing_inside_or_beside_root(
    tmp_path: pathlib.Path,
    relative_path: str,
    expected_error: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    beside_before = set(tmp_path.iterdir())

    outcome = await _execute(
        root,
        _stored(relative_path=relative_path, content="blocked"),
    )

    _assert_dna(outcome, expected_error)
    assert list(root.iterdir()) == []
    assert set(tmp_path.iterdir()) == beside_before


@pytest.mark.asyncio
async def test_final_component_symlink_is_dna_and_outside_target_is_unwritten(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    outcome = await _execute(
        root,
        _stored(relative_path="link.txt", content="blocked"),
    )

    _assert_dna(outcome, "not_regular_file")
    assert outside.read_text(encoding="utf-8") == "outside"
    assert (root / "link.txt").is_symlink()


@pytest.mark.asyncio
async def test_intermediate_component_symlink_is_dna_and_outside_is_unwritten(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    outcome = await _execute(
        root,
        _stored(relative_path="link/blocked.txt", content="blocked"),
    )

    # O_DIRECTORY|O_NOFOLLOW on a symlink component fails ENOTDIR on Linux (the O_DIRECTORY check precedes the ELOOP
    # check). Asserting the exact errno keeps the test able to catch an accidental drop of O_DIRECTORY from the walk.
    _assert_dna(outcome, "walk_failed:ENOTDIR")
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_ancestor_symlink_swap_race_writes_held_directory_inode_only(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "d").mkdir()
    held_directory = root / "held-directory"
    real_open = workspace_write.os.open
    swapped = False

    def racing_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "payload.txt" and dir_fd is not None and not swapped:
            swapped = True
            (root / "d").rename(held_directory)
            (root / "d").symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workspace_write.os, "open", racing_open)

    outcome = await _execute(
        root,
        _stored(relative_path="d/payload.txt", content="anchored"),
    )

    _assert_applied(
        outcome,
        relative_path="d/payload.txt",
        content="anchored",
    )
    assert swapped
    assert (held_directory / "payload.txt").read_text(encoding="utf-8") == (
        "anchored"
    )
    assert not (outside / "payload.txt").exists()


@pytest.mark.asyncio
async def test_final_hardlink_swap_race_writes_held_inode_not_outside_alias(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("original target", encoding="utf-8")
    original_inode = target.stat().st_ino
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    held_target = root / "held-target.txt"
    real_ftruncate = workspace_write.os.ftruncate
    swapped = False

    def racing_ftruncate(fd: int, length: int) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            target.rename(held_target)
            os.link(outside, target)
        real_ftruncate(fd, length)

    monkeypatch.setattr(workspace_write.os, "ftruncate", racing_ftruncate)

    outcome = await _execute(
        root,
        _stored(relative_path="target.txt", content="held inode"),
    )

    _assert_applied(outcome, relative_path="target.txt", content="held inode")
    assert swapped
    assert held_target.stat().st_ino == original_inode
    assert held_target.read_text(encoding="utf-8") == "held inode"
    assert outside.read_text(encoding="utf-8") == "outside"
    assert target.read_text(encoding="utf-8") == "outside"
    assert target.stat().st_ino == outside.stat().st_ino


@pytest.mark.asyncio
async def test_hardlinked_existing_target_is_dna_and_outside_is_unchanged(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.link(outside, root / "alias.txt")

    outcome = await _execute(
        root,
        _stored(relative_path="alias.txt", content="blocked"),
    )

    _assert_dna(outcome, "hardlinked_target")
    assert outside.read_text(encoding="utf-8") == "outside"
    assert (root / "alias.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.asyncio
async def test_fifo_target_is_dna_without_opening_for_io_or_delivering_bytes(
    tmp_path: pathlib.Path,
) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="pipe", content="blocked"),
    )

    _assert_dna(outcome, "not_regular_file")
    assert stat.S_ISFIFO(fifo.stat().st_mode)
    # Prove no bytes were delivered into the pipe: the executor never opened the FIFO for writing, so no writer exists
    # and a non-blocking read-side sees EOF (b""). Had the executor written through the FIFO, a reader would see bytes.
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert os.read(reader, 4096) == b""
    finally:
        os.close(reader)


@pytest.mark.asyncio
async def test_directory_target_is_dna_and_remains_a_directory(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "directory"
    target.mkdir()

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="directory", content="blocked"),
    )

    # The O_EXCL create fails EEXIST, the O_PATH probe opens the directory inode, and S_ISREG rejects it => the exact
    # outcome is not_regular_file (never open_failed:EISDIR, which would imply a different, non-O_PATH open path).
    _assert_dna(outcome, "not_regular_file")
    assert target.is_dir()
    assert list(target.iterdir()) == []


@pytest.mark.asyncio
async def test_cross_device_walk_is_dna_when_second_mount_is_available() -> None:
    root = pathlib.Path("/")
    second_mount = pathlib.Path("/dev/shm")
    if not second_mount.is_dir() or second_mount.stat().st_dev == root.stat().st_dev:
        pytest.skip("no distinct /dev/shm mount available")
    target = second_mount / ("op-2675-" + uuid.uuid4().hex)
    assert not target.exists()

    outcome = await _execute(
        root,
        _stored(
            relative_path=target.relative_to(root).as_posix(),
            content="blocked",
        ),
    )

    assert isinstance(outcome, DefinitelyNotApplied)
    assert outcome.error.startswith("cross_device")
    assert outcome.evidence == "no filesystem mutation"
    assert not target.exists()


@pytest.mark.parametrize("resolver_result", [None, -1], ids=["none", "negative"])
@pytest.mark.asyncio
async def test_no_root_fd_is_dna_and_default_resolver_is_fail_closed(
    tmp_path: pathlib.Path,
    resolver_result: int | None,
) -> None:
    assert workspace_write.default_resolve_root_fd("workspace-1") is None
    before = _fd_count()
    executor = workspace_write.make_workspace_write_executor(
        lambda _workspace_id: resolver_result
    )

    outcome = await executor(_stored())

    _assert_dna(outcome, "no_workspace_root")
    assert list(tmp_path.iterdir()) == []
    assert _fd_count() == before


@pytest.mark.asyncio
async def test_missing_parent_directory_is_dna_and_creates_nothing(
    tmp_path: pathlib.Path,
) -> None:
    outcome = await _execute(
        tmp_path,
        _stored(relative_path="missing/result.txt", content="blocked"),
    )

    _assert_dna(outcome, "walk_failed:ENOENT")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("executable_args", "expected_error"),
    [
        ({"relative_path": "x", "content": "x"}, "invalid_args:workspace_id"),
        ({"workspace_id": "", "relative_path": "x", "content": "x"}, "invalid_args:workspace_id"),
        ({"workspace_id": 1, "relative_path": "x", "content": "x"}, "invalid_args:workspace_id"),
        ({"workspace_id": "workspace-1", "content": "x"}, "invalid_args:relative_path"),
        ({"workspace_id": "workspace-1", "relative_path": "", "content": "x"}, "invalid_args:relative_path"),
        ({"workspace_id": "workspace-1", "relative_path": 1, "content": "x"}, "invalid_args:relative_path"),
        ({"workspace_id": "workspace-1", "relative_path": "x"}, "invalid_args:content"),
        ({"workspace_id": "workspace-1", "relative_path": "x", "content": 1}, "invalid_args:content"),
    ],
    ids=[
        "workspace-missing",
        "workspace-empty",
        "workspace-nonstr",
        "path-missing",
        "path-empty",
        "path-nonstr",
        "content-missing",
        "content-nonstr",
    ],
)
@pytest.mark.asyncio
async def test_invalid_args_are_dna_before_resolver_or_mutation(
    tmp_path: pathlib.Path,
    executable_args: Mapping[str, object],
    expected_error: str,
) -> None:
    resolver_called = False

    def resolve_root_fd(_workspace_id: str) -> int:
        nonlocal resolver_called
        resolver_called = True
        raise AssertionError("invalid args must not reach the resolver")

    executor = workspace_write.make_workspace_write_executor(resolve_root_fd)

    outcome = await executor(_stored(executable_args=executable_args))

    _assert_dna(outcome, expected_error)
    assert not resolver_called
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_post_mutation_write_failure_is_unknown_not_dna(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(_fd: int, _data: bytes) -> int:
        raise OSError(errno.EIO, "injected write failure")

    monkeypatch.setattr(workspace_write.os, "write", fail_write)

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="write-failure.txt", content="content"),
    )

    assert outcome == Unknown(error="write_failed_after_mutation:EIO")
    assert (tmp_path / "write-failure.txt").exists()


@pytest.mark.asyncio
async def test_post_mutation_fsync_failure_is_unknown_not_dna(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fsync(_fd: int) -> None:
        raise OSError(errno.EIO, "injected fsync failure")

    monkeypatch.setattr(workspace_write.os, "fsync", fail_fsync)

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="fsync-failure.txt", content="content"),
    )

    assert outcome == Unknown(error="write_failed_after_mutation:EIO")
    assert (tmp_path / "fsync-failure.txt").read_text(encoding="utf-8") == (
        "content"
    )


@pytest.mark.asyncio
async def test_post_mutation_close_failure_is_unknown_without_double_close(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = workspace_write.os.close
    failed_once = False

    def close_then_fail(fd: int) -> None:
        nonlocal failed_once
        real_close(fd)
        if not failed_once:
            failed_once = True
            raise OSError(errno.EIO, "injected close failure")

    monkeypatch.setattr(workspace_write.os, "close", close_then_fail)

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="close-failure.txt", content="content"),
    )

    assert outcome == Unknown(error="write_failed_after_mutation:EIO")
    assert failed_once
    assert (tmp_path / "close-failure.txt").read_text(encoding="utf-8") == (
        "content"
    )


@pytest.mark.asyncio
async def test_write_all_completes_across_short_os_write_returns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = workspace_write.os.write

    def one_byte_write(fd: int, data: "bytes | memoryview") -> int:
        # Force _write_all's loop by writing at most one byte per call; the full content must still land.
        return real_write(fd, bytes(data[:1]))

    monkeypatch.setattr(workspace_write.os, "write", one_byte_write)

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="short.txt", content="many-bytes-here"),
    )

    _assert_applied(outcome, relative_path="short.txt", content="many-bytes-here")
    assert (tmp_path / "short.txt").read_text(encoding="utf-8") == "many-bytes-here"


@pytest.mark.asyncio
async def test_zero_length_os_write_is_unknown_after_mutation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_write.os, "write", lambda fd, data: 0)

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="stalled.txt", content="payload"),
    )

    # _write_all raises OSError(EIO) on a zero-length write so a stuck sink cannot spin forever; the target was already
    # created/truncated, so the honest outcome is Unknown (not DNA, not an infinite loop).
    assert outcome == Unknown(error="write_failed_after_mutation:EIO")


@pytest.mark.parametrize(
    ("injected_errno", "error_name"),
    [(errno.ETIMEDOUT, "ETIMEDOUT"), (errno.ESTALE, "ESTALE")],
)
@pytest.mark.asyncio
async def test_ambiguous_target_open_errno_is_unknown(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    injected_errno: int,
    error_name: str,
) -> None:
    real_open = workspace_write.os.open

    def failing_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "ambiguous.txt" and dir_fd is not None:
            raise OSError(injected_errno, "injected ambiguous open")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workspace_write.os, "open", failing_open)

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="ambiguous.txt", content="blocked"),
    )

    assert outcome == Unknown(error="open_ambiguous:%s" % error_name)
    assert not (tmp_path / "ambiguous.txt").exists()


@pytest.mark.asyncio
async def test_allowlisted_target_open_errno_is_dna(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = workspace_write.os.open

    def failing_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "denied.txt" and dir_fd is not None:
            raise OSError(errno.EACCES, "injected access denial")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workspace_write.os, "open", failing_open)

    outcome = await _execute(
        tmp_path,
        _stored(relative_path="denied.txt", content="blocked"),
    )

    _assert_dna(outcome, "open_failed:EACCES")
    assert not (tmp_path / "denied.txt").exists()


@pytest.mark.asyncio
async def test_no_fd_leak_across_applied_dna_and_unknown_outcomes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _execute(tmp_path, _stored(relative_path="success.txt"))
    await _execute(tmp_path, _stored(relative_path="../dna.txt"))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    await _execute(tmp_path, _stored(relative_path="fifo"))
    real_open = workspace_write.os.open

    def ambiguous_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "unknown.txt" and dir_fd is not None:
            raise OSError(errno.ETIMEDOUT, "injected timeout")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workspace_write.os, "open", ambiguous_open)
    await _execute(tmp_path, _stored(relative_path="unknown.txt"))


@pytest.mark.asyncio
async def test_repeated_executions_do_not_leak_the_resolver_root_fd(
    tmp_path: pathlib.Path,
) -> None:
    # Regression: the executor OWNS the resolver's root fd (opened fresh per call) and must close it every time, or a
    # real resolver would leak one directory fd per execution (EMFILE). Drive many executions and assert fd stability.
    def resolve_root_fd(workspace_id: str) -> int:
        return os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)

    executor = workspace_write.make_workspace_write_executor(resolve_root_fd)
    before = len(os.listdir("/proc/self/fd"))
    for i in range(64):
        outcome = await executor(_stored(relative_path="leak-%d.txt" % i, content="x"))
        assert isinstance(outcome, Applied)
    assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.asyncio
async def test_dispatch_routes_only_runner_sdk_write_v1_and_noops_everything_else(
    tmp_path: pathlib.Path,
) -> None:
    write_outcome = await _execute(
        tmp_path,
        _stored(relative_path="routed.txt", content="routed"),
        dispatch=True,
    )
    _assert_applied(
        write_outcome,
        relative_path="routed.txt",
        content="routed",
    )
    assert (tmp_path / "routed.txt").read_text(encoding="utf-8") == "routed"

    noop_keys = [
        ("runner_sdk", "Write", "v2"),
        ("specialist", "write_file", "v1"),
        ("specialist", "write_yaml", "v1"),
        ("runner_sdk", "Edit", "v1"),
        ("runner_sdk", "str_replace_based_edit_tool", "v1"),
        ("x", "y", "v1"),
    ]
    for index, (adapter, tool, version) in enumerate(noop_keys):
        relative_path = "noop-%d.txt" % index
        outcome = await _execute(
            tmp_path,
            _stored(
                relative_path=relative_path,
                adapter_namespace=adapter,
                tool_name=tool,
                schema_version=version,
            ),
            dispatch=True,
        )
        assert isinstance(outcome, DefinitelyNotApplied)
        assert outcome.error == ""
        assert "noop" in outcome.evidence
        assert not (tmp_path / relative_path).exists()


def test_outcomes_resolve_to_frozen_terminal_plans() -> None:
    assert resolve_terminal(
        Applied(result={"receipt": "ok"}, evidence="applied"),
        "non_replayable",
    ) == TerminalPlan("consumed", "done", True, False)
    assert resolve_terminal(
        DefinitelyNotApplied(error="blocked", evidence="unmutated"),
        "non_replayable",
    ) == TerminalPlan("failed", "failed", False, True)
    assert resolve_terminal(
        Unknown(error="ambiguous"),
        "non_replayable",
    ) == TerminalPlan("manual", "manual", False, True)


def test_executor_module_is_dormant_and_has_no_gate_dependency() -> None:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    module_path = pathlib.Path(workspace_write.__file__).resolve()
    source = module_path.read_text(encoding="utf-8")
    forbidden_gate_token = "execution" + "_gate"
    assert forbidden_gate_token not in source

    defining_names = (
        "make_" + "dispatch_executor",
        "make_" + "workspace_write_executor",
        "executor_" + "workspace_write",
    )
    references: list[str] = []
    for path in (repo_root / "backend").rglob("*.py"):
        resolved = path.resolve()
        relative = resolved.relative_to(repo_root)
        if resolved == module_path:
            continue
        if "tests" in relative.parts or ".venv" in relative.parts:
            continue
        if relative.parts[:3] == ("backend", "alembic", "versions"):
            continue
        production_source = resolved.read_text(encoding="utf-8")
        if any(name in production_source for name in defining_names):
            references.append(relative.as_posix())

    assert references == []
