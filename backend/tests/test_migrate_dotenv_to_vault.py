"""OP-764 D3 — :mod:`scripts.migrate_dotenv_to_vault` integration tests.

Verifies the legacy ``.env`` → vault + yaml split: secrets land in
the file vault, non-secrets land in ``config/<env>.yaml``, every
secret round-trips, and idempotency holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_DIR = _REPO_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPT_DIR))

from migrate_dotenv_to_vault import main as migrate_main  # noqa: E402

from backend import secrets_provider as sp  # noqa: E402


@pytest.fixture
def hermetic_secret_key(monkeypatch):
    """Pin the Fernet key to a test value so the file vault is
    decryptable from inside the test process."""
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-migrate")
    from backend import secret_store
    secret_store._reset_for_tests()


def _write_dotenv(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _yaml_path_for_env(env_name: str) -> Path:
    """Resolve where the migrator will write the per-env yaml.

    The script writes to ``<repo>/config/<env>.yaml``; tests run
    from the same repo so we point asserts at that path. We
    snapshot + restore content via the ``preserve_yaml`` fixture
    below so the checked-in file isn't mutated by the test.
    """
    return _REPO_ROOT / "config" / f"{env_name}.yaml"


@pytest.fixture
def preserve_yaml():
    """Snapshot config/{dev,staging,prod}.yaml before each migration
    test and restore them on teardown — the script writes into them
    in-place and we don't want the checked-in defaults polluted."""
    targets = [_yaml_path_for_env(e) for e in ("dev", "staging", "prod")]
    snapshots = {p: (p.read_text(encoding="utf-8") if p.exists() else None) for p in targets}
    yield
    for p, content in snapshots.items():
        if content is None and p.exists():
            p.unlink()
        elif content is not None:
            p.write_text(content, encoding="utf-8")


def test_migrate_splits_secrets_and_non_secrets(
    tmp_path, monkeypatch, hermetic_secret_key, preserve_yaml,
):
    dotenv = tmp_path / ".env"
    _write_dotenv(dotenv, [
        "# legacy host .env",
        "OMNISIGHT_ENV=dev",
        "OMNISIGHT_AUTH_MODE=open",
        "OMNISIGHT_DOCKER_RUNTIME=runc",
        "OMNISIGHT_ANTHROPIC_API_KEY=sk-ant-from-dotenv",
        "OMNISIGHT_GITHUB_TOKEN=ghp_from_dotenv",
        # Empty value — must be skipped.
        "OMNISIGHT_OPENAI_API_KEY=",
        # Unknown OMNISIGHT_ var — must be skipped (not declared on Settings).
        "OMNISIGHT_NOT_A_REAL_KNOB=ignored",
        # Non-OMNISIGHT_ var — must be skipped (separately tallied).
        "PATH=/usr/bin",
    ])
    vault = tmp_path / "v.vault"
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))

    rc = migrate_main([
        "--dotenv", str(dotenv),
        "--env", "dev",
        "-v",
    ])
    assert rc == 0

    # Secrets landed in the vault.
    provider = sp.FileSecretsProvider(path=vault)
    assert provider.get("anthropic_api_key") == "sk-ant-from-dotenv"
    assert provider.get("github_token") == "ghp_from_dotenv"
    assert provider.get("openai_api_key") is None  # empty was skipped

    # Non-secrets landed in the yaml.
    import yaml
    yaml_data = yaml.safe_load(_yaml_path_for_env("dev").read_text(encoding="utf-8"))
    assert yaml_data["env"] == "dev"
    assert yaml_data["auth_mode"] == "open"
    assert yaml_data["docker_runtime"] == "runc"
    # Unknown / non-prefixed entries did NOT pollute the yaml.
    assert "not_a_real_knob" not in yaml_data
    assert "path" not in yaml_data


def test_migrate_dry_run_does_not_write(
    tmp_path, monkeypatch, hermetic_secret_key, preserve_yaml,
):
    dotenv = tmp_path / ".env"
    _write_dotenv(dotenv, [
        "OMNISIGHT_ANTHROPIC_API_KEY=sk-ant-dryrun",
        "OMNISIGHT_AUTH_MODE=strict",
    ])
    vault = tmp_path / "v.vault"
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))

    yaml_before = _yaml_path_for_env("staging").read_text(encoding="utf-8")

    rc = migrate_main([
        "--dotenv", str(dotenv),
        "--env", "staging",
        "--dry-run",
    ])
    assert rc == 0

    # Vault file is not created on dry-run.
    assert not vault.exists()
    # YAML untouched on dry-run.
    assert _yaml_path_for_env("staging").read_text(encoding="utf-8") == yaml_before


def test_migrate_idempotent(
    tmp_path, monkeypatch, hermetic_secret_key, preserve_yaml,
):
    """Re-running the migrator with unchanged inputs MUST be a no-op
    — no spurious vault writes, no yaml churn."""
    dotenv = tmp_path / ".env"
    _write_dotenv(dotenv, [
        "OMNISIGHT_ANTHROPIC_API_KEY=sk-ant-idem",
        "OMNISIGHT_DOCKER_RUNTIME=runc",
    ])
    vault = tmp_path / "v.vault"
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))

    rc1 = migrate_main(["--dotenv", str(dotenv), "--env", "dev"])
    assert rc1 == 0
    yaml_after_first = _yaml_path_for_env("dev").read_text(encoding="utf-8")
    vault_after_first = vault.read_text(encoding="utf-8")

    rc2 = migrate_main(["--dotenv", str(dotenv), "--env", "dev"])
    assert rc2 == 0
    assert _yaml_path_for_env("dev").read_text(encoding="utf-8") == yaml_after_first
    assert vault.read_text(encoding="utf-8") == vault_after_first


def test_migrate_refuses_yaml_conflict_without_force(
    tmp_path, monkeypatch, hermetic_secret_key, preserve_yaml,
):
    """If yaml already has docker_runtime=runsc and the dotenv says
    runc, the migrator MUST refuse without ``--force`` so an
    accidental import doesn't silently flip a prod default."""
    dotenv = tmp_path / ".env"
    _write_dotenv(dotenv, ["OMNISIGHT_DOCKER_RUNTIME=runc"])
    vault = tmp_path / "v.vault"
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))

    # config/prod.yaml already pins docker_runtime: runsc
    rc = migrate_main(["--dotenv", str(dotenv), "--env", "prod"])
    assert rc == 4  # conflict exit code

    # YAML was NOT mutated.
    import yaml
    data = yaml.safe_load(_yaml_path_for_env("prod").read_text(encoding="utf-8"))
    assert data["docker_runtime"] == "runsc"


def test_migrate_force_overwrites_conflict(
    tmp_path, monkeypatch, hermetic_secret_key, preserve_yaml,
):
    dotenv = tmp_path / ".env"
    _write_dotenv(dotenv, ["OMNISIGHT_DOCKER_RUNTIME=runc"])
    vault = tmp_path / "v.vault"
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))

    rc = migrate_main([
        "--dotenv", str(dotenv),
        "--env", "prod",
        "--force",
    ])
    assert rc == 0

    import yaml
    data = yaml.safe_load(_yaml_path_for_env("prod").read_text(encoding="utf-8"))
    assert data["docker_runtime"] == "runc"


def test_migrate_handles_quoted_and_inline_comments(
    tmp_path, monkeypatch, hermetic_secret_key, preserve_yaml,
):
    """The dotenv parser MUST strip outer quotes and tolerate inline
    ``# comment`` on unquoted values, matching pydantic-settings'
    own dotenv loader."""
    dotenv = tmp_path / ".env"
    _write_dotenv(dotenv, [
        'OMNISIGHT_AUTH_MODE="strict"',
        "OMNISIGHT_DOCKER_RUNTIME=runc # was runsc; reverted",
    ])
    vault = tmp_path / "v.vault"
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))

    rc = migrate_main([
        "--dotenv", str(dotenv),
        "--env", "dev",
        "--force",
    ])
    assert rc == 0

    import yaml
    data = yaml.safe_load(_yaml_path_for_env("dev").read_text(encoding="utf-8"))
    assert data["auth_mode"] == "strict"
    assert data["docker_runtime"] == "runc"


def test_migrate_missing_dotenv_returns_2(tmp_path, monkeypatch, hermetic_secret_key):
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(tmp_path / "v.vault"))
    rc = migrate_main([
        "--dotenv", str(tmp_path / "does-not-exist.env"),
        "--env", "dev",
    ])
    assert rc == 2
