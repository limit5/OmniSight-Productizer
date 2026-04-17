#!/usr/bin/env python3
"""A2 L1-05 — Production smoke test: 2 real DAGs end-to-end.

DAG #1: compile-flash against host_native (Phase 64-C-LOCAL fast path)
DAG #2: cross-compile against aarch64 (full cross-compile path)

Usage:
    python scripts/prod_smoke_test.py [BASE_URL] [--subset dag1|dag2|both]

    BASE_URL defaults to http://localhost:8000. For production:
    python scripts/prod_smoke_test.py https://omnisight.example.com

    --subset selects which DAG(s) to run. L6 Step 5 of the bootstrap
    wizard invokes `--subset dag1` so a fresh install can smoke-check the
    full pipeline in ~60s without burning the cross-compile budget.

Requires: operator auth cookie or OMNISIGHT_API_TOKEN env var.
Exit codes: 0=pass, 1=DAG submit failed, 2=verification failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib import request, error as urlerror


def _parse_cli(argv: list[str]) -> tuple[str, str]:
    """Return (base_url, subset) parsed from *argv* (sys.argv[1:] shape).

    Kept as a module-level function so the bootstrap wizard endpoint can
    reuse the same subset/base_url semantics when it invokes this script
    via subprocess.
    """
    p = argparse.ArgumentParser(
        prog="prod_smoke_test",
        description="Run OmniSight production smoke DAG(s)",
        add_help=True,
    )
    p.add_argument(
        "base_url", nargs="?", default="http://localhost:8000",
        help="Server base URL (default: http://localhost:8000)",
    )
    p.add_argument(
        "--subset",
        choices=("dag1", "dag2", "both"),
        default="both",
        help=(
            "dag1 = compile-flash host_native only (fast ~60s, used by the "
            "bootstrap wizard's L6 Step 5); dag2 = cross-compile aarch64 only; "
            "both (default) = run the full pair."
        ),
    )
    ns = p.parse_args(argv)
    return ns.base_url.rstrip("/"), ns.subset


BASE_URL, SUBSET = _parse_cli(sys.argv[1:])
API = f"{BASE_URL}/api/v1"
TOKEN = os.environ.get("OMNISIGHT_API_TOKEN", "")
POLL_INTERVAL = 3
POLL_TIMEOUT = 300

# ── DAG definitions ─────────────────────────────────────────────

DAG_1_COMPILE_FLASH_HOST_NATIVE = {
    "dag": {
        "schema_version": 1,
        "dag_id": "smoke-compile-flash-host-native",
        "tasks": [
            {
                "task_id": "compile",
                "description": "Build firmware image (host-native, no cross-compile)",
                "required_tier": "t1",
                "toolchain": "cmake",
                "inputs": [],
                "expected_output": "build/firmware.bin",
                "depends_on": [],
            },
            {
                "task_id": "flash",
                # On host_native the T3 resolver picks LOCAL and the
                # validator swaps the effective tier to t1. Use python3
                # (a t1-legal toolchain) so the symbolic "flash" step
                # validates — there's no physical board to flash when
                # target arch == host arch.
                "description": "Flash built image (T3 resolves to LOCAL on host_native)",
                "required_tier": "t3",
                "toolchain": "python3",
                "inputs": ["build/firmware.bin"],
                "expected_output": "logs/flash.log",
                "depends_on": ["compile"],
            },
        ],
    },
    "target_platform": "host_native",
    "metadata": {"source": "smoke-test:A2-DAG1", "test_run": True},
}

DAG_2_CROSS_COMPILE_AARCH64 = {
    "dag": {
        "schema_version": 1,
        "dag_id": "smoke-cross-compile-aarch64",
        "tasks": [
            {
                "task_id": "cross-compile",
                "description": "Cross-compile firmware for AArch64 target",
                "required_tier": "t1",
                "toolchain": "cmake",
                "inputs": [],
                "expected_output": "build/firmware-aarch64.bin",
                "depends_on": [],
            },
            {
                "task_id": "package",
                "description": "Package cross-compiled artifact for deployment",
                "required_tier": "t1",
                "toolchain": "make",
                "inputs": ["build/firmware-aarch64.bin"],
                "expected_output": "dist/firmware-aarch64.tar.gz",
                "depends_on": ["cross-compile"],
            },
        ],
    },
    "target_platform": "aarch64",
    "metadata": {"source": "smoke-test:A2-DAG2", "test_run": True},
}

ALL_DAGS: list[tuple[str, str, dict]] = [
    ("dag1", "DAG #1: compile-flash (host_native)", DAG_1_COMPILE_FLASH_HOST_NATIVE),
    ("dag2", "DAG #2: cross-compile (aarch64)", DAG_2_CROSS_COMPILE_AARCH64),
]


def _select_dags(subset: str) -> list[tuple[str, dict]]:
    """Filter ALL_DAGS by subset keyword — `dag1`, `dag2`, or `both`."""
    if subset == "both":
        return [(label, payload) for _key, label, payload in ALL_DAGS]
    return [
        (label, payload) for key, label, payload in ALL_DAGS if key == subset
    ]


DAGS = _select_dags(SUBSET)


# ── HTTP helpers ────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _post(path: str, body: dict) -> dict:
    url = f"{API}{path}"
    data = json.dumps(body).encode()
    req = request.Request(url, data=data, headers=_headers(), method="POST")
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urlerror.HTTPError as exc:
        resp_body = exc.read().decode(errors="replace")
        print(f"  HTTP {exc.code}: {resp_body}", file=sys.stderr)
        raise


def _get(path: str) -> dict:
    url = f"{API}{path}"
    req = request.Request(url, headers=_headers(), method="GET")
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ── Submit + poll ───────────────────────────────────────────────

def submit_dag(label: str, payload: dict) -> dict:
    print(f"\n{'='*60}")
    print(f"  Submitting {label}")
    print(f"{'='*60}")
    result = _post("/dag", payload)
    run_id = result.get("run_id", "???")
    plan_id = result.get("plan_id", "???")
    status = result.get("status", "???")
    print(f"  run_id:  {run_id}")
    print(f"  plan_id: {plan_id}")
    print(f"  status:  {status}")
    if result.get("validation_errors"):
        print(f"  errors:  {json.dumps(result['validation_errors'], indent=2)}")
    return result


def poll_run(run_id: str) -> dict:
    """Poll workflow run until terminal status or timeout."""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        run = _get(f"/workflow/runs/{run_id}")
        status = run.get("run", {}).get("status", run.get("status", "unknown"))
        if status in ("completed", "failed", "halted"):
            return run
        print(f"  ... status={status}, waiting {POLL_INTERVAL}s")
        time.sleep(POLL_INTERVAL)
    print(f"  TIMEOUT after {POLL_TIMEOUT}s", file=sys.stderr)
    return {}


# ── Verification ────────────────────────────────────────────────

def verify_run(run_id: str, run_data: dict) -> list[str]:
    """Return list of failures (empty = pass)."""
    failures: list[str] = []
    run_info = run_data.get("run", run_data)
    status = run_info.get("status", "unknown")
    if status != "completed":
        failures.append(f"run status={status}, expected completed")

    steps = run_data.get("steps", [])
    if not steps:
        failures.append("no steps recorded")
    for s in steps:
        if s.get("error"):
            failures.append(f"step {s.get('key','?')} error: {s['error']}")
        if not s.get("is_done"):
            failures.append(f"step {s.get('key','?')} not done")

    return failures


def verify_audit_chain() -> tuple[bool, str]:
    """Call the audit chain verification endpoint."""
    try:
        result = _get("/audit/verify")
        ok = result.get("ok", False)
        detail = result.get("detail", "")
        return ok, detail
    except Exception as exc:
        return False, f"audit verify request failed: {exc}"


# ── Report generation ───────────────────────────────────────────

def generate_report(results: list[dict]) -> str:
    lines = [
        "## A2 L1-05 Prod Smoke Test Report",
        f"",
        f"**Date**: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"**Target**: {BASE_URL}",
        f"**Subset**: {SUBSET}",
        f"",
    ]

    all_pass = True
    for r in results:
        label = r["label"]
        run_id = r.get("run_id", "N/A")
        plan_id = r.get("plan_id", "N/A")
        status = r.get("final_status", "unknown")
        failures = r.get("failures", [])
        passed = len(failures) == 0

        if not passed:
            all_pass = False

        lines.append(f"### {label}")
        lines.append(f"- **run_id**: `{run_id}`")
        lines.append(f"- **plan_id**: `{plan_id}`")
        lines.append(f"- **final_status**: `{status}`")
        lines.append(f"- **result**: {'PASS' if passed else 'FAIL'}")
        if failures:
            for f in failures:
                lines.append(f"  - {f}")
        lines.append("")

    audit_ok, audit_detail = verify_audit_chain()
    lines.append("### Audit Hash-Chain Integrity")
    lines.append(f"- **result**: {'PASS' if audit_ok else 'FAIL'}")
    if audit_detail:
        lines.append(f"- **detail**: {audit_detail}")
    if not audit_ok:
        all_pass = False
    lines.append("")

    lines.append(f"### Overall: {'ALL PASS' if all_pass else 'SOME FAILURES'}")
    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────

def main() -> int:
    print(f"OmniSight Prod Smoke Test — A2 L1-05")
    print(f"Target: {BASE_URL}")

    results: list[dict] = []

    for label, payload in DAGS:
        try:
            submit_result = submit_dag(label, payload)
        except Exception as exc:
            print(f"  SUBMIT FAILED: {exc}", file=sys.stderr)
            results.append({"label": label, "failures": [f"submit failed: {exc}"]})
            continue

        run_id = submit_result.get("run_id")
        plan_id = submit_result.get("plan_id")
        status = submit_result.get("status", "unknown")

        if status == "failed" or not run_id:
            errs = submit_result.get("validation_errors", [])
            results.append({
                "label": label,
                "run_id": run_id,
                "plan_id": plan_id,
                "final_status": status,
                "failures": [f"validation: {e}" for e in errs] or ["submit returned failed"],
            })
            continue

        print(f"\n  Polling run {run_id} for completion...")
        run_data = poll_run(run_id)
        failures = verify_run(run_id, run_data) if run_data else ["poll timed out"]
        final_status = run_data.get("run", {}).get("status", "timeout") if run_data else "timeout"

        results.append({
            "label": label,
            "run_id": run_id,
            "plan_id": plan_id,
            "final_status": final_status,
            "failures": failures,
        })

    report = generate_report(results)
    print(f"\n{'='*60}")
    print(report)
    print(f"{'='*60}")

    report_path = "data/smoke-test-report-a2.md"
    os.makedirs("data", exist_ok=True)
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\nReport saved to {report_path}")

    any_fail = any(r.get("failures") for r in results)
    return 2 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
