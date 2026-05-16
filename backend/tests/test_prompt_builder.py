"""OP-1199 -- type-contract + closure-behavior tests for prompt_builder.

`backend/agents/prompt_builder.py` already had its pure compute helper
(`enrich_system_prompt_with_talents`) covered by `test_talent_tree.py`.
The closure factory `build_talent_prompt_enricher` was untested. These
tests pin its declared signature (W14.4 wiring contract) and its three
documented branches:

* empty / missing per-agent talent set -> returns unmodified prompt
* ``store.list_choices`` raises -> degrade silently, return unmodified
* talents present -> returns the same string as the pure helper

Also asserts the return annotation now resolves to a Callable producing
an Awaitable[str], replacing the previous ``Any`` placeholder.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import pytest

from backend.agents.prompt_builder import (
    TALENT_REMINDER_HEADER,
    build_talent_prompt_enricher,
    enrich_system_prompt_with_talents,
)
from backend.agents.talent_tree import (
    InMemoryTalentChoiceStore,
    TalentChoice,
)
from backend.sandbox_tier import Guild


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _ExplodingTalentChoiceStore:
    """Stand-in for a store whose backend is unreachable.

    Mirrors the closure's degrade-silently contract — the dispatch path
    must never crash because the talent layer raised.
    """

    async def get_choice(self, agent_id: str, milestone_level: int) -> None:
        raise RuntimeError("store unreachable")

    async def list_choices(self, agent_id: str) -> tuple[TalentChoice, ...]:
        raise RuntimeError("store unreachable")

    async def upsert_choice(self, choice: TalentChoice) -> TalentChoice:
        raise RuntimeError("store unreachable")


# ── Signature / type-hint coverage ──────────────────────────────────


def test_build_talent_prompt_enricher_return_annotation_is_async_callable():
    """OP-1199 — the factory's return type must declare an awaitable str.

    Before OP-1199 the annotation was ``Any``; this test pins the
    Callable[..., Awaitable[str]] contract so a future refactor can't
    silently regress to the placeholder.
    """
    hints = typing.get_type_hints(build_talent_prompt_enricher)
    assert hints["return"] == Callable[..., Awaitable[str]]


def test_enrich_system_prompt_with_talents_signature_is_fully_typed():
    """No parameter or return type on the pure helper should be untyped."""
    sig = inspect.signature(enrich_system_prompt_with_talents)
    for name, param in sig.parameters.items():
        assert param.annotation is not inspect.Parameter.empty, (
            f"parameter {name!r} is missing a type annotation"
        )
    assert sig.return_annotation is not inspect.Signature.empty


def test_build_talent_prompt_enricher_signature_is_fully_typed():
    """No parameter or return type on the factory should be untyped."""
    sig = inspect.signature(build_talent_prompt_enricher)
    for name, param in sig.parameters.items():
        assert param.annotation is not inspect.Parameter.empty, (
            f"parameter {name!r} is missing a type annotation"
        )
    assert sig.return_annotation is not inspect.Signature.empty


# ── Closure behavior ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enricher_returns_prompt_unchanged_when_no_choices():
    store = InMemoryTalentChoiceStore()
    enrich = build_talent_prompt_enricher(store)

    base = "You are a backend agent."
    enriched = await enrich("agent-A", base, guild=Guild.backend)

    assert enriched == base


@pytest.mark.asyncio
async def test_enricher_degrades_silently_when_store_raises():
    enrich = build_talent_prompt_enricher(_ExplodingTalentChoiceStore())

    base = "You are a backend agent."
    enriched = await enrich("agent-A", base, guild=Guild.backend)

    assert enriched == base


@pytest.mark.asyncio
async def test_enricher_appends_reminder_block_when_choices_present():
    store = InMemoryTalentChoiceStore()
    await store.upsert_choice(
        TalentChoice("agent-A", 10, "security-first", T0)
    )
    enrich = build_talent_prompt_enricher(store)

    base = "You are a backend agent."
    enriched = await enrich("agent-A", base, guild=Guild.backend)

    assert enriched != base
    assert TALENT_REMINDER_HEADER in enriched
    assert enriched.startswith(base)
    # Closure must agree with the pure helper for the same inputs.
    direct = enrich_system_prompt_with_talents(
        base,
        (TalentChoice("agent-A", 10, "security-first", T0),),
        guild=Guild.backend,
    )
    assert enriched == direct


@pytest.mark.asyncio
async def test_enricher_guild_kwarg_is_keyword_only():
    """The closure's ``guild`` parameter is keyword-only per docstring."""
    store = InMemoryTalentChoiceStore()
    enrich = build_talent_prompt_enricher(store)

    sig = inspect.signature(enrich)
    assert (
        sig.parameters["guild"].kind is inspect.Parameter.KEYWORD_ONLY
    )
