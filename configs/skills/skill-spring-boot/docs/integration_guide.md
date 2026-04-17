# SKILL-SPRING-BOOT Integration Guide

X9 #305. Fifth (and final priority-X) software-vertical skill pack.
Validates that the X0-X4 framework holds on JVM 21 after X5 FastAPI
(Python 3.11+) and X6 Go 1.22+ / X7 Rust 1.76+ / X8 Tauri (dual
Rust+TS).

## Render a project

```python
from pathlib import Path
from backend.spring_boot_scaffolder import ScaffoldOptions, render_project

outcome = render_project(
    out_dir=Path("/tmp/my-service"),
    options=ScaffoldOptions(
        project_name="my-service",
        group_id="com.acme.platform",
        artifact_id="my-service",
        build_tool="maven",          # or "gradle"
        database="postgres",         # or "h2" / "none"
        deploy="both",               # docker + helm
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

## Output tree (build_tool=maven, database=postgres, deploy=both)

```
my-service/
├── pom.xml                       (Spring Boot parent + JaCoCo check goal)
├── Dockerfile                    (temurin:21-jdk → distroless/java21:nonroot)
├── docker-compose.yml            (service + optional postgres)
├── Makefile
├── .env.example
├── .gitignore
├── README.md
├── spdx.allowlist.json           (X4 compliance — denies GPL/AGPL by default)
├── src/
│   ├── main/
│   │   ├── java/com/example/myservice/
│   │   │   ├── Application.java          (@SpringBootApplication)
│   │   │   ├── api/HealthController.java (/api/v1/health)
│   │   │   ├── api/ItemsController.java  (/api/v1/items CRUD)
│   │   │   ├── domain/Item.java          (JPA entity)
│   │   │   ├── domain/ItemRepository.java
│   │   │   ├── service/ItemService.java
│   │   │   └── config/AppProperties.java (@ConfigurationProperties)
│   │   └── resources/
│   │       ├── application.yaml
│   │       ├── logback-spring.xml        (JSON appender)
│   │       └── db/migration/
│   │           └── V1__create_items_table.sql
│   └── test/
│       └── java/com/example/myservice/
│           ├── api/HealthControllerTest.java    (@WebMvcTest)
│           ├── api/ItemsControllerTest.java     (@WebMvcTest + Mockito)
│           ├── domain/ItemRepositoryTest.java   (@DataJpaTest)
│           └── service/ItemServiceTest.java
└── deploy/
    └── helm/
        ├── Chart.yaml
        ├── values.yaml
        └── templates/{deployment,service,ingress}.yaml
```

## Output tree (build_tool=gradle)

Same as Maven except the root has:
```
my-service/
├── build.gradle.kts              (Kotlin DSL + jacoco plugin)
├── settings.gradle.kts
├── gradle/wrapper/gradle-wrapper.properties
├── gradlew                       (chmod 0o755)
└── gradlew.bat
```

## Quick start (after render)

```bash
# Maven
mvn verify                        # runs tests + JaCoCo 70% check
mvn spring-boot:run               # starts the dev server
mvn package -DskipTests           # builds fat jar into target/
docker build -t my-service:0.1.0 .
helm lint deploy/helm

# Gradle
./gradlew check                   # runs tests + JaCoCo 70% verify
./gradlew bootRun                 # starts the dev server
./gradlew bootJar                 # builds fat jar into build/libs/
```

## Framework gates validated

| X-series | What the scaffold exercises                                                      |
|----------|----------------------------------------------------------------------------------|
| X0       | `linux-x86_64-native` profile (`target_kind=software`)                           |
| X1       | `mvn test` + JaCoCo `LINE=0.70` (COVERAGE_THRESHOLDS["java"])                    |
| X2       | backend-java role anti-patterns (constructor inj, @ConfigurationProperties)      |
| X3       | `DockerImageAdapter` + `HelmChartAdapter` + `MavenAdapter`/`GradleAdapter`       |
| X4       | SPDX allowlist + CVE scan + SBOM via `backend.software_compliance` (maven eco)   |

## Flyway workflow

Migrations are discovered from `classpath:db/migration`:

```
src/main/resources/db/migration/
├── V1__create_items_table.sql      (baseline — shipped by scaffold)
├── V2__add_quantity.sql            (operator adds)
└── V3__...
```

`spring.jpa.hibernate.ddl-auto: validate` is pinned in
`application.yaml` so Hibernate refuses to start against an
un-migrated schema — forces operators to ship a migration for every
schema-breaking change.

## JaCoCo coverage floor

- Maven: `jacoco-maven-plugin` runs `prepare-agent` + `report` +
  `check` (LINE 0.70) during `verify`. A PR that drops below 70%
  breaks `mvn verify`.
- Gradle: `jacoco` plugin + `jacocoTestCoverageVerification` task
  wired as `check.dependsOn`. A PR that drops below 70% breaks
  `./gradlew check`.

The `70` value tracks
`backend.software_simulator.COVERAGE_THRESHOLDS["java"]` — if the
anchor constant moves, the scaffold's threshold must move in
lockstep (locked down by `test_x1_java_threshold_is_70`).

## SPDX compliance

`backend.software_compliance.licenses` gained a `maven` ecosystem
detector in X9:
- prefers `mvn license:aggregate-third-party-report` (Mojohaus
  license plugin) when `mvn` is on PATH
- falls back to parsing `<dependency>` blocks in `pom.xml` as
  `UNKNOWN` rows (or `dependencies` block in `build.gradle.kts`)

The gate reports denied / unknown / allowed counts; `spdx.allowlist.json`
is applied the same way as X6/X7/X8 — it's a scaffold-shipped
override that lets an operator waive a specific `name` or
`name@license` when a denied license is acceptable for a given
transitive.
