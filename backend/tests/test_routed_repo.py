"""Tests for OP-2192 / R.0 — routed-repo resolver (multi-project runner routing).

R.0 is the safe floor: config field + dataclass + resolver, no call sites wired.
These tests pin the three resolution cases + the fail-closed contract.
"""

from __future__ import annotations

import pytest

from backend.agents import routed_repo
from backend.agents.routed_repo import (
    RoutedRepo,
    RoutedRepoError,
    resolve_routed_repo,
    routed_repo_label_value,
)


CONF_JSON = (
    '{"conference-appliance": {'
    '"gerrit_url": "ssh://claude-bot@sora.services:29418/omnisight/conference-appliance",'
    '"ref": "refs/for/develop", "context": "conference-appliance"}}'
)


@pytest.fixture
def set_routed(monkeypatch):
    def _set(raw: str) -> None:
        monkeypatch.setattr(routed_repo.settings, "routed_repos", raw, raising=False)
    return _set


# ── routed_repo_label_value ──────────────────────────────────────────────
def test_no_repo_label_returns_none():
    assert routed_repo_label_value(["area:backend", "tier:M"]) is None


def test_single_repo_label_extracted():
    assert routed_repo_label_value(["repo:conference-appliance", "tier:M"]) == "conference-appliance"


def test_repo_label_case_insensitive_prefix():
    assert routed_repo_label_value(["Repo:conference-appliance"]) == "conference-appliance"


def test_conflicting_repo_labels_raise():
    with pytest.raises(RoutedRepoError, match="conflicting"):
        routed_repo_label_value(["repo:a", "repo:b"])


# ── resolve_routed_repo: the three cases ─────────────────────────────────
def test_no_label_resolves_none_even_with_config(set_routed):
    # The common path: no repo: label → None → caller runs the normal
    # productizer path. Must NOT raise even when config is present.
    set_routed(CONF_JSON)
    assert resolve_routed_repo(["area:backend"]) is None


def test_no_label_no_config_is_inert(set_routed):
    set_routed("")
    assert resolve_routed_repo([]) is None


def test_resolves_to_routed_repo(set_routed):
    set_routed(CONF_JSON)
    rr = resolve_routed_repo(["repo:conference-appliance", "tier:M"])
    assert isinstance(rr, RoutedRepo)
    assert rr.name == "conference-appliance"
    assert rr.gerrit_url.endswith("/omnisight/conference-appliance")
    assert rr.ref == "refs/for/develop"
    assert rr.git_account_ref is None
    assert rr.context == "conference-appliance"


def test_ref_defaults_when_omitted(set_routed):
    set_routed('{"x": {"gerrit_url": "ssh://h/omnisight/x"}}')
    rr = resolve_routed_repo(["repo:x"])
    assert rr.ref == "refs/for/develop"
    assert rr.context is None


# ── fail-closed contract (the whole point of R.0) ────────────────────────
def test_repo_label_but_no_entry_raises(set_routed):
    set_routed(CONF_JSON)
    with pytest.raises(RoutedRepoError, match="no routed_repos entry"):
        resolve_routed_repo(["repo:does-not-exist"])


def test_repo_label_but_empty_config_raises(set_routed):
    set_routed("")
    with pytest.raises(RoutedRepoError, match="no routed_repos entry"):
        resolve_routed_repo(["repo:conference-appliance"])


def test_repo_label_but_entry_has_no_url_raises(set_routed):
    set_routed('{"conference-appliance": {"ref": "refs/for/develop"}}')
    with pytest.raises(RoutedRepoError, match="no gerrit_url"):
        resolve_routed_repo(["repo:conference-appliance"])


def test_malformed_json_with_repo_label_raises(set_routed):
    set_routed("{not valid json")
    with pytest.raises(RoutedRepoError, match="invalid JSON"):
        resolve_routed_repo(["repo:conference-appliance"])


def test_non_object_json_with_repo_label_raises(set_routed):
    set_routed("[1, 2, 3]")
    with pytest.raises(RoutedRepoError, match="expected a JSON object"):
        resolve_routed_repo(["repo:conference-appliance"])


def test_entry_not_an_object_raises(set_routed):
    set_routed('{"conference-appliance": "ssh://nope"}')
    with pytest.raises(RoutedRepoError, match="not an object"):
        resolve_routed_repo(["repo:conference-appliance"])
