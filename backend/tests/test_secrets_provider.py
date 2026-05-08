"""OP-764 D3 — :mod:`backend.secrets_provider` unit tests.

Covers the SECRET_FIELDS registry, the three concrete providers
(env / file / hvac shim), and the factory dispatch. The hvac
backend is exercised only through the factory + missing-SDK path —
the real Vault round-trip lives in an integration test outside the
unit suite (no Vault server in the unit job).
"""

from __future__ import annotations

import json

import pytest

from backend import secrets_provider as sp


def test_secret_fields_is_a_frozenset_of_known_creds():
    assert isinstance(sp.SECRET_FIELDS, frozenset)
    # Spot-check across the LLM / git / chatops / OAuth families
    # so a future refactor that drops one entry trips the suite.
    for expected in (
        "anthropic_api_key",
        "openai_api_key",
        "github_token",
        "gerrit_webhook_secret",
        "stripe_secret_key",
        "admin_password",
        "decision_bearer",
        "oauth_google_client_secret",
        "cf_api_token",
    ):
        assert expected in sp.SECRET_FIELDS, expected


def test_is_secret_field_handles_known_and_unknown():
    assert sp.is_secret_field("anthropic_api_key") is True
    # Non-secret config knobs MUST not be in the registry.
    assert sp.is_secret_field("auth_mode") is False
    assert sp.is_secret_field("frontend_origin") is False
    assert sp.is_secret_field("docker_runtime") is False
    # Unknown names default to non-secret (the migrator uses this
    # path to decide where to put a key — false means "yaml").
    assert sp.is_secret_field("not_a_real_field") is False


# ── EnvSecretsProvider ───────────────────────────────────────────


def test_env_provider_reads_from_os_environ(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_ANTHROPIC_API_KEY", "sk-ant-aaaa")
    provider = sp.EnvSecretsProvider()
    assert provider.get("anthropic_api_key") == "sk-ant-aaaa"


def test_env_provider_treats_empty_as_missing(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_ANTHROPIC_API_KEY", "")
    provider = sp.EnvSecretsProvider()
    assert provider.get("anthropic_api_key") is None


def test_env_provider_list_keys_filters_unset(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_ANTHROPIC_API_KEY", "sk-ant-bbbb")
    monkeypatch.delenv("OMNISIGHT_OPENAI_API_KEY", raising=False)
    provider = sp.EnvSecretsProvider()
    keys = list(provider.list_keys())
    assert "anthropic_api_key" in keys
    assert "openai_api_key" not in keys


def test_env_provider_put_is_rejected():
    with pytest.raises(sp.SecretsProviderError):
        sp.EnvSecretsProvider().put("foo", "bar")


# ── FileSecretsProvider ──────────────────────────────────────────


def test_file_provider_missing_file_returns_none(tmp_path):
    provider = sp.FileSecretsProvider(path=tmp_path / "missing.vault")
    assert provider.get("anthropic_api_key") is None
    assert list(provider.list_keys()) == []


def test_file_provider_round_trip(tmp_path, monkeypatch):
    # Use a tmp Fernet key so the test is hermetic — no dependency
    # on the dev box's data/.secret_key.
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-fixture")
    from backend import secret_store
    secret_store._reset_for_tests()

    vault = tmp_path / "secrets.vault"
    provider = sp.FileSecretsProvider(path=vault)
    provider.put("anthropic_api_key", "sk-ant-roundtrip")
    provider.put("openai_api_key", "sk-openai-roundtrip")

    # Reload from disk to verify on-disk format.
    fresh = sp.FileSecretsProvider(path=vault)
    assert fresh.get("anthropic_api_key") == "sk-ant-roundtrip"
    assert fresh.get("openai_api_key") == "sk-openai-roundtrip"
    assert set(fresh.list_keys()) == {"anthropic_api_key", "openai_api_key"}


def test_file_provider_overwrite_replaces_value(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-overwrite")
    from backend import secret_store
    secret_store._reset_for_tests()

    vault = tmp_path / "secrets.vault"
    provider = sp.FileSecretsProvider(path=vault)
    provider.put("anthropic_api_key", "sk-ant-old")
    provider.put("anthropic_api_key", "sk-ant-new")
    assert provider.get("anthropic_api_key") == "sk-ant-new"


def test_file_provider_empty_value_deletes_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-delete")
    from backend import secret_store
    secret_store._reset_for_tests()

    vault = tmp_path / "secrets.vault"
    provider = sp.FileSecretsProvider(path=vault)
    provider.put("anthropic_api_key", "sk-ant-remove-me")
    assert provider.get("anthropic_api_key") == "sk-ant-remove-me"
    provider.put("anthropic_api_key", "")
    assert provider.get("anthropic_api_key") is None


def test_file_provider_corrupt_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-corrupt")
    from backend import secret_store
    secret_store._reset_for_tests()

    vault = tmp_path / "secrets.vault"
    vault.write_text("not-a-fernet-token", encoding="utf-8")
    provider = sp.FileSecretsProvider(path=vault)
    with pytest.raises(sp.SecretsProviderError):
        provider.get("anthropic_api_key")


def test_file_provider_writes_atomic(tmp_path, monkeypatch):
    """Verify the temp-file + rename pattern leaves no half-written
    vault behind on a successful put. Indirect coverage — we just
    confirm there is no dangling ``*.tmp`` after a normal write."""
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-atomic")
    from backend import secret_store
    secret_store._reset_for_tests()

    vault = tmp_path / "secrets.vault"
    provider = sp.FileSecretsProvider(path=vault)
    provider.put("anthropic_api_key", "sk-ant-atomic")
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []


def test_file_provider_payload_is_actually_encrypted(tmp_path, monkeypatch):
    """Defence-in-depth: a random reader of the vault file MUST NOT
    see the secret value as plaintext bytes. If this regresses, the
    Fernet wiring broke and OP-764's headline guarantee (no plaintext
    secrets on disk) is violated."""
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-key-op764-encrypted")
    from backend import secret_store
    secret_store._reset_for_tests()

    vault = tmp_path / "secrets.vault"
    sp.FileSecretsProvider(path=vault).put("anthropic_api_key", "sk-ant-leak-canary")
    raw = vault.read_text(encoding="utf-8")
    assert "sk-ant-leak-canary" not in raw
    # Sanity: it's a Fernet token (base64-ish url-safe). Don't enforce
    # the exact prefix; just confirm we can't trivially json.loads it.
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


# ── HashiCorpVaultProvider ───────────────────────────────────────


def test_hvac_provider_missing_sdk_raises(monkeypatch):
    """When ``hvac`` is not installed, the lazy import inside
    ``_get_client`` MUST raise SecretsProviderError so the misconfig
    surfaces at boot, not inside the first secret read."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "hvac":
            raise ImportError("simulated missing hvac")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    provider = sp.HashiCorpVaultProvider(addr="http://localhost:8200", token="test")
    with pytest.raises(sp.SecretsProviderError, match="hvac SDK"):
        provider.get("anthropic_api_key")


def test_hvac_provider_empty_addr_raises(monkeypatch):
    """If the SDK *is* present but the addr is empty, the provider
    refuses to construct a client rather than silently defaulting
    to localhost."""
    monkeypatch.delenv("OMNISIGHT_VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    pytest.importorskip("hvac", reason="hvac not installed in this env — skip")
    provider = sp.HashiCorpVaultProvider(addr="", token="test")
    with pytest.raises(sp.SecretsProviderError, match="VAULT_ADDR"):
        provider.get("anthropic_api_key")


# ── Factory dispatch ─────────────────────────────────────────────


def test_make_secrets_provider_default_is_env(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_SECRETS_BACKEND", raising=False)
    provider = sp.make_secrets_provider()
    assert isinstance(provider, sp.EnvSecretsProvider)


def test_make_secrets_provider_file_backend_uses_default_path(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_SECRETS_FILE", raising=False)
    provider = sp.make_secrets_provider("file")
    assert isinstance(provider, sp.FileSecretsProvider)
    assert provider._path == sp.FileSecretsProvider.DEFAULT_PATH


def test_make_secrets_provider_file_backend_honours_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNISIGHT_SECRETS_FILE", str(tmp_path / "v.vault"))
    provider = sp.make_secrets_provider("file")
    assert isinstance(provider, sp.FileSecretsProvider)
    assert provider._path == tmp_path / "v.vault"


def test_make_secrets_provider_unknown_backend_raises():
    with pytest.raises(sp.SecretsProviderError, match="invalid"):
        sp.make_secrets_provider("not-a-backend")


def test_singleton_is_cached_and_resettable(monkeypatch):
    sp._reset_for_tests()
    monkeypatch.delenv("OMNISIGHT_SECRETS_BACKEND", raising=False)
    a = sp.get_secrets_provider()
    b = sp.get_secrets_provider()
    assert a is b
    sp._reset_for_tests()
    c = sp.get_secrets_provider()
    assert c is not a
