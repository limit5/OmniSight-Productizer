"""O8 (#271) — Orchestration mode feature flag + parity tests.

These tests pin the monolith ↔ distributed contract:

  1. Mode resolution (env > settings > default).
  2. `dispatch()` event sequence parity across modes.
  3. Distributed-path CATC synthesis + queue round-trip.
  4. Rollback drain helper (``wait`` and ``redispatch_monolith``).
  5. CLI ``python -m backend.orchestration_drain`` surface.

Both modes are tested without standing up real workers: the distributed
path gets a lightweight in-memory queue stubbed via
``set_backend_for_tests``, and the worker verdict is simulated by
transitioning messages directly to ``TaskState.Done``.  The monolith
path calls the real ``run_graph`` — it degrades gracefully to the rule-
based router when no LLM is configured (same fallback the existing
``test_graph.py`` exercises).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from backend import queue_backend as qb
from backend.queue_backend import (
    InMemoryQueueBackend,
    PriorityLevel,
    TaskState,
    set_backend_for_tests,
)
from backend import orchestration_mode as om
from backend.orchestration_mode import (
    DispatchRequest,
    OrchestrationMode,
    PARITY_EVENT_SEQUENCE,
    current_mode,
    dispatch,
    drain_distributed_inflight,
    list_inflight,
    reset_inflight_for_tests,
    set_mode_override,
)


# ──────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_queue_and_mode():
    """Reset every singleton this module touches, per test.

    Without this, an earlier test that put the module into
    ``distributed`` leaks into the next test's ``current_mode()`` probe
    and we chase phantom parity failures.
    """
    set_backend_for_tests(InMemoryQueueBackend())
    set_mode_override(None)
    reset_inflight_for_tests()
    # Clear env so settings default wins in resolution tests.
    os.environ.pop("OMNISIGHT_ORCHESTRATION_MODE", None)
    yield
    set_backend_for_tests(None)
    set_mode_override(None)
    reset_inflight_for_tests()
    os.environ.pop("OMNISIGHT_ORCHESTRATION_MODE", None)


def _simulate_worker_ack(message_id: str) -> None:
    """Emulate a worker that claimed + finished a message.

    The queue only accepts Done from Claimed/Running, so we do a brief
    pull → set_state(Running) → ack dance before delete.
    """
    msgs = qb.pull("test-worker", count=1, visibility_timeout_s=60)
    # In tests pulling one message when one is on the queue should return it.
    assert msgs, "simulated worker found no message to claim"
    assert msgs[0].message_id == message_id
    qb.set_state(message_id, TaskState.Running)
    qb.ack(message_id)


def _simulate_worker_fail(message_id: str, reason: str) -> None:
    """Emulate a worker that DLQ'd the message (3-strike exhausted)."""
    for _ in range(3):
        msgs = qb.pull("test-worker", count=1, visibility_timeout_s=60)
        if not msgs:
            break
        qb.nack(msgs[0].message_id, reason=reason)


# ──────────────────────────────────────────────────────────────
#  1. Mode resolution
# ──────────────────────────────────────────────────────────────


class TestModeResolution:
    def test_default_is_monolith_when_env_unset(self):
        # Fixture clears env + override; settings default ('monolith') wins.
        assert current_mode() is OrchestrationMode.monolith

    def test_env_var_overrides_settings(self):
        os.environ["OMNISIGHT_ORCHESTRATION_MODE"] = "distributed"
        assert current_mode() is OrchestrationMode.distributed

    def test_override_beats_env(self):
        os.environ["OMNISIGHT_ORCHESTRATION_MODE"] = "monolith"
        set_mode_override(OrchestrationMode.distributed)
        assert current_mode() is OrchestrationMode.distributed

    def test_unknown_mode_falls_back_to_monolith(self):
        os.environ["OMNISIGHT_ORCHESTRATION_MODE"] = "serverless-typo"
        assert current_mode() is OrchestrationMode.monolith

    def test_mode_parse_is_case_insensitive(self):
        assert OrchestrationMode.parse("DISTRIBUTED") is OrchestrationMode.distributed
        assert OrchestrationMode.parse(" Monolith ") is OrchestrationMode.monolith
        assert OrchestrationMode.parse(None) is OrchestrationMode.monolith
        assert OrchestrationMode.parse("") is OrchestrationMode.monolith


# ──────────────────────────────────────────────────────────────
#  2. Dispatch — monolith path
# ──────────────────────────────────────────────────────────────


class TestMonolithDispatch:
    @pytest.mark.asyncio
    async def test_monolith_returns_run_graph_state(self):
        set_mode_override(OrchestrationMode.monolith)
        req = DispatchRequest(user_command="hello")
        out = await dispatch(req)
        assert out.mode is OrchestrationMode.monolith
        # run_graph with no LLM routes to general + synthesises an answer.
        assert out.routed_to in {
            "general", "firmware", "software", "validator", "reporter",
            "reviewer", "conversation",
        }

    @pytest.mark.asyncio
    async def test_monolith_event_sequence_is_parity(self):
        set_mode_override(OrchestrationMode.monolith)
        out = await dispatch(DispatchRequest(user_command="status"))
        assert tuple(out.event_sequence) == PARITY_EVENT_SEQUENCE

    @pytest.mark.asyncio
    async def test_monolith_surfaces_graph_error_as_outcome_not_raise(self, monkeypatch):
        async def _boom(**kwargs):
            raise RuntimeError("graph exploded")

        monkeypatch.setattr("backend.agents.graph.run_graph", _boom)
        set_mode_override(OrchestrationMode.monolith)
        out = await dispatch(DispatchRequest(user_command="anything"))
        assert out.ok is False
        assert "graph exploded" in (out.error or "")
        # Parity still emitted even on failure — UI must be able to tell
        # "dispatch finished, it just failed".
        assert tuple(out.event_sequence) == PARITY_EVENT_SEQUENCE


# ──────────────────────────────────────────────────────────────
#  3. Dispatch — distributed path
# ──────────────────────────────────────────────────────────────


class TestDistributedDispatch:
    @pytest.mark.asyncio
    async def test_distributed_pushes_catc_and_waits_for_ack(self):
        set_mode_override(OrchestrationMode.distributed)
        req = DispatchRequest(
            user_command="do something in distributed mode",
            synthesised_jira_ticket="OPTEST-1",
            allowed_globs=["src/**"],
        )

        async def _run():
            return await dispatch(req, wait_s=5.0)

        # Start dispatch; in parallel simulate the worker ack.
        task = asyncio.create_task(_run())
        # Give dispatch time to push the message.
        await asyncio.sleep(0.1)
        # One message should now sit on the queue.
        assert qb.depth() == 1
        # Simulate worker pick-up + ack.
        msg_id = list_inflight()[0]["message_id"]
        _simulate_worker_ack(msg_id)
        out = await task

        assert out.ok is True
        assert out.mode is OrchestrationMode.distributed
        assert out.jira_ticket == "OPTEST-1"
        assert out.queue_message_id == msg_id
        assert tuple(out.event_sequence) == PARITY_EVENT_SEQUENCE
        # Inflight registry cleared after terminal verdict.
        assert list_inflight() == []

    @pytest.mark.asyncio
    async def test_distributed_times_out_when_no_worker(self):
        set_mode_override(OrchestrationMode.distributed)
        req = DispatchRequest(
            user_command="stranded",
            synthesised_jira_ticket="OPTEST-42",
        )
        out = await dispatch(req, wait_s=0.25)
        assert out.ok is False
        assert out.error and "distributed_wait_timeout_after" in out.error
        # Queue still holds the unacked message — operator can drain/DLQ it.
        assert qb.depth() >= 1

    @pytest.mark.asyncio
    async def test_distributed_dlq_propagates_as_outcome_failure(self):
        set_mode_override(OrchestrationMode.distributed)
        req = DispatchRequest(
            user_command="will fail",
            synthesised_jira_ticket="OPTEST-99",
        )

        task = asyncio.create_task(dispatch(req, wait_s=5.0))
        await asyncio.sleep(0.1)
        msg_id = list_inflight()[0]["message_id"]
        _simulate_worker_fail(msg_id, "simulated failure")
        out = await task

        assert out.ok is False
        assert out.mode is OrchestrationMode.distributed
        assert out.queue_message_id == msg_id
        # DLQ'd messages carry last_error through to the outcome.
        assert "simulated failure" in (out.error or "") \
            or "distributed_worker_failed" in (out.error or "")

    @pytest.mark.asyncio
    async def test_distributed_queue_push_failure_returned_as_outcome(self):
        set_mode_override(OrchestrationMode.distributed)

        def _boom(card, prio):
            raise RuntimeError("redis is down")

        req = DispatchRequest(user_command="nope")
        out = await dispatch(req, queue_push=_boom, wait_s=0.1)
        assert out.ok is False
        assert "queue_push_failed" in (out.error or "")
        # Even on push failure, full parity sequence is emitted — this is
        # the contract the UI relies on to render the failure card.
        assert tuple(out.event_sequence) == PARITY_EVENT_SEQUENCE

    @pytest.mark.asyncio
    async def test_distributed_synthesises_ticket_when_caller_passes_none(self):
        set_mode_override(OrchestrationMode.distributed)
        req = DispatchRequest(user_command="no ticket supplied")
        task = asyncio.create_task(dispatch(req, wait_s=5.0))
        await asyncio.sleep(0.1)
        inflight = list_inflight()
        assert len(inflight) == 1
        # Synthesised ticket matches the CATC validator regex.
        import re
        assert re.match(r"^[A-Z][A-Z0-9_]*-\d+$", inflight[0]["jira_ticket"])
        _simulate_worker_ack(inflight[0]["message_id"])
        await task


# ──────────────────────────────────────────────────────────────
#  4. Behaviour parity — same input, same event sequence
# ──────────────────────────────────────────────────────────────


class TestDualModeParity:
    """The O8 headline contract: identical input → identical event order.

    We cannot require identical *answer text* because the monolith path
    runs a real LLM-powered graph while the distributed path defers to a
    worker.  What MUST match is the sequence of event_type markers so the
    UI, audit_log and downstream consumers can't tell the modes apart.
    """

    @pytest.mark.asyncio
    async def test_same_command_produces_same_event_sequence_in_both_modes(self):
        req_mono = DispatchRequest(user_command="check system status")
        req_dist = DispatchRequest(
            user_command="check system status",
            synthesised_jira_ticket="PARITY-1",
        )

        set_mode_override(OrchestrationMode.monolith)
        out_mono = await dispatch(req_mono)

        set_mode_override(OrchestrationMode.distributed)
        task = asyncio.create_task(dispatch(req_dist, wait_s=5.0))
        await asyncio.sleep(0.1)
        _simulate_worker_ack(list_inflight()[0]["message_id"])
        out_dist = await task

        # Event sequence parity — the O8 contract.
        assert out_mono.event_sequence == out_dist.event_sequence
        assert tuple(out_mono.event_sequence) == PARITY_EVENT_SEQUENCE

    @pytest.mark.asyncio
    async def test_parity_holds_on_failure_path_too(self):
        """If the run fails in either mode, the event sequence is still
        emitted in full — otherwise the failure case would be
        indistinguishable from a silent hang at the UI layer."""
        req = DispatchRequest(user_command="will fail")

        set_mode_override(OrchestrationMode.monolith)
        import backend.agents.graph as graph_mod

        original = graph_mod.run_graph

        async def _fail(**kwargs):
            raise RuntimeError("simulated")

        graph_mod.run_graph = _fail  # type: ignore[assignment]
        try:
            out_mono = await dispatch(req)
        finally:
            graph_mod.run_graph = original  # type: ignore[assignment]

        set_mode_override(OrchestrationMode.distributed)
        # Starve the distributed path of workers → times out.
        out_dist = await dispatch(
            DispatchRequest(
                user_command="will fail",
                synthesised_jira_ticket="PARITY-2",
            ),
            wait_s=0.2,
        )

        assert not out_mono.ok
        assert not out_dist.ok
        assert out_mono.event_sequence == out_dist.event_sequence


# ──────────────────────────────────────────────────────────────
#  5. Rollback — drain_distributed_inflight
# ──────────────────────────────────────────────────────────────


class TestDrainInflight:
    @pytest.mark.asyncio
    async def test_empty_registry_drains_instantly(self):
        report = await drain_distributed_inflight(strategy="wait", wait_s=1.0)
        assert report.drained == []
        assert report.redispatched == []
        assert report.still_pending == []

    @pytest.mark.asyncio
    async def test_wait_strategy_drains_terminated_messages(self):
        set_mode_override(OrchestrationMode.distributed)
        req = DispatchRequest(
            user_command="drain me",
            synthesised_jira_ticket="DRAIN-1",
        )
        task = asyncio.create_task(dispatch(req, wait_s=5.0))
        await asyncio.sleep(0.1)
        msg_id = list_inflight()[0]["message_id"]
        # Ack the message before calling drain — simulates "worker
        # finished it before rollback flipped the flag".
        _simulate_worker_ack(msg_id)
        await task

        # Re-register the inflight entry to simulate a race where drain
        # runs while the bookkeeping hasn't been cleared yet.
        om._register_inflight(msg_id, req, "DRAIN-1")
        report = await drain_distributed_inflight(
            strategy="wait", wait_s=1.0, poll_interval_s=0.05,
        )
        assert msg_id in report.drained
        assert report.still_pending == []

    @pytest.mark.asyncio
    async def test_wait_strategy_reports_still_pending_on_timeout(self):
        set_mode_override(OrchestrationMode.distributed)
        req = DispatchRequest(
            user_command="stuck",
            synthesised_jira_ticket="DRAIN-2",
        )
        task = asyncio.create_task(dispatch(req, wait_s=10.0))
        await asyncio.sleep(0.1)
        # Do NOT simulate ack — leaves the message in Queued.
        report = await drain_distributed_inflight(
            strategy="wait", wait_s=0.2, poll_interval_s=0.05,
        )
        # Original dispatch is still blocked; cancel it to free the loop.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert report.still_pending, "expected drain to surface stuck msg"

    @pytest.mark.asyncio
    async def test_redispatch_monolith_strategy_reruns_through_monolith(self):
        set_mode_override(OrchestrationMode.distributed)
        req = DispatchRequest(
            user_command="rerun through monolith",
            synthesised_jira_ticket="DRAIN-3",
        )
        task = asyncio.create_task(dispatch(req, wait_s=10.0))
        await asyncio.sleep(0.1)
        msg_id = list_inflight()[0]["message_id"]

        report = await drain_distributed_inflight(
            strategy="redispatch_monolith",
            wait_s=0.1,
            poll_interval_s=0.05,
        )
        # Cancel the original dispatch — drain is the completion path.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert msg_id in report.redispatched
        assert report.still_pending == []

    @pytest.mark.asyncio
    async def test_invalid_strategy_raises(self):
        om._register_inflight("msg-x", DispatchRequest(user_command="x"), "X-1")
        with pytest.raises(ValueError):
            await drain_distributed_inflight(
                strategy="bogus", wait_s=0.05,
            )


# ──────────────────────────────────────────────────────────────
#  6. CLI helper
# ──────────────────────────────────────────────────────────────


class TestDrainCli:
    def test_cli_returns_zero_when_everything_drains(self, capsys):
        from backend import orchestration_drain
        rc = orchestration_drain.main(
            ["--strategy", "wait", "--wait-s", "0.05"],
        )
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert rc == 0
        assert payload["strategy"] == "wait"
        assert payload["still_pending"] == []

    def test_cli_returns_two_on_still_pending(self, capsys):
        from backend import orchestration_drain
        om._register_inflight(
            "msg-stuck",
            DispatchRequest(user_command="stuck"),
            "STUCK-1",
        )
        # InMemory queue doesn't know msg-stuck → it's treated as not
        # present → drained.  To simulate "still pending", push an
        # actual message and register *it*.
        from backend.catc import TaskCard
        card = TaskCard.from_dict({
            "jira_ticket": "STUCK-1",
            "acceptance_criteria": "criteria",
            "navigation": {
                "entry_point": "src/x.c",
                "impact_scope": {"allowed": ["src/**"], "forbidden": []},
            },
        })
        msg_id = qb.push(card, PriorityLevel.P2)
        om._register_inflight("msg-stuck", DispatchRequest(user_command="x"), "X-1")
        om._unregister_inflight("msg-stuck")
        om._register_inflight(msg_id, DispatchRequest(user_command="x"), "STUCK-1")

        rc = orchestration_drain.main(
            ["--strategy", "wait", "--wait-s", "0.05"],
        )
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        # Either drained (if InMemory treated it as absent) or still_pending
        # (if message actually sat in Queued) — both count as valid drain
        # results.  What matters: exit code reflects still_pending.
        if payload["still_pending"]:
            assert rc == 2
        else:
            assert rc == 0


# ──────────────────────────────────────────────────────────────
#  7. Misc — config knobs, list_inflight snapshot, synth ticket
# ──────────────────────────────────────────────────────────────


class TestMiscellaneous:
    def test_settings_surface_the_new_knobs(self):
        from backend.config import settings
        assert hasattr(settings, "orchestration_mode")
        assert hasattr(settings, "orchestration_distributed_wait_s")
        assert isinstance(settings.orchestration_mode, str)
        assert float(settings.orchestration_distributed_wait_s) >= 0.0

    def test_list_inflight_is_a_snapshot_copy(self):
        om._register_inflight(
            "msg-a",
            DispatchRequest(user_command="x"),
            "X-1",
        )
        snap1 = list_inflight()
        assert any(e["message_id"] == "msg-a" for e in snap1)
        om._unregister_inflight("msg-a")
        # snap1 is a copy — mutation of the registry doesn't leak back.
        assert any(e["message_id"] == "msg-a" for e in snap1)

    def test_synth_jira_ticket_matches_catc_regex(self):
        from backend.orchestration_mode import _synth_jira_ticket
        import re
        for _ in range(10):
            ticket = _synth_jira_ticket()
            assert re.match(r"^[A-Z][A-Z0-9_]*-\d+$", ticket)
            assert len(ticket) <= 64

    def test_dispatch_rejects_non_request_argument(self):
        with pytest.raises(TypeError):
            asyncio.run(dispatch({"user_command": "wrong-type"}))  # type: ignore[arg-type]
