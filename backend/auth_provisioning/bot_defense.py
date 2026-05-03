"""SC.13.1 -- Bot defense scaffold bridge onto AS.3 bot_challenge.

This module emits a small generated-app manifest that reuses the AS.3
TypeScript twin under ``templates/_shared/bot-challenge``.  It does not
reimplement siteverify, score classification, fallback, or reject logic;
generated forms import those primitives from the AS.3 bridge this row
creates.

Module-global state audit (per implement_phase_step.md SOP Step 1):
constants are immutable tuples / frozen dataclasses derived from AS.3 source
constants.  No module-level cache, singleton, env read, network call, or DB
write is introduced; every worker derives the same manifest from source code.

Read-after-write timing audit: N/A -- render helpers are pure manifest
generation and do not read after writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from backend.auth_provisioning.self_hosted import AuthScaffoldEnvVar, AuthScaffoldFile
from backend.security import bot_challenge


DEFAULT_BOT_CHALLENGE_IMPORT = "@/shared/bot-challenge"
DEFAULT_BOT_CHALLENGE_BRIDGE_PATH = "auth/bot-challenge.ts"

_SITE_KEY_ENVS: tuple[tuple[bot_challenge.Provider, str], ...] = (
    (bot_challenge.Provider.TURNSTILE, "NEXT_PUBLIC_TURNSTILE_SITE_KEY"),
    (bot_challenge.Provider.RECAPTCHA_V2, "NEXT_PUBLIC_RECAPTCHA_SITE_KEY"),
    (bot_challenge.Provider.RECAPTCHA_V3, "NEXT_PUBLIC_RECAPTCHA_SITE_KEY"),
    (bot_challenge.Provider.HCAPTCHA, "NEXT_PUBLIC_HCAPTCHA_SITE_KEY"),
)

_DEFAULT_FALLBACK_PROVIDERS: tuple[bot_challenge.Provider, ...] = (
    bot_challenge.Provider.RECAPTCHA_V3,
    bot_challenge.Provider.HCAPTCHA,
)


@dataclass(frozen=True)
class BotDefenseProviderItem:
    """Provider env metadata mirrored from the AS.3 provider catalog."""

    provider: str
    site_key_env: str
    secret_env: str

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "site_key_env": self.site_key_env,
            "secret_env": self.secret_env,
        }


@dataclass(frozen=True)
class BotDefenseScaffoldOptions:
    """Inputs for rendering the SC.13.1 bot defense scaffold bridge."""

    bot_challenge_import: str = DEFAULT_BOT_CHALLENGE_IMPORT
    bridge_path: str = DEFAULT_BOT_CHALLENGE_BRIDGE_PATH

    def validate(self) -> None:
        if not self.bot_challenge_import or not self.bot_challenge_import.strip():
            raise ValueError("bot_challenge_import is required")
        if not self.bridge_path or not self.bridge_path.strip():
            raise ValueError("bridge_path is required")


@dataclass(frozen=True)
class BotDefenseScaffoldResult:
    """Manifest for the SC.13.1 bot defense bridge."""

    files: tuple[AuthScaffoldFile, ...]
    env: tuple[AuthScaffoldEnvVar, ...]
    providers: tuple[BotDefenseProviderItem, ...]
    dependencies: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [f.to_dict() for f in self.files],
            "env": [v.to_dict() for v in self.env],
            "providers": [p.to_dict() for p in self.providers],
            "dependencies": list(self.dependencies),
            "notes": list(self.notes),
        }


def list_bot_defense_providers() -> list[str]:
    """Return the AS.3 provider ids SC.13.1 exposes to generated apps."""

    return [provider.value for provider, _ in _SITE_KEY_ENVS]


def render_bot_defense_scaffold(
    options: BotDefenseScaffoldOptions | None = None,
) -> BotDefenseScaffoldResult:
    """Render a generated-app bridge that reuses AS.3 bot_challenge.

    The result carries env var names only.  Secret values stay with the
    operator / deployment system and are never embedded in scaffold content.
    """

    opts = options or BotDefenseScaffoldOptions()
    opts.validate()
    providers = _provider_items()
    return BotDefenseScaffoldResult(
        files=(
            AuthScaffoldFile(
                opts.bridge_path.strip("/"),
                _bot_challenge_bridge_file(opts.bot_challenge_import),
            ),
        ),
        env=_env_vars(providers),
        providers=providers,
        notes=(
            "reuses AS.3 templates/_shared/bot-challenge for verify/fallback/reject logic",
        ),
    )


def _provider_items() -> tuple[BotDefenseProviderItem, ...]:
    return tuple(
        BotDefenseProviderItem(
            provider=provider.value,
            site_key_env=site_key_env,
            secret_env=bot_challenge.secret_env_for(provider),
        )
        for provider, site_key_env in _SITE_KEY_ENVS
    )


def _env_vars(
    providers: tuple[BotDefenseProviderItem, ...],
) -> tuple[AuthScaffoldEnvVar, ...]:
    seen: set[str] = set()
    items: list[AuthScaffoldEnvVar] = [
        AuthScaffoldEnvVar("BOT_CHALLENGE_PROVIDER", True, source="sc.13.1"),
        AuthScaffoldEnvVar("BOT_CHALLENGE_FALLBACK_PROVIDERS", False, source="sc.13.1"),
    ]
    for provider in providers:
        for name, sensitive in (
            (provider.site_key_env, False),
            (provider.secret_env, True),
        ):
            if name in seen:
                continue
            seen.add(name)
            items.append(
                AuthScaffoldEnvVar(
                    name,
                    True,
                    sensitive=sensitive,
                    source="sc.13.1",
                )
            )
    return tuple(items)


def _bot_challenge_bridge_file(bot_challenge_import: str) -> str:
    imported = _ts_string(bot_challenge_import)
    provider_order = ", ".join(
        f'"{_ts_string(provider)}"'
        for provider in list_bot_defense_providers()
    )
    fallback_order = ", ".join(
        f'"{_ts_string(provider.value)}"' for provider in _DEFAULT_FALLBACK_PROVIDERS
    )
    site_key_pairs = "\n".join(
        f'  "{_ts_string(provider.value)}": "{_ts_string(site_key_env)}",'
        for provider, site_key_env in _SITE_KEY_ENVS
    )
    return f"""// SC.13.1 bot defense bridge.
// Reuses the AS.3 generated-app bot-challenge twin; do not copy provider logic here.

export {{
  BOT_CHALLENGE_REJECTED_CODE,
  BOT_CHALLENGE_REJECTED_HTTP_STATUS,
  Provider,
  pickProvider,
  secretEnvFor,
  shouldReject,
  verifyAndEnforce,
  verifyWithFallback,
  type BotChallengeResult,
  type VerifyContext,
}} from "{imported}"

export const botChallengeProviderOrder = Object.freeze([{provider_order}])

export const botChallengeFallbackOrder = Object.freeze([{fallback_order}])

export const botChallengeSiteKeyEnv = Object.freeze({{
{site_key_pairs}
}})
"""


def _ts_string(value: Optional[str]) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "BotDefenseProviderItem",
    "BotDefenseScaffoldOptions",
    "BotDefenseScaffoldResult",
    "DEFAULT_BOT_CHALLENGE_BRIDGE_PATH",
    "DEFAULT_BOT_CHALLENGE_IMPORT",
    "list_bot_defense_providers",
    "render_bot_defense_scaffold",
]
