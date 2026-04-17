"""G2 #2 — `docker-compose.prod.yml` dual-backend-replica contract tests.

Scope is narrow: verify that the checked-in compose file satisfies
TODO row 1346 (HA-02) — two symmetric backend replicas named
`backend-a` / `backend-b` sharing the same named volumes, each exposing
a distinct internal port (8000 / 8001) that lines up with the Caddyfile
upstream stanza from G2 #1, plus a host-level port mapping so operators
can `curl` each replica independently during a rolling restart.

Pure YAML / string assertions — no Docker runtime required in CI. The
in-container shell-level behaviour of backend-b's `command:` override
(OMNISIGHT_WORKERS fallback, `$$` escape semantics) is verified by
parsing the string, not by actually spawning containers.

This test file is a sibling of:
    * test_compose_prod_image_first.py  — L10 #337 image-first contract
    * test_reverse_proxy_caddyfile.py   — G2 #1 Caddyfile contract
    * test_deploy_docker_nginx.py       — legacy nginx pathway
and stays focused on G2 #2's deliverable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.prod.yml"
CADDYFILE = PROJECT_ROOT / "deploy" / "reverse-proxy" / "Caddyfile"

SHARED_VOLUMES = ("omnisight-data", "omnisight-artifacts", "omnisight-sdks")


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE_PATH.exists(), f"compose file missing at {COMPOSE_PATH}"
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) Both replicas are declared
# ---------------------------------------------------------------------------


class TestReplicasDeclared:
    def test_backend_a_exists(self, compose: dict) -> None:
        assert "backend-a" in compose["services"], (
            "G2 #2 requires a `backend-a` service in docker-compose.prod.yml"
        )

    def test_backend_b_exists(self, compose: dict) -> None:
        assert "backend-b" in compose["services"], (
            "G2 #2 requires a `backend-b` service in docker-compose.prod.yml"
        )

    def test_legacy_backend_name_removed(self, compose: dict) -> None:
        # The single `backend` service was replaced by backend-a / backend-b.
        # Keeping it alongside would double-mount the SQLite volume and
        # cause a third writer to race the two new replicas.
        assert "backend" not in compose["services"], (
            "legacy `backend` service must be removed after the dual-replica "
            "split — keep only backend-a + backend-b"
        )


# ---------------------------------------------------------------------------
# (2) Symmetric volume mounts — shared state
# ---------------------------------------------------------------------------


class TestSharedVolumes:
    @pytest.mark.parametrize("replica", ["backend-a", "backend-b"])
    @pytest.mark.parametrize("volume", SHARED_VOLUMES)
    def test_replica_mounts_shared_volume(
        self, compose: dict, replica: str, volume: str
    ) -> None:
        mounts = [str(m) for m in compose["services"][replica].get("volumes", [])]
        hits = [m for m in mounts if m.startswith(f"{volume}:")]
        assert hits, (
            f"`{replica}` must mount the named volume `{volume}` — shared "
            "state is the whole point of dual-replica HA"
        )

    def test_volumes_top_level_declares_each_shared_volume(
        self, compose: dict
    ) -> None:
        declared = set((compose.get("volumes") or {}).keys())
        for volume in SHARED_VOLUMES:
            assert volume in declared, (
                f"top-level `volumes:` must declare `{volume}` so docker "
                "provisions a single shared backing store"
            )

    def test_replicas_mount_the_same_data_volume_on_the_same_mountpoint(
        self, compose: dict
    ) -> None:
        # Critical invariant — if backend-a and backend-b mount /app/data
        # from different volumes, they diverge silently and HA is broken.
        a_data = _find_mount(compose, "backend-a", "omnisight-data")
        b_data = _find_mount(compose, "backend-b", "omnisight-data")
        assert a_data is not None and b_data is not None
        assert a_data.split(":", 1)[1] == b_data.split(":", 1)[1] == "/app/data"


# ---------------------------------------------------------------------------
# (3) Port contract with Caddyfile upstream stanza
# ---------------------------------------------------------------------------


class TestPortContract:
    def test_backend_a_listens_on_8000_internally(self, compose: dict) -> None:
        # The default Dockerfile CMD binds :8000, so backend-a should NOT
        # override `command:` — rely on the image default.
        svc = compose["services"]["backend-a"]
        assert "command" not in svc or not svc["command"], (
            "backend-a should inherit Dockerfile.backend's default CMD "
            "(port 8000) — no override needed"
        )
        ports = [str(p) for p in svc.get("ports", [])]
        assert any(p.endswith(":8000") for p in ports), (
            "backend-a must publish host:container 8000 (got ports=%r)" % ports
        )

    def test_backend_b_overrides_command_to_port_8001(self, compose: dict) -> None:
        svc = compose["services"]["backend-b"]
        cmd = svc.get("command")
        assert cmd, "backend-b must override `command:` to listen on 8001"
        # Compose may flatten to a single string or keep a list — accept both.
        joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        assert "--port 8001" in joined, (
            "backend-b `command:` must pass `--port 8001` to uvicorn (got: %r)"
            % joined
        )
        # Still mounts to `backend.main:app` — no module drift.
        assert "backend.main:app" in joined, (
            "backend-b must still launch the `backend.main:app` uvicorn target"
        )

    def test_backend_b_command_escapes_dollars_for_compose(
        self, compose: dict
    ) -> None:
        svc = compose["services"]["backend-b"]
        cmd = svc["command"]
        joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        # The shell-level OMNISIGHT_WORKERS override + python3 fallback must
        # survive compose-time interpolation. Single `$` would be eaten;
        # `$$` is the doubled form that renders to literal `$` at runtime.
        assert "$${OMNISIGHT_WORKERS" in joined, (
            "backend-b command must use `$$` to escape OMNISIGHT_WORKERS "
            "interpolation so the container shell — not compose — resolves it"
        )
        assert "$$(python3 " in joined, (
            "backend-b command must use `$$(python3 ...)` so the worker-count "
            "fallback is evaluated inside the container, not at compose-config time"
        )
        assert "$$WORKERS" in joined, (
            "backend-b command must reference `$$WORKERS` (doubled) so the "
            "variable is resolved by the in-container shell"
        )

    def test_backend_b_publishes_host_port_8001(self, compose: dict) -> None:
        ports = [str(p) for p in compose["services"]["backend-b"].get("ports", [])]
        assert any(p.endswith(":8001") for p in ports), (
            "backend-b must publish host:container 8001 so operators can "
            "`curl localhost:8001/readyz` during rolling-restart smoke tests"
        )


# ---------------------------------------------------------------------------
# (4) Healthchecks — both probes hit /readyz on the correct port
# ---------------------------------------------------------------------------


class TestHealthchecks:
    @pytest.mark.parametrize(
        "replica,port",
        [("backend-a", 8000), ("backend-b", 8001)],
    )
    def test_replica_healthcheck_probes_readyz_on_internal_port(
        self, compose: dict, replica: str, port: int
    ) -> None:
        hc = compose["services"][replica].get("healthcheck") or {}
        test = hc.get("test") or []
        joined = " ".join(str(t) for t in test)
        assert f"localhost:{port}/readyz" in joined, (
            f"`{replica}` healthcheck must probe http://localhost:{port}/readyz "
            f"(got: {joined!r})"
        )

    @pytest.mark.parametrize("replica", ["backend-a", "backend-b"])
    def test_replica_healthcheck_has_start_period(
        self, compose: dict, replica: str
    ) -> None:
        # Without a start_period, docker marks the container unhealthy
        # during first-boot DB migration and the rolling-restart flow
        # trips before /readyz has even come online.
        hc = compose["services"][replica].get("healthcheck") or {}
        assert "start_period" in hc, (
            f"`{replica}` healthcheck must declare a start_period "
            "to tolerate first-boot migration latency"
        )


# ---------------------------------------------------------------------------
# (5) Frontend wiring — points at backend-a and depends_on both replicas
# ---------------------------------------------------------------------------


class TestFrontendWiring:
    def test_frontend_backend_url_is_backend_a(self, compose: dict) -> None:
        frontend = compose["services"]["frontend"]
        args = (frontend.get("build") or {}).get("args") or {}
        env = frontend.get("environment") or []
        assert args.get("BACKEND_URL") == "http://backend-a:8000"
        env_map = _env_list_to_map(env)
        assert env_map.get("BACKEND_URL") == "http://backend-a:8000", (
            "frontend BACKEND_URL env must match the build-arg; mismatch would "
            "silently diverge build-time rewrites from runtime fetches"
        )

    def test_frontend_depends_on_both_replicas_healthy(
        self, compose: dict
    ) -> None:
        dep = compose["services"]["frontend"].get("depends_on") or {}
        # `depends_on:` may be a list or a long-form mapping. Either way
        # both replicas must be present; long-form is preferred so the
        # frontend actually waits for /readyz to be green.
        if isinstance(dep, dict):
            assert "backend-a" in dep and "backend-b" in dep, (
                "frontend must `depends_on:` both backend-a and backend-b"
            )
            assert dep["backend-a"].get("condition") == "service_healthy"
            assert dep["backend-b"].get("condition") == "service_healthy"
        else:  # pragma: no cover — short-form fallback
            assert "backend-a" in dep and "backend-b" in dep


# ---------------------------------------------------------------------------
# (6) Caddyfile ↔ compose agreement — the upstream pool maps to reality
# ---------------------------------------------------------------------------


class TestCaddyfileCompatibility:
    @pytest.fixture(scope="class")
    def caddyfile_text(self) -> str:
        assert CADDYFILE.exists(), f"Caddyfile missing at {CADDYFILE}"
        return CADDYFILE.read_text(encoding="utf-8")

    def test_caddy_upstream_names_exist_as_compose_services(
        self, compose: dict, caddyfile_text: str
    ) -> None:
        assert re.search(r"\bbackend-a:8000\b", caddyfile_text), (
            "Caddyfile must reference backend-a:8000 as an upstream"
        )
        assert re.search(r"\bbackend-b:8001\b", caddyfile_text), (
            "Caddyfile must reference backend-b:8001 as an upstream"
        )
        assert "backend-a" in compose["services"]
        assert "backend-b" in compose["services"]


# ---------------------------------------------------------------------------
# (7) Env / hygiene invariants
# ---------------------------------------------------------------------------


class TestEnvHygiene:
    @pytest.mark.parametrize("replica", ["backend-a", "backend-b"])
    def test_replica_env_file_loaded(self, compose: dict, replica: str) -> None:
        # Both replicas must share .env so credentials (DB URL, LLM keys,
        # auth secrets) do not diverge between the two instances.
        env_file = compose["services"][replica].get("env_file")
        assert env_file and ".env" in (env_file if isinstance(env_file, list) else [env_file])

    @pytest.mark.parametrize(
        "replica,expected_id",
        [("backend-a", "backend-a"), ("backend-b", "backend-b")],
    )
    def test_replica_stamps_instance_id(
        self, compose: dict, replica: str, expected_id: str
    ) -> None:
        env = _env_list_to_map(compose["services"][replica].get("environment") or [])
        assert env.get("OMNISIGHT_INSTANCE_ID") == expected_id, (
            f"`{replica}` must stamp OMNISIGHT_INSTANCE_ID={expected_id} so "
            "log + metric output is traceable back to the concrete replica"
        )

    @pytest.mark.parametrize("replica", ["backend-a", "backend-b"])
    def test_replica_runs_in_production_mode(
        self, compose: dict, replica: str
    ) -> None:
        env = _env_list_to_map(compose["services"][replica].get("environment") or [])
        assert env.get("OMNISIGHT_ENV") == "production"
        assert env.get("OMNISIGHT_DEBUG") == "false"
        assert env.get("OMNISIGHT_AUTH_MODE") == "strict"

    @pytest.mark.parametrize("replica", ["backend-a", "backend-b"])
    def test_replica_restart_policy_always(
        self, compose: dict, replica: str
    ) -> None:
        assert compose["services"][replica].get("restart") == "always"

    def test_backend_a_preserves_legacy_dns_alias(self, compose: dict) -> None:
        # configs/prometheus.yml still scrapes `backend:8000` — the DNS
        # alias on backend-a keeps the scrape working without touching
        # that file.  A later commit can widen prometheus to scrape both
        # replicas and drop this alias.
        nets = compose["services"]["backend-a"].get("networks")
        assert isinstance(nets, dict), (
            "backend-a must declare `networks:` so it can attach the "
            "legacy `backend` DNS alias"
        )
        default = nets.get("default") or {}
        aliases = default.get("aliases") or []
        assert "backend" in aliases, (
            "backend-a must alias itself as `backend` on the default network "
            "to keep configs/prometheus.yml scraping without churn"
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _env_list_to_map(env: list | dict) -> dict[str, str]:
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out: dict[str, str] = {}
    for item in env:
        s = str(item)
        if "=" in s:
            k, v = s.split("=", 1)
            out[k] = v
    return out


def _find_mount(compose: dict, service: str, volume: str) -> str | None:
    for m in compose["services"][service].get("volumes", []):
        s = str(m)
        if s.startswith(f"{volume}:"):
            return s
    return None
