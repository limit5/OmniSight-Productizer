"""RPG char-XP atomic — _award_task_xp uses the atomic store award + fires level-up."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from backend.agents import character_card as cc
from backend.agents.character_card import CharacterCard, PostgresCharacterCardStore


def _card(level: int, xp: int) -> CharacterCard:
    return CharacterCard(
        agent_id="nova",
        agent_class="subscription-claude",
        instance_suffix="nova",
        guild="backend",
        level=level,
        xp=xp,
        specialization_label="",
        style_fingerprint="",
        created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )


def _run(monkeypatch, atomic_return, capture):
    async def _fake_atomic(self, agent_id, delta_xp):
        return atomic_return

    monkeypatch.setattr(PostgresCharacterCardStore, "award_xp_atomic", _fake_atomic)
    monkeypatch.setattr(cc, "_emit_level_up_safely",
                        lambda prev, upd: capture.append((prev.level, upd.level)))

    async def _factory():  # unused fake conn factory
        yield None

    return asyncio.run(
        cc._award_task_xp(_factory, agent_id="nova",
                          outcome_status="success", tier="M", base_xp=100)
    )


def test_none_when_card_absent(monkeypatch):
    captured = []
    out = _run(monkeypatch, None, captured)
    assert out is None
    assert captured == []


def test_award_returns_delta_and_card_no_levelup(monkeypatch):
    captured = []
    updated = _card(level=2, xp=300)
    out = _run(monkeypatch, (updated, 2), captured)  # previous_level == updated.level
    assert out is not None
    delta, card = out
    assert delta.xp == 100 and card.xp == 300 and card.level == 2
    assert captured == []  # no level increase → no event


def test_levelup_event_fires_on_increase(monkeypatch):
    captured = []
    updated = _card(level=3, xp=500)
    _run(monkeypatch, (updated, 2), captured)  # previous_level 2 < new 3
    assert captured == [(2, 3)]  # _emit_level_up_safely(prev.level=2, upd.level=3)
