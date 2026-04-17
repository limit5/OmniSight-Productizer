r"""L11 #338 #1 — DigitalOcean App Platform spec contract tests.

These lock in the invariants that make the one-click Deploy button actually
produce a working deploy:

1. File exists, parses as valid YAML, and is picked up by the DO wizard
   (must live at `deploy/digitalocean/app.yaml` — that path is baked into
   the badge link in README.md).
2. Two services: `backend` + `frontend` — matches docker-compose.prod.yml
   topology (so local and cloud deploys behave the same).
3. Backend is PRIVATE (internal_ports only, no public routes). Making the
   FastAPI app publicly reachable from App Platform would bypass the
   Next.js /api proxy and break CORS + CSRF flows.
4. Frontend is PUBLIC at `/` and reaches the backend via
   `${backend.PRIVATE_URL}` — DO's service-discovery placeholder. A
   hard-coded URL here would break on every redeploy.
5. Health check paths point at real endpoints (/api/v1/health for backend,
   / for frontend). A bad path would make App Platform mark the service
   unhealthy and roll back forever.
6. SECRET env vars that carry credentials are flagged `type: SECRET` — a
   plain `type: GENERAL` would leak the value into the App spec JSON
   that's visible to anyone with read access to the app.
7. Critical `OMNISIGHT_*` production envs from `.env.example` are present
   (AUTH_MODE=strict, DEBUG=false, COOKIE_SECURE=true, ADMIN_EMAIL,
   ADMIN_PASSWORD). Missing any of these breaks the internet-exposure
   safety posture documented in the .env.example block.
8. Dockerfile paths referenced actually exist at the repo root.
9. README.md carries the Deploy-to-DO badge + link pointing at this spec.
10. `deploy/digitalocean/README.md` companion doc exists (post-deploy
    runbook).

Rationale — these are structural checks, not integration tests. They
cost <100ms to run and fail loudly the moment someone renames a file,
drops a service, or forgets to flag a new API key as SECRET. The full
"does DO actually deploy it" test is manual and lives in HANDOFF.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "deploy" / "digitalocean" / "app.yaml"
SPEC_README = REPO_ROOT / "deploy" / "digitalocean" / "README.md"
README = REPO_ROOT / "README.md"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def spec() -> dict:
    assert SPEC_PATH.is_file(), f"{SPEC_PATH} missing — L11 #1 deliverable"
    return yaml.safe_load(SPEC_PATH.read_text())


@pytest.fixture(scope="module")
def services(spec: dict) -> dict[str, dict]:
    svcs = spec.get("services") or []
    return {s["name"]: s for s in svcs}


# ── File-level invariants ────────────────────────────────────────────


def test_spec_file_exists():
    assert SPEC_PATH.is_file(), (
        "deploy/digitalocean/app.yaml is the path the DO wizard auto-detects; "
        "moving it breaks the one-click flow."
    )


def test_spec_is_valid_yaml(spec: dict):
    assert isinstance(spec, dict), "app.yaml must parse as a mapping"


def test_spec_has_app_name(spec: dict):
    # `name` appears in the DO dashboard + URL slug; required field.
    assert spec.get("name"), "app.yaml must set `name`"


def test_spec_has_region(spec: dict):
    # Explicit region avoids DO picking an unpredictable default.
    assert spec.get("region"), "app.yaml should pin a region"


def test_spec_declares_deployment_alert(spec: dict):
    """DEPLOYMENT_FAILED alert is the only automatic signal an operator
    gets when a push-triggered deploy goes sideways. Missing it means
    silent failures."""
    alerts = spec.get("alerts") or []
    rules = {a.get("rule") for a in alerts}
    assert "DEPLOYMENT_FAILED" in rules, "must alert on DEPLOYMENT_FAILED"


# ── Service topology ─────────────────────────────────────────────────


def test_has_backend_and_frontend(services: dict):
    assert "backend" in services, "`backend` service missing"
    assert "frontend" in services, "`frontend` service missing"


def test_backend_is_private(services: dict):
    """Backend MUST NOT be exposed publicly. App Platform's routing rule
    is: a service with `routes` is public, with `internal_ports` only
    is private. Violating this means FastAPI is reachable without the
    Next.js proxy → CSRF+CORS checks fire in the wrong context."""
    backend = services["backend"]
    assert backend.get("internal_ports") == [8000], (
        "backend must bind 8000 as an internal port"
    )
    assert not backend.get("routes"), (
        "backend must NOT define public `routes` — it is private-only"
    )


def test_frontend_is_public_at_root(services: dict):
    frontend = services["frontend"]
    assert frontend.get("http_port") == 3000, "frontend must serve on :3000"
    routes = frontend.get("routes") or []
    paths = {r.get("path") for r in routes}
    assert "/" in paths, "frontend must claim the root route `/`"


def test_backend_health_check_points_at_real_endpoint(services: dict):
    """`/api/v1/health` is the endpoint backend/main.py actually serves;
    a typo here makes App Platform loop-rollback forever."""
    hc = services["backend"].get("health_check") or {}
    assert hc.get("http_path") == "/api/v1/health", (
        "backend health_check must use /api/v1/health (the FastAPI probe)"
    )


def test_frontend_health_check_points_at_real_endpoint(services: dict):
    hc = services["frontend"].get("health_check") or {}
    assert hc.get("http_path") == "/", (
        "frontend health_check must use / (Next.js root page)"
    )


# ── Inter-service wiring ─────────────────────────────────────────────


def _envs_by_key(envs: list[dict]) -> dict[str, dict]:
    return {e["key"]: e for e in envs}


def test_frontend_reaches_backend_via_private_url(services: dict):
    """BACKEND_URL must use the `${backend.PRIVATE_URL}` placeholder
    (DO service discovery). A literal URL would go stale on every
    redeploy (DO assigns new internal hostnames)."""
    envs = _envs_by_key(services["frontend"].get("envs") or [])
    be = envs.get("BACKEND_URL")
    assert be is not None, "frontend must declare BACKEND_URL"
    val = be.get("value", "")
    assert "${backend.PRIVATE_URL}" in val, (
        f"BACKEND_URL must reference ${{backend.PRIVATE_URL}}, got {val!r}"
    )


def test_frontend_build_and_runtime_backend_url(services: dict):
    """next.config.mjs rewrites are BAKED IN at build time, so
    BACKEND_URL needs to be present at BUILD time too — not just RUN."""
    envs = _envs_by_key(services["frontend"].get("envs") or [])
    be = envs.get("BACKEND_URL") or {}
    scope = be.get("scope", "")
    assert scope == "RUN_AND_BUILD_TIME", (
        f"BACKEND_URL scope must be RUN_AND_BUILD_TIME (got {scope!r}) "
        "— Next.js bakes the rewrite target at build time"
    )


# ── Secret hygiene ───────────────────────────────────────────────────


CREDENTIAL_KEYS = (
    "OMNISIGHT_ANTHROPIC_API_KEY",
    "OMNISIGHT_OPENAI_API_KEY",
    "OMNISIGHT_GOOGLE_API_KEY",
    "OMNISIGHT_ADMIN_PASSWORD",
)


@pytest.mark.parametrize("key", CREDENTIAL_KEYS)
def test_credential_envs_flagged_as_secret(services: dict, key: str):
    """Any env that carries a real credential post-deploy must be
    `type: SECRET`. DO encrypts SECRET-typed vars at rest; GENERAL-typed
    vars sit in plain text in the app spec JSON."""
    envs = _envs_by_key(services["backend"].get("envs") or [])
    entry = envs.get(key)
    assert entry is not None, f"backend must declare {key}"
    assert entry.get("type") == "SECRET", (
        f"{key} must be `type: SECRET` — carrying a plaintext credential"
    )


def test_no_plaintext_api_keys_in_spec():
    """Belt-and-suspenders: raw spec text must not contain anything that
    looks like a live API key. Placeholders must be obvious sentinels."""
    text = SPEC_PATH.read_text()
    # Common provider key prefixes — loose check to flag accidental paste.
    forbidden_prefixes = ("sk-ant-api", "sk-proj-", "AIzaSy", "xai-")
    for prefix in forbidden_prefixes:
        assert prefix not in text, (
            f"app.yaml appears to contain a live API key (prefix {prefix!r}) "
            "— replace with EV[1:PLACEHOLDER:...] sentinel"
        )


# ── Production safety envs (from .env.example) ───────────────────────


@pytest.mark.parametrize(
    "key,expected",
    [
        ("OMNISIGHT_DEBUG", "false"),
        ("OMNISIGHT_AUTH_MODE", "strict"),
        ("OMNISIGHT_COOKIE_SECURE", "true"),
    ],
)
def test_backend_production_envs_pinned(
    services: dict, key: str, expected: str
):
    """The `.env.example` Internet-exposure-auth block says these three
    MUST be set for production. Cloud deploy is production by
    definition — leaving them to defaults risks open-mode auth."""
    envs = _envs_by_key(services["backend"].get("envs") or [])
    entry = envs.get(key)
    assert entry is not None, f"backend must declare {key}"
    assert entry.get("value") == expected, (
        f"{key} must be pinned to {expected!r} in production"
    )


def test_backend_admin_bootstrap_envs_present(services: dict):
    """K1 (must_change_password) relies on ADMIN_EMAIL + ADMIN_PASSWORD
    being present on first boot to seed the bootstrap admin."""
    envs = _envs_by_key(services["backend"].get("envs") or [])
    assert "OMNISIGHT_ADMIN_EMAIL" in envs, "missing OMNISIGHT_ADMIN_EMAIL"
    assert "OMNISIGHT_ADMIN_PASSWORD" in envs, "missing OMNISIGHT_ADMIN_PASSWORD"


def test_backend_cors_origin_uses_app_url(services: dict):
    """CORS has to accept the browser's Origin header — `${APP_URL}` is
    DO's placeholder for the app's public https URL. Hard-coding would
    drift the moment the app gets a custom domain."""
    envs = _envs_by_key(services["backend"].get("envs") or [])
    entry = envs.get("OMNISIGHT_FRONTEND_ORIGIN")
    assert entry is not None, "backend must declare OMNISIGHT_FRONTEND_ORIGIN"
    assert "${APP_URL}" in entry.get("value", ""), (
        "OMNISIGHT_FRONTEND_ORIGIN should use ${APP_URL} placeholder"
    )


# ── Dockerfile paths resolve ─────────────────────────────────────────


def test_dockerfile_paths_exist(services: dict):
    for name, svc in services.items():
        dfp = svc.get("dockerfile_path")
        assert dfp, f"{name} missing dockerfile_path"
        assert (REPO_ROOT / dfp).is_file(), (
            f"{name} dockerfile_path {dfp!r} does not resolve"
        )


# ── GitHub source matches the Deploy-button URL ──────────────────────


def test_services_source_github_repo_matches_button(services: dict):
    """The Deploy button URL hard-codes `limit5/OmniSight-Productizer` +
    branch `master`. The app.yaml `github:` block must point at the
    same place — otherwise the wizard fork UX gets confusing."""
    for name, svc in services.items():
        gh = svc.get("github") or {}
        assert gh.get("repo") == "limit5/OmniSight-Productizer", (
            f"{name}.github.repo must match the README Deploy-button URL"
        )
        assert gh.get("branch") == "master", (
            f"{name}.github.branch must be `master`"
        )


# ── README + companion doc ───────────────────────────────────────────


def test_readme_has_do_deploy_badge():
    """The Deploy-to-DO button in README.md is the user-facing entry
    point for L11. Its exact text is load-bearing for UX discoverability."""
    text = README.read_text()
    assert "deploytodo.com/do-btn-blue.svg" in text, (
        "README must embed the official Deploy-to-DO badge SVG"
    )
    assert "cloud.digitalocean.com/apps/new?repo=" in text, (
        "README must link to the DO apps/new?repo= one-click flow"
    )
    assert "limit5/OmniSight-Productizer" in text, (
        "Deploy button URL must reference the canonical GitHub repo"
    )


def test_readme_references_the_spec_file():
    """Link from README → deploy/digitalocean/app.yaml means users can
    audit what they're deploying before they click. Required for trust."""
    text = README.read_text()
    assert "deploy/digitalocean/app.yaml" in text, (
        "README must link to deploy/digitalocean/app.yaml"
    )


def test_companion_readme_exists():
    assert SPEC_README.is_file(), (
        "deploy/digitalocean/README.md must exist (post-deploy runbook)"
    )
    body = SPEC_README.read_text()
    # Sanity: not empty, not a 1-liner stub.
    assert len(body) > 500, "companion README is suspiciously short"
    assert "REPLACE_AFTER_DEPLOY" in body or "SECRET" in body or "Secret" in body, (
        "companion README must document post-deploy secret filling"
    )
