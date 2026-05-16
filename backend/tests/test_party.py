"""RPG.W17 -- contract tests for ``backend/agents/party.py`` (OP-220).

Covers the 11 AC cases listed on OP-220 §"Test plan":

 1. create-party happy at 2 / 3 / 4 / 5 members
 2. refuse < MIN_PARTY_SIZE
 3. refuse > MAX_PARTY_SIZE
 4. synergy from matrix lookup (backend × frontend → fullstack +15%)
 5. task exclusive — refuse 2nd
 6. member gated from individual tasks while party holds active task
 7. party XP distribution + personal accrual
 8. missing-synergy degrades to base XP + no bonus
 9. Tier L+ task accepts party (assign_task happy path)
10. party_hall UI renders — see test/components/party-hall.test.tsx
11. idempotent re-pickup (assign same task again is a no-op)

The frontend slice (#10) is exercised in
``test/components/party-hall.test.tsx``; this file covers the backend
contract.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.agents.party import (
    InMemoryPartyStore,
    MAX_PARTY_SIZE,
    MemberAlreadyInParty,
    PartyActiveTaskExists,
    PartyError,
    PartySizeInvalid,
    Party,
    PartyMember,
    PartyState,
    assign_task,
    compute_party_xp_distribution,
    create_party,
    list_active_parties,
    member_is_gated,
    release_task,
    task_complete,
)
from backend.agents.synergy_registry import (
    SynergyComputeFailed,
    load_synergy_matrix,
    synergy_for_members,
    synergy_for_pair,
)


T0 = datetime(2026, 5, 11, tzinfo=timezone.utc)


def _guilds_for(members: list[str], guild: str = "backend") -> dict[str, str]:
    return {member: guild for member in members}


# ── Synergy matrix loader + matrix-lookup contract ──────────────────


def test_synergy_matrix_yaml_parses_and_has_three_must_entries():
    matrix = load_synergy_matrix()
    pairs = {entry.label for entry in matrix.values()}
    assert "fullstack" in pairs       # backend × frontend (+15%)
    assert "hardening" in pairs       # security × devops (+10% security skill)
    assert "pipeline" in pairs        # data × backend (+10% data skill)


def test_synergy_matrix_has_at_least_fifteen_entries():
    matrix = load_synergy_matrix()
    assert len(matrix) >= 15, (
        f"AC #3 requires ~15 cross-Guild combinations; matrix has {len(matrix)}"
    )


def test_synergy_for_pair_is_order_insensitive():
    forward = synergy_for_pair("backend", "frontend")
    reverse = synergy_for_pair("frontend", "backend")
    assert forward is not None
    assert reverse is not None
    assert forward.label == reverse.label == "fullstack"


def test_synergy_for_pair_returns_none_for_same_guild():
    assert synergy_for_pair("backend", "backend") is None


def test_synergy_for_pair_normalises_case_and_whitespace():
    entry = synergy_for_pair("  BACKEND ", " Frontend ")
    assert entry is not None
    assert entry.label == "fullstack"


def test_synergy_for_pair_returns_none_for_invalid_slug_type():
    assert synergy_for_pair("backend", 123) is None  # type: ignore[arg-type]


def test_synergy_for_members_selects_highest_xp_bonus_candidate():
    entry = synergy_for_members(["backend", "frontend", "security"])
    assert entry is not None
    assert entry.label == "fullstack"
    assert entry.xp_bonus == pytest.approx(0.15)


def test_synergy_for_members_tie_breaks_by_skill_bonus_then_label(tmp_path):
    matrix_path = tmp_path / "synergy.yaml"
    matrix_path.write_text(
        """
schema_version: 1
synergies:
  - guilds: [backend, frontend]
    label: alpha
    display_name: Alpha
    xp_bonus: 0.10
    skill_bonus_target: data
    skill_bonus: 0.05
    summary: Alpha entry.
  - guilds: [backend, security]
    label: beta
    display_name: Beta
    xp_bonus: 0.10
    skill_bonus_target: data
    skill_bonus: 0.20
    summary: Beta entry.
  - guilds: [frontend, security]
    label: aardvark
    display_name: Aardvark
    xp_bonus: 0.10
    skill_bonus_target: data
    skill_bonus: 0.20
    summary: Aardvark entry.
""".lstrip(),
        encoding="utf-8",
    )

    entry = synergy_for_members(
        ["backend", "frontend", "security"], path=matrix_path
    )
    assert entry is not None
    assert entry.label == "aardvark"


def test_load_synergy_matrix_rejects_duplicate_label(tmp_path):
    matrix_path = tmp_path / "synergy.yaml"
    matrix_path.write_text(
        """
schema_version: 1
synergies:
  - guilds: [backend, frontend]
    label: duplicate
    display_name: One
    xp_bonus: 0.10
    skill_bonus_target: null
    skill_bonus: null
    summary: One.
  - guilds: [backend, security]
    label: duplicate
    display_name: Two
    xp_bonus: 0.10
    skill_bonus_target: null
    skill_bonus: null
    summary: Two.
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SynergyComputeFailed, match="duplicate label"):
        load_synergy_matrix(matrix_path)


def test_load_synergy_matrix_rejects_duplicate_pair_in_reverse_order(tmp_path):
    matrix_path = tmp_path / "synergy.yaml"
    matrix_path.write_text(
        """
schema_version: 1
synergies:
  - guilds: [backend, frontend]
    label: first
    display_name: First
    xp_bonus: 0.10
    skill_bonus_target: null
    skill_bonus: null
    summary: First.
  - guilds: [frontend, backend]
    label: second
    display_name: Second
    xp_bonus: 0.20
    skill_bonus_target: null
    skill_bonus: null
    summary: Second.
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(SynergyComputeFailed, match="duplicate Guild pair"):
        load_synergy_matrix(matrix_path)


# ── create_party — happy path, 2 / 3 / 4 / 5 members ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [2, 3, 4, 5])
async def test_create_party_happy_path_2_3_4_5_members(size):
    store = InMemoryPartyStore()
    members = [f"agent-{i}" for i in range(size)]
    party = await create_party(
        store,
        name=f"party-{size}",
        member_agent_ids=members,
        member_guilds=_guilds_for(members, guild="backend"),
        now=T0,
    )
    assert party.state.party_id.startswith("party-")
    assert len(party.members) == size
    assert {m.member_agent_id for m in party.members} == set(members)
    assert party.state.active_task_id is None


# ── create_party — refuse < MIN_PARTY_SIZE / > MAX_PARTY_SIZE ─────


@pytest.mark.asyncio
async def test_create_party_refuses_below_min_size():
    store = InMemoryPartyStore()
    with pytest.raises(PartySizeInvalid):
        await create_party(
            store,
            name="solo",
            member_agent_ids=["agent-A"],
            member_guilds=_guilds_for(["agent-A"]),
        )


@pytest.mark.asyncio
async def test_create_party_refuses_above_max_size():
    store = InMemoryPartyStore()
    members = [f"agent-{i}" for i in range(MAX_PARTY_SIZE + 1)]
    with pytest.raises(PartySizeInvalid):
        await create_party(
            store,
            name="too-big",
            member_agent_ids=members,
            member_guilds=_guilds_for(members),
        )


# ── create_party — synergy lookup hits matrix on cross-Guild pair ──


@pytest.mark.asyncio
async def test_create_party_resolves_fullstack_synergy_from_matrix():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="be-fe pair",
        member_agent_ids=["agent-backend", "agent-frontend"],
        member_guilds={
            "agent-backend": "backend",
            "agent-frontend": "frontend",
        },
        now=T0,
    )
    assert party.state.synergy_label == "fullstack"
    assert party.state.synergy_xp_bonus == pytest.approx(0.15)
    assert party.synergy is not None
    assert party.synergy.guilds == ("backend", "frontend")


@pytest.mark.asyncio
async def test_create_party_no_synergy_for_uniform_guild_party():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="all backend",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"], guild="backend"),
        now=T0,
    )
    assert party.state.synergy_label is None
    assert party.state.synergy_xp_bonus == 0.0


# ── create_party — refuse member already in active party ────────────


@pytest.mark.asyncio
async def test_create_party_refuses_member_already_in_active_party():
    store = InMemoryPartyStore()
    await create_party(
        store,
        name="first",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"]),
    )
    with pytest.raises(MemberAlreadyInParty):
        await create_party(
            store,
            name="second-stealing-A",
            member_agent_ids=["agent-A", "agent-C"],
            member_guilds=_guilds_for(["agent-A", "agent-C"]),
        )


# ── assign_task — exclusive (refuse 2nd) ───────────────────────────


@pytest.mark.asyncio
async def test_assign_task_refuses_second_concurrent_task():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="A",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"]),
        now=T0,
    )
    await assign_task(store, party.state.party_id, "OP-T1", now=T0)
    with pytest.raises(PartyActiveTaskExists):
        await assign_task(store, party.state.party_id, "OP-T2", now=T0)


@pytest.mark.asyncio
async def test_assign_task_allows_new_task_after_completion_releases_old_one():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="A",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"]),
        now=T0,
    )
    await assign_task(store, party.state.party_id, "OP-T1", now=T0)
    await task_complete(store, party.state.party_id, total_xp=100, now=T0)

    state = await assign_task(store, party.state.party_id, "OP-T2", now=T0)
    assert state.active_task_id == "OP-T2"


@pytest.mark.asyncio
async def test_assign_task_rejects_missing_party():
    store = InMemoryPartyStore()
    with pytest.raises(PartyError, match="does not exist"):
        await assign_task(store, "party-missing", "OP-T1", now=T0)


@pytest.mark.asyncio
async def test_assign_task_rejects_disbanded_party():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="A",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"]),
        now=T0,
    )
    await store.upsert_state(
        PartyState(
            party_id=party.state.party_id,
            name=party.state.name,
            synergy_label=party.state.synergy_label,
            synergy_xp_bonus=party.state.synergy_xp_bonus,
            active_task_id=None,
            active_task_assigned_at=None,
            created_at=party.state.created_at,
            updated_at=T0,
            disbanded_at=T0,
        )
    )

    with pytest.raises(PartyError, match="is disbanded"):
        await assign_task(store, party.state.party_id, "OP-T1", now=T0)


# ── assign_task — idempotent re-pickup (#11) ───────────────────────


@pytest.mark.asyncio
async def test_assign_task_is_idempotent_on_same_task_id():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="A",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"]),
        now=T0,
    )
    first = await assign_task(store, party.state.party_id, "OP-T1", now=T0)
    second = await assign_task(store, party.state.party_id, "OP-T1", now=T0)
    assert first.active_task_id == second.active_task_id == "OP-T1"
    assert first.active_task_assigned_at == second.active_task_assigned_at


# ── assign_task — Tier L+ accept (happy path of #9) ────────────────


@pytest.mark.asyncio
async def test_assign_task_accepts_tier_l_plus_task():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="party-L",
        member_agent_ids=["agent-A", "agent-B", "agent-C"],
        member_guilds=_guilds_for(["agent-A", "agent-B", "agent-C"]),
        now=T0,
    )
    state = await assign_task(store, party.state.party_id, "OP-220", now=T0)
    assert state.active_task_id == "OP-220"
    assert state.active_task_assigned_at is not None


# ── member_is_gated / pre-pickup ───────────────────────────────────


@pytest.mark.asyncio
async def test_member_is_gated_while_party_holds_active_task():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="A",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"]),
        now=T0,
    )
    # Before assignment, members are free.
    assert await member_is_gated(store, "agent-A") is None
    await assign_task(store, party.state.party_id, "OP-T1", now=T0)
    # After assignment, members are gated.
    gating = await member_is_gated(store, "agent-A")
    assert gating is not None
    assert gating.party_id == party.state.party_id
    assert gating.active_task_id == "OP-T1"


@pytest.mark.asyncio
async def test_member_is_ungated_after_task_complete():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="A",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"]),
        now=T0,
    )
    await assign_task(store, party.state.party_id, "OP-T1", now=T0)
    await task_complete(store, party.state.party_id, total_xp=200, now=T0)
    assert await member_is_gated(store, "agent-A") is None


# ── compute_party_xp_distribution — even split + personal accrual ──


def test_compute_party_xp_distribution_even_split_plus_personal():
    state = PartyState(
        party_id="p1",
        name="X",
        synergy_label="fullstack",
        synergy_xp_bonus=0.15,
        active_task_id="OP-T1",
        active_task_assigned_at=T0,
        created_at=T0,
        updated_at=T0,
        disbanded_at=None,
    )
    members = (
        PartyMember(party_id="p1", member_agent_id="agent-A", joined_at=T0),
        PartyMember(party_id="p1", member_agent_id="agent-B", joined_at=T0),
        PartyMember(party_id="p1", member_agent_id="agent-C", joined_at=T0),
    )
    party = Party(state=state, members=members, synergy=None)
    distribution = compute_party_xp_distribution(
        party,
        total_xp=300,
        personal_xp_by_member={"agent-A": 50, "agent-B": 0, "agent-C": 25},
    )

    # Even split: 300 // 3 = 100 per member.
    shares = {share.member_agent_id: share for share in distribution.shares}
    for share in shares.values():
        assert share.party_share == 100
        # Synergy multiplier 1.15 → 115 per member; bonus = 15.
        assert share.synergy_bonus == 15
    # Personal accrual is added on top, not split.
    assert shares["agent-A"].total == 100 + 15 + 50
    assert shares["agent-B"].total == 100 + 15 + 0
    assert shares["agent-C"].total == 100 + 15 + 25
    assert shares["agent-A"].personal_xp == 50


def test_compute_party_xp_distribution_no_synergy_returns_base_xp():
    state = PartyState(
        party_id="p1",
        name="X",
        synergy_label=None,
        synergy_xp_bonus=0.0,
        active_task_id=None,
        active_task_assigned_at=None,
        created_at=T0,
        updated_at=T0,
        disbanded_at=None,
    )
    members = (
        PartyMember(party_id="p1", member_agent_id="agent-A", joined_at=T0),
        PartyMember(party_id="p1", member_agent_id="agent-B", joined_at=T0),
    )
    party = Party(state=state, members=members, synergy=None)
    distribution = compute_party_xp_distribution(party, total_xp=200)
    for share in distribution.shares:
        assert share.party_share == 100
        assert share.synergy_bonus == 0
        assert share.total == 100


def test_compute_party_xp_distribution_floors_remainder():
    state = PartyState(
        party_id="p1",
        name="X",
        synergy_label=None,
        synergy_xp_bonus=0.0,
        active_task_id="OP-T1",
        active_task_assigned_at=T0,
        created_at=T0,
        updated_at=T0,
        disbanded_at=None,
    )
    members = (
        PartyMember(party_id="p1", member_agent_id="agent-A", joined_at=T0),
        PartyMember(party_id="p1", member_agent_id="agent-B", joined_at=T0),
    )
    party = Party(state=state, members=members, synergy=None)
    distribution = compute_party_xp_distribution(party, total_xp=101)

    assert distribution.total_xp_pool == 101
    assert [share.party_share for share in distribution.shares] == [50, 50]
    assert [share.total for share in distribution.shares] == [50, 50]


def test_compute_party_xp_distribution_empty_party_has_no_shares():
    state = PartyState(
        party_id="p1",
        name="X",
        synergy_label=None,
        synergy_xp_bonus=0.0,
        active_task_id=None,
        active_task_assigned_at=None,
        created_at=T0,
        updated_at=T0,
        disbanded_at=None,
    )
    party = Party(state=state, members=(), synergy=None)
    distribution = compute_party_xp_distribution(party, total_xp=200)

    assert distribution.total_xp_pool == 0
    assert distribution.shares == ()


def test_compute_party_xp_distribution_clamps_negative_personal_xp_to_zero():
    state = PartyState(
        party_id="p1",
        name="X",
        synergy_label=None,
        synergy_xp_bonus=0.0,
        active_task_id="OP-T1",
        active_task_assigned_at=T0,
        created_at=T0,
        updated_at=T0,
        disbanded_at=None,
    )
    members = (
        PartyMember(party_id="p1", member_agent_id="agent-A", joined_at=T0),
        PartyMember(party_id="p1", member_agent_id="agent-B", joined_at=T0),
    )
    party = Party(state=state, members=members, synergy=None)
    distribution = compute_party_xp_distribution(
        party, total_xp=100, personal_xp_by_member={"agent-A": -10}
    )
    shares = {share.member_agent_id: share for share in distribution.shares}

    assert shares["agent-A"].personal_xp == 0
    assert shares["agent-A"].total == 50
    assert shares["agent-B"].personal_xp == 0


def test_compute_party_xp_distribution_clamps_negative_synergy_bonus_to_zero():
    state = PartyState(
        party_id="p1",
        name="X",
        synergy_label="legacy_bad_bonus",
        synergy_xp_bonus=-0.25,
        active_task_id="OP-T1",
        active_task_assigned_at=T0,
        created_at=T0,
        updated_at=T0,
        disbanded_at=None,
    )
    members = (
        PartyMember(party_id="p1", member_agent_id="agent-A", joined_at=T0),
        PartyMember(party_id="p1", member_agent_id="agent-B", joined_at=T0),
    )
    party = Party(state=state, members=members, synergy=None)
    distribution = compute_party_xp_distribution(party, total_xp=100)

    assert [share.synergy_bonus for share in distribution.shares] == [0, 0]
    assert [share.total for share in distribution.shares] == [50, 50]


def test_compute_party_xp_distribution_rejects_bool_total_xp():
    state = PartyState(
        party_id="p1",
        name="X",
        synergy_label=None,
        synergy_xp_bonus=0.0,
        active_task_id=None,
        active_task_assigned_at=None,
        created_at=T0,
        updated_at=T0,
        disbanded_at=None,
    )
    members = (
        PartyMember(party_id="p1", member_agent_id="agent-A", joined_at=T0),
        PartyMember(party_id="p1", member_agent_id="agent-B", joined_at=T0),
    )
    party = Party(state=state, members=members, synergy=None)

    with pytest.raises(TypeError, match="total_xp must be an int"):
        compute_party_xp_distribution(party, total_xp=True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_task_complete_returns_distribution_and_releases_task():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="A",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"]),
        now=T0,
    )
    await assign_task(store, party.state.party_id, "OP-T1", now=T0)
    distribution = await task_complete(
        store,
        party.state.party_id,
        total_xp=100,
        personal_xp_by_member={"agent-A": 10},
        now=T0,
    )
    state = await store.get_state(party.state.party_id)
    shares = {share.member_agent_id: share for share in distribution.shares}

    assert state is not None
    assert state.active_task_id is None
    assert shares["agent-A"].total == 60
    assert shares["agent-B"].total == 50


@pytest.mark.asyncio
async def test_release_task_is_idempotent_when_no_active_task():
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="A",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds=_guilds_for(["agent-A", "agent-B"]),
        now=T0,
    )

    state = await release_task(store, party.state.party_id, now=T0)
    assert state.active_task_id is None
    assert state.active_task_assigned_at is None


# ── Synergy degradation — missing matrix file degrades to base XP ──


@pytest.mark.asyncio
async def test_missing_synergy_yaml_degrades_to_no_bonus(tmp_path):
    # Empty YAML at a custom path → load_synergy_matrix raises
    # SynergyComputeFailed; create_party catches it (AC #3) and
    # falls back to no-bonus base XP.
    bad_yaml = tmp_path / "broken.yaml"
    bad_yaml.write_text("schema_version: 99\nsynergies: []\n", encoding="utf-8")
    store = InMemoryPartyStore()
    party = await create_party(
        store,
        name="degraded",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds={
            "agent-A": "backend",
            "agent-B": "frontend",
        },
        synergy_path=bad_yaml,
        now=T0,
    )
    assert party.state.synergy_label is None
    assert party.state.synergy_xp_bonus == 0.0


def test_synergy_for_members_handles_partial_match_to_none():
    # A 3-member party where none of the three pairs match the matrix
    # → no synergy applies.
    assert synergy_for_members(["custom_a", "custom_b", "custom_c"]) is None


# ── list_active_parties (Party Hall feed) ──────────────────────────


@pytest.mark.asyncio
async def test_list_active_parties_returns_non_disbanded_only():
    store = InMemoryPartyStore()
    await create_party(
        store,
        name="alpha",
        member_agent_ids=["agent-A", "agent-B"],
        member_guilds={"agent-A": "backend", "agent-B": "frontend"},
        now=T0,
    )
    await create_party(
        store,
        name="beta",
        member_agent_ids=["agent-C", "agent-D"],
        member_guilds={"agent-C": "security", "agent-D": "devops"},
        now=T0,
    )
    active = await list_active_parties(store)
    assert {p.state.name for p in active} == {"alpha", "beta"}


# ── Synergy matrix shape: every entry covers exactly 2 distinct guilds


def test_synergy_matrix_entries_have_exactly_two_distinct_guilds():
    matrix = load_synergy_matrix()
    for key, entry in matrix.items():
        a, b = key
        assert a != b, f"synergy {entry.label!r} pairs a Guild with itself"
        assert entry.xp_bonus >= 0.0
