"""Contract tests for backend.agents.submit_queue_worker (OP-751).

Covers the OP-751 acceptance criteria:

* AC#1 (Submit-Ready label defined) — pinned in
  ``test_project_config_defines_submit_ready_label``.
* AC#2 (daemon implemented) — pinned by every functional test in
  this module exercising :class:`SubmitQueueWorker`.
* AC#3 (per-change lock prevents double-process) —
  :func:`test_change_lock_blocks_concurrent_acquisition` and
  :func:`test_process_change_skips_when_lock_held`.
* AC#4 (synthetic 5-PS burst on the same file all merge in order
  with no manual rebases) —
  :func:`test_synthetic_five_ps_burst_all_merge_in_order_no_manual_rebase`.
* AC#5 (rebase conflict → -1 + comment, operator retry possible) —
  :func:`test_rebase_conflict_votes_minus_one_with_comment`
  and :func:`test_after_minus_one_operator_can_retry_with_plus_one`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import submit_queue_worker as sqw


# ── Fakes ─────────────────────────────────────────────────────────────


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
    """Stand-in for urllib.error.HTTPError raised by urlopen."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"HTTP {status}")
        self.code = status
        self.fp = self
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        return None


def _ssh_cmd_builder(*remote_args: str) -> list[str]:
    return ["ssh", "fake-host", *remote_args]


def _ssh_env_builder() -> dict[str, str]:
    return {"GIT_SSH_COMMAND": "ssh"}


def _make_change(
    *,
    number: int,
    change_id: str,
    parent_sha: str,
    submit_ok: bool = True,
    subject: str | None = None,
    owner: str = "claude-bot",
) -> dict[str, Any]:
    return {
        "id": change_id,
        "number": number,
        "subject": subject if subject is not None else f"[OP-{number}] payload",
        "branch": "develop",
        "owner": {"username": owner, "name": owner},
        "lastUpdated": 1_700_000_000 + number,
        "currentPatchSet": {
            "number": 1,
            "revision": f"rev_{number}",
            "parents": [{"revision": parent_sha}],
            "approvals": [{"type": "Code-Review", "value": "2"}],
        },
        "submitRecords": [{"status": "OK" if submit_ok else "NOT_READY"}],
    }


class _GerritScenario:
    """Stateful fake Gerrit: handles query, rebase, submit, review.

    State machine:

      - ``develop_tip`` advances each time submit succeeds.
      - Each change tracks its current parent SHA; rebase updates it to
        the develop tip at the time of the call.
      - Submit removes the change from the open set (status:open query
        no longer returns it).

    The fake is intentionally permissive on URL parsing — production
    code is what's under test, the fake just routes by substring match.
    """

    def __init__(
        self,
        *,
        changes: list[dict[str, Any]],
        develop_tip: str,
        # If a change appears in ``conflict_on_rebase``, attempts to
        # rebase it return HTTP 409 instead of advancing its parent.
        conflict_on_rebase: set[str] | None = None,
    ) -> None:
        self.changes = {str(c["number"]): json.loads(json.dumps(c)) for c in changes}
        self.develop_tip = develop_tip
        self.conflict_on_rebase = conflict_on_rebase or set()
        self.merged_in_order: list[str] = []
        self.rebase_calls: list[dict[str, Any]] = []
        self.submit_calls: list[dict[str, Any]] = []
        self.review_calls: list[dict[str, Any]] = []
        # cycle counter so each merge yields a distinct develop tip.
        self._merge_counter = 0

    # gerrit query stub (run_command)
    def run_command(
        self, cmd: list[str], **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        # Concatenate args so substring matches work regardless of how
        # the production code splits the query across argv positions.
        query = " ".join(cmd)
        if "branch:develop status:merged" in query:
            # Develop tip lookup returns a single record carrying the
            # current tip in currentPatchSet.revision.
            tip_obj = {
                "id": "Idevelop",
                "currentPatchSet": {"revision": self.develop_tip},
            }
            stdout = json.dumps(tip_obj) + "\n"
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        # Submit-Ready=+1 candidate query.
        items: list[dict[str, Any]] = []
        if "label:Submit-Ready=+1" in query:
            for c in self.changes.values():
                if c.get("status") == "MERGED":
                    continue
                if c.get("submit_ready_minus_one"):
                    continue
                items.append(c)
        stdout = "\n".join(json.dumps(o) for o in items) + ("\n" if items else "")
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    # urlopen stub
    def urlopen(self, req: Any, timeout: int = 30) -> Any:
        url = req.full_url
        method = req.get_method()
        body = json.loads(req.data.decode()) if req.data else {}

        # /a/changes/<id>/revisions/current/rebase
        m = re.search(r"/a/changes/([^/]+)/revisions/current/rebase", url)
        if m:
            change_id = m.group(1)
            change = self._change_by_id(change_id)
            self.rebase_calls.append(
                {"change_id": change_id, "body": body},
            )
            if change is None:
                raise _FakeHTTPError_to_urllib(404, b"not found")
            if str(change.get("number")) in self.conflict_on_rebase:
                raise _FakeHTTPError_to_urllib(
                    409, b"merge conflict in: shared.py",
                )
            new_parent = body.get("base") or self.develop_tip
            change["currentPatchSet"]["parents"] = [{"revision": new_parent}]
            change["currentPatchSet"]["number"] = (
                int(change["currentPatchSet"].get("number") or 1) + 1
            )
            new_rev = f"rev_{change['number']}_p{change['currentPatchSet']['number']}"
            change["currentPatchSet"]["revision"] = new_rev
            return _FakeResp(
                200,
                json.dumps({"current_revision": new_rev}).encode(),
            )

        # /a/changes/<id>/submit
        m = re.search(r"/a/changes/([^/]+)/submit$", url)
        if m:
            change_id = m.group(1)
            change = self._change_by_id(change_id)
            self.submit_calls.append({"change_id": change_id})
            if change is None:
                raise _FakeHTTPError_to_urllib(404, b"not found")
            change["status"] = "MERGED"
            self._merge_counter += 1
            self.develop_tip = f"merged_v{self._merge_counter}"
            self.merged_in_order.append(str(change["number"]))
            return _FakeResp(200, b")]}'\n{}")

        # /a/changes/<id>/revisions/current/review (Submit-Ready=-1)
        m = re.search(r"/a/changes/([^/]+)/revisions/current/review", url)
        if m:
            change_id = m.group(1)
            change = self._change_by_id(change_id)
            self.review_calls.append(
                {"change_id": change_id, "body": body},
            )
            if change is not None:
                labels = body.get("labels") or {}
                if labels.get("Submit-Ready") == -1:
                    change["submit_ready_minus_one"] = True
            return _FakeResp(200, b")]}'\n{}")

        raise AssertionError(f"unhandled URL: {method} {url}")

    def _change_by_id(self, change_id: str) -> dict[str, Any] | None:
        for c in self.changes.values():
            if c.get("id") == change_id:
                return c
        return None

    def operator_remarks_ready(self, number: str) -> None:
        """Simulate the operator clearing -1 + voting +1 again."""
        self.changes[number].pop("submit_ready_minus_one", None)


def _FakeHTTPError_to_urllib(status: int, body: bytes):
    """Wrap a fake HTTP error into urllib.error.HTTPError shape."""
    import urllib.error
    err = urllib.error.HTTPError(
        "http://fake", status, "fake", {}, _FakeHTTPError(status, body),
    )
    return err


def _make_worker(
    scenario: _GerritScenario,
    *,
    rate_limit_seconds: float = 0.0,
    log_calls: list[tuple[str, str, dict[str, Any]]] | None = None,
    notify_calls: list[tuple[str, str]] | None = None,
    lock_dir: Path | None = None,
) -> sqw.SubmitQueueWorker:
    log_calls = log_calls if log_calls is not None else []
    notify_calls = notify_calls if notify_calls is not None else []
    return sqw.SubmitQueueWorker(
        project="omnisight/p",
        ssh_cmd_builder=_ssh_cmd_builder,
        ssh_env_builder=_ssh_env_builder,
        worker_username="claude-bot",
        worker_password_loader=lambda: "worker-pw",
        run_command=scenario.run_command,
        urlopen=scenario.urlopen,
        owner_password_loader=lambda u: "owner-pw",
        notify_jira=lambda key, msg: notify_calls.append((key, msg)),
        rate_limit_seconds=rate_limit_seconds,
        poll_interval_seconds=0.0,
        sleep=lambda s: None,
        clock=lambda: 0.0,
        lock_dir=lock_dir or Path("/tmp/_op751_test_locks_unused"),
        log=lambda level, label, **kw: log_calls.append((level, label, kw)),
    )


# ── AC#1 — Submit-Ready label defined in project.config ──────────────


def test_project_config_defines_submit_ready_label() -> None:
    cfg = (REPO_ROOT / ".gerrit" / "project.config").read_text()
    assert '[label "Submit-Ready"]' in cfg
    assert "value = +1" in cfg
    assert "value = -1" in cfg
    # Access entries: both groups can vote -1..+1 on the new label.
    assert "label-Submit-Ready = -1..+1 group non-ai-reviewer" in cfg
    assert "label-Submit-Ready = -1..+1 group ai-reviewer-bots" in cfg


# ── AC#3 — per-change lock prevents double-process ───────────────────


def test_change_lock_blocks_concurrent_acquisition(tmp_path: Path) -> None:
    first = sqw.ChangeLock(tmp_path, "100")
    with first:
        with pytest.raises(sqw.ChangeLockBusy):
            with sqw.ChangeLock(tmp_path, "100"):
                pass
    # Lock released → re-acquire OK.
    with sqw.ChangeLock(tmp_path, "100"):
        pass


def test_process_change_skips_when_lock_held(tmp_path: Path) -> None:
    change = _make_change(number=42, change_id="I42", parent_sha="old")
    scenario = _GerritScenario(changes=[change], develop_tip="tip")
    worker = _make_worker(scenario, lock_dir=tmp_path)

    # Hold the lock externally — process_change must see it busy.
    with sqw.ChangeLock(tmp_path, "42"):
        result = worker.process_change(change)
    assert result.skipped is True
    assert result.skip_reason == "locked"
    # No REST traffic must have escaped while the lock was held.
    assert scenario.rebase_calls == []
    assert scenario.submit_calls == []


# ── AC#4 — synthetic 5-PS same-file burst → ordered, automatic ───────


def test_synthetic_five_ps_burst_all_merge_in_order_no_manual_rebase(
    tmp_path: Path,
) -> None:
    """5 PSes touching the same file, all marked Submit-Ready=+1
    simultaneously. They must all eventually merge in order without
    any human rebase action — that is the whole point of the queue.
    """
    base_sha = "develop_v0"
    changes = [
        _make_change(number=n, change_id=f"I{n}", parent_sha=base_sha)
        for n in (101, 102, 103, 104, 105)
    ]
    scenario = _GerritScenario(
        changes=changes, develop_tip=base_sha,
    )
    worker = _make_worker(scenario, lock_dir=tmp_path)

    # First poll picks all five; submit serialises them.
    results = worker.poll_once()

    # All 5 merged.
    assert [r.merged for r in results] == [True] * 5
    # Order is the operator's mark order (lastUpdated/number ascending).
    assert scenario.merged_in_order == ["101", "102", "103", "104", "105"]

    # No rebase needed for the FIRST merge (its parent already == tip)
    # but the next four MUST have been rebased exactly once each
    # because the develop tip advanced under them.
    rebased_change_ids = [c["change_id"] for c in scenario.rebase_calls]
    assert rebased_change_ids.count("I101") == 0  # already on tip
    assert rebased_change_ids.count("I102") == 1
    assert rebased_change_ids.count("I103") == 1
    assert rebased_change_ids.count("I104") == 1
    assert rebased_change_ids.count("I105") == 1
    # No retries / no double-rebase per change.
    assert len(scenario.rebase_calls) == 4

    # Each rebase used the most recent develop tip as base.
    expected_bases = ["merged_v1", "merged_v2", "merged_v3", "merged_v4"]
    actual_bases = [c["body"].get("base") for c in scenario.rebase_calls]
    assert actual_bases == expected_bases

    # Submit was called once per change.
    assert [c["change_id"] for c in scenario.submit_calls] == [
        "I101", "I102", "I103", "I104", "I105",
    ]
    # No -1 review calls — every change merged cleanly.
    assert scenario.review_calls == []


# ── AC#5 — rebase conflict path ──────────────────────────────────────


def test_rebase_conflict_votes_minus_one_with_comment(
    tmp_path: Path,
) -> None:
    """A change whose rebase 409s must NOT submit; instead the worker
    votes Submit-Ready=-1 with a human-readable comment, and JIRA gets
    the same explanation so the operator sees the failure on the ticket.
    """
    change = _make_change(
        number=200, change_id="I200", parent_sha="stale_parent",
        subject="[OP-200] hot file edit",
    )
    scenario = _GerritScenario(
        changes=[change], develop_tip="develop_tip_sha",
        conflict_on_rebase={"200"},
    )
    notify_calls: list[tuple[str, str]] = []
    worker = _make_worker(
        scenario, lock_dir=tmp_path, notify_calls=notify_calls,
    )

    [result] = worker.poll_once()

    assert result.rejected is True
    assert result.reject_reason == "rebase_conflict"
    assert "shared.py" in result.rebase_files

    # Submit must NOT have been called.
    assert scenario.submit_calls == []

    # Exactly one Submit-Ready=-1 review with the conflict comment.
    assert len(scenario.review_calls) == 1
    review = scenario.review_calls[0]
    assert review["body"]["labels"]["Submit-Ready"] == -1
    msg = review["body"]["message"]
    assert "Rebase conflict" in msg
    assert "shared.py" in msg
    assert "re-mark Submit-Ready=+1" in msg

    # JIRA notified on the same OP key.
    assert notify_calls == [
        ("OP-200",
         "Submit queue rejected the change: " + msg),
    ]


def test_after_minus_one_operator_can_retry_with_plus_one(
    tmp_path: Path,
) -> None:
    """After a -1 vote, the change drops out of the queue's pickup
    query. Once the operator clears the -1 (re-marks +1) the worker
    must re-evaluate the change on the next poll.
    """
    change = _make_change(
        number=300, change_id="I300", parent_sha="stale",
    )
    scenario = _GerritScenario(
        changes=[change], develop_tip="tip_v0",
        conflict_on_rebase={"300"},
    )
    worker = _make_worker(scenario, lock_dir=tmp_path)

    [result] = worker.poll_once()
    assert result.rejected is True

    # Second poll: -1 is sticky, so the change is invisible.
    second = worker.poll_once()
    assert second == []

    # Operator fixes manually + re-marks Submit-Ready=+1.
    scenario.operator_remarks_ready("300")
    # And clears the rebase conflict by acknowledging the resolution.
    scenario.conflict_on_rebase.clear()

    third = worker.poll_once()
    assert len(third) == 1
    assert third[0].merged is True


# ── Auxiliary behaviour ──────────────────────────────────────────────


def test_skips_change_that_is_not_submittable(tmp_path: Path) -> None:
    """Submit-Ready=+1 alone is not enough — Gerrit's submit-rules
    (Human-Plus-2 / No-Veto / etc) must still pass. The queue defers
    to ``submitRecords`` rather than overriding policy.
    """
    change = _make_change(
        number=400, change_id="I400", parent_sha="stale",
        submit_ok=False,
    )
    scenario = _GerritScenario(changes=[change], develop_tip="tip_v0")
    worker = _make_worker(scenario, lock_dir=tmp_path)

    [result] = worker.poll_once()
    assert result.skipped is True
    assert result.skip_reason == "not_submittable"
    assert scenario.rebase_calls == []
    assert scenario.submit_calls == []
    assert scenario.review_calls == []


def test_kill_switch_env_disables_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(sqw.WORKER_DISABLE_ENV, "1")
    change = _make_change(number=500, change_id="I500", parent_sha="x")
    scenario = _GerritScenario(changes=[change], develop_tip="tip")
    worker = _make_worker(scenario, lock_dir=tmp_path)
    assert worker.poll_once() == []
    assert scenario.submit_calls == []


def test_rate_limit_sleeps_between_merges(tmp_path: Path) -> None:
    """The rate-limit must pause between successive merges so the
    auto-rebase sweeper / notifier / CI have time to drain.
    """
    base_sha = "develop_v0"
    changes = [
        _make_change(number=n, change_id=f"I{n}", parent_sha=base_sha)
        for n in (700, 701, 702)
    ]
    scenario = _GerritScenario(changes=changes, develop_tip=base_sha)

    sleeps: list[float] = []
    now = [0.0]

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    def fake_clock() -> float:
        return now[0]

    worker = sqw.SubmitQueueWorker(
        project="omnisight/p",
        ssh_cmd_builder=_ssh_cmd_builder,
        ssh_env_builder=_ssh_env_builder,
        worker_username="claude-bot",
        worker_password_loader=lambda: "worker-pw",
        run_command=scenario.run_command,
        urlopen=scenario.urlopen,
        owner_password_loader=lambda u: "owner-pw",
        notify_jira=None,
        rate_limit_seconds=30.0,
        poll_interval_seconds=0.0,
        sleep=fake_sleep,
        clock=fake_clock,
        lock_dir=tmp_path,
        log=lambda *a, **k: None,
    )
    worker.poll_once()

    # First candidate: no prior merge, so no rate-limit sleep.
    # Subsequent two: each must pause by the configured 30s window.
    rate_limit_sleeps = [s for s in sleeps if s == 30.0]
    assert len(rate_limit_sleeps) == 2


def test_submit_http_error_records_rejection(tmp_path: Path) -> None:
    change = _make_change(number=900, change_id="I900", parent_sha="tip")

    class _RejectingScenario(_GerritScenario):
        def urlopen(self, req: Any, timeout: int = 30) -> Any:
            url = req.full_url
            if "/submit" in url:
                self.submit_calls.append({"change_id": "I900"})
                raise _FakeHTTPError_to_urllib(409, b"merge_failed")
            return super().urlopen(req, timeout=timeout)

    scenario = _RejectingScenario(changes=[change], develop_tip="tip")
    worker = _make_worker(scenario, lock_dir=tmp_path)
    [result] = worker.poll_once()
    assert result.rejected is True
    assert result.reject_reason == "submit_http_error"
    assert len(scenario.review_calls) == 1
    assert scenario.review_calls[0]["body"]["labels"]["Submit-Ready"] == -1
