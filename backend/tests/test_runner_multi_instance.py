"""OP-783 multi-instance runner tests.

Pins the per-instance bot-account contract: cred path resolution,
backpressure state-file scoping, runner env-var propagation, and
backwards-compat for ``instance_id="default"``.

Cost: <100ms, stdlib + pytest only — no JIRA / Gerrit / network.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd


_RUNNER_PATH = REPO_ROOT / "auto-runner-jira.py"
_LAUNCHER_PATH = REPO_ROOT / "scripts" / "launch_runner_instance.sh"
_TEARDOWN_PATH = REPO_ROOT / "scripts" / "teardown_runner_instance.sh"
_SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
_RUNNER_CODEX_UNIT = _SYSTEMD_DIR / "runner-codex@.service"
_RUNNER_CLAUDE_UNIT = _SYSTEMD_DIR / "runner-claude@.service"


def _load_runner() -> object:
    sys.modules.pop("jira_runner_multi_instance_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_multi_instance_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── resolve_bot_username (AC: instance_id resolution) ────────────────


def test_resolve_bot_username_default_returns_legacy_bare_name() -> None:
    """Default instance must NOT change the legacy bot username."""
    assert jd.resolve_bot_username("subscription-codex", "default") == "codex-bot"
    assert jd.resolve_bot_username("subscription-claude", "default") == "claude-bot"
    assert jd.resolve_bot_username("api-openai", "default") == "codex-bot"
    assert jd.resolve_bot_username("api-anthropic", "default") == "claude-bot"


def test_resolve_bot_username_non_default_appends_instance_id() -> None:
    assert jd.resolve_bot_username("subscription-codex", "2") == "codex-bot-2"
    assert jd.resolve_bot_username("subscription-claude", "3") == "claude-bot-3"
    assert jd.resolve_bot_username("subscription-codex", "exp") == "codex-bot-exp"


def test_resolve_bot_username_reads_env_when_instance_id_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_RUNNER_INSTANCE_ID", "5")
    assert jd.resolve_bot_username("subscription-codex") == "codex-bot-5"
    monkeypatch.delenv("OMNISIGHT_RUNNER_INSTANCE_ID", raising=False)
    assert jd.resolve_bot_username("subscription-codex") == "codex-bot"


def test_resolve_bot_username_unknown_class_raises() -> None:
    with pytest.raises(ValueError, match="unknown agent_class"):
        jd.resolve_bot_username("subscription-unknown", "2")


# ── _cred_paths (AC: per-instance state files don't collide) ─────────


def test_cred_paths_default_keeps_legacy_filenames() -> None:
    """Existing single-instance setups must keep loading the same files."""
    env, tok = jd._cred_paths("subscription-codex", "default")
    assert env.name == "jira-codex.env"
    assert tok.name == "jira-codex-token"

    env, tok = jd._cred_paths("subscription-claude", "default")
    assert env.name == "jira-claude.env"
    assert tok.name == "jira-claude-token"


def test_cred_paths_non_default_uses_bot_keyed_filenames() -> None:
    env, tok = jd._cred_paths("subscription-codex", "2")
    assert env.name == "jira-codex-bot-2.env"
    assert tok.name == "jira-codex-bot-2-token"

    env, tok = jd._cred_paths("subscription-claude", "3")
    assert env.name == "jira-claude-bot-3.env"
    assert tok.name == "jira-claude-bot-3-token"


def test_cred_paths_two_instances_dont_collide() -> None:
    a_env, a_tok = jd._cred_paths("subscription-codex", "default")
    b_env, b_tok = jd._cred_paths("subscription-codex", "2")
    c_env, c_tok = jd._cred_paths("subscription-codex", "3")
    assert {a_env, b_env, c_env} == {a_env, b_env, c_env}  # 3 distinct
    assert {a_tok, b_tok, c_tok} == {a_tok, b_tok, c_tok}
    assert len({a_env, b_env, c_env}) == 3
    assert len({a_tok, b_tok, c_tok}) == 3


# ── _gerrit_auth_for_instance ────────────────────────────────────────


def test_gerrit_auth_default_returns_legacy_entry() -> None:
    """Default instance preserves the exact ssh-key paths existing setups depend on."""
    expected = jd._GERRIT_AUTH_BY_CLASS["subscription-codex"]
    assert jd._gerrit_auth_for_instance("subscription-codex", "default") == expected


def test_gerrit_auth_non_default_derives_per_bot_key_path() -> None:
    bot, key = jd._gerrit_auth_for_instance("subscription-codex", "2")
    assert bot == "codex-bot-2"
    assert key.name == "gerrit-codex-bot-2-ed25519"
    assert key.parent == jd.CRED_DIR


def test_gerrit_auth_for_bot_recognizes_per_instance_bots() -> None:
    """Backpressure flow uses _gerrit_auth_for_bot(bot_username) — must
    accept codex-bot-2 etc., not just the static map's two entries."""
    bot, key = jd._gerrit_auth_for_bot("codex-bot-2")
    assert bot == "codex-bot-2"
    assert key.name == "gerrit-codex-bot-2-ed25519"

    bot, key = jd._gerrit_auth_for_bot("claude-bot-3")
    assert bot == "claude-bot-3"
    assert key.name == "gerrit-claude-bot-3-ed25519"


def test_gerrit_auth_for_bot_legacy_default_unchanged() -> None:
    bot, key = jd._gerrit_auth_for_bot("codex-bot")
    assert bot == "codex-bot"
    assert key == jd._GERRIT_AUTH_BY_CLASS["subscription-codex"][1]


# ── _backpressure_state_file (AC: per-bot backpressure scope) ────────


def test_backpressure_state_file_default_keeps_legacy_class_keyed_path() -> None:
    """Default instance MUST keep the agent-class-keyed state path so an
    in-flight ``paused`` latch survives a runner restart."""
    p = jd._backpressure_state_file("subscription-codex", "default")
    assert p == Path("/tmp/runner-backpressure-subscription-codex.state")


def test_backpressure_state_file_non_default_keys_on_bot_username() -> None:
    p = jd._backpressure_state_file("subscription-codex", "2")
    assert p == Path("/tmp/runner-backpressure-codex-bot-2.state")
    p = jd._backpressure_state_file("subscription-claude", "3")
    assert p == Path("/tmp/runner-backpressure-claude-bot-3.state")


def test_backpressure_state_file_two_instances_have_distinct_paths() -> None:
    """codex-bot-2 saturating its review queue MUST NOT pause codex-bot-3."""
    a = jd._backpressure_state_file("subscription-codex", "2")
    b = jd._backpressure_state_file("subscription-codex", "3")
    c = jd._backpressure_state_file("subscription-codex", "default")
    assert len({a, b, c}) == 3


# ── backpressure_decide (AC: per-bot scope) ──────────────────────────


def test_backpressure_decide_queries_per_instance_bot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The PS-count query for instance 2 must hit codex-bot-2, NOT codex-bot."""
    seen: list[str] = []
    state_file = tmp_path / "codex-bot-2.state"

    monkeypatch.setattr(
        jd, "_backpressure_state_file", lambda agent_class, instance_id=None: state_file
    )
    monkeypatch.setattr(jd.settings, "runner_ps_cap", 8)
    monkeypatch.setattr(jd.settings, "runner_ps_floor", 4)
    monkeypatch.setattr(
        jd, "open_ps_count_for", lambda bot_username: seen.append(bot_username) or 3
    )
    monkeypatch.setattr(jd, "notify_operator", lambda **kwargs: None)

    ok, reason = jd.backpressure_decide("subscription-codex", "2")
    assert ok is True
    assert seen == ["codex-bot-2"]
    assert "active" in reason


def test_backpressure_decide_default_unchanged_from_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default instance backpressure path must remain byte-identical to pre-OP-783."""
    seen: list[str] = []
    state_file = tmp_path / "subscription-codex.state"

    monkeypatch.setattr(
        jd, "_backpressure_state_file", lambda agent_class, instance_id=None: state_file
    )
    monkeypatch.setattr(jd.settings, "runner_ps_cap", 8)
    monkeypatch.setattr(jd.settings, "runner_ps_floor", 4)
    monkeypatch.setattr(
        jd, "open_ps_count_for", lambda bot_username: seen.append(bot_username) or 3
    )
    monkeypatch.setattr(jd, "notify_operator", lambda **kwargs: None)

    ok, reason = jd.backpressure_decide("subscription-codex", "default")
    assert ok is True
    assert seen == ["codex-bot"]


# ── _bot_email_for ───────────────────────────────────────────────────


def test_bot_email_for_default_unchanged() -> None:
    assert jd._bot_email_for("subscription-codex", "default") == "rt3628+codex-bot@gmail.com"
    assert jd._bot_email_for("subscription-claude", "default") == "rt3628+claude-bot@gmail.com"


def test_bot_email_for_non_default_uses_per_instance_alias() -> None:
    assert jd._bot_email_for("subscription-codex", "2") == "rt3628+codex-bot-2@gmail.com"
    assert jd._bot_email_for("subscription-claude", "5") == "rt3628+claude-bot-5@gmail.com"


# ── idempotency env override ─────────────────────────────────────────


def test_idempotency_db_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-instance runners need an isolated idempotency DB so two
    parallel processes don't dedup against shared rows."""
    from backend.agents import idempotency

    monkeypatch.setenv(
        "OMNISIGHT_IDEMPOTENCY_DB_PATH", "/tmp/test-idem-codex-bot-2.db"
    )
    importlib.reload(idempotency)
    assert idempotency.IDEMPOTENCY_DB == Path("/tmp/test-idem-codex-bot-2.db")

    monkeypatch.delenv("OMNISIGHT_IDEMPOTENCY_DB_PATH", raising=False)
    importlib.reload(idempotency)
    assert idempotency.IDEMPOTENCY_DB.name == "idem-keys.db"


# ── auto-runner-jira.py reads OMNISIGHT_RUNNER_INSTANCE_ID ───────────


def test_runner_default_instance_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNISIGHT_RUNNER_INSTANCE_ID", raising=False)
    mod = _load_runner()
    assert mod.INSTANCE_ID == "default"


def test_runner_picks_instance_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNISIGHT_RUNNER_INSTANCE_ID", "2")
    mod = _load_runner()
    assert mod.INSTANCE_ID == "2"


def test_runner_resolves_per_instance_bot_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_RUNNER_INSTANCE_ID", "3")
    monkeypatch.setenv("OMNISIGHT_RUNNER_CLASS", "subscription-claude")
    mod = _load_runner()
    assert mod._bot_username() == "claude-bot-3"


def test_runner_default_worktree_per_instance_distinct_from_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default instance must keep `OmniSight-codex-worktree`; instance 2
    must use a sibling `OmniSight-codex-bot-2-worktree` so two runners
    never share a working tree."""
    monkeypatch.delenv("OMNISIGHT_CODEX_WORKTREE", raising=False)
    monkeypatch.delenv("OMNISIGHT_RUNNER_INSTANCE_ID", raising=False)
    mod_default = _load_runner()
    assert mod_default.CODEX_WORKTREE.endswith("OmniSight-codex-worktree")

    monkeypatch.setenv("OMNISIGHT_RUNNER_INSTANCE_ID", "2")
    mod_inst2 = _load_runner()
    assert mod_inst2.CODEX_WORKTREE.endswith("OmniSight-codex-bot-2-worktree")


def test_runner_main_threads_instance_id_into_make_client_and_backpressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: backpressure_decide and make_client are called with the
    runner's INSTANCE_ID, so per-bot quota + creds resolution is routed
    correctly through the dispatch layer."""
    monkeypatch.setenv("OMNISIGHT_RUNNER_INSTANCE_ID", "4")
    monkeypatch.setenv("OMNISIGHT_RUNNER_CLASS", "subscription-codex")
    mod = _load_runner()

    seen_calls: dict[str, tuple] = {}

    monkeypatch.setattr(mod.circuit_breaker, "open_services", lambda: [])
    monkeypatch.setattr(mod.jira_dispatch, "assert_worktree_config_enabled", lambda repo: None)
    monkeypatch.setattr(mod.orphan_salvage, "salvage_orphan_commits", lambda *a, **kw: 0)

    def fake_backpressure(agent_class, instance_id=None):
        seen_calls["backpressure"] = (agent_class, instance_id)
        return False, "8 open PSes (cap 8)"

    monkeypatch.setattr(mod.jira_dispatch, "backpressure_decide", fake_backpressure)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "make_client",
        lambda *a, **kw: pytest.fail("must short-circuit before make_client"),
    )

    rc = mod.main()
    assert rc == 0
    assert seen_calls["backpressure"] == ("subscription-codex", "4")


# ── Launcher script idempotency contract ────────────────────────────


def test_launcher_script_exists_and_is_executable() -> None:
    assert _LAUNCHER_PATH.exists(), f"missing: {_LAUNCHER_PATH}"
    assert os.access(_LAUNCHER_PATH, os.X_OK), f"not executable: {_LAUNCHER_PATH}"


def test_teardown_script_exists_and_is_executable() -> None:
    assert _TEARDOWN_PATH.exists(), f"missing: {_TEARDOWN_PATH}"
    assert os.access(_TEARDOWN_PATH, os.X_OK), f"not executable: {_TEARDOWN_PATH}"


def test_launcher_idempotent_short_circuit_when_session_running() -> None:
    """The launcher's idempotency contract: if `tmux has-session` returns
    success the script MUST exit 0 without spawning a duplicate. This
    test pins that grep-able guard."""
    text = _LAUNCHER_PATH.read_text()
    assert "tmux has-session -t \"$TMUX_SESSION\"" in text, (
        "launcher missing idempotency guard against double-launch"
    )
    assert "exit 0" in text


def test_launcher_handles_codex_claude_and_default_bots() -> None:
    """The launcher's case statement covers the four supported bot prefixes."""
    text = _LAUNCHER_PATH.read_text()
    for pattern in ("codex-bot)", "codex-bot-*)", "claude-bot)", "claude-bot-*)"):
        assert pattern in text, f"launcher missing case branch: {pattern}"


def test_launcher_sets_per_instance_env_for_runner() -> None:
    """The launcher must propagate INSTANCE_ID + per-bot idempotency DB
    path into the spawned runner process — without this, the runner
    would fall back to default-instance state files and collide with
    sibling instances."""
    text = _LAUNCHER_PATH.read_text()
    assert "OMNISIGHT_RUNNER_INSTANCE_ID=$INSTANCE_ID" in text
    assert "OMNISIGHT_IDEMPOTENCY_DB_PATH=$IDEM_DB" in text
    assert "OMNISIGHT_RUNNER_CLASS=$AGENT_CLASS" in text


def test_launcher_uses_per_bot_paths_matching_jira_dispatch() -> None:
    """Pin the launcher's path conventions to match the runtime resolver
    in backend/agents/jira_dispatch.py — drift here means the launcher
    points at one cred file while the runner reads another."""
    text = _LAUNCHER_PATH.read_text()
    assert "gerrit-${BOT_USERNAME}-ed25519" in text
    assert "idem-keys-${BOT_USERNAME}.db" in text
    assert "runner-backpressure-${BOT_USERNAME}.state" in text


def test_teardown_clears_backpressure_latch() -> None:
    """The teardown is the only safe time to drop the paused latch — if
    the runner restarts before the latch is cleared, the new tick
    re-pauses immediately and the operator sees a confusing 'still
    paused after teardown' state."""
    text = _TEARDOWN_PATH.read_text()
    assert "rm -f \"$BACKPRESSURE_STATE\"" in text
    assert "tmux kill-session" in text


# ── systemd template units ──────────────────────────────────────────


def test_systemd_codex_template_unit_present() -> None:
    assert _RUNNER_CODEX_UNIT.exists(), f"missing: {_RUNNER_CODEX_UNIT}"


def test_systemd_claude_template_unit_present() -> None:
    assert _RUNNER_CLAUDE_UNIT.exists(), f"missing: {_RUNNER_CLAUDE_UNIT}"


@pytest.mark.parametrize(
    "unit_path,expected_class",
    [
        (_RUNNER_CODEX_UNIT, "subscription-codex"),
        (_RUNNER_CLAUDE_UNIT, "subscription-claude"),
    ],
)
def test_systemd_unit_propagates_instance_id_via_template_specifier(
    unit_path: Path, expected_class: str
) -> None:
    """Pin: ``%i`` is forwarded as ``OMNISIGHT_RUNNER_INSTANCE_ID``. Without
    this the unit would launch identical processes for `runner-codex@2`
    and `runner-codex@3`."""
    text = unit_path.read_text()
    assert f"OMNISIGHT_RUNNER_CLASS={expected_class}" in text
    assert "OMNISIGHT_RUNNER_INSTANCE_ID=%i" in text
    assert re.search(r"ExecStart=/usr/bin/python3 .*auto-runner-jira\.py", text)
    # Standard systemd discipline: SIGTERM, restart-on-failure, log to file.
    assert "Restart=on-failure" in text
    assert "KillSignal=SIGTERM" in text


@pytest.mark.parametrize(
    "unit_path,bot_prefix",
    [
        (_RUNNER_CODEX_UNIT, "codex-bot"),
        (_RUNNER_CLAUDE_UNIT, "claude-bot"),
    ],
)
def test_systemd_unit_sets_per_instance_idempotency_db(
    unit_path: Path, bot_prefix: str
) -> None:
    """The `@%i` template + per-bot idem DB ensures `runner-codex@2` and
    `runner-codex@3` don't collide on the same SQLite file."""
    text = unit_path.read_text()
    assert f"idem-keys-{bot_prefix}-%i.db" in text


def test_provisioning_doc_present() -> None:
    p = REPO_ROOT / "docs" / "operations" / "multi-instance-runner-provisioning.md"
    assert p.exists()
    text = p.read_text()
    # Sanity: covers the core provisioning steps from the AC.
    for keyword in (
        "JIRA",
        "Gerrit",
        "subscription",
        "launch_runner_instance.sh",
        "OMNISIGHT_RUNNER_INSTANCE_ID",
    ):
        assert keyword in text, f"provisioning doc missing keyword: {keyword}"


def test_runbook_doc_present() -> None:
    p = REPO_ROOT / "docs" / "operations" / "multi-instance-runner-runbook.md"
    assert p.exists()
    text = p.read_text()
    for keyword in (
        "Rollback to single-instance",
        "backpressure",
        "teardown_runner_instance.sh",
    ):
        assert keyword in text, f"runbook missing keyword: {keyword}"
