"""OP-1842 (P2.1) — per-project pinned product-source resolution.

The SOURCE-side analog of ``test_delivery_target`` (OP-1837 / 1B). Covers the
acceptance criteria:

* a JIRA project WITH a configured source resolves to its ProductSource;
* an UNCONFIGURED project (and ``project_key=None``) resolves to ``None`` —
  the current in-repo build flow (back-compat);
* a configured entry missing ``pinned_ref`` is rejected (no blind-HEAD), as is
  one with a missing ``repo_url`` or an unrecognised ``tier``;
* the clone credential is resolved through the ``git_accounts`` model (mocked),
  never inlined in source/config.

Scope: resolver + config only. The runner clone/build wiring (P2.2) and the B2
PR flow (P2.4) are explicitly NOT exercised here — they are not implemented.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend import db_context, git_credentials
from backend.agents import product_source as ps
from backend.config import settings


@pytest.fixture(autouse=True)
def _reset_context(monkeypatch):
    """Keep the product-source config + tenant ContextVar from leaking across
    tests (and from the developer's real ``.env``)."""
    db_context.set_tenant_id(None)
    monkeypatch.setattr(settings, "product_sources", "", raising=False)
    try:
        yield
    finally:
        db_context.set_tenant_id(None)


def _configure(monkeypatch, mapping: dict) -> None:
    monkeypatch.setattr(
        settings, "product_sources", json.dumps(mapping), raising=False
    )


def _fake_pick_by_id(monkeypatch, row):
    async def _fake(account_id, *, tenant_id=None, touch=True):
        _fake.seen = (account_id, tenant_id)
        return row

    _fake.seen = None
    monkeypatch.setattr(git_credentials, "pick_by_id", _fake)
    return _fake


# ── resolve_product_source: configured project → ProductSource ────────


def test_configured_project_resolves_to_its_source(monkeypatch):
    _configure(monkeypatch, {
        "OP": {
            "repo_url": "ssh://git@github.com/operator/camviewpro-android",
            "tier": "consumer",
            "branch": "main",
            "pinned_ref": "v3.2.0",
            "git_account_ref": "camviewpro-ro",
        }
    })
    source = ps.resolve_product_source("OP")
    assert source is not None
    assert source.repo_url == "ssh://git@github.com/operator/camviewpro-android"
    assert source.tier == "consumer"
    assert source.branch == "main"
    assert source.pinned_ref == "v3.2.0"
    assert source.git_account_ref == "camviewpro-ro"


def test_configured_source_normalises_tier_case(monkeypatch):
    _configure(monkeypatch, {
        "OP": {
            "repo_url": "ssh://git@h/p",
            "tier": "Medical",
            "pinned_ref": "abc1234",
            "git_account_ref": "r",
        }
    })
    source = ps.resolve_product_source("OP")
    assert source is not None
    assert source.tier == "medical"


def test_configured_source_branch_optional(monkeypatch):
    _configure(monkeypatch, {
        "OP": {
            "repo_url": "ssh://git@h/p",
            "tier": "automotive",
            "pinned_ref": "9f8e7d6",
        }
    })
    source = ps.resolve_product_source("OP")
    assert source is not None
    assert source.branch is None
    assert source.git_account_ref is None  # public source — no credential


# ── resolve_product_source: unconfigured → None (back-compat) ─────────


def test_unconfigured_project_resolves_to_none(monkeypatch):
    assert ps.resolve_product_source("OP") is None


def test_none_project_key_resolves_to_none(monkeypatch):
    assert ps.resolve_product_source(None) is None


def test_unknown_project_in_map_resolves_to_none(monkeypatch):
    _configure(monkeypatch, {
        "OP": {"repo_url": "ssh://git@h/p", "tier": "consumer", "pinned_ref": "v1"}
    })
    assert ps.resolve_product_source("OTHER") is None


def test_malformed_json_resolves_to_none(monkeypatch):
    monkeypatch.setattr(settings, "product_sources", "{not json", raising=False)
    assert ps.resolve_product_source("OP") is None


def test_non_object_json_resolves_to_none(monkeypatch):
    monkeypatch.setattr(settings, "product_sources", '["a", "b"]', raising=False)
    assert ps.resolve_product_source("OP") is None


# ── resolve_product_source: malformed entry is fail-closed ────────────


def test_missing_pinned_ref_is_rejected(monkeypatch):
    """A configured source with no pinned_ref → reject blind-HEAD."""
    _configure(monkeypatch, {
        "OP": {"repo_url": "ssh://git@h/p", "tier": "consumer", "git_account_ref": "r"}
    })
    with pytest.raises(ps.ProductSourceError):
        ps.resolve_product_source("OP")


def test_empty_pinned_ref_is_rejected(monkeypatch):
    _configure(monkeypatch, {
        "OP": {"repo_url": "ssh://git@h/p", "tier": "consumer", "pinned_ref": "  "}
    })
    with pytest.raises(ps.ProductSourceError):
        ps.resolve_product_source("OP")


def test_missing_repo_url_is_rejected(monkeypatch):
    _configure(monkeypatch, {
        "OP": {"tier": "consumer", "pinned_ref": "v1"}
    })
    with pytest.raises(ps.ProductSourceError):
        ps.resolve_product_source("OP")


def test_invalid_tier_is_rejected(monkeypatch):
    _configure(monkeypatch, {
        "OP": {"repo_url": "ssh://git@h/p", "tier": "enterprise", "pinned_ref": "v1"}
    })
    with pytest.raises(ps.ProductSourceError):
        ps.resolve_product_source("OP")


def test_missing_tier_is_rejected(monkeypatch):
    _configure(monkeypatch, {
        "OP": {"repo_url": "ssh://git@h/p", "pinned_ref": "v1"}
    })
    with pytest.raises(ps.ProductSourceError):
        ps.resolve_product_source("OP")


# ── resolve_product_source_credential: via git_accounts, never inlined ─


def test_credential_resolved_via_git_accounts(monkeypatch):
    row = {"id": "camviewpro-ro", "ssh_key": "/keys/camviewpro-ro", "username": "ci"}
    fake = _fake_pick_by_id(monkeypatch, row)
    source = ps.ProductSource(
        repo_url="ssh://git@github.com/operator/camviewpro-android",
        tier="consumer",
        branch="main",
        pinned_ref="v3.2.0",
        git_account_ref="camviewpro-ro",
    )
    resolved = asyncio.run(
        ps.resolve_product_source_credential(source, tenant_id="t-op")
    )
    assert resolved["ssh_key"] == "/keys/camviewpro-ro"
    # The id + tenant flowed into the git_accounts lookup (creds not inlined).
    assert fake.seen == ("camviewpro-ro", "t-op")


def test_credential_missing_ref_is_refused(monkeypatch):
    source = ps.ProductSource(
        repo_url="ssh://git@h/p", tier="consumer", branch=None,
        pinned_ref="v1", git_account_ref=None,
    )
    with pytest.raises(ps.ProductSourceError):
        asyncio.run(ps.resolve_product_source_credential(source))


def test_credential_unknown_row_is_refused(monkeypatch):
    _fake_pick_by_id(monkeypatch, None)
    source = ps.ProductSource(
        repo_url="ssh://git@h/p", tier="consumer", branch=None,
        pinned_ref="v1", git_account_ref="missing",
    )
    with pytest.raises(ps.ProductSourceError):
        asyncio.run(ps.resolve_product_source_credential(source))


def test_no_inlined_private_key_in_module():
    """Defensive: the resolution module must not inline private-key material."""
    src = Path(ps.__file__).read_text(encoding="utf-8")
    assert "BEGIN OPENSSH PRIVATE KEY" not in src
    assert "BEGIN RSA PRIVATE KEY" not in src
