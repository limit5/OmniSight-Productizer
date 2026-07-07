"""P5 host executor guards — the ONLY thing that runs a real host restart.

The host runs OTHER unrelated services, so the executor MUST never touch anything
that isn't one of our explicitly-named, non-critical units. These tests lock the
two independent gates (hardcoded allowlist + naming prefix) and the dry-run
default, without touching systemd or the DB (the guard logic is pure).
See scripts/p5_host_executor.py.
"""

import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "p5_host_executor",
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "p5_host_executor.py",
)
hx = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hx)


def test_allowed_units_are_exactly_our_safe_set():
    # the hardcoded set must be small + all ours; NOT env-overridable
    assert hx._ALLOWED_UNITS == frozenset({
        "omnisight-slo-monitor.service", "pipeline-coordinator.service"})


def test_unit_is_allowed_accepts_our_units():
    assert hx._unit_is_allowed("omnisight-slo-monitor.service")
    assert hx._unit_is_allowed("pipeline-coordinator.service")


def test_unit_is_allowed_refuses_everything_else():
    for bad in (
        "omnisight-productizer-backend-a-1",   # a prod container (not a user unit)
        "omnisight-pg-primary",                 # the DB
        "some-other-users-service.service",     # ANOTHER service on the shared host
        "sshd.service", "docker.service",       # system services
        "", "  ", None, 123, ["omnisight-slo-monitor.service"],
        "omnisight-slo-monitor.service ",       # trailing space → not an exact match
        "-rf", "--now",                          # flag-looking
    ):
        assert not hx._unit_is_allowed(bad), bad


def test_prefix_guard_is_independent_of_allowlist(monkeypatch):
    # even if the hardcoded allowlist were edited to include a foreign unit, the
    # prefix guard is a SECOND, independent gate that would still refuse it.
    monkeypatch.setattr(hx, "_ALLOWED_UNITS", frozenset({"totally-foreign.service"}))
    assert not hx._unit_is_allowed("totally-foreign.service")   # fails the prefix guard
    # and a name that matches the prefix but isn't in the set is still refused
    monkeypatch.setattr(hx, "_ALLOWED_UNITS", frozenset({"omnisight-slo-monitor.service"}))
    assert not hx._unit_is_allowed("omnisight-anything-else.service")


def test_sql_literal_escapes_quotes():
    assert hx._sql_lit("a'b") == "a''b"
    assert "'" not in hx._sql_lit("x'; DROP TABLE proposed_actions; --").replace("''", "")


def test_sql_literal_truncates_before_escaping():
    # audit r4: truncate the INPUT first (1000 chars) THEN escape, so we can never
    # cut an escaped '' pair in half. 1000 input quotes → 2000 output quotes (even).
    out = hx._sql_lit("'" * 2000)
    assert out == "''" * 1000
    assert len(out) % 2 == 0          # even → a valid single-quoted literal, no half pair


def test_container_and_host_allowlists_match():
    # audit r4: the container's DEFAULT allowlist must equal the host's hardcoded
    # set, so nothing gets deferred that the host will only refuse.
    from backend.agents import action_executor as ex
    assert ex._DEFAULT_RESTART_ALLOWLIST == hx._ALLOWED_UNITS


import json as _json


def _mock_select(rows):
    def _psql(sql):
        if "json_agg" in sql:
            return _json.dumps(rows)
        return ""
    return _psql


def test_dry_run_makes_no_db_writes(monkeypatch):
    calls = []
    def _psql(sql):
        calls.append(sql)
        return _json.dumps([{"id": "pa-1", "params": '{"service":"omnisight-slo-monitor.service"}'},
                            {"id": "pa-2", "params": '{"service":"sshd.service"}'}]) if "json_agg" in sql else ""
    monkeypatch.setattr(hx, "_psql", _psql)
    hx.process(execute=False)
    assert all("UPDATE" not in c for c in calls)   # dry-run: SELECT only, zero writes


def test_execute_claims_before_restart(monkeypatch):
    order = []
    def _psql(sql):
        if "stranded" in sql:                    # the reaper — not part of this flow
            return ""
        if "json_agg" in sql:
            return _json.dumps([{"id": "pa-1", "params": '{"service":"omnisight-slo-monitor.service"}'}])
        if "SET status='host_running'" in sql:   # the atomic claim
            order.append("claim"); return "pa-1"  # won
        if "UPDATE" in sql:
            order.append("finish"); return ""
        return ""
    monkeypatch.setattr(hx, "_psql", _psql)
    monkeypatch.setattr(hx, "_restart", lambda u: (order.append(f"restart:{u}") or (True, "rc=0")))
    hx.process(execute=True)
    assert order == ["claim", "restart:omnisight-slo-monitor.service", "finish"]  # claim BEFORE side effect


def test_lost_claim_does_not_restart(monkeypatch):
    ran = {"n": 0}
    def _psql(sql):
        if "json_agg" in sql:
            return _json.dumps([{"id": "pa-1", "params": '{"service":"omnisight-slo-monitor.service"}'}])
        if "SET status='host_running'" in sql:   # claim LOST (another pass won)
            return ""
        return ""
    monkeypatch.setattr(hx, "_psql", _psql)
    monkeypatch.setattr(hx, "_restart", lambda u: (ran.__setitem__("n", ran["n"] + 1) or (True, "rc=0")))
    hx.process(execute=True)
    assert ran["n"] == 0            # lost the claim → NEVER restarts (no double-restart)


def test_reaper_marks_stale_host_running_failed(monkeypatch):
    # audit r5 AB-05: a stranded host_running row is reaped to 'failed (stranded)'
    calls = []
    monkeypatch.setattr(hx, "_psql", lambda sql: calls.append(sql) or "")
    hx._reap_stranded()
    assert any("SET status='failed'" in c and "host_running" in c and "stranded" in c for c in calls)


def test_execute_pass_reaps_before_taking_work(monkeypatch):
    seq = []
    def _psql(sql):
        if "stranded" in sql:
            seq.append("reap")
        elif "json_agg" in sql:
            seq.append("select"); return "[]"
        return ""
    monkeypatch.setattr(hx, "_psql", _psql)
    hx.process(execute=True)
    assert seq == ["reap", "select"]      # reaper runs BEFORE fetching new work


def test_dry_run_does_not_reap(monkeypatch):
    calls = []
    def _psql(sql):
        calls.append(sql)
        return "[]" if "json_agg" in sql else ""
    monkeypatch.setattr(hx, "_psql", _psql)
    hx.process(execute=False)
    assert not any("stranded" in c for c in calls)   # dry-run: no reaper writes


def test_embedded_tab_in_params_cannot_spoof_a_second_row(monkeypatch):
    # audit r4: JSON fetch (not hand-delimited) — a params with an embedded
    # newline/tab is ONE row with a malformed service → refused, no fake row.
    restarts = []
    def _psql(sql):
        if "json_agg" in sql:
            return _json.dumps([{"id": "pa-1", "params": '{"service":"omnisight-slo-monitor.service\\nfake-id\\t"}'}])
        return ""
    monkeypatch.setattr(hx, "_psql", _psql)
    monkeypatch.setattr(hx, "_restart", lambda u: (restarts.append(u) or (True, "rc=0")))
    hx.process(execute=True)
    assert restarts == []           # the malformed unit is refused; no real unit restarted
