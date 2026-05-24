"""OP-1663 — dev compose ``dag-executor`` stanza: ships INERT + default OFF.

Drift-guard for the config-only wiring added to
``deploy/dev/docker-compose.yml`` (design doc §7 Phase 1). The stanza is
forward-looking wiring for ``backend.dag_executor`` and MUST stay harmless
to ship: a future operator edit cannot silently turn it into a live
service without flipping a test red here.

Two independent inert gates (mirrors the systemd unit
``deploy/systemd/omnisight-dag-executor@.service``):

  * Gate 1 — profile-gated: the service lives behind the ``dag-executor``
    compose profile, so the documented dev bring-up (no ``--profile``)
    does NOT start it. Config shipped, service not started.
  * Gate 2 — armed flag DEFAULT OFF: ``OMNISIGHT_DAG_EXECUTOR_ENABLED``
    defaults to ``0`` (``backend.dag_executor.is_enabled`` arms only on
    exactly ``"1"``), so even a profiled bring-up is a no-op exit 0.

Plus the opt-in plan allowlist (``OMNISIGHT_DAG_EXECUTOR_PLAN_ALLOWLIST``,
empty == nothing allowed) and the ticket MUST-NOTs (no docker.sock).

The YAML-parse assertions run with NO docker socket (CI-safe, mirrors
``backend/tests/test_threat_model_compose_lint.py``). The
``docker compose config`` checks additionally confirm the renderer
accepts the stanza + the env defaults, and SKIP when the CLI is absent —
neither ever runs ``up`` (ticket MUST-NOT: no live bring-up).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_COMPOSE = REPO_ROOT / "deploy" / "dev" / "docker-compose.yml"
SVC = "dag-executor"
PROFILE = "dag-executor"
ENABLE_ENV = "OMNISIGHT_DAG_EXECUTOR_ENABLED"
ALLOWLIST_ENV = "OMNISIGHT_DAG_EXECUTOR_PLAN_ALLOWLIST"


# ─── helpers ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(DEV_COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def executor(compose: dict) -> dict:
    services = compose.get("services") or {}
    assert SVC in services, (
        f"OP-1663: ``services.{SVC}`` MUST exist in deploy/dev/"
        "docker-compose.yml — the stanza is the wire-up point for the "
        "DAG executor (design doc §7 Phase 1)."
    )
    return services[SVC]


def _env_map(service: dict) -> dict[str, str]:
    env = service.get("environment") or []
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    return dict(e.split("=", 1) for e in env if "=" in e)


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _compose_env() -> dict[str, str]:
    env = os.environ.copy()
    env["POSTGRES_PASSWORD"] = "test-password"
    # A tag is required (``${OMNISIGHT_IMAGE_TAG:?...}``); config-only.
    env.setdefault("OMNISIGHT_IMAGE_TAG", "dev-test")
    # Clear any host opt-in so we observe the shipped DEFAULTS.
    env.pop(ENABLE_ENV, None)
    env.pop(ALLOWLIST_ENV, None)
    return env


# ─── Gate 1: profile-gated (config shipped, NOT started) ───────────


def test_stanza_is_profile_gated(executor: dict) -> None:
    """The service sits behind the ``dag-executor`` profile.

    A profiled service is excluded from a plain ``docker compose up`` —
    the AC "Deploy: config shipped, NOT started". The documented dev
    bring-up in the file header passes no ``--profile``.
    """
    profiles = executor.get("profiles") or []
    assert profiles == [PROFILE], (
        f"{SVC} MUST be gated behind exactly the ``{PROFILE}`` profile "
        f"(got {profiles!r}). Without a profile it would start on the "
        "default ``docker compose -p omnisight-dev ... up`` — the stanza "
        "must ship inert, not run."
    )


# ─── Gate 2: armed flag DEFAULT OFF ────────────────────────────────


def test_enable_flag_defaults_off(executor: dict) -> None:
    """``OMNISIGHT_DAG_EXECUTOR_ENABLED`` ships defaulting to ``0``.

    ``backend.dag_executor.is_enabled`` arms ONLY on exactly ``"1"``, so
    a ``0`` (or unset) default keeps the entrypoint a no-op exit 0 even
    if an operator opts into the profile.
    """
    env = _env_map(executor)
    assert ENABLE_ENV in env, f"{ENABLE_ENV} must be set in the stanza."
    val = env[ENABLE_ENV]
    # Interpolation form ``${VAR:-0}`` — default resolves to 0, operator
    # can flip to 1, but the SHIPPED default must never be 1.
    assert ":-0}" in val or val == "0", (
        f"{ENABLE_ENV} default MUST be 0 (got {val!r}). Default OFF is "
        "the load-bearing inert gate."
    )
    assert val.strip() != "1", (
        f"{ENABLE_ENV} MUST NOT ship hard-armed to 1 (got {val!r})."
    )


# ─── opt-in plan allowlist ─────────────────────────────────────────


def test_plan_allowlist_present_and_empty_by_default(executor: dict) -> None:
    """Opt-in allowlist ships empty == nothing allowed (fail-closed)."""
    env = _env_map(executor)
    assert ALLOWLIST_ENV in env, (
        f"{ALLOWLIST_ENV} MUST be wired in the stanza — the ticket asks "
        "for an opt-in plan allowlist config."
    )
    val = env[ALLOWLIST_ENV]
    # ``${VAR:-}`` resolves to empty; never a non-empty hard-coded list.
    assert val in ("", "${%s:-}" % ALLOWLIST_ENV) or val.endswith(":-}"), (
        f"{ALLOWLIST_ENV} MUST default empty (got {val!r}) so nothing is "
        "allowed until an operator explicitly opts in."
    )


# ─── alternate entrypoint ──────────────────────────────────────────


def test_runs_dag_executor_entrypoint(executor: dict) -> None:
    """Reuses the backend image as the ``backend.dag_executor`` entry."""
    command = executor.get("command")
    joined = " ".join(command) if isinstance(command, list) else str(command)
    assert "backend.dag_executor" in joined, (
        f"{SVC} command MUST run ``python3 -m backend.dag_executor`` "
        f"(got {command!r}) — the alternate entrypoint over the backend "
        "image, mirroring the systemd unit."
    )


# ─── MUST-NOT: no docker socket ────────────────────────────────────


def test_no_docker_socket_mount(executor: dict) -> None:
    """Ticket MUST-NOT: the stanza must not require a docker socket.

    The inert skeleton spawns no containers (see the systemd unit's
    relaxed ``Wants=docker.service``), so bind-mounting the daemon socket
    would be both unnecessary and a container-escape primitive.
    """
    volumes = executor.get("volumes") or []
    for v in volumes:
        assert "/var/run/docker.sock" not in str(v), (
            f"{SVC} MUST NOT bind-mount /var/run/docker.sock (got {v!r})."
        )


# ─── inert restart policy ──────────────────────────────────────────


def test_restart_policy_is_inert(executor: dict) -> None:
    """A disabled executor exits 0; restart must not loop that exit.

    ``always`` / ``unless-stopped`` would respawn the clean no-op exit
    forever; ``no`` (mirroring the systemd unit's ``on-failure``) keeps
    the shipped-inert stanza quiet.
    """
    assert executor.get("restart") == "no", (
        f"{SVC} restart MUST be ``no`` (got {executor.get('restart')!r}) "
        "so an un-armed no-op exit 0 is not restart-looped."
    )


def test_depends_on_postgres_healthy(executor: dict) -> None:
    dep = executor.get("depends_on") or {}
    assert isinstance(dep, dict) and "postgres" in dep, (
        "dag-executor must wait on postgres (long-form depends_on)."
    )
    assert dep["postgres"].get("condition") == "service_healthy", (
        "postgres dep must gate on ``service_healthy`` like backend-a."
    )


# ─── docker compose config (local validation, NEVER up) ────────────


def test_compose_config_validates_stanza() -> None:
    """``docker compose config`` accepts the profiled stanza (no ``up``)."""
    if not _docker_available():
        pytest.skip("docker CLI not available")
    proc = subprocess.run(
        ["docker", "compose", "-f", str(DEV_COMPOSE),
         "--profile", PROFILE, "config", "--format", "json"],
        cwd=REPO_ROOT, env=_compose_env(),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    import json

    services = json.loads(proc.stdout)["services"]
    assert SVC in services, f"{SVC} missing from rendered config"
    rendered_env = services[SVC].get("environment") or {}
    assert rendered_env.get(ENABLE_ENV) == "0", (
        f"rendered {ENABLE_ENV} default MUST be ``0`` (got "
        f"{rendered_env.get(ENABLE_ENV)!r})."
    )
    assert rendered_env.get(ALLOWLIST_ENV) == "", (
        f"rendered {ALLOWLIST_ENV} default MUST be empty (got "
        f"{rendered_env.get(ALLOWLIST_ENV)!r})."
    )


def test_default_bring_up_excludes_stanza() -> None:
    """Without ``--profile`` the service is NOT in the bring-up set."""
    if not _docker_available():
        pytest.skip("docker CLI not available")
    proc = subprocess.run(
        ["docker", "compose", "-f", str(DEV_COMPOSE), "config", "--services"],
        cwd=REPO_ROOT, env=_compose_env(),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    services = set(proc.stdout.split())
    assert SVC not in services, (
        f"{SVC} MUST be absent from the default (no-profile) service set "
        f"(got {sorted(services)}) — config shipped, NOT started."
    )
    assert "backend-a" in services, "sanity: backend-a is a default service"
