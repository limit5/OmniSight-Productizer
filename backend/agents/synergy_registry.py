"""RPG.W17 -- synergy matrix loader + lookup for the Party system.

ADR-0008 §"Party / Synergy system (W17)" pins three named cross-Guild
combinations and requires a wider ~15-entry matrix for arbitrary
party composition. The matrix is data-driven — entries live in
``config/synergy_matrix.yaml`` so an operator can add or rebalance a
combination without a code change. The Python side is just a loader,
a drift guard, and two lookup helpers.

Guild slug typing
-----------------
Synergy uses the *higher-level* 8-bucket Guild grouping from
ADR-0008 (``backend``, ``frontend``, ``security``, ``devops``, ``data``,
``mobile``, ``embedded``, ``generalist``) rather than the
``backend.sandbox_tier.Guild`` enum. Those two namespaces deliberately
diverge: ``sandbox_tier.Guild`` is the BP.B admission-matrix slug
(architect, sa_sd, gateway, …), while the synergy slug is the
operator-facing UX bucket the Character Card and Guild Hall present.

The loader keeps slugs as opaque strings; the only validation is
non-empty + the ``schema_version == 1`` gate. Adding a new bucket is
a one-line YAML edit + a frontend AgentGuild type update.

Module-global state audit (per project SOP)
-------------------------------------------
``load_synergy_matrix`` reads ``config/synergy_matrix.yaml`` on every
call — no in-process cache, mirroring the ``talent_tree`` loader. The
file is small (≤ 30 entries × ≤ 200 bytes) so we keep it operator-
mutable at runtime; downstream callers are expected to invoke once
per ``create_party`` and once per ``compute_party_xp_distribution``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import yaml


_REPO_ROOT = Path(__file__).resolve().parents[2]
SYNERGY_MATRIX_PATH = _REPO_ROOT / "config" / "synergy_matrix.yaml"


# ── Errors ──────────────────────────────────────────────────────────


class SynergyRegistryError(RuntimeError):
    """Base class for synergy_registry parse / lookup errors."""


class SynergyComputeFailed(SynergyRegistryError):
    """Raised (and degraded by callers per AC #3) when synergy lookup fails.

    Per OP-220 §"Error catalog": ``SynergyComputeFailed`` should be
    treated by ``party.create_party`` / ``compute_party_xp_distribution``
    as "no bonus, log warning, continue" — never as a hard failure.
    """


# ── Dataclasses ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SynergyEntry:
    """One cross-Guild synergy row from ``config/synergy_matrix.yaml``.

    ``guilds`` is a *sorted* 2-tuple — callers MUST always sort the
    party pair before comparing, so order at the YAML site is
    cosmetic.
    """

    label: str
    display_name: str
    guilds: tuple[str, str]
    xp_bonus: float
    skill_bonus_target: str | None
    skill_bonus: float | None
    summary: str

    def covers(self, guilds: Iterable[str]) -> bool:
        """True if this synergy applies to a party whose ``guilds`` list
        contains *both* members of this entry's pair."""
        bag = {g.strip().lower() for g in guilds if g and g.strip()}
        return all(g in bag for g in self.guilds)


# ── Loader + drift guard ──────────────────────────────────────────


def load_synergy_matrix(
    path: Path | str = SYNERGY_MATRIX_PATH,
) -> Mapping[tuple[str, str], SynergyEntry]:
    """Parse + validate ``config/synergy_matrix.yaml``.

    Returns a frozen mapping keyed by the *sorted* ``(guild_a, guild_b)``
    tuple, so lookups are pair-order-insensitive. Raises
    :class:`SynergyComputeFailed` on YAML / shape errors so the
    upstream call site can degrade to a no-bonus path per AC #3.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SynergyComputeFailed(f"synergy_matrix: cannot read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SynergyComputeFailed("synergy_matrix YAML must be a mapping")
    if raw.get("schema_version") != 1:
        raise SynergyComputeFailed("synergy_matrix schema_version must be 1")

    rows = raw.get("synergies")
    if not isinstance(rows, list) or not rows:
        raise SynergyComputeFailed("synergy_matrix must declare a non-empty synergies list")

    out: dict[tuple[str, str], SynergyEntry] = {}
    seen_labels: set[str] = set()
    for index, row in enumerate(rows):
        entry = _parse_entry(row, index)
        if entry.label in seen_labels:
            raise SynergyComputeFailed(
                f"synergy_matrix duplicate label {entry.label!r} at row {index}"
            )
        seen_labels.add(entry.label)
        key = entry.guilds
        if key in out:
            raise SynergyComputeFailed(
                f"synergy_matrix duplicate Guild pair {key!r} at row {index}; "
                f"previously declared as {out[key].label!r}"
            )
        out[key] = entry
    return MappingProxyType(out)


# ── Public lookup ──────────────────────────────────────────────────


def synergy_for_pair(
    guild_a: str,
    guild_b: str,
    *,
    path: Path | str = SYNERGY_MATRIX_PATH,
) -> SynergyEntry | None:
    """Return the synergy entry for the (unordered) pair, or ``None``.

    A pair of identical Guilds returns ``None`` — synergy is by
    definition cross-Guild (ADR-0008 §W17). Unknown Guild slugs also
    return ``None`` without raising; absence-of-synergy is a valid
    state for a party.
    """
    key = _sorted_pair(guild_a, guild_b)
    if key is None:
        return None
    matrix = load_synergy_matrix(path)
    return matrix.get(key)


def synergy_for_members(
    guilds: Iterable[str],
    *,
    path: Path | str = SYNERGY_MATRIX_PATH,
) -> SynergyEntry | None:
    """Return the *strongest applicable* synergy for a multi-member party.

    "Strongest" = highest ``xp_bonus``; ties are broken by
    ``skill_bonus`` then by label sort, so the lookup is
    deterministic. The strongest entry whose pair is *fully covered*
    by the party's Guild list wins. If no entry's pair is covered,
    returns ``None`` (caller degrades to base XP).
    """
    bag = {g.strip().lower() for g in guilds if g and g.strip()}
    if len(bag) < 2:
        return None
    matrix = load_synergy_matrix(path)
    candidates = [entry for entry in matrix.values() if entry.covers(bag)]
    if not candidates:
        return None
    # Deterministic sort: highest xp_bonus, then highest skill_bonus,
    # then alphabetical label for tie-breaking.
    candidates.sort(
        key=lambda e: (
            -float(e.xp_bonus),
            -float(e.skill_bonus or 0.0),
            e.label,
        )
    )
    return candidates[0]


def all_synergies(
    *,
    path: Path | str = SYNERGY_MATRIX_PATH,
) -> tuple[SynergyEntry, ...]:
    """Return every synergy entry — used by the Party Hall UI badges."""
    matrix = load_synergy_matrix(path)
    return tuple(matrix.values())


# ── Internal helpers ──────────────────────────────────────────────


def _parse_entry(row: Any, index: int) -> SynergyEntry:
    if not isinstance(row, dict):
        raise SynergyComputeFailed(
            f"synergy_matrix row {index} must be a mapping; got {type(row).__name__}"
        )
    guilds_raw = row.get("guilds")
    if not isinstance(guilds_raw, list) or len(guilds_raw) != 2:
        raise SynergyComputeFailed(
            f"synergy_matrix row {index} must declare exactly 2 guilds"
        )
    key = _sorted_pair(guilds_raw[0], guilds_raw[1])
    if key is None:
        raise SynergyComputeFailed(
            f"synergy_matrix row {index} has a duplicate or empty guild slug"
        )
    label = _required_text(row.get("label"), f"row {index} label")
    display_name = _required_text(row.get("display_name"), f"row {index} display_name")
    summary = _required_text(row.get("summary"), f"row {index} summary")
    xp_bonus = _coerce_bonus(row.get("xp_bonus", 0.0), f"row {index} xp_bonus")
    skill_bonus_target_raw = row.get("skill_bonus_target")
    skill_bonus_raw = row.get("skill_bonus")
    skill_bonus_target: str | None
    skill_bonus: float | None
    if skill_bonus_target_raw is None and skill_bonus_raw is None:
        skill_bonus_target = None
        skill_bonus = None
    else:
        if skill_bonus_target_raw is None or skill_bonus_raw is None:
            raise SynergyComputeFailed(
                f"synergy_matrix row {index}: skill_bonus and skill_bonus_target "
                "must both be set or both be null"
            )
        skill_bonus_target = _required_text(
            skill_bonus_target_raw, f"row {index} skill_bonus_target"
        )
        skill_bonus = _coerce_bonus(skill_bonus_raw, f"row {index} skill_bonus")
    return SynergyEntry(
        label=label,
        display_name=display_name,
        guilds=key,
        xp_bonus=xp_bonus,
        skill_bonus_target=skill_bonus_target,
        skill_bonus=skill_bonus,
        summary=summary,
    )


def _sorted_pair(a: Any, b: Any) -> tuple[str, str] | None:
    if not isinstance(a, str) or not isinstance(b, str):
        return None
    clean_a = a.strip().lower()
    clean_b = b.strip().lower()
    if not clean_a or not clean_b or clean_a == clean_b:
        return None
    return tuple(sorted((clean_a, clean_b)))  # type: ignore[return-value]


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise SynergyComputeFailed(f"synergy_matrix {field_name} must be a string")
    clean = value.strip()
    if not clean:
        raise SynergyComputeFailed(f"synergy_matrix {field_name} must be non-empty")
    return clean


def _coerce_bonus(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise SynergyComputeFailed(f"synergy_matrix {field_name} must be a number")
    if not isinstance(value, (int, float)):
        raise SynergyComputeFailed(f"synergy_matrix {field_name} must be a number")
    if float(value) < 0.0:
        raise SynergyComputeFailed(f"synergy_matrix {field_name} must be >= 0.0")
    return float(value)


__all__ = [
    "SYNERGY_MATRIX_PATH",
    "SynergyComputeFailed",
    "SynergyEntry",
    "SynergyRegistryError",
    "all_synergies",
    "load_synergy_matrix",
    "synergy_for_members",
    "synergy_for_pair",
]
