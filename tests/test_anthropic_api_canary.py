"""[OP-1066] Tests for the Anthropic-API surface-drift canary.

Surfaces, mirroring tests/test_canary_pipeline.py:
1. Unit: schema diff matrix.
2. Integration: synthetic canary with mocked Anthropic response.
3. systemd unit contract (hourly cadence, no hard-pinned API key).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "anthropic-api-canary.py"
PINNED_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "anthropic-api-tool-schema-pinned.json"
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
CANARY_SERVICE = SYSTEMD_DIR / "anthropic-api-canary.service"
CANARY_TIMER = SYSTEMD_DIR / "anthropic-api-canary.timer"


def _load_canary_module() -> Any:
    # Hyphenated filename + canary must not be polluted by sibling
    # scripts/ imports, so use spec-loader (same pattern as OP-725).
    spec = importlib.util.spec_from_file_location(
        "anthropic_api_canary_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["anthropic_api_canary_under_test"] = module
    spec.loader.exec_module(module)
    return module


canary = _load_canary_module()


def _good_response() -> dict[str, Any]:
    return {
        "id": "msg_canary_001",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "stop_reason": "tool_use",
        "content": [{
            "type": "tool_use",
            "id": "toolu_canary_001",
            "name": "str_replace_based_edit_tool",
            "input": {"command": "view", "path": "/tmp/x.txt"},
        }],
        "usage": {"input_tokens": 42, "output_tokens": 17},
    }


def _pinned() -> dict[str, Any]:
    return canary.load_pinned_schema(PINNED_FIXTURE_PATH)


# ── pure-function: schema diff matrix ────────────────────────────


def _mutate(resp: dict[str, Any], op: str) -> dict[str, Any]:
    if op == "del:stop_reason":
        del resp["stop_reason"]
    elif op == "set:role=user":
        resp["role"] = "user"
    elif op == "set:stop_reason=refusal":
        resp["stop_reason"] = "refusal"
    elif op == "rename:tool":
        resp["content"][0]["name"] = "text_editor"
    elif op == "del:input.path":
        del resp["content"][0]["input"]["path"]
    elif op == "set:input.command=delete":
        resp["content"][0]["input"]["command"] = "delete"
    elif op == "set:input=string":
        resp["content"][0]["input"] = "view /tmp/foo"
    elif op == "del:usage.output_tokens":
        del resp["usage"]["output_tokens"]
    return resp


def test_clean_response_yields_no_drift():
    assert canary.diff_observed_against_pinned(_good_response(), _pinned()) == []


@pytest.mark.parametrize("mutation,fragment", [
    ("del:stop_reason", "stop_reason"),
    ("set:role=user", "role"),
    ("set:stop_reason=refusal", "refusal"),
    # Renamed tool_use.name silently dispatches to the wrong handler — exact guard.
    ("rename:tool", "tool_use.name"),
    ("del:input.path", "tool_use.input missing key: path"),
    ("set:input.command=delete", "delete"),
    ("set:input=string", "tool_use.input is not a mapping"),
    ("del:usage.output_tokens", "usage missing key: output_tokens"),
])
def test_drift_cases_flagged(mutation: str, fragment: str):
    resp = _mutate(_good_response(), mutation)
    diffs = canary.diff_observed_against_pinned(resp, _pinned())
    assert any(fragment in d for d in diffs), f"{mutation!r}: {diffs}"


def test_end_turn_without_tool_use_is_not_drift():
    # Model choosing not to call the tool is not API drift; false
    # positives erode alert trust.
    resp = _good_response()
    resp["stop_reason"] = "end_turn"
    resp["content"] = [{"type": "text", "text": "I would view the file."}]
    assert canary.diff_observed_against_pinned(resp, _pinned()) == []


def test_diff_works_against_attribute_shaped_response():
    # The Anthropic SDK returns object-shaped responses, not dicts.
    class Block:
        type = "tool_use"; id = "x"; name = "str_replace_based_edit_tool"
        input = {"command": "view", "path": "/tmp/x"}

    class Usage:
        input_tokens = 10; output_tokens = 5

    class Resp:
        id = "msg_x"; type = "message"; role = "assistant"
        model = "claude-sonnet-4-6"; stop_reason = "tool_use"
        content = [Block()]; usage = Usage()

    assert canary.diff_observed_against_pinned(Resp(), _pinned()) == []


# ── pinned fixture sanity ────────────────────────────────────────


def test_pinned_fixture_loads_and_has_required_keys():
    pinned = canary.load_pinned_schema(PINNED_FIXTURE_PATH)
    assert pinned["tool_name"] == "str_replace_based_edit_tool"
    assert pinned["tool_type"] == "text_editor_20250728"
    assert pinned["schema_version"] == "v1"


def test_load_pinned_schema_rejects_missing_required_key(tmp_path: Path):
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"schema_version": "v1"}), encoding="utf-8")
    with pytest.raises(ValueError):
        canary.load_pinned_schema(incomplete)


# ── integration: run_canary with injected api_caller + notifier ──


class _NotifierSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, severity: Any, code: str, **kwargs: Any) -> None:
        self.calls.append({"severity": severity, "code": code, **kwargs})


def test_run_canary_clean_response_exits_zero_and_does_not_notify():
    spy = _NotifierSpy()
    rc = canary.run_canary(
        pinned_path=PINNED_FIXTURE_PATH,
        api_caller=lambda _p: _good_response(),
        notifier=spy,
    )
    assert rc == canary.EXIT_OK
    assert spy.calls == []


def test_run_canary_drifted_response_exits_two_and_fires_one_alert():
    # AC: integration synthetic canary against mocked Anthropic response
    # with intentional diff → alert fires.
    drifted = _good_response()
    drifted["content"][0]["name"] = "renamed_by_anthropic"
    drifted["content"][0]["input"].pop("path")

    spy = _NotifierSpy()
    rc = canary.run_canary(
        pinned_path=PINNED_FIXTURE_PATH,
        api_caller=lambda _p: drifted,
        notifier=spy,
    )
    assert rc == canary.EXIT_DRIFT
    assert len(spy.calls) == 1
    call = spy.calls[0]

    from backend.agents.operator_notifier import Severity
    assert call["severity"] == Severity.DEGRADED
    assert call["code"] == canary.DRIFT_NOTIFICATION_CODE

    ctx = call["context"]
    assert ctx["tool"] == "str_replace_based_edit_tool"
    assert ctx["tool_type"] == "text_editor_20250728"
    assert ctx["diff_count"] >= 2
    assert "expected_schema" in ctx
    assert "actual_schema" in ctx
    assert "runbook" in ctx


def test_run_canary_api_exception_treated_as_drift():
    # A non-RuntimeError SDK fault is real trouble; must not be silently
    # swallowed. RuntimeError is reserved for our own config-faults.
    spy = _NotifierSpy()
    rc = canary.run_canary(
        pinned_path=PINNED_FIXTURE_PATH,
        api_caller=lambda _p: (_ for _ in ()).throw(ValueError("synthetic")),
        notifier=spy,
    )
    assert rc == canary.EXIT_DRIFT
    assert len(spy.calls) == 1
    assert "ValueError" in spy.calls[0]["context"]["actual_schema"]["_exception"]


def test_run_canary_env_error_when_api_key_missing():
    # Missing key must NOT page operators as drift — distinct exit code.
    spy = _NotifierSpy()
    rc = canary.run_canary(
        pinned_path=PINNED_FIXTURE_PATH,
        api_caller=lambda _p: (_ for _ in ()).throw(RuntimeError("ANTHROPIC_API_KEY not set")),
        notifier=spy,
    )
    assert rc == canary.EXIT_ENV_ERROR
    assert spy.calls == []


def test_run_canary_missing_pinned_schema_exits_env_error(tmp_path: Path):
    spy = _NotifierSpy()
    rc = canary.run_canary(
        pinned_path=tmp_path / "nope.json",
        api_caller=lambda _p: _good_response(),
        notifier=spy,
    )
    assert rc == canary.EXIT_ENV_ERROR
    assert spy.calls == []


def test_run_canary_dry_run_response_drift_alerts(tmp_path: Path):
    drifted = _good_response()
    drifted["stop_reason"] = "ratelimit"
    path = tmp_path / "drift.json"
    path.write_text(json.dumps(drifted), encoding="utf-8")

    spy = _NotifierSpy()
    rc = canary.run_canary(
        pinned_path=PINNED_FIXTURE_PATH,
        dry_run_response_path=path,
        notifier=spy,
    )
    assert rc == canary.EXIT_DRIFT
    assert len(spy.calls) == 1


# ── systemd unit contract ────────────────────────────────────────


def test_timer_pins_hourly_cadence():
    text = CANARY_TIMER.read_text(encoding="utf-8")
    assert "OnUnitActiveSec=1h" in text
    assert "Persistent=true" in text
    assert "Unit=anthropic-api-canary.service" in text


def test_service_is_oneshot_and_runs_canary_script():
    text = CANARY_SERVICE.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "scripts/anthropic-api-canary.py" in text
    assert "OP-1066" in text


def test_service_does_not_hard_pin_api_key_in_unit_file():
    # CLAUDE.md L1: "NEVER store API keys, tokens, or secrets in source
    # code or commits". The unit must source the key from the user env.
    text = CANARY_SERVICE.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Environment=") and "ANTHROPIC_API_KEY=" in stripped:
            pytest.fail(f"unit hard-pins ANTHROPIC_API_KEY: {line!r}")
