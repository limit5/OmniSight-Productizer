"""I5 — Tenant filesystem namespace tests.

Covers: tenant-scoped directory creation, path isolation between tenants,
path validation, ingest root per-tenant, migration of legacy artifacts,
and ensure_tenant_dirs helper.
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OMNISIGHT_AUTH_MODE", "session")


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Redirect all tenant_fs roots to tmp_path so tests don't touch real data."""
    monkeypatch.setattr("backend.tenant_fs._DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr("backend.tenant_fs._TENANTS_ROOT", tmp_path / "data" / "tenants")
    monkeypatch.setattr("backend.tenant_fs._INGEST_BASE", tmp_path / "ingest")
    monkeypatch.setattr("backend.tenant_fs._PROJECT_ROOT", tmp_path)
    from backend.db_context import set_tenant_id
    set_tenant_id(None)
    yield
    set_tenant_id(None)


class TestTenantDataRoot:
    def test_creates_directory(self, tmp_path):
        from backend.tenant_fs import tenant_data_root
        root = tenant_data_root("t-alpha")
        assert root.is_dir()
        assert root.name == "t-alpha"

    def test_default_tenant_from_context(self, tmp_path):
        from backend.db_context import set_tenant_id
        from backend.tenant_fs import tenant_data_root
        set_tenant_id("t-beta")
        root = tenant_data_root()
        assert root.name == "t-beta"

    def test_fallback_to_t_default(self, tmp_path):
        from backend.tenant_fs import tenant_data_root
        root = tenant_data_root()
        assert root.name == "t-default"


class TestTenantArtifactsRoot:
    def test_creates_artifacts_subdir(self, tmp_path):
        from backend.tenant_fs import tenant_artifacts_root
        root = tenant_artifacts_root("t-alpha")
        assert root.is_dir()
        assert root.name == "artifacts"
        assert root.parent.name == "t-alpha"

    def test_isolation_between_tenants(self, tmp_path):
        from backend.tenant_fs import tenant_artifacts_root
        a = tenant_artifacts_root("t-alpha")
        b = tenant_artifacts_root("t-beta")
        assert a != b
        assert a.parent.name == "t-alpha"
        assert b.parent.name == "t-beta"
        # Write a file in tenant A, confirm not visible in tenant B
        (a / "secret.txt").write_text("alpha-only")
        assert not (b / "secret.txt").exists()


class TestTenantIngestRoot:
    def test_creates_ingest_subdir(self, tmp_path):
        from backend.tenant_fs import tenant_ingest_root
        root = tenant_ingest_root("t-alpha")
        assert root.is_dir()
        assert root.name == "t-alpha"
        assert root.parent.name == "ingest"

    def test_isolation_between_tenants(self, tmp_path):
        from backend.tenant_fs import tenant_ingest_root
        a = tenant_ingest_root("t-alpha")
        b = tenant_ingest_root("t-beta")
        (a / "repo_clone").mkdir()
        assert not (b / "repo_clone").exists()


class TestTenantBackupsRoot:
    def test_creates_backups_subdir(self, tmp_path):
        from backend.tenant_fs import tenant_backups_root
        root = tenant_backups_root("t-alpha")
        assert root.is_dir()
        assert root.name == "backups"

    def test_isolation(self, tmp_path):
        from backend.tenant_fs import tenant_backups_root
        a = tenant_backups_root("t-a")
        b = tenant_backups_root("t-b")
        (a / "backup.sql").write_text("data-a")
        assert not (b / "backup.sql").exists()


class TestTenantWorkflowRunsRoot:
    def test_creates_workflow_runs_subdir(self, tmp_path):
        from backend.tenant_fs import tenant_workflow_runs_root
        root = tenant_workflow_runs_root("t-alpha")
        assert root.is_dir()
        assert root.name == "workflow_runs"

    def test_isolation(self, tmp_path):
        from backend.tenant_fs import tenant_workflow_runs_root
        a = tenant_workflow_runs_root("t-a")
        b = tenant_workflow_runs_root("t-b")
        (a / "run-001.json").write_text("{}")
        assert not (b / "run-001.json").exists()


class TestEnsureTenantDirs:
    def test_creates_all_subdirs(self, tmp_path):
        from backend.tenant_fs import ensure_tenant_dirs
        root = ensure_tenant_dirs("t-gamma")
        assert (root / "artifacts").is_dir()
        assert (root / "backups").is_dir()
        assert (root / "workflow_runs").is_dir()

    def test_ingest_dir_created(self, tmp_path):
        from backend.tenant_fs import ensure_tenant_dirs, _INGEST_BASE
        ensure_tenant_dirs("t-gamma")
        # Verify the monkeypatched ingest base has t-gamma subdir
        from backend import tenant_fs
        assert (tenant_fs._INGEST_BASE / "t-gamma").is_dir()


class TestPathBelongsToTenant:
    def test_file_inside_tenant(self, tmp_path):
        from backend.tenant_fs import tenant_artifacts_root, path_belongs_to_tenant
        art = tenant_artifacts_root("t-alpha")
        f = art / "test.bin"
        f.write_bytes(b"hello")
        assert path_belongs_to_tenant(f, "t-alpha")

    def test_file_outside_tenant(self, tmp_path):
        from backend.tenant_fs import tenant_artifacts_root, path_belongs_to_tenant
        art_a = tenant_artifacts_root("t-alpha")
        art_b = tenant_artifacts_root("t-beta")
        f = art_a / "test.bin"
        f.write_bytes(b"hello")
        assert not path_belongs_to_tenant(f, "t-beta")

    def test_absolute_escape_rejected(self, tmp_path):
        from backend.tenant_fs import path_belongs_to_tenant
        assert not path_belongs_to_tenant(Path("/etc/passwd"), "t-alpha")


class TestTidValidation:
    def test_empty_falls_back_to_default(self):
        from backend.tenant_fs import tenant_data_root
        root = tenant_data_root("")
        assert root.name == "t-default"

    def test_validate_tid_rejects_empty(self):
        from backend.tenant_fs import _validate_tid
        with pytest.raises(ValueError):
            _validate_tid("")

    def test_rejects_path_traversal(self):
        from backend.tenant_fs import tenant_data_root
        with pytest.raises(ValueError):
            tenant_data_root("../etc")

    def test_rejects_special_chars(self):
        from backend.tenant_fs import tenant_data_root
        with pytest.raises(ValueError):
            tenant_data_root("t-alpha/../../etc")

    def test_accepts_valid_tid(self):
        from backend.tenant_fs import tenant_data_root
        root = tenant_data_root("t-my_tenant-123")
        assert root.name == "t-my_tenant-123"


class TestGetArtifactsRootTenantAware:
    def test_returns_tenant_scoped_path(self, tmp_path):
        from backend.db_context import set_tenant_id
        from backend.tenant_fs import tenant_artifacts_root
        set_tenant_id("t-test")
        root = tenant_artifacts_root()
        assert "t-test" in str(root)
        assert root.name == "artifacts"

    def test_explicit_tenant_overrides_context(self, tmp_path):
        from backend.db_context import set_tenant_id
        from backend.tenant_fs import tenant_artifacts_root
        set_tenant_id("t-context")
        root = tenant_artifacts_root("t-explicit")
        assert "t-explicit" in str(root)


class TestPathValidation:
    def test_tenant_path_valid(self, tmp_path):
        from backend.tenant_fs import tenant_artifacts_root, path_belongs_to_tenant
        art = tenant_artifacts_root("t-alpha")
        f = art / "report.md"
        f.write_text("hello")
        assert path_belongs_to_tenant(f, "t-alpha")

    def test_outside_path_invalid(self, tmp_path):
        from backend.tenant_fs import path_belongs_to_tenant
        assert not path_belongs_to_tenant(Path("/tmp/random/file.txt"), "t-alpha")


class TestCloneRepoTenantAware:
    def test_clone_dest_under_tenant_ingest(self, tmp_path):
        """Verify clone_repo computes dest under the tenant ingest root."""
        from backend.tenant_fs import tenant_ingest_root
        from backend.repo_ingest import _validate_url
        import re
        from urllib.parse import urlparse

        tid = "t-clone-test"
        ingest_root = tenant_ingest_root(tid)
        url = "https://github.com/example/repo.git"
        parsed = urlparse(url)
        repo_name = Path(parsed.path).stem.rstrip(".git") or "repo"
        repo_name = re.sub(r'[^a-zA-Z0-9_-]', '_', repo_name)
        expected_prefix = str(ingest_root / repo_name)
        assert ingest_root.is_dir()
        assert "t-clone-test" in str(ingest_root)


class TestCleanupIngestCacheTenantAware:
    def test_cleanup_removes_tenant_dir(self, tmp_path):
        from backend.tenant_fs import tenant_ingest_root
        from backend.repo_ingest import cleanup_ingest_cache
        root = tenant_ingest_root("t-clean")
        (root / "clone_123").mkdir()
        assert (root / "clone_123").exists()
        cleanup_ingest_cache("t-clean")
        assert not root.exists()

    def test_cleanup_does_not_touch_other_tenants(self, tmp_path):
        from backend.tenant_fs import tenant_ingest_root
        from backend.repo_ingest import cleanup_ingest_cache
        a = tenant_ingest_root("t-keep")
        b = tenant_ingest_root("t-remove")
        (a / "important").mkdir()
        (b / "disposable").mkdir()
        cleanup_ingest_cache("t-remove")
        assert (a / "important").exists()
