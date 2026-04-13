"""Tests for Phase 47B: stuck detection + strategy switch."""

from __future__ import annotations

import time

import pytest

from backend import decision_engine as de
from backend import stuck_detector as sd


class TestPrimitives:

    def test_repeat_error_none(self):
        assert not sd.has_repeat_error(None)
        assert not sd.has_repeat_error([])

    def test_repeat_error_below_threshold(self):
        assert not sd.has_repeat_error(["a", "b", "a"])

    def test_repeat_error_tail_identical(self):
        assert sd.has_repeat_error(["x", "y", "err", "err", "err"])

    def test_repeat_error_ignores_empty_keys(self):
        assert not sd.has_repeat_error(["", "", ""])

    def test_long_running(self):
        now = time.time()
        assert sd.is_long_running(now - 1000, now=now, limit_s=900)
        assert not sd.is_long_running(now - 100, now=now, limit_s=900)
        assert not sd.is_long_running(None, now=now)

    def test_blocked_forever(self):
        now = time.time()
        assert sd.is_blocked_forever(now - 4000, now=now, limit_s=3600)
        assert not sd.is_blocked_forever(now - 100, now=now, limit_s=3600)

    def test_retry_burn(self):
        assert sd.has_retry_burn(5)
        assert not sd.has_retry_burn(2)


class TestStrategyPicker:

    def test_repeat_error_switches_model(self):
        assert sd.pick_strategy(sd.StuckReason.repeat_error) == sd.Strategy.switch_model

    def test_long_running_spawns_alt(self):
        assert sd.pick_strategy(sd.StuckReason.long_running) == sd.Strategy.spawn_alternate

    def test_blocked_forever_escalates(self):
        assert sd.pick_strategy(sd.StuckReason.blocked_forever) == sd.Strategy.escalate


class TestAnalyze:

    def test_no_signal_on_healthy_state(self):
        assert sd.analyze_agent("a1", error_history=["a", "b"], retry_count=1) is None

    def test_repeat_error_signal(self):
        sig = sd.analyze_agent(
            "a1", error_history=["e", "e", "e"], retry_count=0,
        )
        assert sig is not None
        assert sig.reason == sd.StuckReason.repeat_error
        assert sig.suggested_strategy == sd.Strategy.switch_model

    def test_long_running_signal(self):
        now = time.time()
        sig = sd.analyze_agent(
            "a2", started_at=now - 2000, now=now,
        )
        assert sig is not None
        assert sig.reason == sd.StuckReason.long_running

    def test_blocked_task_signal(self):
        now = time.time()
        sig = sd.analyze_blocked_task("t9", blocked_since=now - 7200, now=now)
        assert sig is not None
        assert sig.reason == sd.StuckReason.blocked_forever
        assert sig.suggested_strategy == sd.Strategy.escalate


class TestDecisionEngineBridge:

    def setup_method(self):
        de._reset_for_tests()

    def test_propose_in_manual_queues(self):
        de.set_mode("manual")
        sig = sd.analyze_agent("a1", error_history=["e", "e", "e"])
        dec = sd.propose_remediation(sig)
        assert dec.status == de.DecisionStatus.pending
        # Options include the recommended + retry + escalate
        opt_ids = {o["id"] for o in dec.options}
        assert "switch_model" in opt_ids
        assert "retry_same" in opt_ids
        assert "escalate" in opt_ids
        assert dec.default_option_id == "switch_model"
        assert dec.source["agent_id"] == "a1"

    def test_propose_auto_executes_in_full_auto(self):
        de.set_mode("full_auto")
        sig = sd.analyze_agent("a1", error_history=["e", "e", "e"])
        dec = sd.propose_remediation(sig)
        # risky → auto in full_auto
        assert dec.status == de.DecisionStatus.auto_executed
        assert dec.chosen_option_id == "switch_model"

    def test_escalate_requires_turbo_for_auto(self):
        de.set_mode("full_auto")
        now = time.time()
        sig = sd.analyze_blocked_task("t-stuck", blocked_since=now - 9000, now=now)
        dec = sd.propose_remediation(sig)
        # destructive → full_auto still requires approval
        assert dec.status == de.DecisionStatus.pending
        de.set_mode("turbo")
        dec2 = sd.propose_remediation(sig)
        assert dec2.status == de.DecisionStatus.auto_executed
