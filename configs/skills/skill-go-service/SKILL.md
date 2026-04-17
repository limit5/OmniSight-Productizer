# SKILL-GO-SERVICE — X6 #302

Second software-vertical skill pack. Follows the X5 SKILL-FASTAPI
pattern but swaps the runtime to Go 1.22+ so the X0-X4 framework is
re-exercised on a second language toolchain (modules / go test /
goreleaser) instead of pip / pytest / pyinstaller.

## Why this skill exists

Priority X built five scaffolding layers — platform profile schema
(X0), software simulate-track (X1), role skills (X2), build & package
adapters (X3), and license / CVE / SBOM compliance (X4). The FastAPI
pilot (X5) proved those layers hold on Python. X6 is the first
non-Python consumer: it proves the framework is language-agnostic
and wires the **goreleaser** adapter (new in X3) into a real
end-to-end path.

## Outputs

A rendered Go project tree that:

- builds with `go build ./cmd/server` and serves
  `/api/v1/health` + `/api/v1/items` over Gin (or Fiber, knob-selected)
- passes `go test -race -cover ./...` at ≥ 70% coverage — matches
  `COVERAGE_THRESHOLDS["go"]` in `backend.software_simulator`
- builds a multi-stage scratch Docker image via
  `backend.build_adapters.DockerImageAdapter`
- packages a Helm chart via
  `backend.build_adapters.HelmChartAdapter`
- emits a `.goreleaser.yaml` that `goreleaser check` accepts, consumed
  by `backend.build_adapters.GoreleaserAdapter` for Linux / Darwin /
  Windows × amd64 / arm64 release archives
- passes the three X4 compliance gates (SPDX license / CVE scan / SBOM
  emit — `go` ecosystem via `go-licenses` + `go.mod` fallback)

## Choice knobs

| Knob            | Values                       | Default              |
|-----------------|------------------------------|----------------------|
| `module_path`   | go module path              | `github.com/example/<slug>` |
| `framework`     | `gin` \| `fiber`            | `gin`                |
| `database`      | `postgres` \| `sqlite` \| `none` | `postgres`       |
| `deploy`        | `docker` \| `helm` \| `both` | `both`               |
| `compliance`    | `on` \| `off`                | `on`                 |

See `configs/skills/skill-go-service/tasks.yaml` for the DAG each knob
routes through.

## How to render

```python
from pathlib import Path
from backend.go_service_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/my-service"),
    options=ScaffoldOptions(
        project_name="my-service",
        module_path="github.com/acme/my-service",
        framework="gin",
        database="postgres",
        deploy="both",
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

## goreleaser wiring

The rendered `.goreleaser.yaml` targets Linux / Darwin / Windows on
amd64 + arm64 with `CGO_ENABLED=0` and `-ldflags "-s -w"` for small
static binaries, matching the backend-go role skill's "API server
≤ 25 MiB" budget. The X3 `GoreleaserAdapter` validates the config
via `goreleaser check` and produces archives via `goreleaser release
--snapshot --clean` when `push=False`.
