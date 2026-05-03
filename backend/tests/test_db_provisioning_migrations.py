"""FS.1.2 — Tenant DB migration runner tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.db_provisioning import (
    DBMigrationCommandError,
    UnsupportedDBMigrationToolError,
    build_migration_command,
    run_tenant_migrations,
)


class _RunRecorder:
    def __init__(self, returncode: int = 0, stdout: str = "ok", stderr: str = ""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class TestBuildMigrationCommand:

    def test_prisma_command_uses_schema_selector(self):
        assert build_migration_command("prisma", schema_path="prisma/schema.prisma") == [
            "npx",
            "prisma",
            "migrate",
            "deploy",
            "--schema",
            "prisma/schema.prisma",
        ]

    def test_drizzle_command_uses_config_selector(self):
        assert build_migration_command("drizzle", schema_path="drizzle.config.ts") == [
            "npx",
            "drizzle-kit",
            "migrate",
            "--config",
            "drizzle.config.ts",
        ]

    def test_sqlalchemy_command_uses_alembic_upgrade(self):
        assert build_migration_command(
            "sqlalchemy",
            schema_path="backend/alembic.ini",
            revision="head",
        ) == ["alembic", "-c", "backend/alembic.ini", "upgrade", "head"]

    def test_rejects_unknown_tool(self):
        with pytest.raises(UnsupportedDBMigrationToolError):
            build_migration_command("flyway")


class TestRunTenantMigrations:

    def test_runs_prisma_with_connection_url_only_in_env(self, monkeypatch, tmp_path):
        rec = _RunRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        url = "postgresql://user:secret@example/tenant"

        result = run_tenant_migrations(
            "prisma",
            connection_url=url,
            cwd=tmp_path,
            schema_path="prisma/schema.prisma",
            extra_env={"SHADOW_DATABASE_URL": "postgresql://shadow"},
        )

        assert result.ok is True
        assert result.command == [
            "npx",
            "prisma",
            "migrate",
            "deploy",
            "--schema",
            "prisma/schema.prisma",
        ]
        assert url not in " ".join(result.command)
        argv, kwargs = rec.calls[0]
        assert argv == result.command
        assert kwargs["cwd"] == Path(tmp_path)
        assert kwargs["env"]["DATABASE_URL"] == url
        assert kwargs["env"]["SQLALCHEMY_URL"] == url
        assert kwargs["env"]["SHADOW_DATABASE_URL"] == "postgresql://shadow"
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 120
        assert result.env_vars == [
            "DATABASE_URL",
            "SHADOW_DATABASE_URL",
            "SQLALCHEMY_URL",
        ]

    def test_runs_sqlalchemy_alias_with_custom_revision(self, monkeypatch, tmp_path):
        rec = _RunRecorder(stdout="upgraded")
        monkeypatch.setattr(subprocess, "run", rec)

        result = run_tenant_migrations(
            "alembic",
            connection_url="postgresql://user:pass@example/tenant",
            cwd=tmp_path,
            schema_path="alembic.ini",
            revision="0058",
            timeout=30,
        )

        assert result.tool == "sqlalchemy"
        assert result.stdout == "upgraded"
        assert rec.calls[0][0] == ["alembic", "-c", "alembic.ini", "upgrade", "0058"]
        assert rec.calls[0][1]["timeout"] == 30

    def test_custom_command_still_receives_database_env(self, monkeypatch, tmp_path):
        rec = _RunRecorder()
        monkeypatch.setattr(subprocess, "run", rec)

        result = run_tenant_migrations(
            "drizzle",
            connection_url="mysql://user:pass@example/tenant",
            cwd=tmp_path,
            command=["pnpm", "db:migrate"],
        )

        assert result.command == ["pnpm", "db:migrate"]
        assert rec.calls[0][1]["env"]["DATABASE_URL"].startswith("mysql://")

    def test_nonzero_exit_raises_with_result(self, monkeypatch, tmp_path):
        rec = _RunRecorder(returncode=1, stderr="migration failed")
        monkeypatch.setattr(subprocess, "run", rec)

        with pytest.raises(DBMigrationCommandError) as excinfo:
            run_tenant_migrations(
                "drizzle",
                connection_url="postgresql://user:pass@example/tenant",
                cwd=tmp_path,
            )

        assert excinfo.value.result is not None
        assert excinfo.value.result.returncode == 1
        assert excinfo.value.result.stderr == "migration failed"

    def test_timeout_raises_with_124_result(self, monkeypatch, tmp_path):
        def _timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

        monkeypatch.setattr(subprocess, "run", _timeout)

        with pytest.raises(DBMigrationCommandError) as excinfo:
            run_tenant_migrations(
                "sqlalchemy",
                connection_url="postgresql://user:pass@example/tenant",
                cwd=tmp_path,
                timeout=5,
            )

        assert excinfo.value.result is not None
        assert excinfo.value.result.returncode == 124
        assert excinfo.value.result.stderr == "timeout after 5s"

    def test_connection_url_is_required(self, tmp_path):
        with pytest.raises(ValueError):
            run_tenant_migrations("prisma", connection_url="", cwd=tmp_path)
