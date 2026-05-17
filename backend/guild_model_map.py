"""BP.F.2 -- default model mapping for the 21 BP.B Guilds.

This module is the code-side default table for the Guild model choices
declared in ``configs/model_mapping.yaml``. It is intentionally static:
callers that need operator-editable runtime routing should continue to
read the YAML-backed routing policy, while compile-time consumers can
import this immutable map without touching disk.

Module-global state audit
-------------------------
The public mapping is a ``MappingProxyType`` over a private dict built
from immutable enum keys and string values. There is no cache, file I/O,
or environment-dependent initialization.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from backend.sandbox_tier import Guild


_RAW_GUILD_DEFAULT_MODEL_MAP: dict[Guild, str] = {
    Guild.architect: "anthropic:claude-opus-4-20250514",
    Guild.sa_sd: "anthropic:claude-sonnet-4-20250514",
    Guild.ux: "google:gemini-1.5-pro",
    Guild.pm: "anthropic:claude-sonnet-4-20250514",
    Guild.gateway: "anthropic:claude-haiku-4-20250506",
    Guild.bsp: "anthropic:claude-sonnet-4-20250514",
    Guild.hal: "anthropic:claude-sonnet-4-20250514",
    Guild.algo_cv: "anthropic:claude-opus-4-20250514",
    Guild.optical: "anthropic:claude-sonnet-4-20250514",
    Guild.isp: "anthropic:claude-sonnet-4-20250514",
    Guild.audio: "anthropic:claude-sonnet-4-20250514",
    Guild.frontend: "anthropic:claude-sonnet-4-20250514",
    Guild.backend: "anthropic:claude-sonnet-4-20250514",
    Guild.sre: "anthropic:claude-sonnet-4-20250514",
    Guild.qa: "anthropic:claude-sonnet-4-20250514",
    Guild.auditor: "anthropic:claude-opus-4-20250514",
    Guild.red_team: "xai:grok-3-mini",
    Guild.forensics: "google:gemini-1.5-pro",
    Guild.intel: "google:gemini-1.5-pro",
    Guild.reporter: "anthropic:claude-haiku-4-20250506",
    Guild.custom: "anthropic:claude-sonnet-4-20250514",
}


GUILD_DEFAULT_MODEL_MAP: Mapping[Guild, str] = MappingProxyType(
    _RAW_GUILD_DEFAULT_MODEL_MAP
)


def default_model_for_guild(guild: Guild) -> str:
    """Return the default ``provider:model`` spec for ``guild``."""

    return GUILD_DEFAULT_MODEL_MAP[guild]


def list_guild_default_models() -> tuple[tuple[Guild, str], ...]:
    """Return every Guild default model in canonical enum order."""

    return tuple((guild, default_model_for_guild(guild)) for guild in Guild)


def _assert_mapping_complete() -> None:
    missing = [guild for guild in Guild if guild not in _RAW_GUILD_DEFAULT_MODEL_MAP]
    extra = [guild for guild in _RAW_GUILD_DEFAULT_MODEL_MAP if guild not in Guild]
    if missing or extra:
        raise RuntimeError(
            "Guild default model map drifted from backend.sandbox_tier.Guild: "
            f"missing={sorted(guild.value for guild in missing)}, "
            f"extra={sorted(guild.value for guild in extra)}"
        )


_assert_mapping_complete()


__all__ = [
    "GUILD_DEFAULT_MODEL_MAP",
    "default_model_for_guild",
    "list_guild_default_models",
]
