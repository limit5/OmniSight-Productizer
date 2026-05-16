"""OP-717 — unit tests for backend.agents.conflict_enrichment.

Each test mocks ``asyncio.create_subprocess_exec`` so we exercise
``_run`` end-to-end without needing a real git repo or network. Bytes
returned via the mock subprocess match what real git would emit:

  * ``git fetch ... develop`` → rc=0, no output
  * ``git fetch ... <revision>`` → rc=0
  * ``git checkout origin/develop`` → rc=0
  * ``git merge --no-commit --no-ff <ref>`` → rc=0 if clean, rc=1 if conflict
  * ``git diff --name-only --diff-filter=U`` → list of conflicted paths
  * ``git log -1 --format=%s ...`` → commit subject
  * ``git merge --abort`` → rc=0

The conflict files are real on disk (in a tmp_path repo dir) so
``_read_conflict_file`` exercises actual file I/O on synthetic
content with ``<<<<<<<`` markers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents.conflict_enrichment import (
    ConflictFile,
    EnrichmentResult,
    _slice_context,
    enrich_via_local_merge,
)


# ──────────────────────────────────────────────────────────────────
# pure helpers (no I/O)
# ──────────────────────────────────────────────────────────────────


class TestSliceContext:

    def test_slice_around_first_marker(self):
        body = "\n".join(
            [f"line{i}" for i in range(50)]
            + ["<<<<<<< HEAD", "ours", "=======", "theirs", ">>>>>>> branch"]
            + [f"after{i}" for i in range(20)]
        )
        ctx = _slice_context(body, 20)
        # marker is at line 50 (0-indexed); window is [30, 50+60) → contains marker
        assert "<<<<<<< HEAD" in ctx
        assert "line30" in ctx
        # 30 lines of "lineX" + 5 marker lines + up to 20 "after"
        assert ctx.count("\n") <= 80

    def test_slice_no_marker_falls_back_to_head(self):
        body = "\n".join(f"line{i}" for i in range(100))
        ctx = _slice_context(body, 20)
        # No marker — return first 40 lines (n_lines * 2)
        assert ctx.startswith("line0\n")
        assert "line39" in ctx
        assert "line50" not in ctx


# ──────────────────────────────────────────────────────────────────
# enrich_via_local_merge — input validation
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestInputValidation:

    async def test_missing_change_number(self):
        r = await enrich_via_local_merge(
            change_number="", patchset_revision="abc",
            project="omnisight/x",
        )
        assert r.mergeable is False
        assert "missing" in r.error

    async def test_missing_revision(self):
        r = await enrich_via_local_merge(
            change_number=92, patchset_revision="",
            project="omnisight/x",
        )
        assert "missing" in r.error

    async def test_missing_project(self):
        r = await enrich_via_local_merge(
            change_number=92, patchset_revision="abc", project="",
        )
        assert "missing" in r.error


# ──────────────────────────────────────────────────────────────────
# enrich_via_local_merge — full flow with mocked subprocess
# ──────────────────────────────────────────────────────────────────


def _mock_proc(rc: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """Build an AsyncMock that imitates asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = rc
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    return proc


@pytest.mark.asyncio
async def test_clean_merge_returns_mergeable_true(tmp_path, monkeypatch):
    """git merge --no-commit succeeds → mergeable=True, no conflict files."""
    repo_dir = tmp_path / "omnisight_x" / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()  # marker so clone is skipped

    monkeypatch.setattr(
        "backend.agents.conflict_enrichment._WORK_ROOT", tmp_path,
    )

    # Track the sequence of git invocations
    calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        argv = list(args)
        # Match key commands by suffix
        if "fetch" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--abort" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--no-commit" in argv:
            return _mock_proc(0)            # CLEAN merge
        if "checkout" in argv:
            return _mock_proc(0)
        if "reset" in argv:
            return _mock_proc(0)
        if "log" in argv:
            return _mock_proc(0, stdout=b"head subject" if "develop" in argv[-1] else b"incoming subject")
        return _mock_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await enrich_via_local_merge(
        change_number=100, patchset_revision="abcdef0",
        project="omnisight/x", patchset_number=1,
    )
    assert result.mergeable is True
    assert result.conflict_files == []
    assert not result.too_many
    assert result.error == ""


@pytest.mark.asyncio
async def test_single_file_conflict_returns_marker_text(tmp_path, monkeypatch):
    """git merge fails → diff lists 1 file → its content with markers
    is returned via ConflictFile."""
    repo_dir = tmp_path / "omnisight_x" / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    # Plant a real conflict file so _read_conflict_file picks it up
    conflict_path = repo_dir / "src" / "preferences.py"
    conflict_path.parent.mkdir(parents=True)
    conflict_text = (
        "def f():\n"
        "    return 1\n"
        "<<<<<<< HEAD\n"
        "    return 2\n"
        "=======\n"
        "    return 3\n"
        ">>>>>>> branch\n"
    )
    conflict_path.write_text(conflict_text)

    monkeypatch.setattr(
        "backend.agents.conflict_enrichment._WORK_ROOT", tmp_path,
    )

    async def fake_exec(*args, **kwargs):
        argv = list(args)
        if "fetch" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--abort" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--no-commit" in argv:
            return _mock_proc(1)  # CONFLICT
        if "diff" in argv:
            return _mock_proc(0, stdout=b"src/preferences.py\n")
        if "log" in argv:
            return _mock_proc(0, stdout=b"subject")
        return _mock_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await enrich_via_local_merge(
        change_number=92, patchset_revision="def0",
        project="omnisight/x", patchset_number=1,
    )
    assert result.mergeable is False
    assert not result.too_many
    assert len(result.conflict_files) == 1
    assert result.conflict_files[0].path == "src/preferences.py"
    assert "<<<<<<< HEAD" in result.conflict_files[0].conflict_text
    assert "=======" in result.conflict_files[0].conflict_text
    assert ">>>>>>> branch" in result.conflict_files[0].conflict_text
    # file_context should include the marker since the file is small
    assert "<<<<<<< HEAD" in result.conflict_files[0].file_context


@pytest.mark.asyncio
async def test_multi_file_conflict_returns_all(tmp_path, monkeypatch):
    """3 conflicted files → all returned, alphabetical order."""
    repo_dir = tmp_path / "omnisight_x" / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    body = "before\n<<<<<<< HEAD\na\n=======\nb\n>>>>>>> br\nafter\n"
    for fn in ("a.py", "b.py", "c.md"):
        (repo_dir / fn).write_text(body)

    monkeypatch.setattr(
        "backend.agents.conflict_enrichment._WORK_ROOT", tmp_path,
    )

    async def fake_exec(*args, **kwargs):
        argv = list(args)
        if "fetch" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--abort" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--no-commit" in argv:
            return _mock_proc(1)
        if "diff" in argv:
            # Return out of order to verify alphabetisation
            return _mock_proc(0, stdout=b"c.md\nb.py\na.py\n")
        if "log" in argv:
            return _mock_proc(0, stdout=b"subject")
        return _mock_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await enrich_via_local_merge(
        change_number=92, patchset_revision="def0",
        project="omnisight/x", patchset_number=1,
    )
    assert result.mergeable is False
    paths = [cf.path for cf in result.conflict_files]
    assert paths == ["a.py", "b.py", "c.md"]
    assert all("<<<<<<< HEAD" in cf.conflict_text for cf in result.conflict_files)


@pytest.mark.asyncio
async def test_too_many_conflicts_caps_out(tmp_path, monkeypatch):
    """6 conflicts (> _MAX_CONFLICT_FILES=5) → too_many=True, no file
    contents read (saves I/O on the inevitable abstain)."""
    repo_dir = tmp_path / "omnisight_x" / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    monkeypatch.setattr(
        "backend.agents.conflict_enrichment._WORK_ROOT", tmp_path,
    )

    async def fake_exec(*args, **kwargs):
        argv = list(args)
        if "fetch" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--abort" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--no-commit" in argv:
            return _mock_proc(1)
        if "diff" in argv:
            return _mock_proc(
                0, stdout=b"a.py\nb.py\nc.py\nd.py\ne.py\nf.py\n",
            )
        if "log" in argv:
            return _mock_proc(0, stdout=b"subject")
        return _mock_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await enrich_via_local_merge(
        change_number=92, patchset_revision="def0",
        project="omnisight/x", patchset_number=1,
    )
    assert result.mergeable is False
    assert result.too_many
    # Conflict_files lists all 6 paths (for operator visibility), but
    # all conflict_text is empty (we skipped the read pass).
    assert len(result.conflict_files) == 6
    assert all(cf.conflict_text == "" for cf in result.conflict_files)


@pytest.mark.asyncio
async def test_fetch_failure_returns_error(tmp_path, monkeypatch):
    """Network/perm failure on fetch → error field populated."""
    repo_dir = tmp_path / "omnisight_x" / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    monkeypatch.setattr(
        "backend.agents.conflict_enrichment._WORK_ROOT", tmp_path,
    )

    async def fake_exec(*args, **kwargs):
        argv = list(args)
        if "fetch" in argv:
            return _mock_proc(
                128, stderr=b"fatal: cannot connect to remote",
            )
        return _mock_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await enrich_via_local_merge(
        change_number=92, patchset_revision="def0",
        project="omnisight/x", patchset_number=1,
    )
    assert result.mergeable is False
    assert "fetch develop failed" in result.error


@pytest.mark.asyncio
async def test_binary_file_marked_binary(tmp_path, monkeypatch):
    """Conflicted binary file → ConflictFile(binary=True), no text."""
    repo_dir = tmp_path / "omnisight_x" / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    (repo_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")

    monkeypatch.setattr(
        "backend.agents.conflict_enrichment._WORK_ROOT", tmp_path,
    )

    async def fake_exec(*args, **kwargs):
        argv = list(args)
        if "fetch" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--abort" in argv:
            return _mock_proc(0)
        if "merge" in argv and "--no-commit" in argv:
            return _mock_proc(1)
        if "diff" in argv:
            return _mock_proc(0, stdout=b"logo.png\n")
        if "log" in argv:
            return _mock_proc(0, stdout=b"subject")
        return _mock_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await enrich_via_local_merge(
        change_number=92, patchset_revision="def0",
        project="omnisight/x", patchset_number=1,
    )
    assert len(result.conflict_files) == 1
    assert result.conflict_files[0].binary is True
    assert result.conflict_files[0].conflict_text == "<binary file>"


# ──────────────────────────────────────────────────────────────────────
#  OP-1197 — _project_lock works across asyncio.run() boundaries
# ──────────────────────────────────────────────────────────────────────


def test_project_lock_works_across_separate_event_loops():
    """OP-1197 regression — the bridge daemon's per-event threads
    each call ``asyncio.run(...)``, creating a fresh event loop per
    thread. Before this fix, ``_LOCKS_GUARD`` + ``_LOCKS[project]``
    were ``asyncio.Lock`` objects bound at first-acquire to a
    specific loop; subsequent acquire from a different loop raised
    ``RuntimeError: <asyncio.locks.Lock ...> is bound to a different
    event loop``. With ``threading.Lock`` they work loop-agnostically.

    This test simulates the daemon's pattern: acquire the lock in
    Loop A, dispose Loop A, acquire again in fresh Loop B. The
    original asyncio.Lock implementation would have raised; the
    threading.Lock implementation should not.
    """
    import asyncio
    from backend.agents import conflict_enrichment as ce

    # Reset module-global state so the test runs deterministically
    # regardless of prior test order.
    ce._LOCKS.clear()
    # _LOCKS_GUARD is a threading.Lock now — needs no reset (it's
    # not loop-bound by construction).

    async def acquire_and_release(project: str):
        async with ce._project_lock(project):
            # In real use, this is where git clone/fetch/merge runs.
            # For the test, just confirm we got in + can release.
            return True

    # Loop A — first acquire creates the per-project Lock + uses it.
    result_a = asyncio.run(acquire_and_release("omnisight/x"))
    assert result_a is True
    assert "omnisight/x" in ce._LOCKS

    # Loop A has been disposed by asyncio.run. Loop B is a fresh
    # event loop — this is the scenario that previously raised
    # `bound to a different event loop`.
    result_b = asyncio.run(acquire_and_release("omnisight/x"))
    assert result_b is True

    # Same project key, same threading.Lock object reused.
    # (Sanity-check the lock dict didn't double-up entries.)
    assert len([k for k in ce._LOCKS if k == "omnisight/x"]) == 1


def test_project_lock_serialises_concurrent_threads():
    """Multiple threads acquiring the same project lock should
    serialise — only one thread holds the lock at a time."""
    import asyncio
    import threading
    import time
    from backend.agents import conflict_enrichment as ce

    ce._LOCKS.clear()

    holders: list[str] = []
    barrier = threading.Barrier(3)  # 3 threads + main = 3 (main not in barrier)

    async def hold_briefly(thread_id: str):
        async with ce._project_lock("omnisight/x"):
            holders.append(f"enter-{thread_id}")
            # Small sleep so concurrent threads have a chance to
            # try the acquire — proves serialisation.
            await asyncio.sleep(0.01)
            holders.append(f"exit-{thread_id}")

    def worker(thread_id: str):
        barrier.wait()  # release all 3 workers at once
        asyncio.run(hold_briefly(thread_id))

    threads = [
        threading.Thread(target=worker, args=(str(i),))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "thread hung — lock probably deadlocked"

    # 6 entries: enter-N then exit-N for each N, no interleaving.
    assert len(holders) == 6
    # Check pairings — for each enter, the very next entry must be
    # the matching exit (no interleaving == serialised).
    for i in range(0, 6, 2):
        enter = holders[i]
        exit_ = holders[i + 1]
        assert enter.startswith("enter-")
        assert exit_.startswith("exit-")
        assert enter[len("enter-"):] == exit_[len("exit-"):], (
            f"thread interleaving detected at index {i}: "
            f"{enter} then {exit_} — lock did not serialise"
        )
