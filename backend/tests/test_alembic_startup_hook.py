"""OP-1166 -- startup Alembic upgrade hook contract tests."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from backend import alembic_startup_hook as hook


def _manifest(tmp_path: Path, head: str = "rev_image") -> Path:
    path = tmp_path / "MANIFEST.json"
    path.write_text(
        '{"image_sha":"sha","build_time":"2026-05-16T00:00:00Z",'
        f'"git_ref":"test","alembic_head_in_image":"{head}"}}',
        encoding="utf-8",
    )
    return path


class _Scalar:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar(self) -> bool:
        return self._value


class _Connection:
    def __init__(self) -> None:
        self.locked = False

    def execute(self, statement, params=None):  # noqa: ANN001
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            self.locked = True
            return _Scalar(True)
        if "pg_advisory_unlock" in sql:
            self.locked = False
            return _Scalar(True)
        raise AssertionError(f"unexpected SQL: {sql}")


class _Engine:
    def __init__(self) -> None:
        self.conn = _Connection()

    def connect(self):
        return self

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


def _install_fakes(monkeypatch: pytest.MonkeyPatch, drift_payloads):
    engine = _Engine()
    upgrades: list[str] = []
    payload_iter = iter(drift_payloads)

    monkeypatch.setattr(hook, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(hook, "_check_drift", lambda db_url: next(payload_iter))
    monkeypatch.setattr(
        hook, "_upgrade_head", lambda db_url: upgrades.append(db_url),
    )
    return engine, upgrades


def test_forward_drift_runs_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path, "rev_image")
    _engine, upgrades = _install_fakes(
        monkeypatch,
        [
            {
                "reason": "alembic_head_drift",
                "drift_direction": "image_ahead",
                "image_head": ["rev_image"],
                "db_head": ["rev_old"],
            },
            {
                "reason": "heads_match",
                "drift_direction": "match",
                "image_heads": ["rev_image"],
                "db_heads": ["rev_image"],
            },
        ],
    )

    assert hook.maybe_run_startup_upgrade(
        db_url="postgresql://db", image_head_path=str(manifest)
    ) == "rev_image"
    assert upgrades == ["postgresql://db"]


def test_aligned_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path, "rev_image")
    _engine, upgrades = _install_fakes(
        monkeypatch,
        [
            {
                "reason": "heads_match",
                "drift_direction": "match",
                "image_heads": ["rev_image"],
                "db_heads": ["rev_image"],
            },
        ],
    )

    assert hook.maybe_run_startup_upgrade(
        db_url="postgresql://db", image_head_path=str(manifest)
    ) == "rev_image"
    assert upgrades == []


def test_backward_drift_raises_with_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path, "rev_image")
    _engine, upgrades = _install_fakes(
        monkeypatch,
        [
            {
                "reason": "alembic_head_drift",
                "drift_direction": "db_ahead",
                "image_head": ["rev_image"],
                "db_head": ["rev_db"],
            },
        ],
    )

    with pytest.raises(hook.AlembicBackwardDrift) as exc_info:
        hook.maybe_run_startup_upgrade(
            db_url="postgresql://db", image_head_path=str(manifest)
        )

    assert upgrades == []
    assert "rev_image" in str(exc_info.value)
    assert "rev_db" in str(exc_info.value)
    assert "deploy image >= db_head" in str(exc_info.value)


class _SharedLockConnection:
    def __init__(self, lock, owned) -> None:  # noqa: ANN001
        self._lock = lock
        self._owned = owned

    def execute(self, statement, params=None):  # noqa: ANN001
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            acquired = self._lock.acquire(block=False)
            self._owned.value = bool(acquired)
            return _Scalar(bool(acquired))
        if "pg_advisory_unlock" in sql:
            if self._owned.value:
                self._lock.release()
                self._owned.value = False
            return _Scalar(True)
        raise AssertionError(f"unexpected SQL: {sql}")


class _SharedLockEngine:
    def __init__(self, lock, owned) -> None:  # noqa: ANN001
        self._lock = lock
        self._owned = owned

    def connect(self):
        return self

    def __enter__(self):
        return _SharedLockConnection(self._lock, self._owned)

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


def _lock_holder(ready, release, lock, out) -> None:  # noqa: ANN001
    owned = mp.Value("b", False)
    hook.create_engine = lambda *a, **kw: _SharedLockEngine(lock, owned)
    hook._check_drift = lambda db_url: {
        "reason": "heads_match",
        "drift_direction": "match",
        "db_heads": ["rev_image"],
    }
    hook._upgrade_head = lambda db_url: None
    try:
        with _SharedLockEngine(lock, owned).connect() as conn:
            hook._acquire_advisory_lock(conn, lock_timeout_s=1)
            ready.set()
            release.wait(5)
            hook._release_advisory_lock(conn)
        out.put("released")
    except Exception as exc:  # pragma: no cover - child diagnostic
        out.put(type(exc).__name__)


def test_lock_acquire_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path, "rev_image")
    ctx = mp.get_context("fork")
    ready = ctx.Event()
    release = ctx.Event()
    lock = ctx.Lock()
    out = ctx.Queue()
    holder = ctx.Process(target=_lock_holder, args=(ready, release, lock, out))
    holder.start()
    try:
        assert ready.wait(2), "holder process did not acquire lock"
        owned = ctx.Value("b", False)
        monkeypatch.setattr(
            hook,
            "create_engine",
            lambda *a, **kw: _SharedLockEngine(lock, owned),
        )
        monkeypatch.setattr(hook, "_check_drift", lambda db_url: {})
        monkeypatch.setattr(hook, "_upgrade_head", lambda db_url: None)
        monkeypatch.setattr(hook, "LOCK_POLL_INTERVAL_S", 0.01)

        with pytest.raises(hook.AlembicLockTimeout):
            hook.maybe_run_startup_upgrade(
                db_url="postgresql://db",
                image_head_path=str(manifest),
                lock_timeout_s=0.05,
            )
    finally:
        release.set()
        holder.join(2)
        assert holder.exitcode == 0
        assert out.get_nowait() == "released"


def test_idempotent_forward_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path, "rev_image")
    _engine, upgrades = _install_fakes(
        monkeypatch,
        [
            {
                "reason": "alembic_head_drift",
                "drift_direction": "image_ahead",
                "image_head": ["rev_image"],
                "db_head": ["rev_old"],
            },
            {
                "reason": "heads_match",
                "drift_direction": "match",
                "db_heads": ["rev_image"],
            },
            {
                "reason": "heads_match",
                "drift_direction": "match",
                "db_heads": ["rev_image"],
            },
        ],
    )

    assert hook.maybe_run_startup_upgrade(
        db_url="postgresql://db", image_head_path=str(manifest)
    ) == "rev_image"
    assert hook.maybe_run_startup_upgrade(
        db_url="postgresql://db", image_head_path=str(manifest)
    ) == "rev_image"
    assert upgrades == ["postgresql://db"]


def test_manifest_missing_handled(tmp_path: Path) -> None:
    missing = tmp_path / "MANIFEST.json"
    with pytest.raises(hook.AlembicManifestError) as exc_info:
        hook.maybe_run_startup_upgrade(
            db_url="postgresql://db", image_head_path=str(missing)
        )
    assert "image manifest missing" in str(exc_info.value)
    assert str(missing) in str(exc_info.value)
