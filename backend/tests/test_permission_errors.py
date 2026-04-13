"""Tests for Permission & Environment Auto-Fix (Phase 44).

Covers:
- Permission error classification (all 9 categories)
- Auto-fix actions
- Preventive environment checks
- Integration with error_check_node
"""

from __future__ import annotations

import pytest

from backend.permission_errors import (
    classify_permission_error,
    attempt_auto_fix,
    check_environment,
    PermissionErrorCategory,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Classification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClassification:

    def test_file_readonly(self):
        r = classify_permission_error("Permission denied: write to /workspace/file.c")
        assert r is not None
        assert r["category"] == PermissionErrorCategory.FILE_READONLY
        assert r["auto_fixable"] is True

    def test_dir_not_writable(self):
        r = classify_permission_error("Permission denied: mkdir /workspace/build")
        assert r is not None
        assert r["category"] == PermissionErrorCategory.DIR_NOT_WRITABLE

    def test_ssh_key_permission(self):
        r = classify_permission_error("Permissions 0644 for '/home/user/.ssh/id_ed25519' are too open")
        assert r is not None
        assert r["category"] == PermissionErrorCategory.SSH_KEY_PERMISSION
        assert r["auto_fixable"] is True

    def test_disk_full(self):
        r = classify_permission_error("No space left on device")
        assert r is not None
        assert r["category"] == PermissionErrorCategory.DISK_FULL

    def test_docker_socket(self):
        r = classify_permission_error("permission denied while trying to connect to docker.sock")
        assert r is not None
        assert r["category"] == PermissionErrorCategory.DOCKER_SOCKET
        assert r["auto_fixable"] is False

    def test_port_in_use(self):
        r = classify_permission_error("Error: address already in use :8000")
        assert r is not None
        assert r["category"] == PermissionErrorCategory.PORT_IN_USE

    def test_command_not_found(self):
        r = classify_permission_error("bash: aarch64-linux-gnu-gcc: command not found")
        assert r is not None
        assert r["category"] == PermissionErrorCategory.COMMAND_NOT_FOUND
        assert r["auto_fixable"] is False

    def test_npm_eacces(self):
        r = classify_permission_error("npm ERR! EACCES: permission denied, access '/usr/lib/node_modules'")
        assert r is not None
        assert r["category"] == PermissionErrorCategory.NPM_EACCES

    def test_git_lock(self):
        r = classify_permission_error("Unable to create '/workspace/.git/index.lock': File exists")
        assert r is not None
        assert r["category"] == PermissionErrorCategory.GIT_LOCK
        assert r["auto_fixable"] is True

    def test_normal_output_returns_none(self):
        r = classify_permission_error("[OK] Compilation successful, 0 warnings")
        assert r is None

    def test_empty_returns_none(self):
        assert classify_permission_error("") is None
        assert classify_permission_error(None) is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auto-Fix Actions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAutoFix:

    @pytest.mark.asyncio
    async def test_fix_file_readonly(self, tmp_path):
        # Create a read-only file
        f = tmp_path / "readonly.c"
        f.write_text("int main() {}")
        f.chmod(0o444)

        result = await attempt_auto_fix(
            PermissionErrorCategory.FILE_READONLY,
            f"Permission denied: '{f}'",
            str(tmp_path),
        )
        assert result["fixed"] is True
        assert "chmod" in result["action"]
        # File should now be writable
        import os
        assert os.access(f, os.W_OK)

    @pytest.mark.asyncio
    async def test_fix_ssh_key_permission(self, tmp_path):
        key = tmp_path / "id_test"
        key.write_text("fake key")
        key.chmod(0o644)

        result = await attempt_auto_fix(
            PermissionErrorCategory.SSH_KEY_PERMISSION,
            f"Permissions 0644 for '{key}' are too open",
            "",
        )
        assert result["fixed"] is True
        import stat
        mode = key.stat().st_mode & 0o777
        assert mode == 0o600

    @pytest.mark.asyncio
    async def test_fix_git_lock(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_text("locked")

        result = await attempt_auto_fix(
            PermissionErrorCategory.GIT_LOCK,
            "index.lock exists",
            str(tmp_path),
        )
        assert result["fixed"] is True
        assert not lock.exists()

    @pytest.mark.asyncio
    async def test_fix_port_in_use(self):
        result = await attempt_auto_fix(
            PermissionErrorCategory.PORT_IN_USE,
            "Error: address already in use :8000",
            "",
        )
        assert result["fixed"] is True
        assert "8001" in result["detail"] or "8001" in result["action"]

    @pytest.mark.asyncio
    async def test_docker_not_fixable(self):
        result = await attempt_auto_fix(
            PermissionErrorCategory.DOCKER_SOCKET,
            "permission denied docker.sock",
            "",
        )
        assert result["fixed"] is False

    @pytest.mark.asyncio
    async def test_command_not_found_not_fixable(self):
        result = await attempt_auto_fix(
            PermissionErrorCategory.COMMAND_NOT_FOUND,
            "command not found",
            "",
        )
        assert result["fixed"] is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Environment Checks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEnvironmentChecks:

    @pytest.mark.asyncio
    async def test_returns_list(self):
        issues = await check_environment("/tmp")
        assert isinstance(issues, list)

    @pytest.mark.asyncio
    async def test_each_issue_has_fields(self):
        issues = await check_environment("/tmp")
        for issue in issues:
            assert "check" in issue
            assert "status" in issue
            assert "detail" in issue
            assert "suggestion" in issue


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestErrorCheckIntegration:

    @pytest.mark.asyncio
    async def test_permission_error_detected_in_error_check(self):
        """error_check_node should detect permission errors in tool output."""
        from backend.agents.nodes import error_check_node
        from backend.agents.state import GraphState, ToolResult

        state = GraphState(
            tool_results=[
                ToolResult(
                    tool_name="run_bash",
                    output="[ERROR] Permission denied: write to /workspace/build/output.bin",
                    success=False,
                ),
            ],
            retry_count=0,
            max_retries=3,
        )
        result = await error_check_node(state)
        # Should detect as permission error — either auto-fixed or normal retry
        assert "last_error" in result or "retry_count" in result

    @pytest.mark.asyncio
    async def test_normal_error_not_classified_as_permission(self):
        """Regular tool errors should not trigger permission classifier."""
        from backend.agents.nodes import error_check_node
        from backend.agents.state import GraphState, ToolResult

        state = GraphState(
            tool_results=[
                ToolResult(
                    tool_name="run_bash",
                    output="[ERROR] Compilation failed: undefined reference to main",
                    success=False,
                ),
            ],
            retry_count=0,
            max_retries=3,
        )
        result = await error_check_node(state)
        assert result["retry_count"] == 1  # Normal retry, no auto-fix
