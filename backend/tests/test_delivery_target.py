"""OP-1837 (1B v1) — per-project delivery-target resolution + push threading.

Covers the acceptance criteria:

* a JIRA project WITH a configured target resolves to it;
* an UNCONFIGURED project (and ``project_key=None``) resolves to the
  OmniSight Gerrit default (back-compat);
* the push credential is resolved through the ``git_accounts`` model
  (mocked), never inlined in source/config;
* :func:`push_to_gerrit_for_review` threads the resolved target through —
  a configured project pushes to the customer repo/ref via the git_accounts
  credential and fails closed without one, while the default path (no
  ``project_key``) is untouched.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend import db_context, git_credentials
from backend.agents import delivery_target as dt
from backend.agents import jira_dispatch as jd
from backend.config import settings


@pytest.fixture(autouse=True)
def _reset_context(monkeypatch):
    """Keep the delivery-target config + tenant ContextVar from leaking
    across tests (and from the developer's real ``.env``)."""
    db_context.set_tenant_id(None)
    monkeypatch.setattr(settings, "delivery_targets", "", raising=False)
    try:
        yield
    finally:
        db_context.set_tenant_id(None)


def _configure(monkeypatch, mapping: dict) -> None:
    monkeypatch.setattr(
        settings, "delivery_targets", json.dumps(mapping), raising=False
    )


def _fake_pick_by_id(monkeypatch, row):
    async def _fake(account_id, *, tenant_id=None, touch=True):
        _fake.seen = (account_id, tenant_id)
        return row

    _fake.seen = None
    monkeypatch.setattr(git_credentials, "pick_by_id", _fake)
    return _fake


# ── resolve_delivery_target: configured project ──────────────────────


def test_configured_project_resolves_to_its_target(monkeypatch):
    _configure(monkeypatch, {
        "ACME": {
            "repo_url": "ssh://acme-ci@gerrit.acme.example:29418/acme/widgets",
            "ref_spec": "refs/heads/main",
            "git_account_ref": "acme-gerrit",
        }
    })
    target = dt.resolve_delivery_target("ACME")
    assert target.is_default is False
    assert target.repo_url == "ssh://acme-ci@gerrit.acme.example:29418/acme/widgets"
    assert target.ref_spec == "refs/heads/main"
    assert target.git_account_ref == "acme-gerrit"
    assert target.project_key == "ACME"
    # Scoped to the customer repo — NOT the OmniSight project.
    assert jd.GERRIT_PROJECT_PATH not in target.repo_url


def test_configured_entry_without_ref_spec_defaults_to_refs_for_target(monkeypatch):
    _configure(monkeypatch, {
        "ACME": {"repo_url": "ssh://x@h:29418/p", "git_account_ref": "acme-gerrit"}
    })
    target = dt.resolve_delivery_target("ACME", target="release-1.2")
    assert target.is_default is False
    assert target.ref_spec == "refs/for/release-1.2"


# ── resolve_delivery_target: unconfigured → OmniSight default ─────────


def test_unconfigured_project_resolves_to_omnisight_default(monkeypatch):
    target = dt.resolve_delivery_target("OP")
    assert target.is_default is True
    assert target.git_account_ref is None
    assert target.ref_spec == "refs/for/develop"
    assert jd.GERRIT_PROJECT_PATH in target.repo_url


def test_none_project_key_resolves_to_default(monkeypatch):
    target = dt.resolve_delivery_target(None)
    assert target.is_default is True
    assert jd.GERRIT_PROJECT_PATH in target.repo_url


def test_unknown_project_in_map_resolves_to_default(monkeypatch):
    _configure(monkeypatch, {
        "ACME": {"repo_url": "ssh://x@h:29418/p", "git_account_ref": "r"}
    })
    target = dt.resolve_delivery_target("OTHER")
    assert target.is_default is True


def test_entry_without_repo_url_falls_back_to_default(monkeypatch):
    _configure(monkeypatch, {"ACME": {"git_account_ref": "r"}})
    target = dt.resolve_delivery_target("ACME")
    assert target.is_default is True


def test_malformed_json_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(settings, "delivery_targets", "{not json", raising=False)
    target = dt.resolve_delivery_target("ACME")
    assert target.is_default is True


def test_non_object_json_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(settings, "delivery_targets", '["a", "b"]', raising=False)
    target = dt.resolve_delivery_target("ACME")
    assert target.is_default is True


# ── resolve_delivery_credential: via git_accounts, never inlined ──────


def test_credential_resolved_via_git_accounts(monkeypatch):
    row = {"id": "acme-gerrit", "ssh_key": "/keys/acme-ci-ed25519", "username": "acme-ci"}
    fake = _fake_pick_by_id(monkeypatch, row)
    target = dt.DeliveryTarget(
        repo_url="ssh://acme-ci@h:29418/acme/widgets",
        ref_spec="refs/heads/main",
        git_account_ref="acme-gerrit",
        is_default=False,
        project_key="ACME",
    )
    resolved = asyncio.run(dt.resolve_delivery_credential(target, tenant_id="t-acme"))
    assert resolved["ssh_key"] == "/keys/acme-ci-ed25519"
    # The id + tenant flowed into the git_accounts lookup (creds not inlined).
    assert fake.seen == ("acme-gerrit", "t-acme")


def test_credential_missing_ref_is_refused(monkeypatch):
    target = dt.DeliveryTarget(
        repo_url="ssh://x@h:29418/p", ref_spec="refs/heads/main",
        git_account_ref=None, is_default=False, project_key="ACME",
    )
    with pytest.raises(dt.DeliveryTargetError):
        asyncio.run(dt.resolve_delivery_credential(target))


def test_credential_unknown_row_is_refused(monkeypatch):
    _fake_pick_by_id(monkeypatch, None)
    target = dt.DeliveryTarget(
        repo_url="ssh://x@h:29418/p", ref_spec="refs/heads/main",
        git_account_ref="missing", is_default=False, project_key="ACME",
    )
    with pytest.raises(dt.DeliveryTargetError):
        asyncio.run(dt.resolve_delivery_credential(target))


def test_default_target_has_no_git_accounts_credential(monkeypatch):
    target = dt.resolve_delivery_target("OP")  # default
    with pytest.raises(dt.DeliveryTargetError):
        asyncio.run(dt.resolve_delivery_credential(target))


def test_no_inlined_private_key_in_module():
    """Defensive: the resolution module must not inline private-key material."""
    src = Path(dt.__file__).read_text(encoding="utf-8")
    assert "BEGIN OPENSSH PRIVATE KEY" not in src
    assert "BEGIN RSA PRIVATE KEY" not in src


# ── push_to_gerrit_for_review: target threaded through ────────────────


def test_push_threads_configured_target(monkeypatch, tmp_path):
    key = tmp_path / "acme-ci-ed25519"
    key.write_text("placeholder", encoding="utf-8")
    _configure(monkeypatch, {
        "ACME": {
            "repo_url": "ssh://acme-ci@gerrit.acme.example:29418/acme/widgets",
            "ref_spec": "refs/heads/main",
            "git_account_ref": "acme-gerrit",
        }
    })
    _fake_pick_by_id(
        monkeypatch,
        {"id": "acme-gerrit", "ssh_key": str(key), "username": "acme-ci"},
    )

    captured = {}

    def fake_call(fn, args, **kwargs):
        import subprocess
        captured["argv"] = args
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(jd.BREAKERS["gerrit_ssh"], "call", fake_call)

    result = jd.push_to_gerrit_for_review(
        tmp_path, "subscription-claude", project_key="ACME"
    )
    assert result.success is True
    argv = " ".join(captured["argv"])
    assert "ssh://acme-ci@gerrit.acme.example:29418/acme/widgets" in argv
    assert "HEAD:refs/heads/main" in argv
    # Neither the OmniSight project nor the bot identity is used.
    assert jd.GERRIT_PROJECT_PATH not in argv


def test_push_configured_target_fails_closed_without_credential(monkeypatch, tmp_path):
    _configure(monkeypatch, {
        "ACME": {
            "repo_url": "ssh://x@h:29418/p",
            "ref_spec": "refs/heads/main",
            "git_account_ref": "missing",
        }
    })
    _fake_pick_by_id(monkeypatch, None)  # row not found → fail closed

    called = {"n": 0}

    def _boom(fn, args, **kwargs):  # pragma: no cover — must NOT run
        called["n"] += 1
        raise AssertionError("git push must not run when the credential is unresolved")

    monkeypatch.setattr(jd.BREAKERS["gerrit_ssh"], "call", _boom)

    result = jd.push_to_gerrit_for_review(
        tmp_path, "subscription-claude", project_key="ACME"
    )
    assert result.success is False
    assert called["n"] == 0


def test_push_default_path_unchanged_for_unconfigured(monkeypatch, tmp_path):
    """No ``project_key`` → default delivery target → legacy bot identity +
    OmniSight project SSH URL (byte-identical back-compat)."""
    from backend.agents import auto_rebase, pre_review_self_fix

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
            "omnisight/OmniSight-Productizer/+/91 subject\n"
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
    assert result.change_number == 91
    assert jd.GERRIT_PROJECT_PATH in " ".join(captured["argv"])
