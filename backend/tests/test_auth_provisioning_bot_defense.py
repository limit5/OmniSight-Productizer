"""SC.13.1 -- Bot defense AS.3 reuse scaffold tests."""

from __future__ import annotations

import pytest

from backend.auth_provisioning import (
    BotDefenseScaffoldOptions,
    list_bot_defense_providers,
    render_bot_defense_scaffold,
)
from backend.security import bot_challenge


def test_lists_as3_bot_challenge_providers() -> None:
    assert list_bot_defense_providers() == [
        "turnstile",
        "recaptcha_v2",
        "recaptcha_v3",
        "hcaptcha",
    ]


def test_render_reuses_as3_generated_app_bridge() -> None:
    result = render_bot_defense_scaffold(
        BotDefenseScaffoldOptions(bot_challenge_import="../bot-challenge")
    )

    assert [item.path for item in result.files] == ["auth/bot-challenge.ts"]
    content = result.files[0].content
    assert 'from "../bot-challenge"' in content
    assert "verifyWithFallback" in content
    assert "verifyAndEnforce" in content
    assert "pickProvider" in content
    assert "BOT_CHALLENGE_REJECTED_CODE" in content
    assert "https://challenges.cloudflare.com" not in content
    assert "https://www.google.com/recaptcha" not in content
    assert "https://hcaptcha.com/siteverify" not in content


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
    assert result["providers"][0]["provider"] == "turnstile"
    assert result["env"][0]["name"] == "BOT_CHALLENGE_PROVIDER"
    assert result["dependencies"] == []


@pytest.mark.parametrize(
    "field",
    ["bot_challenge_import", "bridge_path"],
)
def test_options_validate_required_fields(field: str) -> None:
    kwargs = {field: "   "}

    with pytest.raises(ValueError, match=field):
        render_bot_defense_scaffold(BotDefenseScaffoldOptions(**kwargs))
