"""OP-869 multi-environment config and SOPS secret management tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.agents import env_config
from scripts import check_secrets_plaintext
from scripts import drift_detect_env_config


def _write_env(root: Path, env: str, *, config_extra: str = "") -> Path:
    env_dir = root / env
    env_dir.mkdir(parents=True)
    env_dir.joinpath("config.yaml").write_text(
        "\n".join(
            [
                "schema_version: 1",
                f"env: {env}",
                "service_name: omnisight-backend",
                f"public_base_url: https://{env}.example.test",
                f"frontend_origin: https://{env}.example.test",
                "auth_mode: strict",
                "docker_runtime: runsc",
                "llm_provider: anthropic",
                "web_search_provider: none",
                "token_budget_daily: 5.0",
                "token_budget_hourly: 1.0",
                config_extra,
            ]
        ),
        encoding="utf-8",
    )
    env_dir.joinpath("secrets.encrypted.yaml").write_text(
        "secrets:\n  api_key: ENC[AES256_GCM,data:x,type:str]\n",
        encoding="utf-8",
    )
    return env_dir


def _fake_sops(bin_dir: Path, plaintext: str, *, exit_code: int = 0) -> None:
    script = bin_dir / "sops"
    script.write_text(
        "#!/bin/sh\n"
        f"cat <<'EOF'\n{plaintext}\nEOF\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def test_config_load_happy_path_reads_selected_env(tmp_path, monkeypatch):
    env_root = tmp_path / "env"
    _write_env(env_root, "dev")
    _write_env(env_root, "staging")
    key = tmp_path / "age-key.txt"
    key.write_text("AGE-SECRET-KEY-test\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_sops(bin_dir, "secrets:\n  api_key: decrypted-value\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OMNISIGHT_ENV", "staging")

    loaded = env_config.load_env_config(env_root=env_root, age_key_path=key)

    assert loaded.config.env == "staging"
    assert loaded.secrets == {"api_key": "decrypted-value"}


def test_missing_env_refuses_start(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_ENV", raising=False)

    with pytest.raises(env_config.EnvNotSet, match="EnvNotSet"):
        env_config.selected_env()


def test_decrypt_fail_refuses_start_when_age_key_missing(tmp_path):
    secret_file = tmp_path / "secrets.encrypted.yaml"
    secret_file.write_text("secrets: {}\n", encoding="utf-8")

    with pytest.raises(env_config.SecretsDecryptFailed, match="SecretsDecryptFailed"):
        env_config.decrypt_secrets(secret_file, tmp_path / "missing-age-key.txt")


def test_plaintext_scan_refuses_secret_values(tmp_path):
    secret_file = tmp_path / "secrets.encrypted.yaml"
    secret_file.write_text(
        "secrets:\n"
        "  api_key: plaintext-token\n"
        "  password: ENC[AES256_GCM,data:x,type:str]\n",
        encoding="utf-8",
    )

    assert check_secrets_plaintext.check_paths([secret_file]) == [
        (secret_file, 2, "api_key")
    ]


def test_schema_validation_rejects_invalid_config(tmp_path):
    env_dir = _write_env(tmp_path / "env", "prod", config_extra="unexpected: true")

    with pytest.raises(env_config.EnvConfigSchemaInvalid, match="EnvConfigSchemaInvalid"):
        env_config.load_config_file(env_dir / "config.yaml", expected_env="prod")


def test_drift_detection_reports_running_config_difference(tmp_path):
    git_dir = _write_env(tmp_path / "env", "prod")
    running = tmp_path / "running.yaml"
    running.write_text(
        (git_dir / "config.yaml").read_text(encoding="utf-8").replace(
            "token_budget_hourly: 1.0",
            "token_budget_hourly: 2.0",
        ),
        encoding="utf-8",
    )

    diffs = drift_detect_env_config.detect_drift(
        running_config=running,
        git_config=git_dir / "config.yaml",
    )

    assert diffs == ["token_budget_hourly: running=2.0 git=1.0"]
