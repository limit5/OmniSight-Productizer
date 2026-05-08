"""OP-764 D3 — multi-env YAML + vault overlay applied to ``Settings``.

Exercises the ``backend.config._apply_*_overlay`` helpers and the
"synthetic staging spin-up" acceptance test: a fresh process with
only ``OMNISIGHT_ENV=staging`` (plus a populated vault) MUST come
up with non-secret defaults from ``config/staging.yaml`` and secret
values from the vault — no manual ``.env`` copy.
"""

from __future__ import annotations

import os

import pytest

from backend import config as cfg
from backend import secrets_provider as sp


# ─── Helpers ─────────────────────────────────────────────────────


@pytest.fixture
def clean_overlay_env(monkeypatch):
    """Strip env vars the overlay would otherwise see as already-set,
    so each test exercises the overlay write path deterministically.

    monkeypatch undoes both setenv and delenv on teardown — values
    we ``setenv`` survive the test, values we delete are restored.
    Anything the overlay wrote with raw ``os.environ[KEY] = ...``
    is the test's responsibility to clean; we collect the set in a
    sentinel list each test can extend.
    """
    written: list[str] = []

    yield monkeypatch, written

    # Undo any direct os.environ writes the overlay made.
    for key in written:
        os.environ.pop(key, None)


# ─── _apply_yaml_overlay ─────────────────────────────────────────


def test_yaml_overlay_writes_non_secret_fields(tmp_path, clean_overlay_env):
    monkeypatch, written = clean_overlay_env
    yaml_path = tmp_path / "fake.yaml"
    yaml_path.write_text(
        "env: staging\n"
        "auth_mode: strict\n"
        "docker_runtime: runsc\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OMNISIGHT_ENV", raising=False)
    monkeypatch.delenv("OMNISIGHT_AUTH_MODE", raising=False)
    monkeypatch.delenv("OMNISIGHT_DOCKER_RUNTIME", raising=False)
    written.extend(["OMNISIGHT_ENV", "OMNISIGHT_AUTH_MODE", "OMNISIGHT_DOCKER_RUNTIME"])

    applied = cfg._apply_yaml_overlay(yaml_path)
    assert applied == 3
    assert os.environ["OMNISIGHT_ENV"] == "staging"
    assert os.environ["OMNISIGHT_AUTH_MODE"] == "strict"
    assert os.environ["OMNISIGHT_DOCKER_RUNTIME"] == "runsc"


def test_yaml_overlay_does_not_overwrite_existing_env(tmp_path, clean_overlay_env):
    """Real shell env / deploy ``--override`` MUST win — the overlay
    only fills in values that aren't already set."""
    monkeypatch, _ = clean_overlay_env
    yaml_path = tmp_path / "fake.yaml"
    yaml_path.write_text("auth_mode: open\n", encoding="utf-8")
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")

    applied = cfg._apply_yaml_overlay(yaml_path)
    assert applied == 0
    assert os.environ["OMNISIGHT_AUTH_MODE"] == "strict"


def test_yaml_overlay_rejects_secret_field(tmp_path, clean_overlay_env, caplog):
    """A YAML that names a SECRET_FIELDS entry MUST be skipped, so
    a leak via a hand-edited yaml never reaches Settings."""
    monkeypatch, written = clean_overlay_env
    yaml_path = tmp_path / "fake.yaml"
    yaml_path.write_text(
        "auth_mode: strict\n"
        "anthropic_api_key: sk-ant-LEAK\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OMNISIGHT_AUTH_MODE", raising=False)
    monkeypatch.delenv("OMNISIGHT_ANTHROPIC_API_KEY", raising=False)
    written.extend(["OMNISIGHT_AUTH_MODE", "OMNISIGHT_ANTHROPIC_API_KEY"])

    with caplog.at_level("ERROR", logger="backend.config"):
        applied = cfg._apply_yaml_overlay(yaml_path)
    assert applied == 1  # only auth_mode, not the secret
    assert os.environ.get("OMNISIGHT_ANTHROPIC_API_KEY") in (None, "")
    assert any("secret field" in r.getMessage() for r in caplog.records)


def test_yaml_overlay_skips_unknown_field(tmp_path, clean_overlay_env, caplog):
    monkeypatch, written = clean_overlay_env
    yaml_path = tmp_path / "fake.yaml"
    yaml_path.write_text(
        "auth_mode: strict\n"
        "imaginary_knob: 42\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OMNISIGHT_AUTH_MODE", raising=False)
    monkeypatch.delenv("OMNISIGHT_IMAGINARY_KNOB", raising=False)
    written.extend(["OMNISIGHT_AUTH_MODE", "OMNISIGHT_IMAGINARY_KNOB"])

    with caplog.at_level("WARNING", logger="backend.config"):
        applied = cfg._apply_yaml_overlay(yaml_path)
    assert applied == 1
    assert "OMNISIGHT_IMAGINARY_KNOB" not in os.environ
    assert any("unknown" in r.getMessage().lower() for r in caplog.records)


def test_yaml_overlay_skips_empty_and_none_values(tmp_path, clean_overlay_env):
    """Empty string / null in YAML means "fall through" — don't
    set a sentinel empty env that would override a downstream
    pydantic default."""
    monkeypatch, written = clean_overlay_env
    yaml_path = tmp_path / "fake.yaml"
    yaml_path.write_text(
        "frontend_origin: \"\"\n"
        "next_public_api_url: null\n"
        "auth_mode: strict\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OMNISIGHT_FRONTEND_ORIGIN", raising=False)
    monkeypatch.delenv("OMNISIGHT_NEXT_PUBLIC_API_URL", raising=False)
    monkeypatch.delenv("OMNISIGHT_AUTH_MODE", raising=False)
    written.extend([
        "OMNISIGHT_FRONTEND_ORIGIN",
        "OMNISIGHT_NEXT_PUBLIC_API_URL",
        "OMNISIGHT_AUTH_MODE",
    ])

    applied = cfg._apply_yaml_overlay(yaml_path)
    assert applied == 1
    assert "OMNISIGHT_FRONTEND_ORIGIN" not in os.environ
    assert "OMNISIGHT_NEXT_PUBLIC_API_URL" not in os.environ


def test_yaml_overlay_coerces_bool_to_lowercase(tmp_path, clean_overlay_env):
    """YAML bools must round-trip into pydantic-readable lowercase
    strings (``true`` / ``false``); ``str(False)`` would yield
    ``"False"`` which pydantic-settings does coerce, but it's not
    the documented form."""
    monkeypatch, written = clean_overlay_env
    yaml_path = tmp_path / "fake.yaml"
    yaml_path.write_text(
        "debug: true\n"
        "ci_mode: false\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OMNISIGHT_DEBUG", raising=False)
    monkeypatch.delenv("OMNISIGHT_CI_MODE", raising=False)
    written.extend(["OMNISIGHT_DEBUG", "OMNISIGHT_CI_MODE"])

    cfg._apply_yaml_overlay(yaml_path)
    assert os.environ["OMNISIGHT_DEBUG"] == "true"
    assert os.environ["OMNISIGHT_CI_MODE"] == "false"


def test_yaml_overlay_handles_malformed_top_level(tmp_path, clean_overlay_env, caplog):
    """A YAML that is not a mapping (list / scalar) MUST NOT crash
    the boot — log + return 0."""
    yaml_path = tmp_path / "fake.yaml"
    yaml_path.write_text("- 1\n- 2\n", encoding="utf-8")
    with caplog.at_level("ERROR", logger="backend.config"):
        assert cfg._apply_yaml_overlay(yaml_path) == 0
    assert any("must be a mapping" in r.getMessage() for r in caplog.records)


# ─── _apply_secrets_overlay ──────────────────────────────────────


def test_secrets_overlay_env_backend_is_noop(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "env")
    assert cfg._apply_secrets_overlay() == 0


def test_secrets_overlay_pulls_from_file_vault(tmp_path, monkeypatch, clean_overlay_env):
    _, written = clean_overlay_env
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-overlay")
    from backend import secret_store
    secret_store._reset_for_tests()

    vault = tmp_path / "v.vault"
    provider = sp.FileSecretsProvider(path=vault)
    provider.put("anthropic_api_key", "sk-ant-overlay-test")
    provider.put("github_token", "ghp_overlay_test")

    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))
    monkeypatch.delenv("OMNISIGHT_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OMNISIGHT_GITHUB_TOKEN", raising=False)
    written.extend(["OMNISIGHT_ANTHROPIC_API_KEY", "OMNISIGHT_GITHUB_TOKEN"])

    written_count = cfg._apply_secrets_overlay()
    assert written_count == 2
    assert os.environ["OMNISIGHT_ANTHROPIC_API_KEY"] == "sk-ant-overlay-test"
    assert os.environ["OMNISIGHT_GITHUB_TOKEN"] == "ghp_overlay_test"


def test_secrets_overlay_does_not_overwrite_explicit_env(
    tmp_path, monkeypatch, clean_overlay_env,
):
    """An operator-supplied ``--override OMNISIGHT_FOO=bar`` MUST
    win over a vault-stored value of the same key."""
    _, _ = clean_overlay_env
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-explicit")
    from backend import secret_store
    secret_store._reset_for_tests()

    vault = tmp_path / "v.vault"
    sp.FileSecretsProvider(path=vault).put("anthropic_api_key", "sk-ant-vault-side")

    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))
    monkeypatch.setenv("OMNISIGHT_ANTHROPIC_API_KEY", "sk-ant-cli-override")

    cfg._apply_secrets_overlay()
    assert os.environ["OMNISIGHT_ANTHROPIC_API_KEY"] == "sk-ant-cli-override"


# ─── _resolve_yaml_path ──────────────────────────────────────────


def test_resolve_yaml_path_finds_dev_staging_prod():
    """The three repo-checked-in YAMLs MUST be found by their
    canonical names. The synthetic staging spin-up below relies on
    this resolution."""
    assert cfg._resolve_yaml_path("dev") is not None
    assert cfg._resolve_yaml_path("staging") is not None
    assert cfg._resolve_yaml_path("prod") is not None


def test_resolve_yaml_path_aliases_production_to_prod():
    """``OMNISIGHT_ENV=production`` (the value the strict-mode
    validator special-cases) MUST resolve to ``config/prod.yaml``."""
    p = cfg._resolve_yaml_path("production")
    assert p is not None
    assert p.name == "prod.yaml"


def test_resolve_yaml_path_unknown_env_returns_none():
    assert cfg._resolve_yaml_path("imaginary") is None


# ─── Synthetic spin-up: AC #4 ────────────────────────────────────


def test_synthetic_staging_spinup_no_dotenv_required(
    tmp_path, monkeypatch, clean_overlay_env,
):
    """Headline acceptance test for OP-764 D3:

    A fresh process with **only** ``OMNISIGHT_ENV=staging`` plus a
    pre-populated vault MUST produce a fully-configured ``Settings``
    instance — non-secrets from ``config/staging.yaml``, secrets from
    the vault. No ``.env`` involvement.

    Mirrors the AC text: "new staging env spins up with only
    ``--env staging`` flag, no manual .env copy".
    """
    _, written = clean_overlay_env

    # 1. Hermetic Fernet key for the test vault.
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-spinup")
    from backend import secret_store
    secret_store._reset_for_tests()

    # 2. Pre-populate the vault with the secrets a real staging deploy
    #    would have. These are NOT real keys — placeholder values that
    #    let us assert the round-trip into Settings.
    vault = tmp_path / "v.vault"
    provider = sp.FileSecretsProvider(path=vault)
    provider.put("anthropic_api_key", "sk-ant-staging-fixture")
    provider.put("admin_password", "passphrase-of-twelve-or-more-chars")
    provider.put("decision_bearer", "bearer-of-at-least-sixteen-chars-please")

    # 3. Strip every env var the overlay would otherwise see as
    #    already-set, so the overlay write path actually runs.
    for env_name in (
        "OMNISIGHT_ENV", "OMNISIGHT_AUTH_MODE", "OMNISIGHT_DOCKER_RUNTIME",
        "OMNISIGHT_DEBUG", "OMNISIGHT_CI_MODE", "OMNISIGHT_LLM_PROVIDER",
        "OMNISIGHT_ANTHROPIC_API_KEY", "OMNISIGHT_ADMIN_PASSWORD",
        "OMNISIGHT_DECISION_BEARER",
        "OMNISIGHT_TOKEN_BUDGET_DAILY", "OMNISIGHT_TOKEN_BUDGET_HOURLY",
        "OMNISIGHT_WEB_SEARCH_PROVIDER", "OMNISIGHT_FRONTEND_ORIGIN",
    ):
        monkeypatch.delenv(env_name, raising=False)
        written.append(env_name)

    # 4. Set ONLY the bootstrap envelope: which environment + which
    #    secrets backend. Everything else flows from the YAML + vault.
    monkeypatch.setenv("OMNISIGHT_ENV", "staging")
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))

    # 5. Apply the overlay (mimics ``backend.config`` module init).
    cfg._apply_env_overlay()

    # 6. Assert the YAML defaults made it into env.
    assert os.environ["OMNISIGHT_AUTH_MODE"] == "strict", (
        "config/staging.yaml pins auth_mode=strict"
    )
    assert os.environ["OMNISIGHT_DOCKER_RUNTIME"] == "runsc"
    assert os.environ["OMNISIGHT_LLM_PROVIDER"] == "anthropic"

    # 7. Assert the vault secrets made it into env.
    assert os.environ["OMNISIGHT_ANTHROPIC_API_KEY"] == "sk-ant-staging-fixture"
    assert os.environ["OMNISIGHT_ADMIN_PASSWORD"] == "passphrase-of-twelve-or-more-chars"

    # 8. Settings() reads the populated env — the headline behaviour.
    settings_obj = cfg.Settings(_env_file=None)
    assert settings_obj.env == "staging"
    assert settings_obj.auth_mode == "strict"
    assert settings_obj.docker_runtime == "runsc"
    assert settings_obj.llm_provider == "anthropic"
    assert settings_obj.anthropic_api_key == "sk-ant-staging-fixture"
    assert settings_obj.admin_password == "passphrase-of-twelve-or-more-chars"


def test_synthetic_staging_spinup_respects_per_deploy_override(
    tmp_path, monkeypatch, clean_overlay_env,
):
    """AC #3 corollary: ``--override OMNISIGHT_FOO=bar`` (modeled
    here as a pre-set env var) MUST take precedence over both the
    YAML default and the vault."""
    _, written = clean_overlay_env

    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-override")
    from backend import secret_store
    secret_store._reset_for_tests()

    vault = tmp_path / "v.vault"
    sp.FileSecretsProvider(path=vault).put("anthropic_api_key", "sk-ant-vault")

    for env_name in (
        "OMNISIGHT_AUTH_MODE", "OMNISIGHT_DOCKER_RUNTIME",
        "OMNISIGHT_LLM_PROVIDER", "OMNISIGHT_DEBUG", "OMNISIGHT_CI_MODE",
        "OMNISIGHT_TOKEN_BUDGET_DAILY", "OMNISIGHT_TOKEN_BUDGET_HOURLY",
        "OMNISIGHT_WEB_SEARCH_PROVIDER", "OMNISIGHT_FRONTEND_ORIGIN",
    ):
        monkeypatch.delenv(env_name, raising=False)
        written.append(env_name)

    monkeypatch.setenv("OMNISIGHT_ENV", "staging")
    monkeypatch.setenv("OMNISIGHT_SECRETS_BACKEND", "file")
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(vault))
    # Operator overrides for this one boot:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    monkeypatch.setenv("OMNISIGHT_ANTHROPIC_API_KEY", "sk-ant-cli-wins")

    cfg._apply_env_overlay()

    # Operator override beats yaml.
    assert os.environ["OMNISIGHT_AUTH_MODE"] == "open"
    # Operator override beats vault.
    assert os.environ["OMNISIGHT_ANTHROPIC_API_KEY"] == "sk-ant-cli-wins"
