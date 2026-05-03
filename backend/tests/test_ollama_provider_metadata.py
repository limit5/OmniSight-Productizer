"""Tests for Ollama provider metadata."""

from __future__ import annotations

import os


class TestOllamaToolCallingCompat:

    def test_tool_calling_compat_invalidates_on_yaml_mtime(self, monkeypatch, tmp_path):
        from backend.agents import llm

        compat_path = tmp_path / "ollama_tool_calling.yaml"
        compat_path.write_text(
            "models:\n"
            "  first-model:\n"
            "    support: full\n"
            "    min_ollama_version: '0.3.0'\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(llm, "_OLLAMA_TOOL_COMPAT_PATH", compat_path)
        llm.reload_ollama_tool_calling_compat_for_tests()

        first = llm._load_ollama_tool_calling_compat()
        compat_path.write_text(
            "models:\n"
            "  second-model:\n"
            "    support: partial\n"
            "    min_ollama_version: '0.4.0'\n",
            encoding="utf-8",
        )
        stat = compat_path.stat()
        os.utime(compat_path, (stat.st_atime + 2.0, stat.st_mtime + 2.0))
        later = llm._load_ollama_tool_calling_compat()

        assert list(first) == ["first-model"]
        assert list(later) == ["second-model"]
        assert later["second-model"]["support"] == "partial"
