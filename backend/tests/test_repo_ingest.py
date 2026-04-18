"""Tests for B2/INGEST-01 — repo_ingest module.

Covers introspect + map_to_parsed_spec for three starter templates:
  1. v0.app Next.js project
  2. FastAPI backend
  3. Rust CLI tool

Also tests clone_repo validation, credential handling, and edge cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.repo_ingest import (
    _build_auth_url,
    _validate_url,
    clone_repo,
    introspect,
    map_to_parsed_spec,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture helpers — synthetic repo trees
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
def nextjs_repo(tmp_path: Path) -> Path:
    """Simulate a v0.app Next.js project."""
    pkg = {
        "name": "my-v0-app",
        "version": "0.1.0",
        "scripts": {
            "dev": "next dev",
            "build": "next build",
            "start": "next start",
            "lint": "next lint",
        },
        "dependencies": {
            "next": "14.2.0",
            "react": "^18.2.0",
            "react-dom": "^18.2.0",
            "@prisma/client": "^5.0.0",
            "tailwindcss": "^3.4.0",
        },
        "devDependencies": {
            "typescript": "^5.3.0",
            "@types/react": "^18.2.0",
            "prisma": "^5.0.0",
            "eslint": "^8.0.0",
        },
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg, indent=2))
    (tmp_path / "next.config.mjs").write_text(
        "/** @type {import('next').NextConfig} */\n"
        "const nextConfig = {\n"
        "  output: 'standalone',\n"
        "};\n"
        "export default nextConfig;\n"
    )
    (tmp_path / "README.md").write_text(
        "# My V0 App\n\nGenerated with v0.dev.\n"
        "## Getting Started\n\n```bash\nnpm run dev\n```\n"
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("export default function Home() {}")
    return tmp_path


@pytest.fixture()
def fastapi_repo(tmp_path: Path) -> Path:
    """Simulate a FastAPI backend project."""
    (tmp_path / "requirements.txt").write_text(
        "fastapi>=0.109.0\n"
        "uvicorn[standard]>=0.27.0\n"
        "sqlalchemy>=2.0.0\n"
        "psycopg2-binary>=2.9.0\n"
        "pydantic>=2.5.0\n"
        "alembic>=1.13.0\n"
        "# dev\n"
        "pytest>=8.0.0\n"
        "httpx>=0.27.0\n"
    )
    (tmp_path / "README.md").write_text(
        "# FastAPI Backend\n\nA production-ready FastAPI service.\n"
    )
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n"
    )
    return tmp_path


@pytest.fixture()
def rust_cli_repo(tmp_path: Path) -> Path:
    """Simulate a Rust CLI tool project."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\n'
        'name = "my-cli-tool"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
        '\n'
        '[dependencies]\n'
        'clap = { version = "4.5", features = ["derive"] }\n'
        'serde = { version = "1.0", features = ["derive"] }\n'
        'serde_json = "1.0"\n'
        'tokio = { version = "1", features = ["full"] }\n'
        'anyhow = "1.0"\n'
    )
    (tmp_path / "README.md").write_text(
        "# my-cli-tool\n\nA fast CLI utility written in Rust.\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() { println!(\"hello\"); }")
    return tmp_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  URL validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestValidateUrl:
    def test_valid_https(self):
        assert _validate_url("https://github.com/user/repo") == "https://github.com/user/repo"

    def test_valid_ssh(self):
        assert _validate_url("git@github.com:user/repo.git") == "git@github.com:user/repo.git"

    def test_empty(self):
        with pytest.raises(ValueError, match="Empty"):
            _validate_url("")

    def test_shell_injection(self):
        with pytest.raises(ValueError, match="Invalid characters"):
            _validate_url("https://evil.com/repo; rm -rf /")

    def test_bad_scheme(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            _validate_url("ftp://host/repo")

    def test_whitespace_stripped(self):
        assert _validate_url("  https://github.com/r  ") == "https://github.com/r"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auth URL building
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildAuthUrl:
    def test_embeds_token(self):
        url = _build_auth_url("https://github.com/user/repo.git", "ghp_abc123")
        assert "x-access-token:ghp_abc123@github.com" in url
        assert url.startswith("https://")

    def test_no_token_passthrough(self):
        url = _build_auth_url("https://github.com/user/repo.git", "")
        assert "x-access-token" not in url

    def test_ssh_url_passthrough(self):
        url = _build_auth_url("git@github.com:user/repo.git", "ghp_abc123")
        assert url == "git@github.com:user/repo.git"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Template 1: v0.app Next.js
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNextjsTemplate:
    def test_introspect_detects_files(self, nextjs_repo: Path):
        result = introspect(nextjs_repo)
        assert "package.json" in result.detected_files
        assert "next.config.mjs" in result.detected_files
        assert "README.md" in result.detected_files

    def test_introspect_parses_package_json(self, nextjs_repo: Path):
        result = introspect(nextjs_repo)
        assert result.package_json is not None
        assert result.package_json["name"] == "my-v0-app"
        assert "next" in result.package_json["dependencies"]

    def test_introspect_reads_next_config(self, nextjs_repo: Path):
        result = introspect(nextjs_repo)
        assert "standalone" in result.next_config

    def test_map_framework(self, nextjs_repo: Path):
        result = introspect(nextjs_repo)
        spec = map_to_parsed_spec(result)
        assert spec.framework.value == "nextjs"
        assert spec.framework.confidence >= 0.9

    def test_map_runtime_model(self, nextjs_repo: Path):
        result = introspect(nextjs_repo)
        spec = map_to_parsed_spec(result)
        assert spec.runtime_model.value == "ssr"
        assert spec.runtime_model.confidence >= 0.8

    def test_map_project_type(self, nextjs_repo: Path):
        result = introspect(nextjs_repo)
        spec = map_to_parsed_spec(result)
        assert spec.project_type.value == "web_app"

    def test_map_persistence_prisma(self, nextjs_repo: Path):
        result = introspect(nextjs_repo)
        spec = map_to_parsed_spec(result)
        assert spec.persistence.value == "postgres"
        assert spec.persistence.confidence >= 0.6

    def test_map_hardware_not_required(self, nextjs_repo: Path):
        result = introspect(nextjs_repo)
        spec = map_to_parsed_spec(result)
        assert spec.hardware_required.value == "no"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Template 2: FastAPI backend
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFastapiTemplate:
    def test_introspect_detects_files(self, fastapi_repo: Path):
        result = introspect(fastapi_repo)
        assert "requirements.txt" in result.detected_files
        assert "README.md" in result.detected_files

    def test_introspect_parses_requirements(self, fastapi_repo: Path):
        result = introspect(fastapi_repo)
        assert any("fastapi" in r for r in result.requirements_txt)
        assert any("psycopg2" in r for r in result.requirements_txt)
        assert not any(r.startswith("#") for r in result.requirements_txt)

    def test_map_framework(self, fastapi_repo: Path):
        result = introspect(fastapi_repo)
        spec = map_to_parsed_spec(result)
        assert spec.framework.value == "fastapi"
        assert spec.framework.confidence >= 0.9

    def test_map_project_type(self, fastapi_repo: Path):
        result = introspect(fastapi_repo)
        spec = map_to_parsed_spec(result)
        assert spec.project_type.value == "web_app"

    def test_map_persistence(self, fastapi_repo: Path):
        result = introspect(fastapi_repo)
        spec = map_to_parsed_spec(result)
        assert spec.persistence.value == "postgres"
        assert spec.persistence.confidence >= 0.8

    def test_map_runtime_model(self, fastapi_repo: Path):
        result = introspect(fastapi_repo)
        spec = map_to_parsed_spec(result)
        assert spec.runtime_model.value == "ssr"

    def test_map_hardware_not_required(self, fastapi_repo: Path):
        result = introspect(fastapi_repo)
        spec = map_to_parsed_spec(result)
        assert spec.hardware_required.value == "no"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Template 3: Rust CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRustCliTemplate:
    def test_introspect_detects_files(self, rust_cli_repo: Path):
        result = introspect(rust_cli_repo)
        assert "Cargo.toml" in result.detected_files
        assert "README.md" in result.detected_files

    def test_introspect_reads_cargo_toml(self, rust_cli_repo: Path):
        result = introspect(rust_cli_repo)
        assert "clap" in result.cargo_toml
        assert "my-cli-tool" in result.cargo_toml

    def test_map_framework(self, rust_cli_repo: Path):
        result = introspect(rust_cli_repo)
        spec = map_to_parsed_spec(result)
        assert spec.framework.value == "rust"

    def test_map_project_type(self, rust_cli_repo: Path):
        result = introspect(rust_cli_repo)
        spec = map_to_parsed_spec(result)
        assert spec.project_type.value == "cli_tool"
        assert spec.project_type.confidence >= 0.8

    def test_map_runtime_model(self, rust_cli_repo: Path):
        result = introspect(rust_cli_repo)
        spec = map_to_parsed_spec(result)
        assert spec.runtime_model.value == "cli"

    def test_map_hardware_not_required(self, rust_cli_repo: Path):
        result = introspect(rust_cli_repo)
        spec = map_to_parsed_spec(result)
        assert spec.hardware_required.value == "no"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEdgeCases:
    def test_introspect_empty_dir(self, tmp_path: Path):
        result = introspect(tmp_path)
        assert result.detected_files == []
        assert result.package_json is None
        spec = map_to_parsed_spec(result)
        assert spec.project_type.value == "unknown"

    def test_introspect_nonexistent(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            introspect(tmp_path / "nope")

    def test_malformed_package_json(self, tmp_path: Path):
        (tmp_path / "package.json").write_text("{invalid json")
        result = introspect(tmp_path)
        assert result.package_json is None
        assert "package.json" in result.detected_files

    def test_to_dict_serialisable(self, nextjs_repo: Path):
        result = introspect(nextjs_repo)
        spec = map_to_parsed_spec(result)
        d = spec.to_dict()
        assert isinstance(d, dict)
        assert d["framework"]["value"] == "nextjs"

    def test_ssg_next_config(self, tmp_path: Path):
        """Next.js with output: 'export' should map to SSG."""
        pkg = {"name": "ssg-app", "dependencies": {"next": "14.0.0", "react": "18.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        (tmp_path / "next.config.mjs").write_text(
            "const nextConfig = { output: 'export' };\nexport default nextConfig;\n"
        )
        result = introspect(tmp_path)
        spec = map_to_parsed_spec(result)
        assert spec.runtime_model.value == "ssg"

    def test_clone_repo_invalid_url(self):
        with pytest.raises(ValueError):
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                clone_repo("")
            )

    def test_readme_truncated(self, tmp_path: Path):
        long_readme = "# Title\n" + "x" * 20000
        (tmp_path / "README.md").write_text(long_readme)
        result = introspect(tmp_path)
        assert len(result.readme_content) <= 8192
