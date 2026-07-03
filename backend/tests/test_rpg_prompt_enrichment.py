"""OP-2522 — RPG progression → runner prompt enrichment.

The runner's ``_build_character_enrichment_block`` injects the owning
character's talents + capstone + distilled L2 skills into the pickup prompt.
Covers: the call-time flag gate, the bare-bot (character=None) no-op, fail-open
when the RPG stores are offline, and the happy path with faked stores.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_runner() -> Any:
    sys.modules.pop("jira_runner_enrich_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_enrich_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CHAR = SimpleNamespace(slug="iris", guild="isp")


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OMNISIGHT_RPG_PROMPT_ENRICH", raising=False)
    mod = _load_runner()
    assert mod._build_character_enrichment_block(_CHAR, "uvc", "s", "d") == ""


def test_none_character_is_noop(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_RPG_PROMPT_ENRICH", "1")
    mod = _load_runner()
    assert mod._build_character_enrichment_block(None, "uvc", "s", "d") == ""


def test_fail_open_when_stores_offline(monkeypatch) -> None:
    # Flag on but no DB/embedder available in the test env → every section
    # degrades and the whole block is empty (runner pickup must not wedge).
    monkeypatch.setenv("OMNISIGHT_RPG_PROMPT_ENRICH", "1")
    mod = _load_runner()
    out = mod._build_character_enrichment_block(_CHAR, "uvc", "summary", "desc")
    assert out == ""


def test_happy_path_injects_talents_and_l2(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_RPG_PROMPT_ENRICH", "1")
    mod = _load_runner()

    # Fake talent store: return sentinel choices + a capstone.
    class _FakeStore:
        def __init__(self, *_a, **_k) -> None: ...
        async def list_choices(self, agent_id):
            return ("choice",)
        async def get_capstone(self, agent_id):
            return "capstone"

    import backend.agents.talent_tree as tt
    import backend.agents.prompt_builder as pb
    import backend.agents.skill_memory as smem
    import backend.agents.rag_indexer as ri

    monkeypatch.setattr(tt, "PostgresTalentChoiceStore", _FakeStore)
    monkeypatch.setattr(
        pb, "enrich_system_prompt_with_talents",
        lambda p, choices, *, guild=None: p + "\nTALENT-REMINDER",
    )
    monkeypatch.setattr(
        pb, "enrich_system_prompt_with_capstone",
        lambda p, lock, *, guild=None: p + "\nCAPSTONE-SIG",
    )

    class _Emb:
        async def embed_query(self, t): return [0.0]
        async def embed_texts(self, ts): return [[0.0] for _ in ts]

    monkeypatch.setattr(ri, "_build_embedder_from_env", lambda: _Emb())

    async def _fake_store_env():
        return (object(), None)

    monkeypatch.setattr(ri, "_build_store_from_env", _fake_store_env)

    async def _fake_retrieve(*, tenant_id, query_text, embedder, store, top_k=5, **kw):
        return (SimpleNamespace(skill_id="uvc", summary="Bring up UVC at 720p30."),)

    monkeypatch.setattr(smem, "retrieve_distilled_skills", _fake_retrieve)

    out = mod._build_character_enrichment_block(_CHAR, "uvc", "cam bring-up", "desc")
    assert "iris" in out and "isp guild" in out
    assert "TALENT-REMINDER" in out and "CAPSTONE-SIG" in out
    assert "uvc: Bring up UVC at 720p30." in out
    assert "RPG L2" in out
