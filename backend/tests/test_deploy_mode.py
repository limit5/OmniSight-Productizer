"""L7 — tests for :mod:`backend.deploy_mode`.

Covers the full decision table of
:func:`backend.deploy_mode.detect_deploy_mode` plus every individual
signal probe. The probes are driven entirely off module-level path
constants and ``shutil.which`` — the tests monkey-patch those so the
outcome is deterministic regardless of whether the host running
pytest happens to have systemd / docker / a docker socket.
"""

from __future__ import annotations

from pathlib import Path


from backend import deploy_mode as _dm


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _stub_host(
    monkeypatch,
    tmp_path: Path,
    *,
    in_docker_file: bool = False,
    cgroup_token: str | None = None,
    systemd_run_dir: bool = False,
    docker_socket: bool = False,
    which_map: dict[str, str] | None = None,
) -> None:
    """Install a deterministic faux-host for :mod:`backend.deploy_mode`.

    Every filesystem probe points inside *tmp_path* so nothing from the
    real host leaks into the test. ``which_map`` controls the
    ``shutil.which`` lookups — absent keys return ``None``.
    """
    dockerenv = tmp_path / "dockerenv"
    cgroup = tmp_path / "cgroup"
    systemd = tmp_path / "run-systemd-system"
    sock = tmp_path / "docker.sock"

    if in_docker_file:
        dockerenv.write_text("")
    if cgroup_token is not None:
        cgroup.write_text(
            f"12:cpuset:/\n11:memory:/kubepods.slice/{cgroup_token}/pod-abc\n"
        )
    if systemd_run_dir:
        systemd.mkdir()
    if docker_socket:
        import socket as _s

        s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        s.bind(str(sock))
        # Socket stays around until the tmp_path is cleaned up by pytest.

    monkeypatch.setattr(_dm, "_DOCKERENV_MARKER", dockerenv)
    monkeypatch.setattr(_dm, "_CGROUP_PATH", cgroup)
    monkeypatch.setattr(_dm, "_SYSTEMD_RUN_DIR", systemd)
    monkeypatch.setattr(_dm, "_DOCKER_SOCKET", sock)

    lookup = which_map or {}
    monkeypatch.setattr(_dm.shutil, "which", lambda name: lookup.get(name))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Probes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_is_in_docker_detects_dockerenv_marker(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path, in_docker_file=True)
    detected, evidence = _dm._is_in_docker()
    assert detected is True
    assert "dockerenv" in evidence


def test_is_in_docker_detects_cgroup_docker_token(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path, cgroup_token="docker")
    detected, evidence = _dm._is_in_docker()
    assert detected is True
    assert "docker" in evidence


def test_is_in_docker_detects_cgroup_containerd_token(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path, cgroup_token="containerd")
    detected, evidence = _dm._is_in_docker()
    assert detected is True
    assert "containerd" in evidence


def test_is_in_docker_false_on_clean_host(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path)
    detected, evidence = _dm._is_in_docker()
    assert detected is False
    assert "no docker" in evidence


def test_has_systemd_detects_run_dir_plus_binary(monkeypatch, tmp_path):
    _stub_host(
        monkeypatch,
        tmp_path,
        systemd_run_dir=True,
        which_map={"systemctl": "/usr/bin/systemctl"},
    )
    detected, evidence = _dm._has_systemd()
    assert detected is True
    assert "systemctl" in evidence


def test_has_systemd_detects_run_dir_only(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path, systemd_run_dir=True)
    detected, evidence = _dm._has_systemd()
    assert detected is True
    assert "systemctl missing" in evidence


def test_has_systemd_detects_binary_only(monkeypatch, tmp_path):
    _stub_host(
        monkeypatch, tmp_path,
        which_map={"systemctl": "/usr/bin/systemctl"},
    )
    detected, evidence = _dm._has_systemd()
    assert detected is True
    assert "PATH" in evidence


def test_has_systemd_false_on_clean_host(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path)
    detected, evidence = _dm._has_systemd()
    assert detected is False
    assert "no systemd" in evidence


def test_has_docker_socket_detects_unix_socket(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path, docker_socket=True)
    detected, evidence = _dm._has_docker_socket()
    assert detected is True
    assert "socket" in evidence


def test_has_docker_socket_false_on_clean_host(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path)
    detected, evidence = _dm._has_docker_socket()
    assert detected is False
    assert "no docker socket" in evidence


def test_has_docker_socket_rejects_regular_file(monkeypatch, tmp_path):
    """A regular file at the socket path should not be mistaken for a daemon."""
    _stub_host(monkeypatch, tmp_path)
    # Overwrite the stubbed path with a regular file.
    fake_path = tmp_path / "fake-sock"
    fake_path.write_text("not-a-socket")
    monkeypatch.setattr(_dm, "_DOCKER_SOCKET", fake_path)
    detected, _ = _dm._has_docker_socket()
    assert detected is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  detect_deploy_mode — decision table
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_env_override_pins_mode(monkeypatch, tmp_path):
    """``OMNISIGHT_DEPLOY_MODE`` wins over auto-detect."""
    _stub_host(monkeypatch, tmp_path, systemd_run_dir=True,
               which_map={"systemctl": "/usr/bin/systemctl"})
    monkeypatch.setenv("OMNISIGHT_DEPLOY_MODE", "docker-compose")
    result = _dm.detect_deploy_mode()
    assert result.mode == "docker-compose"
    assert result.override_source == "OMNISIGHT_DEPLOY_MODE"
    assert "docker-compose" in result.reason


def test_env_override_accepts_dev(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path, systemd_run_dir=True,
               which_map={"systemctl": "/usr/bin/systemctl"})
    monkeypatch.setenv("OMNISIGHT_DEPLOY_MODE", "dev")
    assert _dm.detect_deploy_mode().mode == "dev"


def test_env_override_ignores_unknown_value(monkeypatch, tmp_path, caplog):
    """Typo in env var → warn and fall through to auto-detect."""
    _stub_host(
        monkeypatch, tmp_path,
        systemd_run_dir=True,
        which_map={"systemctl": "/usr/bin/systemctl"},
    )
    monkeypatch.setenv("OMNISIGHT_DEPLOY_MODE", "kubernetes")
    import logging
    with caplog.at_level(logging.WARNING, logger="backend.deploy_mode"):
        result = _dm.detect_deploy_mode()
    assert result.mode == "systemd"
    assert result.override_source is None
    assert any("kubernetes" in rec.message for rec in caplog.records)


def test_in_docker_with_socket_picks_compose(monkeypatch, tmp_path):
    """Nested docker w/ mounted socket → compose-in-docker."""
    _stub_host(
        monkeypatch, tmp_path,
        in_docker_file=True, docker_socket=True,
        which_map={"docker": "/usr/bin/docker"},
    )
    monkeypatch.delenv("OMNISIGHT_DEPLOY_MODE", raising=False)
    result = _dm.detect_deploy_mode()
    assert result.mode == "docker-compose"
    assert result.in_docker is True
    assert result.has_docker_socket is True
    assert "compose-in-docker" in result.reason


def test_in_docker_without_socket_picks_dev(monkeypatch, tmp_path):
    """Inside a container with no host-socket → dev no-op."""
    _stub_host(monkeypatch, tmp_path, in_docker_file=True)
    monkeypatch.delenv("OMNISIGHT_DEPLOY_MODE", raising=False)
    result = _dm.detect_deploy_mode()
    assert result.mode == "dev"
    assert result.in_docker is True
    assert result.has_docker_socket is False
    assert "already up" in result.reason


def test_systemd_host_picks_systemd(monkeypatch, tmp_path):
    _stub_host(
        monkeypatch, tmp_path,
        systemd_run_dir=True,
        which_map={"systemctl": "/usr/bin/systemctl"},
    )
    monkeypatch.delenv("OMNISIGHT_DEPLOY_MODE", raising=False)
    result = _dm.detect_deploy_mode()
    assert result.mode == "systemd"
    assert result.has_systemd is True


def test_docker_only_host_picks_compose(monkeypatch, tmp_path):
    _stub_host(
        monkeypatch, tmp_path,
        docker_socket=True,
        which_map={"docker": "/usr/bin/docker"},
    )
    monkeypatch.delenv("OMNISIGHT_DEPLOY_MODE", raising=False)
    result = _dm.detect_deploy_mode()
    assert result.mode == "docker-compose"
    assert result.has_docker_socket is True
    assert result.has_docker_binary is True


def test_docker_binary_only_still_picks_compose(monkeypatch, tmp_path):
    """Remote docker context — no local socket but docker CLI present."""
    _stub_host(
        monkeypatch, tmp_path,
        which_map={"docker": "/usr/bin/docker"},
    )
    monkeypatch.delenv("OMNISIGHT_DEPLOY_MODE", raising=False)
    result = _dm.detect_deploy_mode()
    assert result.mode == "docker-compose"
    assert result.has_docker_binary is True
    assert result.has_docker_socket is False


def test_clean_host_falls_back_to_dev(monkeypatch, tmp_path):
    _stub_host(monkeypatch, tmp_path)
    monkeypatch.delenv("OMNISIGHT_DEPLOY_MODE", raising=False)
    result = _dm.detect_deploy_mode()
    assert result.mode == "dev"
    assert result.in_docker is False
    assert result.has_systemd is False
    assert result.has_docker_socket is False


def test_to_dict_round_trip(monkeypatch, tmp_path):
    """``DeployModeDetection.to_dict`` returns all signal fields."""
    _stub_host(
        monkeypatch, tmp_path,
        systemd_run_dir=True,
        which_map={"systemctl": "/usr/bin/systemctl"},
    )
    monkeypatch.delenv("OMNISIGHT_DEPLOY_MODE", raising=False)
    payload = _dm.detect_deploy_mode().to_dict()
    assert set(payload) >= {
        "mode", "in_docker", "has_systemd", "has_docker_socket",
        "has_docker_binary", "has_systemctl_binary", "override_source",
        "reason", "signals",
    }
    assert payload["signals"].get("systemd")
    assert payload["signals"].get("docker")


def test_systemd_preferred_over_docker_when_both_present(monkeypatch, tmp_path):
    """Regression: a bare-metal host with both signals must pick systemd
    so Step 4 exec's ``systemctl start`` (the intended path) instead of
    accidentally double-provisioning via docker compose."""
    _stub_host(
        monkeypatch, tmp_path,
        systemd_run_dir=True, docker_socket=True,
        which_map={
            "systemctl": "/usr/bin/systemctl",
            "docker": "/usr/bin/docker",
        },
    )
    monkeypatch.delenv("OMNISIGHT_DEPLOY_MODE", raising=False)
    assert _dm.detect_deploy_mode().mode == "systemd"
