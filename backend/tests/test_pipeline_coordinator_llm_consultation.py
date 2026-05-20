"""Tier-2 LLM consultation tests (OP-1004 / AUDIT-29f-6) — ADR-0021 §5.2/§5.3.

Coverage map against the Code + Integration acceptance criteria:

  Code AC
    7-section bundle (§5.2)        → test_context_bundle_has_seven_sections*
    claude CLI w/ timeout          → test_cli_runner_{invokes,timeout}
    strict JSON parse; invalid→esc → test_parse_{strict,fenced,invalid}*,
                                      test_consult_parse_failure_escalates
    only §6.1 actions executed     → test_validate_actions_drops_unknown*
    daily budget cap (env)         → test_budget_{from_env,cap,reset_next_day}
    degrade to Tier-1 + alert      → test_consult_degrades_on_budget*

  Integration AC
    Tier-2 on no-match / conflict  → test_hybrid_{no_match,conflict}_consults
    mode "always" → Tier-2         → test_hybrid_mode_always_consults
    Tier-2 action via same layer   → test_integration_tier2_action_in_log
    log distinguishes tier 1 vs 2  → test_integration_tier_distinction
    Cognee recall observable       → test_consult_logs_cited_lessons,
                                      test_bundle_lists_recalled_lessons
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.agents import pipeline_coordinator_llm_consultation as llm
from backend.agents.pipeline_coordinator_capacity import CapacitySnapshot, RunnerCapacity
from backend.agents.pipeline_coordinator_llm_consultation import (
    BudgetGuard,
    HybridDecisionEngine,
    LLMConsultationConfig,
    LLMInvocation,
    LLMResponseInvalid,
    RecalledLesson,
    Tier2Consultant,
    build_context_bundle,
    parse_cli_envelope,
    parse_decision_json,
    validate_actions,
)
from backend.agents.pipeline_coordinator_modes import (
    InvestigationMode,
    ExecutionMode,
    Level,
    SituationProfile,
)
from backend.agents.pipeline_coordinator_rules import (
    ACTION_ESCALATE,
    ACTION_MENTION_OPERATOR,
    ACTION_RELABEL,
    Comment,
    DecisionContext,
    Ticket,
    Tier1RuleEngine,
)

NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


def _ctx(ticket: Ticket | None = None, *, tickets=None, capacity=None, mode="ExecutionMode",
         situation=None) -> DecisionContext:
    return DecisionContext(
        now=NOW,
        capacity=capacity or CapacitySnapshot.empty(captured_at=NOW),
        mode=mode,
        situation=situation,
        ticket=ticket,
        tickets=tickets or {},
    )


def _envelope(decision: dict, *, cost: float = 0.05, in_tok: int = 4000, out_tok: int = 120) -> str:
    """A claude CLI --output-format json envelope wrapping a decision JSON."""
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": json.dumps(decision),
            "total_cost_usd": cost,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        }
    )


class _StubRunner:
    """Injectable LLMRunner returning a canned invocation; records the prompt."""

    def __init__(self, invocation: LLMInvocation) -> None:
        self._invocation = invocation
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> LLMInvocation:
        self.prompts.append(prompt)
        return self._invocation


def _ok_runner(decision: dict, **kw) -> _StubRunner:
    return _StubRunner(LLMInvocation(stdout=_envelope(decision, **kw), stderr="", returncode=0))


# ════════════════════════════════════════════════════════════════════════
# Code AC — 7-section context bundle (ADR §5.2)
# ════════════════════════════════════════════════════════════════════════


def test_context_bundle_has_seven_sections() -> None:
    bundle = build_context_bundle(_ctx(Ticket(key="OP-9")), mode_behavior=ExecutionMode)
    assert len(bundle.sections) == 7
    text = bundle.render()
    for title in bundle.sections:
        assert title in text


def test_context_bundle_sections_in_adr_order() -> None:
    bundle = build_context_bundle(_ctx(Ticket(key="OP-9")), mode_behavior=ExecutionMode)
    positions = [bundle.render().index(t) for t in bundle.sections]
    assert positions == sorted(positions)  # the 7 sections appear in order


def test_bundle_lists_recalled_lessons() -> None:
    """Integration AC: Cognee lesson recall observable in the bundle."""
    lessons = (RecalledLesson("L-OP-870-blockedby.md", "blockedBy direction", 0.91),)
    bundle = build_context_bundle(
        _ctx(Ticket(key="OP-9")), mode_behavior=InvestigationMode, lessons=lessons
    )
    assert "L-OP-870-blockedby.md" in bundle.render()
    assert bundle.lessons == lessons


def test_bundle_walks_blocked_by_chain_to_mode_hops() -> None:
    a = Ticket(key="OP-A", blocked_by=("OP-B",))
    b = Ticket(key="OP-B", blocked_by=("OP-C",))
    c = Ticket(key="OP-C")
    bundle = build_context_bundle(
        _ctx(a, tickets={"OP-A": a, "OP-B": b, "OP-C": c}),
        mode_behavior=InvestigationMode,  # 10-hop
    )
    body = bundle.render()
    assert "OP-B" in body and "OP-C" in body


def test_bundle_renders_runner_capacity() -> None:
    cap = CapacitySnapshot(
        captured_at=NOW,
        runners={"subscription-codex": RunnerCapacity("subscription-codex", free_slots=2)},
    )
    bundle = build_context_bundle(_ctx(Ticket(key="OP-9"), capacity=cap), mode_behavior=ExecutionMode)
    assert "subscription-codex" in bundle.render()


# ════════════════════════════════════════════════════════════════════════
# Code AC — claude CLI invocation via subprocess with timeout
# ════════════════════════════════════════════════════════════════════════


def test_cli_runner_invokes_claude_with_json_output(monkeypatch) -> None:
    captured = {}

    class _FakeProc:
        returncode = 0

        def communicate(self, prompt, timeout=None):
            captured["prompt"] = prompt
            captured["timeout"] = timeout
            return (_envelope({"actions": [], "confidence": "high"}), "")

    def _fake_popen(argv, **kw):
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(llm.subprocess, "Popen", _fake_popen)
    runner = llm.CliLLMRunner(LLMConsultationConfig(cli_timeout_s=42.0, model="claude-opus-4-7"))
    out = runner.invoke("hello")
    assert out.ok
    assert captured["argv"][0] == "claude"
    assert "--output-format" in captured["argv"] and "json" in captured["argv"]
    assert "claude-opus-4-7" in captured["argv"]
    assert captured["timeout"] == 42.0
    assert captured["prompt"] == "hello"


def test_cli_runner_timeout_returns_timed_out(monkeypatch) -> None:
    class _HangingProc:
        returncode = None

        def communicate(self, prompt=None, timeout=None):
            if timeout is not None:
                raise llm.subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
            return ("", "killed")

        def kill(self):
            pass

    monkeypatch.setattr(llm.subprocess, "Popen", lambda *a, **k: _HangingProc())
    out = llm.CliLLMRunner(LLMConsultationConfig(cli_timeout_s=1.0)).invoke("hi")
    assert out.timed_out is True
    assert out.ok is False


def test_cli_runner_missing_binary_is_nonfatal(monkeypatch) -> None:
    def _boom(*a, **k):
        raise OSError("claude: command not found")

    monkeypatch.setattr(llm.subprocess, "Popen", _boom)
    out = llm.CliLLMRunner().invoke("hi")
    assert out.ok is False and out.returncode == 127


# ════════════════════════════════════════════════════════════════════════
# Code AC — strict JSON parse (invalid → escalate)
# ════════════════════════════════════════════════════════════════════════


def test_parse_cli_envelope_extracts_cost_and_result() -> None:
    env = parse_cli_envelope(_envelope({"actions": []}, cost=0.07, in_tok=10, out_tok=3))
    assert env.cost_usd == 0.07
    assert env.input_tokens == 10 and env.output_tokens == 3
    assert json.loads(env.result_text) == {"actions": []}


def test_parse_cli_envelope_empty_raises() -> None:
    with pytest.raises(LLMResponseInvalid):
        parse_cli_envelope("   ")


def test_parse_decision_strict() -> None:
    parsed = parse_decision_json(
        json.dumps(
            {
                "decision_rationale": "reroute",
                "actions": [{"action": "relabel", "ticket": "OP-1", "params": {"add": ["x"]}}],
                "confidence": "high",
                "escalate_if_wrong": False,
                "learning": "lesson",
            }
        )
    )
    assert parsed.confidence == "high"
    assert parsed.actions[0].kind == ACTION_RELABEL
    assert parsed.learning == "lesson"


def test_parse_decision_tolerates_code_fence() -> None:
    fenced = "```json\n" + json.dumps({"actions": [], "confidence": "low"}) + "\n```"
    parsed = parse_decision_json(fenced)
    assert parsed.confidence == "low"


def test_parse_decision_invalid_json_raises() -> None:
    with pytest.raises(LLMResponseInvalid):
        parse_decision_json("not json at all")


def test_parse_decision_non_object_raises() -> None:
    with pytest.raises(LLMResponseInvalid):
        parse_decision_json("[1, 2, 3]")


def test_parse_decision_actions_not_list_raises() -> None:
    with pytest.raises(LLMResponseInvalid):
        parse_decision_json(json.dumps({"actions": "relabel"}))


def test_parse_decision_unknown_confidence_defaults_low() -> None:
    parsed = parse_decision_json(json.dumps({"actions": [], "confidence": "certain"}))
    assert parsed.confidence == "low"


# ════════════════════════════════════════════════════════════════════════
# Code AC — action validation: only §6.1 allowed actions execute
# ════════════════════════════════════════════════════════════════════════


def test_validate_actions_accepts_allowed_kinds() -> None:
    actions, dropped = validate_actions(
        [
            {"action": "relabel", "ticket": "OP-1", "params": {"add": ["q"]}},
            {"action": "escalate", "ticket": "OP-2", "params": {"reason": "r"}},
        ]
    )
    assert {a.kind for a in actions} == {ACTION_RELABEL, ACTION_ESCALATE}
    assert dropped == []


def test_validate_actions_drops_unknown_action_name() -> None:
    actions, dropped = validate_actions(
        [{"action": "rm_rf_prod", "ticket": "OP-1"}, {"action": "merge_to_main"}]
    )
    assert actions == ()
    assert len(dropped) == 2
    assert all(d["reason"] == "unknown_action" for d in dropped)


def test_validate_actions_drops_non_object() -> None:
    actions, dropped = validate_actions(["relabel", 42])
    assert actions == ()
    assert all(d["reason"] == "not_an_object" for d in dropped)


def test_validate_actions_noop_is_not_executable() -> None:
    actions, dropped = validate_actions([{"action": "noop", "params": {"reason": "nothing"}}])
    assert actions == ()  # noop carries no executable effect
    assert dropped == []  # but it is a *recognised* action, not a reject


# ════════════════════════════════════════════════════════════════════════
# Code AC — budget guard (daily cap via env) + reset
# ════════════════════════════════════════════════════════════════════════


def test_budget_from_env_reads_daily_cap(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_COORDINATOR_DAILY_BUDGET_USD", "1.50")
    guard = BudgetGuard.from_env(clock=lambda: NOW)
    assert guard.cap_usd == 1.50


def test_budget_cap_blocks_when_exhausted() -> None:
    guard = BudgetGuard(1.00, clock=lambda: NOW)
    assert guard.can_afford(0.20) is True
    guard.record(0.95)
    assert guard.remaining_usd() == pytest.approx(0.05)
    assert guard.can_afford(0.20) is False
    guard.record(0.10)
    assert guard.exhausted is True


def test_budget_zero_cap_disables_tier2() -> None:
    guard = BudgetGuard(0.0, clock=lambda: NOW)
    assert guard.exhausted is True
    assert guard.can_afford(0.0) is False


def test_budget_resets_next_utc_day() -> None:
    clock_day = {"d": NOW}
    guard = BudgetGuard(1.00, clock=lambda: clock_day["d"])
    guard.record(1.00)
    assert guard.exhausted is True
    clock_day["d"] = NOW + timedelta(days=1)  # day rollover
    assert guard.spent_usd() == 0.0
    assert guard.exhausted is False


def test_budget_from_decision_log_rebuilds_spend(tmp_path: Path) -> None:
    """Stateless rebuild (§3.2): day's spend re-summed from the log."""
    log_dir = tmp_path / "decision-log"
    log_dir.mkdir()
    day_file = log_dir / f"{NOW.date().isoformat()}.jsonl"
    day_file.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"event": "decision_tick", "tier": 2, "llm_consultation": {"cost_usd": 0.30}},
                {"event": "decision_tick", "tier": 2, "llm_consultation": {"cost_usd": 0.45}},
                {"event": "decision_tick", "tier": 1},  # no consult → no cost
            ]
        )
        + "\n"
    )
    guard = BudgetGuard.from_decision_log(log_dir, daily_budget_usd=2.0, clock=lambda: NOW)
    assert guard.spent_usd() == pytest.approx(0.75)


# ════════════════════════════════════════════════════════════════════════
# Tier2Consultant — happy path, parse/invocation failure, budget degrade
# ════════════════════════════════════════════════════════════════════════


def _consultant(runner, *, budget=None, lessons=()) -> Tier2Consultant:
    return Tier2Consultant(
        config=LLMConsultationConfig(daily_budget_usd=10.0, per_decision_estimate_usd=0.20),
        runner=runner,
        budget=budget or BudgetGuard(10.0, clock=lambda: NOW),
        lessons_provider=lambda ctx, w: lessons,
        clock=lambda: NOW,
    )


def test_consult_happy_path_returns_validated_action_and_cost() -> None:
    runner = _ok_runner(
        {
            "decision_rationale": "reroute to codex",
            "actions": [{"action": "relabel", "ticket": "OP-1", "params": {"add": ["x"]}}],
            "confidence": "high",
            "learning": "codex handles backend rewrites",
        },
        cost=0.08,
    )
    consultant = _consultant(runner)
    outcome = consultant.consult(_ctx(Ticket(key="OP-1")))
    assert outcome.reason == llm.TIER2_CONSULTED_REASON
    assert outcome.actions[0].kind == ACTION_RELABEL
    assert outcome.llm_consultation["cost_usd"] == 0.08
    assert outcome.llm_consultation["confidence"] == "high"
    assert outcome.learning == "codex handles backend rewrites"
    assert consultant.budget.spent_usd() == pytest.approx(0.08)


def test_consult_records_cited_lessons_in_metadata() -> None:
    lessons = (RecalledLesson("L-OP-922.md", "release as state machine", 0.8),)
    runner = _ok_runner({"actions": [], "confidence": "low", "escalate_if_wrong": False})
    outcome = _consultant(runner, lessons=lessons).consult(_ctx(Ticket(key="OP-1")))
    assert outcome.llm_consultation["lessons_cited"] == ["L-OP-922.md"]


def test_consult_logs_cited_lessons(caplog) -> None:
    """Integration AC: lesson recall observable (logged) on the Tier-2 path."""
    lessons = (RecalledLesson("L-OP-985.md", "release-cut vs code-review", 0.7),)
    runner = _ok_runner({"actions": [], "confidence": "low"})
    with caplog.at_level(logging.INFO, logger=llm.__name__):
        _consultant(runner, lessons=lessons).consult(_ctx(Ticket(key="OP-1")))
    line = next(m for m in caplog.messages if "Tier-2 consult" in m)
    assert "L-OP-985.md" in line


def test_consult_parse_failure_escalates() -> None:
    runner = _StubRunner(LLMInvocation(stdout="this is not json", stderr="", returncode=0))
    outcome = _consultant(runner).consult(_ctx(Ticket(key="OP-1")))
    assert outcome.reason == llm.TIER2_PARSE_FAILED_REASON
    assert outcome.actions[0].kind == ACTION_ESCALATE
    assert outcome.llm_consultation["cost_usd"] == 0.0


def test_consult_invocation_failure_escalates() -> None:
    runner = _StubRunner(LLMInvocation(stdout="", stderr="boom", returncode=2))
    outcome = _consultant(runner).consult(_ctx(Ticket(key="OP-1")))
    assert outcome.reason == llm.TIER2_INVOCATION_FAILED_REASON
    assert outcome.actions[0].kind == ACTION_ESCALATE


def test_consult_degrades_on_budget_exhausted() -> None:
    spent = BudgetGuard(0.20, clock=lambda: NOW)
    spent.record(0.20)  # fully spent
    runner = _ok_runner({"actions": [{"action": "relabel", "ticket": "OP-1"}]})
    outcome = _consultant(runner, budget=spent).consult(_ctx(Ticket(key="OP-1")))
    assert outcome.degraded is True
    assert outcome.reason == llm.TIER2_DEGRADED_BUDGET_REASON
    assert outcome.actions[0].kind == ACTION_MENTION_OPERATOR
    assert outcome.actions[0].params["urgency"] == "high"
    # The CLI must NOT have been invoked once budget is gone.
    assert runner.prompts == []


def test_consult_drops_unknown_action_keeps_valid(caplog) -> None:
    runner = _ok_runner(
        {
            "actions": [
                {"action": "relabel", "ticket": "OP-1", "params": {"add": ["ok"]}},
                {"action": "delete_repo", "ticket": "OP-1"},
            ],
            "confidence": "medium",
        }
    )
    outcome = _consultant(runner).consult(_ctx(Ticket(key="OP-1")))
    assert [a.kind for a in outcome.actions] == [ACTION_RELABEL]
    assert outcome.llm_consultation["dropped_actions"][0]["action"] == "delete_repo"


# ════════════════════════════════════════════════════════════════════════
# Integration AC — HybridDecisionEngine tier routing
# ════════════════════════════════════════════════════════════════════════


def _hybrid(runner, *, budget=None, lessons=()) -> HybridDecisionEngine:
    return HybridDecisionEngine(
        tier1=Tier1RuleEngine(),
        consultant=_consultant(runner, budget=budget, lessons=lessons),
    )


def test_hybrid_tier1_match_does_not_consult() -> None:
    """A clean Tier-1 match in Execution mode skips Tier-2 (cost saver)."""
    runner = _ok_runner({"actions": []})
    # capability-blocked Story → Tier-1 fires deterministically.
    story = Ticket(
        key="OP-1", issuetype="ストーリー",
        comments=(Comment("[runner-capability-blocked] x"),),
    )
    result = _hybrid(runner).evaluate(_ctx(story))
    assert result.tier == 1
    assert result.rule_name == "capability-blocked-known-locale"
    assert runner.prompts == []  # LLM never consulted


def test_hybrid_no_match_consults_tier2() -> None:
    runner = _ok_runner(
        {"actions": [{"action": "mention_operator", "ticket": "OP-1", "params": {"message": "m"}}],
         "confidence": "high"}
    )
    # A ticket no Tier-1 rule matches (plain, no markers).
    result = _hybrid(runner).evaluate(_ctx(Ticket(key="OP-1", status="To Do")))
    assert result.tier == 2
    assert result.reason == llm.TIER2_CONSULTED_REASON
    assert len(runner.prompts) == 1


class _CountingConsultant:
    """Fake Tier2Consultant that counts consult() invocations (OP-1556 AC).

    Mirrors the seam HybridDecisionEngine depends on — exposes a ``budget``
    property and a ``consult`` that records every call — so a test can assert
    the LLM was (or was not) consulted without spawning a subprocess.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.budget = BudgetGuard(10.0, clock=lambda: NOW)

    def consult(self, ctx, *, behavior=None, trigger="tier1_no_match"):
        self.calls += 1
        return llm.Tier2Outcome(actions=(), reason=llm.TIER2_CONSULTED_REASON, llm_consultation={})


def test_hybrid_idle_tick_noops_without_consulting() -> None:
    """OP-1556: empty work-graph (no focal ticket, no slice) → Tier-1 NoopAction.

    The LLM must NOT be consulted on an idle tick — assert via a fake
    consultant that counts invocations (Integration AC).
    """
    consultant = _CountingConsultant()
    engine = HybridDecisionEngine(tier1=Tier1RuleEngine(), consultant=consultant)
    result = engine.evaluate(_ctx(None))
    assert result.tier == 1
    assert result.tier != 2
    assert result.is_noop
    assert result.reason == llm.TIER1_IDLE_NOOP_REASON
    assert consultant.calls == 0  # consultant.consult NOT called on an idle tick


def test_hybrid_idle_tick_with_runner_never_invokes_cli() -> None:
    """End-to-end via the real consultant seam: the CLI runner is never hit."""
    runner = _ok_runner({"actions": [], "confidence": "low", "escalate_if_wrong": False})
    result = _hybrid(runner).evaluate(_ctx(None))
    assert result.tier == 1
    assert result.is_noop
    assert runner.prompts == []  # no LLM invocation on an idle tick


def test_hybrid_empty_tickets_map_also_short_circuits() -> None:
    """A tick with neither a focal ticket nor a work-graph slice is idle."""
    consultant = _CountingConsultant()
    engine = HybridDecisionEngine(tier1=Tier1RuleEngine(), consultant=consultant)
    result = engine.evaluate(_ctx(None, tickets={}))
    assert result.tier == 1
    assert consultant.calls == 0


def test_hybrid_mode_always_with_situation_still_consults_when_idle() -> None:
    """The documented exception: tier2_policy=always + a real situation consults.

    A deliberate sprint-level/idle consult (Investigation mode carrying an
    actual SituationProfile) is allowed past the idle short-circuit even with
    no focal ticket — UNLESS clause of the Code AC.
    """
    consultant = _CountingConsultant()
    engine = HybridDecisionEngine(tier1=Tier1RuleEngine(), consultant=consultant)
    situation = SituationProfile(urgency=Level.LOW, novelty=Level.HIGH)  # → Investigation
    result = engine.evaluate(_ctx(None, situation=situation, mode="skeleton"))
    assert result.mode == "InvestigationMode"
    assert result.tier == 2
    assert consultant.calls == 1


def test_hybrid_operator_keep_out_vetoes_both_tiers() -> None:
    runner = _ok_runner({"actions": [{"action": "relabel", "ticket": "OP-1"}]})
    t = Ticket(key="OP-1", labels=("coord-skip",))
    result = _hybrid(runner).evaluate(_ctx(t))
    assert result.tier == 1
    assert result.rule_name == "operator-keep-out"
    assert result.is_noop
    assert runner.prompts == []  # escape-hatched ticket never reaches the LLM


def test_hybrid_conflict_routes_to_tier2() -> None:
    runner = _ok_runner({"actions": [], "confidence": "low"})
    # Construct a ticket that trips two *distinct* rules: revert-loop-quarantine
    # (relabel) AND merger-repeat-fail (escalate) — mutually distinct actions.
    t = Ticket(
        key="OP-1",
        revert_count_24h=6,      # > REVERT_LOOP_QUARANTINE_THRESHOLD
        merger_fail_count=4,     # > MERGER_FAIL_ESCALATE_THRESHOLD
    )
    result = _hybrid(runner).evaluate(_ctx(t))
    assert result.tier == 2
    assert len(runner.prompts) == 1


def test_hybrid_mode_always_consults_even_on_tier1_match() -> None:
    """Investigation mode (tier2_policy=always) consults despite a Tier-1 hit."""
    runner = _ok_runner({"actions": [], "confidence": "high"})
    story = Ticket(
        key="OP-1", issuetype="ストーリー",
        comments=(Comment("[runner-capability-blocked] x"),),
    )
    # low urgency + high novelty → InvestigationMode (always Tier-2).
    situation = SituationProfile(urgency=Level.LOW, novelty=Level.HIGH)
    result = _hybrid(runner).evaluate(_ctx(story, situation=situation, mode="skeleton"))
    assert result.mode == "InvestigationMode"
    assert result.tier == 2
    assert len(runner.prompts) == 1


def test_hybrid_budget_degrade_prefers_tier1_action() -> None:
    """Budget hit + a Tier-1 match → Tier-1 action used, plus operator alert."""
    spent = BudgetGuard(0.10, clock=lambda: NOW)
    spent.record(0.10)
    runner = _ok_runner({"actions": []})
    story = Ticket(
        key="OP-1", issuetype="ストーリー",
        comments=(Comment("[runner-capability-blocked] x"),),
    )
    # Force always-consult via Investigation so the budget path is exercised
    # even though Tier-1 matched.
    situation = SituationProfile(urgency=Level.LOW, novelty=Level.HIGH)
    result = _hybrid(runner, budget=spent).evaluate(
        _ctx(story, situation=situation, mode="skeleton")
    )
    assert result.tier == 1  # degraded to Tier-1-only
    assert result.reason == llm.TIER2_DEGRADED_BUDGET_REASON
    kinds = [a.kind for a in result.actions]
    assert ACTION_RELABEL in kinds            # the Tier-1 deterministic action
    assert ACTION_MENTION_OPERATOR in kinds   # the operator budget alert
    assert runner.prompts == []


# ════════════════════════════════════════════════════════════════════════
# Integration AC — through the daemon: decision log, tier distinction
# ════════════════════════════════════════════════════════════════════════


def _read_log(directory: Path) -> list[dict]:
    out: list[dict] = []
    for f in sorted(directory.glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return out


def _coordinator(tmp_path: Path, engine, story):
    from backend.agents.pipeline_coordinator import CoordinatorConfig, PipelineCoordinator

    base = tmp_path / "coord"
    cfg = CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
    )
    coord = PipelineCoordinator(
        cfg,
        engine=engine,
        clock=lambda: NOW,
        work_graph_provider=lambda: __import__(
            "backend.agents.pipeline_coordinator_rules", fromlist=["WorkGraph"]
        ).WorkGraph(focal=story, tickets={story.key: story} if story else {}),
    )
    return coord, cfg


def test_integration_tier2_action_in_log(tmp_path: Path) -> None:
    """Integration AC: Tier-2 action flows through the same action layer + log."""
    runner = _ok_runner(
        {
            "decision_rationale": "ask operator",
            "actions": [{"action": "mention_operator", "ticket": "OP-3",
                         "params": {"message": "please look", "urgency": "medium"}}],
            "confidence": "medium",
            "learning": "novel cross-area dep needs human",
        },
        cost=0.12,
    )
    engine = _hybrid(runner)
    story = Ticket(key="OP-3", status="To Do")  # no Tier-1 rule matches
    coord, cfg = _coordinator(tmp_path, engine, story)

    result = coord.run_once()

    assert result.tier == 2
    [record] = [r for r in _read_log(cfg.decision_log_dir) if r["event"] == "decision_tick"]
    assert record["tier"] == 2
    assert record["actions"][0]["kind"] == ACTION_MENTION_OPERATOR
    # Tier-2 metadata embedded (ADR Appendix C llm_consultation block).
    assert record["llm_consultation"]["confidence"] == "medium"
    assert record["llm_consultation"]["cost_usd"] == 0.12
    # Same shadow action layer as Tier-1.
    assert record["action_results"][0]["shadow"] is True
    assert record["action_results"][0]["executed"] is False


def test_integration_tier_distinction(tmp_path: Path) -> None:
    """Decision log distinguishes tier=1 (Tier-1 rule) vs tier=2 (consult)."""
    runner = _ok_runner({"actions": [], "confidence": "low"})

    # Tick 1: Tier-1 deterministic match (capability-blocked Story).
    t1_story = Ticket(key="OP-5", issuetype="ストーリー",
                      comments=(Comment("[runner-capability-blocked] x"),))
    coord1, cfg1 = _coordinator(tmp_path / "a", _hybrid(_ok_runner({"actions": []})), t1_story)
    coord1.run_once()
    rec1 = [r for r in _read_log(cfg1.decision_log_dir) if r["event"] == "decision_tick"][0]
    assert rec1["tier"] == 1
    assert "llm_consultation" not in rec1  # Tier-1 tick carries no consult block

    # Tick 2: no Tier-1 match → Tier-2 consult.
    t2 = Ticket(key="OP-6", status="To Do")
    coord2, cfg2 = _coordinator(tmp_path / "b", _hybrid(runner), t2)
    coord2.run_once()
    rec2 = [r for r in _read_log(cfg2.decision_log_dir) if r["event"] == "decision_tick"][0]
    assert rec2["tier"] == 2
    assert "llm_consultation" in rec2


def test_integration_budget_cap_event_degrades(tmp_path: Path) -> None:
    """Exercised AC: a budget cap event degrades the coordinator correctly."""
    spent = BudgetGuard(0.10, clock=lambda: NOW)
    spent.record(0.10)
    runner = _ok_runner({"actions": [{"action": "relabel", "ticket": "OP-7"}]})
    engine = _hybrid(runner, budget=spent)
    t = Ticket(key="OP-7", status="To Do")  # no Tier-1 match → would consult
    coord, cfg = _coordinator(tmp_path, engine, t)

    coord.run_once()

    [record] = [r for r in _read_log(cfg.decision_log_dir) if r["event"] == "decision_tick"]
    assert record["reason"] == llm.TIER2_DEGRADED_BUDGET_REASON
    assert record["llm_consultation"]["degraded"] == "budget_cap"
    assert record["actions"][0]["kind"] == ACTION_MENTION_OPERATOR
    assert runner.prompts == []  # no spend once degraded


def test_hybrid_engine_exposes_rule_names_for_startup_log() -> None:
    """Deploy AC seam: the hybrid engine still surfaces the Tier-1 registry."""
    engine = HybridDecisionEngine(consultant=_consultant(_ok_runner({"actions": []})))
    assert "operator-keep-out" in engine.rule_names()
    assert len(engine.rule_names()) == 10
