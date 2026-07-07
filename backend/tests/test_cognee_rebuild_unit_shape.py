"""OP-2551 cognee nightly rebuild systemd contract tests."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE = REPO_ROOT / "deploy" / "systemd" / "cognee-nightly-rebuild.service"
TIMER = REPO_ROOT / "deploy" / "systemd" / "cognee-nightly-rebuild.timer"


def _directive_values(text: str, name: str) -> list[str]:
    prefix = f"{name}="
    return [
        line[len(prefix) :].strip()
        for line in text.splitlines()
        if line.startswith(prefix)
    ]


def test_cognee_rebuild_timer_is_enabled_daily_at_03_utc() -> None:
    text = TIMER.read_text(encoding="utf-8")

    assert "Unit=cognee-nightly-rebuild.service" in text
    assert "OnCalendar=*-*-* 03:00:00 UTC" in text
    assert "Persistent=true" in text
    assert "WantedBy=timers.target" in text
    assert "sudo systemctl enable --now cognee-nightly-rebuild.timer" in text
    assert "deployed:" in text


def test_cognee_rebuild_runs_one_shot_docker_for_each_replica_volume() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    exec_starts = _directive_values(text, "ExecStart")

    assert len(exec_starts) == 2
    assert "Type=oneshot" in text
    assert "EnvironmentFile=-/home/user/omnisight-prod/.env" in text
    assert "deployed:" in text

    joined = "\n".join(exec_starts)
    assert "cognee-data-a:/cognee-data" in joined
    assert "cognee-data-b:/cognee-data" in joined
    # PS2 (review fix): the key chain must include the prod .env's actual
    # key name, else the first nightly run fails with an empty LLM_API_KEY.
    assert "${OPENAI_API_KEY:-}" in joined
    assert "OMNISIGHT_COGNEE_DATA_ROOT=/cognee-data" in joined
    assert "${OMNISIGHT_BACKEND_IMAGE_REF:-" in joined
    assert "${OMNISIGHT_REGISTRY:?" in joined
    assert "/backend:${OMNISIGHT_IMAGE_TAG:?" in joined
    assert "backend:v0.5.0" not in joined

    for exec_start in exec_starts:
        assert "/usr/bin/docker run --rm" in exec_start
        assert "--cpus 2 -m 2g" in exec_start
        assert "--network \"${COMPOSE_PROJECT_NAME:-omnisight-productizer}_default\"" in exec_start
        assert "-e PYTHONPATH=/app" in exec_start
        assert "-e LLM_API_KEY" in exec_start
        assert "-e LLM_PROVIDER" in exec_start
        assert "-e LLM_MODEL" in exec_start
        assert "-e EMBEDDING_API_KEY" in exec_start
        assert "-e EMBEDDING_PROVIDER" in exec_start
        assert "-e EMBEDDING_MODEL" in exec_start
        assert "LLM_PROVIDER=\"${LLM_PROVIDER:-openai}\"" in exec_start
        assert "LLM_MODEL=\"${LLM_MODEL:-gpt-4o-mini}\"" in exec_start
        assert "python3 -m scripts.cognee_full_rebuild --repo-root /app" in exec_start
        assert "--entrypoint" not in exec_start
        assert " sh -c " not in exec_start
        assert "cd /app" not in exec_start
