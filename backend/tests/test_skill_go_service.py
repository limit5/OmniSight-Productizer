"""X6 #302 — SKILL-GO-SERVICE contract tests.

SKILL-GO-SERVICE is the second software-vertical skill pack and
re-exercises the X0-X4 framework on a non-Python language toolchain
(go modules / go test / goreleaser). These tests lock the framework
invariants the same way ``test_skill_fastapi.py`` locks X5:

* **X0** — ``target_kind=software`` profile loads cleanly for the
  ``linux-x86_64-native`` binding the scaffold defaults to.
* **X1** — ``scripts/check_cov.sh`` pins the X1 Go threshold
  (``COVERAGE_THRESHOLDS["go"] == 70.0`` in
  ``backend.software_simulator``).
* **X2** — rendered project honours backend-go role defaults:
  ``log/slog`` structured logging, envconfig (no scattered
  ``os.Getenv``), Go 1.22 module.
* **X3** — ``DockerImageAdapter`` + ``HelmChartAdapter`` +
  ``GoreleaserAdapter`` all accept the rendered tree without hitting
  the network.
* **X4** — ``software_compliance.run_all`` bundle runs against the
  rendered project (ecosystem=go via go.mod) and produces a
  structured verdict.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend.build_adapters import (
    BuildSource,
    DockerImageAdapter,
    GoreleaserAdapter,
    HelmChartAdapter,
)
from backend.go_service_scaffolder import (
    ScaffoldOptions,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
    _slugify_module,
    dry_run_build,
    pilot_report,
    render_project,
    validate_pack,
)
from backend.platform import get_platform_config
from backend.skill_registry import get_skill, list_skills, validate_skill
from backend.software_compliance.licenses import detect_ecosystem
from backend.software_simulator import COVERAGE_THRESHOLDS


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "go-pilot"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="go-pilot",
        framework="gin",
        database="postgres",
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
        assert "skill-go-service" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-go-service")
        assert result.ok, (
            f"skill-go-service validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-go-service")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-go-service")
        assert info is not None and info.manifest is not None
        assert "CORE-05" in info.manifest.depends_on_core
        assert info.manifest.depends_on_skills == []

    def test_manifest_keywords_include_go_markers(self):
        info = get_skill("skill-go-service")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        assert {"go", "golang", "gin", "fiber", "goreleaser", "x6"}.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-go-service"

    def test_skill_dir_resolution(self):
        assert _SKILL_DIR.is_dir()
        assert (_SKILL_DIR / "skill.yaml").is_file()
        assert (_SKILL_DIR / "tasks.yaml").is_file()
        assert (_SKILL_DIR / "SKILL.md").is_file()
        assert _SCAFFOLDS_DIR.is_dir()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module-path slug helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSlugifyModule:
    def test_hyphens_preserved(self):
        assert _slugify_module("my-pilot-service") == "my-pilot-service"

    def test_uppercase_lowered(self):
        assert _slugify_module("MyApi") == "myapi"

    def test_strip_non_alpha(self):
        assert _slugify_module("foo/bar@baz") == "foo-bar-baz"

    def test_empty_fallback(self):
        assert _slugify_module("") == "service"
        assert _slugify_module("---") == "service"

    def test_dot_preserved(self):
        assert _slugify_module("foo.bar") == "foo.bar"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffold option validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScaffoldOptions:
    def test_defaults_validate(self):
        _default_opts().validate()

    def test_bad_framework_rejected(self):
        with pytest.raises(ValueError, match="framework"):
            _default_opts(framework="echo").validate()

    def test_bad_database_rejected(self):
        with pytest.raises(ValueError, match="database"):
            _default_opts(database="mysql").validate()

    def test_bad_deploy_rejected(self):
        with pytest.raises(ValueError, match="deploy"):
            _default_opts(deploy="serverless").validate()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError, match="project_name"):
            _default_opts(project_name="   ").validate()

    def test_default_module_path(self):
        opts = _default_opts(project_name="Acme-Portal")
        assert opts.resolved_module_path() == "github.com/example/acme-portal"

    def test_explicit_module_path_wins(self):
        opts = _default_opts(module_path="gitlab.com/acme/svc")
        assert opts.resolved_module_path() == "gitlab.com/acme/svc"

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
        render_project(project_dir, _default_opts())
        required = [
            "go.mod",
            "go.sum",
            "Dockerfile",
            "docker-compose.yml",
            ".goreleaser.yaml",
            "Makefile",
            "README.md",
            "spdx.allowlist.json",
            ".env.example",
            ".gitignore",
            ".golangci.yml",
            "cmd/server/main.go",
            "internal/api/router.go",
            "internal/api/health.go",
            "internal/api/items.go",
            "internal/api/health_test.go",
            "internal/api/items_test.go",
            "internal/config/config.go",
            "internal/db/db.go",
            "internal/logging/logging.go",
            "scripts/check_cov.sh",
            "deploy/helm/Chart.yaml",
            "deploy/helm/values.yaml",
            "deploy/helm/templates/deployment.yaml",
            "deploy/helm/templates/service.yaml",
            "deploy/helm/templates/ingress.yaml",
        ]
        for rel in required:
            assert (project_dir / rel).is_file(), f"missing: {rel}"

    def test_cov_script_is_executable(self, project_dir):
        render_project(project_dir, _default_opts())
        script = project_dir / "scripts" / "check_cov.sh"
        assert script.stat().st_mode & 0o111, "check_cov.sh must be executable"

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
        assert "sqlite" in env.lower() or "app.db" in env
        assert "postgres://" not in env

    def test_database_postgres_compose_brings_up_postgres(self, project_dir):
        render_project(project_dir, _default_opts(database="postgres"))
        compose = (project_dir / "docker-compose.yml").read_text(encoding="utf-8")
        assert "postgres:" in compose
        assert "pg_isready" in compose

    def test_database_none_omits_db_dep_from_gomod(self, project_dir):
        render_project(project_dir, _default_opts(database="none"))
        gomod = (project_dir / "go.mod").read_text(encoding="utf-8")
        assert "pgx" not in gomod
        assert "modernc.org/sqlite" not in gomod

    def test_idempotent_re_render(self, project_dir):
        o1 = render_project(project_dir, _default_opts())
        o2 = render_project(project_dir, _default_opts())
        assert o1.files_written == o2.files_written
        assert o1.bytes_written == o2.bytes_written

    def test_overwrite_false_warns_on_existing(self, project_dir):
        render_project(project_dir, _default_opts())
        outcome = render_project(project_dir, _default_opts(), overwrite=False)
        assert outcome.warnings
        assert all("skipped existing" in w for w in outcome.warnings)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Framework knob — gin vs fiber
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFrameworkKnob:
    def test_gin_mentions_gin_imports(self, project_dir):
        render_project(project_dir, _default_opts(framework="gin"))
        router = (project_dir / "internal" / "api" / "router.go").read_text()
        assert "github.com/gin-gonic/gin" in router
        assert "gofiber" not in router

    def test_fiber_mentions_fiber_imports(self, project_dir):
        render_project(project_dir, _default_opts(framework="fiber"))
        router = (project_dir / "internal" / "api" / "router.go").read_text()
        assert "github.com/gofiber/fiber/v2" in router
        assert "gin-gonic" not in router

    def test_gomod_pins_framework(self, project_dir):
        render_project(project_dir, _default_opts(framework="gin"))
        gomod_gin = (project_dir / "go.mod").read_text()
        assert "gin-gonic/gin" in gomod_gin
        assert "gofiber" not in gomod_gin

    def test_module_path_injected_into_main(self, project_dir):
        outcome = render_project(
            project_dir,
            _default_opts(module_path="gitlab.com/acme/svc"),
        )
        main = (project_dir / "cmd" / "server" / "main.go").read_text()
        assert "gitlab.com/acme/svc/internal/api" in main
        assert outcome.module_path == "gitlab.com/acme/svc"


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
#  X1 — Go coverage floor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX1CoverageThreshold:
    def test_x1_go_threshold_is_70(self):
        # Anchor assertion: if someone softens this constant, the
        # scaffold's check_cov.sh default must move in lockstep.
        assert COVERAGE_THRESHOLDS["go"] == 70.0

    def test_check_cov_defaults_to_70(self, project_dir):
        render_project(project_dir, _default_opts())
        script = (project_dir / "scripts" / "check_cov.sh").read_text()
        assert 'COVERAGE_THRESHOLD:-70' in script

    def test_makefile_wires_coverage_script(self, project_dir):
        render_project(project_dir, _default_opts())
        makefile = (project_dir / "Makefile").read_text()
        assert "./scripts/check_cov.sh" in makefile


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X2 — backend-go role alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX2RoleAlignment:
    def test_logging_uses_slog(self, project_dir):
        render_project(project_dir, _default_opts())
        log_src = (project_dir / "internal" / "logging" / "logging.go").read_text()
        # backend-go role default: stdlib log/slog, not zap/zerolog.
        assert "log/slog" in log_src
        assert "slog.NewJSONHandler" in log_src
        assert "zap" not in log_src and "zerolog" not in log_src

    def test_config_uses_envconfig_not_bare_osgetenv(self, project_dir):
        render_project(project_dir, _default_opts())
        cfg_src = (project_dir / "internal" / "config" / "config.go").read_text()
        assert "envconfig" in cfg_src
        # backend-go anti-pattern: scattered os.Getenv calls.
        assert "os.Getenv" not in cfg_src

    def test_gomod_pins_go_122(self, project_dir):
        render_project(project_dir, _default_opts())
        gomod = (project_dir / "go.mod").read_text()
        assert "go 1.22" in gomod

    def test_dockerfile_uses_distroless_nonroot(self, project_dir):
        render_project(project_dir, _default_opts())
        df = (project_dir / "Dockerfile").read_text()
        assert "distroless/static" in df
        assert "USER nonroot" in df
        assert "CGO_ENABLED=0" in df

    def test_golangci_config_enables_mandatory_linters(self, project_dir):
        render_project(project_dir, _default_opts())
        lint = (project_dir / ".golangci.yml").read_text()
        for linter in ("errcheck", "gosec", "staticcheck", "govet", "revive"):
            assert linter in lint


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X3 — build adapter resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX3DockerAdapter:
    def test_docker_adapter_validates_rendered_tree(self, project_dir):
        render_project(project_dir, _default_opts())
        adapter = DockerImageAdapter(name="go-pilot", version="0.1.0")
        source = BuildSource(path=project_dir)
        source.validate()
        adapter._validate_source(source)

    def test_dry_run_reports_docker(self, project_dir):
        render_project(project_dir, _default_opts(deploy="docker"))
        res = dry_run_build(project_dir, _default_opts(deploy="docker"))
        assert res["docker"]["artifact_valid"] is True
        assert res["docker"]["adapter"] == "DockerImageAdapter"
        assert "helm" not in res


class TestX3HelmAdapter:
    def test_helm_adapter_validates_rendered_chart(self, project_dir):
        render_project(project_dir, _default_opts())
        chart_dir = project_dir / "deploy" / "helm"
        assert (chart_dir / "Chart.yaml").is_file()
        adapter = HelmChartAdapter(name="go-pilot", version="0.1.0")
        source = BuildSource(path=project_dir, manifest=chart_dir)
        source.validate()
        adapter._validate_source(source)

    def test_dry_run_reports_helm(self, project_dir):
        render_project(project_dir, _default_opts(deploy="helm"))
        res = dry_run_build(project_dir, _default_opts(deploy="helm"))
        assert res["helm"]["artifact_valid"] is True
        assert res["helm"]["adapter"] == "HelmChartAdapter"
        assert "docker" not in res


class TestX3GoreleaserAdapter:
    def test_goreleaser_adapter_validates_rendered_config(self, project_dir):
        render_project(project_dir, _default_opts())
        adapter = GoreleaserAdapter(name="go-pilot", version="0.1.0")
        source = BuildSource(path=project_dir)
        source.validate()
        adapter._validate_source(source)

    def test_goreleaser_always_present_regardless_of_deploy(self, project_dir):
        # Release path is orthogonal to docker/helm deploy — the config
        # must survive every deploy-knob combination.
        for deploy in ("docker", "helm", "both"):
            render_project(project_dir, _default_opts(deploy=deploy))
            res = dry_run_build(project_dir, _default_opts(deploy=deploy))
            assert res["goreleaser"]["artifact_valid"] is True, (
                f"goreleaser dry-run failed for deploy={deploy}: "
                f"{res['goreleaser'].get('artifact_error')}"
            )

    def test_goreleaser_yaml_has_matrix(self, project_dir):
        render_project(project_dir, _default_opts())
        cfg = (project_dir / ".goreleaser.yaml").read_text()
        # Matrix: 3 OSes × 2 arches.
        for goos in ("linux", "darwin", "windows"):
            assert goos in cfg
        for goarch in ("amd64", "arm64"):
            assert goarch in cfg
        assert "CGO_ENABLED=0" in cfg
        assert "-s -w" in cfg  # static + stripped binary

    def test_dry_run_reports_goreleaser(self, project_dir):
        render_project(project_dir, _default_opts(deploy="both"))
        res = dry_run_build(project_dir, _default_opts(deploy="both"))
        assert res["goreleaser"]["adapter"] == "GoreleaserAdapter"
        assert res["goreleaser"]["artifact_valid"] is True


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

    def test_ecosystem_detected_as_go(self, project_dir):
        render_project(project_dir, _default_opts())
        assert detect_ecosystem(project_dir) == "go"

    def test_pilot_report_runs_x4_bundle(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        bundle = report["x4_compliance"]
        assert "gates" in bundle
        gate_ids = {g["gate_id"] for g in bundle["gates"]}
        assert gate_ids == {"license", "cve", "sbom"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pilot report — one-shot X0-X4 roll-up
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPilotReport:
    def test_pilot_report_shape(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        assert report["skill"] == "skill-go-service"
        assert report["x0_profile"] == "linux-x86_64-native"
        assert report["x1_coverage_floor"] == 70.0
        assert "x3_build" in report
        assert "x4_compliance" in report

    def test_pilot_report_is_json_safe(self, project_dir):
        import json
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        json.dumps(report)
