#!/usr/bin/env python3
"""Z.7.7 — CI reporter: POST nightly live-test results to the backend.

Usage (called by the GitHub Actions workflow after the test run):

    python scripts/report_live_test_status.py <pytest-json-report.json>

Environment variables:
    OMNISIGHT_BACKEND_URL     Base URL of the running backend, e.g.
                              "https://omnisight.example.com". Required for
                              the HTTP POST path. When unset the reporter
                              logs to step summary only (graceful degrade
                              for setups without a live backend accessible
                              from the CI runner).
    OMNISIGHT_REPORTER_TOKEN  Pre-shared secret matching the backend's
                              OMNISIGHT_REPORTER_TOKEN. Required when
                              OMNISIGHT_BACKEND_URL is set.
    TEST_EXIT_CODE            Exit code from the pytest run (0 = pass,
                              non-zero = fail). Defaults to "0".
    GITHUB_RUN_ID             Injected automatically by GitHub Actions.

Output:
    Writes a one-line summary to $GITHUB_STEP_SUMMARY (when available) so
    the GitHub Actions run shows the chip state directly on the summary
    page.

    When OMNISIGHT_BACKEND_URL is set, POSTs to
    ``POST /api/v1/runtime/live-test-status`` so the running backend
    updates ``SharedKV("llm_live_test_status")`` and the dashboard chip
    reflects the run within 5 minutes (the chip's polling interval).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path


def _read_report(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _parse_provider_results(report: dict) -> dict[str, dict]:
    """Extract per-provider pass/fail/skip counts from test node IDs."""
    # Test IDs have the form:
    #   backend/tests/test_llm_adapter_live.py::TestAnthropicLive::test_tool_call
    # We key by the class name prefix (Anthropic / OpenAI / Gemini).
    providers: dict[str, dict] = {}

    for test in report.get("tests", []):
        node_id: str = test.get("nodeid", "")
        outcome: str = test.get("outcome", "unknown")

        key = None
        if "Anthropic" in node_id:
            key = "anthropic"
        elif "OpenAI" in node_id:
            key = "openai"
        elif "Gemini" in node_id:
            key = "google"
        if key is None:
            continue

        if key not in providers:
            providers[key] = {"status": "pass", "tests_run": 0, "tests_passed": 0, "tests_skipped": 0}

        if outcome == "skipped":
            providers[key]["tests_skipped"] += 1
        else:
            providers[key]["tests_run"] += 1
            if outcome == "passed":
                providers[key]["tests_passed"] += 1
            else:
                providers[key]["status"] = "fail"

    # A provider is "skip" when every test for it was skipped.
    for key, p in providers.items():
        if p["tests_run"] == 0 and p["tests_skipped"] > 0:
            p["status"] = "skip"

    return providers


def _build_payload(report: dict, exit_code: int, run_id: str, estimated_cost: float | None) -> dict:
    summary = report.get("summary", {})
    passed = int(summary.get("passed", 0))
    failed = int(summary.get("failed", 0))
    skipped = int(summary.get("skipped", 0))
    tests_run = passed + failed

    payload: dict = {
        "status": "pass" if exit_code == 0 else "fail",
        "run_id": run_id or None,
        "tests_run": tests_run,
        "tests_passed": passed,
        "tests_skipped": skipped,
        "providers": _parse_provider_results(report),
    }
    if estimated_cost is not None:
        payload["estimated_cost_usd"] = estimated_cost
    return payload


def _estimate_cost(tests_run: int) -> float:
    """Replicate the budget guard's worst-case estimate (avoids importing the script)."""
    max_input = 600
    max_output = 400
    highest_input = 1.50 / 1_000_000  # OpenAI output price (worst-case)
    highest_output = 1.50 / 1_000_000
    return (max_input * highest_input + max_output * highest_output) * tests_run


def _post_to_backend(backend_url: str, token: str, payload: dict) -> bool:
    """POST the payload to the backend. Returns True on success."""
    endpoint = f"{backend_url.rstrip('/')}/api/v1/runtime/live-test-status"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[reporter] POST {endpoint} → {resp.status}")
            return True
    except urllib.error.HTTPError as exc:
        print(f"[reporter] POST failed: HTTP {exc.code} — {exc.read().decode(errors='replace')}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[reporter] POST failed: {exc}", file=sys.stderr)
        return False


def _write_step_summary(payload: dict) -> None:
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not step_summary:
        return
    status = payload.get("status", "unknown")
    run_id = payload.get("run_id", "—")
    tests_run = payload.get("tests_run", 0)
    tests_passed = payload.get("tests_passed", 0)
    tests_skipped = payload.get("tests_skipped", 0)
    cost = payload.get("estimated_cost_usd")
    cost_str = f"${cost:.4f}" if cost is not None else "—"
    icon = "✅" if status == "pass" else "❌"
    providers = payload.get("providers", {}) or {}
    provider_rows = "\n".join(
        f"| {k.capitalize()} | {v.get('status','?')} | {v.get('tests_passed',0)}/{v.get('tests_run',0)} | {v.get('tests_skipped',0)} |"
        for k, v in providers.items()
    )
    summary = f"""## {icon} LLM Live Integration Tests

| Field | Value |
|---|---|
| Status | **{status.upper()}** |
| Run ID | {run_id} |
| Tests run | {tests_run} |
| Tests passed | {tests_passed} |
| Tests skipped | {tests_skipped} |
| Estimated cost | {cost_str} |

### Per-provider results

| Provider | Status | Passed/Run | Skipped |
|---|---|---|---|
{provider_rows}
"""
    try:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(summary)
        print(f"[reporter] wrote step summary to {step_summary}")
    except Exception as exc:
        print(f"[reporter] could not write step summary: {exc}", file=sys.stderr)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: report_live_test_status.py <pytest-json-report.json>", file=sys.stderr)
        return 2

    report_path = sys.argv[1]
    exit_code = int(os.environ.get("TEST_EXIT_CODE", "0"))
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    backend_url = os.environ.get("OMNISIGHT_BACKEND_URL", "").strip()
    reporter_token = os.environ.get("OMNISIGHT_REPORTER_TOKEN", "").strip()

    report = _read_report(report_path)
    summary = report.get("summary", {})
    tests_run = int(summary.get("passed", 0)) + int(summary.get("failed", 0))
    estimated_cost = _estimate_cost(tests_run)

    payload = _build_payload(report, exit_code, run_id, estimated_cost)
    print(f"[reporter] status={payload['status']}  tests_run={payload['tests_run']}  "
          f"tests_passed={payload['tests_passed']}  skipped={payload['tests_skipped']}  "
          f"estimated_cost=${estimated_cost:.4f}")

    _write_step_summary(payload)

    if backend_url and reporter_token:
        ok = _post_to_backend(backend_url, reporter_token, payload)
        if not ok:
            print("[reporter] WARNING: backend POST failed; dashboard chip will not update this run", file=sys.stderr)
    elif backend_url and not reporter_token:
        print("[reporter] WARNING: OMNISIGHT_BACKEND_URL set but OMNISIGHT_REPORTER_TOKEN missing — skipping POST", file=sys.stderr)
    else:
        print("[reporter] OMNISIGHT_BACKEND_URL not set — skipping backend POST (set it + OMNISIGHT_REPORTER_TOKEN to enable dashboard updates)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
