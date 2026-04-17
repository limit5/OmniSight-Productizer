# SKILL-GO-SERVICE Integration Guide

X6 #302. Second software-vertical skill pack — validates that the
X0-X4 framework holds on Go 1.22+ after X5 SKILL-FASTAPI (#301)
proved it on Python 3.11+.

## Render a project

```python
from pathlib import Path
from backend.go_service_scaffolder import ScaffoldOptions, render_project

outcome = render_project(
    out_dir=Path("/tmp/my-service"),
    options=ScaffoldOptions(
        project_name="my-service",
        module_path="github.com/acme/my-service",
        framework="gin",             # or "fiber"
        database="postgres",         # or "sqlite" / "none"
        deploy="both",               # docker + helm
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

## Output tree

```
my-service/
├── go.mod                    (module path + pinned deps)
├── go.sum                    (empty placeholder — populated by first `go mod tidy`)
├── Dockerfile                (multi-stage: golang:1.22-alpine → distroless/static)
├── docker-compose.yml        (service + optional postgres for local dev)
├── .goreleaser.yaml          (Linux/Darwin/Windows × amd64/arm64 release)
├── Makefile
├── .env.example
├── .gitignore
├── .golangci.yml             (gosec / staticcheck / errcheck / revive)
├── spdx.allowlist.json       (X4 compliance — denies GPL/AGPL by default)
├── cmd/
│   └── server/
│       └── main.go           (HTTP server bootstrap + graceful shutdown)
├── internal/
│   ├── api/
│   │   ├── router.go         (framework-specific engine wiring)
│   │   ├── health.go         (liveness + readiness)
│   │   ├── items.go          (sample CRUD)
│   │   ├── health_test.go
│   │   └── items_test.go
│   ├── config/
│   │   └── config.go         (envconfig — no scattered os.Getenv)
│   ├── db/
│   │   └── db.go             (pgx pool / sqlite handle / no-op)
│   └── logging/
│       └── logging.go        (log/slog JSON handler)
├── deploy/
│   └── helm/
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/{deployment,service,ingress}.yaml
└── scripts/
    └── check_cov.sh          (go test -cover + 70% floor)
```

## Quick start (after render)

```bash
go mod tidy                    # populate go.sum
make test                      # go test -race -cover + 70% floor
make run                       # go run ./cmd/server
make docker                    # docker build
make helm                      # helm lint + package
make release-snapshot          # goreleaser release --snapshot --clean
```

## Framework gates validated

| X-series | What the scaffold exercises                                             |
|----------|-------------------------------------------------------------------------|
| X0       | `linux-x86_64-native` profile (`target_kind=software`)                  |
| X1       | `go test -race -cover ./...` + 70% floor (COVERAGE_THRESHOLDS["go"])    |
| X2       | backend-go role anti-patterns (log/slog, envconfig, no bare os.Getenv)  |
| X3       | `DockerImageAdapter` + `HelmChartAdapter` + `GoreleaserAdapter`         |
| X4       | SPDX allowlist + CVE scan + SBOM via `backend.software_compliance`      |

## goreleaser

`.goreleaser.yaml` targets Linux/Darwin/Windows × amd64/arm64 with
`CGO_ENABLED=0` and `-ldflags "-s -w"`. The X3 `GoreleaserAdapter`
runs `goreleaser release --snapshot --clean` when `push=False`, or
`goreleaser release --clean` when `push=True` and a release tag is
present. `goreleaser check` validates the config offline and is wired
into `make release-check`.
