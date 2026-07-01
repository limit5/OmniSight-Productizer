"""RPG.W1.2 -- agent character card CRUD + first-task creation.

ADR 0008 defines the Layer-1 stat sheet as a tiny PostgreSQL row keyed by
``agent_id``. This module owns the backend CRUD surface for that row and keeps
the first-task auto-create path explicit via ``ensure_card_for_first_task``.

Module-global state audit (SOP Step 1): this module defines immutable
constants, dataclasses, Protocols, and classes only. Mutable character-card
state lives inside the injected store. The included in-memory store is per
worker for dev/tests; production callers should use ``PostgresCharacterCardStore``
against the ``agent_character_card`` table from RPG.W1.1.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from backend.agents.guild_registry import GUILDS
from backend.agents.xp_engine import BASE_TASK_XP

LOG = logging.getLogger(__name__)


ConnFactory = Callable[[], Any]
"""Callable returning an async context manager yielding a connection."""

DEFAULT_LEVEL = 1
DEFAULT_XP = 0
DEFAULT_INSTANCE_SUFFIX = "alpha"
DEFAULT_GUILD = "backend"
DEFAULT_SPECIALIZATION_LABEL = ""
DEFAULT_STYLE_FINGERPRINT = ""
CHARACTER_CARD_GUILDS = frozenset({DEFAULT_GUILD})
CharacterCardSort = Literal["level", "xp", "activity"]

_CARD_RETURNING_COLS = (
    'agent_id, "class" AS agent_class, instance_suffix, guild, level, xp, '
    "specialization_label, style_fingerprint, created_at"
)

_UPDATE_COLUMNS = {
    "agent_class": '"class"',
    "instance_suffix": "instance_suffix",
    "guild": "guild",
    "level": "level",
    "xp": "xp",
    "specialization_label": "specialization_label",
    "style_fingerprint": "style_fingerprint",
}


class CharacterCardError(RuntimeError):
    """Base error for character-card failures."""


class CharacterCardNotFoundError(CharacterCardError):
    """Raised when a requested character card does not exist."""


class CharacterCardAlreadyExistsError(CharacterCardError):
    """Raised when creating a duplicate character card."""


class CharacterCardGuildDriftError(RuntimeError):
    """Raised when character-card Guild slugs drift outside the RPG registry."""


@dataclass(frozen=True)
class CharacterCard:
    """Layer-1 RPG stat sheet for one concrete agent instance."""

    agent_id: str
    agent_class: str
    instance_suffix: str
    guild: str
    level: int
    xp: int
    specialization_label: str
    style_fingerprint: str
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _required("agent_id", self.agent_id))
        object.__setattr__(
            self,
            "agent_class",
            _required("agent_class", self.agent_class),
        )
        object.__setattr__(
            self,
            "instance_suffix",
            _required("instance_suffix", self.instance_suffix),
        )
        object.__setattr__(self, "guild", _required_guild(self.guild))
        if self.level < 1:
            raise ValueError("level must be >= 1")
        if self.xp < 0:
            raise ValueError("xp must be >= 0")
        object.__setattr__(
            self,
            "specialization_label",
            self.specialization_label.strip(),
        )
        object.__setattr__(
            self,
            "style_fingerprint",
            self.style_fingerprint.strip(),
        )
        object.__setattr__(self, "created_at", _utc(self.created_at))


@dataclass(frozen=True)
class CharacterSkillEntry:
    """One W12 skill row as exposed on Character Card reads."""

    skill_id: str
    level: int
    xp: int
    next_level_xp: int
    branch_choice: str | None
    last_active_at: datetime
    branch_choice_required: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "last_active_at", _utc(self.last_active_at))


@dataclass(frozen=True)
class CharacterCardRosterEntry:
    """Guild Hall roster row with activity metadata for sorting/display."""

    card: CharacterCard
    last_activity_at: datetime
    skills: tuple[CharacterSkillEntry, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "last_activity_at", _utc(self.last_activity_at))


@dataclass(frozen=True)
class CharacterCardCreate:
    """Inputs for creating a character card."""

    agent_id: str
    agent_class: str
    guild: str = DEFAULT_GUILD
    instance_suffix: str = DEFAULT_INSTANCE_SUFFIX
    level: int = DEFAULT_LEVEL
    xp: int = DEFAULT_XP
    specialization_label: str = DEFAULT_SPECIALIZATION_LABEL
    style_fingerprint: str = DEFAULT_STYLE_FINGERPRINT
    created_at: datetime | None = None

    def to_card(self) -> CharacterCard:
        return CharacterCard(
            agent_id=self.agent_id,
            agent_class=self.agent_class,
            instance_suffix=self.instance_suffix,
            guild=self.guild,
            level=self.level,
            xp=self.xp,
            specialization_label=self.specialization_label,
            style_fingerprint=self.style_fingerprint,
            created_at=self.created_at or datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class CharacterCardUpdate:
    """Patch-style character card update."""

    agent_class: str | None = None
    instance_suffix: str | None = None
    guild: str | None = None
    level: int | None = None
    xp: int | None = None
    specialization_label: str | None = None
    style_fingerprint: str | None = None


@dataclass(frozen=True)
class FirstTaskCharacterCard:
    """Inputs captured when an agent receives its first task."""

    agent_id: str
    agent_class: str
    task_area: str = DEFAULT_GUILD
    instance_suffix: str = DEFAULT_INSTANCE_SUFFIX
    specialization_label: str = DEFAULT_SPECIALIZATION_LABEL

    def to_create(self) -> CharacterCardCreate:
        return CharacterCardCreate(
            agent_id=self.agent_id,
            agent_class=self.agent_class,
            guild=_guild_from_task_area(self.task_area),
            instance_suffix=self.instance_suffix,
            specialization_label=self.specialization_label,
        )


class CharacterCardStore(Protocol):
    async def create_card(self, card: CharacterCardCreate) -> CharacterCard: ...
    async def get_card(self, agent_id: str) -> CharacterCard | None: ...
    async def list_cards(
        self,
        *,
        guild: str | None = None,
        sort_by: CharacterCardSort = "level",
    ) -> tuple[CharacterCardRosterEntry, ...]: ...
    async def update_card(
        self,
        agent_id: str,
        patch: CharacterCardUpdate,
    ) -> CharacterCard: ...
    async def delete_card(self, agent_id: str) -> bool: ...
    async def ensure_card_for_first_task(
        self,
        first_task: FirstTaskCharacterCard,
    ) -> CharacterCard: ...


class InMemoryCharacterCardStore:
    """Dev/test store. Production durable storage should implement the Protocol."""

    def __init__(self) -> None:
        self._cards: dict[str, CharacterCard] = {}

    async def create_card(self, card: CharacterCardCreate) -> CharacterCard:
        created = card.to_card()
        if created.agent_id in self._cards:
            raise CharacterCardAlreadyExistsError(
                f"character card already exists: {created.agent_id}"
            )
        self._cards[created.agent_id] = created
        return created

    async def get_card(self, agent_id: str) -> CharacterCard | None:
        return self._cards.get(_required("agent_id", agent_id))

    async def list_cards(
        self,
        *,
        guild: str | None = None,
        sort_by: CharacterCardSort = "level",
    ) -> tuple[CharacterCardRosterEntry, ...]:
        clean_guild = _optional_required("guild", guild)
        entries = tuple(
            CharacterCardRosterEntry(card=card, last_activity_at=card.created_at)
            for card in self._cards.values()
            if clean_guild is None or card.guild == clean_guild
        )
        return _sort_roster_entries(entries, sort_by)

    async def update_card(
        self,
        agent_id: str,
        patch: CharacterCardUpdate,
    ) -> CharacterCard:
        agent_id = _required("agent_id", agent_id)
        existing = self._cards.get(agent_id)
        if existing is None:
            raise CharacterCardNotFoundError(f"character card not found: {agent_id}")
        updated = _apply_patch(existing, patch)
        self._cards[agent_id] = updated
        return updated

    async def delete_card(self, agent_id: str) -> bool:
        agent_id = _required("agent_id", agent_id)
        return self._cards.pop(agent_id, None) is not None

    async def ensure_card_for_first_task(
        self,
        first_task: FirstTaskCharacterCard,
    ) -> CharacterCard:
        existing = await self.get_card(first_task.agent_id)
        if existing is not None:
            return existing
        return await self.create_card(first_task.to_create())


class PostgresCharacterCardStore:
    """PostgreSQL-backed character card store.

    The store expects RPG.W1.1's ``agent_character_card`` table:
    ``agent_id / class / instance_suffix / guild / level / xp /
    specialization_label / style_fingerprint / created_at``.
    """

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def create_card(self, card: CharacterCardCreate) -> CharacterCard:
        created = card.to_card()
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_character_card (
                    agent_id, "class", instance_suffix, guild, level, xp,
                    specialization_label, style_fingerprint, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (agent_id) DO NOTHING
                RETURNING {_CARD_RETURNING_COLS}
                """,
                created.agent_id,
                created.agent_class,
                created.instance_suffix,
                created.guild,
                created.level,
                created.xp,
                created.specialization_label,
                created.style_fingerprint,
                created.created_at,
            )
        if row is None:
            raise CharacterCardAlreadyExistsError(
                f"character card already exists: {created.agent_id}"
            )
        return _row_to_card(row)

    async def get_card(self, agent_id: str) -> CharacterCard | None:
        agent_id = _required("agent_id", agent_id)
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_CARD_RETURNING_COLS}
                FROM agent_character_card
                WHERE agent_id = $1
                """,
                agent_id,
            )
        return _row_to_card(row) if row else None

    async def list_cards(
        self,
        *,
        guild: str | None = None,
        sort_by: CharacterCardSort = "level",
    ) -> tuple[CharacterCardRosterEntry, ...]:
        clean_guild = _optional_required("guild", guild)
        order_expr = _postgres_order_expr(sort_by)
        params: list[Any] = []
        where = ""
        if clean_guild is not None:
            params.append(clean_guild)
            where = "WHERE c.guild = $1"

        async with _acquire(self._factory) as conn:
            # tasks.created_at / completed_at are TEXT (alembic 0001
            # baseline DDL is dialect-shared, so the PG compat shim keeps
            # the SQLite ``datetime('now')`` default by rewriting it to
            # ``to_char(...)`` rather than promoting the column to
            # TIMESTAMPTZ). agent_character_card.created_at is TIMESTAMPTZ
            # per migration 0239. COALESCE refuses to unify the two
            # without an explicit cast on the tasks side.
            rows = await conn.fetch(
                f"""
                SELECT c.agent_id, c."class" AS agent_class, c.instance_suffix,
                       c.guild, c.level, c.xp, c.specialization_label,
                       c.style_fingerprint, c.created_at,
                       COALESCE(activity.last_activity_at, c.created_at)
                           AS last_activity_at
                FROM agent_character_card c
                LEFT JOIN (
                    SELECT assigned_agent_id,
                           MAX(COALESCE(
                               completed_at::timestamptz,
                               created_at::timestamptz
                           )) AS last_activity_at
                    FROM tasks
                    WHERE assigned_agent_id IS NOT NULL
                    GROUP BY assigned_agent_id
                ) activity ON activity.assigned_agent_id = c.agent_id
                {where}
                ORDER BY {order_expr} DESC, c.agent_id ASC
                """,
                *params,
            )
        return tuple(_row_to_roster_entry(row) for row in rows)

    async def update_card(
        self,
        agent_id: str,
        patch: CharacterCardUpdate,
    ) -> CharacterCard:
        agent_id = _required("agent_id", agent_id)
        values = _update_values(patch)
        if not values:
            existing = await self.get_card(agent_id)
            if existing is None:
                raise CharacterCardNotFoundError(f"character card not found: {agent_id}")
            return existing

        assignments = [
            f"{_UPDATE_COLUMNS[field]} = ${idx}"
            for idx, (field, _value) in enumerate(values, start=2)
        ]
        params = [agent_id, *[value for _field, value in values]]
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE agent_character_card
                SET {", ".join(assignments)}
                WHERE agent_id = $1
                RETURNING {_CARD_RETURNING_COLS}
                """,
                *params,
            )
        if row is None:
            raise CharacterCardNotFoundError(f"character card not found: {agent_id}")
        return _row_to_card(row)

    async def award_xp_atomic(
        self, agent_id: str, delta_xp: int
    ) -> tuple[CharacterCard, int] | None:
        """Atomically add ``delta_xp`` to the card's xp, then re-sync level.

        The read-modify-write in :func:`_award_task_xp` (get_card then update_card)
        loses updates when two runner slots deliver the same character's ticket at
        once: both read the same xp and the last update clobbers the first. This
        folds the increment into a single ``UPDATE ... SET xp = xp + $delta`` so
        concurrent awards accumulate instead of overwriting. ``level`` is a
        denormalised/derived column (xp is the source of truth), so it is re-synced
        by a second statement on the same connection — the same two-statement
        xp-add-then-level-sync pattern as the skill store (S2b). Returns
        ``(updated_card, previous_level)`` or ``None`` when the card doesn't exist.
        """
        from backend.agents import xp_engine

        agent_id = _required("agent_id", agent_id)
        async with _acquire(self._factory) as conn:
            bumped = await conn.fetchrow(
                """
                UPDATE agent_character_card
                SET xp = xp + $2, updated_at = NOW()
                WHERE agent_id = $1
                RETURNING xp, level
                """,
                agent_id,
                int(delta_xp),
            )
            if bumped is None:
                return None
            new_xp = int(bumped["xp"])
            previous_level = int(bumped["level"])
            new_level = xp_engine.level_for_xp(new_xp)
            if new_level != previous_level:
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_character_card
                    SET level = $2, updated_at = NOW()
                    WHERE agent_id = $1
                    RETURNING {_CARD_RETURNING_COLS}
                    """,
                    agent_id,
                    new_level,
                )
            else:
                row = await conn.fetchrow(
                    f"SELECT {_CARD_RETURNING_COLS} FROM agent_character_card "
                    f"WHERE agent_id = $1",
                    agent_id,
                )
        return _row_to_card(row), previous_level

    async def delete_card(self, agent_id: str) -> bool:
        agent_id = _required("agent_id", agent_id)
        async with _acquire(self._factory) as conn:
            status = await conn.execute(
                "DELETE FROM agent_character_card WHERE agent_id = $1",
                agent_id,
            )
        return _execute_count(status) > 0

    async def ensure_card_for_first_task(
        self,
        first_task: FirstTaskCharacterCard,
    ) -> CharacterCard:
        create = first_task.to_create().to_card()
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_character_card (
                    agent_id, "class", instance_suffix, guild, level, xp,
                    specialization_label, style_fingerprint, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (agent_id) DO UPDATE
                    SET agent_id = agent_character_card.agent_id
                RETURNING {_CARD_RETURNING_COLS}
                """,
                create.agent_id,
                create.agent_class,
                create.instance_suffix,
                create.guild,
                create.level,
                create.xp,
                create.specialization_label,
                create.style_fingerprint,
                create.created_at,
            )
        return _row_to_card(row)


class CharacterCardRegistry:
    """Operator-facing CRUD facade for agent RPG character cards."""

    def __init__(self, store: CharacterCardStore | None = None) -> None:
        self.store = store or InMemoryCharacterCardStore()

    async def create_card(self, card: CharacterCardCreate) -> CharacterCard:
        return await self.store.create_card(card)

    async def get_card(
        self,
        agent_id: str,
        *,
        require_exists: bool = True,
    ) -> CharacterCard | None:
        card = await self.store.get_card(agent_id)
        if card is None and require_exists:
            raise CharacterCardNotFoundError(f"character card not found: {agent_id}")
        return card

    async def list_cards(
        self,
        *,
        guild: str | None = None,
        sort_by: CharacterCardSort = "level",
    ) -> tuple[CharacterCardRosterEntry, ...]:
        return await self.store.list_cards(guild=guild, sort_by=sort_by)

    async def update_card(
        self,
        agent_id: str,
        patch: CharacterCardUpdate,
    ) -> CharacterCard:
        previous = await self.store.get_card(agent_id)
        updated = await self.store.update_card(agent_id, patch)
        if previous is not None and updated.level > previous.level:
            _emit_level_up_safely(previous, updated)
        return updated

    async def delete_card(self, agent_id: str) -> bool:
        return await self.store.delete_card(agent_id)

    async def ensure_card_for_first_task(
        self,
        first_task: FirstTaskCharacterCard,
    ) -> CharacterCard:
        return await self.store.ensure_card_for_first_task(first_task)


async def fetch_skill_entries(
    skill_store: "Any",
    agent_id: str,
) -> tuple[CharacterSkillEntry, ...]:
    """W12 helper: read ``agent_skill_state`` rows for the Character Card.

    ``skill_store`` is any object with the
    ``backend.agents.skill_leveling.SkillStateStore`` shape. The helper
    is intentionally framework-agnostic so the router layer can wire it
    against the Postgres or in-memory store without dragging the
    skill-state import surface into the character-card module
    unconditionally.
    """

    from backend.agents.skill_leveling import (
        BRANCH_LOCK_LEVEL,
        next_level_threshold,
    )

    rows = await skill_store.list_states(_required("agent_id", agent_id))
    return tuple(
        CharacterSkillEntry(
            skill_id=row.skill_id,
            level=row.level,
            xp=row.xp,
            next_level_xp=next_level_threshold(row.level),
            branch_choice=row.branch_choice,
            last_active_at=row.last_active_at,
            branch_choice_required=(
                row.level >= BRANCH_LOCK_LEVEL and row.branch_choice is None
            ),
        )
        for row in rows
    )


@asynccontextmanager
async def _acquire(factory: ConnFactory) -> AsyncIterator[Any]:
    cm = factory()
    async with cm as conn:
        yield conn


def _apply_patch(card: CharacterCard, patch: CharacterCardUpdate) -> CharacterCard:
    values = dict(_update_values(patch))
    if not values:
        return card
    return replace(card, **values)


def _update_values(patch: CharacterCardUpdate) -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []
    if patch.agent_class is not None:
        values.append(("agent_class", _required("agent_class", patch.agent_class)))
    if patch.instance_suffix is not None:
        values.append(
            ("instance_suffix", _required("instance_suffix", patch.instance_suffix))
        )
    if patch.guild is not None:
        values.append(("guild", _required_guild(patch.guild)))
    if patch.level is not None:
        if patch.level < 1:
            raise ValueError("level must be >= 1")
        values.append(("level", patch.level))
    if patch.xp is not None:
        if patch.xp < 0:
            raise ValueError("xp must be >= 0")
        values.append(("xp", patch.xp))
    if patch.specialization_label is not None:
        values.append(("specialization_label", patch.specialization_label.strip()))
    if patch.style_fingerprint is not None:
        values.append(("style_fingerprint", patch.style_fingerprint.strip()))
    return values


def _row_to_card(row: Any) -> CharacterCard:
    return CharacterCard(
        agent_id=row["agent_id"],
        agent_class=row["agent_class"],
        instance_suffix=row["instance_suffix"],
        guild=row["guild"],
        level=row["level"],
        xp=row["xp"],
        specialization_label=row["specialization_label"] or "",
        style_fingerprint=row["style_fingerprint"] or "",
        created_at=row["created_at"],
    )


def _row_to_roster_entry(row: Any) -> CharacterCardRosterEntry:
    return CharacterCardRosterEntry(
        card=_row_to_card(row),
        last_activity_at=row["last_activity_at"],
    )


def _sort_roster_entries(
    entries: tuple[CharacterCardRosterEntry, ...],
    sort_by: CharacterCardSort,
) -> tuple[CharacterCardRosterEntry, ...]:
    _validate_sort(sort_by)
    if sort_by == "level":
        key = lambda entry: (-entry.card.level, entry.card.agent_id)
    elif sort_by == "xp":
        key = lambda entry: (-entry.card.xp, entry.card.agent_id)
    else:
        key = lambda entry: (-entry.last_activity_at.timestamp(), entry.card.agent_id)
    return tuple(sorted(entries, key=key))


def _postgres_order_expr(sort_by: CharacterCardSort) -> str:
    _validate_sort(sort_by)
    if sort_by == "level":
        return "c.level"
    if sort_by == "xp":
        return "c.xp"
    return "COALESCE(activity.last_activity_at, c.created_at)"


def _validate_sort(sort_by: CharacterCardSort) -> None:
    if sort_by not in {"level", "xp", "activity"}:
        raise ValueError("sort_by must be one of: level, xp, activity")


def _required(field: str, value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} is required")
    return stripped


def _optional_required(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    return _required(field, value)


def _required_guild(value: str) -> str:
    guild = _required("guild", value)
    if guild not in GUILDS:
        raise ValueError(f"unknown guild: {guild!r}")
    return guild


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _guild_from_task_area(task_area: str) -> str:
    area = _required("task_area", task_area).lower()
    return _required_guild(area)


def missing_character_card_guilds_from_registry(
    declared_guilds: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Return character-card Guild slugs absent from the RPG Guild registry."""

    declared = CHARACTER_CARD_GUILDS if declared_guilds is None else declared_guilds
    missing = {_required("guild", guild) for guild in declared} - GUILDS
    return tuple(sorted(missing))


def assert_character_card_guilds_within_registry(
    declared_guilds: Iterable[str] | None = None,
) -> None:
    """Raise if character-card Guild slugs drift outside the RPG registry."""

    missing = missing_character_card_guilds_from_registry(declared_guilds)
    if missing:
        raise CharacterCardGuildDriftError(
            "agent_character_card.guild drifted outside guild_registry.GUILDS: "
            f"{list(missing)}"
        )


def _execute_count(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (IndexError, TypeError, ValueError):
        return 0


def _emit_level_up_safely(previous: CharacterCard, updated: CharacterCard) -> None:
    try:
        from backend.events import emit_rpg_level_up

        emit_rpg_level_up(
            updated.agent_id,
            previous_level=previous.level,
            level=updated.level,
            xp=updated.xp,
            broadcast_scope="global",
        )
    except Exception:
        pass
    # W14.1 (OP-185): fire a talent-fork trigger for every
    # Lv-10/30/50/80 milestone crossed by this transition so the
    # Character Card "Talents" tab (W14.5) can surface the picker
    # modal that blocks task assignment until the operator commits.
    # Best-effort — the level-up emit above is what drives the
    # animation; talent-fork is the *operator decision* signal and
    # must never block the character-card update path.
    try:
        from backend.agents.talent_tree import milestones_crossed
        from backend.events import emit_rpg_talent_fork_required

        for milestone in milestones_crossed(previous.level, updated.level):
            emit_rpg_talent_fork_required(
                updated.agent_id,
                milestone=milestone,
                agent_level=updated.level,
                guild=updated.guild,
                broadcast_scope="global",
            )
    except Exception:
        pass


# ── Runner pickup integration (OP-1459) ───────────────────────────


@asynccontextmanager
async def _connect_character_card_from_env() -> AsyncIterator[Any]:
    """Open a single asyncpg connection from ``OMNISIGHT_DATABASE_URL``.

    Mirrors :mod:`backend.agents.runner_metrics_recorder` so the runner's
    one-shot RPG.W1.2 write reuses the same env-DSN contract as the
    telemetry recorder: ``OMNISIGHT_DATABASE_URL`` wins over the generic
    ``DATABASE_URL``; ``asyncpg`` is imported lazily so non-DB-backed
    deployments do not need the dependency.
    """
    dsn = os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("OMNISIGHT_DATABASE_URL or DATABASE_URL is not set")
    from backend.db_url import parse as parse_db_url

    parsed = parse_db_url(dsn)
    if not parsed.is_postgres:
        raise RuntimeError("agent_character_card requires a Postgres database URL")
    import asyncpg  # type: ignore[import-not-found]

    conn = await asyncpg.connect(**parsed.asyncpg_connect_kwargs())
    try:
        yield conn
    finally:
        await conn.close()


def _resolve_first_task_guild(task_area: str | None) -> str:
    """Pick the Guild slug used for first-task card creation.

    The runner ticket area can be a comma-separated list of declared
    areas (`"backend,security"`), `<none>`, or an unknown slug. We pick
    the first segment that maps cleanly into the registry and fall
    back to :data:`DEFAULT_GUILD` ("backend") so dispatch never blocks
    on an unrecognised area — the card is durable identity, not a
    routing decision.
    """
    if not task_area:
        return DEFAULT_GUILD
    for raw in task_area.split(","):
        candidate = raw.strip().lower()
        if candidate and candidate in GUILDS:
            return candidate
    return DEFAULT_GUILD


async def ensure_card_for_first_task_from_env(
    *,
    agent_id: str,
    agent_class: str,
    instance_suffix: str = DEFAULT_INSTANCE_SUFFIX,
    task_area: str | None = None,
    specialization_label: str = DEFAULT_SPECIALIZATION_LABEL,
    conn_factory: ConnFactory | None = None,
) -> CharacterCard | None:
    """Run the W1.2 first-task upsert against the env-configured DSN.

    Returns the existing or freshly-created :class:`CharacterCard` row
    on success, or ``None`` when the call fails open (no DSN, asyncpg
    unavailable, connection error, schema not yet deployed). Failures
    are logged at WARNING with prefix ``CharacterCardFirstTaskFailed``
    so operators can grep journalctl; the caller treats ``None`` as
    "first-task hook skipped" and continues pickup unblocked.

    ``conn_factory`` is a test seam — production callers omit it and
    the env-DSN connection helper above is used.
    """
    guild = _resolve_first_task_guild(task_area)
    first_task = FirstTaskCharacterCard(
        agent_id=agent_id,
        agent_class=agent_class,
        task_area=guild,
        instance_suffix=instance_suffix,
        specialization_label=specialization_label,
    )
    try:
        if conn_factory is None:
            async with _connect_character_card_from_env() as conn:
                store = PostgresCharacterCardStore(_conn_passthrough_factory(conn))
                return await store.ensure_card_for_first_task(first_task)
        store = PostgresCharacterCardStore(conn_factory)
        return await store.ensure_card_for_first_task(first_task)
    except Exception as exc:  # noqa: BLE001 — runner pickup must never wedge on this
        LOG.warning(
            "CharacterCardFirstTaskFailed agent_id=%s agent_class=%s err=%s",
            agent_id,
            agent_class,
            exc,
        )
        return None


def ensure_card_for_first_task_sync(
    *,
    agent_id: str,
    agent_class: str,
    instance_suffix: str = DEFAULT_INSTANCE_SUFFIX,
    task_area: str | None = None,
    specialization_label: str = DEFAULT_SPECIALIZATION_LABEL,
) -> CharacterCard | None:
    """Synchronous wrapper for ``auto-runner-jira.py``.

    Drives a fresh :func:`asyncio.run` loop so the sync dispatch path
    can call into the asyncpg-backed store without restructuring the
    runner. Mirrors the ``*_sync`` shape used by
    :mod:`backend.agents.runner_metrics_recorder`. Must not be invoked
    from inside an already-running event loop.
    """
    return asyncio.run(
        ensure_card_for_first_task_from_env(
            agent_id=agent_id,
            agent_class=agent_class,
            instance_suffix=instance_suffix,
            task_area=task_area,
            specialization_label=specialization_label,
        )
    )


async def _award_task_xp(
    conn_factory: ConnFactory,
    *,
    agent_id: str,
    outcome_status: str,
    tier: str | None,
    base_xp: int,
) -> tuple[Any, CharacterCard] | None:
    """Core award: read card → xp_engine delta → persist new xp+level.

    Uses :class:`CharacterCardRegistry.update_card` so a level increase fires the
    RPG level-up / talent-fork events. Returns ``(XpDelta, updated_card)`` or
    ``None`` when the card doesn't exist yet (nothing to award to).
    """
    from backend.agents import xp_engine

    store = PostgresCharacterCardStore(conn_factory)
    delta = xp_engine.award_xp(
        agent_id,
        {
            "status": outcome_status,
            "base_xp": base_xp,
            "tier_l_plus": str(tier or "").upper() in {"L", "X"},
        },
    )
    result = await store.award_xp_atomic(agent_id, delta.xp)
    if result is None:
        return None  # card doesn't exist yet — nothing to award to
    updated, previous_level = result
    # Preserve the RPG level-up / talent-fork events (previously fired by
    # CharacterCardRegistry.update_card) now that the award is atomic.
    if updated.level > previous_level:
        _emit_level_up_safely(replace(updated, level=previous_level), updated)
    return delta, updated


async def award_task_xp_from_env(
    *,
    agent_id: str,
    outcome_status: str = "success",
    tier: str | None = None,
    base_xp: int = BASE_TASK_XP,
    conn_factory: ConnFactory | None = None,
) -> tuple[Any, CharacterCard] | None:
    """Award task-completion XP to an existing character card (RPG.W4).

    The progression loop's missing link: the runner calls this on a successful
    delivery so the CHARACTER (or bot) that owns the ticket accrues XP and levels
    up per the ADR-0008 ``100·N^1.4`` curve. Fail-open — returns ``None`` on any
    error (no DSN, card absent, schema not deployed) so a delivery is never
    wedged by the RPG write. ``conn_factory`` is a test seam.
    """
    try:
        if conn_factory is None:
            async with _connect_character_card_from_env() as conn:
                return await _award_task_xp(
                    _conn_passthrough_factory(conn),
                    agent_id=agent_id,
                    outcome_status=outcome_status,
                    tier=tier,
                    base_xp=base_xp,
                )
        return await _award_task_xp(
            conn_factory,
            agent_id=agent_id,
            outcome_status=outcome_status,
            tier=tier,
            base_xp=base_xp,
        )
    except Exception as exc:  # noqa: BLE001 — delivery must never wedge on RPG XP
        LOG.warning(
            "CharacterCardXpAwardFailed agent_id=%s outcome=%s err=%s",
            agent_id,
            outcome_status,
            exc,
        )
        return None


def award_task_xp_sync(
    *,
    agent_id: str,
    outcome_status: str = "success",
    tier: str | None = None,
    base_xp: int = BASE_TASK_XP,
) -> tuple[Any, CharacterCard] | None:
    """Synchronous wrapper for ``auto-runner-jira.py`` (mirrors the *_sync shape)."""
    return asyncio.run(
        award_task_xp_from_env(
            agent_id=agent_id,
            outcome_status=outcome_status,
            tier=tier,
            base_xp=base_xp,
        )
    )


def _conn_passthrough_factory(conn: Any) -> ConnFactory:
    @asynccontextmanager
    async def _factory() -> AsyncIterator[Any]:
        yield conn

    return _factory


assert_character_card_guilds_within_registry()
