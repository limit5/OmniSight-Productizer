"""Contract tests for backend.agents.auto_rebase (OP-733).

Covers the OP-733 acceptance criteria:

* AC#1 (debounce) — :func:`test_debounce_collapses_burst_into_one_run`
* AC#2 (REST per owner) — :func:`test_attempt_rebase_uses_owner_basic_auth`
* AC#3 (success → JIRA notice) — :func:`test_success_posts_jira_comment`
* AC#4 (conflict → leave alone) — :func:`test_attempt_rebase_409_returns_conflict`
* AC#5 (skip +2) — :func:`test_skip_when_change_has_code_review_plus_2`
* AC#6 (synthetic 3-PS sweep) — :func:`test_synthetic_sweep_disjoint_overlap_plus_two`
* AC#7 (idempotency) — :func:`test_sweep_twice_does_not_double_upload`
* AC#8 (operator notification on conflict) —
  :func:`test_conflict_emits_warn_log_with_files`
* OP-753 concurrency cap —
  :func:`test_synthetic_eight_rebases_never_exceed_two_active_attempts`
* OP-753 env override —
  :func:`test_rebase_concurrency_env_override_allows_three_active_attempts`

Plus owner-key-bag dispatch + env kill switch + missing-owner skip tests.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import auto_rebase
from backend.agents import gerrit_jira_bridge as bridge


# ── Test scaffolding ─────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.code = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeHTTPError(Exception):
    """Minimal stand-in for urllib.error.HTTPError accepted by sweeper."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"HTTP {status}")
        self.code = status
        self.fp = self
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        return None


def _fake_urlopen_factory(
    captured: list[dict[str, Any]],
    responses: list[Any],
):
    """Build a urlopen that records each call + plays back ``responses``.

    Each response can be a :class:`_FakeResp`, a :class:`_FakeHTTPError`
    (raised on read), or a callable taking the request and returning
    one of the above.
    """
    def fake_urlopen(req: Any, timeout: int = 30) -> Any:
        captured.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": {k.lower(): v for k, v in req.headers.items()},
            "data": req.data,
        })
        idx = len(captured) - 1
        item = responses[idx] if idx < len(responses) else responses[-1]
        if callable(item):
            item = item(req)
        if isinstance(item, _FakeHTTPError):
            # Re-route through the urllib.error path the sweeper catches
            import urllib.error
            err = urllib.error.HTTPError(
                req.full_url, item.code, "fake", {}, item,
            )
            raise err
        return item
    return fake_urlopen


def _gerrit_query_runner(stdout_objs: list[dict[str, Any]]):
    """Build a run_command stub that returns ``stdout_objs`` as JSONL."""
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        stdout = "\n".join(json.dumps(o) for o in stdout_objs) + "\n"
        return subprocess.CompletedProcess(cmd, 0, stdout, "")
    return fake_run


def _ssh_cmd_builder(*remote_args: str) -> list[str]:
    return ["ssh", "fake-host", *remote_args]


def _ssh_env_builder() -> dict[str, str]:
    return {"GIT_SSH_COMMAND": "ssh"}


def _make_change(
    *,
    number: int,
    change_id: str,
    owner: str = "claude-bot",
    parent_sha: str = "old_parent_sha",
    subject: str = "[OP-19] sample",
    code_review_plus_2: bool = False,
) -> dict[str, Any]:
    approvals: list[dict[str, Any]] = []
    if code_review_plus_2:
        approvals.append({"type": "Code-Review", "value": "2"})
    return {
        "id": change_id,
        "number": number,
        "subject": subject,
        "branch": "develop",
        "owner": {"username": owner, "name": owner},
        "currentPatchSet": {
            "number": 1,
            "revision": f"rev_{number}",
            "parents": [{"revision": parent_sha}],
            "approvals": approvals,
        },
    }


def _make_sweeper(
    *,
    open_changes: list[dict[str, Any]] | None = None,
    rest_responses: list[Any] | None = None,
    rest_calls: list[dict[str, Any]] | None = None,
    notify_calls: list[tuple[str, str]] | None = None,
    log_calls: list[tuple[str, str, dict[str, Any]]] | None = None,
    owner_passwords: dict[str, str] | None = None,
) -> auto_rebase.AutoRebaseSweeper:
    rest_calls = rest_calls if rest_calls is not None else []
    notify_calls = notify_calls if notify_calls is not None else []
    log_calls = log_calls if log_calls is not None else []
    owner_passwords = owner_passwords if owner_passwords is not None else {
        "claude-bot": "claude-pw",
        "codex-bot": "codex-pw",
    }
    return auto_rebase.AutoRebaseSweeper(
        ssh_cmd_builder=_ssh_cmd_builder,
        ssh_env_builder=_ssh_env_builder,
        run_command=_gerrit_query_runner(open_changes or []),
        urlopen=_fake_urlopen_factory(
            rest_calls, rest_responses or [_FakeResp(200, b")]}'\n{}")],
        ),
        load_password=lambda u: owner_passwords.get(u),
        notify_jira=lambda key, msg: notify_calls.append((key, msg)),
        log=lambda level, label, **kw: log_calls.append((level, label, kw)),
    )


# ── Owner-key dispatch ───────────────────────────────────────────────


def test_load_owner_password_prefers_dedicated_file(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    pw_path = tmp_path / "gerrit-claude-bot-http-password"
    pw_path.write_text("super-secret\n")
    env_path = tmp_path / "gerrit-claude.env"
    env_path.write_text(
        "OMNISIGHT_GERRIT_CLAUDE_HTTP_PASSWORD=fallback-only\n",
    )
    monkeypatch.setitem(
        auto_rebase.OWNER_HTTP_PASSWORD_PATHS, "claude-bot", pw_path,
    )
    monkeypatch.setitem(
        auto_rebase.OWNER_ENV_FALLBACK, "claude-bot",
        (env_path, "OMNISIGHT_GERRIT_CLAUDE_HTTP_PASSWORD"),
    )
    assert auto_rebase.load_owner_http_password("claude-bot") == "super-secret"


def test_load_owner_password_falls_back_to_env_file(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "absent"
    env_path = tmp_path / "gerrit-codex.env"
    env_path.write_text(
        "# header\n"
        "OMNISIGHT_GERRIT_CODEX_HTTP_PASSWORD=env-secret\n",
    )
    monkeypatch.setitem(
        auto_rebase.OWNER_HTTP_PASSWORD_PATHS, "codex-bot", missing,
    )
    monkeypatch.setitem(
        auto_rebase.OWNER_ENV_FALLBACK, "codex-bot",
        (env_path, "OMNISIGHT_GERRIT_CODEX_HTTP_PASSWORD"),
    )
    assert auto_rebase.load_owner_http_password("codex-bot") == "env-secret"


def test_load_owner_password_unknown_user_returns_none() -> None:
    assert auto_rebase.load_owner_http_password("not-a-bot") is None


# ── attempt_rebase paths ─────────────────────────────────────────────


def test_attempt_rebase_uses_owner_basic_auth() -> None:
    rest_calls: list[dict[str, Any]] = []
    sweeper = _make_sweeper(
        rest_responses=[_FakeResp(
            200, b")]}'\n{\"current_revision\": \"newrev_19\"}",
        )],
        rest_calls=rest_calls,
    )
    result = sweeper.attempt_rebase(
        _make_change(number=19, change_id="I19"),
        target_sha="merged_sha_v1",
    )
    assert result.success is True
    assert result.new_revision == "newrev_19"
    # The REST call must be Basic auth keyed off the owner's password
    assert len(rest_calls) == 1
    auth = rest_calls[0]["headers"].get("authorization", "")
    assert auth.startswith("Basic ")
    import base64 as _b64
    user_pass = _b64.b64decode(auth.split(" ", 1)[1]).decode()
    assert user_pass == "claude-bot:claude-pw"
    # Body must include the merged SHA as base
    body = json.loads(rest_calls[0]["data"].decode())
    assert body == {"base": "merged_sha_v1"}
    # URL must hit the change's REST rebase endpoint
    assert "/a/changes/I19/revisions/current/rebase" in rest_calls[0]["url"]


def test_attempt_rebase_409_returns_conflict() -> None:
    sweeper = _make_sweeper(
        rest_responses=[_FakeHTTPError(
            409, b"merge conflict in: foo.py, bar.py",
        )],
    )
    result = sweeper.attempt_rebase(
        _make_change(number=19, change_id="I19"),
        target_sha="sha",
    )
    assert result.conflict is True
    assert result.success is False
    assert result.files == ("foo.py", "bar.py")


def test_skip_when_change_has_code_review_plus_2() -> None:
    rest_calls: list[dict[str, Any]] = []
    sweeper = _make_sweeper(
        rest_responses=[_FakeResp(200, b")]}'\n{}")],
        rest_calls=rest_calls,
    )
    result = sweeper.attempt_rebase(
        _make_change(number=22, change_id="I22", code_review_plus_2=True),
        target_sha="sha",
    )
    assert result.skipped is True
    assert result.skip_reason == "code_review_plus_2"
    assert rest_calls == []  # no REST issued


def test_skip_when_owner_has_no_password() -> None:
    rest_calls: list[dict[str, Any]] = []
    sweeper = _make_sweeper(
        rest_responses=[_FakeResp(200, b")]}'\n{}")],
        rest_calls=rest_calls,
        owner_passwords={"codex-bot": "x"},  # claude-bot intentionally missing
    )
    result = sweeper.attempt_rebase(
        _make_change(number=33, change_id="I33", owner="claude-bot"),
        target_sha="sha",
    )
    assert result.skipped is True
    assert result.skip_reason.startswith("no_password_for_owner:")
    assert rest_calls == []


def test_skip_when_already_on_target_base() -> None:
    rest_calls: list[dict[str, Any]] = []
    sweeper = _make_sweeper(
        rest_responses=[_FakeResp(200, b")]}'\n{}")],
        rest_calls=rest_calls,
    )
    result = sweeper.attempt_rebase(
        _make_change(number=44, change_id="I44", parent_sha="merged_sha_v1"),
        target_sha="merged_sha_v1",
    )
    assert result.skipped is True
    assert result.skip_reason == "already_on_target_base"
    assert rest_calls == []


def test_attempt_rebase_other_status_records_error() -> None:
    sweeper = _make_sweeper(
        rest_responses=[_FakeHTTPError(403, b"forbidden - bad creds")],
    )
    result = sweeper.attempt_rebase(
        _make_change(number=55, change_id="I55"),
        target_sha="sha",
    )
    assert result.error.startswith("HTTP 403")
    assert result.success is False
    assert result.conflict is False


# ── JIRA notification on success ─────────────────────────────────────


def test_success_posts_jira_comment() -> None:
    notify_calls: list[tuple[str, str]] = []
    sweeper = _make_sweeper(
        rest_responses=[_FakeResp(
            200, b")]}'\n{\"current_revision\": \"newrev\"}",
        )],
        notify_calls=notify_calls,
    )
    sweeper.attempt_rebase(
        _make_change(
            number=19, change_id="I19",
            subject="[OP-101] payload subject",
        ),
        target_sha="abcdef0123456789",
    )
    assert len(notify_calls) == 1
    assert notify_calls[0][0] == "OP-101"
    assert "abcdef01" in notify_calls[0][1]
    assert "Auto-rebased" in notify_calls[0][1]


def test_success_without_op_key_in_subject_does_not_notify() -> None:
    notify_calls: list[tuple[str, str]] = []
    sweeper = _make_sweeper(
        rest_responses=[_FakeResp(200, b")]}'\n{}")],
        notify_calls=notify_calls,
    )
    sweeper.attempt_rebase(
        _make_change(
            number=19, change_id="I19",
            subject="docs only no jira key",
        ),
        target_sha="sha",
    )
    assert notify_calls == []


# ── conflict logging ─────────────────────────────────────────────────


def test_conflict_emits_warn_log_with_files() -> None:
    log_calls: list[tuple[str, str, dict[str, Any]]] = []
    sweeper = _make_sweeper(
        open_changes=[_make_change(number=19, change_id="I19")],
        rest_responses=[_FakeHTTPError(
            409, b"merge conflict in: backend/foo.py",
        )],
        log_calls=log_calls,
    )
    results = sweeper.sweep(project="omnisight/p", merged_sha="sha")
    assert len(results) == 1 and results[0].conflict is True
    warn_logs = [c for c in log_calls if c[1] == "auto_rebase_conflict"]
    assert len(warn_logs) == 1
    assert warn_logs[0][2].get("files") == ["backend/foo.py"]


# ── synthetic 3-PS sweep (AC#6) ──────────────────────────────────────


def test_synthetic_sweep_disjoint_overlap_plus_two() -> None:
    """3 open PSes; merge a develop change touching the overlapping file:

    * disjoint  → rebases cleanly (REST 200)
    * overlap   → conflicts (REST 409)
    * already_+2 → skipped without REST
    """
    disjoint = _make_change(
        number=10, change_id="Idisjoint",
        subject="[OP-10] disjoint",
    )
    overlap = _make_change(
        number=11, change_id="Ioverlap",
        subject="[OP-11] overlap",
    )
    plus_two = _make_change(
        number=12, change_id="Iplus2",
        subject="[OP-12] +2 about to land",
        code_review_plus_2=True,
    )

    rest_calls: list[dict[str, Any]] = []
    notify_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[str, str, dict[str, Any]]] = []

    def respond(req: Any) -> Any:
        if "Idisjoint" in req.full_url:
            return _FakeResp(200, b")]}'\n{\"current_revision\": \"new10\"}")
        if "Ioverlap" in req.full_url:
            return _FakeHTTPError(409, b"merge conflict in: shared.py")
        raise AssertionError(f"unexpected URL: {req.full_url}")

    sweeper = auto_rebase.AutoRebaseSweeper(
        ssh_cmd_builder=_ssh_cmd_builder,
        ssh_env_builder=_ssh_env_builder,
        run_command=_gerrit_query_runner([disjoint, overlap, plus_two]),
        urlopen=_fake_urlopen_factory(rest_calls, [respond, respond]),
        load_password=lambda u: "pw",
        notify_jira=lambda key, msg: notify_calls.append((key, msg)),
        log=lambda level, label, **kw: log_calls.append((level, label, kw)),
    )

    results = sweeper.sweep(
        project="omnisight/p", merged_sha="merged_v1",
    )

    by_change = {r.change_number: r for r in results}
    assert by_change["10"].success is True
    assert by_change["11"].conflict is True
    assert by_change["11"].files == ("shared.py",)
    assert by_change["12"].skipped is True
    assert by_change["12"].skip_reason == "code_review_plus_2"

    # +2 must NOT have triggered any REST
    assert all("Iplus2" not in c["url"] for c in rest_calls)
    assert len(rest_calls) == 2  # only disjoint + overlap

    # Successful rebase posted a JIRA comment to OP-10 only
    assert notify_calls == [(
        "OP-10",
        "Auto-rebased onto merged_v; please re-review the diff "
        "against the new base.",
    )]


# ── idempotency (AC#7) ───────────────────────────────────────────────


def test_sweep_twice_does_not_double_upload() -> None:
    """Two consecutive sweeps with the same merged_sha must not produce
    two POST /rebase calls for the same change.

    First sweep: REST 200 + ``rebased_onto`` records (number, sha).
    Second sweep: in-process dedup short-circuits before REST.
    """
    change = _make_change(number=10, change_id="I10")
    rest_calls: list[dict[str, Any]] = []
    sweeper = auto_rebase.AutoRebaseSweeper(
        ssh_cmd_builder=_ssh_cmd_builder,
        ssh_env_builder=_ssh_env_builder,
        run_command=_gerrit_query_runner([change]),
        urlopen=_fake_urlopen_factory(
            rest_calls,
            [_FakeResp(200, b")]}'\n{\"current_revision\": \"r2\"}")],
        ),
        load_password=lambda u: "pw",
        notify_jira=None,
        log=lambda *a, **k: None,
    )
    results1 = sweeper.sweep(project="p", merged_sha="merged_v1")
    results2 = sweeper.sweep(project="p", merged_sha="merged_v1")

    assert results1[0].success is True
    assert results2[0].skipped is True
    assert results2[0].skip_reason == "already_rebased_in_session"
    assert len(rest_calls) == 1


# ── OP-753 rebase concurrency token bucket ───────────────────────────


def _instrument_attempts(
    sweeper: auto_rebase.AutoRebaseSweeper,
    *,
    sleep_s: float = 0.02,
) -> dict[str, int]:
    counters = {"active": 0, "max_active": 0, "completed": 0}
    lock = threading.Lock()

    def fake_attempt(
        change: dict[str, Any], target_sha: str,
    ) -> auto_rebase.RebaseResult:
        with lock:
            counters["active"] += 1
            counters["max_active"] = max(
                counters["max_active"], counters["active"],
            )
        time.sleep(sleep_s)
        with lock:
            counters["active"] -= 1
            counters["completed"] += 1
        return auto_rebase.RebaseResult(
            change_number=str(change["number"]),
            success=True,
            new_revision=f"rebased-{target_sha}",
        )

    sweeper.attempt_rebase = fake_attempt  # type: ignore[method-assign]
    return counters


def test_synthetic_eight_rebases_never_exceed_two_active_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """8 open PSes fan out, but the default token bucket permits only
    two active rebase attempts at once."""
    monkeypatch.delenv(auto_rebase.REBASE_CONCURRENCY_ENV, raising=False)
    open_changes = [
        _make_change(number=i, change_id=f"I{i}") for i in range(8)
    ]
    sweeper = _make_sweeper(open_changes=open_changes)
    counters = _instrument_attempts(sweeper)

    results = sweeper.sweep(project="p", merged_sha="merged")

    assert len(results) == 8
    assert all(r.success for r in results)
    assert counters["completed"] == 8
    assert counters["max_active"] == 2


def test_rebase_concurrency_env_override_allows_three_active_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(auto_rebase.REBASE_CONCURRENCY_ENV, "3")
    open_changes = [
        _make_change(number=i, change_id=f"I{i}") for i in range(6)
    ]
    sweeper = _make_sweeper(open_changes=open_changes)
    counters = _instrument_attempts(sweeper)

    results = sweeper.sweep(project="p", merged_sha="merged")

    assert len(results) == 6
    assert all(r.success for r in results)
    assert counters["completed"] == 6
    assert counters["max_active"] == 3


# ── env kill switch ──────────────────────────────────────────────────


def test_env_kill_switch_skips_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auto_rebase.SWEEP_DISABLE_ENV, "1")
    rest_calls: list[dict[str, Any]] = []
    sweeper = _make_sweeper(
        open_changes=[_make_change(number=10, change_id="I10")],
        rest_responses=[_FakeResp(200, b")]}'\n{}")],
        rest_calls=rest_calls,
    )
    assert sweeper.sweep(project="p", merged_sha="sha") == []
    assert rest_calls == []


def test_invalid_args_skip_sweep() -> None:
    log_calls: list[tuple[str, str, dict[str, Any]]] = []
    sweeper = _make_sweeper(log_calls=log_calls)
    assert sweeper.sweep(project="", merged_sha="sha") == []
    assert sweeper.sweep(project="p", merged_sha="") == []
    assert any(c[1] == "auto_rebase_sweep_invalid_args" for c in log_calls)


# ── DebouncedSweepScheduler (AC#1 — debounce) ────────────────────────


class _ManualTimer:
    """Substitute for threading.Timer that records but does not auto-fire."""

    instances: list["_ManualTimer"] = []

    def __init__(self, delay: float, callback: Any) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.cancelled = False
        self.started = False
        _ManualTimer.instances.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def is_alive(self) -> bool:
        return self.started and not self.cancelled

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


@pytest.fixture(autouse=True)
def _reset_manual_timer() -> None:
    _ManualTimer.instances.clear()


def test_debounce_collapses_burst_into_one_run() -> None:
    """Five change-merged events within the window → ONE runner call on
    the most recent merged SHA."""
    runs: list[tuple[str, str]] = []
    scheduler = auto_rebase.DebouncedSweepScheduler(
        runner=lambda p, s: runs.append((p, s)),
        delay_seconds=30.0,
        timer_factory=_ManualTimer,
    )
    for sha in ["sha1", "sha2", "sha3", "sha4", "sha5"]:
        scheduler.schedule(project="p", merged_sha=sha)
    # 5 schedules → 5 timers built; first 4 cancelled, last one fires
    assert len(_ManualTimer.instances) == 5
    assert sum(1 for t in _ManualTimer.instances if t.cancelled) == 4
    last = _ManualTimer.instances[-1]
    assert not last.cancelled
    assert last.delay == 30.0
    last.fire()
    assert runs == [("p", "sha5")]


def test_debounce_runner_exception_does_not_propagate() -> None:
    log_calls: list[tuple[str, str, dict[str, Any]]] = []

    def boom(p: str, s: str) -> None:
        raise RuntimeError("synthetic-runner-failure")

    scheduler = auto_rebase.DebouncedSweepScheduler(
        runner=boom,
        delay_seconds=30.0,
        timer_factory=_ManualTimer,
        log=lambda level, label, **kw: log_calls.append((level, label, kw)),
    )
    scheduler.schedule(project="p", merged_sha="sha")
    _ManualTimer.instances[-1].fire()
    err = [c for c in log_calls if c[1] == "auto_rebase_sweep_runner_error"]
    assert len(err) == 1
    assert "synthetic-runner-failure" in err[0][2].get("err", "")


# ── Bridge wiring (process_stream_event → schedule sweep) ────────────


def _bridge_with_captured_scheduler(monkeypatch: pytest.MonkeyPatch
                                    ) -> tuple[bridge.GerritJiraBridge, list[Any]]:
    """Build a bridge whose auto-rebase scheduler is replaced by a spy."""
    schedules: list[tuple[str, str]] = []

    class SpyScheduler:
        def schedule(self, *, project: str, merged_sha: str) -> None:
            schedules.append((project, merged_sha))

    from backend.agents import jira_dispatch
    client = jira_dispatch.DispatchClient(
        agent_class="subscription-claude",
        base_url="https://example.atlassian.net/rest/api/3",
        project_key="OP",
        auth_header="Basic test",
        bot_account_id="acct-1",
        bot_email="rt3628+claude-bot@gmail.com",
    )
    b = bridge.GerritJiraBridge(
        client,
        bridge.BridgeConfig(heartbeat_seconds=9999, periodic_catchup_seconds=0),
        sleep=lambda _: None,
        logger=lambda *a, **k: None,
    )
    b._auto_rebase_scheduler = SpyScheduler()
    return b, schedules


def test_bridge_change_merged_schedules_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, schedules = _bridge_with_captured_scheduler(monkeypatch)
    # Bypass the OP-689 transition path
    b._handle_change_merged = lambda _e: None  # type: ignore[method-assign]
    event = {
        "type": "change-merged",
        "newRev": "abcdef1234",
        "change": {
            "id": "Iabc",
            "number": 19,
            "project": "omnisight/OmniSight-Productizer",
            "branch": "develop",
            "subject": "[OP-19] x",
        },
    }
    b.process_stream_event(event)
    assert schedules == [("omnisight/OmniSight-Productizer", "abcdef1234")]


def test_bridge_skips_sweep_for_non_develop_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, schedules = _bridge_with_captured_scheduler(monkeypatch)
    b._handle_change_merged = lambda _e: None  # type: ignore[method-assign]
    event = {
        "type": "change-merged",
        "newRev": "abcdef1234",
        "change": {
            "id": "Iabc",
            "number": 19,
            "project": "p",
            "branch": "release/foo",
            "subject": "[OP-19] x",
        },
    }
    b.process_stream_event(event)
    assert schedules == []  # non-develop branches are not swept


def test_bridge_skips_sweep_when_missing_merged_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, schedules = _bridge_with_captured_scheduler(monkeypatch)
    b._handle_change_merged = lambda _e: None  # type: ignore[method-assign]
    event = {
        "type": "change-merged",
        "change": {
            "id": "Iabc",
            "project": "p",
            "branch": "develop",
            "subject": "[OP-19] x",
        },
    }
    b.process_stream_event(event)
    assert schedules == []
