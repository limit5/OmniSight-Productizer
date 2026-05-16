"""[OP-973] AUDIT-19c — tests for the continuous develop -> staging sync.

Three surfaces, stdlib + pytest only (no real network / docker / git remote):

1. **Script behaviour** — scripts/sync_staging_to_develop.sh driven via
   subprocess against a temp git repo (for the develop-tip fetch) and
   stub ``docker`` / ``curl`` / ``staging_deploy.sh`` binaries on ``PATH``.
   Covers: idempotent no-op when staging is already on the develop tip,
   a fresh tip triggering ``staging_deploy.sh --image-tag <tip>`` + the
   alembic step + the health re-check, the DevelopTipFetchFailed transient
   (exit 0, staging untouched), StagingDeployFailed (exit 1), and the
   StagingMigrationFailed / StagingHealthzFailed auto-revert paths (the
   deployer is re-invoked with the *previous* tag).
2. **Systemd contract** — staging-sync.{service,timer}: oneshot shape,
   ExecStart wiring, OnFailure -> staging-gate-alert.service, the
   release-audit EnvironmentFile, the 10-min cadence, and the AC #6 boot
   order (staging-sync@2min < staging-gate-canary@3min < staging-gate-smoke@5min).
3. **Shell shape** — shebang + ``set -euo pipefail`` + executable bit.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sync_staging_to_develop.sh"
SYSTEMD = REPO_ROOT / "deploy" / "systemd"


# ─────────────────────────── fixtures / helpers ─────────────────────────────


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(path: Path, branch: str = "develop") -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, capture_output=True)
    _git(["config", "user.email", "t@x"], path)
    _git(["config", "user.name", "t"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "seed"], path)
    return _git(["rev-parse", "HEAD"], path)


def _commit(repo: Path, msg: str, fname: str = "x.txt") -> str:
    (repo / fname).write_text(msg + "\n", encoding="utf-8")
    _git(["add", fname], repo)
    _git(["commit", "-q", "-m", msg], repo)
    return _git(["rev-parse", "HEAD"], repo)


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Return (work, remote): ``work`` is a clone of ``remote`` (branch develop).

    ``resolve_develop_tip`` does ``git -C work fetch origin develop`` then
    ``git rev-parse FETCH_HEAD`` — so the script sees whatever ``remote``'s
    develop tip is after the caller advances it.
    """
    remote = tmp_path / "remote"
    _init_repo(remote)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    _git(["config", "user.email", "t@x"], work)
    _git(["config", "user.name", "t"], work)
    _git(["config", "commit.gpgsign", "false"], work)
    return work, remote


@pytest.fixture()
def stub_bin(tmp_path: Path) -> Path:
    """A ``bin/`` dir holding stub ``docker`` / ``curl`` + a stub deployer.

    * ``docker``  — any invocation exits ``$STUB_DOCKER_RC`` (default 0).
    * ``curl``    — a ``-X POST`` (the alert webhook) always exits 0; any
                    other call (the /health GET) exits ``$STUB_CURL_RC``
                    (default 0).
    * ``staging_deploy.sh`` — parses ``--image-tag T``; appends ``T`` to
                    ``$DEPLOY_CALLS_FILE``; writes ``T`` to
                    ``$STATE_DIR/active_tag`` and ``blue`` to
                    ``$STATE_DIR/active_color``; exits 1 if ``T`` equals
                    ``$STUB_DEPLOY_FAIL_TAG`` else 0.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\nexit ${STUB_DOCKER_RC:-0}\n", encoding="utf-8"
    )
    (bindir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do [[ "$a" == "POST" ]] && exit 0; done\n'
        "exit ${STUB_CURL_RC:-0}\n",
        encoding="utf-8",
    )
    (bindir / "staging_deploy.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'tag=""\n'
        'while [[ $# -gt 0 ]]; do case "$1" in --image-tag) tag="$2"; shift 2;; *) shift;; esac; done\n'
        '[[ -n "${DEPLOY_CALLS_FILE:-}" ]] && printf "%s\\n" "$tag" >> "$DEPLOY_CALLS_FILE"\n'
        'mkdir -p "${STATE_DIR:?}"\n'
        'printf "%s\\n" "$tag"  > "$STATE_DIR/active_tag"\n'
        'printf "blue\\n"        > "$STATE_DIR/active_color"\n'
        '[[ "${STUB_DEPLOY_FAIL_TAG:-}" == "$tag" ]] && exit 1\n'
        "exit 0\n",
        encoding="utf-8",
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    return bindir


def _run(
    *,
    bindir: Path,
    repo: Path,
    state_dir: Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SYNC_REPO"] = str(repo)
    env["SYNC_REMOTE"] = "origin"
    env["SYNC_BRANCH"] = "develop"
    env["STAGING_DEPLOY_SH"] = str(bindir / "staging_deploy.sh")
    env["OMNISIGHT_STAGING_STATE_DIR"] = str(state_dir)
    env["STATE_DIR"] = str(state_dir)  # consumed by the stub deployer
    env["OMNISIGHT_STAGING_URL"] = "https://staging.invalid"
    env["DEPLOY_CALLS_FILE"] = str(state_dir / "deploy_calls.txt")
    # Never let the real release-audit DSN leak in from the host env.
    env.pop("OMNISIGHT_DATABASE_URL", None)
    env.pop("OMNISIGHT_STAGING_ALERT_WEBHOOK", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=timeout
    )


def _deploy_calls(state_dir: Path) -> list[str]:
    f = state_dir / "deploy_calls.txt"
    return f.read_text(encoding="utf-8").split() if f.exists() else []


# ─────────────────────────── script behaviour ──────────────────────────────


def test_idempotent_noop_when_already_on_develop_tip(tmp_path: Path, stub_bin: Path) -> None:
    work, _remote = _repo_with_remote(tmp_path)
    tip = _git(["rev-parse", "origin/develop"], work)
    state = tmp_path / "state"
    state.mkdir()
    (state / "active_tag").write_text(tip + "\n", encoding="utf-8")
    (state / "active_color").write_text("blue\n", encoding="utf-8")

    res = _run(bindir=stub_bin, repo=work, state_dir=state)
    assert res.returncode == 0, res.stderr
    assert "idempotent no-op" in res.stderr
    assert _deploy_calls(state) == []  # deployer never invoked


def test_fresh_tip_deploys_and_migrates(tmp_path: Path, stub_bin: Path) -> None:
    work, remote = _repo_with_remote(tmp_path)
    tip = _commit(remote, "advance develop")  # the new develop tip the script must fetch
    state = tmp_path / "state"
    state.mkdir()
    (state / "active_tag").write_text("oldoldoldoldoldoldoldoldoldoldoldold0000\n", encoding="utf-8")
    (state / "active_color").write_text("blue\n", encoding="utf-8")

    res = _run(bindir=stub_bin, repo=work, state_dir=state)
    assert res.returncode == 0, res.stderr
    assert _deploy_calls(state) == [tip]
    assert f"staging synced to develop tip {tip}" in res.stderr
    # No release-audit DSN in the test env -> the row is skipped (logged).
    assert "release_audit row skipped" in res.stderr


def test_develop_tip_fetch_failure_is_transient(tmp_path: Path, stub_bin: Path) -> None:
    work, _remote = _repo_with_remote(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    # SYNC_REMOTE points at a name that does not exist -> `git fetch` fails.
    res = _run(bindir=stub_bin, repo=work, state_dir=state, extra_env={"SYNC_REMOTE": "nope"})
    assert res.returncode == 0, res.stderr
    assert "DevelopTipFetchFailed" in res.stderr
    assert "leaving staging untouched" in res.stderr
    assert _deploy_calls(state) == []


def test_explicit_tip_override_bypasses_git(tmp_path: Path, stub_bin: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = tmp_path / "state"
    state.mkdir()
    forced = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    res = _run(bindir=stub_bin, repo=repo, state_dir=state, extra_env={"SYNC_DEVELOP_TIP": forced})
    assert res.returncode == 0, res.stderr
    assert _deploy_calls(state) == [forced]


def test_staging_deploy_failure_exits_nonzero(tmp_path: Path, stub_bin: Path) -> None:
    repo = tmp_path / "repo"
    tip = _init_repo(repo)
    state = tmp_path / "state"
    state.mkdir()
    res = _run(
        bindir=stub_bin, repo=repo, state_dir=state,
        extra_env={"SYNC_DEVELOP_TIP": tip, "STUB_DEPLOY_FAIL_TAG": tip},
    )
    assert res.returncode == 1
    assert "StagingDeployFailed" in res.stderr
    assert _deploy_calls(state) == [tip]  # the failed deploy; no rollback (deployer owns its own revert)


def test_migration_failure_rolls_back_to_previous_tag(tmp_path: Path, stub_bin: Path) -> None:
    repo = tmp_path / "repo"
    tip = _init_repo(repo)
    state = tmp_path / "state"
    state.mkdir()
    prev = "1111111111111111111111111111111111111111"
    (state / "active_tag").write_text(prev + "\n", encoding="utf-8")
    (state / "active_color").write_text("blue\n", encoding="utf-8")
    res = _run(
        bindir=stub_bin, repo=repo, state_dir=state,
        extra_env={"SYNC_DEVELOP_TIP": tip, "STUB_DOCKER_RC": "1"},
    )
    assert res.returncode == 1
    assert "StagingMigrationFailed" in res.stderr
    # deployer invoked twice: the develop tip, then the rollback to prev.
    assert _deploy_calls(state) == [tip, prev]


def test_healthz_failure_rolls_back_to_previous_tag(tmp_path: Path, stub_bin: Path) -> None:
    repo = tmp_path / "repo"
    tip = _init_repo(repo)
    state = tmp_path / "state"
    state.mkdir()
    prev = "2222222222222222222222222222222222222222"
    (state / "active_tag").write_text(prev + "\n", encoding="utf-8")
    (state / "active_color").write_text("blue\n", encoding="utf-8")
    res = _run(
        bindir=stub_bin, repo=repo, state_dir=state,
        extra_env={"SYNC_DEVELOP_TIP": tip, "STUB_CURL_RC": "22"},
    )
    assert res.returncode == 1
    assert "StagingHealthzFailed" in res.stderr
    assert _deploy_calls(state) == [tip, prev]


def test_missing_deployer_is_prereq_failure(tmp_path: Path, stub_bin: Path) -> None:
    repo = tmp_path / "repo"
    tip = _init_repo(repo)
    state = tmp_path / "state"
    state.mkdir()
    res = _run(
        bindir=stub_bin, repo=repo, state_dir=state,
        extra_env={"SYNC_DEVELOP_TIP": tip, "STAGING_DEPLOY_SH": str(tmp_path / "nope.sh")},
    )
    assert res.returncode == 3
    assert "staging deployer not found" in res.stderr


def test_skip_migrate_flag(tmp_path: Path, stub_bin: Path) -> None:
    repo = tmp_path / "repo"
    tip = _init_repo(repo)
    state = tmp_path / "state"
    state.mkdir()
    # docker would fail, but SYNC_SKIP_MIGRATE=1 means it is never called.
    res = _run(
        bindir=stub_bin, repo=repo, state_dir=state,
        extra_env={"SYNC_DEVELOP_TIP": tip, "STUB_DOCKER_RC": "1", "SYNC_SKIP_MIGRATE": "1"},
    )
    assert res.returncode == 0, res.stderr
    assert "skipping alembic upgrade" in res.stderr
    assert _deploy_calls(state) == [tip]


# ─────────────────────────── shell shape ───────────────────────────────────


def test_script_is_executable_strict_bash() -> None:
    src = SCRIPT.read_text(encoding="utf-8")
    assert src.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in src
    assert os.access(SCRIPT, os.X_OK), "scripts/sync_staging_to_develop.sh must be executable"
    # syntax-clean
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)


# ─────────────────────────── systemd contract ──────────────────────────────


def _unit(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def _on_boot_seconds(timer_text: str) -> int:
    line = next(l for l in timer_text.splitlines() if l.strip().startswith("OnBootSec="))
    value = line.split("=", 1)[1].strip()
    m = re.fullmatch(r"(\d+)(min|s|sec)?", value)
    assert m, f"unparseable OnBootSec={value!r}"
    n = int(m.group(1))
    return n * 60 if m.group(2) == "min" else n


def test_staging_sync_service_shape() -> None:
    svc = _unit("staging-sync.service")
    assert "Type=oneshot" in svc
    assert "scripts/sync_staging_to_develop.sh" in svc
    assert "OnFailure=staging-gate-alert.service" in svc
    # release-audit DSN carried in via the (optional) EnvironmentFile, like
    # the OP-965 gate units / OP-972 snapshot unit.
    assert "EnvironmentFile=-/home/user/.config/omnisight/release-audit.env" in svc
    assert "STAGING_DEPLOY_SH=" in svc


def test_staging_sync_timer_cadence() -> None:
    tim = _unit("staging-sync.timer")
    assert "Unit=staging-sync.service" in tim
    assert "OnUnitActiveSec=10min" in tim   # matches the OP-965 canary cadence
    assert "Persistent=true" in tim


def test_boot_order_sync_then_canary_then_smoke() -> None:
    # AC #6 — the deploy timer must fire before the probe timers at boot.
    sync = _on_boot_seconds(_unit("staging-sync.timer"))
    canary = _on_boot_seconds(_unit("staging-gate-canary.timer"))
    smoke = _on_boot_seconds(_unit("staging-gate-smoke.timer"))
    assert sync < canary < smoke, (sync, canary, smoke)
