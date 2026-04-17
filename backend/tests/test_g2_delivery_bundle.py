"""G2 delivery bundle — cross-file consistency contract tests.

TODO row 1350 (G2 final deliverable row):
    交付：`deploy/reverse-proxy/Caddyfile`、`docker-compose.prod.yml` diff、
    `scripts/deploy.sh` rolling 模式

The five G2 checkboxes (rows 1345-1349) each ship their own contract
test module that pins the *individual* file's shape:

    * test_reverse_proxy_caddyfile.py         — G2 #1 Caddy listener + upstream
    * test_compose_dual_backend_replicas.py   — G2 #2 dual replica compose
    * test_deploy_sh_rolling.py               — G2 #3 rolling-restart shape
    * test_reverse_proxy_health_eject.py      — G2 #4 Caddy health+fail_timeout
    * test_rolling_deploy_soak.py             — G2 #5 0×5xx soak

This module is the *delivery-row* companion to row 1350: it locks down
the **cross-file invariants** the three primary deliverables must agree
on. Any one of them can be refactored in isolation, but the moment one
renames a service, flips a port, or moves off `/readyz` *without
updating the others*, this file fails and forces the author to reconcile
the drift in the same commit.

Specifically, this file pins:

    1. All three primary deliverables exist and are non-empty.
    2. Caddy upstream addresses == compose service names + internal ports.
    3. `deploy.sh` rolling mode targets the same two services at the same
       host-ports exposed by compose.
    4. All three files speak the same health-probe path (`/readyz`), so a
       path rename has to land everywhere in one PR.
    5. `deploy.sh` drain timeout ≥ 2× Caddy's eject budget (drain must
       outlast the health_fails window, otherwise the drained replica's
       container dies before Caddy has pulled it from the pool — brief
       5xx storm during restart).

The invariants are intentionally structural. CI does **not** boot Caddy,
Docker, or systemd; we verify the checked-in text agrees with itself.
Live behavior is covered by test_rolling_deploy_soak.py's in-memory
integration test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = PROJECT_ROOT / "deploy" / "reverse-proxy" / "Caddyfile"
COMPOSE_PROD = PROJECT_ROOT / "docker-compose.prod.yml"
DEPLOY_SH = PROJECT_ROOT / "scripts" / "deploy.sh"

G2_DELIVERABLES = (CADDYFILE, COMPOSE_PROD, DEPLOY_SH)


@pytest.fixture(scope="module")
def caddy_text() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_PROD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def deploy_text() -> str:
    return DEPLOY_SH.read_text(encoding="utf-8")


# ───────────────────────────────────────────────────────────────────
# 1. Deliverables exist
# ───────────────────────────────────────────────────────────────────


class TestDeliverablesExist:
    """All three row-1350 artifacts must be checked in and non-trivial."""

    @pytest.mark.parametrize("path", G2_DELIVERABLES, ids=lambda p: p.name)
    def test_path_exists(self, path: Path) -> None:
        assert path.is_file(), f"G2 deliverable missing: {path}"

    @pytest.mark.parametrize("path", G2_DELIVERABLES, ids=lambda p: p.name)
    def test_not_stub(self, path: Path) -> None:
        # Sanity floor — a stub / placeholder would be well below this.
        # Caddyfile ≈ 4 KB, compose ≈ 6 KB, deploy.sh ≈ 8 KB in their
        # current form; 500 bytes catches accidental truncations.
        assert path.stat().st_size > 500, f"G2 deliverable suspiciously small: {path}"

    @pytest.mark.parametrize("path", G2_DELIVERABLES, ids=lambda p: p.name)
    def test_utf8_clean(self, path: Path) -> None:
        path.read_text(encoding="utf-8")


# ───────────────────────────────────────────────────────────────────
# 2. Upstream contract: Caddy ⇄ compose
# ───────────────────────────────────────────────────────────────────


class TestCaddyComposeUpstreamContract:
    """Caddy's upstream stanza must address the exact compose services
    at the exact internal ports those services listen on."""

    def test_caddy_references_backend_a_8000(self, caddy_text: str) -> None:
        assert "backend-a:8000" in caddy_text, (
            "Caddyfile must reverse_proxy to backend-a:8000 "
            "(matches docker-compose.prod.yml service backend-a)"
        )

    def test_caddy_references_backend_b_8001(self, caddy_text: str) -> None:
        assert "backend-b:8001" in caddy_text, (
            "Caddyfile must reverse_proxy to backend-b:8001 "
            "(matches docker-compose.prod.yml service backend-b)"
        )

    def test_compose_declares_backend_a_service(self, compose_text: str) -> None:
        assert re.search(r"(?m)^  backend-a:\s*$", compose_text), (
            "docker-compose.prod.yml must declare a top-level `backend-a:` service "
            "— Caddyfile upstream depends on the service name for DNS resolution"
        )

    def test_compose_declares_backend_b_service(self, compose_text: str) -> None:
        assert re.search(r"(?m)^  backend-b:\s*$", compose_text), (
            "docker-compose.prod.yml must declare a top-level `backend-b:` service"
        )

    def test_backend_a_listens_on_8000(self, compose_text: str) -> None:
        # backend-a uses the default uvicorn CMD (port 8000) baked into
        # Dockerfile.backend — we assert the host-port mapping 8000:8000,
        # because the container port is what Caddy's service-DNS dials.
        assert re.search(r'(?m)^\s*-\s*"8000:8000"\s*$', compose_text), (
            "backend-a must publish 8000:8000 so Caddy's backend-a:8000 "
            "upstream reaches uvicorn"
        )

    def test_backend_b_listens_on_8001(self, compose_text: str) -> None:
        # backend-b overrides CMD with --port 8001; verify both that the
        # override is present and that the host port is mapped.
        assert "--port 8001" in compose_text, (
            "backend-b command must explicitly `--port 8001` — "
            "Caddyfile upstream backend-b:8001 depends on it"
        )
        assert re.search(r'(?m)^\s*-\s*"8001:8001"\s*$', compose_text), (
            "backend-b must publish 8001:8001 so rolling deploy's "
            "http://localhost:8001/readyz probe reaches uvicorn"
        )

    def test_both_replicas_share_data_volume(self, compose_text: str) -> None:
        # Shared state is a precondition for symmetric upstreams — if the
        # two replicas talked to different DB files, a request routed to
        # B after A wrote would see stale state, and the upstream pool
        # would no longer be "truly symmetric" as the Caddyfile claims.
        matches = re.findall(r"omnisight-data:/app/data", compose_text)
        assert len(matches) >= 2, (
            "Both backend-a and backend-b must mount `omnisight-data:/app/data`; "
            f"found {len(matches)} reference(s)"
        )


# ───────────────────────────────────────────────────────────────────
# 3. deploy.sh rolling ⇄ compose + Caddy contract
# ───────────────────────────────────────────────────────────────────


class TestDeployRollingMatchesTopology:
    """Rolling restart must drive the two services compose declares, at
    the two ports Caddy expects, probing the path all three speak."""

    def test_rolling_restarts_backend_a_first(self, deploy_text: str) -> None:
        # Never parallel, never B-before-A (A-first is the documented
        # invariant in the Caddyfile comment block and soak test).
        m = re.search(r'rolling_restart_replica\s+"backend-a"\s+8000', deploy_text)
        assert m, "deploy.sh must call rolling_restart_replica 'backend-a' 8000"

    def test_rolling_restarts_backend_b_second(self, deploy_text: str) -> None:
        m = re.search(r'rolling_restart_replica\s+"backend-b"\s+8001', deploy_text)
        assert m, "deploy.sh must call rolling_restart_replica 'backend-b' 8001"

    def test_rolling_a_precedes_b_in_file(self, deploy_text: str) -> None:
        a_idx = deploy_text.find('rolling_restart_replica "backend-a"')
        b_idx = deploy_text.find('rolling_restart_replica "backend-b"')
        assert a_idx != -1 and b_idx != -1
        assert a_idx < b_idx, (
            "A must be rolled before B — reversing order would contradict "
            "the Caddyfile's drain-A-first comment and break the soak invariant"
        )

    def test_rolling_uses_compose_prod(self, deploy_text: str) -> None:
        # Default compose file must be the one G2 #2 extended; operator
        # override via OMNISIGHT_COMPOSE_FILE remains possible.
        assert "docker-compose.prod.yml" in deploy_text, (
            "deploy.sh rolling mode must default to docker-compose.prod.yml"
        )


# ───────────────────────────────────────────────────────────────────
# 4. /readyz is the shared health contract
# ───────────────────────────────────────────────────────────────────


class TestReadyzIsSharedProbePath:
    """A rename of /readyz must land in all three files at once. If one
    file moves to /healthz and another stays on /readyz, Caddy would
    eject healthy replicas or admit dying ones — silent disaster."""

    def test_caddy_probes_readyz(self, caddy_text: str) -> None:
        assert re.search(r"(?m)^\s*health_uri\s+/readyz\s*$", caddy_text), (
            "Caddy active health check must target /readyz"
        )

    def test_compose_healthcheck_uses_readyz(self, compose_text: str) -> None:
        # Both replicas' healthcheck blocks must hit /readyz.
        readyz_refs = re.findall(r"http://localhost:800[01]/readyz", compose_text)
        assert len(readyz_refs) >= 2, (
            "Both backend-a and backend-b compose healthchecks must use /readyz "
            f"(found {len(readyz_refs)} matching URL(s))"
        )

    def test_deploy_polls_readyz(self, deploy_text: str) -> None:
        assert 'ready_url="http://localhost:${port}/readyz"' in deploy_text, (
            "deploy.sh rolling_restart_replica must poll /readyz after recreate"
        )


# ───────────────────────────────────────────────────────────────────
# 5. Drain-vs-eject timing invariant
# ───────────────────────────────────────────────────────────────────


class TestDrainOutlastsCaddyEject:
    """deploy.sh's drain window must be ≥ Caddy's active-probe eject
    budget, otherwise deploy.sh SIGKILLs the container while Caddy still
    considers the replica 'healthy' and routes traffic to it — 5xx."""

    def test_drain_default_is_generous_vs_health_fails_window(
        self, deploy_text: str, caddy_text: str
    ) -> None:
        # Caddy: health_interval 2s × health_fails 3 = 6s eject budget.
        probe_m = re.search(r"health_interval\s+(\d+)s", caddy_text)
        fails_m = re.search(r"health_fails\s+(\d+)", caddy_text)
        assert probe_m and fails_m, (
            "Caddyfile must declare health_interval + health_fails for probe budget calc"
        )
        eject_budget_s = int(probe_m.group(1)) * int(fails_m.group(1))

        # deploy.sh: ROLL_DRAIN_TIMEOUT default.
        drain_m = re.search(
            r'ROLL_DRAIN_TIMEOUT="\$\{OMNISIGHT_ROLL_DRAIN_TIMEOUT:-(\d+)\}"',
            deploy_text,
        )
        assert drain_m, "deploy.sh must declare OMNISIGHT_ROLL_DRAIN_TIMEOUT default"
        drain_default_s = int(drain_m.group(1))

        # Invariant: drain_default ≥ 2 × eject_budget. The 2× margin
        # absorbs one skipped probe (e.g. GC pause) + the backend's
        # own lifecycle.py drain_timeout (30s) without racing.
        assert drain_default_s >= 2 * eject_budget_s, (
            f"deploy.sh drain timeout {drain_default_s}s must be ≥ "
            f"2× Caddy eject budget ({eject_budget_s}s) — otherwise "
            f"SIGKILL can race Caddy's health_fails window and leak 5xx"
        )

    def test_caddy_fail_duration_bounded(self, caddy_text: str) -> None:
        # Passive-eject window must be short enough that a recovered
        # replica re-enters the pool within an operator's attention span.
        m = re.search(r"fail_duration\s+(\d+)s", caddy_text)
        assert m, "Caddyfile must declare fail_duration"
        assert 10 <= int(m.group(1)) <= 120, (
            f"fail_duration {m.group(1)}s outside sane [10, 120] band — "
            f"too short flaps the pool, too long strands recovered replicas"
        )


# ───────────────────────────────────────────────────────────────────
# 6. Docstring cross-reference sanity
# ───────────────────────────────────────────────────────────────────


class TestDocstringsCrossReference:
    """Each deliverable should name the other two in comments so a
    reader finding one file sees the other two listed."""

    def test_caddyfile_mentions_compose_and_deploy(self, caddy_text: str) -> None:
        assert "docker-compose.prod.yml" in caddy_text
        assert "deploy.sh" in caddy_text

    def test_compose_mentions_caddy_and_deploy(self, compose_text: str) -> None:
        assert "Caddyfile" in compose_text or "caddy" in compose_text.lower()
        assert "deploy.sh" in compose_text

    def test_deploy_mentions_caddy_and_compose(self, deploy_text: str) -> None:
        # deploy.sh comment header walks through the rolling contract
        # and must reference both partner files.
        assert "Caddy" in deploy_text or "caddy" in deploy_text
        assert "docker-compose" in deploy_text
