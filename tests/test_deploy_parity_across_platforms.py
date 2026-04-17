r"""L11 #338 #4 — cross-platform deploy parity contract.

Siblings `test_digitalocean_app_spec.py` / `test_railway_spec.py` /
`test_render_blueprint.py` each verify **one** platform in depth. This file
verifies the **cross-cutting invariant** that ALL three platforms uniformly
deliver the triple demanded by the TODO item:

    per platform:  services (backend + frontend)
                 + env vars (from .env.example)
                 + build commands

If a future PR lands an env var on DO + Render but forgets Railway's
dashboard-managed README matrix, or renames Dockerfile.backend without
updating the three specs in lock-step, this file fails. The per-platform
suites cannot catch that kind of drift because they only see one platform.

Shape conventions:

- DO (`deploy/digitalocean/app.yaml`) + Render (`deploy/render/render.yaml`)
  embed env vars IN the spec file (services[].envs / services[].envVars).
- Railway (`deploy/railway/railway.json`) has NO env schema — Railway's
  config-as-code is deploy-policy only; env vars live in the dashboard
  and `deploy/railway/README.md` is the operator-facing matrix. So
  Railway's env coverage is verified against the README text, not the
  JSON — a deliberate asymmetry, not a bug. The per-platform Railway
  suite pins that the JSON MUST NOT embed an env block.
- All three platforms build from the same pair of Dockerfiles at repo
  root (`Dockerfile.backend` + `Dockerfile.frontend`). This is load-
  bearing because it is how `docker-compose.prod.yml` + GHCR images +
  all three cloud specs share one image definition.

Cost: <100ms per run, stdlib + pyyaml only, no network, no Docker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

ENV_EXAMPLE = REPO_ROOT / ".env.example"

DOCKERFILE_BACKEND = REPO_ROOT / "Dockerfile.backend"
DOCKERFILE_FRONTEND = REPO_ROOT / "Dockerfile.frontend"

DO_SPEC = REPO_ROOT / "deploy" / "digitalocean" / "app.yaml"
DO_README = REPO_ROOT / "deploy" / "digitalocean" / "README.md"
RAILWAY_SPEC = REPO_ROOT / "deploy" / "railway" / "railway.json"
RAILWAY_README = REPO_ROOT / "deploy" / "railway" / "README.md"
RENDER_SPEC = REPO_ROOT / "deploy" / "render" / "render.yaml"
RENDER_README = REPO_ROOT / "deploy" / "render" / "README.md"

# The canonical env set each platform must cover on the BACKEND service.
# Sourced directly from `.env.example` — these are the variables the
# Internet-exposure block says MUST be set for a production deploy
# (plus the K1 bootstrap admin pair + LLM provider + one API key for a
# reachable demo). If `.env.example` grows a new production-required
# envi, add it here first — the test will then fail until every platform
# spec / README matrix is updated to match.
CRITICAL_BACKEND_ENVS = [
    # Production hard-pins from .env.example's Internet-exposure block
    "OMNISIGHT_DEBUG",
    "OMNISIGHT_AUTH_MODE",
    "OMNISIGHT_COOKIE_SECURE",
    # K1 bootstrap admin (must_change_password forces rotation on login)
    "OMNISIGHT_ADMIN_EMAIL",
    "OMNISIGHT_ADMIN_PASSWORD",
    # LLM reachability — provider selector + at least one key slot
    "OMNISIGHT_LLM_PROVIDER",
    "OMNISIGHT_ANTHROPIC_API_KEY",
    # CORS — browser must be allowed to call the public origin
    "OMNISIGHT_FRONTEND_ORIGIN",
]

# Frontend-side envs that must appear on every platform's frontend
# service (DO + Render) or frontend env matrix (Railway README).
CRITICAL_FRONTEND_ENVS = [
    "NODE_ENV",
    # Next.js rewrites() target — without it /api/* calls from the
    # browser proxy nowhere and the demo 404s the first time you click
    # anything.
    "BACKEND_URL",
]

# Values the three production hard-pins MUST take (case-sensitive where
# relevant — "strict" is lowercase because backend/config.py compares
# with str.lower()).
PRODUCTION_HARD_PIN_VALUES = {
    "OMNISIGHT_DEBUG": "false",
    "OMNISIGHT_AUTH_MODE": "strict",
    "OMNISIGHT_COOKIE_SECURE": "true",
}


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def do_spec() -> dict:
    assert DO_SPEC.is_file(), f"{DO_SPEC} missing (L11 #1 deliverable)"
    return yaml.safe_load(DO_SPEC.read_text())


@pytest.fixture(scope="module")
def railway_spec() -> dict:
    assert RAILWAY_SPEC.is_file(), f"{RAILWAY_SPEC} missing (L11 #2)"
    return json.loads(RAILWAY_SPEC.read_text())


@pytest.fixture(scope="module")
def railway_readme_text() -> str:
    assert RAILWAY_README.is_file(), f"{RAILWAY_README} missing (L11 #2)"
    return RAILWAY_README.read_text()


@pytest.fixture(scope="module")
def render_spec() -> dict:
    assert RENDER_SPEC.is_file(), f"{RENDER_SPEC} missing (L11 #3)"
    return yaml.safe_load(RENDER_SPEC.read_text())


@pytest.fixture(scope="module")
def env_example_text() -> str:
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} missing"
    return ENV_EXAMPLE.read_text()


def _do_service(spec: dict, name: str) -> dict:
    for svc in spec.get("services", []):
        if svc.get("name") == name:
            return svc
    raise AssertionError(
        f"DO app.yaml missing `services[].name == {name!r}` — parity "
        f"check needs both backend + frontend declared"
    )


def _do_env_keys(service: dict) -> set[str]:
    return {e["key"] for e in service.get("envs", [])}


def _render_service(spec: dict, name: str) -> dict:
    for svc in spec.get("services", []):
        if svc.get("name") == name:
            return svc
    raise AssertionError(
        f"render.yaml missing `services[].name == {name!r}`"
    )


def _render_env_keys(service: dict) -> set[str]:
    return {e["key"] for e in service.get("envVars", [])}


# ── Section 1 — platform discovery: all three platforms present ──────


def test_all_three_platform_dirs_exist():
    """The TODO's L11 calls out three target platforms. If someone
    deletes one `deploy/<platform>/` dir, this fails before any of the
    per-platform suites run (since they each fixture-skip on missing
    files).
    """
    for spec_path, readme_path, platform in [
        (DO_SPEC, DO_README, "digitalocean"),
        (RAILWAY_SPEC, RAILWAY_README, "railway"),
        (RENDER_SPEC, RENDER_README, "render"),
    ]:
        assert spec_path.is_file(), (
            f"{platform}: spec file missing at {spec_path.relative_to(REPO_ROOT)}"
        )
        assert readme_path.is_file(), (
            f"{platform}: README missing at {readme_path.relative_to(REPO_ROOT)}"
        )


def test_all_three_deploy_buttons_present_in_root_readme():
    """Users land on README.md, not the deploy/ subdirs. Each platform
    must have its one-click Deploy badge in the root README, or the
    whole L11 UX is broken."""
    text = (REPO_ROOT / "README.md").read_text()
    assert "cloud.digitalocean.com/apps/new" in text, (
        "README.md must embed the DigitalOcean Deploy button link"
    )
    assert "railway.com/new/template" in text, (
        "README.md must embed the Railway Deploy button link"
    )
    assert "render.com/deploy" in text, (
        "README.md must embed the Render Deploy button link"
    )


# ── Section 2 — services: backend + frontend on every platform ───────


def test_digitalocean_declares_backend_and_frontend_services(do_spec):
    names = {s.get("name") for s in do_spec.get("services", [])}
    assert {"backend", "frontend"}.issubset(names), (
        f"DO app.yaml must declare both `backend` + `frontend` services; "
        f"got {names}"
    )


def test_railway_topology_documents_both_services(railway_readme_text):
    """Railway's JSON is single-service by schema (see sibling suite's
    `test_spec_does_not_embed_env_block`). The README's Topology table
    is where both services are defined — a missing service there means
    the operator only sets up one half of the deploy.
    """
    txt = railway_readme_text
    assert "backend" in txt.lower(), (
        "Railway README must document the backend service"
    )
    assert "frontend" in txt.lower(), (
        "Railway README must document the frontend service"
    )
    assert "Dockerfile.backend" in txt and "Dockerfile.frontend" in txt, (
        "Railway README's Topology table must name both Dockerfiles "
        "so the operator knows which Dockerfile path to set per service"
    )


def test_render_declares_backend_and_frontend_services(render_spec):
    names = {s.get("name") for s in render_spec.get("services", [])}
    assert {"omnisight-backend", "omnisight-frontend"}.issubset(names), (
        f"render.yaml must declare both omnisight-backend + omnisight-"
        f"frontend services; got {names}"
    )


# ── Section 3 — build commands: reference real Dockerfiles ───────────


def test_both_dockerfiles_exist_at_repo_root():
    """The parity of "all three platforms build from one pair of
    Dockerfiles" collapses if the Dockerfiles themselves are missing /
    renamed. This is the foundation the other build-command assertions
    rest on.
    """
    assert DOCKERFILE_BACKEND.is_file(), (
        f"Dockerfile.backend must exist at repo root "
        f"({DOCKERFILE_BACKEND}) — all three cloud specs reference it"
    )
    assert DOCKERFILE_FRONTEND.is_file(), (
        f"Dockerfile.frontend must exist at repo root "
        f"({DOCKERFILE_FRONTEND})"
    )


def test_digitalocean_build_commands_reference_both_dockerfiles(do_spec):
    be = _do_service(do_spec, "backend")
    fe = _do_service(do_spec, "frontend")
    assert be.get("dockerfile_path") == "Dockerfile.backend", (
        f"DO backend must build from Dockerfile.backend; got "
        f"{be.get('dockerfile_path')!r}"
    )
    assert fe.get("dockerfile_path") == "Dockerfile.frontend", (
        f"DO frontend must build from Dockerfile.frontend; got "
        f"{fe.get('dockerfile_path')!r}"
    )


def test_railway_build_command_pins_dockerfile_backend(railway_spec):
    """Railway's default builder is NIXPACKS (language auto-detect),
    which would ignore `Dockerfile.backend` entirely. `build.builder`
    MUST be `DOCKERFILE` AND `dockerfilePath` MUST name the backend
    Dockerfile. Sibling suite already pins this; we re-assert at the
    parity layer so "all three build from the same Dockerfile" is one
    test grep away."""
    build = railway_spec.get("build", {})
    assert build.get("builder") == "DOCKERFILE", (
        f"Railway build.builder must be 'DOCKERFILE'; got "
        f"{build.get('builder')!r} (NIXPACKS would rebuild from source "
        f"and ignore Dockerfile.backend)"
    )
    assert build.get("dockerfilePath") == "Dockerfile.backend", (
        f"Railway build.dockerfilePath must be 'Dockerfile.backend'; "
        f"got {build.get('dockerfilePath')!r}"
    )


def test_railway_frontend_dockerfile_documented_in_readme(railway_readme_text):
    """Railway's frontend service is NOT in the JSON (single-service
    schema). Its Dockerfile selection happens via dashboard — so the
    README's post-deploy runbook MUST instruct the operator to set
    `Dockerfile.frontend` explicitly, else Railway's auto-detect picks
    up the Next.js source and rebuilds it bypassing the multi-stage
    standalone image."""
    assert "Dockerfile.frontend" in railway_readme_text, (
        "Railway README must name Dockerfile.frontend in the operator "
        "runbook — frontend service is dashboard-configured, not JSON-"
        "configured, so the README is the only place this can live"
    )


def test_render_build_commands_reference_both_dockerfiles(render_spec):
    be = _render_service(render_spec, "omnisight-backend")
    fe = _render_service(render_spec, "omnisight-frontend")
    # Render accepts `./Dockerfile.backend` or `Dockerfile.backend` —
    # normalize by stripping a leading "./".
    be_path = (be.get("dockerfilePath") or "").lstrip("./")
    fe_path = (fe.get("dockerfilePath") or "").lstrip("./")
    assert be_path == "Dockerfile.backend", (
        f"Render backend must build from Dockerfile.backend; got "
        f"{be.get('dockerfilePath')!r}"
    )
    assert fe_path == "Dockerfile.frontend", (
        f"Render frontend must build from Dockerfile.frontend; got "
        f"{fe.get('dockerfilePath')!r}"
    )


def test_backend_start_commands_all_name_uvicorn_on_backend_main(
    railway_spec, render_spec
):
    """The backend start command is the "build command" at the runtime
    edge — Railway + Render override the Dockerfile CMD to inject
    `$PORT` / `$OMNISIGHT_WORKERS`. All overrides must still launch the
    same module (`backend.main:app`). A typo here makes the pod boot
    with an empty uvicorn → 502 on every request."""
    railway_cmd = railway_spec.get("deploy", {}).get("startCommand", "")
    render_be = _render_service(render_spec, "omnisight-backend")
    render_cmd = render_be.get("dockerCommand", "")
    for label, cmd in [("railway", railway_cmd), ("render", render_cmd)]:
        assert "uvicorn" in cmd, f"{label} start command must invoke uvicorn"
        assert "backend.main:app" in cmd, (
            f"{label} start command must launch backend.main:app "
            f"(got: {cmd!r})"
        )
    # DO doesn't override the Dockerfile CMD — it inherits the image's
    # ENTRYPOINT/CMD verbatim, which is already pinned by the backend
    # Dockerfile test suite (`test_dockerfile_image_size.py`).


# ── Section 4 — env vars from .env.example on every platform ─────────


def test_env_example_defines_the_critical_backend_envs(env_example_text):
    """Sanity gate: the env names CRITICAL_BACKEND_ENVS pins must all
    appear in `.env.example` (even if as commented `# OMNISIGHT_...`
    defaults). If `.env.example` drops one, the parity contract loses
    its source-of-truth and should fail loudly here, not in the middle
    of the per-platform checks below."""
    for name in CRITICAL_BACKEND_ENVS:
        assert name in env_example_text, (
            f".env.example missing the env name {name!r} — parity "
            f"contract lists it as a production-required env, so the "
            f"source-of-truth file must mention it (even if commented)"
        )


def test_digitalocean_backend_covers_all_critical_envs(do_spec):
    be = _do_service(do_spec, "backend")
    keys = _do_env_keys(be)
    missing = [k for k in CRITICAL_BACKEND_ENVS if k not in keys]
    assert not missing, (
        f"DO backend service missing critical envs: {missing}. "
        f"Every production-required env from .env.example must be "
        f"declared (with `type: SECRET` for secrets, plaintext for "
        f"config)."
    )


def test_railway_backend_env_matrix_covers_all_critical_envs(
    railway_readme_text,
):
    """Railway's JSON can't hold envs — README is the env matrix.
    Every CRITICAL_BACKEND_ENV must be named in the README text, else
    the operator misses it during dashboard setup."""
    missing = [k for k in CRITICAL_BACKEND_ENVS if k not in railway_readme_text]
    assert not missing, (
        f"deploy/railway/README.md missing env names from matrix: "
        f"{missing}. Railway's JSON can't carry envs; README is the "
        f"operator's only guide for what to paste into the Railway "
        f"dashboard."
    )


def test_render_backend_covers_all_critical_envs(render_spec):
    be = _render_service(render_spec, "omnisight-backend")
    keys = _render_env_keys(be)
    missing = [k for k in CRITICAL_BACKEND_ENVS if k not in keys]
    assert not missing, (
        f"Render backend envVars missing critical envs: {missing}. "
        f"Blueprint apply will succeed without them but the app will "
        f"either fail boot-time config validation (strict-mode checks) "
        f"or silently run in a degraded posture."
    )


def test_digitalocean_frontend_covers_critical_frontend_envs(do_spec):
    fe = _do_service(do_spec, "frontend")
    keys = _do_env_keys(fe)
    missing = [k for k in CRITICAL_FRONTEND_ENVS if k not in keys]
    assert not missing, (
        f"DO frontend service missing critical envs: {missing}"
    )


def test_railway_frontend_env_matrix_covers_critical_frontend_envs(
    railway_readme_text,
):
    """Railway's frontend service is dashboard-only (no JSON). The
    README's 'Frontend service' env matrix must name BACKEND_URL +
    NODE_ENV explicitly or the operator misses them."""
    for name in CRITICAL_FRONTEND_ENVS:
        assert name in railway_readme_text, (
            f"deploy/railway/README.md missing frontend env "
            f"{name!r} from its env matrix"
        )


def test_render_frontend_covers_critical_frontend_envs(render_spec):
    fe = _render_service(render_spec, "omnisight-frontend")
    keys = _render_env_keys(fe)
    missing = [k for k in CRITICAL_FRONTEND_ENVS if k not in keys]
    assert not missing, (
        f"Render frontend envVars missing critical envs: {missing}"
    )


# ── Section 5 — production hard-pin values match across platforms ────


def _normalize_env_value(raw) -> str:
    """DO + Render allow bool YAML values (`true`/`false`) OR strings
    (`"true"`/`"false"`). Normalize to lowercase string for comparison.
    """
    if isinstance(raw, bool):
        return "true" if raw else "false"
    return str(raw).strip().lower()


def test_digitalocean_production_hard_pins_match_env_example(do_spec):
    be = _do_service(do_spec, "backend")
    envs = {e["key"]: e for e in be.get("envs", [])}
    for name, expected in PRODUCTION_HARD_PIN_VALUES.items():
        entry = envs.get(name)
        assert entry is not None, f"DO backend missing hard-pin {name}"
        got = _normalize_env_value(entry.get("value"))
        assert got == expected, (
            f"DO backend {name}: expected {expected!r}, got {got!r}"
        )


def test_render_production_hard_pins_match_env_example(render_spec):
    be = _render_service(render_spec, "omnisight-backend")
    envs = {e["key"]: e for e in be.get("envVars", [])}
    for name, expected in PRODUCTION_HARD_PIN_VALUES.items():
        entry = envs.get(name)
        assert entry is not None, f"Render backend missing hard-pin {name}"
        got = _normalize_env_value(entry.get("value"))
        assert got == expected, (
            f"Render backend {name}: expected {expected!r}, got {got!r}"
        )


def test_railway_production_hard_pins_documented_with_values(
    railway_readme_text,
):
    """Railway README uses a markdown table to pin values. Search for
    each `OMNISIGHT_<NAME>` / `<expected>` pair on the same line — the
    README author can't accidentally drop the expected value while
    leaving the env name, which is what we want for parity."""
    for name, expected in PRODUCTION_HARD_PIN_VALUES.items():
        lines_with_name = [
            line for line in railway_readme_text.splitlines() if name in line
        ]
        assert lines_with_name, (
            f"Railway README missing env name {name!r}"
        )
        paired = [line for line in lines_with_name if expected in line]
        assert paired, (
            f"Railway README names {name!r} but no row pairs it with "
            f"the expected production value {expected!r}. Rows found: "
            f"{lines_with_name}"
        )


# ── Section 6 — no live API keys pasted on any platform spec ─────────


def test_no_live_api_keys_leaked_across_any_platform_spec():
    """A single contract that re-asserts the per-platform secret-hygiene
    invariants at the cross-platform layer. If a future PR pastes a
    real key into any of the three specs (or any README), this fires
    before the per-platform suite does — and the message names all three
    paths at once, making triage one grep."""
    real_key_prefixes = (
        "sk-ant-api",  # Anthropic
        "sk-proj-",  # OpenAI project key
        "AIzaSy",  # Google API key
        "xai-",  # xAI
    )
    paths = [DO_SPEC, RAILWAY_SPEC, RENDER_SPEC, DO_README, RAILWAY_README, RENDER_README]
    offenders: list[tuple[Path, str]] = []
    for path in paths:
        text = path.read_text()
        for prefix in real_key_prefixes:
            if prefix in text:
                # README files are allowed to show the prefix in
                # `"Paste your real sk-ant-... key"` style instructions
                # — those have the literal `...` continuation marker.
                # Reject anything else (a full key would have
                # alphanumeric chars after the prefix).
                for line in text.splitlines():
                    if prefix not in line:
                        continue
                    tail = line.split(prefix, 1)[1]
                    if not tail:
                        continue
                    # Consume the next non-"." char; if it's alnum we
                    # probably have an actual key, not a placeholder.
                    first_char = tail.lstrip(".")[:1]
                    if first_char.isalnum():
                        offenders.append((path, line.strip()))
    assert not offenders, (
        "Live API key prefix detected in deploy specs/READMEs — see:\n"
        + "\n".join(f"  {p.relative_to(REPO_ROOT)}: {l}" for p, l in offenders)
    )
