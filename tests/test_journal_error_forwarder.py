r"""OP-724 — ERROR-level journal → pager forwarder (META OP-721 T3).

Acceptance-criteria tests. All four ACs from the ticket are exercised
without requiring a live systemd or a real journalctl process: the
forwarder pipeline is split into pure functions (``derive_code``,
``classify_severity``, ``Pipeline.handle_record``) so the tests just
hand-construct journal records and assert on the dispatch result.

* AC #1 — backend exception → DEGRADED within 1 min.
  ``test_ac1_backend_exception_routes_to_degraded`` proves the severity
  classification, and
  ``test_ac1_dedup_window_meets_one_minute_latency_budget`` pins the
  config so a future operator can't slow it past the AC.
* AC #2 — 5 identical errors in 1 min → 1 alert with count=5.
  ``test_ac2_five_identical_errors_collapse_to_count_five`` drives the
  pipeline through a real T1-shaped Notifier (FakeNotifier) with the
  full coalesce-and-flush loop and asserts on the dispatched count.
* AC #3 — allow-listed code → no alert.
  ``test_ac3_allowlisted_code_is_suppressed`` and
  ``test_ac3_allowlisted_code_does_not_call_notifier`` cover the
  short-circuit.
* AC #4 — forwarder crash → systemd restarts, no log loss > 10s.
  Pinned via the systemd unit-file contract (same enforcement style as
  ``tests/test_systemd_graceful_shutdown.py`` and ``test_daemon_watchdog.py``):
  ``test_ac4_unit_uses_restart_always`` /
  ``test_ac4_unit_restart_sec_under_log_loss_budget`` /
  ``test_ac4_cursor_file_resume_prevents_log_loss`` together prove the
  ≤ 10s log-loss budget.

Cost: <100 ms, stdlib + PyYAML only — same self-defence rationale as
T2 OP-723's tests.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import scripts.journal_error_forwarder as jef  # noqa: E402 — path manip first

CONFIG_PATH = REPO_ROOT / "configs" / "error_pager.yaml"
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
SERVICE_UNIT = SYSTEMD_DIR / "omnisight-journal-error-forwarder.service"


# ─────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────


def _journal_record(
    *,
    message: str,
    priority: int = 3,
    unit: str = "omnisight-backend.service",
    cursor: str = "s=abc;i=1",
) -> dict[str, Any]:
    """Build a journalctl --output=json record dict. Fields chosen to
    match what real journald emits for ERROR-priority backend lines."""
    return {
        "__CURSOR": cursor,
        "MESSAGE": message,
        "PRIORITY": str(priority),
        "_SYSTEMD_UNIT": unit,
        "_PID": "1234",
        "_HOSTNAME": "test-host",
    }


def _read_section(unit: Path, name: str) -> str:
    text = unit.read_text()
    m = re.search(rf"^\[{re.escape(name)}\]\s*\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    assert m, f"section [{name}] not found in {unit}"
    return m.group(1)


def _directive(block: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.+?)\s*$", block, re.M)
    return m.group(1) if m else None


class _FakeNotifier:
    """Behaviour-faithful T1 ``Notifier`` double.

    Implements the subset of T1's API that the forwarder calls
    (``notify``, ``flush_expired``, ``flush_all``) with the same
    burst-coalescing semantics so AC #2 is a real integration test
    rather than a mock-shaped tautology. Clock is injectable so the
    dedup window is deterministic.
    """

    def __init__(self, dedup_window_seconds: float, clock):
        self.dedup_window_seconds = dedup_window_seconds
        self._clock = clock
        # key = (code, severity); value = dict(first_seen, count, last_message, last_context)
        self._bursts: dict[tuple[str, str], dict[str, Any]] = {}
        self.dispatched: list[dict[str, Any]] = []  # what was sent over the wire
        self.notify_calls: list[tuple[str, str, str, dict[str, Any]]] = []

    def notify(
        self,
        severity: str,
        code: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.notify_calls.append((severity, code, message, dict(context or {})))
        now = self._clock()
        key = (code, severity)
        existing = self._bursts.get(key)
        if existing and (now - existing["first_seen"]) > self.dedup_window_seconds:
            self._dispatch(existing, severity, code)
            existing = None
            self._bursts.pop(key, None)
        if existing is None:
            self._bursts[key] = {
                "first_seen": now,
                "count": 1,
                "message": message,
                "context": dict(context or {}),
            }
        else:
            existing["count"] += 1
            existing["message"] = message
            existing["context"] = dict(context or {})

    def flush_expired(self) -> None:
        now = self._clock()
        due_keys = [
            (c, s) for (c, s), e in self._bursts.items()
            if (now - e["first_seen"]) >= self.dedup_window_seconds
        ]
        for key in due_keys:
            entry = self._bursts.pop(key)
            self._dispatch(entry, key[1], key[0])

    def flush_all(self) -> None:
        for (code, severity), entry in list(self._bursts.items()):
            self._dispatch(entry, severity, code)
        self._bursts.clear()

    def _dispatch(self, entry: dict[str, Any], severity: str, code: str) -> None:
        self.dispatched.append({
            "severity": severity,
            "code": code,
            "count": entry["count"],
            "message": entry["message"],
            "context": entry["context"],
        })


def _pipeline_with_fake(
    tmp_path: Path,
    notifier: _FakeNotifier | None,
    *,
    allow: list[jef.AllowListEntry] | None = None,
    severity_map: dict[str, str] | None = None,
    extractors: list[str] | None = None,
    clock=None,
) -> jef.Pipeline:
    extractors = extractors if extractors is not None else [
        # Default: bridge-style ``"event":"<code>"`` pattern.
        r'"event"\s*:\s*"(?P<code>[a-zA-Z0-9_.:-]+)"',
        # Python tracebacks
        r'^(?P<code>[A-Z][A-Za-z0-9_]+(?:Error|Exception|Timeout)):',
    ]
    cfg = jef.ForwarderConfig(
        units=[],
        code_extractors=[jef.CodeExtractor.compile(p) for p in extractors],
        allow_list=list(allow or []),
        severity_map=dict(severity_map or {}),
        dedup_window_seconds=55.0,
        dispatch_poll_seconds=2.0,
        cursor_file=tmp_path / "cursor",
        alerts_path=tmp_path / "alerts.jsonl",
        notifier_module=None,
    )
    return jef.Pipeline(
        config=cfg,
        notifier=notifier,
        file_sink=jef.FileSink(cfg.alerts_path),
        clock=clock or time.time,
    )


# ─────────────────────────────────────────────────────────────────
# AC #1 — backend exception → DEGRADED within 1 min
# ─────────────────────────────────────────────────────────────────


def test_ac1_backend_exception_routes_to_degraded(tmp_path):
    """Python traceback ERROR record → severity DEGRADED, code derived."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 100.0)
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: 100.0)

    record = _journal_record(
        message="ValueError: refused_llm_unavailable: provider returned 503",
        priority=3,
    )
    result = pipe.handle_record(record)

    assert result.severity == jef.SEVERITY_DEGRADED
    assert result.suppressed is False
    assert result.notified is True
    # The Python-traceback extractor wins because it's a more specific
    # pattern than the unit:priority fallback.
    assert result.code == "ValueError"


def test_ac1_alert_priority_routes_to_critical(tmp_path):
    """PRIORITY=2 (CRIT) and PRIORITY=1 (ALERT) → severity CRITICAL.

    The ticket scope says explicitly: ``ALERT/CRITICAL → CRITICAL``."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: 0.0)

    for priority in (1, 2):
        result = pipe.handle_record(_journal_record(
            message="kernel panic: OOM in producer queue",
            priority=priority,
        ))
        assert result.severity == jef.SEVERITY_CRITICAL, (
            f"PRIORITY={priority} (ALERT/CRIT) must map to CRITICAL"
        )


def test_ac1_severity_override_promotes_known_bad_error(tmp_path):
    """A specific code can be promoted CRITICAL via severity_map even
    when its journald PRIORITY would default it to DEGRADED. Mirrors
    the ``refused_llm_unavailable`` example in the ticket scope."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(
        tmp_path, notifier,
        severity_map={"refused_llm_unavailable": "CRITICAL"},
        clock=lambda: 0.0,
    )

    result = pipe.handle_record(_journal_record(
        message='{"event":"refused_llm_unavailable","reason":"provider 503"}',
        priority=3,  # ERROR — would default to DEGRADED
    ))
    assert result.code == "refused_llm_unavailable"
    assert result.severity == jef.SEVERITY_CRITICAL


def test_ac1_dedup_window_meets_one_minute_latency_budget():
    """AC #1 latency budget invariant — flush ≤ 60s.

    The forwarder's worst-case single-error latency is
    ``dedup_window_seconds + dispatch_poll_seconds``. Production config
    must keep that under 60s or AC #1 is structurally violated.
    """
    cfg = jef.load_config(CONFIG_PATH)
    worst_case = cfg.dedup_window_seconds + cfg.dispatch_poll_seconds
    assert worst_case <= 60, (
        f"dedup_window_seconds={cfg.dedup_window_seconds} + "
        f"dispatch_poll_seconds={cfg.dispatch_poll_seconds} = "
        f"{worst_case}s exceeds AC #1's 1-min budget."
    )


def test_ac1_dispatched_within_window_seconds_after_first_error(tmp_path):
    """End-to-end timing: a single error fires, time advances past the
    dedup window, ``flush_expired`` dispatches DEGRADED within budget."""
    now = [100.0]
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: now[0])
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: now[0])

    pipe.handle_record(_journal_record(
        message="ValueError: db pool drained",
        priority=3,
    ))
    # Before the window elapses, nothing has been dispatched yet
    # (T1's coalescing path).
    assert notifier.dispatched == []

    # Advance past the window — exactly the 57s worst-case from the
    # invariant test above.
    now[0] += 57
    notifier.flush_expired()
    assert len(notifier.dispatched) == 1
    assert notifier.dispatched[0]["severity"] == jef.SEVERITY_DEGRADED
    assert notifier.dispatched[0]["count"] == 1


# ─────────────────────────────────────────────────────────────────
# AC #2 — 5 identical errors in 1 min → 1 alert with count=5
# ─────────────────────────────────────────────────────────────────


def test_ac2_five_identical_errors_collapse_to_count_five(tmp_path):
    """The forwarder hands all five errors to T1's burst-coalescer,
    flush_expired fires once, and the resulting outbound notification
    carries count=5 — the literal AC text."""
    now = [0.0]
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: now[0])
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: now[0])

    record_template = _journal_record(
        message='{"event":"refused_llm_unavailable","attempt":1}',
        priority=3,
    )
    # Drive 5 errors at 1-second intervals — the typical retry-loop
    # spacing. They share (code, severity) so they collapse.
    for i in range(5):
        now[0] = float(i)
        record = dict(record_template)
        record["__CURSOR"] = f"s=abc;i={i}"
        record["MESSAGE"] = f'{{"event":"refused_llm_unavailable","attempt":{i + 1}}}'
        result = pipe.handle_record(record)
        assert result.code == "refused_llm_unavailable"

    # Five notify() calls were made — the forwarder didn't pre-collapse.
    assert len(notifier.notify_calls) == 5

    # But before flush, T1's burst-coalescer is holding ONE entry.
    assert notifier.dispatched == []

    # Window elapses; flush dispatches exactly one outbound with count=5.
    now[0] = 100.0
    notifier.flush_expired()
    assert len(notifier.dispatched) == 1, (
        f"AC #2: expected 1 outbound, got {len(notifier.dispatched)} "
        f"({notifier.dispatched})"
    )
    assert notifier.dispatched[0]["count"] == 5
    assert notifier.dispatched[0]["code"] == "refused_llm_unavailable"


def test_ac2_distinct_codes_do_not_collapse(tmp_path):
    """Negative space: AC #2 dedup is per-code, so 5 DISTINCT codes
    must result in 5 outbound notifications. Ensures we didn't
    over-collapse."""
    now = [0.0]
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: now[0])
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: now[0])

    for i in range(5):
        now[0] = float(i)
        pipe.handle_record(_journal_record(
            message=f'{{"event":"distinct_failure_{i}"}}',
            priority=3,
            cursor=f"s=abc;i={i}",
        ))
    now[0] = 100.0
    notifier.flush_expired()
    assert len(notifier.dispatched) == 5
    assert {d["count"] for d in notifier.dispatched} == {1}


# ─────────────────────────────────────────────────────────────────
# AC #3 — allow-listed code → no alert
# ─────────────────────────────────────────────────────────────────


def test_ac3_allowlisted_code_is_suppressed(tmp_path):
    """The allow-list short-circuit returns suppressed=True before the
    notifier is ever called."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(
        tmp_path, notifier,
        allow=[jef.AllowListEntry(
            code="audit_pool_not_initialised",
            suppress=True,
            note="boot-time race; daemon retries on backoff",
        )],
        clock=lambda: 0.0,
    )

    result = pipe.handle_record(_journal_record(
        message='{"event":"audit_pool_not_initialised","retry":1}',
        priority=3,
    ))
    assert result.suppressed is True
    assert result.notified is False
    assert notifier.notify_calls == []


def test_ac3_allowlisted_code_does_not_call_notifier(tmp_path):
    """Mixed traffic: only suppressed codes are dropped; alertable
    codes alongside suppressed ones must still page."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(
        tmp_path, notifier,
        allow=[jef.AllowListEntry(
            code="audit_pool_not_initialised", suppress=True,
        )],
        clock=lambda: 0.0,
    )

    pipe.handle_record(_journal_record(
        message='{"event":"audit_pool_not_initialised"}',
        priority=3,
    ))
    pipe.handle_record(_journal_record(
        message='{"event":"refused_llm_unavailable"}',
        priority=3,
        cursor="s=abc;i=2",
    ))

    assert len(notifier.notify_calls) == 1
    severity, code, _msg, _ctx = notifier.notify_calls[0]
    assert code == "refused_llm_unavailable"
    assert severity == jef.SEVERITY_DEGRADED


def test_ac3_allowlist_pattern_matches_regex(tmp_path):
    """Allow-list also accepts ``code_pattern`` for codes whose suffix
    varies (e.g. transient_5xx_*). AC #3 talks about codes generally;
    a regex matcher is the operator-friendly way to whitelist a family."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(
        tmp_path, notifier,
        allow=[jef.AllowListEntry(
            code_pattern=re.compile(r"^transient_5\d\d_"),
            suppress=True,
        )],
        clock=lambda: 0.0,
    )

    result = pipe.handle_record(_journal_record(
        message='{"event":"transient_503_retry_succeeded"}',
        priority=3,
    ))
    assert result.suppressed is True
    assert notifier.notify_calls == []


def test_ac3_suppressed_record_still_advances_cursor(tmp_path):
    """Subtle but load-bearing: a suppressed record must STILL emit a
    cursor (so the daemon writes it) — otherwise on restart we'd
    replay the noisy record forever and fight the allow-list."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(
        tmp_path, notifier,
        allow=[jef.AllowListEntry(code="audit_pool_not_initialised", suppress=True)],
        clock=lambda: 0.0,
    )
    result = pipe.handle_record(_journal_record(
        message='{"event":"audit_pool_not_initialised"}',
        priority=3,
        cursor="s=abc;i=42",
    ))
    assert result.suppressed is True
    assert result.cursor == "s=abc;i=42"


# ─────────────────────────────────────────────────────────────────
# AC #4 — Forwarder crash → systemd restart, ≤ 10s log loss.
# Pinned via systemd unit-file contract.
# ─────────────────────────────────────────────────────────────────


def test_ac4_service_unit_exists():
    assert SERVICE_UNIT.exists(), (
        f"missing {SERVICE_UNIT.relative_to(REPO_ROOT)} — required by OP-724 AC #4 "
        "(without the unit, there's no systemd to restart the forwarder)."
    )


def test_ac4_unit_uses_restart_always():
    """``Restart=on-failure`` is INSUFFICIENT here: a journalctl SIGPIPE
    exits the python side with returncode 0, which would not trigger
    on-failure. ``Restart=always`` covers both crashes and clean exits."""
    block = _read_section(SERVICE_UNIT, "Service")
    val = _directive(block, "Restart")
    assert val == "always", (
        f"AC #4 needs Restart=always (clean-exit + crash both restart); got {val!r}."
    )


def test_ac4_unit_restart_sec_under_log_loss_budget():
    """Restart gap (RestartSec) + journalctl resume time must fit the
    AC #4 ≤ 10s log-loss budget. We pin RestartSec ≤ 5s here so the
    cursor-file resume has 5s of headroom."""
    block = _read_section(SERVICE_UNIT, "Service")
    val = _directive(block, "RestartSec")
    assert val is not None, "RestartSec must be set so the gap is bounded"
    # Accept '5', '5s', or '5sec' — systemd's all valid.
    digits = re.match(r"(\d+)", val)
    assert digits is not None
    seconds = int(digits.group(1))
    assert seconds <= 5, (
        f"AC #4: RestartSec={val!r} > 5s leaves no headroom under the 10s budget"
    )


def test_ac4_unit_caps_restart_storm():
    """A permabug on startup must not spin restarts forever. AC #4 is
    "systemd restarts" — implies bounded retries, not an infinite loop
    that masks a real outage."""
    block = _read_section(SERVICE_UNIT, "Unit")
    burst = _directive(block, "StartLimitBurst")
    interval = _directive(block, "StartLimitIntervalSec")
    assert burst is not None and int(burst) >= 1
    assert interval is not None


def test_ac4_unit_emits_to_journal():
    """The forwarder's own crash traceback must reach journald — so
    when AC #4 fires (forwarder crashed), the operator can root-cause
    via ``journalctl -u omnisight-journal-error-forwarder``."""
    block = _read_section(SERVICE_UNIT, "Service")
    assert _directive(block, "StandardError") == "journal", (
        "AC #4 + L24 lesson: traceback must land in journald for triage"
    )


def test_ac4_unit_invokes_forwarder_with_config():
    block = _read_section(SERVICE_UNIT, "Service")
    val = _directive(block, "ExecStart") or ""
    assert "journal_error_forwarder.py" in val
    assert "--config" in val
    assert "error_pager.yaml" in val


def test_ac4_unit_send_sigterm_for_clean_flush():
    """SIGTERM must be the kill signal so ``Pipeline.flush`` drains the
    in-flight T1 burst before exit — otherwise a clean ``systemctl stop``
    silently loses the residual count from any active dedup window."""
    block = _read_section(SERVICE_UNIT, "Service")
    val = _directive(block, "KillSignal")
    assert val == "SIGTERM", (
        f"AC #4 boundary: KillSignal must be SIGTERM so Pipeline.flush "
        f"runs on systemctl stop; got {val!r}."
    )


def test_ac4_cursor_file_resume_prevents_log_loss(tmp_path):
    """Functional half of AC #4: across a simulated crash, the cursor
    written before the crash is read on restart so the next journalctl
    invocation skips already-processed records.

    We don't run a real journalctl — we just prove the read/write
    primitives are atomic and round-trip the cursor faithfully. The
    journalctl ``--after-cursor`` flag is a stable interface.
    """
    cursor_file = tmp_path / "cursor"
    # No prior cursor → starts from the journal head (None).
    assert jef.read_cursor(cursor_file) is None

    # Pre-crash: the daemon wrote a cursor after each successful
    # dispatch.
    jef.write_cursor(cursor_file, "s=ABC;i=00ff;b=1234;m=5678;t=8000;x=DEAD")
    assert jef.read_cursor(cursor_file) == "s=ABC;i=00ff;b=1234;m=5678;t=8000;x=DEAD"

    # Post-crash: a new daemon would resume from this cursor.
    argv = jef.journalctl_argv(
        [jef.UnitSpec(name="omnisight-backend.service", scope="system")],
        cursor=jef.read_cursor(cursor_file),
    )
    assert any(
        a.startswith("--after-cursor=s=ABC;i=00ff;") for a in argv
    ), f"journalctl argv missing cursor resume: {argv}"


def test_ac4_cursor_write_is_atomic(tmp_path):
    """Atomicity invariant: a crash mid-write must not leave a half-
    truncated cursor file. We replace via os.replace which is atomic on
    POSIX. Verify by writing, replacing, re-reading."""
    cursor_file = tmp_path / "cursor"
    jef.write_cursor(cursor_file, "first")
    jef.write_cursor(cursor_file, "second")
    jef.write_cursor(cursor_file, "third")
    assert cursor_file.read_text() == "third"
    # No leftover .tmp file from the atomic-rename dance.
    assert not (cursor_file.with_suffix(".tmp")).exists()


def test_ac4_journalctl_argv_uses_priority_filter():
    """The kernel-side priority gate must be present, otherwise the
    Python loop chews on info/debug noise and the AC #1 latency budget
    becomes harder to meet."""
    argv = jef.journalctl_argv(
        [jef.UnitSpec(name="omnisight-backend", scope="system")],
        cursor=None,
    )
    assert "--priority=err" in argv


def test_ac4_journalctl_argv_user_scope_uses_user_unit_flag():
    """Scope='user' must use ``--user-unit`` (not ``--unit``) so a
    user-scoped daemon like gerrit-jira-bridge is reachable from the
    system-scope forwarder."""
    argv = jef.journalctl_argv(
        [jef.UnitSpec(name="gerrit-jira-bridge", scope="user")],
        cursor=None,
    )
    assert "--user-unit" in argv
    assert "--unit" not in argv


# ─────────────────────────────────────────────────────────────────
# Repo-wide invariants on configs/error_pager.yaml
# ─────────────────────────────────────────────────────────────────


def test_config_loads_cleanly():
    """Production YAML must parse with no manual fix-up — same defence
    as test_watchdog_config_load_round_trip."""
    cfg = jef.load_config(CONFIG_PATH)
    assert cfg.units, "no units configured — error pager has nothing to follow"
    assert cfg.code_extractors, "no code extractors — every burst dedup-keyed by unit:priority alone, defeating dedup"


def test_config_lists_backend_and_bridge_units():
    """The forwarder is meaningless if the OP-721 META primary daemons
    aren't in scope. Pin the ticket's named targets."""
    cfg = jef.load_config(CONFIG_PATH)
    names = {u.name for u in cfg.units}
    assert "gerrit-jira-bridge" in names, (
        "configs/error_pager.yaml must include gerrit-jira-bridge — that "
        "is the OP-721 META primary daemon T1/T2/T3 were built around."
    )
    # Either omnisight-backend or its templated worker form must be in.
    assert any("omnisight-backend" in n or "omnisight-worker" in n for n in names)


def test_config_severity_map_uses_valid_values():
    """A typo'd severity (e.g. 'CRIT' instead of 'CRITICAL') silently
    falls through to default in :func:`classify_severity`. Pin the
    spelling here so the typo gets caught at PR time."""
    cfg = jef.load_config(CONFIG_PATH)
    valid = {jef.SEVERITY_WARN, jef.SEVERITY_DEGRADED, jef.SEVERITY_CRITICAL, jef.SEVERITY_P0}
    for code, sev in cfg.severity_map.items():
        assert sev in valid, (
            f"severity_map[{code!r}] = {sev!r} is not a valid Severity "
            f"({sorted(valid)})"
        )


def test_config_notifier_module_points_at_t1():
    """T1 (OP-722) lives at ``backend.agents.operator_notifier``.
    Drift here would silently disable T1 routing (forwarder would fall
    back to file-sink-only, which a tail check might miss)."""
    cfg = jef.load_config(CONFIG_PATH)
    assert cfg.notifier_module == "backend.agents.operator_notifier"


# ─────────────────────────────────────────────────────────────────
# Code derivation + classification — pure unit tests
# ─────────────────────────────────────────────────────────────────


def test_derive_code_uses_event_pattern_first():
    extractors = [
        jef.CodeExtractor.compile(r'"event"\s*:\s*"(?P<code>[a-z_]+)"'),
        jef.CodeExtractor.compile(r'^(?P<code>[A-Z][A-Za-z0-9_]+(?:Error|Exception)):'),
    ]
    code = jef.derive_code(
        '{"event":"refused_llm_unavailable","attempt":1}',
        unit="bridge",
        priority=3,
        extractors=extractors,
    )
    assert code == "refused_llm_unavailable"


def test_derive_code_falls_back_to_unit_priority():
    """No extractor matches → ``<unit>:p<priority>`` fallback. This
    keeps dedup STABLE even for unparseable lines — losing dedup
    here re-introduces the alert flood AC #2 forbids."""
    code = jef.derive_code(
        "some entirely unstructured text",
        unit="omnisight-backend.service",
        priority=3,
        extractors=[],
    )
    assert code == "omnisight-backend.service:p3"


def test_derive_code_handles_missing_unit():
    code = jef.derive_code("blah", unit=None, priority=3, extractors=[])
    assert code == "unknown:p3"


def test_classify_severity_default_table():
    assert jef.classify_severity(0, "x", {}) == jef.SEVERITY_CRITICAL
    assert jef.classify_severity(1, "x", {}) == jef.SEVERITY_CRITICAL
    assert jef.classify_severity(2, "x", {}) == jef.SEVERITY_CRITICAL
    assert jef.classify_severity(3, "x", {}) == jef.SEVERITY_DEGRADED


def test_classify_severity_override_wins_over_priority():
    assert jef.classify_severity(3, "noisy_code", {"noisy_code": "WARN"}) == jef.SEVERITY_WARN


def test_allow_list_first_match_wins():
    al = jef.AllowList([
        jef.AllowListEntry(code="exact_first", suppress=True),
        jef.AllowListEntry(code_pattern=re.compile(r"^exact_"), suppress=False),
    ])
    # Both rules apply to "exact_first" but we only get the first.
    rule = al.match("exact_first")
    assert rule is not None and rule.suppress is True


def test_allow_list_no_match_returns_none():
    al = jef.AllowList([jef.AllowListEntry(code="other", suppress=True)])
    assert al.match("anything_else") is None


# ─────────────────────────────────────────────────────────────────
# T1-handshake invariants — forwarder must call notifier in T1's shape
# ─────────────────────────────────────────────────────────────────


def test_pipeline_passes_severity_string_compatible_with_t1_enum(tmp_path):
    """T1's ``Severity(value)`` constructor expects exactly one of
    'WARN', 'DEGRADED', 'CRITICAL', 'P0'. The forwarder must hand
    those strings verbatim."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: 0.0)

    pipe.handle_record(_journal_record(message="ValueError: bad", priority=3))
    severity, _code, _msg, _ctx = notifier.notify_calls[0]
    # Must round-trip through a Severity-shaped enum without raising.
    valid = {"WARN", "DEGRADED", "CRITICAL", "P0"}
    assert severity in valid


def test_pipeline_truncates_runaway_message(tmp_path):
    """A runaway log line (e.g. a 50KB JSON dump) would bloat the
    JIRA comment to unreadability. The forwarder caps at 512 chars."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: 0.0)

    huge = "ValueError: " + ("X" * 5000)
    pipe.handle_record(_journal_record(message=huge, priority=3))
    _sev, _code, msg, ctx = notifier.notify_calls[0]
    assert len(msg) <= 512
    assert len(ctx["message"]) <= 512


def test_pipeline_carries_journald_identifiers_in_context(tmp_path):
    """Operator triage relies on PID + hostname being in the context.
    Pin so a future refactor doesn't silently drop them."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: 0.0)

    pipe.handle_record(_journal_record(
        message="ValueError: boom",
        priority=3,
    ))
    _sev, _code, _msg, ctx = notifier.notify_calls[0]
    assert ctx["unit"] == "omnisight-backend.service"
    assert ctx["priority"] == 3
    assert ctx["pid"] == "1234"
    assert ctx["hostname"] == "test-host"


def test_pipeline_survives_notifier_exception(tmp_path):
    """A T1 misconfig (e.g. SMTP DNS failure during fanout) must NOT
    propagate up and kill the forwarder — that would defeat the whole
    point of "fail loud, then fan out"."""
    class BrokenNotifier:
        def notify(self, *a, **kw):
            raise RuntimeError("T1 went sideways")
        flush_expired = lambda self: None
        flush_all = lambda self: None

    pipe = _pipeline_with_fake(tmp_path, BrokenNotifier(), clock=lambda: 0.0)
    result = pipe.handle_record(_journal_record(
        message="ValueError: boom", priority=3,
    ))
    # Pipeline reported notifier failure but kept moving.
    assert result.notified is False
    assert result.severity == jef.SEVERITY_DEGRADED
    # File sink still got the record (defence-in-depth).
    alerts_text = (tmp_path / "alerts.jsonl").read_text().strip().splitlines()
    assert len(alerts_text) == 1
    assert json.loads(alerts_text[0])["notified"] is False


# ─────────────────────────────────────────────────────────────────
# Defensive parsing — journald edge cases
# ─────────────────────────────────────────────────────────────────


def test_pipeline_handles_missing_message_field(tmp_path):
    """journald sometimes emits records with no MESSAGE (binary blob,
    truncation). The forwarder must not crash."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: 0.0)

    record = {
        "__CURSOR": "s=abc;i=99",
        "PRIORITY": "3",
        "_SYSTEMD_UNIT": "omnisight-backend.service",
    }
    result = pipe.handle_record(record)  # no MESSAGE
    # Falls back to <unit>:p<priority> code.
    assert result.code == "omnisight-backend.service:p3"


def test_pipeline_handles_priority_as_int(tmp_path):
    """Priority arrives from journalctl as a string by default but
    some downstream tooling re-parses to int. Accept both."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: 0.0)

    rec = _journal_record(message="ValueError: x", priority=3)
    rec["PRIORITY"] = 3  # int instead of str
    result = pipe.handle_record(rec)
    assert result.severity == jef.SEVERITY_DEGRADED


def test_pipeline_handles_priority_garbage(tmp_path):
    """Garbage PRIORITY → default to ERROR-level (3) so we don't drop
    the record on the floor — fail-safe direction is to over-page."""
    notifier = _FakeNotifier(dedup_window_seconds=55, clock=lambda: 0.0)
    pipe = _pipeline_with_fake(tmp_path, notifier, clock=lambda: 0.0)

    rec = _journal_record(message="ValueError: x", priority=3)
    rec["PRIORITY"] = "not-a-number"
    result = pipe.handle_record(rec)
    assert result.severity == jef.SEVERITY_DEGRADED
