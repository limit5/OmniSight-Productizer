"""RPG character registry — the character→(brain, guild, tier) source of truth.

This is the first structural step of the "un-weld" (RPG north-star): today a JIRA
ticket carries a ``class:subscription-<brain>`` label and routing is 2-way welded
(brain × slot). A *character* is a named, reusable persona that OWNS the task and
bears the RPG progression (``agent_character_card`` keyed by the character slug),
while RESOLVING to a brain (which CLI runs + which bot pushes) and a capability
tier ceiling. Physically the work still runs on a free runner SLOT of that brain's
family (a claude character can only run on a claude slot — the CLI + push identity
are brain-bound), but the character is now the first-class owner + stats-bearer +
tier gate, instead of the bare bot account.

Design invariants:

* ``brain`` MUST be a known agent_class (``jira_dispatch._BASE_BOT_BY_CLASS``) so
  the derived ``class:<brain>`` label routes to a real runner + push identity.
* ``guild`` MUST be a known guild slug (``guild_registry.GUILDS``).
* ``max_tier`` is the character's capability ceiling (S<M<L<X) — this is what
  makes "varying-capability characters" real: a cheap brain on a low tier only
  takes small tasks; an elite brain on a high tier takes the hard ones.

The registry is ADDITIVE and back-compatible: a ticket filed with
``character:<slug>`` also carries the derived ``class:<brain>`` so the existing
class-only pickup path is unchanged; the character label only adds ownership +
the tier ceiling on top.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, replace
from typing import Mapping

from backend.agents import jira_dispatch
from backend.agents.guild_registry import GUILDS

log = logging.getLogger(__name__)

# Tier ordering shared with capability_registry.TIER_ORDER (S<M<L<X).
TIER_ORDER: Mapping[str, int] = {"S": 0, "M": 1, "L": 2, "X": 3}

# DB-loader policy knobs (character-recruit design §C2, codex rounds 1+2).
_DB_CONNECT_TIMEOUT_SECONDS = 3
_DB_TTL_BASE_SECONDS = 30.0
_DB_TTL_JITTER_SECONDS = 10.0
# Past this bound a stale-if-error snapshot no longer authorises NEW pickups
# of DB-only characters ("registry stale" denial); built-ins and in-flight
# resolution keep working on the stale snapshot.
MAX_STALE_AGE_SECONDS = 15 * 60.0


class CharacterRegistryError(RuntimeError):
    """A character definition is malformed or unknown."""


@dataclass(frozen=True)
class CharacterDef:
    """A reusable RPG persona bound to a brain + guild + tier ceiling."""

    slug: str
    display_name: str
    brain: str          # agent_class, e.g. "subscription-claude"
    guild: str          # guild_registry slug
    max_tier: str       # capability ceiling S<M<L<X
    blurb: str = ""
    # False = retired: still RESOLVABLE (in-flight card/skill/XP finalization
    # keeps working) but denied for NEW pickups and hidden from filing paths.
    active: bool = True

    def __post_init__(self) -> None:
        if self.brain not in jira_dispatch._BASE_BOT_BY_CLASS:
            raise CharacterRegistryError(
                f"character {self.slug!r} brain {self.brain!r} is not a known "
                f"agent_class {sorted(jira_dispatch._BASE_BOT_BY_CLASS)}"
            )
        if self.guild not in GUILDS:
            raise CharacterRegistryError(
                f"character {self.slug!r} guild {self.guild!r} is not a known "
                f"guild slug"
            )
        if self.max_tier not in TIER_ORDER:
            raise CharacterRegistryError(
                f"character {self.slug!r} max_tier {self.max_tier!r} not in "
                f"{sorted(TIER_ORDER)}"
            )


# Starter roster — one persona per live brain, spanning the capability spread
# (cheap grok on tier S → elite claude on tier L). Grows as the operator assembles
# their preferred combos; a brain may host several characters over time.
CHARACTERS: dict[str, CharacterDef] = {
    c.slug: c
    for c in (
        CharacterDef(
            slug="nova", display_name="Nova", brain="subscription-claude",
            guild="backend", max_tier="L",
            blurb="Elite backend architect — deep, high-tier work.",
        ),
        CharacterDef(
            slug="pixel", display_name="Pixel", brain="subscription-codex",
            guild="frontend", max_tier="M",
            blurb="Frontend/UI specialist.",
        ),
        CharacterDef(
            slug="sage", display_name="Sage", brain="subscription-gemini",
            guild="backend", max_tier="M",
            blurb="Versatile backend generalist.",
        ),
        CharacterDef(
            slug="rex", display_name="Rex", brain="subscription-grok",
            guild="sre", max_tier="S",
            blurb="Fast, cheap tooling/ops hand — small self-contained tasks.",
        ),
    )
}


# ── DB-backed roster (RECRUIT C2) ─────────────────────────────────
#
# The effective roster is CHARACTERS (code built-ins) merged with the
# ``character_def`` table, cached per process behind a jittered TTL with a
# stale-if-error policy. See docs/architecture/2026-07-02-character-recruit-
# epic-design.md §C2 — its policies are the spec.

_DB_ROW_FIELDS = ("slug", "display_name", "brain", "guild", "max_tier", "blurb", "active")
_SHADOW_COMPARE_FIELDS = ("display_name", "brain", "guild", "max_tier", "blurb", "active")


@dataclass(frozen=True)
class _RosterSnapshot:
    characters: dict[str, CharacterDef]  # merged roster, retired-INCLUSIVE
    db_loaded: bool                      # a character_def read ever succeeded
    fetched_at: float | None             # monotonic ts of the last-good DB read
    next_refresh_at: float               # jittered-TTL expiry (monotonic)


_SNAPSHOT: _RosterSnapshot | None = None


def _now() -> float:
    return time.monotonic()


def _next_refresh_at(now: float) -> float:
    # Jittered TTL so concurrent runner processes never align into refresh
    # bursts against the shared DB.
    return now + _DB_TTL_BASE_SECONDS + random.uniform(
        -_DB_TTL_JITTER_SECONDS, _DB_TTL_JITTER_SECONDS
    )


def _fetch_character_rows() -> list[dict]:
    """Sync psycopg2 read of all ``character_def`` rows.

    Raises on ANY failure (missing psycopg2, no DSN, connect/query error);
    :func:`_current_snapshot` translates the failure into the stale-if-error /
    built-ins-only policy — a registry consumer never sees a DB failure raise.
    """
    import psycopg2  # lazy: the registry must import fine without DB deps

    # Same DSN resolution as the other sync runner-side reads.
    from backend.agents.provider_quota_tracker import _resolve_dsn

    dsn = _resolve_dsn()
    if not dsn:
        raise CharacterRegistryError(
            "no PostgreSQL DSN via OMNISIGHT_DATABASE_URL / DATABASE_URL / "
            "OMNI_TEST_PG_URL"
        )
    conn = psycopg2.connect(dsn, connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT slug, display_name, brain, guild, max_tier, blurb, "
                    "active FROM character_def ORDER BY slug"
                )
                return [dict(zip(_DB_ROW_FIELDS, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _merge_db_rows(rows: list[dict]) -> dict[str, CharacterDef]:
    """Merge DB rows over the built-ins (shadow-protected, bad rows skipped)."""
    merged = dict(CHARACTERS)
    for row in rows:
        slug = str(row.get("slug") or "").strip()
        if not slug:
            log.warning(
                "character_registry: skipping character_def row with empty slug: %r",
                row,
            )
            continue
        if slug in CHARACTERS:
            # Shadow-protected built-in: the code constant ALWAYS wins. An
            # exact copy (the 0253 seed) is ignored silently; a divergent row
            # is rejected loudly so a DB edit can never re-point a built-in.
            builtin = CHARACTERS[slug]
            divergent = sorted(
                f for f in _SHADOW_COMPARE_FIELDS if row.get(f) != getattr(builtin, f)
            )
            if divergent:
                log.warning(
                    "character_registry: character_def row for built-in %r "
                    "diverges in %s — row REJECTED, code constant wins "
                    "(built-ins are shadow-protected)",
                    slug, divergent,
                )
            continue
        try:
            merged[slug] = CharacterDef(
                slug=slug,
                display_name=str(row.get("display_name") or ""),
                brain=str(row.get("brain") or ""),
                guild=str(row.get("guild") or ""),
                max_tier=str(row.get("max_tier") or ""),
                blurb=str(row.get("blurb") or ""),
                active=bool(row.get("active", True)),
            )
        except CharacterRegistryError as exc:
            log.warning(
                "character_registry: skipping bad character_def row %r: %s",
                slug, exc,
            )
    return merged


def _current_snapshot() -> _RosterSnapshot:
    """Return the roster snapshot, refreshing from the DB when the TTL expired.

    Stale-if-error: a refresh failure NEVER replaces a previously-good DB
    roster with built-ins (a DB flap mid-poll must not silently convert
    character-owned pickups into bot-owned work) — the last-good snapshot is
    kept, its age logged, and the fetch retried next TTL. Built-ins-only
    happens ONLY on cold start with no reachable DB.
    """
    global _SNAPSHOT
    now = _now()
    snap = _SNAPSHOT
    if snap is not None and now < snap.next_refresh_at:
        return snap
    try:
        rows = _fetch_character_rows()
    except Exception as exc:  # noqa: BLE001 — any DB failure feeds the policy
        if snap is not None and snap.db_loaded:
            age = now - (snap.fetched_at or now)
            log.warning(
                "character_registry: character_def refresh failed (%s); keeping "
                "last-good DB snapshot age=%.0fs%s",
                exc, age,
                " — BEYOND max stale age; DB-only characters are pickup-denied"
                if age > MAX_STALE_AGE_SECONDS else "",
            )
            _SNAPSHOT = replace(snap, next_refresh_at=_next_refresh_at(now))
        else:
            log.warning(
                "character_registry: character_def unavailable on cold start "
                "(%s); built-in roster only", exc,
            )
            _SNAPSHOT = _RosterSnapshot(
                characters=dict(CHARACTERS), db_loaded=False,
                fetched_at=None, next_refresh_at=_next_refresh_at(now),
            )
    else:
        _SNAPSHOT = _RosterSnapshot(
            characters=_merge_db_rows(rows), db_loaded=True,
            fetched_at=now, next_refresh_at=_next_refresh_at(now),
        )
    return _SNAPSHOT


def load_characters(include_retired: bool = False) -> dict[str, CharacterDef]:
    """Return the effective roster: built-ins merged with ``character_def``.

    ``include_retired=False`` (the default) is the FILING view — only active
    characters, the set new work may be assigned to. ``include_retired=True``
    is the RESOLUTION view used by runtime paths serving work that already
    started (card identity, skill/XP finalization, tier denial), so retiring a
    character mid-flight never breaks the running ticket.
    """
    snap = _current_snapshot()
    if include_retired:
        return dict(snap.characters)
    return {slug: c for slug, c in snap.characters.items() if c.active}


def registry_db_loaded() -> bool:
    """True when the current snapshot includes a successful character_def read.

    False means built-ins-only (cold start with no reachable DB) — e.g. the
    filer uses this to warn that DB-recruited characters cannot be validated.
    """
    return _current_snapshot().db_loaded


def resolve_character(slug: str) -> CharacterDef:
    """Return the CharacterDef for ``slug`` or raise CharacterRegistryError.

    Retired-INCLUSIVE: resolution serves work that may already be in flight.
    Callers gating NEW work must check ``.active`` (filing paths) or go
    through :func:`character_retired_denial_from_labels` (pickup).
    """
    roster = load_characters(include_retired=True)
    try:
        return roster[slug]
    except KeyError:
        raise CharacterRegistryError(
            f"unknown character {slug!r}; known: {sorted(roster)}"
        ) from None


def character_brain(slug: str) -> str:
    """Return the agent_class (brain) a character resolves to."""
    return resolve_character(slug).brain


def _character_slug_from_labels(labels) -> str | None:
    for label in labels or ():
        if isinstance(label, str) and label.startswith("character:"):
            return label.split(":", 1)[1].strip()
    return None


def character_from_labels(labels) -> CharacterDef | None:
    """Extract the ``character:<slug>`` label → CharacterDef, or None.

    Unknown slugs return None (fail-open — routing falls back to the raw
    ``class:`` label) rather than raising, so a typo can never wedge pickup.
    Retired-INCLUSIVE (see :func:`resolve_character`).
    """
    slug = _character_slug_from_labels(labels)
    if not slug:
        return None
    return load_characters(include_retired=True).get(slug)


def tier_within_ceiling(tier: str, max_tier: str) -> bool:
    """True if ``tier`` is at or below the character's ``max_tier`` ceiling."""
    t, m = TIER_ORDER.get(tier), TIER_ORDER.get(max_tier)
    if t is None or m is None:
        return False
    return t <= m


def _tier_from_labels(labels) -> str | None:
    for label in labels or ():
        if isinstance(label, str) and label.startswith("tier:"):
            return label.split(":", 1)[1].strip() or None
    return None


def character_tier_denial_from_labels(labels) -> str | None:
    """Return a pickup-denial reason when a character: ticket exceeds its tier ceiling.

    The filer (``--character``) already caps the tier at file time, but a ticket
    can reach pickup via other paths (hand-edited labels, a re-tier, a bulk
    import). This is the pickup-side enforcement of the varying-capability
    contract: a character never executes a ticket above its ``max_tier``. Returns
    ``None`` when there's no character label (nothing to enforce), the character
    is unknown (fail-open — the bare class: label still routes it), the tier is
    absent/unknown, or the tier is within the ceiling.
    """
    char = character_from_labels(labels)
    if char is None:
        return None
    tier = _tier_from_labels(labels)
    if tier is None or tier not in TIER_ORDER:
        return None
    if tier_within_ceiling(tier, char.max_tier):
        return None
    return (
        f"character:{char.slug} tier {tier} exceeds ceiling {char.max_tier}"
    )


def character_retired_denial_from_labels(labels) -> str | None:
    """Return a pickup-denial reason when a character: ticket must not START.

    Two pickup-only policies (design §C2 — in-flight resolution via
    :func:`resolve_character` / :func:`character_from_labels` is unaffected):

    * REGISTRY STALE: a DB-only character whose last-good snapshot is older
      than :data:`MAX_STALE_AGE_SECONDS` is denied ("registry stale") — its
      To-Do tickets safely wait for DB recovery instead of running on
      arbitrarily old definitions. Built-ins are never stale-denied.
    * RETIRED: a retired character takes no NEW work; the ticket waits until
      it is reactivated or re-filed.

    Fail-open like the tier denial: no character label, or a slug unknown to
    the current snapshot, returns None (the bare ``class:`` label still
    routes it — unchanged pickup behavior for class-only tickets).
    """
    slug = _character_slug_from_labels(labels)
    if not slug:
        return None
    snap = _current_snapshot()
    char = snap.characters.get(slug)
    if char is None:
        return None
    if slug not in CHARACTERS and snap.fetched_at is not None:
        age = _now() - snap.fetched_at
        if age > MAX_STALE_AGE_SECONDS:
            return (
                f"character:{slug} registry stale (snapshot age {age:.0f}s > "
                f"{MAX_STALE_AGE_SECONDS:.0f}s) — DB-only character pickup "
                f"denied until the registry refreshes"
            )
    if not char.active:
        return (
            f"character:{slug} is retired — new pickups denied "
            f"(in-flight work unaffected)"
        )
    return None


__all__ = [
    "CharacterDef",
    "CharacterRegistryError",
    "CHARACTERS",
    "MAX_STALE_AGE_SECONDS",
    "TIER_ORDER",
    "load_characters",
    "registry_db_loaded",
    "resolve_character",
    "character_brain",
    "character_from_labels",
    "character_retired_denial_from_labels",
    "character_tier_denial_from_labels",
    "tier_within_ceiling",
]
