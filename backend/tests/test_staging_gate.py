"""[OP-965] AUDIT-17 — tests for the continuous staging gate emitters.

Three surfaces:

1. **Contract round-trip** — a record written by ``backend.agents.staging_gate``
   must be exactly what ``scripts/release_milestone_checker.py``'s
   ``JsonlStatusReader`` + ``check_latest_status`` accept (green → gate OK,
   red → ``status_not_green``). This is the load-bearing test: it pins the
   producer to the consumer that OP-925 R3 needs.
2. **Unit** — pure record helpers, develop-tip parsing, the HTTP/smoke
   probers (with injected runner / opener / sleep), audit-sink best-effort
   semantics, the ``main`` exit codes.
3. **Systemd contract** — timer cadences, OnFailure chain, oneshot shape,
   ExecStart wiring, release-audit EnvironmentFile.

Cost: stdlib + pytest only, no network / git / ssh.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from backend.agents import staging_gate as sg

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = REPO_ROOT / "deploy" / "systemd"
SCRIPT_SHIM = REPO_ROOT / "scripts" / "staging_gate.py"
CHECKER_SCRIPT = REPO_ROOT / "scripts" / "release_milestone_checker.py"

FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"
BACKEND_DIGEST = "sha256:" + "a" * 64
FRONTEND_DIGEST = "sha256:" + "b" * 64
API_VERSION_BODY = {
    "bundle_id": "develop-01234567",
    "backend_image_digest": BACKEND_DIGEST,
    "frontend_image_digest": FRONTEND_DIGEST,
    "api_required": "v1",
}


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("rmc_for_staging_gate_test", CHECKER_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rmc_for_staging_gate_test"] = mod
    spec.loader.exec_module(mod)
    return mod


checker = _load_checker()


# ───────────────────────── contract round-trip ──────────────────────────


@pytest.mark.parametrize("suite", [sg.SUITE_CANARY, sg.SUITE_SMOKE])
def test_green_record_satisfies_milestone_checker(tmp_path: Path, suite: str) -> None:
    out = tmp_path / f"{suite}-status.jsonl"
    now = datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)
    audited: list[tuple[str, dict[str, Any]]] = []

    result = sg.run_gate(
        suite=suite,
        revision=FAKE_SHA,
        probe=lambda: (True, "all good"),
        out_path=out,
        audit_sink=lambda action, payload: audited.append((action, payload)),
        clock=lambda: now,
    )

    assert result.exit_code == 0
    assert result.record is not None and result.record.status == sg.STATUS_GREEN

    reader = checker.JsonlStatusReader([out])
    rec = reader.latest(suite, branch="develop", revision=FAKE_SHA)
    assert rec is not None
    gate = checker.check_latest_status(
        rec,
        gate=("ci_canary" if suite == sg.SUITE_CANARY else "smoke_suite"),
        revision=FAKE_SHA,
        now=now + timedelta(minutes=5),
        max_age=(None if suite == sg.SUITE_CANARY else timedelta(hours=4)),
    )
    assert gate.ok, gate.evidence
    # §6 — exactly one audit call, action namespaced per suite, payload carries
    # the same dict that hit the JSONL.
    assert len(audited) == 1
    assert audited[0][0] == sg.AUDIT_ACTION[suite]
    assert audited[0][1] == rec


def test_jsonl_record_includes_staging_evidence_from_api_version(tmp_path: Path) -> None:
    out = tmp_path / "canary-status.jsonl"
    now = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)

    result = sg.run_gate(
        suite=sg.SUITE_CANARY,
        revision=FAKE_SHA,
        probe=lambda: (True, "all good"),
        out_path=out,
        audit_sink=lambda *_a, **_k: None,
        clock=lambda: now,
        evidence_probe=lambda: {
            "bundle_id": "develop-01234567",
            "backend_digest": BACKEND_DIGEST,
            "frontend_digest": FRONTEND_DIGEST,
            "observed_api_version": dict(API_VERSION_BODY),
        },
    )

    assert result.exit_code == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["bundle_id"] == "develop-01234567"
    assert rec["backend_digest"] == BACKEND_DIGEST
    assert rec["frontend_digest"] == FRONTEND_DIGEST
    assert rec["observed_api_version"] == API_VERSION_BODY
    assert rec["status"] == "green"


def test_missing_staging_evidence_marks_gate_red(tmp_path: Path) -> None:
    out = tmp_path / "canary-status.jsonl"

    def no_evidence() -> dict[str, Any]:
        raise RuntimeError("/api/version missing backend_image_digest")

    result = sg.run_gate(
        suite=sg.SUITE_CANARY,
        revision=FAKE_SHA,
        probe=lambda: (True, "all good"),
        out_path=out,
        audit_sink=lambda *_a, **_k: None,
        evidence_probe=no_evidence,
    )

    assert result.exit_code == 2
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["status"] == "red"
    assert "staging evidence failed" in rec["detail"]


def test_red_record_blocks_the_gate(tmp_path: Path) -> None:
    out = tmp_path / "canary-status.jsonl"
    now = datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)
    result = sg.run_gate(
        suite=sg.SUITE_CANARY,
        revision=FAKE_SHA,
        probe=lambda: (False, "/readyz: HTTP 503"),
        out_path=out,
        audit_sink=lambda *_a, **_k: None,
        clock=lambda: now,
    )
    assert result.exit_code == 2
    # The red line is still written so the checker sees a non-stale red status.
    rec = checker.JsonlStatusReader([out]).latest("canary", branch="develop", revision=FAKE_SHA)
    assert rec is not None and rec["status"] == "red"
    gate = checker.check_latest_status(rec, gate="ci_canary", revision=FAKE_SHA)
    assert not gate.ok
    assert gate.evidence["code"] == "status_not_green"


def test_probe_exception_is_red_not_a_crash(tmp_path: Path) -> None:
    out = tmp_path / "smoke-status.jsonl"

    def boom() -> tuple[bool, str]:
        raise RuntimeError("kaboom")

    result = sg.run_gate(
        suite=sg.SUITE_SMOKE, revision=FAKE_SHA, probe=boom, out_path=out,
        audit_sink=lambda *_a, **_k: None,
    )
    assert result.exit_code == 2
    assert "kaboom" in (result.record.detail if result.record else "")


def test_audit_sink_failure_does_not_break_the_run(tmp_path: Path) -> None:
    out = tmp_path / "canary-status.jsonl"

    def bad_sink(_action: str, _payload: dict[str, Any]) -> None:
        raise OSError("db down")

    result = sg.run_gate(
        suite=sg.SUITE_CANARY, revision=FAKE_SHA, probe=lambda: (True, "ok"),
        out_path=out, audit_sink=bad_sink,
    )
    assert result.exit_code == 0
    assert out.exists() and out.read_text().strip()


# ───────────────────────── record helpers ───────────────────────────────


def test_build_record_shape_and_truncation() -> None:
    now = datetime(2026, 5, 12, 1, 2, 3, tzinfo=timezone.utc)
    rec = sg.build_record(
        suite=sg.SUITE_CANARY, ok=True, detail="x" * 5000, revision=FAKE_SHA,
        run_id="rid-1", now=now,
    )
    obj = rec.to_jsonl_obj()
    assert obj == {
        "suite": "canary",
        "status": "green",
        "branch": "develop",
        "revision": FAKE_SHA,
        "run_id": "rid-1",
        "timestamp": "2026-05-12T01:02:03Z",
        "detail": "x" * 2000,
    }
    # `green` is in the checker's accepted set.
    assert obj["status"] in checker.GREEN_STATUSES
    # not-ok flips only `status`.
    assert sg.build_record(suite=sg.SUITE_SMOKE, ok=False, detail="bad", revision=FAKE_SHA, run_id="r", now=now).status == "red"


def test_api_version_evidence_extracts_digest_pair_and_observed_body() -> None:
    evidence = sg.api_version_evidence(
        base_url="https://staging.x/",
        timeout=1.0,
        opener=lambda url, _timeout: (
            200,
            json.dumps(API_VERSION_BODY | {"url_seen": url}),
        ),
    )

    assert evidence["bundle_id"] == "develop-01234567"
    assert evidence["backend_digest"] == BACKEND_DIGEST
    assert evidence["frontend_digest"] == FRONTEND_DIGEST
    assert evidence["observed_api_version"]["url_seen"] == "https://staging.x/api/version"


def test_api_version_evidence_rejects_missing_digest() -> None:
    body = dict(API_VERSION_BODY)
    body.pop("backend_image_digest")
    with pytest.raises(RuntimeError, match="backend_image_digest"):
        sg.api_version_evidence(
            base_url="https://staging.x",
            timeout=1.0,
            opener=lambda _url, _timeout: (200, json.dumps(body)),
        )


def test_iso_z_is_parseable_by_checker() -> None:
    now = datetime(2026, 5, 12, 12, 30, 45, tzinfo=timezone.utc)
    parsed = checker.parse_timestamp(sg.iso_z(now))
    assert parsed == now


def test_append_jsonl_appends_does_not_truncate(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "canary-status.jsonl"
    sg.append_jsonl(out, {"suite": "canary", "status": "red", "n": 1})
    sg.append_jsonl(out, {"suite": "canary", "status": "green", "n": 2})
    lines = out.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["n"] == 1 and json.loads(lines[1])["n"] == 2


# ───────────────────────── develop-tip resolution ───────────────────────


def _fake_runner(stdout: str = "", *, returncode: int = 0, raise_exc: Exception | None = None):
    def _run(argv, **_kw):  # noqa: ANN001
        if raise_exc is not None:
            raise raise_exc
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")
    return _run


def test_resolve_develop_tip_parses_current_patch_set() -> None:
    rows = "\n".join([
        json.dumps({"project": "p", "currentPatchSet": {"revision": FAKE_SHA}}),
        json.dumps({"type": "stats", "rowCount": 1}),
    ])
    sha = sg.resolve_develop_tip(
        runner=_fake_runner(rows), host="h@x", port=29418, key_path=None, project="omnisight/X",
    )
    assert sha == FAKE_SHA


def test_resolve_develop_tip_argv_uses_current_patch_set_flag() -> None:
    argv = sg._gerrit_query_argv(host="bot@h", port=29418, key_path=Path("/k"), query="project:X branch:develop status:merged")
    assert argv[0] == "ssh" and "--current-patch-set" in argv and "--format=JSON" in argv
    assert argv[-1] == "project:X branch:develop status:merged"
    # key path threaded through
    assert "-i" in argv and "/k" in argv


def test_resolve_develop_tip_raises_when_empty() -> None:
    with pytest.raises(RuntimeError):
        sg.resolve_develop_tip(runner=_fake_runner('{"type":"stats","rowCount":0}'), host="h", port=1, key_path=None, project="p")


# ───────────────────────── HTTP canary prober ───────────────────────────


def test_http_probe_green_when_all_paths_2xx() -> None:
    calls: list[str] = []

    def opener(url: str, _timeout: float) -> tuple[int, str]:
        calls.append(url)
        return 200, "{}"

    ok, detail = sg.http_probe(
        base_url="https://staging.x/", paths=("/healthz", "/readyz"),
        attempts=3, interval=0.0, timeout=1.0, opener=opener, sleeper=lambda _s: None,
    )
    assert ok and "2 probe(s) OK" in detail
    assert calls == ["https://staging.x/healthz", "https://staging.x/readyz"]


def test_http_probe_retries_then_succeeds() -> None:
    seq = iter([(503, ""), (503, ""), (200, "ok")])
    slept: list[float] = []
    ok, _detail = sg.http_probe(
        base_url="https://s", paths=("/readyz",), attempts=5, interval=2.0,
        timeout=1.0, opener=lambda _u, _t: next(seq), sleeper=slept.append,
    )
    assert ok
    assert slept == [2.0, 2.0]


def test_http_probe_red_when_path_never_ok() -> None:
    ok, detail = sg.http_probe(
        base_url="https://s", paths=("/healthz", "/readyz"), attempts=2, interval=0.0,
        timeout=1.0, opener=lambda _u, _t: (503, ""), sleeper=lambda _s: None,
    )
    assert not ok
    assert "/healthz: HTTP 503" in detail and "/readyz: HTTP 503" in detail


def test_http_probe_red_on_connection_error() -> None:
    def opener(_u: str, _t: float) -> tuple[int, str]:
        raise ConnectionRefusedError("nope")

    ok, detail = sg.http_probe(
        base_url="https://s", paths=("/healthz",), attempts=1, interval=0.0,
        timeout=1.0, opener=opener, sleeper=lambda _s: None,
    )
    assert not ok and "ConnectionRefusedError" in detail


# ───────────────────────── smoke prober ─────────────────────────────────


def test_smoke_probe_green_on_exit_zero() -> None:
    captured: dict[str, Any] = {}

    def runner(argv, **_kw):  # noqa: ANN001
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="all 2 DAGs passed", stderr="")

    ok, detail = sg.smoke_probe(
        base_url="https://staging.x/", subset="dag1", timeout=600, runner=runner,
        script_path=Path("/repo/scripts/prod_smoke_test.py"), python_exe="/usr/bin/python3",
    )
    assert ok and "passed" in detail
    assert captured["argv"] == ["/usr/bin/python3", "/repo/scripts/prod_smoke_test.py", "https://staging.x", "--subset", "dag1"]


def test_smoke_probe_red_on_nonzero_with_tail() -> None:
    out = "\n".join(f"line {i}" for i in range(20))

    def runner(argv, **_kw):  # noqa: ANN001
        return subprocess.CompletedProcess(argv, 2, stdout=out, stderr="boom")

    ok, detail = sg.smoke_probe(base_url="https://s", subset="both", timeout=1, runner=runner)
    assert not ok and detail.startswith("prod_smoke_test.py exit 2:")
    assert "boom" in detail and "line 19" in detail and "line 0" not in detail


def test_smoke_probe_red_on_timeout() -> None:
    def runner(argv, **_kw):  # noqa: ANN001
        raise subprocess.TimeoutExpired(argv, 5)

    ok, detail = sg.smoke_probe(base_url="https://s", subset="dag1", timeout=5, runner=runner)
    assert not ok and "timed out" in detail


# ───────────────────────── _write_audit best-effort ─────────────────────


def test_write_audit_calls_audit_log_with_release_gate_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.audit as audit_mod

    seen: dict[str, Any] = {}

    async def fake_log(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 1

    monkeypatch.setattr(audit_mod, "log", fake_log)
    sg._write_audit("release.staging_gate_canary", {"suite": "canary", "status": "green"})
    assert seen["action"] == "release.staging_gate_canary"
    assert seen["entity_kind"] == "release_gate"
    assert seen["entity_id"] == "canary"
    assert seen["actor"] == "staging_gate"
    assert seen["after"] == {"suite": "canary", "status": "green"}


def test_write_audit_swallows_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.audit as audit_mod

    async def boom(**_kwargs: Any) -> int:
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(audit_mod, "log", boom)
    # Best-effort — a dead audit DB must never raise out of the gate run.
    sg._write_audit("release.staging_gate_canary", {"suite": "canary"})


# ───────────────────────── main() exit codes ────────────────────────────


def test_main_green_with_explicit_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "canary-status.jsonl"
    monkeypatch.setattr(
        sg,
        "_default_http_opener",
        lambda url, _t: (
            200,
            json.dumps(API_VERSION_BODY) if url.endswith("/api/version") else "{}",
        ),
    )
    rc = sg.main([
        "--suite", "canary", "--revision", FAKE_SHA, "--out", str(out),
        "--base-url", "https://staging.x", "--no-audit", "--canary-attempts", "1",
        "--canary-interval", "0",
    ])
    assert rc == 0
    rec = checker.JsonlStatusReader([out]).latest("canary", branch="develop", revision=FAKE_SHA)
    assert rec is not None and rec["status"] == "green"
    assert rec["bundle_id"] == "develop-01234567"
    assert rec["backend_digest"] == BACKEND_DIGEST
    assert rec["frontend_digest"] == FRONTEND_DIGEST


def test_main_red_returns_2_and_still_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "canary-status.jsonl"
    monkeypatch.setattr(
        sg,
        "_default_http_opener",
        lambda url, _t: (
            (200, json.dumps(API_VERSION_BODY))
            if url.endswith("/api/version")
            else (503, "")
        ),
    )
    rc = sg.main([
        "--suite", "canary", "--revision", FAKE_SHA, "--out", str(out),
        "--no-audit", "--canary-attempts", "1", "--canary-interval", "0",
    ])
    assert rc == 2
    rec = checker.JsonlStatusReader([out]).latest("canary", branch="develop", revision=FAKE_SHA)
    assert rec is not None and rec["status"] == "red"


def test_main_returns_3_when_develop_tip_unresolvable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "canary-status.jsonl"

    def boom_run(argv, **_kw):  # noqa: ANN001
        raise subprocess.CalledProcessError(255, argv, stderr="ssh: connect refused")

    monkeypatch.setattr(sg.subprocess, "run", boom_run)
    rc = sg.main(["--suite", "canary", "--out", str(out), "--no-audit"])
    assert rc == 3
    assert not out.exists()  # nothing written without a known revision


def test_main_smoke_invokes_prod_smoke_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "smoke-status.jsonl"
    seen: dict[str, Any] = {}

    def fake_run(argv, **kw):  # noqa: ANN001
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(sg.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sg,
        "_default_http_opener",
        lambda _url, _t: (200, json.dumps(API_VERSION_BODY)),
    )
    rc = sg.main([
        "--suite", "smoke", "--revision", FAKE_SHA, "--out", str(out),
        "--base-url", "https://staging.x", "--no-audit", "--smoke-subset", "dag1",
    ])
    assert rc == 0
    assert seen["argv"][-3:] == ["https://staging.x", "--subset", "dag1"]
    assert "prod_smoke_test.py" in seen["argv"][1]


# ───────────────────────── systemd contract ─────────────────────────────


def _unit(name: str) -> str:
    return (SYSTEMD / name).read_text()


def test_canary_units_exist_and_wire_the_shim() -> None:
    svc = _unit("staging-gate-canary.service")
    assert "Type=oneshot" in svc
    assert "scripts/staging_gate.py --suite canary" in svc
    assert "OnFailure=staging-gate-alert.service" in svc
    assert "OMNISIGHT_RELEASE_CANARY_LOG=" in svc
    # §6 — release-audit DSN carried in (optional) EnvironmentFile.
    assert "EnvironmentFile=-/home/user/.config/omnisight/release-audit.env" in svc
    tim = _unit("staging-gate-canary.timer")
    assert "Unit=staging-gate-canary.service" in tim
    assert "OnUnitActiveSec=10min" in tim and "Persistent=true" in tim


def test_smoke_units_exist_and_cadence_under_max_age() -> None:
    svc = _unit("staging-gate-smoke.service")
    assert "Type=oneshot" in svc
    assert "scripts/staging_gate.py --suite smoke" in svc
    assert "OnFailure=staging-gate-alert.service" in svc
    assert "OMNISIGHT_RELEASE_SMOKE_LOG=" in svc
    tim = _unit("staging-gate-smoke.timer")
    assert "Unit=staging-gate-smoke.service" in tim
    assert "Persistent=true" in tim
    # Cadence must stay well inside release_milestone_checker's 4h smoke window.
    line = next(l for l in tim.splitlines() if l.startswith("OnUnitActiveSec="))
    assert line == "OnUnitActiveSec=30min"


def test_alert_unit_emits_structured_line() -> None:
    al = _unit("staging-gate-alert.service")
    assert "Type=oneshot" in al
    assert "staging_gate_alert" in al


def test_shim_is_executable_python_pointing_at_module() -> None:
    src = SCRIPT_SHIM.read_text()
    assert src.startswith("#!/usr/bin/env python3")
    assert "from backend.agents.staging_gate import main" in src
