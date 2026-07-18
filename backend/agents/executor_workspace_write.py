"""U6-0 T9/T10 GAP-5d: first real executor — capability-scoped in-place regular-file write (dormant, gated).

Reachable in prod only behind the U6 execute flag + allowlist + a real workspace-root-fd resolver + a driver loop, all
default-OFF.  The dispatcher fails closed to an inline no-op for every tool except runner_sdk:Write.  Reads only
StoredAction (immutable, server-stored); never model/raw args.  Containment is a CAPABILITY: writes go through directory
fds opened O_NOFOLLOW from an already-open root fd (fds track inodes, not paths); the target is created exclusively or,
if it exists, referenced with O_PATH and fstat-verified to be a same-filesystem, non-aliased REGULAR file before a byte
is written.  Linux-only (O_NOFOLLOW / O_PATH / st_dev / /proc/self/fd); if /proc is absent the existing-target reopen
fails ENOENT before any mutation => DefinitelyNotApplied (fail-closed).
"""
from __future__ import annotations

import errno as _errno
import hashlib
import os
import stat
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping

from backend.agents.execution_contract import Applied
from backend.agents.execution_contract import DefinitelyNotApplied
from backend.agents.execution_contract import ExecOutcome
from backend.agents.execution_contract import StoredAction
from backend.agents.execution_contract import Unknown

# workspace_id -> a FRESH OPEN O_DIRECTORY fd for the workspace root (a true capability), or None.  Injected; the
# resolver opens a new fd per call and the executor OWNS + closes it (via my_fds) exactly once.  Default fail-closed.
WorkspaceRootFdResolver = Callable[[str], "int | None"]

_WRITE_KEY = ("runner_sdk", "Write", "v1")      # the ONLY key routed to the real writer; all else -> inline noop
_NEW_FILE_MODE = 0o666                           # umask-masked by the kernel, matching open()/pathlib.write_text

# A target error in this set provably left the filesystem unmutated (local precondition) => DNA.  Everything NOT in it
# (ETIMEDOUT/ESTALE/EIO/connection loss/unknown transport) => Unknown.  EEXIST is handled via the existing-file path.
_DNA_OPEN_ERRNOS = frozenset({
    _errno.ELOOP, _errno.EISDIR, _errno.ENOTDIR, _errno.EACCES, _errno.EPERM,
    _errno.ENOENT, _errno.EROFS, _errno.ENAMETOOLONG, _errno.ENOSPC, _errno.EDQUOT, _errno.ENXIO,
})


def default_resolve_root_fd(workspace_id: str) -> "int | None":
    """Fail-closed default: no workspace root fd configured, so every write refuses."""
    return None


def _dna(error: str) -> DefinitelyNotApplied:
    return DefinitelyNotApplied(error=error, evidence="no filesystem mutation")


def _components(relative_path: str) -> "list[str] | None":
    """Safe workspace-relative components, or None.

    Rejects absolute paths, NUL, and any empty/'.'/'..' component (and the root itself, which yields no components).
    Closes the '.'/'a/..'->root escape and the NUL->Unknown residual BEFORE any fd is opened.
    """
    if not relative_path or relative_path.startswith("/") or "\0" in relative_path:
        return None
    parts = relative_path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts or None


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte; raise on a zero-length write so a stuck sink cannot spin forever."""
    view = memoryview(data)
    total = 0
    while total < len(view):
        written = os.write(fd, view[total:])
        if written <= 0:
            raise OSError(_errno.EIO, "zero-length write")
        total += written


def _errname(exc: OSError) -> str:
    if exc.errno is None:
        return "UNKNOWN"
    return _errno.errorcode.get(exc.errno, str(exc.errno))


def _open_target(basename: str, parent_fd: int, root_dev: int, my_fds: list) -> "int | ExecOutcome":
    """Return a writable fd for a same-fs, non-aliased REGULAR target, or a DNA/Unknown outcome.

    No mutation occurs on any refusal path: a fresh file is created exclusively (so it is provably ours, regular,
    nlink==1); an existing node is referenced with O_PATH (which invokes no device-open and cannot block) and refused
    before any write if it is not a same-filesystem, unaliased regular file.
    """
    try:
        try:
            fresh = os.open(
                basename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _NEW_FILE_MODE,
                dir_fd=parent_fd,
            )
            my_fds.append(fresh)
            return fresh
        except FileExistsError:
            # Existing node: O_PATH references the inode WITHOUT a device-open side effect and cannot block.  Under
            # O_NOFOLLOW a final SYMLINK opens as the link itself (not its target), so S_ISREG below refuses it
            # (not_regular_file, not ELOOP).  Verify regular/unaliased/same-fs, then reopen the SAME inode via procfs.
            probe = os.open(basename, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent_fd)
            my_fds.append(probe)
            st = os.fstat(probe)
            if not stat.S_ISREG(st.st_mode):
                return _dna("not_regular_file")
            if st.st_nlink != 1:
                return _dna("hardlinked_target")
            if st.st_dev != root_dev:
                return _dna("cross_device_target")
            reopened = os.open("/proc/self/fd/%d" % probe, os.O_WRONLY)
            my_fds.append(reopened)
            return reopened
    except OSError as exc:
        name = _errname(exc)
        if exc.errno in _DNA_OPEN_ERRNOS:
            return _dna("open_failed:%s" % name)
        return Unknown("open_ambiguous:%s" % name)


async def _write(stored: StoredAction, resolve_root_fd: WorkspaceRootFdResolver) -> ExecOutcome:
    args: Mapping[str, object] = stored.executable_args
    workspace_id = args.get("workspace_id")
    relative_path = args.get("relative_path")
    content = args.get("content")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _dna("invalid_args:workspace_id")
    if not isinstance(relative_path, str) or not relative_path:
        return _dna("invalid_args:relative_path")
    if not isinstance(content, str):
        return _dna("invalid_args:content")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        return _dna("invalid_args:content")     # a lone-surrogate / unencodable content is provably un-writable
    parts = _components(relative_path)
    if parts is None:
        return _dna("unsafe_relative_path")
    *dirs, basename = parts
    # Acquire the resolver fd LAST -- after all potentially-raising prep -- so nothing can raise between acquisition and
    # the my_fds ownership below (which closes it).  Otherwise a raising content.encode() would leak the root fd.
    root_fd = resolve_root_fd(workspace_id)
    if not isinstance(root_fd, int) or root_fd < 0:
        return _dna("no_workspace_root")

    # We OWN the resolver's root fd: the resolver opens a fresh fd per call and transfers it here, and nothing else
    # closes it.  Tracking it in my_fds is what prevents a per-execution directory-fd leak (EMFILE) once a real resolver
    # is wired.  It is closed exactly once in the finally (never removed from my_fds), like every walk fd.
    my_fds: list = [root_fd]
    try:
        try:
            root_dev = os.fstat(root_fd).st_dev
            parent_fd = root_fd
            for comp in dirs:
                nxt = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                my_fds.append(nxt)
                if os.fstat(nxt).st_dev != root_dev:
                    return _dna("cross_device_walk")
                parent_fd = nxt
        except OSError as exc:
            return _dna("walk_failed:%s" % _errname(exc))       # read-only opens => provably no mutation

        opened = _open_target(basename, parent_fd, root_dev, my_fds)
        if not isinstance(opened, int):
            return opened                                       # DNA / Unknown; nothing mutated on refusal
        write_fd = opened

        # Mutation begins: any failure now is Unknown.  Untrack BEFORE close so finally never re-closes a freed fd.
        try:
            os.ftruncate(write_fd, 0)
            _write_all(write_fd, data)
            os.fsync(write_fd)
            os.fsync(parent_fd)
            my_fds.remove(write_fd)
            os.close(write_fd)
        except OSError as exc:
            return Unknown("write_failed_after_mutation:%s" % _errname(exc))

        sha256 = hashlib.sha256(data).hexdigest()
        return Applied(
            result={"relative_path": relative_path, "bytes_written": len(data), "sha256": sha256},
            evidence="wrote %d bytes to the granted regular-file capability" % len(data),
        )
    finally:
        for fd in reversed(my_fds):
            try:
                os.close(fd)
            except OSError:
                pass


def make_workspace_write_executor(
    resolve_root_fd: WorkspaceRootFdResolver = default_resolve_root_fd,
) -> Callable[[StoredAction], Awaitable[ExecOutcome]]:
    """Build the real runner_sdk:Write executor closed over a workspace-root-fd resolver."""

    async def workspace_write_executor(stored: StoredAction) -> ExecOutcome:
        return await _write(stored, resolve_root_fd)

    return workspace_write_executor


async def _inline_noop(stored: StoredAction) -> DefinitelyNotApplied:
    # The dispatcher's fallback for any non-Write tool: an honest no-op with no side effect.  Inlined (this module
    # names no other execution module) so the GAP-5b production-caller dormancy scan is not tripped.
    return DefinitelyNotApplied(error="", evidence="noop: tool not implemented by GAP-5d")


def make_dispatch_executor(
    resolve_root_fd: WorkspaceRootFdResolver = default_resolve_root_fd,
) -> Callable[[StoredAction], Awaitable[ExecOutcome]]:
    """Route runner_sdk:Write to the real executor; fail closed to the inline no-op for everything else."""
    write_executor = make_workspace_write_executor(resolve_root_fd)

    async def dispatch(stored: StoredAction) -> ExecOutcome:
        if (stored.adapter_namespace, stored.tool_name, stored.schema_version) == _WRITE_KEY:
            return await write_executor(stored)
        return await _inline_noop(stored)

    return dispatch
