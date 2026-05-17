"""BP.B.7 -- dual-write helpers for ``agent_type`` and ``guild_id``.

During the BP.B transition, durable trace writers keep the legacy
``agent_type`` metadata while also filling the new ``guild_id`` column
whenever either value is present.  The helpers are intentionally small
and pure so middleware/write paths can share one normalization rule.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from typing import Any

from backend.sandbox_tier import Guild


LEGACY_AGENT_TYPE_TO_GUILD_ID: Mapping[str, str] = {
    "firmware": Guild.bsp.value,
    "software": Guild.backend.value,
    "validator": Guild.qa.value,
    "reviewer": Guild.auditor.value,
    "reporter": Guild.reporter.value,
    "general": Guild.custom.value,
}


def _clean(value: Any) -> str:
    if isinstance(value, Guild):
        return value.value
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", "_")


def guild_id_for_agent_type(agent_type: Any) -> str:
    """Return the transition-period Guild slug for a legacy agent type."""

    slug = _clean(agent_type)
    if not slug:
        return ""
    if slug in LEGACY_AGENT_TYPE_TO_GUILD_ID:
        return LEGACY_AGENT_TYPE_TO_GUILD_ID[slug]
    try:
        return Guild(slug).value
    except ValueError:
        return ""


def _agent_type_for_guild_id(guild_id: Any) -> str:
    slug = _clean(guild_id)
    if not slug:
        return ""
    try:
        guild = Guild(slug).value
    except ValueError:
        return ""
    for agent_type, mapped_guild in LEGACY_AGENT_TYPE_TO_GUILD_ID.items():
        if mapped_guild == guild:
            return agent_type
    return guild


def sync_agent_type_guild_id(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy with ``agent_type`` and ``guild_id`` filled in sync.

    Invalid or unknown values are left untouched for backwards
    compatibility; only recognized aliases/Guild slugs produce the
    mirrored field.
    """

    out = dict(values or {})
    agent_type = _clean(out.get("agent_type"))
    guild_id = _clean(out.get("guild_id"))

    if not guild_id and agent_type:
        guild_id = guild_id_for_agent_type(agent_type)
    if not agent_type and guild_id:
        agent_type = _agent_type_for_guild_id(guild_id)

    if agent_type:
        out["agent_type"] = agent_type
    if guild_id:
        out["guild_id"] = guild_id
    return out


def sync_mapping_in_place(values: MutableMapping[str, Any]) -> None:
    values.update(sync_agent_type_guild_id(values))


def guild_id_from_values(*values: Mapping[str, Any] | None) -> str | None:
    """Resolve a nullable DB ``guild_id`` from candidate metadata dicts."""

    for value in values:
        synced = sync_agent_type_guild_id(value)
        guild_id = _clean(synced.get("guild_id"))
        if guild_id:
            return guild_id
        context = synced.get("context")
        if isinstance(context, str):
            try:
                parsed = json.loads(context)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                guild_id = _clean(sync_agent_type_guild_id(parsed).get("guild_id"))
                if guild_id:
                    return guild_id
    return None


def sync_json_mapping(raw: Any) -> str:
    """Dual-write a JSON object string while preserving malformed payloads."""

    if not isinstance(raw, str) or not raw.strip():
        return raw if isinstance(raw, str) else "{}"
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(parsed, dict):
        return raw
    return json.dumps(sync_agent_type_guild_id(parsed), ensure_ascii=False)


__all__ = [
    "LEGACY_AGENT_TYPE_TO_GUILD_ID",
    "guild_id_for_agent_type",
    "guild_id_from_values",
    "sync_agent_type_guild_id",
    "sync_json_mapping",
    "sync_mapping_in_place",
]
