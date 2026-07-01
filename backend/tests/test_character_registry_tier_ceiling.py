"""RPG un-weld (b) — pickup-side character tier-ceiling enforcement."""
from __future__ import annotations

from backend.agents import character_registry as cr


def test_over_ceiling_character_ticket_is_denied():
    # rex's max_tier is S; an L ticket exceeds it.
    reason = cr.character_tier_denial_from_labels(["character:rex", "tier:L"])
    assert reason is not None
    assert "rex" in reason and "L" in reason and "S" in reason


def test_within_ceiling_character_ticket_is_allowed():
    assert cr.character_tier_denial_from_labels(["character:rex", "tier:S"]) is None
    assert cr.character_tier_denial_from_labels(["character:nova", "tier:L"]) is None
    assert cr.character_tier_denial_from_labels(["character:nova", "tier:M"]) is None


def test_no_character_label_is_not_enforced():
    assert cr.character_tier_denial_from_labels(["class:subscription-claude", "tier:X"]) is None


def test_unknown_character_fails_open():
    # A typo'd character can't gate pickup — the bare class: label still routes it.
    assert cr.character_tier_denial_from_labels(["character:ghost", "tier:X"]) is None


def test_missing_or_unknown_tier_is_not_enforced():
    assert cr.character_tier_denial_from_labels(["character:rex"]) is None
    assert cr.character_tier_denial_from_labels(["character:rex", "tier:Z"]) is None


def test_every_roster_character_at_its_own_ceiling_is_allowed():
    for slug, c in cr.CHARACTERS.items():
        assert cr.character_tier_denial_from_labels(
            [f"character:{slug}", f"tier:{c.max_tier}"]
        ) is None
