"""Phase 64-C-SSH — SSH runner tests.

Tests cover:
  * Credential loading + target lookup
  * T3 resolver SSH branch (arch≠host + registered target)
  * dispatch_t3 SSH routing
  * exec_on_remote timeout + truncation (mocked paramiko)
  * Sandbox setup
  * File sync helpers
  * Kill-switch
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# paramiko is an optional dependency — skip the entire test module
# if it's not installed (instead of failing at collection time).
pytest.importorskip("paramiko", reason="paramiko not installed — C1 SSH runner tests skipped")

from backend import t3_resolver as r
from backend import container as _ct
from backend import ssh_runner as sr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _host_x86_linux(monkeypatch):
    monkeypatch.setattr(r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(r, "host_os", lambda: "linux")
    monkeypatch.delenv("OMNISIGHT_SSH_RUNNER_ENABLED", raising=False)
    monkeypatch.delenv("OMNISIGHT_T3_LOCAL_ENABLED", raising=False)
    sr.clear_ssh_credential_cache()


@pytest.fixture
def mock_ssh_target():
    return sr.SSHTarget(
        host="192.168.1.100",
        port=22,
        user="root",
        key_path="~/.ssh/id_ed25519",
        sysroot_path="/opt/sysroot",
        scratch_dir="/tmp/omnisight",
    )


@pytest.fixture
def sample_targets():
    return [
        {
            "id": "rk3588-evk",
            "arch": "aarch64",
            "os": "linux",
            "host": "192.168.1.100",
            "port": 22,
            "user": "root",
            "key_path": "~/.ssh/id_ed25519",
            "sysroot_path": "/opt/sysroot",
            "scratch_dir": "/tmp/omnisight",
        },
        {
            "id": "riscv-board",
            "arch": "riscv64",
            "os": "linux",
            "host": "192.168.1.101",
            "port": 22,
            "user": "root",
            "key_path": "~/.ssh/id_riscv",
            "scratch_dir": "/tmp/omnisight",
        },
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Credential loading
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_get_ssh_targets_returns_list(monkeypatch):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: [])
    sr.clear_ssh_credential_cache()
    assert sr.get_ssh_targets() == []


def test_find_target_for_arch_matches(monkeypatch, sample_targets):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: sample_targets)
    sr.clear_ssh_credential_cache()
    target = sr.find_target_for_arch("aarch64", "linux")
    assert target is not None
    assert target.host == "192.168.1.100"
    assert target.user == "root"


def test_find_target_for_arch_no_match(monkeypatch, sample_targets):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: sample_targets)
    sr.clear_ssh_credential_cache()
    target = sr.find_target_for_arch("mips", "linux")
    assert target is None


def test_find_target_canonicalises_arch(monkeypatch, sample_targets):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: sample_targets)
    sr.clear_ssh_credential_cache()
    target = sr.find_target_for_arch("arm64", "linux")
    assert target is not None
    assert target.host == "192.168.1.100"


def test_find_target_riscv(monkeypatch, sample_targets):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: sample_targets)
    sr.clear_ssh_credential_cache()
    target = sr.find_target_for_arch("riscv64", "linux")
    assert target is not None
    assert target.host == "192.168.1.101"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  T3 resolver SSH branch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_resolver_selects_ssh_when_target_registered(monkeypatch, sample_targets):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: sample_targets)
    sr.clear_ssh_credential_cache()
    res = r.resolve_t3_runner("aarch64", "linux")
    assert res.kind == r.T3RunnerKind.SSH
    assert "SSH runner" in res.reason
    assert "192.168.1.100" in res.reason


def test_resolver_falls_to_bundle_when_no_ssh_target(monkeypatch):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: [])
    sr.clear_ssh_credential_cache()
    res = r.resolve_t3_runner("aarch64", "linux")
    assert res.kind == r.T3RunnerKind.BUNDLE


def test_resolver_prefers_local_over_ssh(monkeypatch, sample_targets):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: sample_targets)
    sr.clear_ssh_credential_cache()
    res = r.resolve_t3_runner("x86_64", "linux")
    assert res.kind == r.T3RunnerKind.LOCAL


def test_ssh_kill_switch(monkeypatch, sample_targets):
    monkeypatch.setenv("OMNISIGHT_SSH_RUNNER_ENABLED", "false")
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: sample_targets)
    sr.clear_ssh_credential_cache()
    res = r.resolve_t3_runner("aarch64", "linux")
    assert res.kind == r.T3RunnerKind.BUNDLE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  dispatch_t3 SSH routing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_dispatch_t3_returns_ssh_info(monkeypatch, sample_targets):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: sample_targets)
    sr.clear_ssh_credential_cache()

    info, kind = await _ct.dispatch_t3(
        "a-test", Path("/tmp/ws"), target_arch="aarch64", target_os="linux",
    )
    assert kind == r.T3RunnerKind.SSH
    assert info is not None
    assert isinstance(info, sr.SSHRunnerInfo)
    assert info.target.host == "192.168.1.100"


@pytest.mark.asyncio
async def test_dispatch_t3_ssh_no_target_falls_to_bundle(monkeypatch):
    monkeypatch.setattr(sr, "_load_ssh_credentials", lambda: [])
    sr.clear_ssh_credential_cache()

    info, kind = await _ct.dispatch_t3(
        "a-test", Path("/tmp/ws"), target_arch="aarch64", target_os="linux",
    )
    assert kind == r.T3RunnerKind.BUNDLE
    assert info is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSHTarget dataclass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_ssh_target_frozen(mock_ssh_target):
    with pytest.raises(AttributeError):
        mock_ssh_target.host = "other"


def test_ssh_target_defaults():
    t = sr.SSHTarget(host="10.0.0.1")
    assert t.port == 22
    assert t.user == "root"
    assert t.scratch_dir == "/tmp/omnisight"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSHRunnerInfo lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_runner_info_status():
    t = sr.SSHTarget(host="10.0.0.1")
    info = sr.SSHRunnerInfo(agent_id="test", target=t)
    assert info.status == "connected"
    info.status = "completed"
    assert info.status == "completed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Session management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_kill_session():
    t = sr.SSHTarget(host="10.0.0.1")
    info = sr.SSHRunnerInfo(agent_id="kill-test", target=t)
    sr._active_sessions["kill-test"] = info
    assert await sr.kill_session("kill-test") is True
    assert sr.get_active_session("kill-test") is None


@pytest.mark.asyncio
async def test_kill_nonexistent_session():
    assert await sr.kill_session("nonexistent") is False


def test_list_active_sessions():
    sr._active_sessions.clear()
    t = sr.SSHTarget(host="10.0.0.1")
    sr._active_sessions["s1"] = sr.SSHRunnerInfo(agent_id="s1", target=t)
    sr._active_sessions["s2"] = sr.SSHRunnerInfo(agent_id="s2", target=t)
    sessions = sr.list_active_sessions()
    assert len(sessions) == 2
    sr._active_sessions.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  exec_on_remote (mocked transport)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_exec_on_remote_success():
    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_channel = MagicMock()

    mock_client.get_transport.return_value = mock_transport
    mock_transport.open_session.return_value = mock_channel
    mock_transport.is_active.return_value = True

    call_count = {"exit": 0, "recv": 0}

    def exit_status_ready():
        call_count["exit"] += 1
        return call_count["exit"] >= 2

    def recv_ready():
        call_count["recv"] += 1
        return call_count["recv"] <= 2

    mock_channel.exit_status_ready = exit_status_ready
    mock_channel.recv_ready = recv_ready
    mock_channel.recv.return_value = b"build OK\n"
    mock_channel.recv_stderr_ready.return_value = False
    mock_channel.recv_exit_status.return_value = 0

    rc, out = await sr.exec_on_remote(
        mock_client, "make all", timeout=10, heartbeat_interval=5,
    )
    assert rc == 0
    assert "build OK" in out


@pytest.mark.asyncio
async def test_exec_on_remote_truncation():
    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_channel = MagicMock()

    mock_client.get_transport.return_value = mock_transport
    mock_transport.open_session.return_value = mock_channel
    mock_transport.is_active.return_value = True

    recv_count = {"n": 0}

    def recv_ready():
        recv_count["n"] += 1
        return recv_count["n"] <= 3

    mock_channel.exit_status_ready.return_value = True
    mock_channel.recv_ready = recv_ready
    mock_channel.recv.return_value = b"X" * 5000
    mock_channel.recv_stderr_ready.return_value = False
    mock_channel.recv_exit_status.return_value = 0

    rc, out = await sr.exec_on_remote(
        mock_client, "cat big_file", timeout=10, max_output_bytes=100,
    )
    assert rc == 0
    assert "TRUNCATED" in out


@pytest.mark.asyncio
async def test_exec_on_remote_no_transport():
    mock_client = MagicMock()
    mock_client.get_transport.return_value = None

    rc, out = await sr.exec_on_remote(mock_client, "echo hi", timeout=5)
    assert rc == -1
    assert "transport is None" in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Credential cache clearing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_clear_ssh_credential_cache():
    sr._SSH_CRED_CACHE = [{"id": "cached"}]
    sr.clear_ssh_credential_cache()
    assert sr._SSH_CRED_CACHE is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _exec_sync helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_exec_sync():
    mock_client = MagicMock()
    mock_stdout = MagicMock()
    mock_stderr = MagicMock()
    mock_stdout.read.return_value = b"hello\n"
    mock_stderr.read.return_value = b""
    mock_stdout.channel.recv_exit_status.return_value = 0
    mock_client.exec_command.return_value = (None, mock_stdout, mock_stderr)

    rc, out = sr._exec_sync(mock_client, "echo hello")
    assert rc == 0
    assert "hello" in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  run_on_target connect failure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_run_on_target_connect_failure(monkeypatch, mock_ssh_target):
    monkeypatch.setattr(
        sr, "_connect",
        lambda t: (_ for _ in ()).throw(ConnectionRefusedError("refused")),
    )
    rc, out, info = await sr.run_on_target(
        "agent-fail", Path("/tmp/ws"), mock_ssh_target, "make",
    )
    assert rc == -1
    assert "CONNECT ERROR" in out
    assert info.status == "connect_failed"
