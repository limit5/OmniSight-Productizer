"""FS.1.7 — Three-provider provision → migrate → smoke tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import respx

from backend.db_provisioning import run_tenant_migrations
from backend.db_provisioning.neon import NEON_API_BASE, NeonDBProvisionAdapter
from backend.db_provisioning.planetscale import (
    PLANETSCALE_API_BASE,
    PlanetScaleDBProvisionAdapter,
)
from backend.db_provisioning.supabase import (
    SUPABASE_API_BASE,
    SupabaseDBProvisionAdapter,
)

N = NEON_API_BASE
P = PLANETSCALE_API_BASE
S = SUPABASE_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, msg="err"):
    return httpx.Response(status, json={"message": msg})


class _RunRecorder:
    def __init__(self, returncode: int = 0, stdout: str = "smoke ok", stderr: str = ""):
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


def _assert_migrate_smoke(rec, *, connection_url: str, provider: str, tmp_path: Path):
    result = run_tenant_migrations(
        "sqlalchemy",
        connection_url=connection_url,
        cwd=tmp_path,
        command=["omnisight-db-smoke", provider],
        extra_env={"OMNISIGHT_DB_PROVIDER": provider},
    )

    assert result.ok is True
    assert result.stdout == "smoke ok"
    assert result.command == ["omnisight-db-smoke", provider]
    assert connection_url not in " ".join(result.command)

    argv, kwargs = rec.calls[-1]
    assert argv == result.command
    assert kwargs["cwd"] == Path(tmp_path)
    assert kwargs["env"]["DATABASE_URL"] == connection_url
    assert kwargs["env"]["SQLALCHEMY_URL"] == connection_url
    assert kwargs["env"]["OMNISIGHT_DB_PROVIDER"] == provider
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


class TestProvisionMigrateSmoke:

    @respx.mock
    async def test_supabase_provision_migrate_smoke(self, monkeypatch, tmp_path):
        rec = _RunRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        respx.get(f"{S}/projects").mock(return_value=_ok([]))
        respx.post(f"{S}/projects").mock(
            return_value=_ok({
                "id": "prj_123",
                "ref": "abcdefghijklmnopqrst",
                "organization_id": "org_123",
                "name": "tenant-demo",
                "region": "us-east-1",
                "status": "ACTIVE",
                "database": {"host": "db.abcdefghijklmnopqrst.supabase.co"},
            }, status=201),
        )

        provision = await SupabaseDBProvisionAdapter(
            token="sbp_ABCDEF0123456789",
            database_name="tenant-demo",
            organization_id="org_123",
            provider_tier="team",
        ).provision_database(db_pass="p=word")

        assert provision.provider == "supabase"
        assert provision.created is True
        assert provision.connection_url == (
            "postgresql://postgres.abcdefghijklmnopqrst:p%3Dword@"
            "db.abcdefghijklmnopqrst.supabase.co:5432/postgres"
        )
        assert provision.encryption_at_rest is not None
        assert provision.backup_schedule is not None
        assert provision.pep_hold is not None

        _assert_migrate_smoke(
            rec,
            connection_url=provision.connection_url,
            provider="supabase",
            tmp_path=tmp_path,
        )

    @respx.mock
    async def test_neon_provision_migrate_smoke(self, monkeypatch, tmp_path):
        rec = _RunRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        respx.get(f"{N}/projects").mock(return_value=_ok({"projects": []}))
        respx.post(f"{N}/projects").mock(
            return_value=_ok({
                "project": {
                    "id": "prj_123",
                    "name": "tenant-demo",
                    "region_id": "aws-us-east-1",
                    "status": "ready",
                },
                "connection_uris": [
                    {"connection_uri": "postgresql://user:pass@ep.example/neondb"},
                ],
            }, status=201),
        )

        provision = await NeonDBProvisionAdapter(
            token="napi_ABCDEF0123456789",
            database_name="tenant-demo",
            provider_tier="business",
        ).provision_database(pg_version=16)

        assert provision.provider == "neon"
        assert provision.created is True
        assert provision.connection_url == "postgresql://user:pass@ep.example/neondb"
        assert provision.encryption_at_rest is not None
        assert provision.backup_schedule is not None
        assert provision.pep_hold is not None

        _assert_migrate_smoke(
            rec,
            connection_url=provision.connection_url,
            provider="neon",
            tmp_path=tmp_path,
        )

    @respx.mock
    async def test_planetscale_provision_migrate_smoke(self, monkeypatch, tmp_path):
        rec = _RunRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        respx.get(f"{P}/organizations/org-demo/databases/tenant-demo").mock(
            return_value=_err(404, "missing"),
        )
        respx.post(f"{P}/organizations/org-demo/databases").mock(
            return_value=_ok({
                "id": "db_123",
                "name": "tenant-demo",
                "state": "ready",
                "region": {"slug": "us-east"},
            }, status=201),
        )
        respx.post(
            f"{P}/organizations/org-demo/databases/tenant-demo/branches/main/passwords",
        ).mock(
            return_value=_ok({
                "id": "pw_123",
                "username": "user",
                "plain_text": "secret",
                "access_host_url": "aws.connect.psdb.cloud",
            }, status=201),
        )

        provision = await PlanetScaleDBProvisionAdapter(
            token="pscale_ABCDEF0123456789",
            database_name="tenant-demo",
            organization="org-demo",
            provider_tier="enterprise",
        ).provision_database(password_name="omnisight")

        assert provision.provider == "planetscale"
        assert provision.created is True
        assert provision.connection_url == (
            "mysql://user:secret@aws.connect.psdb.cloud/tenant-demo?sslaccept=strict"
        )
        assert provision.encryption_at_rest is not None
        assert provision.backup_schedule is not None
        assert provision.pep_hold is not None

        _assert_migrate_smoke(
            rec,
            connection_url=provision.connection_url,
            provider="planetscale",
            tmp_path=tmp_path,
        )
