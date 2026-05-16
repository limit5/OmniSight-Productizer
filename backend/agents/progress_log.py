"""B9 — Anthropic harness per-ticket session progress log (OP-1122).

Append-only JSONL trace of one runner session, persisted at
``<worktree-path>/.runner/progress.txt``. The log is the durable
artifact that lets a crashed Anthropic SDK session resume cleanly
on the next pickup: the 3-step opener in :mod:`session_resume` reads
this file to rebuild the prior conversation outline, the per-iter
stop reasons, and the list of tool calls that already executed.

Schema (AC #2 / OP-1122) — exactly one JSON object per line, terminated
by ``\\n``:

    {
        "ts": "<ISO 8601 UTC>",
        "iter": <int>,
        "role": "<str>",
        "stop_reason": "<str>",
        "tool_calls": [<str>, ...],
        "content_summary": "<str (≤500 chars)>",
        "owner": "<bot identity, optional>"
    }

The optional ``owner`` field is set on the synthetic session-header
entry (``role == "session_header"``, ``iter == 0``) written by
:meth:`ProgressLog.start_session`. It is what :func:`read_owner` uses
to satisfy AC #8 — "owner mismatch (different bot identity) → escalate
``concurrent_runner_conflict``".

Durability (AC #3): every append calls ``fsync()`` on the file
descriptor synchronously. Throughput is not a concern — the launcher
writes ~100 lines/ticket — so durability wins. The parent directory
is also fsynced once at session start so a crash between mkdir + first
write cannot lose the directory entry.

Concurrency (AC #9): every write takes an exclusive ``fcntl.LOCK_EX``
on a dedicated lockfile sibling (``progress.txt.lock``). A second
runner trying to write the same ticket's progress.txt blocks until
the first runner releases. The lock is held for the duration of one
``append`` call only — long-running sessions do not starve operator
``cat progress.txt`` readers.

Corruption (AC #7): :func:`read_entries` skips lines that fail JSON
decode and surfaces them in the returned ``CorruptionReport``. The
caller (session_resume) treats any corruption report as "treat as
absent; start fresh; log for operator". The corrupt file is left
untouched so the operator can inspect it.

Error catalog (mapped to OP-1122 "Error catalog" §):
    progress_txt_corrupt          — :class:`ProgressTxtCorruptError`
    progress_txt_owner_mismatch   — :class:`ProgressTxtOwnerMismatchError`
    concurrent_runner_conflict    — re-raised as
                                    :class:`ConcurrentRunnerConflictError`
                                    by ``ProgressLog.start_session`` when
                                    the existing log's owner differs
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# AC #2 — max chars per ``content_summary`` field.
CONTENT_SUMMARY_MAX_CHARS: int = 500

# AC #1 — the directory under the worktree that holds the per-ticket log.
PROGRESS_DIR_NAME: str = ".runner"
PROGRESS_FILE_NAME: str = "progress.txt"
PROGRESS_LOCK_SUFFIX: str = ".lock"

# Synthetic role used by the session header (carries the owner identity
# so AC #8 owner-mismatch detection has something to compare).
SESSION_HEADER_ROLE: str = "session_header"


# ── Error catalog ──────────────────────────────────────────────────────


class ProgressTxtCorruptError(RuntimeError):
    """``progress.txt`` contained one or more unparseable lines."""

    def __init__(self, path: Path, bad_lines: list[int]) -> None:
        super().__init__(
            f"progress_txt_corrupt: {path} has {len(bad_lines)} unparseable "
            f"line(s) at offsets {bad_lines[:10]}"
        )
        self.path = path
        self.bad_lines = bad_lines


class ProgressTxtOwnerMismatchError(RuntimeError):
    """``progress.txt`` was written by a different bot identity."""

    def __init__(self, path: Path, expected: str, found: str) -> None:
        super().__init__(
            f"progress_txt_owner_mismatch: {path} owner={found!r} but "
            f"runner identity is {expected!r}"
        )
        self.path = path
        self.expected = expected
        self.found = found


class ConcurrentRunnerConflictError(RuntimeError):
    """Two runners claim the same ticket — surrender per AC #8."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"concurrent_runner_conflict: {path}: {detail}")
        self.path = path
        self.detail = detail


# ── Dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProgressEntry:
    """One JSONL row of progress.txt (AC #2 schema)."""

    ts: str
    iter: int
    role: str
    stop_reason: str
    tool_calls: list[str] = field(default_factory=list)
    content_summary: str = ""
    owner: str | None = None  # only set on session_header rows

    def to_jsonl(self) -> str:
        record: dict[str, Any] = {
            "ts": self.ts,
            "iter": self.iter,
            "role": self.role,
            "stop_reason": self.stop_reason,
            "tool_calls": list(self.tool_calls),
            "content_summary": self.content_summary[:CONTENT_SUMMARY_MAX_CHARS],
        }
        if self.owner is not None:
            record["owner"] = self.owner
        return json.dumps(record, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class CorruptionReport:
    """Summary returned by :func:`read_entries` when corrupt lines exist."""

    path: Path
    bad_line_offsets: list[int]  # 1-indexed line numbers
    bad_lines_preview: list[str]  # truncated content of the offending lines

    @property
    def is_clean(self) -> bool:
        return not self.bad_line_offsets


# ── Path helpers ───────────────────────────────────────────────────────


def progress_file_path(worktree_path: Path | str) -> Path:
    """Resolve ``<worktree-path>/.runner/progress.txt`` (AC #1)."""
    return Path(worktree_path) / PROGRESS_DIR_NAME / PROGRESS_FILE_NAME


def _lock_file_path(worktree_path: Path | str) -> Path:
    return progress_file_path(worktree_path).with_suffix(
        progress_file_path(worktree_path).suffix + PROGRESS_LOCK_SUFFIX
    )


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ── Read side (AC #7 corruption tolerance) ─────────────────────────────


def read_entries(
    worktree_path: Path | str,
) -> tuple[list[ProgressEntry], CorruptionReport]:
    """Read all entries; never raise on corruption (AC #7).

    Returns ``(entries, report)``. ``entries`` is the list of cleanly
    parsed rows in file order. ``report.is_clean`` is False iff the
    file contained any unparseable lines — the caller (session_resume)
    treats that as "fresh session; log for operator review".
    """
    path = progress_file_path(worktree_path)
    if not path.is_file():
        return [], CorruptionReport(path=path, bad_line_offsets=[], bad_lines_preview=[])

    entries: list[ProgressEntry] = []
    bad_offsets: list[int] = []
    bad_preview: list[str] = []

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("progress_log.read_failed path=%s err=%s", path, exc)
        return [], CorruptionReport(
            path=path, bad_line_offsets=[1], bad_lines_preview=[f"<read failed: {exc}>"]
        )

    for idx, line in enumerate(raw_text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad_offsets.append(idx)
            bad_preview.append(line[:120])
            continue
        if not isinstance(obj, dict):
            bad_offsets.append(idx)
            bad_preview.append(line[:120])
            continue
        try:
            entries.append(
                ProgressEntry(
                    ts=str(obj.get("ts", "")),
                    iter=int(obj.get("iter", 0)),
                    role=str(obj.get("role", "")),
                    stop_reason=str(obj.get("stop_reason", "")),
                    tool_calls=list(obj.get("tool_calls") or []),
                    content_summary=str(obj.get("content_summary", ""))[
                        :CONTENT_SUMMARY_MAX_CHARS
                    ],
                    owner=(str(obj["owner"]) if "owner" in obj else None),
                )
            )
        except (TypeError, ValueError):
            bad_offsets.append(idx)
            bad_preview.append(line[:120])

    report = CorruptionReport(
        path=path, bad_line_offsets=bad_offsets, bad_lines_preview=bad_preview
    )
    return entries, report


def read_owner(worktree_path: Path | str) -> str | None:
    """Return the owner identity from the session-header row, or ``None``.

    Used by :meth:`ProgressLog.start_session` to enforce AC #8 — if the
    existing log claims a different bot owner, the caller raises
    :class:`ConcurrentRunnerConflictError` and surrenders the ticket.

    Missing file → ``None`` (no claim). Corrupt file → ``None`` (the
    AC #7 path takes over).
    """
    entries, report = read_entries(worktree_path)
    if not report.is_clean:
        return None
    for e in entries:
        if e.role == SESSION_HEADER_ROLE and e.owner:
            return e.owner
    return None


# ── Write side ────────────────────────────────────────────────────────


class ProgressLog:
    """Per-ticket append-only progress log with fsync + flock semantics.

    Typical usage::

        log = ProgressLog(worktree_path, owner="claude-bot")
        log.start_session(reset=True)                   # writes header
        log.append(role="assistant", iter=1,
                   stop_reason="tool_use",
                   tool_calls=["bash", "Read"],
                   content_summary="...")
        ...
    """

    def __init__(self, worktree_path: Path | str, *, owner: str) -> None:
        if not owner:
            raise ValueError("ProgressLog requires a non-empty owner")
        self._worktree_path = Path(worktree_path)
        self._owner = owner
        self._path = progress_file_path(worktree_path)
        self._lock_path = _lock_file_path(worktree_path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def owner(self) -> str:
        return self._owner

    # ── Session lifecycle ─────────────────────────────────────────────

    def start_session(self, *, reset: bool = True) -> ProgressEntry:
        """Initialise the log for a new session.

        ``reset=True`` (default) — truncate any existing file. The B9
        recovery contract is "one writer per ticket": when the operator
        re-routes a ticket to this runner the previous log becomes
        history (rescued by the operator via ``git stash`` snapshots
        that :mod:`runner_progress` writes separately at phase
        boundaries). Per the OP-1122 description: "progress.txt is
        single-writer per ticket. Reset on ticket pickup."

        ``reset=False`` — append to the existing file. Used by
        :mod:`session_resume` on the resume path, after the 3-step
        opener has verified the owner matches the current runner.

        Raises :class:`ConcurrentRunnerConflictError` (AC #8) if
        ``reset=False`` and the existing file's owner differs from
        ``self.owner``.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync_parent_dir()

        if reset:
            # Truncate any existing log atomically; the next append
            # will recreate the file.
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
        else:
            existing_owner = read_owner(self._worktree_path)
            if existing_owner is not None and existing_owner != self._owner:
                raise ConcurrentRunnerConflictError(
                    self._path,
                    f"existing owner={existing_owner!r}, this runner={self._owner!r}",
                )

        header = ProgressEntry(
            ts=_utc_now_iso(),
            iter=0,
            role=SESSION_HEADER_ROLE,
            stop_reason="",
            tool_calls=[],
            content_summary=f"session start ({'fresh' if reset else 'resume'})",
            owner=self._owner,
        )
        self._raw_append(header)
        return header

    # ── Append (AC #3 fsync + AC #9 flock) ────────────────────────────

    def append(
        self,
        *,
        role: str,
        iter: int,
        stop_reason: str = "",
        tool_calls: list[str] | None = None,
        content_summary: str = "",
    ) -> ProgressEntry:
        """Append one structured entry. fsynced before return (AC #3)."""
        entry = ProgressEntry(
            ts=_utc_now_iso(),
            iter=int(iter),
            role=str(role),
            stop_reason=str(stop_reason),
            tool_calls=list(tool_calls or []),
            content_summary=str(content_summary),
        )
        self._raw_append(entry)
        return entry

    def _raw_append(self, entry: ProgressEntry) -> None:
        """Take ``flock``, append + fsync, release lock."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Open or create the lock sibling — keep it persistent so
        # competing writers can ``flock`` on the same file across
        # process boundaries (Linux fcntl locks are per-file, not
        # per-process).
        lock_fd = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT,
            0o644,
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            payload = (entry.to_jsonl() + "\n").encode("utf-8")
            data_fd = os.open(
                self._path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o644,
            )
            try:
                os.write(data_fd, payload)
                os.fsync(data_fd)
            finally:
                os.close(data_fd)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _fsync_parent_dir(self) -> None:
        """Persist the ``.runner`` directory entry to disk."""
        parent = self._path.parent
        try:
            dir_fd = os.open(parent, os.O_RDONLY)
        except OSError as exc:
            log.warning("progress_log.dir_fsync_open path=%s err=%s", parent, exc)
            return
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            log.warning("progress_log.dir_fsync path=%s err=%s", parent, exc)
        finally:
            os.close(dir_fd)


__all__ = [
    "CONTENT_SUMMARY_MAX_CHARS",
    "ConcurrentRunnerConflictError",
    "CorruptionReport",
    "PROGRESS_DIR_NAME",
    "PROGRESS_FILE_NAME",
    "ProgressEntry",
    "ProgressLog",
    "ProgressTxtCorruptError",
    "ProgressTxtOwnerMismatchError",
    "SESSION_HEADER_ROLE",
    "progress_file_path",
    "read_entries",
    "read_owner",
]
