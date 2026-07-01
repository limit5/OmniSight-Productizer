"""RECRUIT C3 (OP-2510) — contract tests for the /agents/characters REST API.

Covers the recruit/retire surface defined in ``backend/routers/agents.py``:

* ``GET /agents/characters`` — built-ins + DB rows merged (shadow-protected
  built-ins), ``include_retired`` filter.
* ``POST /agents/characters`` — recruit: CharacterDef-equivalent validation,
  409 on built-in/existing-slug collision, Lv1/xp0 card creation through the
  idempotent ``ensure_card_for_first_task`` upsert.
* ``PATCH /agents/characters/{slug}`` — display_name/blurb/max_tier/active
  only; brain/guild/slug immutable (400); built-ins not patchable (403);
  retire = ``active=false`` (never DELETE).

The routes are mounted on a local FastAPI app (hermetic, no full app boot).
DI seams ``get_character_def_store`` / ``get_character_card_registry`` are
overridden with in-memory fakes so no Postgres is required.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.character_card import (
    CharacterCardCreate,
    CharacterCardRegistry,
    InMemoryCharacterCardStore,
)
from backend.agents.character_registry import CHARACTERS
from backend.routers.agents import (
    get_character_card_registry,
    get_character_def_store,
    router,
)

BUILT_IN_SLUGS = frozenset(CHARACTERS)

RECRUIT_BODY = {
    "slug": "vulcan",
    "display_name": "Vulcan",
    "brain": "subscription-claude",
    "guild": "sre",
    "max_tier": "M",
    "blurb": "Forge-tempered ops specialist.",
}


class InMemoryCharacterDefStore:
    """Fake of ``PostgresCharacterDefStore`` — same method contracts."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def list_defs(self) -> list[dict[str, Any]]:
        return [dict(row) for _slug, row in sorted(self.rows.items())]

    async def insert_def(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if row["slug"] in self.rows:
            return None
        stored = {**row, "active": True}
        self.rows[row["slug"]] = stored
        return dict(stored)

    async def update_def(
        self, slug: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        row = self.rows.get(slug)
        if row is None:
            return None
        row.update(values)
        return dict(row)


@pytest.fixture
def def_store() -> InMemoryCharacterDefStore:
    return InMemoryCharacterDefStore()


@pytest.fixture
def card_store() -> InMemoryCharacterCardStore:
    return InMemoryCharacterCardStore()


@pytest.fixture
def client(
    def_store: InMemoryCharacterDefStore,
    card_store: InMemoryCharacterCardStore,
) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_character_def_store] = lambda: def_store
    app.dependency_overrides[get_character_card_registry] = lambda: (
        CharacterCardRegistry(card_store)
    )
    return TestClient(app)


class TestListCharacters:
    def test_lists_built_ins_with_flag(self, client: TestClient):
        resp = client.get("/agents/characters")
        assert resp.status_code == 200
        body = resp.json()
        by_slug = {d["slug"]: d for d in body}
        assert set(by_slug) == BUILT_IN_SLUGS
        for d in body:
            assert d["built_in"] is True
            assert d["active"] is True
            assert set(d) == {
                "slug",
                "display_name",
                "brain",
                "guild",
                "max_tier",
                "blurb",
                "active",
                "built_in",
            }
        assert by_slug["nova"]["brain"] == "subscription-claude"
        assert by_slug["nova"]["max_tier"] == "L"

    def test_not_swallowed_by_agent_id_catch_all(self, client: TestClient):
        # ⚠ route-order regression guard: /{agent_id} is declared after
        # /characters; if the order flips this returns 404 "Agent not found".
        resp = client.get("/agents/characters")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_db_rows_merged_after_built_ins(
        self, client: TestClient, def_store: InMemoryCharacterDefStore
    ):
        await def_store.insert_def(
            {
                "slug": "vulcan",
                "display_name": "Vulcan",
                "brain": "subscription-grok",
                "guild": "sre",
                "max_tier": "S",
                "blurb": "",
            }
        )
        body = client.get("/agents/characters").json()
        by_slug = {d["slug"]: d for d in body}
        assert set(by_slug) == BUILT_IN_SLUGS | {"vulcan"}
        assert by_slug["vulcan"]["built_in"] is False

    async def test_built_in_slug_shadow_protected_against_db_row(
        self, client: TestClient, def_store: InMemoryCharacterDefStore
    ):
        # A divergent DB row for a built-in slug must never override the
        # code constant (C2 shadow-protection policy, mirrored by the API).
        def_store.rows["nova"] = {
            "slug": "nova",
            "display_name": "Impostor",
            "brain": "subscription-grok",
            "guild": "sre",
            "max_tier": "S",
            "blurb": "not the real nova",
            "active": False,
        }
        body = client.get("/agents/characters?include_retired=true").json()
        novas = [d for d in body if d["slug"] == "nova"]
        assert len(novas) == 1
        assert novas[0]["display_name"] == "Nova"
        assert novas[0]["brain"] == "subscription-claude"
        assert novas[0]["built_in"] is True

    def test_retired_hidden_by_default_shown_with_flag(self, client: TestClient):
        assert client.post("/agents/characters", json=RECRUIT_BODY).status_code == 201
        retire = client.patch(
            "/agents/characters/vulcan", json={"active": False}
        )
        assert retire.status_code == 200

        default = {d["slug"] for d in client.get("/agents/characters").json()}
        assert "vulcan" not in default
        assert default == BUILT_IN_SLUGS

        included = {
            d["slug"]
            for d in client.get(
                "/agents/characters?include_retired=true"
            ).json()
        }
        assert "vulcan" in included


class TestRecruit:
    def test_recruit_ok_201_with_definition(self, client: TestClient):
        resp = client.post("/agents/characters", json=RECRUIT_BODY)
        assert resp.status_code == 201
        assert resp.json() == {
            "slug": "vulcan",
            "display_name": "Vulcan",
            "brain": "subscription-claude",
            "guild": "sre",
            "max_tier": "M",
            "blurb": "Forge-tempered ops specialist.",
            "active": True,
            "built_in": False,
        }

    def test_recruit_appears_in_get_characters(self, client: TestClient):
        client.post("/agents/characters", json=RECRUIT_BODY)
        body = client.get("/agents/characters").json()
        vulcan = next(d for d in body if d["slug"] == "vulcan")
        assert vulcan["active"] is True
        assert vulcan["built_in"] is False

    async def test_recruit_creates_lv1_xp0_card_in_chosen_guild(
        self, client: TestClient, card_store: InMemoryCharacterCardStore
    ):
        client.post("/agents/characters", json=RECRUIT_BODY)
        card = await card_store.get_card("vulcan")
        assert card is not None
        assert card.level == 1
        assert card.xp == 0
        assert card.guild == "sre"
        assert card.agent_class == "subscription-claude"

    def test_recruit_appears_in_get_agents_cards(self, client: TestClient):
        # Integration AC: the card row shows up in GET /agents/cards
        # immediately (no restart).
        client.post("/agents/characters", json=RECRUIT_BODY)
        cards = client.get("/agents/cards").json()
        vulcan = next(c for c in cards if c["agent_id"] == "vulcan")
        assert vulcan["level"] == 1
        assert vulcan["xp"] == 0
        assert vulcan["guild"] == "sre"

    async def test_recruit_never_clobbers_existing_card_progression(
        self, client: TestClient, card_store: InMemoryCharacterCardStore
    ):
        # ensure_card_for_first_task is an ON-CONFLICT-keep upsert: a card
        # that already accrued XP under this slug survives the recruit.
        await card_store.create_card(
            CharacterCardCreate(
                agent_id="vulcan",
                agent_class="subscription-claude",
                guild="backend",
                level=5,
                xp=1200,
            )
        )
        resp = client.post("/agents/characters", json=RECRUIT_BODY)
        assert resp.status_code == 201
        card = await card_store.get_card("vulcan")
        assert card.level == 5
        assert card.xp == 1200

    def test_duplicate_slug_409(self, client: TestClient):
        assert client.post("/agents/characters", json=RECRUIT_BODY).status_code == 201
        resp = client.post("/agents/characters", json=RECRUIT_BODY)
        assert resp.status_code == 409
        assert "vulcan" in resp.json()["detail"]

    def test_built_in_collision_409(self, client: TestClient):
        resp = client.post(
            "/agents/characters", json={**RECRUIT_BODY, "slug": "nova"}
        )
        assert resp.status_code == 409
        assert "built-in" in resp.json()["detail"]

    async def test_built_in_collision_writes_nothing(
        self,
        client: TestClient,
        def_store: InMemoryCharacterDefStore,
        card_store: InMemoryCharacterCardStore,
    ):
        client.post("/agents/characters", json={**RECRUIT_BODY, "slug": "nova"})
        assert def_store.rows == {}
        assert await card_store.get_card("nova") is None

    @pytest.mark.parametrize(
        "slug", ["Vulcan", "9lives", "-dash", "has space", "has_underscore", ""]
    )
    def test_invalid_slug_400(self, client: TestClient, slug: str):
        resp = client.post(
            "/agents/characters", json={**RECRUIT_BODY, "slug": slug}
        )
        assert resp.status_code == 400
        assert "slug" in resp.json()["detail"]

    def test_unknown_brain_400(self, client: TestClient):
        resp = client.post(
            "/agents/characters", json={**RECRUIT_BODY, "brain": "skynet"}
        )
        assert resp.status_code == 400
        assert "brain" in resp.json()["detail"]

    def test_unknown_guild_400(self, client: TestClient):
        resp = client.post(
            "/agents/characters", json={**RECRUIT_BODY, "guild": "pirates"}
        )
        assert resp.status_code == 400
        assert "guild" in resp.json()["detail"]

    @pytest.mark.parametrize("max_tier", ["XL", "s", "", None])
    def test_invalid_max_tier_400(self, client: TestClient, max_tier):
        resp = client.post(
            "/agents/characters", json={**RECRUIT_BODY, "max_tier": max_tier}
        )
        assert resp.status_code == 400
        assert "max_tier" in resp.json()["detail"]

    def test_missing_display_name_400(self, client: TestClient):
        body = {k: v for k, v in RECRUIT_BODY.items() if k != "display_name"}
        resp = client.post("/agents/characters", json=body)
        assert resp.status_code == 400
        assert "display_name" in resp.json()["detail"]

    async def test_failed_validation_creates_no_card(
        self, client: TestClient, card_store: InMemoryCharacterCardStore
    ):
        client.post(
            "/agents/characters", json={**RECRUIT_BODY, "max_tier": "XL"}
        )
        assert await card_store.get_card("vulcan") is None


class TestPatchCharacter:
    def _recruit(self, client: TestClient) -> None:
        assert client.post("/agents/characters", json=RECRUIT_BODY).status_code == 201

    def test_patch_mutable_fields_ok(self, client: TestClient):
        self._recruit(client)
        resp = client.patch(
            "/agents/characters/vulcan",
            json={
                "display_name": "Vulcan Prime",
                "blurb": "Rebranded.",
                "max_tier": "L",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["display_name"] == "Vulcan Prime"
        assert body["blurb"] == "Rebranded."
        assert body["max_tier"] == "L"
        assert body["active"] is True
        assert body["built_in"] is False

    @pytest.mark.parametrize("field", ["brain", "guild", "slug"])
    def test_immutable_field_400(self, client: TestClient, field: str):
        self._recruit(client)
        resp = client.patch(
            "/agents/characters/vulcan", json={field: "anything"}
        )
        assert resp.status_code == 400
        assert "immutable" in resp.json()["detail"]

    def test_built_in_patch_403(self, client: TestClient):
        resp = client.patch(
            "/agents/characters/nova", json={"display_name": "Supernova"}
        )
        assert resp.status_code == 403
        assert "built-in" in resp.json()["detail"]

    def test_retire_flips_active_and_reactivate(self, client: TestClient):
        self._recruit(client)
        retired = client.patch(
            "/agents/characters/vulcan", json={"active": False}
        )
        assert retired.status_code == 200
        assert retired.json()["active"] is False

        rehired = client.patch(
            "/agents/characters/vulcan", json={"active": True}
        )
        assert rehired.status_code == 200
        assert rehired.json()["active"] is True

    async def test_retire_keeps_definition_row_and_card(
        self,
        client: TestClient,
        def_store: InMemoryCharacterDefStore,
        card_store: InMemoryCharacterCardStore,
    ):
        # Retire is never a DELETE — the definition row and the card/XP
        # history must both survive.
        self._recruit(client)
        client.patch("/agents/characters/vulcan", json={"active": False})
        assert "vulcan" in def_store.rows
        assert await card_store.get_card("vulcan") is not None

    def test_unknown_slug_404(self, client: TestClient):
        resp = client.patch(
            "/agents/characters/ghost", json={"display_name": "Ghost"}
        )
        assert resp.status_code == 404

    def test_unknown_field_400(self, client: TestClient):
        self._recruit(client)
        resp = client.patch(
            "/agents/characters/vulcan", json={"level": 99}
        )
        assert resp.status_code == 400
        assert "unknown field" in resp.json()["detail"]

    def test_empty_patch_400(self, client: TestClient):
        self._recruit(client)
        resp = client.patch("/agents/characters/vulcan", json={})
        assert resp.status_code == 400

    def test_invalid_patch_tier_400(self, client: TestClient):
        self._recruit(client)
        resp = client.patch(
            "/agents/characters/vulcan", json={"max_tier": "XXL"}
        )
        assert resp.status_code == 400

    def test_no_delete_route(self, client: TestClient):
        # MUST NOT: DELETE rows. /agents/characters/{slug} exposes no DELETE
        # (405 method-not-allowed proves the PATCH route exists but DELETE
        # deliberately does not — /agents/{agent_id} DELETE is a different,
        # single-segment path and cannot match this two-segment one).
        self._recruit(client)
        resp = client.delete("/agents/characters/vulcan")
        assert resp.status_code == 405
