"""W4 #278 — docker-nginx adapter tests (filesystem-only, no daemon)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.deploy import BuildArtifact, get_adapter
from backend.deploy.base import (
    DeployArtifactError,
    RollbackUnavailableError,
)
from backend.deploy.docker_nginx import DockerNginxAdapter


@pytest.fixture
def build_site(tmp_path):
    # Isolate from deploy-ctx (output_dir also lives under tmp_path).
    site = tmp_path / "build-out"
    site.mkdir()
    (site / "index.html").write_text(
        "<!doctype html><title>x</title><body><h1>ok</h1></body>",
    )
    (site / "assets").mkdir()
    (site / "assets" / "app.js").write_text("console.log('hi');")
    return site


def _mk(tmp_path, **kw):
    return DockerNginxAdapter.from_plaintext_token(
        token="",
        project_name="demo-site",
        output_dir=tmp_path / "deploy-ctx",
        port=8082,
        **kw,
    )


class TestProvisionRendersBuildContext:

    async def test_provision_writes_canonical_files(self, tmp_path):
        adapter = _mk(tmp_path)
        r = await adapter.provision(env={"API_URL": "https://api.example.com"})
        root = adapter.output_dir
        assert (root / "Dockerfile").exists()
        assert (root / "nginx.conf").exists()
        assert (root / ".dockerignore").exists()
        assert (root / "docker-compose.yml").exists()
        assert (root / "deploy.sh").exists()
        assert (root / "public").is_dir()
        assert (root / ".env.deploy").exists()

        dockerfile = (root / "Dockerfile").read_text()
        assert "FROM nginx:1.27-alpine" in dockerfile
        assert "EXPOSE 8082" in dockerfile
        assert "demo-site" in dockerfile  # labelled with project
        assert "HEALTHCHECK" in dockerfile

        nginx = (root / "nginx.conf").read_text()
        assert "listen       8082" in nginx
        assert "try_files $uri $uri/ /index.html" in nginx
        assert "/healthz" in nginx
        assert "__PORT__" not in nginx  # no placeholders leaked

        env_file = (root / ".env.deploy").read_text()
        assert "API_URL=https://api.example.com" in env_file

        assert r.created is True
        assert r.url == "http://localhost:8082"
        assert r.env_vars_set == ["API_URL"]

    async def test_provision_without_env_skips_env_file(self, tmp_path):
        adapter = _mk(tmp_path)
        await adapter.provision()
        assert not (adapter.output_dir / ".env.deploy").exists()

    async def test_deploy_sh_is_executable(self, tmp_path):
        adapter = _mk(tmp_path)
        await adapter.provision()
        sh = adapter.output_dir / "deploy.sh"
        # On POSIX filesystems we set exec bit explicitly.
        mode = sh.stat().st_mode & 0o111
        assert mode != 0
        content = sh.read_text()
        assert "docker build" in content
        assert "demo-site:latest" in content
        assert "--restart unless-stopped" in content

    async def test_compose_references_project_and_port(self, tmp_path):
        adapter = _mk(tmp_path)
        await adapter.provision()
        compose = (adapter.output_dir / "docker-compose.yml").read_text()
        assert "image: demo-site:latest" in compose
        assert "8082:8082" in compose
        assert "__PROJECT__" not in compose


class TestDeployCopiesArtifact:

    async def test_deploy_copies_files_into_public(self, tmp_path, build_site):
        adapter = _mk(tmp_path)
        await adapter.provision()
        r = await adapter.deploy(BuildArtifact(path=build_site, commit_sha="deadbeef"))
        pub = adapter.output_dir / "public"
        assert (pub / "index.html").exists()
        assert (pub / "assets" / "app.js").exists()
        # identical content
        assert (pub / "index.html").read_text() == (build_site / "index.html").read_text()
        assert r.deployment_id.startswith("demo-site-")
        assert r.status == "ready"
        assert r.url == "http://localhost:8082"
        assert r.raw["files_copied"] == 2

    async def test_deploy_before_provision_auto_provisions(self, tmp_path, build_site):
        adapter = _mk(tmp_path)
        r = await adapter.deploy(BuildArtifact(path=build_site))
        assert (adapter.output_dir / "Dockerfile").exists()
        assert r.raw["files_copied"] >= 1

    async def test_deploy_replaces_previous_public_tree(self, tmp_path, build_site):
        adapter = _mk(tmp_path)
        await adapter.provision()
        # first deploy
        await adapter.deploy(BuildArtifact(path=build_site))
        # second deploy with a totally different set of files
        fresh = tmp_path / "next-build"
        fresh.mkdir()
        (fresh / "index.html").write_text("v2")
        await adapter.deploy(BuildArtifact(path=fresh))
        pub = adapter.output_dir / "public"
        assert (pub / "index.html").read_text() == "v2"
        # old file "assets/app.js" should be gone after replace
        assert not (pub / "assets").exists()

    async def test_deploy_empty_artifact_raises(self, tmp_path):
        adapter = _mk(tmp_path)
        await adapter.provision()
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(DeployArtifactError):
            await adapter.deploy(BuildArtifact(path=empty_dir))


class TestRollbackSemantics:

    async def test_rollback_without_docker_build_is_unavailable(self, tmp_path, build_site):
        adapter = _mk(tmp_path)
        await adapter.provision()
        await adapter.deploy(BuildArtifact(path=build_site))
        with pytest.raises(RollbackUnavailableError):
            await adapter.rollback()


class TestGetUrlAndFactory:

    async def test_get_url_matches_public_url_override(self, tmp_path):
        adapter = _mk(tmp_path, public_url="https://demo.internal.example.com")
        await adapter.provision()
        assert adapter.get_url() == "https://demo.internal.example.com"

    def test_factory_resolves_alias_variants(self):
        assert get_adapter("docker-nginx") is DockerNginxAdapter
        assert get_adapter("docker_nginx") is DockerNginxAdapter
        assert get_adapter("nginx") is DockerNginxAdapter

    def test_from_plaintext_token_accepts_empty(self, tmp_path):
        adapter = DockerNginxAdapter.from_plaintext_token(
            "", project_name="p", output_dir=tmp_path / "x",
        )
        # Token defaults to a non-empty sentinel so the base __init__ is happy,
        # but the raw token is still log-safe.
        fp = adapter.token_fp()
        assert fp  # non-empty
