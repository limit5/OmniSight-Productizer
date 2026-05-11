"""OP-869 runtime loader for deploy/env configuration and SOPS secrets."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_ROOT = REPO_ROOT / "deploy" / "env"
AGE_KEY_PATH = Path("/home/user/.config/omnisight/age-key.txt")
VALID_ENVS = frozenset({"dev", "staging", "prod"})


class EnvNotSet(RuntimeError):
    """Raised when OMNISIGHT_ENV is missing."""


class SecretsDecryptFailed(RuntimeError):
    """Raised when SOPS cannot decrypt the selected environment secrets."""


class EnvConfigSchemaInvalid(RuntimeError):
    """Raised when deploy/env config or decrypted secrets fail validation."""


class EnvConfigDrift(RuntimeError):
    """Raised by drift detection when running prod config differs from git."""


class EnvConfig(BaseModel):
    """Non-secret runtime config loaded from deploy/env/<env>/config.yaml."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    env: Literal["dev", "staging", "prod"]
    service_name: str = Field(min_length=1)
    public_base_url: str = Field(min_length=1)
    frontend_origin: str = Field(min_length=1)
    auth_mode: Literal["open", "strict"]
    docker_runtime: Literal["runc", "runsc"]
    llm_provider: str
    web_search_provider: str = Field(min_length=1)
    token_budget_daily: float = Field(gt=0)
    token_budget_hourly: float = Field(gt=0)


class EnvSecrets(BaseModel):
    """Decrypted SOPS payload shape."""

    model_config = ConfigDict(extra="allow")

    secrets: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeEnvConfig:
    """Resolved non-secret config plus decrypted secret values."""

    config: EnvConfig
    secrets: dict[str, str]


def selected_env(environ: dict[str, str] | None = None) -> str:
    """Return OMNISIGHT_ENV or raise the startup refusal error."""

    env_map = os.environ if environ is None else environ
    value = env_map.get("OMNISIGHT_ENV", "").strip()
    if not value:
        raise EnvNotSet("EnvNotSet: OMNISIGHT_ENV must be one of dev, staging, prod")
    if value not in VALID_ENVS:
        raise EnvConfigSchemaInvalid(
            f"EnvConfigSchemaInvalid: unsupported OMNISIGHT_ENV={value!r}"
        )
    return value


def load_env_config(
    *,
    env: str | None = None,
    env_root: Path | str = ENV_ROOT,
    age_key_path: Path | str = AGE_KEY_PATH,
) -> RuntimeEnvConfig:
    """Load config for OMNISIGHT_ENV and decrypt its SOPS secrets."""

    resolved_env = selected_env() if env is None else _validate_env(env)
    root = Path(env_root) / resolved_env
    config = _load_config(root / "config.yaml", resolved_env)
    secrets = decrypt_secrets(root / "secrets.encrypted.yaml", Path(age_key_path))
    return RuntimeEnvConfig(config=config, secrets=secrets.secrets)


def load_config_file(path: Path | str, *, expected_env: str | None = None) -> EnvConfig:
    """Load and validate one non-secret env config file."""

    return _load_config(Path(path), expected_env)


def decrypt_secrets(path: Path | str, age_key_path: Path | str = AGE_KEY_PATH) -> EnvSecrets:
    """Decrypt one SOPS file using the vendored age key."""

    secret_path = Path(path)
    key_path = Path(age_key_path)
    if not key_path.is_file():
        raise SecretsDecryptFailed(
            f"SecretsDecryptFailed: age key not found at {key_path}"
        )
    if not secret_path.is_file():
        raise SecretsDecryptFailed(
            f"SecretsDecryptFailed: secrets file not found at {secret_path}"
        )

    env = dict(os.environ)
    env["SOPS_AGE_KEY_FILE"] = str(key_path)
    try:
        proc = subprocess.run(
            ["sops", "--decrypt", "--input-type", "yaml", "--output-type", "yaml", str(secret_path)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretsDecryptFailed(f"SecretsDecryptFailed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "sops decrypt failed"
        raise SecretsDecryptFailed(f"SecretsDecryptFailed: {detail}")

    raw = _safe_yaml(proc.stdout, secret_path)
    try:
        return EnvSecrets.model_validate(raw)
    except ValidationError as exc:
        raise EnvConfigSchemaInvalid(f"EnvConfigSchemaInvalid: {exc}") from exc


def _load_config(path: Path, expected_env: str | None) -> EnvConfig:
    raw = _safe_yaml(path.read_text(encoding="utf-8"), path)
    try:
        config = EnvConfig.model_validate(raw)
    except ValidationError as exc:
        raise EnvConfigSchemaInvalid(f"EnvConfigSchemaInvalid: {exc}") from exc
    if expected_env is not None and config.env != expected_env:
        raise EnvConfigSchemaInvalid(
            f"EnvConfigSchemaInvalid: config env {config.env!r} does not match {expected_env!r}"
        )
    return config


def _safe_yaml(text: str, path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise EnvConfigSchemaInvalid(f"EnvConfigSchemaInvalid: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise EnvConfigSchemaInvalid(
            f"EnvConfigSchemaInvalid: {path} must contain a YAML mapping"
        )
    return raw


def _validate_env(env: str) -> str:
    value = env.strip()
    if value not in VALID_ENVS:
        raise EnvConfigSchemaInvalid(
            f"EnvConfigSchemaInvalid: unsupported environment {value!r}"
        )
    return value
