"""Property tests for ``backend.routers.preferences`` public API contracts."""
from __future__ import annotations

import asyncio
import json
import string
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend import auth
from backend.routers import preferences as prefs


SAFE_KEY = st.text(
    alphabet=string.ascii_letters + string.digits + "-_.:",
    min_size=1,
    max_size=80,
)
SAFE_VALUE = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=2048,
)
PREF_VALUE_EDGES = st.one_of(
    st.none(),
    SAFE_VALUE,
    st.just("x" * 65_537),
)
PANEL_STATE = st.sampled_from(["normal", "minimized", "maximized"])
PANEL = st.builds(
    prefs.WarRoomPanel,
    id=SAFE_KEY,
    x=st.integers(min_value=0, max_value=100_000),
    y=st.integers(min_value=0, max_value=100_000),
    width=st.integers(min_value=120, max_value=100_000),
    height=st.integers(min_value=80, max_value=100_000),
    state=PANEL_STATE,
)


def _run(coro):
    return asyncio.run(coro)


def _user() -> auth.User:
    return auth.User(
        id="property-user",
        email="property-user@example.test",
        name="Property User",
        role="admin",
        tenant_id="t-default",
    )


def _patch_preference_writes(monkeypatch) -> list[tuple[str, str, str]]:
    writes: list[tuple[str, str, str]] = []
    emits: list[tuple[str, str, str]] = []

    async def _upsert(user_id: str, key: str, value: str) -> None:
        writes.append((user_id, key, value))

    def _emit(key: str, value: str, user_id: str) -> None:
        emits.append((user_id, key, value))

    monkeypatch.setattr(prefs, "_upsert_preference", _upsert)
    monkeypatch.setattr(prefs, "_emit_preference_updated", _emit)
    return writes


@settings(max_examples=75, deadline=None)
@given(value=PREF_VALUE_EDGES)
def test_pref_body_property_enforces_public_value_contract(value: str | None) -> None:
    if isinstance(value, str) and len(value) <= 65_536:
        body = prefs.PrefBody(value=value)
        assert body.value == value
        assert isinstance(body.value, str)
    else:
        with pytest.raises(ValidationError):
            prefs.PrefBody(value=value)


@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(key=SAFE_KEY, value=SAFE_VALUE)
def test_set_preference_property_round_trips_key_value(
    key: str,
    value: str,
    monkeypatch,
) -> None:
    writes = _patch_preference_writes(monkeypatch)

    response = _run(
        prefs.set_preference(
            key,
            prefs.PrefBody(value=value),
            user=_user(),
        )
    )

    assert response == {"key": key, "value": value}
    assert writes == [("property-user", key, value)]


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(action=st.sampled_from(["skip", "replay"]))
def test_tour_seen_actions_property_are_idempotent(
    action: str,
    monkeypatch,
) -> None:
    writes = _patch_preference_writes(monkeypatch)
    handler = prefs.skip_tour if action == "skip" else prefs.replay_tour
    expected_value = prefs.PREF_TRUE_VALUE if action == "skip" else prefs.PREF_FALSE_VALUE

    first = _run(handler(user=_user()))
    second = _run(handler(user=_user()))

    assert first == second == {
        "key": prefs.TOUR_SEEN_PREF_KEY,
        "value": expected_value,
    }
    assert writes == [
        ("property-user", prefs.TOUR_SEEN_PREF_KEY, expected_value),
        ("property-user", prefs.TOUR_SEEN_PREF_KEY, expected_value),
    ]


@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(value=st.one_of(st.none(), SAFE_VALUE))
def test_onboarding_state_property_treats_only_literal_one_as_seen(
    value: str | None,
    monkeypatch,
) -> None:
    async def _get(user_id: str, key: str) -> str | None:
        assert user_id == "property-user"
        assert key in {
            prefs.SEEN_MP_TOUR_PREF_KEY,
            prefs.SEEN_RPG_TOUR_PREF_KEY,
            prefs.SEEN_RPG_CHARACTER_CARD_TOUR_PREF_KEY,
        }
        return value

    monkeypatch.setattr(prefs, "_get_preference_value", _get)

    mp = _run(prefs.get_multi_provider_onboarding_tour_state(user=_user()))
    rpg = _run(prefs.get_rpg_tour_state(user=_user()))
    card = _run(prefs.get_rpg_character_card_tour_state(user=_user()))

    assert mp == {"key": prefs.SEEN_MP_TOUR_PREF_KEY, "seen": value == "1"}
    assert rpg == {"key": prefs.SEEN_RPG_TOUR_PREF_KEY, "seen": value == "1"}
    assert card == {
        "key": prefs.SEEN_RPG_CHARACTER_CARD_TOUR_PREF_KEY,
        "seen": value == "1",
        "steps": list(prefs.RPG_CHARACTER_CARD_TOUR_STEPS),
    }


@settings(max_examples=75, deadline=None)
@given(panels=st.lists(PANEL, max_size=8, unique_by=lambda panel: panel.id))
def test_war_room_panel_layout_property_accepts_unique_panels(
    panels: list[prefs.WarRoomPanel],
) -> None:
    connections = [
        prefs.WarRoomPanelConnection(
            source=panels[index].id,
            target=panels[index + 1].id,
            kind="related",
        )
        for index in range(max(len(panels) - 1, 0))
    ]

    layout = prefs.WarRoomPanelLayoutBody(
        panels=panels,
        connections=connections,
    )

    assert layout.version == 1
    assert layout.panels == panels
    assert layout.connections == connections


@settings(max_examples=75, deadline=None)
@given(panel=PANEL)
def test_war_room_panel_layout_property_rejects_duplicate_panel_ids(
    panel: prefs.WarRoomPanel,
) -> None:
    with pytest.raises(ValidationError):
        prefs.WarRoomPanelLayoutBody(panels=[panel, panel])


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    body=st.builds(
        prefs.WarRoomPanelLayoutBody,
        panels=st.lists(PANEL, max_size=5, unique_by=lambda panel: panel.id),
    )
)
def test_set_war_room_panel_layout_property_persists_canonical_json(
    body: prefs.WarRoomPanelLayoutBody,
    monkeypatch,
) -> None:
    writes = _patch_preference_writes(monkeypatch)

    response = _run(
        prefs.set_multi_provider_war_room_panel_layout(
            body,
            user=_user(),
        )
    )

    assert response == {
        "key": prefs.MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY,
        "value": body,
    }
    assert writes == [
        (
            "property-user",
            prefs.MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY,
            json.dumps(body.model_dump(), separators=(",", ":"), sort_keys=True),
        )
    ]


@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(stored=st.one_of(st.none(), SAFE_VALUE))
def test_get_war_room_panel_layout_property_falls_back_for_empty_or_invalid(
    stored: str | None,
    monkeypatch,
) -> None:
    async def _get(user_id: str, key: str) -> str | None:
        assert user_id == "property-user"
        assert key == prefs.MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY
        return stored

    monkeypatch.setattr(prefs, "_get_preference_value", _get)

    response = _run(prefs.get_multi_provider_war_room_panel_layout(user=_user()))

    assert response["key"] == prefs.MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY
    assert response["value"] == prefs.WarRoomPanelLayoutBody()
