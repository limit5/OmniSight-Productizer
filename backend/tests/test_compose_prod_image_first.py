"""L10 #337 (item 2) — contract tests for image-first prod compose.

Pins the load-bearing invariants of `docker-compose.prod.yml`:
    1. Backend + frontend both declare an `image:` referencing GHCR
       (registry-first), parameterised by `OMNISIGHT_GHCR_NAMESPACE` +
       `OMNISIGHT_IMAGE_TAG`.
    2. Both also keep their `build:` block so Compose can transparently
       fall back to a local build when the registry image isn't
       available — that is the documented native Compose behaviour
       when both keys are set on the same service.
    3. Image names match what `.github/workflows/docker-publish.yml`
       publishes (`omnisight-backend`, `omnisight-frontend`).
    4. Defaults are placeholders (`your-org`, `latest`) so the
       fallback-to-build path kicks in cleanly on first deploy without
       requiring operators to configure GHCR up-front.
    5. `pull_policy: missing` is set explicitly (== Compose default,
       but pinning it prevents drift to `always`/`build` which would
       break the desired pull-then-build behaviour).
    6. The publishing workflow + compose file agree on the registry
       (`ghcr.io`) and image basenames.
    7. `.env.example` documents both knobs so operators discover them.
    8. `scripts/quick-start.sh` no longer hardcodes `--build` on the
       `up` command (otherwise the image-first path is disabled —
       Compose would always rebuild instead of pulling).

No network / no Docker required: parses YAML + scans the script source.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.prod.yml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
QUICK_START_PATH = REPO_ROOT / "scripts" / "quick-start.sh"


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE_PATH.exists(), f"compose file missing: {COMPOSE_PATH}"
    return yaml.safe_load(COMPOSE_PATH.read_text())


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_PATH.read_text()


# ---------------------------------------------------------------------------
# (1) Both app services declare image: referencing GHCR
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("service", ["backend-a", "backend-b", "frontend"])
def test_service_has_image_referencing_ghcr(compose: dict, service: str) -> None:
    svc = compose["services"][service]
    image = svc.get("image")
    assert isinstance(image, str) and image, (
        f"service `{service}` must declare `image:` for the image-first path"
    )
    assert image.startswith("ghcr.io/"), (
        f"`{service}` image must be on ghcr.io (got: {image}) — public "
        "contract is `ghcr.io/<owner>/omnisight-<role>:<tag>`"
    )


@pytest.mark.parametrize(
    "service,role",
    [("backend-a", "backend"), ("backend-b", "backend"), ("frontend", "frontend")],
)
def test_image_uses_namespace_and_tag_env_overrides(
    compose: dict, service: str, role: str
) -> None:
    image = compose["services"][service]["image"]
    # Pin the env-var contract verbatim so a rename (e.g. dropping the
    # OMNISIGHT_ prefix) — which would silently break operators who
    # already set the original name — fails CI loudly.
    assert "${OMNISIGHT_GHCR_NAMESPACE:-your-org}" in image, (
        f"`{service}.image` must be parameterised by "
        "OMNISIGHT_GHCR_NAMESPACE with a default of `your-org`"
    )
    assert "${OMNISIGHT_IMAGE_TAG:-latest}" in image, (
        f"`{service}.image` must be parameterised by "
        "OMNISIGHT_IMAGE_TAG with a default of `latest`"
    )
    # Image basename must exactly match what docker-publish.yml ships.
    assert f"omnisight-{role}:" in image, (
        f"`{service}.image` basename must be `omnisight-{role}` "
        f"(got: {image}) — must match the GHCR publishing workflow"
    )


# ---------------------------------------------------------------------------
# (2) Both services keep `build:` so Compose can fall back to local build
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "service,dockerfile",
    [
        ("backend-a", "Dockerfile.backend"),
        ("backend-b", "Dockerfile.backend"),
        ("frontend", "Dockerfile.frontend"),
    ],
)
def test_service_keeps_build_for_fallback(
    compose: dict, service: str, dockerfile: str
) -> None:
    svc = compose["services"][service]
    build = svc.get("build")
    assert isinstance(build, dict), (
        f"service `{service}` must keep `build:` block — without it, "
        "Compose has no fallback when the GHCR image isn't pullable, "
        "breaking first-run for operators without OMNISIGHT_GHCR_NAMESPACE set"
    )
    assert build.get("context") == ".", (
        f"`{service}.build.context` must remain `.`"
    )
    assert build.get("dockerfile") == dockerfile, (
        f"`{service}.build.dockerfile` must be {dockerfile}"
    )
    # The Dockerfile must actually exist — otherwise the fallback build
    # would fail just as silently as a missing image.
    assert (REPO_ROOT / dockerfile).exists(), (
        f"build references missing {dockerfile}"
    )


def test_frontend_build_preserves_backend_url_arg(compose: dict) -> None:
    # Frontend's Next.js build needs BACKEND_URL baked in for the
    # rewrite proxy. Regressing this would silently break the prod
    # frontend → backend wiring on the local-build fallback path.
    # G2 #2 (HA-02) — after the dual-replica split, the frontend SSR
    # points at backend-a (the default upstream); Caddy fronts external
    # traffic and G2 #3 will move this to the proxy DNS name.
    args = compose["services"]["frontend"]["build"].get("args") or {}
    assert args.get("BACKEND_URL") == "http://backend-a:8000"


# ---------------------------------------------------------------------------
# (5) pull_policy: missing — explicit so drift to `always`/`build` fails CI
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("service", ["backend-a", "backend-b", "frontend"])
def test_pull_policy_is_missing(compose: dict, service: str) -> None:
    svc = compose["services"][service]
    # `missing` IS Compose's default but pinning it explicitly:
    #   - documents the intent at the call-site
    #   - blocks accidental drift to `always` (which would force a
    #     pull every `up`, slowing operations to a crawl on flaky
    #     networks) or `build` (which would never pull and defeat the
    #     entire image-first contract)
    assert svc.get("pull_policy") == "missing", (
        f"`{service}.pull_policy` must be `missing` to keep the "
        "image-first-then-build behaviour explicit"
    )


# ---------------------------------------------------------------------------
# Sidecars (prometheus / grafana) untouched
# ---------------------------------------------------------------------------

def test_observability_sidecars_unchanged(compose: dict) -> None:
    # The L10 #337 change is scoped to backend + frontend — the
    # observability sidecars use upstream images and have no `build:`
    # to worry about. Pin that they stay on their pinned versions.
    prom = compose["services"]["prometheus"]
    grafana = compose["services"]["grafana"]
    assert prom["image"] == "prom/prometheus:v2.54.1"
    assert grafana["image"] == "grafana/grafana:11.2.0"
    # Both must still be observability-profile-gated so they don't
    # start unprompted.
    assert "observability" in (prom.get("profiles") or [])
    assert "observability" in (grafana.get("profiles") or [])


# ---------------------------------------------------------------------------
# (6) Compose ↔ workflow agreement on registry + image names
# ---------------------------------------------------------------------------

def test_compose_image_names_match_publishing_workflow() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    matrix = workflow["jobs"]["publish"]["strategy"]["matrix"]
    published = {entry["image"] for entry in matrix["include"]}
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    consumed = set()
    # G2 #2: backend-a + backend-b both pull the same `omnisight-backend`
    # image; de-duplicating via set() keeps the set equality check clean.
    for service in ("backend-a", "backend-b", "frontend"):
        image = compose["services"][service]["image"]
        # Strip the `ghcr.io/<ns>/` prefix and `:<tag>` suffix to get
        # the bare image name.
        bare = image.rsplit("/", 1)[-1].split(":", 1)[0]
        consumed.add(bare)
    assert consumed == published, (
        f"compose consumes {consumed} but workflow publishes {published} "
        "— image-first deployment requires both files to agree"
    )
    # Both must use ghcr.io as the registry.
    assert workflow["env"]["REGISTRY"] == "ghcr.io"
    for service in ("backend-a", "backend-b", "frontend"):
        assert compose["services"][service]["image"].startswith("ghcr.io/")


# ---------------------------------------------------------------------------
# (7) .env.example documents both knobs
# ---------------------------------------------------------------------------

def test_env_example_documents_image_first_knobs() -> None:
    body = ENV_EXAMPLE_PATH.read_text()
    # Operators discover env vars by grepping .env.example. Knobs that
    # aren't there might as well not exist for L10's stated UX goal of
    # "first deploy 5-10 min → 30 s".
    assert "OMNISIGHT_GHCR_NAMESPACE" in body, (
        "OMNISIGHT_GHCR_NAMESPACE must be documented in .env.example"
    )
    assert "OMNISIGHT_IMAGE_TAG" in body, (
        "OMNISIGHT_IMAGE_TAG must be documented in .env.example"
    )
    # The L10 #337 reference in the section header makes it
    # discoverable when an operator searches the codebase for
    # context behind the env var.
    assert "L10 #337" in body or "GHCR" in body


# ---------------------------------------------------------------------------
# (8) quick-start.sh no longer hardcodes --build on the `up` command
# ---------------------------------------------------------------------------

def test_quick_start_does_not_force_rebuild_on_up() -> None:
    body = QUICK_START_PATH.read_text()
    lines = body.splitlines()
    # Find the line that runs `docker compose up` non-interactively.
    # Allow `--build` to appear in *comments* (the helper prints to
    # the user how to force a rebuild manually) but not in the actual
    # invocation, which would defeat the image-first contract.
    up_invocations = [
        line for line in lines
        if "docker compose" in line
        and " up " in line
        and not line.lstrip().startswith("#")
        and not line.lstrip().startswith("echo")
    ]
    assert up_invocations, (
        "expected at least one `docker compose ... up` invocation in quick-start.sh"
    )
    for line in up_invocations:
        assert "--build" not in line, (
            "quick-start.sh `docker compose up` must NOT pass --build "
            "(would force local rebuild and bypass the GHCR pull "
            f"path); found: {line!r}"
        )
