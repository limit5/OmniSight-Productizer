"""OP-767 staging auto-deploy contract tests."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "auto_deploy_staging.py"
COMPOSE_PATH = REPO_ROOT / "deploy" / "staging" / "docker-compose.yml"
CADDY_JSON = REPO_ROOT / "deploy" / "staging" / "caddy.json"
DOC = REPO_ROOT / "docs" / "operations" / "staging-environment.md"


def _load_script():
    spec = importlib.util.spec_from_file_location("auto_deploy_staging", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE_PATH.exists(), f"missing {COMPOSE_PATH}"
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _env_map(service: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in service.get("environment") or []:
        key, _, value = str(item).partition("=")
        out[key] = value
    return out


def test_staging_compose_declares_prod_shape_at_smaller_scale(compose: dict) -> None:
    services = compose["services"]
    for name in ("postgres", "backend-a", "backend-b", "frontend", "caddy", "bridge-daemon"):
        assert name in services

    assert services["backend-a"]["image"] == services["backend-b"]["image"]
    assert "omnisight-backend:${OMNISIGHT_IMAGE_TAG:-latest}" in services["backend-a"]["image"]
    assert services["frontend"]["image"].endswith("/omnisight-frontend:${OMNISIGHT_IMAGE_TAG:-latest}")
    assert services["backend-a"]["mem_limit"] == "2g"
    assert services["backend-b"]["mem_limit"] == "2g"
    assert services["postgres"]["mem_limit"] == "1g"


def test_staging_frontend_uses_public_staging_api_base(compose: dict) -> None:
    frontend = compose["services"]["frontend"]
    args = frontend["build"]["args"]
    env = _env_map(frontend)

    assert args["VITE_API_BASE"] == "https://staging.sora.services/api"
    assert env["VITE_API_BASE"] == "https://staging.sora.services/api"
    assert args["NEXT_PUBLIC_API_URL"] == "https://staging.sora.services/api/v1"
    assert env["BACKEND_URL"] == "http://caddy"


def test_caddy_json_routes_api_to_both_backends_and_root_to_frontend() -> None:
    cfg = json.loads(CADDY_JSON.read_text(encoding="utf-8"))
    routes = cfg["apps"]["http"]["servers"]["staging"]["routes"]
    api_handler = routes[0]["handle"][0]
    root_handler = routes[1]["handle"][0]

    upstreams = {u["dial"] for u in api_handler["upstreams"]}
    assert upstreams == {"backend-a:8000", "backend-b:8001"}
    assert api_handler["health_checks"]["active"]["uri"] == "/readyz"
    assert root_handler["upstreams"] == [{"dial": "frontend:3000"}]


def test_auto_deploy_main_promoted_pulls_migrates_ups_and_smokes(tmp_path: Path) -> None:
    mod = _load_script()
    calls: list[tuple[list[str], dict[str, str], int]] = []
    events: list[tuple[str, dict]] = []
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")

    def runner(cmd, **kwargs):
        calls.append((list(cmd), dict(kwargs["env"]), kwargs["timeout"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    result = mod.deploy_on_main_promoted(
        {"event": "main_promoted", "promoted_tip": "abc1234"},
        compose_file=compose,
        env_file=tmp_path / "missing.env",
        staging_url="https://staging.sora.services",
        deadline_seconds=300,
        runner=runner,
        event_sink=lambda event, payload: events.append((event, payload)),
    )

    assert result.status == "deployed"
    assert result.image_tag == "abc1234"
    assert [call[0][-1] for call in calls[:4]] == ["pull", "postgres", "heads", "-d"]
    assert calls[0][1]["OMNISIGHT_IMAGE_TAG"] == "abc1234"
    assert calls[2][0][-6:] == [
        "backend-a",
        "python",
        "-m",
        "alembic",
        "upgrade",
        "heads",
    ]
    assert calls[4][0][-3:] == ["https://staging.sora.services", "--subset", "dag1"]
    assert events[0][0] == "staging_deployed"
    assert events[0][1]["deadline_seconds"] == 300


def test_synthetic_main_promoted_deploy_finishes_inside_five_minute_contract(
    tmp_path: Path,
) -> None:
    mod = _load_script()
    event_log = tmp_path / "release.log"
    cursor = tmp_path / "cursor"
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    event_log.write_text(
        "prefix " + json.dumps({"event": "main_promoted", "image_tag": "sha-5678"}) + "\n",
        encoding="utf-8",
    )

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    started = time.monotonic()
    results = mod.run_once(
        event_log=event_log,
        cursor=cursor,
        compose_file=compose,
        env_file=tmp_path / "missing.env",
        staging_url="https://staging.sora.services",
        deadline_seconds=300,
        runner=runner,
        event_sink=lambda _event, _payload: None,
    )
    elapsed = time.monotonic() - started

    assert [r.status for r in results] == ["deployed"]
    assert results[0].image_tag == "sha-5678"
    assert elapsed < 300
    assert int(cursor.read_text(encoding="utf-8")) == event_log.stat().st_size


def test_docs_record_reachability_worker_smoke_and_parity_lesson() -> None:
    text = DOC.read_text(encoding="utf-8")

    assert "staging.sora.services" in text
    assert "main_promoted" in text
    assert "python -m alembic upgrade heads" in text
    assert "scripts/prod_smoke_test.py https://staging.sora.services --subset dag1" in text
    assert "Staging-Prod Parity Lesson" in text
