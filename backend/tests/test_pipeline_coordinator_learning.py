"""Learning-loop tests (OP-1009 / AUDIT-29f-11) — ADR-0021 §10.

Coverage map against the acceptance criteria:

  Code AC
    daily + weekly handlers          → test_run_daily_*, test_run_weekly_*
    decision write-back → Cognee node→ test_write_back_*, test_node_renders_edges
      with edges (tickets/lessons/mode)
    24h outcome-check                → test_outcome_check_*
    weekly retro groups by           → test_weekly_groups_by_profile_and_action
      (profile_class, action_type)
    Tier-1 proposal → coord_rule_*   → test_graduation_writes_proposal_file
    operator @-mention               → test_graduation_mentions_operator
    synthetic decisions → valid Python→ test_graduation_produces_valid_python

  Integration AC
    every Tier-2 decision → write-back→ test_daemon_tier2_writes_back_in_tick
      within 60s (in-tick)
    outcome via JIRA state change    → test_jira_observer_*
    proposal passes syntax check     → test_graduation_produces_valid_python

  Exercised AC
    >=5 decisions written back       → test_exercised_five_writebacks
    >=1 outcome-check completed       → test_outcome_check_records_verdict
    >=1 graduation proposal generated → test_graduation_writes_proposal_file

Stateless-across-restarts (§3.2)     → test_from_decision_log_rebuilds_schedule
"""

from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from backend.agents import learning_loop as ll
from backend.agents.learning_loop import (
    DecisionNode,
    DecisionRecord,
    JiraStateChangeObserver,
    LearningLoop,
    LearningLoopConfig,
    OutcomeVerdict,
    RetroGroup,
    parse_decision_record,
    render_rule_proposal,
)
from backend.agents.pipeline_coordinator import DecisionLog

NOW = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


# ── helpers ──────────────────────────────────────────────────────────────


class FakeWriteBack:
    """Records nodes instead of touching Cognee."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.nodes: list[DecisionNode] = []

    def write_node(self, node: DecisionNode) -> bool:
        self.nodes.append(node)
        return self.ok


class FakeObserver:
    def __init__(self, verdict: OutcomeVerdict) -> None:
        self.verdict = verdict
        self.seen: list[str] = []

    def observe(self, record: DecisionRecord) -> OutcomeVerdict:
        self.seen.append(record.decision_id)
        return self.verdict


def _tick_record(
    *,
    decision_id: str,
    ts: datetime,
    tier: int = 2,
    mode: str = "ExecutionMode",
    action_kind: str = "relabel",
    target: str = "OP-100",
    params: dict | None = None,
    profile: dict | None = None,
    learning: str = "graduate me",
    lessons: list[str] | None = None,
) -> dict:
    rec = {
        "ts": ts.isoformat(),
        "event": "decision_tick",
        "engine_version": "2.0.0-hybrid",
        "mode": mode,
        "decision_id": decision_id,
        "tier": tier,
        "reason": "tier2_llm_consulted",
        "actions": [
            {"kind": action_kind, "target": target, "params": params or {}, "dry_run": True}
        ],
        "dry_run": True,
    }
    if profile is not None:
        rec["situation_profile"] = profile
    if tier == 2:
        rec["llm_consultation"] = {
            "confidence": "high",
            "decision_rationale": "because",
            "learning": learning,
            "lessons_cited": lessons or ["L-OP-922.md"],
            "cost_usd": 0.1,
        }
    return rec


def _loop(tmp_path: Path, **kwargs) -> tuple[LearningLoop, DecisionLog, LearningLoopConfig]:
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    cfg = kwargs.pop("config", None) or LearningLoopConfig(proposal_dir=tmp_path / "proposals")
    loop = LearningLoop(
        decision_log=log,
        config=cfg,
        clock=lambda: kwargs.pop("now", NOW),
        writeback=kwargs.pop("writeback", FakeWriteBack()),
        observer=kwargs.pop("observer", FakeObserver(OutcomeVerdict(ll.OUTCOME_SUCCESS))),
        **kwargs,
    )
    return loop, log, cfg


def _read_events(log_dir: Path, event: str) -> list[dict]:
    out: list[dict] = []
    for path in sorted(Path(log_dir).glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("event") == event:
                    out.append(rec)
    return out


# ── parsing ────────────────────────────────────────────────────────────────


def test_parse_decision_record_extracts_edges_and_consult() -> None:
    raw = _tick_record(decision_id="d1", ts=NOW, target="OP-5",
                        lessons=["L-OP-870.md"], learning="x")
    rec = parse_decision_record(raw)
    assert rec is not None
    assert rec.is_tier2
    assert rec.tickets == ("OP-5",)
    assert rec.action_types == ("relabel",)
    assert rec.lessons_cited == ("L-OP-870.md",)
    assert rec.learning == "x"
    assert rec.confidence == "high"


def test_parse_skips_non_tick_lines() -> None:
    assert parse_decision_record({"event": "shutdown_began", "ts": NOW.isoformat()}) is None
    assert parse_decision_record({"event": "decision_tick"}) is None  # no ts


def test_profile_class_prefers_profile_then_mode() -> None:
    with_profile = parse_decision_record(
        _tick_record(decision_id="d", ts=NOW,
                     profile={"urgency": "high", "risk": "low",
                              "novelty": "low", "reversibility": "high"})
    )
    assert with_profile is not None
    assert with_profile.profile_class == "u=high|r=low|n=low|rev=high"
    no_profile = parse_decision_record(_tick_record(decision_id="d", ts=NOW, mode="RescueMode"))
    assert no_profile is not None
    assert no_profile.profile_class == "mode=RescueMode"


# ── write-back (§10.1) ──────────────────────────────────────────────────────


def test_node_renders_edges() -> None:
    node = DecisionNode(
        decision_id="d1", ts=NOW, mode="ExecutionMode", rationale="r",
        learning="lesson", confidence="high", tickets=("OP-1", "OP-2"),
        lessons_cited=("L-OP-922.md",), action_types=("relabel",), outcome="success",
    )
    body = node.render()
    assert "touched_ticket -> OP-1" in body
    assert "touched_ticket -> OP-2" in body
    assert "cited_lesson -> L-OP-922.md" in body
    assert "operated_in_mode -> ExecutionMode" in body
    assert "outcome: success" in body
    assert node.metadata()["tickets"] == ["OP-1", "OP-2"]


def test_write_back_creates_node_and_receipt(tmp_path: Path) -> None:
    wb = FakeWriteBack()
    loop, log, _ = _loop(tmp_path, writeback=wb)
    rec = parse_decision_record(_tick_record(decision_id="d1", ts=NOW, target="OP-9"))
    result = loop.write_back_decision(rec)
    assert result.written is True
    assert wb.nodes[0].decision_id == "d1"
    assert wb.nodes[0].tickets == ("OP-9",)
    receipts = _read_events(log.directory, ll.WRITEBACK_EVENT)
    assert receipts[0]["decision_id"] == "d1"
    assert receipts[0]["written"] is True


def test_write_back_skips_non_tier2(tmp_path: Path) -> None:
    wb = FakeWriteBack()
    loop, log, _ = _loop(tmp_path, writeback=wb)
    rec = parse_decision_record(_tick_record(decision_id="t1", ts=NOW, tier=1))
    assert loop.write_back_decision(rec).written is False
    assert wb.nodes == []


def test_write_back_fail_open_on_gateway_error(tmp_path: Path) -> None:
    class Boom:
        def write_node(self, node):
            raise RuntimeError("neo4j down")

    loop, log, _ = _loop(tmp_path, writeback=Boom())
    rec = parse_decision_record(_tick_record(decision_id="d1", ts=NOW))
    assert loop.write_back_decision(rec).written is False  # swallowed
    assert _read_events(log.directory, ll.WRITEBACK_EVENT)[0]["written"] is False


def test_build_default_learning_loop_uses_backend_api_gateway(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    loop = ll.build_default_learning_loop(log)
    assert isinstance(loop._writeback, ll.BackendApiWriteBackGateway)


def test_backend_api_gateway_posts_decision_node() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"written": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = ll.BackendApiWriteBackGateway(
        api_base="http://backend",
        bearer_token="secret",
        client=client,
    )
    rec = parse_decision_record(
        _tick_record(decision_id="d-api", ts=NOW, target="OP-1557")
    )
    assert rec is not None
    node = DecisionNode.from_record(rec)
    try:
        assert gateway.write_node(node) is True
    finally:
        client.close()
    assert requests[0].url == httpx.URL(
        f"http://backend{ll.LEARNING_WRITEBACK_PATH}"
    )
    assert requests[0].headers["authorization"] == "Bearer secret"
    payload = json.loads(requests[0].content)
    assert payload["kind"] == ll.SOURCE_KIND_COORD_DECISION
    assert payload["identifier"] == "d-api"
    assert payload["metadata"]["tickets"] == ["OP-1557"]


def test_backend_api_gateway_unreachable_degrades_once(caplog: pytest.LogCaptureFixture) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="cognee down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = ll.BackendApiWriteBackGateway(api_base="http://backend", client=client)
    rec = parse_decision_record(_tick_record(decision_id="d-down", ts=NOW))
    assert rec is not None
    node = DecisionNode.from_record(rec)
    try:
        with caplog.at_level(logging.INFO, logger="backend.agents.learning_loop"):
            assert gateway.write_node(node) is False
            assert gateway.write_node(node) is False
    finally:
        client.close()
    assert caplog.text.count("[coord-learn] write-back unavailable") == 1


def test_write_back_records_dry_run_receipt_when_backend_api_unreachable(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="cognee down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = ll.BackendApiWriteBackGateway(api_base="http://backend", client=client)
    loop, log, _ = _loop(tmp_path, writeback=gateway)
    rec = parse_decision_record(_tick_record(decision_id="d-dry", ts=NOW))
    assert rec is not None
    try:
        result = loop.write_back_decision(rec)
    finally:
        client.close()
    assert result.written is False
    receipt = _read_events(log.directory, ll.WRITEBACK_EVENT)[0]
    assert receipt["decision_id"] == "d-dry"
    assert receipt["written"] is False
    assert receipt["dry_run"] is True


@pytest.mark.asyncio
async def test_memory_router_ingests_decision_node_via_cognee(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from backend import auth
    from backend.agents import cognee_integration as ci
    from backend.routers import memory as memory_router

    captured = []

    class Report:
        sources_ingested = 1

    class FakeAdapter:
        async def ingest(self, sources):
            captured.extend(sources)
            return Report()

    monkeypatch.setattr(ci.CogneeAdapter, "from_env", classmethod(lambda cls: FakeAdapter()))
    app = FastAPI()
    app.dependency_overrides[auth.require_admin] = lambda: auth.User(
        id="u", email="u@example.com", name="U", role="admin",
    )
    app.include_router(memory_router.router, prefix="/api/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/memory/cognee/decision-nodes",
            json={
                "kind": ll.SOURCE_KIND_COORD_DECISION,
                "identifier": "d-router",
                "content": "decision body",
                "metadata": {"tickets": ["OP-1557"]},
            },
        )
    assert response.status_code == 200
    assert response.json() == {"written": True}
    assert captured[0].kind == ll.SOURCE_KIND_COORD_DECISION
    assert captured[0].identifier == "d-router"


# ── daily handler: catch-up write-back + 24h outcome-check (§10.1) ──────────


def test_run_daily_writes_back_uncovered_tier2(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    # A Tier-2 decision made 2h ago with no write-back receipt.
    log.append(_tick_record(decision_id="d1", ts=NOW - timedelta(hours=2)))
    wb = FakeWriteBack()
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=tmp_path / "p"),
                        clock=lambda: NOW, writeback=wb,
                        observer=FakeObserver(OutcomeVerdict(ll.OUTCOME_UNKNOWN)))
    report = loop.run_daily(NOW)
    assert report.writeback_count == 1
    assert wb.nodes[0].decision_id == "d1"


def test_outcome_check_records_verdict(tmp_path: Path) -> None:
    """Exercised AC: >=1 outcome-check completed 24h after a decision."""
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    log.append(_tick_record(decision_id="d1", ts=NOW - timedelta(hours=25)))
    obs = FakeObserver(OutcomeVerdict(ll.OUTCOME_SUCCESS, "landed"))
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=tmp_path / "p"),
                        clock=lambda: NOW, writeback=FakeWriteBack(), observer=obs)
    report = loop.run_daily(NOW)
    assert ("d1", ll.OUTCOME_SUCCESS) in report.outcomes
    assert obs.seen == ["d1"]
    checks = _read_events(log.directory, ll.OUTCOME_CHECK_EVENT)
    assert checks[0]["outcome"] == ll.OUTCOME_SUCCESS


def test_outcome_check_skips_too_recent(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    log.append(_tick_record(decision_id="d1", ts=NOW - timedelta(hours=1)))
    obs = FakeObserver(OutcomeVerdict(ll.OUTCOME_SUCCESS))
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=tmp_path / "p"),
                        clock=lambda: NOW, writeback=FakeWriteBack(), observer=obs)
    report = loop.run_daily(NOW)
    assert report.outcomes == []
    assert obs.seen == []  # not yet observable (< 24h)


def test_outcome_check_idempotent(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    log.append(_tick_record(decision_id="d1", ts=NOW - timedelta(hours=25)))
    obs = FakeObserver(OutcomeVerdict(ll.OUTCOME_SUCCESS))
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=tmp_path / "p"),
                        clock=lambda: NOW, writeback=FakeWriteBack(), observer=obs)
    loop.run_daily(NOW)
    loop.run_daily(NOW)  # second pass must not re-check
    assert obs.seen == ["d1"]
    assert len(_read_events(log.directory, ll.OUTCOME_CHECK_EVENT)) == 1


# ── weekly retro + graduation (§10.2) ───────────────────────────────────────


def _seed_consistent_class(log: DecisionLog, n: int, *, action="relabel", outcome="success") -> None:
    profile = {"urgency": "high", "risk": "low", "novelty": "low", "reversibility": "high"}
    for i in range(n):
        did = f"d{i}"
        log.append(_tick_record(decision_id=did, ts=NOW - timedelta(days=2),
                                 action_kind=action, target=f"OP-{i}", profile=profile,
                                 learning=f"pattern {i}"))
        log.append({"ts": (NOW - timedelta(days=1)).isoformat(),
                    "event": ll.OUTCOME_CHECK_EVENT, "decision_id": did, "outcome": outcome})


def test_weekly_groups_by_profile_and_action(tmp_path: Path) -> None:
    decisions = [
        parse_decision_record(_tick_record(
            decision_id=f"d{i}", ts=NOW, action_kind=("relabel" if i < 2 else "transition"),
            profile={"urgency": "high", "risk": "low", "novelty": "low", "reversibility": "high"}))
        for i in range(3)
    ]
    groups = LearningLoop._group([d for d in decisions if d])
    keys = {(g.profile_class, g.action_type) for g in groups}
    assert ("u=high|r=low|n=low|rev=high", "relabel") in keys
    assert ("u=high|r=low|n=low|rev=high", "transition") in keys


def test_graduation_writes_proposal_file(tmp_path: Path) -> None:
    """Exercised AC: >=1 rule-graduation proposal generated."""
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    _seed_consistent_class(log, 4)
    mentions: list[tuple] = []
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=tmp_path / "prop"),
                        clock=lambda: NOW, writeback=FakeWriteBack(),
                        observer=FakeObserver(OutcomeVerdict(ll.OUTCOME_SUCCESS)),
                        operator_mention=lambda t, m, u: mentions.append((t, m, u)))
    report = loop.run_weekly(NOW)
    assert report.proposal_count == 1
    path = report.proposal_paths[0]
    assert path.exists()
    assert path.name.endswith(".py")
    assert (tmp_path / "prop" / "__init__.py").exists()
    grad = _read_events(log.directory, ll.GRADUATION_EVENT)
    assert grad[0]["action_type"] == "relabel"


def test_graduation_produces_valid_python(tmp_path: Path) -> None:
    """Code AC: synthetic decisions → graduation pipeline produces valid Python."""
    profile = {"urgency": "high", "risk": "low", "novelty": "low", "reversibility": "high"}
    decisions = tuple(
        parse_decision_record(_tick_record(decision_id=f"d{i}", ts=NOW, profile=profile,
                                            learning=f"learn {i}"))
        for i in range(3)
    )
    group = RetroGroup(profile_class="u=high|r=low|n=low|rev=high",
                       action_type="relabel", decisions=decisions)
    proposal = render_rule_proposal(group, now=NOW)
    # Parses + compiles as a Python module.
    tree = ast.parse(proposal.source)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert funcs and funcs[0].name.startswith("proposed_")
    assert "@rule" in proposal.source
    compile(proposal.source, "<test>", "exec")  # raises if invalid


def test_graduation_mentions_operator(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    _seed_consistent_class(log, 3)
    mentions: list[tuple] = []
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=tmp_path / "p"),
                        clock=lambda: NOW, writeback=FakeWriteBack(),
                        observer=FakeObserver(OutcomeVerdict(ll.OUTCOME_SUCCESS)),
                        operator_mention=lambda t, m, u: mentions.append((t, m, u)))
    loop.run_weekly(NOW)
    assert len(mentions) == 1
    assert "graduate" in mentions[0][1].lower()
    assert "coord_rule_proposals" in mentions[0][1]


def test_no_graduation_when_inconsistent(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    _seed_consistent_class(log, 2, outcome="success")
    _seed_consistent_class(log, 1, outcome="failure")  # a failure poisons the class
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=tmp_path / "p"),
                        clock=lambda: NOW, writeback=FakeWriteBack(),
                        observer=FakeObserver(OutcomeVerdict(ll.OUTCOME_SUCCESS)))
    report = loop.run_weekly(NOW)
    assert report.proposal_count == 0


def test_no_graduation_below_min_samples(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    _seed_consistent_class(log, 2)  # below default min of 3
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=tmp_path / "p"),
                        clock=lambda: NOW, writeback=FakeWriteBack(),
                        observer=FakeObserver(OutcomeVerdict(ll.OUTCOME_SUCCESS)))
    assert loop.run_weekly(NOW).proposal_count == 0


# ── JIRA-state-change outcome observer (Integration AC) ─────────────────────


class FakeJiraClient:
    def __init__(self, state: dict[str, dict]) -> None:
        self._state = state

    def get_ticket_state(self, key: str):
        return self._state.get(key)


def test_jira_observer_transition_landed() -> None:
    rec = parse_decision_record(_tick_record(
        decision_id="d", ts=NOW, action_kind="transition", target="OP-1",
        params={"to_status": "To Do"}))
    client = FakeJiraClient({"OP-1": {"status": "To Do", "labels": []}})
    obs = JiraStateChangeObserver(client_factory=lambda: client)
    assert obs.observe(rec).outcome == ll.OUTCOME_SUCCESS


def test_jira_observer_transition_did_not_land() -> None:
    rec = parse_decision_record(_tick_record(
        decision_id="d", ts=NOW, action_kind="transition", target="OP-1",
        params={"to_status": "To Do"}))
    client = FakeJiraClient({"OP-1": {"status": "In Progress", "labels": []}})
    obs = JiraStateChangeObserver(client_factory=lambda: client)
    assert obs.observe(rec).outcome == ll.OUTCOME_FAILURE


def test_jira_observer_coord_skip_is_operator_override() -> None:
    rec = parse_decision_record(_tick_record(
        decision_id="d", ts=NOW, action_kind="relabel", target="OP-1",
        params={"add": ["x"]}))
    client = FakeJiraClient({"OP-1": {"status": "To Do", "labels": ["coord-skip"]}})
    obs = JiraStateChangeObserver(client_factory=lambda: client)
    assert obs.observe(rec).outcome == ll.OUTCOME_OPERATOR_OVERRIDE


def test_jira_observer_unknown_when_unreadable() -> None:
    rec = parse_decision_record(_tick_record(decision_id="d", ts=NOW, target="OP-1"))
    obs = JiraStateChangeObserver(client_factory=lambda: FakeJiraClient({}))
    assert obs.observe(rec).outcome == ll.OUTCOME_UNKNOWN


def test_jira_observer_fail_open_on_error() -> None:
    def boom():
        raise RuntimeError("jira down")

    rec = parse_decision_record(_tick_record(decision_id="d", ts=NOW))
    assert JiraStateChangeObserver(client_factory=boom).observe(rec).outcome == ll.OUTCOME_UNKNOWN


# ── scheduling + statelessness (§3.2) ───────────────────────────────────────


def test_due_seeds_baseline_then_fires(tmp_path: Path) -> None:
    loop, log, _ = _loop(tmp_path)
    # Cold (last_*_at None) → does not fire, just seeds.
    assert loop.maybe_run(NOW) == (None, None)
    loop.seed_schedule(NOW)
    # Within the interval → no fire.
    daily, weekly = loop.maybe_run(NOW + timedelta(hours=1))
    assert daily is None and weekly is None
    # Past the daily interval → daily fires, weekly not yet.
    daily, weekly = loop.maybe_run(NOW + timedelta(hours=25))
    assert daily is not None and weekly is None


def test_from_decision_log_rebuilds_schedule(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    log.append({"ts": (NOW - timedelta(hours=2)).isoformat(), "event": ll.DAILY_RAN_EVENT})
    log.append({"ts": (NOW - timedelta(days=2)).isoformat(), "event": ll.WEEKLY_RAN_EVENT})
    loop = LearningLoop.from_decision_log(log, clock=lambda: NOW,
                                          config=LearningLoopConfig(proposal_dir=tmp_path / "p"))
    # Daily ran 2h ago → not due; weekly ran 2d ago → not due (7d interval).
    daily, weekly = loop.maybe_run(NOW)
    assert daily is None and weekly is None
    # The recorded daily run was at NOW-2h. 23h after that (NOW+21h) → still
    # not due; 25h after (NOW+23h) → due.
    assert loop.maybe_run(NOW + timedelta(hours=21))[0] is None
    assert loop.maybe_run(NOW + timedelta(hours=23))[0] is not None


# ── daemon integration (Integration + Exercised AC) ─────────────────────────


def test_daemon_tier2_writes_back_in_tick(tmp_path: Path) -> None:
    """Integration AC: every Tier-2 decision triggers write-back within the tick."""
    pytest_importorskip_consultation()
    from backend.agents.pipeline_coordinator import CoordinatorConfig, PipelineCoordinator
    from backend.agents.pipeline_coordinator_rules import Ticket, WorkGraph

    engine = _hybrid_tier2_engine()
    base = tmp_path / "coord"
    cfg = CoordinatorConfig(config_dir=base, heartbeat_path=base / "hb",
                            decision_log_dir=base / "decision-log")
    log = DecisionLog(cfg.decision_log_dir, clock=lambda: NOW)
    wb = FakeWriteBack()
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=base / "p"),
                        clock=lambda: NOW, writeback=wb,
                        observer=FakeObserver(OutcomeVerdict(ll.OUTCOME_UNKNOWN)))
    story = Ticket(key="OP-77", status="To Do")
    coord = PipelineCoordinator(
        cfg, engine=engine, clock=lambda: NOW, decision_log=log, learning_loop=loop,
        work_graph_provider=lambda: WorkGraph(focal=story, tickets={story.key: story}),
    )
    result = coord.run_once()
    assert result.tier == 2
    assert len(wb.nodes) == 1  # written back in the same tick
    assert wb.nodes[0].tickets  # carries the touched ticket edge


def test_exercised_five_writebacks(tmp_path: Path) -> None:
    """Exercised AC: >=5 Tier-2 decisions written back over a shadow window."""
    log = DecisionLog(tmp_path / "decision-log", clock=lambda: NOW)
    for i in range(5):
        log.append(_tick_record(decision_id=f"d{i}", ts=NOW - timedelta(hours=1), target=f"OP-{i}"))
    wb = FakeWriteBack()
    loop = LearningLoop(decision_log=log, config=LearningLoopConfig(proposal_dir=tmp_path / "p"),
                        clock=lambda: NOW, writeback=wb,
                        observer=FakeObserver(OutcomeVerdict(ll.OUTCOME_UNKNOWN)))
    report = loop.run_daily(NOW)
    assert report.writeback_count == 5
    assert len(wb.nodes) == 5


# ── small fixtures for the daemon-integration test ──────────────────────────


def pytest_importorskip_consultation() -> None:
    pytest.importorskip("backend.agents.pipeline_coordinator_llm_consultation")


def _hybrid_tier2_engine():
    from backend.agents.pipeline_coordinator_llm_consultation import (
        BudgetGuard,
        HybridDecisionEngine,
        LLMInvocation,
        Tier2Consultant,
    )

    class StubRunner:
        def invoke(self, prompt: str) -> LLMInvocation:
            payload = {
                "result": json.dumps({
                    "decision_rationale": "ask operator",
                    "actions": [{"action": "mention_operator", "ticket": "OP-77",
                                 "params": {"message": "hi", "urgency": "low"}}],
                    "confidence": "medium",
                    "learning": "novel case",
                }),
                "total_cost_usd": 0.1,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
            return LLMInvocation(stdout=json.dumps(payload), stderr="", returncode=0)

    consultant = Tier2Consultant(runner=StubRunner(), budget=BudgetGuard(5.0, clock=lambda: NOW),
                                 lessons_provider=lambda ctx, w: ())
    return HybridDecisionEngine(consultant=consultant)
