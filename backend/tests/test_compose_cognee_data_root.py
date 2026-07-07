"""OP-2552 — cognee data-root wiring contract in `docker-compose.prod.yml`.

Why the native env vars are load-bearing: cognee 1.0.9's BaseConfig is a
pydantic-settings model (no env_prefix) whose ``system_root_directory`` /
``data_root_directory`` fields resolve from SYSTEM_ROOT_DIRECTORY /
DATA_ROOT_DIRECTORY when the (lru-cached) config is first built, and the
relational config freezes its sqlite db_path from system_root_directory
at first touch. On the serving path cognee is imported at backend startup
BEFORE the adapter's programmatic setter runs (OP-2550's
``_configure_cognee_data_root``), so without container-level env the
store lands on the read-only site-packages default — v0.7.38 prod:
``database_path=.../site-packages/.cognee_system``, "unable to open
database file", kg_source=degraded, /cognee-data volume empty.

Pure YAML assertions — no Docker runtime required (same style as
test_compose_dual_backend_replicas.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.prod.yml"

REPLICAS = ("backend-a", "backend-b")
COGNEE_ROOT = "/cognee-data"


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE_PATH.exists(), f"compose file missing at {COMPOSE_PATH}"
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _env_map(compose: dict, service: str) -> dict[str, str]:
    env = compose["services"][service].get("environment") or []
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out: dict[str, str] = {}
    for item in env:
        s = str(item)
        if "=" in s:
            k, v = s.split("=", 1)
            out[k] = v
    return out


class TestNativeRootEnv:
    @pytest.mark.parametrize("replica", REPLICAS)
    def test_omnisight_root_still_set(self, compose: dict, replica: str) -> None:
        # The adapter seam (OP-2550) keys off this var — belt-and-braces
        # programmatic setter + native-env mirror both derive from it.
        env = _env_map(compose, replica)
        assert env.get("OMNISIGHT_COGNEE_DATA_ROOT") == COGNEE_ROOT

    @pytest.mark.parametrize("replica", REPLICAS)
    def test_native_system_root_set_at_container_level(
        self, compose: dict, replica: str
    ) -> None:
        env = _env_map(compose, replica)
        assert env.get("SYSTEM_ROOT_DIRECTORY") == f"{COGNEE_ROOT}/system", (
            f"`{replica}` must set cognee's native SYSTEM_ROOT_DIRECTORY at "
            "the container level — the adapter's programmatic setter runs "
            "AFTER cognee is imported at backend startup, too late to move "
            "the lru-cached sqlite db_path (OP-2552)"
        )

    @pytest.mark.parametrize("replica", REPLICAS)
    def test_native_data_root_set_at_container_level(
        self, compose: dict, replica: str
    ) -> None:
        env = _env_map(compose, replica)
        assert env.get("DATA_ROOT_DIRECTORY") == f"{COGNEE_ROOT}/data", (
            f"`{replica}` must set cognee's native DATA_ROOT_DIRECTORY at "
            "the container level (OP-2552)"
        )

    @pytest.mark.parametrize("replica", REPLICAS)
    def test_native_roots_nest_under_omnisight_root(
        self, compose: dict, replica: str
    ) -> None:
        # Layout consistency with the adapter's derivation
        # (<root>/system + <root>/data) — a drifted native path would put
        # the store outside the mounted volume and silently reintroduce
        # the read-only-rootfs failure.
        env = _env_map(compose, replica)
        root = env["OMNISIGHT_COGNEE_DATA_ROOT"]
        assert env["SYSTEM_ROOT_DIRECTORY"] == f"{root}/system"
        assert env["DATA_ROOT_DIRECTORY"] == f"{root}/data"


class TestVolumeAndRootfsInvariants:
    @pytest.mark.parametrize(
        "replica,volume",
        [("backend-a", "cognee-data-a"), ("backend-b", "cognee-data-b")],
    )
    def test_per_replica_volume_mounted_at_root(
        self, compose: dict, replica: str, volume: str
    ) -> None:
        # PER-REPLICA volumes by design (sqlite: two writers on one file
        # corrupts it) — ticket MUST NOT: "no shared volume".
        mounts = [str(m) for m in compose["services"][replica].get("volumes", [])]
        assert f"{volume}:{COGNEE_ROOT}" in mounts, (
            f"`{replica}` must mount its OWN volume `{volume}` at "
            f"{COGNEE_ROOT} (got mounts={mounts!r})"
        )

    def test_replicas_do_not_share_a_cognee_volume(self, compose: dict) -> None:
        def _cognee_volume(replica: str) -> str:
            for m in compose["services"][replica].get("volumes", []):
                s = str(m)
                if s.endswith(f":{COGNEE_ROOT}"):
                    return s.split(":", 1)[0]
            pytest.fail(f"`{replica}` has no mount at {COGNEE_ROOT}")

        assert _cognee_volume("backend-a") != _cognee_volume("backend-b")

    @pytest.mark.parametrize("replica", REPLICAS)
    def test_read_only_rootfs_preserved(self, compose: dict, replica: str) -> None:
        # Ticket MUST NOT: "keep read_only rootfs" — the cognee volume is
        # the only writable cognee surface.
        assert compose["services"][replica].get("read_only") is True
