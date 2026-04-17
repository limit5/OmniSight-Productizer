# SKILL-FASTAPI — X5 #301 (pilot)

First software-vertical skill pack. Generates a FastAPI 0.110+ project
that exercises every X0-X4 capability the framework ships.

## Why this skill exists

Priority X built five scaffolding layers — platform profile schema (X0),
software simulate-track (X1), role skills (X2), build & package
adapters (X3), and license / CVE / SBOM compliance (X4). On their own
those layers are a framework; they become load-bearing only after a
real skill pack consumes every one of them. SKILL-FASTAPI is that
pack — same pilot pattern D1 set for C5, D29 for C26, and W6 for
W0-W5.

It is also a **dogfood**: OmniSight's own backend is FastAPI, so the
same stack that runs the product also powers every generated customer
service. Anything that breaks the scaffolded project breaks our
own backend first — the cheapest possible feedback loop.

## Outputs

A rendered project tree that:

- boots with `uvicorn <pkg>.main:app --reload` and serves
  `/api/v1/health` + `/api/v1/items`
- passes `scripts/simulate.sh --type=software --module=linux-x86_64-native --software-app-path=.` at ≥ 80% coverage
- passes `scripts/dump_openapi.py --check` (same N3 contract gate OmniSight uses)
- builds a multi-stage Docker image via `backend.build_adapters.DockerImageAdapter`
- packages a Helm chart via `backend.build_adapters.HelmChartAdapter`
- passes the three X4 compliance gates (SPDX license / CVE scan / SBOM emit)

## Choice knobs

| Knob           | Values                       | Default              |
|----------------|------------------------------|----------------------|
| `package_name` | python identifier            | slugified project    |
| `database`     | `sqlite` \| `postgres`       | `postgres`           |
| `auth`         | `jwt` \| `oauth2` \| `none`  | `jwt`                |
| `deploy`       | `docker` \| `helm` \| `both` | `both`               |
| `compliance`   | `on` \| `off`                | `on`                 |

See `configs/skills/skill-fastapi/tasks.yaml` for the DAG that each
knob routes through.

## How to render

```python
from pathlib import Path
from backend.fastapi_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/my-service"),
    options=ScaffoldOptions(
        project_name="my-service",
        database="postgres",
        auth="jwt",
        deploy="both",
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

## N3 OpenAPI governance integration

The scaffold ships a per-project `scripts/dump_openapi.py` whose
contract is byte-for-byte compatible with OmniSight's own script
(offline `app.openapi()` dump, `--check` mode exits non-zero on
drift). Wire it into the project's CI `openapi-contract` job exactly
like OmniSight does — drift fails the build, so API-breaking
changes are gated at PR time, not at client-integration time.
