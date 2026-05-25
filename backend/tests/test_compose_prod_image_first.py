"""Contract tests for the release-train prod compose.

OP-1722 (2026-05-25) — MODERNIZED from the pre-release-train "image-first
GHCR" contract to the single-trunk release-train contract. The compose file
itself was migrated correctly (OP-1515 / RT-05a/b); these guard tests had
gone stale and were the failing party. The contract changed as follows
(GHCR → GitLab CR names, `missing` → `always` pull); see the per-test
rationale comments + the AC links to OP-1515 / RT-05b / ADR-0040 / ADR-0042.

Pins the load-bearing invariants of `docker-compose.prod.yml`:
    1. Backend (×2 HA replicas) + frontend declare an `image:` that
       resolves through the `${OMNISIGHT_REGISTRY}` abstraction (OP-1515 /
       ADR-0042 GHCR decommission) — NOT a hardcoded `ghcr.io/...` ref.
    2. Both also keep their `build:` block so Compose can transparently
       fall back to a local build when the registry image isn't
       available — that is the documented native Compose behaviour
       when both keys are set on the same service.
    3. Image basenames match what the GitLab CI release-train publisher
       (`.candidate_image_matrix` in `.gitlab-ci.yml`) ships: `backend`,
       `frontend` (the GHCR `omnisight-*` names were retired per ADR-0042).
    4. The registry/tag knobs fail closed (`${OMNISIGHT_REGISTRY:?...}` /
       `${OMNISIGHT_IMAGE_TAG:?...}`) so a missing registry never silently
       falls back to ghcr.io; an optional per-image DIGEST ref
       (`OMNISIGHT_{BACKEND,FRONTEND}_IMAGE_REF`, OP-1696) overrides the tag.
    5. `pull_policy: always` is set explicitly. Under the release train a
       deploy pins a promoted ref (digest via RT-05b / ADR-0040 §5
       digest-promotion); `always` re-pulls the pinned ref every `up`.
       Pinning it blocks drift back to `missing`/`build`.
    6. The GitLab CI release-train publisher + compose file agree on the
       app image basenames.
    7. `.env.example` documents the release-train knobs so operators
       discover them.
    8. `scripts/quick-start.sh` no longer hardcodes `--build` on the
       `up` command (otherwise the registry pull path is disabled —
       Compose would always rebuild instead of pulling).

No network / no Docker required: parses YAML + scans the script source.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.prod.yml"
# Release-train publisher source-of-truth: the GitLab CI candidate build
# matrix (the GHCR `.github/workflows/docker-publish.yml` was retired as the
# app-image publish path per ADR-0042). Read-only here — same as the old test
# read the GHA workflow; this test lives in backend/tests, not ci.
GITLAB_CI_PATH = REPO_ROOT / ".gitlab-ci.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
QUICK_START_PATH = REPO_ROOT / "scripts" / "quick-start.sh"

# The three app services under the release-train HA topology (G2 dual backend
# replica + frontend). All three resolve their `image:` through the same
# release-train contract.
APP_SERVICES = ("backend-a", "backend-b", "frontend")


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE_PATH.exists(), f"compose file missing: {COMPOSE_PATH}"
    return yaml.safe_load(COMPOSE_PATH.read_text())


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_PATH.read_text()


# ---------------------------------------------------------------------------
# (1) All app services declare image: resolving via the OMNISIGHT_REGISTRY
#     abstraction (release train — OP-1515 / ADR-0042 GHCR decommission)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("service", APP_SERVICES)
def test_service_image_resolves_via_registry_abstraction(
    compose: dict, service: str
) -> None:
    # OP-1722: the old contract pinned a hardcoded `ghcr.io/...` ref. The
    # release train (OP-1515 OMNISIGHT_REGISTRY abstraction, ADR-0042 GHCR
    # decommission → GitLab CR `sora.services:49160` as the sole registry)
    # makes the registry a parameter. Pin that the ref goes through
    # `${OMNISIGHT_REGISTRY...}` and does NOT hardcode ghcr.io — a re-hardcode
    # would re-introduce the drift OP-1515 abolished.
    svc = compose["services"][service]
    image = svc.get("image")
    assert isinstance(image, str) and image, (
        f"service `{service}` must declare `image:` for the registry pull path"
    )
    assert "${OMNISIGHT_REGISTRY" in image, (
        f"`{service}` image must resolve through the `${{OMNISIGHT_REGISTRY}}` "
        f"abstraction (got: {image}) — release-train contract is "
        "`${OMNISIGHT_REGISTRY:?...}/<role>:<tag>` (OP-1515 / ADR-0042)"
    )
    assert "ghcr.io" not in image, (
        f"`{service}` image must NOT hardcode ghcr.io (got: {image}) — GHCR "
        "is decommissioned for app images (ADR-0042); the emergency cutover "
        "fallback is an operator `.env` override, not a baked compose ref"
    )


@pytest.mark.parametrize(
    "service,role,digest_var",
    [
        ("backend-a", "backend", "OMNISIGHT_BACKEND_IMAGE_REF"),
        ("backend-b", "backend", "OMNISIGHT_BACKEND_IMAGE_REF"),
        ("frontend", "frontend", "OMNISIGHT_FRONTEND_IMAGE_REF"),
    ],
)
def test_image_uses_registry_tag_and_digest_overrides(
    compose: dict, service: str, role: str, digest_var: str
) -> None:
    # OP-1722: pin the release-train env-var contract verbatim so a rename
    # fails CI loudly. The knobs changed under the release train:
    #   OMNISIGHT_GHCR_NAMESPACE:-your-org  →  OMNISIGHT_REGISTRY:?registry required
    #   OMNISIGHT_IMAGE_TAG:-latest         →  OMNISIGHT_IMAGE_TAG:?image tag required
    #   (new) per-image DIGEST ref OMNISIGHT_{BACKEND,FRONTEND}_IMAGE_REF (OP-1696)
    image = compose["services"][service]["image"]
    # OP-1515: registry is REQUIRED and fails closed (`:?`) — never a
    # `:-default` that could silently fall back to ghcr.io.
    assert "${OMNISIGHT_REGISTRY:?registry required}" in image, (
        f"`{service}.image` must be parameterised by a fail-closed "
        "`${OMNISIGHT_REGISTRY:?registry required}` (OP-1515)"
    )
    assert "${OMNISIGHT_IMAGE_TAG:?image tag required}" in image, (
        f"`{service}.image` must be parameterised by a fail-closed "
        "`${OMNISIGHT_IMAGE_TAG:?image tag required}` (OP-1515)"
    )
    # OP-1696: an optional per-image digest ref overrides the tag path. The
    # backend replicas share OMNISIGHT_BACKEND_IMAGE_REF; the frontend is a
    # separate image with OMNISIGHT_FRONTEND_IMAGE_REF.
    assert f"${{{digest_var}:-" in image, (
        f"`{service}.image` must honour the per-image digest override "
        f"`${{{digest_var}:-...}}` (OP-1696) — when set it short-circuits the "
        "registry/tag fallback for a deploy-by-digest"
    )
    # Image basename must exactly match what the GitLab CI release-train
    # publisher ships (`backend` / `frontend`) — the GHCR `omnisight-*`
    # basenames were retired (ADR-0042).
    assert f"/{role}:" in image, (
        f"`{service}.image` basename must be `{role}` "
        f"(got: {image}) — must match the GitLab CI release-train publisher"
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
    # G2 #4 — frontend SSR routes through Caddy (health-aware) so it
    # survives backend rolling restart without OOM. See commit eae6e0a.
    args = compose["services"]["frontend"]["build"].get("args") or {}
    assert args.get("BACKEND_URL") == "http://caddy:8000"


# ---------------------------------------------------------------------------
# (5) pull_policy: always — explicit so drift to `missing`/`build` fails CI
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("service", APP_SERVICES)
def test_pull_policy_is_always(compose: dict, service: str) -> None:
    svc = compose["services"][service]
    # OP-1722: the contract flipped `missing` → `always` under the release
    # train. A deploy pins a promoted ref (RT-05b deploy-by-digest +
    # ADR-0040 §5 "promotion is by image digest, not rebuild") and `always`
    # re-pulls that exact pinned ref on every `up` so the running container
    # is guaranteed to be the validated digest — `missing` would skip the
    # pull when a stale same-tag image already sat in the local cache.
    # Pinning `always` explicitly blocks drift back to `missing` (stale
    # cache wins) or `build` (never pulls, defeats deploy-by-digest).
    assert svc.get("pull_policy") == "always", (
        f"`{service}.pull_policy` must be `always` so each `up` re-pulls "
        "the release-train-pinned ref (RT-05b deploy-by-digest)"
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
# (6) Compose ↔ release-train publisher agreement on app image basenames
# ---------------------------------------------------------------------------

def test_compose_image_names_match_release_train_publisher() -> None:
    # OP-1722: the publisher source-of-truth moved from the GHCR GitHub-
    # Actions workflow to the GitLab CI candidate build matrix (ADR-0042
    # GHCR decommission — GitLab CI is the active publish path; ADR-0040
    # tag/digest-driven release train). Cross-check that the basenames
    # compose consumes are exactly the ones the release-train publisher
    # builds, so the two files cannot drift apart.
    gitlab_ci = yaml.safe_load(GITLAB_CI_PATH.read_text())
    # `.candidate_image_matrix` is the shared parallel matrix the
    # `candidate-build-image` job extends; each entry's IMAGE_NAME is
    # pushed as `${CI_REGISTRY_IMAGE}/${IMAGE_NAME}` (.gitlab-ci.yml).
    matrix = gitlab_ci[".candidate_image_matrix"]["parallel"]["matrix"]
    published = {entry["IMAGE_NAME"] for entry in matrix}
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    consumed = set()
    # G2 #2: backend-a + backend-b both pull the same `backend` image;
    # de-duplicating via set() keeps the set equality check clean.
    for service in APP_SERVICES:
        image = compose["services"][service]["image"]
        # Strip the `${OMNISIGHT_REGISTRY...}/` prefix and `:<tag>` suffix
        # to get the bare image basename (rsplit on the last `/`, then take
        # everything before the first `:`).
        bare = image.rsplit("/", 1)[-1].split(":", 1)[0]
        consumed.add(bare)
    deployable_app_images = {"backend", "frontend"}
    assert consumed == deployable_app_images, (
        f"compose consumes {consumed}, expected {deployable_app_images} "
        "for the release-train app deployment path"
    )
    assert deployable_app_images <= published, (
        f"GitLab CI publishes {published}, missing a compose-consumed app "
        "image — deploy-by-digest requires both files to agree on basenames"
    )
    # The customer-side `omnisight-proxy` is NOT in the candidate matrix and
    # NOT started by docker-compose.prod.yml — it keeps its own GHCR publish
    # contract (the dormant OP-1719 GitLab-CI GHCR publisher, ADR-0042 §🔑
    # on-prem trigger). Pin that it stays out of the SaaS app deploy set.
    assert "omnisight-proxy" not in published, (
        "omnisight-proxy is a customer-side image with its own (dormant) "
        "GHCR publisher — it must not enter the release-train candidate "
        "matrix that compose consumes"
    )
    assert "omnisight-proxy" not in consumed, (
        "omnisight-proxy is customer-side and intentionally not started by "
        "docker-compose.prod.yml"
    )
    # App images must resolve via the registry abstraction (no hardcoded
    # ghcr.io) — the release-train registry contract (OP-1515 / ADR-0042).
    for service in APP_SERVICES:
        image = compose["services"][service]["image"]
        assert "${OMNISIGHT_REGISTRY" in image and "ghcr.io" not in image


# ---------------------------------------------------------------------------
# (7) .env.example documents the release-train knobs
# ---------------------------------------------------------------------------

def test_env_example_documents_release_train_knobs() -> None:
    body = ENV_EXAMPLE_PATH.read_text()
    # OP-1722: operators discover env vars by grepping .env.example. The
    # release-train deploy contract is driven by OMNISIGHT_REGISTRY +
    # OMNISIGHT_IMAGE_TAG (OP-1515); the old OMNISIGHT_GHCR_NAMESPACE knob
    # was retired with the GHCR decommission (ADR-0042).
    assert "OMNISIGHT_REGISTRY" in body, (
        "OMNISIGHT_REGISTRY must be documented in .env.example (OP-1515)"
    )
    assert "OMNISIGHT_IMAGE_TAG" in body, (
        "OMNISIGHT_IMAGE_TAG must be documented in .env.example"
    )
    # An OP-1515 / OP-1478 reference in the section header makes the knob
    # discoverable when an operator searches the codebase for the context
    # behind the registry abstraction.
    assert "OP-1515" in body or "OMNISIGHT_REGISTRY" in body


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
