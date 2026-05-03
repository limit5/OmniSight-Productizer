"""SC.13.1 / SC.13.2 / SC.13.3 / SC.13.4 -- Bot defense scaffold bridge.

This module emits a small generated-app manifest that reuses the AS.3
TypeScript twin under ``templates/_shared/bot-challenge``. SC.13.2 adds
the default form belt for login / signup / password-reset / contact /
comment forms. SC.13.3 adds the AS.4 honeypot field bridge for the AS.4
supported form namespaces. SC.13.4 adds a small observability dashboard
helper for challenge pass / fail / fallback ratios. It does not reimplement
siteverify, score classification, fallback, reject, or honeypot field-name
logic; generated forms import those primitives from the AS.3 / AS.4 bridges
this module creates.

Module-global state audit (per implement_phase_step.md SOP Step 1):
constants are immutable tuples / frozen dataclasses derived from AS.3 and
AS.4 / AS.6 source constants.  No module-level cache, singleton, env read,
network call, or DB write is introduced; every worker derives the same
manifest from source code.

Read-after-write timing audit: N/A -- render helpers are pure manifest
generation and do not read after writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from backend.auth_provisioning.self_hosted import AuthScaffoldEnvVar, AuthScaffoldFile
from backend.security import bot_challenge
from backend.security import honeypot
from backend.security import turnstile_form_verifier as turnstile_forms


DEFAULT_BOT_CHALLENGE_IMPORT = "@/shared/bot-challenge"
DEFAULT_HONEYPOT_IMPORT = "@/shared/honeypot"
DEFAULT_BOT_CHALLENGE_BRIDGE_PATH = "auth/bot-challenge.ts"
DEFAULT_HONEYPOT_BRIDGE_PATH = "auth/honeypot.ts"
DEFAULT_BOT_DEFENSE_FORMS_PATH = "auth/bot-defense-forms.ts"
DEFAULT_BOT_DEFENSE_DASHBOARD_PATH = "auth/bot-defense-dashboard.ts"

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

_DEFAULT_FORM_ITEMS: tuple[tuple[str, str, Optional[str]], ...] = (
    ("login", turnstile_forms.FORM_ACTION_LOGIN, turnstile_forms.FORM_PATH_LOGIN),
    ("signup", turnstile_forms.FORM_ACTION_SIGNUP, turnstile_forms.FORM_PATH_SIGNUP),
    (
        "password-reset",
        turnstile_forms.FORM_ACTION_PASSWORD_RESET,
        turnstile_forms.FORM_PATH_PASSWORD_RESET,
    ),
    ("contact", turnstile_forms.FORM_ACTION_CONTACT, turnstile_forms.FORM_PATH_CONTACT),
    ("comment", "comment", None),
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
    honeypot_form_path: Optional[str] = None
    source: str = "sc.13.2"

    def to_dict(self) -> dict[str, Any]:
        return {
            "form": self.form,
            "widget_action": self.widget_action,
            "token_body_field": self.token_body_field,
            "honeypot_form_path": self.honeypot_form_path,
            "source": self.source,
        }


@dataclass(frozen=True)
class BotDefenseScaffoldOptions:
    """Inputs for rendering the SC.13 bot defense scaffold bridge."""

    bot_challenge_import: str = DEFAULT_BOT_CHALLENGE_IMPORT
    honeypot_import: str = DEFAULT_HONEYPOT_IMPORT
    bridge_path: str = DEFAULT_BOT_CHALLENGE_BRIDGE_PATH
    honeypot_bridge_path: str = DEFAULT_HONEYPOT_BRIDGE_PATH
    forms_path: str = DEFAULT_BOT_DEFENSE_FORMS_PATH
    dashboard_path: str = DEFAULT_BOT_DEFENSE_DASHBOARD_PATH

    def validate(self) -> None:
        if not self.bot_challenge_import or not self.bot_challenge_import.strip():
            raise ValueError("bot_challenge_import is required")
        if not self.honeypot_import or not self.honeypot_import.strip():
            raise ValueError("honeypot_import is required")
        if not self.bridge_path or not self.bridge_path.strip():
            raise ValueError("bridge_path is required")
        if not self.honeypot_bridge_path or not self.honeypot_bridge_path.strip():
            raise ValueError("honeypot_bridge_path is required")
        if not self.forms_path or not self.forms_path.strip():
            raise ValueError("forms_path is required")
        if not self.dashboard_path or not self.dashboard_path.strip():
            raise ValueError("dashboard_path is required")


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

    return [form for form, _, _ in _DEFAULT_FORM_ITEMS]


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
                opts.honeypot_bridge_path.strip("/"),
                _honeypot_bridge_file(opts.honeypot_import),
            ),
            AuthScaffoldFile(
                opts.forms_path.strip("/"),
                _bot_defense_forms_file(
                    opts.bridge_path,
                    opts.honeypot_bridge_path,
                    opts.forms_path,
                ),
            ),
            AuthScaffoldFile(
                opts.dashboard_path.strip("/"),
                _bot_defense_dashboard_file(
                    opts.bridge_path,
                    opts.dashboard_path,
                ),
            ),
        ),
        env=_env_vars(providers),
        providers=providers,
        forms=forms,
        notes=(
            "reuses AS.3 templates/_shared/bot-challenge for verify/fallback/reject logic",
            "SC.13.2 defaults cover login/signup/password-reset/contact/comment forms",
            "reuses AS.4 templates/_shared/honeypot for auto hidden-field generation",
            "SC.13.4 dashboard exposes challenge pass/fail/fallback ratios",
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
            honeypot_form_path=honeypot_form_path,
        )
        for form, widget_action, honeypot_form_path in _DEFAULT_FORM_ITEMS
        if honeypot_form_path is None or honeypot_form_path in honeypot._FORM_PREFIXES
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
  OUTCOME_BLOCKED_LOWSCORE,
  OUTCOME_BYPASS_APIKEY,
  OUTCOME_BYPASS_BOOTSTRAP,
  OUTCOME_BYPASS_CHATOPS,
  OUTCOME_BYPASS_IP_ALLOWLIST,
  OUTCOME_BYPASS_PROBE,
  OUTCOME_BYPASS_TEST_TOKEN,
  OUTCOME_BYPASS_WEBHOOK,
  OUTCOME_JSFAIL_FALLBACK_HCAPTCHA,
  OUTCOME_JSFAIL_FALLBACK_RECAPTCHA,
  OUTCOME_JSFAIL_HONEYPOT_FAIL,
  OUTCOME_JSFAIL_HONEYPOT_PASS,
  OUTCOME_PASS,
  OUTCOME_UNVERIFIED_LOWSCORE,
  OUTCOME_UNVERIFIED_SERVERERR,
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


def _bot_defense_dashboard_file(
    bridge_path: str,
    dashboard_path: str,
) -> str:
    bridge_import = _relative_ts_import(bridge_path, dashboard_path)
    return f"""// SC.13.4 bot defense observability dashboard.
// Buckets AS.3 outcomes into challenge pass/fail/fallback ratios.

import {{
  OUTCOME_BLOCKED_LOWSCORE,
  OUTCOME_BYPASS_APIKEY,
  OUTCOME_BYPASS_BOOTSTRAP,
  OUTCOME_BYPASS_CHATOPS,
  OUTCOME_BYPASS_IP_ALLOWLIST,
  OUTCOME_BYPASS_PROBE,
  OUTCOME_BYPASS_TEST_TOKEN,
  OUTCOME_BYPASS_WEBHOOK,
  OUTCOME_JSFAIL_FALLBACK_HCAPTCHA,
  OUTCOME_JSFAIL_FALLBACK_RECAPTCHA,
  OUTCOME_JSFAIL_HONEYPOT_FAIL,
  OUTCOME_JSFAIL_HONEYPOT_PASS,
  OUTCOME_PASS,
  OUTCOME_UNVERIFIED_LOWSCORE,
  OUTCOME_UNVERIFIED_SERVERERR,
  type BotChallengeResult,
}} from "{bridge_import}"

export interface BotDefenseChallengeDashboardSummary {{
  readonly total: number
  readonly passCount: number
  readonly failCount: number
  readonly fallbackCount: number
  readonly passRatio: number | null
  readonly failRatio: number | null
  readonly fallbackRatio: number | null
}}

export type BotDefenseChallengeDashboardEvent = Pick<BotChallengeResult, "outcome">

export const botDefensePassOutcomes = Object.freeze(new Set<string>([
  OUTCOME_PASS,
  OUTCOME_BYPASS_APIKEY,
  OUTCOME_BYPASS_WEBHOOK,
  OUTCOME_BYPASS_CHATOPS,
  OUTCOME_BYPASS_BOOTSTRAP,
  OUTCOME_BYPASS_PROBE,
  OUTCOME_BYPASS_IP_ALLOWLIST,
  OUTCOME_BYPASS_TEST_TOKEN,
  OUTCOME_JSFAIL_HONEYPOT_PASS,
]))

export const botDefenseFailOutcomes = Object.freeze(new Set<string>([
  OUTCOME_UNVERIFIED_LOWSCORE,
  OUTCOME_UNVERIFIED_SERVERERR,
  OUTCOME_BLOCKED_LOWSCORE,
  OUTCOME_JSFAIL_HONEYPOT_FAIL,
]))

export const botDefenseFallbackOutcomes = Object.freeze(new Set<string>([
  OUTCOME_JSFAIL_FALLBACK_RECAPTCHA,
  OUTCOME_JSFAIL_FALLBACK_HCAPTCHA,
]))

export const botDefenseChallengeDashboardBuckets = Object.freeze({{
  pass: botDefensePassOutcomes,
  fail: botDefenseFailOutcomes,
  fallback: botDefenseFallbackOutcomes,
}})

function safeRatio(count: number, total: number): number | null {{
  return total === 0 ? null : count / total
}}

export function botDefenseChallengeDashboard(
  events: ReadonlyArray<BotDefenseChallengeDashboardEvent>,
): BotDefenseChallengeDashboardSummary {{
  let passCount = 0
  let failCount = 0
  let fallbackCount = 0

  for (const event of events) {{
    const outcome = event.outcome
    if (botDefenseFallbackOutcomes.has(outcome)) {{
      fallbackCount += 1
    }} else if (botDefensePassOutcomes.has(outcome)) {{
      passCount += 1
    }} else if (botDefenseFailOutcomes.has(outcome)) {{
      failCount += 1
    }}
  }}

  const total = passCount + failCount + fallbackCount
  return Object.freeze({{
    total,
    passCount,
    failCount,
    fallbackCount,
    passRatio: safeRatio(passCount, total),
    failRatio: safeRatio(failCount, total),
    fallbackRatio: safeRatio(fallbackCount, total),
  }})
}}
"""


def _honeypot_bridge_file(honeypot_import: str) -> str:
    imported = _ts_string(honeypot_import)
    return f"""// SC.13.3 honeypot bridge.
// Reuses the AS.4 generated-app honeypot twin; do not copy field-name logic here.

export {{
  HONEYPOT_HIDE_CSS,
  HONEYPOT_INPUT_ATTRS,
  OS_HONEYPOT_CLASS,
  currentEpoch,
  expectedFieldNames,
  honeypotFieldName,
  validateAndEnforce as validateHoneypotAndEnforce,
  validateHoneypot,
  type HoneypotResult,
}} from "{imported}"

export const ANONYMOUS_TENANT_ID = "_anonymous"
"""


def _bot_defense_forms_file(
    bridge_path: str,
    honeypot_bridge_path: str,
    forms_path: str,
) -> str:
    bridge_import = _relative_ts_import(bridge_path, forms_path)
    honeypot_import = _relative_ts_import(honeypot_bridge_path, forms_path)
    form_rows = "\n".join(
        "  "
        + "{ "
        + f'form: "{_ts_string(item.form)}", '
        + f'widgetAction: "{_ts_string(item.widget_action)}", '
        + f'tokenBodyField: "{_ts_string(item.token_body_field)}", '
        + (
            f'honeypotFormPath: "{_ts_string(item.honeypot_form_path)}"'
            if item.honeypot_form_path
            else "honeypotFormPath: null"
        )
        + " },"
        for item in _form_items()
    )
    return f"""// SC.13.2 / SC.13.3 bot defense default form belt.
// Generated forms share one VerifyContext shape plus the AS.3 / AS.4 bridges.

import {{
  botChallengeFallbackOrder,
  botChallengeProviderOrder,
  botChallengeSiteKeyEnv,
  Provider,
  type VerifyContext,
}} from "{bridge_import}"

import {{
  ANONYMOUS_TENANT_ID,
  HONEYPOT_HIDE_CSS,
  HONEYPOT_INPUT_ATTRS,
  OS_HONEYPOT_CLASS,
  currentEpoch,
  honeypotFieldName,
}} from "{honeypot_import}"

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

export interface BotDefenseHoneypotField {{
  readonly type: "text"
  readonly name: string
  readonly defaultValue: ""
  readonly className: string
  readonly inputAttrs: typeof HONEYPOT_INPUT_ATTRS
  readonly styleText: string
  readonly formPath: string
}}

export function botDefenseHoneypotFieldForForm(opts: {{
  form: BotDefenseFormId
  tenantId?: string | null
  nowMs?: number
}}): BotDefenseHoneypotField | null {{
  const form = botDefenseFormDefaults(opts.form)
  if (!form.honeypotFormPath) return null
  const tenantId = opts.tenantId ?? ANONYMOUS_TENANT_ID
  const name = honeypotFieldName(form.honeypotFormPath, tenantId, currentEpoch(opts.nowMs))
  return Object.freeze({{
    type: "text",
    name,
    defaultValue: "",
    className: OS_HONEYPOT_CLASS,
    inputAttrs: HONEYPOT_INPUT_ATTRS,
    styleText: `.${{OS_HONEYPOT_CLASS}}{{${{HONEYPOT_HIDE_CSS}}}}`,
    formPath: form.honeypotFormPath,
  }})
}}

export const botDefenseFormBelt = Object.freeze({{
  forms: botDefenseDefaultForms,
  providerOrder: botChallengeProviderOrder,
  fallbackOrder: botChallengeFallbackOrder,
  siteKeyEnv: botChallengeSiteKeyEnv,
  honeypotInputAttrs: HONEYPOT_INPUT_ATTRS,
  honeypotHideCss: HONEYPOT_HIDE_CSS,
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
    "DEFAULT_BOT_DEFENSE_DASHBOARD_PATH",
    "DEFAULT_BOT_DEFENSE_FORMS_PATH",
    "DEFAULT_HONEYPOT_BRIDGE_PATH",
    "DEFAULT_HONEYPOT_IMPORT",
    "list_bot_defense_forms",
    "list_bot_defense_providers",
    "render_bot_defense_scaffold",
]
