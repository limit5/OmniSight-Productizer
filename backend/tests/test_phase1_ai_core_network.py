"""Phase-1 wire-up (2026-04-20) — docker-compose ai_core network contract.

The Phase-1 sub-phase 1-1 + 1-2 commit attaches backend-a + backend-b
to the external ``omnisight-ai-core_omnisight_net`` network so they can
reach the sibling ``ai_cache`` Redis instance (and later Gemma4 inference
endpoint). This test file pins the declarative contract so a future
operator cannot accidentally:

  * rename the external network and forget to update the compose file
    (silent failure — backend starts with in-memory fallback and nobody
    notices until quota halves under load)
  * remove the ``ai_core`` attachment from backend-a or backend-b
  * attach caddy / frontend / cloudflared / docker-socket-proxy to
    ai_core (widens lateral-movement blast radius beyond what Phase 1
    needs — only backend calls Redis)
  * drop the ``external: true`` flag (would cause compose to CREATE
    the network on ``up`` and detach it from the sibling stack)
  * drop the ``default`` network from backend-a/b (would sever them
    from caddy / docker-socket-proxy)

All checks parse the YAML directly with ``yaml.safe_load`` — no docker
daemon required, so the test runs in CI with zero infrastructure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO_ROOT / "docker-compose.prod.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    yaml = pytest.importorskip("yaml")
    text = _COMPOSE.read_text(encoding="utf-8")
    # The file carries a top-level ``version:`` key that modern compose
    # warns about — ``yaml.safe_load`` handles it fine, just note the
    # warning is cosmetic.
    return yaml.safe_load(text)


# ─── top-level networks block ──────────────────────────────────────


def test_ai_core_network_is_declared_external(compose):
    networks = compose.get("networks") or {}
    assert "ai_core" in networks, (
        "Phase-1 contract: top-level ``networks:`` block must declare "
        "``ai_core`` (attached to by backend-a/b). Missing means compose "
        "silently falls back to ``default`` only and Redis becomes "
        "unreachable again."
    )
    ai_core = networks["ai_core"]
    assert ai_core.get("external") is True, (
        "ai_core MUST be ``external: true`` — the network is created by "
        "the sibling ``omnisight-ai-core`` compose stack. Without this "
        "flag compose would create its own detached network on ``up``."
    )
    assert ai_core.get("name") == "omnisight-ai-core_omnisight_net", (
        "External network name is frozen: it must match the physical "
        "network name created by the ai-core compose project. Renaming "
        "ai-core's compose project breaks this contract — handle that "
        "separately if it ever changes."
    )


# ─── backend services MUST attach to both default + ai_core ────────


@pytest.mark.parametrize("svc", ["backend-a", "backend-b"])
def test_backend_replica_is_dual_homed(compose, svc):
    services = compose.get("services") or {}
    assert svc in services, f"service {svc} missing from compose"
    nets = services[svc].get("networks") or {}
    # Compose accepts both dict-form (our style) and list-form. Normalize.
    if isinstance(nets, list):
        keys = set(nets)
    else:
        keys = set(nets.keys())
    assert "default" in keys, (
        f"{svc} must remain attached to ``default`` for caddy / "
        "frontend / cloudflared / docker-socket-proxy reach; losing "
        "this severs the productizer-internal LB mesh."
    )
    assert "ai_core" in keys, (
        f"{svc} must be attached to ``ai_core`` for ai_cache (Redis) "
        "reach. Phase-1 rationale: without this, rate_limit.py falls "
        "back to per-replica in-memory (quota halved) and shared_state "
        "loses cross-replica atomicity — two regressions, one cause."
    )


# ─── security: other services must NOT be on ai_core ───────────────


_AI_CORE_DENYLIST: tuple[str, ...] = (
    "caddy",
    "frontend",
    "cloudflared",
    "docker-socket-proxy",
    "prometheus",
    "grafana",
)


@pytest.mark.parametrize("svc", _AI_CORE_DENYLIST)
def test_non_backend_services_do_not_join_ai_core(compose, svc):
    """Principle of least lateral reach: only backend needs to talk to
    Redis / Gemma4. Widening the ai_core attachment to any other
    service in this compose enlarges the attack surface between the
    two stacks without adding capability.
    """
    services = compose.get("services") or {}
    if svc not in services:
        pytest.skip(f"optional service {svc} absent from compose — nothing to assert")
    nets = services[svc].get("networks") or {}
    if isinstance(nets, list):
        keys = set(nets)
    else:
        keys = set(nets.keys())
    assert "ai_core" not in keys, (
        f"service ``{svc}`` must NOT join ai_core — only backend-a/b "
        "legitimately need that cross-stack reach. If you have a new "
        "reason (e.g. prometheus scraping ai_cache metrics), justify it "
        "in the compose comment + update this test's denylist with the "
        "reason spelled out."
    )


# ─── env.file contract — operators flip these in .env ───────────────


def test_env_file_referenced_by_backends(compose):
    """Both backends must read from ``.env`` so the operator's
    ``OMNISIGHT_REDIS_URL`` + ``OMNISIGHT_WORKERS`` knobs reach
    uvicorn. Dropping this would make the wire-up invisible to
    ``docker compose up`` even with the network attached."""
    for svc in ("backend-a", "backend-b"):
        env_file = compose["services"][svc].get("env_file")
        assert env_file, f"{svc} is missing env_file"
        # Accept list or scalar form.
        paths = env_file if isinstance(env_file, list) else [env_file]
        assert ".env" in paths, (
            f"{svc} must reference .env — Phase-1 knobs live there"
        )
