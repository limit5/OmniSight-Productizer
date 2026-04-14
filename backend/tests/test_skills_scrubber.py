"""Phase 62 S1 — PII / secret scrubber."""

from __future__ import annotations

import pytest

from backend.skills_scrubber import SAFETY_THRESHOLD, is_safe_to_promote, scrub


@pytest.mark.parametrize("payload,placeholder", [
    ("AKIAIOSFODNN7EXAMPLE", "[AWS_KEY]"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "[GITHUB_PAT]"),
    ("glpat-abc123def456ghi789jk", "[GITLAB_PAT]"),
    ("sk-anyOpenAIKey1234567890abcd", "[OPENAI_KEY]"),
    ("sk-ant-some-anthropic-key-1234567890", "[ANTHROPIC_KEY]"),
    ("xoxb-12345-67890-abcdefghij", "[SLACK_TOKEN]"),
])
def test_provider_keys_are_redacted(payload, placeholder):
    out, hits = scrub(f"value: {payload} end")
    assert placeholder in out
    assert payload not in out
    assert sum(hits.values()) >= 1


def test_jwt_redacted():
    jwt = "eyJabc.eyJdef.signature123"
    out, _ = scrub(f"Authorization: Bearer {jwt}")
    assert "[JWT]" in out
    assert jwt not in out


def test_ssh_private_key_block_redacted():
    block = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
             "MIIBlahBlahBlah==\n"
             "-----END OPENSSH PRIVATE KEY-----")
    out, _ = scrub(block)
    assert "[SSH_PRIVATE_KEY]" in out
    assert "MIIBlahBlahBlah" not in out


def test_env_assignment_form_redacted():
    out, _ = scrub('export API_KEY="hunter2supersecret"')
    assert "[REDACTED]" in out
    assert "hunter2supersecret" not in out


def test_email_redacted():
    out, _ = scrub("Contact john.doe@company.com for help")
    assert "[EMAIL]" in out
    assert "john.doe@company.com" not in out


def test_home_path_redacted():
    out, _ = scrub("Workspace at /home/alice/work/repo and /Users/bob/code")
    assert "[HOME_PATH]" in out
    assert "/home/alice" not in out
    assert "/Users/bob" not in out


def test_public_paths_passthrough():
    """We should NOT scrub /usr/bin, /opt/, /etc/ — those are not PII."""
    out, _ = scrub("Run /usr/bin/gcc; config in /etc/hosts")
    assert "/usr/bin/gcc" in out
    assert "/etc/hosts" in out


def test_ipv4_redacted_but_loopback_kept():
    out, _ = scrub("Server at 192.168.1.42, local at 127.0.0.1, gateway 10.0.0.1")
    assert "[IPV4]" in out
    assert "192.168.1.42" not in out
    assert "10.0.0.1" not in out
    assert "127.0.0.1" in out  # loopback exempt


def test_high_entropy_blob_redacted_last():
    """Generic blob catcher catches long opaque strings unrelated to
    the env-assign / provider-key prefixes."""
    blob = "a" * 50  # 50 char blob
    out, hits = scrub(f"checksum value {blob} ok")
    assert "[OPAQUE_BLOB]" in out
    assert hits["high_entropy"] == 1


def test_env_assign_wins_over_high_entropy():
    """`token: <blob>` should be caught by env_assign, not by the
    generic high-entropy fallback. Order matters."""
    blob = "x" * 50
    out, hits = scrub(f"token: {blob}")
    assert "[REDACTED]" in out
    assert hits["env_assign"] == 1
    assert hits["high_entropy"] == 0


def test_short_strings_pass_through():
    out, hits = scrub("Use rc=0 and len=15 for testing")
    assert sum(hits.values()) == 0
    assert "rc=0" in out


def test_scrub_empty_string():
    out, hits = scrub("")
    assert out == ""
    assert sum(hits.values()) == 0


def test_safety_threshold_under_limit_promotes():
    hits = scrub("Contact a@b.com and c@d.com")[1]
    assert is_safe_to_promote(hits)


def test_safety_threshold_over_limit_blocks_promotion():
    big = " ".join(f"user{i}@x.com" for i in range(SAFETY_THRESHOLD + 5))
    _, hits = scrub(big)
    assert not is_safe_to_promote(hits)


def test_multiple_classes_in_one_pass():
    payload = (
        "Email john@x.com, key=AKIAIOSFODNN7EXAMPLE, "
        "path /home/jane/work, server 10.20.30.40"
    )
    out, hits = scrub(payload)
    assert "[EMAIL]" in out
    assert "[AWS_KEY]" in out
    assert "[HOME_PATH]" in out
    assert "[IPV4]" in out
    assert hits["email"] == 1
    assert hits["aws_key"] == 1
    assert hits["home_path"] == 1
    assert hits["ipv4"] == 1
