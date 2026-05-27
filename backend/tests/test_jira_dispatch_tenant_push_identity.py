"""OP-1779 (1A.2) — per-tenant Gerrit push identity, closes L6.

Verifies that the push/auth path in :mod:`backend.agents.jira_dispatch`:

* keeps the internal ``omnisight-self`` tenant on the shared bot identity +
  the OmniSight project (back-compat), and
* forces any customer tenant onto its OWN ``git_accounts`` Gerrit credential,
  refusing the shared bot key / the OmniSight project / another tenant's row
  (fail-closed) rather than ever borrowing the bot identity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import db_context, git_credentials
from backend.agents import jira_dispatch as jd
from backend.agents import runner_tenant


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    """Keep the tenant ContextVar from leaking across tests (or into the
    sibling push-semantics suite that assumes a no-tenant-bound context)."""
    db_context.set_tenant_id(None)
    try:
        yield
    finally:
        db_context.set_tenant_id(None)


def _fake_registry(monkeypatch, rows: list[dict]) -> None:
    """Make ``get_credential_registry_async`` return *rows* for any tenant."""

    async def _fake(tenant_id=None, *, enabled_only=True):
        return list(rows)

    monkeypatch.setattr(git_credentials, "get_credential_registry_async", _fake)


def _gerrit_row(**over) -> dict:
    row = {
        "id": "acme-gerrit",
        "tenant_id": "t-acme",
        "platform": "gerrit",
        "instance_url": "https://gerrit.acme.example",
        "url": "https://gerrit.acme.example",
        "username": "acme-ci",
        "ssh_key": "/keys/acme-ci-ed25519",
        "ssh_host": "gerrit.acme.example",
        "ssh_port": 29418,
        "project": "acme/widgets",
        "is_default": True,
        "enabled": True,
    }
    row.update(over)
    return row


# ── omnisight-self / back-compat ─────────────────────────────────────


def test_self_unbound_context_uses_bot_identity_and_omnisight_project(monkeypatch):
    """No tenant bound → legacy bot identity + OmniSight project (is_bot)."""
    key = Path("/keys/gerrit-claude-bot-ed25519")
    monkeypatch.setattr(
        jd, "_gerrit_auth_for_instance",
        lambda agent_class, instance_id=None: ("claude-bot", key),
    )
    ident = jd.resolve_gerrit_push_identity("subscription-claude")
    assert ident.is_bot is True
    assert ident.tenant_id == runner_tenant.OMNISIGHT_SELF_TENANT
    assert ident.ssh_user == "claude-bot"
    assert ident.ssh_key == key
    assert ident.project == jd.GERRIT_PROJECT_PATH
    assert ident.ssh_url == (
        "ssh://claude-bot@sora.services:29418/omnisight/OmniSight-Productizer"
    )


def test_self_explicitly_bound_uses_bot_identity(monkeypatch):
    db_context.set_tenant_id(runner_tenant.OMNISIGHT_SELF_TENANT)
    key = Path("/keys/gerrit-codex-bot-ed25519")
    monkeypatch.setattr(
        jd, "_gerrit_auth_for_instance",
        lambda agent_class, instance_id=None: ("codex-bot", key),
    )
    ident = jd.resolve_gerrit_push_identity("subscription-codex")
    assert ident.is_bot is True
    assert ident.project == jd.GERRIT_PROJECT_PATH


# ── customer tenant: happy path via its own credential ───────────────


def test_customer_tenant_uses_its_own_credential(monkeypatch):
    db_context.set_tenant_id("t-acme")
    _fake_registry(monkeypatch, [_gerrit_row()])
    ident = jd.resolve_gerrit_push_identity("subscription-claude")
    assert ident.is_bot is False
    assert ident.tenant_id == "t-acme"
    assert ident.ssh_user == "acme-ci"
    assert ident.ssh_key == Path("/keys/acme-ci-ed25519")
    assert ident.ssh_host == "gerrit.acme.example"
    assert ident.project == "acme/widgets"
    # Scoped to the tenant's own project — NOT OmniSight's.
    assert jd.GERRIT_PROJECT_PATH not in ident.ssh_url
    assert ident.ssh_url == "ssh://acme-ci@gerrit.acme.example:29418/acme/widgets"


# ── customer tenant: fail-closed isolation guards (L6) ───────────────


def test_customer_tenant_no_credential_is_refused(monkeypatch):
    db_context.set_tenant_id("t-acme")
    _fake_registry(monkeypatch, [])
    with pytest.raises(jd.TenantPushIdentityError) as exc:
        jd.resolve_gerrit_push_identity("subscription-claude")
    assert "shared bot key" in str(exc.value)


def test_customer_tenant_shim_fallback_row_excluded(monkeypatch):
    """A legacy-shim OmniSight row (tenant_id='t-default') is NOT this
    customer's row, so it must not satisfy the customer push."""
    db_context.set_tenant_id("t-acme")
    shim = _gerrit_row(
        id="default-gerrit", tenant_id="t-default",
        project=jd.GERRIT_PROJECT_PATH,
    )
    _fake_registry(monkeypatch, [shim])
    with pytest.raises(jd.TenantPushIdentityError):
        jd.resolve_gerrit_push_identity("subscription-claude")


def test_customer_tenant_pointing_at_omnisight_project_refused(monkeypatch):
    db_context.set_tenant_id("t-acme")
    _fake_registry(monkeypatch, [_gerrit_row(project=jd.GERRIT_PROJECT_PATH)])
    with pytest.raises(jd.TenantPushIdentityError) as exc:
        jd.resolve_gerrit_push_identity("subscription-claude")
    assert "OmniSight project" in str(exc.value)


def test_customer_tenant_resolving_shared_bot_key_refused(monkeypatch):
    db_context.set_tenant_id("t-acme")
    bot_key = str(jd.CRED_DIR / "gerrit-claude-bot-ed25519")
    _fake_registry(monkeypatch, [_gerrit_row(ssh_key=bot_key)])
    with pytest.raises(jd.TenantPushIdentityError) as exc:
        jd.resolve_gerrit_push_identity("subscription-claude")
    assert "shared bot key" in str(exc.value)


def test_customer_tenant_per_instance_bot_key_refused(monkeypatch):
    """Per-instance bot keys (gerrit-claude-bot-2-...) are also refused."""
    db_context.set_tenant_id("t-acme")
    bot_key = str(jd.CRED_DIR / "gerrit-codex-bot-3-ed25519")
    _fake_registry(monkeypatch, [_gerrit_row(ssh_key=bot_key)])
    with pytest.raises(jd.TenantPushIdentityError):
        jd.resolve_gerrit_push_identity("subscription-codex")


def test_customer_tenant_missing_project_refused(monkeypatch):
    db_context.set_tenant_id("t-acme")
    _fake_registry(monkeypatch, [_gerrit_row(project="")])
    with pytest.raises(jd.TenantPushIdentityError) as exc:
        jd.resolve_gerrit_push_identity("subscription-claude")
    assert "project namespace" in str(exc.value)


def test_disabled_customer_row_not_eligible(monkeypatch):
    db_context.set_tenant_id("t-acme")
    _fake_registry(monkeypatch, [_gerrit_row(enabled=False)])
    with pytest.raises(jd.TenantPushIdentityError):
        jd.resolve_gerrit_push_identity("subscription-claude")


# ── end-to-end push behaviour ────────────────────────────────────────


def test_customer_push_refused_never_invokes_git(monkeypatch, tmp_path):
    """A customer tenant with no credential: push returns a hard failure and
    NO git/ssh subprocess is ever attempted (so the bot key can't leak)."""
    db_context.set_tenant_id("t-acme")
    _fake_registry(monkeypatch, [])

    called = {"n": 0}

    def _boom(fn, args, **kwargs):  # pragma: no cover — must NOT run
        called["n"] += 1
        raise AssertionError("git push must not run for a refused customer push")

    monkeypatch.setattr(jd.BREAKERS["gerrit_ssh"], "call", _boom)

    result = jd.push_to_gerrit_for_review(tmp_path, "subscription-claude")
    assert result.success is False
    assert called["n"] == 0
    assert "shared bot key" in result.detail


def test_self_push_unchanged(monkeypatch, tmp_path):
    """omnisight-self push path is byte-for-byte the legacy behaviour: bot
    key, OmniSight project SSH URL, clean success."""
    from backend.agents import auto_rebase, pre_review_self_fix

    db_context.set_tenant_id(runner_tenant.OMNISIGHT_SELF_TENANT)
    key = tmp_path / "gerrit-codex-bot-ed25519"
    key.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(
        jd, "_gerrit_auth_for_instance",
        lambda agent_class, instance_id=None: ("codex-bot", key),
    )
    monkeypatch.setattr(jd, "_head_change_id", lambda worktree_path: "Iabc123")

    captured = {}

    def fake_call(fn, args, **kwargs):
        import subprocess
        captured["argv"] = args
        blob = (
            "remote:   https://sora.services:29420/c/"
            "omnisight/OmniSight-Productizer/+/77 subject\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr=blob)

    monkeypatch.setattr(jd.BREAKERS["gerrit_ssh"], "call", fake_call)
    monkeypatch.setattr(auto_rebase, "load_owner_http_password", lambda user: "secret")
    monkeypatch.setattr(
        pre_review_self_fix, "self_fix_mergeability",
        lambda **kwargs: pre_review_self_fix.SelfFixResult(mergeable=True, attempts=1),
    )

    result = jd.push_to_gerrit_for_review(tmp_path, "subscription-codex")
    assert result.success is True
    assert result.change_number == 77
    # The push targeted the OmniSight project over the bot key.
    assert "omnisight/OmniSight-Productizer" in " ".join(captured["argv"])
