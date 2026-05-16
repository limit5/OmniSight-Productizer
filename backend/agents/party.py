"""RPG.W17 -- Party / Synergy system (OP-220).

ADR-0008 §"Party / Synergy system (W17)" lets the operator group 2-5
agents into a *party* that takes a single Tier L+ task as a unit. The
party's Guild composition feeds the cross-Guild synergy matrix to
yield an XP bonus and (sometimes) a skill-specific XP bonus, applied
when the party's task completes.

This module owns:

* The store Protocol + in-memory + Postgres backings of the
  ``agent_party`` membership table and the companion
  ``agent_party_state`` table (alembic 0230).
* The public helpers called out in OP-220:
  :func:`create_party`, :func:`assign_task`,
  :func:`compute_party_xp_distribution`, plus the
  :func:`member_is_gated` pre-pickup helper used by
  :mod:`backend.agents.jira_dispatch` to refuse individual task
  pickups while a member's party holds an active task.
* The error catalog from OP-220 §"Error catalog":
  :class:`PartySizeInvalid`, :class:`MemberAlreadyInParty`,
  :class:`PartyActiveTaskExists`, :class:`MemberInActiveParty`. (The
  fifth error, :class:`SynergyComputeFailed`, is re-exported from
  :mod:`backend.agents.synergy_registry` because that's where it's
  raised — see AC #3.)

W17 sub-wave coverage
---------------------
The W17 ship is split across TODO.md sub-waves; the rows live in
this module:

* **W17.2 (OP-193) — Party formation rules**: the size bound (2-5
  members), the per-member Guild requirement, and the cross-Guild
  synergy lookup that gates the "synergy bonus" attached to the
  party. The contract is pinned by :data:`MIN_PARTY_SIZE`,
  :data:`MAX_PARTY_SIZE`, :class:`PartyFormationRules`,
  :func:`party_formation_rules`, and the pure pre-flight
  :func:`preview_party_formation` (mirrors OP-179's ``ToolLevelSpec``
  / OP-180's ``build_feature_unlock_gate`` attribution pattern — no
  new persistence, no new YAML; just exposes the existing
  ``_validate_members`` / ``_validate_member_guilds`` /
  ``_resolve_synergy`` contract as a structured, raising-free surface
  for the W17.6 Party Builder UI). Tests live in
  :mod:`backend.tests.test_party` (size happy + below/above bounds +
  fullstack matrix lookup + uniform-Guild → no synergy + missing
  YAML degrades).
* W17.4 (per-task exclusivity) lives in :func:`assign_task`.
* W17.5 (shared XP + personal accrual) lives in
  :func:`compute_party_xp_distribution`.

Module-global state audit (per project SOP)
-------------------------------------------
The module declares no module-level mutable state. Every store
implementation is dependency-injected by the caller; the in-memory
default lives inside a per-instance object. ``create_party`` does not
allocate a UUID via ``uuid.uuid4`` unless an explicit ``party_id`` is
omitted by the caller, so tests can pin determinism.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from backend.agents.synergy_registry import (
    SYNERGY_MATRIX_PATH,
    SynergyComputeFailed,
    SynergyEntry,
    synergy_for_members,
)


LOG = logging.getLogger("backend.agents.party")
ConnFactory = Callable[[], Any]


# ── Constants from ADR-0008 §"Party / Synergy system (W17)" ────────

# W17.2 (OP-193) party formation bounds. Mirrored by the
# ``agent_party_state`` CHECK constraint in alembic 0230, so a manual
# DB write also cannot violate the bound.
MIN_PARTY_SIZE = 2
MAX_PARTY_SIZE = 5


# ── Errors (OP-220 §"Error catalog") ───────────────────────────────


class PartyError(RuntimeError):
    """Base class for W17 party errors."""


class PartySizeInvalid(PartyError):
    """Refuse create_party with < MIN_PARTY_SIZE or > MAX_PARTY_SIZE members."""


class MemberAlreadyInParty(PartyError):
    """Refuse adding a member who is already in an active party."""


class PartyActiveTaskExists(PartyError):
    """Refuse assigning a second concurrent task to one party."""


class MemberInActiveParty(PartyError):
    """JQL pre-pickup refusal for an individual task while the agent's
    party holds an active task. Re-exported by
    :mod:`backend.agents.jira_dispatch` so the pre-pickup gate can
    return a structured reason string."""


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class PartyMember:
    """One row from ``agent_party``."""

    party_id: str
    member_agent_id: str
    joined_at: datetime
    released_at: datetime | None = None


@dataclass(frozen=True)
class PartyState:
    """One row from ``agent_party_state`` — the party-level view."""

    party_id: str
    name: str
    synergy_label: str | None
    synergy_xp_bonus: float
    active_task_id: str | None
    active_task_assigned_at: datetime | None
    created_at: datetime
    updated_at: datetime
    disbanded_at: datetime | None = None


@dataclass(frozen=True)
class Party:
    """Aggregate view of a party — state + members. Convenience for
    the Party Hall UI and the JSON router."""

    state: PartyState
    members: tuple[PartyMember, ...]
    synergy: SynergyEntry | None = None


@dataclass(frozen=True)
class MemberXpShare:
    """Per-member XP attribution from :func:`compute_party_xp_distribution`."""

    member_agent_id: str
    personal_xp: int
    party_share: int
    synergy_bonus: int
    total: int


@dataclass(frozen=True)
class PartyXpDistribution:
    """Result of :func:`compute_party_xp_distribution`."""

    party_id: str
    total_xp_pool: int
    synergy_label: str | None
    synergy_xp_bonus: float
    shares: tuple[MemberXpShare, ...]


@dataclass(frozen=True)
class PartyFormationRules:
    """Pinned W17.2 (OP-193) formation contract.

    Returned by :func:`party_formation_rules`. Mirrors the
    ``ToolLevelSpec`` pattern from OP-179 — a frozen, read-only
    catalog of the static contract so the Party Builder UI (W17.6)
    and the API legend can render the bounds without hard-coding
    them out of band.
    """

    min_size: int
    max_size: int
    synergy_matrix_path: Path


@dataclass(frozen=True)
class PartyFormationPreview:
    """Pre-flight result of :func:`preview_party_formation`.

    ``issues`` is a tuple of human-readable refusal strings — empty
    iff the formation is acceptable to :func:`create_party` modulo
    the runtime "is anyone already in an active party" check, which
    only :func:`create_party` can perform because it needs the store
    handle. ``synergy`` is the entry that *would* apply at the time
    of preview; it is ``None`` for same-Guild parties, for parties
    whose Guild list is not covered by any matrix entry, and when
    the synergy YAML cannot be loaded (degraded per AC #3).
    """

    member_agent_ids: tuple[str, ...]
    synergy: SynergyEntry | None
    issues: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        """True iff no formation rule is violated."""
        return not self.issues


# ── Store Protocols ────────────────────────────────────────────────


class PartyStore(Protocol):
    async def upsert_state(self, state: PartyState) -> PartyState: ...
    async def get_state(self, party_id: str) -> PartyState | None: ...
    async def list_members(self, party_id: str) -> tuple[PartyMember, ...]: ...
    async def add_member(self, member: PartyMember) -> PartyMember: ...
    async def active_party_for_member(
        self, member_agent_id: str
    ) -> PartyState | None: ...
    async def list_active_parties(self) -> tuple[PartyState, ...]: ...


# ── In-memory store (dev / tests) ──────────────────────────────────


class InMemoryPartyStore:
    """Dev / test store. Mirrors the Postgres semantics for the unit
    tests on OP-220 §"Test plan"."""

    def __init__(self) -> None:
        self._states: dict[str, PartyState] = {}
        self._members: dict[tuple[str, str], PartyMember] = {}

    async def upsert_state(self, state: PartyState) -> PartyState:
        self._states[state.party_id] = state
        return state

    async def get_state(self, party_id: str) -> PartyState | None:
        return self._states.get(party_id)

    async def list_members(self, party_id: str) -> tuple[PartyMember, ...]:
        return tuple(
            member
            for (pid, _aid), member in self._members.items()
            if pid == party_id and member.released_at is None
        )

    async def add_member(self, member: PartyMember) -> PartyMember:
        self._members[(member.party_id, member.member_agent_id)] = member
        return member

    async def active_party_for_member(
        self, member_agent_id: str
    ) -> PartyState | None:
        for (_pid, aid), member in self._members.items():
            if aid != member_agent_id or member.released_at is not None:
                continue
            state = self._states.get(member.party_id)
            if state is None or state.disbanded_at is not None:
                continue
            return state
        return None

    async def list_active_parties(self) -> tuple[PartyState, ...]:
        return tuple(
            state for state in self._states.values() if state.disbanded_at is None
        )


# ── Postgres store ─────────────────────────────────────────────────


class PostgresPartyStore:
    """``agent_party`` + ``agent_party_state``-backed store (alembic 0230)."""

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def upsert_state(self, state: PartyState) -> PartyState:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_party_state (
                    party_id, name, synergy_label, synergy_xp_bonus,
                    active_task_id, active_task_assigned_at,
                    created_at, updated_at, disbanded_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), $8)
                ON CONFLICT (party_id) DO UPDATE
                    SET name = EXCLUDED.name,
                        synergy_label = EXCLUDED.synergy_label,
                        synergy_xp_bonus = EXCLUDED.synergy_xp_bonus,
                        active_task_id = EXCLUDED.active_task_id,
                        active_task_assigned_at = EXCLUDED.active_task_assigned_at,
                        disbanded_at = EXCLUDED.disbanded_at,
                        updated_at = NOW()
                RETURNING party_id, name, synergy_label, synergy_xp_bonus,
                          active_task_id, active_task_assigned_at,
                          created_at, updated_at, disbanded_at
                """,
                state.party_id,
                state.name,
                state.synergy_label,
                float(state.synergy_xp_bonus),
                state.active_task_id,
                state.active_task_assigned_at,
                state.created_at,
                state.disbanded_at,
            )
        return _row_to_state(row)

    async def get_state(self, party_id: str) -> PartyState | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT party_id, name, synergy_label, synergy_xp_bonus,
                       active_task_id, active_task_assigned_at,
                       created_at, updated_at, disbanded_at
                FROM agent_party_state
                WHERE party_id = $1
                """,
                party_id,
            )
        return _row_to_state(row) if row else None

    async def list_members(self, party_id: str) -> tuple[PartyMember, ...]:
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                """
                SELECT party_id, member_agent_id, joined_at, released_at
                FROM agent_party
                WHERE party_id = $1 AND released_at IS NULL
                ORDER BY joined_at ASC
                """,
                party_id,
            )
        return tuple(_row_to_member(row) for row in rows)

    async def add_member(self, member: PartyMember) -> PartyMember:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_party (
                    party_id, member_agent_id, joined_at, released_at,
                    created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, NOW(), NOW())
                ON CONFLICT (party_id, member_agent_id) DO UPDATE
                    SET joined_at = EXCLUDED.joined_at,
                        released_at = EXCLUDED.released_at,
                        updated_at = NOW()
                RETURNING party_id, member_agent_id, joined_at, released_at
                """,
                member.party_id,
                member.member_agent_id,
                member.joined_at,
                member.released_at,
            )
        return _row_to_member(row)

    async def active_party_for_member(
        self, member_agent_id: str
    ) -> PartyState | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT s.party_id, s.name, s.synergy_label, s.synergy_xp_bonus,
                       s.active_task_id, s.active_task_assigned_at,
                       s.created_at, s.updated_at, s.disbanded_at
                FROM agent_party_state s
                JOIN agent_party m ON m.party_id = s.party_id
                WHERE m.member_agent_id = $1
                  AND m.released_at IS NULL
                  AND s.disbanded_at IS NULL
                  AND s.active_task_id IS NOT NULL
                LIMIT 1
                """,
                member_agent_id,
            )
        return _row_to_state(row) if row else None

    async def list_active_parties(self) -> tuple[PartyState, ...]:
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                """
                SELECT party_id, name, synergy_label, synergy_xp_bonus,
                       active_task_id, active_task_assigned_at,
                       created_at, updated_at, disbanded_at
                FROM agent_party_state
                WHERE disbanded_at IS NULL
                ORDER BY created_at DESC
                """
            )
        return tuple(_row_to_state(row) for row in rows)


# ── Public helpers ─────────────────────────────────────────────────


def party_formation_rules(
    *,
    synergy_path: Path | str = SYNERGY_MATRIX_PATH,
) -> PartyFormationRules:
    """Return the pinned W17.2 (OP-193) formation contract.

    Pure accessor — no I/O, no synergy YAML read. Consumers (the
    Party Builder UI legend, the ``/agents/parties/formation-rules``
    legend, pre-flight admission gates) use this to surface the
    contract bounds without hard-coding the integers out of band.
    """
    return PartyFormationRules(
        min_size=MIN_PARTY_SIZE,
        max_size=MAX_PARTY_SIZE,
        synergy_matrix_path=Path(synergy_path),
    )


def preview_party_formation(
    member_agent_ids: Sequence[str],
    member_guilds: Mapping[str, str],
    *,
    synergy_path: Path | str = SYNERGY_MATRIX_PATH,
) -> PartyFormationPreview:
    """Pre-flight check for the W17.2 (OP-193) formation contract.

    Runs the size + per-member Guild + cross-Guild synergy lookup
    that :func:`create_party` performs, *without* touching a store or
    raising — every refusal is reported through
    :attr:`PartyFormationPreview.issues` instead. The W17.6 Party
    Builder UI calls this on each form keystroke to render live
    validation; the authoritative refusal still happens server-side
    in :func:`create_party`, because only that path can probe the
    store for "is this member already in an active party?".

    Synergy lookup degrades to ``None`` if the YAML cannot be loaded,
    matching the AC #3 degradation contract — a YAML edit accident
    must not block formation previews.
    """
    members: tuple[str, ...]
    issues: list[str] = []
    try:
        members = _validate_members(member_agent_ids)
    except (PartySizeInvalid, MemberAlreadyInParty, ValueError, TypeError) as exc:
        normalised = (
            tuple(m for m in member_agent_ids if isinstance(m, str))
            if isinstance(member_agent_ids, (list, tuple))
            else ()
        )
        return PartyFormationPreview(
            member_agent_ids=normalised,
            synergy=None,
            issues=(str(exc),),
        )
    try:
        _validate_member_guilds(members, member_guilds)
    except (PartyError, TypeError) as exc:
        issues.append(str(exc))
    synergy: SynergyEntry | None = None
    if not issues:
        synergy = _resolve_synergy(member_guilds.values(), path=synergy_path)
    return PartyFormationPreview(
        member_agent_ids=members,
        synergy=synergy,
        issues=tuple(issues),
    )


async def create_party(
    store: PartyStore,
    name: str,
    member_agent_ids: Sequence[str],
    member_guilds: Mapping[str, str],
    *,
    party_id: str | None = None,
    now: datetime | None = None,
    synergy_path: Path | str = SYNERGY_MATRIX_PATH,
) -> Party:
    """Create a new party.

    Orchestrates the W17.2 (OP-193) formation contract:

    * :func:`_validate_members` enforces the 2-5 size bound and
      refuses duplicates,
    * :func:`_validate_member_guilds` requires a per-member Guild
      slug,
    * the store's ``active_party_for_member`` probe refuses members
      already in another active party,
    * :func:`_resolve_synergy` consults the cross-Guild matrix and
      attaches the synergy bonus (or ``None`` for same-Guild parties
      / parties not covered by any matrix entry).

    Per AC #3 ``SynergyComputeFailed`` from the registry is **caught
    here** and degraded to "no synergy + log warning"; the party is
    still created with ``synergy_label=None`` and
    ``synergy_xp_bonus=0.0``.

    ``member_guilds`` is a per-member Guild lookup keyed by
    ``member_agent_id`` — supplied by the caller so this helper stays
    storage-agnostic (the router resolves it from the agent's
    character_card.guild before calling). For client-side live
    validation without a store round-trip, see
    :func:`preview_party_formation` (W17.2 pre-flight helper).
    """
    clean_name = _required("name", name)
    members = _validate_members(member_agent_ids)
    _validate_member_guilds(members, member_guilds)
    chosen_id = party_id or f"party-{uuid.uuid4().hex[:12]}"
    moment = _utc(now or datetime.now(timezone.utc))

    # Refuse members already in an active party.
    for member_id in members:
        active = await store.active_party_for_member(member_id)
        if active is not None:
            raise MemberAlreadyInParty(
                f"agent {member_id!r} is already in active party "
                f"{active.party_id!r}"
            )

    # Synergy lookup — degrade on failure per AC #3.
    synergy = _resolve_synergy(member_guilds.values(), path=synergy_path)
    state = PartyState(
        party_id=chosen_id,
        name=clean_name,
        synergy_label=synergy.label if synergy else None,
        synergy_xp_bonus=synergy.xp_bonus if synergy else 0.0,
        active_task_id=None,
        active_task_assigned_at=None,
        created_at=moment,
        updated_at=moment,
        disbanded_at=None,
    )
    persisted_state = await store.upsert_state(state)
    persisted_members: list[PartyMember] = []
    for member_id in members:
        row = await store.add_member(
            PartyMember(
                party_id=chosen_id,
                member_agent_id=member_id,
                joined_at=moment,
                released_at=None,
            )
        )
        persisted_members.append(row)
    return Party(
        state=persisted_state,
        members=tuple(persisted_members),
        synergy=synergy,
    )


async def assign_task(
    store: PartyStore,
    party_id: str,
    task_id: str,
    *,
    now: datetime | None = None,
) -> PartyState:
    """Assign a single Tier L+ task to ``party_id``.

    Per AC #4 ("per-task exclusivity") this refuses with
    :class:`PartyActiveTaskExists` if the party already holds another
    active task. Idempotent on the *same* task — re-assigning the
    same ``task_id`` returns the existing state row unchanged.
    """
    clean_party_id = _required("party_id", party_id)
    clean_task_id = _required("task_id", task_id)
    state = await store.get_state(clean_party_id)
    if state is None:
        raise PartyError(f"party {clean_party_id!r} does not exist")
    if state.disbanded_at is not None:
        raise PartyError(f"party {clean_party_id!r} is disbanded")
    if state.active_task_id is not None and state.active_task_id != clean_task_id:
        raise PartyActiveTaskExists(
            f"party {clean_party_id!r} already holds active task "
            f"{state.active_task_id!r}; cannot assign {clean_task_id!r}"
        )
    if state.active_task_id == clean_task_id:
        return state
    moment = _utc(now or datetime.now(timezone.utc))
    updated = replace(
        state,
        active_task_id=clean_task_id,
        active_task_assigned_at=moment,
        updated_at=moment,
    )
    return await store.upsert_state(updated)


async def release_task(
    store: PartyStore,
    party_id: str,
    *,
    now: datetime | None = None,
) -> PartyState:
    """Clear the party's ``active_task_id``.

    Called by :func:`task_complete` after XP distribution; exposed as
    a public helper so an operator can manually release a stuck party
    via the router.
    """
    clean_party_id = _required("party_id", party_id)
    state = await store.get_state(clean_party_id)
    if state is None:
        raise PartyError(f"party {clean_party_id!r} does not exist")
    if state.active_task_id is None:
        return state
    moment = _utc(now or datetime.now(timezone.utc))
    updated = replace(
        state,
        active_task_id=None,
        active_task_assigned_at=None,
        updated_at=moment,
    )
    return await store.upsert_state(updated)


def compute_party_xp_distribution(
    party: Party,
    total_xp: int,
    *,
    personal_xp_by_member: Mapping[str, int] | None = None,
) -> PartyXpDistribution:
    """Split ``total_xp`` evenly across the party, plus per-member personal XP.

    Per OP-220 AC #2 ("compute_party_xp_distribution") and the W17
    state-transition contract:

    * Each member gets ``total_xp // N`` (integer floor — remainder
      stays on the table; surfaced via the
      :class:`PartyXpDistribution.total_xp_pool` field for audit).
    * Each member additionally gets ``personal_xp`` from
      ``personal_xp_by_member`` (per-member task contribution).
    * If a synergy applies, each share is multiplied by
      ``(1 + synergy_xp_bonus)`` and ``synergy_bonus`` records the
      delta.

    Pure function — no DB calls. Persistence happens in
    :func:`task_complete` once the operator's `XpDelta` rows are
    written through :mod:`backend.agents.character_card`.
    """
    if not isinstance(total_xp, int) or isinstance(total_xp, bool):
        raise TypeError("total_xp must be an int")
    if total_xp < 0:
        raise ValueError("total_xp must be >= 0")

    members = party.members
    if not members:
        return PartyXpDistribution(
            party_id=party.state.party_id,
            total_xp_pool=0,
            synergy_label=party.state.synergy_label,
            synergy_xp_bonus=party.state.synergy_xp_bonus,
            shares=(),
        )

    per_member_xp = total_xp // len(members)
    bonus_rate = max(0.0, float(party.state.synergy_xp_bonus or 0.0))
    personal_lookup = personal_xp_by_member or {}

    shares: list[MemberXpShare] = []
    for member in members:
        base_share = per_member_xp
        # Round (not floor) to avoid FP truncation surprises like
        # ``int(100 * 1.15) == 114``. Bonus is a small additive
        # integer; round-half-to-even is acceptable here because the
        # distribution is consumed as XP rather than money.
        synergy_bonus = max(0, round(base_share * bonus_rate))
        boosted = base_share + synergy_bonus
        personal = int(max(0, personal_lookup.get(member.member_agent_id, 0)))
        shares.append(
            MemberXpShare(
                member_agent_id=member.member_agent_id,
                personal_xp=personal,
                party_share=base_share,
                synergy_bonus=synergy_bonus,
                total=boosted + personal,
            )
        )

    return PartyXpDistribution(
        party_id=party.state.party_id,
        total_xp_pool=total_xp,
        synergy_label=party.state.synergy_label,
        synergy_xp_bonus=party.state.synergy_xp_bonus,
        shares=tuple(shares),
    )


async def task_complete(
    store: PartyStore,
    party_id: str,
    total_xp: int,
    *,
    personal_xp_by_member: Mapping[str, int] | None = None,
    now: datetime | None = None,
) -> PartyXpDistribution:
    """Mark a party's task complete, compute XP, and ungate members.

    Composes the W17 state-transition contract:

        ``party.task_complete(outcome)``
          → ``compute_party_xp_distribution``
          → mark members ungated for individual tasks
          → caller emits ``party:task:completed`` SSE
    """
    state = await store.get_state(party_id)
    if state is None:
        raise PartyError(f"party {party_id!r} does not exist")
    members = await store.list_members(party_id)
    party = Party(state=state, members=members, synergy=None)
    distribution = compute_party_xp_distribution(
        party, total_xp, personal_xp_by_member=personal_xp_by_member
    )
    await release_task(store, party_id, now=now)
    return distribution


async def member_is_gated(
    store: PartyStore, member_agent_id: str
) -> PartyState | None:
    """Return the party currently gating ``member_agent_id``, or None.

    Used by :mod:`backend.agents.jira_dispatch` as the
    :class:`MemberInActiveParty` pre-pickup check (AC #4 / §"State
    transitions" — gated for individual tasks while party holds an
    active task). "Gated" specifically means
    ``active_task_id is not None``; merely belonging to a party is not
    enough.
    """
    clean = _required("member_agent_id", member_agent_id)
    state = await store.active_party_for_member(clean)
    if state is None or state.active_task_id is None:
        return None
    return state


async def get_party(store: PartyStore, party_id: str) -> Party | None:
    """Hydrate a :class:`Party` aggregate from the store, or ``None``."""
    state = await store.get_state(_required("party_id", party_id))
    if state is None:
        return None
    members = await store.list_members(state.party_id)
    synergy: SynergyEntry | None
    if state.synergy_label:
        # Resolve back to the entry for UI badges. The lookup is
        # best-effort: an operator may have rotated the YAML.
        try:
            from backend.agents.synergy_registry import load_synergy_matrix

            matrix = load_synergy_matrix()
            synergy = next(
                (
                    entry
                    for entry in matrix.values()
                    if entry.label == state.synergy_label
                ),
                None,
            )
        except SynergyComputeFailed:
            synergy = None
    else:
        synergy = None
    return Party(state=state, members=members, synergy=synergy)


async def list_active_parties(store: PartyStore) -> tuple[Party, ...]:
    """List every non-disbanded party — used by the Party Hall UI."""
    states = await store.list_active_parties()
    out: list[Party] = []
    for state in states:
        members = await store.list_members(state.party_id)
        out.append(Party(state=state, members=members, synergy=None))
    return tuple(out)


# ── Internal helpers ───────────────────────────────────────────────


def _validate_members(member_agent_ids: Sequence[str]) -> tuple[str, ...]:
    """Enforce the W17.2 (OP-193) size bound + duplicate-member check.

    Raises :class:`PartySizeInvalid` outside ``[MIN_PARTY_SIZE,
    MAX_PARTY_SIZE]`` and :class:`MemberAlreadyInParty` on duplicates
    within the proposed list.
    """
    if not isinstance(member_agent_ids, (list, tuple)):
        raise TypeError("member_agent_ids must be a sequence of strings")
    cleaned: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(member_agent_ids):
        if not isinstance(raw, str):
            raise TypeError(
                f"member_agent_ids[{index}] must be a string; got {type(raw).__name__}"
            )
        clean = raw.strip()
        if not clean:
            raise ValueError(f"member_agent_ids[{index}] is empty")
        if clean in seen:
            raise MemberAlreadyInParty(
                f"member {clean!r} appears twice in member_agent_ids"
            )
        seen.add(clean)
        cleaned.append(clean)
    if len(cleaned) < MIN_PARTY_SIZE:
        raise PartySizeInvalid(
            f"party requires at least {MIN_PARTY_SIZE} members; got {len(cleaned)}"
        )
    if len(cleaned) > MAX_PARTY_SIZE:
        raise PartySizeInvalid(
            f"party allows at most {MAX_PARTY_SIZE} members; got {len(cleaned)}"
        )
    return tuple(cleaned)


def _validate_member_guilds(
    members: tuple[str, ...],
    member_guilds: Mapping[str, str],
) -> None:
    """Enforce the W17.2 (OP-193) per-member Guild requirement.

    Every member must have a non-empty Guild slug in
    ``member_guilds`` so the cross-Guild synergy lookup has a
    well-defined Guild bag. The caller resolves slugs from the
    member's ``character_card.guild`` before invoking.
    """
    if not isinstance(member_guilds, Mapping):
        raise TypeError("member_guilds must be a mapping member_agent_id -> guild_slug")
    for member_id in members:
        guild = member_guilds.get(member_id)
        if guild is None:
            raise PartyError(
                f"member_guilds is missing a guild slug for member {member_id!r}"
            )
        if not isinstance(guild, str) or not guild.strip():
            raise PartyError(
                f"member_guilds[{member_id!r}] must be a non-empty guild slug"
            )


def _resolve_synergy(
    guilds: Iterable[str],
    *,
    path: Path | str,
) -> SynergyEntry | None:
    """W17.2 (OP-193) cross-Guild synergy lookup.

    Wrap :func:`synergy_for_members` so AC #3 ("degrade to no-bonus
    base XP + log warning") fires uniformly across call sites
    (``create_party`` and :func:`preview_party_formation`).
    """
    try:
        return synergy_for_members(guilds, path=path)
    except SynergyComputeFailed as exc:
        LOG.warning("synergy_compute_failed; degrading to no-bonus base XP: %s", exc)
        return None


@asynccontextmanager
async def _acquire(factory: ConnFactory) -> AsyncIterator[Any]:
    cm = factory()
    async with cm as conn:
        yield conn


def _row_to_state(row: Any) -> PartyState:
    return PartyState(
        party_id=row["party_id"],
        name=row["name"],
        synergy_label=row["synergy_label"],
        synergy_xp_bonus=float(row["synergy_xp_bonus"] or 0.0),
        active_task_id=row["active_task_id"],
        active_task_assigned_at=_optional_utc(row["active_task_assigned_at"]),
        created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]),
        disbanded_at=_optional_utc(row["disbanded_at"]),
    )


def _row_to_member(row: Any) -> PartyMember:
    return PartyMember(
        party_id=row["party_id"],
        member_agent_id=row["member_agent_id"],
        joined_at=_utc(row["joined_at"]),
        released_at=_optional_utc(row["released_at"]),
    )


def _required(field_name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} is required")
    return clean


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _utc(value) if value is not None else None


__all__ = [
    "InMemoryPartyStore",
    "MAX_PARTY_SIZE",
    "MIN_PARTY_SIZE",
    "MemberAlreadyInParty",
    "MemberInActiveParty",
    "MemberXpShare",
    "Party",
    "PartyActiveTaskExists",
    "PartyError",
    "PartyFormationPreview",
    "PartyFormationRules",
    "PartyMember",
    "PartySizeInvalid",
    "PartyState",
    "PartyStore",
    "PartyXpDistribution",
    "PostgresPartyStore",
    "SynergyComputeFailed",
    "assign_task",
    "compute_party_xp_distribution",
    "create_party",
    "get_party",
    "list_active_parties",
    "member_is_gated",
    "party_formation_rules",
    "preview_party_formation",
    "release_task",
    "task_complete",
]
