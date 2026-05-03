"""SC.13.1 -- Bot defense AS.3 reuse scaffold tests."""

from __future__ import annotations

import re

import pytest

from backend.auth_provisioning import (
    BotDefenseScaffoldOptions,
    list_bot_defense_forms,
    list_bot_defense_providers,
    render_bot_defense_scaffold,
)
from backend.security import bot_challenge
from backend.security import honeypot
from backend.security import turnstile_form_verifier as turnstile_forms


def _dashboard_bucket(content: str, name: str) -> set[str]:
    start = content.index(f"export const {name} = Object.freeze(new Set<string>([")
    end = content.index("]))", start)
    return set(re.findall(r"\bOUTCOME_[A-Z0-9_]+\b", content[start:end]))


def test_lists_as3_bot_challenge_providers() -> None:
    assert list_bot_defense_providers() == [
        "turnstile",
        "recaptcha_v2",
        "recaptcha_v3",
        "hcaptcha",
    ]


def test_lists_sc_13_2_default_forms() -> None:
    assert list_bot_defense_forms() == [
        "login",
        "signup",
        "password-reset",
        "contact",
        "comment",
    ]


def test_render_reuses_as3_generated_app_bridge() -> None:
    result = render_bot_defense_scaffold(
        BotDefenseScaffoldOptions(bot_challenge_import="../bot-challenge")
    )

    assert [item.path for item in result.files] == [
        "auth/bot-challenge.ts",
        "auth/honeypot.ts",
        "auth/bot-defense-forms.ts",
        "auth/bot-defense-dashboard.ts",
    ]
    content = result.files[0].content
    assert 'from "../bot-challenge"' in content
    assert "verifyWithFallback" in content
    assert "verifyAndEnforce" in content
    assert "pickProvider" in content
    assert "BOT_CHALLENGE_REJECTED_CODE" in content
    assert "OUTCOME_JSFAIL_FALLBACK_RECAPTCHA" in content
    assert "https://challenges.cloudflare.com" not in content
    assert "https://www.google.com/recaptcha" not in content
    assert "https://hcaptcha.com/siteverify" not in content


def test_render_sc_13_2_default_form_belt() -> None:
    result = render_bot_defense_scaffold()
    forms = {item.form: item for item in result.forms}
    content = result.files[2].content

    assert list(forms) == [
        "login",
        "signup",
        "password-reset",
        "contact",
        "comment",
    ]
    assert forms["login"].widget_action == "login"
    assert forms["signup"].widget_action == "signup"
    assert forms["password-reset"].widget_action == "pwreset"
    assert forms["contact"].widget_action == "contact"
    assert forms["comment"].widget_action == "comment"
    assert set(item.token_body_field for item in forms.values()) == {"turnstile_token"}
    assert 'from "./bot-challenge"' in content
    assert "botDefenseDefaultForms" in content
    assert 'form: "password-reset", widgetAction: "pwreset"' in content
    assert 'form: "comment", widgetAction: "comment"' in content
    assert "botDefenseVerifyContextForForm" in content
    assert "expectedAction: form.widgetAction" in content


def test_render_sc_13_3_honeypot_bridge_reuses_as4_generated_app_twin() -> None:
    result = render_bot_defense_scaffold(
        BotDefenseScaffoldOptions(honeypot_import="../honeypot")
    )

    content = result.files[1].content
    assert result.files[1].path == "auth/honeypot.ts"
    assert 'from "../honeypot"' in content
    assert "honeypotFieldName" in content
    assert "HONEYPOT_INPUT_ATTRS" in content
    assert "OS_HONEYPOT_CLASS" in content
    assert 'export const ANONYMOUS_TENANT_ID = "_anonymous"' in content
    assert "validateHoneypotAndEnforce" in content


def test_render_sc_13_3_default_forms_carry_as4_honeypot_paths() -> None:
    result = render_bot_defense_scaffold()
    forms = {item.form: item for item in result.forms}
    content = result.files[2].content

    assert forms["login"].honeypot_form_path == "/api/v1/auth/login"
    assert forms["signup"].honeypot_form_path == "/api/v1/auth/signup"
    assert forms["password-reset"].honeypot_form_path == "/api/v1/auth/password-reset"
    assert forms["contact"].honeypot_form_path == "/api/v1/auth/contact"
    assert forms["comment"].honeypot_form_path is None
    assert set(
        item.honeypot_form_path
        for item in forms.values()
        if item.honeypot_form_path is not None
    ) == set(honeypot._FORM_PREFIXES.keys())
    assert 'honeypotFormPath: "/api/v1/auth/login"' in content
    assert 'honeypotFormPath: "/api/v1/auth/password-reset"' in content
    assert 'form: "comment", widgetAction: "comment", tokenBodyField: "turnstile_token", honeypotFormPath: null' in content
    assert "botDefenseHoneypotFieldForForm" in content
    assert "honeypotFieldName(form.honeypotFormPath" in content
    assert "inputAttrs: HONEYPOT_INPUT_ATTRS" in content
    assert "styleText: `.${OS_HONEYPOT_CLASS}{${HONEYPOT_HIDE_CSS}}`" in content


def test_render_sc_13_4_challenge_ratio_dashboard() -> None:
    result = render_bot_defense_scaffold()
    content = result.files[3].content

    assert result.files[3].path == "auth/bot-defense-dashboard.ts"
    assert 'from "./bot-challenge"' in content
    assert "BotDefenseChallengeDashboardSummary" in content
    assert "botDefenseChallengeDashboard" in content
    assert "passCount" in content
    assert "failCount" in content
    assert "fallbackCount" in content
    assert "passRatio: safeRatio(passCount, total)" in content
    assert "failRatio: safeRatio(failCount, total)" in content
    assert "fallbackRatio: safeRatio(fallbackCount, total)" in content
    assert "OUTCOME_JSFAIL_FALLBACK_RECAPTCHA" in content
    assert "OUTCOME_JSFAIL_FALLBACK_HCAPTCHA" in content


def test_sc_13_5_form_belt_tracks_as6_form_constants() -> None:
    result = render_bot_defense_scaffold()
    forms = {item.form: item for item in result.forms}

    assert forms["login"].widget_action == turnstile_forms.FORM_ACTION_LOGIN
    assert forms["signup"].widget_action == turnstile_forms.FORM_ACTION_SIGNUP
    assert forms["password-reset"].widget_action == turnstile_forms.FORM_ACTION_PASSWORD_RESET
    assert forms["contact"].widget_action == turnstile_forms.FORM_ACTION_CONTACT
    assert forms["login"].honeypot_form_path == turnstile_forms.FORM_PATH_LOGIN
    assert forms["signup"].honeypot_form_path == turnstile_forms.FORM_PATH_SIGNUP
    assert forms["password-reset"].honeypot_form_path == turnstile_forms.FORM_PATH_PASSWORD_RESET
    assert forms["contact"].honeypot_form_path == turnstile_forms.FORM_PATH_CONTACT


def test_sc_13_5_dashboard_buckets_cover_every_as3_outcome_once() -> None:
    content = render_bot_defense_scaffold().files[3].content
    pass_bucket = _dashboard_bucket(content, "botDefensePassOutcomes")
    fail_bucket = _dashboard_bucket(content, "botDefenseFailOutcomes")
    fallback_bucket = _dashboard_bucket(content, "botDefenseFallbackOutcomes")

    assert not (pass_bucket & fail_bucket)
    assert not (pass_bucket & fallback_bucket)
    assert not (fail_bucket & fallback_bucket)
    assert pass_bucket | fail_bucket | fallback_bucket == {
        name
        for name, value in vars(bot_challenge).items()
        if name.startswith("OUTCOME_") and value in bot_challenge.ALL_OUTCOMES
    }


def test_sc_13_5_result_notes_name_each_sc_13_layer() -> None:
    result = render_bot_defense_scaffold()
    notes = "\n".join(result.notes)
    forms = result.to_dict()["forms"]

    assert "SC.13.2 defaults cover login/signup/password-reset/contact/comment forms" in notes
    assert "reuses AS.4 templates/_shared/honeypot" in notes
    assert "SC.13.4 dashboard exposes challenge pass/fail/fallback ratios" in notes
    assert {item["source"] for item in forms} == {"sc.13.2"}


def test_provider_env_manifest_reuses_as3_secret_env_names() -> None:
    result = render_bot_defense_scaffold()
    providers = {item.provider: item for item in result.providers}

    assert providers["turnstile"].secret_env == bot_challenge.secret_env_for(
        bot_challenge.Provider.TURNSTILE
    )
    assert providers["recaptcha_v2"].secret_env == bot_challenge.secret_env_for(
        bot_challenge.Provider.RECAPTCHA_V2
    )
    assert providers["recaptcha_v3"].secret_env == bot_challenge.secret_env_for(
        bot_challenge.Provider.RECAPTCHA_V3
    )
    assert providers["hcaptcha"].secret_env == bot_challenge.secret_env_for(
        bot_challenge.Provider.HCAPTCHA
    )
    assert providers["turnstile"].site_key_env == "NEXT_PUBLIC_TURNSTILE_SITE_KEY"
    assert providers["recaptcha_v3"].site_key_env == "NEXT_PUBLIC_RECAPTCHA_SITE_KEY"
    assert providers["hcaptcha"].site_key_env == "NEXT_PUBLIC_HCAPTCHA_SITE_KEY"


def test_env_manifest_declares_sensitive_server_secrets_once() -> None:
    result = render_bot_defense_scaffold()
    env = {item.name: item for item in result.env}

    assert env["BOT_CHALLENGE_PROVIDER"].source == "sc.13.1"
    assert env["NEXT_PUBLIC_TURNSTILE_SITE_KEY"].sensitive is False
    assert env["NEXT_PUBLIC_RECAPTCHA_SITE_KEY"].sensitive is False
    assert env["NEXT_PUBLIC_HCAPTCHA_SITE_KEY"].sensitive is False
    assert env["OMNISIGHT_TURNSTILE_SECRET"].sensitive is True
    assert env["OMNISIGHT_RECAPTCHA_SECRET"].sensitive is True
    assert env["OMNISIGHT_HCAPTCHA_SECRET"].sensitive is True
    assert list(env).count("OMNISIGHT_RECAPTCHA_SECRET") == 1


def test_result_to_dict_is_json_ready() -> None:
    result = render_bot_defense_scaffold().to_dict()

    assert result["files"][0]["path"] == "auth/bot-challenge.ts"
    assert result["files"][1]["path"] == "auth/honeypot.ts"
    assert result["files"][2]["path"] == "auth/bot-defense-forms.ts"
    assert result["files"][3]["path"] == "auth/bot-defense-dashboard.ts"
    assert result["providers"][0]["provider"] == "turnstile"
    assert result["forms"][0]["form"] == "login"
    assert result["forms"][2]["widget_action"] == "pwreset"
    assert result["forms"][2]["honeypot_form_path"] == "/api/v1/auth/password-reset"
    assert result["forms"][4]["honeypot_form_path"] is None
    assert result["env"][0]["name"] == "BOT_CHALLENGE_PROVIDER"
    assert result["dependencies"] == []


@pytest.mark.parametrize(
    "field",
    [
        "bot_challenge_import",
        "honeypot_import",
        "bridge_path",
        "honeypot_bridge_path",
        "forms_path",
        "dashboard_path",
    ],
)
def test_options_validate_required_fields(field: str) -> None:
    kwargs = {field: "   "}

    with pytest.raises(ValueError, match=field):
        render_bot_defense_scaffold(BotDefenseScaffoldOptions(**kwargs))
