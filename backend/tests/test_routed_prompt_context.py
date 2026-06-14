"""Tests for OP-2198 / R.5 — routed tickets do not inherit productizer context.

A routed (repo:-labelled) ticket runs under the omnisight-self tenant but must
NOT receive the PRODUCTIZER repo's internal SOP corpus (lessons / anti-patterns
/ CLAUDE.md doc-rules) — those describe productizer process, not the routed
repo. A normal productizer ticket still gets them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from backend.agents import jira_dispatch, routed_repo, runner_tenant


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("arj_r5", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_client():
    class _C:
        agent_class = "subscription-claude"
        bot_email = "b@x"
        bot_account_id = "acc"
    return _C()


@pytest.fixture
def patch_issue(monkeypatch):
    def _set(labels):
        payload = {"fields": {"summary": "stub", "labels": labels, "components": []}}
        monkeypatch.setattr(jira_dispatch, "_request", lambda *a, **kw: payload)
    return _set


@pytest.fixture(autouse=True)
def self_tenant(runner, monkeypatch):
    # Force the omnisight-self tenant context for all these tests (both routed
    # and productizer tickets are self-tenant — R.5 distinguishes by repo:).
    # _build_prompt reads db_context.current_tenant_id() first, then falls back
    # to runner_tenant.resolve_tenant_id(labels).
    monkeypatch.setattr(
        runner.db_context, "current_tenant_id",
        lambda: runner_tenant.OMNISIGHT_SELF_TENANT, raising=False,
    )


DOCRULES_MARKER = "Documentation rules (per CLAUDE.md L1"


def test_productizer_ticket_gets_internal_docrules(runner, fake_client, patch_issue, monkeypatch):
    monkeypatch.setattr(routed_repo.settings, "routed_repos", "", raising=False)
    patch_issue(["area:backend", "tier:M"])  # no repo: label → productizer
    prompt = runner._build_prompt(fake_client, "OP-1000", "body")
    assert DOCRULES_MARKER in prompt  # productizer self-tenant keeps internal context


def test_routed_ticket_strips_internal_docrules(runner, fake_client, patch_issue, monkeypatch):
    monkeypatch.setattr(
        routed_repo.settings, "routed_repos",
        '{"conference-appliance": {"gerrit_url": "ssh://h/omnisight/conference-appliance"}}',
        raising=False,
    )
    patch_issue(["area:embedded", "tier:M", "repo:conference-appliance"])
    prompt = runner._build_prompt(fake_client, "OP-2170", "body")
    # the productizer-internal doc-rules must NOT leak into a routed prompt
    assert DOCRULES_MARKER not in prompt
    # but the ticket key + body are still there (prompt is otherwise built)
    assert "OP-2170" in prompt
    assert "body" in prompt


def test_unresolvable_repo_label_fails_safe_to_strip(runner, fake_client, patch_issue, monkeypatch):
    # repo: present but no config → R.5 fails safe to "routed" (strip), never
    # leaks productizer context. (R.1 is what actually abstains on pickup.)
    monkeypatch.setattr(routed_repo.settings, "routed_repos", "", raising=False)
    patch_issue(["repo:does-not-exist", "tier:M"])
    prompt = runner._build_prompt(fake_client, "OP-9", "body")
    assert DOCRULES_MARKER not in prompt
