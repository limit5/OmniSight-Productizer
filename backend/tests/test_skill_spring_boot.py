"""X9 #305 — SKILL-SPRING-BOOT contract tests.

SKILL-SPRING-BOOT is the fifth (and final priority-X) software-vertical
skill pack and re-exercises the X0-X4 framework on JVM 21 LTS after
X5 (Python) / X6 (Go) / X7 (Rust) / X8 (Tauri — Rust + TS).

* **X0** — ``target_kind=software`` profile loads cleanly for the
  ``linux-x86_64-native`` binding the scaffold defaults to.
* **X1** — ``pom.xml`` JaCoCo ``check`` goal (or Gradle
  ``jacocoTestCoverageVerification``) pins the X1 Java threshold
  (``COVERAGE_THRESHOLDS["java"] == 70.0`` in
  ``backend.software_simulator``).
* **X2** — rendered project honours backend-java role defaults:
  constructor injection (no ``@Autowired`` field), ``@ConfigurationProperties``
  (no scattered ``System.getenv``), ``spring.jpa.hibernate.ddl-auto: validate``.
* **X3** — ``DockerImageAdapter`` + ``HelmChartAdapter`` +
  ``MavenAdapter`` / ``GradleAdapter`` all accept the rendered tree
  without hitting the network.
* **X4** — ``software_compliance.run_all`` bundle runs against the
  rendered project (ecosystem=maven via pom.xml / build.gradle.kts)
  and produces a structured verdict.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend.build_adapters import (
    BuildSource,
    DockerImageAdapter,
    GradleAdapter,
    HelmChartAdapter,
    MavenAdapter,
    default_targets_for_role,
)
from backend.platform import get_platform_config
from backend.skill_registry import get_skill, list_skills, validate_skill
from backend.software_compliance.licenses import detect_ecosystem
from backend.software_simulator import COVERAGE_THRESHOLDS
from backend.spring_boot_scaffolder import (
    ScaffoldOptions,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
    _slugify_artifact,
    _slugify_package_segment,
    dry_run_build,
    pilot_report,
    render_project,
    validate_pack,
)


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "spring-pilot"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="spring-pilot",
        build_tool="maven",
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
        assert "skill-spring-boot" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-spring-boot")
        assert result.ok, (
            f"skill-spring-boot validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-spring-boot")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-spring-boot")
        assert info is not None and info.manifest is not None
        assert "CORE-05" in info.manifest.depends_on_core
        assert info.manifest.depends_on_skills == []

    def test_manifest_keywords_include_spring_markers(self):
        info = get_skill("skill-spring-boot")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        assert {
            "java", "jvm-21", "spring-boot", "maven", "gradle",
            "flyway", "junit-5", "jacoco", "x9",
        }.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-spring-boot"

    def test_skill_dir_resolution(self):
        assert _SKILL_DIR.is_dir()
        assert (_SKILL_DIR / "skill.yaml").is_file()
        assert (_SKILL_DIR / "tasks.yaml").is_file()
        assert (_SKILL_DIR / "SKILL.md").is_file()
        assert _SCAFFOLDS_DIR.is_dir()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slug helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSlugifyArtifact:
    def test_hyphens_preserved(self):
        assert _slugify_artifact("my-pilot-service") == "my-pilot-service"

    def test_uppercase_lowered(self):
        assert _slugify_artifact("MyApi") == "myapi"

    def test_strip_non_alpha(self):
        assert _slugify_artifact("foo/bar@baz") == "foo-bar-baz"

    def test_collapse_runs(self):
        assert _slugify_artifact("foo___bar") == "foo-bar"

    def test_empty_fallback(self):
        assert _slugify_artifact("") == "service"
        assert _slugify_artifact("---") == "service"


class TestSlugifyPackageSegment:
    def test_hyphen_to_underscore(self):
        assert _slugify_package_segment("my-service") == "my_service"

    def test_dot_to_underscore(self):
        assert _slugify_package_segment("my.service") == "my_service"

    def test_leading_digit_prefixed(self):
        assert _slugify_package_segment("42-service").startswith("pkg_")

    def test_empty_fallback(self):
        assert _slugify_package_segment("") == "service"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffold option validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScaffoldOptions:
    def test_defaults_validate(self):
        _default_opts().validate()

    def test_bad_build_tool_rejected(self):
        with pytest.raises(ValueError, match="build_tool"):
            _default_opts(build_tool="bazel").validate()

    def test_bad_database_rejected(self):
        with pytest.raises(ValueError, match="database"):
            _default_opts(database="mysql").validate()

    def test_bad_deploy_rejected(self):
        with pytest.raises(ValueError, match="deploy"):
            _default_opts(deploy="serverless").validate()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError, match="project_name"):
            _default_opts(project_name="   ").validate()

    def test_bad_group_id_rejected(self):
        with pytest.raises(ValueError, match="group_id"):
            _default_opts(group_id="NotReverseDns").validate()

    def test_group_id_requires_dot(self):
        with pytest.raises(ValueError, match="group_id"):
            _default_opts(group_id="example").validate()

    def test_default_artifact_id_slug(self):
        opts = _default_opts(project_name="Acme-Portal")
        assert opts.resolved_artifact_id() == "acme-portal"

    def test_explicit_artifact_id_wins(self):
        opts = _default_opts(artifact_id="custom-name")
        assert opts.resolved_artifact_id() == "custom-name"

    def test_default_base_package_joins_group_and_artifact(self):
        opts = _default_opts(group_id="com.acme.platform", project_name="my-service")
        assert opts.resolved_base_package() == "com.acme.platform.my_service"
        assert opts.resolved_package_path() == "com/acme/platform/my_service"

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


class TestRenderOutcomeMaven:
    def test_render_creates_required_files(self, project_dir):
        render_project(project_dir, _default_opts())
        pkg = "com/example/spring_pilot"
        required = [
            "pom.xml",
            "Dockerfile",
            "docker-compose.yml",
            "Makefile",
            "README.md",
            "spdx.allowlist.json",
            ".env.example",
            ".gitignore",
            f"src/main/java/{pkg}/Application.java",
            f"src/main/java/{pkg}/config/AppProperties.java",
            f"src/main/java/{pkg}/api/HealthController.java",
            f"src/main/java/{pkg}/api/ItemsController.java",
            f"src/main/java/{pkg}/service/ItemService.java",
            f"src/main/java/{pkg}/domain/Item.java",
            f"src/main/java/{pkg}/domain/ItemRepository.java",
            "src/main/resources/application.yaml",
            "src/main/resources/logback-spring.xml",
            "src/main/resources/db/migration/V1__create_items_table.sql",
            f"src/test/java/{pkg}/api/HealthControllerTest.java",
            f"src/test/java/{pkg}/api/ItemsControllerTest.java",
            f"src/test/java/{pkg}/domain/ItemRepositoryTest.java",
            f"src/test/java/{pkg}/service/ItemServiceTest.java",
            "deploy/helm/Chart.yaml",
            "deploy/helm/values.yaml",
            "deploy/helm/templates/deployment.yaml",
            "deploy/helm/templates/service.yaml",
            "deploy/helm/templates/ingress.yaml",
        ]
        for rel in required:
            assert (project_dir / rel).is_file(), f"missing: {rel}"

    def test_maven_skips_gradle_quartet(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        for f in (
            "build.gradle.kts",
            "settings.gradle.kts",
            "gradlew",
            "gradle/wrapper/gradle-wrapper.properties",
        ):
            assert not (project_dir / f).exists(), f"maven shouldn't ship {f}"

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

    def test_database_h2_skips_postgres_deps(self, project_dir):
        render_project(project_dir, _default_opts(database="h2"))
        pom = (project_dir / "pom.xml").read_text(encoding="utf-8")
        assert "com.h2database" in pom
        assert "postgresql" not in pom.lower() or "flyway-database-postgresql" not in pom

    def test_database_none_drops_jpa_and_flyway(self, project_dir):
        render_project(project_dir, _default_opts(database="none"))
        pom = (project_dir / "pom.xml").read_text(encoding="utf-8")
        assert "spring-boot-starter-data-jpa" not in pom
        assert "flyway-core" not in pom
        pkg = "com/example/spring_pilot"
        assert not (project_dir / f"src/main/java/{pkg}/domain/Item.java").exists()
        assert not (project_dir / f"src/main/java/{pkg}/domain/ItemRepository.java").exists()
        assert not (project_dir / "src/main/resources/db/migration/V1__create_items_table.sql").exists()

    def test_idempotent_re_render(self, project_dir):
        o1 = render_project(project_dir, _default_opts())
        o2 = render_project(project_dir, _default_opts())
        assert sorted(o1.files_written) == sorted(o2.files_written)
        assert o1.bytes_written == o2.bytes_written

    def test_overwrite_false_warns_on_existing(self, project_dir):
        render_project(project_dir, _default_opts())
        outcome = render_project(project_dir, _default_opts(), overwrite=False)
        assert outcome.warnings
        assert all("skipped existing" in w for w in outcome.warnings)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Build-tool knob — maven vs gradle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildToolKnob:
    def test_gradle_ships_kotlin_dsl(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        assert (project_dir / "build.gradle.kts").is_file()
        assert (project_dir / "settings.gradle.kts").is_file()
        assert (project_dir / "gradle" / "wrapper" / "gradle-wrapper.properties").is_file()
        assert (project_dir / "gradlew").is_file()

    def test_gradle_skips_pom_xml(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        assert not (project_dir / "pom.xml").exists()

    def test_gradlew_is_executable(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        gradlew = project_dir / "gradlew"
        assert gradlew.stat().st_mode & 0o111, "gradlew must be executable"

    def test_maven_build_file_pins_parent(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        pom = (project_dir / "pom.xml").read_text(encoding="utf-8")
        assert "spring-boot-starter-parent" in pom
        assert "<version>3.2.5</version>" in pom

    def test_gradle_build_file_pins_plugin(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        build = (project_dir / "build.gradle.kts").read_text(encoding="utf-8")
        assert 'id("org.springframework.boot") version "3.2.5"' in build

    def test_makefile_wires_build_tool_specific_targets(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        mk = (project_dir / "Makefile").read_text(encoding="utf-8")
        assert "mvn spring-boot:run" in mk
        render_project(project_dir, _default_opts(build_tool="gradle"))
        mk = (project_dir / "Makefile").read_text(encoding="utf-8")
        assert "./gradlew bootRun" in mk


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
#  X1 — Java coverage floor (70%)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX1CoverageThreshold:
    def test_x1_java_threshold_is_70(self):
        # Anchor assertion: if someone softens this constant, the
        # scaffold's JaCoCo rule must move in lockstep.
        assert COVERAGE_THRESHOLDS["java"] == 70.0

    def test_maven_pom_pins_jacoco_70(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        pom = (project_dir / "pom.xml").read_text(encoding="utf-8")
        assert "jacoco-maven-plugin" in pom
        assert "<minimum>0.70</minimum>" in pom

    def test_gradle_build_pins_jacoco_70(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        build = (project_dir / "build.gradle.kts").read_text(encoding="utf-8")
        assert "jacocoTestCoverageVerification" in build
        assert '"0.70".toBigDecimal' in build

    def test_maven_verify_wires_jacoco_check(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        pom = (project_dir / "pom.xml").read_text(encoding="utf-8")
        assert "<goal>check</goal>" in pom

    def test_gradle_check_depends_on_jacoco(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        build = (project_dir / "build.gradle.kts").read_text(encoding="utf-8")
        assert "tasks.check" in build
        assert "jacocoTestCoverageVerification" in build


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X2 — backend-java role alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX2RoleAlignment:
    def test_items_controller_uses_constructor_injection(self, project_dir):
        render_project(project_dir, _default_opts())
        pkg = "com/example/spring_pilot"
        src = (project_dir / f"src/main/java/{pkg}/api/ItemsController.java").read_text()
        # backend-java anti-pattern: @Autowired on field.
        assert "@Autowired" not in src
        assert "public ItemsController(ItemService service)" in src

    def test_app_properties_uses_configurationproperties(self, project_dir):
        render_project(project_dir, _default_opts())
        pkg = "com/example/spring_pilot"
        src = (project_dir / f"src/main/java/{pkg}/config/AppProperties.java").read_text()
        assert "@ConfigurationProperties" in src
        assert "System.getenv" not in src

    def test_application_yaml_pins_ddl_auto_validate(self, project_dir):
        render_project(project_dir, _default_opts(database="postgres"))
        yaml_text = (project_dir / "src/main/resources/application.yaml").read_text()
        assert "ddl-auto: validate" in yaml_text

    def test_pom_pins_jdk_21(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        pom = (project_dir / "pom.xml").read_text()
        assert "<java.version>21</java.version>" in pom
        assert "<source>21</source>" in pom
        assert "<target>21</target>" in pom

    def test_gradle_pins_jdk_21(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        build = (project_dir / "build.gradle.kts").read_text()
        assert "JavaLanguageVersion.of(21)" in build

    def test_virtual_threads_opt_in(self, project_dir):
        # backend-java role: virtual threads on JDK 21 should be opt-in.
        render_project(project_dir, _default_opts())
        yaml_text = (project_dir / "src/main/resources/application.yaml").read_text()
        assert "virtual:" in yaml_text
        assert "enabled: true" in yaml_text

    def test_dockerfile_uses_distroless_nonroot(self, project_dir):
        render_project(project_dir, _default_opts())
        df = (project_dir / "Dockerfile").read_text()
        assert "distroless/java21" in df
        assert "nonroot" in df
        assert "USER nonroot" in df

    def test_fail_on_warning(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        pom = (project_dir / "pom.xml").read_text()
        assert "<failOnWarning>true</failOnWarning>" in pom


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Flyway migrations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFlyway:
    def test_flyway_baseline_migration_shipped_with_postgres(self, project_dir):
        render_project(project_dir, _default_opts(database="postgres"))
        mig = project_dir / "src/main/resources/db/migration/V1__create_items_table.sql"
        assert mig.is_file()
        assert "CREATE TABLE" in mig.read_text()

    def test_flyway_migration_shipped_with_h2(self, project_dir):
        render_project(project_dir, _default_opts(database="h2"))
        mig = project_dir / "src/main/resources/db/migration/V1__create_items_table.sql"
        assert mig.is_file()

    def test_flyway_migration_skipped_when_database_none(self, project_dir):
        render_project(project_dir, _default_opts(database="none"))
        mig = project_dir / "src/main/resources/db/migration/V1__create_items_table.sql"
        assert not mig.exists()

    def test_application_yaml_wires_flyway_classpath_location(self, project_dir):
        render_project(project_dir, _default_opts(database="postgres"))
        yaml_text = (project_dir / "src/main/resources/application.yaml").read_text()
        assert "locations: classpath:db/migration" in yaml_text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JUnit 5 test surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestJunit5:
    def test_health_controller_test_uses_webmvctest(self, project_dir):
        render_project(project_dir, _default_opts())
        pkg = "com/example/spring_pilot"
        src = (project_dir / f"src/test/java/{pkg}/api/HealthControllerTest.java").read_text()
        assert "@WebMvcTest" in src
        assert "org.junit.jupiter.api.Test" in src

    def test_items_controller_test_uses_mockito(self, project_dir):
        render_project(project_dir, _default_opts())
        pkg = "com/example/spring_pilot"
        src = (project_dir / f"src/test/java/{pkg}/api/ItemsControllerTest.java").read_text()
        assert "@MockBean" in src
        assert "@WebMvcTest" in src

    def test_items_repository_test_uses_datajpatest(self, project_dir):
        render_project(project_dir, _default_opts(database="postgres"))
        pkg = "com/example/spring_pilot"
        src = (project_dir / f"src/test/java/{pkg}/domain/ItemRepositoryTest.java").read_text()
        assert "@DataJpaTest" in src
        # Postgres knob wires Testcontainers.
        assert "PostgreSQLContainer" in src


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X3 — build adapter resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX3DockerAdapter:
    def test_docker_adapter_validates_rendered_tree(self, project_dir):
        render_project(project_dir, _default_opts())
        adapter = DockerImageAdapter(name="spring-pilot", version="0.1.0")
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
        adapter = HelmChartAdapter(name="spring-pilot", version="0.1.0")
        source = BuildSource(path=project_dir, manifest=chart_dir)
        source.validate()
        adapter._validate_source(source)

    def test_dry_run_reports_helm(self, project_dir):
        render_project(project_dir, _default_opts(deploy="helm"))
        res = dry_run_build(project_dir, _default_opts(deploy="helm"))
        assert res["helm"]["artifact_valid"] is True
        assert res["helm"]["adapter"] == "HelmChartAdapter"
        assert "docker" not in res


class TestX3MavenAdapter:
    def test_maven_adapter_validates_rendered_pom(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        adapter = MavenAdapter(name="spring-pilot", version="0.1.0")
        source = BuildSource(path=project_dir)
        source.validate()
        adapter._validate_source(source)

    def test_maven_adapter_rejects_missing_pom(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        adapter = MavenAdapter(name="spring-pilot", version="0.1.0")
        source = BuildSource(path=project_dir)
        source.validate()
        with pytest.raises(Exception, match="pom.xml"):
            adapter._validate_source(source)

    def test_dry_run_reports_maven_for_maven_knob(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        res = dry_run_build(project_dir, _default_opts(build_tool="maven"))
        assert res["maven"]["artifact_valid"] is True
        assert res["maven"]["adapter"] == "MavenAdapter"
        assert res["maven"]["pom"].endswith("pom.xml")
        assert "gradle" not in res

    def test_backend_java_role_default_targets_include_maven_gradle(self):
        targets = default_targets_for_role("backend-java")
        assert "maven" in targets
        assert "gradle" in targets
        assert "docker" in targets


class TestX3GradleAdapter:
    def test_gradle_adapter_validates_rendered_build_file(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        adapter = GradleAdapter(name="spring-pilot", version="0.1.0")
        source = BuildSource(path=project_dir)
        source.validate()
        adapter._validate_source(source)

    def test_gradle_adapter_rejects_missing_build_file(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        adapter = GradleAdapter(name="spring-pilot", version="0.1.0")
        source = BuildSource(path=project_dir)
        source.validate()
        with pytest.raises(Exception, match="build.gradle"):
            adapter._validate_source(source)

    def test_dry_run_reports_gradle_for_gradle_knob(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        res = dry_run_build(project_dir, _default_opts(build_tool="gradle"))
        assert res["gradle"]["artifact_valid"] is True
        assert res["gradle"]["adapter"] == "GradleAdapter"
        assert res["gradle"]["build_file"].endswith("build.gradle.kts")
        assert "maven" not in res


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
        # Java ecosystem: EPL-2.0 is common (JUnit 5, H2), include it.
        assert "EPL-2.0" in allow["allowed_licenses"]

    def test_ecosystem_detected_as_maven_for_pom(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        assert detect_ecosystem(project_dir) == "maven"

    def test_ecosystem_detected_as_maven_for_gradle_kts(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        assert detect_ecosystem(project_dir) == "maven"

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
    def test_pilot_report_shape_maven(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="maven"))
        report = pilot_report(project_dir, _default_opts(build_tool="maven"))
        assert report["skill"] == "skill-spring-boot"
        assert report["x0_profile"] == "linux-x86_64-native"
        assert report["x1_coverage_floor"] == 70.0
        assert "x3_build" in report
        assert "x4_compliance" in report
        assert report["options"]["base_package"] == "com.example.spring_pilot"

    def test_pilot_report_shape_gradle(self, project_dir):
        render_project(project_dir, _default_opts(build_tool="gradle"))
        report = pilot_report(project_dir, _default_opts(build_tool="gradle"))
        assert report["x1_coverage_floor"] == 70.0
        assert report["options"]["build_tool"] == "gradle"

    def test_pilot_report_is_json_safe(self, project_dir):
        import json
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        json.dumps(report)
