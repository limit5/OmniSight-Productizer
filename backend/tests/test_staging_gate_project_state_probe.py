"""[OP-2558] — tests for the authed project-state canary probe.

Mirrors ``backend/tests/test_staging_gate.py``: stdlib + pytest only, no
network / git / ssh. Every case uses injected HTTP-opener seams — per the
OP-2558 clarification the Code AC is satisfied ENTIRELY offline (a live
staging round-trip is physically impossible: staging is auth-mode=strict with
NO JIRA creds, so ``jira_source`` can never be ``live`` there — that green proof
is the operator's Deploy step, not the runner's).

Probe contract under test:

  * activates only when the fixture-ticket env is set (else skipped, INFO log);
  * fails CLOSED — bearer-absent / staging-JIRA-not-live / hollow-kg = LOUD red
    JSONL line (exit 2), never the exit-3 nothing-written path, never a pass;
  * a healthy dual-half-LIVE fixture is green;
  * observed markers land in the gate evidence JSON;
  * a transient HTTP-0 is retried once.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.agents import staging_gate as sg

FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"
BACKEND_DIGEST = "sha256:" + "a" * 64
FRONTEND_DIGEST = "sha256:" + "b" * 64
API_VERSION_BODY = {
    "bundle_id": "develop-01234567",
    "backend_image_digest": BACKEND_DIGEST,
    "frontend_image_digest": FRONTEND_DIGEST,
    "api_required": "v1",
}
FIXTURE_TICKET = "OP-2558"


def _project_state_body(
    *,
    jira_source: str = "live",
    kg_source: str = "live",
    kg_neighbours: list[dict[str, Any]] | None = None,
) -> str:
    structural: dict[str, Any] = {
        "ticket": FIXTURE_TICKET,
        "jira_source": jira_source,
        "kg_source": kg_source,
        "kg_neighbours": (
            kg_neighbours
            if kg_neighbours is not None
            else [{"identifier": "L-OP-1736", "kind": "lesson", "score": 14.1}]
        ),
    }
    return json.dumps({"ticket": FIXTURE_TICKET, "structural": structural})


def _authed_opener(status: int, body: str):
    seen: dict[str, Any] = {}

    def opener(url: str, _timeout: float, headers: dict[str, str]) -> tuple[int, str]:
        seen["url"] = url
        seen["headers"] = headers
        return status, body

    opener.seen = seen  # type: ignore[attr-defined]
    return opener


# ───────────────────────── pure probe unit ──────────────────────────────


def test_probe_green_on_dual_half_live() -> None:
    opener = _authed_opener(200, _project_state_body())
    result = sg.project_state_probe(
        base_url="https://staging.x/",
        fixture_ticket=FIXTURE_TICKET,
        bearer="tok-abc",
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=opener,
    )
    assert result.ok
    assert result.markers["project_state_probe"] == "ok"
    assert result.markers["observed_jira_source"] == "live"
    assert result.markers["observed_kg_source"] == "live"
    assert result.markers["observed_kg_neighbours_count"] == 1
    # authed against the fixture ticket, bearer in the header.
    assert opener.seen["url"] == (  # type: ignore[attr-defined]
        "https://staging.x/api/v1/project-state?ticket=OP-2558"
    )
    assert opener.seen["headers"]["Authorization"] == "Bearer tok-abc"  # type: ignore[attr-defined]


def test_probe_config_error_when_bearer_absent() -> None:
    """Fail CLOSED — no bearer is a LOUD red, and we never even make the call."""
    opener = _authed_opener(200, _project_state_body())
    result = sg.project_state_probe(
        base_url="https://staging.x",
        fixture_ticket=FIXTURE_TICKET,
        bearer=None,
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=opener,
    )
    assert not result.ok
    assert result.markers["project_state_probe"] == "config_error"
    assert result.markers["project_state_config_error"] == "bearer_absent"
    assert sg.ENV_STAGING_GATE_BEARER in result.detail
    assert "url" not in opener.seen  # type: ignore[attr-defined]  # no call made


@pytest.mark.parametrize("empty", ["", "   "])
def test_probe_config_error_on_blank_bearer(empty: str) -> None:
    result = sg.project_state_probe(
        base_url="https://staging.x",
        fixture_ticket=FIXTURE_TICKET,
        bearer=empty,
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=_authed_opener(200, _project_state_body()),
    )
    assert not result.ok and result.markers["project_state_config_error"] == "bearer_absent"


@pytest.mark.parametrize("status", [401, 403])
def test_probe_red_on_auth_rejection(status: int) -> None:
    result = sg.project_state_probe(
        base_url="https://staging.x",
        fixture_ticket=FIXTURE_TICKET,
        bearer="bad-tok",
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=_authed_opener(status, ""),
    )
    assert not result.ok
    assert result.markers["project_state_probe"] == "auth_error"
    assert str(status) in result.detail


def test_probe_config_error_when_jira_not_live() -> None:
    """A JIRA-less staging (jira_source != live) is a config-error red — NOT a
    silent kg-only pass (OP-2558 MUST-NOT: do not weaken to 'jira OR kg')."""
    opener = _authed_opener(200, _project_state_body(jira_source="degraded"))
    result = sg.project_state_probe(
        base_url="https://staging.x",
        fixture_ticket=FIXTURE_TICKET,
        bearer="tok",
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=opener,
    )
    assert not result.ok
    assert result.markers["project_state_probe"] == "config_error"
    assert "jira_source" in result.markers["project_state_config_error"]
    assert result.markers["observed_kg_source"] == "live"  # kg live is irrelevant


def test_probe_red_when_kg_live_but_zero_neighbours() -> None:
    """kg can be 'live' with 0 neighbours — that hollow half is a red."""
    result = sg.project_state_probe(
        base_url="https://staging.x",
        fixture_ticket=FIXTURE_TICKET,
        bearer="tok",
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=_authed_opener(200, _project_state_body(kg_neighbours=[])),
    )
    assert not result.ok
    assert result.markers["project_state_probe"] == "degraded"
    assert result.markers["observed_kg_neighbours_count"] == 0


def test_probe_red_when_kg_degraded() -> None:
    result = sg.project_state_probe(
        base_url="https://staging.x",
        fixture_ticket=FIXTURE_TICKET,
        bearer="tok",
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=_authed_opener(200, _project_state_body(kg_source="degraded", kg_neighbours=[])),
    )
    assert not result.ok and result.markers["project_state_probe"] == "degraded"


def test_probe_retries_once_on_transient_http_0() -> None:
    seq = iter([(0, "conn reset"), (200, _project_state_body())])
    calls: list[str] = []

    def opener(url: str, _t: float, _h: dict[str, str]) -> tuple[int, str]:
        calls.append(url)
        return next(seq)

    result = sg.project_state_probe(
        base_url="https://staging.x",
        fixture_ticket=FIXTURE_TICKET,
        bearer="tok",
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=opener,
    )
    assert result.ok
    assert len(calls) == 2
    assert result.markers["project_state_retried_http_0"] is True


def test_probe_red_when_http_0_persists() -> None:
    result = sg.project_state_probe(
        base_url="https://staging.x",
        fixture_ticket=FIXTURE_TICKET,
        bearer="tok",
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=_authed_opener(0, "refused"),
    )
    assert not result.ok
    assert result.markers["project_state_probe"] == "http_error"
    assert result.markers["project_state_http_status"] == 0


def test_probe_red_on_non_json_body() -> None:
    result = sg.project_state_probe(
        base_url="https://staging.x",
        fixture_ticket=FIXTURE_TICKET,
        bearer="tok",
        path=sg.DEFAULT_PROJECT_STATE_PATH,
        timeout=1.0,
        opener=_authed_opener(200, "<html>gateway</html>"),
    )
    assert not result.ok and result.markers["project_state_probe"] == "bad_json"


# ───────────────── main() integration — activation + fail-closed ────────


def _patch_openers(
    monkeypatch: pytest.MonkeyPatch, *, project_state: tuple[int, str]
) -> None:
    """Stub /api/version (unauthed) green and project-state (authed) to caller."""
    monkeypatch.setattr(
        sg,
        "_default_http_opener",
        lambda url, _t: (
            (200, json.dumps(API_VERSION_BODY))
            if url.endswith("/api/version")
            else (200, "{}")
        ),
    )
    monkeypatch.setattr(
        sg,
        "_default_authed_http_opener",
        lambda _url, _t, _h: project_state,
    )


def _run_canary(tmp_path: Path, out_name: str = "canary-status.jsonl") -> tuple[int, dict[str, Any]]:
    out = tmp_path / out_name
    rc = sg.main([
        "--suite", "canary", "--revision", FAKE_SHA, "--out", str(out),
        "--base-url", "https://staging.x", "--no-audit",
        "--canary-attempts", "1", "--canary-interval", "0",
    ])
    rec = json.loads(out.read_text(encoding="utf-8"))
    return rc, rec


def test_main_probe_skipped_when_fixture_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture unset → probe skipped, existing /healthz+/readyz canary green,
    and NO project-state keys leak into the evidence."""
    monkeypatch.delenv(sg.ENV_STAGING_GATE_FIXTURE_TICKET, raising=False)
    monkeypatch.delenv(sg.ENV_STAGING_GATE_BEARER, raising=False)
    monkeypatch.setattr(
        sg,
        "_default_http_opener",
        lambda url, _t: (
            (200, json.dumps(API_VERSION_BODY))
            if url.endswith("/api/version")
            else (200, "{}")
        ),
    )
    rc, rec = _run_canary(tmp_path)
    assert rc == 0 and rec["status"] == "green"
    ps_keys = {"project_state_probe", "project_state_config_error", "observed_jira_source",
               "observed_kg_source", "observed_kg_neighbours_count", "project_state_http_status"}
    assert not (ps_keys & set(rec))


def test_main_green_on_healthy_fixture_records_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sg.ENV_STAGING_GATE_FIXTURE_TICKET, FIXTURE_TICKET)
    monkeypatch.setenv(sg.ENV_STAGING_GATE_BEARER, "tok-abc")
    _patch_openers(monkeypatch, project_state=(200, _project_state_body()))
    rc, rec = _run_canary(tmp_path)
    assert rc == 0 and rec["status"] == "green"
    assert rec["project_state_probe"] == "ok"
    assert rec["observed_jira_source"] == "live"
    assert rec["observed_kg_neighbours_count"] == 1
    # existing api-version evidence still present.
    assert rec["backend_digest"] == BACKEND_DIGEST


def test_main_red_on_401_without_bearer_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sg.ENV_STAGING_GATE_FIXTURE_TICKET, FIXTURE_TICKET)
    monkeypatch.setenv(sg.ENV_STAGING_GATE_BEARER, "wrong")
    _patch_openers(monkeypatch, project_state=(401, ""))
    rc, rec = _run_canary(tmp_path)
    assert rc == 2 and rec["status"] == "red"
    assert rec["project_state_probe"] == "auth_error"


def test_main_config_error_red_when_bearer_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bearer-absent → RED JSONL line written + exit 2 (NOT the exit-3
    nothing-written config path). OP-2558 [2]."""
    monkeypatch.setenv(sg.ENV_STAGING_GATE_FIXTURE_TICKET, FIXTURE_TICKET)
    monkeypatch.delenv(sg.ENV_STAGING_GATE_BEARER, raising=False)
    _patch_openers(monkeypatch, project_state=(200, _project_state_body()))
    out = tmp_path / "canary-status.jsonl"
    rc = sg.main([
        "--suite", "canary", "--revision", FAKE_SHA, "--out", str(out),
        "--base-url", "https://staging.x", "--no-audit",
        "--canary-attempts", "1", "--canary-interval", "0",
    ])
    assert rc == 2  # red, not 3
    assert out.exists()  # a line WAS written (not the exit-3 nothing-written path)
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["status"] == "red"
    assert rec["project_state_config_error"] == "bearer_absent"


def test_main_config_error_red_when_staging_jira_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """jira_source != live (JIRA-less staging) → config-error red, not a silent
    kg-only pass. OP-2558 MUST-NOT."""
    monkeypatch.setenv(sg.ENV_STAGING_GATE_FIXTURE_TICKET, FIXTURE_TICKET)
    monkeypatch.setenv(sg.ENV_STAGING_GATE_BEARER, "tok")
    _patch_openers(
        monkeypatch, project_state=(200, _project_state_body(jira_source="unavailable"))
    )
    rc, rec = _run_canary(tmp_path)
    assert rc == 2 and rec["status"] == "red"
    assert rec["project_state_probe"] == "config_error"
    assert "jira_source" in rec["project_state_config_error"]


def test_main_red_on_hollow_kg_half(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sg.ENV_STAGING_GATE_FIXTURE_TICKET, FIXTURE_TICKET)
    monkeypatch.setenv(sg.ENV_STAGING_GATE_BEARER, "tok")
    _patch_openers(
        monkeypatch, project_state=(200, _project_state_body(kg_neighbours=[]))
    )
    rc, rec = _run_canary(tmp_path)
    assert rc == 2 and rec["status"] == "red"
    assert rec["project_state_probe"] == "degraded"
    assert rec["observed_kg_neighbours_count"] == 0


def test_main_red_when_healthz_down_even_if_project_state_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The project-state probe augments, never replaces, /healthz+/readyz."""
    monkeypatch.setenv(sg.ENV_STAGING_GATE_FIXTURE_TICKET, FIXTURE_TICKET)
    monkeypatch.setenv(sg.ENV_STAGING_GATE_BEARER, "tok")
    monkeypatch.setattr(
        sg,
        "_default_http_opener",
        lambda url, _t: (
            (200, json.dumps(API_VERSION_BODY))
            if url.endswith("/api/version")
            else (503, "")  # /healthz + /readyz down
        ),
    )
    monkeypatch.setattr(
        sg, "_default_authed_http_opener", lambda _u, _t, _h: (200, _project_state_body())
    )
    rc, rec = _run_canary(tmp_path)
    assert rc == 2 and rec["status"] == "red"
    # project-state half still observed & recorded as ok.
    assert rec["project_state_probe"] == "ok"


def test_bearer_is_env_only_never_a_cli_flag() -> None:
    """The bearer must never be accepted as a CLI literal (secret hygiene)."""
    parser = sg.build_arg_parser()
    flags = {a.option_strings[0] for a in parser._actions if a.option_strings}
    assert "--project-state-fixture-ticket" in flags
    assert not any("bearer" in f.lower() for f in flags)
