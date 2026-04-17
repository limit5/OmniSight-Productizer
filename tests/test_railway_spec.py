r"""L11 #338 #2 — Railway deploy spec contract tests.

Sibling to `tests/test_digitalocean_app_spec.py`. Same shape, same cost
profile (<100ms, pure structural asserts, no network, no Docker), but
targeting Railway's single-service `railway.json` + the repo-level
Deploy-button + companion runbook.

Why these invariants matter:

1. Railway's `railway.json` is SINGLE-SERVICE (top-level `build` +
   `deploy` only — no `services` block like DigitalOcean's app.yaml).
   If someone "improves" this file into a multi-service shape it will
   silently stop being valid Railway config.
2. `build.builder` MUST be `DOCKERFILE`. Railway's default is NIXPACKS,
   which tries to auto-detect language stacks and would completely
   ignore `Dockerfile.backend`.
3. `build.dockerfilePath` must point at `Dockerfile.backend`. Railway
   looks for `Dockerfile` at service root by default; the explicit
   pointer keeps the behavior identical regardless of whether
   Railway changes its auto-detect heuristics.
4. `deploy.startCommand` must override the Dockerfile `CMD` so uvicorn
   binds Railway's injected `$PORT` (NOT the hardcoded 8000 that
   docker-compose relies on). Without this, Railway's edge proxy
   cannot reach the service and every request returns 502.
5. `deploy.healthcheckPath` must be `/api/v1/health` — the real
   FastAPI route (backend mounts `health.router` under
   `settings.api_prefix` which defaults to `/api/v1`). A typo makes
   Railway roll back every deploy because the rollout never goes green.
6. `deploy.restartPolicyType` must be `ON_FAILURE` (not `NEVER`). On a
   transient crash Railway must bring the service back up; `NEVER`
   leaves a dead service until an operator notices.
7. The README button URL must use Railway's official template flow
   (`railway.com/new/template?template=<url-encoded-repo>`) — a
   handcrafted URL won't pre-populate the repo picker.
8. The companion `deploy/railway/README.md` exists and documents the
   env-var matrix (Railway has no env schema in `railway.json`, so the
   runbook is the ONLY place those variables are declared).

These are structural checks — they fail loudly when someone drops a
field, renames a file, or silently switches the builder. The
"does Railway actually deploy it" check is manual and lives in
`HANDOFF.md`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "deploy" / "railway" / "railway.json"
SPEC_README = REPO_ROOT / "deploy" / "railway" / "README.md"
README = REPO_ROOT / "README.md"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def spec() -> dict:
    assert SPEC_PATH.is_file(), f"{SPEC_PATH} missing — L11 #2 deliverable"
    return json.loads(SPEC_PATH.read_text())


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC_PATH.read_text()


# ── File-level invariants ────────────────────────────────────────────


def test_spec_file_exists():
    # deploy/railway/railway.json is the path the Deploy button
    # companion README tells operators to set as "Config-as-Code Path"
    # in the Railway dashboard. Moving it breaks the documented flow.
    assert SPEC_PATH.is_file(), (
        "deploy/railway/railway.json must exist — it is the Railway "
        "config-as-code file operators point their service at."
    )


def test_spec_is_valid_json(spec: dict):
    assert isinstance(spec, dict), "railway.json must parse as a JSON object"


def test_spec_declares_railway_schema(spec: dict):
    """The `$schema` pointer gets IDE validation + lets Railway's
    `railway.com/railway.schema.json` catch typos early. Not strictly
    required but cheap insurance."""
    schema = spec.get("$schema", "")
    assert "railway" in schema and "schema.json" in schema, (
        f"$schema should reference railway.com's schema, got {schema!r}"
    )


def test_spec_is_single_service_shape(spec: dict):
    """Railway's config-as-code schema is flat — top-level keys are
    `$schema`, `build`, `deploy`. There is NO `services` block (unlike
    DigitalOcean's app.yaml). A `services` key here silently stops the
    file from being a valid Railway config."""
    forbidden = {"services"}
    leaked = forbidden & set(spec.keys())
    assert not leaked, (
        f"railway.json must be single-service — leaked keys {leaked} "
        "suggest someone tried to shape it like the DO spec"
    )


# ── Build block ──────────────────────────────────────────────────────


def test_build_uses_dockerfile_builder(spec: dict):
    """Railway's default builder is NIXPACKS (language auto-detect),
    which completely ignores Dockerfile.backend. `DOCKERFILE` is
    load-bearing — without it the wrong image gets built."""
    build = spec.get("build") or {}
    assert build.get("builder") == "DOCKERFILE", (
        f"build.builder must be 'DOCKERFILE', got {build.get('builder')!r}"
    )


def test_build_dockerfile_path_resolves(spec: dict):
    """`build.dockerfilePath` must point at a real file; a typo here
    makes Railway crash the build step (before deploy even starts)."""
    build = spec.get("build") or {}
    path = build.get("dockerfilePath")
    assert path == "Dockerfile.backend", (
        f"dockerfilePath must be 'Dockerfile.backend' (matches the "
        f"backend service topology documented in the companion README), "
        f"got {path!r}"
    )
    assert (REPO_ROOT / path).is_file(), (
        f"dockerfilePath {path!r} does not resolve from repo root"
    )


# ── Deploy block ─────────────────────────────────────────────────────


def test_deploy_start_command_binds_railway_port(spec: dict):
    """Railway's edge proxy routes public traffic to the `$PORT` env
    var it injects at runtime. `Dockerfile.backend` hardcodes 8000 in
    its CMD (so docker-compose keeps working), so railway.json MUST
    override the command to bind `$PORT` or every request returns 502.
    Also pin the fallback so local `railway up` without `$PORT` still
    boots (Railway-CLI doesn't always set it)."""
    deploy = spec.get("deploy") or {}
    cmd = deploy.get("startCommand", "")
    assert "${PORT" in cmd or "$PORT" in cmd, (
        f"startCommand must bind Railway's $PORT, got {cmd!r}"
    )
    assert "uvicorn" in cmd and "backend.main:app" in cmd, (
        f"startCommand must launch the FastAPI app via uvicorn, got {cmd!r}"
    )


def test_deploy_healthcheck_points_at_real_endpoint(spec: dict):
    """`/api/v1/health` is what `backend/routers/health.py` actually
    registers under `settings.api_prefix` (default `/api/v1`).
    Mismatch causes Railway to roll back every deploy — the new
    revision never flips to healthy."""
    deploy = spec.get("deploy") or {}
    assert deploy.get("healthcheckPath") == "/api/v1/health", (
        f"healthcheckPath must be '/api/v1/health', got "
        f"{deploy.get('healthcheckPath')!r}"
    )


def test_deploy_healthcheck_timeout_is_reasonable(spec: dict):
    """Railway's docs cap healthcheck timeout at 3600s. A value <5 is
    too tight (container cold-start + weasyprint lib load takes ~10s).
    Pin [10, 300] as the sane band."""
    deploy = spec.get("deploy") or {}
    timeout = deploy.get("healthcheckTimeout")
    assert isinstance(timeout, int), "healthcheckTimeout must be set"
    assert 10 <= timeout <= 300, (
        f"healthcheckTimeout {timeout} is outside the sane [10, 300] band"
    )


def test_deploy_restart_policy_is_on_failure(spec: dict):
    """`ON_FAILURE` bounces the container on crash; `NEVER` leaves a
    dead service dark until an operator notices. Critical for a
    single-replica deploy with no manual oncall."""
    deploy = spec.get("deploy") or {}
    assert deploy.get("restartPolicyType") == "ON_FAILURE", (
        f"restartPolicyType must be 'ON_FAILURE', got "
        f"{deploy.get('restartPolicyType')!r}"
    )


def test_deploy_has_replica_count(spec: dict):
    """Railway defaults `numReplicas` to 1, but we pin it explicitly
    so the live spec shows it — otherwise an operator toggling
    auto-scale in the UI can silently change it."""
    deploy = spec.get("deploy") or {}
    assert deploy.get("numReplicas") == 1, (
        f"numReplicas must be pinned to 1 for the single-tenant demo"
    )


# ── Secret hygiene (spec text) ───────────────────────────────────────


def test_no_plaintext_api_keys_in_spec(spec_text: str):
    """Railway config-as-code intentionally has no env block — secrets
    belong in the Railway dashboard, not in the repo. This is a
    belt-and-suspenders check to flag accidental paste of a provider
    key prefix into the spec."""
    forbidden_prefixes = ("sk-ant-api", "sk-proj-", "AIzaSy", "xai-")
    for prefix in forbidden_prefixes:
        assert prefix not in spec_text, (
            f"railway.json appears to contain a live API key "
            f"(prefix {prefix!r}); secrets belong in the Railway dashboard"
        )


def test_spec_does_not_embed_env_block(spec: dict):
    """Railway's schema has no `envs` / `env` / `variables` top-level
    field. A leaked block here is the sign of someone porting the DO
    schema across without reading Railway's docs — the file would parse
    but Railway would simply ignore the envs, masking a config gap."""
    leaked = {"envs", "env", "variables"} & set(spec.keys())
    assert not leaked, (
        f"railway.json must not declare env/variables inline — found "
        f"{leaked}. Railway reads envs from the dashboard / CLI only; "
        f"document them in deploy/railway/README.md instead."
    )


# ── README + companion doc ───────────────────────────────────────────


def test_readme_has_railway_deploy_badge():
    """The Deploy-on-Railway button in README.md is the user-facing
    entry point for L11 #2. Its exact text is load-bearing for UX
    discoverability — if the badge SVG URL drifts, the README renders
    a broken image."""
    text = README.read_text()
    assert "railway.com/button.svg" in text, (
        "README must embed the official Deploy-on-Railway badge SVG"
    )
    assert "railway.com/new/template?template=" in text, (
        "README must link to railway.com/new/template?template= — "
        "that's Railway's documented one-click flow"
    )
    assert "limit5%2FOmniSight-Productizer" in text or \
           "limit5/OmniSight-Productizer" in text, (
        "Railway Deploy button must URL-encode the canonical repo path"
    )


def test_readme_references_the_railway_spec_file():
    """Link from README → deploy/railway/railway.json lets users audit
    the config before clicking the button. Required for trust and for
    symmetry with the DigitalOcean entry."""
    text = README.read_text()
    assert "deploy/railway/railway.json" in text, (
        "README must link to deploy/railway/railway.json"
    )
    assert "deploy/railway/README.md" in text, (
        "README must link to the Railway runbook"
    )


def test_companion_readme_exists_and_documents_envs():
    """`deploy/railway/README.md` is the ONLY place Railway env vars
    are declared (Railway's config-as-code has no env schema). A stub
    or empty file means operators deploy with no guidance → surface
    broken (CORS, auth, admin bootstrap)."""
    assert SPEC_README.is_file(), (
        "deploy/railway/README.md must exist (post-deploy runbook)"
    )
    body = SPEC_README.read_text()
    assert len(body) > 800, (
        "companion README is suspiciously short — Railway deploys "
        "need a real env matrix + the monorepo setup walkthrough"
    )
    # The runbook MUST explain how to wire both services together.
    for required in (
        "OMNISIGHT_AUTH_MODE",
        "OMNISIGHT_DEBUG",
        "OMNISIGHT_COOKIE_SECURE",
        "OMNISIGHT_ADMIN_EMAIL",
        "OMNISIGHT_ADMIN_PASSWORD",
        "OMNISIGHT_FRONTEND_ORIGIN",
        "BACKEND_URL",
        "RAILWAY_PRIVATE_DOMAIN",
        "RAILWAY_PUBLIC_DOMAIN",
        "Dockerfile.frontend",
    ):
        assert required in body, (
            f"companion README missing required mention of {required!r}"
        )
