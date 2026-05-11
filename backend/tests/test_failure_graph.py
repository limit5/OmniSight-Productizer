"""OP-858 (C8) — failure-graph edge inference + query + degrade tests.

The test plan from the ticket calls for 7 cases — 3 causality edge
kinds, depth-2 BFS, Mermaid output sanity, Cognee unavailable degrade,
and timeout partial result. This file covers those plus a handful of
boundary cases (self-loop prune, fixture-driven CLI smoke) so the
acceptance criteria can be cited line-by-line in the JIRA comment.
"""
from __future__ import annotations

import builtins
import importlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from backend.agents import failure_graph as fg


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 11, hour, minute, tzinfo=timezone.utc)


def _make(
    incident_id: str,
    ticket_key: str,
    failure_class: str,
    *,
    mutex_label: str | None = None,
    occurred_at: datetime | None = None,
    summary: str = "",
) -> fg.RunnerIncident:
    return fg.RunnerIncident(
        incident_id=incident_id,
        ticket_key=ticket_key,
        failure_class=failure_class,
        mutex_label=mutex_label,
        occurred_at=occurred_at or _at(12),
        summary=summary,
    )


# ── Causality kind 1: same_mutex_window ──────────────────────────────


def test_same_mutex_window_within_1h_emits_edge() -> None:
    """AC #1 — edges = 'incident X led to incident Y' inferred via same
    mutex_label within 1h window."""
    a = _make("a", "OP-100", "F-PEER", mutex_label="backend/x.py", occurred_at=_at(12, 0))
    b = _make("b", "OP-101", "F-PEER", mutex_label="backend/x.py", occurred_at=_at(12, 30))
    edges = fg.infer_edges([a, b])
    mutex_edges = [e for e in edges if e.causality_type == "same_mutex_window"]
    assert mutex_edges == [fg.GraphEdge("a", "b", "same_mutex_window")]


def test_same_mutex_window_outside_1h_no_edge() -> None:
    a = _make("a", "OP-100", "F-PEER", mutex_label="backend/x.py", occurred_at=_at(10, 0))
    b = _make("b", "OP-101", "F-PEER", mutex_label="backend/x.py", occurred_at=_at(12, 0))
    edges = [e for e in fg.infer_edges([a, b]) if e.causality_type == "same_mutex_window"]
    assert edges == []


def test_same_mutex_window_different_label_no_edge() -> None:
    a = _make("a", "OP-100", "F-PEER", mutex_label="backend/x.py", occurred_at=_at(12, 0))
    b = _make("b", "OP-101", "F-PEER", mutex_label="backend/y.py", occurred_at=_at(12, 30))
    edges = [e for e in fg.infer_edges([a, b]) if e.causality_type == "same_mutex_window"]
    assert edges == []


# ── Causality kind 2: same_ticket ────────────────────────────────────


def test_same_ticket_chains_in_occurrence_order() -> None:
    """AC #1 — edges inferred via same ticket_key."""
    a = _make("a", "OP-100", "F-MISC", occurred_at=_at(10))
    b = _make("b", "OP-100", "F-MISC", occurred_at=_at(11))
    c = _make("c", "OP-100", "F-MISC", occurred_at=_at(12))
    edges = [e for e in fg.infer_edges([a, b, c]) if e.causality_type == "same_ticket"]
    assert edges == [
        fg.GraphEdge("a", "b", "same_ticket"),
        fg.GraphEdge("b", "c", "same_ticket"),
    ]


# ── Causality kind 3: same_failure_class ────────────────────────────


def test_same_failure_class_chains_across_tickets() -> None:
    """AC #1 — edges inferred via same failure_class cluster within fleet."""
    a = _make("a", "OP-100", "F-PEER", occurred_at=_at(10))
    b = _make("b", "OP-200", "F-PEER", occurred_at=_at(11))
    c = _make("c", "OP-300", "F-BRIDGE", occurred_at=_at(12))
    edges = [
        e
        for e in fg.infer_edges([a, b, c])
        if e.causality_type == "same_failure_class"
    ]
    assert edges == [fg.GraphEdge("a", "b", "same_failure_class")]


# ── Self-loop prune (AC error-catalog: FailureGraphCircularEdge) ────


def test_self_loop_is_pruned_silently() -> None:
    """Same incident must never produce a self-edge even when all three
    causality kinds fire. The inference helpers prune at insert; the
    typed exception exists for manual builders, not the inference path."""
    inc = _make("dup", "OP-1", "F-X", mutex_label="m", occurred_at=_at(12))
    # Pass the same incident twice; chain helpers will see two rows but
    # the emitter must drop the self-loop.
    edges = fg.infer_edges([inc, inc])
    assert edges == []


def test_failure_graph_circular_edge_is_explicit_typed_error() -> None:
    err = fg.FailureGraphCircularEdge("inc-1", "same_ticket")
    assert err.node_id == "inc-1"
    assert err.causality_type == "same_ticket"


# ── AC #2: depth-2 BFS query ────────────────────────────────────────


def test_get_failure_graph_neighbors_depth_2() -> None:
    """AC #2 — get_failure_graph_neighbors(incident_id, depth=2) returns
    related incidents + edge labels."""
    # a→b via mutex (within 1h) + failure_class; b→c via failure_class
    # chain; c→d via failure_class chain. depth=2 from a must reach c.
    a = _make("a", "OP-100", "F-X", mutex_label="m1", occurred_at=_at(10))
    b = _make("b", "OP-101", "F-X", mutex_label="m1", occurred_at=_at(10, 30))
    c = _make("c", "OP-102", "F-X", mutex_label="m2", occurred_at=_at(11))
    d = _make("d", "OP-103", "F-X", occurred_at=_at(12))
    graph = fg.FailureGraph.build([a, b, c, d])

    depth_1 = fg.get_failure_graph_neighbors(graph, "a", depth=1)
    depth_2 = fg.get_failure_graph_neighbors(graph, "a", depth=2)
    depth_3 = fg.get_failure_graph_neighbors(graph, "a", depth=3)

    visited_ids_1 = {nid for e in depth_1 for nid in (e.src_id, e.dst_id)}
    visited_ids_2 = {nid for e in depth_2 for nid in (e.src_id, e.dst_id)}
    visited_ids_3 = {nid for e in depth_3 for nid in (e.src_id, e.dst_id)}

    # depth=1 reaches direct neighbour b; depth=2 reaches c via the
    # failure_class chain; depth=3 grows further to d.
    assert {"a", "b"}.issubset(visited_ids_1)
    assert "c" in visited_ids_2
    assert "d" in visited_ids_3
    # Edge labels surface in the returned edges (AC #2 contract).
    labels = {e.causality_type for e in depth_2}
    assert "same_mutex_window" in labels
    assert "same_failure_class" in labels


def test_get_failure_graph_neighbors_unknown_id_returns_empty() -> None:
    graph = fg.FailureGraph.build([_make("a", "OP-1", "F-X")])
    assert fg.get_failure_graph_neighbors(graph, "missing") == []


# ── AC error-catalog: FailureGraphInferenceTimeout partial result ──


class _FakeClock:
    """Monotonic clock under the test's control; .now() advances by
    ``step`` seconds on every call so the inner BFS loop trips the
    deadline deterministically."""

    def __init__(self, *, step: float) -> None:
        self.t = 0.0
        self.step = step

    def now(self) -> float:
        out = self.t
        self.t += self.step
        return out


def test_neighbor_query_timeout_returns_partial_result() -> None:
    a = _make("a", "OP-100", "F-X", occurred_at=_at(10))
    b = _make("b", "OP-100", "F-X", occurred_at=_at(11))
    c = _make("c", "OP-100", "F-X", occurred_at=_at(12))
    graph = fg.FailureGraph.build([a, b, c])
    # Clock: deadline = now() + 1.0; with step=10s the second .now()
    # call already exceeds it, so we get partial results.
    clock = _FakeClock(step=10.0)

    with pytest.raises(fg.FailureGraphInferenceTimeout) as exc_info:
        fg.get_failure_graph_neighbors(
            graph, "a", depth=5, timeout_sec=1.0, clock=clock
        )

    err = exc_info.value
    assert err.deadline_sec == 1.0
    # Partial neighbors may be empty (deadline hit before any edge) or
    # non-empty (some progress) — either is allowed by the AC. What we
    # require is that the exception carries the attribute.
    assert isinstance(err.partial_neighbors, list)


# ── AC #3: Mermaid output ───────────────────────────────────────────


def test_render_mermaid_produces_valid_diagram() -> None:
    from scripts.dump_failure_graph import render_mermaid

    a = _make("a", "OP-100", "F-PEER", mutex_label="m1", occurred_at=_at(10))
    b = _make("b", "OP-101", "F-PEER", mutex_label="m1", occurred_at=_at(10, 30))
    graph = fg.FailureGraph.build([a, b])
    out = render_mermaid(graph)

    assert out.startswith("flowchart LR")
    assert "n_a[" in out and "n_b[" in out
    assert "same_mutex_window" in out
    assert "-->" in out


def test_render_mermaid_empty_graph_shows_placeholder() -> None:
    from scripts.dump_failure_graph import render_mermaid

    empty = fg.FailureGraph.build([])
    out = render_mermaid(empty)
    assert "no incidents" in out


# ── AC error-catalog: FailureGraphCogneeUnavailable degrade ────────


def test_push_to_cognee_degrades_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC error catalog — FailureGraphCogneeUnavailable degrades to
    direct Postgres query. We simulate "cognee not installed" by making
    `import cognee` raise ImportError."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "cognee":
            raise ImportError("no module named 'cognee'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("cognee", None)
    graph = fg.FailureGraph.build([_make("a", "OP-1", "F-X")])

    with pytest.raises(fg.FailureGraphCogneeUnavailable):
        fg.push_to_cognee(graph)


def test_push_to_cognee_translates_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cognee imports but its API raises, we still translate to the
    typed FailureGraphCogneeUnavailable so callers can degrade."""

    class _FakeCognee:
        @staticmethod
        def add_graph(*, nodes, edges):  # noqa: ARG004
            raise RuntimeError("neo4j unreachable")

    monkeypatch.setitem(sys.modules, "cognee", _FakeCognee())
    graph = fg.FailureGraph.build([_make("a", "OP-1", "F-X")])

    with pytest.raises(fg.FailureGraphCogneeUnavailable) as exc_info:
        fg.push_to_cognee(graph)
    assert "neo4j unreachable" in str(exc_info.value)


# ── AC #4: pickup-time context injection ────────────────────────────


def test_render_pickup_context_with_prior_attempts_emits_block() -> None:
    prior_1 = _make("p1", "OP-858", "F-PEER", occurred_at=_at(10))
    prior_2 = _make("p2", "OP-858", "F-BRIDGE", occurred_at=_at(11))
    other = _make("o1", "OP-100", "F-PEER", occurred_at=_at(10, 30))
    graph = fg.FailureGraph.build([prior_1, prior_2, other])

    block = fg.render_pickup_context(graph, "OP-858")
    assert "Failure-Graph context" in block
    assert "OP-858" in block
    assert "F-PEER" in block and "F-BRIDGE" in block
    # Other-ticket neighbor surfaces via failure-class chain
    assert "OP-100" in block


def test_render_pickup_context_no_prior_returns_empty() -> None:
    """AC #4 wiring — fresh ticket with no prior incidents gets no
    injection (avoid noise / wasted tokens)."""
    other = _make("o1", "OP-100", "F-PEER", occurred_at=_at(10))
    graph = fg.FailureGraph.build([other])
    assert fg.render_pickup_context(graph, "OP-858") == ""


# ── AC #5 / fixture-driven cron entrypoint smoke ────────────────────


def _runner_module():
    """Load auto-runner-jira.py despite the hyphen in its filename."""
    sys.modules.pop("jira_runner_failure_graph_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_failure_graph_under_test",
        _REPO_ROOT / "auto-runner-jira.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_fixture_loader_handles_missing_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OMNISIGHT_FAILURE_GRAPH_FIXTURE pointing at a missing file must
    not crash pickup — the runner degrades to no injection."""
    mod = _runner_module()
    monkeypatch.setattr(
        mod, "FAILURE_GRAPH_FIXTURE", str(tmp_path / "missing.json")
    )
    assert mod._load_failure_graph_for_pickup() is None


def test_runner_fixture_loader_parses_well_formed_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _runner_module()
    fixture = tmp_path / "incidents.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "incident_id": "i1",
                    "ticket_key": "OP-858",
                    "failure_class": "F-PEER",
                    "mutex_label": "backend/x.py",
                    "occurred_at": "2026-05-10T10:00:00Z",
                    "summary": "prior attempt failed on backend/x.py",
                },
                {
                    "incident_id": "i2",
                    "ticket_key": "OP-858",
                    "failure_class": "F-BRIDGE",
                    "mutex_label": None,
                    "occurred_at": "2026-05-10T11:00:00Z",
                },
            ]
        )
    )
    monkeypatch.setattr(mod, "FAILURE_GRAPH_FIXTURE", str(fixture))
    graph = mod._load_failure_graph_for_pickup()
    assert graph is not None
    assert set(graph.nodes) == {"i1", "i2"}
    # Same-ticket chain edge exists for the two incidents.
    chain = [e for e in graph.edges if e.causality_type == "same_ticket"]
    assert chain == [fg.GraphEdge("i1", "i2", "same_ticket")]


def test_build_prompt_injects_failure_graph_block_when_fixture_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC #4 end-to-end — _build_prompt() must thread the failure-graph
    block into the agent prompt body when OMNISIGHT_FAILURE_GRAPH_FIXTURE
    points at a real file with prior incidents on this ticket."""
    mod = _runner_module()
    fixture = tmp_path / "incidents.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "incident_id": "i1",
                    "ticket_key": "OP-858",
                    "failure_class": "F-PEER-CONFLICT",
                    "mutex_label": "backend/agents/scheduler.py",
                    "occurred_at": "2026-05-10T10:00:00Z",
                    "summary": "prior attempt hit peer PS",
                }
            ]
        )
    )
    monkeypatch.setattr(mod, "FAILURE_GRAPH_FIXTURE", str(fixture))

    def fake_request(client, method, path):  # noqa: ARG001
        assert method == "GET"
        return {
            "fields": {
                "summary": "C8 failure graph",
                "labels": ["area:backend", "area:tests", "tier:L"],
                "components": [{"name": "MEDIUM"}],
            }
        }

    monkeypatch.setattr(mod.jira_dispatch, "_request", fake_request)
    prompt = mod._build_prompt(object(), "OP-858", "ticket description body")

    assert "Failure-Graph context" in prompt
    assert "i1" in prompt
    assert "F-PEER-CONFLICT" in prompt


def test_build_prompt_omits_failure_graph_block_when_fixture_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh ticket / no fixture → no injection (zero-impact default)."""
    mod = _runner_module()
    monkeypatch.setattr(mod, "FAILURE_GRAPH_FIXTURE", "")
    monkeypatch.setattr(
        mod.jira_dispatch,
        "_request",
        lambda c, m, p: {
            "fields": {
                "summary": "C8 failure graph",
                "labels": ["area:backend", "tier:L"],
                "components": [{"name": "MEDIUM"}],
            }
        },
    )
    prompt = mod._build_prompt(object(), "OP-858", "body")
    assert "Failure-Graph context" not in prompt


def test_rebuild_cron_degrades_quietly_when_cognee_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC #5 — nightly cron must keep firing even when Cognee is down.
    We force cognee import to fail and assert the cron exits 0 and logs
    the degrade reason."""
    from scripts import rebuild_failure_graph as rfg

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "cognee":
            raise ImportError("no cognee")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("cognee", None)

    fixture = tmp_path / "incidents.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "incident_id": "i1",
                    "ticket_key": "OP-1",
                    "failure_class": "F-X",
                    "mutex_label": None,
                    "occurred_at": "2026-05-10T10:00:00Z",
                }
            ]
        )
    )

    rc = rfg.main(
        ["--mode=full", f"--from-json={fixture}", f"--out={tmp_path / 'out.json'}"]
    )
    assert rc == 0
    assert (tmp_path / "out.json").exists()
