"""P5 execution (block 1c) — the narrow, gated, reversible executor + watch fix.

Safety-critical: proves the executor only ever runs an ALLOWLISTED restart, and
ONLY when OMNISIGHT_P5_EXECUTE is on (default off → everything is a dry-run);
deploy/promote/rollback are never auto-run; off-allowlist restarts are refused.
Also covers the rewritten supervisor_release_status (deployed version + pending
proposals, no release_dashboard greenlet). See backend/agents/action_executor.py.
"""

import asyncio

import backend.agents.action_executor as ex
import backend.agents.tools as tools
from backend.agents.tools import supervisor_release_status


def _run(coro):
    return asyncio.run(coro)


# ── executor: default OFF → dry-run, nothing runs ─────────────────────
def test_restart_allowlisted_flag_off_is_dry_run(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_P5_EXECUTE", raising=False)
    ran = {"n": 0}
    async def _fake_restart(svc, timeout=30.0):
        ran["n"] = ran.get("n", 0) + 1
        return (True, "rc=0")
    monkeypatch.setattr(ex, "_do_restart", _fake_restart)
    action = {"action_kind": "restart", "params": '{"service":"omnisight-slo-monitor.service"}'}
    r = _run(ex.execute_approved_action(action, actor="op@x"))
    assert r["ok"] and r["dry_run"] and not r["executed"]
    assert "dry-run" in r["detail"] and "OMNISIGHT_P5_EXECUTE is off" in r["detail"]
    assert ran["n"] == 0        # the real restart was NEVER called


def test_restart_allowlisted_flag_on_executes(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_P5_EXECUTE", "1")
    ran = {"svc": None}
    async def _fake(svc, timeout=30.0):
        ran["svc"] = svc
        return True, "rc=0"
    monkeypatch.setattr(ex, "_do_restart", _fake)
    action = {"action_kind": "restart", "params": '{"service":"pipeline-coordinator.service"}'}
    r = _run(ex.execute_approved_action(action, actor="op@x"))
    assert r["ok"] and r["executed"] and not r["dry_run"]
    assert ran["svc"] == "pipeline-coordinator.service"
    assert "restarted" in r["detail"] and "op@x" in r["detail"]


def test_restart_off_allowlist_refused_even_with_flag_on(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_P5_EXECUTE", "1")
    ran = {"n": 0}
    async def _fake_restart(svc, timeout=30.0):
        ran["n"] = ran.get("n", 0) + 1
        return (True, "rc=0")
    monkeypatch.setattr(ex, "_do_restart", _fake_restart)
    # the prod backend container is NOT in the allowlist — must be refused
    action = {"action_kind": "restart", "params": '{"service":"omnisight-productizer-backend-a-1"}'}
    r = _run(ex.execute_approved_action(action, actor="op@x"))
    assert not r["ok"] and not r["executed"]
    assert "not in the restart allowlist".lower() in r["detail"].lower()
    assert ran["n"] == 0        # never touched the real restart


def test_restart_failure_reports_failed(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_P5_EXECUTE", "1")
    async def _fail(svc, timeout=30.0):
        return (False, "rc=1 stderr=Unit not found")
    monkeypatch.setattr(ex, "_do_restart", _fail)
    action = {"action_kind": "restart", "params": '{"service":"omnisight-slo-monitor.service"}'}
    r = _run(ex.execute_approved_action(action, actor="op@x"))
    assert not r["ok"] and r["executed"] and "FAILED" in r["detail"]


def test_deploy_promote_rollback_never_auto_executed(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_P5_EXECUTE", "1")   # even with execution ON
    for kind in ("deploy", "promote", "rollback"):
        r = _run(ex.execute_approved_action({"action_kind": kind, "params": "{}"}, actor="op@x"))
        assert not r["ok"] and not r["executed"]
        assert "not auto-executed" in r["detail"] and "release-train" in r["detail"]


def test_unknown_kind_refused():
    r = _run(ex.execute_approved_action({"action_kind": "nuke", "params": "{}"}, actor="op@x"))
    assert not r["ok"] and not r["executed"] and "unknown" in r["detail"].lower()


def test_restart_non_string_service_refused_not_crash(monkeypatch):
    # audit r3 EXEC-01: a Sora-seeded non-string 'service' must be a clean refusal,
    # never an unhandled crash that strands an approved proposal.
    monkeypatch.setenv("OMNISIGHT_P5_EXECUTE", "1")
    for bad in ('{"service": 123}', '{"service": null}', '{"service": ["x"]}', '{}'):
        r = _run(ex.execute_approved_action({"action_kind": "restart", "params": bad}, actor="op@x"))
        assert not r["ok"] and not r["executed"], bad
        assert "string 'service'" in r["detail"], bad


def test_allowlist_rejects_dash_leading_entries(monkeypatch):
    # audit r3 EXEC-03: a dash-leading allowlist entry (looks like a flag) is dropped
    monkeypatch.setenv("OMNISIGHT_P5_RESTART_ALLOWLIST", "-rf, good.service, --now")
    assert ex.restart_allowlist() == frozenset({"good.service"})


def test_explicit_dry_run_never_executes_even_with_flag_on(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_P5_EXECUTE", "1")
    ran = {"n": 0}
    async def _fake_restart(svc, timeout=30.0):
        ran["n"] = ran.get("n", 0) + 1
        return (True, "rc=0")
    monkeypatch.setattr(ex, "_do_restart", _fake_restart)
    action = {"action_kind": "restart", "params": '{"service":"omnisight-slo-monitor.service"}'}
    r = _run(ex.execute_approved_action(action, actor="op@x", dry_run=True))
    assert r["dry_run"] and not r["executed"] and ran["n"] == 0


def test_allowlist_env_override(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_P5_RESTART_ALLOWLIST", "only-this.service, and-this.service")
    al = ex.restart_allowlist()
    assert al == frozenset({"only-this.service", "and-this.service"})


# ── watch tool: rewritten to avoid the release_dashboard greenlet bug ──
class _FakeConn: ...
class _FakePool:
    def acquire(self):
        class _C:
            async def __aenter__(s): return _FakeConn()
            async def __aexit__(s, *a): return False
        return _C()


def test_release_status_reports_version_and_pending(monkeypatch):
    import backend.api_versioning as av
    from backend import db
    monkeypatch.setattr(av, "get_deploy_overlay",
                        lambda: {"deployed_tag": "v0.7.35", "build_git_sha": "abcdef123456", "bundle_id": "b1"})
    async def _list(conn, *, status, limit):
        assert status == "pending"
        return [{"id": "pa-1", "title": "restart: service=omnisight-slo-monitor.service", "proposed_by": "sora"}]
    monkeypatch.setattr(db, "list_proposed_actions", _list)
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    out = _run(supervisor_release_status.ainvoke({}))
    assert out.startswith("[SUPERVISOR]")
    assert "v0.7.35" in out and "abcdef123456" in out
    assert "AWAITING APPROVAL" in out and "pa-1" in out


def test_release_status_fail_open_on_overlay_error(monkeypatch):
    import backend.api_versioning as av
    from backend import db
    def _boom(): raise RuntimeError("no overlay")
    monkeypatch.setattr(av, "get_deploy_overlay", _boom)
    async def _list(conn, *, status, limit): return []
    monkeypatch.setattr(db, "list_proposed_actions", _list)
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    out = _run(supervisor_release_status.ainvoke({}))
    # per-section degradation: version line notes the error, tool still returns [SUPERVISOR]
    assert out.startswith("[SUPERVISOR]") and "version unavailable" in out
