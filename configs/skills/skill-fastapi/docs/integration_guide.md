# SKILL-FASTAPI Integration Guide

X5 #301. Produced by the pilot skill that validates the X0-X4 framework.

## Render a project

```python
from pathlib import Path
from backend.fastapi_scaffolder import ScaffoldOptions, render_project

outcome = render_project(
    out_dir=Path("/tmp/my-service"),
    options=ScaffoldOptions(
        project_name="my-service",
        database="postgres",   # or "sqlite"
        auth="jwt",            # or "oauth2" / "none"
        deploy="both",         # docker + helm
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

## Output tree

```
my-service/
├── pyproject.toml          (fastapi / uvicorn / sqlalchemy / alembic / pydantic-settings pinned)
├── Dockerfile              (multi-stage: uv builder → python:3.12-slim runtime)
├── docker-compose.yml      (service + postgres for local dev)
├── alembic.ini
├── Makefile
├── .env.example
├── spdx.allowlist.json     (X4 compliance — denies GPL/AGPL by default)
├── src/
│   └── <package>/
│       ├── __init__.py
│       ├── main.py         (FastAPI app factory + lifespan)
│       ├── config.py       (pydantic-settings — no direct os.environ reads)
│       ├── db.py           (SQLAlchemy 2.x async engine + session dep)
│       ├── models.py
│       ├── schemas.py
│       ├── api/
│       │   ├── __init__.py
│       │   └── v1/
│       │       ├── __init__.py
│       │       ├── health.py
│       │       └── items.py
│       └── core/
│           ├── __init__.py
│           ├── logging.py  (structured JSON formatter)
│           └── security.py (only when auth != "none")
├── alembic/
│   ├── env.py              (async-engine aware)
│   ├── script.py.mako
│   └── versions/0001_initial.py
├── tests/
│   ├── conftest.py         (TestClient + AsyncClient + isolated DB)
│   ├── test_health.py
│   └── test_items.py
├── deploy/
│   └── helm/
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/{deployment,service,ingress}.yaml
└── scripts/
    └── dump_openapi.py     (N3 governance-compatible)
```

## Quick start (after render)

```bash
# Install + migrate + run locally
uv sync                         # or pip install -e .[dev]
alembic upgrade head
uvicorn <package>.main:app --reload

# Test + coverage (X1 simulate-track compatible)
pytest --cov=src --cov-fail-under=80

# OpenAPI contract gate (N3)
python scripts/dump_openapi.py        # writes ./openapi.json
python scripts/dump_openapi.py --check   # fails on drift

# Container + chart (X3 adapters)
docker compose up -d            # local dev
docker build -t my-service:0.1.0 .

# X4 compliance
python -m backend.software_compliance --app-path .
```

## Framework gates validated

| X-series | What the scaffold exercises                                    |
|----------|----------------------------------------------------------------|
| X0       | `linux-x86_64-native` profile (`target_kind=software`)         |
| X1       | pytest + `--cov-fail-under=80` (matches `COVERAGE_THRESHOLDS["python"]`) |
| X2       | backend-python role anti-patterns (pydantic-settings, async routes, structured logging) |
| X3       | `DockerImageAdapter` + `HelmChartAdapter` (both resolve against rendered artifacts) |
| X4       | SPDX allowlist + CVE scan + SBOM via `backend.software_compliance` |

## N3 OpenAPI governance

The per-project `scripts/dump_openapi.py` is a byte-for-byte contract
copy of OmniSight's own script. Wire the project's CI like this:

```yaml
- name: openapi-contract
  run: |
    python scripts/dump_openapi.py --check
```

Any Pydantic model or route change that hasn't been committed as
`openapi.json` fails the build, so API-breaking changes are caught
at PR time.
