# SKILL-SPRING-BOOT — X9 #305

Fifth (and final priority-X) software-vertical skill pack. Closes the
X0-X4 framework evidence surface: after X5 FastAPI (Python), X6 Go,
X7 Rust, X8 Tauri (dual-language Rust+TS), X9 proves the framework on
the JVM — third major runtime, first enterprise-Java consumer.

## Why this skill exists

The `backend-java` role (X2) references X9 as its canonical template
and the role's quality bar (`mvn verify` / JaCoCo 70% / OWASP DC / no
`@Autowired` on fields) needs a concrete pilot that operators can
render and inspect. The Maven/Gradle build path into
`backend.build_adapters` also needs a real dogfood — priority X
shipped six NATIVE + skill-hook adapters without a Java one because
no Java consumer existed yet; X9 adds `MavenAdapter` +
`GradleAdapter` under the same `_SkillHookAdapter` contract as
`GoreleaserAdapter` and `CargoDistAdapter`.

X4 SPDX license scan grows a `maven` ecosystem detector: the existing
cargo / go / pip / npm scanners each have a walk-fallback when the
preferred CLI is missing, so the new `_scan_maven` adapter mirrors
that — prefers `mvn license:aggregate-third-party-report`, falls back
to parsing the `<dependencies>` block in `pom.xml` / `dependencies`
in `build.gradle.kts` as `UNKNOWN` rows so the gate always runs.

## Outputs

A rendered Spring Boot 3.2+ project tree that:

- builds with `mvn package` (Maven) or `./gradlew bootJar` (Gradle)
  and produces an executable fat jar ≤ 80 MiB
- serves `/api/v1/health` (backed by Spring Actuator) +
  `/api/v1/items` CRUD via Spring Web MVC with constructor injection
- passes `mvn test` / `gradle test` at ≥ 70% JaCoCo line coverage —
  matches `COVERAGE_THRESHOLDS["java"]` in `backend.software_simulator`
- builds a multi-stage Docker image via
  `backend.build_adapters.DockerImageAdapter`
  (`eclipse-temurin:21-jdk` builder → `gcr.io/distroless/java21:nonroot`
  runtime, uid 65532)
- packages a Helm chart via
  `backend.build_adapters.HelmChartAdapter`
- emits a `pom.xml` / `build.gradle.kts` that the new
  `MavenAdapter` / `GradleAdapter` `_validate_source` accepts
- passes the three X4 compliance gates (SPDX license / CVE scan /
  SBOM emit — `maven` ecosystem via `mvn license:aggregate-third-party-report`
  + `pom.xml` fallback)
- runs Flyway migrations (`V1__create_items_table.sql` is shipped as
  a representative baseline; operator adds more)

## Choice knobs

| Knob            | Values                         | Default              |
|-----------------|--------------------------------|----------------------|
| `group_id`      | Maven/Gradle group             | `com.example`        |
| `artifact_id`   | build artifact id              | `<slug of project_name>` |
| `build_tool`    | `maven` \| `gradle`            | `maven`              |
| `database`      | `postgres` \| `h2` \| `none`   | `postgres`           |
| `deploy`        | `docker` \| `helm` \| `both`   | `both`               |
| `compliance`    | `on` \| `off`                  | `on`                 |

See `configs/skills/skill-spring-boot/tasks.yaml` for the DAG each
knob routes through.

## How to render

```python
from pathlib import Path
from backend.spring_boot_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/my-service"),
    options=ScaffoldOptions(
        project_name="my-service",
        group_id="com.acme.platform",
        artifact_id="my-service",
        build_tool="maven",
        database="postgres",
        deploy="both",
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

## Maven / Gradle wiring

The rendered `pom.xml` pins `spring-boot-starter-parent` to `3.2.x`,
uses `maven-compiler-plugin` with `<source>21</source>` /
`<target>21</target>` and `<failOnWarning>true</failOnWarning>`, and
wires `jacoco-maven-plugin` with a `check` goal at `LINE` coverage
`0.70`. The rendered `build.gradle.kts` uses the Kotlin DSL with
`java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }`
and a `jacocoTestCoverageVerification` rule at `0.70`.

The new X3 `MavenAdapter` / `GradleAdapter` validate the scaffold
ships the right root file (`pom.xml` / `build.gradle.kts` +
`gradlew`) and produce a fat jar under `target/` / `build/libs/`
without running `mvn package` / `./gradlew bootJar` on CI.

## Flyway migrations

Migrations live under `src/main/resources/db/migration/` as
`V<version>__<description>.sql`. A `V1__create_items_table.sql`
baseline is shipped so `mvn test` runs with a populated schema under
Testcontainers (or H2 when `database=h2`). Operators add
`V2__*.sql` onwards; Flyway picks them up automatically at
`spring.flyway.locations=classpath:db/migration`.

## JUnit 5 test surface

- `HealthControllerTest` uses `@WebMvcTest` to hit `/api/v1/health`
  without booting the full context (fast, < 2s).
- `ItemsControllerTest` uses `@WebMvcTest` + Mockito 5 to stub the
  service layer; verifies CRUD contract shape without a DB.
- `ItemsRepositoryTest` uses `@DataJpaTest` against Testcontainers
  Postgres when `database=postgres`, H2 otherwise — validates Flyway
  migrations apply cleanly and the repository round-trips an entity.
- JaCoCo is wired via `jacoco-maven-plugin` / `jacoco` Gradle plugin;
  coverage floor `LINE=0.70` matches `COVERAGE_THRESHOLDS["java"]`.
