"""C1 phase-snapshot + progress durability (SP-B-X-002a / OP-1060).

Per ``docs/architecture/sdk-runner-sprint-b-error-handling.md`` §C1
line 543-559 (crash mid-execution) + §7 line 667-675 (recovery
primitives → Worktree snapshot) + line 677-687 (progress.txt schema).

Two recovery primitives:

1. **Phase-boundary snapshots** — at every aggregate-FSM state
   transition (idle → picking_up → restoring_session → working →
   submitting → completed/aborted/failed), if the worktree is dirty,
   ``git stash push -m "phase-snapshot @ <phase> @ <iso> @ <ticket>"
   --include-untracked`` retains the in-progress edits under a labeled,
   locatable ref. The runner does NOT auto-restore (operator-witnessed
   CLI only, per AC §C1 "Do NOT auto-restore snapshots"). ``stash push``
   (not ``stash create``) is required because the ``create`` form
   returns a hash that has no reachable ref and is GC-eligible
   immediately — the ``push`` form attaches the entry to ``refs/stash``
   so it can be located via label on resume.

2. **Durable progress.txt** — the snapshot ref + the last completed
   phase are persisted under the worktree at ``progress.txt`` via
   write-temp + fsync + atomic rename, so a runner host crash between
   write and rename leaves either the prior progress or the new one,
   never a torn record. The parent directory is also fsynced so the
   rename itself reaches stable storage.

On the next pickup, :func:`find_recovered_snapshot` re-reads
``progress.txt``, cross-checks the recorded ref against
``git stash list``, and (if matched) returns the row so the caller
posts the single ``[progress-recovered]`` operator-facing comment from
:func:`format_recovered_comment`.

Architecturally this module is **pure recovery state**: it owns no
JIRA transitions, no Gerrit pushes, no notifier wiring. Callers (the
runner main loop in ``auto-runner-jira.py``) decide *when* to record
and *whether* to surface; this module decides *what* the artifact
looks like and *how* it survives a kill -9.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

PROGRESS_FILENAME = "progress.txt"
_PROGRESS_TMP_SUFFIX = ".tmp"

# Stash label per AC §C1: 4 ` @ `-separated fields, label[0] = phase-snapshot.
_STASH_LABEL_PREFIX = "phase-snapshot"
_STASH_LABEL_SEP = " @ "
# Output format for `git stash list` cross-check. Matches AC §C1's
# verification step: %gd = stash@{N}, %s = subject (contains our label).
_STASH_LIST_FORMAT = "%gd:%s"


# ── Progress schema (B9 + AC §C1) ──────────────────────────────────────


@dataclass(frozen=True)
class Progress:
    """One row of ``progress.txt``. AC §C1 mandates the two starred
    fields; the surrounding metadata is bookkeeping the operator needs
    to interpret the row.

    Fields are primitives so the JSON round-trip is lossless and a torn
    write (parent dir not fsynced) fails the JSON parse rather than
    silently misreading a corrupted struct.
    """

    ticket_key: str
    phase_completed: str  # ★ AC §C1
    phase_snapshot_stash_ref: str  # ★ AC §C1 — hash, or "" if clean
    phase_snapshot_stash_label: str = ""  # full label, "" iff ref is ""
    phase_snapshot_stash_ref_name: str = ""  # e.g. "stash@{0}" at write time
    iso_utc: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Progress":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"progress.txt root is {type(data).__name__}, want dict")
        return cls(
            ticket_key=str(data.get("ticket_key", "")),
            phase_completed=str(data.get("phase_completed", "")),
            phase_snapshot_stash_ref=str(data.get("phase_snapshot_stash_ref", "")),
            phase_snapshot_stash_label=str(data.get("phase_snapshot_stash_label", "")),
            phase_snapshot_stash_ref_name=str(data.get("phase_snapshot_stash_ref_name", "")),
            iso_utc=str(data.get("iso_utc", "")),
        )


def progress_path(worktree_path: Path) -> Path:
    return worktree_path / PROGRESS_FILENAME


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_stash_label(phase: str, ticket_key: str, *, iso: str | None = None) -> str:
    """Canonical stash label per AC §C1.

    Format: ``phase-snapshot @ {phase} @ {iso} @ {ticket}``. The ``" @ "``
    separator is load-bearing — :func:`_label_in_stash_list` matches the
    suffix exactly, and ``git stash`` may prefix the subject with
    ``On <branch>:``, so we anchor on suffix not prefix.
    """
    when = iso if iso is not None else _utc_iso_now()
    return _STASH_LABEL_SEP.join([_STASH_LABEL_PREFIX, phase, when, ticket_key])


# ── Git plumbing ───────────────────────────────────────────────────────


def _git(worktree_path: Path, args: list[str], *, timeout: int = 30) -> str:
    """Thin git wrapper. Raises on non-zero exit; returns stdout. We use
    ``check=False`` + explicit raise so the error message carries the
    full git stderr — the runner logs use this directly.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {worktree_path}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


#: Files written by the runner pipeline itself for bookkeeping / safety,
#: NOT by the CLI's ticket work. These must be filtered out of every
#: dirty-check that decides whether the worktree is "CLI-modified".
#:
#: Public + canonical: ``jira_dispatch.ensure_change_ids`` imports this
#: same constant rather than duplicating the set (SP-B-X-018 / OP-1076).
#: If a new runner-runtime file is added in the future, add it HERE
#: only — both dirty-check sites will pick it up automatically.
#:
#: Entries:
#:   * ``progress.txt`` (+ ``.tmp``) — phase-progress writer (SP-B-X-002a
#:     / OP-1060). ``runner_progress.record_phase`` writes this at every
#:     FSM boundary; a naive dirty-check would report dirty on every
#:     phase transition.
#:   * ``.runner-cwd-sentinel`` — workspace-tamper sentinel (OP-842 /
#:     OP-836). ``runner_workspace_safety.write_workspace_sentinel``
#:     drops this pre-CLI as an intentionally-untracked tamper-detection
#:     marker. Two distinct dirty-check failure modes were created when
#:     it leaked into either of the two checks (workspace-tampered if
#:     phase-snapshot stash-sweeps it, dirty-worktree if ensure_change_ids
#:     surfaces it) — both fixed by including it here.
#:
#: Re-exported from :mod:`backend.agents.runner_artifacts` (OP-1111).
#: The canonical home moved to a dedicated module so dirty-check sites
#: have a single import point without coupling to the progress.txt
#: writer. Legacy callers that imported
#: ``runner_progress.RUNNER_RUNTIME_ARTIFACTS`` keep working unchanged.
#:
#: OP-1111 also reversed the original SP-B-X-018 "filter-only"
#: decision: ``.gitignore`` now mirrors this set (defense in depth —
#: Python-layer filter + git-layer ignore). See
#: ``backend/agents/runner_artifacts.py`` module docstring.
from backend.agents.runner_artifacts import (  # noqa: E402
    RUNNER_RUNTIME_ARTIFACTS,
)

#: Back-compat alias — pre-SP-B-X-018 callers used this private name.
#: New code MUST import :data:`RUNNER_RUNTIME_ARTIFACTS` from
#: :mod:`backend.agents.runner_artifacts` instead.
_OUR_OWN_ARTIFACTS = RUNNER_RUNTIME_ARTIFACTS


def _worktree_dirty(worktree_path: Path) -> bool:
    """True iff ``git status --porcelain -uall`` returns non-empty
    (any tracked or untracked change), **excluding our own progress.txt
    bookkeeping artifacts**.

    The ``-uall`` flag makes the dirty check see exactly what
    ``--include-untracked`` would stash, so the two never disagree
    (avoids the "clean per status, dirty per stash" trap where
    untracked-dirs hide files from default porcelain).

    Excluding ``progress.txt`` itself is deliberate: this module writes
    progress.txt at every phase boundary as an untracked file, so a
    naive dirty check would report "dirty" purely because of the
    bookkeeping row we just wrote — leading to a spurious stash on
    every subsequent phase boundary (and stash-sweeps that delete the
    very pointer file we are writing). The OP-842 ``.runner-cwd-sentinel``
    filter in :func:`jira_dispatch.ensure_change_ids` is the same shape
    of fix for the same shape of problem.
    """
    out = _git(worktree_path, ["status", "--porcelain", "-uall"])
    for raw in out.splitlines():
        if not raw.strip():
            continue
        # `git status --porcelain` rows are "XY <filename>"; XY is 2
        # status chars + a space (XY-formatted). filename starts at col 3.
        filename = raw[3:].strip()
        # Rename rows look like "old -> new"; the "new" side is what
        # would land in the stash, so split on " -> " conservatively.
        if " -> " in filename:
            filename = filename.split(" -> ", 1)[1]
        if filename in _OUR_OWN_ARTIFACTS:
            continue
        return True
    return False


def _label_in_stash_list(listing: str, label: str) -> bool:
    """``git stash list --format=%gd:%s`` prints one line per stash;
    the subject usually starts ``On <branch>:`` or ``WIP on <branch>:``
    and ends with the message we passed. Match by suffix only.
    """
    for line in listing.splitlines():
        _, _, subject = line.partition(":")
        if subject.strip().endswith(label):
            return True
    return False


# ── Snapshot ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PhaseSnapshotResult:
    """Outcome of one :func:`take_phase_snapshot` call.

    ``stash_ref`` is the commit hash from ``git rev-parse stash@{0}``,
    because ``stash@{N}`` is a positional reflog ref — it slides forward
    as subsequent stash pushes land on top. The hash is the only stable
    handle to *this* snapshot. ``stash_ref_name`` is preserved only for
    the operator recipe (``git stash apply stash@{0}`` is more familiar
    than the hash form).
    """

    stash_ref: str
    stash_ref_name: str
    label: str
    dirty: bool

    @classmethod
    def clean(cls) -> "PhaseSnapshotResult":
        return cls(stash_ref="", stash_ref_name="", label="", dirty=False)


def take_phase_snapshot(
    worktree_path: Path,
    phase: str,
    ticket_key: str,
    *,
    iso: str | None = None,
) -> PhaseSnapshotResult:
    """Stash if dirty, then derive + verify the resulting ref.

    AC §C1 sequence:
      1. ``git stash push -m <label> --include-untracked``
      2. ``git rev-parse stash@{0}`` → resolved hash
      3. ``git stash list --format=%gd:%s`` MUST contain the label

    Each step's failure short-circuits to a clean result + WARN log. A
    missing snapshot must NEVER wedge the FSM transition — the
    durability of the *progress.txt* row is the operator-visible
    signal, and the row honestly records ``phase_snapshot_stash_ref=""``
    when the stash step failed.
    """
    try:
        dirty = _worktree_dirty(worktree_path)
    except RuntimeError as exc:
        log.warning("phase-snapshot: dirty-check failed (%s); returning clean", exc)
        return PhaseSnapshotResult.clean()

    if not dirty:
        return PhaseSnapshotResult.clean()

    label = build_stash_label(phase, ticket_key, iso=iso)
    try:
        _git(worktree_path, ["stash", "push", "-m", label, "--include-untracked"])
    except RuntimeError as exc:
        log.warning("phase-snapshot: git stash push failed (%s); returning clean", exc)
        return PhaseSnapshotResult.clean()

    try:
        ref_hash = _git(worktree_path, ["rev-parse", "stash@{0}"]).strip()
    except RuntimeError as exc:
        log.warning("phase-snapshot: git rev-parse stash@{0} failed (%s)", exc)
        return PhaseSnapshotResult.clean()

    try:
        listing = _git(worktree_path, ["stash", "list", f"--format={_STASH_LIST_FORMAT}"])
    except RuntimeError as exc:
        log.warning("phase-snapshot: git stash list failed (%s)", exc)
        return PhaseSnapshotResult.clean()

    if not _label_in_stash_list(listing, label):
        log.warning("phase-snapshot: label %r not found in stash list", label)
        return PhaseSnapshotResult.clean()

    return PhaseSnapshotResult(
        stash_ref=ref_hash,
        stash_ref_name="stash@{0}",
        label=label,
        dirty=True,
    )


# ── Atomic write (AC §C1 — fsync + rename) ─────────────────────────────


def write_progress_atomic(worktree_path: Path, progress: Progress) -> None:
    """Atomic-rename + fsync write of progress.txt.

    Sequence:
      1. write JSON to ``progress.txt.tmp``
      2. ``fsync`` the file descriptor (forces page-cache → disk for the body)
      3. ``os.replace`` to ``progress.txt`` (atomic on POSIX same-fs)
      4. ``fsync`` the parent directory (forces the rename's directory
         entry to disk; without this, a power-loss between (3) and the
         fs-internal dir flush can leave the temp file but lose the
         final ``progress.txt`` entry — same hazard SQLite WAL guards
         against)
    """
    path = progress_path(worktree_path)
    tmp = path.with_suffix(path.suffix + _PROGRESS_TMP_SUFFIX)
    payload = (progress.to_json() + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(worktree_path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_progress(worktree_path: Path) -> Progress | None:
    """Return the persisted :class:`Progress` row, or ``None`` if absent
    / empty / malformed. Malformed rows log WARN — they signal a torn
    write (host crash between fsync and rename) that the operator
    should be told about, but they must not raise into the runner main
    loop (a half-written progress.txt should not block fresh pickup).
    """
    path = progress_path(worktree_path)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("read_progress: %s read failed (%s)", path, exc)
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Progress.from_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("read_progress: malformed %s — %s", path, exc)
        return None


# ── Public composition + recovery surface ──────────────────────────────


def record_phase(
    worktree_path: Path,
    phase: str,
    ticket_key: str,
    *,
    iso: str | None = None,
) -> PhaseSnapshotResult:
    """Compose: :func:`take_phase_snapshot` + :func:`write_progress_atomic`.

    Used by the runner main loop at every FSM state transition (see
    ``auto-runner-jira.py``). Errors in the snapshot path are absorbed
    by :func:`take_phase_snapshot`; errors writing progress.txt
    propagate so the operator sees the durability failure — a torn
    progress.txt would silently break the resume path.
    """
    snap = take_phase_snapshot(worktree_path, phase, ticket_key, iso=iso)
    when = iso if iso is not None else _utc_iso_now()
    write_progress_atomic(
        worktree_path,
        Progress(
            ticket_key=ticket_key,
            phase_completed=phase,
            phase_snapshot_stash_ref=snap.stash_ref,
            phase_snapshot_stash_label=snap.label,
            phase_snapshot_stash_ref_name=snap.stash_ref_name,
            iso_utc=when,
        ),
    )
    return snap


def format_recovered_comment(progress: Progress) -> str:
    """Build the ``[progress-recovered]`` comment per AC §C1.

    Mirrors the snippet in the ticket description verbatim — operators
    grep these strings, so the literal phrasing is part of the contract.
    """
    recipe_ref = progress.phase_snapshot_stash_ref_name or progress.phase_snapshot_stash_ref
    return (
        f"[progress-recovered] phase={progress.phase_completed} "
        f"stash_ref={progress.phase_snapshot_stash_ref} "
        f'stash_label="{progress.phase_snapshot_stash_label}"\n'
        f"Operator: run `git stash apply {recipe_ref}` to restore worktree."
    )


def find_recovered_snapshot(worktree_path: Path) -> Progress | None:
    """Return the prior :class:`Progress` row iff it points to a stash
    entry still present in this worktree.

    Returns ``None`` when:
      - ``progress.txt`` is absent / malformed
      - ``phase_snapshot_stash_ref`` is empty (prior phase saw a clean
        worktree — nothing to surface; the bookkeeping row is still
        truthful but the operator doesn't need a comment)
      - the recorded label is not in ``git stash list`` (operator
        already ran ``git stash apply`` / ``git stash drop``, or this
        is a fresh worktree on a different host)

    AC §C1 mandates the negative-listing branch — the runner must not
    cry wolf about a snapshot it can no longer hand the operator.
    """
    progress = read_progress(worktree_path)
    if progress is None:
        return None
    if not progress.phase_snapshot_stash_ref:
        return None
    if not progress.phase_snapshot_stash_label:
        return None
    try:
        listing = _git(worktree_path, ["stash", "list", f"--format={_STASH_LIST_FORMAT}"])
    except RuntimeError as exc:
        log.warning("find_recovered_snapshot: git stash list failed (%s)", exc)
        return None
    if not _label_in_stash_list(listing, progress.phase_snapshot_stash_label):
        return None
    return progress
