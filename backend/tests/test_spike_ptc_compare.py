"""OP-861 — PTC feasibility spike harness tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "spike_ptc_compare.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("spike_ptc_compare", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_ptc_request_shape_matches_documented_api_surface():
    harness = _load_harness()
    payload = harness.build_ptc_request()

    assert payload["model"] == "claude-sonnet-4-6"
    assert {
        "type": "code_execution_20260120",
        "name": "code_execution",
    } in payload["tools"]
    callable_tools = [
        tool for tool in payload["tools"] if tool.get("name") != "code_execution"
    ]
    assert callable_tools
    assert all(
        tool["allowed_callers"] == ["code_execution_20260120"]
        for tool in callable_tools
    )


def test_fallback_when_ptc_feature_unavailable_preserves_baseline_metrics():
    harness = _load_harness()
    comparison = harness.compare_workflow(ptc_available=False)

    assert comparison.ptc_available is False
    assert comparison.ptc.mode == "ptc-fallback"
    assert comparison.ptc.input_tokens == comparison.baseline.input_tokens
    assert comparison.ptc.wall_s == comparison.baseline.wall_s
    assert comparison.delta["model_turns_saved"] == 0


def test_comparison_metrics_show_read_only_ptc_savings():
    harness = _load_harness()
    comparison = harness.compare_workflow(ptc_available=True)

    assert comparison.baseline.tool_invocations == comparison.ptc.tool_invocations == 3
    assert comparison.ptc.model_turns < comparison.baseline.model_turns
    assert comparison.ptc.input_tokens < comparison.baseline.input_tokens
    assert comparison.ptc.wall_s < comparison.baseline.wall_s
    assert comparison.delta["input_tokens_saved_pct"] > 0


def test_catalog_has_20_plus_tools_and_separates_safe_reads_from_writes():
    harness = _load_harness()
    catalog = harness.classify_catalog()

    assert catalog["total"] >= 20
    assert catalog["ptc_safe"] > 0
    assert catalog["ptc_safe"] < catalog["total"]
    tools = {tool["name"]: tool for tool in catalog["tools"]}
    assert tools["jira_fetch"]["ptc_safe"] is True
    assert tools["gerrit_push"]["ptc_safe"] is False
    assert tools["mcp_list"]["reason"].startswith("MCP connector")


def test_cli_writes_json_and_markdown(tmp_path):
    harness = _load_harness()
    md = tmp_path / "ptc.md"
    js = tmp_path / "ptc.json"

    rc = harness.main(["--output", str(md), "--json", str(js)])

    assert rc == 0
    assert md.read_text(encoding="utf-8").startswith("# OP-861 PTC Offline Comparison")
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["comparison"]["workflow"].startswith("fetch ticket")
    assert payload["catalog"]["total"] >= 20
    assert payload["ptc_request"]["tools"][0]["type"] == "code_execution_20260120"
