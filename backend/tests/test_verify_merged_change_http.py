"""β-F (leg-2) — ``verify_merged_change_http`` parity + fail-closed contract.

Locks the serving-safe REST verifier: it returns the SAME ``VerifiedMerge``
shape as the SSH ``verify_merged_change`` and — critically — makes the SAME
NON-BOT ``Code-Review>=2`` decision (via ``derive_ground_truths``), without
the SSH shell key. The bot-only-+2 → None case is the audit's load-bearing
assertion (the O6 ``merger-agent-bot`` exception must not confirm ground truth).
"""

from __future__ import annotations

import json

import pytest

from backend.agents import mcp_gerrit
from backend.config import settings

_PROJECT = "omnisight/verify-test"


def _rest_body(rows: list[dict]) -> str:
    """Gerrit REST body = XSSI guard line + JSON."""
    return ")]}'\n" + json.dumps(rows)


def _merged_row(**over) -> dict:
    row = {
        "_number": 4242,
        "project": _PROJECT,
        "branch": "develop",
        "change_id": "I1234567890abcdef1234567890abcdef12345678",
        "subject": "[OP-2462] fix the thing",
        "status": "MERGED",
        "current_revision": "abcdef1234567890",
        "revisions": {"abcdef1234567890": {"_number": 5}},
        "labels": {
            "Code-Review": {"all": [{"value": 2, "username": "sora", "name": "Sora"}]},
            "Verified": {"all": [{"value": 1, "username": "ci-bot"}]},
        },
    }
    row.update(over)
    return row


class _FakeResp:
    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _install_fake_httpx(monkeypatch, *, resp=None, raises=None) -> dict:
    import httpx

    captured: dict = {}

    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, auth=None):
            captured.update(url=url, params=params, auth=auth)
            if raises is not None:
                raise raises
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return captured


@pytest.fixture()
def http_creds(monkeypatch):
    monkeypatch.setattr(mcp_gerrit, "GERRIT_PROJECT_PATH", _PROJECT)
    monkeypatch.setattr(settings, "gerrit_url", "https://gerrit.test")
    monkeypatch.setattr(settings, "gerrit_http_user", "verify-account")
    monkeypatch.setattr(settings, "gerrit_http_password", "scoped-pw")


@pytest.mark.asyncio
async def test_happy_path_returns_canonical_identity(monkeypatch, http_creds):
    cap = _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([_merged_row()])))
    vm = await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT)
    assert vm is not None
    assert vm.change_number == 4242
    assert vm.change_id == "I1234567890abcdef1234567890abcdef12345678"
    assert vm.canonical_subject == "[OP-2462] fix the thing"
    assert vm.revision == "abcdef1234567890"
    assert vm.branch == "develop"
    assert vm.project == _PROJECT
    assert vm.plus2_reviewer == "sora"  # NON-BOT approver carried for ledger evidence
    assert vm.patchset_count == 5  # β-1: revisions[current]._number == patchset count
    # Requested the detailed-labels + current-revision options against /a/changes/.
    assert cap["url"].endswith("/a/changes/")
    assert cap["params"]["o"] == ["CURRENT_REVISION", "DETAILED_LABELS"]


@pytest.mark.asyncio
async def test_bot_only_plus2_fails_closed(monkeypatch, http_creds):
    """merger-agent-bot +2 ALONE must not confirm (the O6-exception leak)."""
    row = _merged_row(labels={
        "Code-Review": {"all": [{"value": 2, "username": "merger-agent-bot"}]},
    })
    _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([row])))
    assert await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT) is None


@pytest.mark.asyncio
async def test_not_merged_fails_closed(monkeypatch, http_creds):
    row = _merged_row(status="NEW")
    _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([row])))
    assert await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT) is None


@pytest.mark.asyncio
async def test_missing_creds_returns_none_before_any_request(monkeypatch, http_creds):
    monkeypatch.setattr(settings, "gerrit_http_password", "")
    # No fake httpx installed — a request attempt would AttributeError; the
    # cred short-circuit must fire first.
    assert await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT) is None


@pytest.mark.asyncio
async def test_zero_and_multi_rows_fail_closed(monkeypatch, http_creds):
    _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([])))
    assert await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT) is None
    _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([_merged_row(), _merged_row()])))
    assert await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT) is None


@pytest.mark.asyncio
async def test_identity_mismatch_fails_closed(monkeypatch, http_creds):
    _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([_merged_row()])))
    assert await mcp_gerrit.verify_merged_change_http(
        change_number=4242, project=_PROJECT, expected_revision="deadbeef") is None
    _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([_merged_row()])))
    assert await mcp_gerrit.verify_merged_change_http(
        change_number=4242, project=_PROJECT, expected_change_id="Iwrong") is None


@pytest.mark.asyncio
async def test_number_mismatch_fails_closed(monkeypatch, http_creds):
    # The row Gerrit returns must match the requested number (query integrity).
    _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([_merged_row(_number=9999)])))
    assert await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT) is None


@pytest.mark.asyncio
async def test_wrong_project_arg_fails_closed(monkeypatch, http_creds):
    _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([_merged_row()])))
    assert await mcp_gerrit.verify_merged_change_http(change_number=4242, project="other/proj") is None


@pytest.mark.asyncio
async def test_transport_error_never_raises(monkeypatch, http_creds):
    import httpx
    _install_fake_httpx(monkeypatch, raises=httpx.ConnectError("boom"))
    assert await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT) is None


@pytest.mark.asyncio
async def test_non_200_fails_closed(monkeypatch, http_creds):
    _install_fake_httpx(monkeypatch, resp=_FakeResp(status_code=403, text="forbidden"))
    assert await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT) is None


def test_rest_labels_to_approvals_shape():
    labels = {
        "Code-Review": {"all": [
            {"value": 2, "username": "sora", "name": "Sora"},
            {"value": 0, "username": "x"},
        ]},
        "Verified": {"all": [{"value": 1, "username": "ci-bot"}]},
    }
    approvals = mcp_gerrit._rest_labels_to_approvals(labels)
    assert {"type": "Code-Review", "value": 2,
            "by": {"username": "sora", "name": "Sora"}} in approvals
    assert any(a["type"] == "Verified" and a["value"] == 1 for a in approvals)
    # both Code-Review entries (incl. the 0) carried — the deriver filters, not us.
    assert len([a for a in approvals if a["type"] == "Code-Review"]) == 2
    assert mcp_gerrit._rest_labels_to_approvals(None) == []


def test_parse_gerrit_rest_json_strips_xssi():
    assert mcp_gerrit._parse_gerrit_rest_json(")]}'\n[1,2]") == [1, 2]
    assert mcp_gerrit._parse_gerrit_rest_json('[{"a":1}]') == [{"a": 1}]


@pytest.mark.asyncio
async def test_missing_revisions_yields_none_patchset_count(monkeypatch, http_creds):
    row = _merged_row()
    del row["revisions"]
    _install_fake_httpx(monkeypatch, resp=_FakeResp(text=_rest_body([row])))
    vm = await mcp_gerrit.verify_merged_change_http(change_number=4242, project=_PROJECT)
    assert vm is not None
    assert vm.patchset_count is None  # degrade, never invent


def test_ssh_verify_carries_patchset_count(monkeypatch):
    """SSH path parity: currentPatchSet.number (a string in gerrit query
    output) surfaces as VerifiedMerge.patchset_count."""
    monkeypatch.setattr(mcp_gerrit, "GERRIT_PROJECT_PATH", _PROJECT)
    monkeypatch.setattr(
        mcp_gerrit, "_ssh_argv", lambda *a, **k: ["true"],
    )
    row = json.dumps({
        "number": "4242", "project": _PROJECT, "branch": "develop",
        "id": "I1234567890abcdef1234567890abcdef12345678",
        "subject": "[OP-2462] fix the thing", "status": "MERGED",
        "currentPatchSet": {
            "revision": "abcdef1234567890", "number": "7",
            "approvals": [
                {"type": "Code-Review", "value": "2", "by": {"username": "sora"}}
            ],
        },
    })
    stats = json.dumps({"type": "stats", "rowCount": 1})
    monkeypatch.setattr(
        mcp_gerrit, "_run_gerrit_query", lambda *a, **k: row + "\n" + stats + "\n",
    )
    vm = mcp_gerrit.verify_merged_change(change_number=4242, project=_PROJECT)
    assert vm is not None
    assert vm.patchset_count == 7
    assert vm.plus2_reviewer == "sora"


# ── β-F capability selection (webhook HTTP-first / SSH-fallback / degraded) ──

def test_http_verify_available_requires_all_three(monkeypatch):
    monkeypatch.setattr(settings, "gerrit_url", "https://g")
    monkeypatch.setattr(settings, "gerrit_http_user", "u")
    monkeypatch.setattr(settings, "gerrit_http_password", "p")
    assert mcp_gerrit.http_verify_available() is True
    for missing in ("gerrit_url", "gerrit_http_user", "gerrit_http_password"):
        with monkeypatch.context() as m:
            m.setattr(settings, "gerrit_url", "https://g")
            m.setattr(settings, "gerrit_http_user", "u")
            m.setattr(settings, "gerrit_http_password", "p")
            m.setattr(settings, missing, "")
            assert mcp_gerrit.http_verify_available() is False


def test_verify_capability_http_when_creds_present(monkeypatch):
    monkeypatch.setattr(settings, "gerrit_url", "https://g")
    monkeypatch.setattr(settings, "gerrit_http_user", "u")
    monkeypatch.setattr(settings, "gerrit_http_password", "p")
    assert mcp_gerrit.verify_capability() == "http"


class _FakeKey:
    def __init__(self, present: bool) -> None:
        self._present = present

    def exists(self) -> bool:
        return self._present


def _no_http(monkeypatch):
    monkeypatch.setattr(settings, "gerrit_url", "")
    monkeypatch.setattr(settings, "gerrit_http_user", "")
    monkeypatch.setattr(settings, "gerrit_http_password", "")


def test_verify_capability_ssh_when_key_present(monkeypatch):
    _no_http(monkeypatch)
    monkeypatch.setattr(mcp_gerrit, "_gerrit_auth_for_instance",
                        lambda *a, **k: ("bot", _FakeKey(True)))
    assert mcp_gerrit.verify_capability() == "ssh"


def test_verify_capability_degraded_when_neither(monkeypatch):
    _no_http(monkeypatch)
    monkeypatch.setattr(mcp_gerrit, "_gerrit_auth_for_instance",
                        lambda *a, **k: ("bot", _FakeKey(False)))
    assert mcp_gerrit.verify_capability() == "degraded"


def test_verify_capability_degraded_when_key_probe_raises(monkeypatch):
    _no_http(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("no auth configured")

    monkeypatch.setattr(mcp_gerrit, "_gerrit_auth_for_instance", _boom)
    assert mcp_gerrit.verify_capability() == "degraded"
