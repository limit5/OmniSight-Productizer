"""R2 (#308) — Semantic Entropy Monitor tests.

Covers the pure-Python math primitives, the pluggable embedding backend,
the rolling-window state machine, and the integration with the event
bus + debug blackboard. The lexical fallback is used throughout so the
suite runs without needing ``sentence-transformers`` installed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import events, semantic_entropy as se
from backend.finding_types import FindingType


@pytest.fixture(autouse=True)
def _reset_between_tests():
    """Each test starts with a clean monitor + lexical embedder."""
    se.reset_for_tests()
    yield
    se.reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure math primitives
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCosine:

    def test_identical_vectors_have_cosine_1(self):
        assert se._cosine([1.0, 0.0, 1.0], [1.0, 0.0, 1.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_have_cosine_0(self):
        assert se._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector_is_safe(self):
        assert se._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert se._cosine([], [1.0]) == 0.0

    def test_different_lengths_truncated(self):
        # Falls back to the shorter vector length — used when the
        # lexical vocab grows between embed calls.
        assert se._cosine([1.0, 1.0, 1.0], [1.0, 1.0]) == pytest.approx(1.0)


class TestPairwise:

    def test_single_vector_returns_zero(self):
        assert se.pairwise_similarity_mean([[1.0, 0.0]]) == 0.0

    def test_identical_rolls_up_to_1(self):
        v = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        assert se.pairwise_similarity_mean(v) == pytest.approx(1.0)

    def test_mixed_averages_pairs(self):
        v = [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]
        # pairs: (1,2)=0, (1,3)=1, (2,3)=0 → mean = 1/3
        assert se.pairwise_similarity_mean(v) == pytest.approx(1.0 / 3)


class TestClassify:

    def test_thresholds_map_correctly(self):
        assert se.classify(0.0) == "ok"
        assert se.classify(0.49) == "ok"
        assert se.classify(0.50) == "warning"
        assert se.classify(0.69) == "warning"
        assert se.classify(0.70) == "deadlock"
        assert se.classify(0.95) == "deadlock"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Lexical embedder (default fallback)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLexicalEmbed:

    def test_same_words_different_order_are_close(self):
        vecs = se.lexical_embed(["fix the bug", "the bug fix"])
        assert se._cosine(vecs[0], vecs[1]) == pytest.approx(1.0)

    def test_disjoint_docs_are_far_apart(self):
        vecs = se.lexical_embed(["resolve compilation error", "ship the release"])
        assert se._cosine(vecs[0], vecs[1]) == 0.0

    def test_partial_overlap_is_between(self):
        vecs = se.lexical_embed(["fix the bug", "fix the test"])
        sim = se._cosine(vecs[0], vecs[1])
        assert 0.0 < sim < 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pluggable embedder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmbedderInjection:

    def test_set_embedder_is_used_by_monitor(self):
        called = {"n": 0}

        def fake_embed(texts):
            called["n"] += 1
            # Deterministic: all vectors identical so similarity=1.
            return [[1.0, 1.0] for _ in texts]

        se.set_embedder(fake_embed)
        mon = se.SemanticEntropyMonitor(check_every_n=1)
        mon.ingest("a1", "first output", force_check=True)
        mon.ingest("a1", "second output", force_check=True)
        assert called["n"] > 0
        snap = mon.snapshot_agent("a1")
        assert snap is not None
        assert snap["entropy_score"] == pytest.approx(1.0)

    def test_reset_restores_lexical(self):
        se.set_embedder(lambda ts: [[0.0] for _ in ts])
        se.set_embedder(None)
        # Default embedder should now be the lexical one again.
        assert se.get_embedder() is se.lexical_embed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rolling window / ingest
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIngest:

    def test_single_output_does_not_compute(self):
        mon = se.SemanticEntropyMonitor(check_every_n=1)
        assert mon.ingest("a1", "hello", force_check=True) is None
        # Snapshot exists but no score yet.
        snap = mon.snapshot_agent("a1")
        assert snap is not None
        assert snap["entropy_score"] == 0.0

    def test_every_n_gating(self):
        mon = se.SemanticEntropyMonitor(check_every_n=3)
        assert mon.ingest("a1", "o1") is None
        assert mon.ingest("a1", "o2") is None
        assert mon.ingest("a1", "o3") is not None
        assert mon.ingest("a1", "o4") is None
        assert mon.ingest("a1", "o5") is None
        assert mon.ingest("a1", "o6") is not None

    def test_empty_output_is_noop(self):
        mon = se.SemanticEntropyMonitor(check_every_n=1)
        assert mon.ingest("a1", "") is None
        assert mon.ingest("a1", None) is None  # type: ignore[arg-type]

    def test_rolling_window_trims(self):
        mon = se.SemanticEntropyMonitor(window_size=3, check_every_n=1)
        for t in ["alpha", "beta", "gamma", "delta"]:
            mon.ingest("a1", t, force_check=True)
        snap = mon.snapshot_agent("a1")
        assert len(snap["recent_outputs"]) == 3
        assert snap["recent_outputs"][0] == "beta"

    def test_highest_entropy_picks_most_repetitive_agent(self):
        mon = se.SemanticEntropyMonitor(check_every_n=1)
        # Identical outputs → similarity = 1.0 for this agent.
        for _ in range(3):
            mon.ingest("stuck", "resolve the bug in driver.c", force_check=True)
        # Varied outputs → low similarity for this agent.
        mon.ingest("busy", "reading driver.c", force_check=True)
        mon.ingest("busy", "running unit test", force_check=True)
        top = mon.highest_entropy()
        assert top is not None
        assert top["agent_id"] == "stuck"
        assert top["verdict"] == "deadlock"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration — SSE + debug finding + metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIntegration:

    def test_deadlock_emits_sse_and_debug_finding(self):
        q = events.bus.subscribe()
        try:
            # Stub the embedder so every run yields a dead identical score.
            se.set_embedder(lambda ts: [[1.0, 0.0] for _ in ts])
            for i in range(5):
                se.record_output("agent-deadlock", f"iter-{i}", task_id="t-42",
                                 force_check=True)

            async def drain():
                out = []
                while not q.empty():
                    out.append(await q.get())
                return out
            msgs = asyncio.run(drain())
        finally:
            events.bus.unsubscribe(q)

        entropy_events = [m for m in msgs if m["event"] == "agent.entropy"]
        debug_events = [m for m in msgs if m["event"] == "debug_finding"]
        assert entropy_events, "expected at least one agent.entropy event"
        first = json.loads(entropy_events[0]["data"])
        assert first["agent_id"] == "agent-deadlock"
        assert first["verdict"] == "deadlock"
        assert first["entropy_score"] >= first["threshold_deadlock"]

        assert debug_events, "expected at least one cognitive_deadlock finding"
        dbg = json.loads(debug_events[0]["data"])
        assert dbg["finding_type"] == FindingType.cognitive_deadlock.value
        assert dbg["agent_id"] == "agent-deadlock"
        assert dbg["task_id"] == "t-42"
        assert dbg["severity"] == "warn"

    def test_healthy_agent_emits_no_debug_finding(self):
        q = events.bus.subscribe()
        try:
            mon = se.get_monitor()
            # Distinct vectors → low similarity → "ok".
            def embed(texts):
                return [[float(i == idx) for idx in range(len(list(texts)))] for i, _ in enumerate(texts)]
            # The above closure captures len dynamically; simpler approach:
            def embed_fn(texts):
                xs = list(texts)
                return [[1.0 if i == j else 0.0 for j in range(len(xs))] for i in range(len(xs))]
            se.set_embedder(embed_fn)
            for i in range(4):
                mon.ingest("agent-ok", f"novel output {i}", force_check=True)
            async def drain():
                out = []
                while not q.empty():
                    out.append(await q.get())
                return out
            msgs = asyncio.run(drain())
        finally:
            events.bus.unsubscribe(q)

        debug_events = [m for m in msgs if m["event"] == "debug_finding"]
        assert not debug_events, "healthy agent should not trip cognitive_deadlock"

        entropy_events = [m for m in msgs if m["event"] == "agent.entropy"]
        assert entropy_events
        last = json.loads(entropy_events[-1]["data"])
        assert last["verdict"] == "ok"

    def test_agent_update_feeds_monitor_automatically(self):
        """emit_agent_update with thought_chain should trigger an entropy
        measurement after enough rounds — the monitor is wired into the
        default agent event path so callers don't need to know about it.
        """
        # Force-check on every call so we don't need 3 rounds.
        mon = se.get_monitor()
        mon.check_every_n = 1
        se.set_embedder(lambda ts: [[1.0, 0.0] for _ in ts])

        events.emit_agent_update("a1", "running", "trying to fix the bug")
        events.emit_agent_update("a1", "running", "still trying to fix the bug")
        events.emit_agent_update("a1", "running", "still fixing the bug")

        snap = se.snapshot_agent("a1")
        assert snap is not None
        assert snap["entropy_score"] > 0.0

    def test_highest_entropy_exposes_top_score(self):
        se.set_embedder(lambda ts: [[1.0, 0.0] for _ in ts])
        for _ in range(3):
            se.record_output("hot-agent", "payload", force_check=True)
        top = se.highest_entropy_agent()
        assert top is not None
        assert top["agent_id"] == "hot-agent"
        assert top["verdict"] == "deadlock"
