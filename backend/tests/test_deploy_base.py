"""W4 #278 — Tests for the shared deploy adapter base + factory."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import secret_store
from backend.deploy import (
    BuildArtifact,
    DeployArtifactError,
    DeployError,
    WebDeployAdapter,
    get_adapter,
    list_providers,
    token_fingerprint,
)
from backend.deploy.base import (
    DeployResult,
    ProvisionResult,
    RollbackUnavailableError,
)


class TestProviderFactory:

    def test_list_providers_enumerates_four(self):
        providers = list_providers()
        assert providers == ["vercel", "netlify", "cloudflare-pages", "docker-nginx"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("vercel", "VercelAdapter"),
            ("netlify", "NetlifyAdapter"),
            ("cloudflare-pages", "CloudflarePagesAdapter"),
            ("cloudflare", "CloudflarePagesAdapter"),
            ("cf-pages", "CloudflarePagesAdapter"),
            ("docker-nginx", "DockerNginxAdapter"),
            ("docker_nginx", "DockerNginxAdapter"),
            ("docker", "DockerNginxAdapter"),
            ("nginx", "DockerNginxAdapter"),
            ("VERCEL", "VercelAdapter"),
        ],
    )
    def test_get_adapter_resolves_known(self, key, cls_name):
        cls = get_adapter(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, WebDeployAdapter)

    def test_get_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            get_adapter("s3-static")
        assert "Unknown deploy provider" in str(excinfo.value)
        for p in list_providers():
            assert p in str(excinfo.value)

    def test_every_adapter_has_unique_provider_classvar(self):
        seen = set()
        for p in list_providers():
            cls = get_adapter(p)
            assert cls.provider, f"{cls.__name__} missing provider classvar"
            assert cls.provider not in seen
            seen.add(cls.provider)


class TestTokenFingerprint:

    def test_masks_short_tokens(self):
        assert token_fingerprint("") == "****"
        assert token_fingerprint("abcd1234") == "****"

    def test_shows_last_four_for_long_tokens(self):
        token = "cfABCDEF" + "x" * 32 + "WXYZ"
        fp = token_fingerprint(token)
        assert fp.endswith("WXYZ")
        assert "ABCDEF" not in fp
        # Ensure the full token is not inside the fingerprint.
        assert token not in fp


class TestBuildArtifact:

    def test_path_is_coerced_to_pathlib(self, tmp_path):
        ba = BuildArtifact(path=str(tmp_path))
        assert isinstance(ba.path, Path)
        assert ba.path == tmp_path

    def test_validate_ok(self, tmp_path):
        (tmp_path / "a.html").write_text("<html/>")
        ba = BuildArtifact(path=tmp_path)
        ba.validate()  # no raise

    def test_validate_missing_dir_raises(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        ba = BuildArtifact(path=missing)
        with pytest.raises(DeployArtifactError):
            ba.validate()

    def test_validate_path_is_file_not_dir_raises(self, tmp_path):
        f = tmp_path / "a.html"
        f.write_text("x")
        ba = BuildArtifact(path=f)
        with pytest.raises(DeployArtifactError):
            ba.validate()


class TestEncryptedTokenFactory:

    def test_from_encrypted_token_decrypts_via_secret_store(self, tmp_path, monkeypatch):
        # Isolate secret store to a temp key file.
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-w4")
        secret_store._reset_for_tests()

        plaintext = "vrc_abcdef0123456789"
        ciphertext = secret_store.encrypt(plaintext)

        vercel_cls = get_adapter("vercel")
        adapter = vercel_cls.from_encrypted_token(ciphertext, project_name="demo-app")
        assert isinstance(adapter, WebDeployAdapter)
        assert adapter.project_name == "demo-app"
        # Token fingerprint only exposes the last four chars.
        fp = adapter.token_fp()
        assert fp.endswith("6789")
        assert plaintext not in fp

    def test_from_plaintext_token_bypasses_secret_store(self):
        vercel_cls = get_adapter("vercel")
        adapter = vercel_cls.from_plaintext_token("xx123456789012", project_name="p")
        assert adapter.project_name == "p"


class TestResultDataclasses:

    def test_provision_result_to_dict(self):
        r = ProvisionResult(
            provider="vercel", project_id="prj_1", project_name="demo",
            url="https://demo.vercel.app", created=True,
            env_vars_set=["API_URL"],
        )
        d = r.to_dict()
        assert d["provider"] == "vercel"
        assert d["project_id"] == "prj_1"
        assert d["url"].startswith("https://")
        assert d["env_vars_set"] == ["API_URL"]

    def test_deploy_result_to_dict(self):
        r = DeployResult(
            provider="netlify", deployment_id="dep_1",
            url="https://demo.netlify.app", status="ready",
        )
        d = r.to_dict()
        assert d["provider"] == "netlify"
        assert d["deployment_id"] == "dep_1"
        assert d["status"] == "ready"


class TestInterfaceContract:
    """Every adapter must implement the four abstract methods."""

    @pytest.mark.parametrize("provider", ["vercel", "netlify", "cloudflare-pages", "docker-nginx"])
    def test_required_methods_present(self, provider):
        cls = get_adapter(provider)
        for name in ("provision", "deploy", "rollback", "get_url"):
            assert callable(getattr(cls, name)), f"{cls.__name__} missing {name}"

    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            WebDeployAdapter(token="t", project_name="p")  # type: ignore[abstract]

    def test_rollback_unavailable_error_is_deploy_error_subclass(self):
        assert issubclass(RollbackUnavailableError, DeployError)
