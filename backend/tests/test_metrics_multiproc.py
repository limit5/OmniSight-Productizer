"""OP-2562 — multiprocess-safe Prometheus exposition (metrics-floor accuracy).

Covers the ticket's offline Code-AC + Integration-AC:

  (a) env UNSET  → render_exposition() is byte-identical to the classic
      single-registry render (the fail-safe / dormant-ship invariant).
  (b) env SET    → render_exposition() returns a MultiProcessCollector
      render without raising.
  (c) a Gauge with an explicit multiprocess_mode is exported WITHOUT the
      per-pid series explosion (contrasted against a default-'all' Gauge,
      which does explode — that is exactly what the annotations prevent).
  (d) mark_process_dead is invoked on lifespan-shutdown when the env is
      set and NOT invoked when unset (spy), plus an AST check that
      backend/main.py wires the hook AFTER the lifespan `yield`.
  (e) every Gauge in backend.metrics carries an explicitly-chosen
      multiprocess_mode != 'all' (a bare Gauge DEFAULTS to 'all', so the
      guard must reject 'all', not merely require the attribute) — in
      BOTH the import-time block and the reset_for_tests() rebind block.
  (integration) the cross-worker SUM proof at the prometheus_client
      level: two simulated worker PIDs write 3 and 4 to the same counter
      → the multiproc render reports 7; the same driver with env UNSET
      falls back to the single-registry render (values do NOT sum).

Import-timing invariant (ticket Goal #2): prometheus_client selects its
value-storage backend once per metric CONSTRUCTION via values.ValueClass,
so these tests monkeypatch ``values.ValueClass = values.MultiProcessValue(...)``
under a monkeypatched PROMETHEUS_MULTIPROC_DIR *before* building the
metrics they inspect — mirroring what "env present at process launch"
guarantees in prod.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from backend import metrics as m

prom_only = pytest.mark.skipif(
    not m.is_available(), reason="prometheus_client not installed"
)

ENV = "PROMETHEUS_MULTIPROC_DIR"


def _unset_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def _write_as_pid(fake_pid: int):
    """Point values.ValueClass at a per-PID mmap writer for `fake_pid`."""
    from prometheus_client import values

    values.ValueClass = values.MultiProcessValue(lambda: fake_pid)


@pytest.fixture
def mp_env(tmp_path, monkeypatch):
    """PROMETHEUS_MULTIPROC_DIR set to a fresh tmp dir; ValueClass restored."""
    from prometheus_client import values

    monkeypatch.setenv(ENV, str(tmp_path))
    original = values.ValueClass
    yield tmp_path
    values.ValueClass = original


# ── AC (a): fail-safe — env unset means byte-identical output ────────


@prom_only
def test_env_unset_is_byte_identical_to_single_registry_render(monkeypatch):
    _unset_env(monkeypatch)
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    golden = generate_latest(m.REGISTRY)
    body, content_type = m.render_exposition()
    assert body == golden
    assert content_type == CONTENT_TYPE_LATEST


@prom_only
def test_env_unset_never_imports_multiprocess(monkeypatch):
    # The dormant path must not even touch prometheus_client.multiprocess.
    _unset_env(monkeypatch)
    import builtins

    real_import = builtins.__import__

    def guard(name, *a, **kw):
        assert "multiprocess" not in name, (
            f"dormant path imported {name!r}"
        )
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", guard)
    m.render_exposition()


# ── AC (b): env set → MultiProcessCollector render, no raise ─────────


@prom_only
def test_env_set_renders_multiprocess_collector(mp_env):
    from prometheus_client import CONTENT_TYPE_LATEST, Counter

    _write_as_pid(101)
    Counter("op2562_ac_b_demo", "written under the multiproc value class",
            registry=None).inc(5)

    body, content_type = m.render_exposition()
    assert content_type == CONTENT_TYPE_LATEST
    # The render comes from the per-PID db files, not the global REGISTRY.
    assert b"op2562_ac_b_demo_total 5.0" in body


# ── AC (c): explicit multiprocess_mode avoids per-pid explosion ──────


@prom_only
def test_explicit_mode_gauge_has_no_per_pid_series(mp_env):
    from prometheus_client import Gauge

    for fake_pid, value in ((111, 1.0), (222, 2.0)):
        _write_as_pid(fake_pid)
        Gauge("op2562_ac_c_annotated", "explicit mode",
              multiprocess_mode="livemostrecent", registry=None).set(value)
        Gauge("op2562_ac_c_bare", "default mode 'all'",
              registry=None).set(value)

    body = m.render_exposition()[0].decode()
    annotated = [ln for ln in body.splitlines()
                 if ln.startswith("op2562_ac_c_annotated")]
    bare = [ln for ln in body.splitlines()
            if ln.startswith("op2562_ac_c_bare")]
    # One aggregated series, no pid label — the annotation works.
    assert len(annotated) == 1 and 'pid="' not in annotated[0]
    # The un-annotated control DOES explode into per-pid series — this is
    # exactly the failure mode AC(e) guards every real Gauge against.
    assert len(bare) == 2 and all('pid="' in ln for ln in bare)


# ── AC (d): mark_process_dead on lifespan-shutdown, env-gated ────────


@prom_only
def test_worker_shutdown_marks_process_dead_when_env_set(mp_env, monkeypatch):
    import prometheus_client.multiprocess as mp_mod

    calls: list[int] = []
    monkeypatch.setattr(mp_mod, "mark_process_dead",
                        lambda pid, path=None: calls.append(pid))
    m.multiprocess_worker_shutdown()
    assert calls == [os.getpid()]


@prom_only
def test_worker_shutdown_noop_when_env_unset(monkeypatch):
    import prometheus_client.multiprocess as mp_mod

    _unset_env(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(mp_mod, "mark_process_dead",
                        lambda pid, path=None: calls.append(pid))
    m.multiprocess_worker_shutdown()
    assert calls == []


def test_lifespan_shutdown_wires_worker_cleanup():
    """backend/main.py must call multiprocess_worker_shutdown AFTER yield.

    Pure-AST check (no backend.main import — its lifespan needs a live DB
    pool, which is out of scope for the offline runner gate).
    """
    source = (Path(__file__).resolve().parent.parent / "main.py").read_text()
    tree = ast.parse(source)
    lifespan = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    yield_lines = [n.lineno for n in ast.walk(lifespan)
                   if isinstance(n, ast.Yield)]
    assert yield_lines, "lifespan lost its yield?"
    hook_calls = [
        n.lineno for n in ast.walk(lifespan)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "multiprocess_worker_shutdown"
    ]
    assert hook_calls, "lifespan no longer calls multiprocess_worker_shutdown"
    assert min(hook_calls) > max(yield_lines), (
        "multiprocess_worker_shutdown must run in the SHUTDOWN block "
        "(after the lifespan yield)"
    )


# ── AC (e): every Gauge carries an explicitly-chosen mode ────────────


def _assert_all_gauges_annotated():
    gauges = {name: obj for name, obj in vars(m).items()
              if isinstance(obj, m.Gauge)}
    # Shape guard: if this ever drops, the module-introspection sweep is
    # no longer seeing the real registry block.
    # 48 -> 52: U6-8 added 4 memory-tier liveness gauges (l2 summaries
    # count/age, l3 fact-by-state, l3 expired-promoted backlog).
    assert len(gauges) == 52, f"expected 52 module-level Gauges, got {len(gauges)}"
    offenders = {name: g._multiprocess_mode for name, g in gauges.items()
                 if g._multiprocess_mode == "all"}
    assert not offenders, (
        "Gauges without an explicitly-chosen multiprocess_mode (a bare "
        f"Gauge defaults to 'all' → per-pid series explosion): {offenders}"
    )


@prom_only
def test_every_gauge_has_explicit_multiprocess_mode():
    _assert_all_gauges_annotated()


@prom_only
def test_every_gauge_has_explicit_mode_after_reset_for_tests():
    # The reset_for_tests() rebind block must stay in lockstep with the
    # import-time block.
    m.reset_for_tests()
    _assert_all_gauges_annotated()


@prom_only
def test_process_start_time_uses_min():
    assert m.process_start_time._multiprocess_mode == "min"


# ── Integration-AC: cross-worker SUM proof (offline, no server) ──────


@prom_only
def test_two_workers_counter_sums_across_pids(mp_env):
    from prometheus_client import Counter

    for fake_pid, inc in ((101, 3), (202, 4)):
        _write_as_pid(fake_pid)
        Counter("op2562_integration_demo", "two simulated workers",
                registry=None).inc(inc)

    body = m.render_exposition()[0].decode()
    lines = [ln for ln in body.splitlines()
             if ln.startswith("op2562_integration_demo_total")]
    # ONE aggregated sample summing both workers — 3 + 4 → 7, not one
    # worker's value (the metrics-floor undercount this ticket fixes).
    assert lines == ["op2562_integration_demo_total 7.0"]


@prom_only
def test_same_driver_env_unset_returns_single_registry_render(tmp_path,
                                                              monkeypatch):
    from prometheus_client import generate_latest, values

    _unset_env(monkeypatch)
    original = values.ValueClass
    try:
        # Same two-writer driver, but with the env unset the db files are
        # never consulted: render_exposition() must return the plain
        # single-registry render and the demo counter must NOT appear.
        monkeypatch.setenv(ENV, str(tmp_path))
        from prometheus_client import Counter

        for fake_pid, inc in ((101, 3), (202, 4)):
            _write_as_pid(fake_pid)
            Counter("op2562_unset_demo", "ignored in single mode",
                    registry=None).inc(inc)
    finally:
        values.ValueClass = original
    monkeypatch.delenv(ENV)

    body, _ = m.render_exposition()
    assert body == generate_latest(m.REGISTRY)
    assert b"op2562_unset_demo" not in body
