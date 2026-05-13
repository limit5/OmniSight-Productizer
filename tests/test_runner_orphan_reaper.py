"""Unit + integration tests for the C1 orphan reaper (OP-1059).

Covers the classification logic + JIRA-side cleanup pipeline of
:mod:`backend.agents.runner_orphan_reaper`. The integration case
simulates the design-doc §C1 dead-runner scenario: a child subprocess
writes a heartbeat file and is then killed ``SIGKILL``; the reaper
runs one cycle against an in-memory JIRA stub and is asserted to land
the ticket back in a clean To Do state.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pytest

from backend.agents import jira_dispatch as jd
from backend.agents import runner_orphan_reaper as reaper


# ── In-memory JIRA stub ───────────────────────────────────────────────


class FakeJira:
    """Minimal stand-in mirroring the surface ``reaper`` touches.

    Models labels as a set (set-union ``add`` / set-difference
    ``remove``), assignee as a single-valued last-writer-wins field,
    status as a string. Records every transition so tests can assert
    the four-step reap sequence fired in order.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.issues: dict[str, dict] = {}
        self.comments: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, str]] = []
        self.search_responses: list[dict] = []

    def add_issue(self, key: str, *, status: str = "In Progress",
                  labels: tuple[str, ...] = (),
                  assignee: str | None = "bot-acc") -> None:
        self.issues[key] = {
            "status": status, "labels": set(labels), "assignee": assignee,
        }

    def request(self, client: jd.DispatchClient, method: str, path: str,
                body: dict | None = None) -> dict:
        with self._lock:
            if method == "POST" and path == "/search/jql":
                issues = []
                for k, v in self.issues.items():
                    if v["status"] not in jd.IN_PROGRESS_STATUS_NAMES:
                        continue
                    issues.append({
                        "key": k,
                        "fields": {
                            "status": {"name": v["status"]},
                            "labels": sorted(v["labels"]),
                            "assignee": (
                                {"accountId": v["assignee"]}
                                if v["assignee"] else None
                            ),
                        },
                    })
                return {"issues": issues}

            if method == "GET" and path.startswith("/issue/"):
                key = path.split("/issue/", 1)[1].split("?")[0]
                v = self.issues.get(key) or {}
                return {
                    "fields": {
                        "status": {"name": v.get("status", "?")},
                        "labels": sorted(v.get("labels") or set()),
                        "assignee": (
                            {"accountId": v.get("assignee")}
                            if v.get("assignee") else None
                        ),
                    },
                }

            if method == "PUT" and path.startswith("/issue/"):
                key = path.split("/issue/", 1)[1]
                v = self.issues.setdefault(key, {"status": "?", "labels": set(), "assignee": None})
                fields = (body or {}).get("fields") or {}
                if "assignee" in fields:
                    a = fields["assignee"]
                    v["assignee"] = (a or {}).get("accountId") if a else None
                for op in ((body or {}).get("update") or {}).get("labels", []):
                    if "add" in op:
                        v["labels"].add(op["add"])
                    if "remove" in op:
                        v["labels"].discard(op["remove"])
                return {}

            if method == "POST" and path.endswith("/comment"):
                key = path.split("/issue/", 1)[1].split("/comment", 1)[0]
                self.comments.append((key, json.dumps(body)))
                return {}

            if method == "POST" and path.endswith("/transitions"):
                key = path.split("/issue/", 1)[1].split("/transitions", 1)[0]
                tid = (((body or {}).get("transition") or {}).get("id")) or ""
                self.transitions.append((key, tid))
                if tid == jd.TRANSITION_IDS["back_to_todo"]:
                    self.issues[key]["status"] = "To Do"
                return {}

            return {}


def _client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-claude",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="bot-acc",
        bot_email="bot@example.invalid",
    )


@pytest.fixture
def fake_jira(monkeypatch):
    fake = FakeJira()
    monkeypatch.setattr(jd, "_request", fake.request)
    monkeypatch.setattr(
        jd, "_request_idempotent",
        lambda client, method, path, body, idem_key: fake.request(client, method, path, body),
    )
    monkeypatch.setattr(
        reaper, "_request",
        lambda client, method, path, body=None: fake.request(client, method, path, body),
    )
    monkeypatch.setattr(
        reaper, "_request_idempotent",
        lambda client, method, path, body, idem_key: fake.request(client, method, path, body),
    )
    # Silence the operator_notifier — its real implementation builds
    # channels from env on first call. Reaper tests should not exercise
    # the notifier itself; that suite lives in test_operator_notifier.py.
    notify_calls: list[tuple] = []
    monkeypatch.setattr(
        reaper, "notify",
        lambda severity, code, message="", context=None, **kw: notify_calls.append(
            (severity, code, message, context or {})
        ),
    )
    fake.notify_calls = notify_calls  # type: ignore[attr-defined]
    return fake


# ── Helpers ───────────────────────────────────────────────────────────


def _make_token(epoch_us: int, uid: str = "abcdef01") -> str:
    """Build a fencing token at a deterministic age."""
    return f"{epoch_us:016d}-{uid}"


# ── Classification (unit) ─────────────────────────────────────────────


def test_classify_skips_ticket_without_claim_labels() -> None:
    issue = {"key": "OP-1", "fields": {"labels": ["tier:M", "area:backend"]}}
    now_us = 1_700_000_000_000_000
    assert reaper.classify_in_progress_issue(issue, now_us=now_us, ttl_s=3600) is None


def test_classify_skips_live_claim_within_ttl() -> None:
    now_us = 1_700_000_000_000_000
    young = _make_token(now_us - 60 * 1_000_000)  # 60s old, well within 1h TTL
    issue = {"key": "OP-2", "fields": {"labels": [f"claim:default:{young}"]}}
    assert reaper.classify_in_progress_issue(issue, now_us=now_us, ttl_s=3600) is None


def test_classify_returns_candidate_when_token_exceeds_ttl() -> None:
    now_us = 1_700_000_000_000_000
    old_epoch = now_us - 2 * 3600 * 1_000_000  # 2h old → past 1h TTL
    token = _make_token(old_epoch, "deadbeef")
    issue = {"key": "OP-3", "fields": {"labels": [f"claim:claude-1:{token}", "tier:M"]}}
    cand = reaper.classify_in_progress_issue(issue, now_us=now_us, ttl_s=3600)
    assert cand is not None
    assert cand.ticket_key == "OP-3"
    assert cand.instance_id == "claude-1"
    assert cand.token == token
    assert cand.epoch_us == old_epoch
    assert cand.age_seconds == pytest.approx(7200, abs=1)


def test_classify_picks_oldest_token_when_multiple_claims_stacked() -> None:
    now_us = 1_700_000_000_000_000
    older = _make_token(now_us - 4 * 3600 * 1_000_000, "11111111")
    newer = _make_token(now_us - 2 * 3600 * 1_000_000, "22222222")
    issue = {
        "key": "OP-4",
        "fields": {"labels": [f"claim:default:{newer}", f"claim:default:{older}"]},
    }
    cand = reaper.classify_in_progress_issue(issue, now_us=now_us, ttl_s=3600)
    assert cand is not None
    assert cand.token == older  # oldest fenced claim drives the heartbeat probe


def test_classify_ignores_pre_audit_24_bare_claim_label() -> None:
    now_us = 1_700_000_000_000_000
    issue = {"key": "OP-5", "fields": {"labels": ["claim:default", "tier:M"]}}
    # Bare label has no token → no age signal → defer to next claimer's
    # stale-sweep, classify returns None.
    assert reaper.classify_in_progress_issue(issue, now_us=now_us, ttl_s=3600) is None


# ── Heartbeat probe (unit) ────────────────────────────────────────────


def test_heartbeat_absent_means_orphan(tmp_path: Path) -> None:
    hb = reaper.probe_heartbeat("claude-1", 1_700_000_000_000_000, "OP-9", root=tmp_path)
    assert hb.alive is False
    assert hb.reason == "heartbeat-file-absent"


def test_heartbeat_with_live_pid_marks_alive(tmp_path: Path) -> None:
    epoch_us = 1_700_000_000_000_000
    hb_dir = tmp_path / "default" / f"{epoch_us}-OP-10"
    hb_dir.mkdir(parents=True)
    (hb_dir / "heartbeat").write_text(json.dumps({"pid": os.getpid()}))
    hb = reaper.probe_heartbeat("default", epoch_us, "OP-10", root=tmp_path)
    assert hb.alive is True
    assert hb.pid == os.getpid()


def test_heartbeat_with_dead_pid_marks_orphan(tmp_path: Path) -> None:
    epoch_us = 1_700_000_000_000_000
    hb_dir = tmp_path / "default" / f"{epoch_us}-OP-11"
    hb_dir.mkdir(parents=True)
    # Spawn + reap a short-lived child so its PID is guaranteed gone by
    # the time we probe. subprocess.run does not expose the child's PID,
    # so use Popen + wait.
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])  # noqa: S603
    dead_pid = p.pid
    p.wait()
    (hb_dir / "heartbeat").write_text(json.dumps({"pid": dead_pid}))
    hb = reaper.probe_heartbeat("default", epoch_us, "OP-11", root=tmp_path)
    assert hb.alive is False
    assert hb.reason == "heartbeat-pid-dead"


def test_heartbeat_malformed_json_marks_orphan(tmp_path: Path) -> None:
    epoch_us = 1_700_000_000_000_000
    hb_dir = tmp_path / "default" / f"{epoch_us}-OP-12"
    hb_dir.mkdir(parents=True)
    (hb_dir / "heartbeat").write_text("not json {")
    hb = reaper.probe_heartbeat("default", epoch_us, "OP-12", root=tmp_path)
    assert hb.alive is False
    assert hb.reason == "heartbeat-malformed-json"


def test_heartbeat_missing_pid_marks_orphan(tmp_path: Path) -> None:
    epoch_us = 1_700_000_000_000_000
    hb_dir = tmp_path / "default" / f"{epoch_us}-OP-13"
    hb_dir.mkdir(parents=True)
    (hb_dir / "heartbeat").write_text(json.dumps({"ticket": "OP-13"}))
    hb = reaper.probe_heartbeat("default", epoch_us, "OP-13", root=tmp_path)
    assert hb.alive is False
    assert hb.reason == "heartbeat-missing-pid"


# ── Kill-switch ───────────────────────────────────────────────────────


def test_kill_switch_disabled_short_circuits_main(monkeypatch, fake_jira) -> None:
    monkeypatch.setenv("OMNISIGHT_RUNNER_REAPER_ENABLED", "0")
    rc = reaper.main(["--agent-class", "subscription-claude"])
    assert rc == 0
    # No JIRA writes occurred because main bailed before make_client.
    assert fake_jira.comments == []
    assert fake_jira.transitions == []


@pytest.mark.parametrize("val,expected", [
    ("", True),
    ("1", True),
    ("yes", True),
    ("0", False),
    ("false", False),
    ("FALSE", False),
    ("no", False),
    ("off", False),
])
def test_reaper_enabled_parses_truthy_strings(val, expected) -> None:
    assert reaper._reaper_enabled({"OMNISIGHT_RUNNER_REAPER_ENABLED": val}) is expected


def test_read_claim_ttl_falls_back_to_default_on_garbage() -> None:
    assert reaper._read_claim_ttl_s({"OMNISIGHT_RUNNER_CLAIM_TTL_SEC": "abc"}) == 3600
    assert reaper._read_claim_ttl_s({"OMNISIGHT_RUNNER_CLAIM_TTL_SEC": "-5"}) == 3600
    assert reaper._read_claim_ttl_s({"OMNISIGHT_RUNNER_CLAIM_TTL_SEC": "120"}) == 120
    assert reaper._read_claim_ttl_s({}) == 3600


# ── reap_one (unit, action ordering) ──────────────────────────────────


def test_reap_one_posts_comment_strips_claim_clears_assignee_and_transitions(fake_jira) -> None:
    now_us = 1_700_000_000_000_000
    old_epoch = now_us - 2 * 3600 * 1_000_000
    token = _make_token(old_epoch, "11112222")
    fake_jira.add_issue("OP-20", labels=(f"claim:claude-1:{token}", "tier:M"),
                        assignee="bot-acc")

    cand = reaper.OrphanCandidate(
        ticket_key="OP-20", instance_id="claude-1", token=token,
        epoch_us=old_epoch, age_seconds=7200,
    )
    hb = reaper.HeartbeatStatus(alive=False, reason="heartbeat-file-absent")

    reaper.reap_one(_client(), cand, hb)

    issue = fake_jira.issues["OP-20"]
    # claim label stripped, tier:M preserved.
    assert not any(l.startswith("claim:") for l in issue["labels"]), issue["labels"]
    assert "tier:M" in issue["labels"]
    assert issue["assignee"] is None
    assert issue["status"] == "To Do"
    # The reap comment landed.
    assert any("[reaper-detected-orphan]" in body for _, body in fake_jira.comments)
    # Operator alert fired with the spec's exact code and severity.
    sev, code, message, context = fake_jira.notify_calls[0]
    assert code == "runner_orphan_reaped"
    assert sev.value == "WARN"
    assert context["ticket"] == "OP-20"
    assert context["claim_age_sec"] == 7200


# ── run_cycle (integration over the FakeJira fixture) ─────────────────


def test_run_cycle_reaps_only_ttl_exceeded_with_dead_heartbeat(fake_jira, tmp_path) -> None:
    now_us = 1_700_000_000_000_000

    # Ticket A: claim aged 2h, no heartbeat file → orphan.
    old_epoch_a = now_us - 2 * 3600 * 1_000_000
    token_a = _make_token(old_epoch_a, "aaaaaaaa")
    fake_jira.add_issue("OP-100", labels=(f"claim:claude-1:{token_a}",))

    # Ticket B: claim aged 5m → live, must not be reaped.
    young_epoch = now_us - 5 * 60 * 1_000_000
    token_b = _make_token(young_epoch, "bbbbbbbb")
    fake_jira.add_issue("OP-101", labels=(f"claim:claude-2:{token_b}",))

    # Ticket C: claim aged 2h but heartbeat says our own PID is alive → skip.
    old_epoch_c = now_us - 2 * 3600 * 1_000_000
    token_c = _make_token(old_epoch_c, "cccccccc")
    fake_jira.add_issue("OP-102", labels=(f"claim:claude-3:{token_c}",))
    hb_dir_c = tmp_path / "claude-3" / f"{old_epoch_c}-OP-102"
    hb_dir_c.mkdir(parents=True)
    (hb_dir_c / "heartbeat").write_text(json.dumps({"pid": os.getpid()}))

    # Ticket D: not In Progress → ignored by JQL filter.
    fake_jira.add_issue("OP-103", status="To Do",
                        labels=(f"claim:claude-4:{token_a}",))

    result = reaper.run_cycle(_client(), ttl_s=3600, now_us=now_us, heartbeat_root=tmp_path)

    assert result.reaped == 1
    assert result.scanned == 3  # A + B + C; D is filtered by JQL status clause
    # OP-100 returned to clean state.
    assert fake_jira.issues["OP-100"]["status"] == "To Do"
    assert fake_jira.issues["OP-100"]["assignee"] is None
    assert not any(l.startswith("claim:") for l in fake_jira.issues["OP-100"]["labels"])
    # OP-101 and OP-102 untouched.
    assert fake_jira.issues["OP-101"]["status"] == "In Progress"
    assert fake_jira.issues["OP-102"]["status"] == "In Progress"


# ── Integration: synthetic dead-runner scenario ───────────────────────


def test_dead_runner_returns_ticket_to_clean_todo_within_one_cycle(
    fake_jira, tmp_path
) -> None:
    """The SP-B-X-002 §C1 "kill -9 mid-pickup" scenario, end-to-end.

    Spawns a child that writes its own heartbeat then exits *without
    cleaning up* (SIGKILL equivalent — the runner never ran its release
    path). The reaper runs one cycle and is asserted to:

    1. Detect the orphan,
    2. Strip the claim label,
    3. Clear the assignee,
    4. Transition the ticket back to To Do,
    5. Emit the WARN operator alert.
    """
    instance_id = "claude-2"
    ticket = "OP-700"
    # Anchor the claim's epoch in the past so it is unambiguously
    # TTL-exceeded against the cycle's ``now_us``.
    now_us = time.time_ns() // 1000
    old_epoch = now_us - 2 * 3600 * 1_000_000
    token = _make_token(old_epoch, "deadbeef")
    fake_jira.add_issue(ticket, labels=(f"claim:{instance_id}:{token}",))

    # Child writes its PID into the heartbeat, then exits — SIGKILL would
    # leave the file in place; a normal exit leaves it in place too, since
    # the C1 invariant is "runner died before cleanup", not "runner
    # cleaned up". The reaper's job is to handle either case via the
    # PID-liveness probe.
    hb_dir = tmp_path / instance_id / f"{old_epoch}-{ticket}"
    hb_dir.mkdir(parents=True)
    hb_path = hb_dir / "heartbeat"

    script = textwrap.dedent(f"""
        import json, os, sys
        from pathlib import Path
        Path({str(hb_path)!r}).write_text(json.dumps({{"pid": os.getpid()}}))
        sys.exit(0)
    """)
    child = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], check=True, capture_output=True,
    )
    assert child.returncode == 0
    assert hb_path.exists()
    # The child's PID is now gone — its parent shell has reaped it.
    # The reaper must treat the heartbeat as dead.

    result = reaper.run_cycle(_client(), ttl_s=3600, now_us=now_us,
                              heartbeat_root=tmp_path)

    assert result.reaped == 1
    issue = fake_jira.issues[ticket]
    assert issue["status"] == "To Do"
    assert issue["assignee"] is None
    assert not any(l.startswith("claim:") for l in issue["labels"])
    assert any("[reaper-detected-orphan]" in body for _, body in fake_jira.comments)
    assert fake_jira.notify_calls
    sev, code, _, ctx = fake_jira.notify_calls[0]
    assert sev.value == "WARN"
    assert code == "runner_orphan_reaped"
    assert ctx["ticket"] == ticket


# ── Heartbeat path construction ───────────────────────────────────────


def test_heartbeat_path_matches_design_doc_layout(tmp_path: Path) -> None:
    # Documents the exact layout the runner's pickup script writes to.
    p = reaper.heartbeat_path("claude-2", 1_700_000_000_000_000, "OP-555",
                              root=tmp_path)
    assert p == tmp_path / "claude-2" / "1700000000000000-OP-555" / "heartbeat"
