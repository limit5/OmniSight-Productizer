"""RPG.W14 -- talent tree at Lv-{10,30,50,80} milestones.

ADR-0008 §"Talent tree (W14)" defines an immutable per-``(agent_id,
milestone_level)`` operator choice picked from 3 Guild-specific options
loaded out of ``config/talent_tree.yaml``. Once chosen, the talent
feeds two downstream surfaces: MP routing weight (matched tasks get a
+20% bump — feature-flagged so RPG.W7.1 can drop in cleanly) and
system-prompt enrichment (a Guild-specific reminder is appended at
task start).

This module owns:

* The YAML loader + drift guard (mirroring the W11.2 ``skill_matrix``
  pattern but for talents), with deterministic defaults for Guilds not
  explicitly curated in YAML.
* The :class:`TalentChoiceStore` Protocol with an in-memory store for
  dev / tests and a Postgres store wired to alembic 0228.
* The capstone ability store (alembic 0229) and the Lv-80 gate that
  refuses to lock a capstone before the Lv-80 milestone talent is
  picked.
* The public helper surface called out in OP-219:
  :func:`available_talents`, :func:`lock_talent`,
  :func:`agent_talent_summary`, plus capstone equivalents.

W14 sub-wave coverage in this module
------------------------------------
The W14 module shipped in one bundle under OP-219; this table tracks
the attribution of each sub-wave back to its dedicated TODO row so a
future reader of git blame can resolve a symbol to its W14.x ticket.

- W14.1 (OP-185): :data:`MILESTONE_LEVELS` +
  :func:`available_talents` + :func:`lock_talent`'s
  ``agent_level >= milestone`` gate + :class:`MilestoneNotReached` --
  ADR-0008 §"Talent tree (W14)" 's "Lv 10/30/50/80 trigger a talent
  fork" gate. The W14.5 (OP-189) Character Card modal consumes
  ``available_talents`` and posts back through ``lock_talent``.

- W14.2 (OP-186): :class:`TalentOption` +
  :func:`load_talent_tree`'s per-Guild parser +
  :data:`OPTIONS_PER_MILESTONE` + :class:`TalentIdNotInTree` --
  ADR-0008 §"Talent tree (W14)" 's "3 Guild-specific options per
  milestone declared in ``config/talent_tree.yaml``" rule, including
  the drift guard that rejects a lock whose ``talent_id`` is not
  declared in the YAML.

- W14.3 (OP-187): :class:`TalentChoice` +
  :class:`TalentChoiceStore` Protocol +
  :class:`PostgresTalentChoiceStore` + :class:`InMemoryTalentChoiceStore`
  + :class:`TalentAlreadyLocked` -- ADR-0008 §"Talent tree (W14)" 's
  ``agent_talent_choice`` table (alembic 0228) with the
  ``(agent_id, milestone_level)`` primary key and immutable-on-pick
  semantics. ``lock_talent`` is idempotent on the same ``talent_id``
  and raises ``TalentAlreadyLocked`` on a different value — operators
  must spawn a new agent instance for a fresh pick.

- W14.4 (OP-188): :data:`ROUTING_WEIGHT_TALENT_MATCH` +
  :func:`routing_weight_multiplier_for_talents` +
  :func:`prompt_reminders_for_talents` +
  :class:`RoutingWeightInjectionFailed` -- ADR-0008 §"Talent tree
  (W14)" 's "talent affects routing weight + system prompt
  enrichment" clause. The two pure compute helpers feed two
  consumer-module wiring layers:

  * :func:`backend.agents.routing_policy.talent_routing_weight_multiplier`
    + :func:`backend.agents.routing_policy.build_talent_routing_weight_resolver`
    -- the +20%-per-matching-talent multiplier, feature-flagged on
    ``OMNISIGHT_MP_TALENT_ROUTING_ENABLED`` and degrading silently to
    ``1.0`` on YAML errors.
  * :func:`backend.agents.prompt_builder.enrich_system_prompt_with_talents`
    + :func:`backend.agents.prompt_builder.build_talent_prompt_enricher`
    -- the ``Talent reminders (per RPG.W14):`` block appended at task
    start, ordered by ascending milestone so the earliest commitments
    come first.

  The ``build_talent_*`` closures take a :class:`TalentChoiceStore`
  and return an ``async (agent_id, …) -> result`` callable so dispatch
  callers don't have to hand-roll the ``store.list_choices`` lookup.

- W14.6 (OP-190): :data:`CAPSTONE_LEVEL` + :class:`CapstoneAbility` +
  :class:`CapstoneStore` + :func:`capstone_for_guild` +
  :func:`lock_capstone_ability` + :class:`CapstoneRequiresLv80` --
  ADR-0008 §"Talent tree (W14)" 's "Lv 80 unlocks a Guild signature
  ability" gate. ``lock_capstone_ability`` is gated by both
  ``agent_level >= 80`` AND the Lv-80 milestone talent being already
  locked; either gate raises ``CapstoneRequiresLv80``.

Module-global state audit (per project SOP)
-------------------------------------------
The module reads ``config/talent_tree.yaml`` lazily on each
``available_talents`` / drift call; the YAML is the source of truth
for curated Guilds and the compiled defaults cover the remaining Guild
enum members.
There is no in-process cache because the talent tree is small (Guilds
× 4 milestones × 3 options ≈ kilobytes) and we want operators editing
the YAML to see effects on the next call without a restart. Mutable
state lives entirely inside the injected store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

import yaml

from backend.sandbox_tier import Guild


ConnFactory = Callable[[], Any]


# ── Constants from ADR-0008 §"Talent tree (W14)" ───────────────────

MILESTONE_LEVELS: tuple[int, ...] = (10, 30, 50, 80)
CAPSTONE_LEVEL = 80
OPTIONS_PER_MILESTONE = 3
ROUTING_WEIGHT_TALENT_MATCH = 1.20
"""W14.4 (OP-188) — multiplier applied by MP routing_policy when a
task label matches a locked talent's ``routing_label``. ``+20%`` per
ADR-0008 §"Routing integration"; feature-flagged on
``OMNISIGHT_MP_TALENT_ROUTING_ENABLED`` so RPG.W7.1
(``prefer_agent_id``) can drop in cleanly."""

_DEFAULT_TALENT_SUFFIXES: Mapping[int, tuple[str, str, str]] = MappingProxyType(
    {
        10: ("first", "guard", "path"),
        30: ("model", "planner", "connector"),
        50: ("stabilizer", "optimizer", "observer"),
        80: ("master", "architect", "synthesizer"),
    }
)

_DEFAULT_GUILD_TALENT_FOCI: Mapping[Guild, tuple[str, ...]] = MappingProxyType(
    {
        Guild.architect: (
            "system", "adr", "tradeoff", "boundary", "roadmap", "integration",
            "migration", "resilience", "platform", "portfolio", "governance",
            "evolution",
        ),
        Guild.sa_sd: (
            "interface", "sequence", "modularity", "contract", "state",
            "dependency", "reuse", "failure", "maintainability", "domain",
            "review", "integration",
        ),
        Guild.ux: (
            "research", "accessibility", "flow", "information", "prototype",
            "content", "usability", "mobile", "handoff", "delight", "journey",
            "system",
        ),
        Guild.pm: (
            "outcome", "scope", "risk", "stakeholder", "priority",
            "acceptance", "dependency", "release", "metrics", "strategy",
            "roadmap", "feedback",
        ),
        Guild.gateway: (
            "protocol", "throttle", "routing", "a2a", "mcp", "backpressure",
            "compatibility", "observability", "failover", "traffic", "policy",
            "control",
        ),
        Guild.bsp: (
            "boot", "devicetree", "kernel", "board", "crosscompile",
            "peripheral", "bringup", "power", "storage", "secureboot",
            "factory", "recovery",
        ),
        Guild.hal: (
            "abstraction", "driver", "register", "vendor", "timing", "dma",
            "interrupt", "portability", "testbench", "lowpower",
            "compatibility", "diagnostic",
        ),
        Guild.algo_cv: (
            "dataset", "accuracy", "latency", "robustness", "calibration",
            "tracking", "benchmark", "edgecase", "pipeline", "fusion",
            "optimization", "explainability",
        ),
        Guild.optical: (
            "lens", "focus", "illumination", "calibration", "distortion",
            "thermal", "mtf", "alignment", "ircut", "tolerance", "lab",
            "field",
        ),
        Guild.isp: (
            "exposure", "color", "noise", "sharpness", "hdr", "awb", "ae",
            "af", "tuning", "artifact", "sensor", "pipeline",
        ),
        Guild.audio: (
            "aec", "noise", "latency", "codec", "beamforming", "gain",
            "wakeword", "jitter", "room", "dsp", "quality", "diagnostic",
        ),
        Guild.frontend: (
            "accessibility", "motion", "typescript", "design-system", "ssr",
            "state", "web-perf", "visual-regression", "i18n",
            "ui-architecture", "dx", "design-engineering",
        ),
        Guild.backend: (
            "schema", "performance", "security", "distributed", "data-model",
            "api-contract", "incident", "refactor", "observability", "legacy",
            "platform", "cost",
        ),
        Guild.sre: (
            "slo", "incident", "capacity", "deployment", "alert", "runbook",
            "rollback", "observability", "errorbudget", "resilience", "cost",
            "oncall",
        ),
        Guild.qa: (
            "contract", "regression", "fixture", "coverage", "e2e",
            "exploratory", "matrix", "flake", "performance", "release",
            "risk", "traceability",
        ),
        Guild.auditor: (
            "evidence", "chain", "policy", "readmodel", "control", "finding",
            "retention", "sampling", "attestation", "compliance", "exception",
            "signoff",
        ),
        Guild.red_team: (
            "injection", "authz", "exfiltration", "sandbox", "fuzzing",
            "supplychain", "bypass", "privacy", "abusecase", "persistence",
            "detection", "reporting",
        ),
        Guild.forensics: (
            "timeline", "log", "artifact", "rootcause", "containment",
            "correlation", "snapshot", "replay", "blast", "recovery",
            "lesson", "chain",
        ),
        Guild.intel: (
            "cve", "vendor", "feed", "exploit", "dependency", "threatmodel",
            "freshness", "triage", "advisory", "signal", "watchlist",
            "briefing",
        ),
        Guild.reporter: (
            "summary", "release", "changelog", "audience", "evidence",
            "timeline", "translation", "runbook", "narrative", "executive",
            "operator", "archive",
        ),
        Guild.mobile: (
            "lifecycle", "offline", "battery", "startup", "gesture",
            "permission", "crash", "deeplink", "notification", "storeops",
            "device-matrix", "release-train",
        ),
        Guild.custom: (
            "charter", "constraint", "routing", "template", "boundary",
            "handoff", "validation", "metric", "integration", "operator",
            "extension", "review",
        ),
    }
)
ROUTING_WEIGHT_CAPSTONE_MATCH = 1.50
"""Multiplier applied by MP routing_policy when a task label matches a
Lv-80 capstone's ``signature_label``. ``+50%`` — strictly larger than
the per-talent +20% from :data:`ROUTING_WEIGHT_TALENT_MATCH` so the
single-signature ability outweighs any single talent fork. Shares the
same RPG.W14 feature flag as talent routing weight; see
:func:`backend.agents.routing_policy.is_talent_routing_enabled`."""

CAPSTONE_SIGNATURE_HEADER = "Signature ability (per RPG.W14 capstone):"
"""Header line prepended to the capstone signature-ability block when
:func:`prompt_builder.enrich_system_prompt_with_capstone` injects it
into the system prompt at task start. Distinct from the per-talent
``Talent reminders`` header so operators reading the prompt log can
tell at a glance whether the agent is executing a Lv-80 signature
move."""


# ── YAML path resolution ───────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
TALENT_TREE_PATH = _REPO_ROOT / "config" / "talent_tree.yaml"


# ── Errors (from OP-219 §"Error catalog") ──────────────────────────


class TalentTreeError(RuntimeError):
    """Base class for W14 talent-tree errors."""


class TalentAlreadyLocked(TalentTreeError):
    """Raised when an operator attempts to re-write an already-locked talent."""


class MilestoneNotReached(TalentTreeError):
    """Raised when agent.level < milestone_level at lock time."""


class TalentIdNotInTree(TalentTreeError):
    """Raised when ``talent_id`` drifts from ``config/talent_tree.yaml``."""


class CapstoneRequiresLv80(TalentTreeError):
    """Raised when a capstone lock fires before the Lv-80 milestone."""


class RoutingWeightInjectionFailed(TalentTreeError):
    """Raised (and degraded silently by the call site) when MP is unreachable."""


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TalentOption:
    """One of the three Lv-N talent picks for a Guild."""

    talent_id: str
    display_name: str
    summary: str
    routing_label: str
    prompt_reminder: str


@dataclass(frozen=True)
class CapstoneAbility:
    """The single Lv-80 capstone unlock per Guild.

    ``signature_label`` is the routing keyword the MP routing policy
    matches against task labels (mirrors the per-talent
    ``routing_label`` but applied at the Guild-capstone scope).
    ``signature_prompt`` is the multi-line block that
    :mod:`backend.agents.prompt_builder` injects under
    :data:`CAPSTONE_SIGNATURE_HEADER` at task start when the agent
    holds this capstone lock. ``commit_budget`` is the operator-facing
    advisory commit ceiling (``≤ N commit``) — per ADR-0008
    §"Talent tree (W14)" the canonical capstone shape is "single
    signature ability surgical-refactor in ≤ 3 commit"; ``None`` means
    no advisory budget is asserted for this capstone.
    """

    ability_id: str
    display_name: str
    summary: str
    signature_label: str
    signature_prompt: str
    commit_budget: int | None = None


@dataclass(frozen=True)
class GuildTalentTree:
    """All milestone options + the capstone for one Guild."""

    guild: Guild
    options_by_milestone: Mapping[int, tuple[TalentOption, ...]]
    capstone: CapstoneAbility


@dataclass(frozen=True)
class TalentChoice:
    """Durable per-``(agent_id, milestone_level)`` row in ``agent_talent_choice``."""

    agent_id: str
    milestone_level: int
    talent_id: str
    chosen_at: datetime


@dataclass(frozen=True)
class CapstoneLock:
    """Durable per-agent row in ``agent_capstone_ability``."""

    agent_id: str
    ability_id: str
    locked_at: datetime


@dataclass(frozen=True)
class TalentSummary:
    """Aggregate view of every milestone choice + capstone for an agent."""

    agent_id: str
    choices: tuple[TalentChoice, ...] = ()
    capstone: CapstoneLock | None = None


# ── YAML loader + drift guard ──────────────────────────────────────


def load_talent_tree(
    path: Path | str = TALENT_TREE_PATH,
) -> Mapping[Guild, GuildTalentTree]:
    """Load and validate ``config/talent_tree.yaml``.

    Validates shape per ADR-0008: every Guild has exactly four
    milestone keys (10/30/50/80), each with exactly three options,
    plus a non-empty capstone block. Talent ids must be unique within
    a Guild (across milestones) — duplicates raise
    :class:`TalentTreeError`.
    """
    tree_path = Path(path)
    raw = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TalentTreeError("talent_tree YAML must be a mapping")
    if raw.get("schema_version") != 1:
        raise TalentTreeError("talent_tree schema_version must be 1")

    guilds_raw = raw.get("guilds")
    if not isinstance(guilds_raw, dict) or not guilds_raw:
        raise TalentTreeError("talent_tree must declare at least one guild")

    out: dict[Guild, GuildTalentTree] = {}
    for guild_name, body in guilds_raw.items():
        guild = _coerce_guild(guild_name)
        if not isinstance(body, dict):
            raise TalentTreeError(f"talent_tree {guild.value!r} body must be a mapping")
        milestones = _parse_milestones(body.get("milestones"), guild)
        capstone = _parse_capstone(body.get("capstone"), guild)
        out[guild] = GuildTalentTree(
            guild=guild,
            options_by_milestone=MappingProxyType(milestones),
            capstone=capstone,
        )
    for guild in Guild:
        out.setdefault(guild, _default_guild_talent_tree(guild))
    _assert_talent_tree_complete(out)
    _assert_global_talent_ids_unique(out)
    return MappingProxyType(out)


def available_talents(
    agent_id: str,
    guild: Guild | str,
    milestone: int,
    *,
    path: Path | str = TALENT_TREE_PATH,
) -> tuple[TalentOption, ...]:
    """Return the 3 options for a given Guild × milestone.

    Per OP-219 the call signature accepts ``agent_id`` for symmetry
    with :func:`lock_talent` even though the YAML lookup itself is
    agent-independent — keeping ``agent_id`` in the signature lets us
    add per-agent gating (cooldowns, dual-class restrictions) without
    breaking callers when those features land.
    """
    _required("agent_id", agent_id)
    guild_enum = guild if isinstance(guild, Guild) else _coerce_guild(guild)
    milestone_int = _coerce_milestone(milestone)
    tree = load_talent_tree(path)
    if guild_enum not in tree:
        raise TalentTreeError(
            f"talent_tree has no entry for guild {guild_enum.value!r}; "
            f"populate it in {TALENT_TREE_PATH.name}"
        )
    return tree[guild_enum].options_by_milestone[milestone_int]


def milestones_crossed(
    previous_level: int,
    new_level: int,
) -> tuple[int, ...]:
    """Return the milestone levels crossed by a ``previous_level → new_level`` transition.

    W14.1 (OP-185): the talent-fork trigger half of ADR-0008 §"Talent
    tree (W14)". A level transition from ``previous_level`` to
    ``new_level`` "crosses" milestone ``M`` iff ``previous_level < M <=
    new_level`` — i.e. the agent has just *reached or passed* the gate.
    The returned tuple preserves ascending milestone order so the
    earliest commitment fires first (mirrors the ordering contract of
    :func:`backend.agents.skill_leveling.unlocks_crossed`).

    Returns ``()`` for any non-increasing transition (``new_level <=
    previous_level``) — the helper is pure and never raises on this
    case so callers can ask "did the latest XP award cross a
    milestone?" without first checking direction. Demotion is not a
    real state on the W4 character-level path (XP only grows; decay
    erodes XP, not level), but the function tolerates it for symmetry
    with the skill-leveling test pattern.
    """
    if not isinstance(previous_level, int) or isinstance(previous_level, bool):
        raise TypeError("previous_level must be an int")
    if not isinstance(new_level, int) or isinstance(new_level, bool):
        raise TypeError("new_level must be an int")
    if new_level <= previous_level:
        return ()
    return tuple(
        milestone
        for milestone in MILESTONE_LEVELS
        if previous_level < milestone <= new_level
    )


def pending_milestone_forks(
    agent_level: int,
    choices: tuple[TalentChoice, ...] | tuple[int, ...],
) -> tuple[int, ...]:
    """Return milestones the agent has reached but not yet locked a talent for.

    W14.1 (OP-185): this is the "talent fork required" signal consumed
    by the Character Card "Talents" tab (W14.5 modal) and the
    ``GET /agents/{agent_id}/talents`` endpoint. A milestone ``M`` is
    *pending* iff ``agent_level >= M`` and no
    :class:`TalentChoice` row exists for ``M`` in ``choices``.

    ``choices`` accepts either a tuple of :class:`TalentChoice` rows
    (the canonical store-emitted shape from
    :meth:`TalentChoiceStore.list_choices`) or a tuple of integer
    ``milestone_level`` values (the de-normalised shape some routers
    pass after projecting the rows down).

    The returned tuple is in ascending milestone order so the UI can
    surface the earliest unresolved fork first. Returns ``()`` for any
    agent below the first milestone or whose every reached milestone is
    already locked.
    """
    if not isinstance(agent_level, int) or isinstance(agent_level, bool):
        raise TypeError("agent_level must be an int")
    if agent_level < 1:
        raise ValueError("agent_level must be >= 1")
    locked: set[int] = set()
    for entry in choices:
        if isinstance(entry, TalentChoice):
            locked.add(int(entry.milestone_level))
        elif isinstance(entry, int) and not isinstance(entry, bool):
            locked.add(entry)
        else:
            raise TypeError(
                "choices entries must be TalentChoice or int milestone_level"
            )
    return tuple(
        milestone
        for milestone in MILESTONE_LEVELS
        if agent_level >= milestone and milestone not in locked
    )


def capstone_for_guild(
    guild: Guild | str,
    *,
    path: Path | str = TALENT_TREE_PATH,
) -> CapstoneAbility:
    """Return the single Lv-80 capstone ability for ``guild``."""
    guild_enum = guild if isinstance(guild, Guild) else _coerce_guild(guild)
    tree = load_talent_tree(path)
    if guild_enum not in tree:
        raise TalentTreeError(
            f"talent_tree has no entry for guild {guild_enum.value!r}"
        )
    return tree[guild_enum].capstone


def _assert_talent_id_in_tree(
    guild: Guild,
    milestone: int,
    talent_id: str,
    *,
    path: Path | str = TALENT_TREE_PATH,
) -> TalentOption:
    options = available_talents("__validator__", guild, milestone, path=path)
    for option in options:
        if option.talent_id == talent_id:
            return option
    allowed = sorted(option.talent_id for option in options)
    raise TalentIdNotInTree(
        f"talent_id {talent_id!r} is not declared for "
        f"({guild.value}, Lv {milestone}); expected one of {allowed}"
    )


# ── Store Protocols + in-memory store ──────────────────────────────


class TalentChoiceStore(Protocol):
    async def get_choice(
        self, agent_id: str, milestone_level: int
    ) -> TalentChoice | None: ...
    async def list_choices(
        self, agent_id: str
    ) -> tuple[TalentChoice, ...]: ...
    async def upsert_choice(self, choice: TalentChoice) -> TalentChoice: ...


class CapstoneStore(Protocol):
    async def get_capstone(self, agent_id: str) -> CapstoneLock | None: ...
    async def upsert_capstone(self, lock: CapstoneLock) -> CapstoneLock: ...


class InMemoryTalentChoiceStore:
    """Dev / test store."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, int], TalentChoice] = {}

    async def get_choice(
        self, agent_id: str, milestone_level: int
    ) -> TalentChoice | None:
        return self._rows.get((agent_id, milestone_level))

    async def list_choices(
        self, agent_id: str
    ) -> tuple[TalentChoice, ...]:
        return tuple(
            row
            for key, row in self._rows.items()
            if key[0] == agent_id
        )

    async def upsert_choice(self, choice: TalentChoice) -> TalentChoice:
        key = (choice.agent_id, choice.milestone_level)
        existing = self._rows.get(key)
        if existing is not None:
            return existing
        self._rows[key] = choice
        return choice


class InMemoryCapstoneStore:
    """Dev / test store for the Lv-80 capstone lock."""

    def __init__(self) -> None:
        self._rows: dict[str, CapstoneLock] = {}

    async def get_capstone(self, agent_id: str) -> CapstoneLock | None:
        return self._rows.get(agent_id)

    async def upsert_capstone(self, lock: CapstoneLock) -> CapstoneLock:
        self._rows[lock.agent_id] = lock
        return lock


# ── Postgres stores ─────────────────────────────────────────────────


class PostgresTalentChoiceStore:
    """``agent_talent_choice``-backed store (alembic 0228)."""

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def get_choice(
        self, agent_id: str, milestone_level: int
    ) -> TalentChoice | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT agent_id, milestone_level, talent_id, chosen_at
                FROM agent_talent_choice
                WHERE agent_id = $1 AND milestone_level = $2
                """,
                _required("agent_id", agent_id),
                _coerce_milestone(milestone_level),
            )
        return _row_to_choice(row) if row else None

    async def list_choices(
        self, agent_id: str
    ) -> tuple[TalentChoice, ...]:
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                """
                SELECT agent_id, milestone_level, talent_id, chosen_at
                FROM agent_talent_choice
                WHERE agent_id = $1
                ORDER BY milestone_level ASC
                """,
                _required("agent_id", agent_id),
            )
        return tuple(_row_to_choice(row) for row in rows)

    async def upsert_choice(self, choice: TalentChoice) -> TalentChoice:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_talent_choice (
                    agent_id, milestone_level, talent_id, chosen_at,
                    created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, NOW(), NOW())
                ON CONFLICT (agent_id, milestone_level) DO NOTHING
                RETURNING agent_id, milestone_level, talent_id, chosen_at
                """,
                choice.agent_id,
                choice.milestone_level,
                choice.talent_id,
                choice.chosen_at,
            )
            if row is None:
                row = await conn.fetchrow(
                    """
                    SELECT agent_id, milestone_level, talent_id, chosen_at
                    FROM agent_talent_choice
                    WHERE agent_id = $1 AND milestone_level = $2
                    """,
                    choice.agent_id,
                    choice.milestone_level,
                )
        return _row_to_choice(row)


class PostgresCapstoneStore:
    """``agent_capstone_ability``-backed store (alembic 0229)."""

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def get_capstone(self, agent_id: str) -> CapstoneLock | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT agent_id, ability_id, locked_at
                FROM agent_capstone_ability
                WHERE agent_id = $1
                """,
                _required("agent_id", agent_id),
            )
        return _row_to_capstone(row) if row else None

    async def upsert_capstone(self, lock: CapstoneLock) -> CapstoneLock:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_capstone_ability (
                    agent_id, ability_id, locked_at, created_at, updated_at
                )
                VALUES ($1, $2, $3, NOW(), NOW())
                ON CONFLICT (agent_id) DO UPDATE
                    SET ability_id = EXCLUDED.ability_id,
                        locked_at = EXCLUDED.locked_at,
                        updated_at = NOW()
                RETURNING agent_id, ability_id, locked_at
                """,
                lock.agent_id,
                lock.ability_id,
                lock.locked_at,
            )
        return _row_to_capstone(row)


# ── Public operations ───────────────────────────────────────────────


async def lock_talent(
    store: TalentChoiceStore,
    agent_id: str,
    guild: Guild | str,
    milestone: int,
    talent_id: str,
    *,
    agent_level: int,
    now: datetime | None = None,
    path: Path | str = TALENT_TREE_PATH,
) -> TalentChoice:
    """Persist an immutable talent choice for ``(agent_id, milestone)``.

    Idempotent on the *same* talent (returns the existing row unchanged)
    and refuses to overwrite a *different* talent with
    :class:`TalentAlreadyLocked` — operators must spawn a new agent
    instance for a fresh pick.

    Gates
    -----
    * ``agent_level >= milestone`` — :class:`MilestoneNotReached` otherwise.
    * ``talent_id`` must appear in the Guild × milestone block —
      :class:`TalentIdNotInTree` otherwise (drift guard vs YAML).
    """
    agent_id = _required("agent_id", agent_id)
    guild_enum = guild if isinstance(guild, Guild) else _coerce_guild(guild)
    milestone_int = _coerce_milestone(milestone)
    talent_id = _required("talent_id", talent_id)
    if not isinstance(agent_level, int) or isinstance(agent_level, bool):
        raise TypeError("agent_level must be an int")
    if agent_level < milestone_int:
        raise MilestoneNotReached(
            f"agent {agent_id!r} is Lv {agent_level}; "
            f"talent lock requires Lv {milestone_int}+"
        )
    _assert_talent_id_in_tree(guild_enum, milestone_int, talent_id, path=path)

    existing = await store.get_choice(agent_id, milestone_int)
    if existing is not None and existing.talent_id != talent_id:
        raise TalentAlreadyLocked(
            f"talent already locked to {existing.talent_id!r} for "
            f"({agent_id}, Lv {milestone_int}); refusing to re-write to {talent_id!r}"
        )
    if existing is not None and existing.talent_id == talent_id:
        return existing
    choice = TalentChoice(
        agent_id=agent_id,
        milestone_level=milestone_int,
        talent_id=talent_id,
        chosen_at=_utc(now or datetime.now(timezone.utc)),
    )
    return await store.upsert_choice(choice)


async def lock_capstone_ability(
    store: CapstoneStore,
    talent_store: TalentChoiceStore,
    agent_id: str,
    guild: Guild | str,
    *,
    agent_level: int,
    now: datetime | None = None,
    path: Path | str = TALENT_TREE_PATH,
) -> CapstoneLock:
    """Lock the Lv-80 capstone ability for ``agent_id``.

    Per ADR-0008 the capstone is gated on:
      1. agent_level >= 80
      2. the Lv-80 milestone talent already being locked (final pick)

    Both gates raise :class:`CapstoneRequiresLv80`.
    """
    agent_id = _required("agent_id", agent_id)
    guild_enum = guild if isinstance(guild, Guild) else _coerce_guild(guild)
    if not isinstance(agent_level, int) or isinstance(agent_level, bool):
        raise TypeError("agent_level must be an int")
    if agent_level < CAPSTONE_LEVEL:
        raise CapstoneRequiresLv80(
            f"agent {agent_id!r} is Lv {agent_level}; capstone requires Lv {CAPSTONE_LEVEL}+"
        )
    lv80_choice = await talent_store.get_choice(agent_id, CAPSTONE_LEVEL)
    if lv80_choice is None:
        raise CapstoneRequiresLv80(
            f"agent {agent_id!r} has not locked their Lv-{CAPSTONE_LEVEL} "
            "milestone talent yet; capstone is gated on the final pick"
        )

    capstone = capstone_for_guild(guild_enum, path=path)
    existing = await store.get_capstone(agent_id)
    if existing is not None and existing.ability_id != capstone.ability_id:
        # The Guild rotated under the agent — refuse rather than silently
        # overwrite the historic capstone row.
        raise TalentAlreadyLocked(
            f"capstone already locked to {existing.ability_id!r} for {agent_id!r}; "
            f"refusing to re-write to {capstone.ability_id!r}"
        )
    if existing is not None:
        return existing
    lock = CapstoneLock(
        agent_id=agent_id,
        ability_id=capstone.ability_id,
        locked_at=_utc(now or datetime.now(timezone.utc)),
    )
    return await store.upsert_capstone(lock)


async def agent_talent_summary(
    store: TalentChoiceStore,
    agent_id: str,
    *,
    capstone_store: CapstoneStore | None = None,
) -> TalentSummary:
    """Return the agent's full talent chain (every milestone + capstone)."""
    agent_id = _required("agent_id", agent_id)
    choices = await store.list_choices(agent_id)
    capstone = (
        await capstone_store.get_capstone(agent_id)
        if capstone_store is not None
        else None
    )
    return TalentSummary(agent_id=agent_id, choices=choices, capstone=capstone)


def routing_weight_multiplier_for_talents(
    choices: tuple[TalentChoice, ...],
    *,
    task_labels: tuple[str, ...],
    path: Path | str = TALENT_TREE_PATH,
    guild: Guild | str | None = None,
) -> float:
    """W14.4 (OP-188) -- return the routing-weight multiplier for ``choices``.

    Each ``choice.talent_id`` is resolved against ``config/talent_tree.yaml``
    to find its ``routing_label``. Every choice whose label appears in
    ``task_labels`` contributes :data:`ROUTING_WEIGHT_TALENT_MATCH`.

    The function is pure — MP routing_policy callers invoke it
    behind the :func:`backend.agents.routing_policy.is_talent_routing_enabled`
    feature flag and degrade silently
    (:class:`RoutingWeightInjectionFailed`) on import/IO errors. ``guild``
    is optional: when omitted we scan every Guild's tree (cheap, 21
    guilds × 4 milestones × 3 options today).

    See :func:`backend.agents.routing_policy.build_talent_routing_weight_resolver`
    for the W14.4 dispatcher-wiring helper that fetches an agent's
    talent choices from the store and applies this multiplier in one
    call — production callers reach for that closure rather than
    composing ``store.list_choices`` + this helper themselves.
    """
    if not choices or not task_labels:
        return 1.0
    try:
        tree = load_talent_tree(path)
    except TalentTreeError as exc:  # pragma: no cover — defensive
        raise RoutingWeightInjectionFailed(str(exc)) from exc
    label_set = {label.strip().lower() for label in task_labels if label}
    multiplier = 1.0
    talents_to_match = {choice.talent_id for choice in choices}
    guilds_to_scan: tuple[Guild, ...]
    if guild is None:
        guilds_to_scan = tuple(tree.keys())
    else:
        guild_enum = guild if isinstance(guild, Guild) else _coerce_guild(guild)
        guilds_to_scan = (guild_enum,) if guild_enum in tree else ()
    for g in guilds_to_scan:
        for options in tree[g].options_by_milestone.values():
            for option in options:
                if option.talent_id not in talents_to_match:
                    continue
                if option.routing_label.strip().lower() in label_set:
                    multiplier *= ROUTING_WEIGHT_TALENT_MATCH
    return multiplier


def capstone_signature_block(ability: CapstoneAbility) -> str:
    """Return the prompt block that anchors the Lv-80 signature ability.

    Composed of ``display_name`` (one line), ``signature_prompt`` body,
    and the operator-facing ``≤ N commit`` advisory when
    ``commit_budget`` is set. Pure function — :mod:`prompt_builder`
    prepends :data:`CAPSTONE_SIGNATURE_HEADER` when it injects this
    into the system prompt.
    """
    body = ability.signature_prompt.strip()
    lines = [f"{ability.display_name} — {ability.summary.strip()}", body]
    if ability.commit_budget is not None:
        lines.append(
            f"Commit budget: ≤ {ability.commit_budget} commit "
            f"(surgical refactor discipline; bundle related edits into the "
            f"same change rather than splitting into many small commits)."
        )
    return "\n\n".join(line for line in lines if line)


def capstone_matches_task_labels(
    ability: CapstoneAbility,
    task_labels: tuple[str, ...],
) -> bool:
    """Return whether ``ability.signature_label`` appears in ``task_labels``.

    Case-insensitive (mirrors the per-talent routing match in
    :func:`routing_weight_multiplier_for_talents`). Pure / cheap: the
    MP routing policy can call this for every locked capstone in the
    candidate set without YAML I/O.
    """
    if not task_labels:
        return False
    label = ability.signature_label.strip().lower()
    if not label:
        return False
    return any(
        isinstance(item, str) and item.strip().lower() == label
        for item in task_labels
    )


def capstone_routing_weight_multiplier(
    capstone: CapstoneLock | None,
    *,
    task_labels: tuple[str, ...],
    guild: Guild | str | None = None,
    path: Path | str = TALENT_TREE_PATH,
) -> float:
    """Return the routing-weight multiplier contributed by ``capstone``.

    ``None`` (no capstone locked) or empty ``task_labels`` → ``1.0``.
    The lock's ``ability_id`` is resolved against
    ``config/talent_tree.yaml``: when ``guild`` is given we scan only
    that Guild's capstone (the common case — routing already knows
    the candidate's Guild); when ``guild`` is omitted we scan all
    Guild capstones (cheap, ≤ 2 today).

    Pure — feature-gate at the routing_policy call site, not here.
    YAML/IO errors raise :class:`RoutingWeightInjectionFailed` so the
    caller can degrade silently per ADR-0008 §"Error catalog".
    """
    if capstone is None or not task_labels:
        return 1.0
    try:
        tree = load_talent_tree(path)
    except TalentTreeError as exc:  # pragma: no cover — defensive
        raise RoutingWeightInjectionFailed(str(exc)) from exc
    guilds_to_scan: tuple[Guild, ...]
    if guild is None:
        guilds_to_scan = tuple(tree.keys())
    else:
        guild_enum = guild if isinstance(guild, Guild) else _coerce_guild(guild)
        guilds_to_scan = (guild_enum,) if guild_enum in tree else ()
    for g in guilds_to_scan:
        ability = tree[g].capstone
        if ability.ability_id != capstone.ability_id:
            continue
        if capstone_matches_task_labels(ability, task_labels):
            return ROUTING_WEIGHT_CAPSTONE_MATCH
    return 1.0


def prompt_reminders_for_talents(
    choices: tuple[TalentChoice, ...],
    *,
    path: Path | str = TALENT_TREE_PATH,
    guild: Guild | str | None = None,
) -> tuple[str, ...]:
    """W14.4 (OP-188) -- prompt-reminder strings for every locked talent.

    Used by :func:`backend.agents.prompt_builder.enrich_system_prompt_with_talents`
    to enrich the system prompt at task start. Order is by ascending
    milestone level so the earliest commitments (Lv 10) come first.

    See :func:`backend.agents.prompt_builder.build_talent_prompt_enricher`
    for the W14.4 dispatcher-wiring closure that fetches an agent's
    talent choices from the store and applies the enrichment in one
    call.
    """
    if not choices:
        return ()
    tree = load_talent_tree(path)
    talent_index: dict[str, TalentOption] = {}
    if guild is None:
        guilds_to_scan: tuple[Guild, ...] = tuple(tree.keys())
    else:
        guild_enum = guild if isinstance(guild, Guild) else _coerce_guild(guild)
        guilds_to_scan = (guild_enum,) if guild_enum in tree else ()
    for g in guilds_to_scan:
        for options in tree[g].options_by_milestone.values():
            for option in options:
                talent_index[option.talent_id] = option
    sorted_choices = sorted(choices, key=lambda c: c.milestone_level)
    reminders: list[str] = []
    for choice in sorted_choices:
        option = talent_index.get(choice.talent_id)
        if option is None:
            continue
        reminders.append(option.prompt_reminder.strip())
    return tuple(reminders)


# ── Internal helpers ────────────────────────────────────────────────


@asynccontextmanager
async def _acquire(factory: ConnFactory) -> AsyncIterator[Any]:
    cm = factory()
    async with cm as conn:
        yield conn


def _row_to_choice(row: Any) -> TalentChoice:
    return TalentChoice(
        agent_id=row["agent_id"],
        milestone_level=int(row["milestone_level"]),
        talent_id=row["talent_id"],
        chosen_at=_utc(row["chosen_at"]),
    )


def _row_to_capstone(row: Any) -> CapstoneLock:
    return CapstoneLock(
        agent_id=row["agent_id"],
        ability_id=row["ability_id"],
        locked_at=_utc(row["locked_at"]),
    )


def _required(field_name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} is required")
    return clean


def _coerce_guild(value: Any) -> Guild:
    if isinstance(value, Guild):
        return value
    if not isinstance(value, str):
        raise TalentTreeError("guild must be a string or Guild enum")
    try:
        return Guild(value.strip())
    except ValueError as exc:
        raise TalentTreeError(f"unknown guild: {value!r}") from exc


def _coerce_milestone(value: Any) -> int:
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError as exc:
            raise TalentTreeError(f"milestone must be an integer; got {value!r}") from exc
    if not isinstance(value, int) or isinstance(value, bool):
        raise TalentTreeError("milestone must be an integer")
    if value not in MILESTONE_LEVELS:
        raise TalentTreeError(
            f"milestone must be one of {list(MILESTONE_LEVELS)}; got {value}"
        )
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_milestones(
    raw: Any,
    guild: Guild,
) -> dict[int, tuple[TalentOption, ...]]:
    if not isinstance(raw, dict) or not raw:
        raise TalentTreeError(
            f"talent_tree {guild.value!r} must declare a non-empty milestones mapping"
        )
    parsed: dict[int, tuple[TalentOption, ...]] = {}
    keys_seen: set[int] = set()
    for key, options in raw.items():
        milestone = _coerce_milestone(key)
        if milestone in keys_seen:
            raise TalentTreeError(
                f"talent_tree {guild.value!r} has duplicate milestone {milestone}"
            )
        keys_seen.add(milestone)
        parsed[milestone] = _parse_options(options, guild, milestone)
    missing = sorted(set(MILESTONE_LEVELS) - keys_seen)
    if missing:
        raise TalentTreeError(
            f"talent_tree {guild.value!r} is missing milestones {missing}"
        )
    extra = sorted(keys_seen - set(MILESTONE_LEVELS))
    if extra:  # pragma: no cover — _coerce_milestone already rejects extras
        raise TalentTreeError(
            f"talent_tree {guild.value!r} has unexpected milestones {extra}"
        )
    # Drift guard: every talent_id within a Guild must be unique across
    # milestones — otherwise routing_label / prompt_reminder lookups
    # become ambiguous.
    seen_ids: set[str] = set()
    for options in parsed.values():
        for option in options:
            if option.talent_id in seen_ids:
                raise TalentTreeError(
                    f"talent_tree {guild.value!r} has duplicate talent_id "
                    f"{option.talent_id!r}"
                )
            seen_ids.add(option.talent_id)
    return parsed


def _parse_options(
    raw: Any,
    guild: Guild,
    milestone: int,
) -> tuple[TalentOption, ...]:
    if not isinstance(raw, list):
        raise TalentTreeError(
            f"talent_tree {guild.value!r} Lv {milestone} options must be a list"
        )
    if len(raw) != OPTIONS_PER_MILESTONE:
        raise TalentTreeError(
            f"talent_tree {guild.value!r} Lv {milestone} must have exactly "
            f"{OPTIONS_PER_MILESTONE} options; got {len(raw)}"
        )
    parsed: list[TalentOption] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise TalentTreeError(
                f"talent_tree {guild.value!r} Lv {milestone} entry must be a mapping"
            )
        option = TalentOption(
            talent_id=_required_text(entry.get("talent_id"), "talent_id"),
            display_name=_required_text(entry.get("display_name"), "display_name"),
            summary=_required_text(entry.get("summary"), "summary"),
            routing_label=_required_text(entry.get("routing_label"), "routing_label"),
            prompt_reminder=_required_text(
                entry.get("prompt_reminder"), "prompt_reminder"
            ),
        )
        if option.talent_id in seen:
            raise TalentTreeError(
                f"talent_tree {guild.value!r} Lv {milestone} duplicates "
                f"talent_id {option.talent_id!r}"
            )
        seen.add(option.talent_id)
        parsed.append(option)
    return tuple(parsed)


def _parse_capstone(raw: Any, guild: Guild) -> CapstoneAbility:
    if not isinstance(raw, dict):
        raise TalentTreeError(
            f"talent_tree {guild.value!r} must declare a capstone block"
        )
    ability_id = _required_text(raw.get("ability_id"), "ability_id")
    display_name = _required_text(raw.get("display_name"), "display_name")
    summary = _required_text(raw.get("summary"), "summary")
    # RPG.W14.6 — the signature-ability runtime surface is required on
    # every capstone so prompt enrichment + routing weight have a
    # deterministic input. Default fallbacks live here (not in the
    # dataclass) so the YAML drift guard catches missing fields at
    # boot rather than silently shipping a no-op capstone.
    signature_label = raw.get("signature_label")
    if signature_label is None:
        # Backwards-compat fallback: derive from ability_id so older
        # capstone blocks keep loading. Operators get a deterministic
        # routing label without having to touch the YAML, but the
        # ADR-0008 W14.6 contract still expects explicit declarations
        # going forward.
        signature_label = ability_id.replace("_", "-")
    else:
        signature_label = _required_text(signature_label, "signature_label")
    signature_prompt = raw.get("signature_prompt")
    if signature_prompt is None:
        # Backwards-compat fallback: reuse summary so the prompt
        # injector still has something to write under the header.
        signature_prompt = summary
    else:
        signature_prompt = _required_text(signature_prompt, "signature_prompt")
    commit_budget_raw = raw.get("commit_budget")
    commit_budget: int | None
    if commit_budget_raw is None:
        commit_budget = None
    elif isinstance(commit_budget_raw, int) and not isinstance(commit_budget_raw, bool):
        if commit_budget_raw <= 0:
            raise TalentTreeError(
                f"talent_tree {guild.value!r} capstone commit_budget must be positive"
            )
        commit_budget = commit_budget_raw
    else:
        raise TalentTreeError(
            f"talent_tree {guild.value!r} capstone commit_budget must be a "
            f"positive integer or omitted"
        )
    return CapstoneAbility(
        ability_id=ability_id,
        display_name=display_name,
        summary=summary,
        signature_label=signature_label,
        signature_prompt=signature_prompt,
        commit_budget=commit_budget,
    )


def _default_guild_talent_tree(guild: Guild) -> GuildTalentTree:
    """Build deterministic default talent options for Guilds not in YAML."""
    foci = _DEFAULT_GUILD_TALENT_FOCI[guild]
    options_by_milestone: dict[int, tuple[TalentOption, ...]] = {}
    focus_offset = 0
    for milestone in MILESTONE_LEVELS:
        suffixes = _DEFAULT_TALENT_SUFFIXES[milestone]
        options: list[TalentOption] = []
        for suffix in suffixes:
            focus = foci[focus_offset]
            focus_offset += 1
            option_slug = f"{_slug(guild.value)}-{_slug(focus)}-{suffix}"
            options.append(
                TalentOption(
                    talent_id=option_slug,
                    display_name=_title(f"{focus} {suffix}"),
                    summary=(
                        f"Bias toward {_words(focus)} decisions for the "
                        f"{_title(guild.value)} Guild."
                    ),
                    routing_label=_slug(focus),
                    prompt_reminder=(
                        f"For {_words(guild.value)} work, foreground "
                        f"{_words(focus)} tradeoffs before choosing an "
                        "implementation path."
                    ),
                )
            )
        options_by_milestone[milestone] = tuple(options)
    return GuildTalentTree(
        guild=guild,
        options_by_milestone=MappingProxyType(options_by_milestone),
        capstone=CapstoneAbility(
            ability_id=f"{_slug(guild.value)}-capstone",
            display_name=f"{_title(guild.value)} Capstone",
            summary=(
                f"Dedicated Lv-80 capstone for the {_words(guild.value)} "
                "Guild, reserved for high-context specialist execution."
            ),
            signature_label=f"{_slug(guild.value)}-capstone",
            signature_prompt=(
                f"Signature ability of the {_title(guild.value)} Guild: "
                f"apply full {_words(guild.value)} specialist depth to "
                "this task — state the plan, execute surgically, and "
                "verify the outcome before handing off."
            ),
        ),
    )


def _assert_talent_tree_complete(tree: Mapping[Guild, GuildTalentTree]) -> None:
    missing = sorted(guild.value for guild in set(Guild) - set(tree))
    if missing:
        raise TalentTreeError(f"talent_tree missing Guild defaults: {missing}")


def _assert_global_talent_ids_unique(
    tree: Mapping[Guild, GuildTalentTree],
) -> None:
    seen: dict[str, Guild] = {}
    for guild, guild_tree in tree.items():
        for options in guild_tree.options_by_milestone.values():
            for option in options:
                owner = seen.setdefault(option.talent_id, guild)
                if owner != guild:
                    raise TalentTreeError(
                        f"talent_tree talent_id {option.talent_id!r} is reused "
                        f"by both {owner.value!r} and {guild.value!r}"
                    )


def _slug(value: str) -> str:
    return value.replace("_", "-").lower()


def _words(value: str) -> str:
    return value.replace("_", " ").replace("-", " ")


def _title(value: str) -> str:
    acronyms = {
        "a2a",
        "adr",
        "ae",
        "aec",
        "af",
        "awb",
        "cve",
        "dma",
        "dsp",
        "e2e",
        "hdr",
        "i18n",
        "ircut",
        "mcp",
        "mtf",
        "slo",
        "ssr",
    }
    words: list[str] = []
    for word in _words(value).split():
        words.append(word.upper() if word.lower() in acronyms else word.title())
    return " ".join(words)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TalentTreeError(f"talent_tree {field_name} must be a string")
    clean = value.strip()
    if not clean:
        raise TalentTreeError(f"talent_tree {field_name} must be non-empty")
    return clean


__all__ = [
    "CAPSTONE_LEVEL",
    "CAPSTONE_SIGNATURE_HEADER",
    "CapstoneAbility",
    "CapstoneLock",
    "CapstoneRequiresLv80",
    "CapstoneStore",
    "GuildTalentTree",
    "InMemoryCapstoneStore",
    "InMemoryTalentChoiceStore",
    "MILESTONE_LEVELS",
    "MilestoneNotReached",
    "OPTIONS_PER_MILESTONE",
    "PostgresCapstoneStore",
    "PostgresTalentChoiceStore",
    "ROUTING_WEIGHT_CAPSTONE_MATCH",
    "ROUTING_WEIGHT_TALENT_MATCH",
    "RoutingWeightInjectionFailed",
    "TALENT_TREE_PATH",
    "TalentAlreadyLocked",
    "TalentChoice",
    "TalentChoiceStore",
    "TalentIdNotInTree",
    "TalentOption",
    "TalentSummary",
    "TalentTreeError",
    "agent_talent_summary",
    "available_talents",
    "capstone_for_guild",
    "capstone_matches_task_labels",
    "capstone_routing_weight_multiplier",
    "capstone_signature_block",
    "load_talent_tree",
    "lock_capstone_ability",
    "lock_talent",
    "milestones_crossed",
    "pending_milestone_forks",
    "prompt_reminders_for_talents",
    "routing_weight_multiplier_for_talents",
]
