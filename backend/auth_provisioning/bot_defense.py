"""SC.13.1 / SC.13.2 -- Bot defense scaffold bridge onto AS.3 bot_challenge.

This module emits a small generated-app manifest that reuses the AS.3
TypeScript twin under ``templates/_shared/bot-challenge``. SC.13.2 adds
the default form belt for login / signup / password-reset / contact /
comment forms. It does not reimplement siteverify, score classification,
fallback, or reject logic; generated forms import those primitives from
the AS.3 bridge this module creates.

Module-global state audit (per implement_phase_step.md SOP Step 1):
constants are immutable tuples / frozen dataclasses derived from AS.3 and
AS.6 source constants.  No module-level cache, singleton, env read, network
call, or DB write is introduced; every worker derives the same manifest from
source code.

Read-after-write timing audit: N/A -- render helpers are pure manifest
generation and do not read after writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from backend.auth_provisioning.self_hosted import AuthScaffoldEnvVar, AuthScaffoldFile
from backend.security import bot_challenge
from backend.security import turnstile_form_verifier as turnstile_forms


DEFAULT_BOT_CHALLENGE_IMPORT = "@/shared/bot-challenge"
DEFAULT_BOT_CHALLENGE_BRIDGE_PATH = "auth/bot-challenge.ts"
DEFAULT_BOT_DEFENSE_FORMS_PATH = "auth/bot-defense-forms.ts"

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

_DEFAULT_FORM_ITEMS: tuple[tuple[str, str], ...] = (
    ("login", turnstile_forms.FORM_ACTION_LOGIN),
    ("signup", turnstile_forms.FORM_ACTION_SIGNUP),
    ("password-reset", turnstile_forms.FORM_ACTION_PASSWORD_RESET),
    ("contact", turnstile_forms.FORM_ACTION_CONTACT),
    ("comment", "comment"),
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
class BotDefenseFormItem:
    """Generated-app form metadata for the SC.13.2 default belt."""

    form: str
    widget_action: str
    token_body_field: str
    source: str = "sc.13.2"

    def to_dict(self) -> dict[str, str]:
        return {
            "form": self.form,
            "widget_action": self.widget_action,
            "token_body_field": self.token_body_field,
            "source": self.source,
        }


@dataclass(frozen=True)
class BotDefenseScaffoldOptions:
    """Inputs for rendering the SC.13 bot defense scaffold bridge."""

    bot_challenge_import: str = DEFAULT_BOT_CHALLENGE_IMPORT
    bridge_path: str = DEFAULT_BOT_CHALLENGE_BRIDGE_PATH
    forms_path: str = DEFAULT_BOT_DEFENSE_FORMS_PATH

    def validate(self) -> None:
        if not self.bot_challenge_import or not self.bot_challenge_import.strip():
            raise ValueError("bot_challenge_import is required")
        if not self.bridge_path or not self.bridge_path.strip():
            raise ValueError("bridge_path is required")
        if not self.forms_path or not self.forms_path.strip():
            raise ValueError("forms_path is required")


@dataclass(frozen=True)
class BotDefenseScaffoldResult:
    """Manifest for the SC.13 bot defense bridge and default forms."""

    files: tuple[AuthScaffoldFile, ...]
    env: tuple[AuthScaffoldEnvVar, ...]
    providers: tuple[BotDefenseProviderItem, ...]
    forms: tuple[BotDefenseFormItem, ...] = ()
    dependencies: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [f.to_dict() for f in self.files],
            "env": [v.to_dict() for v in self.env],
            "providers": [p.to_dict() for p in self.providers],
            "forms": [f.to_dict() for f in self.forms],
            "dependencies": list(self.dependencies),
            "notes": list(self.notes),
        }


def list_bot_defense_providers() -> list[str]:
    """Return the AS.3 provider ids SC.13.1 exposes to generated apps."""

    return [provider.value for provider, _ in _SITE_KEY_ENVS]


def list_bot_defense_forms() -> list[str]:
    """Return the SC.13.2 default form ids in generated-app UI order."""

    return [form for form, _ in _DEFAULT_FORM_ITEMS]


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
    forms = _form_items()
    return BotDefenseScaffoldResult(
        files=(
            AuthScaffoldFile(
                opts.bridge_path.strip("/"),
                _bot_challenge_bridge_file(opts.bot_challenge_import),
            ),
            AuthScaffoldFile(
                opts.forms_path.strip("/"),
                _bot_defense_forms_file(opts.bridge_path, opts.forms_path),
            ),
        ),
        env=_env_vars(providers),
        providers=providers,
        forms=forms,
        notes=(
            "reuses AS.3 templates/_shared/bot-challenge for verify/fallback/reject logic",
            "SC.13.2 defaults cover login/signup/password-reset/contact/comment forms",
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


def _form_items() -> tuple[BotDefenseFormItem, ...]:
    return tuple(
        BotDefenseFormItem(
            form=form,
            widget_action=widget_action,
            token_body_field=turnstile_forms.TURNSTILE_TOKEN_BODY_FIELD,
        )
        for form, widget_action in _DEFAULT_FORM_ITEMS
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


def _bot_defense_forms_file(bridge_path: str, forms_path: str) -> str:
    bridge_import = _relative_ts_import(bridge_path, forms_path)
    form_rows = "\n".join(
        "  "
        + "{ "
        + f'form: "{_ts_string(item.form)}", '
        + f'widgetAction: "{_ts_string(item.widget_action)}", '
        + f'tokenBodyField: "{_ts_string(item.token_body_field)}"'
        + " },"
        for item in _form_items()
    )
    return f"""// SC.13.2 bot defense default form belt.
// Generated forms share one VerifyContext shape and the AS.3 bridge.

import {{
  botChallengeFallbackOrder,
  botChallengeProviderOrder,
  botChallengeSiteKeyEnv,
  Provider,
  type VerifyContext,
}} from "{bridge_import}"

export const botDefenseDefaultForms = Object.freeze([
{form_rows}
] as const)

export type BotDefenseFormId = (typeof botDefenseDefaultForms)[number]["form"]

export function botDefenseFormDefaults(form: BotDefenseFormId) {{
  const item = botDefenseDefaultForms.find((candidate) => candidate.form === form)
  if (!item) throw new Error(`unsupported bot defense form: ${{String(form)}}`)
  return item
}}

export function botDefenseSiteKeyEnv(provider: Provider): string {{
  return botChallengeSiteKeyEnv[provider]
}}

export function botDefenseVerifyContextForForm(opts: {{
  form: BotDefenseFormId
  provider: Provider
  token?: string | null
  secret?: string | null
  phase?: number
  remoteIp?: string | null
}}): VerifyContext {{
  const form = botDefenseFormDefaults(opts.form)
  return {{
    provider: opts.provider,
    token: opts.token ?? null,
    secret: opts.secret ?? null,
    phase: opts.phase ?? 1,
    widgetAction: form.widgetAction,
    expectedAction: form.widgetAction,
    remoteIp: opts.remoteIp ?? null,
  }}
}}

export const botDefenseFormBelt = Object.freeze({{
  forms: botDefenseDefaultForms,
  providerOrder: botChallengeProviderOrder,
  fallbackOrder: botChallengeFallbackOrder,
  siteKeyEnv: botChallengeSiteKeyEnv,
}})
"""


def _relative_ts_import(target_path: str, from_path: str) -> str:
    target_parts = target_path.strip("/").split("/")
    from_parts = from_path.strip("/").split("/")
    if len(target_parts) == len(from_parts) and target_parts[:-1] == from_parts[:-1]:
        return f"./{target_parts[-1].removesuffix('.ts')}"
    return target_path.strip("/").removesuffix(".ts")


def _ts_string(value: Optional[str]) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "BotDefenseProviderItem",
    "BotDefenseFormItem",
    "BotDefenseScaffoldOptions",
    "BotDefenseScaffoldResult",
    "DEFAULT_BOT_CHALLENGE_BRIDGE_PATH",
    "DEFAULT_BOT_CHALLENGE_IMPORT",
    "DEFAULT_BOT_DEFENSE_FORMS_PATH",
    "list_bot_defense_forms",
    "list_bot_defense_providers",
    "render_bot_defense_scaffold",
]
