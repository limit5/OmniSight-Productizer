"""X5 #301 — SKILL-FASTAPI pilot skill contract tests.

SKILL-FASTAPI is the first software-vertical skill pack and the pilot
that validates the X0-X4 framework end-to-end. These tests lock the
framework invariants the same way D1 SKILL-UVC locked C5, D29
SKILL-HMI-WEBUI locked C26, and W6 SKILL-NEXTJS locked W0-W5:

* **X0** — ``target_kind=software`` profile loads cleanly for the
  ``linux-x86_64-native`` binding the scaffold defaults to.
* **X1** — rendered pyproject.toml pins ``--cov-fail-under=80``
  (matches ``COVERAGE_THRESHOLDS["python"]`` in
  ``backend.software_simulator``).
* **X2** — rendered project honours the backend-python role skill:
  pydantic-settings config, SQLAlchemy 2.x async engine, structured
  JSON logging, no bare ``os.environ`` reads.
* **X3** — ``DockerImageAdapter`` + ``HelmChartAdapter`` both accept
  the rendered tree without hitting the network.
* **X4** — ``software_compliance.run_all`` bundle runs against the
  rendered project and produces a structured verdict.
* **N3** — ``scripts/dump_openapi.py`` in the scaffold carries the
  same ``--check`` contract OmniSight uses for its own OpenAPI drift
  gate.
"""

from __future__ import annotations

import compileall
import tempfile
from pathlib import Path

import pytest

from backend.build_adapters import BuildSource, DockerImageAdapter, HelmChartAdapter
from backend.fastapi_scaffolder import (
    ScaffoldOptions,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
    _derive_package_name,
    dry_run_build,
    pilot_report,
    render_project,
    validate_pack,
)
from backend.platform_profile import get_platform_config
from backend.skill_registry import get_skill, list_skills, validate_skill


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "pilot-service"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="pilot-service",
        database="postgres",
        auth="jwt",
        deploy="both",
        compliance=True,
    )
    kwargs.update(overrides)
    return ScaffoldOptions(**kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Skill pack registry invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSkillPackRegistry:
    def test_pack_discoverable(self):
        names = {s.name for s in list_skills()}
        assert "skill-fastapi" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-fastapi")
        assert result.ok, (
            f"skill-fastapi validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-fastapi")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-fastapi")
        assert info is not None and info.manifest is not None
        # CORE-05 is the skill pack framework itself; the skill has no
        # other skill dependencies (unlike SKILL-NEXTJS which depends
        # on enterprise_web).
        assert "CORE-05" in info.manifest.depends_on_core
        assert info.manifest.depends_on_skills == []

    def test_manifest_keywords_include_pilot_marker(self):
        info = get_skill("skill-fastapi")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        assert {"pilot", "x5", "fastapi", "python", "dogfood"}.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-fastapi"

    def test_skill_dir_resolution(self):
        assert _SKILL_DIR.is_dir()
        assert (_SKILL_DIR / "skill.yaml").is_file()
        assert (_SKILL_DIR / "tasks.yaml").is_file()
        assert (_SKILL_DIR / "SKILL.md").is_file()
        assert _SCAFFOLDS_DIR.is_dir()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Package-name slug helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDerivePackageName:
    def test_hyphenated_project_name_becomes_underscore(self):
        assert _derive_package_name("my-pilot-service") == "my_pilot_service"

    def test_uppercase_is_lowered(self):
        assert _derive_package_name("My-API") == "my_api"

    def test_numeric_prefix_gets_app_prefix(self):
        # Leading digit would produce an invalid Python identifier.
        assert _derive_package_name("42mile") == "app_42mile"

    def test_all_non_alpha_fallback(self):
        assert _derive_package_name("---") == "app"

    def test_empty_string_fallback(self):
        assert _derive_package_name("") == "app"

    def test_already_snake_case_preserved(self):
        assert _derive_package_name("clean_name") == "clean_name"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffold option validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScaffoldOptions:
    def test_defaults_validate(self):
        _default_opts().validate()  # no exception

    def test_bad_database_rejected(self):
        with pytest.raises(ValueError, match="database"):
            _default_opts(database="mysql").validate()

    def test_bad_auth_rejected(self):
        with pytest.raises(ValueError, match="auth"):
            _default_opts(auth="saml").validate()

    def test_bad_deploy_rejected(self):
        with pytest.raises(ValueError, match="deploy"):
            _default_opts(deploy="serverless").validate()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError, match="project_name"):
            _default_opts(project_name="   ").validate()

    def test_explicit_package_name_wins(self):
        opts = _default_opts(package_name="custom_pkg")
        assert opts.resolved_package_name() == "custom_pkg"

    def test_default_package_name_slug(self):
        opts = _default_opts(project_name="My-API-Service")
        assert opts.resolved_package_name() == "my_api_service"

    def test_builds_docker_flags(self):
        assert _default_opts(deploy="docker").builds_docker() is True
        assert _default_opts(deploy="helm").builds_docker() is False
        assert _default_opts(deploy="both").builds_docker() is True

    def test_builds_helm_flags(self):
        assert _default_opts(deploy="helm").builds_helm() is True
        assert _default_opts(deploy="docker").builds_helm() is False
        assert _default_opts(deploy="both").builds_helm() is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Render outcome — structural invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRenderOutcome:
    def test_render_creates_required_files(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        pkg = outcome.package_name
        assert pkg == "pilot_service"
        required = [
            "pyproject.toml",
            "Dockerfile",
            "docker-compose.yml",
            "alembic.ini",
            "Makefile",
            "README.md",
            "spdx.allowlist.json",
            ".env.example",
            f"src/{pkg}/__init__.py",
            f"src/{pkg}/main.py",
            f"src/{pkg}/config.py",
            f"src/{pkg}/db.py",
            f"src/{pkg}/models.py",
            f"src/{pkg}/schemas.py",
            f"src/{pkg}/api/__init__.py",
            f"src/{pkg}/api/v1/__init__.py",
            f"src/{pkg}/api/v1/health.py",
            f"src/{pkg}/api/v1/items.py",
            f"src/{pkg}/core/__init__.py",
            f"src/{pkg}/core/logging.py",
            f"src/{pkg}/core/security.py",
            "alembic/env.py",
            "alembic/script.py.mako",
            "alembic/versions/0001_initial.py",
            "tests/__init__.py",
            "tests/conftest.py",
            "tests/test_health.py",
            "tests/test_items.py",
            "scripts/dump_openapi.py",
            "deploy/helm/Chart.yaml",
            "deploy/helm/values.yaml",
            "deploy/helm/templates/deployment.yaml",
            "deploy/helm/templates/service.yaml",
            "deploy/helm/templates/ingress.yaml",
        ]
        for rel in required:
            assert (project_dir / rel).is_file(), f"missing: {rel}"

    def test_auth_none_skips_security_module(self, project_dir):
        outcome = render_project(project_dir, _default_opts(auth="none"))
        pkg = outcome.package_name
        assert not (project_dir / "src" / pkg / "core" / "security.py").exists()

    def test_deploy_docker_skips_helm_dir(self, project_dir):
        render_project(project_dir, _default_opts(deploy="docker"))
        assert not (project_dir / "deploy" / "helm").exists()
        assert (project_dir / "Dockerfile").is_file()

    def test_deploy_helm_skips_dockerfile(self, project_dir):
        render_project(project_dir, _default_opts(deploy="helm"))
        assert not (project_dir / "Dockerfile").exists()
        assert not (project_dir / "docker-compose.yml").exists()
        assert (project_dir / "deploy" / "helm" / "Chart.yaml").is_file()

    def test_compliance_off_skips_spdx_allowlist(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        assert not (project_dir / "spdx.allowlist.json").exists()

    def test_database_sqlite_env_references_sqlite(self, project_dir):
        render_project(project_dir, _default_opts(database="sqlite"))
        env = (project_dir / ".env.example").read_text(encoding="utf-8")
        assert "sqlite+aiosqlite" in env
        assert "postgresql" not in env

    def test_database_postgres_compose_brings_up_postgres(self, project_dir):
        render_project(project_dir, _default_opts(database="postgres"))
        compose = (project_dir / "docker-compose.yml").read_text(encoding="utf-8")
        assert "postgres:" in compose
        assert "pg_isready" in compose

    def test_idempotent_re_render_overwrites(self, project_dir):
        outcome1 = render_project(project_dir, _default_opts())
        outcome2 = render_project(project_dir, _default_opts())
        assert outcome1.files_written == outcome2.files_written
        assert outcome1.bytes_written == outcome2.bytes_written

    def test_re_render_with_overwrite_false_warns_on_existing(self, project_dir):
        render_project(project_dir, _default_opts())
        outcome = render_project(project_dir, _default_opts(), overwrite=False)
        assert outcome.warnings, "expected at least one 'skipped existing' warning"
        assert all("skipped existing" in w for w in outcome.warnings)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rendered-code syntactic sanity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRenderedCode:
    def test_every_rendered_python_file_compiles(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        pkg = outcome.package_name
        # compileall returns True on success; rtl=True recurses.
        ok = compileall.compile_dir(
            str(project_dir / "src" / pkg),
            quiet=1, force=True,
        )
        assert ok, f"compileall failed for src/{pkg}/"

    def test_rendered_conftest_compiles(self, project_dir):
        render_project(project_dir, _default_opts())
        compile((project_dir / "tests" / "conftest.py").read_text(), "conftest.py", "exec")

    def test_rendered_alembic_env_compiles(self, project_dir):
        render_project(project_dir, _default_opts())
        compile((project_dir / "alembic" / "env.py").read_text(), "env.py", "exec")

    def test_rendered_dump_openapi_compiles(self, project_dir):
        render_project(project_dir, _default_opts())
        compile(
            (project_dir / "scripts" / "dump_openapi.py").read_text(),
            "dump_openapi.py",
            "exec",
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X0 — platform profile binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX0PlatformBinding:
    def test_default_profile_resolves_software(self):
        cfg = get_platform_config("linux-x86_64-native")
        assert cfg["target_kind"] == "software"

    def test_render_records_profile_binding(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        assert outcome.profile_binding == "linux-x86_64-native"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X1 — pytest + coverage gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX1CoverageThreshold:
    def test_pyproject_pins_cov_fail_under_80(self, project_dir):
        render_project(project_dir, _default_opts())
        text = (project_dir / "pyproject.toml").read_text(encoding="utf-8")
        assert "--cov-fail-under=80" in text, (
            "X1 COVERAGE_THRESHOLDS['python'] must survive render"
        )

    def test_pyproject_enables_asyncio_mode(self, project_dir):
        render_project(project_dir, _default_opts())
        text = (project_dir / "pyproject.toml").read_text(encoding="utf-8")
        assert 'asyncio_mode = "auto"' in text

    def test_pyproject_declares_httpx_dev_dep(self, project_dir):
        render_project(project_dir, _default_opts())
        text = (project_dir / "pyproject.toml").read_text(encoding="utf-8")
        assert "httpx" in text  # X5 ticket requires httpx for pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X2 — backend-python role alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX2RoleAlignment:
    def test_config_uses_pydantic_settings(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        cfg = (project_dir / "src" / outcome.package_name / "config.py").read_text()
        # backend-python role anti-pattern: never read os.environ directly.
        assert "pydantic_settings" in cfg
        assert "BaseSettings" in cfg

    def test_no_bare_os_environ_reads_in_runtime(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        # Allow-list: conftest.py legitimately seeds os.environ for test
        # isolation BEFORE importing the app. Runtime package code must
        # not do this.
        pkg = project_dir / "src" / outcome.package_name
        for py in pkg.rglob("*.py"):
            text = py.read_text()
            assert "os.environ[" not in text, f"bare os.environ read in {py}"
            assert "os.environ.get(" not in text, f"bare os.environ.get in {py}"

    def test_async_engine_and_session_dep(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        db = (project_dir / "src" / outcome.package_name / "db.py").read_text()
        # SQLAlchemy 2.x async is the role default.
        assert "create_async_engine" in db
        assert "async_sessionmaker" in db
        assert "get_db" in db

    def test_structured_json_logging(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        logp = (project_dir / "src" / outcome.package_name / "core" / "logging.py").read_text()
        # No print(), use JSON formatter — role anti-pattern.
        assert "JsonFormatter" in logp


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X3 — build adapter resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX3DockerAdapter:
    def test_docker_adapter_validates_rendered_tree(self, project_dir):
        render_project(project_dir, _default_opts())
        adapter = DockerImageAdapter(name="pilot-service", version="0.1.0")
        source = BuildSource(path=project_dir)
        source.validate()
        adapter._validate_source(source)  # should not raise

    def test_dry_run_build_reports_docker(self, project_dir):
        render_project(project_dir, _default_opts(deploy="docker"))
        result = dry_run_build(project_dir, _default_opts(deploy="docker"))
        assert result["docker"]["artifact_valid"] is True
        assert result["docker"]["adapter"] == "DockerImageAdapter"
        assert "helm" not in result  # deploy=docker skips helm probe


class TestX3HelmAdapter:
    def test_helm_adapter_validates_rendered_chart(self, project_dir):
        render_project(project_dir, _default_opts())
        chart_dir = project_dir / "deploy" / "helm"
        assert (chart_dir / "Chart.yaml").is_file()
        adapter = HelmChartAdapter(name="pilot-service", version="0.1.0")
        source = BuildSource(path=project_dir, manifest=chart_dir)
        source.validate()
        adapter._validate_source(source)  # should not raise

    def test_dry_run_build_reports_helm(self, project_dir):
        render_project(project_dir, _default_opts(deploy="helm"))
        result = dry_run_build(project_dir, _default_opts(deploy="helm"))
        assert result["helm"]["artifact_valid"] is True
        assert result["helm"]["adapter"] == "HelmChartAdapter"
        assert "docker" not in result  # deploy=helm skips docker probe

    def test_dry_run_build_both_targets(self, project_dir):
        render_project(project_dir, _default_opts(deploy="both"))
        result = dry_run_build(project_dir, _default_opts(deploy="both"))
        assert result["docker"]["artifact_valid"] is True
        assert result["helm"]["artifact_valid"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X4 — software compliance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX4Compliance:
    def test_spdx_allowlist_denies_gpl_by_default(self, project_dir):
        render_project(project_dir, _default_opts())
        import json
        allow = json.loads(
            (project_dir / "spdx.allowlist.json").read_text(encoding="utf-8")
        )
        assert "GPL-3.0" in allow["denied_licenses"]
        assert "AGPL-3.0" in allow["denied_licenses"]
        assert "Apache-2.0" in allow["allowed_licenses"]
        assert "MIT" in allow["allowed_licenses"]

    def test_pilot_report_runs_x4_bundle(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        bundle = report["x4_compliance"]
        assert "gates" in bundle
        gate_ids = {g["gate_id"] for g in bundle["gates"]}
        assert gate_ids == {"license", "cve", "sbom"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  N3 — OpenAPI governance contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestN3OpenApiGovernance:
    def test_dump_openapi_script_shipped(self, project_dir):
        render_project(project_dir, _default_opts())
        script = project_dir / "scripts" / "dump_openapi.py"
        assert script.is_file()

    def test_dump_openapi_contract_flags(self, project_dir):
        render_project(project_dir, _default_opts())
        text = (project_dir / "scripts" / "dump_openapi.py").read_text(encoding="utf-8")
        # The N3 contract: argparse --check, offline create_app().openapi(), exit 1 on drift.
        assert "--check" in text
        assert "create_app" in text
        assert ".openapi()" in text

    def test_dump_openapi_references_rendered_package(self, project_dir):
        outcome = render_project(project_dir, _default_opts(project_name="Acme-Portal"))
        text = (project_dir / "scripts" / "dump_openapi.py").read_text(encoding="utf-8")
        assert f"from {outcome.package_name}.main import create_app" in text

    def test_dry_run_build_flags_openapi_presence(self, project_dir):
        render_project(project_dir, _default_opts())
        result = dry_run_build(project_dir, _default_opts())
        assert result["openapi_dump"]["present"] is True
        assert result["openapi_dump"]["mentions_check_flag"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pilot report — one-shot X0-X4 roll-up
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPilotReport:
    def test_pilot_report_shape(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        assert report["skill"] == "skill-fastapi"
        assert report["x0_profile"] == "linux-x86_64-native"
        assert report["x1_coverage_floor"] == 80
        assert "x3_build" in report
        assert "x4_compliance" in report

    def test_pilot_report_serialises_without_error(self, project_dir):
        import json
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        # Shape must be JSON-safe for CI log ingestion.
        json.dumps(report)
