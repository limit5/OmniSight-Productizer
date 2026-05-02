"""FS.1.2 — Tenant database migration runner.

Provisioning adapters create the tenant-owned database; this module runs
the product schema into that freshly provisioned database. The runner is
intentionally subprocess-based so Prisma, Drizzle, and SQLAlchemy/Alembic
projects can keep using their native migration tools.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
This module defines immutable constants/classes/functions only. No
module-level cache, singleton, or mutable registry is read or written;
each invocation derives command/env from its explicit inputs.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence


class DBMigrationError(Exception):
    """Base for tenant DB migration runner errors."""

    def __init__(self, message: str, result: Optional["DBMigrationResult"] = None):
        super().__init__(message)
        self.result = result


class UnsupportedDBMigrationToolError(DBMigrationError):
    """Requested migration tool is not supported by FS.1.2."""


class DBMigrationCommandError(DBMigrationError):
    """Native migration command exited non-zero or timed out."""


@dataclass
class DBMigrationResult:
    """Outcome of a native tenant DB migration command."""

    tool: str
    command: list[str]
    cwd: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    elapsed_ms: int = 0
    env_vars: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "command": list(self.command),
            "cwd": self.cwd,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "elapsed_ms": self.elapsed_ms,
            "env_vars": list(self.env_vars),
            "ok": self.ok,
        }


def _normalize_tool(tool: str) -> str:
    key = tool.strip().lower().replace("_", "-")
    if key in ("sqlalchemy", "alembic"):
        return "sqlalchemy"
    if key == "prisma":
        return "prisma"
    if key == "drizzle":
        return "drizzle"
    raise UnsupportedDBMigrationToolError(
        "Unknown DB migration tool "
        f"'{tool}'. Expected one of: prisma, drizzle, sqlalchemy"
    )


def _schema_arg(path: Path) -> str:
    return str(path)


def build_migration_command(
    tool: str,
    *,
    schema_path: Optional[Path | str] = None,
    revision: str = "head",
) -> list[str]:
    """Build the native migration command for ``tool``.

    ``schema_path`` maps to each ecosystem's native selector:
    Prisma ``--schema``, Drizzle ``--config``, and Alembic ``-c``.
    """
    normalized = _normalize_tool(tool)
    path = Path(schema_path) if schema_path is not None else None
    if normalized == "prisma":
        cmd = ["npx", "prisma", "migrate", "deploy"]
        if path is not None:
            cmd.extend(["--schema", _schema_arg(path)])
        return cmd
    if normalized == "drizzle":
        cmd = ["npx", "drizzle-kit", "migrate"]
        if path is not None:
            cmd.extend(["--config", _schema_arg(path)])
        return cmd
    cmd = ["alembic"]
    if path is not None:
        cmd.extend(["-c", _schema_arg(path)])
    cmd.extend(["upgrade", revision])
    return cmd


def run_tenant_migrations(
    tool: str,
    *,
    connection_url: str,
    cwd: Path | str,
    schema_path: Optional[Path | str] = None,
    revision: str = "head",
    timeout: int = 120,
    extra_env: Optional[Mapping[str, str]] = None,
    command: Optional[Sequence[str]] = None,
) -> DBMigrationResult:
    """Run native schema migrations against a tenant-owned database.

    The connection URL is only passed through environment variables
    (``DATABASE_URL`` and ``SQLALCHEMY_URL``) so it never appears in the
    logged command/result argv.
    """
    if not connection_url:
        raise ValueError("connection_url is required")
    normalized = _normalize_tool(tool)
    work_dir = Path(cwd)
    argv = list(command) if command is not None else build_migration_command(
        normalized,
        schema_path=schema_path,
        revision=revision,
    )
    env = os.environ.copy()
    env["DATABASE_URL"] = connection_url
    env["SQLALCHEMY_URL"] = connection_url
    if extra_env:
        env.update(extra_env)
    env_vars = sorted({"DATABASE_URL", "SQLALCHEMY_URL", *(extra_env or {}).keys()})

    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        result = DBMigrationResult(
            tool=normalized,
            command=argv,
            cwd=str(work_dir),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            env_vars=env_vars,
        )
    except subprocess.TimeoutExpired as exc:
        result = DBMigrationResult(
            tool=normalized,
            command=argv,
            cwd=str(work_dir),
            returncode=124,
            stdout=exc.stdout or "",
            stderr=f"timeout after {timeout}s",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            env_vars=env_vars,
        )
        raise DBMigrationCommandError(result.stderr, result=result) from exc

    if result.returncode != 0:
        raise DBMigrationCommandError(
            result.stderr or result.stdout or "migration command failed",
            result=result,
        )
    return result


__all__ = [
    "DBMigrationCommandError",
    "DBMigrationError",
    "DBMigrationResult",
    "UnsupportedDBMigrationToolError",
    "build_migration_command",
    "run_tenant_migrations",
]
