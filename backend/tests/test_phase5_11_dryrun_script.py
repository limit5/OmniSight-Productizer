"""Phase 5-11 (#multi-account-forge) — dry-run script contract tests.

Locks the ``scripts/migrate_legacy_credentials_dryrun.py`` behaviour
so regressions in either the script OR the underlying
``_plan_rows()`` planner are caught at CI time:

* ``_fingerprint`` helper masks plaintext correctly.
* ``plan()`` wrapper delegates to the production planner — no
  divergent logic between dry-run and real-run.
* ``run()`` emits the right text / JSON format.
* Plaintext secrets are **never** printed (core security contract).
* ``--strict-idempotency`` exits 3 when DB probe reports non-empty.

No PG required — the script's ``plan()`` path uses
``_plan_rows()`` which reads ``Settings`` via monkeypatchable
attributes, and ``--probe-db`` is stubbed via a mock in the
idempotency test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Load the script as a module without executing ``main()``
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "migrate_legacy_credentials_dryrun.py"
)


@pytest.fixture(scope="module")
def dryrun():
    """Import the script file as a module named
    ``_phase5_11_dryrun_mod`` and return the module object so tests
    can poke at its internals.
    """
    spec = importlib.util.spec_from_file_location(
        "_phase5_11_dryrun_mod", str(_SCRIPT_PATH),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Ensure the project root is on sys.path so "backend" imports
    # from inside the script succeed.
    proj_root = str(_SCRIPT_PATH.resolve().parents[1])
    if proj_root not in sys.path:
        sys.path.insert(0, proj_root)
    spec.loader.exec_module(mod)
    return mod


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _fingerprint helper (mirrors secret_store.fingerprint)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_fingerprint_empty(dryrun):
    assert dryrun._fingerprint("") == ""


def test_fingerprint_short(dryrun):
    assert dryrun._fingerprint("short") == "****"
    assert dryrun._fingerprint("12345678") == "****"


def test_fingerprint_long(dryrun):
    assert dryrun._fingerprint("ghp_verylongtoken_last4") == "…ast4"
    assert dryrun._fingerprint("abcdefghij") == "…ghij"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  plan() delegates to backend._plan_rows → no divergence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_plan_delegates_to_lcm(dryrun, monkeypatch):
    """``plan()`` must call ``legacy_credential_migration._plan_rows``,
    not reimplement the precedence logic. Monkeypatch the planner and
    verify the script's output reflects the patched result."""
    import backend.legacy_credential_migration as lcm

    fake_rows = [
        {
            "id": "ga-fake-github",
            "platform": "github",
            "label": "fake",
            "instance_url": "https://github.com",
            "token": "ghp_token_to_be_masked_abc1",
            "ssh_key": "",
            "webhook_secret": "",
            "ssh_host": "",
            "ssh_port": 0,
            "project": "",
            "url_patterns": [],
            "is_default": True,
            "enabled": True,
            "source": "test",
        }
    ]
    monkeypatch.setattr(lcm, "_plan_rows", lambda: fake_rows)

    out = dryrun.plan()
    assert out["total"] == 1
    assert out["per_platform"] == {"github": 1}
    row = out["rows"][0]
    assert row["id"] == "ga-fake-github"
    # Plaintext token must NOT appear anywhere in the output.
    assert "ghp_token_to_be_masked_abc1" not in json.dumps(
        out, default=str,
    )
    # Fingerprint IS present.
    assert row["token_fingerprint"] == "…abc1"


def test_plan_empty_returns_zero(dryrun, monkeypatch):
    import backend.legacy_credential_migration as lcm
    monkeypatch.setattr(lcm, "_plan_rows", lambda: [])
    out = dryrun.plan()
    assert out["total"] == 0
    assert out["per_platform"] == {}
    assert out["rows"] == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JSON output format — machine-readable contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_run_json_output_has_expected_shape(
    dryrun, monkeypatch, capsys,
):
    import backend.legacy_credential_migration as lcm

    fake_rows = [
        {
            "id": "ga-fake-jira",
            "platform": "jira",
            "label": "jira-fake",
            "instance_url": "https://fake.atlassian.net",
            "token": "jira_plaintext_zzyx",
            "ssh_key": "",
            "webhook_secret": "whs_plaintext_WXYZ",
            "ssh_host": "",
            "ssh_port": 0,
            "project": "PROJ",
            "url_patterns": [],
            "is_default": True,
            "enabled": True,
            "source": "notification_jira",
        }
    ]
    monkeypatch.setattr(lcm, "_plan_rows", lambda: fake_rows)

    rc = dryrun.run(
        emit_json=True, probe_db=False, strict_idempotency=False,
    )
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)

    assert "plan" in data
    assert data["plan"]["total"] == 1
    assert data["plan"]["per_platform"] == {"jira": 1}
    row = data["plan"]["rows"][0]
    assert row["token_fingerprint"] == "…zzyx"
    assert row["webhook_secret_fingerprint"] == "…WXYZ"
    # Probe was not requested — must be null.
    assert data["probe"] is None

    # CRITICAL security invariant.
    assert "jira_plaintext_zzyx" not in out
    assert "whs_plaintext_WXYZ" not in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Text output — operator-friendly + no plaintext leak
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_run_text_output_never_contains_plaintext(
    dryrun, monkeypatch, capsys,
):
    import backend.legacy_credential_migration as lcm

    fake_rows = [
        {
            "id": "ga-text-out",
            "platform": "github",
            "label": "text-test",
            "instance_url": "https://github.com",
            "token": "ghp_DO_NOT_ECHO_plaintext_nope",
            "ssh_key": "SSH_KEY_DO_NOT_ECHO_KKEY",
            "webhook_secret": "WEBHOOK_SECRET_NEVER_ECHO",
            "ssh_host": "",
            "ssh_port": 0,
            "project": "",
            "url_patterns": ["github.com/acme/*"],
            "is_default": False,
            "enabled": True,
            "source": "github_token_map[github.com]",
        }
    ]
    monkeypatch.setattr(lcm, "_plan_rows", lambda: fake_rows)

    rc = dryrun.run(
        emit_json=False, probe_db=False, strict_idempotency=False,
    )
    assert rc == 0
    out = capsys.readouterr().out

    # The row's essentials ARE visible.
    assert "ga-text-out" in out
    assert "github.com/acme/*" in out
    assert "…nope" in out      # token fingerprint
    assert "…KKEY" in out      # ssh_key fingerprint
    assert "…ECHO" in out      # webhook_secret fingerprint

    # Plaintext secrets are NEVER echoed.
    assert "ghp_DO_NOT_ECHO_plaintext_nope" not in out
    assert "SSH_KEY_DO_NOT_ECHO_KKEY" not in out
    assert "WEBHOOK_SECRET_NEVER_ECHO" not in out


def test_run_text_output_empty_plan_has_helpful_message(
    dryrun, monkeypatch, capsys,
):
    import backend.legacy_credential_migration as lcm
    monkeypatch.setattr(lcm, "_plan_rows", lambda: [])

    rc = dryrun.run(
        emit_json=False, probe_db=False, strict_idempotency=False,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "No legacy credentials found" in out
    assert "OMNISIGHT_GITHUB_TOKEN" in out  # operator hint


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  --strict-idempotency exit codes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_strict_idempotency_exits_3_when_rows_present(
    dryrun, monkeypatch, capsys,
):
    import backend.legacy_credential_migration as lcm

    monkeypatch.setattr(
        lcm, "_plan_rows",
        lambda: [{
            "id": "x", "platform": "github", "label": "x",
            "instance_url": "", "token": "abcdxxxx_last", "ssh_key": "",
            "webhook_secret": "", "ssh_host": "", "ssh_port": 0,
            "project": "", "url_patterns": [],
            "is_default": False, "enabled": True, "source": "test",
        }],
    )

    async def _fake_probe():
        return {"available": True, "count": 5, "error": None}

    monkeypatch.setattr(dryrun, "_probe_db_for_existing_rows", _fake_probe)

    rc = dryrun.run(
        emit_json=False, probe_db=True, strict_idempotency=True,
    )
    assert rc == 3, (
        "strict-idempotency should exit 3 when git_accounts has rows"
    )
    out = capsys.readouterr().out
    assert "already has" in out


def test_strict_idempotency_exits_0_when_empty_db(
    dryrun, monkeypatch, capsys,
):
    import backend.legacy_credential_migration as lcm
    monkeypatch.setattr(lcm, "_plan_rows", lambda: [])

    async def _fake_probe():
        return {"available": True, "count": 0, "error": None}

    monkeypatch.setattr(dryrun, "_probe_db_for_existing_rows", _fake_probe)

    rc = dryrun.run(
        emit_json=False, probe_db=True, strict_idempotency=True,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "EMPTY" in out


def test_strict_idempotency_ignored_without_probe(
    dryrun, monkeypatch, capsys,
):
    """--strict-idempotency without --probe-db is a no-op (exit 0)."""
    import backend.legacy_credential_migration as lcm
    monkeypatch.setattr(lcm, "_plan_rows", lambda: [])

    rc = dryrun.run(
        emit_json=False, probe_db=False, strict_idempotency=True,
    )
    assert rc == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Error-path: bad planner raises → script returns 2, stderr has trace
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_plan_exception_returns_exit_code_2(
    dryrun, monkeypatch, capsys,
):
    import backend.legacy_credential_migration as lcm

    def _explode():
        raise RuntimeError("mock planner failure")

    monkeypatch.setattr(lcm, "_plan_rows", _explode)

    rc = dryrun.run(
        emit_json=False, probe_db=False, strict_idempotency=False,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "RuntimeError" in err


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration with real Settings via env vars
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_plan_picks_up_real_settings(dryrun, monkeypatch):
    """When ``settings.github_token`` is set (via a monkeypatched
    Settings field), the real ``_plan_rows`` returns the corresponding
    row and the dry-run summary reflects it. Proves end-to-end that
    the script output tracks the live config snapshot.
    """
    from backend.config import settings

    # Monkeypatch the Settings singleton's fields directly — the
    # dry-run script will read them via legacy_credential_migration.
    monkeypatch.setattr(
        settings, "github_token", "ghp_settings_test_last",
    )
    # Clear any other legacy knobs so we get exactly one row.
    for fld in (
        "gitlab_token", "gitlab_url", "github_token_map",
        "gitlab_token_map", "gerrit_enabled",
        "gerrit_ssh_host", "gerrit_instances",
        "notification_jira_url", "notification_jira_token",
    ):
        if hasattr(settings, fld):
            cur = getattr(settings, fld)
            if isinstance(cur, bool):
                monkeypatch.setattr(settings, fld, False)
            elif isinstance(cur, str):
                monkeypatch.setattr(settings, fld, "")

    out = dryrun.plan()
    assert out["total"] == 1
    row = out["rows"][0]
    assert row["platform"] == "github"
    assert row["id"] == "ga-legacy-github-github-com"
    assert row["source"] == "github_token"
    assert row["is_default"] is True
    assert row["token_fingerprint"] == "…last"
    # Plaintext NEVER in the summary.
    assert "ghp_settings_test_last" not in json.dumps(out)
