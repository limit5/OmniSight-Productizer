"""R20 Phase 0 — secret_filter.redact unit tests.

Each test asserts (a) the secret is replaced with the right [REDACTED]
label and (b) the surrounding plain text is preserved. Tests double as
a regression suite when adding new patterns.
"""

from __future__ import annotations

import io
import logging

from backend.security.secret_filter import (
    SecretScrubbingFilter,
    install_logging_filter,
    redact,
    redact_for_log,
)


# ─── Provider-specific tokens ───


def test_github_pat_is_redacted():
    text = "Use ghp_AbCdEf1234567890qrstuvwxyzABCDEF12 to clone."
    out, fired = redact(text)
    assert "ghp_AbCdEf" not in out
    assert "[REDACTED:github_pat]" in out
    assert "github_pat" in fired


def test_github_oauth_is_redacted():
    text = "Token: gho_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    out, fired = redact(text)
    assert "[REDACTED:github_oauth]" in out
    assert "github_oauth" in fired


def test_gitlab_pat_is_redacted():
    text = "auth: glpat-abcdefghijklmnopqrst"
    out, fired = redact(text)
    assert "[REDACTED:gitlab_pat]" in out
    assert "gitlab_pat" in fired


def test_aws_access_key_is_redacted():
    text = "AKIAIOSFODNN7EXAMPLE is the key id"
    out, fired = redact(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED:aws_access_key]" in out


def test_aws_secret_assignment_is_redacted():
    text = 'aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
    out, fired = redact(text)
    assert "wJalrXUtnFEMI" not in out
    assert "[REDACTED:aws_secret]" in out


def test_slack_bot_token_is_redacted():
    text = "xoxb-1234-5678-AbCdEfGhIjKlMnOp is our bot"
    out, fired = redact(text)
    assert "xoxb-1234-5678" not in out
    assert "slack_token" in fired


def test_slack_webhook_is_redacted():
    text = "POST to https://hooks.slack.com/services/T0AAAAAAA/B0BBBBBBB/ccccccccccccccc"
    out, fired = redact(text)
    assert "ccccccccccccccc" not in out
    assert "slack_webhook" in fired


def test_stripe_secret_is_redacted():
    text = "sk_live_abcdefghijklmnop1234567890ABCDEF lives here"
    out, fired = redact(text)
    assert "sk_live_abcdefghij" not in out
    assert "stripe" in fired


def test_google_api_key_prefix_is_redacted():
    text = "GOOGLE_API_KEY=AIzaSyDabcdefghijklmnopqrstuvwxy123456789"
    out, fired = redact(text)
    assert "AIzaSyD" not in out
    assert "[REDACTED:google_api_key]" in out
    assert "google_api_key" in fired


def test_generic_api_key_assignment_is_redacted():
    text = 'x-api-key="prod_live_abcdefghijklmnopqrstuvwxyz123456"'
    out, fired = redact(text)
    assert "prod_live_abcdefghijklmnopqrstuvwxyz" not in out
    assert "[REDACTED:api_key]" in out
    assert "api_key_assignment" in fired


def test_anthropic_key_is_redacted_specifically():
    text = "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
    out, fired = redact(text)
    assert "[REDACTED:anthropic_key]" in out
    assert "anthropic" in fired
    # Ensure we matched as anthropic, not openai (sk- prefix collision)
    assert "[REDACTED:openai_key]" not in out


def test_openai_key_is_redacted():
    text = "use sk-abcdefghijklmnopqrstuvwxyz123456"
    out, fired = redact(text)
    assert "[REDACTED:openai_key]" in out


def test_bearer_header_is_redacted():
    text = "Authorization: Bearer abc.def.ghijklmnopqrst.uvwxyz123456"
    out, fired = redact(text)
    assert "abc.def.ghijklmnopqrst.uvwxyz123456" not in out
    assert "Bearer [REDACTED:token]" in out


def test_oauth_token_assignment_is_redacted():
    text = "refresh_token=rt_abcdefghijklmnopqrstuvwxyz1234567890"
    out, fired = redact(text)
    assert "rt_abcdefghijklmnopqrstuvwxyz" not in out
    assert "[REDACTED:oauth_token]" in out
    assert "oauth_token" in fired


def test_jwt_is_redacted():
    text = (
        "Cookie: session=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0IiwibmFtZSI6IkpvZSJ9"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out, fired = redact(text)
    assert "eyJhbGciOiJI" not in out
    assert "[REDACTED:jwt]" in out


def test_cookie_header_session_is_redacted():
    text = "Cookie: csrftoken=public; sessionid=abcdef1234567890abcdef1234567890; theme=dark"
    out, fired = redact(text)
    assert "abcdef1234567890" not in out
    assert "Cookie: [REDACTED:cookie]" in out
    assert "cookie" in fired


def test_database_url_with_password_is_redacted():
    text = "DATABASE_URL=postgresql://app_user:p@ssw0rd-value@pg-primary:5432/app"
    out, fired = redact(text)
    assert "p@ssw0rd-value" not in out
    assert "[REDACTED:database_url]" in out
    assert "database_url" in fired


def test_private_key_block_is_redacted_whole():
    text = (
        "Here's the key:\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAA...lots of base64...\n"
        "AAAAA\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
        "Use it carefully."
    )
    out, fired = redact(text)
    assert "BEGIN OPENSSH" not in out
    assert "[REDACTED:private_key_block]" in out
    assert "Use it carefully." in out  # surrounding text preserved


# ─── Internal hostname leaks ───


def test_internal_pg_host_is_redacted():
    text = "Connect to pg-primary on port 5432"
    out, fired = redact(text)
    assert "pg-primary" not in out
    assert "pg_internal" in fired


def test_internal_ai_cache_is_redacted():
    text = "Redis is at ai_cache:6379"
    out, fired = redact(text)
    assert "ai_cache" not in out
    assert "ai_internal" in fired


# ─── Allowlist (false-positive prevention) ───


def test_public_model_id_is_not_redacted():
    text = "Default model is claude-haiku-4-5-20251001 for fast responses."
    out, fired = redact(text)
    # Model id is in the allowlist; no redaction should fire even
    # though it's a long alphanumeric string.
    assert "claude-haiku-4-5-20251001" in out
    assert fired == []


# ─── Pass-through (no secrets) ───


def test_plain_text_passes_through():
    text = "How do I configure a git repo? Open Settings → Source Control."
    out, fired = redact(text)
    assert out == text
    assert fired == []


def test_empty_string_returns_empty():
    out, fired = redact("")
    assert out == ""
    assert fired == []


# ─── KS.1.7 logger scrubber ───


def test_redact_for_log_uses_uniform_placeholder():
    out, fired = redact_for_log("OpenAI key sk-abcdefghijklmnopqrstuvwxyz123456")
    assert out == "OpenAI key [REDACTED]"
    assert fired == ["openai"]


def test_secret_scrubbing_filter_redacts_formatted_log_args():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretScrubbingFilter())
    logger = logging.getLogger("backend.tests.secret_scrubber")
    old_handlers = list(logger.handlers)
    old_propagate = logger.propagate
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info("token=%s", "sk-abcdefghijklmnopqrstuvwxyz123456")
    finally:
        logger.handlers = old_handlers
        logger.propagate = old_propagate

    logged = stream.getvalue()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in logged
    assert "[REDACTED]" in logged


def test_install_logging_filter_is_idempotent_on_logger_and_handlers():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("backend.tests.install_secret_scrubber")
    old_handlers = list(logger.handlers)
    logger.handlers = [handler]
    try:
        first = install_logging_filter(logger)
        second = install_logging_filter(logger)
    finally:
        logger.handlers = old_handlers

    assert first is second
    assert len([f for f in logger.filters if isinstance(f, SecretScrubbingFilter)]) == 1
    assert len([f for f in handler.filters if isinstance(f, SecretScrubbingFilter)]) == 1


# ─── Multi-secret regression ───


def test_multiple_secrets_in_one_message_all_redacted():
    text = (
        "Setup: GITHUB=ghp_AbCdEf1234567890qrstuvwxyzABCDEF12 "
        "ANTHROPIC=sk-ant-api03-abcdefghijklmnopqrstuvwxyz "
        "host=pg-primary"
    )
    out, fired = redact(text)
    assert "ghp_AbCdEf" not in out
    assert "sk-ant-api03" not in out
    assert "pg-primary" not in out
    assert "github_pat" in fired
    assert "anthropic" in fired
    assert "pg_internal" in fired
