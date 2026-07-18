"""Offline tests for the U6-0 GAP-5c-loop sub-leaf B workspace-root-fd resolver (no PostgreSQL)."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat

import pytest

from backend.agents import workspace_root_resolver as resolver

_ENV = resolver._ROOTS_ENV


def _wid_for(root: pathlib.Path, tenant: str = "t-x", adapter: str = "runner_sdk") -> str:
    resolved = str(pathlib.Path(root).resolve())
    return "ws:%s:%s:%s" % (tenant, adapter, hashlib.sha256(resolved.encode("utf-8")).hexdigest())


def test_fail_closed_when_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert resolver._load_roots() == []
    assert resolver.resolve_root_fd(_wid_for(tmp_path)) is None


def test_allowlisted_matching_id_returns_a_directory_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    monkeypatch.setenv(_ENV, json.dumps([str(tmp_path)]))
    fd = resolver.resolve_root_fd(_wid_for(tmp_path))
    assert isinstance(fd, int) and fd >= 0
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        (tmp_path / "child.txt").write_text("x", encoding="utf-8")
        child = os.open("child.txt", os.O_RDONLY, dir_fd=fd)   # the fd works as a dir_fd anchor
        os.close(child)
    finally:
        os.close(fd)


def test_real_guard_drift_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    # Feed B an id built by the REAL guard recipe. If authoritative_context_resolver's hash recipe ever drifts, the id
    # changes and B (mirroring the recipe) fails to resolve => this test fails, catching the drift.
    from backend.agents import authoritative_context_resolver as acr

    root = tmp_path / "wörk-空間"     # non-ASCII: exercises the utf-8 encode in both the guard's sha256 and B's
    root.mkdir()

    class _Shadow:
        workspace_root = str(root)

    class _Ctx:
        tenant_id = "t-drift"

    monkeypatch.setattr(acr, "resolve_shadow_context", lambda adapter, tool, schema: _Shadow())
    built = acr.resolve_authoritative_workspace(_Ctx(), "runner_sdk", "Write", "v1")
    assert built is not None
    workspace_id, _guard_root = built

    monkeypatch.setenv(_ENV, json.dumps([str(root)]))
    fd = resolver.resolve_root_fd(workspace_id)
    assert isinstance(fd, int) and fd >= 0
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def test_root_not_allowlisted_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(_ENV, json.dumps([str(other)]))
    assert resolver.resolve_root_fd(_wid_for(tmp_path)) is None


def test_digest_mismatch_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(_ENV, json.dumps([str(root)]))
    assert resolver.resolve_root_fd(_wid_for(other)) is None   # allowlisted root, but the id is for another dir


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "nope",
        "ws:t:a:",
        "ws:t:a:" + "z" * 64,
        "ws:t:a:" + "A" * 64,       # uppercase hex is rejected (hexdigest is lowercase)
        "ws:t:a:" + "a" * 63,       # wrong length
        "not-ws:t:a:" + "a" * 64,
    ],
)
def test_malformed_id_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, bad_id: str) -> None:
    monkeypatch.setenv(_ENV, json.dumps([str(tmp_path)]))
    assert resolver.resolve_root_fd(bad_id) is None


def test_non_str_id_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv(_ENV, json.dumps([str(tmp_path)]))
    assert resolver.resolve_root_fd(None) is None       # type: ignore[arg-type]
    assert resolver.resolve_root_fd(123) is None        # type: ignore[arg-type]


def test_symlink_root_resolves_to_same_digest(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    # Allowlist the SYMLINK; query the id for the REAL dir. Both Path().resolve() to `real` => same digest => match.
    monkeypatch.setenv(_ENV, json.dumps([str(link)]))
    fd = resolver.resolve_root_fd(_wid_for(real))
    assert isinstance(fd, int)
    try:
        assert os.fstat(fd).st_ino == real.stat().st_ino
    finally:
        os.close(fd)


def test_missing_root_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    wid = _wid_for(gone)
    monkeypatch.setenv(_ENV, json.dumps([str(gone)]))
    gone.rmdir()
    assert resolver.resolve_root_fd(wid) is None        # matched digest, but os.open fails => None


def test_file_root_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setenv(_ENV, json.dumps([str(f)]))
    assert resolver.resolve_root_fd(_wid_for(f)) is None    # O_DIRECTORY open of a regular file => OSError => None


def test_non_absolute_and_whitespace_entries_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    monkeypatch.setenv(_ENV, json.dumps([".", "rel/x", "  ", " /leading", str(tmp_path)]))
    assert resolver._load_roots() == [str(tmp_path)]


def test_one_bad_entry_does_not_abort_a_good_one(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv(_ENV, json.dumps(["/non\x00exist", str(tmp_path)]))   # NUL path => Path.resolve ValueError
    fd = resolver.resolve_root_fd(_wid_for(tmp_path))
    assert isinstance(fd, int)
    os.close(fd)


def test_symlink_loop_entry_is_non_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    # On Python 3.12 Path.resolve() raises RuntimeError for a symlink loop; the per-entry catch must skip it so a later
    # valid root still resolves (the loop entry is placed first).
    loop = tmp_path / "loop"
    os.symlink(loop, loop)          # self-referential symlink
    valid = tmp_path / "valid"
    valid.mkdir()
    monkeypatch.setenv(_ENV, json.dumps([str(loop), str(valid)]))
    fd = resolver.resolve_root_fd(_wid_for(valid))
    assert isinstance(fd, int)
    os.close(fd)


@pytest.mark.parametrize("bad_env", ["not json", "{}", "[1, 2]", '[""]', "[null]", "42"])
def test_malformed_env_yields_no_roots(monkeypatch: pytest.MonkeyPatch, bad_env: str) -> None:
    monkeypatch.setenv(_ENV, bad_env)
    assert resolver._load_roots() == []


def test_duplicate_roots_resolve_once(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv(_ENV, json.dumps([str(tmp_path), str(tmp_path)]))
    opens = {"n": 0}
    real_open = resolver.os.open

    def counting_open(*args, **kwargs):
        opens["n"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(resolver.os, "open", counting_open)
    fd = resolver.resolve_root_fd(_wid_for(tmp_path))
    assert isinstance(fd, int)
    assert opens["n"] == 1          # the first matching root short-circuits => opened exactly once
    os.close(fd)


def test_runtime_flippable(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    wid = _wid_for(tmp_path)
    monkeypatch.delenv(_ENV, raising=False)
    assert resolver.resolve_root_fd(wid) is None
    monkeypatch.setenv(_ENV, json.dumps([str(tmp_path)]))
    fd = resolver.resolve_root_fd(wid)
    assert isinstance(fd, int)
    os.close(fd)
    monkeypatch.delenv(_ENV, raising=False)
    assert resolver.resolve_root_fd(wid) is None


def test_no_fd_leak_on_none_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv(_ENV, json.dumps([str(tmp_path)]))
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(64):
        assert resolver.resolve_root_fd(_wid_for(tmp_path / "nomatch")) is None
        assert resolver.resolve_root_fd("bad-id") is None
    assert len(os.listdir("/proc/self/fd")) == before


def test_module_is_dormant_with_no_production_caller() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    # resume_loop.py (default-OFF, OMNISIGHT_U6_RESUME_LOOP_ENABLED) imports
    # resolve_root_fd to build the dispatch executor (GAP-5c-loop-C); it is the
    # sole production caller and keeps the resolver dormant-by-gate.
    allowed = {"workspace_root_resolver.py", "resume_loop.py"}
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "versions" in parts:
            continue
        if path.name in allowed:
            continue
        if "workspace_root_resolver" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))
    assert offenders == [], (
        f"workspace_root_resolver reachable only via the default-OFF loop; "
        f"unexpected caller: {offenders}"
    )
