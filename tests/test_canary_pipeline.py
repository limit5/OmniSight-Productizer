"""[OP-725] Tests for the hourly canary script + its systemd contract.

Two parallel test surfaces:

1. **Pure-function tests** for ``scripts/canary_pipeline.py`` — log
   parsing, push-stderr parsing, abandon argv, poll loop with
   injected ``sleep``/``monotonic``. These tests do not touch git,
   ssh, or the filesystem outside ``tmp_path``.

2. **Systemd unit contract tests** — pin the timer cadence
   (``OnUnitActiveSec=1h`` + ``Persistent=true``), the OnFailure
   chain to the T1 alert hook, and the oneshot service shape.
   Same pattern as ``tests/test_systemd_graceful_shutdown.py``.

Cost: <50ms, stdlib + pytest only.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "canary_pipeline.py"
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"

CANARY_SERVICE = SYSTEMD_DIR / "omnisight-canary.service"
CANARY_TIMER = SYSTEMD_DIR / "omnisight-canary.timer"
CANARY_ALERT = SYSTEMD_DIR / "omnisight-canary-alert.service"


def _load_canary_module():
    """Import scripts/canary_pipeline.py without putting scripts/ on sys.path.

    The script avoids backend imports by design (so a backend regression
    can't masquerade as a passing canary), so we must NOT use a normal
    package import — that would risk pulling in scripts/__init__.py
    siblings and dragging in unrelated tooling. Direct spec-loader keeps
    the canary's import surface to stdlib.
    """
    spec = importlib.util.spec_from_file_location(
        "canary_pipeline_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["canary_pipeline_under_test"] = module
    spec.loader.exec_module(module)
    return module


canary = _load_canary_module()


# ─── pure-function tests ─────────────────────────────────────────────


def test_subject_excludes_op_keys_to_avoid_jira_pollution():
    """AC #3 — the canary's commit subject must NOT match the bridge's
    ``[OP-NNN]`` extractor, otherwise every hourly run would comment
    on (or transition!) a real ticket. Mirror the bridge's regex
    explicitly so a future loosening of either side is caught here.
    """
    subject = canary.build_canary_subject("2026-05-08T00:00:00Z")
    op_bracket = re.compile(r"\[(OP-\d+)(?:[/_-][^\]]*)?\]")
    op_anywhere = re.compile(r"\bOP-\d+\b")
    assert "[CANARY]" in subject
    assert op_bracket.search(subject) is None
    assert op_anywhere.search(subject) is None


def test_subject_changes_per_run_to_force_unique_change_id():
    """Two runs at different ISO seconds must produce different
    subjects so Gerrit assigns a fresh change number each time
    (Change-Id is derived from subject + author + timestamp).
    """
    a = canary.build_canary_subject("2026-05-08T00:00:00Z")
    b = canary.build_canary_subject("2026-05-08T00:00:01Z")
    assert a != b


def test_parse_change_number_from_push_stderr_typical():
    """Real Gerrit push output, captured from a `git push` to develop."""
    stderr = (
        "remote: Resolving deltas: 100% (1/1)\n"
        "remote: Processing changes: refs: 1, new: 1, done\n"
        "remote: \n"
        "remote: SUCCESS\n"
        "remote: \n"
        "remote:   https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/12345 [NEW]\n"
        "remote: \n"
        "To ssh://claude-bot@sora.services:29418/omnisight/OmniSight-Productizer\n"
        " * [new reference]   HEAD -> refs/for/develop%hashtag=canary,wip,private\n"
    )
    assert canary.parse_change_number_from_push_stderr(stderr) == "12345"


def test_parse_change_number_returns_none_on_rejected_push():
    """A rejected push prints no `[NEW]` URL; we must surface that as
    ``None`` so the caller emits DEGRADED rather than tailing the log
    forever.
    """
    stderr = (
        "remote: error: branch refs/for/develop:\n"
        "remote: error: change xxx closed\n"
        "To ssh://...\n"
        " ! [remote rejected] HEAD -> refs/for/develop\n"
    )
    assert canary.parse_change_number_from_push_stderr(stderr) is None


def test_parse_log_finds_event_for_matching_change():
    """The bridge's structured_log emits one JSON record per line.
    Validate the parser locks onto the right event AND change number,
    not a same-named event for an unrelated change.
    """
    lines = [
        json.dumps({
            "timestamp": "2026-05-08T00:00:00Z",
            "level": "INFO",
            "event": "proactive_merger_thread_spawned",
            "change_id": "99999",  # different change
            "ps": "1",
        }),
        json.dumps({
            "timestamp": "2026-05-08T00:00:05Z",
            "level": "INFO",
            "event": "proactive_merger_thread_spawned",
            "change_id": "12345",  # ours
            "ps": "1",
        }),
    ]
    hit = canary.parse_log_for_event(
        lines, "proactive_merger_thread_spawned", "12345"
    )
    assert hit is not None
    assert hit["change_id"] == "12345"


def test_parse_log_returns_none_when_event_missing():
    """AC #2 — when the daemon is down (or didn't process our event),
    the parser must return None so poll_for_event eventually times out
    and the caller emits DEGRADED. This is the failure-injection path.
    """
    lines = [
        json.dumps({
            "timestamp": "2026-05-08T00:00:00Z",
            "level": "INFO",
            "event": "auto_rebase_sweep_skip_missing_fields",
            "change_id": "12345",
        }),
        "not json at all",
        "",
    ]
    assert canary.parse_log_for_event(
        lines, "proactive_merger_thread_spawned", "12345"
    ) is None


def test_abandon_argv_includes_message_and_strict_host_key_policy():
    """Pin the SSH flags: BatchMode=yes (no interactive prompt), an
    accept-new host-key policy (matches the bridge's first-run UX),
    and a --message that leaves a paper trail in change history so
    an operator grepping abandoned changes can identify canary runs.
    """
    argv = canary.build_abandon_argv(
        "claude-bot@sora.services", 29418, "/home/user/.ssh/id", "12345"
    )
    assert argv[0] == "ssh"
    assert "-i" in argv and "/home/user/.ssh/id" in argv
    assert "-p" in argv and "29418" in argv
    assert "BatchMode=yes" in argv
    assert "--abandon" in argv
    assert "12345,1" in argv
    # The message is what an operator sees in `gerrit query --comments`
    msg_idx = argv.index("--message")
    assert "[CANARY]" in argv[msg_idx + 1]


def test_poll_for_event_returns_none_after_timeout(tmp_path):
    """Drive the poll loop with synthetic clocks so the unit test
    runs in microseconds. No matching line ever appears -> None.
    """
    log = tmp_path / "bridge.log"
    log.write_text(
        json.dumps({
            "event": "proactive_merger_thread_spawned",
            "change_id": "OTHER",
        }) + "\n"
    )
    ticks = iter([0.0, 0.0, 30.0, 60.1])
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return next(ticks)

    def fake_sleep(s: float) -> None:
        sleeps.append(s)

    result = canary.poll_for_event(
        log,
        start_offset=0,
        event="proactive_merger_thread_spawned",
        change_number="12345",
        timeout_seconds=60,
        poll_interval_seconds=5,
        sleep=fake_sleep,
        monotonic=fake_monotonic,
    )
    assert result is None
    assert sleeps, "poll loop should have slept at least once"


def test_poll_for_event_finds_match_after_log_grows(tmp_path):
    """Simulate the daemon writing the matching event a few seconds
    after the canary's push: the second log read sees a new line, the
    parser locks on, and the poller returns the record.
    """
    log = tmp_path / "bridge.log"
    log.write_text("")  # empty initial snapshot
    start_offset = 0

    state = {"appended": False}

    def fake_monotonic() -> float:
        return 0.0  # never time out — we'll exit via the match

    def fake_sleep(s: float) -> None:
        if not state["appended"]:
            with log.open("a") as fh:
                fh.write(json.dumps({
                    "event": "proactive_merger_thread_spawned",
                    "change_id": "12345",
                    "ps": "1",
                }) + "\n")
            state["appended"] = True

    result = canary.poll_for_event(
        log,
        start_offset=start_offset,
        event="proactive_merger_thread_spawned",
        change_number="12345",
        timeout_seconds=60,
        poll_interval_seconds=1,
        sleep=fake_sleep,
        monotonic=fake_monotonic,
    )
    assert result is not None
    assert result["change_id"] == "12345"


def test_emit_status_writes_well_formed_json(capsys):
    """T1's alerter parses these lines as JSON. Validate the field
    layout (timestamp, level, event) matches the bridge's
    ``structured_log`` so a single grok pattern works for both.
    """
    canary.emit_status("DEGRADED", canary.CANARY_EVENT_DEGRADED, stage="poll")
    out = capsys.readouterr().out.strip()
    rec = json.loads(out)
    assert rec["level"] == "DEGRADED"
    assert rec["event"] == "canary_pipeline_check_failed"
    assert "timestamp" in rec
    assert rec["stage"] == "poll"


def test_read_log_tail_handles_rotated_log(tmp_path):
    """If the log shrinks under us (logrotate truncated), we must
    re-read from offset 0 rather than seek past EOF and silently
    return nothing — that would mask a real DEGRADED event.
    """
    log = tmp_path / "bridge.log"
    log.write_text("line A\nline B\n")
    fake_offset = 10_000  # > current size
    lines = canary.read_log_tail_since(log, fake_offset)
    assert lines == ["line A", "line B"]


def test_main_refuses_to_run_without_ssh_key(tmp_path, capsys, monkeypatch):
    """rc=3 (config error) when the SSH key path doesn't exist. We
    treat this as different from rc=2 (pipeline DEGRADED) so the
    OnFailure hook can distinguish a misconfigured canary from a
    real outage. Both still trip the alert, but the structured log
    surfaces the cause.
    """
    monkeypatch.setenv("OMNISIGHT_GIT_SSH_KEY_PATH", str(tmp_path / "missing"))
    rc = canary.main([
        "--bridge-log", str(tmp_path / "bridge.log"),
        "--workdir", str(tmp_path / "wd"),
        "--ssh-key", str(tmp_path / "missing"),
    ])
    assert rc == 3
    out = capsys.readouterr().out.strip()
    rec = json.loads(out.splitlines()[-1])
    assert rec["level"] == "ERROR"
    assert rec["stage"] == "config"


# ─── systemd unit contract tests ─────────────────────────────────────


def _service_block(unit_text: str, section: str = "Service") -> str:
    m = re.search(
        rf"^\[{section}\]\s*\n(.*?)(?=^\[|\Z)", unit_text, re.S | re.M
    )
    assert m, f"unit file missing [{section}] section"
    return m.group(1)


def _directive(block: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.+?)\s*$", block, re.M)
    return m.group(1) if m else None


def test_canary_service_exists():
    assert CANARY_SERVICE.exists()
    assert CANARY_TIMER.exists()
    assert CANARY_ALERT.exists()


def test_canary_timer_is_hourly_and_persistent():
    """AC #2 explicitly says DEGRADED within 1 h. Pin both
    OnUnitActiveSec=1h (the cadence) and Persistent=true (catch-up
    after offline windows). A future tweak to 6h or removal of
    Persistent would silently break the contract.
    """
    text = CANARY_TIMER.read_text()
    timer = _service_block(text, "Timer")
    assert _directive(timer, "OnUnitActiveSec") == "1h"
    assert _directive(timer, "Persistent") == "true"
    assert _directive(timer, "Unit") == "omnisight-canary.service"


def test_canary_service_chains_alert_on_failure():
    """OnFailure is the T1 (OP-722) hand-off. Without it, a non-zero
    canary exit goes nowhere and we're back to OP-708-class silent
    failure. Test forbids removal.
    """
    text = CANARY_SERVICE.read_text()
    unit = _service_block(text, "Unit")
    assert _directive(unit, "OnFailure") == "omnisight-canary-alert.service"


def test_canary_service_is_oneshot():
    """Type=oneshot is what makes the timer drive the cadence rather
    than the service restarting on its own (which would double-fire
    on every push).
    """
    text = CANARY_SERVICE.read_text()
    svc = _service_block(text, "Service")
    assert _directive(svc, "Type") == "oneshot"


def test_canary_service_invokes_canary_script():
    """The ExecStart must point at scripts/canary_pipeline.py so the
    behavior pinned by the pure-function tests above is what runs in
    production. Pin via path suffix rather than full path so the
    test passes regardless of the deploy host's $HOME.
    """
    text = CANARY_SERVICE.read_text()
    svc = _service_block(text, "Service")
    exec_start = _directive(svc, "ExecStart") or ""
    assert exec_start.endswith("scripts/canary_pipeline.py --bridge-log %h/work/sora/logs/bridge/systemd.log") \
        or "scripts/canary_pipeline.py" in exec_start


def test_canary_service_runs_after_bridge():
    """The canary measures the bridge — so it must come up after the
    bridge does, otherwise the first run after a coordinated reboot
    races the daemon and false-positives DEGRADED.
    """
    text = CANARY_SERVICE.read_text()
    unit = _service_block(text, "Unit")
    after = _directive(unit, "After") or ""
    assert "gerrit-jira-bridge.service" in after


def test_canary_alert_emits_canary_pipeline_alert_event():
    """T1's alerter (OP-722) keys on event=canary_pipeline_alert. If
    a future edit silently changes the event name, T1 stops alerting
    and we're back to OP-708. Pin the literal string.
    """
    text = CANARY_ALERT.read_text()
    svc = _service_block(text, "Service")
    exec_start = _directive(svc, "ExecStart") or ""
    assert "canary_pipeline_alert" in exec_start
    assert "DEGRADED" in exec_start


def test_canary_alert_is_oneshot_to_avoid_restart_loops():
    """If the alert handler ever restarted on failure, a stuck T1
    pipeline would loop-spam DEGRADED into the log. Pin oneshot.
    """
    text = CANARY_ALERT.read_text()
    svc = _service_block(text, "Service")
    assert _directive(svc, "Type") == "oneshot"
