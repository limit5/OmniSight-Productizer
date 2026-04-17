# OmniSight Productizer

![Next.js](https://img.shields.io/badge/Next.js-16.2-black?logo=next.js)
![React](https://img.shields.io/badge/React-19.2-61DAFB?logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.7-3178C6?logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-4.2-06B6D4?logo=tailwindcss&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.1-1C3C3C?logo=langchain&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Isolation-2496ED?logo=docker&logoColor=white)

![Anthropic](https://img.shields.io/badge/Anthropic-Claude-D97706?logo=anthropic&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT-412991?logo=openai&logoColor=white)
![Google](https://img.shields.io/badge/Google-Gemini-4285F4?logo=google&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama-Local-ffffff?logo=ollama&logoColor=black)

![Tests](https://img.shields.io/badge/Tests-909-brightgreen)
![Tools](https://img.shields.io/badge/Tools-29_Sandboxed-green)
![Agents](https://img.shields.io/badge/Roles-19_Skills-blueviolet)
![API](https://img.shields.io/badge/API-~80_Endpoints-blue)

Full-stack autonomous development command center for embedded AI cameras (UVC/RTSP). Multi-agent orchestration with isolated workspaces, real-time streaming UI, dual-track simulation, and Docker-containerized cross-compilation.

## Architecture

```
Browser (Windows/Linux)
    |
Next.js (WSL2:3000)              Frontend — Sci-Fi FUI dashboard (18 components)
    | rewrites proxy
FastAPI (WSL2:8000)               Backend — Multi-agent engine (14 routers, ~80 endpoints)
    |
    +-- Orchestrator Gateway      Jira webhook → LLM DAG split → N CATCs → queue (O4 / /api/v1/orchestrator/*)
    +-- CATC Queue + Workers      Redis Streams queue + stateless worker pool + file-path dist-lock (O0-O3)
    +-- IntentSource Bridge       JIRA / GitHub / GitLab vendor-agnostic adapter + bidirectional status sync + audit (O5)
    +-- Merger Agent              Conflict-block resolver + Gerrit patchset push + scope-limited +2 vote (O6, human +2 still required for submit)
    +-- Merge Arbiter             Gerrit webhook → merger → human-vote reconciliation; dual-+2 SSOT evaluator + GitHub Actions fallback (O7 / /api/v1/orchestrator/merge-conflict, /human-vote, /check-change-ready)
    +-- Orchestration Mode Flag   monolith (run_graph in-proc, default) ↔ distributed (queue dispatch) feature flag + parity-locked SSE sequence + rollback drain CLI (O8 / OMNISIGHT_ORCHESTRATION_MODE, docs/ops/orchestration_migration.md)
    +-- Orchestration Observability Unified /metrics exporter (O1+O2+O6 series) + awaiting-human-+2 registry + orchestration.queue.tick / lock.acquired|released / merger.voted SSE + Prometheus alert rules (O9 / /api/v1/orchestration/snapshot, components/omnisight/orchestration-panel.tsx, deploy/prometheus/orchestration_alerts.rules.yml)
    +-- LangGraph Pipeline        Orchestrator → Conversation/Specialist → Tool Executor → Summarizer
    +-- 8 LLM Providers           Anthropic, OpenAI, Google, xAI, Groq, DeepSeek, Together, Ollama
    +-- 29 Sandboxed Tools        File, Git, Bash, Simulation, Platform, Review, Report
    +-- EventBus → SSE            Real-time push + event persistence + replay API
    +-- WorkspaceManager          git worktree per agent (CODEOWNERS enforcement)
    +-- ContainerManager          Docker cross-compilation (aarch64/armv7/riscv64 + vendor SDK mount)
    +-- Multi-Track Simulation    algo / hw / npu / deploy / hmi (constrained HMI bundle)
    +-- 4-Tier Notifications      L1 log → L2 Slack → L3 Jira → L4 PagerDuty (DLQ + retry)
    +-- NPI Lifecycle             8 phases × 3 tracks × 4 business models (Timeline + Gantt)
    +-- Slash Commands            22 commands with autocomplete (/status, /build, /simulate, ...)
    +-- SQLite WAL                11 tables, event_log, debug_findings, integrity check
```

## Quick Start

```bash
# 0. Config (one-time)
cp .env.example .env
#   then edit .env — at minimum set OMNISIGHT_LLM_PROVIDER + its API key.
#   Without one, agents run in rule-based fallback mode (see below).

# 1. Backend  (Python deps are hash-locked; N1 policy)
cd OmniSight-Productizer
python3 -m pip install --upgrade pip   # avoid Python 3.12 resolver bugs
pip install --require-hashes -r backend/requirements.txt
python3 -m uvicorn backend.main:app --port 8000

# 2. Frontend (new terminal)  —  pnpm is canonical (N1 policy)
#    Requires Node 20.17.x (.nvmrc); run `nvm use` first if you have nvm.
pnpm install --frozen-lockfile
pnpm run dev

# 3. Browser
open http://localhost:3000

# Alternative: one-shot docker compose
#   docker compose up --build
#   (reads the same .env; exposes :3000 + :8000)
```

Interactive API docs live at `http://localhost:8000/docs` (FastAPI
auto-generated Swagger) once the backend is up.

### Environment Variables

Copy `.env.example` to `.env` and set your LLM API key:

```bash
OMNISIGHT_LLM_PROVIDER=anthropic
OMNISIGHT_ANTHROPIC_API_KEY=sk-ant-...

# Or use local Ollama (no key needed)
# OMNISIGHT_LLM_PROVIDER=ollama
```

Without an API key the system runs in rule-based fallback mode — all features work, agents produce template responses instead of LLM-generated ones.

## Key Features

### Multi-Agent Pipeline
- **8 specialist agents**: firmware, software, validator, reporter, reviewer, general, custom, devops
- **19 role skill files** with domain-specific prompts (BSP, ISP, HAL, algorithm, SDET, etc.)
- **Conversational AI**: intent detection routes questions to conversation node (no tools), tasks to specialists
- **Self-healing**: error_check → retry (3x) → loop detection → human escalation
- **Verification loop**: simulation [FAIL] → auto-fix code → re-verify (2x max)

### Simulation & Verification
- **Multi-track**: algo (data-driven replay + Valgrind) / hw (mock sysfs + QEMU cross-run) / npu / deploy / **hmi** (C26 — constrained HMI bundle + IEC 62443 gate + budget gate) / web (W2 — Lighthouse / bundle / a11y / SEO / E2E / visual) / mobile (P2 — iOS Simulator + Android Emulator + UI test + device farm + screenshot matrix) / **software** (X1 — per-language pytest / go test / cargo test / mvn test / npm test / dotnet test + coverage gate + optional benchmark regression)
- **simulate.sh**: unified test runner with JSON report, coverage enforcement, cmake toolchain support
- **4 platform profiles**: aarch64, armv7, riscv64, vendor-example (extensible for any SoC)
- **Generalized profile schema (W0 #274)**: `target_kind: embedded | web | mobile | software` + `configs/platforms/schema.yaml` + `backend/platform.py::get_platform_config()` dispatcher — unblocks Priority W (Next.js / Nuxt.js) / P (iOS / Android) / X (software-only) verticals without touching the embedded fast path
- **Software simulate track (X1 #297)**: `scripts/simulate.sh --type=software --module=<software-profile>` thin shell dispatcher over `backend/software_simulator.py`. Language autodetect from project root markers (`pyproject.toml` / `setup.py` / `setup.cfg` / `requirements.txt` → python; `go.mod` → go; `Cargo.toml` → rust; `pom.xml` / `build.gradle` / `build.gradle.kts` / `gradlew` → java; `package.json` → node; `*.csproj` / `*.sln` → csharp) with explicit `--language=` override. Per-language test-runner dispatcher: `pytest` (falls back to `python3 -m pytest`) / `go test -json ./...` / `cargo test --no-fail-fast` / `mvn -B -q test` or `./gradlew test` / `pnpm test` or `yarn test` or `npm test --silent` (lockfile-based picker) / `dotnet test`. Coverage gate with X1-ticket thresholds pinned in `COVERAGE_THRESHOLDS` (Python 80% / Go 70% / Rust 75% / Java 70% / Node 80% / C# 70%), overridable with `--coverage-override=<pct>`. Coverage parsers: `coverage.py` TOTAL line / `go tool cover -func` total / `cargo llvm-cov --summary-only` (or tarpaulin fallback) / JaCoCo `line` counter XML / c8-istanbul `coverage-summary.json` total / Coverlet cobertura `line-rate`. Benchmark regression (opt-in via `--benchmark=on`) compares `--benchmark-current-ms=<float>` against `test_assets/benchmarks/<module>.json` with a 10% default threshold. Every external CLI is optional — sandbox runs without pytest/go/cargo/mvn/npm/dotnet degrade to a `mock` verdict that the shell distinguishes from a real pass. Profile validation refuses non-`software` `target_kind` (raises `SoftwareSimError`). 55 unit tests in `backend/tests/test_software_simulator.py` (language autodetect × 10 markers + C# csproj/sln + specific-before-generic + missing-dir; X1 thresholds pinned per-language + override + unknown-language; pytest/go-json/cargo/maven/node-jest+vitest+mocha/dotnet output parsers; Python/Go/Rust coverage regex parsers; JaCoCo XML fixture + cobertura XML fixture + c8/istanbul JSON summary ± threshold + mock fallback; benchmark regression missing-baseline skip / within-threshold pass / over-threshold fail / missing-current mock / corrupt-baseline fail; orchestrator profile mismatch raises / no-language returns error / forced-language override / benchmark opt-in; JSON result shape; supported-languages surface) + 8 shell-integration tests in `backend/tests/test_software_simulate.py` (envelope shape / python+go+rust autodetect + coverage threshold / language override / coverage override / benchmark opt-in degrades to skip without baseline / non-software profile surfaces error). 63/63 new tests green; 86/86 adjacent (`test_web_simulate` + `test_mobile_simulate` + `test_platform_schema`) green with zero regression
- **Software pilot skill pack (X5 #301)**: `configs/skills/skill-fastapi/` + `backend/fastapi_scaffolder.py` — first software-vertical skill pack; pilot that validates the X0-X4 framework end-to-end (same pattern D1 SKILL-UVC set for C5, D29 SKILL-HMI-WEBUI for C26, W6 SKILL-NEXTJS for the web vertical, and P7 SKILL-IOS for the mobile vertical). Also the first X-series **dogfood** — OmniSight's own backend is FastAPI, so the generated customer service consumes the exact stack the product runs. Renders a production-ready Python 3.11+ service with FastAPI 0.110+ app factory + async lifespan, Pydantic v2 + pydantic-settings config (never reads `os.environ` directly per the backend-python role anti-pattern), SQLAlchemy 2.x async engine + `async_sessionmaker` + `get_db` dependency, Alembic migrations (async-engine aware `env.py`, `0001_initial.py` creating the `items` table round-trips clean on upgrade + downgrade), structured JSON logging via `python-json-logger`, `/api/v1/health` liveness probe + `/api/v1/items` CRUD demo, knob-conditional auth module (`jwt` → pyjwt + passlib bcrypt, `oauth2` → authlib, `none` → module not rendered), pytest 8 + pytest-asyncio + httpx `AsyncClient` test harness with isolated in-memory SQLite per-test, multi-stage Dockerfile (builder uses `uv` with `pip` fallback, runtime `python:3.12-slim` + non-root `app:10001` + container-native HEALTHCHECK), docker-compose for local dev (postgres:16-alpine + pg_isready healthcheck), Helm chart (`Chart.yaml` / `values.yaml` with readOnlyRootFilesystem + drop-ALL-capabilities / deployment+service+ingress templates lintable by `helm lint`). **N3 OpenAPI governance integration**: every render ships a `scripts/dump_openapi.py` byte-for-byte compatible with OmniSight's own N3 contract script — offline `create_app().openapi()` dump + `--check` mode that exits 1 on drift + `json.dumps(..., sort_keys=True)` so CI detects real breaking changes without order-noise false-positives. `pyproject.toml` pins `--cov-fail-under=80` (matches `COVERAGE_THRESHOLDS["python"]` in `backend.software_simulator`) so the X1 floor survives the render. `ScaffoldOptions(project_name, package_name=None, database=postgres|sqlite, auth=jwt|oauth2|none, deploy=docker|helm|both, compliance=on|off, platform_profile="linux-x86_64-native")` — `_derive_package_name` slugifies the project name (lowercase, non-alnum → `_`, leading-digit prefix `app_`, empty fallback `app`). `dry_run_build()` constructs the X3 `DockerImageAdapter` + `HelmChartAdapter` against the rendered tree and exercises their `_validate_source` path without running `docker build` / `helm package` (catches "scaffold rendered but Dockerfile missing" class regressions on CI). `pilot_report()` aggregates X0 profile binding + X1 coverage floor + X3 build adapter report + X4 `software_compliance.run_all(ecosystem=None)` bundle into one JSON-safe dict. 57 offline contract tests: registry (7: discoverable / validates-clean / 5 artifact kinds / CORE-05 dep / pilot keywords / validate_pack helper / skill dir) + package slug (6) + option validation (9) + render outcome (9: 33 required files / knob-gated skips / database-specific fingerprints / idempotence / overwrite=False warnings) + rendered code syntactic sanity (4: src tree compileall + conftest + alembic env + dump_openapi) + X0 binding (2) + X1 coverage (3: `--cov-fail-under=80` pin + asyncio_mode + httpx dev dep) + X2 role alignment (4: pydantic_settings usage / no bare `os.environ` in runtime package / async engine + session dep / JsonFormatter) + X3 Docker adapter (2) + X3 Helm adapter (3) + X4 compliance (2: SPDX allowlist denies GPL/AGPL + pilot_report exposes license/cve/sbom gate trio) + N3 governance (4: script shipped / contract flags / references rendered package / dry_run flags openapi presence) + pilot_report shape (2). Adjacent regression: `test_build_adapters` 108 + `test_skill_nextjs` 85 + `test_software_simulator` 55 = 208 tests zero-drift.
- **Software license / CVE / SBOM compliance (X4 #300)**: `backend/software_compliance/` + `scripts/software_compliance.py` + `python3 -m backend.software_compliance` — three offline gates (license allow/deny, CVE scan, SBOM emit) parallel to W5 `web_compliance` but covering every X-series ecosystem. **Multi-ecosystem license scanner** (`licenses.py`) with auto-detect precedence `Cargo.toml` > `go.mod` > `pyproject.toml`/`requirements.txt`/`setup.py`/`setup.cfg` > `package.json` (forceable via `--ecosystem=cargo|go|pip|npm`): cargo (`cargo-license --json` preferred → `Cargo.lock` walk fallback), go (`go-licenses report --template=CSV` → `go.mod` require-block parser), pip (`pip-licenses --format=json --with-system` → `requirements.txt` / `pyproject.toml` / `setup.{py,cfg}` direct-dep parser), npm (`license-checker --json --production` → `node_modules/**/package.json` walk → `package.json` direct deps). Every external CLI optional — missing tool returns `source="mock"` treated as skipped, never a fake pass. SPDX normalisation handles str/dict/list manifest shapes; `_expand_atoms()` cracks compound expressions like `(MIT OR GPL-3.0-or-later)` into atoms and strips `-or-later`/`-only`/`+` suffixes; `_license_matches()` case-insensitive deny-match fails if any atom is denied (same semantics as W5 `web_compliance.spdx`). `DEFAULT_DENY_LICENSES` pins 17 copyleft/non-commercial SPDX ids (GPL 1/2/3, LGPL 2.0/2.1/3.0, AGPL 1/3, SSPL-1.0, CC-BY-NC 1/2/3/4, CC-BY-NC-SA-4.0, CPAL-1.0, EUPL-1.2, OSL-3.0); `--allowlist=name` or `--allowlist=name@license` moves a specific row back to `allowed`; `--deny` can fully override. **CVE scanner** (`cves.py`) probes `trivy` → `grype` → `osv-scanner` in order (forceable via `--cve-scanner=xxx`): trivy (`trivy fs --format json --scanners vuln`, rc=0/1 both valid since trivy exits 1 when findings exist), grype (`grype dir:<path> -o json`), osv-scanner (`osv-scanner --format json -r <path>` with `MODERATE → MEDIUM` severity normalisation). Severity threshold configurable — default `--cve-fail-on=CRITICAL,HIGH` fails the gate; `CRITICAL` alone to loosen or include `MEDIUM` to tighten. Raw findings always preserved regardless of threshold so audit consumers can reapply. **SBOM emitter** (`sbom.py`) ships two formats natively (no syft dependency) — **CycloneDX 1.5 JSON** (`bomFormat/specVersion/serialNumber=urn:uuid:<v4>/metadata.component/components[]` with PURL `pkg:cargo|golang|pypi|npm/<name>@<ver>` + `licenses=[{expression}]`) and **SPDX 2.3 tag-value** (`SPDXVersion/DataLicense: CC0-1.0/DocumentNamespace/PackageName/SPDXID=SPDXRef-Pkg-<idx>-<sanitised>/PackageVersion/ExternalRef PACKAGE-MANAGER purl + Relationship SPDXRef-ROOT DEPENDS_ON`). SPDX IDs auto-sanitised to `[A-Za-z0-9.-]+` ≤255 char. Bundle orchestrator (`bundle.run_all`) composes all three with `GateVerdict={pass,fail,error,skipped}` — `passed` iff no gate is fail/error (skipped non-blocking, same contract as C8 `TestVerdict.skipped`); `sbom` gate is advisory — pass when write succeeds / error when IO fails / never fails the ship. `bundle_to_compliance_report()` bridges to C8 `ComplianceReport(tool_name="x4_software_compliance", protocol=ComplianceProtocol.onvif, metadata={origin:"software_compliance", ecosystem, bundle})` reusing the W5 `onvif` slot trick. CLI `scripts/software_compliance.py --app-path=... [--ecosystem=...] [--allowlist=...] [--deny=...] [--cve-scanner=...] [--cve-fail-on=CRITICAL,HIGH] [--sbom-format={cyclonedx,spdx}] [--sbom-out=./sbom.cdx.json] [--component-name=...] [--component-version=...] [--json-out=...]` emits a single JSON summary and returns exit code 0 (pass) / 1 (fail) / 2 (caller error). **Smoke test**: `scripts/software_compliance.py --app-path=. --sbom-format=spdx --sbom-out=/tmp/x4.spdx` auto-detected the repo as npm, walked 1768 packages in `node_modules`, correctly identified 3 LGPL-3.0-or-later `@img/sharp-libvips-*` packages (real deny hit, not mock), wrote a 21232-line SPDX document, exited 1; adding `--allowlist=@img/sharp-libvips-linux-x64,@img/sharp-libvips-linuxmusl-x64` flipped to exit 0. 75 offline contract tests in `backend/tests/test_software_compliance.py` covering SPDX normalisation (string/dict/list/None) + atom expansion + deny matching + ecosystem auto-detect (5 markers + precedence) + Cargo.lock parser + go.mod block/single require + pip requirements/pyproject parser + package.json direct-deps + node_modules walk (skips `test*`) + cargo-license/pip-licenses/license-checker/go-licenses JSON/CSV parsers (monkey-patched, offline) + tool-failure → walk fallback + trivy/grype/osv payload parsers + severity threshold tightening/loosening + bundle composition (clean passes, dirty fails, sbom write-to-disk, unknown-format errors, to_dict shape) + C8 bridge conversion + CLI exit codes 0/1/2 + allowlist arg parsing — 75/75 in 0.30s; adjacent regression (`test_web_compliance` 46 + `test_build_adapters` 108 + `test_software_simulator` 55 + `test_compliance_harness` 68 = 277 tests) green with zero regression
- **Software build & package adapters (X3 #299)**: `backend/build_adapters.py` + `configs/build_targets.yaml` + `scripts/build_package.py` — single dispatcher with 12 adapters covering 8 native targets (`docker` OCI build + push to GHCR / Docker Hub / ECR / GCR / ACR / private; `helm` chart lint + package; `deb` / `rpm` Linux packages via `fpm` preferred or `dpkg-deb` / `rpmbuild` fallback; `msi` Windows installer via WiX `candle` + `light` two-phase; `nsis` via `makensis` with `-D` injection; `dmg` via `hdiutil` (or `create-dmg`); `pkg` via `pkgbuild` + `productbuild`) and 4 skill-hook adapters wrapping language-native release toolchains (`cargo-dist` Rust multi-platform release; `goreleaser` Go binary + brew tap + checksums with `--snapshot` auto-added when `push=False`; `pyinstaller` Python single-file `--onefile`; `electron-builder` for AppImage / dmg / nsis / msi). Mock-skip contract: `runner_path()` walks `shutil.which()`; missing binary returns `BuildResult(available=False, ok=False)` with `status() == "skip"` (never fakes a pass) so sandbox / CI-first-run images stay testable. Host gating via `TARGET_HOST_REQUIREMENTS` rejects deb/rpm off-Linux and msi/dmg/pkg off-Windows/macOS; `build_matrix()` downgrades `HostMismatchError` to a skip row so multi-target dispatch always returns one result per requested target. Caller-side validation: `normalize_version()` lowercases + replaces `+` → `-` for Docker tags (Docker tag rules forbid `+`) and `-` → `_` for RPM (dash is reserved for the release separator); `validate_artifact_name()` enforces Helm kebab-case + Debian Policy §5.6.7 for deb/rpm + Docker repo grammar with slashes. **X2 role → preferred targets** map (`ROLE_DEFAULT_TARGETS`): backend-python → `[docker, pyinstaller]` / backend-go → `[docker, goreleaser]` / backend-rust → `[docker, cargo-dist]` / backend-node + backend-java → `[docker]` / cli-tooling → `[goreleaser, cargo-dist, pyinstaller]` / desktop-electron → `[electron-builder]` / desktop-tauri → `[cargo-dist]` / desktop-qt → `[deb, rpm, dmg, msi]`. CLI `scripts/build_package.py --list-targets / --target=… / --role=… / --push / --registry={ghcr,dockerhub,ecr,gcr,acr,private} / --registry-arg key=value (repeatable) / --extra key=value (repeatable) / --pretty / --ignore-host-mismatch` emits a single JSON summary; exit code 0 = pass, 1 = fail, 2 = caller error, 3 = all-skip (host has none of the required runners — distinguishes "not applicable here" from "actually broken"). Smoke test: `Dockerfile FROM scratch` + `--target=docker --name=foo --version=1.0.0` produced a real image with `digest=sha256:3a58…6aec` end-to-end. 108 contract tests in `backend/tests/test_build_adapters.py` cover registry enumeration + version + name validation + every adapter's `_validate_source` + skip-when-no-runner + URI composition for all 6 Docker registries + Helm lint warning propagation + fpm / dpkg-deb / rpmbuild dispatch + WiX two-phase + macOS pkg identifier injection + cargo-dist / goreleaser / pyinstaller / electron-builder skill-hook arg composition + role-defaults mapping + YAML/module two-way sync (target id set + kind tag + host_os list) + CLI exit-code coverage — 108/108 in 0.31s; 206/206 adjacent (`test_software_role_skills.py` 75 + `test_software_simulator.py` 55 + `test_software_simulate.py` 8 + `test_platform_default.py` 5 + `test_platform_schema.py` 63) green with zero regression
- **Software role skills (X2 #298)**: `configs/roles/software/{backend-python,backend-go,backend-rust,backend-node,backend-java,cli-tooling,desktop-electron,desktop-tauri,desktop-qt}.skill.md` — 9 new role skills covering 5 backend stacks (FastAPI/Django/Flask · gin/fiber/net-http · axum/actix/rocket · Express/NestJS/Fastify · Spring Boot/Quarkus), cross-language CLI tooling (Cobra/Clap/Commander/Typer/Picocli) and 3 desktop stacks (Electron / Tauri / Qt6 + PySide6). Each role pins its primary X1 simulate-track coverage threshold (Python 80% / Go 70% / Rust 75% / Node 80% / Java 70%) from `backend.software_simulator.COVERAGE_THRESHOLDS`, references at least the `linux-x86_64-native` X0 profile, tells the agent how to invoke `scripts/simulate.sh --type=software`, and ships a 5-section structure (核心職責 / 框架選型矩陣 / 技術棧預設 / 作業流程 / 品質標準 + Anti-patterns + PR 自審). Tool whitelist follows the same W3 rule (no `[all]`, ≤20 tools); keyword sets are disjoint across the 5 backend roles and the 3 desktop roles to keep routing deterministic. Companion `backend/tests/test_software_role_skills.py` contract tests pin all 9 frontmatter shapes + cite-X1-thresholds + cite-X0-profiles + simulate.sh references + keyword routing (75 tests in 0.16s). `backend/prompt_loader.py::_MAX_ROLE_SKILL` raised 6000 → 8000 chars to fit the high-density cli-tooling / desktop-qt skills without silent truncation
- **Software platform profiles (X0 #296)**: `configs/platforms/{linux-x86_64-native,linux-arm64-native,windows-msvc-x64,macos-arm64-native,macos-x64-native}.yaml` — 5 Priority-X software profiles with `target_kind: software` + `software_runtime: native` (language-agnostic at profile level; X2 role skills override per-project to python/node/jvm/go/rust) + OS-native `packaging` defaults (`deb` / `msi` / `dmg`) + `host_arch` / `host_os` dispatch signals. Linux pair kept distinct from `host_native` (embedded escape hatch) and `aarch64` (embedded cross-compile) so X1 simulate-track / X3 package adapter / X4 license scan dispatch cleanly on `kind=software`. macOS pair declares `min_os_version` 14 (Apple Silicon) / 12 (Intel legacy) with shape-only `signing_identity: ""` — Developer ID material injected at build time via the P3 secret_store HSM path mirroring iOS. Windows MSVC profile pins VS 2022 Build Tools 17.0 + Windows SDK 10.0.22621.0. 34 new parametrized unit cases covering enumeration / kind dispatch / validate-clean / host-shape / software_runtime=native / build_cmd non-empty fallback / no-duplicate-silo with host_native & aarch64 / macOS signing shape-only invariant / Windows MSVC pin / Linux docker-packages parity — all green alongside the 29 W0 regression cases (63/63 in `test_platform_schema.py`) and adjacent platform suites (205/205 total across mobile/web/host_native/sdk_discovery)
- **Web vertical skill packs (W6 #280 / W7 #281 / W8 #282)**: `configs/skills/skill-nextjs/` pilot (Next.js 16 App Router + Turbopack root pin), `configs/skills/skill-nuxt/` cross-stack confirmation (Nuxt 4 + Nitro 4-preset: `node-server` / `vercel` / `cloudflare-pages` / `bun` via `NITRO_PRESET` env var), and `configs/skills/skill-astro/` content-vertical consumer (Astro 5 SSG-by-default, Islands architecture with `react` / `vue` / `svelte` hydration knob, MDX content collections with Zod schema, Sanity / Contentful headless-CMS source adapters + webhook verifiers, 4-target: `static` / `@astrojs/node` / `@astrojs/vercel` / `@astrojs/cloudflare` via `ASTRO_TARGET` env var) — all three expose the same `ScaffoldOptions` / `render_project` / `dry_run_deploy` / `pilot_report` public API through `backend/nextjs_scaffolder.py` + `backend/nuxt_scaffolder.py` + `backend/astro_scaffolder.py`, validating the W0-W5 framework at n=3 consumers across stacks (React + Vue + Astro) AND shapes (whole-app SSR + whole-app SSR + content-first SSG) without framework-level changes
- **Shared Headless CMS adapter library (W9 #283)**: `backend/cms/` — unified `CMSSource` ABC + 4 providers (Sanity / Strapi / Contentful / Directus) + `get_cms_source(provider)` factory, with two-verb async interface `fetch(query)` / `webhook_handler(payload)`; provider-specific webhook verification (HMAC-SHA256 for Sanity/Strapi/Directus, shared-secret for Contentful/Directus, bearer-token for Strapi) normalised into a typed `CMSWebhookEvent`, with typed errors (`InvalidCMSTokenError` / `MissingCMSScopeError` / `CMSNotFoundError` / `CMSQueryError` / `CMSRateLimitError` / `CMSSignatureError`) and Fernet-at-rest secret loading through `from_encrypted_token` — 95 unit tests (respx-mocked); lives in the OmniSight backend process (not in generated user sites), so orchestrator / batch-import jobs / future HMI forms can read any headless CMS without spinning up an Astro runtime
- **Web observability & monitoring (W10 #284)**: `backend/observability/` — unified `RUMAdapter` ABC + 2 providers (Sentry envelope intake / Datadog browser RUM `/api/v2/rum`) with three-verb interface `send_vital(WebVital)` / `send_error(ErrorEvent)` / `browser_snippet()`; in-process `CoreWebVitalsAggregator` (rolling window, per-`(metric, page)` bucket with rollup, P50/P75/P95 + good/needs-improvement/poor counts using Google CWV 2024-2026 thresholds where INP replaces FID, threading-safe); `ErrorToIntentRouter` converts browser errors to O5 IntentSource subtasks (JIRA / GitHub Issues / GitLab) with SHA-1 dedup over (release + message + top-frame) and a 24h sliding window — duplicates increment a counter and append a comment to the existing ticket. FastAPI router at `/api/v1/rum/{vitals,errors,dashboard,errors/recent,health}` (unauth beacon endpoints with hard payload caps: 16 KiB vitals / 64 KiB errors). Errors are NEVER sampled (sampling hides regressions); vitals respect `sample_rate`. 184 unit + integration tests (respx-mocked + FastAPI TestClient); browser snippets ship `web-vitals` lib + `navigator.sendBeacon` integration with `include_dsn=False` env-var fallback for CSP-strict deployments
- **Mobile observability & monitoring (P10 #295)**: `backend/mobile_observability/` — unified `MobileObservabilityAdapter` ABC + 2 providers (Firebase Crashlytics for crash / non-fatal / ANR / Performance Monitoring; Sentry Mobile NDJSON envelope for all four surfaces) with four-verb interface `send_crash(MobileCrash)` / `send_hang(HangEvent)` / `send_render(RenderMetric)` / `native_snippet(platform)`; `ANRDetector` server-side classifier (Android ANR 5s/10s warning/critical per Play Vitals; iOS watchdog/hang 250ms/1000ms per Apple `MXDiagnosticPayload`; background ANR suppressed per Google guidance) + `android_anr_snippet` (`ANRWatchDog` vendor-neutral wiring) + `ios_watchdog_snippet` (`MXMetricManagerSubscriber`); in-process `RenderMetricAggregator` (rolling window bucketed by `(metric, platform, screen)` with `*`-screen rollup, P50/P75/P95 + good/needs-improvement/poor counts using frame_draw 16/33 ms = 60 FPS budget + hang 250/1000 ms, threading-safe, 100% shape-symmetric to W10 `backend.observability.vitals` so web+mobile dashboards read the same contract). `HangEvent.severity` is the single source of truth — both Crashlytics and Sentry adapters respect the same `critical` / `warning` / `info` boundaries; critical-level hangs bypass `sample_rate` (sampling hides regressions); crashes are NEVER sampled; render metrics respect sampling (high-frequency). Typed exception hierarchy (`MobileObservabilityError` / `InvalidMobileTokenError` 401 / `MissingMobileScopeError` 403 / `MobilePayloadError` 400 / `MobileRateLimitError` 429) mirrors the W10 layout. DSN / API key loaded via `from_encrypted_dsn(ciphertext)` (plaintext never logged — only `dsn_fingerprint()` last 4 chars). Platform translation table `android→android / ios→cocoa / flutter→dart / react-native→javascript` baked inside the Sentry adapter so consumers only see OmniSight-canonical platform names. Native snippets (4 per provider) wire crash + hang + render collection directly into `Application.onCreate()` / `AppDelegate.application(_:)` / `main()` pre-`runApp` / `index.js` pre-`AppRegistry` — each provider honours its own idiomatic init (`SentryAndroid.init { options -> options.isAnrEnabled = true; options.anrTimeoutIntervalMillis = 5000; options.isEnableFramesTracking = true }` / `SentrySDK.start { options in options.enableWatchdogTerminationTracking = true; options.enableAppHangTracking = true; options.appHangTimeoutInterval = 2.0 }` / `FirebaseCrashlytics.getInstance().setCrashlyticsCollectionEnabled(true)` / `FlutterError.onError = FirebaseCrashlytics.instance.recordFlutterFatalError`). 143 offline contract tests in <1s (respx-mocked vendor HTTP; no network / vendor SDK / emulator required): base 54 + ANR detector 22 + render aggregator 19 + Crashlytics adapter 19 + Sentry adapter 22 + end-to-end integration 7 (ANR detector → adapter.send_hang → respx envelope assertion). Regression: W10 web observability 192 tests + P7/P8/P9 mobile skill 260 tests zero-drift
- **Mobile platform profiles (P0 #285)**: `configs/platforms/{ios-arm64,ios-simulator,android-arm64-v8a,android-armeabi-v7a}.yaml` — 4 Priority-P mobile profiles declaring `sdk_version` (iOS 17.5 / Android 35) / `min_os_version` (iOS 16.0 for StoreKit 2 floor / Android 24 for Play floor) / `toolchain_path` (Xcode 16 clang / NDK r27 per-ABI clang) / `emulator_spec` (structured mapping with `kind: simulator | avd | paired_simulator` discriminator). iOS pair pins SDK/min-OS in lockstep; Android pair shares compile+target+min SDK so one ABI slice can't silently fail Play upload while the other passes. `backend/platform.py::_resolve_mobile` extended from 4 → 12 build_toolchain fields to surface SDK / NDK / toolchain / emulator spec for P1 Docker image and P2 simulate-track consumers. 34 unit tests (parametrized enumeration / validation / resolver + iOS-specific fat-binary + StoreKit 2 floor + Android ABI-pair lockstep + NDK clang API suffix cross-check + .aab output + AVD spec)
- **Cross-platform mobile skill packs (P9 #294)**: `configs/skills/skill-flutter/` + `backend/flutter_scaffolder.py` (primary) and `configs/skills/skill-rn/` + `backend/rn_scaffolder.py` (contrast) — n=3 and n=4 consumers of the P0-P6 mobile framework (after P7 SKILL-IOS pilot + P8 SKILL-ANDROID n=2). FIRST cross-platform packs: one render produces both an iOS-ready and Android-ready project from the same source, and `pilot_report()` aggregates BOTH `ios-arm64` and `android-arm64-v8a` P0 bindings plus `mobile_compliance.run_all(platform="both")` in one shot. **SKILL-FLUTTER** renders Flutter 3.22+ / Dart 3.4+ with Riverpod 2 + `go_router` 7 + Material 3 theme, `firebase_messaging` push (token SHA-256 fingerprinted, never logged raw; `.p8` APNs key server-side), `in_app_purchase` cross-store IAP bridge (server-verify stub returns false by default so no client-side entitlement grant is possible), `analysis_options.yaml` with `avoid_print: error` + `use_build_context_synchronously: error` codifying the P4 flutter-dart role anti-patterns as lint errors, widget tests (`flutter test --coverage`) + integration tests (`flutter test integration_test/` matching the P2 `run_flutter_tests` runner contract), iOS `Podfile`/`Info.plist`/`PrivacyInfo.xcprivacy` pinned to `ios-arm64.yaml`, Android Gradle 8 / Kotlin 2.0 / `minSdk`+`targetSdk` pinned to `android-arm64-v8a.yaml`, Fastlane dual-lane (`ios_beta` via `OMNISIGHT_MACOS_BUILDER` + `android_internal` via `OMNISIGHT_KEYSTORE_*`, both `before_all` fail-fast), both `AppStoreMetadata.json` + `PlayStoreMetadata.json` + `docs/play/data_safety.yaml` green against `mobile_compliance.run_all(platform="both")`. **SKILL-RN** renders React Native 0.76 + TypeScript 5 strict + Hermes + New Architecture (Fabric renderer + TurboModules + `RCT_NEW_ARCH_ENABLED=1` / `newArchEnabled=true`), `@react-native-firebase/messaging` + `react-native-iap` with the same token-fingerprint / server-verify invariants, Zustand store + React Navigation 7, ESLint `no-console: error` codifying the P4 react-native role anti-patterns, Jest + `@testing-library/react-native` unit tests + Detox E2E (`detox test` matching P2 `run_rn_tests`). Single `package_id` knob flows through iOS `CFBundleIdentifier` + Android `applicationId` + both store-submission JSON metadata + Fastlane Appfile — the whole cross-platform promise is one id per product. The Python API is deliberately identical shape (`ScaffoldOptions` / `RenderOutcome` / `render_project` / `pilot_report` / `validate_pack`) across SKILL-IOS / SKILL-ANDROID / SKILL-FLUTTER / SKILL-RN so operators can swap imports without rewriting orchestration glue. Knobs: `project_name` / `package_id` (defaults to `com.example.<lowercased>`) / `push` (default on) / `payments` (default on) / `compliance` (default on). 68 (Flutter) + 69 (RN) = 137 offline contract tests covering registry + scaffold render + dual-rail P0 binding + P2 autodetect (flutter wins over native subdirs, react-native from `package.json` dep) + P3 dual signing chain (iOS `ExportOptions.plist.example` + Android `key.properties.example` env-refs only, `.gitignore` bans `*.jks` / `*.keystore` / `*.p12` / `google-services.json`) + P4 role anti-patterns + P5 dual-store shape + P6 `platform="both"` pass-clean + pilot_report aggregation; all pass in <3.5s combined; 469 mobile-vertical regression suite + 437 adjacent (skill-nextjs / skill-astro / skill-nuxt / codesign_store / app_store_connect / google_play_developer / store_submission / prompt_loader) all green, zero regression
- **Second mobile skill pack (P8 #293)**: `configs/skills/skill-android/` + `backend/android_scaffolder.py` — n=2 consumer of the P0-P6 mobile framework (P7 SKILL-IOS was the pilot). Renders a Jetpack Compose + Kotlin 2.0 + Gradle 8 Android app with `MainActivity` (`ComponentActivity` + `enableEdgeToEdge`), `HomeScreen.kt` (ViewModel + StateFlow + `collectAsStateWithLifecycle` — P4 anti-pattern locked), Material 3 `Theme.kt` with dynamic color on Android 12+, an FCM push template (`FcmMessagingService` + `PushRegistrar` — SHA-256-fingerprinted token logging, raw token never hits logcat, `.json` service-account lives server-side only), a Play Billing 7 template (`BillingClientManager` — `BillingClient` + `PurchasesUpdatedListener` + stub `verifyPurchase` with explicit "REPLACE with server-side Play Developer API call before shipping" TODO so client-side IAP bypass is impossible by default), `BillingScreen` Compose sheet, JUnit4 unit + Espresso/`createAndroidComposeRule` UI test skeletons matching `mobile_simulator.resolve_ui_framework('espresso')`, an AndroidManifest.xml with knob-conditional `POST_NOTIFICATIONS` + `<service>` entries, `keystore.properties.example` holding only `$OMNISIGHT_KEYSTORE_*` env placeholders (P3 HSM path), `app/build.gradle.kts` with `signingConfigs.release` reading the same env (fail-closed if unset), `allWarningsAsErrors=true` + `jvmTarget=17` + Compose plugin Kotlin 2.0, `minSdk` / `targetSdk` / `compileSdk` all pinned to `configs/platforms/android-arm64-v8a.yaml` via a single Jinja variable, Fastlane `Fastfile` (`gradle bundleRelease` / `supply internal` / `supply production` with staged 10% rollout + `before_all` guard on release lanes), `PlayStoreMetadata.json` (P5 `google_play_developer.upload_bundle` shape: package_name / listing / content_rating / target_audience / data_safety_form_path / tracks), `docs/play/data_safety.yaml` (Data Safety form source pre-populated with `declared_sdks`), `gradle-8.7-bin.zip` wrapper pin synced with the P1 Docker image, `.gitignore` banning `keystore.properties` / `*.jks` / `*.keystore` / `*.p12` / `google-services.json` / `*-play-service-account.json`. Knobs: `project_name` (required) / `package_id` (defaults to `com.example.<lowercased>`) / `push` (default on) / `billing` (default on) / `compliance` (default on). `namespace="com.omnisight.pilot"` stays fixed (matches Kotlin source tree); `applicationId` takes the `package_id` knob, supporting whitelabel flavour builds. `pilot_report()` aggregates P0 profile binding + P2 simulate autodetect (`espresso`) + P5 Play metadata sanity + P6 `mobile_compliance.run_all(platform="android")` into one report. 67 offline contract tests across 9 test classes (registry / scaffold render / P0 platform binding / P2 simulate binding / P3 keystore placeholders + Fastfile env gate + .gitignore secret-ban / P4 android-kotlin anti-patterns (no `println` / `print` / `System.out` / `System.err` / `Log.d` / `Log.v` — strip Kotlin comments first; ViewModel + StateFlow usage; no `observeAsState`; `allWarningsAsErrors`; billing `verifyPurchase` wired through `onPurchasesUpdated`) / P5 submission shape / P6 compliance pass-clean + Privacy gate detects firebase-messaging / pilot_report / package-id resolution / toolchain pins); adjacent regression (skill_ios + mobile_compliance + mobile_simulate + mobile_toolchain + platform_mobile_profiles + skill_framework + skill_nextjs, 412 tests) zero-regression
- **Mobile pilot skill pack (P7 #292)**: `configs/skills/skill-ios/` + `backend/ios_scaffolder.py` — first mobile-vertical skill pack; pilot that validates the P0-P6 framework end-to-end (same pattern D1 SKILL-UVC set for C5, D29 SKILL-HMI-WEBUI for C26, and W6 SKILL-NEXTJS for the web vertical). Renders a SwiftUI 6 + Swift 5.9 strict-concurrency project with `App.swift` (`@UIApplicationDelegateAdaptor` adapter), `ContentView.swift` (`@Observable` macro, `.task { … }` pattern, accessibility labels), an APNs push template (`AppDelegate` + `PushNotificationManager` — UNUserNotificationCenter delegate, SHA-256-fingerprinted token logging via `os.Logger`, never bakes the .p8 auth key, forwards raw token only to backend), a StoreKit 2 in-app purchase template (`StoreKitManager` actor — `Product.products` / `purchase` / `Transaction.updates` listener / `verifyResult(VerificationResult)` rejecting `.unverified` per Apple's contract; `StoreView` with sandbox-ready buy sheet; `Configuration.storekit` test plan including consumable + non-consumable + monthly subscription), XCTest + XCUITest skeletons matching `mobile_simulator.resolve_ui_framework('xcuitest')`, an XcodeGen `project.yml` (deterministic .xcodeproj materialisation, no .pbxproj merge conflicts), Common/Signing `xcconfig` (Signing.xcconfig holds `$(OMNISIGHT_CODE_SIGN_IDENTITY)` / `$(OMNISIGHT_PROVISIONING_PROFILE_SPECIFIER)` / `$(OMNISIGHT_DEVELOPMENT_TEAM)` placeholders the P3 codesign chain materialises at build time — never bakes a real cert hash), `IPHONEOS_DEPLOYMENT_TARGET` pinned to `configs/platforms/ios-arm64.yaml` `min_os_version` (16.0 — StoreKit 2 floor) across 4 surfaces (xcconfig + project.yml + Package.swift + Podfile, single source of truth), Fastlane `Fastfile` (gym/pilot/deliver lanes honouring `OMNISIGHT_MACOS_BUILDER` per P1 #286), `AppStoreMetadata.json` (P5 ASC `create_version`-shaped, includes age_rating / uses_idfa / IAP list), `Info.plist` + `App.entitlements` + `PrivacyInfo.xcprivacy` (Apple required-reason API declarations), `Package.swift` SPM manifest + Modules/Feature/ library demonstrating dependency surface, optional `Podfile` for legacy migration. Knobs: `package_manager` (`spm | cocoapods | both`) / `push` (default on) / `storekit` (default on) / `compliance` (default on) / `bundle_id` (defaults to `com.example.<lowercased>`). `pilot_report()` aggregates P0 profile binding + P2 simulate autodetect + P5 ASC metadata sanity + P6 mobile_compliance bundle into one report. 56 offline contract tests across 8 test classes (registry / scaffold render / P0 platform binding (4-surface deployment-target check) / P2 simulate binding / P3 codesign chain (regex-checks no real cert hash or provisioning UUID baked) / P4 ios-swift role anti-patterns (`@Observable` over `ObservableObject` in code, no `print()` in any `.swift`, `os.Logger` wired, SWIFT_STRICT_CONCURRENCY=complete, StoreKit 2 verifyResult guards JWS) / P5 store submission shape / P6 compliance pass-clean + Privacy XML well-formed / pilot_report aggregation / bundle-id resolution); adjacent regression (skill_framework + skill_nextjs + skill_astro + skill_nuxt + mobile_compliance + mobile_simulate + platform_mobile_profiles, 366 tests) green
- **Store compliance gates (P6 #291)**: `backend/mobile_compliance/` — three offline static-scan gates that run before P5 store submission. **App Store Review Guidelines** (`app_store_guidelines.py`) scans `.swift` / `.m` / `.mm` / `.h*` / `.c*` + Info.plist + fastlane metadata for Guideline 3.1.1 (non-Apple IAP + digital-goods twin-signal blocker — `NON_APPLE_PAYMENT_SDK_MARKERS` alone is only a warning), 2.3.10 (misleading marketing — 6 regex rules + `BARE_TITLE_WORDS = {free, lite, beta, test, demo}`), 2.5.1 (curated private-API selector list + `/System/Library/PrivateFrameworks/` `dlopen` detection with `#if DEBUG` suppression + Swift base-name alias matching so `_setBackgroundStyle(x)` still matches the Obj-C `_setBackgroundStyle:` selector), and 5.1.1 (Info.plist `NS*UsageDescription` cross-check against 7 permission-gated APIs); supports `.app-store-review-ignore` with `#` comments and directory-prefix ignore. **Google Play Policy** (`play_policy.py`) parses AndroidManifest.xml + `build.gradle(.kts)` (Groovy and Kotlin DSL via `_TARGET_SDK_RE`); enforces `ACCESS_BACKGROUND_LOCATION` requires `docs/play/background_location_justification.md` + a `ACCESS_FINE_LOCATION` or `ACCESS_COARSE_LOCATION` co-declaration; pins `MIN_TARGET_SDK=35` as the 2026 Play floor (configurable via CLI / API); cross-checks `docs/play/data_safety.yaml.declared_sdks` against Gradle dependencies with `group:artifact` / `group`-prefix fallback. **Privacy label generator** (`privacy_labels.py`) discovers SDKs from Podfile.lock (only inside the `PODS:` section) + `Package.resolved` v2 and v3 + SPM `project.pbxproj` refs + `build.gradle(.kts)`; four-tier matcher (exact → identifier-prefix → subspec `Foo/Bar` → Maven group) against `configs/privacy_label_sdks.yaml` catalogue of 15 curated SDKs (Firebase Analytics/Crashlytics/Messaging, Google Sign-In, Facebook SDK, AdMob, Stripe, Sentry, Mixpanel, Amplitude, Branch, OneSignal, Segment, AppLovin, RevenueCat — each with Apple + Play category mapping, purpose codes, `linked_to_user` + `tracking` flags); emits iOS App Privacy nutrition-label JSON + Play Data Safety YAML, auto-setting `requires_app_tracking_transparency=true` when any matched SDK has `tracking: true`. Bundle orchestrator (`run_all(app_path, platform="ios|android|both", min_target_sdk=35)`) composes all three with `GateVerdict.skipped` semantics for non-applicable gates (iOS-only repo → Play skipped; Android-only → ASC skipped; empty dir → all 3 skipped), so `bundle.passed = no fail/error`. `bundle_to_compliance_report()` bridges into C8's `ComplianceReport(tool_name="p6_mobile_compliance", metadata={"origin": "mobile_compliance", "platform": ..., "bundle": ...})` mirroring W5's "borrow onvif slot" trick so the audit hash-chain + HMI compliance-tools listing pick it up for free. CLI `python3 -m backend.mobile_compliance --app-path=... [--platform=...] [--min-target-sdk=...] [--json-out=...] [--label-ios-out=...] [--data-safety-out=...]` with YAML+JSON extract flags for direct ASC / Play Console upload; exit code mirrors `bundle.passed`. FastAPI router at `/api/v1/mobile-compliance/{gates,run,privacy-label}` — `gates` list (operator) / `run` executes bundle and auto-persists to C8 audit log (admin) / `privacy-label` is a preview-only generate (operator). 99 offline unit + integration tests: ASC gate 21 incl. parametrised misleading-copy + `#if DEBUG` suppression + ignore-file + Info.plist cross-check / Play gate 16 incl. Kotlin DSL + floor-configurable + group-match + background-location justification branch / privacy labels 19 incl. Podfile.lock + Package.resolved v2+v3 + Kotlin DSL + ATT flag / bundle 9 incl. composite iOS+Android / C8 bridge 5 / CLI 5 / router 9 using FastAPI `dependency_overrides` to bypass auth. Adjacent regression (P5 store_submission 31 + ASC/Play clients 58 + W5 web_compliance 60 + C8 compliance_harness 35, 184 total) zero-regression
- **Store submission automation (P5 #290)**: `backend/app_store_connect.py` + `backend/google_play_developer.py` + `backend/store_submission.py` + `backend/internal_distribution.py` — four offline-testable modules that drive App Store Connect / Google Play Developer / TestFlight / Firebase App Distribution without ever dialling the real stores from unit tests. **Transport abstraction** (`HttpTransport` using stdlib `urllib` / `FakeTransport` with FIFO response queue) plus injectable JWT signer (`ES256` for ASC / `RS256` for Play) makes every operation deterministic. **O7 dual-+2 gate** (`backend.store_submission.approve_submission`) reuses `backend.submit_rule` to evaluate the Gerrit vote bundle and returns a `StoreSubmissionContext` that the store clients consume — store-facing targets (App Store review / Play production / Play track update) require Merger +2 **and** Human +2 **and** non-empty release notes **and** an entry in the `CodeSignAuditChain` for the artifact sha; internal targets (TestFlight / Firebase) ship on Merger +2 alone with a distinct `allow_internal_merger_only` reason code. `StoreSubmissionAuditChain` is a hash-chain (`SHA-256(prev || canonical(record))`) mirror of `CodeSignAuditChain` / `MergerVoteAuditChain` so a tamper on any row invalidates every subsequent `curr_hash`. Play client wraps publishing in a `GooglePlayEdit` context manager (open/commit/abort as a transaction) and enforces staged-rollout invariants (`completed` ↔ `user_fraction == 1.0`; `inProgress` ↔ `0 < fraction < 1`; `internal` / `alpha` cannot use `inProgress`). Internal distribution ships a unified `InternalDistributionManager` with a `TesterGroup` dataclass whose `__post_init__` enforces platform-specific fields (iOS → at least one email; Android → Firebase alias) and a `distribute()` router that rejects platform-mismatched groups. 89 offline unit tests (credentials validation / JWT shape / transport auth-header scrubbing / create_version + upload_build + submit_for_review strict-dual-sign + upload_screenshot / edit open-commit-abort / staged-rollout 4 invariants / dual-sign happy + 4 rejection + unknown-artifact + chain tamper / TesterGroup validation + 2-phase TestFlight distribute + Firebase distribute + manager routing); 128 adjacent sibling tests green with zero regression
- **Code-signing chain management (P3 #288)**: `backend/codesign_store.py` — extends `backend/secret_store.py` Fernet at-rest encryption into a full signing-artefact registry. Stores 3 Apple cert kinds (Developer ID / Provisioning Profile / App Store Distribution) + Android per-app keystore (`keystore_bytes` + `alias` + two Fernet-encrypted passwords `keystore_password` / `key_password`) via a file-backed JSON index at `data/codesign_store.json` (mode `0o600`, atomic-rename persist, corrupt-file-survivable). Optional HSM backing (`HSMVendor: none | aws_kms | gcp_kms | yubihsm`) with vendor-specific `key_ref` shape validators — **HSM-backed path stores zero key material locally** (`_encrypt_material(vendor != none, ...) → ""`), and `decrypt_material()` explicitly refuses HSM-backed certs (`"private key never leaves the HSM"`), pinning the "私鑰不出 HSM" invariant. Sign audit via `CodeSignAuditChain` — a hash-chain (`SHA-256(prev || canonical(record))`) mirror of `MergerVoteAuditChain` that records `cert_id` / `cert_fingerprint` / `artifact_path` / `artifact_sha256` / `actor` / `hsm_vendor` / `reason_code` / `ts` per sign, with `verify()` / `head()` / `for_cert()` / `for_artifact()` helpers + fire-and-forget `backend.audit.log_sync` persistence. `attest_sign()` is the signing facade — it does NOT invoke `codesign` / `apksigner` (P5 store-upload scope) but writes the audit entry + refuses expired certs + surfaces an `HSMProvider` handle to the transport layer. Expiry monitoring via `EXPIRY_THRESHOLD_DAYS = (1, 7, 30)` / `severity_for_days()` mapping to `critical` / `warn` / `notice`, with `check_cert_expiries()` (pure) + `fire_expiry_alerts(publisher=…)` (SSE push). Operator CLI `scripts/codesign_manage.py` (`list` / `show` / `expiries` / `audit` / `audit-verify`) — intentionally no `decrypt` subcommand so key material never leaves the module API. 63 unit tests (HSM shape validators incl. govcloud ARN + KMS-alias-rejection invariant / Apple cert registration / Provisioning / Android keystore round-trip / HSM-backed no-material invariant / corrupt-file recovery / hash-chain tamper detection / attest_sign expired-cert refusal + HSM provider surface / expiry three-threshold bucketing + sentinel-string secret-leak check)

### HMI Embedded Web UI Framework (C26 / L4-CORE-26)
- **Constrained generator**: whitelist Preact / lit-html / vanilla JS; inlines CSS + i18n JSON; rejects CDN / analytics / `eval` / inline event attrs
- **Bundle size budget**: per-platform flash-partition-aware (aarch64 512 KiB / armv7 256 KiB / riscv64 1 MiB / host_native 4 MiB) — CI hard-fail via `BudgetExceeded`
- **IEC 62443-4-2 SL2 gate**: CSP directives + required headers + forbidden patterns + inline event attr scan
- **Binding generator**: NL prompt + HAL schema → `fastcgi` / `mongoose` / `civetweb` C handler skeleton + matching JS client
- **Shared components**: network / OTA / logs viewer — reused by D2 IPCam / D8 Router / D9 5G-GW / D17 Industrial-PC / D24 POS / D25 Kiosk
- **i18n pool**: en / zh-TW / ja / zh-CN (extensible via overrides), shared with D-series doc templates
- **ABI matrix**: frozen Chromium/WebKit compatibility table per platform (aarch64 / armv7 / riscv64 / host_native)
- **Pluggable LLM backend**: anthropic (Opus 4.7 Design Tool) / ollama / rule-based — override via `HMI_LLM_PROVIDER` env (falls back to `OMNISIGHT_LLM_PROVIDER`, then rule-based)
- **Endpoints**: 13 REST routes under `/api/v1/hmi/*` (summary, platforms, abi-check, generate, budget-check, security-scan, binding/generate, components/assemble, ...)
- **Simulation**: `scripts/simulate.sh --type=hmi` — generates bundle + runs budget + security gates + optional headless Chromium + QEMU smoke

### DevOps Integration
- **Gerrit**: AI reviewer (patchset-created → auto-review), Code-Review -1 → auto-fix task
- **GitHub/GitLab/Jira**: bidirectional webhook sync (HMAC/token validation, 5s debounce)
- **CI/CD triggers**: GitHub Actions, Jenkins, GitLab CI (fire-and-forget on merge)
- **CODEOWNERS**: file-level ownership enforcement + pre-merge conflict detection

### NPI Lifecycle
- **8 phases**: PRD → EIV → POC → HVT → EVT → DVT → PVT → MP
- **3 tracks**: Engineering, Design, Market (OBM mode)
- **4 business models**: ODM, OEM, JDM, OBM
- **Gantt chart**: horizontal progress bars with Timeline/Gantt toggle

### Multi-Tenancy
- **Tenant isolation**: schema-level (I1), RLS (I2), SSE filter (I3), secrets (I4), filesystem (I5)
- **Sandbox fair-share**: DRF per-tenant capacity (I6) — CAPACITY_MAX=12, guaranteed minimum per tenant, idle borrowing with 30s grace reclaim, turbo cap prevents single-tenant monopoly
- **Rate limiting**: 3-dimension (per-IP + per-user + per-tenant) Redis token bucket (I9) — plan-based quotas (free/starter/pro/enterprise), automatic in-memory fallback
- **Resource hard isolation**: cgroup CPU/mem (M1), per-tenant disk quota + LRU sweep (M2), per-tenant per-key LLM circuit breaker (M3)
- **Per-tenant observability + billing (M4)**: cgroup v2 scraper (`backend/host_metrics.py`) samples every running sandbox by `tenant_id` label → 7 Prometheus metrics (`tenant_cpu_percent`, `tenant_mem_used_gb`, `tenant_disk_used_gb`, `tenant_sandbox_count` + `tenant_cpu_seconds_total` / `tenant_mem_gb_seconds_total` / `tenant_derate_total`); `/host/metrics` REST with admin/user ACL; culprit-aware AIMD (`tenant_aimd.plan_derate()`) derates only the outlier tenant instead of flat host-wide; `scripts/usage_report.py` renders billing-ready text/JSON/CSV
- **Per-tenant egress allowlist (M6)**: DB-backed `tenant_egress_policies` (`allowed_hosts[]` + `allowed_cidrs[]` + `default_action`) replaces the global `OMNISIGHT_T1_EGRESS_ALLOW_HOSTS` env. Sandbox launch path consults per-tenant policy first, falls back to legacy env when DB row missing. `python -m backend.tenant_egress emit-rules` produces a JSON rule plan that `scripts/apply_tenant_egress.sh` materialises as iptables `-m owner --uid-owner <sandbox_uid>` chains. Settings → Network Egress UI lets viewer/operator file `host`/`cidr` requests; admin one-click approve merges into the live policy. Default-deny: empty allow-list → `--network none`

### Reliability & Recovery
- **Token budget**: 3-tier (80% warn → 90% downgrade → 100% freeze) + daily auto-reset
- **Provider failover**: per-tenant per-key circuit breaker (M3) — `(tenant_id, provider, api_key_fingerprint)` triple, 5min cooldown, audit on open/close, `/providers/circuits` REST + Settings UI panel; tenant A's bad key cannot derail tenant B
- **Watchdog**: 30min task timeout, 2hr stuck detection, dynamic reallocation
- **Startup cleanup**: reset stuck agents/simulations, orphan containers, git locks
- **Event persistence**: DLQ with retry (3x exponential backoff), event_log table, replay API
- **Debug blackboard**: cross-agent error tracking, loop detection, /system/debug API

### Hardware Abstraction (Layer A Core)
- **Machine Vision** (C24): GenICam + 4 transport adapters + HW trigger + multi-camera calibration
- **Depth/3D Sensing** (C23): ToF + structured light + stereo SGBM + point cloud + ICP/SLAM
- **Barcode Scanner** (C22): 4 vendor adapters + 16 symbologies + 3 decode modes
- **Motion Control** (C25): G-code interpreter + 3 stepper drivers + PID heaters + thermal runaway safety

### Skill Packs (Layer D)
- **UVC Gadget** (D1): UVC 1.5 descriptor scaffold + gadget-fs binding + UVCH264 payload + USB-CV compliance (pilot skill — validates CORE-05 framework)

### Slash Commands
Type `/` in any input field for autocomplete:

| Category | Commands |
|----------|----------|
| System | `/status` `/info` `/debug` `/logs` `/devices` |
| Dev | `/build` `/test` `/simulate` `/review` `/platform` |
| Agent | `/spawn` `/agents` `/tasks` `/assign` `/invoke` |
| Provider | `/provider` `/switch` `/budget` |
| NPI | `/npi` `/sdks` |
| Tools | `/help` `/clear` `/refresh` |

## Project Structure

```
OmniSight-Productizer/
├── app/                    # Next.js pages + error boundary
├── components/omnisight/   # 18 FUI components
├── hooks/                  # use-engine (SSE + state), use-mobile
├── lib/                    # api.ts, slash-commands.ts, i18n
├── backend/
│   ├── agents/             # graph.py, nodes.py, llm.py, tools.py, state.py
│   ├── routers/            # 14 API routers
│   ├── docker/             # Dockerfile.agent (aarch64 + Valgrind + QEMU)
│   ├── tests/              # 29 test files, 370 tests
│   ├── slash_commands.py   # 22 / command handlers
│   ├── codeowners.py       # File ownership parser
│   ├── notifications.py    # L1-L4 tiered dispatch + DLQ
│   └── ...
├── configs/
│   ├── platforms/          # 18 platform profiles (embedded: aarch64, armv7, riscv64, vendor-example, host_native; web: static, ssr-node, edge-cloudflare, vercel; mobile: ios-arm64, ios-simulator, android-arm64-v8a, android-armeabi-v7a; software: linux-x86_64-native, linux-arm64-native, windows-msvc-x64, macos-arm64-native, macos-x64-native)
│   ├── roles/              # 19 role skill files
│   ├── skills/             # 4 Anthropic-format task skills
│   ├── models/             # 7 LLM model rule files
│   ├── templates/          # 2 Jinja2 report templates
│   ├── CODEOWNERS          # File → agent type mapping
│   └── hardware_manifest.yaml
├── scripts/simulate.sh     # Dual-track simulation runner
├── test_assets/            # Ground truth test data
├── docs/
│   ├── design/             # 10 system design documents
│   └── sop/                # Implementation SOP
└── HANDOFF.md              # Complete project state document
```

## Development Phases

28 phases completed (Phase 1-29), covering:
- Core infrastructure (Phase 1-12)
- NPI + Artifact + Simulation (Phase 13-15)
- Error recovery + Conversational AI (Phase 16-19)
- Multi-agent patterns: 5 patterns at 80-98% coverage (Phase 20-24)
- Provider UI + Webhooks + Handoff viz + NPI Gantt (Phase 25-27)
- SoC SDK integration + Slash commands (Phase 28-29)

See [HANDOFF.md](HANDOFF.md) for detailed phase history and future roadmap.

## Prewarm Pool Multi-Tenant Safety (M5)

Speculative Tier-1 container pre-warm (Phase 67-C) is now tenant-scoped. The policy is controlled by `OMNISIGHT_PREWARM_POLICY`:

| Policy | Behavior | When to pick |
|---|---|---|
| `per_tenant` (default) | Each tenant gets its own pre-warm bucket (depth 1-2 per bucket). Tenant A's pre-warmed container can never be consumed by tenant B. | SaaS / multi-tenant — always. |
| `shared` | Single global bucket (legacy Phase 67-C behavior). Faster fan-out but cross-tenant filesystem residue risk. Emits a startup warning. | Single-tenant or fully-trusted deployments. |
| `disabled` | Pre-warm entirely off. Trade 300 ms cold-start for zero speculative-container state. | High-security customers (compliance / audit). |

Regardless of policy, every `consume()` force-clears the tenant's `/tmp/omnisight_ingest/<tid>/` namespace before handing the container to the real task — so no speculative scratch-file residue ever leaks into a real workspace. Cleanup failures are logged but never void a valid pre-warm hit.

Pre-warm itself remains opt-in via `OMNISIGHT_PREWARM_ENABLED=true`; `policy=disabled` takes precedence when both are set.

## Per-tenant Egress Allowlist (M6)

Tier-1 sandbox egress is now controlled per tenant through DB-backed policy plus an admin approval workflow. The legacy global `OMNISIGHT_T1_EGRESS_ALLOW_HOSTS` env is auto-migrated into `t-default` on first boot and remains a fallback when no DB row exists.

| Layer | What happens |
|---|---|
| `tenant_egress_policies` table | One row per tenant: `allowed_hosts[]`, `allowed_cidrs[]`, `default_action` (`deny` recommended). DB is the source of truth. |
| Sandbox launch | `start_container` resolves `tenant_id` → `sandbox_net.resolve_network_arg(tenant_id=…)` consults the policy. Empty allow-list = `--network none` (full air-gap). Any allowed host/CIDR opens the bridge. |
| Iptables installer | Operator runs `sudo scripts/apply_tenant_egress.sh --tenant <tid> --uid <sandbox_uid>` (or `--all`). The script reads the JSON rule plan from `python -m backend.tenant_egress emit-rules` and installs an `OMNISIGHT-EGRESS-<tid>` chain hooked into `OUTPUT -m owner --uid-owner <uid>`, with a terminal `DROP` (when `default_action=deny`). |
| Approval workflow | Viewer/operator file additions via the Settings → Network Egress UI. The request lands as `pending` in `tenant_egress_requests`. Admin clicks `approve` (or `reject`); approval merges the value into the live policy and audits the decision. |
| Audit chain | `tenant_egress.upsert`, `request_submit`, `request_approve`, `request_reject` all enter the per-tenant `audit_log` hash chain — answers "who allowed `evil.com` for tenant X" via a single `audit.query(entity_kind='tenant_egress')`. |

Operators wanting to keep the pre-M6 single-tenant flow do nothing — the legacy env still works and the t-default policy gets a `legacy-migration` audit row on first upgrade.

## Dependency Governance (N1)

Dependencies are fully locked and every lockfile drift fails CI before the rest of the pipeline runs. Use this section when onboarding or upgrading.

| Layer | Tool / file | What changes through it |
|---|---|---|
| Node version | `.nvmrc` + `.node-version` (both `20.17.0`) + `package.json` `engines.node` `>=20.17.0 <21` | `nvm use` / `fnm use` / `asdf install` / Volta / `actions/setup-node@v4` `node-version-file: .nvmrc` all resolve to the same version |
| JS package manager | `package.json` `packageManager: pnpm@9.15.4` + `engines.pnpm: >=9` | Node 20's built-in `corepack` downloads and pins pnpm automatically — contributors don't need a global install |
| JS dependency graph | `pnpm-lock.yaml` (canonical, committed) | `pnpm install --frozen-lockfile` everywhere (local dev, Dockerfile.frontend, CI, release). `package-lock.json` and `yarn.lock` are both `.gitignore`d and the CI drift gate rejects any stray copy |
| Python ranges | `backend/requirements.in` (human-readable) | Edit this file to add/remove/bump a package, then regenerate |
| Python lock | `backend/requirements.txt` (pip-compile output with `--generate-hashes`) | Regenerate with `pip-compile --generate-hashes backend/requirements.in` — every pin carries at least one `sha256:` hash |
| Python install | `pip install --require-hashes -r backend/requirements.txt` | Used in `Dockerfile.backend`, `scripts/deploy.sh`, and every CI job. A missing/mismatched hash aborts install |
| CI drift gate | `.github/workflows/ci.yml` `lockfile-drift` job | Runs first. Rejects stray `package-lock.json`/`yarn.lock`, re-runs `pnpm install --frozen-lockfile` + `pip-compile`, fails the build if anything diffs |

**Typical flows:**

```bash
# Add a Python dep
echo "somelib==1.2.3" >> backend/requirements.in
pip-compile --generate-hashes backend/requirements.in
git add backend/requirements.in backend/requirements.txt

# Bump a JS dep
pnpm update some-package
git add package.json pnpm-lock.yaml
```

If CI fails with `Lockfile drift detected`, re-run the corresponding regenerate command locally, commit the lock, push.

### Renovate auto-PRs (N2)

[Renovate](https://docs.renovatebot.com/) opens dependency PRs every weekend (`Asia/Taipei`) per the policy in [`renovate.json`](renovate.json). Full SOP — group rules, tiered auto-merge, vulnerability handling, operator bootstrap — lives in [`docs/ops/renovate_policy.md`](docs/ops/renovate_policy.md).

| Update type | Auto-merge? | Reviewers | Notes |
|---|---|---|---|
| CVE / vulnerability | yes (CI green) | none | Opens immediately, jumps the queue (`prPriority: 100`) |
| patch / pin / digest | yes (CI green) | none | 3-day upstream wait |
| minor | no | 1 (CODEOWNERS) | 5-day wait |
| major | no — never | 2 + G3 blue-green | 14-day wait, label `deploy/blue-green-required` |

Group rules keep peer-coupled families together so the lockfile stays internally consistent: `@radix-ui/*`, `@ai-sdk/*`, `langchain*`/`langgraph` (Python), `@types/*`, GitHub Actions, and Docker base images each merge as one PR.

CI validates `renovate.json` against the Renovate JSON schema in the `renovate-config` job before any other gate runs — typos cannot silently disable the bot.

### LangChain / LangGraph adapter firewall (N4)

All `langchain*` and `langgraph*` imports are funneled through a single module, [`backend/llm_adapter.py`](backend/llm_adapter.py). Every other backend module imports message classes, graph primitives, and provider factories from the adapter — never from LangChain directly.

Why: LangChain ships breaking changes at patch cadence (message shapes, tool-call formats, provider arg names). Keeping the surface contained to one file means a LangChain upgrade is a one-file change plus running the adapter test suite, not an 8-file sweep across agents/, routers/, and tests.

The adapter exposes a stable, version-decoupled API:

| Symbol | Purpose |
|---|---|
| `invoke_chat(messages, ...)` | Single synchronous chat turn → text |
| `stream_chat(messages, ...)` | Async iterator of text chunks |
| `tool_call(messages, tools, ...)` | Chat with tools bound → normalized `AdapterToolResponse` |
| `embed(texts, ...)` | Provider-agnostic embeddings (OpenAI + Ollama) |
| `build_chat_model(provider, ...)` | Provider factory (only `agents.llm` should use directly) |
| `HumanMessage` / `AIMessage` / `SystemMessage` / … | Re-exported LangChain message primitives |
| `StateGraph` / `END` / `add_messages` | Re-exported LangGraph primitives |
| `tool` | Re-exported `@tool` decorator |

A CI gate (`llm-adapter-firewall` job, runs in parallel with `lint`) enforces the firewall by scanning every `backend/**/*.py` for forbidden imports using stdlib `ast`. The script — [`scripts/check_llm_adapter_firewall.py`](scripts/check_llm_adapter_firewall.py) — exits non-zero with `::error file=...,line=...::` annotations if any file other than `backend/llm_adapter.py` (plus its own test file) imports from `langchain*` or `langgraph*`. Upgrades follow this workflow:

1. Bump the `langchain-*` / `langgraph` pin in `backend/requirements.in`.
2. Regenerate `backend/requirements.txt` via `pip-compile --generate-hashes`.
3. Run `pytest backend/tests/test_llm_adapter.py` — 50 tests cover all public symbols.
4. If any test fails, the fix is isolated to `backend/llm_adapter.py`; no other file needs changes.

### Framework fallback branches (N9)

Two long-running branches stand permanent guard over the framework rollback path: `compat/nextjs-15` (held one major behind master's Next 16) and `compat/pydantic-v2` (pre-emptive — declared today even though Pydantic v3 has not shipped yet). The branches are declared in [`.fallback/manifests/*.toml`](./.fallback/) — a single TOML per branch carries the pin, the `freshness_days` window, and the `skip_globs` that the rebase tool uses to filter master commits when keeping the fallback evergreen.

Three CI surfaces enforce the policy: ([`fallback-branches.yml`](.github/workflows/fallback-branches.yml)) re-builds + runs core tests on every `compat/**` push and on a Sunday-night cron, certifying the fallback as deployable; ([`major-upgrade-gate.yml`](.github/workflows/major-upgrade-gate.yml)) blocks any `tier/major` PR that bumps `next` or `pydantic` until the corresponding fallback branch shows a green CI run within its freshness window (defaults to 14 days); and the carve-outs in [`renovate.json`](renovate.json) prevent Renovate from bumping the pinned framework on its own fallback (which would defeat the entire point), while still flowing security patches and unrelated minor bumps so the branch doesn't rot.

Operator tooling stays stdlib-only for the same self-defense reason as N5/N6/N7/N8: when a major framework upgrade is what just exploded production, the rollback tools must not depend on the framework that's broken. [`scripts/fallback_setup.sh`](scripts/fallback_setup.sh) materialises the branches locally (one-shot), [`scripts/fallback_rebase.py`](scripts/fallback_rebase.py) plans + applies the weekly cherry-pick of non-framework commits (refuses to auto-split commits that straddle safe + skip paths), and [`scripts/check_fallback_freshness.py`](scripts/check_fallback_freshness.py) is the gate's GH Actions API probe. Full lifecycle, retirement criteria, and per-incident playbook in [`docs/ops/fallback_branches.md`](docs/ops/fallback_branches.md); production rollback path is wired into [`docs/ops/dependency_upgrade_runbook.md`](docs/ops/dependency_upgrade_runbook.md) Phase 4.5 ("Path C — Fallback-branch rollback").

### DB engine compatibility matrix (N8)

A dedicated workflow ([`.github/workflows/db-engine-matrix.yml`](.github/workflows/db-engine-matrix.yml)) exercises every committed Alembic migration against two engines ahead of the G4 Postgres cutover. **Hard gate:** SQLite 3.40.1 + 3.45.3 (the floor and ceiling of what production has ever run). **Advisory:** Postgres 15 + 16 (red-X by design today — the baseline migrations use SQLite-only idioms; the cells go hard-gate after G4 ports the SQL). **Advisory:** an engine-specific SQL linter ([`scripts/check_migration_syntax.py`](scripts/check_migration_syntax.py)) emits `::warning ...` annotations for every `AUTOINCREMENT`, `datetime('now')`, `INSERT OR IGNORE`, `CREATE VIRTUAL TABLE USING fts5`, `PRAGMA`, `BEGIN IMMEDIATE`, and related SQLite idiom it finds in migration files.

The dual-track validator ([`scripts/alembic_dual_track.py`](scripts/alembic_dual_track.py)) runs `upgrade head → step-down to revision 0001 → re-upgrade head`, then diff-checks the two schema fingerprints. An asymmetric up/down pair fails the cell. Already earned its keep on day one: caught a latent SQLAlchemy-2.x bug in migration 0014 where `conn.execute("SELECT …")` needs a `text()` wrapper. SQLite versions are pinned deterministically via `LD_PRELOAD` of a source-built `libsqlite3.so` (cached across runs); the workflow asserts `sqlite3.sqlite_version` matches the matrix pin before running migrations. Full SOP + G4 handoff plan in [`docs/ops/db_matrix.md`](docs/ops/db_matrix.md).

### Multi-version CI matrix (N7)

A nightly workflow ([`.github/workflows/multi-version-matrix.yml`](.github/workflows/multi-version-matrix.yml)) exercises the test suite against the **next** versions of every interpreter and the FastAPI minor stream — Python 3.12 (gate) + 3.13 (advisory), Node 20.x (gate) + 22.x (advisory), FastAPI pinned (gate) + latest minor (advisory). PRs continue to run only the gate cells via [`ci.yml`](.github/workflows/ci.yml), so PR latency is unchanged; advisory cells use `continue-on-error: true` so a deprecation in Python 3.13 cannot red-X a green run. The matrix's job is to surface what the next upgrade will require *before* it lands.

Every advisory cell pipes its captured pytest / vitest / tsc log through [`scripts/surface_deprecations.py`](scripts/surface_deprecations.py), which (a) emits one `::warning ...` GitHub Actions annotation per unique deprecation message — capped at 30 so a runaway log can't flood the run sidebar — and (b) appends a deduplicated count-by-message table to the per-job `GITHUB_STEP_SUMMARY`. The script is stdlib-only for the same self-defense reason `upgrade_preview.py` (N5) and `check_eol.py` (N6) are: it cannot itself be broken by the dep upgrade it summarises. Full SOP — when to act on a red advisory cell, how each install command differs from PR — lives in [`docs/ops/ci_matrix.md`](docs/ops/ci_matrix.md).

### Nightly upgrade preview (N5)

Every night at 01:00 Asia/Taipei a separate workflow ([`.github/workflows/upgrade-preview.yml`](.github/workflows/upgrade-preview.yml)) trial-upgrades the lockfiles in a fresh GitHub runner, installs the upgraded deps, runs the full backend pytest suite + Chromium Playwright suite against them, and posts a single open issue tagged `dependency-preview` with: outdated tables, suspected-breaking callouts, lockfile diffs (truncated), and the tail logs. Operators read the issue on Monday morning to decide whether to let the weekend Renovate batch land, pin a package, or coordinate a blue-green deploy. The preview never auto-merges and never mutates committed files — full SOP in [`docs/ops/upgrade_preview.md`](docs/ops/upgrade_preview.md).

The "suspected breaking" classifier in [`scripts/upgrade_preview.py`](scripts/upgrade_preview.py) flags major bumps, 0.x minor bumps (pre-1.0 SemVer convention), and any change to a hand-curated watchlist of strategic packages (`langchain*`, `pydantic`, `next`, `react`, `@radix-ui/*`, `@ai-sdk/*`, `playwright`, `vitest`, …). The renderer is stdlib-only so it survives the very dep break it is trying to forecast.

### Upgrade cadence + blue-green gate (N10)

The authoritative cadence contract lives in [`docs/ops/dependency_upgrade_policy.md`](docs/ops/dependency_upgrade_policy.md): **patch** ships weekly (CI-green auto-merge), **minor** ships bi-weekly (1 reviewer, 5-day soak), **major** ships **quarterly** (2 reviewers, 14-day soak) and is physically gated to the G3 blue-green deploy path — standby upgrade → smoke test → traffic cut-over → old version held hot for 24 h. One package per PR (or one N2 peer-coupled group) so a failing major is always `git revert`-able in one shot.

Two gates couple the policy to the repo:
* [`.github/workflows/blue-green-gate.yml`](.github/workflows/blue-green-gate.yml) runs on every PR and (a) auto-applies a sticky `requires-blue-green` label when [`scripts/bluegreen_label_decider.py`](scripts/bluegreen_label_decider.py) detects a Renovate major label, a `Update <pkg> to v<N>` title, or a hand-authored major bump in `package.json` / `backend/requirements.in` / `.nvmrc` / `.node-version`; and (b) exposes `N10 / blue-green-label` as a required status check that stays red until the PR body carries the full ceremony checklist (standby / smoke / cut-over / 24h), checked by [`scripts/bluegreen_pr_gate.py`](scripts/bluegreen_pr_gate.py). A manual waiver via `deploy/bluegreen-waived` is honoured but audit-logged.
* [`scripts/check_bluegreen_gate.py`](scripts/check_bluegreen_gate.py) is called from `scripts/deploy.sh` on every **prod** deploy. It looks up the PR that introduced the target ref, checks for the sticky label, and refuses the deploy (exit 2) unless [`docs/ops/upgrade_rollback_ledger.md`](docs/ops/upgrade_rollback_ledger.md) has a matching `shipped` / `rolled-back` / `waived` entry. The ledger is append-only and powers the quarterly policy review (opens a `policy-review` issue if rollback rate exceeds 25 % or mean soak falls below 24 h).

## Theme

The UI is deliberately **dark-only** — the "FUI" (fictional user interface)
language (neural-grid, holo-glass, deep-space gradients, scan-lines) is
designed around a dark canvas. There is no light-mode toggle. A
`color-scheme: dark` declaration is set at the root so browsers render
native controls, scrollbars and autofill in the dark palette even when
the host OS is configured for light mode. Users who prefer a light UI
should use a different tool — this is a mission-control dashboard, not a
documentation site.

Motion preferences are honoured: `prefers-reduced-motion: reduce`
disables the neural-flow animation, toast urgency pulses, and all
tween transitions.

## License

Proprietary. All rights reserved.
