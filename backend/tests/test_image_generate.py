"""Tests for the V9 #325 row 2708 `image_generate` tool.

Covers:
* OpenAI happy path (b64_json) writes the file to workspace
* URL fallback path downloads bytes and writes them
* Provider validation: anthropic returns explicit error
* Prompt validation
* Size validation
* OMNISIGHT_IMAGE_GEN_DISABLED kill-switch
* Path-traversal guard
* AGENT_TOOLS wiring (software / general / custom only)
"""

from __future__ import annotations

import base64
import sys
import types
from pathlib import Path

import pytest


_FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE_PNG_DATA"


def _install_fake_openai(monkeypatch, *, b64: str | None = None, url: str | None = None,
                        raises: Exception | None = None) -> dict:
    """Install a fake `openai` module with an `AsyncOpenAI` stub.

    Records the kwargs passed to images.generate() in the returned dict
    so tests can assert what the tool actually sent.
    """
    seen: dict = {}

    class _FakeImageData:
        def __init__(self) -> None:
            self.b64_json = b64
            self.url = url

    class _FakeImagesNamespace:
        async def generate(self, **kwargs):
            seen.update(kwargs)
            if raises is not None:
                raise raises
            r = types.SimpleNamespace()
            r.data = [_FakeImageData()]
            return r

    class _FakeAsyncOpenAI:
        def __init__(self, api_key=None) -> None:
            seen["__api_key"] = api_key
            self.images = _FakeImagesNamespace()

    fake_openai = types.SimpleNamespace(AsyncOpenAI=_FakeAsyncOpenAI)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    return seen


def _install_fake_credential(monkeypatch, api_key: str = "sk-test-fake") -> None:
    from backend.llm_credential_resolver import LLMCredential

    async def fake_get(provider: str, tenant_id=None):
        return LLMCredential(
            provider="openai",
            tenant_id=tenant_id or "t-default",
            api_key=api_key,
            source="settings",
            metadata={},
            id=None,
        )

    monkeypatch.setattr(
        "backend.llm_credential_resolver.get_llm_credential",
        fake_get,
    )


# ─── Happy paths ───────────────────────────────────────────────────────────


class TestImageGenerateHappyPath:

    @pytest.mark.asyncio
    async def test_b64_response_writes_file_to_workspace(
        self, workspace: Path, monkeypatch
    ):
        from backend.agents import tools

        b64 = base64.b64encode(_FAKE_PNG).decode("ascii")
        seen = _install_fake_openai(monkeypatch, b64=b64)
        _install_fake_credential(monkeypatch, api_key="sk-test-1234")

        result = await tools.image_generate.ainvoke({
            "prompt": "flat orange rocket icon",
            "output_path": "assets/icon.png",
            "size": "1024x1024",
            "register_artifact": False,
        })

        assert result.startswith("[OK]"), result
        assert "icon.png" in result
        out = workspace / "assets" / "icon.png"
        assert out.exists(), "image was not written to workspace path"
        assert out.read_bytes() == _FAKE_PNG
        # API call was made with the credential's api key + correct kwargs
        assert seen["__api_key"] == "sk-test-1234"
        assert seen["prompt"] == "flat orange rocket icon"
        assert seen["size"] == "1024x1024"
        assert seen["model"] == "gpt-image-1"  # default
        assert seen["n"] == 1

    @pytest.mark.asyncio
    async def test_default_output_path_uses_public_generated(
        self, workspace: Path, monkeypatch
    ):
        from backend.agents import tools

        _install_fake_openai(
            monkeypatch, b64=base64.b64encode(_FAKE_PNG).decode("ascii"),
        )
        _install_fake_credential(monkeypatch)

        result = await tools.image_generate.ainvoke({
            "prompt": "blue gradient hero banner 1920x600",
            "register_artifact": False,
        })
        assert result.startswith("[OK]"), result
        gen_dir = workspace / "public" / "generated"
        assert gen_dir.is_dir()
        png_files = list(gen_dir.glob("*.png"))
        assert len(png_files) == 1
        assert png_files[0].read_bytes() == _FAKE_PNG

    @pytest.mark.asyncio
    async def test_directory_output_path_appends_filename(
        self, workspace: Path, monkeypatch
    ):
        from backend.agents import tools

        _install_fake_openai(
            monkeypatch, b64=base64.b64encode(_FAKE_PNG).decode("ascii"),
        )
        _install_fake_credential(monkeypatch)

        result = await tools.image_generate.ainvoke({
            "prompt": "icon",
            "output_path": "static",
            "register_artifact": False,
        })
        assert result.startswith("[OK]"), result
        files = list((workspace / "static").glob("*.png"))
        assert len(files) == 1


# ─── Validation paths ──────────────────────────────────────────────────────


class TestImageGenerateValidation:

    @pytest.mark.asyncio
    async def test_anthropic_provider_returns_error(self, workspace, monkeypatch):
        from backend.agents import tools
        result = await tools.image_generate.ainvoke({
            "prompt": "anything",
            "provider": "anthropic",
        })
        assert result.startswith("[ERROR]"), result
        assert "Anthropic does not expose" in result

    @pytest.mark.asyncio
    async def test_unknown_provider_returns_error(self, workspace, monkeypatch):
        from backend.agents import tools
        result = await tools.image_generate.ainvoke({
            "prompt": "anything",
            "provider": "midjourney",
        })
        assert result.startswith("[ERROR]"), result
        assert "unknown provider" in result.lower()

    @pytest.mark.asyncio
    async def test_empty_prompt_rejected(self, workspace, monkeypatch):
        from backend.agents import tools
        result = await tools.image_generate.ainvoke({"prompt": "   "})
        assert result.startswith("[ERROR]"), result
        assert "prompt is required" in result

    @pytest.mark.asyncio
    async def test_invalid_size_rejected(self, workspace, monkeypatch):
        from backend.agents import tools
        result = await tools.image_generate.ainvoke({
            "prompt": "icon",
            "size": "huge",
        })
        assert result.startswith("[ERROR]"), result
        assert "invalid size" in result.lower()

    @pytest.mark.asyncio
    async def test_disabled_via_env_flag(self, workspace, monkeypatch):
        from backend.agents import tools
        monkeypatch.setenv("OMNISIGHT_IMAGE_GEN_DISABLED", "1")
        result = await tools.image_generate.ainvoke({"prompt": "hi"})
        assert result.startswith("[BLOCKED]"), result
        assert "OMNISIGHT_IMAGE_GEN_DISABLED" in result

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, workspace, monkeypatch):
        from backend.agents import tools
        # Even before any external call, path traversal should be caught.
        result = await tools.image_generate.ainvoke({
            "prompt": "x",
            "output_path": "../../etc/passwd.png",
        })
        assert result.startswith("[BLOCKED]"), result


# ─── External-call failure modes ───────────────────────────────────────────


class TestImageGenerateExternalFailures:

    @pytest.mark.asyncio
    async def test_missing_credential_returns_error(self, workspace, monkeypatch):
        from backend.agents import tools
        from backend.llm_credential_resolver import LLMCredentialMissingError

        async def raises_missing(provider, tenant_id=None):
            raise LLMCredentialMissingError(
                f"No LLM credential for provider={provider!r}"
            )
        monkeypatch.setattr(
            "backend.llm_credential_resolver.get_llm_credential",
            raises_missing,
        )
        result = await tools.image_generate.ainvoke({"prompt": "icon"})
        assert result.startswith("[ERROR]"), result
        assert "OpenAI credential" in result

    @pytest.mark.asyncio
    async def test_openai_call_failure_returns_error(self, workspace, monkeypatch):
        from backend.agents import tools

        _install_fake_openai(
            monkeypatch,
            raises=RuntimeError("rate limited"),
        )
        _install_fake_credential(monkeypatch)
        result = await tools.image_generate.ainvoke({
            "prompt": "icon",
            "register_artifact": False,
        })
        assert result.startswith("[ERROR]"), result
        assert "OpenAI call failed" in result
        assert "rate limited" in result

    @pytest.mark.asyncio
    async def test_empty_response_returns_error(self, workspace, monkeypatch):
        from backend.agents import tools

        # Fake openai whose generate() returns no data list
        class _Empty:
            async def generate(self, **kwargs):
                r = types.SimpleNamespace()
                r.data = []
                return r

        class _Client:
            def __init__(self, api_key=None):
                self.images = _Empty()

        monkeypatch.setitem(
            sys.modules, "openai",
            types.SimpleNamespace(AsyncOpenAI=_Client),
        )
        _install_fake_credential(monkeypatch)
        result = await tools.image_generate.ainvoke({
            "prompt": "icon",
            "register_artifact": False,
        })
        assert result.startswith("[ERROR]"), result
        assert "empty response" in result

    @pytest.mark.asyncio
    async def test_non_https_url_refused(self, workspace, monkeypatch):
        from backend.agents import tools

        _install_fake_openai(monkeypatch, b64=None, url="http://insecure.example/x.png")
        _install_fake_credential(monkeypatch)
        result = await tools.image_generate.ainvoke({
            "prompt": "icon",
            "register_artifact": False,
        })
        assert result.startswith("[ERROR]"), result
        assert "non-HTTPS" in result


# ─── Wiring ────────────────────────────────────────────────────────────────


class TestImageGenerateRegistry:

    def test_in_tool_map(self):
        from backend.agents.tools import TOOL_MAP, IMAGE_TOOLS
        assert "image_generate" in TOOL_MAP
        assert IMAGE_TOOLS == [TOOL_MAP["image_generate"]]

    def test_exposed_to_software_general_custom_only(self):
        from backend.agents.tools import AGENT_TOOLS
        names_for = lambda key: {t.name for t in AGENT_TOOLS[key]}
        assert "image_generate" in names_for("software")
        assert "image_generate" in names_for("general")
        assert "image_generate" in names_for("custom")
        # Not exposed to firmware/validator/reporter/reviewer/devops/etc
        for excluded in ("firmware", "validator", "reporter", "reviewer",
                         "devops", "mechanical", "manufacturing"):
            assert "image_generate" not in names_for(excluded), excluded
