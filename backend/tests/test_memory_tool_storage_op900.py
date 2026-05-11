"""OP-900 F2 -- Memory Tool storage provisioning + fleet caps."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from backend.agents.memory_tool_handler import (
    ERR_DIR_NOT_WRITABLE,
    ERR_STORAGE_FULL,
    MemoryToolConfig,
    MemoryToolHandler,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _audit_rows(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_provision_memory_tool_storage_idempotent(tmp_path: Path) -> None:
    """AC #1/#2: provisioning creates root + fleet dirs at mode 0750."""
    root = tmp_path / "memory"
    env = {
        **os.environ,
        "OMNISIGHT_MEMORY_TOOL_ROOT": str(root),
        "OMNISIGHT_BACKEND_SERVICE_USER": os.environ.get("USER", "nobody"),
        "OMNISIGHT_BACKEND_SERVICE_GROUP": subprocess.check_output(
            ["id", "-gn"], text=True
        ).strip(),
    }
    script = REPO_ROOT / "scripts" / "provision_memory_tool_storage.sh"

    for _ in range(2):
        subprocess.run([str(script)], cwd=REPO_ROOT, env=env, check=True)

    assert _mode(root) == 0o750
    for fleet in ("claude", "codex", "merger"):
        path = root / fleet
        assert path.is_dir()
        assert _mode(path) == 0o750
        assert path.stat().st_uid == os.getuid()


def test_cap_from_env_and_eviction_audit_jsonl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC #3/#4 and error catalog: env cap triggers eviction + JSONL audit."""
    progress = tmp_path / "progress.txt"
    monkeypatch.setenv("OMNISIGHT_MEMORY_TOOL_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("OMNISIGHT_MEMORY_TOOL_CAP_MB", "1")

    handler = MemoryToolHandler(MemoryToolConfig.from_env(
        fleet_id="codex",
        progress_path=progress,
        ticket_key="OP-900",
    ))
    assert handler.config.storage_root == tmp_path / "memory" / "codex"
    assert handler.config.cap_mb == 1

    chunk = "x" * (300 * 1024)
    for i in range(3):
        result = handler.handle({
            "command": "create",
            "path": f"/memories/file-{i}.md",
            "file_text": chunk,
        })
        assert result["ok"] is True
        path = handler.config.storage_root / f"file-{i}.md"
        os.utime(path, (1000.0 + i, 1000.0 + i))

    result = handler.handle({
        "command": "create",
        "path": "/memories/file-3.md",
        "file_text": chunk,
    })
    assert result["ok"] is True
    assert not (handler.config.storage_root / "file-0.md").exists()

    rows = _audit_rows(progress)
    evict_rows = [row for row in rows if row.get("op") == "evict"]
    assert evict_rows
    assert evict_rows[0]["type"] == "memory_tool"
    assert evict_rows[0]["tool"] == "memory"
    assert evict_rows[0]["key"] == "/memories/file-0.md"
    assert evict_rows[0]["ticket_key"] == "OP-900"
    assert any(row.get("op") == "MemoryCapExceeded" for row in rows)

    huge = "y" * (2 * 1024 * 1024)
    oversize = handler.handle({
        "command": "create",
        "path": "/memories/huge.md",
        "file_text": huge,
    })
    assert oversize["error"] == ERR_STORAGE_FULL


def test_memory_dir_not_writable_refuses_start(tmp_path: Path) -> None:
    """AC error catalog: unwritable fleet dir raises at handler startup."""
    root = tmp_path / "memory" / "codex"
    root.mkdir(parents=True)
    root.chmod(0o550)
    try:
        with pytest.raises(Exception) as excinfo:
            MemoryToolHandler(MemoryToolConfig(
                fleet_id="codex",
                storage_root=root,
            ))
        assert getattr(excinfo.value, "error_code", None) == ERR_DIR_NOT_WRITABLE
    finally:
        root.chmod(0o750)


def test_systemd_hourly_du_report_contract() -> None:
    """AC #5/#6: timer and service pin hourly du JSONL report path."""
    service = (
        REPO_ROOT / "deploy/systemd/memory-tool-storage-monitor.service"
    ).read_text(encoding="utf-8")
    timer = (
        REPO_ROOT / "deploy/systemd/memory-tool-storage-monitor.timer"
    ).read_text(encoding="utf-8")
    backend_service = (
        REPO_ROOT / "deploy/systemd/omnisight-backend.service"
    ).read_text(encoding="utf-8")
    runner_codex = (
        REPO_ROOT / "deploy/systemd/runner-codex@.service"
    ).read_text(encoding="utf-8")
    runner_claude = (
        REPO_ROOT / "deploy/systemd/runner-claude@.service"
    ).read_text(encoding="utf-8")

    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert "du -sh" in service
    assert "OMNISIGHT_MEMORY_TOOL_USAGE_REPORT=/var/omnisight/memory/usage.jsonl" in service
    assert '"op":"du"' in service
    assert "OMNISIGHT_MEMORY_TOOL_ROOT=/var/omnisight/memory" in backend_service
    assert "OMNISIGHT_MEMORY_TOOL_ROOT=/var/omnisight/memory" in runner_codex
    assert "OMNISIGHT_MEMORY_TOOL_ROOT=/var/omnisight/memory" in runner_claude
    assert "ReadWritePaths=/var/omnisight/memory" in backend_service
