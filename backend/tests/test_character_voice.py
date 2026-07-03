"""RPG.W15 — character voice/persona field.

Covers:
* built-ins carry a non-empty voice (code-authoritative);
* the DB loader reads ``voice`` for recruited characters;
* voice divergence on a built-in slug is IGNORED (voice is excluded from
  _SHADOW_COMPARE_FIELDS — code wins, no drift rejection);
* migration 0255's built-in VOICES are an exact copy of the code roster
  (drift guard, mirroring the 0253/0254 seed guards);
* the router's ``_character_voice`` helper resolves a voice and fail-opens.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from backend.agents import character_registry as cr

_M255 = Path(__file__).resolve().parents[2] / (
    "backend/alembic/versions/0255_character_voice.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("m0255", _M255)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_builtins_have_voice() -> None:
    for slug, char in cr.CHARACTERS.items():
        assert char.voice.strip(), f"built-in {slug} has empty voice"
        assert "口頭禪" in char.voice, f"built-in {slug} voice lacks a catchphrase"


def test_db_loader_reads_recruit_voice() -> None:
    row = {
        "slug": "blaze",
        "display_name": "Blaze",
        "brain": "subscription-claude",
        "guild": "backend",
        "max_tier": "M",
        "blurb": "Recruited hand.",
        "voice": "衝勁十足的新人。口頭禪:「交給我試試！」",
        "active": True,
    }
    merged = cr._merge_db_rows([row])
    assert merged["blaze"].voice == row["voice"]


def test_builtin_voice_divergence_ignored() -> None:
    # A DB row for a built-in slug with a DIFFERENT voice must NOT override the
    # code constant, and (unlike a brain/guild divergence) must NOT be treated
    # as drift — voice is intentionally out of _SHADOW_COMPARE_FIELDS.
    assert "voice" not in cr._SHADOW_COMPARE_FIELDS
    nova = cr.CHARACTERS["nova"]
    row = {
        "slug": "nova",
        "display_name": nova.display_name,
        "brain": nova.brain,
        "guild": nova.guild,
        "max_tier": nova.max_tier,
        "blurb": nova.blurb,
        "voice": "完全不同的語氣",
        "active": nova.active,
    }
    merged = cr._merge_db_rows([row])
    assert merged["nova"].voice == nova.voice  # code wins


def test_migration_0255_builtin_voices_match_code() -> None:
    m = _load_migration()
    voices = dict(m.VOICES)
    for slug, char in cr.CHARACTERS.items():
        assert voices[slug] == char.voice, (
            f"0255 VOICES[{slug}] drifted from the registry constant"
        )
    # every roster slug (built-ins + the 4 recruits) has a seeded voice
    assert set(voices) == {
        "nova", "pixel", "sage", "rex", "iris", "argus", "kai", "vega",
    }


def test_router_character_voice_helper() -> None:
    from backend.routers import agents as router
    assert router._character_voice("nova") == cr.CHARACTERS["nova"].voice
    # unknown slug → fail-open None (never raises)
    assert router._character_voice("does-not-exist-xyz") is None
