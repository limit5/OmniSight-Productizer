r"""L11 #338 #3 — Render Blueprint spec contract tests.

Sibling to `tests/test_digitalocean_app_spec.py` + `tests/test_railway_spec.py`.
Same cost profile (<100ms, pure structural asserts, no network, no Docker),
but targeting Render's multi-service `render.yaml` Blueprint + the repo-level
Deploy button + companion runbook.

Why these invariants matter:

1. Render picks up `render.yaml` automatically on first Blueprint apply —
   if this file moves, `test_spec_file_exists` catches it before the
   Deploy button 404s.

2. Render's Blueprint is **multi-service** (like DigitalOcean's app.yaml,
   unlike Railway's single-service `railway.json`). Both `omnisight-backend`
   + `omnisight-frontend` MUST be declared — dropping one makes the
   topology broken in a way that only fails at runtime.

3. Backend MUST be `type: pserv` (private service). `type: web` would
   expose FastAPI directly to the internet, bypassing Next.js's /api
   rewrite + CSRF/CORS middleware — a silent security regression.

4. Frontend MUST be `type: web` with `healthCheckPath: /`. Dropping the
   health check makes Render's deploy rollout unable to tell whether a
   new revision is actually serving, so it may flip live on a broken one.

5. `BACKEND_URL` on the frontend MUST hard-pin `http://omnisight-backend:8000`.
   Render's Blueprint doesn't support string templating across services
   (`fromService` gives hostname OR port, not a full URL), so we rely on
   the stable internal-hostname convention: pserv hostname == service
   name. An empty / stale value here breaks the Next.js rewrite proxy.

6. Backend `dockerCommand` MUST override the Dockerfile CMD — pinning
   port 8000 + honoring `$OMNISIGHT_WORKERS` from the Blueprint envs.
   Without this, Dockerfile's CPU-auto workers heuristic can over-fork
   on Render's tiny instance tiers and OOM the container.

7. Credentials (`OMNISIGHT_*_API_KEY`, `OMNISIGHT_ADMIN_PASSWORD`) MUST
   be `sync: false`. Plaintext in the spec = leaked in git; Render's
   `sync: false` prompts the operator for each value on Blueprint apply
   and rotates them via dashboard instead.

8. The three `.env.example` Internet-exposure hard-pins (DEBUG=false /
   AUTH_MODE=strict / COOKIE_SECURE=true) MUST be present on backend.
   Missing any one breaks the production auth posture.

9. README.md carries the Deploy-to-Render badge + link pointing at this
   spec's repo URL. Broken badge = broken one-click UX.

10. `deploy/render/README.md` companion doc exists + documents the
    2-stage post-deploy flow (because Stage 2 is load-bearing for CORS
    and won't happen unless the operator reads it).

These are structural checks — they fail loudly when someone drops a
field, renames a file, flips a service type, or accidentally commits a
live API key. The "does Render actually deploy it" check is manual and
lives in `HANDOFF.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "deploy" / "render" / "render.yaml"
SPEC_README = REPO_ROOT / "deploy" / "render" / "README.md"
README = REPO_ROOT / "README.md"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def spec() -> dict:
    assert SPEC_PATH.is_file(), f"{SPEC_PATH} missing — L11 #3 deliverable"
    return yaml.safe_load(SPEC_PATH.read_text())


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC_PATH.read_text()


@pytest.fixture(scope="module")
def services(spec: dict) -> dict[str, dict]:
    svcs = spec.get("services") or []
    return {s["name"]: s for s in svcs}


def _envs_by_key(envs: list[dict]) -> dict[str, dict]:
    return {e["key"]: e for e in envs}


# ── File-level invariants ────────────────────────────────────────────


def test_spec_file_exists():
    # `deploy/render/render.yaml` is the path the companion README tells
    # operators to point Render's Blueprint importer at. Moving it
    # breaks the documented flow.
    assert SPEC_PATH.is_file(), (
        "deploy/render/render.yaml must exist — it is the Render "
        "Blueprint file operators point their workspace at."
    )


def test_spec_is_valid_yaml(spec: dict):
    assert isinstance(spec, dict), "render.yaml must parse as a mapping"


def test_spec_is_multi_service_shape(spec: dict):
    """Render's Blueprint schema is multi-service — a top-level `services`
    list holds each service. Missing this key means the file is not a
    valid Render Blueprint; Render will silently ignore it on import."""
    svcs = spec.get("services")
    assert isinstance(svcs, list) and len(svcs) >= 2, (
        "render.yaml must declare `services:` with at least 2 entries "
        "(backend + frontend)"
    )


# ── Service topology ─────────────────────────────────────────────────


def test_has_backend_and_frontend(services: dict):
    assert "omnisight-backend" in services, (
        "`omnisight-backend` service missing — the Blueprint topology "
        "needs both services for the frontend's rewrite proxy to work"
    )
    assert "omnisight-frontend" in services, (
        "`omnisight-frontend` service missing"
    )


def test_backend_is_private_pserv(services: dict):
    """Render's private-service type is `pserv` — only reachable via the
    internal hostname `<service-name>:<port>` from sibling services.
    Flipping to `type: web` would expose FastAPI directly to the public
    internet, bypassing Next.js's /api proxy + CSRF/CORS middleware."""
    be = services["omnisight-backend"]
    assert be.get("type") == "pserv", (
        f"backend must be `type: pserv` (private service); got "
        f"{be.get('type')!r}. `web` would expose FastAPI publicly."
    )


def test_frontend_is_public_web(services: dict):
    """Frontend is the sole public entry point — browser hits the
    *.onrender.com URL and Next.js rewrites proxy /api/* to the pserv."""
    fe = services["omnisight-frontend"]
    assert fe.get("type") == "web", (
        f"frontend must be `type: web` (public); got {fe.get('type')!r}"
    )


def test_both_services_use_docker_runtime(services: dict):
    """`runtime: docker` is load-bearing — Render's default is
    auto-detect, which would try to run Next.js/FastAPI natively and
    ignore `Dockerfile.backend` / `Dockerfile.frontend` entirely."""
    for name, svc in services.items():
        runtime = svc.get("runtime") or svc.get("env")
        assert runtime == "docker", (
            f"{name} must declare `runtime: docker` (or legacy `env: docker`), "
            f"got {runtime!r}"
        )


def test_both_services_pin_a_region(services: dict):
    """Without `region`, Render picks a default that may change — and if
    backend + frontend land in different regions the pserv internal
    hostname won't resolve. Pin explicitly."""
    regions = {svc.get("region") for svc in services.values()}
    assert None not in regions, "every service must pin `region`"
    assert len(regions) == 1, (
        f"backend + frontend must share the same `region` for pserv "
        f"internal DNS to resolve; got {regions}"
    )


# ── Health checks ────────────────────────────────────────────────────


def test_backend_health_check_points_at_real_endpoint(services: dict):
    """`/api/v1/health` is what `backend/routers/health.py` actually
    registers (mounted under `settings.api_prefix = /api/v1`). Typo =
    Render rolls back every deploy because the new revision never flips
    healthy."""
    hc = services["omnisight-backend"].get("healthCheckPath")
    assert hc == "/api/v1/health", (
        f"backend healthCheckPath must be '/api/v1/health', got {hc!r}"
    )


def test_frontend_health_check_points_at_root(services: dict):
    hc = services["omnisight-frontend"].get("healthCheckPath")
    assert hc == "/", (
        f"frontend healthCheckPath must be '/' (Next.js root route), "
        f"got {hc!r}"
    )


# ── Backend dockerCommand override ───────────────────────────────────


def test_backend_docker_command_pins_port_and_workers(services: dict):
    """Dockerfile.backend's CMD uses an auto-workers heuristic (CPU/2)
    that can over-fork on Render's tiny instance tiers. The Blueprint
    override:
      - keeps port pinned at 8000 (pserv doesn't get $PORT injection,
        and frontend hard-pins BACKEND_URL=http://omnisight-backend:8000)
      - reads OMNISIGHT_WORKERS from the Blueprint env (operator-tunable)
      - launches via `python -m uvicorn backend.main:app`
    Missing any of the above breaks the contract the Railway + DO specs
    also honor — the three cloud targets must behave identically."""
    be = services["omnisight-backend"]
    cmd = be.get("dockerCommand") or ""
    assert "uvicorn" in cmd and "backend.main:app" in cmd, (
        f"dockerCommand must launch the FastAPI app via uvicorn, got {cmd!r}"
    )
    assert "--port 8000" in cmd, (
        f"dockerCommand must pin port 8000 for pserv internal hostname "
        f"discovery, got {cmd!r}"
    )
    assert "OMNISIGHT_WORKERS" in cmd, (
        f"dockerCommand must honor OMNISIGHT_WORKERS from the Blueprint, "
        f"got {cmd!r}"
    )


# ── Inter-service wiring ─────────────────────────────────────────────


def test_frontend_hard_pins_backend_url(services: dict):
    """Render's Blueprint env vars don't support string templating —
    `fromService` exposes a sibling's `host` / `port` / `hostport` but
    NOT a full `http://host:port` URL. Hard-pin the internal hostname
    instead; pserv service-name is the canonical internal DNS name."""
    envs = _envs_by_key(services["omnisight-frontend"].get("envVars") or [])
    be_url = envs.get("BACKEND_URL")
    assert be_url is not None, "frontend must declare BACKEND_URL"
    val = be_url.get("value", "")
    assert val == "http://omnisight-backend:8000", (
        f"BACKEND_URL must hard-pin 'http://omnisight-backend:8000' "
        f"(pserv internal hostname convention); got {val!r}"
    )


# ── Secret hygiene ───────────────────────────────────────────────────


CREDENTIAL_KEYS = (
    "OMNISIGHT_ANTHROPIC_API_KEY",
    "OMNISIGHT_OPENAI_API_KEY",
    "OMNISIGHT_GOOGLE_API_KEY",
    "OMNISIGHT_ADMIN_PASSWORD",
)


@pytest.mark.parametrize("key", CREDENTIAL_KEYS)
def test_credential_envs_flagged_sync_false(services: dict, key: str):
    """Any backend env that carries a real credential post-deploy must
    use `sync: false`. Render's Blueprint wizard prompts the operator
    for each `sync: false` var on apply — never writing the value to git.
    A plaintext `value:` would leak through git history on every push."""
    envs = _envs_by_key(services["omnisight-backend"].get("envVars") or [])
    entry = envs.get(key)
    assert entry is not None, f"backend must declare {key}"
    assert entry.get("sync") is False, (
        f"{key} must be `sync: false` so Render prompts the operator "
        f"instead of baking a value into the Blueprint"
    )
    assert "value" not in entry, (
        f"{key} must NOT carry a `value:` — `sync: false` only"
    )


def test_no_plaintext_api_keys_in_spec(spec_text: str):
    """Belt-and-suspenders: raw spec text must not contain anything that
    looks like a live API key. `sync: false` takes care of the schema
    contract; this catches accidental paste."""
    forbidden_prefixes = ("sk-ant-api", "sk-proj-", "AIzaSy", "xai-")
    for prefix in forbidden_prefixes:
        assert prefix not in spec_text, (
            f"render.yaml appears to contain a live API key "
            f"(prefix {prefix!r}); secrets belong behind `sync: false`"
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
    """The `.env.example` Internet-exposure-auth block mandates these
    three for any production deploy. Cloud deploy IS production by
    definition — leaving defaults risks open-mode auth on a public URL."""
    envs = _envs_by_key(services["omnisight-backend"].get("envVars") or [])
    entry = envs.get(key)
    assert entry is not None, f"backend must declare {key}"
    assert str(entry.get("value")) == expected, (
        f"{key} must be pinned to {expected!r} in production, got "
        f"{entry.get('value')!r}"
    )


def test_backend_admin_bootstrap_envs_present(services: dict):
    """K1's `must_change_password` seed needs ADMIN_EMAIL + ADMIN_PASSWORD
    on first boot. Missing them = bootstrap admin creation silently
    skipped → no way to log in to the fresh deploy."""
    envs = _envs_by_key(services["omnisight-backend"].get("envVars") or [])
    assert "OMNISIGHT_ADMIN_EMAIL" in envs, "missing OMNISIGHT_ADMIN_EMAIL"
    assert "OMNISIGHT_ADMIN_PASSWORD" in envs, (
        "missing OMNISIGHT_ADMIN_PASSWORD"
    )


def test_backend_database_path_on_persistent_disk(services: dict):
    """Render's container filesystem is ephemeral without a disk. The
    Blueprint attaches a 1 GB disk at /var/data; OMNISIGHT_DATABASE_PATH
    must land inside that mount or SQLite gets wiped on every redeploy."""
    be = services["omnisight-backend"]
    disk = be.get("disk") or {}
    mount = disk.get("mountPath", "")
    assert mount == "/var/data", (
        f"backend disk mountPath must be /var/data (the path the DB env "
        f"points at); got {mount!r}"
    )
    size = disk.get("sizeGB")
    assert isinstance(size, int) and size >= 1, (
        f"disk sizeGB must be a positive int, got {size!r}"
    )
    envs = _envs_by_key(be.get("envVars") or [])
    db_path = (envs.get("OMNISIGHT_DATABASE_PATH") or {}).get("value", "")
    assert db_path.startswith(mount + "/"), (
        f"OMNISIGHT_DATABASE_PATH={db_path!r} must live inside the disk "
        f"mountPath {mount!r} or SQLite gets wiped on redeploy"
    )


def test_cors_origin_is_operator_filled(services: dict):
    """Render can't template a full `https://<host>` URL from fromService
    (it exposes hostname OR port, never scheme+host+port). Operator must
    fill OMNISIGHT_FRONTEND_ORIGIN manually post-deploy; flagging it
    `sync: false` makes the Blueprint wizard prompt for it explicitly
    so it doesn't get silently skipped."""
    envs = _envs_by_key(services["omnisight-backend"].get("envVars") or [])
    entry = envs.get("OMNISIGHT_FRONTEND_ORIGIN")
    assert entry is not None, (
        "backend must declare OMNISIGHT_FRONTEND_ORIGIN (CORS gate)"
    )
    assert entry.get("sync") is False, (
        "OMNISIGHT_FRONTEND_ORIGIN must be `sync: false` — operator fills "
        "the https://*.onrender.com URL after first deploy (Stage 2 in "
        "the runbook)"
    )


# ── Dockerfile paths resolve ─────────────────────────────────────────


def test_dockerfile_paths_exist(services: dict):
    """A typo in `dockerfilePath` makes Render's build stage fail before
    deploy even starts — expensive to debug via the dashboard."""
    for name, svc in services.items():
        dfp = svc.get("dockerfilePath")
        assert dfp, f"{name} missing dockerfilePath"
        # Normalize leading ./ which Render accepts but Path doesn't
        rel = dfp.removeprefix("./")
        assert (REPO_ROOT / rel).is_file(), (
            f"{name} dockerfilePath {dfp!r} does not resolve from repo root"
        )


# ── Blueprint repo field matches Deploy-button URL ───────────────────


def test_services_repo_matches_button(services: dict):
    """The Deploy button URL points at `limit5/OmniSight-Productizer`.
    The Blueprint `repo:` field on each service must match — otherwise
    Render's wizard shows a repo picker mismatch and confuses the
    operator at the exact step the click is supposed to skip."""
    for name, svc in services.items():
        repo = svc.get("repo", "")
        assert "limit5/OmniSight-Productizer" in repo, (
            f"{name}.repo must reference limit5/OmniSight-Productizer, "
            f"got {repo!r}"
        )
        assert svc.get("branch") == "master", (
            f"{name}.branch must be `master`"
        )


# ── README + companion doc ───────────────────────────────────────────


def test_readme_has_render_deploy_badge():
    """The Deploy-to-Render button in README.md is the user-facing
    entry point for L11 #3. Its exact text is load-bearing for UX
    discoverability — if the badge SVG URL drifts, the README renders
    a broken image."""
    text = README.read_text()
    assert "render.com/images/deploy-to-render-button.svg" in text, (
        "README must embed the official Deploy-to-Render badge SVG"
    )
    assert "render.com/deploy?repo=" in text, (
        "README must link to render.com/deploy?repo= — that's Render's "
        "documented one-click Blueprint flow"
    )
    assert "limit5/OmniSight-Productizer" in text, (
        "Render Deploy button must reference the canonical GitHub repo"
    )


def test_readme_references_the_render_spec_file():
    """Link from README → deploy/render/render.yaml lets users audit
    the Blueprint before clicking. Required for trust + for symmetry
    with the DO / Railway entries."""
    text = README.read_text()
    assert "deploy/render/render.yaml" in text, (
        "README must link to deploy/render/render.yaml"
    )
    assert "deploy/render/README.md" in text, (
        "README must link to the Render runbook"
    )


def test_companion_readme_exists_and_documents_flow():
    """`deploy/render/README.md` is the operator's only source for the
    2-stage post-deploy flow (Stage 2 sets the CORS origin + NEXT_PUBLIC_API_URL
    manually because Render Blueprints can't template URL strings). A
    stub / empty file = deploys come up with CORS broken and the operator
    has no guidance for the fix."""
    assert SPEC_README.is_file(), (
        "deploy/render/README.md must exist (post-deploy runbook)"
    )
    body = SPEC_README.read_text()
    assert len(body) > 800, (
        "companion README is suspiciously short — Render deploys need "
        "the 2-stage post-deploy walkthrough + env matrix"
    )
    # The runbook MUST explain every load-bearing contract.
    for required in (
        "OMNISIGHT_AUTH_MODE",
        "OMNISIGHT_DEBUG",
        "OMNISIGHT_COOKIE_SECURE",
        "OMNISIGHT_ADMIN_EMAIL",
        "OMNISIGHT_ADMIN_PASSWORD",
        "OMNISIGHT_FRONTEND_ORIGIN",
        "NEXT_PUBLIC_API_URL",
        "BACKEND_URL",
        "omnisight-backend",
        "omnisight-frontend",
        "pserv",
        "sync: false",
        "onrender.com",
        "Stage 2",
    ):
        assert required in body, (
            f"companion README missing required mention of {required!r}"
        )
